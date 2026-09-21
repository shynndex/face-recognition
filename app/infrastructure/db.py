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

SCHEMA_VERSION = 4  # tăng khi có thay đổi cấu trúc bảng (PRAGMA user_version)

# Khóa toàn cục tuần tự hóa giao dịch SQLite giữa các THREAD (Bước 15).
# SyncService chạy trong QThread (không đơ UI) nhưng dùng chung connection
# với main thread → mọi transaction phải qua session() và nằm trong khóa
# này để không interleave lẫn nhau (BEGIN/COMMIT không được lồng nhau).
_DB_LOCK = threading.RLock()


# Định nghĩa sync_outbox tách riêng — SCHEMA_SQL dùng khi tạo DB mới,
# _migrate_v1_to_v2 dùng khi dựng LẠI bảng cho DB cũ v1 (CHECK entity cũ
# không chứa 'shift'/'attendance_day' → ghi outbox chấm công sẽ crash).
SYNC_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS sync_outbox (
    id         TEXT PRIMARY KEY,        -- UUID v4
    entity     TEXT NOT NULL CHECK (entity IN ('person', 'face_sample', 'recognition_event', 'shift', 'attendance_day')),
    entity_id  TEXT NOT NULL,
    op         TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    synced_at  TEXT                     -- NULL = chưa đồng bộ
)
"""


# -------------------------------------------------------------
# Lược đồ CSDL — giữ NGUYÊN cú pháp như spec 5.4 (dùng chung D1)
# -------------------------------------------------------------
SCHEMA_SQL = f"""
-- 1) persons — người đã đăng ký khuôn mặt
CREATE TABLE IF NOT EXISTS persons (
    id               TEXT PRIMARY KEY,  -- UUID v4 (hex) — KHÔNG dùng AUTOINCREMENT
    name             TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    thumbnail_path   TEXT NOT NULL,     -- đường dẫn ảnh đại diện (local)
    thumbnail_r2_key TEXT,               -- NULL = chưa upload lên R2
    shift_id         TEXT REFERENCES shifts(id) ON DELETE SET NULL -- NULL = ca mặc định
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
{SYNC_OUTBOX_DDL};
-- Partial index: chỉ quét hàng chưa đồng bộ — nhanh cho SyncService
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON sync_outbox(synced_at) WHERE synced_at IS NULL;

-- 5) settings — khóa-giá trị (ngưỡng, camera idx, ... — bổ sung cho config.json)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 6) shifts — ca làm việc (attendance-spec FR-3): giờ quy định + dung sai trễ
CREATE TABLE IF NOT EXISTS shifts (
    id            TEXT PRIMARY KEY,      -- UUID v4 (hex)
    name          TEXT NOT NULL CHECK (length(trim(name)) > 0),
    start_time    TEXT NOT NULL,         -- 'HH:MM' giờ ĐỊA PHƯƠNG
    end_time      TEXT NOT NULL,         -- 'HH:MM' — <= start_time = ca đêm (qua nửa đêm)
    factor        REAL NOT NULL DEFAULT 1.0 CHECK (factor > 0),  -- hệ số lương ca (lương thô)
    break_start   TEXT,                  -- 'HH:MM' đầu nghỉ giữa ca (NULL = không nghỉ cố định)
    break_end     TEXT,                  -- 'HH:MM' hết nghỉ — <= break_start = nghỉ qua nửa đêm
    grace_minutes INTEGER NOT NULL DEFAULT 10 CHECK (grace_minutes >= 0),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 7) attendance_days — bản ghi ngày công (1 dòng / người / ngày — FR-1)
CREATE TABLE IF NOT EXISTS attendance_days (
    id              TEXT PRIMARY KEY,    -- UUID v4 (hex)
    person_id       TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    work_date       TEXT NOT NULL,       -- 'YYYY-MM-DD' theo NGÀY BẮT ĐẦU CA (giờ địa phương)
    shift_id        TEXT REFERENCES shifts(id) ON DELETE SET NULL, -- ca áp dụng lúc tính
    check_in_at     TEXT,                -- ISO UTC; NULL = chưa có giờ vào
    check_out_at    TEXT,                -- ISO UTC; NULL = thiếu giờ ra
    status          TEXT NOT NULL DEFAULT 'auto'
                    CHECK (status IN ('auto', 'leave', 'trip', 'manual')),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
    note            TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (person_id, work_date)        -- chống trùng dòng công cùng ngày
);
CREATE INDEX IF NOT EXISTS idx_attendance_person_date
    ON attendance_days(person_id, work_date DESC);
CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance_days(work_date);

-- 8) attendance_audit — lịch sử sửa tay bản ghi công (FR-5; KHÔNG sync D1)
CREATE TABLE IF NOT EXISTS attendance_audit (
    id                 TEXT PRIMARY KEY, -- UUID v4 (hex)
    attendance_day_id  TEXT NOT NULL REFERENCES attendance_days(id) ON DELETE CASCADE,
    action             TEXT NOT NULL CHECK (action IN ('edit_time', 'set_status', 'add_note')),
    old_value          TEXT NOT NULL DEFAULT '',
    new_value          TEXT NOT NULL DEFAULT '',
    edited_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    detail             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_attendance_audit_day
    ON attendance_audit(attendance_day_id, edited_at DESC);
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
            if version < 2:
                self._migrate_v1_to_v2()
            if version < 3:
                self._migrate_v2_to_v3()
            if version < 4:
                self._migrate_v3_to_v4()
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
            logger.info("Đã khởi tạo CSDL schema v%d tại %s", SCHEMA_VERSION, self._path)

    def _migrate_v1_to_v2(self) -> None:
        """Nâng cấp DB v1 → v2 (chấm công — attendance-spec FR-1).

        DB v1 có 2 điểm không tương thích với schema mới:
          1. ``persons`` thiếu cột ``shift_id`` → ALTER TABLE ADD COLUMN.
          2. ``sync_outbox`` có CHECK entity cũ (không chứa 'shift' /
             'attendance_day') → dựng lại bảng, giữ nguyên dữ liệu outbox
             đang chờ đồng bộ (SQLite không sửa được CHECK nên phải rebuild).
        Các bảng MỚI (shifts/attendance_days/attendance_audit) đã được
        executescript(SCHEMA_SQL) tạo ở trên nhờ IF NOT EXISTS.
        """
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(persons)").fetchall()
        }
        if "shift_id" not in cols:
            self._conn.execute(
                "ALTER TABLE persons ADD COLUMN shift_id TEXT"
                " REFERENCES shifts(id) ON DELETE SET NULL"
            )
            logger.info("Migration v1→v2: đã thêm cột persons.shift_id")

        # Rebuild sync_outbox (CHECK mới). DROP INDEX trước — index gắn bảng cũ.
        self._conn.execute("DROP INDEX IF EXISTS idx_outbox_pending")
        self._conn.execute("ALTER TABLE sync_outbox RENAME TO sync_outbox_v1_old")
        self._conn.executescript(SYNC_OUTBOX_DDL)
        self._conn.execute(
            "INSERT INTO sync_outbox (id, entity, entity_id, op, created_at, synced_at)"
            " SELECT id, entity, entity_id, op, created_at, synced_at"
            " FROM sync_outbox_v1_old"
        )
        self._conn.execute("DROP TABLE sync_outbox_v1_old")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_pending"
            " ON sync_outbox(synced_at) WHERE synced_at IS NULL"
        )
        logger.info("Migration v1→v2: đã dựng lại sync_outbox với entity chấm công")

    def _migrate_v2_to_v3(self) -> None:
        """Nâng cấp DB v2 → v3 (lương thô — hệ số ca theo attendance-spec FR-3).

        Thêm cột ``shifts.factor`` (REAL DEFAULT 1.0) — ca mới 1.0 trừ khi
        chỉnh tay; ngày công giữ ``shift_id`` của lúc tính nên tổng lương
        tháng không đổi khi sửa ca sau này (đúng nguyên tắc spec: ngày đã
        ghi giữ nguyên shift_id đã dùng).
        """
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(shifts)").fetchall()
        }
        if "factor" not in cols:
            self._conn.execute(
                "ALTER TABLE shifts ADD COLUMN factor REAL NOT NULL DEFAULT 1.0"
                " CHECK (factor > 0)"
            )
            logger.info("Migration v2→v3: đã thêm cột shifts.factor")

    def _migrate_v3_to_v4(self) -> None:
        """Nâng cấp DB v3 → v4 (nghỉ giữa ca theo attendance-spec FR-3/FR-4).

        Thêm 2 cột ``shifts.break_start`` / ``shifts.break_end`` ('HH:MM'
        địa phương, NULL = ca không có khoảng nghỉ cố định). Ca cũ giữ
        nguyên hành vi (không trừ gì) — chỉ ca mới cấu hình nghỉ mới trừ.
        """
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(shifts)").fetchall()
        }
        if "break_start" not in cols:
            self._conn.execute(
                "ALTER TABLE shifts ADD COLUMN break_start TEXT"
            )
            self._conn.execute(
                "ALTER TABLE shifts ADD COLUMN break_end TEXT"
            )
            logger.info("Migration v3→v4: đã thêm cột shifts.break_start/break_end")

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
