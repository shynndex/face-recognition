"""Widget dùng chung — ``PasswordEdit``: ô mật khẩu kèm icon con mắt.

Icon con mắt nằm NGAY TRONG ô nhập (trailing action của QLineEdit), không
còn nút chữ "👁 Hiện / 🙈 Ẩn" riêng. Icon vẽ từ SVG nhúng (Feather-style)
qua ``QSvgRenderer`` — không cần file tài nguyên ngoài.

Trạng thái chuẩn (giống Material):
  - mật khẩu đang ẨN   → icon mắt bị gạch (eye-off) + tooltip "Hiện mật khẩu"
  - mật khẩu đang HIỆN → icon mắt mở            + tooltip "Ẩn mật khẩu"
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLineEdit

# Mắt đang mở (hiện nội dung) — stroke xám trung tính, đọc được cả 2 theme
_EYE_OPEN_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' "
    "viewBox='0 0 24 24' fill='none' stroke='#8a919d' stroke-width='2' "
    "stroke-linecap='round' stroke-linejoin='round'>"
    "<path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z'/>"
    "<circle cx='12' cy='12' r='3'/></svg>"
)

# Mắt bị gạch (đang ẩn nội dung)
_EYE_OFF_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' "
    "viewBox='0 0 24 24' fill='none' stroke='#8a919d' stroke-width='2' "
    "stroke-linecap='round' stroke-linejoin='round'>"
    "<path d='M9.88 9.88a3 3 0 1 0 4.24 4.24'/>"
    "<path d='M10.73 5.08A10.43 10.43 0 0 1 12 5c6.5 0 10 7 10 7a13.16 "
    "13.16 0 0 1-1.67 2.68'/>"
    "<path d='M6.61 6.61A13.526 13.526 0 0 0 2 12s3.5 7 10 7a9.74 9.74 0 0 0 "
    "5.39-1.61'/>"
    "<line x1='2' y1='2' x2='22' y2='22'/></svg>"
)


def _eye_icon(svg: str) -> QIcon:
    """Render chuỗi SVG thành QIcon (render 2x để nét trên màn HiDPI)."""
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pix = QPixmap(40, 40)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


class PasswordEdit(QLineEdit):
    """QLineEdit mật khẩu với icon con mắt bật/tắt hiện chữ ngay trong ô."""

    def __init__(self, parent: QLineEdit | None = None) -> None:
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self._revealed = False
        self._icon_open = _eye_icon(_EYE_OPEN_SVG)
        self._icon_off = _eye_icon(_EYE_OFF_SVG)
        self._eye_action = self.addAction(
            self._icon_off, QLineEdit.ActionPosition.TrailingPosition
        )
        self._eye_action.setToolTip("Hiện mật khẩu")
        self._eye_action.triggered.connect(self.toggle_visible)

    def is_revealed(self) -> bool:
        """Mật khẩu đang hiện rõ (plain text) hay không."""
        return self._revealed

    def toggle_visible(self) -> None:
        """Bật/tắt hiện mật khẩu + đổi icon/tooltip tương ứng."""
        self._revealed = not self._revealed
        self.setEchoMode(
            QLineEdit.EchoMode.Normal if self._revealed else QLineEdit.EchoMode.Password
        )
        self._eye_action.setIcon(self._icon_open if self._revealed else self._icon_off)
        self._eye_action.setToolTip("Ẩn mật khẩu" if self._revealed else "Hiện mật khẩu")

    def reset_echo(self) -> None:
        """Về trạng thái ẨN mặc định (dùng khi đổi chế độ màn hình khóa)."""
        if self._revealed:
            self.toggle_visible()
