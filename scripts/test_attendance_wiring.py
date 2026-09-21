"""Kiểm tra 3 ĐIỂM NỐI AttendanceService.on_event (attendance-spec FR-2).

1. Webcam + Ảnh: RecognitionService.save_event → on_event (điểm nối CHUNG
   của CameraView/PhotoView). Người lạ / giả mạo / mặt bị che KHÔNG chấm.
2. Pull sync: SyncService._pull với FakeClient (không cần mạng) — sự kiện
   mobile kéo về từ D1 cập nhật ngày công theo detected_at gốc.
3. Kiểm tra ngày công đúng ngày, đúng giờ vào/ra sau mỗi đường nối.

Chạy:  .venv\\Scripts\\python.exe scripts\\test_attendance_wiring.py
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.infrastructure.db import Database  # noqa: E402
from app.infrastructure.repositories import PersonRepository  # noqa: E402
from app.services.attendance import AttendanceService  # noqa: E402
from app.services.recognition import RecognitionService  # noqa: E402
from app.services.sync import SyncService  # noqa: E402

TEMP_DB = PROJECT_ROOT / "data" / "test_attendance_wiring.db"

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
    """Mô phỏng D1Client.query() — trả dữ liệu có sẵn, không gọi mạng."""

    def __init__(self, persons: list[dict], events: list[dict]) -> None:
        self._persons = persons
        self._events = events

    def query(self, sql: str, params=None) -> list[dict]:
        if "FROM persons" in sql:
            return self._persons
        if "FROM face_samples" in sql:
            return []
        if "FROM recognition_events" in sql:
            return self._events
        return []


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEMP_DB) + suffix).unlink(missing_ok=True)

    db = Database(TEMP_DB)
    config = Config()
    people = PersonRepository(db)
    today = datetime.now().date()
    y, mo, d = today.year, today.month, today.day
    crop = np.zeros((10, 10, 3), dtype=np.uint8)  # ảnh giả cho snapshot

    # ---- 1) save_event (webcam + ảnh) → on_event ----
    print("\n[1] RecognitionService.save_event → ngày công (webcam + ảnh)")
    service = RecognitionService(db)
    mai = people.add("Mai", "")
    event_id = service.save_event(
        person_id=mai.id, similarity=0.91, face_crop=crop, source="webcam"
    )
    attendance = AttendanceService(db)
    rec = attendance._days.find(mai.id, today.isoformat())
    check("webcam: save_event tạo check-in", rec is not None and rec.check_in_at is not None)
    event2 = service.save_event(
        person_id=mai.id, similarity=0.90, face_crop=crop, source="photo"
    )
    rec = attendance._days.find(mai.id, today.isoformat())
    check("ảnh: sự kiện 2 nguồn khác vẫn cùng dòng công", rec is not None)
    check("2 sự kiện 2 id riêng", event_id != event2)

    print("\n[1b] Chặn: người lạ / giả mạo / mặt bị che KHÔNG chấm công")
    count_before = len(attendance._days.list_month(y, mo))
    service.save_event(person_id=None, similarity=None, face_crop=crop, is_unknown=True)
    check("người lạ không tạo ngày công",
          len(attendance._days.list_month(y, mo)) == count_before)
    service.save_event(
        person_id=mai.id, similarity=0.9, face_crop=crop, is_spoof=True
    )
    check("giả mạo không tạo/cập nhật ngày công",
          len(attendance._days.list_month(y, mo)) == count_before)
    service.save_event(
        person_id=mai.id, similarity=0.9, face_crop=crop, is_occluded=True
    )
    check("mặt bị che không tạo/cập nhật ngày công",
          len(attendance._days.list_month(y, mo)) == count_before)

    # ---- 2) Pull sync (FakeClient — sự kiện mobile từ D1) ----
    print("\n[2] SyncService._pull — sự kiện mobile kéo về cập nhật ngày công")
    yesterday = today - timedelta(days=1)
    fake = FakeD1Client(
        persons=[],  # người đã có ở local → pull bỏ qua
        events=[{
            "id": uuid.uuid4().hex,
            "person_id": mai.id,
            "label": "Mai",
            "source": "mobile",
            "detected_at": iso_local(yesterday.year, yesterday.month, yesterday.day, 22, 5),
            "similarity": 0.88,
            "snapshot_path": "",
            "snapshot_r2_key": None,
            "is_unknown": 0,
        }],
    )
    sync = SyncService(db, config)
    from app.services.sync import SyncResult

    result = SyncResult()
    sync._pull(fake, None, result)
    rec = attendance._days.find(mai.id, yesterday.isoformat())
    check("sự kiện mobile tạo ngày công hôm qua", rec is not None)
    from app.services.attendance import _parse_iso_utc
    expected = _parse_iso_utc(iso_local(yesterday.year, yesterday.month, yesterday.day, 22, 5))
    actual = _parse_iso_utc(rec.check_in_at) if rec else None
    check("check-in giữ detected_at GỐC (22:05) của sự kiện", actual == expected)
    check("pull đếm 1 dòng kéo về", result.pulled == 1)

    # Sự kiện người lạ từ cloud → không chấm
    fake2 = FakeD1Client(
        persons=[],
        events=[{
            "id": uuid.uuid4().hex,
            "person_id": None,
            "label": "Người lạ",
            "source": "mobile",
            "detected_at": iso_local(y, mo, d, 23, 0),
            "similarity": None,
            "snapshot_path": "",
            "snapshot_r2_key": None,
            "is_unknown": 1,
        }],
    )
    before = len(attendance._days.list_month(y, mo))
    sync._pull(fake2, None, SyncResult())
    check("pull sự kiện người lạ không tạo ngày công",
          len(attendance._days.list_month(y, mo)) == before)

    # ---- 3) on_event lỗi mềm (detected_at sai không giết luồng) ----
    print("\n[3] Lỗi mềm — detected_at rác không làm giết luồng")
    attendance.on_event(mai.id, "khong-phai-iso")
    check("on_event không ném với detected_at rác", True)  # tới đây = không ném

    db.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEMP_DB) + suffix).unlink(missing_ok=True)
    print(f"\nKẾT QUẢ: {PASS} qua / {FAIL} thất bại")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
