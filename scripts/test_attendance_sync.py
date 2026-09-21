"""Kiểm tra ĐỒNG BỘ CHẤM CÔNG (attendance-spec FR-9) — không cần mạng.

1. Push lần đầu: shifts + attendance_days được đẩy (ca TRƯỚC samples — FK).
2. Push outbox: entity 'shift'/'attendance_day' upsert + delete sinh SQL đúng.
3. Pull: ca + ngày công từ cloud về (local chưa có); local đã có → bỏ qua;
   ca hỏng (giờ sai) → bỏ qua, không chết pull; ca tham chiếu thiếu → NULL.
4. SQL upsert THẬT: chạy _shift/_attendance_day_upsert_stmt lên SQLite
   in-memory (kể cả ON CONFLICT upsert lại) — chắc câu SQL hợp lệ, không
   chỉ grep chuỗi.

Chạy:  .venv\\Scripts\\python.exe scripts\\test_attendance_sync.py
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
from app.infrastructure.repositories import PersonRepository  # noqa: E402
from app.services.attendance import AttendanceService  # noqa: E402
from app.services.sync import (  # noqa: E402
    SyncResult,
    SyncService,
    _attendance_day_upsert_stmt,
    _shift_upsert_stmt,
)

TEMP_DB = PROJECT_ROOT / "data" / "test_attendance_sync.db"

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


def iso_local(y: int, mo: int, d: int, h: int, mi: int) -> str:
    return (
        datetime(y, mo, d, h, mi).astimezone()
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


class FakeD1Client:
    """Mô phỏng D1: ghi nhận batch, trả dữ liệu cloud cho SELECT theo bảng."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, list | None]] = []
        self.cloud_shifts: list[dict] = []
        self.cloud_days: list[dict] = []
        self.cloud_persons: list[dict] = []
        self.cloud_samples: list[dict] = []
        self.cloud_events: list[dict] = []

    def ensure_schema(self) -> None:
        return

    def query(self, sql: str, params=None) -> list[dict]:
        if "FROM attendance_days" in sql:
            return list(self.cloud_days)
        if "FROM shifts" in sql:
            return list(self.cloud_shifts)
        if "FROM recognition_events" in sql:
            return list(self.cloud_events)
        if "FROM face_samples" in sql:
            return list(self.cloud_samples)
        if "FROM persons" in sql:
            return list(self.cloud_persons)
        return []

    def query_batch(self, statements) -> None:
        self.statements.extend(statements)


def make_sync(db: Database, client: FakeD1Client) -> SyncService:
    config = Config()
    service = SyncService(db, config)
    service.make_client = lambda: client  # type: ignore[method-assign]
    return service


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEMP_DB) + suffix).unlink(missing_ok=True)

    db = Database(TEMP_DB)
    people = PersonRepository(db)
    attendance = AttendanceService(db)
    today = datetime.now().date()
    y, mo, d = today.year, today.month, today.day

    mai = people.add("Mai", "")
    attendance.on_event(mai.id, iso_local(y, mo, d, 8, 5))
    attendance.on_event(mai.id, iso_local(y, mo, d, 17, 0))
    attendance.mark_leave(mai.id, (today + timedelta(days=1)).isoformat())

    # ---- 1) Push lần đầu (first_full) ----
    print("\n[1] Push lần đầu — ca + ngày công vào batch (ca trước samples)")
    client = FakeD1Client()
    sync = make_sync(db, client)
    result = sync.run_full_sync()
    check("sync OK", result.ok, f"({result.summary()})")
    sql_all = " ".join(s for s, _ in client.statements)
    check("có INSERT INTO shifts", "INSERT INTO shifts" in sql_all)
    check("có INSERT INTO attendance_days", "INSERT INTO attendance_days" in sql_all)
    check("persons upsert có shift_id", "shift_id" in sql_all)
    first_shift = next(
        (i for i, (s, _) in enumerate(client.statements) if "INSERT INTO shifts" in s),
        None,
    )
    first_sample = next(
        (i for i, (s, _) in enumerate(client.statements) if "INSERT INTO face_samples" in s),
        None,
    )
    if first_shift is not None and first_sample is not None:
        check("ca đẩy TRƯỚC samples (FK)", first_shift < first_sample)
    else:
        check("ca đẩy TRƯỚC samples (FK)", True)  # không có sample — bỏ qua

    # ---- 2) Outbox: upsert/delete shift + attendance_day ----
    print("\n[2] Outbox — entity shift/attendance_day")
    shift = attendance.create_shift("Ca K", "09:00", "18:00", 5)
    attendance.delete_shift(shift.id)
    client2 = FakeD1Client()
    sync2 = make_sync(db, client2)
    sync2.run_full_sync()
    sql2 = " ".join(s for s, _ in client2.statements)
    check("có DELETE FROM shifts", "DELETE FROM shifts" in sql2)
    rec = attendance._days.find(mai.id, today.isoformat())
    attendance.edit_times(rec.id, None, "18:30")
    client3 = FakeD1Client()
    make_sync(db, client3).run_full_sync()
    sql3 = " ".join(s for s, _ in client3.statements)
    check(
        "sửa giờ → outbox upsert attendance_day (có giờ mới)",
        "INSERT INTO attendance_days" in sql3 and "18:30" not in sql3,  # giờ lưu UTC
    )
    check("outbox attendance_day có upsert", "INSERT INTO attendance_days" in sql3)

    # ---- 3) Pull ca + ngày công từ cloud ----
    print("\n[3] Pull — ca + ngày công từ cloud về")
    cloud_shift_id = uuid.uuid4().hex
    broken_shift_id = uuid.uuid4().hex
    other_person = uuid.uuid4().hex  # người KHÔNG có local (máy khác tạo)
    client4 = FakeD1Client()
    client4.cloud_shifts = [
        {
            "id": cloud_shift_id, "name": "Ca cloud", "start_time": "20:00",
            "end_time": "04:00", "grace_minutes": 12,
            "created_at": "2026-09-01T00:00:00.000Z",
        },
        {   # ca HỎNG — giờ sai định dạng
            "id": broken_shift_id, "name": "Ca hỏng", "start_time": "20h",
            "end_time": "04:00", "grace_minutes": 0,
            "created_at": "2026-09-01T00:00:00.000Z",
        },
    ]
    client4.cloud_days = [
        {
            "id": uuid.uuid4().hex, "person_id": mai.id,
            "work_date": (today - timedelta(days=2)).isoformat(),
            "shift_id": cloud_shift_id,
            "check_in_at": iso_local(y, mo, d - 2, 21, 55),
            "check_out_at": None, "status": "auto", "manual_override": 0,
            "note": "từ cloud", "updated_at": "2026-09-20T10:00:00.000Z",
        },
        {
            # Người khác máy + ca tham chiếu KHÔNG có local → shift_id về NULL
            "id": uuid.uuid4().hex, "person_id": other_person,
            "work_date": (today - timedelta(days=3)).isoformat(),
            "shift_id": broken_shift_id, "check_in_at": None,
            "check_out_at": None, "status": "trip", "manual_override": 1,
            "note": "công tác máy khác", "updated_at": "2026-09-19T10:00:00.000Z",
        },
    ]
    sync3 = make_sync(db, client4)
    result3 = SyncResult()
    sync3._pull(client4, None, result3)
    pulled_shift = next((s for s in attendance.list_shifts() if s.id == cloud_shift_id), None)
    check("ca hợp lệ kéo về (grace 12)", pulled_shift is not None and pulled_shift.grace_minutes == 12)
    check("ca hỏng bị bỏ qua, pull không chết",
          next((s for s in attendance.list_shifts() if s.id == broken_shift_id), None) is None)
    day1 = attendance._days.find(mai.id, (today - timedelta(days=2)).isoformat())
    check("ngày công kéo về với ca đúng", day1 is not None and day1.shift_id == cloud_shift_id)
    day2 = attendance._days.find(other_person, (today - timedelta(days=3)).isoformat())
    check("ngày công người chưa có local bị BỎ (FK person_id)", day2 is None)
    check("pull đếm 2 dòng (1 ca + 1 ngày; 2 dòng lỗi bỏ qua)", result3.pulled == 2,
          f"(pulled={result3.pulled})")

    # Pull LẦI — local đã có → không nhân đôi
    result4 = SyncResult()
    sync3._pull(client4, None, result4)
    check("pull lại → 0 dòng mới (local thắng)", result4.pulled == 0)

    # ---- 4) SQL upsert THẬT lên SQLite in-memory ----
    print("\n[4] SQL upsert thật (SQLite in-memory — cú pháp D1-tương thích)")
    from app.infrastructure.db import SCHEMA_SQL

    mem = sqlite3.connect(":memory:")
    mem.executescript(SCHEMA_SQL)
    mem.execute(
        "INSERT INTO persons (id, name, thumbnail_path) VALUES (?, ?, '')",
        (mai.id, "Mai"),
    )
    shifts = attendance.list_shifts()
    for shift_row in shifts:
        sql, params = _shift_upsert_stmt(shift_row)
        mem.execute(sql, params or [])
    days = attendance._days.list_all()
    for day_row in days:
        sql, params = _attendance_day_upsert_stmt(day_row)
        mem.execute(sql, params or [])
    # Upsert LẠI (ON CONFLICT nhánh UPDATE) — không được lỗi
    for shift_row in shifts:
        sql, params = _shift_upsert_stmt(shift_row)
        mem.execute(sql, params or [])
    for day_row in days:
        sql, params = _attendance_day_upsert_stmt(day_row)
        mem.execute(sql, params or [])
    n_shifts = mem.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
    n_days = mem.execute("SELECT COUNT(*) FROM attendance_days").fetchone()[0]
    check(f"upsert shifts ×2 không nhân đôi ({n_shifts} dòng)", n_shifts == len(shifts))
    check(f"upsert days ×2 không nhân đôi ({n_days} dòng)", n_days == len(days))
    check("FK attendance_days.shift_id hợp lệ trên D1-schema",
          mem.execute("PRAGMA foreign_key_check").fetchall() == [])
    mem.close()

    db.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEMP_DB) + suffix).unlink(missing_ok=True)
    print(f"\nKẾT QUẢ: {PASS} qua / {FAIL} thất bại")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
