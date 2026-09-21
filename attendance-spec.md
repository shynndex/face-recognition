# Spec: Module chấm công (Attendance)

> Trạng thái: **DONE** — đã triển khai đủ FR-1…FR-9 (ngày 2026-09-21, xem bảng trạng thái ở mục 0).
> Mục tiêu của spec: bổ sung **tầng domain chấm công** đè lên dữ liệu nhận diện đã có —
> bản ghi vào/ra theo ngày, ca làm việc, đi muộn, nghỉ phép, sửa tay + audit, báo cáo CSV/PDF.

---

## 0. Trạng thái triển khai (cập nhật 2026-09-21 — đã đối chiếu mã trên đĩa)

| FR | Nội dung | Trạng thái | File |
|---|---|---|---|
| FR-1 | 3 bảng mới + `persons.shift_id`, migration v1→v2→**v3** (`shifts.factor`) | ✅ Xong | `app/infrastructure/db.py`, `app/infrastructure/d1_client.py`, `app/infrastructure/repositories.py` |
| FR-2 | `on_event` ghép check-in/out, ánh xạ `work_date` theo cửa sổ ca (ca đêm về ngày bắt đầu) | ✅ Xong | `app/services/attendance.py`, nối tại `app/services/recognition.py` (`save_event`) + `app/services/sync.py` (`_pull`) |
| FR-3 | CRUD ca (kèm **hệ số lương ca**), gán ca theo người, ca mặc định "Hành chính" tự tạo | ✅ Xong | `app/ui/settings_view.py` (`ShiftDialog` + ô Hệ số lương, thẻ CHẤM CÔNG), `app/ui/person_list_view.py` (combo gán ca), `app/services/attendance.py`, `app/infrastructure/repositories.py` (`PersonRepository.set_shift`) |
| FR-4 | Trạng thái + đi muộn + thiếu giờ ra + vắng mặt suy ra khi render | ✅ Xong | `app/services/attendance.py` |
| FR-5 | Sửa tay + `PasswordDialog.require` + audit cũ→mới + chống ghi đè `manual_override` | ✅ Xong | `app/ui/attendance_view.py`, `app/ui/attendance_edit_dialog.py`, `app/services/attendance.py` |
| FR-6 | Cài đặt → CHẤM CÔNG: ca mặc định, ngày làm việc `attendance_workdays` | ✅ Xong | `app/ui/settings_view.py`, `app/config.py` |
| FR-7 | Trang "Chấm công": bảng tháng màu ô, chi tiết ngày, tổng hợp; kèm nút Đánh phép/Công tác cho ngày chưa chấm | ✅ Xong | `app/ui/attendance_view.py`, `app/ui/main_window.py` (`PAGES`) |
| FR-8 | Xuất CSV UTF-8 BOM + PDF (QPrinter + QTextDocument) **+ bảng lương thô** (`PayrollDialog`, khối lương trong CSV, trang 2 PDF, đơn giá VND) | ✅ Xong | `app/ui/attendance_view.py`, `app/config.py` (`attendance_pay_rate`) |
| FR-9 | Sync D1: push/pull `shifts` + `attendance_days` (audit không sync), outbox entity `shift`/`attendance_day` | ✅ Xong | `app/services/sync.py`, `app/infrastructure/d1_client.py` |

**Test:** `scripts/test_attendance.py` (37 — schema v3/CRUD ca/ghép sự kiện/ca đêm/trạng thái/audit/migration v1), `scripts/test_attendance_gui.py` (45 — bảng tháng/CSV+khối lương/PDF 2 trang/dialog sửa/đánh phép/PayrollDialog), `scripts/test_attendance_wiring.py` (11 — 3 điểm nối FR-2), `scripts/test_attendance_settings.py` (15 — FR-3/FR-6/đơn giá), `scripts/test_attendance_sync.py` (17 — FR-9, gồm SQL upsert thật lên SQLite), `scripts/test_payroll.py` (24 — migration v2→v3/lương thô/đơn giá/format_vnd), `scripts/test_dashboard_gui.py` (33 — Dashboard + thẻ lương + **xuất nhanh CSV/PDF khớp trang Chấm công**). Các test cũ (repository, auth, matcher, step_02/03/08/09/10/11/14/15/16/17/18/19/21/26) đều pass.

### Mở rộng đã triển khai ngoài spec gốc (2026-09-21)

| Phần | Nội dung | File chính |
|---|---|---|
| **Lương thô** (schema v3) | Cột `shifts.factor` REAL DEFAULT 1.0 (CHECK > 0) ở SQLite + D1, migration v2→v3 (ALTER — ca cũ nhận 1.0); `AttendanceService.payroll_summary` → `PersonPayroll`: lương = Σ (số công mỗi ca × hệ số ca đó), đếm theo `shift_id` ĐÃ LƯU từng ngày (sửa hệ số không đổi tháng cũ; xóa ca → hệ số quy 1.0 nhưng ngày vẫn giữ); phép/công tác/vắng không lương, thiếu giờ ra vẫn tính công | `app/infrastructure/db.py`, `app/infrastructure/d1_client.py`, `app/infrastructure/repositories.py` (`Shift.factor`), `app/services/attendance.py` |
| **Đơn giá VND** | `config.attendance_pay_rate` (0 = chưa đặt) — `pay_amount = gross_pay × đơn giá`; `format_vnd()` → `12.500.000 đ`; ô "Đơn giá 1 công (VND)" trong Cài đặt → CHẤM CÔNG | `app/config.py`, `app/ui/settings_view.py`, `app/services/attendance.py` |
| **PayrollDialog + xuất** | Nút [ Bảng lương ] trên trang Chấm công: bảng 9–10 cột (Người · Ca áp dụng · Hệ số · Số công · Phép · Công tác · Vắng · Lương thô · Tiền lương (VND) · Chi tiết theo ca) + dòng TỔNG TIỀN (đủ 10 cột — tiền nằm đúng cột); CSV thêm khối `LƯƠNG THÔ` (UTF-8 BOM); PDF thêm trang 2 bảng lương. **Xuất nhanh từ Dashboard** dùng chung 2 hàm `payroll_csv_rows` / `payroll_pdf_html` → file từ 2 nơi khớp từng ô | `app/ui/attendance_view.py`, `app/ui/dashboard_view.py` |
| **Dashboard hôm nay** | Trang "Tổng quan" (mục đầu sidebar, `PAGES` 8 mục) — 5 thẻ trạng thái hôm nay + thẻ **"Lương thô tháng này"** (tổng tiền toàn công ty, nhãn kèm công quy đổi × đơn giá; chưa đặt đơn giá → hiện công QĐ + gợi ý) kèm 2 nút **[ Xuất CSV ] / [ Xuất PDF ]** xuất nhanh báo cáo lương ngay từ Dashboard | `app/ui/dashboard_view.py`, `app/ui/main_window.py`, `app/services/attendance.py` (`today_overview`) |

---

## 1. Bối cảnh / hiện trạng (đã đọc code)

| Thành phần | Vị trí | Hành vi hiện tại |
|---|---|---|
| Sự kiện nhận diện | `app/infrastructure/repositories.py` (`recognition_events`) | `person_id`, `label`, `source` (webcam/photo/mobile), `detected_at` (UTC), `similarity`, snapshot, `is_unknown`. Sự kiện BẤT BIẾN, không có khái niệm "ngày công". |
| Ghi sự kiện | `app/ui/camera_view.py` (debounce 5s/người, dòng ~433), `photo_view.py`, sync pull | Mỗi lần nhận diện (sau debounce) ghi 1 sự kiện rời rạc. Debounce là chống tràn DB, **không phải** ngữ nghĩa chấm công. |
| Xem lịch sử | `app/services/history.py`, `app/ui/history_view.py` | Liệt kê/lọc/xóa sự kiện theo tên, nguồn, ngày (Hôm nay/7/30). Không tổng hợp thành công. |
| Người | `app/infrastructure/repositories.py` (`persons`), `app/services/person.py` | Chỉ có `name` + thumbnail + thời điểm nhận diện gần nhất. Không có phòng ban, ca. |
| Ca / giờ quy định | — | Không tồn tại. Không tính được đi muộn / về sớm / số giờ làm. |
| Xuất báo cáo | — | Không có bất kỳ xuất CSV/Excel/PDF nào trong code (đã tìm kiếm). |
| Sửa dữ liệu tay | — | Chỉ sửa tên / xóa người / xóa sự kiện. Không có sửa giờ chấm công, không có nghỉ phép. |
| Xác thực nhạy cảm | `app/ui/password_dialog.py` (`PasswordDialog.require`) | Dùng sẵn cho xóa/sửa người, xóa lịch sử — **tái dùng được** cho sửa bản ghi công. |
| Đồng bộ | `app/services/sync.py` (outbox pattern) | Đẩy/kéo `persons`, `face_samples`, `recognition_events`. Bảng mới phải thêm vào schema + outbox + pull. |

**Khoảng cách chính so với phần mềm chấm công:** sự kiện rời rạc chưa ghép thành bản ghi ngày công;
không có ca/giờ quy định; không phân loại trạng thái; không sửa tay khi thiếu sót; không có báo cáo xuất file.

---

## 2. Mục tiêu & ngoài phạm vi (theo phỏng vấn)

### Trong phạm vi
1. **Màn "Chấm công"** (thêm vào sidebar) — bảng ngày công theo người × ngày trong tháng.
2. **Tự động ghép** check-in/check-out từ sự kiện nhận diện đã có (không thêm thao tác bấm).
3. **Ca làm việc tự tạo** (tên, giờ vào, giờ ra, dung sai trễ) + **gán theo người**, có ca mặc định.
4. **Trạng thái ngày**: có mặt / đi muộn (X phút) / thiếu giờ ra / nghỉ phép / công tác / vắng mặt.
5. **Sửa tay** giờ vào/ra + đánh nghỉ phép/công tác, mỗi lần sửa yêu cầu mật khẩu (`PasswordDialog.require`) và **ghi audit log**.
6. **Xuất báo cáo** tháng: CSV (mở được bằng Excel) + PDF.
7. Cấu hình **Cài đặt → CHẤM CÔNG**: quản lý ca, ngày làm việc trong tuần, ca mặc định.

### Ngoài phạm vi (đã chốt KHÔNG làm)
- Phân quyền quản lý/nhân viên (vẫn 1 mật khẩu chung — sửa tay dùng `PasswordDialog`).
- Phòng ban / tổ nhóm.
- Tính lương, phụ cấp, làm thêm giờ tự động.
- GPS / IP / ràng buộc thiết bị khi chấm.
- Luồng "xin nghỉ phép → duyệt" (nghỉ phép được nhập trực tiếp khi sửa tay).
- Nút chấm công chủ động trên webcam (đã chốt: tự động từ nhận diện).
- Đồng bộ audit log lên cloud (audit là log cục bộ).

---

## 3. Yêu cầu chức năng (FR)

### FR-1 — Mô hình dữ liệu (3 bảng mới)
Bổ sung vào **cả** schema SQLite local (`app/infrastructure/db.py`) lẫn D1 (`app/infrastructure/d1_client.py`) — `CREATE TABLE IF NOT EXISTS` nên DB cũ không mất dữ liệu:

- `shifts` — ca làm việc:
  - `id` TEXT PK (UUID), `name` TEXT NOT NULL (duy nhất), `start_time` TEXT 'HH:MM', `end_time` TEXT 'HH:MM',
    `grace_minutes` INTEGER (dung sai trễ, mặc định 10), `created_at` TEXT UTC.
  - Ca đêm (kết thúc sau nửa đêm) = `end_time <= start_time` — suy ra được, không cần cột riêng.
- `attendance_days` — **1 dòng / người / ngày** (bản ghi công):
  - `id` TEXT PK (UUID), `person_id` TEXT NOT NULL, `work_date` TEXT 'YYYY-MM-DD' (theo **ngày bắt đầu ca**),
    `shift_id` TEXT (ca áp dụng tại thời điểm tính), `check_in_at` TEXT UTC NULL, `check_out_at` TEXT UTC NULL,
    `status` TEXT: `auto` (có mặt, tự sinh) / `leave` (nghỉ phép) / `trip` (công tác) / `manual` (đã sửa tay),
    `manual_override` INTEGER 0/1, `note` TEXT, `updated_at` TEXT UTC.
  - Đi muộn **không lưu cột riêng** — tính lúc hiển thị từ `check_in_at` + ca (tránh lệch khi sửa ca).
- `attendance_audit` — lịch sử sửa tay (cục bộ, KHÔNG sync):
  - `id`, `attendance_day_id`, `action` ('edit_time' / 'set_status' / 'add_note'),
    `old_value` TEXT, `new_value` TEXT, `edited_at` TEXT UTC, `detail` TEXT.
  - UNIQUE(person_id, work_date) trên `attendance_days` để chống trùng dòng.

### FR-2 — Ghép sự kiện → ngày công (tự động)
- Điểm ghi sự kiện nhận diện (webcam `camera_view._record_event`, ảnh `photo_view`, và **pull từ cloud**)
  gọi thêm `AttendanceService.on_event(person_id, detected_at_utc)` — chỉ với người đã đăng ký (`person_id` không NULL).
- Quy tắc trong 1 ngày: sự kiện **đầu tiên** = check-in; sự kiện **cuối cùng** = check-out.
  Sự kiện mới đến → nếu ngày chưa có dòng: tạo dòng (check_in = detected_at);
  nếu đã có: cập nhật `check_out = detected_at` (khi mới hơn check_in), status giữ `auto`.
- **Ánh xạ sự kiện → ngày công (work_date)**: đổi `detected_at` UTC sang giờ **địa phương**; nếu giờ địa phương
  thuộc cửa sổ ca bắt đầu ngày D (xem FR-3) → `work_date = D`. Ca thường: cửa sổ = [start_time, end_time] ngày D.
  Ca đêm: cửa sổ = [start_time ngày D → end_time ngày D+1] — toàn bộ sự kiện trong cửa sổ thuộc **ngày D**.
- Giờ lưu UTC giữ nguyên quy ước hiện tại; mọi hiển thị / nhập tay theo giờ địa phương 'HH:MM'.
- `work_date` hiển thị "Thiếu giờ ra" nếu có check_in nhưng không check_out (chờ sửa — FR-5).

### FR-3 — Ca làm việc
- CRUD ca ở Cài đặt → CHẤM CÔNG: tên, giờ vào 'HH:MM', giờ ra 'HH:MM', dung sai trễ (phút) **+ hệ số lương ca `factor` (schema v3, mặc định 1.0)**. Validate: giờ hợp lệ, tên không trống/trùng, hệ số > 0 (giá trị vô lý kẹp về 0.01).
- **Gán ca theo người**: cột "Ca" trong Danh sách người (combo chọn ca hoặc "Mặc định"); `AttendanceService.assign_shift` → `PersonRepository.set_shift` (không phải `PersonService` như dự kiến ban đầu).
  Lưu cột `shift_id` vào bảng `persons` (NULL = dùng ca mặc định) — thêm vào sync person.
- **Ca mặc định**: id lưu bảng `settings` local (key-value, key `attendance_default_shift`).
  Bảng `shifts` trống khi lần đầu chạy → tự tạo 1 ca "Hành chính" 08:00–17:00, dung sai 10 phút, hệ số 1.0, làm mặc định.
- Xóa ca đang được gán → những người đó quay về ca mặc định; `attendance_days` đã tính **không** đổi.
- **Lương thô theo ca (mở rộng, đã triển khai)**: lương 1 người/tháng = Σ (số công của từng ca × `factor` ca đó) × đơn giá VND — đếm theo `shift_id` ĐÃ LƯU trên từng ngày công nên sửa hệ số/xóa ca không làm thay đổi công đã chấm (xóa ca → hệ số quy về 1.0, ngày vẫn giữ). Xem FR-8 và mục 0 (bảng mở rộng).
- Ngày **không làm việc** (cấu hình FR-6): sự kiện nhận diện vẫn ghi ngày công (`auto`) — chấm được cả cuối tuần, không đánh vắng.

### FR-4 — Trạng thái & tính toán hiển thị
- `auto` + có check_in: "Có mặt"; nếu check_in (giờ địa phương) > giờ vào ca + dung sai → "Đi muộn X phút".
- `auto` + có đủ 2 mốc: hiển thị thêm số giờ làm (check_out − check_in, làm tròn phút).
- `auto` + thiếu check_out: "Thiếu giờ ra" — tô đỏ, không tính giờ làm, **vẫn tính 1 công** (đã có mặt).
- `leave` → "Nghỉ phép"; `trip` → "Công tác"; `manual` → trạng thái tự đặt + nhãn "đã sửa tay".
- **Vắng mặt**: chỉ đánh cho ngày làm việc (FR-6) **đã qua** (trước hôm nay) mà không có dòng `attendance_days`.
  Không tạo dòng DB cho ngày vắng — suy ra lúc render báo cáo (tránh phình DB và mất tính tự do sửa ca).
- **Số công** của người trong tháng = ngày `auto`/`manual` có check_in + `leave` + `trip` (kể cả đi muộn, kể cả thiếu giờ ra).

### FR-5 — Sửa tay + audit log
- Màn Chấm công → bấm ô ngày (hoặc nút "Sửa" trong chi tiết) → dialog:
  sửa giờ vào / giờ ra (bổ sung giờ ra thiếu), đổi trạng thái (Có mặt / Nghỉ phép / Công tác), ghi chú.
- Lưu yêu cầu **mật khẩu** qua `PasswordDialog.require(auth, "sửa bản ghi chấm công")` (tái dùng đúng hiện trạng).
- Mỗi lần lưu ghi 1 dòng `attendance_audit` (giá trị cũ → mới, thời điểm). Chi tiết ngày hiển thị danh sách audit gần nhất.
- Giờ nhập tay 'HH:MM' địa phương → đổi UTC theo `work_date` khi lưu. Nhập sai định dạng → báo lỗi, không lưu.
- Sửa tay đặt `manual_override = 1`; sự kiện nhận diện sau đó **không ghi đè** giá trị đã sửa tay (check_in/check_out giữ nguyên).

### FR-6 — Cấu hình (Cài đặt → CHẤM CÔNG)
- Quản lý ca (bảng CRUD như FR-3) + chọn ca mặc định.
- **Ngày làm việc trong tuần**: checkbox T2…CN — mặc định T2–T6. Lưu `config.attendance_workdays: list[int]` (Monday=0, ví dụ `[0,1,2,3,4]`).
- Dùng cho: đánh vắng (FR-4) và tô nền cột cuối tuần trong bảng tháng.

### FR-7 — Màn "Chấm công" (trang mới trong sidebar)
- Thêm `("Chấm công", "AttendanceView")` vào `PAGES` (main_window) — vị trí sau "Lịch sử".
- Chọn **tháng** (mặc định tháng hiện tại) + người (mặc định: tất cả).
- Bảng tháng: dòng = người, cột = ngày 1..31, ô = trạng thái màu (có mặt / đi muộn / thiếu giờ ra / phép / công tác / vắng / — ngày không làm việc).
- Header mỗi dòng: tên + ca + **số công** + số lần đi muộn + số ngày thiếu giờ ra (đếm nhanh).
- Bấm ô → chi tiết ngày: giờ vào/ra, số giờ làm, trạng thái, ghi chú, lịch audit + nút "Sửa" (FR-5).
- Cảnh báo tổng: "N ngày chưa có giờ ra — bấm vào ô đỏ để bổ sung".

### FR-8 — Xuất báo cáo
- Nút **"Xuất CSV"**: bảng tháng đang xem → file CSV **UTF-8 BOM** (mở bằng Excel giữ đúng dấu tiếng Việt) —
  dùng module `csv` chuẩn, **không thêm dependency**. Cột: Người, Ca, Ngày, Giờ vào, Giờ ra, Số giờ làm, Trạng thái, Đi muộn (phút), Ghi chú.
- Nút **"Xuất PDF"**: render bảng tổng hợp + chi tiết ngày bất thường qua **QPrinter (PySide6.QtPrintSupport, output PDF)** +
  QTextDocument HTML — có sẵn trong PySide6, **không thêm reportlab**; font hệ thống Windows hiển thị dấu tiếng Việt.
- Cả hai dùng `QFileDialog.getSaveFileName` để chọn nơi lưu; báo "Đã xuất: <đường dẫn>" khi xong.
- **Bảng lương thô (mở rộng, đã triển khai)**:
  - Nút **[ Bảng lương ]** trên trang Chấm công → `PayrollDialog`: bảng tra nhanh Người · Ca áp dụng · Hệ số · Số công · Phép · Công tác · Vắng · Lương thô (công quy đổi, in đậm) · **Tiền lương (VND)** (chỉ khi đã đặt đơn giá) · Chi tiết theo ca (`Ca đêm: 4 × 1.5`) + dòng **TỔNG TIỀN**.
  - CSV: sau khối chi tiết thêm khối `LƯƠNG THÔ` với header riêng (gồm cột Tiền lương (VND) + dòng TỔNG TIỀN — đủ 10 cột, tổng nằm đúng cột tiền) — vẫn UTF-8 BOM.
  - PDF: bảng lương là **trang 2** (`page-break-before`), tự thêm cột tiền + dòng tổng + ghi chú đơn giá; chưa đặt đơn giá → giữ nguyên dạng công quy đổi.
  - Đơn giá 1 công quy đổi lưu `config.attendance_pay_rate` (VND, 0 = chưa đặt) — chỉnh ở Cài đặt → CHẤM CÔNG; `format_vnd()` định dạng `12.500.000 đ`.
  - Nguồn số duy nhất: `AttendanceService.payroll_summary` — Dashboard (thẻ "Lương thô tháng này"), PayrollDialog, CSV và PDF luôn khớp nhau.
  - **Xuất nhanh từ Dashboard (đã triển khai)**: 2 nút [ Xuất CSV ] / [ Xuất PDF ] ngay trên thẻ lương — CSV gồm chi tiết công + khối lương, PDF 2 trang (bảng công tháng + bảng lương); tên file gợi ý `bang-cong-luong-{yyyy}-{MM}`. Hiện thực qua 2 hàm dùng chung `payroll_csv_rows` / `payroll_pdf_html` (`app/ui/attendance_view.py`) mà cả AttendanceView và DashboardView cùng gọi — file xuất từ Dashboard khớp từng ô với file từ trang Chấm công (test so khớp từng dòng).

### FR-9 — Đồng bộ D1 (giữ outbox pattern hiện tại)
- Schema D1 thêm `shifts` + `attendance_days` (giống FR-1). **Audit log KHÔNG sync** (log cục bộ).
- Outbox entity mới: `shift`, `attendance_day` — đẩy theo dạng upsert/delete như hiện tại.
- Pull: chèn dòng cloud local chưa có (id UUID); sự kiện mobile kéo về tiếp tục chạy qua `AttendanceService.on_event`.
- Ghi chú mở: sau này app mobile có thể ghi `attendance_days` trực tiếp — hiện tại mobile chỉ ghi sự kiện nhận diện, desktop tự ghép.

---

## 4. Luồng chi tiết

### 4.1 Nhận diện → ngày công (tự động)
1. Camera/ảnh nhận diện người đã biết → ghi sự kiện (như cũ, debounce 5s).
2. `AttendanceService.on_event` → ánh xạ `detected_at` (UTC→local) về cửa sổ ca → `work_date`.
3. Ngày chưa có dòng → tạo (check_in); đã có → cập nhật check_out; dòng `manual_override` → bỏ qua.
4. Trạng thái "Đi muộn X phút" / "Thiếu giờ ra" tính khi hiển thị theo ca áp dụng.

### 4.2 Xem + sửa tay
1. Sidebar "07 Chấm công" → chọn tháng → bảng màu ô.
2. Ô đỏ "Thiếu giờ ra" → chi tiết → "Sửa" → nhập giờ ra → `PasswordDialog.require` → lưu + audit.

### 4.3 Tạo ca + gán
1. Cài đặt → CHẤM CÔNG → thêm ca (tên, giờ, dung sai) → chọn ca mặc định.
2. Danh sách người → combo "Ca" từng người (mặc định = ca chung).
3. Thay đổi ca chỉ ảnh hưởng ngày tính **từ nay** — ngày đã ghi giữ nguyên `shift_id` đã dùng.

### 4.4 Xuất báo cáo
1. Màn Chấm công → chọn tháng → "Xuất CSV" / "Xuất PDF" → chọn nơi lưu → mở bằng Excel / in PDF.

### 4.5 Lần đầu chạy (migration)
1. DB cũ chưa có 3 bảng → `CREATE TABLE IF NOT EXISTS` tự tạo, dữ liệu cũ nguyên vẹn.
2. `shifts` trống → tự tạo ca "Hành chính" 08:00–17:00 dung sai 10 phút làm mặc định.
3. Sự kiện CŨ không được ghép ngược tự động (tránh khối lượng lớn khi mở app lần đầu);
   ngày công chỉ hình thành từ sự kiện mới sau khi nâng cấp (ghi rõ trong hint nếu cần).

---

## 5. Thay đổi theo file (ĐÃ CODE — đối chiếu 2026-09-21)

- ✅ `app/infrastructure/db.py` + `app/infrastructure/d1_client.py`: 3 bảng mới (FR-1, FR-9); cột `shift_id` cho `persons`; migrations `v1→v2` (ALTER + rebuild outbox CHECK, giữ hàng chờ sync) và `v2→v3` (cột `shifts.factor` — lương thô).
- ✅ `app/infrastructure/repositories.py`: `ShiftRepository` (CRUD kèm `factor`), `AttendanceDayRepository`, `AttendanceAuditRepository`; `PersonRepository.set_shift()`.
- ✅ `app/services/attendance.py` (**mới**): ánh xạ event→work_date, `on_event`, tổng hợp tháng, trạng thái + đi muộn, audit, `assign_shift`, `edit_times`/`set_status`/`set_note`/`mark_leave`, `today_overview` (Dashboard), `payroll_summary` + `format_vnd` (lương thô — mở rộng FR-8).
- ✅ `app/ui/attendance_view.py` (**mới**): bảng tháng, chi tiết ngày, nút Sửa (gọi `PasswordDialog.require`) + nút Đánh phép/Công tác, xuất CSV + PDF (QPrinter) **+ nút Bảng lương → `PayrollDialog`** (lương thô — mở rộng FR-8).
- ✅ `app/ui/attendance_edit_dialog.py` (**mới**): dialog sửa giờ vào/ra, trạng thái, ghi chú.
- ✅ `app/ui/main_window.py`: trang "Chấm công" vào `PAGES` (+ trang "Tổng quan" Dashboard đầu tiên — 8 mục).
- ✅ `app/ui/dashboard_view.py` (**mới**): Dashboard hôm nay — 5 thẻ trạng thái + thẻ "Lương thô tháng này" kèm nút xuất nhanh CSV/PDF (mở rộng FR-8, dùng chung `payroll_csv_rows`/`payroll_pdf_html` với AttendanceView).
- ✅ `app/ui/person_list_view.py` + `app/services/attendance.py` (`assign_shift`): cột gán ca (combo) — ghi chú: `set_shift` thuộc `AttendanceService`, không phải `PersonService` như dự kiến ban đầu.
- ✅ `app/ui/settings_view.py`: mục CHẤM CÔNG (CRUD ca qua `ShiftDialog`, ca mặc định, ngày làm việc, nút lưu riêng).
- ✅ Điểm nối FR-2 gộp về **một chỗ**: `app/services/recognition.py` (`save_event` — phủ cả `camera_view` lẫn `photo_view`) + `app/services/sync.py` (`_pull` cho sự kiện mobile); `camera_view.py`/`photo_view.py` **không cần sửa**.
- ✅ `app/config.py`: `attendance_workdays: list[int] = [0, 1, 2, 3, 4]` (tự lọc giá trị vô lý) + `attendance_pay_rate: int = 0` (đơn giá 1 công quy đổi VND — mở rộng FR-8).
- ✅ `app/services/sync.py`: `SyncService.db` (property truy cập Database).
- Test mới: `scripts/test_attendance.py`, `test_attendance_gui.py`, `test_attendance_wiring.py`, `test_attendance_settings.py`, `test_attendance_sync.py`, `test_payroll.py` (lương thô + migration v3), `test_dashboard_gui.py` (Dashboard + thẻ lương).

## 6. Bản sao nội dung (copy) đã dùng (tiếng Việt)
- Trang: `Chấm công` · tổng: `Bảng công tháng {MM/yyyy}`
- Trạng thái: `Có mặt` / `Đi muộn {x} phút` / `Thiếu giờ ra` / `Nghỉ phép` / `Công tác` / `Vắng mặt` / `—`
- Nút: `Sửa` / `Xuất CSV` / `Xuất PDF` / `Đặt ca mặc định`
- Dialog sửa: `Sửa bản ghi chấm công — {tên}, {ngày}` · `Giờ vào (HH:MM)` · `Giờ ra (HH:MM)` · `Trạng thái` · `Ghi chú`
- Xác thực: `Nhập mật khẩu để sửa bản ghi chấm công`
- Cảnh báo: `{n} ngày chưa có giờ ra — bấm ô đỏ để bổ sung`
- Xuất xong: `Đã xuất: {đường dẫn}`

## 7. Ràng buộc & nguyên tắc
- KHÔNG đổi luồng nhận diện / ghi sự kiện hiện có — chỉ thêm bước cập nhật ngày công sau khi ghi.
- Quy ước thời gian giữ nguyên: lưu UTC, hiển thị/nhập giờ địa phương.
- Xuất file bằng Qt (QPrinter) + module `csv` — KHÔNG thêm dependency mới.
- Không tính lương, không phân quyền mới trong bản này.
  *(Cập nhật 2026-09-21: **lương thô** — số công × hệ số ca × đơn giá — đã triển khai ngoài phạm vi gốc theo yêu cầu mới; vẫn KHÔNG tính lương chi tiết, phụ cấp, làm thêm giờ.)*
- Desktop Windows, chạy từ mã nguồn lẫn `.exe` (dữ liệu ở `APP_DIR`).

## 8. Các quyết định đã chốt (thay cho câu hỏi mở)
| # | Vấn đề | Quyết định (2026-09-21) | Ghi chú áp dụng |
|---|---|---|---|
| 1 | Định vị | **Module chấm công đầy đủ** trong app hiện tại | Thêm trang, giữ nguyên các màn cũ |
| 2 | Mô hình thời gian | **Nhiều ca tự tạo, gán theo người**, có ca mặc định | FR-3 |
| 3 | Cách chấm | **Tự động từ nhận diện** (không nút bấm) | FR-2 |
| 4 | Thiếu giờ ra | **Cờ chờ sửa**, không tự bịa giờ | FR-4, FR-5 |
| 5 | Sửa tay | **Có**, cần mật khẩu + **audit log** cũ→mới | FR-5 |
| 6 | Báo cáo | **CSV (UTF-8 BOM, Excel mở được) + PDF (QPrinter)** | FR-8 |
| 7 | Số công | Ngày có mặt (kể cả muộn, kể cả thiếu giờ ra) + phép + công tác | FR-4 |
| 8 | Ngày làm việc | Mặc định T2–T6, cấu hình được | FR-6 |
| 9 | Ca đêm | work_date = **ngày bắt đầu ca**; cửa sổ kéo đến giờ ra ngày hôm sau | FR-2 |
| 10 | Audit log | KHÔNG sync D1; shifts + attendance_days có sync | FR-9 |
| 11 | Sự kiện cũ | KHÔNG ghép ngược — ngày công bắt đầu từ sự kiện mới | 4.5 |

## 9. Định nghĩa "hoàn thành" (acceptance criteria) — tất cả ĐÃ ĐẠT qua test
- [x] Nhận diện webcam/ảnh lần đầu trong ngày tạo check-in; lần cuối tạo check-out; đi nhiều lần trong ngày không sinh thêm dòng. (`test_attendance.py`, `test_attendance_wiring.py`)
- [x] Người có ca 08:00 dung sai 10 phút, check-in 08:12 → hiện "Đi muộn 2 phút"; check-in 07:55 → "Có mặt". (`test_attendance.py`)
- [x] Ngày chỉ có check-in → "Thiếu giờ ra" đỏ, không tính giờ làm, vẫn tính 1 công; sửa tay bổ sung giờ ra → hết cờ + có dòng audit. (`test_attendance.py`, `test_attendance_gui.py`)
- [x] Sửa tay luôn hỏi mật khẩu (PasswordDialog); huỷ xác thực → không lưu. (`test_attendance_gui.py`)
- [x] Tạo/sửa/xóa ca hoạt động; người chưa gán dùng ca mặc định; xóa ca → người về ca mặc định. (`test_attendance.py`, `test_attendance_settings.py`)
- [x] Ca 22:00–06:00: sự kiện 01:00 sáng thuộc ngày bắt đầu ca. (`test_attendance.py`)
- [x] Bảng tháng hiển thị đúng màu trạng thái + số công + số lần muộn; ngày không làm việc tô "—". (`test_attendance_gui.py`)
- [x] Nghỉ phép/công tác đặt tay qua dialog; ngày đó không bị đánh vắng. (`test_attendance_gui.py` — nút "Đánh phép/Công tác…")
- [x] Xuất CSV mở bằng Excel đúng dấu tiếng Việt; PDF hiển thị đúng bảng tháng. (`test_attendance_gui.py`)
- [x] DB cũ mở lên không crash, tự tạo 3 bảng + ca "Hành chính" mặc định. (`test_attendance.py` — migration v1→v2)
- [x] Sync D1: shifts + attendance_days đẩy/kéo được; audit log không lên cloud. (`test_attendance_sync.py`)
