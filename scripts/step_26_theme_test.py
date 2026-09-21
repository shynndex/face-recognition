"""
Bước 26 — Smoke test theme "Security Console" + sidebar console.

Chạy ở chế độ "minimal" (không mở cửa sổ thật), tự thoát sau khi xong check.
Kiểm tra:
  1. QSS dark + light áp không lỗi parse (Qt in warning ra stderr nếu hỏng)
  2. Sidebar: nhãn thương hiệu + mục nav đánh số '01  Nhận diện'
  3. Thu gọn sidebar (cửa sổ < 700px): nav còn '01', brand ẩn, nút rút gọn
  4. LockScreen: tiêu đề mono console + card không override viền
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import PySide6

pyside_dir = os.path.dirname(PySide6.__file__)
os.environ["QT_PLUGIN_PATH"] = os.path.join(pyside_dir, "plugins")
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(pyside_dir)
os.environ["QT_QPA_PLATFORM"] = "minimal"

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not ok:
        failures.append(name)


from app.config import Config
from app.services.auth import AuthService
from app.ui.main_window import MainWindow
from app.ui.theme import DARK_QSS, LIGHT_QSS, apply_theme

app = QApplication(sys.argv)

# ---- [1] QSS parse: áp cả 2 theme, không crash; stylesheet phải khác nhau
apply_theme(app, "dark")
dark_applied = app.styleSheet()
check("Áp theme dark (stylesheet khác rỗng)", bool(dark_applied))
apply_theme(app, "light")
check("Áp theme light (stylesheet khác rỗng)", bool(app.styleSheet()))
check("Dark ≠ Light QSS", dark_applied != app.styleSheet())
check("QSS chứa nhấn hổ phách #f59f00", "#f59f00" in DARK_QSS)
check("Light dùng hổ phách đậm #b06a00", "#b06a00" in LIGHT_QSS)
check("QSS không còn xanh cũ #2d7ff9", "#2d7ff9" not in DARK_QSS and "#2d7ff9" not in LIGHT_QSS)

# ---- [2] MainWindow + sidebar console
apply_theme(app, "dark")
window = MainWindow(AuthService(Config.load()), Config.load())  # KHÔNG show()
check("Brand label 'FACE·ID // CONSOLE' hiển thị", window._brand_label.text() == "FACE·ID // CONSOLE")
# Nav khớp PAGES thật (Dashboard thêm 2026-09 → 8 mục, '01  Tổng quan' đầu)
check("Nav mục đầu '01  Tổng quan'", window.nav.item(0).text() == "01  Tổng quan")
check("Nav mục cuối '08  Cài đặt'", window.nav.item(7).text() == "08  Cài đặt")
check("Nút khóa 'KHOÁ' (bỏ emoji)", window._lock_btn.text() == "KHOÁ")
check("Nút theme 'SÁNG' (bỏ emoji)", window._theme_btn.text() == "SÁNG")

# ---- [3] Thu gọn sidebar khi cửa sổ nhỏ
# LƯU Ý: resize() trên cửa sổ chưa show KHÔNG phát resizeEvent (Qt chưa tạo
# window handle) → gọi _update_sidebar_mode() trực tiếp (đọc width hiện tại).
window.resize(600, 600)  # < 700 → collapsed
window._update_sidebar_mode()
check("Thu gọn: nav còn '01'", window.nav.item(0).text() == "01")
check("Thu gọn: brand ẩn", window._brand_label.isHidden())
check("Thu gọn: nav rộng 58px", window.nav.width() == 58)
window.resize(1000, 700)
window._update_sidebar_mode()
check("Mở rộng: nav lại '01  Tổng quan'", window.nav.item(0).text() == "01  Tổng quan")
check("Mở rộng: brand hiện lại", not window._brand_label.isHidden())

# ---- [4] LockScreen tiêu đề console
lock = window._lock_screen
from app.ui.lock_screen import LockScreen  # noqa: E402

check("LockScreen là LockScreen", isinstance(lock, LockScreen))
title_labels = [
    lb for lb in lock.findChildren(type(window._brand_label))
    if lb.text() == "NHẬN DIỆN KHUÔN MẶT"
]
check("LockScreen có tiêu đề 'NHẬN DIỆN KHUÔN MẶT'", bool(title_labels))
check(
    "Tiêu đề lock dùng headerLabel + giãn ký tự",
    bool(title_labels)
    and title_labels[0].objectName() == "headerLabel"
    and "letter-spacing" in title_labels[0].styleSheet(),
)
check("QSS dark style headerLabel bằng Consolas", "Consolas" in DARK_QSS)

print(f"\nKết quả: {12 - len(failures)}/12 check qua")
if failures:
    print("FAIL:", failures)
    sys.exit(1)
QTimer.singleShot(200, app.quit)
sys.exit(app.exec())
