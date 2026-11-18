# LC79 VIP PRO — Dual Dashboard

## Đăng nhập web
Vào /dashboard → nhập **tài khoản + mật khẩu LC79** → vào Control Center.
Không cần DASHBOARD_TOKEN.

## Render
Build: pip install -r requirements.txt
Start: python main.py

Env tối thiểu:
- BOT_TOKEN (Telegram bot, vẫn dùng lệnh /dangnhap nếu muốn)
- LC79_DATA_DIR=/data (Persistent Disk khuyến nghị)

## Tính năng
- Login LC79 trực tiếp trên web, UI VIP cam/đen/vàng
- Số dư realtime, mốc vốn, lãi/lỗ, accuracy
- Auto MD5 + TX độc lập, chạy server-side (đóng web vẫn cược)
- Lịch sử có giờ + lý do dự đoán
- Xuất TXT

## Telegram (tùy chọn)
/dangnhap TK MK
/tudong md5 bat 10k mode=canbang
/tudong tx bat 10k
/thongtin | /lichsu | /xuatlichsu all | /dung
