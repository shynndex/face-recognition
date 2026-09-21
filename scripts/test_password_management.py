"""Kiểm tra phần còn thiếu của spec password-management-spec.md (2026-09-03):

  - FR-7  Tự khóa khi không dùng: combo Tắt/1/5/15 phút ở Cài đặt → BẢO MẬT
          (lưu config.idle_lock_minutes); MainWindow tự khóa sau thời gian
          quy định, chỉ phím/chuột TRONG cửa sổ reset bộ đếm (camera chạy
          không reset — đã chốt), đang ở LockScreen thì không đếm.
  - FR-5  [Đổi câu hỏi bảo mật] ở Cài đặt: xác thực mật khẩu cũ (Password-
          Dialog) → SecurityQuestionsDialog 2 câu → lưu hash argon2 mới.

Chạy:  .venv\\Scripts\\python.exe scripts\\test_password_management.py

QUAN TRỌNG: test dùng file config TẠM (data/test_config_pw_mgmt.json) —
KHÔNG đụng config.json thật (trỏ CONFIG_PATH sang file tạm, an toàn).
"""
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Console Windows (cp1252) không in được tiếng Việt — ép stdout/stderr UTF-8
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import PySide6  # noqa: E402

# Khắc phục lỗi đường dẫn plugins khi project path có dấu cách (giống main.py)
pyside_dir = os.path.dirname(PySide6.__file__)
os.environ["QT_PLUGIN_PATH"] = os.path.join(pyside_dir, "plugins")
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(pyside_dir)
os.environ["QT_QPA_PLATFORM"] = "minimal"  # chế độ ảo — không mở cửa sổ thật

from PySide6.QtCore import QEvent, QObject  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.config as config_module  # noqa: E402
from app.config import Config  # noqa: E402
from app.services.auth import AuthService  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.settings_view import SecurityQuestionsDialog, SettingsView  # noqa: E402

# File config TẠM — trỏ CONFIG_PATH sang đây (Config.save/load tìm lúc GỌI)
TEMP_CONFIG = PROJECT_ROOT / "data" / "test_config_pw_mgmt.json"
TEMP_CONFIG.unlink(missing_ok=True)
config_module.CONFIG_PATH = TEMP_CONFIG

passed = True

try:
    app = QApplication.instance() or QApplication(sys.argv)
    config = Config(camera_index=99, camera_width=320, camera_height=240)
    auth = AuthService(config)

    window = MainWindow(auth, config)
    window.show()
    app.processEvents()

    # ── [1] Đặt mật khẩu + câu hỏi qua LockScreen (tiền đề cho các phần sau)
    lock = window._lock_screen
    lock._pw1.setText("Mat-khau-123")
    lock._pw2.setText("Mat-khau-123")
    lock._q1_edit.setText("Quê quán của bạn?")
    lock._a1_edit.setText("Hà Nội")
    lock._q2_edit.setText("Tên thú cưng đầu tiên?")
    lock._a2_edit.setText("Miu")
    lock._on_submit()
    assert auth.has_password, "Phải đặt được mật khẩu"
    assert auth.has_security_questions, "Phải đặt được 2 câu hỏi bảo mật"
    assert window._root.currentWidget() is window._content, "Phải vào nội dung"
    print("[1] Tiền đề: đặt mật khẩu + câu hỏi bảo mật lần đầu: OK")

    # ── [2] FR-7: combo tự khóa ở trang Cài đặt — đúng 4 lựa chọn + load config
    settings = window._settings_view
    idle_combo = settings._idle_combo
    data = [idle_combo.itemData(i) for i in range(idle_combo.count())]
    assert data == [0, 1, 5, 15], f"Combo phải có Tắt/1/5/15 phút (thực tế: {data})"
    assert idle_combo.currentData() == 0, "Mặc định phải là Tắt"
    # _on_save kết thúc bằng QMessageBox.information (modal — treo test ở nền
    # minimal) → thay bằng no-op trong lúc test, xong trả lại như cũ.
    import app.ui.settings_view as sv

    class FakeBox:
        @staticmethod
        def information(*_args, **_kwargs):
            pass

        @staticmethod
        def warning(*_args, **_kwargs):
            pass

    orig_box = sv.QMessageBox
    sv.QMessageBox = FakeBox
    try:
        # Đổi lựa chọn → Lưu thay đổi → config.idle_lock_minutes phải cập nhật
        index_5 = idle_combo.findData(5)
        idle_combo.setCurrentIndex(index_5)
        settings._on_save()
        assert config.idle_lock_minutes == 5, (
            f"Lưu xong config phải là 5 phút (thực tế: {config.idle_lock_minutes})"
        )
        saved = Config.load(TEMP_CONFIG)
        assert saved.idle_lock_minutes == 5, (
            "idle_lock_minutes phải ghi vào config.json"
        )
        # Load lại trang → combo phản ánh đúng config
        settings._load_from_config()
        assert idle_combo.currentData() == 5, "Combo phải load đúng giá trị config"
        # Khôi phục về Tắt rồi Lưu → về 0
        idle_combo.setCurrentIndex(idle_combo.findData(0))
        settings._on_save()
        assert config.idle_lock_minutes == 0
    finally:
        sv.QMessageBox = orig_box
    print("[2] FR-7 combo tự khóa (Tắt/1/5/15) + lưu/load config: OK")

    # ── [3] FR-7: QTimer tự khóa sau thời gian không tương tác
    config.idle_lock_minutes = 0
    window._last_activity = time.monotonic() - 10 * 60  # giả lập 10 phút trước
    window._check_idle_lock()
    assert window._root.currentWidget() is window._content, "idle=0 (Tắt) không được tự khóa"
    # Bật 5 phút → cùng trạng thái idle 10 phút → phải tự khóa
    config.idle_lock_minutes = 5
    window._check_idle_lock()
    assert window._root.currentWidget() is window._lock_screen, "Quá 5 phút phải tự khóa"
    # Sau khi khóa: không đếm nữa (kể cả idle rất lớn) + reset() đã chạy
    window._last_activity = time.monotonic() - 60 * 60
    window._check_idle_lock()
    assert window._root.currentWidget() is window._lock_screen
    print("[3] FR-7 tự khóa khi quá thời gian (không đếm khi đang khóa/Tắt): OK")

    # ── [4] FR-7: chỉ phím/chuột reset bộ đếm — camera chạy không reset
    window._lock_now()
    lock._pw1.setText("Mat-khau-123")
    lock._on_submit()  # mở khóa lại để ở trang nội dung
    assert window._root.currentWidget() is window._content
    window._last_activity = time.monotonic() - 10 * 60
    config.idle_lock_minutes = 5
    # Sự kiện KHÔNG phải tương tác (di chuột không bấm — Hover/Move) → không reset
    window.eventFilter(window, QEvent(QEvent.Type.HoverMove))
    window._check_idle_lock()
    assert window._root.currentWidget() is window._lock_screen, (
        "Hover/di chuột không được reset bộ đếm idle"
    )
    # Mở khóa lại → sự kiện bấm chuột/KeyPress phải reset bộ đếm
    window._lock_now()
    lock._pw1.setText("Mat-khau-123")
    lock._on_submit()
    window._last_activity = time.monotonic() - 10 * 60
    window.eventFilter(window, QEvent(QEvent.Type.MouseButtonPress))
    assert time.monotonic() - window._last_activity < 1, "MouseButtonPress phải reset idle"
    window.eventFilter(window, QEvent(QEvent.Type.KeyPress))
    assert time.monotonic() - window._last_activity < 1, "KeyPress phải reset idle"
    window._check_idle_lock()
    assert window._root.currentWidget() is window._content, "Vừa tương tác → không khóa"
    print("[4] FR-7 chỉ phím/chuột reset bộ đếm (hover không tính): OK")

    # ── [5] FR-7: bộ đếm lại từ đầu sau khi tự khóa + mở khóa
    config.idle_lock_minutes = 1
    window._lock_now()
    lock._pw1.setText("Mat-khau-123")
    lock._on_submit()
    assert time.monotonic() - window._last_activity < 2, "Mở khóa phải coi như vừa tương tác"
    config.idle_lock_minutes = 0  # dọn — tránh timer thật khóa giữa test
    print("[5] FR-7 mở khóa reset bộ đếm idle: OK")

    # ── [6] FR-5: [Đổi câu hỏi bảo mật] — dialog lưu qua AuthService
    from PySide6.QtWidgets import QDialog

    settings._sq_status.setText("")  # xóa nhãn cũ để kiểm tra cập nhật
    dialog = SecurityQuestionsDialog(auth, settings)
    # Điền sẵn câu hỏi cũ (load từ config)
    assert dialog._q1_edit.text() == "Quê quán của bạn?", "Câu hỏi cũ phải điền sẵn"
    assert dialog._q2_edit.text() == "Tên thú cưng đầu tiên?", "Câu hỏi cũ phải điền sẵn"
    # Trả lời sai quy tắc (quá ngắn) → lỗi, dialog KHÔNG đóng (chưa Accepted)
    dialog._q1_edit.setText("Câu hỏi mới số 1?")
    dialog._a1_edit.setText("ab")
    dialog._a2_edit.setText("Miu Vàng")
    dialog._on_confirm()
    assert dialog.result() != QDialog.DialogCode.Accepted, (
        "Lỗi validate phải giữ dialog mở (không được accept)"
    )
    assert dialog._error.text(), "Phải hiện lỗi"
    assert not dialog._error.isHidden(), "Nhãn lỗi phải được bật hiện"
    assert auth.config.security_question_1 == "Quê quán của bạn?", (
        "Validate lỗi → câu hỏi cũ chưa được thay"
    )
    # Nhập hợp lệ → lưu thành công, verify bằng câu trả lời MỚI được
    dialog._a1_edit.setText("Hà Nội Cổ")
    dialog._on_confirm()
    assert auth.has_security_questions, "Phải lưu được câu hỏi mới"
    assert auth.verify_security_answers("hà nội cổ", "miu vàng"), (
        "Câu trả lời MỚI phải so khớp được (trim + lowercase, không bỏ dấu)"
    )
    assert not auth.verify_security_answers("hà nội", "miu"), (
        "Câu trả lời CŨ phải bị vô hiệu sau khi đổi"
    )
    print("[6] FR-5 SecurityQuestionsDialog: validate + lưu câu hỏi mới: OK")

    # ── [7] FR-5: _on_change_security_questions yêu cầu mật khẩu cũ
    # Giả lập PasswordDialog.require trả False (hủy xác thực) → câu hỏi giữ nguyên
    import app.ui.settings_view as sv

    class FakeRequireFalse:
        @staticmethod
        def require(*_args, **_kwargs):
            return False

    orig_require = sv.PasswordDialog.require
    sv.PasswordDialog.require = FakeRequireFalse.require
    try:
        settings._on_change_security_questions()
        assert auth.verify_security_answers("hà nội cổ", "miu vàng"), (
            "Hủy xác thực → câu hỏi cũ phải giữ nguyên"
        )
    finally:
        sv.PasswordDialog.require = orig_require
    print("[7] FR-5 hủy xác thực mật khẩu → không đổi câu hỏi: OK")

    # ── [8] Dọn config.idle_lock_minutes qua combo (tránh lưu rác vào file tạm)
    settings._load_from_config()
    assert settings._idle_combo.currentData() == 0, "Combo phải về Tắt theo config"
    print("[8] Combo phản ánh đúng config sau khi load lại: OK")

except AssertionError as exc:
    passed = False
    print(f"THẤT BẠI: {exc}")
except Exception as exc:  # noqa: BLE001 — in lỗi để debug GUI test
    passed = False
    print(f"LỖI KHÔNG DỰ KIẾN: {type(exc).__name__}: {exc}")

# Dọn file tạm — không cần khôi phục gì vì chưa bao giờ đụng config.json thật
TEMP_CONFIG.unlink(missing_ok=True)

if passed:
    print("\n=== TẤT CẢ TEST QUẢN LÝ MẬT KHẨU (FR-5 đổi câu hỏi + FR-7 idle) ĐỀU QUA ===")
else:
    print("\n=== CÓ TEST THẤT BẠI ===")
    sys.exit(1)
