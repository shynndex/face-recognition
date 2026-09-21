"""DashboardView — trang TỔNG QUAN chấm công hôm nay (mở app là thấy).

Đọc dữ liệu từ ``AttendanceService.today_overview()``:
  - Hàng thẻ thống kê: Đã vào · Đi muộn · Chưa giờ ra · Nghỉ phép/Công tác ·
    Chưa chấm (chỉ ngày làm việc).
  - Bảng trạng thái từng người: tên · ca · giờ vào · giờ ra · trạng thái
    (màu nền theo ô công — tái dùng bảng màu của AttendanceView).

Điều hướng: nút [ Bảng công tháng ] bắn tín hiệu ``attendance_requested`` →
MainWindow chuyển sang trang Chấm công; nút [ Làm mới ] tải lại bằng tay.
Trang tự tải lại mỗi lần hiển thị (``showEvent`` — cùng pattern HistoryView).
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
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
    PersonTodayStatus,
    TodayOverview,
    format_vnd,
)
from app.ui.theme import ACCENT

logger = logging.getLogger(__name__)

TITLE = "TỔNG QUAN HÔM NAY"

# Màu trạng thái — TÁI DÙNG bảng màu của AttendanceView (1 nguồn sự thật,
# đổi màu ở bảng tháng thì Dashboard tự theo). Không vòng import:
# AttendanceView không tham chiếu DashboardView.
from app.ui.attendance_view import (  # noqa: E402
    COLOR_ABSENT,
    COLOR_LATE,
    COLOR_LEAVE,
    COLOR_MISSING,
    COLOR_OFF,
    COLOR_PRESENT,
    COLOR_TRIP,
)


class _StatCard(QFrame):
    """Một thẻ thống kê: số to + nhãn nhỏ (objectName 'panelCard' của theme)."""

    def __init__(self, label: str, color: str, tooltip: str = "") -> None:
        super().__init__()
        self.setObjectName("panelCard")
        self.setToolTip(tooltip)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self._value = QLabel("0")
        self._value.setStyleSheet(
            f"font-size: 26px; font-weight: bold; color: {color};"
        )
        self._label = QLabel(label)
        self._label.setStyleSheet("font-size: 12px;")
        layout.addWidget(self._value)
        layout.addWidget(self._label)

    def set_value(self, value: int) -> None:
        self._value.setText(str(value))

    def set_text(self, text: str) -> None:
        """Đặt giá trị dạng chuỗi (tiền VND, công quy đổi…)."""
        self._value.setText(text)

    def set_label(self, text: str) -> None:
        """Đổi nhãn phụ (VD: chi tiết cách tính lương tháng)."""
        self._label.setText(text)


class DashboardView(QWidget):
    """Trang tổng quan chấm công hôm nay — mục đầu tiên trong sidebar."""

    # Bấm [ Bảng công tháng ] → MainWindow mở trang Chấm công (FR-7)
    attendance_requested = Signal()

    def __init__(
        self,
        db: Database,
        config: Config | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config or Config()
        self._service = AttendanceService(db, self._config)
        self._overview: TodayOverview | None = None
        self._build_ui()

    # ---------------------------------------------------------
    # Giao diện
    # ---------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self._title = QLabel(TITLE)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        header.addWidget(self._title)
        header.addStretch(1)
        self._date_label = QLabel("")
        self._date_label.setStyleSheet("font-size: 13px;")
        header.addWidget(self._date_label)

        refresh_btn = QPushButton("Làm mới")
        refresh_btn.setObjectName("secondaryBtn")
        refresh_btn.setToolTip("Tải lại tổng quan hôm nay")
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        # Hàng thẻ thống kê (5 thẻ)
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self._card_in = _StatCard("Đã vào", COLOR_PRESENT.name(), "Số người đã check-in hôm nay")
        self._card_late = _StatCard("Đi muộn", COLOR_LATE.name(), "Số người check-in muộn hơn dung sai ca")
        self._card_missing = _StatCard("Chưa giờ ra", COLOR_MISSING.name(), "Đã vào nhưng chưa có giờ ra")
        self._card_leave = _StatCard("Phép/Công tác", COLOR_LEAVE.name(), "Nghỉ phép hoặc công tác hôm nay")
        self._card_absent = _StatCard("Chưa chấm", COLOR_OFF.name(), "Ngày làm việc — chưa thấy check-in")
        for card in (self._card_in, self._card_late, self._card_missing,
                     self._card_leave, self._card_absent):
            cards.addWidget(card, stretch=1)
        layout.addLayout(cards)

        # Thẻ LƯƠNG THÔ THÁNG NÀY (mở rộng FR-8) — hàng riêng, màu nhấn theme:
        # tổng lương toàn công ty = Σ (số công × hệ số ca) × đơn giá.
        # Kèm nút xuất nhanh CSV/PDF — tái dùng hàm dùng chung của AttendanceView
        # nên file xuất từ Dashboard LUÔN KHỚP bảng lương trang Chấm công.
        payroll_row = QHBoxLayout()
        payroll_row.setSpacing(10)
        self._card_payroll = _StatCard(
            "Lương thô tháng này", ACCENT,
            "Tổng lương toàn công ty tháng hiện tại — chi tiết ở trang Chấm công → Bảng lương",
        )
        payroll_row.addWidget(self._card_payroll, stretch=1)

        payroll_btns = QVBoxLayout()
        payroll_btns.setSpacing(4)
        self._payroll_csv_btn = QPushButton("Xuất CSV")
        self._payroll_csv_btn.setObjectName("secondaryBtn")
        self._payroll_csv_btn.setToolTip(
            "Xuất bảng công + khối lương thô tháng ra file CSV mở bằng Excel"
        )
        self._payroll_csv_btn.clicked.connect(self._export_payroll_csv)
        payroll_btns.addWidget(self._payroll_csv_btn)
        self._payroll_pdf_btn = QPushButton("Xuất PDF")
        self._payroll_pdf_btn.setObjectName("secondaryBtn")
        self._payroll_pdf_btn.setToolTip(
            "Lưu báo cáo công + bảng lương thô tháng ra file PDF"
        )
        self._payroll_pdf_btn.clicked.connect(self._export_payroll_pdf)
        payroll_btns.addWidget(self._payroll_pdf_btn)
        payroll_row.addLayout(payroll_btns)
        layout.addLayout(payroll_row)

        # Bảng trạng thái từng người
        self._table = QTableWidget(0, 6)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, stretch=1)

        hint = QLabel(
            "Người đăng ký chưa check-in hôm nay hiện ở thẻ “Chưa chấm” — "
            "chi tiết từng ngày ở trang Chấm công."
        )
        hint.setStyleSheet("font-size: 12px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    # ---------------------------------------------------------
    # Dữ liệu & hiển thị
    # ---------------------------------------------------------
    def refresh(self) -> None:
        """Tải tổng hợp hôm nay + lương tháng + vẽ lại (vào trang / Làm mới)."""
        self._overview = self._service.today_overview()
        now = datetime.now()
        self._payroll = self._service.payroll_summary(now.year, now.month)
        self._fill()

    def showEvent(self, event) -> None:  # noqa: N802 (chuẩn Qt)
        """Vào trang là tải lại — luôn thấy trạng thái mới nhất."""
        super().showEvent(event)
        self.refresh()

    def _fill(self) -> None:
        """Đổ thẻ thống kê + bảng người từ ``self._overview``."""
        overview = self._overview
        if overview is None:
            return
        now = datetime.now()
        self._date_label.setText(
            f"{now.strftime('%d/%m/%Y')} · "
            + ("Ngày làm việc" if overview.is_workday else "Ngoài ngày làm việc")
        )

        self._card_in.set_value(overview.checked_in)
        self._card_late.set_value(overview.late_count)
        self._card_missing.set_value(overview.missing_checkout)
        # Thẻ Phép/Công tác gộp 2 trạng thái đặt tay (leave + trip)
        self._card_leave.set_value(overview.leave_count + overview.trip_count)
        # Ngày nghỉ: cả đội chưa chấm là bình thường — thẻ về 0
        self._card_absent.set_value(
            overview.not_checked_in if overview.is_workday else 0
        )

        # Thẻ lương thô tháng này (tổng toàn công ty — mở rộng FR-8)
        self._fill_payroll_card(now)

        headers = ["Người", "Ca", "Giờ vào", "Giờ ra", "Trạng thái", "Ghi chú"]
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        rows = overview.rows
        self._table.setRowCount(len(rows))
        for r, person in enumerate(rows):
            self._table.setItem(r, 0, self._plain(person.person_name))
            self._table.setItem(r, 1, self._plain(person.shift_name))
            self._table.setItem(r, 2, self._plain(person.check_in_local or "—"))
            self._table.setItem(r, 3, self._plain(person.check_out_local or "—"))

            status_item = self._plain(person.label)
            color = self._status_color(person)
            if color is not None:
                status_item.setBackground(QColor(color))
            self._table.setItem(r, 4, status_item)
            note = ""
            if person.missing_checkout:
                note = "Bổ sung giờ ra ở trang Chấm công"
            self._table.setItem(r, 5, self._plain(note))

        self._table.resizeColumnsToContents()

    # ---------------------------------------------------------
    # Xuất nhanh báo cáo lương tháng (mở rộng FR-8)
    # ---------------------------------------------------------
    def _export_payroll_csv(self) -> None:
        """Xuất CSV (chi tiết công + khối lương thô) — hàm dùng chung của AttendanceView."""
        from app.ui.attendance_view import payroll_csv_rows  # noqa: PLC0415 — tránh vòng import lúc nạp module

        now = datetime.now()
        default_name = f"bang-cong-luong-{now.year}-{now.month:02d}.csv"
        path_str, _filter = QFileDialog.getSaveFileName(
            self, "Xuất CSV", default_name, "CSV (*.csv)"
        )
        if not path_str:
            return
        rows = payroll_csv_rows(
            self._service.payroll_summary(now.year, now.month),
            max(0, int(getattr(self._config, "attendance_pay_rate", 0) or 0)),
        )
        try:
            with Path(path_str).open("w", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh).writerows(rows)
        except OSError as exc:
            QMessageBox.warning(self, "Xuất CSV", f"Không ghi được file:\n{exc}")
            return
        logger.info("Đã xuất CSV lương từ Dashboard: %s", path_str)
        QMessageBox.information(self, "Xuất CSV", f"Đã xuất: {path_str}")

    def _export_payroll_pdf(self) -> None:
        """Lưu PDF (bảng công + trang bảng lương) — hàm dùng chung của AttendanceView."""
        from app.ui.attendance_view import payroll_pdf_html  # noqa: PLC0415 — tránh vòng import lúc nạp module

        from PySide6.QtGui import QTextDocument  # noqa: PLC0415
        from PySide6.QtPrintSupport import QPrinter  # noqa: PLC0415

        now = datetime.now()
        default_name = f"bang-cong-luong-{now.year}-{now.month:02d}.pdf"
        path_str, _filter = QFileDialog.getSaveFileName(
            self, "Xuất PDF", default_name, "PDF (*.pdf)"
        )
        if not path_str:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(path_str)
        html = self._attendance_pdf_html(payroll_pdf_html)
        doc = QTextDocument()
        doc.setHtml(html)
        doc.print_(printer)
        logger.info("Đã xuất PDF lương từ Dashboard: %s", path_str)
        QMessageBox.information(self, "Xuất PDF", f"Đã xuất: {path_str}")

    def _attendance_pdf_html(self, payroll_html_fn) -> str:
        """HTML báo cáo từ Dashboard: trang bảng công (ký hiệu ✓/!/P/T/V) + trang lương.

        Trang bảng công dựng theo cùng định dạng ``AttendanceView._pdf_html``
        nhưng dữ liệu tính độc lập qua ``month_summary`` (không phụ thuộc UI).
        """
        import calendar

        now = datetime.now()
        year, month = now.year, now.month
        summaries = self._service.month_summary(year, month)
        days_in_month = calendar.monthrange(year, month)[1]
        head_cells = "".join(f"<th>{d}</th>" for d in range(1, days_in_month + 1))
        body_rows: list[str] = []
        for summary in summaries:
            cells: list[str] = []
            for d in range(1, days_in_month + 1):
                key = f"{year:04d}-{month:02d}-{d:02d}"
                view = summary.days.get(key)
                color, text = "#000", "—"
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
        monthly_html = (
            f"<h2>Bảng công tháng {month:02d}/{year}</h2>"
            f"<p>{legend}</p>"
            "<table border='0.5' cellspacing='0' cellpadding='3'>"
            f"<tr><th>Người</th>{head_cells}</tr>{''.join(body_rows)}</table>"
        )
        payroll_html = payroll_html_fn(
            self._service.payroll_summary(year, month), year, month,
            max(0, int(getattr(self._config, "attendance_pay_rate", 0) or 0)),
        )
        return monthly_html + payroll_html

    def _fill_payroll_card(self, now: datetime) -> None:
        """Thẻ tổng lương toàn công ty tháng hiện tại (mở rộng FR-8).

        Có đơn giá → hiện số tiền VND, nhãn kèm cách bẻ (công quy đổi × đơn giá).
        Chưa đặt đơn giá → hiện công quy đổi + gợi ý nơi đặt đơn giá.
        """
        payroll = getattr(self, "_payroll", None) or []
        total_gross = round(sum(p.gross_pay for p in payroll), 2)
        total_amount = sum(p.pay_amount for p in payroll)
        rate = max(0, int(getattr(self._config, "attendance_pay_rate", 0) or 0))
        period = now.strftime("%m/%Y")
        if rate > 0:
            self._card_payroll.set_text(format_vnd(total_amount))
            self._card_payroll.set_label(
                f"Tháng {period} · {total_gross:g} công quy đổi × {format_vnd(rate)}/công"
            )
        else:
            self._card_payroll.set_text(f"{total_gross:g} công QĐ")
            self._card_payroll.set_label(
                f"Tháng {period} · chưa đặt đơn giá (Cài đặt → CHẤM CÔNG)"
            )

    @staticmethod
    def _status_color(person: PersonTodayStatus) -> str | None:
        """Màu trạng thái hôm nay — khớp chú giải bảng công tháng."""
        if person.missing_checkout:
            return COLOR_MISSING.name()  # thiếu giờ ra quan trọng hơn muộn
        if person.late_minutes > 0:
            return COLOR_LATE.name()
        if person.status == "leave":
            return COLOR_LEAVE.name()
        if person.status == "trip":
            return COLOR_TRIP.name()
        if person.check_in_local:
            return COLOR_PRESENT.name()
        if person.status == "absent":  # phòng hờ — hôm nay không suy ra vắng
            return COLOR_ABSENT.name()
        return None  # chưa chấm / ngày nghỉ — không tô

    @staticmethod
    def _plain(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item
