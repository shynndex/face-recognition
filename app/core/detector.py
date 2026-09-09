"""Phát hiện khuôn mặt — SCRFD (module detection của bộ model buffalo_l).

Chỉ tải module `detection` (nhẹ hơn, nhanh hơn tải toàn bộ bộ model).
Kết quả mỗi khuôn mặt là đối tượng Face có:
  - face.bbox      : [x1, y1, x2, y2]
  - face.det_score : độ tin cậy (0..1)
  - face.kps       : 5 điểm landmark (mắt, mũi, miệng)
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from insightface.app import FaceAnalysis

from app.config import RESOURCE_DIR

logger = logging.getLogger(__name__)

# FaceAnalysis tìm model tại <root>/models/buffalo_l.
# Mã nguồn: RESOURCE_DIR = thư mục project; .exe: RESOURCE_DIR = bundle
# (cạnh exe đối với onedir, _MEIPASS đối với onefile — Bước 12).
MODELS_ROOT = RESOURCE_DIR / "models"

# ONNX Runtime chạy GPU qua DirectML (fallback CPU)
PROVIDERS = ["DmlExecutionProvider", "CPUExecutionProvider"]


class FaceDetector:
    """Bọc SCRFD: nhận ảnh BGR → trả về danh sách khuôn mặt phát hiện."""

    def __init__(
        self,
        models_root: Path = MODELS_ROOT,
        det_thresh: float = 0.5,
        det_size: tuple[int, int] = (640, 640),
    ) -> None:
        # Lưu ý: insightface 1.0.1 bị segfault với allowed_modules=['detection']
        # → dùng FaceAnalysis đầy đủ (tải cả bộ model, chỉ đọc kết quả detection).
        self._app = FaceAnalysis(
            name="buffalo_l",
            root=str(models_root),
            providers=PROVIDERS,
        )
        self._app.prepare(ctx_id=0, det_size=det_size, det_thresh=det_thresh)
        logger.info("FaceDetector sẵn sàng (SCRFD buffalo_l)")

    def detect(self, img_bgr: np.ndarray) -> list:
        """Phát hiện khuôn mặt trong ảnh BGR; trả danh sách Face (bbox, score, kps)."""
        return self._app.get(img_bgr)

    @property
    def app(self) -> FaceAnalysis:
        """FaceAnalysis dùng chung — cho FaceEmbedder tái sử dụng model đã nạp."""
        return self._app
