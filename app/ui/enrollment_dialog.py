"""EnrollmentDialog — đăng ký khuôn mặt đơn giản 4 bước (Bước 24).

Quy trình đơn giản hóa (từ 6 → 4 bước):

    Nhìn thẳng(2s) → Quay trái → Quay phải → Nhìn thẳng(2s) → Face verification

Bỏ 2 bước khó trên webcam laptop:
  - Bỏ "Chớp mắt" (quá khó detect ở 10-15 FPS)
  - Bỏ "Mở miệng/Cười" (baseline detection không ổn định)

Mỗi bước: hệ thống TỰ PHÁT HIỆN hành động bằng chỉ số khuôn mặt
(app/core/face_metrics.py) dựa trên landmark 68 điểm + 5 điểm kps:
  - Nhìn thẳng  : mũi gần tâm (nose_shift < 0.20) + mắt mở RÕ (EAR > 0.30)
  - Quay trái   : nose_shift < -0.25
  - Quay phải   : nose_shift > +0.25

Bước 24 bổ sung: Auto-retry khi mẫu bị reject (mờ/tối), tự động chụp
lại ≤3 lần + hiện lý do realtime.

Face verification: so sánh embedding mẫu 4 (nhìn thẳng) với mẫu 1
(nhìn thẳng) — cosine ≥ 0.45 → đồng nhất.

Kiến trúc luồng giống CameraView: EnrollmentWorker chạy trong QThread.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
)

from app.config import Config
from app.core import face_metrics
from app.core.detector import FaceDetector, MODELS_ROOT
from app.core.embedder import FaceEmbedder
from app.core.preprocess import preprocess_frame
from app.infrastructure.camera import CameraCapture
from app.infrastructure.db import Database
from app.services.enrollment import CapturedSample, EnrollmentService

logger = logging.getLogger(__name__)

# Giới hạn thời gian thu (giây) — spec: "tự dừng khi đủ mẫu hoặc quá giới hạn"
TIMEOUT_SECONDS = 60
# Ngưỡng chất lượng frame (Bước 24: hạ cho webcam laptop)
MIN_FACE_SCORE = 0.60    # hạ từ 0.70 — webcam laptop ảnh mờ hơn
MIN_FACE_SIZE = 100       # hạ từ 120 — chấp nhận mặt nhỏ hơn
MIN_BRIGHTNESS = 30       # hạ từ 40 — webcam laptop tối hơn
MAX_BRIGHTNESS = 235      # nới từ 220 — chấp nhận sáng hơn chút
# Độ nét tối thiểu của vùng mặt (Laplacian variance). Hạ từ 40 xuống 25 —
# webcam laptop rung nhẹ, Laplacian thấp hơn (lena nét ≈ 537, webcam thường
# ≈ 80-200, laptop mờ ≈ 30-60).
MIN_SHARPNESS = 25
# Số khung gần đây giữ trong đệm để chọn khung NÉT NHẤT khi chụp mẫu
BUFFER_FRAMES = 5
# Chờ tối thiểu giữa 2 mẫu (giây) — để người dùng kịp chuyển tư thế
MIN_GAP_SECONDS = 0.8
# Số frame liên tiếp điều kiện đúng → mới chụp.
# Hạ xuống 2 (từ 3) — webcam laptop 10-15 FPS, đeo kính → EAR dao
# động quanh ngưỡng → cần ít frame liên tiếp hơn để pass.
FRAMES_TO_CONFIRM = 2
# Số frame thất bại được DUNG THỨ trước khi đếm lại từ đầu — hấp thụ rung
# nhẹ (mặt lắc vài chục ms trong lúc giữ yên không phải bỏ cuộc từ đầu).
# Tăng lên 3 (từ 2) — webcam laptop 10-15 FPS dễ mất 1-2 frame giữa chừng
# khi mặt hơi lắc, không nên reset stable_frames vì vậy.
GRACE_FRAMES = 3
# Thời gian tối thiểu PHẢI GIỮ NGUYÊN hành động (giây) trước khi chụp.
# Hạ xuống 0.6s (từ 0.8) — webcam laptop FPS thấp, giữ 0.8s nhiều
# khi bị timeout giữa chừng. 0.6s đủ để lấy frame nét.
MIN_HOLD_SECONDS = 0.6
# Số lần retry tối đa khi mẫu bị reject (Bước 24 — auto-retry)
MAX_AUTO_RETRIES = 3
# Ngưỡng Face verification: cosine mẫu cuối (nhìn thẳng) vs mẫu đầu (nhìn thẳng)
# phải ≥ ngưỡng này. Ngưỡng 0.45 đủ thấp để chấp nhận webcam laptop (ảnh mờ hơn)
# nhưng đủ cao để phát hiện "không phải cùng người".
VERIFY_THRESHOLD = 0.45

# ── Bước 25: Ngưỡng mờ THÍCH NGHI theo chính webcam ──────────────
# Ngưỡng tuyệt đối MIN_QUALITY_BLUR (=60) lệch nặng giữa các máy: máy dev
# đo ~104 còn webcam người dùng chỉ ~43-44 DÙ GIỮ YÊN → mọi mẫu bị reject
# → "Không thu đủ mẫu" (bug thực tế 2026-09-03). Giải pháp: chấp nhận mẫu
# khi độ nét ≥ max(sàn, 60% độ nét CAO NHẤT đã thấy trong phiên) —
#   - máy nét (max ~104)  → ngưỡng ~62: vẫn lọc rung như trước;
#   - webcam nền kém (max ~45) → ngưỡng 27, sàn 30 → mẫu ~43 ĐẠT;
#   - vẫn chặn frame tụt mạnh so với chính webcam đó (rung lúc chụp).
ADAPTIVE_BLUR_FLOOR = 30.0    # sàn tuyệt đối — dưới đây là mờ nặng thật sự
ADAPTIVE_BLUR_RATIO = 0.60    # mẫu phải đạt ≥ 60% độ nét nền của webcam

# Màu vẽ (BGR)
OVAL_COLOR_OK = (0, 200, 0)
OVAL_COLOR_WAIT = (0, 160, 255)
TEXT_COLOR = (255, 255, 255)


@dataclass(frozen=True)
class GuidanceStep:
    """Một bước trong chuỗi hướng dẫn đăng ký."""

    name: str                     # tên bước hiển thị (vd: "Chớp mắt")
    instruction: str              # câu hướng dẫn đầy đủ
    check: Callable[[object, dict], bool]  # nhận (Face, state) → True nếu đúng
    frames_to_confirm: int = FRAMES_TO_CONFIRM  # frame liên tiếp cần đúng
    min_hold_seconds: float = MIN_HOLD_SECONDS  # phải GIỮ NGUYÊN bao lâu


def _check_frontal(face, state: dict) -> bool:
    """Nhìn thẳng CHẶT (is_frontal_strict) — không chụp vội."""
    return face_metrics.is_frontal_strict(face)


def _check_turn_left(face, state: dict) -> bool:
    """Quay đầu sang trái."""
    return face_metrics.is_turning_left(face)


def _check_turn_right(face, state: dict) -> bool:
    """Quay đầu sang phải."""
    return face_metrics.is_turning_right(face)


def _pick_sharpest(recent, frame: np.ndarray, face):
    """Chọn (frame, face) NÉT NHẤT trong đệm các khung gần đây.

    Khi đủ điều kiện chụp (giữ yên đủ lâu), khung hình NGAY LÚC ĐÓ có thể
    bị rung mờ — khung trong đệm có độ nét (Laplacian) cao hơn cho mẫu
    sắc nét hơn (embedding + thumbnail chất lượng hơn). Fallback: frame
    hiện tại nếu đệm rỗng.
    """
    best_frame, best_face = frame, face
    best_sharp = -1.0
    for sharp, f, fc in recent:
        if sharp > best_sharp:
            best_sharp, best_frame, best_face = sharp, f, fc
    return best_frame, best_face


# Chuỗi 4 bước đăng ký (Bước 24 — đơn giản hóa)
# Bỏ "Chớp mắt" (quá khó detect trên webcam laptop 10-15 FPS)
# Bỏ "Mở miệng/Cười" (baseline detection không ổn định)
# Giữ 4 bước đa dạng góc: thẳng → trái → phải → thẳng (xác minh)
GUIDANCE_STEPS: list[GuidanceStep] = [
    GuidanceStep("Nhìn thẳng", "Nhìn thẳng vào camera, GIỮ YÊN 2 giây", _check_frontal, min_hold_seconds=2.0),                GuidanceStep("Quay trái", "Quay đầu sang trái 45°, giữ yên 1 giây", _check_turn_left, min_hold_seconds=1.0),
                GuidanceStep("Quay phải", "Quay đầu sang phải 45°, giữ yên 1 giây", _check_turn_right, min_hold_seconds=1.0),
                GuidanceStep("Nhìn thẳng", "Nhìn thẳng lần nữa, GIỮ YÊN 2 giây (xác minh)", _check_frontal, min_hold_seconds=2.0),
]


class EnrollmentWorker(QObject):
    """Vòng lặp thu mẫu theo chuỗi bước — chạy trong QThread.

    Mỗi bước: chờ tới khi điều kiện hành động đúng trong N frame liên
    tiếp → chụp 1 mẫu → sang bước kế. Cuối cùng kiểm tra Face verification.
    """

    frame_ready = Signal(object)   # numpy.ndarray BGR (đã vẽ hướng dẫn)
    started = Signal(int, int)     # (w, h) độ phân giải camera
    error = Signal(str)
    step_changed = Signal(int, str)    # (bước 1-based, tên bước)
    sample_captured = Signal(int, int, str)  # (mẫu đã thu, tổng, quality_label)
    progress_text = Signal(str)    # hướng dẫn/trạng thái thời gian thực
    verification = Signal(bool, float)  # (đúng/không, cosine giữa mẫu 1 và 6)
    finished = Signal(object)      # list[CapturedSample]

    def __init__(
        self,
        camera_index: int,
        width: int,
        height: int,
        detector: FaceDetector,
        embedder: FaceEmbedder,
        seed_sample: CapturedSample | None = None,
    ) -> None:
        super().__init__()
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._detector = detector
        self._embedder = embedder
        self._seed_sample = seed_sample
        self._running = False
        # Bước 25: độ nét (Laplacian) CAO NHẤT đã thấy trong phiên — làm
        # "độ nét nền" của chính webcam để tính ngưỡng mờ thích nghi.
        self._best_sharpness = 0.0

    # ---------------------------------------------------------
    # Vòng lặp chính
    # ---------------------------------------------------------
    @Slot()
    def run(self) -> None:
        capture = CameraCapture(self._camera_index, self._width, self._height)
        if not capture.open():
            self.error.emit("Không mở được webcam — kiểm tra camera / chỉ số camera")
            return
        self.started.emit(capture.actual_width, capture.actual_height)

        start_time = time.time()
        self._running = True
        self._best_sharpness = 0.0  # reset mỗi phiên đăng ký

        # Có ảnh người lạ từ webcam (nút [ Đăng ký ngay ])? → dùng làm MẪU 1,
        # bỏ bước "Nhìn thẳng" đầu (seed đã là ảnh nhìn thẳng), chỉ cần làm
        # 5 bước còn lại. Face verification cuối vẫn so mẫu cuối với seed.
        if self._seed_sample is not None:
            samples = [self._seed_sample]
            steps = GUIDANCE_STEPS[1:]
            self.sample_captured.emit(1, len(GUIDANCE_STEPS), float(self._seed_sample.quality))
            self.progress_text.emit(
                f"Mẫu 1: ảnh từ webcam — {steps[0].instruction}"
            )
        else:
            samples = []
            steps = GUIDANCE_STEPS

        for step_idx, step in enumerate(steps):
            if not self._running:
                capture.release()
                return  # người dùng hủy giữa chừng — phải đóng camera

            stable_frames = 0
            failed_streak = 0  # frame thất bại liên tiếp (dung thứ rung nhẹ)
            reject_count = 0  # Bước 24: đếm lần reject liên tiếp
            captured = False
            hold_since: float | None = None  # bắt đầu đếm giờ GIỮ NGUYÊN
            last_capture_time = time.time()
            step_state: dict = {}
            # Đệm các khung gần đây (kèm độ nét) — chụp khung NÉT NHẤT
            recent: deque = deque(maxlen=BUFFER_FRAMES)
            step_number = step_idx + (2 if self._seed_sample is not None else 1)
            self.step_changed.emit(step_number, step.name)
            self.progress_text.emit(step.instruction)

            # Vòng lặp chạy CHO TỚI KHI chụp được mẫu (không thoát sớm khi
            # gesture đạt ngưỡng nhưng chưa đủ MIN_GAP_SECONDS — nếu không
            # bước sẽ bị bỏ qua mà không có mẫu nào!)
            while self._running and not captured:
                if time.time() - start_time >= TIMEOUT_SECONDS:
                    self.error.emit("Hết thời gian đăng ký — thử lại lần nữa")
                    capture.release()
                    return

                frame = capture.read()
                if frame is None:
                    self.error.emit("Không đọc được khung hình từ camera")
                    capture.release()
                    return

                face = self._pick_best_face(frame)

                # Lưu khung + độ nét vào đệm (bản SAO — frame sẽ bị vẽ
                # hướng dẫn lên sau; bản copy giữ ảnh sạch cho mẫu đăng ký).
                # Đồng thời cập nhật độ nét nền của webcam (cao nhất đã thấy)
                # cho ngưỡng mờ thích nghi (Bước 25).
                if face is not None:
                    sharpness = self._sharpness(frame, face)
                    recent.append((sharpness, frame.copy(), face))
                    if sharpness > self._best_sharpness:
                        self._best_sharpness = sharpness

                # Chờ đủ MIN_GAP_SECONDS kể từ đầu bước rồi mới BẮT ĐẦU phát
                # hiện cử chỉ. Nếu không: cử chỉ NHANH (chớp mắt ~0.1-0.3s)
                # có thể hoàn thành trước khi hết khoảng nghỉ → người dùng
                # buông cử chỉ (mở mắt) → bị coi "bỏ dở" → phải chớp LẦN 2
                # (ngược với yêu cầu tăng độ nhạy). Chờ nghỉ xong mới đếm ⇒
                # chụp NGAY khi cử chỉ vừa đạt chuẩn.
                if time.time() - last_capture_time < MIN_GAP_SECONDS:
                    self._draw_guidance(frame, face, ok=False, step_name=step.name)
                    self.frame_ready.emit(frame)
                    continue

                ok = face is not None and step.check(face, step_state)
                if ok:
                    stable_frames += 1
                    failed_streak = 0
                    logger.info(
                        "%s OK: ns=%.3f ear=%.3f (stable=%d/%d)",
                        step.name, face_metrics.nose_shift(face),
                        face_metrics.eye_aspect_ratio(face),
                        stable_frames, step.frames_to_confirm,
                    )
                    if stable_frames >= step.frames_to_confirm and hold_since is None:
                        hold_since = time.time()  # đã đủ frame — bắt đầu đếm giữ
                else:
                    failed_streak += 1
                    # Debug: log giá trị thực khi bước fail
                    if face is not None:
                        ns = face_metrics.nose_shift(face)
                        ear = face_metrics.eye_aspect_ratio(face)
                        yaw = face_metrics.head_yaw(face)
                        roll = face_metrics.head_roll(face)
                        fails = []
                        if abs(ns) >= face_metrics.STRICT_NOSE_SHIFT:
                            fails.append(f"ns={ns:.3f}>={face_metrics.STRICT_NOSE_SHIFT}")
                        if ear <= face_metrics.EAR_OPEN_THRESHOLD:
                            fails.append(f"ear={ear:.3f}<={face_metrics.EAR_OPEN_THRESHOLD}")
                        if abs(yaw) >= face_metrics.FRONTAL_YAW_DEG:
                            fails.append(f"yaw={yaw:.1f}>={face_metrics.FRONTAL_YAW_DEG}")
                        if abs(roll) >= face_metrics.FRONTAL_ROLL_DEG:
                            fails.append(f"roll={roll:.1f}>={face_metrics.FRONTAL_ROLL_DEG}")
                        if not face_metrics.is_turning_left(face):
                            fails.append(f"not_turn_left(ns={ns:.3f})")
                        if not face_metrics.is_turning_right(face):
                            fails.append(f"not_turn_right(ns={ns:.3f})")
                        logger.info(
                            "%s FAIL: ns=%.3f ear=%.3f yaw=%.1f roll=%.1f stable=%d/%d — %s",
                            step.name, ns, ear, yaw, roll, stable_frames, step.frames_to_confirm,
                            ",".join(fails) if fails else "unknown",
                        )
                    # Dung thứ vài frame thất bại (rung nhẹ khi giữ yên) trước
                    # khi đếm lại từ đầu
                    if failed_streak > GRACE_FRAMES:
                        stable_frames = 0
                        hold_since = None
                    # KHÔNG clear step_state — ví dụ chu kỳ chớp mắt cần nhớ
                    # "đã từng mở" để biết khi nhắm là chớp thật (không phải
                    # mí mắt rũ từ đầu)
                self._draw_guidance(frame, face, ok=ok, step_name=step.name)

                # Điều kiện chụp: đủ frame + GIỮ NGUYÊN đủ lâu + đủ khoảng nghỉ
                held_long_enough = (
                    hold_since is not None
                    and time.time() - hold_since >= step.min_hold_seconds
                )
                if (
                    held_long_enough
                    and time.time() - last_capture_time >= MIN_GAP_SECONDS
                ):
                    # Chụp khung NÉT NHẤT trong đệm (không phải khung đúng
                    # lúc hết giờ — khung đó có thể bị rung mờ)
                    best_frame, best_face = _pick_sharpest(recent, frame, face)
                    if self._capture_sample(best_frame, best_face, samples):
                        captured = True
                        last_capture_time = time.time()
                        reject_count = 0  # reset khi chụp thành công
                    else:
                        # Bước 24: auto-retry — đếm reject, nếu quá nhiều → bỏ bước
                        reject_count += 1
                        if reject_count >= MAX_AUTO_RETRIES:
                            self.progress_text.emit(
                                f"❌ Bỏ bước '{step.name}' — "
                                f"không chụp được mẫu sau {MAX_AUTO_RETRIES} lần. "
                                "Chuyển bước tiếp theo."
                            )
                            captured = True  # bỏ qua bước này, sang bước kế
                            last_capture_time = time.time()

                self.frame_ready.emit(frame)

        capture.release()

        # ---- Face verification: mẫu cuối (nhìn thẳng lần nữa) vs mẫu đầu ----
        if len(samples) < 2:
            self.error.emit("Không thu đủ mẫu — thử lại lần nữa")
            return
        sim = FaceEmbedder.cosine_similarity(samples[0].embedding, samples[-1].embedding)
        logger.info("Face verification: cosine = %.3f", sim)
        self.verification.emit(sim >= VERIFY_THRESHOLD, sim)
        if sim < VERIFY_THRESHOLD:
            self.error.emit(
                f"Xác minh thất bại (tương đồng {sim:.2f} < {VERIFY_THRESHOLD:.2f}) — "
                "có thể không phải cùng một người, hãy thử lại"
            )
            return

        logger.info("Thu mẫu hoàn tất: %d mẫu", len(samples))
        self.finished.emit(samples)

    def stop(self) -> None:
        """Yêu cầu dừng thu (gọi từ luồng UI)."""
        self._running = False

    # ---------------------------------------------------------
    # Thu mẫu & lọc chất lượng
    # ---------------------------------------------------------
    def _adaptive_blur_min(self) -> float:
        """Ngưỡng độ nét tối thiểu của mẫu — THÍCH NGHI theo webcam (Bước 25).

        Ngưỡng tuyệt đối (60) gây reject hàng loạt trên webcam có độ nét
        nền thấp (~43). Dùng "độ nét nền" = giá trị Laplacian cao nhất đã
        thấy trong phiên: mẫu chỉ cần đạt ≥ 60% mức đó (nhưng không dưới
        sàn 30 — dưới mức này là mờ nặng thật sự).
        """
        return max(
            ADAPTIVE_BLUR_FLOOR,
            ADAPTIVE_BLUR_RATIO * self._best_sharpness,
        )

    def _capture_sample(self, frame: np.ndarray, face, samples: list[CapturedSample]) -> bool:
        """Trích embedding + crop ảnh mặt → thêm vào danh sách mẫu.

        Bước 22: Tính SampleQuality — auto-reject nếu quá xấu.
        Bước 24: Auto-retry do caller quản lý (reject_count trong run()).
        """
        quality = face_metrics.compute_quality(face, frame)
        # Bước 25: ngưỡng mờ THÍCH NGHI theo webcam (không còn cố định 60 —
        # lệch nặng giữa các máy: máy dev ~104, webcam người dùng ~43). Các
        # ngưỡng khác vẫn dùng đúng hằng số của is_good() (MIN_QUALITY_*).
        blur_min = self._adaptive_blur_min()
        if not quality.is_good(blur_min=blur_min):
            # Lý do reject dùng ĐÚNG ngưỡng (MIN_QUALITY_* + blur thích nghi)
            # — trước đây dùng nhầm MIN_SHARPNESS/MIN_BRIGHTNESS của bước
            # _pick_best_face (nới hơn) → hiện lý do SAI, khó debug.
            reasons = []
            if quality.face_score < face_metrics.MIN_QUALITY_FACE_SCORE:
                reasons.append(f"mặt chưa rõ ({quality.face_score:.2f})")
            if quality.blur_score < blur_min:
                reasons.append(f"ảnh mờ ({quality.blur_score:.0f})")
            if quality.brightness < face_metrics.MIN_QUALITY_BRIGHTNESS:
                reasons.append(f"quá tối ({quality.brightness:.0f})")
            elif quality.brightness > face_metrics.MAX_QUALITY_BRIGHTNESS:
                reasons.append(f"quá sáng ({quality.brightness:.0f})")
            if quality.occlusion >= face_metrics.MIN_QUALITY_OCCLUSION:
                reasons.append(f"bị che ({quality.occlusion:.2f})")
            reason_str = ", ".join(reasons) if reasons else "chất lượng thấp"
            logger.info(
                "Mẫu REJECT: %s — det=%.2f blur=%.0f(cần≥%.0f) "
                "bright=%.0f occ=%.2f",
                reason_str, quality.face_score, quality.blur_score, blur_min,
                quality.brightness, quality.occlusion,
            )
            self.progress_text.emit(
                f"⚠ Mẫu chưa đạt: {reason_str} — đang tự chỉnh..."
            )
            return False

        embedding = self._embedder.embed_face(face)
        if embedding is None:
            self.progress_text.emit("Không trích được embedding — giữ nguyên tư thế")
            return False
        crop = self._crop_face(frame, face)
        samples.append(
            CapturedSample(
                embedding=embedding,
                quality=quality.overall,
                face_crop=crop,
            )
        )
        quality_label = (
            f"{quality.overall:.0f}/100 ({quality.label()}) — "
            f"det={quality.face_score:.2f} blur={quality.blur_score:.0f} "
            f"bright={quality.brightness:.0f} occ={quality.occlusion:.2f}"
        )
        logger.info("Mẫu OK: %s", quality_label)
        self.sample_captured.emit(len(samples), len(GUIDANCE_STEPS), quality_label)
        return True

    def _pick_best_face(self, frame: np.ndarray):
        """Chọn khuôn mặt tốt nhất trong frame; None nếu không đạt tiêu chuẩn."""
        # Bước 21: CLAHE preprocessing — detect trên ảnh đã chuẩn hóa ánh sáng
        # (embedding phải khớp giữa lúc đăng ký và nhận diện)
        detect_frame = preprocess_frame(frame)
        try:
            faces = self._detector.detect(detect_frame)
        except Exception:  # noqa: BLE001 — không để lỗi GPU giết chết luồng thu
            logger.exception("Lỗi phát hiện khuôn mặt khi đăng ký")
            return None

        if not faces:
            self.progress_text.emit("Chưa thấy khuôn mặt — đưa mặt vào khung...")
            return None

        best = None
        best_size = 0
        for face in faces:
            score = float(face.det_score)
            x1, y1, x2, y2 = face.bbox.astype(int)
            w, h = x2 - x1, y2 - y1
            frame_w = frame.shape[1]

            if score < MIN_FACE_SCORE:
                self.progress_text.emit(
                    f"Mặt chưa rõ ({score:.2f}) — lại gần hơn hoặc tăng ánh sáng"
                )
                continue
            if w < MIN_FACE_SIZE or h < MIN_FACE_SIZE:
                self.progress_text.emit("Khuôn mặt quá nhỏ — tiến lại gần camera")
                continue
            if w > frame_w * 0.8:
                self.progress_text.emit("Khuôn mặt quá gần — lùi ra chút")
                continue

            if self._sharpness(frame, face) < MIN_SHARPNESS:
                self.progress_text.emit("Ảnh đang bị mờ — giữ yên, tránh rung máy")
                continue

            # Mặt bị che khuất → mẫu chất lượng kém (embedding lệch → nhận
            # diện sau kém). Chặn mẫu bị che NHIỀU, nhắc khi hơi bị che.
            occ = face_metrics.occlusion_score(face, frame)
            if occ >= face_metrics.OCCLUSION_BLOCK:
                self.progress_text.emit(
                    "Mặt bị che (khẩu trang/tay/vật cản) — bỏ vật cản để tiếp tục"
                )
                continue
            if occ >= face_metrics.OCCLUSION_WARN:
                self.progress_text.emit("Mặt hơi bị che — chỉnh lại cho rõ mặt")

            brightness = self._brightness(frame)
            if brightness < MIN_BRIGHTNESS:
                self.progress_text.emit("Ánh sáng quá tối — bật thêm đèn")
                continue
            if brightness > MAX_BRIGHTNESS:
                self.progress_text.emit("Bị chói — tránh ngược sáng")
                continue

            # Chọn khuôn mặt LỚN NHẤT đạt chuẩn (gần camera nhất → rõ nhất)
            if w * h > best_size:
                best = face
                best_size = w * h

        return best

    @staticmethod
    def _brightness(frame: np.ndarray) -> float:
        """Độ sáng trung bình của frame (0..255)."""
        return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())

    @staticmethod
    def _sharpness(frame: np.ndarray, face) -> float:
        """Độ nét vùng mặt — Laplacian variance (cao = nét, thấp = mờ do rung).

        Chỉ tính trên VÙNG KHUÔN MẶT (không phải cả frame — nền mờ không
        liên quan). Dùng để lọc khung mờ + chọn khung nét nhất khi chụp.
        """
        x1, y1, x2, y2 = face.bbox.astype(int)
        h, w = frame.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return 0.0
        gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    @staticmethod
    def _crop_face(frame: np.ndarray, face) -> np.ndarray:
        """Cắt ảnh khuôn mặt (có thêm viền) làm thumbnail."""
        x1, y1, x2, y2 = face.bbox.astype(int)
        h, w = frame.shape[:2]
        pad = int((x2 - x1) * 0.15)
        x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
        x2, y2 = min(w, x2 + pad), min(h, y2 + pad)
        return frame[y1:y2, x1:x2]

    # ---------------------------------------------------------
    # Vẽ hướng dẫn
    # ---------------------------------------------------------
    def _draw_guidance(
        self, frame: np.ndarray, face, ok: bool, step_name: str = ""
    ) -> None:
        """Vẽ khung bầu dục THEO MẶT + khung mặt + mũi tên hướng (Bước 25)."""
        h, w = frame.shape[:2]
        color = OVAL_COLOR_OK if ok else OVAL_COLOR_WAIT

        if face is not None:
            # Oval căn giữa THEO KHUÔN MẶT — nhận diện ở MỌI VỊ TRÍ trong
            # khung (không ép người dùng đưa mặt về một chỗ cố định).
            # Webcam laptop đặt THẤP: nếu oval cố định ở giữa khung, đưa
            # mặt lên phần trên = camera nhìn từ góc dốc → pose/EAR lệch
            # → chỉ phần dưới oval mới pass. Oval theo mặt giải quyết triệt để.
            x1, y1, x2, y2 = face.bbox.astype(int)
            fc_x = (x1 + x2) // 2
            fc_y = (y1 + y2) // 2
            fw, fh = x2 - x1, y2 - y1
            axes = (int(fw * 0.85), int(fh * 1.05))
        else:
            # Chưa thấy mặt → oval mặc định ở giữa làm MỐC NGẮM
            fc_x, fc_y = w // 2, int(h * 0.45)
            axes = (int(w * 0.28), int(h * 0.34))
        cv2.ellipse(frame, (fc_x, fc_y), axes, 0, 0, 360, color, 2, cv2.LINE_AA)

        # Khung quanh mặt đã chọn
        if face is not None:
            x1, y1, x2, y2 = face.bbox.astype(int)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        # Bước 25: mũi tên hướng quay đầu
        if step_name == "Quay trái":
            arrow_x = int(w * 0.15)
            arrow_y = int(h * 0.45)
            cv2.arrowedLine(
                frame, (arrow_x + 40, arrow_y), (arrow_x, arrow_y),
                (0, 200, 255), 3, cv2.LINE_AA, tipLength=0.4,
            )
            cv2.putText(
                frame, "<", (arrow_x - 10, arrow_y + 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2,
            )
        elif step_name == "Quay phải":
            arrow_x = int(w * 0.85)
            arrow_y = int(h * 0.45)
            cv2.arrowedLine(
                frame, (arrow_x - 40, arrow_y), (arrow_x, arrow_y),
                (0, 200, 255), 3, cv2.LINE_AA, tipLength=0.4,
            )
            cv2.putText(
                frame, ">", (arrow_x - 5, arrow_y + 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2,
            )


class EnrollmentDialog(QDialog):
    """Hộp thoại đăng ký khuôn mặt: tên + thu mẫu theo bước + lưu CSDL."""

    def __init__(
        self,
        config: Config,
        db: Database,
        detector: FaceDetector | None = None,
        seed_sample: CapturedSample | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._db = db
        self._detector = detector
        self._seed_sample = seed_sample
        self._embedder: FaceEmbedder | None = None
        self._service = EnrollmentService(db)
        self._thread: QThread | None = None
        self._worker: EnrollmentWorker | None = None

        self.setWindowTitle("Đăng ký khuôn mặt")
        self.setModal(True)
        self.resize(760, 680)
        self._build_ui()

        # Có mẫu 1 từ ảnh người lạ (webcam)? → cập nhật giao diện cho đúng
        if seed_sample is not None:
            self._video_label.setText(
                "📷 Mẫu 1 đã lấy từ webcam — làm tiếp 3 bước còn lại"
            )
            self._steps_label.setText(self._steps_preview(2))
            self._progress_label.setText(
                "Đã thu: ● ○ ○ ○ (1/4) — ảnh từ webcam"
            )
            self._progress_bar.setValue(25)  # 1/4 = 25%

    # ---------------------------------------------------------
    # Giao diện (theo wireframe 5.5.3)
    # ---------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(QLabel("ĐĂNG KÝ KHUÔN MẶT"))

        # Tên người dùng
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Tên người dùng:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Ví dụ: Nguyễn Văn A")
        name_row.addWidget(self._name_edit, stretch=1)
        layout.addLayout(name_row)

        # Vùng video
        self._video_label = QLabel("Chưa bắt đầu thu — nhập tên rồi bấm nút bên dưới")
        self._video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_label.setMinimumSize(640, 400)
        self._video_label.setObjectName("videoLabel")
        self._video_label.setStyleSheet("font-size: 14px;")
        layout.addWidget(self._video_label, stretch=1)

        # Danh sách 6 bước + bước hiện tại
        self._steps_label = QLabel(self._steps_preview(0))
        self._steps_label.setWordWrap(True)
        self._steps_label.setStyleSheet("font-size: 13px;")
        layout.addWidget(self._steps_label)

        # Mẹo đeo kính: nhận diện ổn định nhất khi trạng thái đeo kính
        # lúc đăng ký và lúc quét là NHẤT QUÁN
        glasses_tip = QLabel(
            "💡 Đeo kính? Hãy GIỮ NGUYÊN kính khi đăng ký VÀ khi quét — "
            "nhận diện sẽ ổn định hơn (đăng ký có kính, quét cũng đeo kính)."
        )
        glasses_tip.setWordWrap(True)
        glasses_tip.setStyleSheet("font-size: 12px;")
        layout.addWidget(glasses_tip)

        # Hướng dẫn + tiến độ
        self._guide_label = QLabel("Nhập tên rồi bấm 'Bắt đầu thu' để bắt đầu.")
        self._guide_label.setWordWrap(True)
        layout.addWidget(self._guide_label)

        # Bước 25: Thanh tiến độ realtime
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p%")
        self._progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #555; border-radius: 4px; "
            "text-align: center; height: 18px; } "
            "QProgressBar::chunk { background-color: #2d7ff9; border-radius: 3px; }"
        )
        layout.addWidget(self._progress_bar)

        self._progress_label = QLabel("Đã thu: 0/4 mẫu")
        layout.addWidget(self._progress_label)

        # Bước 25: Hint ngữ cảnh realtime
        self._hint_label = QLabel("")
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet(
            "font-size: 13px; color: #888; padding: 4px 8px; "
            "background: #1a1a2e; border-radius: 4px;"
        )
        self._hint_label.setMinimumHeight(24)
        layout.addWidget(self._hint_label)

        # Nút điều khiển
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Bắt đầu thu")
        self._start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_btn.clicked.connect(self._start_capture)
        btn_row.addWidget(self._start_btn)

        self._cancel_btn = QPushButton("Hủy")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)

        layout.addLayout(btn_row)

    @staticmethod
    def _steps_preview(current: int) -> str:
        """Hiển thị 6 bước: bước đã xong ✓, bước hiện tại ▶, bước chưa tới ○."""
        parts = []
        for i, step in enumerate(GUIDANCE_STEPS):
            if i + 1 < current:
                parts.append(f"✓ {step.name}")
            elif i + 1 == current:
                parts.append(f"▶ {step.name}")
            else:
                parts.append(f"○ {step.name}")
        return "  ".join(parts)

    # ---------------------------------------------------------
    # Điều khiển thu
    # ---------------------------------------------------------
    def _start_capture(self) -> None:
        """Bắt đầu thu mẫu: nạp model (nếu cần) + chạy worker trong QThread."""
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Thiếu tên", "Vui lòng nhập tên người dùng trước.")
            return
        self._person_name = name

        # Nạp model lần đầu (nếu chưa có — tái sử dụng nếu đã nạp từ CameraView)
        if self._detector is None:
            self._guide_label.setText("Đang nạp model nhận diện...")
            QApplication.processEvents()
            self._detector = self._load_detector()
            if self._detector is None:
                return
        if self._embedder is None:
            self._embedder = FaceEmbedder(self._detector)

        self._thread = QThread(self)
        self._worker = EnrollmentWorker(
            self._config.camera_index,
            self._config.camera_width,
            self._config.camera_height,
            detector=self._detector,
            embedder=self._embedder,
            seed_sample=self._seed_sample,
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.started.connect(
            lambda w, h: self._guide_label.setText(
                f"Camera mở: {w}×{h} — làm theo từng bước bên dưới"
            )
        )
        self._worker.step_changed.connect(self._on_step_changed)
        self._worker.sample_captured.connect(self._on_sample_captured)
        self._worker.progress_text.connect(self._guide_label.setText)
        self._worker.progress_text.connect(self._update_hint)  # Bước 25
        self._worker.verification.connect(self._on_verification)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)

        self._thread.start()
        self._start_btn.setEnabled(False)
        self._cancel_btn.setText("Dừng & đóng")
        logger.info("Bắt đầu đăng ký khuôn mặt '%s'", self._person_name)

    def _load_detector(self) -> FaceDetector | None:
        """Tạo FaceDetector; None nếu thiếu model (hiện thông báo lỗi)."""
        try:
            if not (MODELS_ROOT / "models" / "buffalo_l").exists():
                raise FileNotFoundError(
                    f"Chưa có model tại {MODELS_ROOT / 'models' / 'buffalo_l'}"
                )
            detector = FaceDetector(MODELS_ROOT)
            logger.info("Đã nạp model phát hiện khuôn mặt (enrollment)")
            return detector
        except Exception as exc:  # noqa: BLE001
            logger.exception("Không nạp được model: %s", exc)
            QMessageBox.critical(
                self, "Lỗi model", f"Không nạp được model phát hiện khuôn mặt:\n{exc}"
            )
            return None

    def _stop_capture(self) -> None:
        """Dừng thu + giải phóng thread (gọi khi xong/hủy/đóng).

        An toàn kể cả khi camera treo: nếu thread chưa kết thúc sau thời
        gian chờ thì GIỮ tham chiếu (không xóa sớm — tránh crash do hủy
        C++ object khi luồng còn chạy). Thread tự kết thúc khi MSMF trả
        lỗi; lần mở sau kiểm tra isRunning() để không mở camera song song.
        """
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            if self._thread.wait(1500):
                self._thread = None
                self._worker = None
            # else: camera treo → giữ tham chiếu, để thread tự kết thúc
        self._start_btn.setEnabled(True)
        self._cancel_btn.setText("Hủy")

    # ---------------------------------------------------------
    # Xử lý tín hiệu
    # ---------------------------------------------------------
    @Slot(object)
    def _on_frame(self, frame: np.ndarray) -> None:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        self._video_label.setPixmap(
            pixmap.scaled(
                self._video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    @Slot(int, str)
    def _on_step_changed(self, step: int, name: str) -> None:
        self._steps_label.setText(self._steps_preview(step))
        self._guide_label.setText(f"Bước {step}/{len(GUIDANCE_STEPS)}: {name}")

    @Slot(int, int, str)
    def _on_sample_captured(self, count: int, target: int, quality_info: str) -> None:
        dots = "● " * count + "○ " * (target - count)
        self._progress_label.setText(f"Đã thu: {dots.strip()} ({count}/{target}) · {quality_info}")
        # Bước 25: cập nhật thanh tiến độ
        pct = int(count / target * 100) if target > 0 else 0
        self._progress_bar.setValue(pct)
        self._hint_label.setText("")  # xóa hint khi chụp thành công

    @Slot(str)
    def _update_hint(self, text: str) -> None:
        """Bước 25: Hiển thị hint ngữ cảnh dựa trên tiến trình thu mẫu.

        Mapping tin nhắn worker → hint ngắn gọn hiển thị bên dưới progress bar:
          - 'Chưa thấy khuôn mặt' → '👋 Đưa mặt vào giữa khung hình'
          - 'Mặt quá nhỏ'         → '📏 Tiến lại gần camera'
          - 'Mặt quá gần'         → '📏 Lùi ra chút'
          - 'quá tối'             → '💡 Bật thêm đèn hoặc quay về phía ánh sáng'
          - 'chói'                → '💡 Tránh ngược sáng'
          - 'mẫu chưa đạt'        → giữ nguyên hint trước
          - khác                   → xóa hint
        """
        t = text.lower()
        hint = ""
        if "chưa thấy" in t or "không thấy" in t:
            hint = "👋 Đưa mặt vào giữa khung hình"
        elif "quá nhỏ" in t or "tiến lại" in t:
            hint = "📏 Tiến lại gần camera"
        elif "quá gần" in t or "lùi ra" in t:
            hint = "📏 Lùi ra chút"
        elif "quá tối" in t or "tối" in t:
            hint = "💡 Bật thêm đèn hoặc quay về phía ánh sáng"
        elif "chói" in t or "quá sáng" in t or "ngược sáng" in t:
            hint = "💡 Tránh ngược sáng"
        elif "mẫu chưa đạt" in t or "tự chỉnh" in t:
            # Giữ nguyên hint trước — không xóa khi đang retry
            return
        elif "đang giữ" in t or "giữ yên" in t:
            hint = "✓ Đang giữ... hoàn thành sắp xong!"
        else:
            hint = ""  # xóa hint cho các tin nhắn khác
        self._hint_label.setText(hint)

    @Slot(bool, float)
    def _on_verification(self, ok: bool, sim: float) -> None:
        if ok:
            self._progress_label.setText(
                f"✅ Face verification: ĐỒNG NHẤT (cosine {sim:.2f}) — "
                "mẫu 1 và mẫu 4 giống nhau"
            )
        else:
            self._progress_label.setText(
                f"⚠ Face verification: NGHI NGỜ (cosine {sim:.2f}) — "
                "mẫu 1 và mẫu 4 khác nhau, có thể không phải cùng người"
            )

    @Slot(str)
    def _on_error(self, message: str) -> None:
        logger.error("Lỗi đăng ký: %s", message)
        self._stop_capture()
        QMessageBox.critical(self, "Lỗi đăng ký", message)

    @Slot(object)
    def _on_finished(self, samples: list) -> None:
        """Đủ mẫu + xác minh OK → lưu vào CSDL và đóng dialog thành công."""
        self._stop_capture()
        try:
            person = self._service.save_person(self._person_name, samples)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Không lưu được người: %s", exc)
            QMessageBox.critical(self, "Lỗi lưu", f"Không lưu được người dùng:\n{exc}")
            return
        QMessageBox.information(
            self,
            "Đăng ký thành công",
            f"Đã đăng ký '{person.name}' với {len(samples)} mẫu khuôn mặt.",
        )
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 (chuẩn Qt)
        self._stop_capture()
        super().closeEvent(event)
