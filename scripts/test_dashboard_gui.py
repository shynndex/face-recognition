"""Kiểm tra GUI trang TỔNG QUAN (Dashboard) — minimal, tự thoát.

1. Service.today_overview(): đếm đúng đã vào / muộn / chưa giờ ra / chưa chấm.
2. Trang Dashboard: thẻ thống kê đúng số, bảng người có giờ vào/ra, màu trạng thái.
3. Ngày nghỉ (cuối tuần): thẻ Chưa chấm về 0, nhãn "Ngày nghỉ".
4. Refresh theo dữ liệu mới (mở app là thấy thay đổi).

Chạy:  .venv\\Scripts\\python.exe scripts/test_dashboard_gui.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import PySide6  # noqa: E402

pyside_dir = os.path.dirname(PySide6.__file__)
os.environ["QT_PLUGIN_PATH"] = os.path.join(pyside_dir, "plugins")
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(pyside_dir)
os.environ["QT_QPA_PLATFORM"] = "minimal"

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import Config  # noqa: E402
from app.infrastructure.db import Database  # noqa: E402
from app.infrastructure.repositories import PersonRepository  # noqa: E402
from app.services.attendance import AttendanceService  # noqa: E402
from app.ui.dashboard_view import DashboardView  # noqa: E402
from app.ui.main_window import PAGES  # noqa: E402

TEMP_DB = PROJECT_ROOT / "data" / "test_dashboard.db"

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


def iso_local(h: int, mi: int) -> str:
    """Giờ địa phương hôm nay (h:mi) → ISO UTC đúng quy ước DB."""
    now = datetime.now()
    return (
        datetime(now.year, now.month, now.day, h, mi)
        .astimezone()
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def main() -> int:
    cleanup()
    app = QApplication.instance() or QApplication(sys.argv)

    db = Database(TEMP_DB)
    config = Config()
    service = AttendanceService(db, config)
    people = PersonRepository(db)

    # ---- Nhóm 1: service.today_overview trên dữ liệu có kiểm soát ----
    print("== Nhóm 1: AttendanceService.today_overview ==")
    mai = people.add("Nguyễn Mai", "")
    service.on_event(mai.id, iso_local(8, 12))    # muộn 2 phút (ca 08:00 + 10')
    service.on_event(mai.id, iso_local(17, 30))   # đủ giờ ra
    lan = people.add("Trần Lan", "")
    service.on_event(lan.id, iso_local(7, 55))    # đúng giờ, chưa giờ ra
    people.add("Lê Bảo", "")                      # chưa chấm

    overview = service.today_overview()
    check("3 người đăng ký", overview.total == 3)
    check("2 người đã vào", overview.checked_in == 2)
    today = datetime.now().date()
    payroll_full = service.payroll_summary(today.year, today.month)  # nhóm lương
    check("1 người đi muộn", overview.late_count == 1)
    check("1 người chưa chấm (nếu hôm nay là ngày làm việc)",
          overview.not_checked_in == (1 if overview.is_workday else 0))
    labels = {r.person_name: r for r in overview.rows}
    check("Mai có giờ vào 08:12",
          labels["Nguyễn Mai"].check_in_local == "08:12",
          f"(={labels['Nguyễn Mai'].check_in_local})")
    check("Lan thiếu giờ ra", labels["Trần Lan"].missing_checkout)
    check("Bảo chưa chấm",
          labels["Lê Bảo"].label == ("Chưa chấm" if overview.is_workday else "Ngày nghỉ"))

    # ---- Nhóm 2: DashboardView hiển thị đúng ----
    print("== Nhóm 2: DashboardView ==")
    view = DashboardView(db, config=config)
    view.refresh()  # không đợi showEvent

    check("Thẻ Đã vào = 2", view._card_in._value.text() == "2")
    check("Thẻ Đi muộn = 1", view._card_late._value.text() == "1")
    check("Thẻ Chưa giờ ra = 1", view._card_missing._value.text() == "1")
    check("Thẻ Chưa chấm đúng ngữ cảnh ngày làm việc",
          view._card_absent._value.text() == ("1" if overview.is_workday else "0"))
    check("Bảng 3 dòng người", view._table.rowCount() == 3)
    names = [view._table.item(r, 0).text() for r in range(view._table.rowCount())]
    check("Bảng đủ tên người",
          {"Nguyễn Mai", "Trần Lan", "Lê Bảo"}.issubset(set(names)))
    row_mai = names.index("Nguyễn Mai")
    check("Giờ vào Mai hiển thị 08:12", view._table.item(row_mai, 2).text() == "08:12")
    check("Giờ ra Mai hiển thị 17:30", view._table.item(row_mai, 3).text() == "17:30")
    check("Trạng thái Mai tô màu (muộn hoặc có mặt)",
          view._table.item(row_mai, 4).background().color().name().lower()
          in ("#b26a00", "#2e7d32"))
    check("Ngày trong tiêu đề = hôm nay",
          datetime.now().strftime("%d/%m/%Y") in view._date_label.text())

    # ---- Nhóm 3: refresh theo dữ liệu mới ----
    print("== Nhóm 3: Làm mới theo dữ liệu mới ==")
    bao = people.get(labels["Lê Bảo"].person_id)
    service.on_event(bao.id, iso_local(21, 5))    # Bảo vào muộn (ca 08:00)
    view.refresh()
    check("Sau khi Bảo vào: thẻ Đã vào = 3", view._card_in._value.text() == "3")
    check("Thẻ Đi muộn = 2", view._card_late._value.text() == "2")
    check("Thẻ Chưa giờ ra = 2", view._card_missing._value.text() == "2")

    # ---- Nhóm 5: thẻ Lương thô tháng này (mở rộng FR-8) ----
    print("== Nhóm 5: Thẻ lương thô tháng ==")
    from app.services.attendance import format_vnd

    # Tính LẠI payroll sau MỌI sự kiện (Nhóm 3 đã cho Bảo check-in):
    # Mai 1 ngày × 1.0 + Lan 1 ngày (thiếu giờ ra vẫn tính công) × 1.0 +
    # Bảo 1 ngày (chỉ check-in) × 1.0 = 3.0 công quy đổi
    payroll_full = service.payroll_summary(today.year, today.month)
    total_gross = round(sum(p.gross_pay for p in payroll_full), 2)
    # Chưa đặt đơn giá → hiện công quy đổi + gợi ý
    view._fill_payroll_card(datetime.now())
    check("Chưa đơn giá → hiện công quy đổi",
          view._card_payroll._value.text() == f"{total_gross:g} công QĐ",
          f"({view._card_payroll._value.text()} / dự kiến {total_gross:g})")
    check("Chưa đơn giá → nhãn gợi ý Cài đặt",
          "chưa đặt đơn giá" in view._card_payroll._label.text())

    # Đặt đơn giá 350.000 → hiện tiền VND đúng tổng
    view._config.attendance_pay_rate = 350_000
    view.refresh()  # payroll_summary tính lại pay_amount với đơn giá mới
    expected_amount = sum(p.pay_amount for p in view._payroll)
    check("Có đơn giá → thẻ hiện tiền VND",
          view._card_payroll._value.text() == format_vnd(expected_amount),
          f"({view._card_payroll._value.text()} / dự kiến {format_vnd(expected_amount)})")
    check("Nhãn kèm công quy đổi × đơn giá",
          f"× {format_vnd(350_000)}/công" in view._card_payroll._label.text())
    view._config.attendance_pay_rate = 0  # trả lại như cũ

    # ---- Nhóm 6: Xuất nhanh CSV/PDF từ Dashboard — khớp trang Chấm công ----
    print("== Nhóm 6: Xuất nhanh từ Dashboard ==")
    from app.ui.attendance_view import AttendanceView, payroll_csv_rows, payroll_pdf_html
    from app.services.auth import AuthService

    # Bỏ QFileDialog: gọi trực tiếp phần dựng nội dung của handler Dashboard
    view._config.attendance_pay_rate = 350_000
    now = datetime.now()
    dash_rows = payroll_csv_rows(
        service.payroll_summary(now.year, now.month),
        view._config.attendance_pay_rate,
    )
    dash_csv = "\n".join(
        ",".join(str(cell) for cell in row) for row in dash_rows
    )
    check("CSV từ Dashboard có khối LƯƠNG THÔ", "LƯƠNG THÔ" in dash_csv)
    check("CSV từ Dashboard có dòng TỔNG TIỀN",
          "TỔNG TIỀN" in dash_csv
          and format_vnd(sum(p.pay_amount for p in payroll_full)) in dash_csv)

    # So khớp với AttendanceView (trang Chấm công) — cùng service, cùng config
    auth = AuthService(config)
    att_view = AttendanceView(db, auth, config=config)
    att_view._refresh()
    att_csv = "\n".join(
        ",".join(str(cell) for cell in row) for row in att_view._csv_rows()
    )
    # Khối lương của AttendanceView = payroll_csv_rows + 2 dòng mở đầu ([] + tiêu đề)
    att_payroll_block = "\n".join(
        ",".join(str(cell) for cell in row) for row in payroll_csv_rows(
            service.payroll_summary(now.year, now.month),
            view._config.attendance_pay_rate,
        )
    )
    check("Khối lương Dashboard ≡ khối lương trang Chấm công",
          att_payroll_block in att_csv and
          all(",".join(str(c) for c in r) in att_csv for r in dash_rows))

    # PDF: HTML từ Dashboard chứa bảng công + bảng lương, khớp hàm dùng chung
    dash_html = view._attendance_pdf_html(payroll_pdf_html)
    check("HTML Dashboard có bảng công tháng", "Bảng công tháng" in dash_html)
    check("HTML Dashboard có trang bảng lương thô",
          "Bảng lương thô tháng" in dash_html
          and "page-break-before" in dash_html)
    check("HTML Dashboard khớp hàm dùng chung (trang lương)",
          payroll_pdf_html(
              service.payroll_summary(now.year, now.month), now.year, now.month,
              view._config.attendance_pay_rate,
          ) in dash_html)
    view._config.attendance_pay_rate = 0  # trả lại

    # ---- Nhóm 4: PAGES — Dashboard đầu tiên, mở app là thấy ----
    print("== Nhóm 4: PAGES ==")
    check("Dashboard là trang đầu", PAGES[0][1] == "DashboardView", f"({PAGES[0][1]})")
    check("Trang Chấm công vẫn có sau Lịch sử",
          [p[1] for p in PAGES].index("AttendanceView")
          > [p[1] for p in PAGES].index("HistoryView"))
    check("Sidebar có nhãn Tổng quan", any(p[0] == "Tổng quan" for p in PAGES))

    db.close()
    cleanup()
    print(f"\nKết quả: {PASS} QUA / {FAIL} THAT")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
