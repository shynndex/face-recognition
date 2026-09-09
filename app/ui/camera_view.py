"""CameraView — nhận diện thời gian thực hoàn chỉnh (Bước 9, wireframe 5.5.2).

Kiến trúc luồng: CameraWorker chạy trong QThread — đọc frame từ webcam,
phát hiện + so khớp khuôn mặt, vẽ lên frame, rồi phát tín hiệu về UI.
Nhờ vậy vòng lặp KHÔNG chặn luồng giao diện (UI không bị \"đơ\").

Bước 9 bổ sung so với Bước 3-5:
  - NHIỀU mặt cùng lúc: mỗi mặt một khung + tên + điểm tương đồng.
  - NGƯỜI LẠ: làm mờ tự động (riêng tư) + khung đỏ + nút [ Đăng ký ngay ]
    (ảnh người lạ làm MẪU 1 khi mở EnrollmentDialog).
  - GHI SỰ KIỆN: mỗi lần nhận diện (đã biết hoặc người lạ) → lưu
    recognition_events + snapshot (debounce 5s/người — tránh tràn DB).

QUAN TRỌNG (đa luồng + sqlite): worker KHÔNG đụng CSDL. Worker dùng bộ
so khớp trong BỘ NHỚ (nạp trước ở luồng UI) và phát tín hiệu sự kiện;
CameraView (luồng UI) mới gọi RecognitionService.save_event.
"""
from __future__ import annotations

import logging
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.core import face_metrics
from app.core.detector import FaceDetector, MODELS_ROOT
from app.core.embedder import EMBEDDING_DIM, FaceEmbedder
from app.core.preprocess import preprocess_frame
from app.core.liveness import LivenessTracker
from app.core.matcher import MatchResult
from app.core.temporal import DEFAULT_WINDOW_SIZE, FaceTracker, TemporalBuffer
from app.infrastructure.camera import CameraCapture
from app.infrastructure.db import Database
from app.services.recognition import RecognitionService

logger = logging.getLogger(__name__)

# Màu vẽ (BGR)
KNOWN_COLOR = (0, 200, 0)      # xanh — người đã đăng ký
UNKNOWN_COLOR = (0, 60, 255)   # đỏ — người lạ
SPOOF_COLOR = (0, 165, 255)    # cam — nghi ngờ giả mạo (Bước 17)
TEXT_COLOR = (255, 255, 255)

# ── Confidence colors (BGR) ──
#   ≥ 0.80  xanh lá  — tin cậy cao
#   0.60–0.80  vàng  — tin cậy trung bình
#   0.40–0.60  cam   — tin cậy thấp
#   < 0.40  đỏ       — gần ngưỡng, có thể nhầm
# LƯU Ý: các màu KNOWN/UNKNOWN/SPOOF/CONF phía trên hiện chỉ còn được dùng
# bởi photo_view.py và script test (scripts/step_23_gui_test.py). Overlay
# nhận dạng THỜI GIAN THỰC dưới đây theo ảnh tham chiếu: đồng màu TRẮNG.
CONF_HIGH = (94, 208, 69)     # #45D05A — xanh lá (tin cậy cao ≥0.80)
CONF_MED = (0, 191, 255)      # #FFBF00 — vàng (0.60–0.80)
CONF_LOW = (0, 140, 255)      # #FF8C00 — cam (0.40–0.60)
CONF_VERY_LOW = (0, 69, 255)  # #FF4500 — đỏ (< 0.40)


# ── Overlay nhận dạng thời gian thực (kiểu ảnh tham chiếu) ─────────
# Khung + nhãn chip đồng màu TRẮNG cho MỌI trạng thái (người quen / người
# lạ / giả mạo / bị che) — trạng thái thể hiện bằng CHỮ trên chip (Unicode).
BOX_COLOR = (255, 255, 255)   # BGR — khung viền khuôn mặt
CHIP_BG = (255, 255, 255)     # BGR — nền chip nhãn
CHIP_TEXT = (18, 18, 18)      # BGR — chữ đậm trên nền trắng
CHIP_FONT_PX = 22             # cỡ chữ trên chip (px)


def _confidence_color(similarity: float) -> tuple[int, int, int]:
    """Trả về màu BGR theo mức similarity (dùng cho photo_view + script test)."""
    if similarity >= 0.80:
        return CONF_HIGH
    if similarity >= 0.60:
        return CONF_MED
    if similarity >= 0.40:
        return CONF_LOW
    return CONF_VERY_LOW


# ── Font Unicode (tiếng Việt có dấu) — cv2.putText KHÔNG render được dấu ──
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def _pil_font(size_px: int) -> ImageFont.FreeTypeFont:
    """Font TrueType hỗ trợ tiếng Việt, cache theo cỡ chữ.

    Thứ tự ưu tiên font Windows: Segoe UI → Arial → Tahoma.
    Fallback: font mặc định của Pillow (không dấu nhưng không crash).
    """
    if size_px not in _FONT_CACHE:
        font = None
        for name in ("segoeui.ttf", "arial.ttf", "tahoma.ttf"):
            try:
                font = ImageFont.truetype(name, size_px)
                break
            except OSError:
                continue
        _FONT_CACHE[size_px] = font if font is not None else ImageFont.load_default()
    return _FONT_CACHE[size_px]

# Debounce ghi sự kiện: tối đa 1 sự kiện / người / khoảng thời gian này
EVENT_DEBOUNCE_SECONDS = 5.0

# ── Bước 25: Cảnh báo "Ảnh mờ" realtime dùng ngưỡng THÍCH NGHI ──────
# Quality gate kiểm tra mỗi ~2s; cảnh báo mờ khi độ nét hiện tại
# < max(40, 50% độ nét CAO NHẤT đã thấy). Máy nét (max ~104) giữ ngưỡng
# ~52 như trước (lọc rung); webcam nền mềm (max ~45) KHÔNG bị cảnh báo
# oan khi người dùng đã giữ yên — vì ~43 là độ nét nền của chính nó.
QUALITY_BLUR_WARN_FLOOR = 40.0
QUALITY_BLUR_WARN_RATIO = 0.50


class CameraWorker(QObject):
    """Vòng lặp đọc frame + nhận diện — chạy trong QThread.

    CameraCapture được tạo TRONG run() (đúng luồng worker) vì
    VideoCapture của OpenCV không an toàn đa luồng.
    """

    frame_ready = Signal(object)          # numpy.ndarray BGR (đã vẽ)
    started = Signal(int, int)            # độ phân giải thực tế (w, h)
    error = Signal(str)
    fps_updated = Signal(float)
    # Có người lạ trong khung gần nhất không (bật/tắt nút [Đăng ký ngay])
    unknown_face = Signal(bool)
    # Người lạ vừa xuất hiện: (ảnh crop, embedding, chất lượng) — làm mẫu 1
    unknown_crop = Signal(object, object, float)
    # Sự kiện nhận diện (debounce) — gửi về UI để lưu CSDL (tránh đụng DB ở worker)
    recognition_hit = Signal(str, float, object)      # (person_id, similarity, crop)
    recognition_unknown = Signal(object)              # crop người lạ
    # Nghi ngờ giả mạo (Bước 17): nhận diện được NGƯỜI ĐÃ BIẾT nhưng không
    # thấy dấu hiệu sống (chớp mắt) → (person_id, similarity, crop) để UI
    # ghi sự kiện "Giả mạo" vào lịch sử (audit trail).
    spoof_suspected = Signal(str, float, object)
    # Mặt bị che NHIỀU (Bước 18): nhận diện được NGƯỜI ĐÃ BIẾT nhưng
    # occlusion cao (không đủ tin cậy) → (person_id, similarity, crop) để
    # UI ghi sự kiện "Mặt bị che" vào lịch sử (audit trail).
    occlusion_blocked = Signal(str, float, object)

    def __init__(
        self,
        camera_index: int,
        width: int,
        height: int,
        detector: FaceDetector | None = None,
        embedder: FaceEmbedder | None = None,
        service: RecognitionService | None = None,
        threshold: float = 0.40,
        anti_spoofing_enabled: bool = True,
        smoothing_window: int = DEFAULT_WINDOW_SIZE,
        clahe_enabled: bool = True,
    ) -> None:
        super().__init__()
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._detector = detector
        self._embedder = embedder
        self._service = service
        self._threshold = threshold
        self._anti_spoofing_enabled = anti_spoofing_enabled
        self._smoothing_window = smoothing_window
        self._clahe_enabled = clahe_enabled
        # Tracker liveness RIÊNG CHO TỪNG NGƯỜI (2 người trong khung hình
        # mỗi người có chu kỳ chớp mắt của riêng mình — không gộp chung).
        self._liveness: dict[str, LivenessTracker] = {}
        # Temporal smoothing + face tracking (Bước 19)
        self._tracker = FaceTracker()
        self._buffers: dict[int, TemporalBuffer] = {}  # track_id → buffer
        self._running = False
        self._was_unknown = False
        self._last_unknown_seed: tuple | None = None
        self._last_event_at: dict[str, float] = {}
        self._last_quality_check: float = 0.0  # Bước 22: Quality Gate
        # Bước 25: độ nét (Laplacian) cao nhất đã thấy — nền của webcam để
        # cảnh báo "Ảnh mờ" không kêu oan trên webcam nền mềm
        self._best_sharpness = 0.0

    # ---------------------------------------------------------
    # Vòng lặp chính
    # ---------------------------------------------------------
    @Slot()
    def run(self) -> None:
        capture = CameraCapture(self._camera_index, self._width, self._height)
        try:
            if not capture.open():
                self.error.emit("Không mở được webcam — kiểm tra camera / chỉ số camera")
                return
            self.started.emit(capture.actual_width, capture.actual_height)

            self._running = True
            frame_count = 0
            t0 = time.time()
            self._best_sharpness = 0.0  # reset mỗi phiên mở camera

            while self._running:
                frame = capture.read()
                if frame is None:
                    self.error.emit("Không đọc được khung hình từ camera")
                    break
                if self._detector is not None:
                    self._process_frame(frame)
                # LƯU Ý (đa luồng): frame truyền qua tín hiệu là QUEUED — giữ THAM
                # CHIẾU (không sao chép). An toàn vì capture.read() cấp buffer mới
                # mỗi frame và _on_frame cvtColor ngay. KHÔNG được sửa frame sau
                # khi emit ở bất kỳ đâu (sẽ làm hỏng ảnh worker đang xử lý).
                self.frame_ready.emit(frame)

                # Cập nhật FPS mỗi ~1 giây
                frame_count += 1
                elapsed = time.time() - t0
                if elapsed >= 1.0:
                    self.fps_updated.emit(frame_count / elapsed)
                    frame_count = 0
                    t0 = time.time()
        finally:
            # LUÔN đóng camera — kể cả khi có lỗi bất ngờ trong vòng lặp
            # (nếu không, MSMF giữ webcam và thread chết không sạch).
            capture.release()

    def stop(self) -> None:
        """Yêu cầu dừng vòng lặp (gọi từ luồng UI)."""
        self._running = False
        self._tracker.reset()
        self._buffers.clear()

    # ---------------------------------------------------------
    # Xử lý khung hình: phát hiện → so khớp → vẽ
    # ---------------------------------------------------------
    def _process_frame(self, frame: np.ndarray) -> bool:
        """Nhận diện MỌI khuôn mặt trong frame; trả True nếu có người lạ.

        Bước 19: Dùng FaceTracker gán track_id cho mỗi bbox, rồi TemporalBuffer
        bỏ phiếu N khung gần nhất để tên không nhấp nháy.
        """
        # Bước 21: CLAHE preprocessing — chuẩn hóa ánh sáng trước khi embed
        # (phải áp dụng CẢ KHI ĐĂNG KÝ VÀ KHI NHẬN DIỆN để embedding khớp)
        detect_frame = preprocess_frame(frame) if self._clahe_enabled else frame
        try:
            faces = self._detector.detect(detect_frame)
        except Exception:  # noqa: BLE001 — lỗi GPU không được giết luồng camera
            logger.exception("Lỗi phát hiện khuôn mặt — tạm tắt nhận diện")
            self._detector = None
            return False

        # Bước 19: FaceTracker — gán track_id cho mỗi bbox
        bboxes = [face.bbox for face in faces]
        track_map = self._tracker.update(bboxes)  # {bbox_idx: track_id}

        has_unknown = False
        for i, face in enumerate(faces):
            x1, y1, x2, y2 = face.bbox.astype(int)
            track_id = track_map.get(i, -1)

            # Trích embedding (dùng chung cho so khớp + mẫu đăng ký người lạ)
            embedding = self._embedder.embed_face(face) if self._embedder else None

            # So khớp thô (trước khi smoothing)
            raw_result: MatchResult | None = None
            if self._service is not None and embedding is not None:
                raw_result = self._service.match(embedding, self._threshold)

            # Bước 19: Temporal smoothing — thêm raw_result vào buffer
            result: MatchResult | None = None
            if self._smoothing_window > 0 and track_id >= 0:
                if track_id not in self._buffers:
                    self._buffers[track_id] = TemporalBuffer(self._smoothing_window)
                stable_id = self._buffers[track_id].add(raw_result)
                if stable_id is not None and raw_result is not None:
                    # Hiển thị kết quả smoothing (giữ similarity gốc)
                    result = MatchResult(
                        person_id=stable_id,
                        similarity=raw_result.similarity,
                    )
            else:
                result = raw_result  # không smoothing — dùng kết quả thô

            if result is not None:
                # MẶT BỊ CHE NHIỀU (Bước 18): không đủ tin cậy để nhận diện
                # → chặn, hiện "⚠ Mặt bị che" thay vì đoán sai tên.
                occ = face_metrics.occlusion_score(face, frame)
                if occ >= face_metrics.OCCLUSION_BLOCK:
                    self._mark_occluded(frame, face, result)
                elif self._anti_spoofing_enabled and not self._is_live(
                    result.person_id, face
                ):
                    self._mark_spoof(frame, face, result)
                else:
                    self._draw_known(
                        frame, face, result,
                        warn=occ >= face_metrics.OCCLUSION_WARN,
                    )
                    self._maybe_emit_hit(frame, face, result)
            else:
                # NGƯỜI LẠ (hoặc chưa có dữ liệu đăng ký)
                has_unknown = True
                self._mark_unknown(frame, face, embedding)

        # Phát tín hiệu khi trạng thái người lạ THAY ĐỔI (tránh spam mỗi frame)
        if has_unknown != self._was_unknown:
            self._was_unknown = has_unknown
            self.unknown_face.emit(has_unknown)
            if has_unknown and self._last_unknown_seed is not None:
                self.unknown_crop.emit(*self._last_unknown_seed)

        # Bước 22: Quality Gate — cảnh báo realtime nếu điều kiện xấu
        self._draw_quality_gate(frame, faces)

        return has_unknown

    # ---------------------------------------------------------
    # Che khuất / occlusion (Bước 18)
    # ---------------------------------------------------------
    def _mark_occluded(
        self, frame: np.ndarray, face, result: MatchResult
    ) -> None:
        """Mặt bị che NHIỀU: không nhận diện sai — khung trắng + chip "Mặt bị che".

        KHÔNG làm mờ (đây là chính người dùng, không phải người lạ). Vẫn GHI
        sự kiện "Mặt bị che: <tên>" vào lịch sử (Bước 18) — audit trail
        biết là AI bị chặn vì che, kèm snapshot (cắt BẢN SAO trước khi vẽ
        khung để ảnh còn sạch).
        """
        x1, y1, x2, y2 = face.bbox.astype(int)
        crop = self._crop_face(frame, face)
        self._draw_box(frame, x1, y1, x2, y2)
        self._draw_chip(frame, "Mặt bị che", x1, y1)

        # Ghi sự kiện bị che (debounce theo từng người — tránh tràn DB)
        if crop is not None and self._should_save(f"occluded:{result.person_id}"):
            self.occlusion_blocked.emit(
                result.person_id, result.similarity, crop
            )

    # ---------------------------------------------------------
    # Chống giả mạo (Bước 17)
    # ---------------------------------------------------------
    def _is_live(self, person_id: str, face) -> bool:
        """Cập nhật tracker liveness của người này với frame hiện tại.

        Tracker theo dõi chu kỳ chớp mắt (mở→nhắm→mở) trong cửa sổ trượt
        ~10 giây + KHOAN DUNG 6 giây khi mới xuất hiện (người thật vừa
        vào khung chưa kịp chớp không bị bắt vội). Ảnh tĩnh giơ trước
        camera không bao giờ chớp → hết khoan dung, trả False.
        """
        tracker = self._liveness.setdefault(person_id, LivenessTracker())
        tracker.update(face)
        return tracker.is_live()

    def _mark_spoof(self, frame: np.ndarray, face, result: MatchResult) -> None:
        """Đánh dấu khuôn mặt NGHI NGỜ GIẢ MẠO: mờ + khung trắng + chip "Giả mạo".

        Người dùng đã chốt chính sách "Chặn": nhận diện bị chặn (không
        hiển thị tên) và ghi sự kiện "Giả mạo" vào lịch sử (debounce).
        """
        x1, y1, x2, y2 = face.bbox.astype(int)
        # Cắt BẢN SAO TRƯỚC khi làm mờ để snapshot còn nét (giống người lạ)
        crop = self._crop_face(frame, face)

        self._blur_face(frame, face)
        self._draw_box(frame, x1, y1, x2, y2)
        self._draw_chip(frame, "Giả mạo", x1, y1)

        # Ghi sự kiện giả mạo (debounce theo từng người — tránh tràn DB)
        if crop is not None and self._should_save(f"spoof:{result.person_id}"):
            self.spoof_suspected.emit(result.person_id, result.similarity, crop)

    # ---------------------------------------------------------
    # Vẽ & ghi sự kiện
    # ---------------------------------------------------------
    def _draw_known(
        self,
        frame: np.ndarray,
        face,
        result: MatchResult,
        warn: bool = False,
    ) -> None:
        """Vẽ người đã biết: khung trắng + chip nhãn "Tên điểm" (kiểu tham chiếu).

        ``warn=True`` (mặt hơi bị che) → thêm "!" sau tên.
        """
        x1, y1, x2, y2 = face.bbox.astype(int)
        self._draw_box(frame, x1, y1, x2, y2)
        name = self._label_for(result.person_id)
        score = f" {result.similarity:.2f}" if result.similarity is not None else ""
        text = f"{name}!{score}" if warn else f"{name}{score}"
        self._draw_chip(frame, text, x1, y1)

    def _label_for(self, person_id: str) -> str:
        """Tên người theo id (qua service — worker không đụng DB)."""
        if self._service is not None:
            return self._service.label_of(person_id)
        return "?"

    def _mark_unknown(self, frame: np.ndarray, face, embedding) -> None:
        """Làm mờ vùng mặt (riêng tư) + khung trắng + chip 'Người lạ'."""
        x1, y1, x2, y2 = face.bbox.astype(int)
        # Lưu ảnh crop + embedding NGƯỜI LẠ (làm mẫu 1 cho [Đăng ký ngay]) —
        # cắt BẢN SAO TRƯỚC khi làm mờ để snapshot còn nét
        crop = None
        if embedding is not None:
            crop = self._crop_face(frame, face)
            self._last_unknown_seed = (crop, embedding, float(face.det_score))
        else:
            # Không trích được embedding → xóa seed cũ (tránh dùng nhầm người lạ cũ)
            self._last_unknown_seed = None

        self._blur_face(frame, face)
        self._draw_box(frame, x1, y1, x2, y2)
        # Người lạ nhưng mặt bị che/không rõ → ghi chú để người dùng biết lý do
        occ = face_metrics.occlusion_score(face, frame)
        if occ >= face_metrics.OCCLUSION_WARN:
            self._draw_chip(frame, "Mặt bị che / không rõ", x1, y1)
        else:
            self._draw_chip(frame, "Người lạ", x1, y1)

        # Sự kiện người lạ (debounce) — dùng đúng crop NÉT đã cắt
        if crop is not None and self._should_save("unknown"):
            self.recognition_unknown.emit(crop)

    def _maybe_emit_hit(self, frame: np.ndarray, face, result: MatchResult) -> None:
        """Ghi sự kiện nhận diện người đã biết (debounce 5s/người)."""
        if not self._should_save(result.person_id):
            return
        crop = self._crop_face(frame, face)
        self.recognition_hit.emit(result.person_id, result.similarity, crop)

    def _should_save(self, key: str) -> bool:
        now = time.time()
        if now - self._last_event_at.get(key, 0.0) < EVENT_DEBOUNCE_SECONDS:
            return False
        self._last_event_at[key] = now
        return True

    # ---------------------------------------------------------
    # Vẽ trợ giúp
    # ---------------------------------------------------------
    @staticmethod
    def _draw_label(
        frame: np.ndarray, text: str, x: int, y: int, color: tuple,
        scale: float = 0.8,
    ) -> None:
        """Vẽ nhãn nền màu đặc + chữ Unicode (tiếng Việt KHÔNG bị mất dấu).

        Thay cv2.putText (font HERSHEY không vẽ được dấu tiếng Việt) bằng
        PIL + font hệ thống (Segoe UI...). Giữ nguyên hình dạng cũ: nền màu
        theo ``color``, chữ trắng, vị trí ``y`` gần đáy nhãn như trước.
        Ký tự '⚠' được đổi thành '!' trước khi vẽ — đa số font hệ thống
        (Segoe UI/Arial/Tahoma) không có glyph này nên giữ nguyên sẽ vẽ
        thành ô trống/rác.
        """
        text = text.replace("⚠", "!")
        # HERSHEY cũ vẽ nét dày (thickness 2) → dùng cỡ lớn hơn một chút
        # để chữ nét đều, dễ đọc (20px ≈ nét của HERSHEY scale 0.8)
        font_px = max(14, int(scale * 25 + 0.5))
        font = _pil_font(font_px)
        asc, desc = font.getmetrics()
        th = asc + desc
        tw = float(font.getlength(text))
        baseline = int(y) - 2
        # Khung nền giống cv2 cũ: trên baseline - th - 8, dưới + 2 (chừa
        # thêm descender để chữ như g/y/j không bị cắt)
        top = baseline - th - 8
        bottom = baseline + desc + 2
        left = int(x)
        right = left + int(tw) + 10
        h, w = frame.shape[:2]
        if bottom <= top or left >= w or top >= h or right <= 0:
            return
        left, top = max(0, left), max(0, top)
        right, bottom = min(w, right), min(h, bottom)
        if right <= left or bottom <= top:
            return
        sub = frame[top:bottom, left:right].copy()
        pil = Image.fromarray(cv2.cvtColor(sub, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil)
        draw.rectangle(
            [0, 0, right - left - 1, bottom - top - 1],
            fill=(color[2], color[1], color[0]),
        )
        # anchor "ls" = trái-baseline: (3, baseline - top) trong tọa độ sub
        draw.text(
            (3, baseline - top), text, font=font,
            fill=(TEXT_COLOR[2], TEXT_COLOR[1], TEXT_COLOR[0]),
            anchor="ls",
        )
        frame[top:bottom, left:right] = cv2.cvtColor(
            np.asarray(pil), cv2.COLOR_RGB2BGR
        )

    @staticmethod
    def _draw_box(
        frame: np.ndarray,
        x1: int, y1: int, x2: int, y2: int,
        color: tuple = BOX_COLOR,
        thickness: int = 2,
    ) -> None:
        """Khung viền ĐỦ 4 cạnh, mảnh — giống ảnh tham chiếu (thay 4 góc L).

        Màu đồng nhất (BOX_COLOR trắng) cho MỌI trạng thái; trạng thái
        được thể hiện bằng chữ trên chip nhãn (_draw_chip).
        """
        cv2.rectangle(
            frame, (int(x1), int(y1)), (int(x2), int(y2)),
            color, thickness, cv2.LINE_AA,
        )

    @staticmethod
    def _draw_chip(
        frame: np.ndarray,
        text: str,
        x1: int,
        y1: int,
    ) -> None:
        """Chip nhãn TRẮNG ngay trên mép trái khung — giống ảnh tham chiếu.

        Toàn bộ chip (nền trắng + chữ đậm) vẽ TRONG MỘT LẦN bằng PIL —
        sửa lỗi nhãn cũ bị mất/cắt ký tự (vd "Long" hiện thành chữ rác):
          - bề rộng chip đo bằng CHÍNH font dùng để vẽ → luôn đủ chỗ;
          - chữ đặt cách lề trái ``pad_x - left`` (left = left-bearing của
            ký tự đầu) → ký tự đầu tiên luôn hiển thị đầy đủ;
          - chip bị kẹp trong biên frame (không vẽ tràn / cắt ngoài mép).
        Không đủ chỗ phía trên khung → dời xuống ngay dưới mép trên khung.
        """
        font = _pil_font(CHIP_FONT_PX)
        left, top, right, bottom = font.getbbox(text)
        tw, th = right - left, bottom - top
        if tw <= 0 or th <= 0:
            return
        pad_x, pad_y = 8, 4
        gap = 4
        chip_w = min(tw + pad_x * 2, max(1, frame.shape[1] - 2))
        chip_h = min(th + pad_y * 2, max(1, frame.shape[0] - 2))
        if chip_w <= pad_x * 2 or chip_h <= pad_y * 2:
            return  # frame quá nhỏ so với chữ — bỏ qua
        cx = max(1, min(int(x1), frame.shape[1] - chip_w))
        cy = int(y1) - chip_h - gap
        if cy < 1:
            cy = min(int(y1) + gap, max(1, frame.shape[0] - chip_h))
        bg_rgb = (CHIP_BG[2], CHIP_BG[1], CHIP_BG[0])
        fg_rgb = (CHIP_TEXT[2], CHIP_TEXT[1], CHIP_TEXT[0])
        x0, y0 = cx, cy
        sub = frame[y0:y0 + chip_h, x0:x0 + chip_w].copy()
        pil = Image.fromarray(cv2.cvtColor(sub, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil)
        draw.rectangle([0, 0, chip_w - 1, chip_h - 1], fill=bg_rgb)
        draw.text((pad_x - left, pad_y - top), text, font=font, fill=fg_rgb)
        frame[y0:y0 + chip_h, x0:x0 + chip_w] = cv2.cvtColor(
            np.asarray(pil), cv2.COLOR_RGB2BGR
        )

    @staticmethod
    def _blur_face(frame: np.ndarray, face) -> None:
        """Làm mờ Gaussian MẠNH vùng khuôn mặt (riêng tư người lạ).

        Kernel tỉ lệ theo kích thước mặt (tối thiểu 25px) — mờ rõ rệt,
        không nhìn được ai là ai (mục đích làm mờ riêng tư).
        """
        x1, y1, x2, y2 = face.bbox.astype(int)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
        w, h = x2 - x1, y2 - y1
        if w < 4 or h < 4:
            return
        region = frame[y1:y2, x1:x2].copy()
        k = max(25, min(w, h) // 4 | 1)
        if k % 2 == 0:
            k += 1  # kernel Gaussian phải là số LẺ
        frame[y1:y2, x1:x2] = cv2.GaussianBlur(region, (k, k), 0)

    @staticmethod
    def _crop_face(frame: np.ndarray, face) -> np.ndarray:
        """Cắt ảnh khuôn mặt (có viền) — bản SAO để gửi qua tín hiệu an toàn."""
        x1, y1, x2, y2 = face.bbox.astype(int)
        h, w = frame.shape[:2]
        pad_x = int((x2 - x1) * 0.25)
        pad_y = int((y2 - y1) * 0.25)
        x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
        x2, y2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
        return frame[y1:y2, x1:x2].copy()

    # ---------------------------------------------------------
    # Bước 22: Quality Gate — cảnh báo realtime
    # ---------------------------------------------------------
    QUALITY_GATE_INTERVAL = 2.0  # giây giữa 2 lần check (tránh spam)

    def _draw_quality_gate(self, frame: np.ndarray, faces: list) -> None:
        """Kiểm tra chất lượng khung hình + vẽ cảnh báo realtime.

        Chỉ check mỗi ~2 giây để không spam.
        """
        now = time.time()
        if now - self._last_quality_check < self.QUALITY_GATE_INTERVAL:
            return
        self._last_quality_check = now

        warnings: list[str] = []

        # 1. Không thấy mặt nào
        if not faces:
            warnings.append("⚠ Không thấy khuôn mặt — đưa mặt vào khung")
        else:
            face = faces[0]  # check mặt lớn nhất
            # 2. Mặt quá nhỏ (xa camera)
            x1, y1, x2, y2 = face.bbox.astype(int)
            w, h = x2 - x1, y2 - y1
            if w < 100 or h < 100:
                warnings.append("⚠ Khuôn mặt quá xa — tiến lại gần camera")
            # 3. Ảnh mờ — ngưỡng THÍCH NGHI theo độ nét nền của webcam
            #    (Bước 25): webcam nền mềm (Laplacian ~40-50 dù giữ yên)
            #    không bị cảnh báo oan, máy nét vẫn lọc rung như trước.
            sharp = face_metrics._sharpness(frame, face)
            if sharp > self._best_sharpness:
                self._best_sharpness = sharp
            blur_warn = max(
                QUALITY_BLUR_WARN_FLOOR,
                self._best_sharpness * QUALITY_BLUR_WARN_RATIO,
            )
            if sharp < blur_warn:
                warnings.append("⚠ Ảnh mờ — giữ yên đầu, tránh rung")

        # 4. Ánh sáng yếu/tối
        brightness = float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())
        if brightness < 50:
            warnings.append("⚠ Ánh sáng yếu — bật thêm đèn")
        elif brightness > 230:
            warnings.append("⚠ Quá sáng — tránh ngược sáng")

        # Vẽ cảnh báo (nếu có)
        if warnings:
            h, w = frame.shape[:2]
            y_start = 30
            for msg in warnings[:2]:  # tối đa 2 cảnh báo
                self._draw_label(frame, msg, 10, y_start, SPOOF_COLOR)
                y_start += 35


class CameraView(QWidget):
    """Khu vực xem webcam: video preview + điều khiển."""

    # Người dùng nhấn [ Đăng ký ngay ]: (ảnh crop, embedding, chất lượng)
    enroll_requested = Signal(object, object, float)

    def __init__(
        self,
        config: Config,
        db: Database | None = None,
        detector: FaceDetector | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._db = db
        self._detector = detector
        self._embedder: FaceEmbedder | None = None
        self._service: RecognitionService | None = None
        self._thread: QThread | None = None
        self._worker: CameraWorker | None = None
        self._last_unknown_seed: tuple | None = None
        if db is not None:
            self._service = RecognitionService(db)
        self._build_ui()

    # ---------------------------------------------------------
    # Giao diện
    # ---------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # --- Vùng video (nền tối cố định — video cần nền tối ở cả 2 theme) ---
        self._video_label = QLabel("Webcam chưa mở")
        self._video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Minimum NHỎ (không phải 640x480): ảnh vẫn scale giữ tỉ lệ khi hiển thị,
        # nhưng layout không bị ép rộng → cửa sổ co nhỏ được (kéo góc hoạt động
        # trên màn hình nhỏ / scale 125%).
        self._video_label.setMinimumSize(400, 300)
        self._video_label.setObjectName("videoLabel")
        self._video_label.setStyleSheet("font-size: 18px;")
        layout.addWidget(self._video_label, stretch=1)

        # --- Panel điều khiển bên phải ---
        panel = QVBoxLayout()
        panel.setSpacing(8)

        ctl_title = QLabel("ĐIỀU KHIỂN")
        ctl_title.setObjectName("sectionTitle")
        panel.addWidget(ctl_title)

        self._status_label = QLabel("Trạng thái: chưa mở")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("font-size: 14px;")
        panel.addWidget(self._status_label)

        self._start_btn = QPushButton("Mở webcam")
        self._start_btn.clicked.connect(self.start_camera)
        panel.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Tạm dừng")
        self._stop_btn.clicked.connect(self.stop_camera)
        self._stop_btn.setEnabled(False)
        panel.addWidget(self._stop_btn)

        # Nút [ Đăng ký ngay ] — chỉ hiện khi có người lạ trong khung hình
        self._enroll_now_btn = QPushButton("⚡ Đăng ký ngay")
        self._enroll_now_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._enroll_now_btn.setObjectName("accentBtn")
        self._enroll_now_btn.setToolTip(
            "Đăng ký người lạ đang ở trong khung hình (ảnh hiện tại làm mẫu 1)"
        )
        self._enroll_now_btn.setStyleSheet("border-radius: 6px; padding: 10px; font-size: 14px;")
        self._enroll_now_btn.clicked.connect(self._on_enroll_now)
        self._enroll_now_btn.hide()  # ẩn cho tới khi có người lạ
        panel.addWidget(self._enroll_now_btn)

        panel.addStretch(1)

        layout.addLayout(panel)

    # ---------------------------------------------------------
    # Điều khiển camera
    # ---------------------------------------------------------
    def start_camera(self) -> None:
        """Mở webcam và chạy vòng lặp nhận diện trong QThread."""
        if self._thread is not None and self._thread.isRunning():
            return  # đã chạy rồi (hoặc thread cũ còn sống do camera treo — KHÔNG mở song song)
        if self._thread is not None:
            # Thread cũ đã kết thúc nhưng chưa được dọn (stop_camera giữ lại
            # khi chờ dừng quá hạn) → dọn sạch trước khi tạo thread mới
            self._thread = None
            self._worker = None

        # Nạp model phát hiện khuôn mặt lần đầu (mất ~1-2 giây — chỉ 1 lần)
        if self._detector is None:
            self._status_label.setText("Đang nạp model nhận diện...")
            QApplication.processEvents()  # vẽ lại label ngay (UI bị chặn lúc nạp model)
            self._detector = self._load_detector()
            if self._detector is None:
                return  # giữ nguyên thông báo lỗi model, không mở camera
        # Embedder tái sử dụng FaceAnalysis của detector — không nạp model lần 2
        if self._embedder is None:
            self._embedder = FaceEmbedder(self._detector)

        # Nạp danh sách embedding đã đăng ký vào bộ so khớp (LUỒNG UI — trước khi mở)
        if self._service is not None:
            try:
                self._service.reload()
            except Exception:  # noqa: BLE001
                logger.exception("Lỗi nạp embedding cho bộ so khớp")
                self._service = None
        self._last_unknown_seed = None

        self._thread = QThread(self)
        self._worker = CameraWorker(
            self._config.camera_index,
            self._config.camera_width,
            self._config.camera_height,
            detector=self._detector,
            embedder=self._embedder,
            service=self._service,
            threshold=self._config.recognition_threshold,
            anti_spoofing_enabled=self._config.anti_spoofing_enabled,
            smoothing_window=self._config.smoothing_window,
            clahe_enabled=self._config.clahe_enabled,
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.started.connect(self._on_camera_started)
        self._worker.fps_updated.connect(self._on_fps)
        self._worker.error.connect(self._on_error)
        self._worker.unknown_face.connect(self._on_unknown_face)
        self._worker.unknown_crop.connect(self._on_unknown_crop)
        self._worker.recognition_hit.connect(self._on_recognition_hit)
        self._worker.recognition_unknown.connect(self._on_recognition_unknown)
        self._worker.spoof_suspected.connect(self._on_spoof_suspected)
        self._worker.occlusion_blocked.connect(self._on_occlusion_blocked)

        self._thread.start()
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_label.setText("Đang mở webcam...")
        logger.info("Bắt đầu camera (index=%d)", self._config.camera_index)

    @property
    def detector(self) -> FaceDetector | None:
        """FaceDetector đã nạp (nếu có) — cho EnrollmentDialog tái sử dụng model."""
        return self._detector

    def _load_detector(self) -> FaceDetector | None:
        """Tạo FaceDetector; trả None nếu model thiếu/lỗi (camera vẫn chạy, không nhận diện)."""
        try:
            if not (MODELS_ROOT / "models" / "buffalo_l").exists():
                raise FileNotFoundError(
                    f"Chưa có model tại {MODELS_ROOT / 'models' / 'buffalo_l'} — "
                    "chạy lại Bước 0.4 để tải model."
                )
            detector = FaceDetector(MODELS_ROOT)
            logger.info("Đã nạp model phát hiện khuôn mặt")
            return detector
        except Exception as exc:  # noqa: BLE001 — hiển thị lỗi cho người dùng, không crash
            logger.exception("Không nạp được model: %s", exc)
            self._status_label.setText(f"⚠ Không nạp được model phát hiện khuôn mặt: {exc}")
            return None

    def stop_camera(self) -> None:
        """Dừng vòng lặp, đóng camera và giải phóng thread.

        QUAN TRỌNG (an toàn đa luồng): nếu worker KHÔNG kịp dừng trong
        thời gian chờ (ví dụ MSMF treo khi mở camera lỗi), KHÔNG được xóa
        thread — giữ tham chiếu cho nó tự kết thúc. Xóa sớm khi luồng còn
        chạy sẽ gây hỏng heap (cv2 không an toàn đa luồng) nếu mở lại ngay:
        2 luồng camera chạy song song → crash.
        """
        if self._thread is None and self._worker is None:
            return  # chưa chạy — không làm gì (tránh log/thay đổi UI thừa)
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            stopped = self._thread.wait(1500)  # chờ tối đa 1.5s cho worker dừng
            if stopped:
                # Thread đã kết thúc sạch → dọn tham chiếu
                self._thread = None
                self._worker = None
            # else: thread vẫn chạy (camera treo) → GIỮ tham chiếu;
            #       start_camera kiểm tra isRunning() để không mở song song.

        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText("Trạng thái: đã dừng")
        self._enroll_now_btn.hide()
        logger.info("Dừng camera")

    # ---------------------------------------------------------
    # Xử lý tín hiệu từ worker
    # ---------------------------------------------------------
    @Slot(object)
    def _on_frame(self, frame: np.ndarray) -> None:
        """Nhận frame BGR (đã vẽ) → chuyển RGB → hiển thị lên QLabel."""
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)  # sao chép dữ liệu — an toàn
        self._video_label.setPixmap(
            pixmap.scaled(
                self._video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    @Slot(int, int)
    def _on_camera_started(self, width: int, height: int) -> None:
        self._status_label.setText(f"Đang chạy: CAM {self._config.camera_index} · {width}×{height}")

    @Slot(float)
    def _on_fps(self, fps: float) -> None:
        self._status_label.setText(
            f"{self._status_label.text().split(' · FPS')[0]} · FPS: {fps:.1f}"
        )

    @Slot(bool)
    def _on_unknown_face(self, has_unknown: bool) -> None:
        """Bật/tắt nút [ Đăng ký ngay ] theo sự hiện diện của người lạ."""
        self._enroll_now_btn.setVisible(has_unknown)

    @Slot(object, object, float)
    def _on_unknown_crop(self, crop, embedding, quality: float) -> None:
        """Lưu ảnh + embedding người lạ vừa xuất hiện (mẫu 1 cho đăng ký ngay)."""
        self._last_unknown_seed = (crop, embedding, quality)

    @Slot(str, float, object)
    def _on_recognition_hit(self, person_id: str, similarity: float, crop) -> None:
        """Người đã biết được nhận diện → ghi sự kiện + snapshot (LUỒNG UI)."""
        if self._service is not None:
            self._service.save_event(
                person_id=person_id,
                similarity=similarity,
                face_crop=crop,
            )

    @Slot(object)
    def _on_recognition_unknown(self, crop) -> None:
        """Người lạ → ghi sự kiện (is_unknown=1) + snapshot."""
        if self._service is not None:
            self._service.save_event(
                person_id=None,
                similarity=None,
                face_crop=crop,
                is_unknown=True,
            )

    @Slot(str, float, object)
    def _on_spoof_suspected(self, person_id: str, similarity: float, crop) -> None:
        """Nghi ngờ giả mạo → ghi sự kiện 'Giả mạo' vào lịch sử (audit trail)."""
        if self._service is not None:
            self._service.save_event(
                person_id=person_id,
                similarity=similarity,
                face_crop=crop,
                is_spoof=True,
            )

    @Slot(str, float, object)
    def _on_occlusion_blocked(self, person_id: str, similarity: float, crop) -> None:
        """Mặt bị che nhiều → ghi sự kiện 'Mặt bị che' vào lịch sử (audit trail)."""
        if self._service is not None:
            self._service.save_event(
                person_id=person_id,
                similarity=similarity,
                face_crop=crop,
                is_occluded=True,
            )

    @Slot(str)
    def _on_error(self, message: str) -> None:
        """Lỗi camera: dừng trước, rồi hiện thông báo (tránh bị ghi đè)."""
        logger.error("Lỗi camera: %s", message)
        self.stop_camera()
        self._status_label.setText(f"⚠ {message}")

    # ---------------------------------------------------------
    # Đăng ký ngay
    # ---------------------------------------------------------
    def _on_enroll_now(self) -> None:
        """Người dùng nhấn [ Đăng ký ngay ] → gửi ảnh + embedding người lạ."""
        if self._last_unknown_seed is not None:
            crop, embedding, quality = self._last_unknown_seed
            self.enroll_requested.emit(crop, embedding, quality)

    # ---------------------------------------------------------
    # Vòng đời widget
    # ---------------------------------------------------------
    def hideEvent(self, event) -> None:  # noqa: N802 (chuẩn Qt)
        """Tự dừng camera khi rời màn hình (tiết kiệm tài nguyên + riêng tư)."""
        self.stop_camera()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 (chuẩn Qt)
        """Tự mở webcam khi vào màn hình CameraView."""
        super().showEvent(event)
        if self._thread is None or not self._thread.isRunning():
            self.start_camera()

    def closeEvent(self, event) -> None:  # noqa: N802 (chuẩn Qt)
        self.stop_camera()
        super().closeEvent(event)
