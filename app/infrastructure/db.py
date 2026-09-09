"""Kết nối SQLite + khởi tạo lược đồ cơ sở dữ liệu (Bước 6).

Chứa:
- Lớp ``Database``: quản lý kết nối, bật các PRAGMA quan trọng
  (khóa ngoại + WAL), tự tạo bảng khi mở lần đầu.
- Lược đồ đầy đủ (5 bảng + index) theo spec mục 5.4 — cú pháp SQL
  dùng chung cho SQLite local và Cloudflare D1 (đồng bộ sau này).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import APP_DIR

logger = logging.getLogger(__name__)

# -------------------------------------------------------------
# Đường dẫn & phiên bản schema
# -------------------------------------------------------------
# Dữ liệu người dùng đặt ở APP_DIR (cạnh .exe khi đã đóng gói — Bước 12)
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "app.db"

SCHEMA_VERSION = 1  # tăng khi có thay đổi cấu trúc bảng (PRAGMA user_version)

# Khóa toàn cục tuần tự hóa giao dịch SQLite giữa các THREAD (Bước 15).
# SyncService chạy trong QThread (không đơ UI) nhưng dùng chung connection
# với main thread → mọi transaction phải qua session() và nằm trong khóa
# này để không interleave lẫn nhau (BEGIN/COMMIT không được lồng nhau).
_DB_LOCK = threading.RLock()


# -------------------------------------------------------------
# Lược đồ CSDL — giữ NGUYÊN cú pháp như spec 5.4 (dùng chung D1)
# -------------------------------------------------------------
SCHEMA_SQL = """
-- 1) persons — người đã đăng ký khuôn mặt
CREATE TABLE IF NOT EXISTS persons (
    id               TEXT PRIMARY KEY,  -- UUID v4 (hex) — KHÔNG dùng AUTOINCREMENT
    name             TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    thumbnail_path   TEXT NOT NULL,     -- đường dẫn ảnh đại diện (local)
    thumbnail_r2_key TEXT               -- NULL = chưa upload lên R2
);

-- 2) face_samples — các mẫu embedding (3–5 mẫu/người)
CREATE TABLE IF NOT EXISTS face_samples (
    id          TEXT PRIMARY KEY,       -- UUID v4
    person_id   TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,          -- 512 × float32 = 2048 bytes
    quality     REAL NOT NULL DEFAULT 0.0 CHECK (quality >= 0.0 AND quality <= 1.0),
    captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_face_samples_person ON face_samples(person_id);

-- 3) recognition_events — lịch sử nhận diện (audit trail, kèm ảnh snapshot)
CREATE TABLE IF NOT EXISTS recognition_events (
    id              TEXT PRIMARY KEY,   -- UUID v4
    person_id       TEXT REFERENCES persons(id) ON DELETE SET NULL,
    label           TEXT NOT NULL,      -- tên người, hoặc 'Người lạ' nếu is_unknown = 1
    source          TEXT NOT NULL CHECK (source IN ('webcam', 'photo', 'mobile')),
    detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    similarity      REAL,               -- điểm tương đồng cao nhất (0..1)
    snapshot_path   TEXT,               -- ảnh chụp local
    snapshot_r2_key TEXT,               -- NULL = chưa upload lên R2
    is_unknown      INTEGER NOT NULL DEFAULT 0 CHECK (is_unknown IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_events_detected_at ON recognition_events(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_person      ON recognition_events(person_id);
CREATE INDEX IF NOT EXISTS idx_events_source      ON recognition_events(source);

-- 4) sync_outbox — hàng đợi đồng bộ cloud (outbox pattern, dùng ở Bước 15)
CREATE TABLE IF NOT EXISTS sync_outbox (
    id         TEXT PRIMARY KEY,        -- UUID v4
    entity     TEXT NOT NULL CHECK (entity IN ('person', 'face_sample', 'recognition_event')),
    entity_id  TEXT NOT NULL,
    op         TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    synced_at  TEXT                     -- NULL = chưa đồng bộ
);
-- Partial index: chỉ quét hàng chưa đồng bộ — nhanh cho SyncService
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON sync_outbox(synced_at) WHERE synced_at IS NULL;

-- 5) settings — khóa-giá trị (ngưỡng, camera idx, ... — bổ sung cho config.json)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """Quản lý kết nối SQLite: mở kết nối, bật PRAGMA, tạo schema.

    Cách dùng:
        db = Database()            # dùng file mặc định data/app.db
        with db.session() as conn: # tự commit (hoặc rollback nếu có lỗi)
            conn.execute(...)
    """

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        """Đường dẫn file cơ sở dữ liệu."""
        return self._path

    # ---------------------------------------------------------
    # Kết nối
    # ---------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Mở kết nối (nếu chưa mở), bật PRAGMA và tạo schema khi cần.

        Dùng lazy: chỉ tạo file DB + bảng khi thực sự có thao tác đầu tiên.
        """
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: SyncService (QThread) dùng chung kết nối
            # với main thread — an toàn vì session() luôn giữ _DB_LOCK
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.row_factory = sqlite3.Row  # đọc cột theo tên (row["name"])
            conn.execute("PRAGMA foreign_keys = ON")   # bật khóa ngoại
            conn.execute("PRAGMA journal_mode = WAL")  # ghi bền, đọc/ghi song song
            self._conn = conn
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        """Tạo bảng nếu chưa có; quản lý phiên bản qua PRAGMA user_version."""
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
            logger.info("Đã khởi tạo CSDL schema v%d tại %s", SCHEMA_VERSION, self._path)

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """Ngữ cảnh giao dịch: commit khi thành công, rollback khi có lỗi.

        Toàn bộ thân giao dịch nằm trong ``_DB_LOCK`` để các THREAD khác
        nhau (UI + QThread đồng bộ) không xen kẽ transaction của nhau.
        """
        conn = self.connect()
        with _DB_LOCK:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        """Đóng kết nối (gọi khi thoát app)."""
        with _DB_LOCK:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
