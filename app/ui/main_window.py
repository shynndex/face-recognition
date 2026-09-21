"""Cửa sổ chính — màn hình khóa + điều hướng giữa các màn hình (Bước 3).

Cấu trúc: QMainWindow chứa QStackedWidget với 2 tầng:
  1. LockScreen  — luôn hiển thị khi mở app / bấm Khoá / Ctrl+L
  2. Content     — sidebar điều hướng + các màn hình:
       CameraView (Bước 3) · PhotoView (Bước 10) · EnrollmentDialog (Bước 8)
       PersonListView (Bước 7) · HistoryView (Bước 11) · SettingsView (Bước 14)
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QEvent, QThread, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.infrastructure.db import Database
from app.services.auth import AuthService
from app.services.sync import SyncService
from app.ui.attendance_view import AttendanceView
from app.ui.camera_view import CameraView
from app.ui.dashboard_view import DashboardView
from app.ui.enrollment_dialog import EnrollmentDialog
from app.ui.history_view import HistoryView
from app.ui.lock_screen import LockScreen
from app.ui.person_list_view import PersonListView
from app.ui.photo_view import PhotoView
from app.ui.settings_view import SettingsView

logger = logging.getLogger(__name__)

# Danh sách màn hình: (tên hiển thị trên sidebar, tên trang)
# Phong cách Security Console: mục nav đánh số kiểu console, không dùng emoji.
# Dashboard đặt ĐẦU tiên — mở app/mở khóa là thấy tổng quan chấm công hôm nay.
PAGES: list[tuple[str, str]] = [
    ("Tổng quan", "DashboardView"),
    ("Nhận diện", "CameraView"),
    ("Nhận diện ảnh", "PhotoView"),
    ("Đăng ký mới", "EnrollmentDialog"),
    ("Danh sách người", "PersonListView"),
    ("Lịch sử", "HistoryView"),
    ("Chấm công", "AttendanceView"),
    ("Cài đặt", "SettingsView"),
]
# Nhãn rút gọn khi sidebar thu gọn (số thứ tự kiểu console)
_ICONS: list[str] = ["01", "02", "03", "04", "05", "06", "07", "08"]


def _nav_label(title: str, index: int) -> str:
    """Nhãn sidebar mở rộng: '01  Nhận diện' — đánh số mono kiểu console."""
    return f"{index + 1:02d}  {title}"
# Ngưỡng chiều rộng (px) — dưới mức này sidebar thu gọn chỉ còn icon
_SIDEBAR_COLLAPSE_WIDTH = 700
_SIDEBAR_EXPANDED_WIDTH = 200
_SIDEBAR_COLLAPSED_WIDTH = 58


class MainWindow(QMainWindow):
    """Cửa sổ chính: màn hình khóa bao quanh vùng điều hướng nội dung."""

    def __init__(self, auth: AuthService, config: Config) -> None:
        super().__init__()
        self._auth = auth
        self._config = config
        self.setWindowTitle("Nhận Diện Khuôn Mặt · Face Recognition")
        # Cửa sổ khởi động VỪA màn hình (trừ 40px viền). Trước đây cố định
        # 1100x700: trên màn hình nhỏ / scale 125% chiều cao logical chỉ ~660px
        # → cửa sổ cao hơn màn hình, đáy bị cắt và không kéo nhỏ được.
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(
            min(1100, screen.width() - 40),
            min(700, screen.height() - 40),
        )

        self._root = QStackedWidget()
        self._lock_screen = LockScreen(auth)
        self._content = self._build_content()

        self._root.addWidget(self._lock_screen)
        self._root.addWidget(self._content)
        self.setCentralWidget(self._root)

        # Tự khóa khi không dùng (FR-7): QTimer một lần đọc lại idle_lock_minutes
        # mỗi giây; chỉ phím/chuột TRONG cửa sổ reset bộ đếm (đã chốt: camera
        # chạy không reset; khi đang ở LockScreen thì không đếm). 0 = tắt.
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(1000)
        self._idle_timer.timeout.connect(self._check_idle_lock)
        self._last_activity = time.monotonic()
        self._idle_timer.start()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self._lock_screen.unlocked.connect(
            lambda: self._root.setCurrentWidget(self._content)
        )
        # Bật cloud → tự đồng bộ 1 lần khi mở khóa (Bước 15)
        self._lock_screen.unlocked.connect(self._maybe_auto_sync)
        # Nút [Dang ky ngay] tren man hinh webcam -> mo EnrollmentDialog
        # voi anh nguoi la lam mau 1 (phai dung camera truoc, mo lai sau)
        self._camera_view.enroll_requested.connect(self._open_enrollment_from_unknown)
        # Danh sách người thay đổi (xóa/sửa) → reload Matcher nhận diện
        # (tránh ghost match sau khi xóa người — embedding cũ vẫn còn trong bộ nhớ)
        self._person_list_view.person_changed.connect(
            self._camera_view._service.reload
        )

        # Luôn khởi động ở màn hình khóa (lần đầu dùng → đặt mật khẩu mới)
        self._root.setCurrentWidget(self._lock_screen)

        # Phím tắt Ctrl+L khóa ứng dụng
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._lock_now)
        logger.info("Khởi tạo cửa sổ chính hoàn tất (%d màn hình)", len(PAGES))

    # ---------------------------------------------------------
    # Tự khóa khi không dùng (FR-7 — spec quản lý mật khẩu)
    # ---------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 (chuẩn Qt)
        """Bắt phím/chuột trong cửa sổ chính → reset bộ đếm idle (FR-7).

        Chỉ lắng nghe các sự kiện tương tác được chốt (phím, chuột, cuộn,
        cảm ứng); di chuột ĐƠN THUẶN (Hover) không reset. Khi đang hiển thị
        LockScreen thì không đếm nữa (đã khóa rồi).
        """
        if event.type() in (
            QEvent.Type.KeyPress,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.Wheel,
            QEvent.Type.TouchUpdate,
            QEvent.Type.TouchBegin,
        ):
            self._last_activity = time.monotonic()
        return super().eventFilter(obj, event)

    def _check_idle_lock(self) -> None:
        """Hết giờ: quá idle_lock_minutes không tương tác → tự khóa (FR-7).

        Đọc config MỖI giây (rẻ) nên đổi tùy chọn ở Cài đặt có hiệu lực
        ngay mà không cần khởi động lại. 0 = tắt; đang ở LockScreen hoặc
        cửa sổ ẩn (đã thu nhỏ) thì không đếm.
        """
        minutes = self._config.idle_lock_minutes or 0
        if minutes <= 0:
            return
        if self._root.currentWidget() is not self._content:
            return  # đang ở màn hình khóa — không đếm (đã chốt)
        if not self.isVisible():
            return  # cửa sổ ẩn (thu nhỏ) — không khóa ngầm
        idle_seconds = time.monotonic() - self._last_activity
        if idle_seconds >= minutes * 60:
            logger.info(
                "Tự khóa sau %d phút không tương tác (FR-7)", minutes
            )
            self._lock_now()

    # ---------------------------------------------------------
    # Vùng nội dung (sidebar + các trang)
    # ---------------------------------------------------------

    def _build_content(self) -> QWidget:
        """Xây dựng phần nội dung: sidebar điều hướng + các trang."""
        central = QWidget()
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Cột trái: sidebar + nút Khóa
        left = QVBoxLayout()
        left.setSpacing(0)

        # Nhãn thương hiệu kiểu console trên đỉnh sidebar (ẩn khi thu gọn)
        self._brand_label = QLabel("FACE·ID // CONSOLE")
        self._brand_label.setObjectName("sectionTitle")
        self._brand_label.setStyleSheet("font-size: 14px; padding: 4px 2px 10px 2px;")

        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setFixedWidth(_SIDEBAR_EXPANDED_WIDTH)
        for i, (title, _) in enumerate(PAGES):
            item = QListWidgetItem(_nav_label(title, i))
            item.setToolTip(title)  # hiển thị khi sidebar thu gọn
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._switch_page)

        self._lock_btn = QPushButton("KHOÁ")
        self._lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lock_btn.setObjectName("sidebarBtn")
        self._lock_btn.setToolTip("Khoá ứng dụng (Ctrl+L)")
        self._lock_btn.clicked.connect(self._lock_now)

        # Nút S/T — chuyển đổi Dark/Light theme (FR-9, wireframe [S/T])
        self._theme_btn = QPushButton("SÁNG")
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.setObjectName("sidebarBtn")
        self._theme_btn.setToolTip("Chuyển đổi chủ đề Sáng / Tối")
        self._theme_btn.clicked.connect(self._toggle_theme)
        self._update_theme_btn()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_row.addWidget(self._lock_btn, stretch=1)
        btn_row.addWidget(self._theme_btn)

        left.addWidget(self._brand_label)
        left.addWidget(self.nav)
        left.addLayout(btn_row)

        # Vùng nội dung: CameraView thật + các trang placeholder
        self._db = Database()
        self._camera_view = CameraView(self._config, db=self._db)
        self._enrollment_page = self._make_enrollment_page()
        self._person_list_view = PersonListView(
            self._db,
            self._auth,
            enroll_callback=self._open_enrollment,
        )
        self._photo_view = PhotoView(
            self._config,
            self._db,
            detector=self._camera_view.detector,
        )
        self._history_view = HistoryView(self._db, self._auth)
        self._attendance_view = AttendanceView(
            self._db, self._auth, config=self._config
        )
        self._dashboard_view = DashboardView(self._db, config=self._config)
        # Dashboard → nút [ Bảng công tháng ] mở trang Chấm công
        self._dashboard_view.attendance_requested.connect(
            lambda: self._open_page("AttendanceView")
        )
        self._sync_service = SyncService(self._db, self._config)
        self._settings_view = SettingsView(self._config, self._auth, self._sync_service)
        self._settings_view.settings_saved.connect(self._on_settings_saved)
        self.stack = QStackedWidget()
        for _, name in PAGES:
            if name == "CameraView":
                page = self._camera_view
            elif name == "EnrollmentDialog":
                page = self._enrollment_page
            elif name == "PersonListView":
                page = self._person_list_view
            elif name == "PhotoView":
                page = self._photo_view
            elif name == "HistoryView":
                page = self._history_view
            elif name == "AttendanceView":
                page = self._attendance_view
            elif name == "DashboardView":
                page = self._dashboard_view
            elif name == "SettingsView":
                page = self._settings_view
            else:
                page = self._make_placeholder(name)
            self.stack.addWidget(page)

        root_layout.addLayout(left)
        root_layout.addWidget(self.stack, stretch=1)
        return central

    # ---------------------------------------------------------
    # Hàm nội bộ
    # ---------------------------------------------------------

    def _make_placeholder(self, name: str) -> QWidget:
        """Tạo một trang placeholder cho màn hình chưa triển khai."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)

        label = QLabel(f"{name}\n(sẽ triển khai ở các bước tiếp theo)")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 18px;")
        layout.addWidget(label)

        layout.addStretch(1)
        return page

    def _make_enrollment_page(self) -> QWidget:
        """Trang 'Đăng ký mới': nút mở EnrollmentDialog (FR-1)."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)

        title = QLabel("Đăng ký khuôn mặt mới")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel(
            "Nhập tên, sau đó nhìn vào camera và xoay đầu từ từ — "
            "hệ thống tự chụp 5 khung hình tốt nhất và lưu vào cơ sở dữ liệu."
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        open_btn = QPushButton("＋ Đăng ký khuôn mặt")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(self._open_enrollment)
        layout.addWidget(open_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)
        return page

    def _open_enrollment(self) -> None:
        """Mở EnrollmentDialog — tái sử dụng model đã nạp của CameraView nếu có.

        QUAN TRỌNG: dừng hẳn camera của CameraView TRƯỚC khi mở dialog —
        tránh 2 luồng cùng mở 1 webcam (gây lỗi MSMF "OnReadSample failed").
        """
        self._camera_view.stop_camera()
        dialog = EnrollmentDialog(
            self._config,
            self._db,
            detector=self._camera_view.detector,
            parent=self,
        )
        dialog.exec()
        # Camera của CameraView sẽ tự mở lại khi quay về trang webcam (showEvent)

    def _open_enrollment_from_unknown(
        self, crop, embedding, quality: float
    ) -> None:
        """[Dang ky ngay]: anh crop nguoi la lam mau 1 cua EnrollmentDialog.

        Camera dang chay tren trang webcam -> dung truoc (tranh 2 luong cung
        mo webcam), mo dialog, roi mo lai camera khi dong.
        """
        from app.services.enrollment import CapturedSample

        self._camera_view.stop_camera()
        seed = CapturedSample(
            embedding=embedding,
            quality=quality,
            face_crop=crop,
        )
        dialog = EnrollmentDialog(
            self._config,
            self._db,
            detector=self._camera_view.detector,
            seed_sample=seed,
            parent=self,
        )
        dialog.exec()
        # Van dang o trang webcam (dialog modal khong lam page an) -> mo lai tu dong
        self._camera_view.start_camera()

    def _switch_page(self, index: int) -> None:
        """Chuyển màn hình khi người dùng chọn trong sidebar."""
        if 0 <= index < self.stack.count():
            self.stack.setCurrentIndex(index)
            logger.debug("Chuyển đến màn hình: %s", PAGES[index][0])

    def _open_page(self, page_name: str) -> None:
        """Mở trang theo tên PAGES (dùng cho tín hiệu giữa các trang)."""
        for index, (_, name) in enumerate(PAGES):
            if name == page_name:
                self.nav.setCurrentRow(index)  # _switch_page tự chạy theo
                return
        logger.warning("Không tìm thấy trang: %s", page_name)

    def _toggle_theme(self) -> None:
        """Chuyển đổi Dark/Light theme (FR-9) — áp ngay + lưu vào config.json."""
        from PySide6.QtWidgets import QApplication

        from app.ui.theme import apply_theme

        new_theme = "light" if self._config.theme == "dark" else "dark"
        self._config.theme = new_theme
        apply_theme(QApplication.instance(), new_theme)
        self._config.save()
        self._update_theme_btn()
        logger.info("Đã chuyển theme → %s", new_theme)

    def _update_theme_btn(self) -> None:
        """Nhãn nút S/T: hiển thị theme sẽ chuyển tới (ngược với theme hiện tại)."""
        self._theme_btn.setText("TỐI" if self._config.theme == "light" else "SÁNG")

    def _maybe_auto_sync(self) -> None:
        """Tự đồng bộ khi mở khóa nếu đã bật cloud + đủ thông tin (Bước 15)."""
        if not self._config.sync_enabled or not self._sync_service.is_configured():
            return
        if getattr(self, "_auto_sync_thread", None) is not None \
                and self._auto_sync_thread.isRunning():
            return  # đang đồng bộ rồi — không chạy song song
        from app.ui.settings_view import SyncWorker

        self._auto_sync_thread = QThread(self)
        worker = SyncWorker(self._sync_service)
        worker.moveToThread(self._auto_sync_thread)
        self._auto_sync_thread.started.connect(worker.run)
        worker.finished.connect(self._on_auto_sync_finished)
        self._auto_sync_thread.start()
        logger.info("Tự động đồng bộ cloud sau khi mở khóa")

    def _on_auto_sync_finished(self, ok: bool, summary: str) -> None:
        """Kết quả tự đồng bộ → cập nhật trạng thái ở trang Cài đặt + log."""
        thread, self._auto_sync_thread = self._auto_sync_thread, None
        if thread is not None:
            thread.quit()
            thread.wait(1500)
            thread.deleteLater()
        self._settings_view.set_sync_status(summary)
        logger.info("Tự đồng bộ hoàn tất (%s): %s", "OK" if ok else "LỖI", summary)

    def _on_settings_saved(self) -> None:
        """Cài đặt vừa lưu: dừng camera để lần mở sau dùng cấu hình mới.

        CameraView đọc camera_index/ngưỡng tại thời điểm start_camera —
        nếu camera đang chạy với cấu hình cũ thì phải dừng (trang Cài đặt
        vừa lưu cấu hình mới, quay lại webcam sẽ mở đúng).
        """
        self._camera_view.stop_camera()
        # Vừa bật cloud trong Cài đặt → đồng bộ ngay (không chờ lần mở sau)
        self._maybe_auto_sync()

    def _lock_now(self) -> None:
        """Khóa ứng dụng: dừng camera (riêng tư) + về màn hình khóa."""
        self._camera_view.stop_camera()
        self._root.setCurrentWidget(self._lock_screen)
        self._lock_screen.reset()
        logger.info("Đã khóa ứng dụng")

    # ---------------------------------------------------------
    # Responsive sidebar — tự thu gọn khi cửa sổ nhỏ
    # ---------------------------------------------------------
    def _update_sidebar_mode(self) -> None:
        """Thu gọn sidebar (chỉ số thứ tự) khi cửa sổ nhỏ, mở rộng khi to.

        Ngưỡng: _SIDEBAR_COLLAPSE_WIDTH (700px). Khi thu gọn:
          - nav chỉ hiển thị số thứ tự kiểu console (01…06)
          - nút Khoá / theme rút gọn chữ, ẩn nhãn thương hiệu
        Khi mở rộng:
          - nav hiển thị '01  Tên màn hình'
          - nút về kích thước bình thường
        """
        w = self.width()
        collapsed = w < _SIDEBAR_COLLAPSE_WIDTH
        if collapsed == getattr(self, "_sidebar_collapsed", None):
            return  # chưa thay đổi — bỏ qua
        self._sidebar_collapsed = collapsed
        if collapsed:
            self.nav.setFixedWidth(_SIDEBAR_COLLAPSED_WIDTH)
            for i, short in enumerate(_ICONS):
                if i < self.nav.count():
                    self.nav.item(i).setText(short)
            self._brand_label.setVisible(False)
            self._lock_btn.setText("KHOÁ")
            self._theme_btn.setText(
                "TỐI" if self._config.theme == "light" else "SÁNG"
            )
        else:
            self.nav.setFixedWidth(_SIDEBAR_EXPANDED_WIDTH)
            for i, (title, _) in enumerate(PAGES):
                if i < self.nav.count():
                    self.nav.item(i).setText(_nav_label(title, i))
            self._brand_label.setVisible(True)
            self._lock_btn.setText("KHOÁ")
            self._update_theme_btn()

    def resizeEvent(self, event) -> None:  # noqa: N802 (chuẩn Qt)
        """Phát hiện thay đổi kích thước → cập nhật sidebar."""
        super().resizeEvent(event)
        self._update_sidebar_mode()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (chuẩn Qt)
        """Đóng app: dừng camera + thread đồng bộ và giải phóng tài nguyên."""
        self._camera_view.stop_camera()
        self._settings_view.stop_probe()
        self._idle_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        auto = getattr(self, "_auto_sync_thread", None)
        if auto is not None and auto.isRunning():
            auto.quit()
            auto.wait(1500)
        super().closeEvent(event)
