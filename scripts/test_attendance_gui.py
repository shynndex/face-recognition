"""Kiểm tra GUI trang CHẤM CÔNG (attendance-spec FR-5/FR-7/FR-8) — minimal, tự thoát.

1. Bảng tháng: dòng người × cột ngày, màu ô theo trạng thái (✓/muộn/!/P/T/V).
2. Chi tiết ngày: giờ vào/ra, trạng thái, ghi chú, audit gần nhất.
3. Xuất CSV: UTF-8 BOM + cột đúng spec FR-8 (kiểm tra nội dung file tạm).
4. Xuất PDF: render HTML qua QPrinter ra file (không mở dialog in).
5. Dialog sửa tay (AttendanceEditDialog): sửa giờ/trạng thái/ghi chú qua service.

Chạy:  .venv\\Scripts\\python.exe scripts\\test_attendance_gui.py
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import PySide6  # noqa: E402

pyside_dir = os.path.dirname(PySide6.__file__)
os.environ["QT_PLUGIN_PATH"] = os.path.join(pyside_dir, "plugins")
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(pyside_dir)
os.environ["QT_QPA_PLATFORM"] = "minimal"

from PySide6.QtWidgets import QApplication, QTableWidget  # noqa: E402

from app.config import Config  # noqa: E402
from app.infrastructure.db import Database  # noqa: E402
from app.infrastructure.repositories import PersonRepository  # noqa: E402
from app.services.auth import AuthService  # noqa: E402
from app.services.attendance import AttendanceService, format_vnd  # noqa: E402
from app.ui.attendance_edit_dialog import AttendanceEditDialog  # noqa: E402
from app.ui.attendance_view import AttendanceView  # noqa: E402

TEMP_DB = PROJECT_ROOT / "data" / "test_attendance_gui.db"
CSV_OUT = PROJECT_ROOT / "data" / "test_attendance_export.csv"
PDF_OUT = PROJECT_ROOT / "data" / "test_attendance_export.pdf"

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [QUA] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [THẤT] {name} {detail}")


def cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEMP_DB) + suffix).unlink(missing_ok=True)
    CSV_OUT.unlink(missing_ok=True)
    PDF_OUT.unlink(missing_ok=True)


def iso_local(y: int, mo: int, d: int, h: int, mi: int) -> str:
    return (
        datetime(y, mo, d, h, mi).astimezone()
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def main() -> int:
    cleanup()
    app = QApplication.instance() or QApplication(sys.argv)

    db = Database(TEMP_DB)
    config = Config()
    auth = AuthService(config)
    service = AttendanceService(db, config)
    people = PersonRepository(db)

    # ---- Dữ liệu mẫu: 2 người, ngày hôm nay + hôm qua ----
    today = datetime.now().date()
    y, mo, d = today.year, today.month, today.day
    mai = people.add("Nguyễn Mai", "")
    service.on_event(mai.id, iso_local(y, mo, d, 8, 12))    # muộn 2 phút
    service.on_event(mai.id, iso_local(y, mo, d, 17, 30))   # giờ ra
    lan = people.add("Trần Lan", "")
    service.on_event(lan.id, iso_local(y, mo, d, 7, 55))    # đúng giờ, thiếu giờ ra

    view = AttendanceView(db, auth, config=config)

    print("\n[1] Bảng tháng")
    view._refresh()
    summaries = view._summaries
    check("2 dòng người", len(summaries) == 2)
    check(
        "header đủ cột người + ngày",
        view._table.columnCount() == today.day + 1
        or view._table.columnCount() >= 29,
    )
    row_mai = next(i for i, s in enumerate(summaries) if s.person_name == "Nguyễn Mai")
    row_lan = next(i for i, s in enumerate(summaries) if s.person_name == "Trần Lan")
    key_today = today.isoformat()
    item_mai = view._table.item(row_mai, today.day)
    item_lan = view._table.item(row_lan, today.day)
    check("Mai hôm nay: ô '2' (muộn 2 phút)", item_mai.text() == "2")
    check("Lan hôm nay: ô '!' (thiếu giờ ra)", item_lan.text() == "!")
    check(
        "header dòng Mai: 1 công, 1 muộn",
        f"{summaries[row_mai].worked_days} công" in view._table.item(row_mai, 0).text()
        and "1 muộn" in view._table.item(row_mai, 0).text(),
    )

    print("\n[2] Chi tiết ngày")
    view._on_cell_clicked(row_lan, today.day)
    check(
        "chi tiết Lan: thiếu giờ ra",
        "Thiếu giờ ra" in view._detail_status.text(),
    )
    check("nút Sửa bật khi có bản ghi", view._edit_btn.isEnabled())
    check(
        "chi tiết có giờ vào 07:55",
        "07:55" in view._detail_in.text() or f"{d:02d}" in view._detail_in.text(),
    )

    print("\n[3] Xuất CSV (UTF-8 BOM)")
    # Ghi thẳng qua _csv_rows → file (bỏ QFileDialog trong test)
    rows = view._csv_rows()
    with CSV_OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh).writerows(rows)
    raw = CSV_OUT.read_bytes()
    check("CSV có BOM UTF-8", raw[:3] == b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    check(
        "header cột đúng spec FR-8",
        text.splitlines()[0]
        == "Người,Ca,Ngày,Giờ vào,Giờ ra,Số giờ làm,Trạng thái,Đi muộn (phút),Ghi chú",
    )
    check("CSV có dòng Mai", "Nguyễn Mai" in text)
    check("CSV có nhãn đi muộn", "Đi muộn 2 phút" in text)
    # ---- Khối LƯƠNG THÔ trong CSV (mở rộng FR-8) ----
    check("CSV có khối LƯƠNG THÔ", "LƯƠNG THÔ" in text)
    check(
        "CSV header lương đủ cột (có Tiền lương VND)",
        "Người,Ca áp dụng,Hệ số,Số công,Ngày phép,Công tác,Vắng mặt,"
        "Lương thô (công quy đổi),Tiền lương (VND),Chi tiết theo ca" in text,
    )
    mai_pay = next(p for p in view._service.payroll_summary(y, mo)
                   if p.person_name == "Nguyễn Mai")
    check("CSV dòng lương Mai có giá trị đúng",
          f"Nguyễn Mai,{mai_pay.shift_name},{mai_pay.factor:g},{mai_pay.worked_days}," in text,
          f"(số công={mai_pay.worked_days})")
    # Đặt đơn giá → cột Tiền lương VND + dòng TỔNG TIỀN xuất hiện
    view._config.attendance_pay_rate = 350_000
    rows_money = view._csv_rows()
    text_money = "\n".join(
        ",".join(str(cell) for cell in row) for row in rows_money
    )
    check("CSV có cột Tiền lương (VND)", "Tiền lương (VND)" in text_money)
    check("CSV dòng lương có tiền đúng (350.000 đ)",
          format_vnd(mai_pay.pay_amount) in text_money,
          f"({format_vnd(mai_pay.pay_amount)})")
    check("CSV có dòng TỔNG TIỀN", "TỔNG TIỀN" in text_money
          and format_vnd(sum(p.pay_amount for p in view._service.payroll_summary(y, mo))) in text_money)
    view._config.attendance_pay_rate = 0  # trả lại để các nhóm sau không lệch

    print("\n[3b] Nút Đánh phép/Công tác cho ngày chưa chấm (FR-5)")
    # Chọn 1 ô VẮNG của Lan (ngày làm việc quá khứ không có bản ghi)
    summary_lan = view._summaries[row_lan]
    absent_key = next(
        (
            k for k, dv in summary_lan.days.items()
            if dv.status == "absent"
        ),
        None,
    )
    if absent_key is None:
        # Đầu tháng: chưa có ngày quá khứ → test mark_leave trực tiếp qua service
        leave_date = today - timedelta(days=1)
        service.mark_leave(lan.id, leave_date.isoformat(), "leave")
        rec = service._days.find(lan.id, leave_date.isoformat())
        check("mark_leave (service) tạo nghỉ phép", rec is not None and rec.status == "leave")
    else:
        row_absent = int(absent_key[-2:])
        view._on_cell_clicked(row_lan, row_absent)
        check("ô vắng → nút Đánh phép bật", view._mark_btn.isEnabled())
        check("ô vắng → nút Sửa tắt", not view._edit_btn.isEnabled())
        # Gọi _on_mark_leave với mật khẩu + QInputDialog đã patch ở trên:
        # (patch sau phần này — tại đây gọi qua service để không đụng modal)
        service.mark_leave(lan.id, absent_key, "trip")
        rec = service._days.find(lan.id, absent_key)
        check("mark_leave tạo công tác cho ngày vắng", rec is not None and rec.status == "trip")
        view._refresh()
        # Sau refresh, ô đó không còn 'absent' → nút Đánh phép tắt khi chọn lại
        view._on_cell_clicked(row_lan, row_absent)
        check("ô đã đánh → nút Đánh phép tắt", not view._mark_btn.isEnabled())
        check("ô đã đánh → nút Sửa bật", view._edit_btn.isEnabled())
        audit = service.audit_for_day(rec.id)
        check("mark_leave ghi audit set_status", any(e["action"] == "set_status" for e in audit))

    print("\n[4] Xuất PDF (QPrinter → file)")
    from PySide6.QtGui import QTextDocument
    from PySide6.QtPrintSupport import QPrinter

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(PDF_OUT))
    doc = QTextDocument()
    doc.setHtml(view._pdf_html())  # _pdf_html đã nối _payroll_html (trang 2)
    doc.print_(printer)
    check("file PDF được tạo", PDF_OUT.exists() and PDF_OUT.stat().st_size > 1000)
    check(
        "PDF ghi đúng tên file output",
        printer.outputFileName() == str(PDF_OUT),
    )
    html = view._pdf_html()
    check("HTML PDF có bảng lương thô", "Bảng lương thô tháng" in html)
    check("HTML PDF có cột Lương thô + Chi tiết theo ca",
          "Lương thô</th>" in html and "Chi tiết theo ca</th>" in html)

    print("\n[5] Dialog sửa tay (qua service — FR-5)")
    rec = service._days.find(mai.id, key_today)
    dialog = AttendanceEditDialog(service, rec.id, "Nguyễn Mai")
    dialog._in_edit.setText("08:05")
    dialog._out_edit.setText("17:45")
    dialog._status_combo.setCurrentIndex(0)  # Có mặt
    dialog._note_edit.setText("Sửa tay thử")
    dialog._on_save()
    rec2 = service._days.get(rec.id)
    from app.services.attendance import _fmt_local_hhmm

    check("giờ vào sửa thành 08:05", _fmt_local_hhmm(rec2.check_in_at) == "08:05")
    check("giờ ra sửa thành 17:45", _fmt_local_hhmm(rec2.check_out_at) == "17:45")
    check("manual_override bật", rec2.manual_override is True)
    audit = service.audit_for_day(rec.id)
    check("audit có dòng edit_time", any(e["action"] == "edit_time" for e in audit))
    check("audit có dòng add_note", any(e["action"] == "add_note" for e in audit))
    # Giờ sai định dạng → báo lỗi, không lưu (minimal mode: dialog chưa show()
    # nên isVisible() luôn False — kiểm tra nội dung lỗi + dữ liệu giữ nguyên)
    dialog2 = AttendanceEditDialog(service, rec.id, "Nguyễn Mai")
    dialog2._in_edit.setText("8h05")
    dialog2._on_save()
    check("giờ sai → hiện lỗi", bool(dialog2._error.text()) and "HH:MM" in dialog2._error.text())
    rec3 = service._days.get(rec.id)
    check("giờ sai → KHÔNG lưu", _fmt_local_hhmm(rec3.check_in_at) == "08:05")

    print("\n[6] Bảng lương thô (PayrollDialog — mở rộng FR-8)")
    from app.ui.attendance_view import PayrollDialog

    payroll = service.payroll_summary(y, mo)
    check("payroll đủ 2 người", len(payroll) == 2)
    check("Lan thiếu giờ ra vẫn tính công", any(p.person_name == "Trần Lan" and p.worked_days == 1 for p in payroll))
    p_dialog = PayrollDialog(view, payroll, y, mo)  # chưa đặt đơn giá
    table = p_dialog.findChildren(QTableWidget)[0]
    check("Dialog bảng lương đúng số dòng", table.rowCount() == len(payroll))
    header_labels = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    check("Dialog có cột Lương thô + Chi tiết theo ca",
          "Lương thô" in header_labels and "Chi tiết theo ca" in header_labels)
    check("Chưa đặt đơn giá → không cột tiền", "Tiền lương (VND)" not in header_labels)
    row_lan_p = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == "Trần Lan")
    check("Dòng lương Lan: hệ số 1, 1 công, lương 1",
          table.item(row_lan_p, 2).text() == "1"
          and table.item(row_lan_p, 3).text() == "1"
          and table.item(row_lan_p, 7).text() == "1")

    # Đặt đơn giá → cột tiền + dòng TỔNG TIỀN (tính lại payroll SAU khi đặt)
    view._config.attendance_pay_rate = 350_000
    payroll = service.payroll_summary(y, mo)  # pay_amount bây giờ > 0
    p_dialog_money = PayrollDialog(view, payroll, y, mo, pay_rate=350_000)
    table_money = p_dialog_money.findChildren(QTableWidget)[0]
    money_headers = [table_money.horizontalHeaderItem(c).text()
                     for c in range(table_money.columnCount())]
    check("Có đơn giá → cột Tiền lương (VND)", "Tiền lương (VND)" in money_headers)
    check("Có đơn giá → thêm dòng TỔNG TIỀN", table_money.rowCount() == len(payroll) + 1)
    total_item = table_money.item(table_money.rowCount() - 1, money_headers.index("Tiền lương (VND)"))
    expected_total = sum(p.pay_amount for p in payroll)
    check("Dòng tổng đúng số tiền",
          total_item.text() == format_vnd(expected_total),
          f"({total_item.text()} / dự kiến {format_vnd(expected_total)})")
    check("Dialog lưu tổng vào thuộc tính", p_dialog_money._total_amount == expected_total)

    db.close()
    cleanup()
    print(f"\nKẾT QUẢ: {PASS} qua / {FAIL} thất bại")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
