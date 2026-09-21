"""AttendanceService — tầng domain chấm công (attendance-spec FR-2…FR-6).

Nhiệm vụ:
  1. ``on_event(person_id, detected_at)``: nhận sự kiện nhận diện đã ghi —
     ánh xạ về ngày công (work_date) rồi ghi/cập nhật ``attendance_days``:
     sự kiện ĐẦU TIÊN trong ngày = check-in, cuối cùng = check-out.
  2. Ánh xạ event → work_date theo ca: đổi detected_at (UTC) sang giờ ĐỊA
     PHƯƠNG; sự kiện trong cửa sổ ca bắt đầu ngày D (ca đêm: kéo đến giờ ra
     ngày D+1) thuộc ngày D — work_date = ngày BẮT ĐẦU CA.
  3. Tổng hợp tháng (bảng công FR-7) + trạng thái hiển thị (FR-4): đi muộn
     tính lúc HIỂN THỊ từ check_in + ca (không lưu cột — tránh lệch khi sửa ca).
  4. Sửa tay (FR-5): đổi giờ / trạng thái / ghi chú — mọi thay đổi ghi
     ``attendance_audit`` (giá trị cũ → mới).
  5. Khởi tạo lần đầu: ca trống → tự tạo ca "Hành chính" 08:00–17:00 làm
     mặc định (FR-3). Ca mặc định lưu bảng settings (key-value local).

QUAN TRỌNG (đa luồng — giống RecognitionService): chỉ gọi từ LUỒNG UI,
sau khi UI đã ghi sự kiện nhận diện; worker camera không đụng DB.

Không làm gì phá luồng nhận diện hiện có: sự kiện cũ KHÔNG ghép ngược —
ngày công chỉ hình thành từ sự kiện gọi on_event sau khi nâng cấp (FR-11).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import Config
from app.infrastructure.db import Database
from app.infrastructure.repositories import (
    AttendanceAuditRepository,
    AttendanceDay,
    AttendanceDayRepository,
    PersonRepository,
    Shift,
    ShiftRepository,
)

logger = logging.getLogger(__name__)

# Tên ca mặc định tự tạo lần đầu (attendance-spec 4.5)
DEFAULT_SHIFT_NAME = "Hành chính"
DEFAULT_SHIFT_START = "08:00"
DEFAULT_SHIFT_END = "17:00"
DEFAULT_SHIFT_GRACE = 10
DEFAULT_SHIFT_FACTOR = 1.0

# Key bảng settings local — id ca mặc định (người chưa gán ca dùng ca này)
KEY_DEFAULT_SHIFT = "attendance_default_shift"

# Trạng thái ngày công (FR-1/FR-4)
STATUS_AUTO = "auto"      # có mặt (tự sinh từ nhận diện)
STATUS_LEAVE = "leave"    # nghỉ phép
STATUS_TRIP = "trip"      # công tác
STATUS_MANUAL = "manual"  # đã sửa tay

# Nhãn hiển thị trạng thái (FR-6 copy)
STATUS_LABELS: dict[str, str] = {
    STATUS_AUTO: "Có mặt",
    STATUS_LEAVE: "Nghỉ phép",
    STATUS_TRIP: "Công tác",
    STATUS_MANUAL: "Đã sửa tay",
}


@dataclass
class DayView:
    """Trạng thái HIỂN THỊ của 1 ngày công — mọi thứ tính sẵn cho UI.

    ``status`` là trạng thái DB; ``label`` là chuỗi hiển thị (Đi muộn X phút,
    Thiếu giờ ra, ...). ``is_workday`` = ngày làm việc theo config (FR-6).
    """

    day: AttendanceDay | None
    work_date: str
    status: str                     # auto/leave/trip/manual, hoặc 'absent' suy ra
    label: str                      # chuỗi tiếng Việt hiển thị
    late_minutes: int = 0           # >0 = đi muộn
    worked_minutes: int | None = None  # None = không tính được (thiếu giờ ra)
    missing_checkout: bool = False
    is_workday: bool = True


@dataclass
class PersonPayroll:
    """Lương thô 1 người trong tháng: số công × hệ số ca (mở rộng FR-8).

    ``factor`` là hệ số ca ÁP DỤNG lúc tính (gán riêng hoặc mặc định) —
    ngày công giữ shift_id của ngày đó nên tổng không đổi khi đổi ca sau.
    ``shift_totals``: tên ca → (số công, hệ số) — hiển thị cách bẻ số công.
    """

    person_id: str
    person_name: str
    shift_name: str                    # tên ca áp dụng hiện tại
    factor: float                      # hệ số ca áp dụng
    worked_days: int                   # ngày tính lương (auto/manual đủ hoặc thiếu giờ ra)
    gross_pay: float                   # = Σ worked_days của từng ca × hệ số ca đó
    pay_amount: int                    # gross_pay × đơn giá (VND) — 0 khi chưa đặt đơn giá
    leave_count: int
    trip_count: int
    absent_count: int                  # vắng mặt (ngày làm việc đã qua — KHÔNG lương)
    shift_totals: list[tuple[str, int, float]]  # (tên ca, số công, hệ số)


@dataclass
class PersonMonthSummary:
    """Tổng hợp 1 người trong tháng (header dòng bảng tháng — FR-7)."""

    person_id: str
    person_name: str
    shift_name: str                 # tên ca (mặc định nếu chưa gán)
    worked_days: int                # số công
    late_count: int
    missing_checkout_count: int
    leave_count: int
    trip_count: int
    days: dict[str, DayView]        # 'YYYY-MM-DD' → DayView


@dataclass
class PersonTodayStatus:
    """Trạng thái chấm công HÔM NAY của 1 người (Dashboard — mở app là thấy).

    ``status``/``label`` là trạng thái hôm nay (suy ra 'Vắng mặt' cho ngày
    làm việc ĐÃ QUA; ngày hôm nay chưa chấm chỉ là 'Chưa chấm').
    """

    person_id: str
    person_name: str
    shift_name: str                    # tên ca áp dụng (mặc định nếu chưa gán)
    status: str                        # auto/leave/trip/manual/absent/''
    label: str                         # chuỗi tiếng Việt hiển thị
    check_in_local: str | None = None  # 'HH:MM' giờ vào (địa phương)
    check_out_local: str | None = None # 'HH:MM' giờ ra (địa phương)
    late_minutes: int = 0
    missing_checkout: bool = False     # đã vào nhưng CHƯA có giờ ra
    is_workday: bool = True            # hôm nay có phải ngày làm việc


@dataclass
class TodayOverview:
    """Tổng quan chấm công hôm nay — dữ liệu cho thẻ Dashboard."""

    work_date: str                     # 'YYYY-MM-DD' hôm nay
    is_workday: bool                   # hôm nay có phải ngày làm việc
    total: int                         # số người đã đăng ký
    checked_in: int                    # đã có check-in hôm nay
    missing_checkout: int              # đã vào nhưng chưa ra (còn trong ca)
    late_count: int                    # đi muộn hôm nay
    leave_count: int                   # nghỉ phép hôm nay
    trip_count: int                    # công tác hôm nay
    not_checked_in: int                # chưa chấm (ngày làm việc — phải có mặt)
    rows: list[PersonTodayStatus]      # trạng thái từng người


def format_vnd(amount: int | float) -> str:
    """Số tiền → '12.500.000 đ' (dấu chấm phân cách nghìn, chuẩn Việt Nam)."""
    return f"{round(amount):,} đ".replace(",", ".")


class AttendanceService:
    """Điều phối chấm công: event → ngày công → tổng hợp → sửa tay."""

    def __init__(self, db: Database, config: Config | None = None) -> None:
        self._db = db
        # Config tùy chọn: on_event / sửa tay không cần config; config dùng
        # cho attendance_workdays + attendance_pay_rate khi TỔNG HỢP.
        # Không truyền → mặc định (T2–T6, đơn giá 0 = chưa đặt).
        self._config = config or Config()
        self._days = AttendanceDayRepository(db)
        self._shifts = ShiftRepository(db)
        self._people = PersonRepository(db)
        self._audit = AttendanceAuditRepository(db)
        self.ensure_default_shift()

    # -------------------------------------------------------------
    # Khởi tạo lần đầu (spec 4.5)
    # -------------------------------------------------------------
    def ensure_default_shift(self) -> None:
        """Bảng ca trống → tự tạo ca 'Hành chính' 08:00–17:00 làm mặc định.

        Chạy lại vô hại: đã có ca → thôi. Không ghi outbox (lần chạy đầu
        trên máy mới, sync đầy đủ sẽ đẩy người + ca đi sau — ca tạo ở đây
        vẫn cần outbox, giữ True).
        """
        if self._shifts.list_all():
            return
        shift = self._shifts.add(
            name=DEFAULT_SHIFT_NAME,
            start_time=DEFAULT_SHIFT_START,
            end_time=DEFAULT_SHIFT_END,
            grace_minutes=DEFAULT_SHIFT_GRACE,
            factor=DEFAULT_SHIFT_FACTOR,
        )
        self.set_default_shift_id(shift.id)
        logger.info("Đã tạo ca mặc định '%s' (%s–%s)", shift.name, shift.start_time, shift.end_time)

    # -------------------------------------------------------------
    # Ca làm việc (FR-3)
    # -------------------------------------------------------------
    def list_shifts(self) -> list[Shift]:
        """Toàn bộ ca (cho combo gán ca + bảng cấu hình)."""
        return self._shifts.list_all()

    def get_default_shift(self) -> Shift | None:
        """Ca mặc định (người chưa gán dùng ca này); None nếu chưa có."""
        shift_id = self._get_setting(KEY_DEFAULT_SHIFT)
        if shift_id:
            shift = self._shifts.get(shift_id)
            if shift is not None:
                return shift
        shifts = self._shifts.list_all()
        return shifts[0] if shifts else None

    def set_default_shift_id(self, shift_id: str) -> None:
        """Đặt ca mặc định (id lưu bảng settings local)."""
        self._set_setting(KEY_DEFAULT_SHIFT, shift_id)

    def create_shift(
        self, name: str, start: str, end: str, grace: int = 10, factor: float = 1.0
    ) -> Shift:
        """Tạo ca mới; chặn tên trùng."""
        if self._shifts.get_by_name(name) is not None:
            raise ValueError(f"Đã tồn tại ca tên '{name.strip()}'")
        return self._shifts.add(
            name=name, start_time=start, end_time=end,
            grace_minutes=grace, factor=factor,
        )

    def update_shift(
        self, shift_id: str, name: str, start: str, end: str,
        grace: int = 10, factor: float = 1.0,
    ) -> bool:
        """Sửa ca; ngày công đã tính giữ shift_id cũ (FR-3) — lương tháng cũ không đổi."""
        return self._shifts.update(shift_id, name, start, end, grace, factor)

    def delete_shift(self, shift_id: str) -> bool:
        """Xóa ca; người gán ca này về ca mặc định (FK ON DELETE SET NULL)."""
        if self._get_setting(KEY_DEFAULT_SHIFT) == shift_id:
            raise ValueError("Không xóa được ca đang là ca mặc định — đặt ca khác làm mặc định trước")
        return self._shifts.delete(shift_id)

    def assign_shift(self, person_id: str, shift_id: str | None) -> bool:
        """Gán ca cho người; None = về ca mặc định."""
        return self._people.set_shift(person_id, shift_id)

    def shift_of(self, person_id: str) -> Shift | None:
        """Ca áp dụng cho người: ca gán riêng, không có → ca mặc định."""
        person = self._people.get(person_id)
        if person is not None and person.shift_id:
            shift = self._shifts.get(person.shift_id)
            if shift is not None:
                return shift
        return self.get_default_shift()

    # -------------------------------------------------------------
    # Ghép sự kiện → ngày công (FR-2) — lõi của module
    # -------------------------------------------------------------
    def on_event(self, person_id: str, detected_at: str | None = None) -> None:
        """Cập nhật ngày công từ 1 sự kiện nhận diện ĐÃ GHI.

        Gọi từ UI ngay sau khi ``save_event``/pull sync thành công với
        người ĐÃ ĐĂNG KÝ (person_id khác None). Người lạ bỏ qua.
        Lỗi KHÔNG ném ra ngoài — chấm công không được làm sập luồng nhận diện.
        """
        try:
            self._on_event_impl(person_id, detected_at)
        except Exception:  # noqa: BLE001 — chấm công lỗi mềm, không chặn nhận diện
            logger.exception("Cập nhật ngày công thất bại (person=%s)", person_id)

    def _on_event_impl(self, person_id: str, detected_at: str | None) -> None:
        if not person_id:
            return
        dt_utc = _parse_iso_utc(detected_at) if detected_at else datetime.now(timezone.utc)
        if dt_utc is None:
            logger.warning("detected_at không đọc được: %r — bỏ qua", detected_at)
            return

        work_date, _shift = self._resolve_work_date(person_id, dt_utc)
        if work_date is None:
            return  # chưa có ca nào (bảng ca rỗng do chỉnh tay DB)

        existing = self._days.find(person_id, work_date)
        if existing is not None:
            if existing.manual_override:
                logger.debug("Ngày %s đã sửa tay — không ghi đè (FR-5)", work_date)
                return
            # Lần sau trong ngày: chỉ cập nhật check_out nếu MỚI HƠN check_in.
            # check_in giữ nguyên (sự kiện đầu = check-in — FR-2).
            self._days.upsert_auto(
                person_id=person_id,
                work_date=work_date,
                check_in_at=existing.check_in_at or _to_iso(dt_utc),
                check_out_at=_to_iso(dt_utc),
                shift_id=existing.shift_id,
            )
            return

        # Lần đầu trong ngày → tạo dòng: sự kiện này = check-in
        shift = self.shift_of(person_id)
        self._days.upsert_auto(
            person_id=person_id,
            work_date=work_date,
            check_in_at=_to_iso(dt_utc),
            check_out_at=None,  # chưa có giờ ra → "Thiếu giờ ra" (FR-4)
            shift_id=shift.id if shift else None,
        )
        logger.debug("Đã ghi check-in %s ngày %s", person_id, work_date)

    def _resolve_work_date(
        self, person_id: str, dt_utc: datetime
    ) -> tuple[str | None, Shift | None]:
        """Ánh xạ thời điểm (UTC) → (work_date, ca áp dụng).

        Đổi sang giờ ĐỊA PHƯƠNG, tìm cửa sổ ca chứa thời điểm đó:
          - Ca thường 08:00–17:00: cửa sổ = [08:00, 17:00] cùng ngày D.
          - Ca đêm 22:00–06:00: cửa sổ = [22:00 ngày D → 06:00 ngày D+1];
            sự kiện 01:00 sáng thuộc ngày D (ngày bắt đầu ca — FR-2).
        Không ca nào chứa → dùng ca mặc định của ngày hiện tại (đơn giản,
        đúng cho ca thường; ca đêm lệch giờ ngoài cửa sổ vẫn về đúng ngày).
        """
        shift = self.shift_of(person_id)
        if shift is None:
            return None, None
        local_dt = dt_utc.astimezone()
        start_h, start_m = _parse_hhmm(shift.start_time)
        end_h, end_m = _parse_hhmm(shift.end_time)
        shift_start = local_dt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        shift_end = local_dt.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if shift_end <= shift_start:
            shift_end += timedelta(days=1)  # ca đêm — cửa sổ kéo qua nửa đêm

        # Thử cửa sổ ca NGÀY HÔM NAY; trước giờ vào → thử cửa sổ HÔM QUA
        # (sự kiện 00:30 sau ca đêm bắt đầu 22:00 hôm qua thuộc ngày hôm qua).
        for anchor in (local_dt, local_dt - timedelta(days=1)):
            window_start = anchor.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            window_end = window_start + (shift_end - shift_start)
            if window_start <= local_dt <= window_end:
                return anchor.strftime("%Y-%m-%d"), shift
        # Ngoài cửa sổ (làm ngoài giờ) → tính theo ngày hiện tại
        return local_dt.strftime("%Y-%m-%d"), shift

    # -------------------------------------------------------------
    # Tổng hợp tháng + trạng thái hiển thị (FR-4 / FR-7)
    # -------------------------------------------------------------
    def month_summary(self, year: int, month: int, person_id: str = "") -> list[PersonMonthSummary]:
        """Bảng công tháng: 1 dòng / người (mọi người hoặc 1 người).

        Trạng thái 'Vắng mặt' chỉ SUY RA lúc render cho ngày làm việc ĐÃ
        QUA không có dòng công — không tạo dòng DB (FR-4).
        """
        from app.infrastructure.repositories import Person

        people = (
            [self._people.get(person_id)] if person_id
            else self._people.list_all()
        )
        people = [p for p in people if p is not None]
        days_map: dict[str, dict[str, AttendanceDay]] = {}
        for day in self._days.list_month(year, month, person_id):
            days_map.setdefault(day.person_id, {})[day.work_date] = day

        today = datetime.now().date()
        summaries: list[PersonMonthSummary] = []
        for person in people:
            shift = self.shift_of(person.id)
            days: dict[str, DayView] = {}
            worked = late = missing = leave = trip = 0
            for date in _dates_in_month(year, month):
                key = date.isoformat()
                record = days_map.get(person.id, {}).get(key)
                view = self._build_day_view(record, date, shift)
                days[key] = view
                if view.status == "absent":
                    continue
                if view.missing_checkout:
                    missing += 1
                if view.late_minutes > 0:
                    late += 1
                if view.status == STATUS_LEAVE:
                    leave += 1
                elif view.status == STATUS_TRIP:
                    trip += 1
                elif view.status in (STATUS_AUTO, STATUS_MANUAL):
                    worked += 1  # thiếu giờ ra vẫn tính công (FR-4)
            summaries.append(
                PersonMonthSummary(
                    person_id=person.id,
                    person_name=person.name,
                    shift_name=shift.name if shift else "—",
                    worked_days=worked,
                    late_count=late,
                    missing_checkout_count=missing,
                    leave_count=leave,
                    trip_count=trip,
                    days=days,
                )
            )
        return summaries

    # -------------------------------------------------------------
    # Tổng quan hôm nay — Dashboard (mở app là thấy)
    # -------------------------------------------------------------
    def today_overview(self) -> TodayOverview:
        """Tổng quan chấm công HÔM NAY: ai đã vào / chưa ra / muộn / vắng.

        Tái dùng ``_build_day_view`` cho đúng ngữ nghĩa trạng thái FR-4.
        Vắng mặt chỉ suy ra cho ngày làm việc ĐÃ QUA — ngày hôm nay chưa
        chấm chỉ là 'Chưa chấm' (còn cả ngày để vào).
        """
        today = datetime.now().date()
        people = self._people.list_all()
        rows: list[PersonTodayStatus] = []
        is_workday = today.weekday() in self._config.attendance_workdays
        for person in people:
            shift = self.shift_of(person.id)
            record = self._days.find(person.id, today.isoformat())
            if record is None:
                # Hôm nay chưa chấm — KHÔNG dùng nhãn 'Vắng mặt' của
                # _build_day_view (nhãn đó chỉ dành cho ngày ĐÃ QUA — FR-4)
                rows.append(
                    PersonTodayStatus(
                        person_id=person.id,
                        person_name=person.name,
                        shift_name=shift.name if shift else "—",
                        status="",
                        label="Ngày nghỉ" if not is_workday else "Chưa chấm",
                        is_workday=is_workday,
                    )
                )
                continue
            view = self._build_day_view(record, today, shift)
            rows.append(
                PersonTodayStatus(
                    person_id=person.id,
                    person_name=person.name,
                    shift_name=shift.name if shift else "—",
                    status=view.status,
                    label=view.label,
                    check_in_local=_fmt_local_hhmm(record.check_in_at),
                    check_out_local=(
                        _fmt_local_hhmm(record.check_out_at)
                        if record.check_out_at else None
                    ),
                    late_minutes=view.late_minutes,
                    missing_checkout=view.missing_checkout,
                    is_workday=is_workday,
                )
            )
        return TodayOverview(
            work_date=today.isoformat(),
            is_workday=is_workday,
            total=len(rows),
            checked_in=sum(1 for r in rows if r.check_in_local),
            missing_checkout=sum(1 for r in rows if r.missing_checkout),
            late_count=sum(1 for r in rows if r.late_minutes > 0),
            leave_count=sum(1 for r in rows if r.status == STATUS_LEAVE),
            trip_count=sum(1 for r in rows if r.status == STATUS_TRIP),
            not_checked_in=(
                sum(1 for r in rows if not r.check_in_local) if is_workday else 0
            ),
            rows=rows,
        )

    # -------------------------------------------------------------
    # Lương thô (mở rộng FR-8): số công × hệ số ca
    # -------------------------------------------------------------
    def payroll_summary(self, year: int, month: int) -> list[PersonPayroll]:
        """Bảng lương thô tháng: 1 dòng / người — số công × hệ số ca.

        Đếm theo SHIFT_ID ĐÃ LƯU trên từng ngày công (không theo ca hiện
        tại của người) — đúng nguyên tắc FR-3: ngày đã tính giữ nguyên ca.
        Ngày tính lương: ``auto``/``manual`` có check_in (kể cả thiếu giờ
        ra, kể cả đi muộn). Nghỉ phép/công tác KHÔNG tính lương (không đi
        làm); vắng mặt cũng vậy. ``shift_totals`` bẻ số công theo ca cho
        dòng chi tiết của báo cáo.
        """
        summaries = self.month_summary(year, month)
        shift_cache: dict[str, Shift | None] = {}

        def _shift_by_id(shift_id: str | None) -> Shift | None:
            if shift_id not in shift_cache:
                shift_cache[shift_id] = (
                    self._shifts.get(shift_id) if shift_id else None
                )
            return shift_cache[shift_id]

        result: list[PersonPayroll] = []
        pay_rate = max(0, int(getattr(self._config, "attendance_pay_rate", 0) or 0))
        for summary in summaries:
            per_shift: dict[str, int] = {}  # shift_id → số công
            paid_days = 0
            for view in summary.days.values():
                if view.status not in (STATUS_AUTO, STATUS_MANUAL):
                    continue  # phép/công tác/vắng/không có bản ghi — không lương
                if view.day is None or not view.day.check_in_at:
                    continue  # phải có check-in mới tính công
                paid_days += 1
                sid = view.day.shift_id
                per_shift[sid] = per_shift.get(sid, 0) + 1

            shift_totals: list[tuple[str, int, float]] = []
            gross = 0.0
            for sid, count in per_shift.items():
                shift = _shift_by_id(sid)
                factor = shift.factor if shift else 1.0
                name = shift.name if shift else "—"
                shift_totals.append((name, count, factor))
                gross += count * factor
            shift_totals.sort(key=lambda item: item[0])

            result.append(
                PersonPayroll(
                    person_id=summary.person_id,
                    person_name=summary.person_name,
                    shift_name=summary.shift_name,
                    factor=self._factor_of(summary.person_id),
                    worked_days=paid_days,
                    gross_pay=round(gross, 2),
                    pay_amount=round(gross * pay_rate),
                    leave_count=summary.leave_count,
                    trip_count=summary.trip_count,
                    absent_count=sum(
                        1 for v in summary.days.values() if v.status == "absent"
                    ),
                    shift_totals=shift_totals,
                )
            )
        return result

    def _factor_of(self, person_id: str) -> float:
        """Hệ số ca áp dụng HIỆN TẠI cho người (gán riêng → ca mặc định)."""
        shift = self.shift_of(person_id)
        return shift.factor if shift else 1.0

    def _build_day_view(
        self, record: AttendanceDay | None, date, shift: Shift | None
    ) -> DayView:
        """Dựng DayView hiển thị cho 1 ô trong bảng tháng (FR-4)."""
        workday = date.weekday() in self._config.attendance_workdays
        past = date < datetime.now().date()

        if record is None:
            # Không có dòng công: vắng mặt CHỈ khi ngày làm việc đã qua
            if workday and past:
                return DayView(day=None, work_date=date.isoformat(), status="absent",
                               label="Vắng mặt", is_workday=workday)
            return DayView(day=None, work_date=date.isoformat(), status="", label="—",
                           is_workday=workday)

        status = record.status
        if status == STATUS_LEAVE:
            return DayView(day=record, work_date=record.work_date, status=status,
                           label=STATUS_LABELS[status], is_workday=workday)
        if status == STATUS_TRIP:
            return DayView(day=record, work_date=record.work_date, status=status,
                           label=STATUS_LABELS[status], is_workday=workday)

        late_minutes = self.late_minutes(record, shift)
        missing = record.check_in_at and not record.check_out_at
        if missing:
            label = f"Đi muộn {late_minutes} phút, thiếu giờ ra" if late_minutes > 0 else "Thiếu giờ ra"
        elif late_minutes > 0:
            label = f"Đi muộn {late_minutes} phút"
        else:
            label = STATUS_LABELS.get(status, status)
        worked = self.worked_minutes(record)
        suffix = f" ({worked // 60}h{worked % 60:02d})" if worked is not None else ""
        return DayView(
            day=record,
            work_date=record.work_date,
            status=status,
            label=label + suffix,
            late_minutes=late_minutes,
            worked_minutes=worked,
            missing_checkout=bool(missing),
            is_workday=workday,
        )

    # -------------------------------------------------------------
    # Tính toán hiển thị (FR-4)
    # -------------------------------------------------------------
    def late_minutes(self, record: AttendanceDay, shift: Shift | None) -> int:
        """Số phút đi muộn so với ca (dung sai grace_minutes); <=0 → 0.

        So giờ ĐỊA PHƯƠNG của check_in với giờ vào ca CỦA NGÀY work_date.
        Ca đêm: nếu check_in rơi trước nửa đêm (như 23:50) so với start
        22:00 cùng ngày — window từ ngày work_date.
        """
        if not record.check_in_at or shift is None:
            return 0
        check_in = _parse_iso_utc(record.check_in_at)
        if check_in is None:
            return 0
        local_in = check_in.astimezone()
        start_h, start_m = _parse_hhmm(shift.start_time)
        year, month, day = (int(p) for p in record.work_date.split("-"))
        shift_start = datetime(year, month, day, start_h, start_m).astimezone()
        grace = max(0, shift.grace_minutes)
        delta = (local_in - shift_start).total_seconds() / 60.0 - grace
        return max(0, round(delta))

    def worked_minutes(self, record: AttendanceDay) -> int | None:
        """Số giờ làm (phút) = check_out − check_in; None khi thiếu giờ ra."""
        if not record.check_in_at or not record.check_out_at:
            return None
        start = _parse_iso_utc(record.check_in_at)
        end = _parse_iso_utc(record.check_out_at)
        if start is None or end is None:
            return None
        delta = end - start
        return max(0, round(delta.total_seconds() / 60.0))

    # -------------------------------------------------------------
    # Sửa tay + audit (FR-5) — UI gọi sau khi PasswordDialog.require ĐÚNG
    # -------------------------------------------------------------
    def edit_times(
        self,
        day_id: str,
        check_in_local: str | None,
        check_out_local: str | None,
    ) -> AttendanceDay:
        """Sửa giờ vào/ra (nhập 'HH:MM' giờ địa phương) + ghi audit 'edit_time'.

        ``None`` = giữ nguyên. Chuỗi rỗng '' = xóa mốc. Giờ địa phương đổi
        UTC theo ``work_date`` khi lưu.
        """
        record = self._require_day(day_id)
        changes: list[tuple[str, str, str]] = []
        new_in = record.check_in_at
        new_out = record.check_out_at
        if check_in_local is not None:
            old_label = _fmt_local_hhmm(record.check_in_at)
            new_in = _local_hhmm_to_iso(check_in_local, record.work_date)
            changes.append(("edit_time", f"vào {old_label}", f"vào {check_in_local}"))
        if check_out_local is not None:
            old_label = _fmt_local_hhmm(record.check_out_at)
            new_out = _local_hhmm_to_iso(check_out_local, record.work_date)
            changes.append(("edit_time", f"ra {old_label}", f"ra {check_out_local}"))
        if not changes:
            return record

        record.check_in_at = new_in
        record.check_out_at = new_out
        record.manual_override = True
        record.status = STATUS_MANUAL if record.status == STATUS_AUTO else record.status
        saved = self._days.save(record)
        for action, old, new in changes:
            self._audit.add(day_id, action, old_value=old, new_value=new,
                            detail="Sửa giờ bằng tay")
        return saved

    def set_status(self, day_id: str, status: str) -> AttendanceDay:
        """Đổi trạng thái ngày (leave/trip/manual/auto) + ghi audit 'set_status'."""
        if status not in (STATUS_AUTO, STATUS_LEAVE, STATUS_TRIP, STATUS_MANUAL):
            raise ValueError(f"Trạng thái không hợp lệ: {status}")
        record = self._require_day(day_id)
        old = record.status
        if old == status:
            return record
        record.status = status
        record.manual_override = True
        saved = self._days.save(record)
        self._audit.add(
            day_id, "set_status",
            old_value=STATUS_LABELS.get(old, old),
            new_value=STATUS_LABELS.get(status, status),
            detail="Đổi trạng thái bằng tay",
        )
        return saved

    def set_note(self, day_id: str, note: str) -> AttendanceDay:
        """Ghi chú ngày công + ghi audit 'add_note'."""
        record = self._require_day(day_id)
        old = record.note
        record.note = note.strip()
        if old == record.note:
            return record
        record.manual_override = True
        saved = self._days.save(record)
        self._audit.add(day_id, "add_note", old_value=old, new_value=record.note,
                        detail="Ghi chú bằng tay")
        return saved

    def mark_leave(self, person_id: str, work_date: str, status: str = STATUS_LEAVE) -> AttendanceDay:
        """Đánh nghỉ phép / công tác cho ngày CHƯA có dòng công (FR-5).

        Tạo dòng công mới (không có giờ vào/ra) — ngày đó không bị đánh vắng.
        """
        if status not in (STATUS_LEAVE, STATUS_TRIP):
            raise ValueError("Chỉ đánh được nghỉ phép hoặc công tác")
        existing = self._days.find(person_id, work_date)
        if existing is not None:
            return self.set_status(existing.id, status)
        shift = self.shift_of(person_id)
        record = AttendanceDay(
            id=uuid.uuid4().hex,
            person_id=person_id,
            work_date=work_date,
            shift_id=shift.id if shift else None,
            status=status,
            manual_override=True,
        )
        saved = self._days.add_external(record)
        self._audit.add(
            saved.id, "set_status",
            old_value="Vắng mặt",
            new_value=STATUS_LABELS[status],
            detail="Đánh bằng tay cho ngày chưa chấm",
        )
        return saved

    def audit_for_day(self, day_id: str) -> list[dict[str, str]]:
        """Lịch sử audit của 1 ngày (mới nhất trước) — hiển thị trong chi tiết."""
        return self._audit.list_for_day(day_id)

    def _require_day(self, day_id: str) -> AttendanceDay:
        record = self._days.get(day_id)
        if record is None:
            raise ValueError(f"Bản ghi công không tồn tại: {day_id}")
        return record

    # -------------------------------------------------------------
    # Bảng settings key-value (giống SyncService — lưu ca mặc định)
    # -------------------------------------------------------------
    def _get_setting(self, key: str) -> str | None:
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def _set_setting(self, key: str, value: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


# ---------------------------------------------------------------------------
# Hàm phụ — thời gian
# ---------------------------------------------------------------------------

def _parse_iso_utc(value: str) -> datetime | None:
    """Đọc chuỗi ISO UTC (hậu tố 'Z' hoặc không) → datetime UTC; None nếu sai."""
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def _to_iso(dt: datetime) -> str:
    """datetime → chuỗi ISO UTC có 'Z' (khớp định dạng detected_at hiện có)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_hhmm(value: str) -> tuple[int, int]:
    """'HH:MM' → (giờ, phút); sai → ValueError với thông điệp thân thiện."""
    try:
        hours, minutes = value.strip().split(":")
        if len(hours) != 2 or len(minutes) != 2:
            raise ValueError
        if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
            raise ValueError
        return int(hours), int(minutes)
    except (ValueError, AttributeError):
        raise ValueError(f"Giờ không hợp lệ (cần dạng HH:MM): {value!r}") from None


def _fmt_local_hhmm(iso_utc: str | None) -> str:
    """ISO UTC → 'HH:MM' giờ địa phương (hiển thị giá trị cũ khi audit)."""
    dt = _parse_iso_utc(iso_utc) if iso_utc else None
    return dt.astimezone().strftime("%H:%M") if dt else "—"


def _local_hhmm_to_iso(hhmm: str, work_date: str) -> str | None:
    """'HH:MM' giờ địa phương của ngày work_date → ISO UTC.

    Chuỗi rỗng → None (xóa mốc). Sai định dạng → ValueError (UI báo lỗi,
    không lưu).
    """
    hhmm = hhmm.strip()
    if not hhmm:
        return None
    hours, minutes = _parse_hhmm(hhmm)
    year, month, day = (int(p) for p in work_date.split("-"))
    local = datetime(year, month, day, hours, minutes).astimezone()
    return _to_iso(local)


def _dates_in_month(year: int, month: int) -> list:
    """Danh sách ngày (date) trong tháng — dùng cho bảng tháng FR-7."""
    from datetime import date as date_cls

    start = date_cls(year, month, 1)
    next_month = date_cls(year + 1, 1, 1) if month == 12 else date_cls(year, month + 1, 1)
    days = (next_month - start).days
    return [start + timedelta(days=i) for i in range(days)]
