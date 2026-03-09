# Telegram OCR Bot (Doc so tien + cong tong)

Bot nhan 1-50 anh giao dich, tu dong OCR so tien (vi du 90k / 90.000d), hien tung dong va cong tong.

Neu do tin cay OCR < 90% (hoac anh mo/chói/nghieng) bot se:
- Gui nguoc lai anh do
- Yeu cau ban nhap tay so tien hoac gui anh ro hon

## 1) Cai dat

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Can cai [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) tren may.

Windows thuong dung:
- `C:\Program Files\Tesseract-OCR\tesseract.exe`

## 2) Cau hinh

Copy file mau:

```bash
copy .env.example .env
```

Sua `.env`:
- `BOT_TOKEN`: lay tu BotFather
- `CONFIDENCE_THRESHOLD`: mac dinh 0.9
- `TESSERACT_CMD`: duong dan tesseract.exe neu can
- `MAX_IMAGES_PER_BATCH`: mac dinh 50

## 3) Chay bot

```bash
python bot.py
```

## 4) Lenh trong Telegram

- `/start`: huong dan nhanh
- `/sum` hoac `/tong`: xem danh sach so tien va tong
- `/reset`: xoa phien hien tai

## Luu y quan trong

- Yeu cau "do chinh xac > 90%" la nguong kiem soat theo confidence + chat luong anh, khong phai cam ket tuyet doi cho moi anh.
- De dat hieu qua cao, nen chup can man hinh giao dich, ro net, it loe, khong nghieng.
- Neu ban can do chinh xac thuc te on dinh hon, nen thu them model OCR chuyen biet (PaddleOCR/doctr) va bo du lieu test rieng.
