"""Repository pattern — tầng truy xuất dữ liệu người + embedding (Bước 6).

UI/service KHÔNG viết câu lệnh SQL trực tiếp mà gọi qua repository —
mỗi repository gói trọn các thao tác SQL của một "thực thể" (person,
face_sample). Lợi ích: code sạch, dễ đổi nguồn dữ liệu sau này (ví dụ
D1 cloud), dễ kiểm thử.

Embedding được lưu dạng BLOB: mảng numpy.float32 (512 phần tử) → bytes
(2048 bytes) bằng ``tobytes()``; khi đọc dùng ``np.frombuffer``.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.infrastructure.db import Database

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 512  # kích thước vector ArcFace (buffalo_l)


# =============================================================
# Mô hình dữ liệu (dataclass — đối tượng thuần, không liên quan SQL)
# =============================================================

@dataclass
class Person:
    """Một người đã đăng ký khuôn mặt."""

    id: str
    name: str
    created_at: str
    thumbnail_path: str
    thumbnail_r2_key: str | None = None
    shift_id: str | None = None  # NULL = dùng ca mặc định (attendance-spec FR-3)


@dataclass
class Shift:
    """Ca làm việc (attendance-spec FR-3): giờ quy định + dung sai trễ.

    Ca đêm: ``end_time <= start_time`` (kết thúc sau nửa đêm).
    ``factor`` = hệ số lương của ca (lương thô = số công × hệ số; schema v3).
    """

    id: str
    name: str
    start_time: str  # 'HH:MM' giờ địa phương
    end_time: str    # 'HH:MM'
    grace_minutes: int = 10
    created_at: str = ""
    factor: float = 1.0
    break_start: str | None = None  # 'HH:MM' đầu nghỉ giữa ca (schema v4)
    break_end: str | None = None    # 'HH:MM' hết nghỉ — None = không nghỉ cố định


@dataclass
class AttendanceDay:
    """Bản ghi ngày công — 1 dòng / người / ngày (attendance-spec FR-1).

    Giờ lưu ISO UTC; ``work_date`` là ngày bắt đầu ca (giờ địa phương).
    Đi muộn tính lúc HIỂN THỊ (không lưu cột riêng — tránh lệch khi sửa ca).
    """

    id: str
    person_id: str
    work_date: str                     # 'YYYY-MM-DD'
    shift_id: str | None = None
    check_in_at: str | None = None     # ISO UTC; None = chưa có giờ vào
    check_out_at: str | None = None    # None = thiếu giờ ra
    status: str = "auto"               # auto / leave / trip / manual
    manual_override: bool = False
    note: str = ""
    updated_at: str = ""


@dataclass
class FaceSample:
    """Một mẫu embedding của một người (mỗi người có 3–5 mẫu)."""

    id: str
    person_id: str
    embedding: np.ndarray  # float32, hình dạng (EMBEDDING_DIM,)
    quality: float
    captured_at: str


@dataclass
class RecognitionEvent:
    """Một sự kiện nhận diện — audit trail (wireframe 5.5.4).

    is_unknown = True khi là người lạ (person_id = None, label = 'Người lạ').
    """

    id: str
    person_id: str | None
    label: str
    source: str
    detected_at: str
    similarity: float | None
    snapshot_path: str
    is_unknown: bool
    snapshot_r2_key: str | None = None


def _new_id() -> str:
    """Tạo ID UUID v4 dạng hex — tránh xung đột khi đồng bộ nhiều máy."""
    return uuid.uuid4().hex


def _embedding_to_blob(embedding: np.ndarray) -> bytes:
    """Chuyển vector float32 (512,) thành bytes để lưu BLOB."""
    arr = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if arr.size != EMBEDDING_DIM:
        raise ValueError(
            f"Embedding phải có {EMBEDDING_DIM} chiều, nhận được {arr.size}"
        )
    return arr.tobytes()


def _blob_to_embedding(blob: bytes) -> np.ndarray:
    """Đọc lại vector float32 (512,) từ bytes đã lưu."""
    return np.frombuffer(blob, dtype=np.float32)


# =============================================================
# Repository
# =============================================================

class PersonRepository:
    """Thao tác bảng ``persons`` (thêm / xem / sửa tên / xóa)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- Ghi -------------------------------------------------
    def add(
        self,
        name: str,
        thumbnail_path: str,
        person_id: str | None = None,
        created_at: str | None = None,
        thumbnail_r2_key: str | None = None,
        record_outbox: bool = True,
    ) -> Person:
        """Thêm người mới, trả về đối tượng Person đã lưu (kèm id).

        ``person_id``/``created_at``: chỉ dùng khi KÉO dữ liệu từ cloud về
        (phải giữ nguyên id để không trùng lặp — Bước 15). ``record_outbox``
        = False khi thao tác do chính sync tạo ra (tránh vòng lặp đẩy lại).
        ``thumbnail_r2_key``: key ảnh đại diện trên R2 khi kéo người từ
        cloud về (Bước 16) — lưu ngay để lần sync sau không upload lại.
        """
        person = Person(
            id=person_id or _new_id(),
            name=name.strip(),
            created_at=created_at or "",  # điền sau khi SQL tự đặt DEFAULT
            thumbnail_path=thumbnail_path,
            thumbnail_r2_key=thumbnail_r2_key,
        )
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO persons (id, name, created_at, thumbnail_path, thumbnail_r2_key)"
                " VALUES (?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), ?, ?)",
                (
                    person.id,
                    person.name,
                    person.created_at or None,
                    person.thumbnail_path,
                    person.thumbnail_r2_key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM persons WHERE id = ?", (person.id,)
            ).fetchone()
        if record_outbox:
            self._outbox().add("person", person.id, "upsert")
        logger.info("Đã thêm người '%s' (id=%s)", person.name, person.id)
        return _row_to_person(row)

    def set_thumbnail_r2_key(self, person_id: str, r2_key: str) -> bool:
        """Ghi key ảnh trên R2 sau khi đã upload thành công (Bước 16).

        KHÔNG ghi outbox — key là kết quả của chính quá trình sync, ghi
        outbox sẽ tạo vòng lặp đẩy lại. Lần sync sau đọc từ DB và thấy
        key đã có → bỏ qua upload.
        """
        with self._db.session() as conn:
            cur = conn.execute(
                "UPDATE persons SET thumbnail_r2_key = ? WHERE id = ?",
                (r2_key, person_id),
            )
        return cur.rowcount > 0

    def update_thumbnail(
        self, person_id: str, thumbnail_path: str, record_outbox: bool = True
    ) -> bool:
        """Cập nhật đường dẫn ảnh đại diện (gọi sau khi đã lưu file thumbnail)."""
        with self._db.session() as conn:
            cur = conn.execute(
                "UPDATE persons SET thumbnail_path = ? WHERE id = ?",
                (thumbnail_path, person_id),
            )
        if cur.rowcount and record_outbox:
            self._outbox().add("person", person_id, "upsert")
        return cur.rowcount > 0

    def rename(
        self, person_id: str, new_name: str, record_outbox: bool = True
    ) -> bool:
        """Đổi tên người. Trả về True nếu tìm thấy và đã đổi."""
        new_name = new_name.strip()
        if not new_name:
            return False
        with self._db.session() as conn:
            cur = conn.execute(
                "UPDATE persons SET name = ? WHERE id = ?",
                (new_name, person_id),
            )
        if cur.rowcount and record_outbox:
            self._outbox().add("person", person_id, "upsert")
        if cur.rowcount:
            logger.info("Đã đổi tên người %s → '%s'", person_id, new_name)
        return cur.rowcount > 0

    def delete(self, person_id: str, record_outbox: bool = True) -> bool:
        """Xóa người — các face_samples tự xóa (ON DELETE CASCADE).

        Ghi chú: recognition_events giữ lại với person_id = NULL
        (ON DELETE SET NULL — không làm mất lịch sử).
        """
        with self._db.session() as conn:
            cur = conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        if cur.rowcount and record_outbox:
            self._outbox().add("person", person_id, "delete")
        if cur.rowcount:
            logger.info("Đã xóa người %s (kèm face_samples)", person_id)
        return cur.rowcount > 0

    def _outbox(self) -> SyncOutboxRepository:
        return SyncOutboxRepository(self._db)

    def set_shift(
        self, person_id: str, shift_id: str | None, record_outbox: bool = True
    ) -> bool:
        """Gán ca làm việc cho người (attendance-spec FR-3).

        ``shift_id=None`` → người này dùng ca mặc định. Cột ``shift_id``
        có khóa ngoại ON DELETE SET NULL — xóa ca tự động đưa về mặc định.
        """
        with self._db.session() as conn:
            cur = conn.execute(
                "UPDATE persons SET shift_id = ? WHERE id = ?",
                (shift_id, person_id),
            )
        if cur.rowcount and record_outbox:
            self._outbox().add("person", person_id, "upsert")
        return cur.rowcount > 0

    # -- Đọc -------------------------------------------------
    def get(self, person_id: str) -> Person | None:
        """Lấy một người theo id; None nếu không tồn tại."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT * FROM persons WHERE id = ?", (person_id,)
            ).fetchone()
        return _row_to_person(row) if row else None

    def list_all(self) -> list[Person]:
        """Danh sách toàn bộ người, sắp theo thời gian tạo mới nhất."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM persons ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_person(r) for r in rows]

    def count(self) -> int:
        """Tổng số người đã đăng ký."""
        with self._db.session() as conn:
            row = conn.execute("SELECT COUNT(*) FROM persons").fetchone()
        return int(row[0])


class FaceSampleRepository:
    """Thao tác bảng ``face_samples`` (mẫu embedding của từng người)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- Ghi -------------------------------------------------
    def add(
        self,
        person_id: str,
        embedding: np.ndarray,
        quality: float = 1.0,
        sample_id: str | None = None,
        captured_at: str | None = None,
        record_outbox: bool = True,
    ) -> FaceSample:
        """Thêm một mẫu embedding cho người, trả về đối tượng đã lưu.

        ``sample_id``/``captured_at``: giữ nguyên id khi KÉO từ cloud về
        (Bước 15); ``record_outbox=False`` cho thao tác do sync tạo.
        """
        sample = FaceSample(
            id=sample_id or _new_id(),
            person_id=person_id,
            embedding=np.asarray(embedding, dtype=np.float32),
            quality=float(quality),
            captured_at=captured_at or "",
        )
        blob = _embedding_to_blob(sample.embedding)
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO face_samples (id, person_id, embedding, quality, captured_at)"
                " VALUES (?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
                (sample.id, sample.person_id, blob, sample.quality, sample.captured_at or None),
            )
            row = conn.execute(
                "SELECT * FROM face_samples WHERE id = ?", (sample.id,)
            ).fetchone()
        if record_outbox:
            self._outbox().add("face_sample", sample.id, "upsert")
        logger.debug("Đã thêm mẫu embedding %s cho người %s", sample.id, person_id)
        return _row_to_sample(row)

    def get(self, sample_id: str) -> FaceSample | None:
        """Lấy một mẫu embedding theo id; None nếu không tồn tại."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT * FROM face_samples WHERE id = ?", (sample_id,)
            ).fetchone()
        return _row_to_sample(row) if row else None

    def _outbox(self) -> SyncOutboxRepository:
        return SyncOutboxRepository(self._db)

    # -- Đọc -------------------------------------------------
    def list_by_person(self, person_id: str) -> list[FaceSample]:
        """Tất cả mẫu embedding của một người."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM face_samples WHERE person_id = ?"
                " ORDER BY captured_at ASC",
                (person_id,),
            ).fetchall()
        return [_row_to_sample(r) for r in rows]

    def count_by_person(self, person_id: str) -> int:
        """Số mẫu embedding của một người."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM face_samples WHERE person_id = ?",
                (person_id,),
            ).fetchone()
        return int(row[0])

    def all_samples(self) -> list[FaceSample]:
        """Toàn bộ mẫu embedding của mọi người (dùng cho so khớp Bước 6)."""
        with self._db.session() as conn:
            rows = conn.execute("SELECT * FROM face_samples").fetchall()
        return [_row_to_sample(r) for r in rows]


class RecognitionEventRepository:
    """Thao tác bảng ``recognition_events`` (lịch sử nhận diện).

    Bước 9: ghi sự kiện khi nhận diện real-time (kèm snapshot).
    Bước 11 (HistoryView) sẽ mở rộng thêm truy vấn tìm kiếm.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- Ghi -------------------------------------------------
    def add(
        self,
        person_id: str | None,
        label: str,
        source: str,
        similarity: float | None = None,
        snapshot_path: str = "",
        is_unknown: bool = False,
        event_id: str | None = None,
        detected_at: str | None = None,
        snapshot_r2_key: str | None = None,
        record_outbox: bool = True,
    ) -> str:
        """Ghi một sự kiện nhận diện; trả về id sự kiện vừa tạo.

        person_id = None + is_unknown = 1 khi gặp người lạ.
        ``event_id``/``detected_at``: giữ nguyên khi KÉO từ cloud về
        (Bước 15); ``record_outbox=False`` cho thao tác do sync tạo.
        ``snapshot_r2_key``: key ảnh chụp trên R2 khi kéo sự kiện từ cloud
        (Bước 16) — lưu ngay để lần sync sau không upload lại.
        """
        event_id = event_id or _new_id()
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO recognition_events"
                " (id, person_id, label, source, similarity, snapshot_path,"
                " is_unknown, detected_at, snapshot_r2_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), ?)",
                (
                    event_id,
                    person_id,
                    label,
                    source,
                    similarity,
                    snapshot_path,
                    1 if is_unknown else 0,
                    detected_at or None,
                    snapshot_r2_key,
                ),
            )
        if record_outbox:
            self._outbox().add("recognition_event", event_id, "upsert")
        logger.debug("Đã ghi sự kiện nhận diện %s (%s)", event_id, label)
        return event_id

    def set_snapshot_r2_key(self, event_id: str, r2_key: str) -> bool:
        """Ghi key ảnh snapshot trên R2 sau khi upload thành công (Bước 16).

        KHÔNG ghi outbox — key là kết quả của chính quá trình sync (lý do
        giống set_thumbnail_r2_key của PersonRepository).
        """
        with self._db.session() as conn:
            cur = conn.execute(
                "UPDATE recognition_events SET snapshot_r2_key = ? WHERE id = ?",
                (r2_key, event_id),
            )
        return cur.rowcount > 0

    def list_all(self) -> list[RecognitionEvent]:
        """Toàn bộ sự kiện, mới nhất trước (dùng cho đợt đồng bộ đầu tiên)."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM recognition_events ORDER BY detected_at ASC"
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def _outbox(self) -> SyncOutboxRepository:
        return SyncOutboxRepository(self._db)

    # -- Đọc -------------------------------------------------
    def last_detected_at(self, person_id: str) -> str | None:
        """Thời điểm nhận diện gần nhất của người (ISO); None nếu chưa từng."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT detected_at FROM recognition_events"
                " WHERE person_id = ? ORDER BY detected_at DESC LIMIT 1",
                (person_id,),
            ).fetchone()
        return str(row["detected_at"]) if row else None

    def get(self, event_id: str) -> RecognitionEvent | None:
        """Lấy một sự kiện theo id; None nếu không tồn tại."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT * FROM recognition_events WHERE id = ?", (event_id,)
            ).fetchone()
        return _row_to_event(row) if row else None

    @staticmethod
    def _status_where(status: str) -> tuple[str, list[Any]]:
        """Điều kiện SQL cho bộ lọc trạng thái sự kiện (Bước 18 mở rộng).

        - ``blocked``: sự kiện bị CHẶN nhận diện — Giả mạo (B17) hoặc Mặt
          bị che (B18) — nhận biết qua tiền tố label (xem HistoryView).
        - ``unknown``: người lạ (is_unknown = 1).
        - ``confirmed``: nhận diện thành công (không phải người lạ, không
          bị chặn).
        Trả về (điều kiện SQL, tham số) — '' + [] khi status rỗng (tất cả).
        """
        if status == "blocked":
            return (
                " AND (label LIKE 'Giả mạo:%' OR label LIKE 'Mặt bị che:%')",
                [],
            )
        if status == "unknown":
            return " AND is_unknown = 1", []
        if status == "confirmed":
            return (
                " AND is_unknown = 0"
                " AND label NOT LIKE 'Giả mạo:%'"
                " AND label NOT LIKE 'Mặt bị che:%'",
                [],
            )
        return "", []

    def list_events(
        self,
        query: str = "",
        source: str = "",
        status: str = "",
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RecognitionEvent]:
        """Danh sách sự kiện MỚI NHẤT TRƯỚC (Bước 11 — HistoryView).

        Bộ lọc (kết hợp được với nhau):
        - ``query``: khớp tên hiển thị (label) — không phân biệt hoa thường
        - ``source``: '' = tất cả, ngược lại khớp chính xác (webcam/photo/mobile)
        - ``status``: '' = tất cả, 'blocked'/'unknown'/'confirmed' (xem
          _status_where) — lọc nhanh các lần bị CHẶN (Giả mạo / Mặt bị che)
        - ``since``: mốc thời gian ISO — chỉ lấy sự kiện từ mốc này trở đi
        - ``limit`` / ``offset``: phân trang
        """
        sql = "SELECT * FROM recognition_events WHERE 1=1"
        params: list[Any] = []
        if query:
            sql += " AND label LIKE ? COLLATE NOCASE"
            params.append(f"%{query}%")
        if source:
            sql += " AND source = ?"
            params.append(source)
        status_sql, status_params = self._status_where(status)
        sql += status_sql
        params.extend(status_params)
        if since:
            sql += " AND detected_at >= ?"
            params.append(since)
        sql += " ORDER BY detected_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._db.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def count_events(
        self,
        query: str = "",
        source: str = "",
        status: str = "",
        since: str | None = None,
    ) -> int:
        """Đếm sự kiện theo CÙNG bộ lọc của list_events (dùng cho phân trang)."""
        sql = "SELECT COUNT(*) FROM recognition_events WHERE 1=1"
        params: list[Any] = []
        if query:
            sql += " AND label LIKE ? COLLATE NOCASE"
            params.append(f"%{query}%")
        if source:
            sql += " AND source = ?"
            params.append(source)
        status_sql, status_params = self._status_where(status)
        sql += status_sql
        params.extend(status_params)
        if since:
            sql += " AND detected_at >= ?"
            params.append(since)
        with self._db.session() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0])

    # -- Xóa -------------------------------------------------
    def delete(self, event_id: str, record_outbox: bool = True) -> bool:
        """Xóa một sự kiện; trả True nếu tìm thấy và đã xóa."""
        with self._db.session() as conn:
            cur = conn.execute(
                "DELETE FROM recognition_events WHERE id = ?", (event_id,)
            )
        if cur.rowcount and record_outbox:
            self._outbox().add("recognition_event", event_id, "delete")
        return cur.rowcount > 0

    def count_today(self) -> int:
        """Số sự kiện nhận diện từ đầu ngày HÔM NAY (giờ địa phương).

        detected_at lưu theo UTC (hậu tố 'Z') → đổi mốc 00:00 giờ địa
        phương sang UTC rồi so chuỗi ISO (cùng định dạng, so được).
        """
        from datetime import datetime, timezone

        midnight_local = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone()
        midnight_utc = midnight_local.astimezone(timezone.utc)
        boundary = midnight_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM recognition_events WHERE detected_at >= ?",
                (boundary,),
            ).fetchone()
        return int(row[0])


# =============================================================
# SyncOutboxRepository — hàng đợi đồng bộ cloud (Bước 15, outbox pattern)
# =============================================================

class SyncOutboxRepository:
    """Hàng đợi "cần đồng bộ lên cloud".

    Mọi thao tác GHI local (thêm/sửa/xóa người, mẫu, sự kiện) ghi 1 dòng
    vào đây; SyncService (Bước 15) đọc các dòng chưa đồng bộ (synced_at
    IS NULL), đẩy lên D1 rồi đánh dấu đã xong. Đây là **outbox pattern**:
    local ghi trước (offline OK), cloud đồng bộ sau khi có mạng.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, entity: str, entity_id: str, op: str) -> None:
        """Ghi 1 thao tác cần đồng bộ (upsert / delete).

        Nếu đã có dòng CHƯA đồng bộ cho cùng thực thể → xóa dòng cũ rồi
        ghi dòng mới (chỉ giữ thao tác MỚI NHẤT — đổi tên 3 lần trước khi
        sync chỉ cần đẩy 1 lần với tên cuối cùng).
        """
        with self._db.session() as conn:
            conn.execute(
                "DELETE FROM sync_outbox"
                " WHERE entity = ? AND entity_id = ? AND synced_at IS NULL",
                (entity, entity_id),
            )
            conn.execute(
                "INSERT INTO sync_outbox (id, entity, entity_id, op) VALUES (?, ?, ?, ?)",
                (_new_id(), entity, entity_id, op),
            )

    def list_pending(self) -> list[tuple[str, str, str, str]]:
        """Các thao tác chưa đồng bộ: (outbox_id, entity, entity_id, op)."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT id, entity, entity_id, op FROM sync_outbox"
                " WHERE synced_at IS NULL ORDER BY created_at ASC"
            ).fetchall()
        return [(r["id"], r["entity"], r["entity_id"], r["op"]) for r in rows]

    def mark_synced(self, outbox_ids: list[str], synced_at: str) -> int:
        """Đánh dấu các dòng đã đẩy lên cloud thành công; trả về số dòng."""
        if not outbox_ids:
            return 0
        placeholders = ",".join("?" for _ in outbox_ids)
        with self._db.session() as conn:
            cur = conn.execute(
                f"UPDATE sync_outbox SET synced_at = ? WHERE id IN ({placeholders})",
                [synced_at, *outbox_ids],
            )
        return cur.rowcount

    def count_pending(self) -> int:
        """Số thao tác đang chờ đồng bộ."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE synced_at IS NULL"
            ).fetchone()
        return int(row[0])


# =============================================================
# Hàm phụ chuyển hàng SQL → dataclass
# =============================================================

def _row_to_person(row: Any) -> Person:
    # shift_id có thể vắng mặt khi schema chưa nâng cấp (DB cũ mở bằng code
    # mới trước khi chạy executescript) — đọc an toàn qua keys().
    shift_id = row["shift_id"] if "shift_id" in row.keys() else None
    return Person(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        thumbnail_path=row["thumbnail_path"],
        thumbnail_r2_key=row["thumbnail_r2_key"],
        shift_id=shift_id,
    )


def _row_to_sample(row: Any) -> FaceSample:
    return FaceSample(
        id=row["id"],
        person_id=row["person_id"],
        embedding=_blob_to_embedding(row["embedding"]),
        quality=row["quality"],
        captured_at=row["captured_at"],
    )


def _row_to_event(row: Any) -> RecognitionEvent:
    return RecognitionEvent(
        id=row["id"],
        person_id=row["person_id"],
        label=row["label"],
        source=row["source"],
        detected_at=row["detected_at"],
        similarity=row["similarity"],
        snapshot_path=row["snapshot_path"] or "",
        snapshot_r2_key=row["snapshot_r2_key"],
        is_unknown=bool(row["is_unknown"]),
    )


def _row_to_shift(row: Any) -> Shift:
    return Shift(
        id=row["id"],
        name=row["name"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        grace_minutes=int(row["grace_minutes"]),
        created_at=row["created_at"],
        factor=float(row["factor"]),
        break_start=row["break_start"],
        break_end=row["break_end"],
    )


def _row_to_attendance_day(row: Any) -> AttendanceDay:
    return AttendanceDay(
        id=row["id"],
        person_id=row["person_id"],
        work_date=row["work_date"],
        shift_id=row["shift_id"],
        check_in_at=row["check_in_at"],
        check_out_at=row["check_out_at"],
        status=row["status"],
        manual_override=bool(row["manual_override"]),
        note=row["note"] or "",
        updated_at=row["updated_at"],
    )


# =============================================================
# ShiftRepository — ca làm việc (attendance-spec FR-3)
# =============================================================

class ShiftRepository:
    """Thao tác bảng ``shifts`` (CRUD ca làm việc).

    Outbox entity 'shift' — SyncService đẩy lên D1 (attendance-spec FR-9).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- Ghi -------------------------------------------------
    def add(
        self,
        name: str,
        start_time: str,
        end_time: str,
        grace_minutes: int = 10,
        shift_id: str | None = None,
        created_at: str | None = None,
        factor: float = 1.0,
        break_start: str | None = None,
        break_end: str | None = None,
        record_outbox: bool = True,
    ) -> Shift:
        """Thêm ca mới; ``shift_id``/``created_at`` dùng khi KÉO từ cloud về."""
        name = name.strip()
        if not name:
            raise ValueError("Tên ca không được trống")
        _validate_hhmm(start_time)
        _validate_hhmm(end_time)
        if bool(break_start) != bool(break_end):
            raise ValueError("Nghỉ giữa ca cần ĐỦ giờ bắt đầu và kết thúc (HH:MM)")
        if break_start:
            _validate_hhmm(break_start)
            _validate_hhmm(break_end)
        shift = Shift(
            id=shift_id or _new_id(),
            name=name,
            start_time=start_time,
            end_time=end_time,
            grace_minutes=max(0, int(grace_minutes)),
            created_at=created_at or "",
            factor=max(float(factor), 0.01),
            break_start=break_start or None,
            break_end=break_end or None,
        )
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO shifts (id, name, start_time, end_time, grace_minutes, factor, break_start, break_end, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
                (
                    shift.id,
                    shift.name,
                    shift.start_time,
                    shift.end_time,
                    shift.grace_minutes,
                    shift.factor,
                    shift.break_start,
                    shift.break_end,
                    shift.created_at or None,
                ),
            )
        if record_outbox:
            SyncOutboxRepository(self._db).add("shift", shift.id, "upsert")
        logger.info("Đã thêm ca '%s' (%s–%s)", shift.name, shift.start_time, shift.end_time)
        return shift

    def update(
        self,
        shift_id: str,
        name: str,
        start_time: str,
        end_time: str,
        grace_minutes: int,
        factor: float = 1.0,
        break_start: str | None = None,
        break_end: str | None = None,
        record_outbox: bool = True,
    ) -> bool:
        """Sửa ca; ngày công ĐÃ TÍNH giữ nguyên shift_id cũ (FR-3)."""
        name = name.strip()
        if not name:
            return False
        _validate_hhmm(start_time)
        _validate_hhmm(end_time)
        if bool(break_start) != bool(break_end):
            raise ValueError("Nghỉ giữa ca cần ĐỦ giờ bắt đầu và kết thúc (HH:MM)")
        if break_start:
            _validate_hhmm(break_start)
            _validate_hhmm(break_end)
        with self._db.session() as conn:
            cur = conn.execute(
                "UPDATE shifts SET name = ?, start_time = ?, end_time = ?,"
                " grace_minutes = ?, factor = ?, break_start = ?, break_end = ?"
                " WHERE id = ?",
                (name, start_time, end_time, max(0, int(grace_minutes)),
                 max(float(factor), 0.01), break_start or None, break_end or None,
                 shift_id),
            )
        if cur.rowcount and record_outbox:
            SyncOutboxRepository(self._db).add("shift", shift_id, "upsert")
        return cur.rowcount > 0

    def delete(self, shift_id: str, record_outbox: bool = True) -> bool:
        """Xóa ca — persons.shift_id tự về NULL (ON DELETE SET NULL) = ca mặc định.

        attendance_days.shift_id cũng SET NULL (ngày đã tính giữ nguyên số,
        chỉ mất tham chiếu ca — đúng spec FR-3).
        """
        with self._db.session() as conn:
            cur = conn.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))
        if cur.rowcount and record_outbox:
            SyncOutboxRepository(self._db).add("shift", shift_id, "delete")
        if cur.rowcount:
            logger.info("Đã xóa ca %s (người gán ca này về ca mặc định)", shift_id)
        return cur.rowcount > 0

    # -- Đọc -------------------------------------------------
    def get(self, shift_id: str) -> Shift | None:
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT * FROM shifts WHERE id = ?", (shift_id,)
            ).fetchone()
        return _row_to_shift(row) if row else None

    def list_all(self) -> list[Shift]:
        """Toàn bộ ca, ca tạo trước đứng trước (ổn định khi hiển thị combo)."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM shifts ORDER BY created_at ASC, name ASC"
            ).fetchall()
        return [_row_to_shift(r) for r in rows]

    def get_by_name(self, name: str) -> Shift | None:
        """Tìm ca theo tên (chính xác) — dùng kiểm tra trùng khi tạo."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT * FROM shifts WHERE name = ?", (name.strip(),)
            ).fetchone()
        return _row_to_shift(row) if row else None


# =============================================================
# AttendanceDayRepository — bản ghi ngày công (attendance-spec FR-1/FR-2)
# =============================================================

class AttendanceDayRepository:
    """Thao tác bảng ``attendance_days`` (1 dòng / người / ngày).

    Outbox entity 'attendance_day' — SyncService đẩy lên D1 (FR-9).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- Ghi -------------------------------------------------
    def upsert_auto(
        self,
        person_id: str,
        work_date: str,
        check_in_at: str,
        check_out_at: str | None,
        shift_id: str | None,
        record_outbox: bool = True,
    ) -> AttendanceDay:
        """Ghi/ cập nhật ngày công TỰ ĐỘNG từ sự kiện nhận diện (FR-2).

        Lần đầu trong ngày → INSERT (check_in); các lần sau → UPDATE
        check_out nếu mới hơn. KHÔNG đụng dòng manual_override (caller —
        AttendanceService — kiểm tra trước). Trả về dòng sau khi ghi.
        """
        day_id = _new_id()
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO attendance_days"
                " (id, person_id, work_date, shift_id, check_in_at, check_out_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, 'auto')"
                " ON CONFLICT(person_id, work_date) DO UPDATE SET"
                " check_out_at = COALESCE(excluded.check_out_at, attendance_days.check_out_at),"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (day_id, person_id, work_date, shift_id, check_in_at, check_out_at),
            )
            row = conn.execute(
                "SELECT * FROM attendance_days WHERE person_id = ? AND work_date = ?",
                (person_id, work_date),
            ).fetchone()
        if record_outbox:
            SyncOutboxRepository(self._db).add("attendance_day", row["id"], "upsert")
        return _row_to_attendance_day(row)

    def save(
        self,
        day: AttendanceDay,
        record_outbox: bool = True,
    ) -> AttendanceDay:
        """Lưu THAY ĐỔI TOÀN BỘ dòng ngày công (dùng cho sửa tay — FR-5).

        Ghi outbox 'upsert' để đẩy lên D1. ``updated_at`` tự làm mới.
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE attendance_days SET check_in_at = ?, check_out_at = ?,"
                " status = ?, manual_override = ?, note = ?,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                " WHERE id = ?",
                (
                    day.check_in_at,
                    day.check_out_at,
                    day.status,
                    1 if day.manual_override else 0,
                    day.note,
                    day.id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM attendance_days WHERE id = ?", (day.id,)
            ).fetchone()
        if record_outbox:
            SyncOutboxRepository(self._db).add("attendance_day", day.id, "upsert")
        return _row_to_attendance_day(row) if row else day

    def add_external(
        self,
        day: AttendanceDay,
        record_outbox: bool = True,
    ) -> AttendanceDay:
        """Chèn 1 dòng ngày công từ NGOÀI (kéo từ cloud về — FR-9).

        Giữ nguyên id + mọi trường; ``updated_at`` rỗng → SQL tự đặt DEFAULT.
        Không đụng outbox khi ``record_outbox=False`` (tránh vòng lặp sync).
        """
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO attendance_days"
                " (id, person_id, work_date, shift_id, check_in_at, check_out_at,"
                "  status, manual_override, note, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?,"
                "  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
                (
                    day.id,
                    day.person_id,
                    day.work_date,
                    day.shift_id,
                    day.check_in_at,
                    day.check_out_at,
                    day.status,
                    1 if day.manual_override else 0,
                    day.note,
                    day.updated_at or None,
                ),
            )
        if record_outbox:
            SyncOutboxRepository(self._db).add("attendance_day", day.id, "upsert")
        return day

    # -- Đọc -------------------------------------------------
    def get(self, day_id: str) -> AttendanceDay | None:
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT * FROM attendance_days WHERE id = ?", (day_id,)
            ).fetchone()
        return _row_to_attendance_day(row) if row else None

    def find(self, person_id: str, work_date: str) -> AttendanceDay | None:
        """Dòng công của người trong 1 ngày; None nếu chưa có (chưa chấm)."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT * FROM attendance_days WHERE person_id = ? AND work_date = ?",
                (person_id, work_date),
            ).fetchone()
        return _row_to_attendance_day(row) if row else None

    def list_all(self) -> list[AttendanceDay]:
        """Toàn bộ ngày công (dùng cho đợt đồng bộ ĐẦU TIÊN — FR-9)."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM attendance_days ORDER BY work_date ASC, person_id ASC"
            ).fetchall()
        return [_row_to_attendance_day(r) for r in rows]

    def list_month(self, year: int, month: int, person_id: str = "") -> list[AttendanceDay]:
        """Ngày công trong tháng (mọi người hoặc 1 người) — dùng bảng tháng FR-7."""
        date_prefix = f"{year:04d}-{month:02d}-"
        sql = "SELECT * FROM attendance_days WHERE work_date LIKE ?"
        params: list[Any] = [f"{date_prefix}%"]
        if person_id:
            sql += " AND person_id = ?"
            params.append(person_id)
        sql += " ORDER BY work_date ASC, person_id ASC"
        with self._db.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_attendance_day(r) for r in rows]


# =============================================================
# AttendanceAuditRepository — lịch sử sửa tay (attendance-spec FR-5, KHÔNG sync)
# =============================================================

class AttendanceAuditRepository:
    """Lịch sử sửa bản ghi công — log CỤC BỘ, không đồng bộ D1 (FR-9)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        attendance_day_id: str,
        action: str,
        old_value: str = "",
        new_value: str = "",
        detail: str = "",
    ) -> None:
        """Ghi 1 dòng audit; ``action``: edit_time / set_status / add_note."""
        if action not in ("edit_time", "set_status", "add_note"):
            raise ValueError(f"Hành động audit không hợp lệ: {action}")
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO attendance_audit"
                " (id, attendance_day_id, action, old_value, new_value, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_new_id(), attendance_day_id, action, old_value, new_value, detail),
            )

    def list_for_day(self, attendance_day_id: str) -> list[dict[str, str]]:
        """Audit của 1 ngày, MỚI NHẤT TRƯỚC (dict: action/old/new/edited_at/detail)."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT action, old_value, new_value, edited_at, detail"
                " FROM attendance_audit WHERE attendance_day_id = ?"
                " ORDER BY edited_at DESC",
                (attendance_day_id,),
            ).fetchall()
        return [
            {
                "action": r["action"],
                "old_value": r["old_value"],
                "new_value": r["new_value"],
                "edited_at": r["edited_at"],
                "detail": r["detail"],
            }
            for r in rows
        ]


def _validate_hhmm(value: str) -> None:
    """Kiểm tra chuỗi giờ 'HH:MM' hợp lệ (00:00–23:59); sai → ValueError."""
    try:
        hours, minutes = value.strip().split(":")
        if len(hours) != 2 or len(minutes) != 2:
            raise ValueError
        if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError(f"Giờ không hợp lệ (cần dạng HH:MM): {value!r}") from None
