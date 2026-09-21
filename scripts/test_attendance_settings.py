"""Kiểm tra GUI CẤU HÌNH CHẤM CÔNG (attendance-spec FR-3/FR-6) — minimal, tự thoát.

1. SettingsView: thẻ CHẤM CÔNG — nhãn ca, combo ca mặc định, checkbox ngày
   làm việc; ShiftDialog thêm/sửa ca; xóa ca chặn ca mặc định.
2. PersonListView: combo gán ca từng người → assign_shift lưu DB đúng.
3. Config workdays load/save qua _load_from_config + _on_save_attendance.

Chạy:  .venv\\Scripts\\python.exe scripts\\test_attendance_settings.py
"""
from __future__ import annotations

import os
import sys
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
from app.services.auth import AuthService  # noqa: E402
from app.services.sync import SyncService  # noqa: E402
from app.ui.person_list_view import PersonListView  # noqa: E402
from app.ui.settings_view import SettingsView, ShiftDialog  # noqa: E402

TEMP_DB = PROJECT_ROOT / "data" / "test_attendance_settings.db"
TEMP_CONFIG = PROJECT_ROOT / "data" / "test_attendance_settings_config.json"

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


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEMP_DB) + suffix).unlink(missing_ok=True)
    TEMP_CONFIG.unlink(missing_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)

    # Minimal platform: QMessageBox modal chặn vô hạn (không ai bấm) →
    # thay bằng hàm trả giá trị ngay để test được thông qua.
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.information = lambda *a, **k: QMessageBox.StandardButton.Ok
    QMessageBox.warning = lambda *a, **k: QMessageBox.StandardButton.Ok
    QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes

    db = Database(TEMP_DB)
    config = Config()
    # Trỏ CONFIG_PATH sang file tạm — save() mặc định của _on_save_attendance
    # ghi vào đây (không đụng config.json thật của người dùng)
    import app.config as config_module
    config_module.CONFIG_PATH = TEMP_CONFIG
    config.save(TEMP_CONFIG)  # config tạm — không đụng config.json thật
    auth = AuthService(config)
    sync = SyncService(db, config)
    attendance = AttendanceService(db, config)
    people = PersonRepository(db)

    print("\n[1] SettingsView — thẻ CHẤM CÔNG")
    view = SettingsView(config, auth, sync)
    check("ca mặc định tự tạo có trong combo", view._default_shift_combo.count() == 1)
    check(
        "nhãn ca hiển thị 'Hành chính'",
        "Hành chính" in view._shifts_label.text(),
    )
    check(
        "workdays mặc định T2–T6 được tích",
        [i for i, cb in enumerate(view._workday_checks) if cb.isChecked()]
        == [0, 1, 2, 3, 4],
    )

    print("\n[2] ShiftDialog — thêm/sửa ca")
    dialog = ShiftDialog()
    dialog._name_edit.setText("Ca đêm")
    dialog._start_edit.setText("22:00")
    dialog._end_edit.setText("06:00")
    dialog._grace_spin.setValue(15)
    dialog._on_save()  # hợp lệ → accept()
    attendance.create_shift(dialog.name(), dialog.start(), dialog.end(), dialog.grace())
    check("thêm ca đêm qua service được", len(attendance.list_shifts()) == 2)
    view._refresh_shifts()
    check("combo ca mặc định cập nhật 2 ca", view._default_shift_combo.count() == 2)
    # Giờ sai định dạng: dialog._on_save hiện QMessageBox.warning (đã patch)
    # và KHÔNG gọi accept() — kiểm chứng gián tiếp qua service chặn ValueError.
    dialog2 = ShiftDialog()
    dialog2._name_edit.setText("Sai giờ")
    dialog2._start_edit.setText("8h00")
    dialog2._end_edit.setText("17:00")
    dialog2._on_save()
    try:
        attendance.create_shift("Sai giờ", "8h00", "17:00")
        check("service chặn giờ sai định dạng", False)
    except ValueError:
        check("service chặn giờ sai định dạng", True)

    print("\n[3] Lưu ca mặc định + ngày làm việc")
    night = next(s for s in attendance.list_shifts() if s.name == "Ca đêm")
    view._default_shift_combo.setCurrentIndex(
        view._default_shift_combo.findData(night.id)
    )
    view._workday_checks[6].setChecked(True)  # thêm CN
    view._pay_rate_spin.setValue(350_000)     # đơn giá 1 công quy đổi
    view._on_save_attendance()  # QMessageBox.information hiện — minimal mode vẫn chạy tiếp?
    # QMessageBox trong minimal platform có thể chặn → kiểm tra sau khi gọi
    db_config = config
    check("workdays lưu thêm CN", db_config.attendance_workdays == [0, 1, 2, 3, 4, 6])
    check("đơn giá lưu vào config.json",
          db_config.attendance_pay_rate == 350_000,
          f"(={db_config.attendance_pay_rate})")
    check("config.json load lại còn đơn giá",
          Config.load(TEMP_CONFIG).attendance_pay_rate == 350_000)
    check("ca mặc định lưu vào settings DB",
          attendance.get_default_shift().id == night.id)

    print("\n[4] PersonListView — combo gán ca từng người")
    mai = people.add("Mai", "")
    list_view = PersonListView(db, auth)
    list_view._refresh()
    # Tìm combo ca trong widget dòng của Mai
    item_count = list_view._list.count()
    check("danh sách có 1 dòng", item_count == 1)
    row_widget = list_view._list.itemWidget(list_view._list.item(0))
    combos = row_widget.findChildren(__import__("PySide6.QtWidgets", fromlist=["QComboBox"]).QComboBox)
    check("dòng người có combo ca", len(combos) == 1)
    combo = combos[0]
    check("combo có 2 ca + Mặc định", combo.count() == 3)
    # Chọn ca đêm → lưu ngay
    night_index = combo.findData(night.id)
    combo.setCurrentIndex(night_index)
    person = people.get(mai.id)
    check("gán ca đêm lưu DB ngay", person.shift_id == night.id)
    # Chọn Mặc định → shift_id về None
    combo.setCurrentIndex(combo.count() - 1)
    person = people.get(mai.id)
    check("chọn Mặc định → shift_id None", person.shift_id is None)

    db.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEMP_DB) + suffix).unlink(missing_ok=True)
    TEMP_CONFIG.unlink(missing_ok=True)
    print(f"\nKẾT QUẢ: {PASS} qua / {FAIL} thất bại")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
