---
title: ระบบลงรับหนังสือราชการ
emoji: 📚
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# ระบบลงรับหนังสือราชการ

โรงเรียนบ้านโพนทองประชาอุทิศ · สพป.สกลนคร เขต ๑

ระบบช่วยลงรับหนังสือราชการ ใช้ได้ทั้งบนคอมและมือถือ

## ความสามารถ

| โหมด | ทำอะไร |
|---|---|
| ๑ | ดึงหนังสือใหม่จากเว็บ สพป. → AI อ่านและเกษียณ → ลงตรายาง → ส่งเข้ากลุ่ม LINE |
| ๒ | อัปโหลด PDF/รูปเอง แล้วให้ AI เกษียณให้ |
| ๓ | ลงตรายางเลขรับบนกระดาษ A4 เปล่า ไว้ปริ้นทับเอกสารที่ส่งมาเป็นกระดาษ |

พร้อมหน้าถอยเลขรับเวลาลงผิด และระบบผู้ใช้หลายคนแบบต้องอนุมัติก่อน

## ตัวแปรระบบที่ต้องตั้ง

### จำเป็น

| ชื่อ | คืออะไร |
|---|---|
| `SARABAN_STORE` | ใส่ `sheets` (บน hosting ดิสก์ถูกล้างทุกครั้งที่รีสตาร์ท) |
| `SARABAN_SHEET_ID` | รหัสสเปรดชีต (เอาจาก URL) |
| `SARABAN_SA_JSON` | เนื้อไฟล์กุญแจ service account ทั้งก้อน |
| `SARABAN_WORK` | ใส่ `/tmp` — ที่เขียนไฟล์ชั่วคราว |
| `SARABAN_OUTPUT` | ใส่ `/tmp/out` — ที่พักไฟล์ PDF ก่อนส่งขึ้น Drive |
| `SARABAN_GEMINI_API_KEY` | กุญแจ Gemini (AI อ่านหนังสือ) |
| `SARABAN_LOGIN_USER` / `SARABAN_LOGIN_PASS` | รหัสเข้าเว็บ สพป. |

### ถ้าจะให้ส่ง LINE

`SARABAN_LINE_ACCESS_TOKEN` · `SARABAN_LINE_GROUP_ID` · `SARABAN_IMGBB_API_KEY`

### ปรับการยิงเว็บ สพป. (ไม่ตั้งก็ได้)

| ชื่อ | คืออะไร | ค่าเริ่มต้น |
|---|---|---|
| `SARABAN_REQUEST_GAP` | เว้นจังหวะระหว่าง request กี่วินาที — กัน rate limit | `0.7` |
| `SARABAN_USER_AGENT` | User-Agent ที่ใช้ยิง ควรตรงกับ Chrome เวอร์ชันที่ใช้จริง | Chrome รุ่นล่าสุดที่ตั้งไว้ในโค้ด |
| `SARABAN_MAX_DOWNLOAD_MB` | ขนาดไฟล์แนบสูงสุดที่ยอมโหลด | `80` |

> เช็กว่าเน็ตเส้นที่ใช้อยู่เข้าเว็บ สพป. ได้ไหม: `python check_access.py`

### ถ้าจะให้เก็บไฟล์ PDF ถาวร

`SARABAN_DRIVE_UPLOAD=on` · `SARABAN_DRIVE_FOLDER=<รหัสโฟลเดอร์>` · `SARABAN_DRIVE_URL=<ลิงก์โฟลเดอร์>`

> ต้องแชร์โฟลเดอร์ Drive ให้อีเมล service account เป็น "ผู้แก้ไข" ก่อน

## ครั้งแรกที่เปิดใช้

เข้าหน้า `/register` แล้วสมัคร — **คนแรกจะได้เป็นผู้ดูแลระบบอัตโนมัติ** ใช้งานได้ทันที
คนถัดไปต้องรอผู้ดูแลกดอนุมัติ

## รันบนเครื่องตัวเอง

```bash
pip install -r requirements.txt
python -m uvicorn web.main:app --host 0.0.0.0 --port 8000
```

เวอร์ชันเดสก์ท็อป (Windows): เปิด `bot3.pyw`
