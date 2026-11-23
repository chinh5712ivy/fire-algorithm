# Kiểm tra bàn giao · 23/11/2026

- Bốn unit test trong `test_fire_filter.py` đạt.
- Chạy module từ thư mục repo theo trace với clip lửa nhỏ, 51 frame, xác nhận ở 1,6 giây. Kết quả từng trạng thái trùng lượt trước.
- Chạy YOLOX trực tiếp với clip đèn xe, 51 frame, không có ứng viên được xác nhận.
- Kiểm tra đủ 31 MP4 kết quả và hai MP4 mô phỏng. Tất cả dùng H.264, có số frame hợp lệ và SHA-256 khớp tệp gốc bàn giao.
- Tất cả liên kết file tương đối trong README trỏ tới tệp/thư mục có trong repo. `git diff --check` đạt.
- Không tìm thấy `testing/run_all_test_scripts.sh` trong workspace hoặc bản VIAVF-TXC đối chiếu. Đã chạy các kiểm tra thay thế ở trên.
- Nhánh hiển thị và Q/Esc từng được kiểm tra qua mô phỏng HighGUI. Cửa sổ desktop chưa được xác minh trực quan trong lượt bàn giao.
