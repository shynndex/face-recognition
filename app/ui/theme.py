"""Quản lý giao diện Dark/Light theme (Bước 13, FR-9).

Một bảng màu duy nhất, áp toàn cục qua ``QApplication.setStyleSheet``.
Quy tắc thiết kế:
  - Màu sắc nằm Ở ĐÂY (QSS), KHÔNG rải rác trong từng view.
  - Các nút/panel đặc biệt được đánh dấu bằng ``setObjectName`` và style
    theo tên: ``primaryBtn`` (xanh) · ``accentBtn`` (cam) · ``dangerBtn``
    (đỏ) · ``secondaryBtn`` (viền mảnh) · ``card`` · ``videoLabel`` ·
    ``imageLabel`` · ``thumbLabel`` · ``navList``.
  - View chỉ giữ lại thuộc tính BỐ CỤC inline (font-size, padding, weight).

Cách dùng:
    from app.ui.theme import apply_theme
    apply_theme(QApplication.instance(), config.theme)   # khi khởi động
    apply_theme(QApplication.instance(), "light")        # khi bấm nút S/T
"""
from __future__ import annotations

# ============================================================
# THEME TỐI (dark) — mặc định
# ============================================================
DARK_QSS = """
QWidget {
    background-color: #1e1e1e;
    color: #e8e8e8;
}
QLabel { background: transparent; }
QLabel#sectionTitle {
    color: #9aa0a8;
    font-weight: bold;
    letter-spacing: 1px;
    font-size: 12px;
}
QToolTip {
    background-color: #2d2d30; color: #e8e8e8;
    border: 1px solid #4a4a52; border-radius: 4px;
    padding: 4px 6px;
}
QCheckBox { spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #4a4a52; border-radius: 4px;
    background-color: #2d2d30;
}
QCheckBox::indicator:checked {
    background-color: #2d7ff9; border-color: #2d7ff9;
    image: none;
}

QLineEdit, QComboBox, QSpinBox {
    background-color: #2d2d30;
    color: #e8e8e8;
    border: 1px solid #4a4a52;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #2d7ff9;
}
QLineEdit:focus, QComboBox:focus { border-color: #2d7ff9; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #2d2d30; color: #e8e8e8;
    selection-background-color: #2d7ff9;
}

QPushButton {
    background-color: #3a3a42;
    color: #e8e8e8;
    border: 1px solid #4a4a52;
    border-radius: 6px;
    padding: 7px 14px;
}
QPushButton:hover { background-color: #46464f; }
QPushButton:pressed { background-color: #2d2d30; }
QPushButton:disabled { background-color: #26262a; color: #6f6f78; }

QPushButton#primaryBtn {
    background-color: #2d7ff9; color: white;
    border: none; font-weight: bold;
}
QPushButton#primaryBtn:hover { background-color: #1a6ce0; }
QPushButton#primaryBtn:disabled { background-color: #2e4a75; color: #93b4de; }

QPushButton#accentBtn {
    background-color: #e8590c; color: white;
    border: none; font-weight: bold;
}
QPushButton#accentBtn:hover { background-color: #d9480f; }
QPushButton#accentBtn:disabled { background-color: #7a4a2a; color: #d0b8a0; }

QPushButton#dangerBtn {
    background-color: transparent; color: #ff6b6b;
    border: 1px solid #cc5a5a;
}
QPushButton#dangerBtn:hover { background-color: #3a2323; }

QPushButton#secondaryBtn {
    background-color: transparent; color: #c8d0dc;
    border: 1px solid #4a4a52;
}
QPushButton#secondaryBtn:hover { border-color: #2d7ff9; color: #7ab2ff; }

QListWidget, QTableWidget, QTreeWidget {
    background-color: #252528;
    border: 1px solid #3a3a42;
    border-radius: 8px;
    alternate-background-color: #2a2a2e;
}
QListWidget::item { padding: 4px; }
QListWidget::item:selected, QTableWidget::item:selected {
    background-color: #2d4a75; color: white;
}
QTableWidget { gridline-color: #33333a; }
QHeaderView::section {
    background-color: #2a2a2e; color: #c8d0dc;
    border: none; border-bottom: 1px solid #3a3a42;
    padding: 6px; font-weight: bold;
}
QListWidget#navList {
    background-color: #232326;
    border: none;
    border-radius: 12px;
    font-size: 14px;
    padding: 6px;
}
QListWidget#navList::item {
    padding: 12px 14px;
    min-height: 24px;
    border-radius: 8px;
    margin: 2px 4px;
}
QListWidget#navList::item:hover {
    background-color: #2a3a50;
    color: #a0c4ff;
}
QListWidget#navList::item:selected {
    background-color: #1a2a3a; color: #60a5fa; font-weight: bold;
    border-left: 3px solid #2d7ff9; padding-left: 11px;
}

QPushButton#sidebarBtn {
    background-color: #2d2d30;
    color: #c8d0dc;
    border: 1px solid #3a3a42;
    border-radius: 8px;
    padding: 8px;
    font-size: 13px;
}
QPushButton#sidebarBtn:hover { background-color: #3a3a42; color: white; }
QPushButton#sidebarBtn:pressed { background-color: #2d7ff9; }

QDialog { background-color: #1e1e1e; }
QMessageBox { background-color: #1e1e1e; }

QLabel#card, QFrame#card {
    background-color: #2d2d30;
    border: 1px solid #3f3f48;
    border-radius: 10px;
}
QLabel#videoLabel, QLabel#imageLabel {
    background-color: #151515; color: #7f7f88;
    border: 1px solid #3a3a42; border-radius: 8px;
}
QLabel#thumbLabel {
    background-color: #33333a;
    border: 1px solid #44444c;
    border-radius: 8px;
}

QSlider::groove:horizontal {
    height: 6px; background: #3a3a42;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 16px; height: 16px; margin: -5px 0;
    background: #2d7ff9; border-radius: 8px;
}
QSlider::sub-page:horizontal {
    background: #2d7ff9; border-radius: 3px;
}
QSlider::handle:horizontal:hover { background: #4a94ff; }

QScrollBar:vertical { background: #232326; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #4a4a52; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #5a5a64; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: #232326; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #4a4a52; border-radius: 5px; min-width: 30px; }
"""

# ============================================================
# THEME SÁNG (light)
# ============================================================
LIGHT_QSS = """
QWidget {
    background-color: #f5f5f7;
    color: #1f1f1f;
}
QLabel { background: transparent; }
QLabel#sectionTitle {
    color: #6f6f77;
    font-weight: bold;
    letter-spacing: 1px;
    font-size: 12px;
}
QToolTip {
    background-color: #ffffff; color: #1f1f1f;
    border: 1px solid #c8c8cc; border-radius: 4px;
    padding: 4px 6px;
}
QCheckBox { spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #c8c8cc; border-radius: 4px;
    background-color: #ffffff;
}
QCheckBox::indicator:checked {
    background-color: #2d7ff9; border-color: #2d7ff9;
    image: none;
}

QLineEdit, QComboBox, QSpinBox {
    background-color: #ffffff;
    color: #1f1f1f;
    border: 1px solid #c8c8cc;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #2d7ff9;
}
QLineEdit:focus, QComboBox:focus { border-color: #2d7ff9; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #ffffff; color: #1f1f1f;
    selection-background-color: #d0e4ff;
}

QPushButton {
    background-color: #ffffff;
    color: #1f1f1f;
    border: 1px solid #c8c8cc;
    border-radius: 6px;
    padding: 7px 14px;
}
QPushButton:hover { background-color: #eef1f5; }
QPushButton:pressed { background-color: #e0e4ea; }
QPushButton:disabled { background-color: #f0f0f2; color: #9a9aa2; }

QPushButton#primaryBtn {
    background-color: #2d7ff9; color: white;
    border: none; font-weight: bold;
}
QPushButton#primaryBtn:hover { background-color: #1a6ce0; }
QPushButton#primaryBtn:disabled { background-color: #a9c6f0; color: #e8f0fa; }

QPushButton#accentBtn {
    background-color: #e8590c; color: white;
    border: none; font-weight: bold;
}
QPushButton#accentBtn:hover { background-color: #d9480f; }
QPushButton#accentBtn:disabled { background-color: #e0b89a; color: #f8ede4; }

QPushButton#dangerBtn {
    background-color: #fdf6f6; color: #c33;
    border: 1px solid #d66;
}
QPushButton#dangerBtn:hover { background-color: #fbe9e9; }

QPushButton#secondaryBtn {
    background-color: #fafafa; color: #333;
    border: 1px solid #bbb;
}
QPushButton#secondaryBtn:hover { border-color: #2d7ff9; color: #2d7ff9; }

QListWidget, QTableWidget, QTreeWidget {
    background-color: #ffffff;
    border: 1px solid #d0d0d4;
    border-radius: 8px;
    alternate-background-color: #f2f4f7;
}
QListWidget::item { padding: 4px; }
QListWidget::item:selected, QTableWidget::item:selected {
    background-color: #d0e4ff; color: #1f1f1f;
}
QTableWidget { gridline-color: #e4e4e8; }
QHeaderView::section {
    background-color: #eef1f5; color: #555;
    border: none; border-bottom: 1px solid #d0d0d4;
    padding: 6px; font-weight: bold;
}
QListWidget#navList {
    background-color: #f0f0f4;
    border: none;
    border-radius: 12px;
    font-size: 14px;
    padding: 6px;
}
QListWidget#navList::item {
    padding: 12px 14px;
    min-height: 24px;
    border-radius: 8px;
    margin: 2px 4px;
}
QListWidget#navList::item:hover {
    background-color: #dce8f5;
    color: #1a5fb4;
}
QListWidget#navList::item:selected {
    background-color: #e0ecfa; color: #1a5fb4; font-weight: bold;
    border-left: 3px solid #2d7ff9; padding-left: 11px;
}

QPushButton#sidebarBtn {
    background-color: #e8eaee;
    color: #444;
    border: 1px solid #d0d0d4;
    border-radius: 8px;
    padding: 8px;
    font-size: 13px;
}
QPushButton#sidebarBtn:hover { background-color: #d0d4dc; color: #1a1a1a; }
QPushButton#sidebarBtn:pressed { background-color: #2d7ff9; color: white; }

QDialog { background-color: #f5f5f7; }
QMessageBox { background-color: #f5f5f7; }

QLabel#card, QFrame#card {
    background-color: #ffffff;
    border: 1px solid #d0d0d4;
    border-radius: 10px;
}
QLabel#videoLabel, QLabel#imageLabel {
    background-color: #151515; color: #8a8a92;
    border: 1px solid #3a3a42; border-radius: 8px;
}
QLabel#thumbLabel {
    background-color: #eef1f5;
    border: 1px solid #d0d0d4;
    border-radius: 8px;
}

QSlider::groove:horizontal {
    height: 6px; background: #d0d0d4;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 16px; height: 16px; margin: -5px 0;
    background: #2d7ff9; border-radius: 8px;
}
QSlider::sub-page:horizontal {
    background: #2d7ff9; border-radius: 3px;
}
QSlider::handle:horizontal:hover { background: #1a6ce0; }

QScrollBar:vertical { background: #f0f0f2; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #c0c0c8; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #a8a8b0; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: #f0f0f2; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #c0c0c8; border-radius: 5px; min-width: 30px; }
"""


def apply_theme(app, theme: str) -> None:
    """Áp stylesheet cho toàn ứng dụng theo theme ('dark' hoặc 'light')."""
    stylesheet = DARK_QSS if theme == "dark" else LIGHT_QSS
    app.setStyleSheet(stylesheet)
