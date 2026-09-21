"""Kiểm tra LƯƠNG THÔ (số công × hệ số ca) + migration schema v3 — không mạng.

1. Schema v3: DB mới có cột shifts.factor; DB v2 CŨ nâng cấp giữ nguyên dữ liệu.
2. Service: payroll_summary đếm đúng số công theo shift_id ĐÃ LƯU từng ngày
   (đổi ca sau không ảnh hưởng tháng cũ), nghỉ phép/công tác/vắng không lương.
3. Lương = Σ (số công mỗi ca × hệ số ca đó); ca bị xóa → hệ số 1.0.
4. Repository: Shift.factor CRUD + validate; outbox 'shift' đẩy factor lên D1.

Chạy:  .venv\\Scripts\\python.exe scripts/test_payroll.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.infrastructure.db import Database  # noqa: E402
from app.infrastructure.repositories import (  # noqa: E402
    AttendanceDay,
    ShiftRepository,
)
from app.services.attendance import AttendanceService  # noqa: E402

TEMP_DB = PROJECT_ROOT / "data" / "test_payroll.db"
OLD_DB = PROJECT_ROOT / "data" / "test_payroll_v2.db"

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [QUA] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [THẤT] {name} {detail}")


def cleanup() -> None:
    for path in (TEMP_DB, OLD_DB):
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)


def iso_local(h: int, mi: int, day_offset: int = 0) -> str:
    now = datetime.now()
    return (
        datetime(now.year, now.month, now.day + day_offset, h, mi)
        .astimezone()
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def main() -> int:
    cleanup()

    # ---- Nhóm 1: schema v3 + migration từ v2 cũ ----
    print("== Nhóm 1: Schema v3 + migration v2→v3 ==")
    # DB v2 THẬT (không có cột factor) — dựng bằng SQL trực tiếp
    conn = sqlite3.connect(OLD_DB)
    conn.execute("PRAGMA user_version = 2")
    conn.executescript(
        """
        CREATE TABLE persons (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT,
            thumbnail_path TEXT NOT NULL, thumbnail_r2_key TEXT, shift_id TEXT
        );
        CREATE TABLE shifts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, start_time TEXT NOT NULL,
            end_time TEXT NOT NULL, grace_minutes INTEGER NOT NULL DEFAULT 10,
            created_at TEXT
        );
        INSERT INTO shifts (id, name, start_time, end_time, created_at)
            VALUES ('s1', 'Ca cũ', '08:00', '17:00', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    db = Database(OLD_DB)
    shifts = ShiftRepository(db)
    old_shift = shifts.get("s1")
    check("DB v2 mở lên không crash (user_version=3)",
          db._conn.execute("PRAGMA user_version").fetchone()[0] == 3)
    check("Ca cũ giữ nguyên dữ liệu", old_shift is not None and old_shift.name == "Ca cũ")
    check("Ca cũ nhận hệ số mặc định 1.0", old_shift is not None and old_shift.factor == 1.0)
    db.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(OLD_DB) + suffix).unlink(missing_ok=True)

    # ---- Nhóm 2: dữ liệu + payroll ----
    print("== Nhóm 2: AttendanceService.payroll_summary ==")
    db = Database(TEMP_DB)
    service = AttendanceService(db)
    shifts = ShiftRepository(db)

    from app.infrastructure.repositories import PersonRepository
    people = PersonRepository(db)

    hanh_chinh = service.get_default_shift()  # Hành chính 08:00–17:00 (factor 1.0)
    dem = shifts.add(name="Ca đêm", start_time="22:00", end_time="06:00", factor=1.5)

    mai = people.add("Nguyễn Mai", "")
    lan = people.add("Trần Lan", "")
    service.assign_shift(mai.id, dem.id)  # Mai gán ca đêm 1.5

    # Mai: 4 công ca đêm (hệ số 1.5) + 1 phép + 1 công tác
    for offset in (1, 2, 3, 4):
        service.on_event(mai.id, iso_local(23, 0, day_offset=-offset))
    for offset in (5, 6):
        service.mark_leave(mai.id, iso_local(8, 0, day_offset=-offset)[:10],
                           "leave" if offset == 5 else "trip")
    # Lan (Hành chính): 3 công ca thường + 2 ngày nghỉ chủ động bỏ trống (vắng)
    for offset in (1, 2, 3):
        service.on_event(lan.id, iso_local(8, 5, day_offset=-offset))

    payroll = {p.person_name: p for p in service.payroll_summary(
        datetime.now().year, datetime.now().month)}
    mai_pay, lan_pay = payroll["Nguyễn Mai"], payroll["Trần Lan"]

    check("Mai 4 công lương (phép+công tác không lương)", mai_pay.worked_days == 4)
    check("Mai lương = 4 × 1.5 = 6.0", mai_pay.gross_pay == 6.0, f"(={mai_pay.gross_pay})")
    check("Chưa đặt đơn giá → tiền = 0", mai_pay.pay_amount == 0,
          f"(={mai_pay.pay_amount})")
    check("Mai hệ số ca áp dụng = 1.5", mai_pay.factor == 1.5)
    check("Mai có 1 phép + 1 công tác",
          mai_pay.leave_count == 1 and mai_pay.trip_count == 1)
    check("Chi tiết Mai bẻ theo ca: ('Ca đêm', 4, 1.5)",
          mai_pay.shift_totals == [("Ca đêm", 4, 1.5)], f"({mai_pay.shift_totals})")
    check("Lan 3 công", lan_pay.worked_days == 3)
    check("Lan lương = 3 × 1.0 = 3.0", lan_pay.gross_pay == 3.0, f"(={lan_pay.gross_pay})")
    # Ngày vắng = MỌI ngày làm việc đã qua trong tháng không có bản ghi
    # (Lan chỉ có bản ghi 3 ngày gần nhất) — theo config workdays
    from app.config import Config
    from datetime import date as date_cls
    workdays = set(Config().attendance_workdays)
    today = datetime.now().date()
    worked_dates = {today - timedelta(days=off) for off in (1, 2, 3)}
    month_start = today.replace(day=1)
    expected_absent = sum(
        1 for i in range((today - month_start).days)
        if (d := month_start + timedelta(days=i)).weekday() in workdays
        and d not in worked_dates
    )
    check("Lan vắng đúng mọi ngày làm việc không có bản ghi",
          lan_pay.absent_count == expected_absent,
          f"(={lan_pay.absent_count}, dự kiến {expected_absent})")

    # ---- Nhóm 3: đổi ca sau không đổi lương tháng cũ; xóa ca → hệ số 1.0 ----
    print("== Nhóm 3: Nguyên tắc 'ngày đã tính giữ nguyên ca' ==")
    shifts.update(dem.id, name="Ca đêm", start_time="22:00", end_time="06:00",
                  grace_minutes=10, factor=2.0)
    payroll = {p.person_name: p for p in service.payroll_summary(
        datetime.now().year, datetime.now().month)}
    check("Sửa hệ số ca → lương tháng hiện tại theo hệ số mới (4 × 2.0 = 8.0)",
          payroll["Nguyễn Mai"].gross_pay == 8.0,
          f"(={payroll['Nguyễn Mai'].gross_pay})")

    # Ngày công cũ lưu shift_id — xóa ca: ngày giữ số, hệ số quy về 1.0
    shifts.delete(dem.id)
    payroll = {p.person_name: p for p in service.payroll_summary(
        datetime.now().year, datetime.now().month)}
    check("Xóa ca → lương Mai = 4 × 1.0 (ngày vẫn giữ, ca mất tham chiếu)",
          payroll["Nguyễn Mai"].gross_pay == 4.0,
          f"(={payroll['Nguyễn Mai'].gross_pay})")
    check("Chi tiết ca mất tham chiếu hiện '—'",
          payroll["Nguyễn Mai"].shift_totals == [("—", 4, 1.0)])

    # ---- Nhóm 4: CRUD factor + outbox đẩy factor ----
    print("== Nhóm 4: Repository + outbox ==")
    ca_x2 = shifts.add(name="Ca ×2", start_time="09:00", end_time="18:00", factor=2.0)
    check("Tạo ca với hệ số 2.0", shifts.get(ca_x2.id).factor == 2.0)
    shifts.update(ca_x2.id, name="Ca ×2", start_time="09:00", end_time="18:00",
                  grace_minutes=10, factor=0.5)
    check("Sửa hệ số 0.5", shifts.get(ca_x2.id).factor == 0.5)
    # Hệ số vô lý KHÔNG ném — kẹp về 0.01 (giống grace_minutes kẹp >= 0)
    so_am = shifts.add(name="Số âm", start_time="09:00", end_time="18:00", factor=-1.0)
    check("Hệ số âm bị kẹp về 0.01", shifts.get(so_am.id).factor == 0.01,
          f"(={shifts.get(so_am.id).factor})")

    from app.infrastructure.repositories import SyncOutboxRepository
    from app.services.sync import _shift_upsert_stmt
    stmt, params = _shift_upsert_stmt(shifts.get(ca_x2.id))
    check("Outbox SQL upsert mang factor", "factor" in stmt and "0.5" in (params or []),
          f"({[p for p in (params or []) if '0.5' in str(p)]})")

    # ---- Nhóm 5: đơn giá tiền (attendance_pay_rate) ----
    print("== Nhóm 5: Lương ra tiền VND ==")
    from app.services.attendance import format_vnd

    check("format_vnd đúng dấu chấm nghìn",
          format_vnd(12500000) == "12.500.000 đ", f"({format_vnd(12500000)})")
    check("format_vnd làm tròn lẻ", format_vnd(1234.6) == "1.235 đ")

    config2 = type(service._config)()
    config2.attendance_pay_rate = 350_000
    service2 = AttendanceService(db, config2)
    # Lập lại dữ liệu cho service2 (cùng DB — người/ngày đã có sẵn)
    money = {p.person_name: p for p in service2.payroll_summary(
        datetime.now().year, datetime.now().month)}
    # Lưu ý: Nhóm 3 đã xóa ca đêm → công đêm của Mai mất tham chiếu ca,
    # hệ số quy về 1.0 → Mai 4.0 công quy đổi (không còn 6.0)
    check("Mai: 4.0 công quy đổi × 350.000 = 1.400.000",
          money["Nguyễn Mai"].pay_amount == 1_400_000,
          f"(={money['Nguyễn Mai'].pay_amount:,})")
    check("Lan: 3.0 × 350.000 = 1.050.000",
          money["Trần Lan"].pay_amount == 1_050_000,
          f"(={money['Trần Lan'].pay_amount:,})")

    # Đơn giá âm/None từ file config cũ → kẹp về 0 (config __post_init__)
    from app.config import Config as ConfigCls
    cfg_bad = ConfigCls(attendance_pay_rate=-5)
    check("Đơn giá âm bị kẹp về 0", cfg_bad.attendance_pay_rate == 0)

    db.close()
    cleanup()
    print(f"\nKẾT QUẢ: {PASS} qua / {FAIL} thất bại")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
