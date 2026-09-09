"""PhotoView — nhận diện ảnh tĩnh (Bước 10, wireframe 5.5.8).

Luồng dùng: [ Mở ảnh… ] → hiển thị ảnh gốc → [ Nhận diện ] chạy pipeline
MỘT LẦN trên QThread (không đơ UI với ảnh lớn) → mỗi khuôn mặt một khung
+ tên + điểm (hoặc "Người lạ" khung đỏ) → tự động lưu recognition_events
với ``source='photo'``.

Với người lạ trong ảnh: [ Đăng ký ngay ] mở EnrollmentDialog với ảnh crop
làm MẪU 1 (giống Bước 9) — sau khi đăng ký xong, tự chạy lại nhận diện để
người đó giờ được nhận diện trong ảnh.

Tái sử dụng (không viết lại): RecognitionService (nạp embedding + so khớp
+ ghi sự kiện), FaceDetector/FaceEmbedder (Bước 4-5), các hàm vẽ/crop của
CameraWorker (Bước 9), seed_sample của EnrollmentDialog (Bước 9).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.core import face_metrics
from app.core.detector import FaceDetector, MODELS_ROOT
from app.core.embedder import FaceEmbedder
from app.core.preprocess import preprocess_frame
from app.infrastructure.db import Database
from app.services.enrollment import CapturedSample
from app.services.recognition import RecognitionService
from app.ui.camera_view import (
    KNOWN_COLOR,
    SPOOF_COLOR,
    UNKNOWN_COLOR,
    CameraWorker,
)
from app.ui.enrollment_dialog import EnrollmentDialog

logger = logging.getLogger(__name__)


@dataclass
class PhotoFaceResult:
    """Kết quả nhận diện MỘT khuôn mặt trong ảnh."""

    index: int                # số thứ tự hiển thị (1-based)
    label: str                # tên người hoặc "Người lạ"
    similarity: float | None  # điểm tương đồng (người lạ → None)
    is_unknown: bool
    person_id: str | None
    bbox: tuple               # (x1, y1, x2, y2)
    crop: np.ndarray | None   # ảnh crop khuôn mặt (làm mẫu 1 khi đăng ký ngay)
    embedding: np.ndarray | None
    quality: float
    occluded: bool = False    # mặt bị che khuất / không rõ (Bước 18)


class PhotoWorker(QObject):
    """Nhận diện một ảnh tĩnh MỘT LẦN — chạy trong QThread (ảnh lớn không đơ UI)."""

    finished = Signal(object, object)  # (ảnh đã vẽ BGR, list[PhotoFaceResult])
    error = Signal(str)

    def __init__(
        self,
        image: np.ndarray,
        detector: FaceDetector,
        embedder: FaceEmbedder,
        service: RecognitionService | None,
        threshold: float,
    ) -> None:
        super().__init__()
        self._image = image
        self._detector = detector
        self._embedder = embedder
        self._service = service
        self._threshold = threshold

    @Slot()
    def run(self) -> None:
        # Bước 21: CLAHE preprocessing — detect trên ảnh đã chuẩn hóa ánh sáng
        detect_image = preprocess_frame(self._image)
        try:
            faces = self._detector.detect(detect_image)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Lỗi phát hiện khuôn mặt trong ảnh")
            self.error.emit(f"Không nhận diện được ảnh: {exc}")
            return

        frame = self._image.copy()
        results: list[PhotoFaceResult] = []
        for i, face in enumerate(faces, start=1):
            x1, y1, x2, y2 = face.bbox.astype(int)
            embedding = self._embedder.embed_face(face) if self._embedder else None

            result = None
            if self._service is not None and embedding is not None:
                result = self._service.match(embedding, self._threshold)

            crop = CameraWorker._crop_face(self._image, face)  # crop từ ảnh GỐC
            occ = face_metrics.occlusion_score(face, self._image)
            occluded = occ >= face_metrics.OCCLUSION_WARN
            if result is not None:
                label = self._service.label_of(result.person_id) if self._service else "?"
                if occluded:
                    label += " ⚠ bị che"
                CameraWorker._draw_label(
                    frame,
                    f"{label} ({result.similarity:.2f})",
                    x1, max(0, y1 - 8),
                    SPOOF_COLOR if occluded else KNOWN_COLOR,
                )
                cv2.rectangle(frame, (x1, y1), (x2, y2), KNOWN_COLOR, 2)
                results.append(
                    PhotoFaceResult(
                        index=i, label=label, similarity=result.similarity,
                        is_unknown=False, person_id=result.person_id,
                        bbox=(x1, y1, x2, y2), crop=crop,
                        embedding=embedding, quality=float(face.det_score),
                        occluded=occluded,
                    )
                )
            else:
                label = "⚠ Mặt bị che / không rõ" if occluded else "Người lạ"
                CameraWorker._draw_label(
                    frame, label, x1, max(0, y1 - 8),
                    SPOOF_COLOR if occluded else UNKNOWN_COLOR,
                )
                cv2.rectangle(frame, (x1, y1), (x2, y2), UNKNOWN_COLOR, 2)
                results.append(
                    PhotoFaceResult(
                        index=i, label=label, similarity=None,
                        is_unknown=True, person_id=None,
                        bbox=(x1, y1, x2, y2), crop=crop,
                        embedding=embedding, quality=float(face.det_score),
                        occluded=occluded,
                    )
                )

        self.finished.emit(frame, results)


class PhotoView(QWidget):
    """Trang nhận diện ảnh tĩnh (FR-3)."""

    def __init__(
        self,
        config: Config,
        db: Database,
        detector: FaceDetector | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._db = db
        self._detector = detector
        self._embedder: FaceEmbedder | None = None
        self._service = RecognitionService(db)
        self._thread: QThread | None = None
        self._worker: PhotoWorker | None = None
        self._image: np.ndarray | None = None
        self._image_path: str = ""
        self._results: list[PhotoFaceResult] = []
        self._build_ui()

    # ---------------------------------------------------------
    # Giao diện (wireframe 5.5.8)
    # ---------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Thanh công cụ: mở ảnh + tên file + nút nhận diện
        toolbar = QHBoxLayout()
        open_btn = QPushButton("📂 Mở ảnh…")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(self._open_image)
        toolbar.addWidget(open_btn)

        self._file_label = QLabel("Chưa chọn ảnh")
        self._file_label.setStyleSheet("font-size: 15px;")
        toolbar.addWidget(self._file_label, stretch=1)

        self._analyze_btn = QPushButton("Nhận diện")
        self._analyze_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._analyze_btn.setEnabled(False)  # chưa có ảnh
        self._analyze_btn.clicked.connect(self._analyze)
        toolbar.addWidget(self._analyze_btn)
        layout.addLayout(toolbar)

        # Vùng chính: ảnh + bảng kết quả
        body = QHBoxLayout()
        body.setSpacing(12)

        self._image_label = QLabel("Mở một ảnh (jpg/png/webp) để nhận diện khuôn mặt")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(560, 420)
        self._image_label.setObjectName("imageLabel")
        self._image_label.setStyleSheet("font-size: 18px;")
        body.addWidget(self._image_label, stretch=1)

        # Bảng kết quả bên phải
        panel = QVBoxLayout()
        panel.setSpacing(8)
        results_title = QLabel("KẾT QUẢ")
        results_title.setObjectName("sectionTitle")
        panel.addWidget(results_title)

        self._results_list = QListWidget()
        # Màu nền/viền do theme QSS quyết định — chỉ giữ bo góc
        self._results_list.setStyleSheet("border-radius: 6px;")
        panel.addWidget(self._results_list, stretch=1)

        self._status_label = QLabel("—")
        self._status_label.setStyleSheet("font-size: 15px;")
        panel.addWidget(self._status_label)
        body.addLayout(panel)
        layout.addLayout(body, stretch=1)

        hint = QLabel("Mẹo: ảnh càng rõ mặt, càng ít người → kết quả càng chính xác")
        hint.setStyleSheet("font-size: 14px;")
        layout.addWidget(hint)

    # ---------------------------------------------------------
    # Mở ảnh
    # ---------------------------------------------------------
    def _open_image(self) -> None:
        """Chọn file ảnh → hiển thị ảnh gốc (chưa nhận diện)."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn ảnh",
            "",
            "Ảnh (*.jpg *.jpeg *.png *.webp);;Tất cả (*.*)",
        )
        if not path:
            return
        image = cv2.imread(path)
        if image is None:
            QMessageBox.warning(self, "Lỗi ảnh", f"Không đọc được ảnh:\n{path}")
            return
        self._image = image
        self._image_path = path
        self._file_label.setText(f"Ảnh: {path.split('/')[-1]} ({image.shape[1]}×{image.shape[0]})")
        self._analyze_btn.setEnabled(True)
        self._results_list.clear()
        self._status_label.setText("Đã tải ảnh — bấm [Nhận diện]")
        self._display(image)

    # ---------------------------------------------------------
    # Nhận diện
    # ---------------------------------------------------------
    def _analyze(self) -> None:
        """Chạy pipeline nhận diện ảnh MỘT LẦN trên QThread."""
        if self._image is None:
            return
        # Chống bấm [Nhận diện] 2 lần (kể cả trong lúc đang nạp model —
        # nếu không sẽ có 2 worker chạy song song và ghi TRÙNG sự kiện)
        if self._thread is not None and self._thread.isRunning():
            return
        self._analyze_btn.setEnabled(False)
        self._status_label.setText("Đang nhận diện...")

        # Nạp model (lần đầu) + embedding đã đăng ký (LUỒNG UI — trước khi chạy)
        if self._detector is None:
            self._status_label.setText("Đang nạp model nhận diện...")
            QApplication.processEvents()
            self._detector = self._load_detector()
            if self._detector is None:
                self._analyze_btn.setEnabled(True)  # bật lại nếu model lỗi
                self._status_label.setText("⚠ Không nạp được model")
                return
        if self._embedder is None:
            self._embedder = FaceEmbedder(self._detector)
        try:
            self._service.reload()
        except Exception:  # noqa: BLE001
            logger.exception("Lỗi nạp embedding cho nhận diện ảnh")
            self._service = RecognitionService(self._db)

        self._thread = QThread(self)
        self._worker = PhotoWorker(
            self._image,
            detector=self._detector,
            embedder=self._embedder,
            service=self._service,
            threshold=self._config.recognition_threshold,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _load_detector(self) -> FaceDetector | None:
        """Tạo FaceDetector; None nếu model thiếu/lỗi (hiện thông báo)."""
        try:
            if not (MODELS_ROOT / "models" / "buffalo_l").exists():
                raise FileNotFoundError(
                    f"Chưa có model tại {MODELS_ROOT / 'models' / 'buffalo_l'}"
                )
            detector = FaceDetector(MODELS_ROOT)
            logger.info("Đã nạp model phát hiện khuôn mặt (photo)")
            return detector
        except Exception as exc:  # noqa: BLE001
            logger.exception("Không nạp được model: %s", exc)
            self._status_label.setText(f"⚠ {exc}")
            return None

    # ---------------------------------------------------------
    # Xử lý kết quả
    # ---------------------------------------------------------
    @Slot(object, object)
    def _on_finished(self, frame: np.ndarray, results: list) -> None:
        """Hiển thị ảnh đã vẽ + bảng kết quả + lưu sự kiện (source='photo')."""
        self._stop_thread()
        self._analyze_btn.setEnabled(True)
        self._results = results
        self._display(frame)
        self._populate_results(results)

        # Ghi sự kiện nhận diện (LUỒNG UI — worker không đụng DB)
        saved = 0
        for r in results:
            if r.is_unknown:
                if r.crop is not None:
                    self._service.save_event(
                        person_id=None, similarity=None,
                        face_crop=r.crop, is_unknown=True, source="photo",
                    )
                    saved += 1
            else:
                self._service.save_event(
                    person_id=r.person_id, similarity=r.similarity,
                    face_crop=r.crop, source="photo",
                )
                saved += 1
        self._status_label.setText(
            f"✓ Đã lưu {saved} sự kiện · {len(results)} khuôn mặt"
        )

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._stop_thread()
        self._analyze_btn.setEnabled(True)
        self._status_label.setText(f"⚠ {message}")

    def _stop_thread(self) -> None:
        """Dừng luồng nhận diện: worker đã xong → thoát event loop của QThread.

        QUAN TRỌNG: QThread mặc định chạy event loop MÃI (exec()) — việc
        worker.run() kết thúc KHÔNG tự dừng thread. Nếu không gọi quit(),
        thread rò rỉ và bị hủy khi đang chạy lúc view đóng → crash
        'QThread: Destroyed while thread is still running'.
        """
        thread = self._thread
        self._thread = None
        self._worker = None
        if thread is not None:
            thread.quit()
            if not thread.wait(1500):
                # Chưa kịp dừng (đang xử lý ảnh lớn) → GIỮ tham chiếu, tránh
                # hủy C++ object khi luồng còn chạy; sẽ tự dừng khi xong
                self._thread = thread

    def _populate_results(self, results: list) -> None:
        """Bảng kết quả: mỗi khuôn mặt một dòng; người lạ kèm [Đăng ký ngay]."""
        self._results_list.clear()
        for r in results:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(8, 6, 8, 6)
            layout.setSpacing(8)

            if r.is_unknown:
                text = QLabel(f"{r.index}. ⚠ {r.label}")
                text.setStyleSheet("color: #c33; font-weight: bold; font-size: 14px;")
            else:
                score = f"{r.similarity:.2f}" if r.similarity is not None else "—"
                warn = " ⚠ bị che" if r.occluded else ""
                text = QLabel(f"{r.index}. {r.label}{warn} · {score}")
                text.setStyleSheet("font-size: 14px;")
            layout.addWidget(text, stretch=1)

            if r.is_unknown and r.embedding is not None:
                enroll_btn = QPushButton("Đăng ký ngay")
                enroll_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                enroll_btn.setObjectName("accentBtn")
                enroll_btn.setStyleSheet("border-radius: 5px; padding: 4px 10px; font-size: 13px;")
                enroll_btn.clicked.connect(
                    lambda _=False, r=r: self._on_enroll(r)
                )
                layout.addWidget(enroll_btn)

            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self._results_list.addItem(item)
            self._results_list.setItemWidget(item, row)

    def _on_enroll(self, r: PhotoFaceResult) -> None:
        """[Đăng ký ngay]: ảnh crop người lạ làm MẪU 1 → sau đó chạy lại nhận diện."""
        seed = CapturedSample(
            embedding=r.embedding,
            quality=r.quality,
            face_crop=r.crop,
        )
        dialog = EnrollmentDialog(
            self._config,
            self._db,
            detector=self._detector,
            seed_sample=seed,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            logger.info("Đăng ký ngay từ ảnh thành công — chạy lại nhận diện")
            self._analyze()  # người giờ đã đăng ký → nhận diện lại để thấy tên

    # ---------------------------------------------------------
    # Hiển thị ảnh
    # ---------------------------------------------------------
    def _display(self, image: np.ndarray) -> None:
        """Chuyển ảnh BGR → pixmap vừa khung hiển thị."""
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        self._image_label.setPixmap(
            pixmap.scaled(
                self._image_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # ---------------------------------------------------------
    # Vòng đời
    # ---------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 (chuẩn Qt)
        """Đóng view: dừng luồng nhận diện nếu còn chạy (không để crash khi thoát)."""
        self._stop_thread()
        super().closeEvent(event)
