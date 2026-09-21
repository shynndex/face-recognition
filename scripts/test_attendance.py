"""Kiểm tra tầng dữ liệu CHẤM CÔNG (attendance-spec) — dùng DB TẠM, không đụng data/app.db.

Chạy:  .venv\\Scripts\\python.exe scripts\\test_attendance.py

Kiểm tra:
1. Schema v2: đủ 8 bảng + cột persons.shift_id + UNIQUE(person_id, work_date)
2. Ca làm việc: CRUD + validate HH:MM + chặn trùng tên + xóa ca mặc định bị chặn
3. on_event: sự kiện đầu = check-in, sự kiện sau = check-out; nhiều lần không sinh thêm dòng
4. Ánh xạ work_date theo ca: ca đêm 22:00–06:00 → sự kiện 01:00 sáng thuộc ngày bắt đầu ca
5. Trạng thái: đi muộn theo dung sai, thiếu giờ ra, nghỉ phép/công tác, vắng mặt (ngày quá khứ)
6. Sửa tay + audit: edit_times/set_status/set_note ghi log cũ→mới; manual_override chặn ghi đè
7. DB cũ v1 (chưa có bảng mới) mở bằng code mới → tự nâng cấp, dữ liệu cũ nguyên vẹn
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.infrastructure.db import Database  # noqa: E402
from app.infrastructure.repositories import (  # noqa: E402
    AttendanceAuditRepository,
    AttendanceDayRepository,
    PersonRepository,
    ShiftRepository,
)
from app.services.attendance import (  # noqa: E402
    STATUS_LABELS,
    AttendanceService,
)

TEMP_DB = PROJECT_ROOT / "data" / "test_attendance.db"

EXPECTED_TABLES = {
    "persons",
    "face_samples",
    "recognition_events",
    "sync_outbox",
    "settings",
    "shifts",
    "attendance_days",
    "attendance_audit",
}

passed = True


def check(label: str, ok: bool) -> None:
    global passed
    if ok:
        print(f"  ✓ {label}")
    else:
        passed = False
        print(f"  ✗ THẤT BẠI: {label}")


def cleanup() -> None:
    for name in (TEMP_DB.name, TEMP_DB.name + "-wal", TEMP_DB.name + "-shm"):
        Path(str(TEMP_DB.parent / name)).unlink(missing_ok=True)


def cleanup_old() -> None:
    """Xóa file DB v1 giả lập (không đụng TEMP_DB đang mở kết nối)."""
    old_path = PROJECT_ROOT / "data" / "test_attendance_old.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(old_path) + suffix).unlink(missing_ok=True)


def iso_local(y: int, mo: int, d: int, h: int, mi: int) -> str:
    """Thời điểm địa phương → ISO UTC (mô phỏng detected_at của sự kiện)."""
    return (
        datetime(y, mo, d, h, mi).astimezone()
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def main() -> None:
    cleanup()

    # DB tạm riêng (không đụng data/app.db); config mặc định T2–T6
    db = Database(TEMP_DB)
    config = Config()
    service = AttendanceService(db, config)
    people = PersonRepository(db)
    shifts = ShiftRepository(db)
    days = AttendanceDayRepository(db)

    # ---- 1) Schema v3 + ca mặc định tự tạo ----
    print("\n[1] Schema v3 + khởi tạo ca mặc định")
    with db.session() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        tables = {r["name"] for r in rows if not r["name"].startswith("sqlite_")}
        check("đủ 8 bảng", EXPECTED_TABLES.issubset(tables))
        check(
            "user_version = 3 (schema v3 — lương thô)",
            conn.execute("PRAGMA user_version").fetchone()[0] == 3,
        )
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(persons)").fetchall()
        }
        check("persons có cột shift_id", "shift_id" in cols)
        shift_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(shifts)").fetchall()
        }
        check("shifts có cột factor", "factor" in shift_cols)
        default_shift_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'attendance_default_shift'"
        ).fetchone()
    check("ca 'Hành chính' tự tạo làm mặc định", default_shift_row is not None)
    default_shift = shifts.get(default_shift_row["value"]) if default_shift_row else None
    check(
        "ca mặc định 08:00–17:00 dung sai 10",
        default_shift is not None
        and default_shift.start_time == "08:00"
        and default_shift.end_time == "17:00"
        and default_shift.grace_minutes == 10,
    )

    # ---- 2) Ca làm việc CRUD ----
    print("\n[2] Ca làm việc: CRUD + validate")
    night = service.create_shift("Ca đêm", "22:00", "06:00", 15)
    check("tạo ca đêm 22:00–06:00", night.end_time == "06:00")
    try:
        service.create_shift("Ca đêm", "22:00", "06:00")
        check("chặn trùng tên ca", False)
    except ValueError:
        check("chặn trùng tên ca", True)
    try:
        service.create_shift("Sai giờ", "8am", "17:00")
        check("chặn giờ sai định dạng", False)
    except ValueError:
        check("chặn giờ sai định dạng", True)
    check("sửa ca được", service.update_shift(night.id, "Ca đêm'", "23:00", "07:00", 20))
    check("sửa xong đúng dữ liệu", shifts.get(night.id).start_time == "23:00")
    service.update_shift(night.id, "Ca đêm", "22:00", "06:00", 15)
    try:
        service.delete_shift(default_shift.id)
        check("chặn xóa ca mặc định", False)
    except ValueError:
        check("chặn xóa ca mặc định", True)

    # ---- 3) on_event: check-in / check-out ----
    print("\n[3] on_event — ghép sự kiện thành ngày công")
    mai = people.add("Mai", "")
    today = datetime.now().date()
    y, mo, d = today.year, today.month, today.day
    service.on_event(mai.id, iso_local(y, mo, d, 8, 12))  # trễ 2 phút sau dung sai
    rec = days.find(mai.id, today.isoformat())
    check("sự kiện đầu tạo check-in", rec is not None and rec.check_in_at is not None)
    check("chưa có giờ ra → thiếu giờ ra", rec.check_out_at is None)
    service.on_event(mai.id, iso_local(y, mo, d, 17, 5))
    rec = days.find(mai.id, today.isoformat())
    check("sự kiện sau cập nhật check-out", rec.check_out_at is not None)
    service.on_event(mai.id, iso_local(y, mo, d, 17, 6))
    rec2 = days.find(mai.id, today.isoformat())
    check("vẫn 1 dòng/ngày (UNIQUE upsert)", rec2.id == rec.id)
    check("check_out mới hơn được giữ", rec2.check_out_at >= rec.check_out_at)
    check(
        "check_in không bị ghi đè",
        rec2.check_in_at == rec.check_in_at,
    )
    summary = service.month_summary(y, mo, mai.id)[0]
    view = summary.days[today.isoformat()]
    check("đi muộn 2 phút (08:12, dung sai 10)", view.late_minutes == 2)
    check(
        "số giờ làm ~ 8h53m",
        abs((view.worked_minutes or 0) - (8 * 60 + 53)) <= 1,
    )
    out_of_month = (today.replace(day=1) - timedelta(days=1)).month
    service.on_event(mai.id, iso_local(y, out_of_month, 15, 9, 0))
    other = service.month_summary(y, out_of_month, mai.id)[0]
    check(
        "ngày khác tháng tạo dòng riêng",
        other.days.get(f"{y}-{out_of_month:02d}-15") is not None
        and other.days[f"{y}-{out_of_month:02d}-15"].day is not None,
    )

    # ---- 4) Ánh xạ work_date theo ca đêm ----
    print("\n[4] Ca đêm — sự kiện sau nửa đêm thuộc ngày bắt đầu ca")
    lan = people.add("Lan", "")
    service.assign_shift(lan.id, night.id)
    check("gán ca theo người", service.shift_of(lan.id).id == night.id)
    # Sự kiện 01:00 sáng ngày D+1 → thuộc ca bắt đầu 22:00 ngày D
    service.on_event(lan.id, iso_local(y, mo, d, 1, 0))
    rec = days.find(lan.id, (today - timedelta(days=1)).isoformat())
    check(
        "01:00 sáng → work_date = hôm qua (ngày bắt đầu ca)",
        rec is not None and rec.work_date == (today - timedelta(days=1)).isoformat(),
    )
    service.on_event(lan.id, iso_local(y, mo, d, 5, 30))  # 05:30 — trong cửa sổ ca đêm
    rec = days.find(lan.id, (today - timedelta(days=1)).isoformat())
    check("05:30 sáng cùng cửa sổ ca → check-out", rec is not None and rec.check_out_at is not None)

    # ---- 5) Trạng thái hiển thị ----
    print("\n[5] Trạng thái: muộn / thiếu giờ ra / phép / vắng")
    hanh = people.add("Hành", "")
    service.on_event(hanh.id, iso_local(y, mo, d, 8, 30))  # muộn 20 phút
    view = service.month_summary(y, mo, hanh.id)[0].days[today.isoformat()]
    check("đi muộn 20 phút", view.late_minutes == 20 and "Đi muộn 20 phút" in view.label)
    check("thiếu giờ ra được cắm cờ", view.missing_checkout is True)
    # Ngày làm việc ĐÃ QUA không có dòng → vắng mặt (chọn ngày trong tháng này,
    # trước hôm nay, rơi vào T2–T6 theo workdays mặc định)
    absent_date = next(
        (
            today - timedelta(days=back)
            for back in range(1, min(today.day, 20))
            if (today - timedelta(days=back)).weekday() in config.attendance_workdays
        ),
        None,
    )
    if absent_date is not None:
        view = service.month_summary(y, mo, hanh.id)[0].days[absent_date.isoformat()]
        check("ngày làm việc quá khứ không chấm → Vắng mặt", view.status == "absent")
    else:
        check("ngày làm việc quá khứ không chấm → Vắng mặt", True)  # đầu tháng — bỏ qua

    # ---- 6) Sửa tay + audit + manual_override ----
    print("\n[6] Sửa tay + audit log")
    target = days.find(mai.id, today.isoformat())
    old_out = target.check_out_at
    saved = service.edit_times(target.id, None, "18:00")
    check("sửa giờ ra được", _local_hhmm(saved.check_out_at) == "18:00")
    check("manual_override bật", saved.manual_override is True)
    log = service.audit_for_day(target.id)
    check("audit có dòng edit_time", any(e["action"] == "edit_time" for e in log))
    check(
        "audit giữ giá trị cũ → mới (kèm nhãn vào/ra)",
        any(
            e["old_value"].endswith(_local_hhmm(old_out))
            and e["new_value"].endswith("18:00")
            for e in log
        ),
    )
    before = days.get(target.id)
    service.on_event(mai.id, iso_local(y, mo, d, 19, 0))
    after = days.get(target.id)
    check(
        "sự kiện sau KHÔNG ghi đè dòng đã sửa tay",
        after.check_out_at == before.check_out_at
        and after.check_in_at == before.check_in_at,
    )
    saved = service.set_status(target.id, "leave")
    check("đổi trạng thái nghỉ phép", saved.status == "leave")
    saved = service.set_note(target.id, "Ứng triệu ad-hoc cho khách VIP")
    check("ghi chú được", saved.note == "Ứng triệu ad-hoc cho khách VIP")
    log = service.audit_for_day(target.id)
    check(
        "audit đủ 3 loại",
        {e["action"] for e in log} >= {"edit_time", "set_status", "add_note"},
    )
    # Nghỉ phép cho ngày CHƯA chấm
    free_date = today + timedelta(days=1)
    service.mark_leave(mai.id, free_date.isoformat())
    rec = days.find(mai.id, free_date.isoformat())
    check("mark_leave tạo dòng nghỉ phép", rec is not None and rec.status == "leave")

    # ---- 7) DB cũ v1 tự nâng cấp ----
    print("\n[7] DB cũ schema v1 mở bằng code mới → tự nâng cấp")
    cleanup_old()
    old_path = PROJECT_ROOT / "data" / "test_attendance_old.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(old_path) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(str(old_path))
    # Schema v1 thu nhỏ: 5 bảng ĐÚNG NGUYÊN v1 (persons KHÔNG có shift_id,
    # sync_outbox có CHECK entity cũ) + 1 người + 1 outbox đang chờ sync.
    conn.executescript("""
CREATE TABLE IF NOT EXISTS persons (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    thumbnail_path   TEXT NOT NULL,
    thumbnail_r2_key TEXT
);
CREATE TABLE IF NOT EXISTS face_samples (
    id          TEXT PRIMARY KEY,
    person_id   TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,
    quality     REAL NOT NULL DEFAULT 0.0 CHECK (quality >= 0.0 AND quality <= 1.0),
    captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS recognition_events (
    id              TEXT PRIMARY KEY,
    person_id       TEXT REFERENCES persons(id) ON DELETE SET NULL,
    label           TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('webcam', 'photo', 'mobile')),
    detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    similarity      REAL,
    snapshot_path   TEXT,
    snapshot_r2_key TEXT,
    is_unknown      INTEGER NOT NULL DEFAULT 0 CHECK (is_unknown IN (0, 1))
);
CREATE TABLE IF NOT EXISTS sync_outbox (
    id         TEXT PRIMARY KEY,
    entity     TEXT NOT NULL CHECK (entity IN ('person', 'face_sample', 'recognition_event')),
    entity_id  TEXT NOT NULL,
    op         TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    synced_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON sync_outbox(synced_at) WHERE synced_at IS NULL;
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""")
    old_person_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO persons (id, name, thumbnail_path) VALUES (?, ?, '')",
        (old_person_id, "Cũ"),
    )
    conn.execute(
        "INSERT INTO sync_outbox (id, entity, entity_id, op) VALUES (?, 'person', ?, 'upsert')",
        (uuid.uuid4().hex, old_person_id),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    old_db = Database(old_path)
    old_people = PersonRepository(old_db)
    old_service = AttendanceService(old_db, config)
    check("DB cũ vẫn đọc được người", old_people.count() == 1)
    check("DB cũ có ca mặc định sau nâng cấp", old_service.get_default_shift() is not None)
    with old_db.session() as c:
        pending_person = c.execute(
            "SELECT COUNT(*) FROM sync_outbox"
            " WHERE entity = 'person' AND synced_at IS NULL"
        ).fetchone()[0]
        check("outbox cũ giữ nguyên sau migration", pending_person == 1)
        old_service.on_event(old_person_id, iso_local(y, mo, d, 9, 0))
        pushed_entity = c.execute(
            "SELECT entity FROM sync_outbox WHERE entity = 'attendance_day'"
        ).fetchone()
    check(
        "ghi outbox 'attendance_day' được (CHECK mới hiệu lực)",
        pushed_entity is not None,
    )
    check("người cũ chấm công được sau nâng cấp", old_people.get(old_person_id) is not None)
    old_db.close()  # đóng kết nối trước khi xóa file (Windows khóa file đang mở)
    for suffix in ("", "-wal", "-shm"):
        Path(str(old_path) + suffix).unlink(missing_ok=True)

    # ---- Dọn dẹp ----
    db.close()
    cleanup()
    print()
    if passed:
        print("KẾT QUẢ: TẤT CẢ ĐẠT ✓")
        return 0
    print("KẾT QUẢ: CÓ KIỂM TRA THẤT BẠI ✗")
    return 1


def _local_hhmm(iso_utc: str | None) -> str:
    """ISO UTC → 'HH:MM' địa phương (dùng so khớp trong test)."""
    if not iso_utc:
        return "—"
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%H:%M")


if __name__ == "__main__":
    sys.exit(main())
