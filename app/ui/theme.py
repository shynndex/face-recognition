"""Quản lý giao diện — hướng thẩm mỹ **Security Console** (2026-09).

Hướng thiết kế: nền than tối, một màu nhấn duy nhất (hổ phách #f59f00),
chi tiết kiểu console (Consolas), viền sắc, bo góc nhỏ.
Vai trò màu: primaryBtn = hành động chính, accentBtn = hành động nổi bậc,
dangerBtn = hủy/nguy hiểm. Light theme giữ nguyên vai trò, nền giấy ấm.

Một bảng QSS duy nhất, áp toàn cục qua ``QApplication.setStyleSheet``.
Quy tắc thiết kế:
  - Màu sắc nằm Ở ĐÂY (QSS), KHÔNG rải rác trong từng view.
  - Các nút/panel đặc biệt được đánh dấu bằng ``setObjectName`` và style
    theo tên: ``primaryBtn`` (hành động chính) · ``accentBtn`` (nổi bậc) ·
    ``dangerBtn`` (hủy) · ``secondaryBtn`` (viền mảnh) · ``card`` ·
    ``videoLabel`` · ``imageLabel`` · ``thumbLabel`` · ``navList`` ·
    ``panelCard`` (panel viền nhấn) · ``headerLabel`` (tiêu đề section).
  - View chỉ giữ lại thuộc tính BỐ CỤC inline (font-size, padding, weight).

Cách dùng:
    from app.ui.theme import apply_theme
    apply_theme(QApplication.instance(), config.theme)   # khi khởi động
    apply_theme(QApplication.instance(), "light")        # khi bấm nút S/T
"""
from __future__ import annotations

# ============================================================
# TOKEN THIẾT KẾ — tham chiếu chung (QSS dưới đây tự giữ giá trị đồng bộ)
# ============================================================
ACCENT = "#f59f00"  # hổ phách — màu nhấn duy nhất của Security Console
FONT_UI = 'Segoe UI, "Be Vietnam Pro", Arial, sans-serif'
FONT_MONO = 'Consolas, "Cascadia Mono", "Courier New", monospace'

# ============================================================
# THEME TỐI (dark) — mặc định
# ============================================================
DARK_QSS = """
* {
    font-family: Segoe UI, "Be Vietnam Pro", Arial, sans-serif;
}
QWidget {
    background-color: #14161a;
    color: #d6dae2;
}
QLabel { background: transparent; }

/* ---------- Tiêu đề section kiểu console ---------- */
QLabel#sectionTitle, QLabel#headerLabel {
    font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
    color: #f59f00;
    font-weight: bold;
    letter-spacing: 2px;
    font-size: 12px;
}
QToolTip {
    background-color: #1d2026; color: #d6dae2;
    border: 1px solid #f59f00; border-radius: 4px;
    padding: 4px 6px;
}
QCheckBox { spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #3a4049; border-radius: 3px;
    background-color: #1d2026;
}
QCheckBox::indicator:checked {
    background-color: #f59f00; border-color: #f59f00;
    image: none;
}

/* ---------- Ô nhập ---------- */
QLineEdit, QComboBox, QSpinBox {
    background-color: #1a1d22;
    color: #e8ebf0;
    border: 1px solid #3a4049;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #f59f00;
    selection-color: #14161a;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #f59f00;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #1d2026; color: #e8ebf0;
    selection-background-color: #f59f00; selection-color: #14161a;
}

/* ---------- Nút thường ---------- */
QPushButton {
    background-color: #232830;
    color: #d6dae2;
    border: 1px solid #3a4049;
    border-radius: 4px;
    padding: 7px 14px;
}
QPushButton:hover { background-color: #2b313a; border-color: #4a515c; }
QPushButton:pressed { background-color: #1a1d22; }
QPushButton:disabled { background-color: #1d2026; color: #5c636e; }

/* Nút hành động chính — hổ phách đặc, chữ tối */
QPushButton#primaryBtn {
    background-color: #f59f00; color: #14161a;
    border: none; font-weight: bold;
}
QPushButton#primaryBtn:hover { background-color: #ffb324; }
QPushButton#primaryBtn:pressed { background-color: #d98800; }
QPushButton#primaryBtn:disabled { background-color: #5a4a26; color: #8a7d5c; }

/* Nút nổi bậc — viền hổ phách trên nền tối, không đặc */
QPushButton#accentBtn {
    background-color: #2a2313; color: #ffbf3d;
    border: 1px solid #f59f00; font-weight: bold;
}
QPushButton#accentBtn:hover { background-color: #3a2f16; }
QPushButton#accentBtn:pressed { background-color: #1d1810; }
QPushButton#accentBtn:disabled {
    background-color: #221e17; color: #7a6a45; border-color: #6a5a30;
}

/* Nút nguy hiểm — viền đỏ, nền trong */
QPushButton#dangerBtn {
    background-color: transparent; color: #ff7b72;
    border: 1px solid #a63d38;
}
QPushButton#dangerBtn:hover { background-color: #331c1a; }

/* Nút phụ — viền mảnh */
QPushButton#secondaryBtn {
    background-color: transparent; color: #a9b1bd;
    border: 1px solid #3a4049;
}
QPushButton#secondaryBtn:hover {
    border-color: #f59f00; color: #f59f00; background-color: #2a2313;
}

/* ---------- Danh sách / bảng ---------- */
QListWidget, QTableWidget, QTreeWidget {
    background-color: #191c21;
    border: 1px solid #2e333c;
    border-radius: 6px;
    alternate-background-color: #1d2026;
}
QListWidget::item { padding: 4px; }
QListWidget::item:selected, QTableWidget::item:selected {
    background-color: #33290f; color: #ffbf3d;
}
QTableWidget { gridline-color: #262a31; }
QHeaderView::section {
    background-color: #1d2026; color: #f59f00;
    border: none; border-bottom: 1px solid #2e333c;
    padding: 6px; font-weight: bold;
    font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
    letter-spacing: 1px;
}

/* ---------- Sidebar điều hướng ---------- */
QListWidget#navList {
    background-color: #101216;
    border: none;
    border-right: 1px solid #2e333c;
    border-radius: 0;
    font-size: 14px;
    padding: 8px 0;
}
QListWidget#navList::item {
    padding: 11px 16px;
    min-height: 24px;
    border-radius: 0;
    margin: 0;
    border-left: 3px solid transparent;
}
QListWidget#navList::item:hover {
    background-color: #1a1d22;
    color: #ffbf3d;
}
QListWidget#navList::item:selected {
    background-color: #1c1a12;
    color: #f59f00;
    font-weight: bold;
    border-left: 3px solid #f59f00;
    padding-left: 13px;
}

/* Nút sidebar (Khoá / theme) — hàng dưới cùng sidebar */
QPushButton#sidebarBtn {
    background-color: #1d2026;
    color: #a9b1bd;
    border: 1px solid #2e333c;
    border-radius: 4px;
    padding: 8px;
    font-size: 13px;
}
QPushButton#sidebarBtn:hover {
    background-color: #2a2313; color: #f59f00; border-color: #f59f00;
}
QPushButton#sidebarBtn:pressed { background-color: #f59f00; color: #14161a; }

QDialog { background-color: #14161a; }
QMessageBox { background-color: #14161a; }

/* ---------- Panel / thẻ ---------- */
QLabel#card, QFrame#card {
    background-color: #191c21;
    border: 1px solid #2e333c;
    border-radius: 6px;
}
QLabel#panelCard, QFrame#panelCard {
    background-color: #191c21;
    border: 1px solid #3a4049;
    border-top: 2px solid #f59f00;
    border-radius: 6px;
}
QLabel#videoLabel, QLabel#imageLabel {
    background-color: #0d0e11; color: #5c636e;
    border: 1px solid #2e333c; border-radius: 6px;
}
QLabel#thumbLabel {
    background-color: #232830;
    border: 1px solid #3a4049;
    border-radius: 6px;
}

/* ---------- Thanh trượt — kiểu console ---------- */
QSlider::groove:horizontal {
    height: 4px; background: #2e333c;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px; height: 14px; margin: -5px 0;
    background: #f59f00; border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #8a6512; border-radius: 2px;
}
QSlider::handle:horizontal:hover { background: #ffb324; }

/* ---------- Progress bar — chunk hổ phách ---------- */
QProgressBar {
    border: 1px solid #2e333c; border-radius: 4px;
    text-align: center;
    background: #1a1d22; color: #e8ebf0;
}
QProgressBar::chunk {
    background-color: #f59f00; border-radius: 3px;
}

/* ---------- Scrollbar mảnh ---------- */
QScrollBar:vertical { background: #101216; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4049; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #f59f00; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: #101216; height: 8px; margin: 0; }
QScrollBar::handle:horizontal { background: #3a4049; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #f59f00; }
"""

# ============================================================
# THEME SÁNG (light) — "bản đồ giấy kỹ thuật": nền giấy ấm,
# mực đậm, nhấn hổ phách đậm hơn (#b06a00) để đủ tương phản
# ============================================================
LIGHT_QSS = """
* {
    font-family: Segoe UI, "Be Vietnam Pro", Arial, sans-serif;
}
QWidget {
    background-color: #f2f0eb;
    color: #1d2129;
}
QLabel { background: transparent; }

/* ---------- Tiêu đề section kiểu console ---------- */
QLabel#sectionTitle, QLabel#headerLabel {
    font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
    color: #b06a00;
    font-weight: bold;
    letter-spacing: 2px;
    font-size: 12px;
}
QToolTip {
    background-color: #ffffff; color: #1d2129;
    border: 1px solid #b06a00; border-radius: 4px;
    padding: 4px 6px;
}
QCheckBox { spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #c9c5bb; border-radius: 3px;
    background-color: #ffffff;
}
QCheckBox::indicator:checked {
    background-color: #b06a00; border-color: #b06a00;
    image: none;
}

/* ---------- Ô nhập ---------- */
QLineEdit, QComboBox, QSpinBox {
    background-color: #ffffff;
    color: #1d2129;
    border: 1px solid #c9c5bb;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #b06a00;
    selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #b06a00;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #ffffff; color: #1d2129;
    selection-background-color: #b06a00; selection-color: #ffffff;
}

/* ---------- Nút thường ---------- */
QPushButton {
    background-color: #ffffff;
    color: #1d2129;
    border: 1px solid #c9c5bb;
    border-radius: 4px;
    padding: 7px 14px;
}
QPushButton:hover { background-color: #ece9e2; border-color: #b0aa9c; }
QPushButton:pressed { background-color: #e0dcd2; }
QPushButton:disabled { background-color: #eeede8; color: #a5a29a; }

/* Nút hành động chính — hổ phách đậm đặc, chữ trắng */
QPushButton#primaryBtn {
    background-color: #b06a00; color: #ffffff;
    border: none; font-weight: bold;
}
QPushButton#primaryBtn:hover { background-color: #c97b06; }
QPushButton#primaryBtn:pressed { background-color: #965a00; }
QPushButton#primaryBtn:disabled { background-color: #d8c3a0; color: #faf6ee; }

/* Nút nổi bậc — nền hổ phách nhạt, viền đậm */
QPushButton#accentBtn {
    background-color: #fdeccc; color: #7d4a00;
    border: 1px solid #b06a00; font-weight: bold;
}
QPushButton#accentBtn:hover { background-color: #fbdca6; }
QPushButton#accentBtn:pressed { background-color: #f7cf8a; }
QPushButton#accentBtn:disabled {
    background-color: #efe8da; color: #b3a58c; border-color: #d8c3a0;
}

/* Nút nguy hiểm — viền đỏ, nền trong */
QPushButton#dangerBtn {
    background-color: transparent; color: #b03030;
    border: 1px solid #cc6666;
}
QPushButton#dangerBtn:hover { background-color: #f6e4e4; }

/* Nút phụ — viền mảnh */
QPushButton#secondaryBtn {
    background-color: transparent; color: #4a4f58;
    border: 1px solid #c9c5bb;
}
QPushButton#secondaryBtn:hover {
    border-color: #b06a00; color: #b06a00; background-color: #fdeccc;
}

/* ---------- Danh sách / bảng ---------- */
QListWidget, QTableWidget, QTreeWidget {
    background-color: #ffffff;
    border: 1px solid #d5d1c7;
    border-radius: 6px;
    alternate-background-color: #f7f5f0;
}
QListWidget::item { padding: 4px; }
QListWidget::item:selected, QTableWidget::item:selected {
    background-color: #fdeccc; color: #7d4a00;
}
QTableWidget { gridline-color: #e6e3db; }
QHeaderView::section {
    background-color: #ece9e2; color: #7d4a00;
    border: none; border-bottom: 1px solid #d5d1c7;
    padding: 6px; font-weight: bold;
    font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
    letter-spacing: 1px;
}

/* ---------- Sidebar điều hướng ---------- */
QListWidget#navList {
    background-color: #e9e6df;
    border: none;
    border-right: 1px solid #d5d1c7;
    border-radius: 0;
    font-size: 14px;
    padding: 8px 0;
}
QListWidget#navList::item {
    padding: 11px 16px;
    min-height: 24px;
    border-radius: 0;
    margin: 0;
    border-left: 3px solid transparent;
}
QListWidget#navList::item:hover {
    background-color: #fdeccc;
    color: #7d4a00;
}
QListWidget#navList::item:selected {
    background-color: #f7e3bd;
    color: #7d4a00;
    font-weight: bold;
    border-left: 3px solid #b06a00;
    padding-left: 13px;
}

/* Nút sidebar (Khoá / theme) — hàng dưới cùng sidebar */
QPushButton#sidebarBtn {
    background-color: #e2dfd7;
    color: #4a4f58;
    border: 1px solid #d0ccc2;
    border-radius: 4px;
    padding: 8px;
    font-size: 13px;
}
QPushButton#sidebarBtn:hover {
    background-color: #fdeccc; color: #7d4a00; border-color: #b06a00;
}
QPushButton#sidebarBtn:pressed { background-color: #b06a00; color: #ffffff; }

QDialog { background-color: #f2f0eb; }
QMessageBox { background-color: #f2f0eb; }

/* ---------- Panel / thẻ ---------- */
QLabel#card, QFrame#card {
    background-color: #ffffff;
    border: 1px solid #d5d1c7;
    border-radius: 6px;
}
QLabel#panelCard, QFrame#panelCard {
    background-color: #ffffff;
    border: 1px solid #c9c5bb;
    border-top: 2px solid #b06a00;
    border-radius: 6px;
}
QLabel#videoLabel, QLabel#imageLabel {
    background-color: #16181c; color: #8a8a92;
    border: 1px solid #3a3a42; border-radius: 6px;
}
QLabel#thumbLabel {
    background-color: #ece9e2;
    border: 1px solid #d5d1c7;
    border-radius: 6px;
}

/* ---------- Thanh trượt — kiểu console ---------- */
QSlider::groove:horizontal {
    height: 4px; background: #d5d1c7;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px; height: 14px; margin: -5px 0;
    background: #b06a00; border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #dcb377; border-radius: 2px;
}
QSlider::handle:horizontal:hover { background: #c97b06; }

/* ---------- Progress bar — chunk hổ phách đậm ---------- */
QProgressBar {
    border: 1px solid #d5d1c7; border-radius: 4px;
    text-align: center;
    background: #ffffff; color: #1d2129;
}
QProgressBar::chunk {
    background-color: #b06a00; border-radius: 3px;
}

/* ---------- Scrollbar mảnh ---------- */
QScrollBar:vertical { background: #e9e6df; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #c9c5bb; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #b06a00; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: #e9e6df; height: 8px; margin: 0; }
QScrollBar::handle:horizontal { background: #c9c5bb; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #b06a00; }
"""


def apply_theme(app, theme: str) -> None:
    """Áp stylesheet cho toàn ứng dụng theo theme ('dark' hoặc 'light')."""
    stylesheet = DARK_QSS if theme == "dark" else LIGHT_QSS
    app.setStyleSheet(stylesheet)
