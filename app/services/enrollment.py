"""Dịch vụ đăng ký khuôn mặt — use case FR-1 (Bước 7).

Nhận danh sách các mẫu đã thu (embedding + chất lượng + ảnh crop khuôn
mặt) từ EnrollmentDialog, rồi:
  1. Tạo bản ghi ``persons`` (tên, thumbnail = ảnh crop mặt tốt nhất).
  2. Tạo các bản ghi ``face_samples`` (3–5 mẫu embedding/người).

UI không thao tác repository trực tiếp mà gọi qua service này
(theo kiến trúc phân lớp — spec mục 5.1).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from app.infrastructure.db import DATA_DIR, Database
from app.infrastructure.repositories import (
    FaceSampleRepository,
    Person,
    PersonRepository,
)

logger = logging.getLogger(__name__)

THUMBS_DIR = DATA_DIR / "thumbs"


@dataclass
class CapturedSample:
    """Một khung hình tốt đã được chọn trong lúc đăng ký."""

    embedding: np.ndarray  # float32 (512,)
    quality: float         # điểm tin cậy của phát hiện (0..1)
    face_crop: np.ndarray  # ảnh crop khuôn mặt (BGR) — dùng làm thumbnail


class EnrollmentService:
    """Lưu người mới cùng các mẫu embedding vào CSDL."""

    def __init__(self, db: Database, thumbs_dir: Path = THUMBS_DIR) -> None:
        self._db = db
        self._people = PersonRepository(db)
        self._samples = FaceSampleRepository(db)
        self._thumbs_dir = thumbs_dir

    # ---------------------------------------------------------
    # Use case chính
    # ---------------------------------------------------------
    def save_person(self, name: str, samples: list[CapturedSample]) -> Person:
        """Lưu người + embedding. Trả về Person đã lưu.

        Ném ValueError nếu tên trống hoặc không có mẫu nào.
        """
        name = name.strip()
        if not name:
            raise ValueError("Tên không được để trống")
        if not samples:
            raise ValueError("Không có mẫu embedding nào để lưu")

        # 1) Tạo thumbnail (ảnh crop mặt của mẫu đầu tiên — đại diện)
        self._thumbs_dir.mkdir(parents=True, exist_ok=True)
        person = self._people.add(name=name, thumbnail_path="")
        thumb_path = self._save_thumbnail(person.id, samples[0].face_crop)
        self._people.update_thumbnail(person.id, thumb_path)

        # 2) Lưu từng mẫu embedding
        for sample in samples:
            self._samples.add(
                person_id=person.id,
                embedding=sample.embedding,
                # Chuyển quality từ thang 0-100 (SampleQuality.overall)
                # sang 0-1 (DB CHECK constraint: 0.0 ≤ quality ≤ 1.0)
                quality=float(sample.quality) / 100.0 if sample.quality > 1.0 else float(sample.quality),
            )

        # Đọc lại từ DB để trả Person với thumbnail_path đã cập nhật
        person = self._people.get(person.id)
        logger.info(
            "Đăng ký thành công: '%s' (id=%s) với %d mẫu",
            person.name, person.id, len(samples),
        )
        return person

    # ---------------------------------------------------------
    # Nội bộ
    # ---------------------------------------------------------
    def _save_thumbnail(self, person_id: str, face_crop: np.ndarray) -> str:
        """Lưu ảnh crop khuôn mặt làm thumbnail; trả đường dẫn tương đối với DATA_DIR."""
        filename = f"{person_id}.jpg"
        abs_path = self._thumbs_dir / filename
        # Nén JPEG chất lượng cao (ảnh nhỏ ~10-30KB)
        cv2.imwrite(
            str(abs_path),
            face_crop,
            [cv2.IMWRITE_JPEG_QUALITY, 92],
        )
        # Lưu đường dẫn TƯƠNG ĐỐI với thư mục dữ liệu (di động khi đổi máy)
        return str(self._thumbs_dir.relative_to(DATA_DIR) / filename)
