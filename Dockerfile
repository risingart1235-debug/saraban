# ระบบลงรับหนังสือราชการ — เวอร์ชันเว็บ
# ใช้ได้กับ Hugging Face Spaces / Render / Google Cloud Run / VPS
FROM python:3.12-slim

# ไม่ต้องลง poppler แล้ว เพราะเปลี่ยนไปใช้ PyMuPDF ซึ่งมากับ pip
# ลงแค่ fontconfig ให้ระบบรู้จักฟอนต์ไทยที่เราขนไปเอง
RUN apt-get update && apt-get install -y --no-install-recommends fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces รันคอนเทนเนอร์ด้วยผู้ใช้ uid 1000 ไม่ใช่ root
# ถ้าไม่สร้างผู้ใช้นี้ไว้ โปรแกรมจะเขียนไฟล์ชั่วคราวไม่ได้เลย
RUN useradd -m -u 1000 app
WORKDIR /app

COPY --chown=app:app requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ฟอนต์สารบรรณต้องขนไปด้วย ไม่งั้นตรายางจะเป็นสี่เหลี่ยม
COPY --chown=app:app *.ttf ./
COPY --chown=app:app core.py store.py sppweb.py drive.py thaiwords.py ./
COPY --chown=app:app thai_words.txt ./
COPY --chown=app:app web/ ./web/

# ที่เขียนไฟล์ชั่วคราว/log และที่พักไฟล์ PDF ก่อนส่งขึ้น Drive
ENV SARABAN_WORK=/tmp \
    SARABAN_OUTPUT=/tmp/out
RUN mkdir -p /tmp/out && chown -R app:app /tmp/out

USER app

# ---------------------------------------------------------------
# ความลับทั้งหมดส่งผ่านตัวแปรระบบ ห้ามใส่ในอิมเมจ
#
# จำเป็น:
#   SARABAN_STORE=sheets            บน hosting ดิสก์ถูกล้างเมื่อรีสตาร์ท
#   SARABAN_SHEET_ID=<รหัสสเปรดชีต>
#   SARABAN_SA_JSON=<เนื้อไฟล์ service_account.json ทั้งก้อน>
#   SARABAN_GEMINI_API_KEY=<กุญแจ Gemini>
#   SARABAN_LOGIN_USER / SARABAN_LOGIN_PASS      รหัสเข้าเว็บ สพป.
#
# ถ้าจะส่ง LINE:
#   SARABAN_LINE_ACCESS_TOKEN / SARABAN_LINE_GROUP_ID / SARABAN_IMGBB_API_KEY
#
# ถ้าจะเก็บไฟล์ PDF ถาวรบน Drive:
#   SARABAN_DRIVE_UPLOAD=on
#   SARABAN_DRIVE_FOLDER=<รหัสโฟลเดอร์>
#   SARABAN_DRIVE_URL=<ลิงก์โฟลเดอร์ สำหรับปุ่มบนหน้าแรก>
# ---------------------------------------------------------------

# แต่ละเจ้าใช้พอร์ตไม่เหมือนกัน จึงอ่านจากตัวแปร PORT ที่ผู้ให้บริการฉีดมาให้
#   Koyeb / Render / Cloud Run -> ตั้ง PORT ให้เอง (มักเป็น 8000)
#   Hugging Face Spaces        -> ใช้ 7860
# ใช้รูปแบบ shell เพื่อให้ ${PORT} ถูกแทนค่าจริงตอนรัน
ENV PORT=7860
EXPOSE 7860
CMD python -m uvicorn web.main:app --host 0.0.0.0 --port ${PORT:-7860}
