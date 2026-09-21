"""AttendanceEditDialog — sửa bản ghi ngày công (attendance-spec FR-5).

UI gọi SAU KHI PasswordDialog.require trả True (xác thực mật khẩu nằm ở
AttendanceView._on_edit — cùng pattern HistoryView._on_delete). Dialog cho:
  - sửa giờ vào / giờ ra ('HH:MM' địa phương; ô rỗng = xóa mốc);
  - đổi trạng thái: Có mặt / Nghỉ phép / Công tác;
  - ghi chú.
Mọi thay đổi gọi AttendanceService (tự ghi attendance_audit cũ→mới).
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.services.attendance import (
    STATUS_AUTO,
    STATUS_LABELS,
    STATUS_LEAVE,
    STATUS_TRIP,
    AttendanceService,
)

logger = logging.getLogger(__name__)


class AttendanceEditDialog(QDialog):
    """Hộp thoại sửa giờ vào/ra + trạng thái + ghi chú của 1 ngày công."""

    def __init__(
        self,
        service: AttendanceService,
        day_id: str,
        person_name: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._day_id = day_id
        self._record = service._require_day(day_id)  # nạp 1 lần — hiển thị giá trị cũ

        self.setWindowTitle("SỬA BẢN GHI CHẤM CÔNG")
        self.setModal(True)
        self.setFixedWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel(
            f"Sửa bản ghi chấm công — {person_name}, {self._record.work_date}"
        )
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        title.setWordWrap(True)
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(8)

        self._in_edit = self._make_time_edit(self._record.check_in_at)
        self._out_edit = self._make_time_edit(self._record.check_out_at)
        form.addRow("Giờ vào (HH:MM):", self._in_edit)
        form.addRow("Giờ ra (HH:MM):", self._out_edit)

        self._status_combo = QComboBox()
        for status in (STATUS_AUTO, STATUS_LEAVE, STATUS_TRIP):
            self._status_combo.addItem(STATUS_LABELS[status], status)
        index = self._status_combo.findData(self._record.status)
        if index < 0:  # 'manual' không có trong combo → ánh về 'Có mặt'
            index = 0
        self._status_combo.setCurrentIndex(index)
        form.addRow("Trạng thái:", self._status_combo)

        self._note_edit = QLineEdit(self._record.note)
        self._note_edit.setPlaceholderText("Ghi chú (tùy chọn)")
        form.addRow("Ghi chú:", self._note_edit)
        layout.addLayout(form)

        hint = QLabel("Để trống ô giờ = xóa mốc đó. Giờ nhập theo giờ máy (địa phương).")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 12px;")
        layout.addWidget(hint)

        self._error = QLabel()
        self._error.setStyleSheet("color: #d33; font-weight: bold;")
        self._error.setVisible(False)
        layout.addWidget(self._error)

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

    # ---------------------------------------------------------
    # Ghi
    # ---------------------------------------------------------
    def _make_time_edit(self, iso_utc: str | None) -> QLineEdit:
        """Ô giờ 'HH:MM' — chứa giá trị cũ (đổi UTC→địa phương khi mở)."""
        edit = QLineEdit()
        if iso_utc:
            from app.services.attendance import _fmt_local_hhmm

            edit.setText(_fmt_local_hhmm(iso_utc))
        edit.setPlaceholderText("HH:MM")
        edit.setMaxLength(5)
        return edit

    def _on_save(self) -> None:
        """Lưu mọi thay đổi qua service — lỗi (giờ sai) hiển thị, không đóng."""
        check_in = self._in_edit.text().strip()
        check_out = self._out_edit.text().strip()

        old_status = self._record.status
        new_status = self._status_combo.currentData()
        # 'manual' (đã sửa giờ trước đó) → nếu người dùng không đổi trạng thái
        # thì giữ 'manual', tránh ghi đè mất vết
        if old_status == "manual" and new_status == STATUS_AUTO:
            new_status = "manual"

        try:
            # Chỉ gọi service khi có thay đổi thật (giờ: None = giữ nguyên)
            old_in = self._local_hhmm(self._record.check_in_at)
            old_out = self._local_hhmm(self._record.check_out_at)
            if check_in != old_in or check_out != old_out:
                self._service.edit_times(
                    self._day_id,
                    check_in_local=check_in if check_in != old_in else None,
                    check_out_local=check_out if check_out != old_out else None,
                )
            if new_status != old_status:
                self._service.set_status(self._day_id, new_status)
            if self._note_edit.text().strip() != self._record.note:
                self._service.set_note(self._day_id, self._note_edit.text())
        except ValueError as exc:
            self._error.setText(str(exc))
            self._error.setVisible(True)
            return

        logger.info("Đã sửa bản ghi chấm công %s", self._day_id)
        self.accept()

    @staticmethod
    def _local_hhmm(iso_utc: str | None) -> str:
        if not iso_utc:
            return ""
        from app.services.attendance import _fmt_local_hhmm

        return _fmt_local_hhmm(iso_utc)
