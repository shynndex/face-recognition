"""SettingsView — trang Cài đặt hoàn chỉnh (Bước 14, wireframe 5.5.7).

Các nhóm cài đặt (mỗi nhóm một QFrame#card):
  - CAMERA: chọn camera (index) + độ phân giải + nút [Kết nối thử] đo FPS
    thực tế bằng CameraCapture (chạy trong QThread — không đơ UI).
  - NHẬN DIỆN: thanh trượt ngưỡng tương đồng 0.00–0.90 (mặc định 0.40),
    hiển thị giá trị thời gian thực + giải thích hướng chỉnh.
  - BẢO MẬT: [Đổi mật khẩu] → PasswordDialog xác thực mật khẩu CŨ → dialog
    nhập mật khẩu MỚI 2 lần → cập nhật hash trong config.json (FR-11).
    Kèm hàng "Câu hỏi bảo mật" + [Đổi câu hỏi bảo mật] (FR-5 — cũng xác
    thực mật khẩu cũ) và combo "Tự khóa khi không dùng" FR-7 (Tắt/1/5/15
    phút → config.idle_lock_minutes).
  - ĐỒNG BỘ CLOUD: bật/tắt đồng bộ (FR-11 — cần mật khẩu); logic đồng bộ
    thật ở Bước 15, nút [Đồng bộ ngay] tạm khóa tới khi có SyncService.

[Lưu thay đổi] ghi config.json; [Khôi phục mặc định] đưa các ô về giá trị
khởi tạo (chưa lưu — người dùng bấm Lưu nếu muốn giữ).

Các thay đổi NHẠY CẢM (đổi mật khẩu, bật/tắt cloud) đều qua PasswordDialog
theo FR-11 — cùng nguyên tắc với Xóa người (Bước 7) và Xóa sự kiện (Bước 11).
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QComboBox,
    QDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.core.temporal import DEFAULT_WINDOW_SIZE
from app.infrastructure.camera import CameraCapture
from app.infrastructure.d1_client import D1Client, D1Error
from app.infrastructure.s3_client import S3Error
from app.services.auth import (
    REQUIREMENTS_TEXT,
    AuthService,
    validate_password_strength,
)
from app.services.sync import SyncService
from app.ui.password_dialog import PasswordDialog
from app.ui.widgets import PasswordEdit

logger = logging.getLogger(__name__)

# Các mức độ phân giải cho combo (giá trị lưu vào config)
RESOLUTIONS: list[tuple[str, int, int]] = [
    ("640 × 480", 640, 480),
    ("1280 × 720", 1280, 720),
    ("1920 × 1080", 1920, 1080),
]

# Số camera tối đa liệt kê trong combo
MAX_CAMERA_INDEX = 5

# Ngưỡng: slider chạy 0..90 (ứng 0.00–0.90) — khuyến nghị 0.40
SLIDER_MIN = 0
SLIDER_MAX = 90


class CameraProbeWorker(QObject):
    """Đo FPS thực tế của camera — chạy trong QThread (không đơ UI).

    CameraCapture phải được tạo/đọc/đóng trong CÙNG một luồng (cv2 không
    an toàn đa luồng) → worker mở camera, đọc ~1.2 giây, rồi báo kết quả.
    """

    finished = Signal(bool, float, int, int)  # (ok, fps, w, h)

    def __init__(self, index: int, width: int, height: int) -> None:
        super().__init__()
        self._index = index
        self._width = width
        self._height = height

    @Slot()
    def run(self) -> None:
        capture = CameraCapture(self._index, self._width, self._height)
        try:
            if not capture.open():
                self.finished.emit(False, 0.0, 0, 0)
                return
            t0 = time.time()
            frames = 0
            while time.time() - t0 < 1.2:
                if capture.read() is None:
                    break
                frames += 1
            elapsed = time.time() - t0
            fps = frames / elapsed if elapsed > 0 else 0.0
            self.finished.emit(True, fps, capture.actual_width, capture.actual_height)
        finally:
            capture.release()


class CloudProbeWorker(QObject):
    """Kiểm tra kết nối D1 (SELECT 1) + object storage (head_bucket) — trong QThread.

    Kiểm tra lần lượt: D1 trước (bắt buộc), storage sau (nếu đã điền thông
    tin — chưa điền thì báo "bỏ qua", không coi là lỗi).
    """

    finished = Signal(bool, str)  # (ok, thông điệp)

    def __init__(self, service: SyncService) -> None:
        super().__init__()
        self._service = service

    @Slot()
    def run(self) -> None:
        client = self._service.make_client()
        if client is None:
            self.finished.emit(False, "Chưa đủ thông tin cloud (Account/Database/Token)")
            return
        try:
            client.test_connection()
        except D1Error as exc:
            self.finished.emit(False, f"✗ D1: {exc}")
            return

        # Storage: tùy chọn — chưa điền thông tin thì bỏ qua, không phải lỗi
        storage = self._service.make_storage_client()
        if storage is None:
            self.finished.emit(
                True, "✓ D1 kết nối thành công · Lưu ảnh (Supabase) chưa cấu hình — bỏ qua"
            )
            return
        try:
            storage.test_connection()
            self.finished.emit(
                True, "✓ D1 + Supabase Storage kết nối thành công — sẵn sàng đồng bộ ảnh"
            )
        except S3Error as exc:
            self.finished.emit(False, f"✗ Storage: {exc} (D1 đã kết nối được)")


class SyncWorker(QObject):
    """Chạy một lượt đồng bộ đầy đủ (push + pull) — trong QThread."""

    finished = Signal(bool, str)  # (ok, tóm tắt kết quả)

    def __init__(self, service: SyncService) -> None:
        super().__init__()
        self._service = service

    @Slot()
    def run(self) -> None:
        result = self._service.run_full_sync()
        self.finished.emit(result.ok, result.summary())


class ChangePasswordDialog(QDialog):
    """Nhập mật khẩu MỚI 2 lần (sau khi đã xác thực mật khẩu cũ)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ĐỔI MẬT KHẨU")
        self.setModal(True)
        self.setFixedWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel(REQUIREMENTS_TEXT)
        title.setStyleSheet("font-size: 13px; font-weight: bold;")
        title.setWordWrap(True)
        layout.addWidget(title)

        # Ô mật khẩu + icon con mắt ngay trong ô (không còn nút chữ riêng)
        self._pw1 = PasswordEdit()
        self._pw1.setPlaceholderText("Mật khẩu mới")
        self._pw1.returnPressed.connect(self._on_confirm)
        layout.addWidget(self._pw1)

        self._pw2 = PasswordEdit()
        self._pw2.setPlaceholderText("Nhập lại mật khẩu mới")
        self._pw2.returnPressed.connect(self._on_confirm)
        layout.addWidget(self._pw2)

        self._error = QLabel()
        self._error.setStyleSheet("color: #d33; font-weight: bold;")
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        layout.addWidget(self._error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Hủy")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("Đổi mật khẩu")
        confirm.setDefault(True)
        confirm.clicked.connect(self._on_confirm)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

        self._pw1.setFocus()

    def _on_confirm(self) -> None:
        new_pw = self._pw1.text()
        errors = validate_password_strength(new_pw)
        if errors:
            self._show_error("\n".join(errors))
            return
        if new_pw != self._pw2.text():
            self._show_error("Hai lần nhập không khớp")
            return
        self.accept()

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(True)

    def new_password(self) -> str:
        """Mật khẩu mới đã xác nhận (gọi sau khi exec() trả Accepted)."""
        return self._pw1.text()


class SecurityQuestionsDialog(QDialog):
    """Nhập/cập nhật 2 câu hỏi bảo mật (spec quản lý mật khẩu — FR-5).

    Mở từ Cài đặt → BẢO MẬT sau khi đã xác thực mật khẩu cũ qua
    PasswordDialog; câu hỏi cũ được điền sẵn (giữ nguyên được),
    câu trả lời phải nhập lại (chỉ lưu hash argon2 — không plaintext).
    """

    def __init__(self, auth: AuthService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._auth = auth
        self.setWindowTitle("ĐỔI CÂU HỎI BẢO MẬT")
        self.setModal(True)
        self.setFixedWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        warn = QLabel(
            "Dùng để KHÔI PHỤC mật khẩu khi quên (màn hình khóa → 'Quên mật khẩu?'). "
            "Câu trả lời chỉ lưu dạng hash — hãy nhớ kỹ; quên cả câu trả lời thì "
            "không đặt lại được mật khẩu."
        )
        warn.setStyleSheet("font-size: 12px; color: #b06a00;")
        warn.setWordWrap(True)
        layout.addWidget(warn)

        cfg = auth.config
        self._q1_edit = QLineEdit(cfg.security_question_1)
        self._q1_edit.setPlaceholderText("Câu hỏi bảo mật 1 (vd: Quê quán của bạn?)")
        self._a1_edit = QLineEdit()
        self._a1_edit.setPlaceholderText("Câu trả lời 1 (≥3 ký tự, phân biệt dấu)")
        self._q2_edit = QLineEdit(cfg.security_question_2)
        self._q2_edit.setPlaceholderText("Câu hỏi bảo mật 2 (khác câu 1)")
        self._a2_edit = QLineEdit()
        self._a2_edit.setPlaceholderText("Câu trả lời 2")

        form = QFormLayout()
        form.addRow("Câu hỏi 1:", self._q1_edit)
        form.addRow("Trả lời 1:", self._a1_edit)
        form.addRow("Câu hỏi 2:", self._q2_edit)
        form.addRow("Trả lời 2:", self._a2_edit)
        layout.addLayout(form)

        self._error = QLabel()
        self._error.setStyleSheet("color: #d33; font-weight: bold;")
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        layout.addWidget(self._error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Hủy")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("Lưu câu hỏi")
        confirm.setDefault(True)
        confirm.clicked.connect(self._on_confirm)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

        self._a1_edit.setFocus()

    def _on_confirm(self) -> None:
        """Lưu qua AuthService.set_security_questions (validate + hash)."""
        try:
            self._auth.set_security_questions(
                self._q1_edit.text(), self._a1_edit.text(),
                self._q2_edit.text(), self._a2_edit.text(),
            )
        except ValueError as exc:
            self._show_error(str(exc))
            return
        logger.info("Đã đổi câu hỏi bảo mật qua Cài đặt")
        self.accept()

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(True)


class SettingsView(QWidget):
    """Trang Cài đặt — đọc/ghi config.json (wireframe 5.5.7)."""

    # Phát khi [Lưu thay đổi] thành công — MainWindow dừng camera để lần
    # mở sau dùng cấu hình mới (index/ngưỡng đọc lúc start_camera).
    settings_saved = Signal()

    def __init__(
        self,
        config: Config,
        auth: AuthService,
        sync: SyncService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._auth = auth
        self._sync = sync
        self._probe_thread: QThread | None = None
        self._probe_worker: CameraProbeWorker | None = None
        self._cloud_thread: QThread | None = None
        self._cloud_worker: QObject | None = None
        # Service chấm công (CRUD ca, ca mặc định — attendance-spec FR-3)
        from app.services.attendance import AttendanceService

        self._attendance = AttendanceService(sync.db)
        self._build_ui()
        self._load_from_config()
        self._refresh_shifts()

    # ---------------------------------------------------------
    # Giao diện (wireframe 5.5.7)
    # ---------------------------------------------------------
    def _build_ui(self) -> None:
        # Toàn bộ nội dung nằm trong QScrollArea: trang này cao (~900px) nếu
        # để trực tiếp sẽ ÉP cửa sổ chính phải to tương ứng → trên màn hình
        # nhỏ / scale 125-150% cửa sổ cao hơn màn hình (bị cắt đáy) và không
        # kéo nhỏ được. Bọc scroll → trang cuộn được, cửa sổ co về kích thước
        # hợp lý (fix lỗi "kéo góc không resize được").
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        scroll.setWidget(content)

        title = QLabel("CÀI ĐẶT")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        # ---- CAMERA ----
        camera_card = QFrame()
        camera_card.setObjectName("card")
        camera_card.setStyleSheet("border-radius: 10px;")
        cam_grid = QGridLayout(camera_card)
        cam_grid.setContentsMargins(16, 12, 16, 12)
        cam_grid.setSpacing(8)

        cam_header = QLabel("── CAMERA ──")
        cam_header.setObjectName("sectionTitle")
        cam_grid.addWidget(cam_header, 0, 0, 1, 2)
        cam_grid.addWidget(QLabel("Camera:"), 1, 0)

        self._camera_combo = QComboBox()
        self._camera_combo.setEditable(True)
        for i in range(MAX_CAMERA_INDEX + 1):
            self._camera_combo.addItem(f"CAM {i}")
        self._camera_combo.setToolTip("Chỉ số webcam (0 = mặc định). Có thể nhập tay.")
        cam_grid.addWidget(self._camera_combo, 1, 1)

        cam_grid.addWidget(QLabel("Độ phân giải:"), 2, 0)
        self._res_combo = QComboBox()
        for label, w, h in RESOLUTIONS:
            self._res_combo.addItem(label, (w, h))
        cam_grid.addWidget(self._res_combo, 2, 1)

        self._test_btn = QPushButton("Kết nối thử")
        self._test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._test_btn.setObjectName("secondaryBtn")
        self._test_btn.setStyleSheet("border-radius: 5px; padding: 5px 14px; font-size: 12px;")
        self._test_btn.clicked.connect(self._on_test_camera)
        cam_grid.addWidget(self._test_btn, 3, 0)

        self._camera_status = QLabel("(chưa kiểm tra)")
        self._camera_status.setStyleSheet("font-size: 12px;")
        cam_grid.addWidget(self._camera_status, 3, 1)
        layout.addWidget(camera_card)

        # ---- NHẬN DIỆN ----
        recog_card = QFrame()
        recog_card.setObjectName("card")
        recog_card.setStyleSheet("border-radius: 10px;")
        recog_grid = QGridLayout(recog_card)
        recog_grid.setContentsMargins(16, 12, 16, 12)
        recog_grid.setSpacing(8)

        recog_header = QLabel("── NHẬN DIỆN ──")
        recog_header.setObjectName("sectionTitle")
        recog_grid.addWidget(recog_header, 0, 0, 1, 3)
        recog_grid.addWidget(QLabel("Ngưỡng tương đồng:"), 1, 0)

        self._threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self._threshold_slider.setRange(SLIDER_MIN, SLIDER_MAX)
        self._threshold_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._threshold_slider.setTickInterval(10)
        self._threshold_slider.valueChanged.connect(self._on_threshold_changed)
        recog_grid.addWidget(self._threshold_slider, 1, 1)

        self._threshold_label = QLabel()
        self._threshold_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        recog_grid.addWidget(self._threshold_label, 1, 2)

        hint = QLabel(
            "thấp ──── khuyến nghị ──── cao\n"
            "Cao hơn = nghiêm ngặt hơn: ít nhầm lẫn nhưng dễ bỏ sót.\n"
            "Nếu nhận diện nhầm người → tăng ngưỡng; bỏ sót người quen → giảm ngưỡng."
        )
        hint.setStyleSheet("font-size: 12px;")
        hint.setWordWrap(True)
        recog_grid.addWidget(hint, 2, 0, 1, 3)

        # Chống giả mạo (Bước 17, FR-8): yêu cầu thấy chớp mắt định kỳ
        # trong khung hình, nếu không → chặn nhận diện (coi như người lạ).
        self._spoof_check = QCheckBox(
            "Chống giả mạo (yêu cầu chớp mắt định kỳ — chặn ảnh/video giả)"
        )
        self._spoof_check.setToolTip(
            "Ảnh in / ảnh trên màn hình không chớp mắt → bị chặn. "
            "Tắt nếu thấy phiền khi nhìn chăm chú lâu."
        )
        recog_grid.addWidget(self._spoof_check, 3, 0, 1, 3)

        # Temporal smoothing (Bước 19): thanh trượt độ ổn định tên
        recog_grid.addWidget(QLabel("Độ ổn định tên:"), 4, 0)
        self._smoothing_slider = QSlider(Qt.Orientation.Horizontal)
        self._smoothing_slider.setRange(0, 15)  # 0=không smoothing, 15=mượt nhất
        self._smoothing_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._smoothing_slider.setTickInterval(5)
        self._smoothing_slider.valueChanged.connect(self._on_smoothing_changed)
        recog_grid.addWidget(self._smoothing_slider, 4, 1)
        self._smoothing_label = QLabel()
        self._smoothing_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        recog_grid.addWidget(self._smoothing_label, 4, 2)
        smoothing_hint = QLabel(
            "Ít ──── khuyến nghị ──── Nhiều\n"
            "Nhiều = tên ít nhấp nháy hơn nhưng phản hồi chậm hơn."
        )
        smoothing_hint.setStyleSheet("font-size: 12px;")
        smoothing_hint.setWordWrap(True)
        recog_grid.addWidget(smoothing_hint, 5, 0, 1, 3)

        # CLAHE preprocessing (Bước 21): chuẩn hóa ánh sáng trước khi embed
        self._clahe_check = QCheckBox(
            "Chuẩn hóa ánh sáng (CLAHE — giúp nhận diện ổn định hơn khi sáng thay đổi)"
        )
        self._clahe_check.setToolTip(
            "CLAHE điều chỉnh luminance trước khi embed — "
            "giúp cosine ổn định hơn khi ánh sáng thay đổi. "
            "Tắt nếu webcam đã có ánh sáng cố định tốt."
        )
        recog_grid.addWidget(self._clahe_check, 6, 0, 1, 3)
        layout.addWidget(recog_card)

        # ---- CHẤM CÔNG (attendance-spec FR-3/FR-6) ----
        # CRUD ca + ca mặc định (lưu DB, nút Lưu riêng) + ngày làm việc
        # (lưu config.json, áp khi bấm [Lưu thay đổi] chung).
        att_card = QFrame()
        att_card.setObjectName("card")
        att_card.setStyleSheet("border-radius: 10px;")
        att_layout = QVBoxLayout(att_card)
        att_layout.setContentsMargins(16, 12, 16, 12)
        att_layout.setSpacing(8)

        att_header = QLabel("── CHẤM CÔNG ──")
        att_header.setObjectName("sectionTitle")
        att_layout.addWidget(att_header)

        self._shifts_label = QLabel("Ca làm việc: (đang tải...)")
        self._shifts_label.setStyleSheet("font-size: 13px;")
        self._shifts_label.setWordWrap(True)
        att_layout.addWidget(self._shifts_label)

        shift_btn_row = QHBoxLayout()
        self._add_shift_btn = QPushButton("＋ Thêm ca")
        self._add_shift_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_shift_btn.setObjectName("secondaryBtn")
        self._add_shift_btn.setStyleSheet("border-radius: 5px; padding: 5px 14px; font-size: 12px;")
        self._add_shift_btn.clicked.connect(self._on_add_shift)
        shift_btn_row.addWidget(self._add_shift_btn)

        self._edit_shift_btn = QPushButton("Sửa ca")
        self._edit_shift_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._edit_shift_btn.setObjectName("secondaryBtn")
        self._edit_shift_btn.setStyleSheet("border-radius: 5px; padding: 5px 14px; font-size: 12px;")
        self._edit_shift_btn.clicked.connect(self._on_edit_shift)
        shift_btn_row.addWidget(self._edit_shift_btn)

        self._del_shift_btn = QPushButton("Xóa ca")
        self._del_shift_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._del_shift_btn.setObjectName("dangerBtn")
        self._del_shift_btn.setStyleSheet("border-radius: 5px; padding: 5px 14px; font-size: 12px;")
        self._del_shift_btn.clicked.connect(self._on_delete_shift)
        shift_btn_row.addWidget(self._del_shift_btn)
        shift_btn_row.addStretch(1)
        att_layout.addLayout(shift_btn_row)

        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Ca mặc định (người chưa gán):"))
        self._default_shift_combo = QComboBox()
        self._default_shift_combo.setToolTip("Ca áp dụng cho người chưa được gán ca riêng")
        default_row.addWidget(self._default_shift_combo, stretch=1)
        att_layout.addLayout(default_row)

        # Ngày làm việc trong tuần (config.attendance_workdays — FR-6)
        workdays_row = QHBoxLayout()
        workdays_row.addWidget(QLabel("Ngày làm việc:"))
        self._workday_checks: list[QCheckBox] = []
        for day_name in ("T2", "T3", "T4", "T5", "T6", "T7", "CN"):
            cb = QCheckBox(day_name)
            workdays_row.addWidget(cb)
            self._workday_checks.append(cb)
        workdays_row.addStretch(1)
        att_layout.addLayout(workdays_row)

        # Đơn giá 1 công quy đổi (VND) — lương thô = công quy đổi × đơn giá
        rate_row = QHBoxLayout()
        rate_row.addWidget(QLabel("Đơn giá 1 công (VND):"))
        self._pay_rate_spin = QSpinBox()
        self._pay_rate_spin.setRange(0, 100_000_000)
        self._pay_rate_spin.setSingleStep(10_000)
        self._pay_rate_spin.setValue(self._config.attendance_pay_rate)
        self._pay_rate_spin.setToolTip(
            "Tiền cho 1 công quy đổi (VD 350000). Lương = số công × hệ số ca × đơn giá. "
            "Để 0 nếu chỉ cần công quy đổi, không ra tiền."
        )
        rate_row.addWidget(self._pay_rate_spin, stretch=1)
        att_layout.addLayout(rate_row)

        att_btn_row = QHBoxLayout()
        att_btn_row.addStretch(1)
        self._save_att_btn = QPushButton("Lưu ca mặc định + ngày làm việc")
        self._save_att_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_att_btn.setObjectName("primaryBtn")
        self._save_att_btn.setStyleSheet("border-radius: 6px; padding: 6px 16px; font-size: 12px;")
        self._save_att_btn.clicked.connect(self._on_save_attendance)
        att_btn_row.addWidget(self._save_att_btn)
        att_layout.addLayout(att_btn_row)

        att_hint = QLabel(
            "Ca lưu vào CSDL (đẩy lên cloud khi đồng bộ). Gán ca cho từng người ở "
            "trang Danh sách người. Ca đêm: giờ ra nhỏ hơn hoặc bằng giờ vào."
        )
        att_hint.setStyleSheet("font-size: 11px;")
        att_hint.setWordWrap(True)
        att_layout.addWidget(att_hint)
        layout.addWidget(att_card)

        # ---- BẢO MẬT ----
        security_card = QFrame()
        security_card.setObjectName("card")
        security_card.setStyleSheet("border-radius: 10px;")
        sec_layout = QVBoxLayout(security_card)
        sec_layout.setContentsMargins(16, 12, 16, 12)
        sec_layout.setSpacing(8)

        sec_header = QLabel("── BẢO MẬT ──")
        sec_header.setObjectName("sectionTitle")
        sec_layout.addWidget(sec_header)

        pw_row = QHBoxLayout()
        pw_row.addWidget(QLabel("Mật khẩu:"))
        self._pw_status = QLabel()
        self._pw_status.setStyleSheet("font-size: 12px;")
        pw_row.addWidget(self._pw_status)
        pw_row.addStretch(1)

        change_pw_btn = QPushButton("Đổi mật khẩu")
        change_pw_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        change_pw_btn.setObjectName("secondaryBtn")
        change_pw_btn.setStyleSheet("border-radius: 5px; padding: 5px 14px; font-size: 12px;")
        change_pw_btn.clicked.connect(self._on_change_password)
        pw_row.addWidget(change_pw_btn)

        sec_layout.addLayout(pw_row)

        # Câu hỏi bảo mật + nút đổi (FR-5 — cần xác thực mật khẩu cũ)
        sq_row = QHBoxLayout()
        sq_row.addWidget(QLabel("Câu hỏi bảo mật:"))
        self._sq_status = QLabel()
        self._sq_status.setStyleSheet("font-size: 12px;")
        sq_row.addWidget(self._sq_status)
        sq_row.addStretch(1)

        change_sq_btn = QPushButton("Đổi câu hỏi bảo mật")
        change_sq_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        change_sq_btn.setObjectName("secondaryBtn")
        change_sq_btn.setStyleSheet("border-radius: 5px; padding: 5px 14px; font-size: 12px;")
        change_sq_btn.clicked.connect(self._on_change_security_questions)
        sq_row.addWidget(change_sq_btn)
        sec_layout.addLayout(sq_row)

        # Tự khóa khi không dùng (FR-7): Tắt / 1 / 5 / 15 phút (mặc định Tắt)
        idle_row = QHBoxLayout()
        idle_row.addWidget(QLabel("Tự khóa khi không dùng:"))
        self._idle_combo = QComboBox()
        for minutes in (0, 1, 5, 15):
            label = "Tắt" if minutes == 0 else (
                "1 phút" if minutes == 1 else f"{minutes} phút"
            )
            self._idle_combo.addItem(label, minutes)
        idle_row.addWidget(self._idle_combo)
        idle_row.addStretch(1)
        sec_layout.addLayout(idle_row)

        layout.addWidget(security_card)

        # ---- ĐỒNG BỘ CLOUD (Bước 15) ----
        cloud_card = QFrame()
        cloud_card.setObjectName("card")
        cloud_card.setStyleSheet("border-radius: 10px;")
        cloud_layout = QVBoxLayout(cloud_card)
        cloud_layout.setContentsMargins(16, 12, 16, 12)
        cloud_layout.setSpacing(8)

        cloud_header = QLabel("── ĐỒNG BỘ CLOUD (Cloudflare D1) ──")
        cloud_header.setObjectName("sectionTitle")
        cloud_layout.addWidget(cloud_header)

        sync_row = QHBoxLayout()
        sync_row.addWidget(QLabel("Đồng bộ cloud:"))
        self._sync_check = QCheckBox("Bật")
        self._sync_check.stateChanged.connect(self._on_sync_toggled)
        sync_row.addWidget(self._sync_check)
        sync_row.addStretch(1)
        cloud_layout.addLayout(sync_row)

        # 3 thông tin kết nối D1 (hiện/ẩn theo trạng thái checkbox)
        self._cloud_fields = QGridLayout()
        self._cloud_fields.setContentsMargins(0, 0, 0, 0)
        self._cloud_fields.setSpacing(6)

        self._account_edit = QLineEdit()
        self._account_edit.setPlaceholderText("dash.cloudflare.com → trang tổng quan (32 ký tự)")
        self._cloud_fields.addWidget(QLabel("Account ID:"), 0, 0)
        self._cloud_fields.addWidget(self._account_edit, 0, 1)

        self._database_edit = QLineEdit()
        self._database_edit.setPlaceholderText("D1 → tên database → ID (UUID)")
        self._cloud_fields.addWidget(QLabel("Database ID:"), 1, 0)
        self._cloud_fields.addWidget(self._database_edit, 1, 1)

        self._token_edit = QLineEdit()
        self._token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_edit.setPlaceholderText("API token (quyền Account · D1 · Edit)")
        self._cloud_fields.addWidget(QLabel("API Token:"), 2, 0)
        self._cloud_fields.addWidget(self._token_edit, 2, 1)

        # Thông tin Supabase Storage (Bước 16) — lưu ảnh snapshot/thumbnail
        # (S3-compatible qua boto3; free tier KHÔNG cần thẻ tín dụng)
        sb_title = QLabel("── LƯU ẢNH (Supabase Storage) ──")
        sb_title.setStyleSheet("font-size: 12px; font-weight: bold;")
        self._cloud_fields.addWidget(sb_title, 3, 0, 1, 2)

        self._sb_endpoint_edit = QLineEdit()
        self._sb_endpoint_edit.setPlaceholderText("https://<ref>.supabase.co/storage/v1/s3")
        self._cloud_fields.addWidget(QLabel("Endpoint S3:"), 4, 0)
        self._cloud_fields.addWidget(self._sb_endpoint_edit, 4, 1)

        self._sb_region_edit = QLineEdit()
        self._sb_region_edit.setPlaceholderText("Region (trang Storage → S3 Access Keys)")
        self._cloud_fields.addWidget(QLabel("Region:"), 5, 0)
        self._cloud_fields.addWidget(self._sb_region_edit, 5, 1)

        self._sb_bucket_edit = QLineEdit()
        self._sb_bucket_edit.setPlaceholderText("Tên bucket (Storage → New bucket)")
        self._cloud_fields.addWidget(QLabel("Bucket:"), 6, 0)
        self._cloud_fields.addWidget(self._sb_bucket_edit, 6, 1)

        self._sb_key_edit = QLineEdit()
        self._sb_key_edit.setPlaceholderText("Access Key ID (S3 Access Keys → Create new)")
        self._cloud_fields.addWidget(QLabel("Access Key:"), 7, 0)
        self._cloud_fields.addWidget(self._sb_key_edit, 7, 1)

        self._sb_secret_edit = QLineEdit()
        self._sb_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._sb_secret_edit.setPlaceholderText("Secret Access Key (chỉ hiện 1 lần khi tạo)")
        self._cloud_fields.addWidget(QLabel("Secret Key:"), 8, 0)
        self._cloud_fields.addWidget(self._sb_secret_edit, 8, 1)
        cloud_layout.addLayout(self._cloud_fields)

        cloud_btn_row = QHBoxLayout()
        self._cloud_test_btn = QPushButton("Kết nối thử")
        self._cloud_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cloud_test_btn.setObjectName("secondaryBtn")
        self._cloud_test_btn.setStyleSheet("border-radius: 5px; padding: 5px 14px; font-size: 12px;")
        self._cloud_test_btn.clicked.connect(self._on_test_cloud)
        cloud_btn_row.addWidget(self._cloud_test_btn)

        self._sync_now_btn = QPushButton("Đồng bộ ngay")
        self._sync_now_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_now_btn.setObjectName("primaryBtn")
        self._sync_now_btn.setStyleSheet("border-radius: 6px; padding: 6px 16px; font-size: 12px;")
        self._sync_now_btn.clicked.connect(self._on_sync_now)
        cloud_btn_row.addWidget(self._sync_now_btn)
        cloud_btn_row.addStretch(1)
        cloud_layout.addLayout(cloud_btn_row)

        self._sync_status = QLabel("Chưa kết nối. Nhập 3 thông tin cloud rồi bấm [Kết nối thử].")
        self._sync_status.setStyleSheet("font-size: 12px;")
        self._sync_status.setWordWrap(True)
        cloud_layout.addWidget(self._sync_status)

        hint = QLabel(
            "Hướng dẫn D1: dash.cloudflare.com → D1 SQL database → Create database → "
            "lấy Database ID; My Profile → API Tokens → Create Token (quyền Account · D1 · Edit).\n"
            "Hướng dẫn lưu ảnh (Supabase, free — không cần thẻ): supabase.com → New project → "
            "Storage → Settings → bật 'S3 protocol' + tạo S3 Access Keys (copy Access Key ID + "
            "Secret, secret chỉ hiện 1 lần) → Storage → New bucket (đặt tên). "
            "Ứng dụng tự tạo bảng ở lần sync đầu."
        )
        hint.setStyleSheet("font-size: 11px;")
        hint.setWordWrap(True)
        cloud_layout.addWidget(hint)
        layout.addWidget(cloud_card)

        # ---- Hành động ----
        action_row = QHBoxLayout()
        action_row.addStretch(1)

        reset_btn = QPushButton("Khôi phục mặc định")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setObjectName("secondaryBtn")
        reset_btn.setStyleSheet("border-radius: 5px; padding: 7px 14px;")
        reset_btn.clicked.connect(self._on_reset_defaults)
        action_row.addWidget(reset_btn)

        save_btn = QPushButton("Lưu thay đổi")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setObjectName("primaryBtn")
        save_btn.setStyleSheet("border-radius: 6px; padding: 8px 20px; font-size: 13px;")
        save_btn.clicked.connect(self._on_save)
        action_row.addWidget(save_btn)

        layout.addLayout(action_row)

        note = QLabel("⚠ Thay đổi cài đặt nhạy cảm (mật khẩu, đồng bộ cloud) yêu cầu mật khẩu")
        note.setStyleSheet("font-size: 12px; color: #b06a00;")  # hổ phách — đọc được cả 2 theme
        layout.addWidget(note)

        layout.addStretch(1)

    # ---------------------------------------------------------
    # Đồng bộ widget <-> config
    # ---------------------------------------------------------
    def _load_from_config(self) -> None:
        """Đổ giá trị config.json vào các ô (gọi khi khởi tạo trang)."""
        self._camera_combo.setCurrentText(f"CAM {self._config.camera_index}")
        for i in range(self._res_combo.count()):
            w, h = self._res_combo.itemData(i)
            if (w, h) == (self._config.camera_width, self._config.camera_height):
                self._res_combo.setCurrentIndex(i)
                break
        else:
            self._res_combo.setCurrentIndex(1)  # mặc định 1280×720
        self._threshold_slider.setValue(round(self._config.recognition_threshold * 100))
        self._update_threshold_label()
        self._spoof_check.setChecked(self._config.anti_spoofing_enabled)
        self._clahe_check.setChecked(self._config.clahe_enabled)
        self._smoothing_slider.setValue(self._config.smoothing_window)
        self._update_smoothing_label()

        # Ngày làm việc (attendance-spec FR-6) + đơn giá lương thô
        workdays = set(self._config.attendance_workdays or [0, 1, 2, 3, 4])
        for i, cb in enumerate(self._workday_checks):
            cb.setChecked(i in workdays)
        self._pay_rate_spin.setValue(self._config.attendance_pay_rate)

        self._sync_check.blockSignals(True)
        self._sync_check.setChecked(self._config.sync_enabled)
        self._sync_check.blockSignals(False)

        # Thông tin cloud (Bước 15) + Supabase Storage (Bước 16)
        self._account_edit.setText(self._config.cloud_account_id)
        self._database_edit.setText(self._config.cloud_database_id)
        self._token_edit.setText(self._config.cloud_api_token)
        self._sb_endpoint_edit.setText(self._config.sb_endpoint)
        self._sb_region_edit.setText(self._config.sb_region)
        self._sb_bucket_edit.setText(self._config.sb_bucket)
        self._sb_key_edit.setText(self._config.sb_access_key_id)
        self._sb_secret_edit.setText(self._config.sb_secret_access_key)
        self._update_cloud_enabled()

        if self._auth.has_password:
            self._pw_status.setText("✓ Đã đặt mật khẩu")
        else:
            self._pw_status.setText("Chưa đặt mật khẩu")

        # Tự khóa khi không dùng (FR-7) + trạng thái câu hỏi bảo mật
        idle_index = self._idle_combo.findData(self._config.idle_lock_minutes)
        self._idle_combo.setCurrentIndex(idle_index if idle_index >= 0 else 0)
        if self._auth.has_security_questions:
            self._sq_status.setText("✓ Đã thiết lập 2 câu hỏi")
        else:
            self._sq_status.setText("Chưa thiết lập — không khôi phục được khi quên mật khẩu")

    def _update_threshold_label(self) -> None:
        self._threshold_label.setText(f"{self._threshold_slider.value() / 100:.2f}")

    @staticmethod
    def _camera_index_from_text(text: str) -> int:
        """'CAM 3' hoặc '3' → 3; chuỗi lạ → 0."""
        digits = "".join(ch for ch in text if ch.isdigit())
        return int(digits) if digits else 0

    # ---------------------------------------------------------
    # Hành động
    # ---------------------------------------------------------
    def _on_threshold_changed(self, _value: int) -> None:
        self._update_threshold_label()

    def _on_smoothing_changed(self, _value: int) -> None:
        self._update_smoothing_label()

    def _update_smoothing_label(self) -> None:
        v = self._smoothing_slider.value()
        if v == 0:
            self._smoothing_label.setText("Tắt")
        else:
            self._smoothing_label.setText(f"{v} khung")

    def _on_test_camera(self) -> None:
        """[Kết nối thử]: đo FPS thật bằng CameraCapture trong QThread."""
        if self._probe_thread is not None and self._probe_thread.isRunning():
            return  # đang kiểm tra rồi — không mở camera song song
        index = self._camera_index_from_text(self._camera_combo.currentText())
        w, h = self._res_combo.currentData()

        self._test_btn.setEnabled(False)
        self._camera_status.setText("Đang kiểm tra camera...")

        self._probe_thread = QThread(self)
        self._probe_worker = CameraProbeWorker(index, w, h)
        self._probe_worker.moveToThread(self._probe_thread)
        self._probe_thread.started.connect(self._probe_worker.run)
        self._probe_worker.finished.connect(self._on_probe_finished)
        self._probe_thread.start()
        logger.info("Bắt đầu kiểm tra camera (index=%d, %dx%d)", index, w, h)

    @Slot(bool, float, int, int)
    def _on_probe_finished(self, ok: bool, fps: float, width: int, height: int) -> None:
        """Kết quả đo camera → cập nhật nhãn + dọn thread sạch sẽ."""
        if ok:
            self._camera_status.setText(
                f"✓ webcam hoạt động · {fps:.0f} FPS · {width}×{height}"
            )
        else:
            self._camera_status.setText("✗ Không mở được camera — kiểm tra chỉ số/thiết bị")

        # Dừng thread (run() đã xong; quit() tắt event loop) rồi dọn tham chiếu
        thread, self._probe_thread = self._probe_thread, None
        self._probe_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(1500)
            thread.deleteLater()

        self._test_btn.setEnabled(True)

    def _on_change_password(self) -> None:
        """Đổi mật khẩu: xác thực mật khẩu CŨ → nhập mới 2 lần → lưu hash (FR-11)."""
        if not PasswordDialog.require(
            self._auth,
            "Đổi mật khẩu",
            self,
            note="Nhập mật khẩu hiện tại để tiếp tục.",
        ):
            return
        dialog = ChangePasswordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._auth.set_password(dialog.new_password())
        self._pw_status.setText("✓ Đã đặt mật khẩu")
        logger.info("Đã đổi mật khẩu qua Cài đặt")
        QMessageBox.information(self, "Thành công", "Đã đổi mật khẩu mới.")

    def _on_change_security_questions(self) -> None:
        """Đổi câu hỏi bảo mật: xác thực mật khẩu cũ → dialog 2 câu (FR-5)."""
        if not self._auth.has_password:
            QMessageBox.warning(
                self, "Chưa có mật khẩu", "Hãy đặt mật khẩu trước khi tạo câu hỏi bảo mật."
            )
            return
        if not PasswordDialog.require(
            self._auth,
            "Đổi câu hỏi bảo mật",
            self,
            note="Nhập mật khẩu hiện tại để tiếp tục.",
        ):
            return
        dialog = SecurityQuestionsDialog(self._auth, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._sq_status.setText("✓ Đã thiết lập 2 câu hỏi")
        logger.info("Đã đổi câu hỏi bảo mật qua Cài đặt")
        QMessageBox.information(self, "Thành công", "Đã lưu câu hỏi bảo mật mới.")

    def _on_sync_toggled(self, state: int) -> None:
        """Bật/tắt cloud = thay đổi nhạy cảm → cần mật khẩu (FR-11).

        Nếu người dùng hủy xác thực → trả checkbox về trạng thái cũ.
        """
        checked = state == Qt.CheckState.Checked.value
        if not PasswordDialog.require(
            self._auth,
            "Bật đồng bộ cloud" if checked else "Tắt đồng bộ cloud",
            self,
            note="Đây là thay đổi nhạy cảm — cần xác nhận mật khẩu.",
        ):
            # Khôi phục trạng thái cũ mà KHÔNG kích hoạt lại signal
            self._sync_check.blockSignals(True)
            self._sync_check.setChecked(not checked)
            self._sync_check.blockSignals(False)
            return
        logger.info("Thay đổi trạng thái đồng bộ cloud → %s", "bật" if checked else "tắt")
        self._update_cloud_enabled()

    def _update_cloud_enabled(self) -> None:
        """Bật/tắt các ô nhập + nút cloud theo checkbox (tắt → khóa ô)."""
        enabled = self._sync_check.isChecked()
        for edit in (
            self._account_edit,
            self._database_edit,
            self._token_edit,
            self._sb_endpoint_edit,
            self._sb_region_edit,
            self._sb_bucket_edit,
            self._sb_key_edit,
            self._sb_secret_edit,
        ):
            edit.setEnabled(enabled)
        self._cloud_test_btn.setEnabled(enabled)
        self._sync_now_btn.setEnabled(
            enabled and self._sync.is_configured()
        )

    def _run_cloud_worker(self, worker: QObject, on_finished) -> None:
        """Chạy 1 worker (kiểm tra kết nối / đồng bộ) trong QThread riêng.

        Đảm bảo không bao giờ có 2 thread cloud song song (nút bị disable
        khi đang chạy) và luôn dọn thread sạch sau khi xong.
        """
        if self._cloud_thread is not None and self._cloud_thread.isRunning():
            return
        self._cloud_thread = QThread(self)
        self._cloud_worker = worker
        worker.moveToThread(self._cloud_thread)
        self._cloud_thread.started.connect(worker.run)
        worker.finished.connect(on_finished)
        self._cloud_thread.start()

    def _finish_cloud_worker(self) -> None:
        """Dọn thread cloud sau khi worker báo xong (bật lại nút)."""
        thread, self._cloud_thread = self._cloud_thread, None
        self._cloud_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(1500)
            thread.deleteLater()
        self._update_cloud_enabled()

    def _sync_from_fields(self) -> None:
        """Đưa các ô đang nhập vào config (TRONG BỘ NHỚ) để thử/dồng bộ ngay
        không cần bấm [Lưu thay đổi] trước. Ghi đĩa vẫn do nút Lưu."""
        self._config.cloud_account_id = self._account_edit.text().strip()
        self._config.cloud_database_id = self._database_edit.text().strip()
        self._config.cloud_api_token = self._token_edit.text().strip()
        self._config.sb_endpoint = self._sb_endpoint_edit.text().strip()
        self._config.sb_region = self._sb_region_edit.text().strip()
        self._config.sb_bucket = self._sb_bucket_edit.text().strip()
        self._config.sb_access_key_id = self._sb_key_edit.text().strip()
        self._config.sb_secret_access_key = self._sb_secret_edit.text().strip()

    def _on_test_cloud(self) -> None:
        """[Kết nối thử]: SELECT 1 trên D1 với thông tin đang nhập (chưa lưu)."""
        self._sync_from_fields()
        self._sync_status.setText("Đang kiểm tra kết nối...")
        self._cloud_test_btn.setEnabled(False)
        self._sync_now_btn.setEnabled(False)
        self._run_cloud_worker(
            CloudProbeWorker(self._sync),
            self._on_cloud_probe_finished,
        )

    @Slot(bool, str)
    def _on_cloud_probe_finished(self, ok: bool, message: str) -> None:
        self._sync_status.setText(message)
        self._finish_cloud_worker()

    def _on_sync_now(self) -> None:
        """[Đồng bộ ngay]: push + pull toàn bộ (chạy trong QThread)."""
        self._sync_from_fields()
        self._sync_status.setText("Đang đồng bộ... (lần đầu có thể lâu hơn)")
        self._sync_now_btn.setEnabled(False)
        self._cloud_test_btn.setEnabled(False)
        self._run_cloud_worker(SyncWorker(self._sync), self._on_sync_finished)

    @Slot(bool, str)
    def _on_sync_finished(self, ok: bool, summary: str) -> None:
        self._sync_status.setText(summary)
        self._finish_cloud_worker()
        if ok:
            logger.info("Đồng bộ từ Cài đặt: %s", summary)

    def set_sync_status(self, text: str) -> None:
        """Cập nhật nhãn trạng thái đồng bộ (MainWindow gọi sau auto-sync)."""
        self._sync_status.setText(text)

    # ---------------------------------------------------------
    # CHẤM CÔNG — CRUD ca + ca mặc định (attendance-spec FR-3/FR-6)
    # ---------------------------------------------------------
    def _refresh_shifts(self) -> None:
        """Đọc lại danh sách ca từ DB → nhãn + combo ca mặc định."""
        from app.services.attendance import KEY_DEFAULT_SHIFT

        self._shifts = self._attendance.list_shifts()
        if self._shifts:
            self._shifts_label.setText(
                "Ca làm việc: "
                + ";  ".join(
                    f"{s.name} ({s.start_time}–{s.end_time})" for s in self._shifts
                )
            )
        else:
            self._shifts_label.setText("Ca làm việc: chưa có ca nào")

        # Combo ca mặc định: giữ lựa chọn cũ nếu ca vẫn còn
        default_id = self._attendance._get_setting(KEY_DEFAULT_SHIFT)
        self._default_shift_combo.blockSignals(True)
        self._default_shift_combo.clear()
        for i, shift in enumerate(self._shifts):
            self._default_shift_combo.addItem(
                f"{shift.name} ({shift.start_time}–{shift.end_time})", shift.id
            )
        if default_id:
            index = self._default_shift_combo.findData(default_id)
            if index >= 0:
                self._default_shift_combo.setCurrentIndex(index)
        self._default_shift_combo.blockSignals(False)

    def _on_add_shift(self) -> None:
        """Thêm ca mới qua ShiftDialog (tên/giờ/dung sai — validate trong service)."""
        dialog = ShiftDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._attendance.create_shift(
                dialog.name(), dialog.start(), dialog.end(), dialog.grace(),
                dialog.factor(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Không thêm được ca", str(exc))
            return
        logger.info("Đã thêm ca '%s' qua Cài đặt", dialog.name())
        self._refresh_shifts()

    def _on_edit_shift(self) -> None:
        """Sửa ca đang chọn trong combo ca mặc định (danh sách ca ngắn — chọn
        qua combo đơn giản hơn bảng)."""
        shift_id = self._default_shift_combo.currentData()
        if shift_id is None:
            QMessageBox.information(self, "Sửa ca", "Chưa có ca nào để sửa.")
            return
        shift = next(s for s in self._shifts if s.id == shift_id)
        dialog = ShiftDialog(self, shift=shift)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._attendance.update_shift(
                shift_id, dialog.name(), dialog.start(), dialog.end(),
                dialog.grace(), dialog.factor(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Không sửa được ca", str(exc))
            return
        self._refresh_shifts()

    def _on_delete_shift(self) -> None:
        """Xóa ca đang chọn — xác nhận; người gán ca này về ca mặc định (FR-3)."""
        shift_id = self._default_shift_combo.currentData()
        if shift_id is None:
            QMessageBox.information(self, "Xóa ca", "Chưa có ca nào để xóa.")
            return
        shift = next(s for s in self._shifts if s.id == shift_id)
        answer = QMessageBox.question(
            self,
            "Xác nhận xóa ca",
            f"Xóa ca '{shift.name}'?\n\nNgười đang gán ca này sẽ chuyển về ca mặc định. "
            "Ngày công đã tính không đổi.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._attendance.delete_shift(shift_id)
        except ValueError as exc:
            QMessageBox.warning(self, "Không xóa được", str(exc))
            return
        logger.info("Đã xóa ca '%s' qua Cài đặt", shift.name)
        self._refresh_shifts()

    def _on_save_attendance(self) -> None:
        """Lưu ca mặc định (settings DB) + ngày làm việc + đơn giá (config.json)."""
        shift_id = self._default_shift_combo.currentData()
        if shift_id:
            self._attendance.set_default_shift_id(shift_id)
        self._config.attendance_workdays = [
            i for i, cb in enumerate(self._workday_checks) if cb.isChecked()
        ]
        self._config.attendance_pay_rate = self._pay_rate_spin.value()
        self._config.save()
        logger.info(
            "Đã lưu cài đặt chấm công: ca mặc định=%s, ngày làm việc=%s, đơn giá=%s",
            shift_id,
            self._config.attendance_workdays,
            self._config.attendance_pay_rate,
        )
        QMessageBox.information(self, "Đã lưu", "Đã lưu ca mặc định + ngày làm việc.")

    def _on_reset_defaults(self) -> None:
        """Khôi phục mặc định: đưa các ô về giá trị khởi tạo (chưa lưu)."""
        defaults = Config()
        self._camera_combo.setCurrentText(f"CAM {defaults.camera_index}")
        for i in range(self._res_combo.count()):
            w, h = self._res_combo.itemData(i)
            if (w, h) == (defaults.camera_width, defaults.camera_height):
                self._res_combo.setCurrentIndex(i)
                break
        self._threshold_slider.setValue(round(defaults.recognition_threshold * 100))
        self._spoof_check.setChecked(defaults.anti_spoofing_enabled)
        self._clahe_check.setChecked(defaults.clahe_enabled)
        self._smoothing_slider.setValue(defaults.smoothing_window)
        self._update_smoothing_label()
        defaults_idle = self._idle_combo.findData(defaults.idle_lock_minutes)
        self._idle_combo.setCurrentIndex(defaults_idle if defaults_idle >= 0 else 0)
        self._sync_check.blockSignals(True)
        self._sync_check.setChecked(defaults.sync_enabled)
        self._sync_check.blockSignals(False)
        self._account_edit.clear()
        self._database_edit.clear()
        self._token_edit.clear()
        self._sb_endpoint_edit.clear()
        self._sb_region_edit.clear()
        self._sb_bucket_edit.clear()
        self._sb_key_edit.clear()
        self._sb_secret_edit.clear()
        self._update_cloud_enabled()
        self._camera_status.setText("(chưa kiểm tra)")
        self._sync_status.setText("Chưa kết nối. Nhập thông tin cloud rồi bấm [Kết nối thử].")
        logger.info("Đã khôi phục cài đặt về mặc định (chưa lưu)")

    def _on_save(self) -> None:
        """[Lưu thay đổi]: ghi config.json + thông báo để dừng camera."""
        self._config.camera_index = self._camera_index_from_text(
            self._camera_combo.currentText()
        )
        w, h = self._res_combo.currentData()
        self._config.camera_width = w
        self._config.camera_height = h
        self._config.recognition_threshold = self._threshold_slider.value() / 100
        self._config.anti_spoofing_enabled = self._spoof_check.isChecked()
        self._config.clahe_enabled = self._clahe_check.isChecked()
        self._config.smoothing_window = self._smoothing_slider.value()
        # Tự khóa khi không dùng (FR-7) — data của combo là số phút (0 = tắt)
        self._config.idle_lock_minutes = self._idle_combo.currentData() or 0
        self._config.sync_enabled = self._sync_check.isChecked()
        # Thông tin cloud (Bước 15) + R2 (Bước 16)
        self._config.cloud_account_id = self._account_edit.text().strip()
        self._config.cloud_database_id = self._database_edit.text().strip()
        self._config.cloud_api_token = self._token_edit.text().strip()
        self._config.sb_endpoint = self._sb_endpoint_edit.text().strip()
        self._config.sb_region = self._sb_region_edit.text().strip()
        self._config.sb_bucket = self._sb_bucket_edit.text().strip()
        self._config.sb_access_key_id = self._sb_key_edit.text().strip()
        self._config.sb_secret_access_key = self._sb_secret_edit.text().strip()
        self._config.save()
        self._update_cloud_enabled()

        # Camera đọc index/ngưỡng lúc start_camera → dừng để lần mở sau
        # (showEvent của CameraView) dùng đúng cấu hình mới.
        self.settings_saved.emit()
        logger.info(
            "Đã lưu cài đặt: camera=%d, %dx%d, ngưỡng=%.2f, sync=%s",
            self._config.camera_index,
            self._config.camera_width,
            self._config.camera_height,
            self._config.recognition_threshold,
            self._config.sync_enabled,
        )
        QMessageBox.information(self, "Đã lưu", "Đã lưu thay đổi cài đặt.")

    # ---------------------------------------------------------
    # Vòng đời widget
    # ---------------------------------------------------------
    def stop_probe(self) -> None:
        """Dừng thread đo camera + thread cloud nếu còn chạy (gọi khi đóng app
        — tránh cảnh báo 'QThread: Destroyed while thread is still running')."""
        if self._probe_thread is not None and self._probe_thread.isRunning():
            self._probe_thread.quit()
            self._probe_thread.wait(1500)
            self._probe_thread = None
            self._probe_worker = None
        if self._cloud_thread is not None and self._cloud_thread.isRunning():
            self._cloud_thread.quit()
            self._cloud_thread.wait(1500)
            self._cloud_thread = None
            self._cloud_worker = None


class ShiftDialog(QDialog):
    """Hộp thoại thêm/sửa ca làm việc (attendance-spec FR-3).

    ``shift=None`` → thêm mới; truyền Shift → điền sẵn giá trị cũ (sửa).
    Validate HH:MM do ShiftRepository/AttendanceService đảm nhận — lỗi
    ValueError hiển thị qua QMessageBox ở caller.
    """

    def __init__(self, parent=None, shift=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("THÊM CA" if shift is None else "SỬA CA")
        self.setModal(True)
        self.setFixedWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._name_edit = QLineEdit(shift.name if shift else "")
        self._name_edit.setPlaceholderText("VD: Hành chính, Ca đêm")
        form.addRow("Tên ca:", self._name_edit)

        self._start_edit = QLineEdit(shift.start_time if shift else "")
        self._start_edit.setPlaceholderText("HH:MM (VD: 08:00)")
        self._start_edit.setMaxLength(5)
        form.addRow("Giờ vào:", self._start_edit)

        self._end_edit = QLineEdit(shift.end_time if shift else "")
        self._end_edit.setPlaceholderText("HH:MM (VD: 17:00; ca đêm 06:00)")
        self._end_edit.setMaxLength(5)
        form.addRow("Giờ ra:", self._end_edit)

        self._grace_spin = QSpinBox()
        self._grace_spin.setRange(0, 120)
        self._grace_spin.setValue(shift.grace_minutes if shift else 10)
        self._grace_spin.setSuffix(" phút")
        self._grace_spin.setToolTip("Trễ trong khoảng này vẫn tính đúng giờ")
        form.addRow("Dung sai trễ:", self._grace_spin)

        # Hệ số lương ca (lương thô = số công × hệ số — schema v3)
        self._factor_spin = QDoubleSpinBox()
        self._factor_spin.setRange(0.1, 10.0)
        self._factor_spin.setDecimals(2)
        self._factor_spin.setSingleStep(0.25)
        self._factor_spin.setValue(shift.factor if shift else 1.0)
        self._factor_spin.setToolTip(
            "Hệ số lương của ca — 1.0 ca thường, 1.5 ca tăng đơn. "
            "Lương tháng = số công mỗi ca × hệ số ca đó."
        )
        form.addRow("Hệ số lương:", self._factor_spin)
        layout.addLayout(form)

        hint = QLabel(
            "Ca đêm: giờ ra nhỏ hơn hoặc bằng giờ vào (VD 22:00 → 06:00). "
            "Sự kiện sau nửa đêm vẫn tính về ngày bắt đầu ca."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 11px;")
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Hủy")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Lưu")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _on_save(self) -> None:
        """Kiểm tra nhanh tên rỗng + định dạng giờ trước khi đóng."""
        from app.infrastructure.repositories import _validate_hhmm

        if not self._name_edit.text().strip():
            QMessageBox.warning(self, "Thiếu thông tin", "Tên ca không được trống.")
            return
        for edit in (self._start_edit, self._end_edit):
            try:
                _validate_hhmm(edit.text())
            except ValueError:
                QMessageBox.warning(
                    self, "Sai định dạng",
                    f"Giờ '{edit.text()}' không hợp lệ — cần dạng HH:MM (00:00–23:59).",
                )
                return
        self.accept()

    # Giá trị đọc sau khi dialog được chấp nhận
    def name(self) -> str:
        return self._name_edit.text().strip()

    def start(self) -> str:
        return self._start_edit.text().strip()

    def end(self) -> str:
        return self._end_edit.text().strip()

    def grace(self) -> int:
        return self._grace_spin.value()

    def factor(self) -> float:
        return self._factor_spin.value()
