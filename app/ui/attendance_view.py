"""AttendanceView — trang CHẤM CÔNG (attendance-spec FR-7).

Bảng công tháng: dòng = người, cột = ngày 1..31, ô = trạng thái màu
(Có mặt · Đi muộn X phút · Thiếu giờ ra · Nghỉ phép · Công tác · Vắng mặt).
Header mỗi dòng hiển thị tên + số công + số lần muộn + số ngày thiếu giờ ra.

Bấm ô ngày → panel chi tiết: giờ vào/ra, số giờ làm, trạng thái, ghi chú,
lịch audit + nút [ Sửa… ] → AttendanceEditDialog (FR-5 — PasswordDialog.require).
[ Xuất CSV ] / [ Xuất PDF ] — báo cáo tháng đang xem (FR-8).
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.infrastructure.db import Database
from app.services.attendance import (
    AttendanceService,
    PersonMonthSummary,
    PersonPayroll,
    format_vnd,
)
from app.services.auth import AuthService
from app.ui.attendance_edit_dialog import AttendanceEditDialog
from app.ui.password_dialog import PasswordDialog

logger = logging.getLogger(__name__)

TITLE = "BẢNG CÔNG THÁNG"

# Màu nền ô theo trạng thái (nhẹ — bảng vẫn đọc được chữ ở 2 theme)
COLOR_PRESENT = QColor("#2e7d32")       # xanh — có mặt
COLOR_LATE = QColor("#b26a00")          # cam — đi muộn
COLOR_MISSING = QColor("#c62828")       # đỏ — thiếu giờ ra
COLOR_LEAVE = QColor("#1565c0")         # xanh dương — nghỉ phép
COLOR_TRIP = QColor("#6a4fb3")          # tím — công tác
COLOR_ABSENT = QColor("#c62828")        # đỏ — vắng mặt
COLOR_OFF = QColor("#9e9e9e")           # xám — ngày không làm việc

# Nhãn rút gọn trong ô ngày (bảng tháng đủ hẹp)
_SHORT: dict[str, str] = {
    "": "—",
    "absent": "V",
    "leave": "P",
    "trip": "T",
    "manual": "S",
}


class AttendanceView(QWidget):
    """Trang bảng công tháng (thêm vào sidebar sau 'Lịch sử')."""

    def __init__(
        self,
        db: Database,
        auth: AuthService,
        config: Config | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._auth = auth
        self._config = config or Config()
        self._service = AttendanceService(db, self._config)
        now = datetime.now()
        self._year = now.year
        self._month = now.month
        # Bản tổng hợp đang hiển thị (dùng cho xuất CSV/PDF)
        self._summaries: list[PersonMonthSummary] = []
        self._build_ui()

    # ---------------------------------------------------------
    # Giao diện
    # ---------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Header: tiêu đề + chọn tháng/năm + nút xuất
        header = QHBoxLayout()
        title = QLabel(TITLE)
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)

        header.addWidget(QLabel("Tháng:"))
        self._month_combo = QComboBox()
        for m in range(1, 13):
            self._month_combo.addItem(f"{m:02d}", m)
        self._month_combo.setCurrentIndex(self._month - 1)
        self._month_combo.currentIndexChanged.connect(self._on_period_changed)
        header.addWidget(self._month_combo)

        self._year_combo = QComboBox()
        current_year = datetime.now().year
        for y in range(current_year - 2, current_year + 2):
            self._year_combo.addItem(str(y), y)
        self._year_combo.setCurrentIndex(self._year_combo.count() - 2)
        self._year_combo.currentIndexChanged.connect(self._on_period_changed)
        header.addWidget(self._year_combo)

        self._csv_btn = QPushButton("Xuất CSV")
        self._csv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._csv_btn.setToolTip("Xuất bảng công tháng ra file CSV mở bằng Excel")
        self._csv_btn.clicked.connect(self._export_csv)
        header.addWidget(self._csv_btn)

        # Bảng lương thô (mở rộng FR-8): số công × hệ số ca
        self._payroll_btn = QPushButton("Bảng lương")
        self._payroll_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._payroll_btn.setObjectName("secondaryBtn")
        self._payroll_btn.setToolTip("Số công × hệ số ca từng người trong tháng")
        self._payroll_btn.clicked.connect(self._show_payroll)
        header.addWidget(self._payroll_btn)

        self._pdf_btn = QPushButton("Xuất PDF")
        self._pdf_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pdf_btn.setToolTip("In / lưu báo cáo tháng ra file PDF")
        self._pdf_btn.clicked.connect(self._export_pdf)
        header.addWidget(self._pdf_btn)
        layout.addLayout(header)

        # Chú giải màu
        legend = QHBoxLayout()
        for color, text in (
            (COLOR_PRESENT, "Có mặt"),
            (COLOR_LATE, "Đi muộn"),
            (COLOR_MISSING, "Thiếu giờ ra"),
            (COLOR_LEAVE, "Nghỉ phép"),
            (COLOR_TRIP, "Công tác"),
            (COLOR_ABSENT, "Vắng mặt"),
            (COLOR_OFF, "Ngày nghỉ"),
        ):
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color.name()}; font-size: 14px;")
            legend.addWidget(dot)
            label = QLabel(text)
            label.setStyleSheet("font-size: 12px;")
            legend.addWidget(label)
            legend.addSpacing(8)
        legend.addStretch(1)
        layout.addLayout(legend)

        # Bảng tháng: cột 0 = người (ghim trái), cột 1..31 = ngày
        self._table = QTableWidget(0, 32)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self._table, stretch=1)

        # Panel chi tiết ngày công
        self._detail_title = QLabel("Chọn một ô ngày để xem chi tiết")
        self._detail_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self._detail_title)

        detail_row = QHBoxLayout()
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self._detail_shift = QLabel("Ca: —")
        self._detail_in = QLabel("Giờ vào: —")
        self._detail_out = QLabel("Giờ ra: —")
        self._detail_status = QLabel("Trạng thái: —")
        self._detail_note = QLabel("Ghi chú: —")
        for i, label in enumerate(
            (self._detail_shift, self._detail_in, self._detail_out,
             self._detail_status, self._detail_note)
        ):
            label.setStyleSheet("font-size: 13px;")
            grid.addWidget(label, 0, i)
        self._detail_audit = QLabel("Lịch sử sửa: —")
        self._detail_audit.setStyleSheet("font-size: 12px;")
        self._detail_audit.setWordWrap(True)
        grid.addWidget(self._detail_audit, 1, 0, 1, 5)
        detail_row.addLayout(grid, stretch=1)

        self._edit_btn = QPushButton("✎ Sửa…")
        self._edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._edit_btn.setEnabled(False)
        self._edit_btn.setToolTip("Sửa giờ vào/ra, trạng thái (yêu cầu mật khẩu)")
        self._edit_btn.clicked.connect(self._on_edit)
        detail_row.addWidget(self._edit_btn, alignment=Qt.AlignmentFlag.AlignTop)

        self._mark_btn = QPushButton("Đánh phép/Công tác…")
        self._mark_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mark_btn.setEnabled(False)
        self._mark_btn.setToolTip(
            "Đánh nghỉ phép hoặc công tác cho ngày CHƯA có bản ghi công "
            "(ngày vắng mặt / tương lai) — yêu cầu mật khẩu"
        )
        self._mark_btn.clicked.connect(self._on_mark_leave)
        detail_row.addWidget(self._mark_btn, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(detail_row)

    # ---------------------------------------------------------
    # Dữ liệu & hiển thị
    # ---------------------------------------------------------
    def _on_period_changed(self, *_args) -> None:
        """Đổi tháng/năm → vẽ lại bảng."""
        self._month = self._month_combo.currentData()
        self._year = self._year_combo.currentData()
        self._refresh()

    def _refresh(self) -> None:
        """Tải tổng hợp tháng + vẽ bảng (gọi khi vào trang / đổi kỳ)."""
        self._summaries = self._service.month_summary(self._year, self._month)
        self._fill_table()

    def _fill_table(self) -> None:
        """Đổ bảng công: dòng người × cột ngày (dùng DayView từ service)."""
        import calendar

        days_in_month = calendar.monthrange(self._year, self._month)[1]
        headers = ["Người"] + [str(d) for d in range(1, days_in_month + 1)]
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)

        self._table.setRowCount(len(self._summaries))
        for row, summary in enumerate(self._summaries):
            header_text = (
                f"{summary.person_name}  ·  {summary.shift_name}  ·  "
                f"{summary.worked_days} công · {summary.late_count} muộn · "
                f"{summary.missing_checkout_count} thiếu ra"
            )
            name_item = QTableWidgetItem(header_text)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, name_item)

            for day in range(1, days_in_month + 1):
                key = f"{self._year:04d}-{self._month:02d}-{day:02d}"
                view = summary.days.get(key)
                item = self._make_day_item(view)
                self._table.setItem(row, day, item)

    @staticmethod
    def _make_day_item(view):
        """Ô ngày: text rút gọn + màu theo trạng thái (None → ngoài dữ liệu)."""
        from app.services.attendance import DayView  # import cục bộ tránh vòng

        if view is None:
            view = DayView(day=None, work_date="", status="", label="—")
        if view.status == "auto" or view.status == "manual":
            if view.missing_checkout:
                color, text = COLOR_MISSING, "!"
            elif view.late_minutes > 0:
                color, text = COLOR_LATE, str(view.late_minutes)
            else:
                color, text = COLOR_PRESENT, "✓"
        elif view.status == "leave":
            color, text = COLOR_LEAVE, _SHORT["leave"]
        elif view.status == "trip":
            color, text = COLOR_TRIP, _SHORT["trip"]
        elif view.status == "absent":
            color, text = COLOR_ABSENT, _SHORT["absent"]
        else:
            color, text = COLOR_OFF, _SHORT.get(view.status, "—")

        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(color)
        if view.day is not None:
            # Tooltip = nhãn đầy đủ (Đi muộn 12 phút (8h30)...)
            item.setToolTip(f"{view.work_date}: {view.label}")
        elif view.status == "absent":
            item.setToolTip(f"{view.work_date}: Vắng mặt")
        elif view.status == "":
            item.setToolTip(f"{view.work_date}: Ngày không làm việc / chưa có dữ liệu")
        return item

    def _on_cell_clicked(self, row: int, column: int) -> None:
        """Bấm ô ngày (cột ≥ 1) → chi tiết; cột 0 (người) bỏ qua."""
        if not (0 <= row < len(self._summaries)) or column < 1:
            return
        summary = self._summaries[row]
        day = column  # cột 1..31 ứng ngày 1..31
        key = f"{self._year:04d}-{self._month:02d}-{day:02d}"
        view = summary.days.get(key)
        if view is None:
            return
        # Lưu cả DÒNG người của ô — tên + person_id lấy đúng người bấm
        # (không dò lại theo work_date — 2 người cùng ngày sẽ nhầm)
        self._selected = view
        self._selected_summary = summary
        self._update_detail(view)

    def _update_detail(self, view) -> None:
        """Cập nhật panel chi tiết + bật nút Sửa/Đánh phép theo loại ô."""
        record = view.day
        summary_name = self._summary_name_of(view)
        self._detail_title.setText(
            f"Chi tiết — {summary_name}, {self._format_date(view.work_date)}"
        )
        if record is None:
            self._detail_shift.setText("Ca: —")
            self._detail_in.setText("Giờ vào: —")
            self._detail_out.setText("Giờ ra: —")
            self._detail_status.setText(f"Trạng thái: {view.label}")
            self._detail_note.setText("Ghi chú: —")
            self._detail_audit.setText("Lịch sử sửa: —")
            self._edit_btn.setEnabled(False)
            # Ô chưa có bản ghi (vắng mặt / tương lai) → bật nút Đánh phép
            self._mark_btn.setEnabled(True)
            return

        shift = self._service.shift_of(record.person_id)
        self._detail_shift.setText(
            f"Ca: {shift.name if shift else '—'}"
            f" ({shift.start_time}–{shift.end_time})" if shift else "Ca: —"
        )
        self._detail_in.setText(f"Giờ vào: {self._fmt_hhmm(record.check_in_at)}")
        self._detail_out.setText(f"Giờ ra: {self._fmt_hhmm(record.check_out_at)}")
        status_text = view.label
        if record.manual_override:
            status_text += " · đã sửa tay"
        self._detail_status.setText(f"Trạng thái: {status_text}")
        self._detail_note.setText(f"Ghi chú: {record.note or '—'}")

        audit = self._service.audit_for_day(record.id)
        if audit:
            latest = audit[0]
            self._detail_audit.setText(
                f"Lịch sử sửa: {len(audit)} lần — gần nhất: "
                f"{latest['new_value']} ({self._fmt_dt(latest['edited_at'])})"
            )
        else:
            self._detail_audit.setText("Lịch sử sửa: chưa có")
        self._edit_btn.setEnabled(True)
        self._mark_btn.setEnabled(False)  # đã có bản ghi → đổi trạng thái qua Sửa

    def _summary_name_of(self, view) -> str:
        """Tên người của ô đang chọn (từ DÒNG đã bấm — không dò lại theo ngày)."""
        summary = getattr(self, "_selected_summary", None)
        if summary is not None and view.work_date in summary.days:
            return summary.person_name
        # Dự phòng: ô ngoài danh sách đã bấm (hiếm — refresh giữa chừng)
        for summary in self._summaries:
            if view.work_date in summary.days:
                return summary.person_name
        return "?"

    # ---------------------------------------------------------
    # Sửa tay (FR-5)
    # ---------------------------------------------------------
    def _on_edit(self) -> None:
        """Mở dialog sửa bản ghi đang chọn — PasswordDialog.require trước (FR-5).

        Mọi thay đổi bản ghi công đều là thao tác nhạy cảm: sai/hủy mật
        khẩu → không mở dialog sửa. Đúng → AttendanceEditDialog (tự ghi
        audit qua service).
        """
        view = getattr(self, "_selected", None)
        if view is None or view.day is None:
            return
        record = view.day
        person_name = self._summary_name_of(view)
        if not PasswordDialog.require(
            self._auth,
            f"Sửa bản ghi chấm công — {person_name}, {self._format_date(record.work_date)}",
            self,
            note="Mọi thay đổi giờ/trạng thái sẽ được ghi vào lịch sử sửa (audit).",
        ):
            logger.info("Hủy sửa bản ghi chấm công (không xác thực được)")
            return
        dialog = AttendanceEditDialog(
            self._service,
            record.id,
            person_name,
            parent=self,
        )
        if dialog.exec():
            self._refresh()

    def _on_mark_leave(self) -> None:
        """Đánh nghỉ phép / công tác cho ngày CHƯA có bản ghi (FR-5).

        Luồng: chọn ô trống → [Đánh phép/Công tác…] → mật khẩu (PasswordDialog)
        → chọn loại (Nghỉ phép / Công tác) → mark_leave tạo dòng công mới
        (không giờ vào/ra) + audit 'set_status' — ngày đó không bị đánh vắng.
        """
        view = getattr(self, "_selected", None)
        summary = getattr(self, "_selected_summary", None)
        if view is None or summary is None or view.day is not None:
            return  # ô đã có bản ghi → đổi trạng thái qua [Sửa…]
        person_id = summary.person_id
        person_name = summary.person_name
        if not PasswordDialog.require(
            self._auth,
            f"Đánh trạng thái ngày công — {person_name}, {self._format_date(view.work_date)}",
            self,
            note="Ngày này chưa có bản ghi công — sẽ tạo bản ghi mới kèm lịch sử sửa.",
        ):
            logger.info("Hủy đánh phép/công tác (không xác thực được)")
            return
        choices = ("Nghỉ phép", "Công tác")
        choice, ok = QInputDialog.getItem(
            self, "Chọn trạng thái",
            f"Trạng thái cho {person_name} ngày {self._format_date(view.work_date)}:",
            choices, 0, False,
        )
        if not ok:
            return
        status = "leave" if choice == "Nghỉ phép" else "trip"
        try:
            self._service.mark_leave(person_id, view.work_date, status)
        except ValueError as exc:
            QMessageBox.warning(self, "Không đánh được", str(exc))
            return
        logger.info(
            "Đã đánh '%s' cho %s ngày %s", choice, person_name, view.work_date
        )
        self._refresh()

    # ---------------------------------------------------------
    # Xuất báo cáo (FR-8)
    # ---------------------------------------------------------
    def _export_csv(self) -> None:
        """Xuất bảng công tháng → CSV UTF-8 BOM (Excel mở giữ dấu)."""
        default_name = f"bang-cong-{self._year}-{self._month:02d}.csv"
        path_str, _filter = QFileDialog.getSaveFileName(
            self, "Xuất CSV", default_name, "CSV (*.csv)"
        )
        if not path_str:
            return
        path = Path(path_str)
        rows = self._csv_rows()
        try:
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerows(rows)
        except OSError as exc:
            QMessageBox.warning(self, "Xuất CSV", f"Không ghi được file:\n{exc}")
            return
        logger.info("Đã xuất CSV: %s", path)
        QMessageBox.information(self, "Xuất CSV", f"Đã xuất: {path}")

    def _csv_rows(self) -> list[list[str]]:
        """Dựng nội dung CSV: header + 1 dòng/ngày công của tháng."""
        rows: list[list[str]] = [
            ["Người", "Ca", "Ngày", "Giờ vào", "Giờ ra", "Số giờ làm",
             "Trạng thái", "Đi muộn (phút)", "Ghi chú"]
        ]
        for summary in self._summaries:
            for key in sorted(summary.days):
                view = summary.days[key]
                if view.day is None and view.status != "absent":
                    continue  # ngày không làm việc / chưa chấm — bỏ
                record = view.day
                shift = self._service.shift_of(summary.person_id)
                rows.append([
                    summary.person_name,
                    shift.name if shift else "—",
                    key,
                    self._fmt_hhmm(record.check_in_at) if record else "—",
                    self._fmt_hhmm(record.check_out_at) if record else "—",
                    self._fmt_worked(view.worked_minutes) if record else "—",
                    view.label,
                    str(view.late_minutes) if view.late_minutes > 0 else "0",
                    record.note if record else "",
                ])

        # ---- Khối LƯƠNG THÔ (mở rộng FR-8): số công × hệ số ca ----
        rows.append([])
        rows.extend(payroll_csv_rows(
            self._service.payroll_summary(self._year, self._month),
            max(0, int(getattr(self._config, "attendance_pay_rate", 0) or 0)),
        ))
        return rows

    # ---------------------------------------------------------
    # Bảng lương thô (mở rộng FR-8): số công × hệ số ca
    # ---------------------------------------------------------
    def _show_payroll(self) -> None:
        """Dialog bảng lương thô tháng đang xem (không sửa được — chỉ tra)."""
        payroll = self._service.payroll_summary(self._year, self._month)
        dialog = PayrollDialog(
            self, payroll, self._year, self._month,
            pay_rate=self._config.attendance_pay_rate,
        )
        dialog.exec()

    def _export_pdf(self) -> None:
        """In / lưu báo cáo tháng ra PDF (QPrinter + QTextDocument HTML)."""
        default_name = f"bang-cong-{self._year}-{self._month:02d}.pdf"
        path_str, _filter = QFileDialog.getSaveFileName(
            self, "Xuất PDF", default_name, "PDF (*.pdf)"
        )
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if path_str:
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(path_str)
        else:
            dialog = QPrintDialog(printer, self)
            if dialog.exec() != QPrintDialog.DialogCode.Accepted:
                return

        doc = QTextDocument()
        doc.setHtml(self._pdf_html())
        doc.print_(printer)
        if path_str:
            logger.info("Đã xuất PDF: %s", path_str)
            QMessageBox.information(self, "Xuất PDF", f"Đã xuất: {path_str}")

    def _pdf_html(self) -> str:
        """HTML báo cáo: bảng người × ngày với ký hiệu trạng thái."""
        import calendar

        days_in_month = calendar.monthrange(self._year, self._month)[1]
        head_cells = "".join(f"<th>{d}</th>" for d in range(1, days_in_month + 1))
        body_rows: list[str] = []
        for summary in self._summaries:
            cells: list[str] = []
            for d in range(1, days_in_month + 1):
                key = f"{self._year:04d}-{self._month:02d}-{d:02d}"
                view = summary.days.get(key)
                color = "#000"
                text = "—"
                if view is not None:
                    if view.status in ("auto", "manual"):
                        if view.missing_checkout:
                            color, text = COLOR_MISSING.name(), "!"
                        elif view.late_minutes > 0:
                            color, text = COLOR_LATE.name(), str(view.late_minutes)
                        else:
                            color, text = COLOR_PRESENT.name(), "✓"
                    elif view.status == "leave":
                        color, text = COLOR_LEAVE.name(), "P"
                    elif view.status == "trip":
                        color, text = COLOR_TRIP.name(), "T"
                    elif view.status == "absent":
                        color, text = COLOR_ABSENT.name(), "V"
                cells.append(f'<td style="color:{color}; text-align:center;">{text}</td>')
            body_rows.append(
                f"<tr><td>{summary.person_name} ({summary.shift_name}, "
                f"{summary.worked_days} công)</td>{''.join(cells)}</tr>"
            )
        legend = (
            "✓ Có mặt · ! Thiếu giờ ra · số = phút đi muộn · P Nghỉ phép · "
            "T Công tác · V Vắng mặt"
        )
        return (
            f"<h2>Bảng công tháng {self._month:02d}/{self._year}</h2>"
            f"<p>{legend}</p>"
            "<table border='0.5' cellspacing='0' cellpadding='3'>"
            f"<tr><th>Người</th>{head_cells}</tr>{''.join(body_rows)}</table>"
        ) + self._payroll_html()

    def _payroll_html(self) -> str:
        """HTML bảng lương thô (trang 2 PDF) — ủy quyền cho hàm dùng chung."""
        return payroll_pdf_html(
            self._service.payroll_summary(self._year, self._month),
            self._year, self._month,
            max(0, int(getattr(self._config, "attendance_pay_rate", 0) or 0)),
        )

    # ---------------------------------------------------------
    # Tiện ích hiển thị
    # ---------------------------------------------------------
    @staticmethod
    def _fmt_hhmm(iso_utc: str | None) -> str:
        if not iso_utc:
            return "—"
        try:
            dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%H:%M")
        except ValueError:
            return "—"

    @staticmethod
    def _fmt_worked(minutes: int | None) -> str:
        if minutes is None:
            return "—"
        return f"{minutes // 60}h{minutes % 60:02d}"

    @staticmethod
    def _format_date(iso_date: str) -> str:
        try:
            d = date.fromisoformat(iso_date)
            return d.strftime("%d/%m/%Y")
        except ValueError:
            return iso_date

    @staticmethod
    def _fmt_dt(iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%H:%M %d/%m")
        except ValueError:
            return iso[:16]

    # ---------------------------------------------------------
    # Vòng đời
    # ---------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 (chuẩn Qt)
        """Mỗi khi vào trang → tải lại dữ liệu mới nhất."""
        super().showEvent(event)
        self._refresh()


class PayrollDialog(QDialog):
    """Dialog bảng lương thô tháng: số công × hệ số ca (mở rộng FR-8).

    Bảng tra nhanh — muốn lưu file thì dùng [ Xuất CSV ] / [ Xuất PDF ]
    (cả hai đều có sẵn khối lương ở trang sau khối chi tiết).
    """

    def __init__(
        self,
        parent,
        payroll: list[PersonPayroll],
        year: int,
        month: int,
        pay_rate: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"BẢNG LƯƠNG THÔ — {month:02d}/{year}")
        self.setModal(True)
        self.resize(860, 440)
        self._pay_rate = max(0, int(pay_rate))
        has_money = self._pay_rate > 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel(f"Lương thô = số công × hệ số ca — tháng {month:02d}/{year}")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        rate_note = (
            f" Đơn giá 1 công quy đổi: {format_vnd(self._pay_rate)}."
            if has_money else " Chưa đặt đơn giá — chỉ hiện công quy đổi "
            "(đặt ở Cài đặt → CHẤM CÔNG)."
        )
        hint = QLabel(
            "Nghỉ phép / công tác / vắng mặt không tính lương; thiếu giờ ra "
            f"vẫn tính công. Hệ số chỉnh ở Cài đặt → CHẤM CÔNG → Sửa ca.{rate_note}"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 12px;")
        layout.addWidget(hint)

        headers = [
            "Người", "Ca áp dụng", "Hệ số", "Số công", "Phép", "Công tác",
            "Vắng", "Lương thô",
        ] + (["Tiền lương (VND)"] if has_money else []) + ["Chi tiết theo ca"]
        money_col = 8 if has_money else -1  # vị trí cột tiền (-1 = không có)
        detail_col = len(headers) - 1
        total_rows = 1 if (has_money and any(p.pay_amount for p in payroll)) else 0
        table = QTableWidget(len(payroll) + total_rows, len(headers))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setHorizontalHeaderLabels(headers)
        total_amount = 0
        for r, p in enumerate(payroll):
            detail = "; ".join(
                f"{name}: {count} × {factor:g}"
                for name, count, factor in p.shift_totals
            )
            total_amount += p.pay_amount
            values = [
                p.person_name,
                p.shift_name,
                f"{p.factor:g}",
                str(p.worked_days),
                str(p.leave_count),
                str(p.trip_count),
                str(p.absent_count),
                f"{p.gross_pay:g}",
            ]
            if has_money:
                values.append(format_vnd(p.pay_amount))
            values.append(detail)
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if c in (7, money_col):
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if c == money_col:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(r, c, item)
        if total_rows:
            total_row = len(payroll)
            fill = [""] * len(headers)
            fill[0] = "TỔNG TIỀN"
            fill[money_col] = format_vnd(total_amount)
            for c, text in enumerate(fill):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                if c == money_col:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(total_row, c, item)
        table.resizeColumnsToContents()
        layout.addWidget(table, stretch=1)

        close_btn = QPushButton("Đóng")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        self._total_amount = total_amount if total_rows else 0


# ---------------------------------------------------------------------------
# Hàm dùng chung — AttendanceView và DashboardView cùng gọi để báo cáo lương
# từ 2 nơi LUÔN KHỚP nhau (nguồn số: AttendanceService.payroll_summary).
# ---------------------------------------------------------------------------

def _shift_detail(shift_totals: list[tuple[str, int, float]]) -> str:
    """Danh sách ca → 'Ca đêm: 4 × 1.5; Hành chính: 2 × 1' (dùng chung CSV/PDF)."""
    return "; ".join(
        f"{name}: {count} × {factor:g}"
        for name, count, factor in shift_totals
    )


def payroll_csv_rows(
    payroll: list[PersonPayroll], pay_rate: int
) -> list[list[str]]:
    """Khối LƯƠNG THÔ của CSV: tiêu đề + header + dòng người + TỔNG TIỀN.

    Đủ 10 cột khớp header; đơn giá 0 → cột tiền trống (không bịa số).
    Dùng chung cho xuất từ AttendanceView và Dashboard.
    """
    rows: list[list[str]] = [
        ["LƯƠNG THÔ", "", "", "", "", "", "", "", "", ""],
        ["Người", "Ca áp dụng", "Hệ số", "Số công",
         "Ngày phép", "Công tác", "Vắng mặt",
         "Lương thô (công quy đổi)", "Tiền lương (VND)", "Chi tiết theo ca"],
    ]
    total_amount = 0
    for p in payroll:
        total_amount += p.pay_amount
        rows.append([
            p.person_name,
            p.shift_name,
            f"{p.factor:g}",
            str(p.worked_days),
            str(p.leave_count),
            str(p.trip_count),
            str(p.absent_count),
            f"{p.gross_pay:g}",
            format_vnd(p.pay_amount) if p.pay_amount else "",
            _shift_detail(p.shift_totals),
        ])
    if total_amount:
        rows.append([
            "TỔNG TIỀN", "", "", "", "", "", "", "",
            format_vnd(total_amount), "",
        ])
    return rows


def payroll_pdf_html(
    payroll: list[PersonPayroll],
    year: int,
    month: int,
    pay_rate: int,
) -> str:
    """HTML trang bảng lương thô của PDF — nội dung khớp khối CSV ở trên."""
    pay_rate = max(0, int(pay_rate))
    has_money = pay_rate > 0
    money_header = "<th>Tiền lương (VND)</th>" if has_money else ""
    body_rows: list[str] = []
    total_amount = 0
    for p in payroll:
        total_amount += p.pay_amount
        money_cell = (
            f"<td style='text-align:right;'><b>{format_vnd(p.pay_amount)}</b></td>"
            if has_money else ""
        )
        body_rows.append(
            f"<tr><td>{p.person_name}</td><td>{p.shift_name}</td>"
            f"<td style='text-align:center;'>{p.factor:g}</td>"
            f"<td style='text-align:center;'>{p.worked_days}</td>"
            f"<td style='text-align:center;'>{p.leave_count}</td>"
            f"<td style='text-align:center;'>{p.trip_count}</td>"
            f"<td style='text-align:center;'>{p.absent_count}</td>"
            f"<td style='text-align:center;'><b>{p.gross_pay:g}</b></td>"
            f"{money_cell}<td>{_shift_detail(p.shift_totals)}</td></tr>"
        )
    total_row = ""
    if has_money and total_amount:
        total_row = (
            "<tr><td><b>TỔNG TIỀN</b></td>"
            + "<td></td>" * 8
            + f"<td style='text-align:right;'><b>{format_vnd(total_amount)}</b></td>"
            + "<td></td></tr>"
        )
    rate_note = (
        f" Đơn giá 1 công quy đổi: {format_vnd(pay_rate)}."
        if has_money else ""
    )
    return (
        "<p style='page-break-before: always;'></p>"
        f"<h2>Bảng lương thô tháng {month:02d}/{year}</h2>"
        "<p>Lương thô = số công × hệ số ca — nghỉ phép / công tác / "
        f"vắng mặt không tính lương.{rate_note}</p>"
        "<table border='0.5' cellspacing='0' cellpadding='3'>"
        "<tr><th>Người</th><th>Ca áp dụng</th><th>Hệ số</th><th>Số công</th>"
        "<th>Phép</th><th>Công tác</th><th>Vắng</th><th>Lương thô</th>"
        f"{money_header}<th>Chi tiết theo ca</th></tr>"
        f"{''.join(body_rows)}{total_row}</table>"
    )
