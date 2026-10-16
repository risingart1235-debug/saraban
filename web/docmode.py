"""docmode.py — โหมดที่ ๑ (ดึงจากเว็บ สพป.) และโหมดที่ ๒ (อัปโหลดไฟล์เอง)

งานหนัก (โหลดไฟล์ + ให้ AI อ่าน) ใช้เวลาหลายสิบวินาที ถ้าให้เบราว์เซอร์รอ
คำตอบเดียวจนจบ มือถือมักตัดการเชื่อมต่อก่อน จึงทำเป็น "งาน" (job):
  ๑. เปิดงาน -> คืน job_id ทันที แล้วไปทำเบื้องหลัง
  ๒. เบราว์เซอร์ถามสถานะเป็นระยะจนกว่าจะพร้อม
"""
import os
import io
import re
import base64
import hashlib
import hmac
import queue
import secrets
import shutil
import threading
import time
import traceback
import unicodedata
import uuid
from datetime import datetime, timedelta

from urllib.parse import quote

import core
from core import now_th
import sppweb
from web import signature as sigmod
from core import (
    Image,
    render_transparent_stamp, render_transparent_kasien,
    find_stamp_pos, place_kasien, kasien_clear_at, find_name_marks, paint_marks,
    generate_kasien_text, extract_recipient_line, classify_recipient,
    get_thai_date, get_thai_time_rounded, normalize_typed_date, to_thai_digits,
    format_scraped_date, get_next_receipt_no, page_order, auto_line_pages, chosen_line_pages,
    upload_to_imgbb, send_line_with_image, PdfMerger,
)

DPI = 200                      # ความละเอียดที่ใช้ทำงานจริง (เท่ากับเวอร์ชันเดสก์ท็อป)
PREVIEW_DPI = 100              # ความละเอียดภาพที่ส่งให้เบราว์เซอร์ (เล็กลงครึ่ง โหลดเร็วบนมือถือ)
MAX_REVIEW_PAGES = 100         # แสดงได้ถึงหน้านี้ (ภาพหน้าโหลดตอนเลื่อนไปถึง เอกสารหนาก็เปิดเร็ว)
CM = DPI / 2.54

# ค่าเริ่มต้นสำหรับเรียงลำดับเมื่อไม่มีเวลาสร้าง — ต้องมีโซนเวลาเหมือน now_th()
# ไม่งั้นเทียบ datetime แบบมีโซนกับไม่มีโซนแล้วพัง
_OLDEST = datetime.min.replace(tzinfo=core.THAI_TZ)

_jobs = {}
_lock = threading.Lock()
JOB_TTL = timedelta(hours=3)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


# งาน OCR/AI ใช้ RAM และ CPU สูง จึงต้องมีทั้งจำนวน worker และคิวที่มีขอบเขต
# ปรับได้บน Render โดยไม่ต้องแก้โค้ด แต่ค่าเริ่มต้นตั้งใจให้เครื่องเล็กทำพร้อมกันแค่ ๒ งาน
JOB_WORKERS = _env_int("SARABAN_JOB_WORKERS", 2, 1, 8)
JOB_QUEUE_LIMIT = _env_int("SARABAN_JOB_QUEUE_LIMIT", 20, 1, 100)
_work_queue = queue.Queue(maxsize=JOB_QUEUE_LIMIT)

# ลงรับผิด -> กด "♻️ ลงรับใหม่ (ใช้เลขเดิม)" ในโหมด ๑ ได้ภายในกี่วันหลังลงรับ
# เรื่องจากมือถือต้องเก็บต้นฉบับไว้บนไดร์ฟเอง เพราะบน Render ดึงจากเว็บ สพป. ใหม่ไม่ได้ (โดนบล็อก)
# ของเดิมลบต้นฉบับทิ้งทันทีที่ลงรับ ลงรับผิดแล้วแก้จากมือถือไม่ได้เลย (ตั้ง 0 = ลบทันทีแบบเดิม)
REDO_DAYS = _env_int("SARABAN_REDO_DAYS", 7, 0, 60)
REDO_KEEP = timedelta(days=REDO_DAYS)

# งานฝาก/ลบไฟล์คิวบนไดร์ฟ ทำเบื้องหลังทีละงานด้วยเธรดเดียว
# ของเดิมเปิดเธรดใหม่ทุกเรื่อง มือถือส่งรวด ๘ เรื่องก็อัปพร้อมกันหลายทาง แต่ละทาง
# อ่านไฟล์ทั้งก้อนเข้า RAM (อัปแบบไม่แบ่งก้อนใช้ราว ๓ เท่าของขนาดไฟล์) แถมยิงซ้อนกัน
# บนการเชื่อมต่อเดียว เครื่องฟรีของ Render มี RAM 512 MB เกินเมื่อไหร่ถูกฆ่าแล้วบูตใหม่
# ระหว่างนั้นเว็บตอบ 503 ทั้งเว็บ — มือถือที่กำลังส่งอยู่ก็พังรวดทุกเรื่องที่เหลือ
_drive_tasks = queue.Queue()

# ปุ่ม 🔎 สแกน — อ่านหน้าภาพสแกนด้วย Tesseract ทีละเรื่อง ทีละหน้า ด้วยเธรดเดียว
# แยกจากคิวงานเปิดเรื่อง (AI) ไม่งั้นสแกนยาวๆ เรื่องหนึ่งจะขวางคนอื่นเปิดเรื่องใหม่ไม่ได้
# และเครื่องฟรีของ Render แรงไม่พอให้อ่านภาพสองเรื่องพร้อมกัน
_ocr_tasks = queue.Queue()
OCR_MAX_PAGES = 30             # สแกนครั้งละไม่เกินนี้ (Render ฟรีหน้าละ ~๑๕ วินาที ๓๐ หน้า ~๗ นาที)
_ocr_sec = [15.0]              # วินาทีต่อหน้าที่วัดได้ล่าสุด ไว้บอกเวลาโดยประมาณก่อนกดสแกน
# เปิดเว็บบนคอมเอง (เปิดเซิร์ฟเวอร์.bat ตั้ง SARABAN_OCR_AUTO=1): คอมอ่านภาพเร็ว สแกนหน้าภาพทุกหน้าให้เอง
# ตอนเปิดเรื่องเหมือนโปรแกรมบนคอม — บน Render ฟรีช้า จึงยังให้กดปุ่ม 🔎 สแกน เลือกหน้าเอง
OCR_AUTO = os.environ.get("SARABAN_OCR_AUTO", "").strip().lower() in ("1", "on", "true", "yes")

# กู้คิวจากไดร์ฟเสร็จหรือยัง — เซิร์ฟเวอร์เพิ่งตื่น/บูตใหม่ คิวในหน่วยความจำยังว่างอยู่
# จนกว่า restore_queue จะทำเสร็จ ใครจะถามว่า "อะไรอยู่ในคิวแล้ว" ต้องรอตัวนี้ก่อน
_queue_restored = threading.Event()
RESTORE_WAIT_SEC = 45


class QueueFullError(RuntimeError):
    pass


class AlreadyHandledError(RuntimeError):
    def __init__(self, status: str, receipt_no: str = ""):
        self.status = status
        self.receipt_no = str(receipt_no or "")
        label = "ลงรับแล้ว" if status == "registered" else "ข้ามแล้ว"
        super().__init__(f"หนังสือเรื่องนี้{label}" +
                         (f" (เลขรับ {self.receipt_no})" if self.receipt_no else ""))


class DriveNotReadyError(RuntimeError):
    """อัปไฟล์ขึ้นไดร์ฟไม่ได้ — ต้องหยุดก่อนกินเลขรับ ไม่ใช่ปล่อยให้ลงทะเบียนแล้วไฟล์หาย"""


class OcrUnavailable(RuntimeError):
    """เครื่องนี้ไม่มีตัวอ่านภาพ (Tesseract ภาษาไทย) — สแกนหาชื่อในหน้าภาพไม่ได้"""


class JobStateError(RuntimeError):
    def __init__(self, message: str, *, status: str = "", result=None):
        super().__init__(message)
        self.status = status
        self.result = result


def _worker_loop(tasks):
    while True:
        work = tasks.get()
        try:
            work()
        except Exception:
            # work แต่ละตัวต้องเก็บข้อผิดพลาดไว้ใน job ของตนเองอยู่แล้ว
            # guard นี้กัน worker ตายถ้ามี bug ในตัวจัดการ error เอง
            traceback.print_exc()
        finally:
            tasks.task_done()


for _worker_no in range(JOB_WORKERS):
    threading.Thread(target=_worker_loop, args=(_work_queue,),
                     name=f"saraban-worker-{_worker_no + 1}", daemon=True).start()
threading.Thread(target=_worker_loop, args=(_drive_tasks,),
                 name="saraban-drive", daemon=True).start()
threading.Thread(target=_worker_loop, args=(_ocr_tasks,),
                 name="saraban-ocr", daemon=True).start()


# ==========================================================
# เครื่องมือช่วย
# ==========================================================
def _png_uri(img) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _render_pdf_page(pdf_path: str, page_no: int, dpi: int = DPI):
    """แปลงหน้า PDF เป็นรูป — ใช้ PyMuPDF จึงไม่ต้องลง Poppler บนเซิร์ฟเวอร์"""
    import fitz
    with core.PDF_LOCK:
        try:
            with fitz.open(pdf_path) as doc:
                page_no = max(1, min(page_no, len(doc)))
                px = doc[page_no - 1].get_pixmap(dpi=dpi)
                return Image.frombytes("RGB", (px.width, px.height), px.samples), len(doc)
        finally:
            core.release_pdf_cache()      # ไม่งั้นเรื่องหลายหน้าค้างแรมไว้หลายร้อย MB


def _clean_text(value, default: str = "", limit: int = 500) -> str:
    """เก็บ metadata ไว้แสดงผลได้ แต่ตัด control character และจำกัดขนาด"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join("".join(ch if (ch >= " " and ch != "\x7f") else " "
                            for ch in text).split())
    return (text[:limit] or default)


def sanitize_phone_meta(meta: dict) -> dict:
    raw_id = _clean_text(meta.get("book_id"), "", 128)
    book_id = re.sub(r"[^0-9A-Za-z_.-]", "", raw_id)
    if not book_id:
        raise ValueError("ไม่ได้ระบุ book_id ที่ถูกต้อง")
    return {
        "book_id": book_id,
        "doc_no": _clean_text(meta.get("doc_no"), "-", 200),
        "doc_title": _clean_text(meta.get("doc_title"), "-", 500),
        "doc_date": _clean_text(meta.get("doc_date"), "-", 100),
        "sender": _clean_text(meta.get("sender"), "-", 300),
        "emoji": _clean_text(meta.get("emoji"), "🔵", 16),
        "attach": _clean_text(meta.get("attach"), "", 4000),
        "redo_no": _clean_text(meta.get("redo_no"), "", 50) or None,
    }


def safe_output_filename(doc_no: str, receipt_no: str) -> str:
    """ชื่อไฟล์ที่ปลอดภัยทั้ง Windows/Linux และมีเลขรับเพื่อไม่เขียนทับกัน"""
    receipt = re.sub(r"[^0-9A-Za-zก-๙_.-]", "_",
                     unicodedata.normalize("NFKC", str(receipt_no or "")))[:40] or "ไม่ทราบเลข"
    stem = unicodedata.normalize("NFKC", str(doc_no or ""))
    stem = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._")
    if not stem or stem == "-":
        stem = "เอกสารนำเข้า"
    stem = stem[:100].rstrip(" ._") or "เอกสารนำเข้า"
    return f"เลขรับ_{receipt}_{stem}.pdf"


def contained_path(root: str, *parts: str):
    """คืน absolute path เมื่ออยู่ใต้ root จริง; คืน None เมื่อพยายามไต่โฟลเดอร์"""
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, *[str(p) for p in parts]))
    try:
        return candidate if os.path.commonpath((root_abs, candidate)) == root_abs else None
    except ValueError:  # คนละ drive บน Windows
        return None


# ==========================================================
# รูปที่ส่งเข้า LINE — บน hosting ให้ LINE มาดึงจากเราเอง
# ==========================================================
# imgbb บล็อก IP ของศูนย์ข้อมูล ตอบ 400 code 103 "You have been forbidden to use
# this website" (อาการเดียวกับที่เว็บ สพป. บล็อก Render จนต้องให้มือถือไปดึงแทน)
# บน hosting จึงตัดคนกลางทิ้ง ให้เซิร์ฟเวอร์ของ LINE มาดึงรูปจากเราตรงๆ
#
# บนเครื่องตัวเองยังใช้ imgbb เหมือนเดิม เพราะเซิร์ฟเวอร์อยู่หลังเราเตอร์บ้าน
# LINE เข้าไม่ถึง — public_base_url() จะว่าง แล้วโค้ดถอยไปใช้ imgbb เอง
#
# รูปหมดอายุเร็ว (ไม่ถึงชั่วโมง): เดิมเก็บรูปไว้แค่บนดิสก์ชั่วคราวกับรายชื่อในหน่วยความจำ
# Render ฟรีหลับเองเมื่อไม่มีใครเข้า ๑๕ นาที และเริ่มใหม่ทุกครั้งที่ deploy ตื่นมาทั้งสองอย่าง
# ว่างเปล่า LINE มาดึงรูปตอนมีคนกดเปิดดูทีหลังเจอ "ไม่พบ" จึงขึ้นว่ารูปหมดอายุ
# ตอนนี้รูปทุกหน้าที่ส่ง LINE เก็บไว้คู่กับไฟล์ PDF (โฟลเดอร์เดียวกันบนไดร์ฟ ชื่อตามไฟล์ PDF)
# ลิงก์เซ็นกำกับไว้ (ไม่ต้องจำในหน่วยความจำ) เริ่มใหม่แล้วมีคนเปิดเมื่อไร หารูปบนไดร์ฟ
# จากโทเคนที่ติดไว้กับไฟล์ ดึงกลับมาให้ — ไฟล์ไม่ได้เปิดสาธารณะ ดูได้ผ่านลิงก์นี้ทางเดียว
_LINE_IMG_DIR = core._w("_lineimg")
_LINE_IMG_KEEP = 120           # สำเนาบนเครื่องเก็บล่าสุดเท่านี้ ที่เก่ากว่านั้นดึงจากไดร์ฟเอา
_LINE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{40}")
_line_key = None


def public_base_url() -> str:
    """ที่อยู่สาธารณะของเซิร์ฟเวอร์ — ว่างแปลว่ารันในเครื่อง

    RENDER_EXTERNAL_URL เป็นตัวแปรที่ Render ใส่ให้เองอัตโนมัติ ไม่ต้องตั้งเพิ่ม
    ถ้าย้ายไป hosting เจ้าอื่นค่อยตั้ง SARABAN_PUBLIC_URL เอง
    """
    return (os.environ.get("SARABAN_PUBLIC_URL", "").strip()
            or os.environ.get("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")


def _line_sig(head: str) -> str:
    global _line_key
    if _line_key is None:
        _line_key = core.server_key("saraban line image")
    mac = hmac.new(_line_key, head.encode(), hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(mac).decode()


def _new_line_token() -> str:
    """โทเคนในลิงก์รูป = สุ่ม ๒๔ ตัว + ลายเซ็น ๑๖ ตัว (เดาไม่ได้ และปลอมไม่ได้)"""
    head = secrets.token_urlsafe(18)
    return head + _line_sig(head)


def _line_token_ok(token: str) -> bool:
    """ตรวจลายเซ็นก่อนเสมอ — โทเคนมั่วๆ ตอบ "ไม่พบ" ทันที ไม่ต้องไปถามไดร์ฟ
    (ไม่งั้นใครยิงลิงก์มั่วรัวๆ ก็ทำให้เซิร์ฟเวอร์ไปค้นไดร์ฟจนคิวงานไดร์ฟอื่นติดหมด)"""
    token = str(token or "")
    return bool(_LINE_TOKEN_RE.fullmatch(token)) and hmac.compare_digest(
        token[24:].encode(), _line_sig(token[:24]).encode())


def publish_line_image(src_path: str, day: str = None) -> str:
    """รูปหน้าที่ส่งเข้า LINE: สำรองขึ้นไดร์ฟคู่กับไฟล์ PDF แล้วคืน URL ให้ LINE มาดึง

    src_path คือรูปที่บันทึกไว้ข้างไฟล์ PDF แล้ว (ชื่อตามไฟล์ PDF) ส่วน day คือโฟลเดอร์ของวันนั้น
    — ขึ้นไดร์ฟไปอยู่โฟลเดอร์เดียวกับ PDF ด้วยชื่อเดียวกัน (ทำเบื้องหลัง ไม่ให้ผู้ใช้รอ)
    คืน "" ถ้ารันในเครื่อง (ผู้เรียกถอยไปใช้ imgbb) แต่ยังสำรองขึ้นไดร์ฟให้ถ้าเปิดใช้ไว้

    บน hosting คัดลอกไว้อีกชุดที่ชื่อเป็นโทเคน ให้ LINE มาดึงครั้งแรกได้ทันทีหลังส่ง
    (ไม่ต้องรอฝากขึ้นไดร์ฟเสร็จ) และอยู่รอดแม้ _cleanup() ลบโฟลเดอร์งานทิ้ง
    """
    base = public_base_url()
    token = ""
    if base:
        os.makedirs(_LINE_IMG_DIR, exist_ok=True)
        token = _new_line_token()
        shutil.copyfile(src_path, os.path.join(_LINE_IMG_DIR, token + ".jpg"))
        _trim_line_images()
    _backup_line_image(src_path, day, token)
    return f"{base}/api/line-image/{token}.jpg" if token else ""


def _trim_line_images():
    """สำเนาบนเครื่องเก็บแค่ล่าสุด _LINE_IMG_KEEP รูป ที่เก่ากว่านั้นยังอยู่บนไดร์ฟ"""
    try:
        files = sorted((e for e in os.scandir(_LINE_IMG_DIR) if e.name.endswith(".jpg")),
                       key=lambda e: e.stat().st_mtime)
    except OSError:
        return
    for e in files[:-_LINE_IMG_KEEP]:
        try:
            os.remove(e.path)
        except OSError:
            pass


def _backup_line_image(path: str, day: str, token: str):
    """ฝากรูปขึ้นไดร์ฟเบื้องหลัง (คิวเดียวกับงานไดร์ฟอื่น ยิงทีละคำสั่ง)"""
    def work():
        try:
            import drive as _dr
            if not _dr.enabled():
                return
            src = path
            if not os.path.exists(src) and token:   # รูปข้างไฟล์ PDF หายก่อนถึงคิว ใช้ชุดสำหรับ LINE
                src = os.path.join(_LINE_IMG_DIR, token + ".jpg")
            if os.path.exists(src):
                _dr.line_image_put(src, os.path.basename(path), day=day, token=token)
        except Exception as e:
            print(f"สำรองรูปที่ส่ง LINE ขึ้นไดร์ฟไม่สำเร็จ: {type(e).__name__}: {e}")

    _drive_tasks.put(work)


def line_image_path(token: str) -> str:
    """หาไฟล์รูปจากโทเคน คืน "" ถ้าไม่มี

    โทเคนผ่านการตรวจลายเซ็นและรูปแบบ (ตัวอักษร A-Z a-z 0-9 _ - ยาว ๔๐ ตัวพอดี) ก่อนจะเอาไป
    ต่อเป็นชื่อไฟล์ จึงไม่มีทางหลุดไปอ่านไฟล์อื่นในเครื่อง
    ไม่มีบนเครื่อง (เซิร์ฟเวอร์เพิ่งเริ่มใหม่ ดิสก์ถูกล้าง) ก็ดึงสำเนาที่ฝากไว้บนไดร์ฟกลับมา
    """
    if not _line_token_ok(token):
        return ""
    path = os.path.join(_LINE_IMG_DIR, token + ".jpg")
    if os.path.exists(path):
        return path
    try:
        import drive as _dr
        if _dr.enabled():
            os.makedirs(_LINE_IMG_DIR, exist_ok=True)
            if _dr.line_image_get(token, path):
                _trim_line_images()
                return path
    except Exception as e:
        print(f"ดึงรูปที่ส่ง LINE กลับจากไดร์ฟไม่สำเร็จ: {type(e).__name__}: {e}")
    return ""


# ==========================================================
# คิวที่รอดตอนเซิร์ฟเวอร์เกิดใหม่
# ==========================================================
# ของเดิมเก็บงานไว้ใน _jobs (หน่วยความจำ) กับดิสก์ชั่วคราว ซึ่ง Render ล้างทิ้ง
# ทุกครั้งที่ deploy หรือหลับแล้วตื่น เรื่องที่รอลงรับหายหมด ต้องให้มือถือไปดึง
# จาก สพป. มาส่งใหม่ทั้งชุด (เซิร์ฟเวอร์ดึงเองไม่ได้ เว็บ สพป. บล็อก IP ศูนย์ข้อมูล)


def _backup_to_drive(job: dict, pdf: str):
    """ฝากไฟล์ไว้บนไดร์ฟเบื้องหลัง ไม่ให้มือถือต้องรอ

    ทำเบื้องหลังเพราะมือถือส่งรวดเดียวหลายเรื่อง ถ้ารออัปทีละไฟล์จะช้ามาก
    แลกกับช่องว่างสั้นๆ ถ้าเครื่องดับพอดีก่อนถึงคิวอัป เรื่องนั้นจะไม่ถูกสำรอง
    (ดู _drive_tasks ว่าทำไมต้องเข้าแถวทีละเรื่อง ไม่แยกเธรดใครเธรดมัน)
    """
    def work():
        # ข้ามไปแล้วระหว่างรอคิวอัป ไม่ต้องฝากแล้ว — แต่ที่ลงรับไปแล้วยังต้องฝาก
        # (เก็บไว้ให้กดลงรับใหม่ได้ ดู REDO_KEEP) ไม่งั้นเรื่องที่ลงรับไวกว่าที่อัปทันจะลงรับใหม่ไม่ได้
        status = job.get("status")
        if status == "skipped" or (status == "done" and not _keeps_original(job)):
            return
        try:
            import drive as _dr
            meta = {k: job.get(k) for k in ("book_id", "doc_no", "doc_title", "doc_date",
                                            "sender", "emoji", "attach", "redo_no", "user",
                                            "main_pages", "extras_got")}
            meta["created"] = job["created"].isoformat()
            meta["job_id"] = job["id"]      # กู้กลับมาด้วยเลขงานเดิม หน้าที่เปิดค้างไว้จะได้ไม่หลงทาง
            fid = _dr.queue_put(pdf, meta)
            if fid:
                _set(job, drive_file_id=fid)
        except Exception as e:
            print(f"ฝากไฟล์คิวไว้บนไดร์ฟไม่สำเร็จ: {type(e).__name__}: {e}")

    _drive_tasks.put(work)


def _drop_backup(job: dict):
    """เรื่องนี้จบแล้ว (ลงรับหรือข้าม) เอาของที่ฝากไว้ออก"""
    def work():
        # อ่าน id ตอนถึงคิว ไม่ใช่ตอนสั่ง — ถ้าลงรับไวกว่าที่ฝากเสร็จ
        # งานฝากที่อยู่ก่อนหน้าในแถวเดียวกันจะเติม id ไว้ให้แล้ว ไม่เหลือไฟล์ค้างบนไดร์ฟ
        fid = job.get("drive_file_id")
        if not fid:
            return
        import drive as _dr
        if _dr.queue_drop(fid):
            _set(job, drive_file_id="")

    _drive_tasks.put(work)


def _keeps_original(job: dict) -> bool:
    """ลงรับแล้วยังเก็บต้นฉบับไว้ให้กด ♻️ ลงรับใหม่ได้ไหม — เฉพาะเรื่องจากมือถือ
    (เรื่องจากเว็บ สพป. เครื่องที่ต่อเว็บได้ดึงใหม่เองได้ ส่วนไฟล์ที่อัปเอง ผู้ใช้มีไฟล์อยู่แล้ว)"""
    return bool(REDO_DAYS and job.get("source") == "phone" and job.get("book_id"))


def _has_original(job: dict) -> bool:
    """ต้นฉบับยังอยู่ไหม — ในเครื่อง หรือฝากไว้บนไดร์ฟ (หลังเซิร์ฟเวอร์เกิดใหม่มีแต่บนไดร์ฟ)"""
    return bool(job.get("drive_file_id")) or bool(
        job.get("dir") and os.path.exists(os.path.join(job["dir"], "doc.pdf")))


def _redo_since(rec: dict, meta: dict):
    """เวลาที่ลงรับ ถ้ายังอยู่ในกำหนดเก็บไว้ลงรับใหม่ (REDO_KEEP) — ไม่อยู่หรือทำไม่ได้ คืน None

    ทำไม่ได้ = ข้ามไปแล้ว หรือประวัติรุ่นเก่าที่ไม่ได้จดเลขรับไว้ (ไม่รู้จะใช้เลขอะไร)
    """
    if not REDO_DAYS or rec.get("status") != "registered" or not rec.get("receipt_no"):
        return None
    try:
        at = datetime.strptime(str(rec.get("when") or "").strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        # ไม่รู้เวลาลงรับ ใช้เวลาที่มือถือส่งเข้ามาแทน (เก่ากว่า หมดกำหนดเร็วกว่า ไม่ค้างไดร์ฟนาน)
        try:
            at = datetime.fromisoformat(str(meta.get("created") or ""))
        except ValueError:
            return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=core.THAI_TZ)
    return at if now_th() - at <= REDO_KEEP else None


def _expire_kept():
    """พ้นกำหนดเก็บไว้ลงรับใหม่แล้ว — เอาต้นฉบับบนไดร์ฟออก แล้วลืมเรื่องนั้นไป

    ลืมแล้วมือถือก็ไม่ดึงซ้ำ เพราะเรื่องนี้อยู่ในประวัติลงรับ (ชีต) แล้ว
    """
    now = now_th()
    with _lock:
        old = [j for j in _jobs.values()
               if j.get("status") == "done" and _keeps_original(j)
               and now - (j.get("registered_at") or j["created"]) > REDO_KEEP]
        for j in old:
            _jobs.pop(j["id"], None)
    for j in old:
        _drop_backup(j)
        shutil.rmtree(j.get("dir") or "", ignore_errors=True)


def restore_queue() -> int:
    """กู้คิวจากไดร์ฟ แล้วบอกทุกคนที่รออยู่ว่ากู้เสร็จแล้ว (ถึงจะกู้ไม่สำเร็จก็ต้องบอก)"""
    try:
        return _restore_from_drive()
    finally:
        _queue_restored.set()


def wait_queue_restored(timeout: float = RESTORE_WAIT_SEC) -> bool:
    """รอให้กู้คิวจากไดร์ฟเสร็จ (หลังเซิร์ฟเวอร์เกิดใหม่) คืน False ถ้ารอจนหมดเวลา"""
    return _queue_restored.wait(timeout)


def _restore_from_drive() -> int:
    """ดึงคิวที่ฝากไว้กลับมาตอนเปิดเซิร์ฟเวอร์ คืนจำนวนเรื่องที่กู้ได้

    กู้กลับมาเป็นสถานะ "รอเปิดอ่าน" เหมือนตอนมือถือเพิ่งส่งเข้ามา ไม่ได้เก็บผล
    วิเคราะห์ของ AI ไว้ด้วย เรื่องที่เคยกดเปิดอ่านแล้วจึงต้องให้ AI อ่านใหม่
    (เก็บด้วยก็ได้แต่ซับซ้อนขึ้นมากและกินที่ ไม่คุ้มกับที่ประหยัดได้)

    ตัวไฟล์ยังไม่ดึงลงมาตอนนี้ รอจนผู้ใช้กดเปิดเรื่องนั้นจริงถึงค่อยดึง
    (เหตุผลเดียวกับ prepare_stored — ไม่เปลืองแรงกับเรื่องที่ยังไม่มีใครดู)

    เรื่องที่ลงรับไปแล้วแต่ยังอยู่ในกำหนดเก็บไว้ลงรับใหม่ (REDO_KEEP) กู้กลับมาเป็น "ลงรับแล้ว"
    ไม่เข้าคิว แค่ให้หน้ารายการมีปุ่ม ♻️ ลงรับใหม่ให้กด
    """
    try:
        import drive as _dr
        if not _dr.enabled():
            return 0
        items = _dr.queue_list()
    except Exception as e:
        print(f"กู้คิวจากไดร์ฟไม่สำเร็จ: {type(e).__name__}: {e}")
        return 0

    restored = kept = 0
    for item in items:
        meta = item["meta"]
        bid = str(meta.get("book_id") or "")
        try:
            rec = _durable_phone_record(bid)
        except Exception:
            rec = None    # อ่านสถานะไม่ได้ก็กู้ไว้ก่อน ดีกว่าทำหาย
        registered_at = _redo_since(rec, meta) if rec else None
        if rec and registered_at is None:
            # ข้ามไปแล้ว / ลงรับนานเกินกำหนดเก็บ (รวมที่จบไประหว่างเซิร์ฟเวอร์ดับ) — เก็บกวาดทิ้งเลย
            try:
                _dr.queue_drop(item["id"])
            except Exception:
                pass      # ลบไม่ได้ก็ไม่กู้ ไว้ลองลบใหม่ตอนเซิร์ฟเวอร์เกิดใหม่รอบหน้า
            continue
        state = ({"status": "done", "step": "ลงรับแล้ว", "receipt_no": str(rec["receipt_no"]),
                  "registered_at": registered_at}
                 if registered_at else {"status": "stored", "step": "รอเปิดอ่าน"})
        with _lock:
            if any(j.get("source") == "phone" and j.get("book_id") == bid
                   for j in _jobs.values()):
                continue
            fields = {k: meta.get(k) or d for k, d in (
                ("doc_no", "-"), ("doc_title", "-"), ("doc_date", "-"),
                ("sender", "-"), ("emoji", "🔵"), ("attach", ""))}
            # เลขงานเดิม — ลิงก์ /doc?job=... ที่เปิดค้างไว้ก่อนเซิร์ฟเวอร์เริ่มใหม่ยังใช้ได้
            # (กดโหลดใหม่แล้ว AI อ่านเรื่องเดิมให้ ไม่ใช่ "ไม่พบงานนี้")
            old_id = str(meta.get("job_id") or "")
            jid = old_id if re.fullmatch(r"[0-9a-f]{32}", old_id) and old_id not in _jobs else None
            job = _new_job_locked(meta.get("user") or "phone", jid=jid, source="phone", **state,
                                  book_id=bid, redo_no=meta.get("redo_no") or None,
                                  main_pages=meta.get("main_pages") or None,
                                  extras_got=meta.get("extras_got"),
                                  drive_file_id=item["id"], **fields)
            try:                        # ให้เรียงตามเวลาที่มือถือส่งจริง ไม่ใช่เวลาที่กู้
                job["created"] = datetime.fromisoformat(meta["created"])
            except Exception:
                pass
        restored += 1
        kept += 1 if registered_at else 0
    if restored - kept:
        print(f"กู้คิวรอลงรับจากไดร์ฟกลับมาได้ {restored - kept} เรื่อง")
    if kept:
        print(f"กู้เรื่องที่ลงรับแล้ว (เก็บต้นฉบับไว้ลงรับใหม่) กลับมาได้ {kept} เรื่อง")
    return restored


def _cleanup():
    """ลบงานเก่าที่ค้างไว้ กันหน่วยความจำบวม

    ยกเว้นเรื่องที่ยังมีของฝากอยู่บนไดร์ฟ (drive_file_id) เพราะนั่นแปลว่ายังไม่ได้
    ลงรับและยังไม่ได้ข้าม — เป็นงานค้างจริงที่ต้องคาไว้ให้เห็น ไม่ใช่ขยะ
    สำคัญกับเรื่องที่กู้กลับมาหลังเซิร์ฟเวอร์เกิดใหม่ ซึ่งเวลาสร้างเป็นของเมื่อวาน
    ถ้าไม่ยกเว้นไว้ พอมีใครนำเข้าไฟล์ในโหมด ๒ ทีเดียว คิวที่เพิ่งกู้มาจะหายเกลี้ยง
    (หรือลงรับแล้วแต่เก็บต้นฉบับไว้ลงรับใหม่ — อันนั้น _expire_kept ลบให้เมื่อพ้นกำหนด)
    """
    _expire_kept()
    now = now_th()
    with _lock:
        stale = [j for j, v in _jobs.items()
                 if now - v["created"] > JOB_TTL and not v.get("drive_file_id")]
        for jid in stale:
            v = _jobs.pop(jid)
            shutil.rmtree(v.get("dir", ""), ignore_errors=True)


def _new_job_locked(user: str, jid: str = None, **initial) -> dict:
    jid = jid or uuid.uuid4().hex
    d = os.path.join(core._w("_jobs"), jid)
    os.makedirs(d, exist_ok=True)
    job = {"id": jid, "user": user, "created": now_th(),
           "status": "analyzing", "step": "กำลังเตรียมเอกสาร...", "dir": d}
    job.update(initial)
    _jobs[jid] = job
    return job


def _enqueue(job: dict, work):
    _set(job, status="queued", step="อยู่ในคิวรอประมวลผล...")
    try:
        _work_queue.put_nowait(work)
    except queue.Full:
        _set(job, status="error", step="คิวเต็ม", error=(
            "เซิร์ฟเวอร์มีงานรอเต็มแล้ว กรุณารอสักครู่แล้วส่งใหม่โดยเลือก retry"))
        raise QueueFullError("คิวประมวลผลเต็ม กรุณารอสักครู่แล้วลองใหม่")


def _phone_prepare_work(job: dict):
    """งานอ่าน/เกษียณของเรื่องจากมือถือ — สร้างจากข้อมูลที่เก็บไว้ใน job"""
    pdf = os.path.join(job["dir"], "doc.pdf")

    def work():
        try:
            # กู้มาจากไดร์ฟหลังเซิร์ฟเวอร์เกิดใหม่ ตัวไฟล์ยังไม่ได้ดึงลงมา
            if not os.path.exists(pdf) and job.get("drive_file_id"):
                _set(job, status="analyzing", step="กำลังดึงไฟล์กลับจากไดร์ฟ...")
                import drive as _dr
                _dr.queue_fetch(job["drive_file_id"], pdf)
            _set(job, status="analyzing", step="กำลังเตรียมเอกสารจากมือถือ...")
            _prepare(job, pdf, doc_no=job["doc_no"], doc_title=job["doc_title"],
                     doc_date=job["doc_date"], sender=job["sender"],
                     emoji=job["emoji"], attach=job["attach"],
                     book_id=job["book_id"], redo_no=job.get("redo_no"),
                     main_pages=job.get("main_pages"))
        except Exception as e:
            _fail(job, e)

    return work


def prepare_stored(job: dict) -> bool:
    """เริ่มประมวลผลเรื่องที่เก็บไฟล์ไว้แล้ว — เรียกตอนผู้ใช้แตะเปิดเรื่องนั้น

    แยกจากตอนรับไฟล์ เพราะมือถือส่งมารวดเดียวหลายเรื่อง ถ้าเข้าคิวประมวลผลทันที
    ที่รับ เครื่องจะไล่ทำทุกเรื่องรวมถึงเรื่องที่ยังไม่มีใครเปิดดู กินแรงเปล่า
    (โหมด ๑ ก็ทำแบบนี้ — หน้ารายการไม่เรียก AI เรียกตอนกดเปิดเรื่อง)

    คืน False ถ้าเรื่องนี้เริ่มไปแล้ว/ทำเสร็จแล้ว จะได้ไม่ทำซ้ำเวลากดรัวๆ
    """
    # เรื่องนี้อาจถูกลงรับ/ข้ามจากที่อื่นไปแล้ว (เช่นไปกดในโหมด ๑) หลังมือถือส่งเข้ามา
    # เช็คก่อนเข้าคิว จะได้ไม่เปลือง AI กับเรื่องที่ทำไปแล้ว และบอกผู้ใช้ได้ตรงๆ
    if job.get("book_id") and not job.get("redo_no"):
        handled = _durable_phone_record(job["book_id"])
        if handled:
            label = "ลงรับแล้ว" if handled.get("status") == "registered" else "ข้ามแล้ว"
            rno = handled.get("receipt_no", "")
            _set(job, status="error", step=label,
                 error=f"หนังสือเรื่องนี้{label}" + (f" (เลขรับ {rno})" if rno else "") +
                       " — ไม่ต้องลงรับซ้ำ")
            # จบไปทางอื่นแล้ว (เช่นลงรับจากเครื่องที่บ้านผ่านโปรแกรมเดสก์ท็อป)
            # เอาของที่ฝากไว้บนไดร์ฟออกเลย ไม่ต้องรอให้เซิร์ฟเวอร์เกิดใหม่มาเก็บกวาด
            _drop_backup(job)
            return False

    with _lock:
        if job.get("status") != "stored":
            return False
        # ยึดสถานะทันทีในล็อก กันกดพร้อมกันสองครั้งแล้วเข้าคิวซ้ำ
        job["status"] = "queued"
        job["step"] = "อยู่ในคิวรอประมวลผล..."
    work = _phone_prepare_work(job)
    try:
        _work_queue.put_nowait(work)
    except queue.Full:
        _set(job, status="error", step="คิวเต็ม",
             error="เซิร์ฟเวอร์มีงานรอเต็มแล้ว กรุณารอสักครู่แล้วเปิดเรื่องนี้ใหม่")
        raise QueueFullError("คิวประมวลผลเต็ม กรุณารอสักครู่แล้วลองใหม่")
    return True


def _durable_phone_record(book_id: str):
    """อ่านสถานะถาวร; history รุ่นเก่าที่ไม่มีรายละเอียดก็ถือว่าจัดการแล้ว"""
    import store as _st
    st = _st.get_store()
    records = st.doc_records()
    rec = records.get(str(book_id), {})
    if rec.get("status") in ("registered", "skipped"):
        return rec
    if str(book_id) in st.history_ids():
        return {"status": "registered", "receipt_no": rec.get("receipt_no", "")}
    return None


def claim_phone_job(user: str, meta: dict, retry_failed: bool = False):
    """อะตอมมิกต่อโปรเซส: คืน (job, created) และไม่สร้างงานซ้ำจาก book_id เดียวกัน

    งานที่จบไปแล้ว (ลงรับ/ข้าม) แต่ชีตไม่มีบันทึกแล้ว = ถูกถอนออกจากประวัติที่หน้า 🔧 ถอยเลข
    เพื่อเอากลับมาลงรับใหม่ — ทิ้งงานเก่า ให้ไฟล์ที่มือถือส่งมาเข้าคิวแทน
    ของเดิมตอบมือถือว่า "จัดการไปแล้วโดยอีกเครื่อง" จนกว่าเซิร์ฟเวอร์จะรีสตาร์ท ดึงกลับเข้าคิวไม่ได้
    (ยิ่งตอนนี้งานที่ลงรับแล้วอยู่หลายวัน เพราะเก็บต้นฉบับไว้ลงรับใหม่ ดู REDO_KEEP)
    """
    clean = sanitize_phone_meta(meta)
    durable = _durable_phone_record(clean["book_id"])
    retired, claimed = [], None
    with _lock:
        matching = [j for j in _jobs.values()
                    if j.get("source") == "phone" and j.get("book_id") == clean["book_id"]]
        matching.sort(key=lambda j: j.get("created") or _OLDEST, reverse=True)
        for existing in matching:
            status = existing.get("status", "")
            if status in ("done", "skipped") and not durable:
                retired.append(_jobs.pop(existing["id"]))
                continue
            if status in ("done", "skipped"):
                raise AlreadyHandledError(
                    "registered" if status == "done" else "skipped",
                    existing.get("reserved_receipt") or existing.get("receipt_no", ""))
            if status == "error" and retry_failed:
                existing.update(clean)
                existing.update(user=user, status="uploading", error="",
                                step="กำลังรับไฟล์จากมือถือ...")
                claimed = (existing, True)
            else:
                # uploading/queued/analyzing/ready/saving/save_error/error ล้วนต้องใช้ job เดิม
                claimed = (existing, False)
            break
        if claimed is None:
            if durable and not clean.get("redo_no"):
                # มี redo_no = ตั้งใจลงรับซ้ำด้วยเลขเดิม จึงไม่ใช่การลงรับซ้ำโดยพลาด
                raise AlreadyHandledError(durable.get("status", "registered"),
                                          durable.get("receipt_no", ""))
            claimed = (_new_job_locked(user, source="phone", status="uploading",
                                       step="กำลังรับไฟล์จากมือถือ...", **clean), True)
    for old in retired:
        _drop_backup(old)           # ต้นฉบับที่เก็บไว้ (ถ้ามี) — ไฟล์ที่เพิ่งส่งมาใช้แทนแล้ว
        shutil.rmtree(old.get("dir") or "", ignore_errors=True)
    return claimed


def get_job(job_id: str, user: str):
    with _lock:
        j = _jobs.get(job_id)
    if not j:
        return None
    # งานจากมือถือเป็น "คิวกลาง" ของโรงเรียน ใครล็อกอินเว็บก็เข้าตรวจได้
    # (ผ่าน current_user มาแล้ว) ไม่งั้นเจ้าของ token "phone" กับคนรีวิวคนละชื่อ จะเปิดไม่ได้
    if j["user"] != user and j.get("source") != "phone":
        return None
    return j


def _sent_key(dt):
    return dt.strftime("%Y-%m-%d") if dt else "-"


def _sent_thai(dt):
    if not dt:
        return ""
    return to_thai_digits(f"{dt.day} {core.THAI_MONTHS_ABBR[dt.month - 1]} {dt.year + 543}")


# เรื่องจากมือถือที่เซิร์ฟเวอร์รับไฟล์ไว้แล้วและยังไม่จบ — มือถือไม่ต้องดึงจาก สพป. มาส่งซ้ำ
# ไม่นับ error (ให้มือถือส่งไฟล์ใหม่ทับได้ ดู retry_failed) และ uploading (ยังรับไฟล์ไม่จบ)
# ไม่นับที่จบแล้ว (ลงรับ/ข้าม) — อยู่ในประวัติที่มือถือได้ไปอยู่แล้ว ถ้าถูกถอนออกจากประวัติ
# (หน้า 🔧 ถอยเลข) แปลว่าจะเอากลับมาลงรับใหม่ ต้องให้มือถือดึงมาส่งได้ (ดู claim_phone_job)
_HELD = ("stored", "queued", "analyzing", "ready", "saving", "save_error", "skipping")


def phone_held_ids() -> set:
    """book_id ที่ดึงลงคิวรอลงรับแล้ว และยังไม่ได้ลงรับ/ข้าม (ดู _HELD)

    นับจากของที่เซิร์ฟเวอร์ถืออยู่จริง ไม่ได้จดแยกไว้ที่ไหน จึงไม่มีวันคลาดกับคิว
    ถ้าเซิร์ฟเวอร์ทำเรื่องไหนหาย (ล้มก่อนฝากไดร์ฟเสร็จ) เรื่องนั้นหลุดจากรายการ
    แล้วมือถือดึงมาส่งใหม่เองรอบหน้า — ถ้าจดไว้ในมือถือ เรื่องแบบนี้จะหายเงียบ
    """
    with _lock:
        return {str(j["book_id"]) for j in _jobs.values()
                if j.get("source") == "phone" and j.get("book_id")
                and j.get("status") in _HELD}


def phone_queue() -> list:
    """งานที่มือถือส่งเข้ามาและยังไม่จบ (ยังไม่ done/skip) — ใหม่สุดอยู่บน

    ต้องรวม status "error" ด้วย ไม่งั้นเรื่องที่ประมวลผลพังจะหายเงียบ
    ผู้ใช้จะเห็นแค่หน้าว่างทั้งที่สคริปต์บอกว่าส่งสำเร็จ — หาสาเหตุไม่ได้เลย
    """
    with _lock:
        rows = [(j.get("created"), _phone_row(j)) for j in _jobs.values()
                if j.get("source") == "phone" and j.get("status") in (
                    "uploading", "stored", "queued", "analyzing", "ready", "saving",
                    "skipping", "error", "save_error")]
    rows.sort(key=lambda r: r[0] or _OLDEST, reverse=True)
    return [r[1] for r in rows]


def phone_registered() -> list:
    """เรื่องจากมือถือที่ลงรับแล้วและยังเก็บต้นฉบับไว้ — ใหม่สุดอยู่บน

    ให้ขึ้นในรายการโหมด ๑ พร้อมปุ่ม ♻️ ลงรับใหม่ (บน Render รายการมีแต่ของจากมือถือ
    ถ้าไม่ใส่ไว้ เรื่องที่ลงรับแล้วจะหายจากจอทันที ลงรับผิดก็หาทางกลับไปแก้ไม่ได้)
    """
    _expire_kept()
    with _lock:
        rows = [(j.get("created"), _phone_row(j)) for j in _jobs.values()
                if j.get("status") == "done" and _keeps_original(j) and _has_original(j)]
    rows.sort(key=lambda r: r[0] or _OLDEST, reverse=True)
    return [r[1] for r in rows]


def _phone_row(j: dict) -> dict:
    return {
        "job_id": j["id"],
        "status": j.get("status"),
        "step": j.get("step", ""),
        "error": j.get("error", ""),
        "receipt_no": j.get("receipt_no", ""),
        "doc_no": j.get("doc_no", "-"),
        "doc_title": j.get("doc_title", "-"),
        "doc_date": j.get("doc_date", "-"),
        "sender": j.get("sender", "-"),
        "emoji": j.get("emoji", ""),
        "book_id": j.get("book_id", ""),
        "time": j["created"].strftime("%H:%M") if j.get("created") else "",
        # เติมให้หน้ารายการรวมใช้ได้เหมือนของที่ดึงจากเว็บ สพป.
        # ของ สพป. คือ "วันที่เว็บอัปโหลด" ของทางนี้คือ "วันที่มือถือส่งเข้ามา"
        "sent_key": _sent_key(j.get("created")),
        "sent_date": _sent_thai(j.get("created")),
        "sent_time": j["created"].strftime("%H:%M") if j.get("created") else "",
        "source": "phone",
    }


# ติดตัวเรื่องมาตั้งแต่มือถือส่ง ลงรับใหม่ต้องคงไว้ — ที่เหลือ (ผล AI ตำแหน่งที่วาง เลขที่จองไว้)
# ล้างทิ้งให้หมด โดยเฉพาะ reserved_receipt: ถ้าค้างไว้ ตอนบันทึกจะข้ามการเขียนทับแถวในทะเบียน
_PHONE_SEED = ("id", "user", "created", "dir", "source", "book_id", "doc_no", "doc_title",
               "doc_date", "sender", "emoji", "attach", "main_pages", "extras_got",
               "drive_file_id", "registered_at")


def reopen_phone(book_id: str, redo_no: str = None):
    """เปิดเรื่องจากมือถือที่ลงรับไปแล้วขึ้นมาทำใหม่ จากต้นฉบับที่เก็บไว้ (ดู REDO_KEEP)

    redo_no = ลงรับใหม่ด้วยเลขเดิม (เขียนทับแถวเดิมในทะเบียน ไม่กินเลขใหม่ ส่ง LINE ใหม่)
    ไม่ใส่ = ลงรับตามปกติ ได้เฉพาะเรื่องที่ไม่มีสถานะลงรับ/ข้ามแล้ว (ถอนออกจากประวัติที่หน้า 🔧 ถอยเลข)
    คืน None ถ้าเซิร์ฟเวอร์ไม่มีต้นฉบับ — ผู้เรียกไปดึงจากเว็บ สพป. แทน (ได้เฉพาะเครื่องที่ต่อเว็บได้)
    """
    bid = str(book_id or "").strip()

    def latest():
        mine = [j for j in _jobs.values() if j.get("source") == "phone" and j.get("book_id") == bid]
        return max(mine, key=lambda j: j.get("created") or _OLDEST) if mine else None

    with _lock:
        if not bid or latest() is None:
            return None             # เช็คในหน่วยความจำก่อน ไม่ต้องอ่านชีตทุกครั้งที่เปิดเรื่องจากเว็บ สพป.
    if not redo_no and _durable_phone_record(bid):
        return None
    with _lock:
        job = latest()
        status = job.get("status") if job else None
        if status in ("uploading", "queued", "analyzing", "ready", "saving", "save_error"):
            return job              # กำลังทำอยู่ (เปิดค้างไว้ หรืออีกเครื่องกดไปก่อน) — เปิดงานเดิมต่อ
        if status not in ("done", "error", "stored") or not _has_original(job):
            return None
        fresh = {"status": "stored", "step": "รอเปิดอ่าน", "error": "",
                 "redo_no": str(redo_no) if redo_no else None,
                 "redo_of": job.get("result") if status == "done" else job.get("redo_of")}
        # สถานะเปลี่ยนทีเดียวตอน update — หน้าที่ถามสถานะอยู่ (ไม่ได้ถือล็อก) ไม่เจองานที่ไม่มี status
        for k in [k for k in job if k not in _PHONE_SEED and k not in fresh]:
            del job[k]
        job.update(fresh)
    # เซิร์ฟเวอร์เกิดใหม่มาแล้ว โฟลเดอร์งานไม่มี — ไฟล์ดึงจากไดร์ฟลงมาตอนผู้ใช้เปิดเรื่อง
    os.makedirs(job["dir"], exist_ok=True)
    return job


# ==========================================================
# เปิดงานใหม่
# ==========================================================
def create_job(user: str) -> dict:
    _cleanup()
    with _lock:
        return _new_job_locked(user)


def _set(job, **kw):
    with _lock:
        job.update(kw)


def start_from_spp(user: str, book_id: str, session_state=None, redo_no: str = None) -> dict:
    """โหมดที่ ๑ — ดึงหนังสือจากเว็บ สพป. มาลงรับ

    redo_no = ลงรับใหม่โดยใช้เลขรับเดิม (ไม่กินเลขใหม่ และจะเขียนทับแถวเดิมในทะเบียน)
    """
    job = create_job(user)

    def work():
        try:
            _set(job, status="analyzing", step="กำลังเข้าสู่ระบบเว็บ สพป. ...")
            # session_state อาจเป็น PHPSESSID แบบเก่า หรือสถานะเต็มที่มี cookie
            # ทุกตัวกับ User-Agent; new_session สร้างตัวใหม่จึงไม่แชร์ Session ข้ามเธรด
            sess = sppweb.new_session(session_state) if session_state else None
            if sess is None or not sppweb.is_logged_in(sess):
                sess = sppweb.login()

            _set(job, step="กำลังเปิดหน้ารายละเอียดหนังสือ...")
            docs = {d["book_id"]: d for d in sppweb.list_documents(sess, pages=3)}
            meta = docs.get(str(book_id))
            if not meta:
                raise RuntimeError("ไม่พบหนังสือเรื่องนี้ในระบบแล้ว (อาจถูกย้ายหน้า)")
            det = sppweb.fetch_detail(sess, book_id)
            if not det["main_pdf"]:
                raise RuntimeError("หนังสือเรื่องนี้ไม่มีไฟล์ PDF แนบ")

            _set(job, step=f"กำลังดาวน์โหลดไฟล์ (แนบ {len(det['attachments'])} รายการ)...")
            pdf = os.path.join(job["dir"], "doc.pdf")
            main = os.path.join(job["dir"], "main.pdf")
            sppweb.download(sess, det["main_pdf"], main)
            extras = sppweb.download_extras(sess, det, job["dir"])
            main_pages = _combine(main, extras, pdf)
            _set(job, extras_got=len(extras) if main_pages else 0)

            _prepare(job, pdf,
                     doc_no=meta["doc_no"], doc_title=meta["doc_title"],
                     doc_date=meta["doc_date"], sender=meta["sender"],
                     emoji=det["emoji"], attach=sppweb.attach_text(det["attachments"]),
                     book_id=str(book_id), redo_no=redo_no, main_pages=main_pages)
        except Exception as e:
            _fail(job, e)

    _enqueue(job, work)
    return job


def start_from_upload(user: str, filename: str, data: bytes) -> dict:
    """โหมดที่ ๒ — อัปโหลดไฟล์เอง"""
    job = create_job(user)

    def work():
        try:
            _set(job, status="analyzing", step="กำลังเตรียมไฟล์...")
            pdf = os.path.join(job["dir"], "doc.pdf")
            ext = filename.lower().rsplit(".", 1)[-1]
            if ext in ("jpg", "jpeg", "png"):
                Image.open(io.BytesIO(data)).convert("RGB").save(pdf)
            else:
                with open(pdf, "wb") as f:
                    f.write(data)
            _prepare(job, pdf, doc_no="-", doc_title="-", doc_date="-", sender="-",
                     emoji="🔵", attach="📥 นำเข้าไฟล์โดยผู้ใช้งาน (Manual Import)",
                     book_id=None, redo_no=None)
        except Exception as e:
            _fail(job, e)

    _enqueue(job, work)
    return job


def start_from_upload_path(user: str, filename: str, source_path: str,
                           redo_no: str = None) -> dict:
    """รับ ownership ของไฟล์ที่ API stream ลงดิสก์แล้ว จึงไม่ต้องเก็บทั้งไฟล์ใน RAM

    redo_no = ลงรับใหม่ด้วยเลขเดิม ไม่กินเลขใหม่ ใช้ตอนที่แถวทะเบียนมีอยู่แล้ว
    แต่ไฟล์หาย (เช่นอัปขึ้นไดร์ฟไม่สำเร็จตอนนั้น) จะได้ไม่ต้องลบแถวทิ้งแล้ว
    เหลือช่องว่างในทะเบียนราชการ
    """
    job = create_job(user)
    pdf = os.path.join(job["dir"], "doc.pdf")
    os.replace(source_path, pdf)

    def work():
        try:
            _set(job, status="analyzing", step="กำลังเตรียมไฟล์...")
            ext = (filename or "").lower().rsplit(".", 1)[-1]
            if ext in ("jpg", "jpeg", "png"):
                converted = os.path.join(job["dir"], "converted.pdf")
                Image.open(pdf).convert("RGB").save(converted, "PDF", resolution=float(DPI))
                os.replace(converted, pdf)
            _prepare(job, pdf, doc_no="-", doc_title="-", doc_date="-", sender="-",
                     emoji="🔵", attach="📥 นำเข้าไฟล์โดยผู้ใช้งาน (Manual Import)",
                     book_id=None, redo_no=redo_no)
        except Exception as e:
            _fail(job, e)

    _enqueue(job, work)
    return job


def start_from_phone_path(user: str, source_path: str, meta: dict,
                          retry_failed: bool = False, extras=()):
    """โหมดที่ ๑ แต่คนไปดึงจาก สพป. คือ *มือถือ* (อุปกรณ์ที่เว็บอนุญาต)

    มือถือล็อกอิน/โหลด PDF เองจากเน็ตที่ผ่านด่านได้ แล้วส่งไฟล์ + ข้อมูล
    หนังสือมาที่นี่ เซิร์ฟเวอร์รับช่วงทำ AI เกษียณ + ตรายาง + LINE + ทะเบียน ต่อ
    โดยไม่ต้องแตะเว็บ สพป. เลย — ใช้ _prepare ตัวเดียวกับโหมด ๑ ปกติ
    extras = ไฟล์แนบ PDF อื่นของเรื่องเดียวกัน ต่อท้ายหนังสือนำเป็นเล่มเดียว (มือถือรวมเองไม่ได้)
    """
    job, created = claim_phone_job(user, meta, retry_failed=retry_failed)
    if not created:
        return job, False

    pdf = os.path.join(job["dir"], "doc.pdf")
    try:
        main_pages = _combine(source_path, list(extras), pdf)
        _set(job, main_pages=main_pages, extras_got=len(extras) if main_pages else 0)
    except Exception as e:
        _fail(job, e)
        raise

    # เก็บไฟล์ไว้เฉยๆ ยังไม่เข้าคิวประมวลผล — รอผู้ใช้แตะเปิดเรื่องนี้ก่อน
    # (ดูเหตุผลที่ prepare_stored)
    _set(job, status="stored", step="รอเปิดอ่าน")
    _backup_to_drive(job, pdf)
    return job, True


def start_from_phone(user: str, pdf_bytes: bytes, meta: dict,
                     retry_failed: bool = False) -> dict:
    """compatibility wrapper สำหรับผู้เรียกเดิม; API ใหม่ใช้ start_from_phone_path"""
    job, created = claim_phone_job(user, meta, retry_failed=retry_failed)
    if not created:
        return job
    pdf = os.path.join(job["dir"], "doc.pdf")
    try:
        with open(pdf, "wb") as f:
            f.write(pdf_bytes)
        # เก็บไว้ก่อน ยังไม่ประมวลผล (ดู prepare_stored)
        _set(job, status="stored", step="รอเปิดอ่าน")
        return job
    except Exception as e:
        _fail(job, e)
        raise


def _fail(job, e):
    try:
        with open(core._w("ai_error.log"), "a", encoding="utf-8") as f:
            f.write("[" + now_th().strftime("%Y-%m-%d %H:%M:%S") + "] เว็บ/เปิดงาน\n")
            f.write(traceback.format_exc())
    except Exception:
        pass
    _set(job, status="error", error=f"{type(e).__name__}: {e}")


# ==========================================================
# เตรียมข้อมูลให้หน้าจอแก้ไข
# ==========================================================
_ATTACH_URL = re.compile(r"👉\s*(\S+)")


def missing_extras(job) -> int:
    """ไฟล์แนบ PDF ที่เว็บ สพป. มีแต่ไม่ได้มาต่อท้ายเรื่องนี้ — หน้าจัดเอกสารเตือนให้รู้

    นับจากรายการไฟล์แนบในข้อความ LINE (attach) เทียบกับที่ได้มาจริง (extras_got)
    มือถือที่ยังใช้ phone_fetch.py รุ่นก่อนส่งมาแค่หนังสือนำ ไฟล์แนบหายเงียบโดยไม่มีอะไรบอก
    (ไฟล์ใหญ่เกินงบหรือโหลดไม่สำเร็จก็ข้ามเหมือนกัน) — รูป/Word/Excel ไม่นับ เพราะตั้งใจให้เป็นลิงก์
    """
    links = [u for u in _ATTACH_URL.findall(job.get("attach") or "")
             if sppweb._attachment_ext(u) in sppweb.PAGE_EXTENSIONS]
    if not any(sppweb._attachment_ext(u) == ".pdf" for u in links):
        return 0                                   # อัปโหลดเอง / ไม่มีรายการไฟล์แนบ
    expected = min(len(links) - 1, sppweb.MAX_EXTRA_FILES)     # ไฟล์ PDF แรก = หนังสือนำ
    got = job.get("extras_got")
    if got is None:          # กู้จากไดร์ฟที่ฝากไว้ก่อนมีตัวนับ: รู้แค่ว่ามีไฟล์แนบต่อท้ายหรือไม่
        got = expected if job.get("main_pages") else 0
    return max(0, expected - got)


def _combine(main: str, extras: list, pdf: str):
    """หนังสือนำ + ไฟล์แนบอื่นของเรื่องเดียวกัน -> pdf เล่มเดียว (หนังสือนำอยู่หน้าแรกๆ เสมอ)
    คืนจำนวนหน้าของหนังสือนำ หรือ None ถ้าไม่มีไฟล์แนบต่อท้าย — รวมไม่สำเร็จก็ใช้แค่หนังสือนำเหมือนเดิม"""
    if extras:
        try:
            return core.combine_attachments(main, extras, pdf)
        except Exception as e:
            print(f"รวมไฟล์แนบไม่สำเร็จ ใช้แค่หนังสือนำ: {type(e).__name__}: {e}")
    os.replace(main, pdf)
    return None


def _prepare(job, pdf_path, *, doc_no, doc_title, doc_date, sender, emoji, attach, book_id, redo_no=None,
             main_pages=None):
    # มีไฟล์แนบต่อท้าย (main_pages = จำนวนหน้าหนังสือนำ) — AI อ่านแค่หนังสือนำเหมือนเดิม
    # ไม่เปลืองโควตากับสิ่งที่ส่งมาด้วย และไม่หลงไปเอาลายเซ็นในไฟล์แนบเป็นหน้าลายเซ็นผู้ส่ง
    ai_pdf = pdf_path
    if main_pages:
        try:
            ai_pdf = core.pdf_head(pdf_path, main_pages, os.path.join(job["dir"], "ai.pdf"))
        except Exception as e:
            print(f"แยกหนังสือนำไม่สำเร็จ ให้ AI อ่านทั้งเล่ม: {type(e).__name__}: {e}")

    # คัดกรองผู้รับก่อน — ถ้าอ่านในเครื่องได้และไม่ใช่ของเรา จะได้ไม่เปลือง AI
    _set(job, step="กำลังตรวจบรรทัด 'เรียน' ในไฟล์...")
    recipient = extract_recipient_line(ai_pdf)
    category = classify_recipient(recipient)

    _set(job, step="กำลังให้ AI อ่านและเกษียณหนังสือ (ใช้เวลาสักครู่)...")
    ai_text, sig_page, ai_no, ai_title, ai_date, ai_sender, ai_recipient = generate_kasien_text(ai_pdf)

    if category == "unknown":
        # ไฟล์สแกนอ่านเองไม่ได้ ใช้บรรทัด "เรียน" ที่ AI อ่านมาแทน (ไม่เปลือง API เพิ่ม)
        recipient = ai_recipient if (ai_recipient and ai_recipient != "-") else recipient
        category = classify_recipient(recipient)

    doc_no = doc_no if doc_no != "-" else ai_no
    doc_title = doc_title if doc_title != "-" else ai_title
    doc_date = doc_date if doc_date != "-" else ai_date
    sender = sender if sender != "-" else ai_sender

    _set(job, step="กำลังเตรียมภาพหน้าเอกสาร...")
    p1, total = _render_pdf_page(pdf_path, 1, DPI)
    sig_page = max(1, min(sig_page, main_pages or total, total))
    psig = _render_pdf_page(pdf_path, sig_page, DPI)[0] if sig_page > 1 else None

    # ลงรับใหม่ -> ใช้เลขเดิม / ลงรับครั้งแรก -> ดูเลขถัดไปเฉยๆ (จองจริงตอนกดบันทึก)
    receipt_no = str(redo_no) if redo_no else get_next_receipt_no()

    # ตำแหน่งตรายาง — หาที่ว่างมุมขวาบนของหน้าแรก
    stamp = render_transparent_stamp(receipt_no, 100)
    sx, sy = find_stamp_pos(p1, stamp.width, stamp.height)

    # ตำแหน่งคำเกษียณ — ชิดลายเซ็นผู้ส่งบนหน้าที่มีลายเซ็น ขนาด ๑๐๐% เสมอ (ไม่ย่อ)
    # ที่ว่างข้างลายเซ็นไม่พอก็ยังวางข้างลายเซ็น ทับตัวหนังสือบ้าง ผู้ใช้ลากย้ายเอง (บอกไว้บนจอ)
    target = psig if psig is not None else p1
    kx, ky, ksize, kwrap = place_kasien(target, ai_text, first_page=psig is None, dpi=DPI)
    kbox = render_transparent_kasien(ai_text, int(target.width * 0.42 * kwrap / 100.0), ksize)
    kclear = kasien_clear_at(target, ai_text, kx, ky, dpi=DPI)

    pages = [{"name": "หน้าแรก", "png": _png_uri(_render_pdf_page(pdf_path, 1, PREVIEW_DPI)[0]),
              "w": p1.width, "h": p1.height, "index": 1}]
    if psig is not None:
        pages.append({"name": f"หน้าที่มีลายเซ็น (หน้า {sig_page})",
                      "png": _png_uri(_render_pdf_page(pdf_path, sig_page, PREVIEW_DPI)[0]),
                      "w": psig.width, "h": psig.height, "index": sig_page})

    # แสดงทุกหน้าในเอกสาร (ไม่เกิน MAX_REVIEW_PAGES) รวมสิ่งที่ส่งมาด้วยที่ต่อท้ายไว้ — วางของได้ทุกหน้า
    # เลือกได้ว่าจะส่งหน้าไหนเข้า LINE และเรื่องที่ขึ้นคำเตือน "อาจไม่ใช่หนังสือของโรงเรียนเรา"
    # ก็ได้ตรวจบัญชีรายชื่อที่มักอยู่หน้าท้ายๆ ด้วย
    # ภาพหน้าอื่นนอกจากหน้าแรก/หน้าลายเซ็น เบราว์เซอร์โหลดเองตอนเลื่อนไปถึง (preview_page)
    # ไม่เรนเดอร์ทุกหน้ารอไว้ก่อน เอกสาร ๕๐ หน้าบนเครื่องฟรีของ Render จะช้ามาก
    all_pages = False
    if total > len(pages):
        shown = {pg["index"] for pg in pages}
        sizes = core.page_sizes(pdf_path, DPI)  # w/h ที่ DPI เต็ม เพราะใช้คิดตำแหน่งเป็นซม.
        for n in range(1, min(total, MAX_REVIEW_PAGES) + 1):
            if n in shown:
                continue
            pages.append({"name": f"หน้า {n}", "png": f"/api/doc/{job['id']}/page/{n}",
                          "w": sizes[n][0], "h": sizes[n][1], "index": n})
        pages.sort(key=lambda pg: pg["index"])
        all_pages = True

    # หาลำดับของหน้าที่มีลายเซ็นจากลิสต์จริง "หลังเรียงแล้ว" ไม่ใช่เดาว่าเป็นตัวที่ ๑
    # ไม่งั้นพอแทรกหน้าอื่นเข้ามา คำเกษียณจะไปตกผิดหน้า
    kpage = (next((i for i, pg in enumerate(pages) if pg["index"] == sig_page), 0)
             if psig is not None else 0)

    # ชื่อโรงเรียนในเอกสาร — หาไว้ทุกเรื่อง (ไม่ถึงวินาที) ส่วนจะป้ายสีไหมขึ้นกับตั้งค่าบัญชีของคนที่เปิด
    # (เรื่องจากมือถือเตรียมไว้ก่อนมีใครเปิด จึงยังไม่รู้ว่าใครจะเป็นคนลงรับ)
    marks, scan_pages = _find_marks(pdf_path, pages)

    _set(job,
         status="ready", step="พร้อมแล้ว",
         pdf_path=pdf_path, book_id=book_id, total_pages=total, sig_page=sig_page,
         redo_no=(str(redo_no) if redo_no else None),
         doc_no=doc_no, doc_title=doc_title, doc_date=doc_date, sender=sender,
         emoji=emoji, attach=attach, recipient=recipient, category=category,
         all_pages=all_pages, marks=marks, scan_pages=scan_pages, main_pages=main_pages,
         receipt_no=receipt_no, pages=pages,
         stamp={"size_pct": 100, "page": 0,
                "left_cm": round(sx / CM, 2), "top_cm": round(sy / CM, 2),
                "png": _png_uri(stamp), "w": stamp.width, "h": stamp.height},
         boxes=[{"id": "b1", "text": ai_text, "size_pct": ksize, "wrap_pct": kwrap,
                 "indent_pct": 100, "bg": False, "border": False,
                 "page": kpage,
                 "left_cm": round(kx / CM, 2), "top_cm": round(ky / CM, 2),
                 "fitted": kclear,             # False = ทับตัวหนังสืออยู่ หน้าจอบอกให้ลากย้าย
                 "png": _png_uri(kbox), "w": kbox.width, "h": kbox.height}])

    # เปิดเว็บบนคอมเอง — สแกนหน้าภาพทุกหน้าเบื้องหลังเลย ไม่ต้องกดปุ่ม (หน้าจอแสดงความคืบหน้าที่ปุ่ม 🔎)
    if OCR_AUTO and scan_pages:
        try:
            start_ocr(job, scan_pages[0], scan_pages[-1], only=set(scan_pages), limit=MAX_REVIEW_PAGES, auto=True)
        except Exception as e:                 # ไม่มี Tesseract ฯลฯ — ยังกดปุ่มเองได้ หน้าจอบอกเหตุ
            print(f"สแกนหน้าภาพเองไม่ได้: {type(e).__name__}: {e}")


def _find_marks(pdf_path: str, pages: list) -> tuple:
    """ที่ป้ายสีชื่อโรงเรียนบนหน้าที่แสดง [{page: ลำดับหน้าบนจอ, x, y, w, h}] (จุดที่ DPI เต็ม)
    กับเลขหน้าที่เป็นภาพสแกนซึ่งหาชื่อไม่ได้ — หาไม่สำเร็จ (ไฟล์แปลก) ถือว่าทุกหน้าต้องดูเอง"""
    try:
        found, scans = find_name_marks(pdf_path, [pg["index"] for pg in pages], DPI)
    except Exception as e:
        print(f"หาชื่อโรงเรียนในไฟล์ไม่สำเร็จ: {type(e).__name__}: {e}")
        return [], sorted(pg["index"] for pg in pages)
    marks = [{"page": i, "x": round(x0), "y": round(y0), "w": round(x1 - x0), "h": round(y1 - y0)}
             for i, pg in enumerate(pages) for x0, y0, x1, y1 in found.get(pg["index"], ())]
    return marks, sorted(scans)


def preview_page(job, n: int) -> str:
    """ภาพหน้าที่ n สำหรับหน้าจอ — เรนเดอร์ตอนเบราว์เซอร์เลื่อนไปถึงหน้านั้นจริง แล้วเก็บไว้ในโฟลเดอร์งาน
    คืน "" ถ้าไม่ใช่หน้าที่แสดงอยู่ของเรื่องนี้ หรือเรื่องจบไปแล้ว (ไฟล์ถูกเก็บกวาด)

    JPEG คุณภาพ ๗๐: หน้าสแกนเป็น PNG หนักราว ๕๘๐ KB (วัดจากของจริง) JPEG เหลือ ~๑๑๖ KB
    เล็กลง ๕ เท่า และยังอ่านชื่อโรงเรียนในบัญชีรายชื่อแนบท้ายออกสบาย
    """
    pdf = job.get("pdf_path") or ""
    if not any(pg["index"] == n for pg in job.get("pages") or ()) or not os.path.exists(pdf):
        return ""
    path = os.path.join(job["dir"], f"preview{n}.jpg")
    if not os.path.exists(path):
        img = _render_pdf_page(pdf, n, PREVIEW_DPI)[0]
        part = f"{path}.{threading.get_ident()}.part"     # สองแท็บขอหน้าเดียวกันพร้อมกันได้
        img.convert("RGB").save(part, "JPEG", quality=70, optimize=True)
        os.replace(part, path)
    return path


def ocr_state(job) -> dict:
    """สถานะการสแกนหาชื่อของเรื่องนี้ ให้หน้าจอแสดงความคืบหน้า (ready = เครื่องนี้มีตัวอ่านภาพไหม)"""
    state = dict(job.get("ocr") or {})
    state.pop("stop", None)
    state.update(ready=bool(core.tesseract_cmd()), sec_per_page=round(_ocr_sec[0], 1),
                 max_pages=OCR_MAX_PAGES)
    return state


def start_ocr(job, first: int, last: int, *, only=None, limit=None, auto=False) -> dict:
    """ปุ่ม 🔎 สแกน — อ่านหน้า first..last (เลขหน้าจริง) ด้วยตัวอ่านภาพ หาชื่อโรงเรียนในหน้าที่เป็นภาพสแกน

    ผู้ใช้เลือกช่วงเอง (ปกติคือหน้าบัญชีรายชื่อ) เพราะอ่านช้า ทำเบื้องหลังทีละหน้า หน้าจอถามความคืบหน้าเอง
    หน้าที่เจอชื่อแล้ว (จากตัวหนังสือในไฟล์) ไม่อ่านซ้ำ — เจอชื่อเพิ่มก็ต่อท้าย job["marks"] ให้ป้ายสีได้เหมือนกัน
    only = อ่านเฉพาะหน้าพวกนี้ในช่วงนั้น / auto = ระบบสแกนเองตอนเปิดเรื่อง (เปิดเว็บบนคอม — OCR_AUTO)
    """
    if not core.tesseract_cmd():
        raise OcrUnavailable("เครื่องนี้ยังไม่มีตัวอ่านภาพ (Tesseract ภาษาไทย) — สแกนหาชื่อในหน้าภาพไม่ได้")
    if job.get("status") not in ("ready", "save_error"):
        raise JobStateError("เรื่องนี้ไม่ได้อยู่ในหน้าจัดเอกสารแล้ว", status=job.get("status", ""))
    first, last = min(first, last), max(first, last)
    limit = OCR_MAX_PAGES if limit is None else limit
    found = {job["pages"][m["page"]]["index"] for m in job.get("marks") or ()}
    pages = [pg["index"] for pg in job["pages"] if first <= pg["index"] <= last and pg["index"] not in found
             and (only is None or pg["index"] in only)]
    if not any(first <= pg["index"] <= last for pg in job["pages"]):
        raise ValueError("ช่วงหน้าที่จะสแกนไม่มีอยู่ในเรื่องนี้")
    with _lock:
        current = job.get("ocr") or {}
        if current.get("status") in ("queued", "running"):
            return ocr_state(job)             # กดซ้ำระหว่างสแกนอยู่ ไม่เริ่มใหม่
        job["ocr"] = state = {"status": "queued", "first": first, "last": last,
                              "pages": pages[:limit], "cut": len(pages) > limit, "auto": auto,
                              "done": 0, "current": None, "found": [], "error": "", "stop": False}
    _ocr_tasks.put(lambda: _run_ocr(job, state))
    return ocr_state(job)


def stop_ocr(job) -> dict:
    state = job.get("ocr")
    if state:
        state["stop"] = True
    return ocr_state(job)


def _run_ocr(job, state):
    state["status"] = "running"
    try:
        for i, n in enumerate(state["pages"]):
            # กดหยุด / เริ่มสแกนช่วงใหม่ / บันทึกหรือไม่รับเรื่องนี้ไปแล้ว — เลิกอ่านหน้าที่เหลือ
            if state["stop"] or job.get("ocr") is not state or job.get("status") not in ("ready", "save_error"):
                state["status"] = "stopped"
                return
            state["current"] = n
            began = time.time()
            boxes = core.ocr_name_marks(job["pdf_path"], n, DPI)
            _ocr_sec[0] = (_ocr_sec[0] + time.time() - began) / 2
            pi = next(k for k, pg in enumerate(job["pages"]) if pg["index"] == n)
            new = [{"page": pi, "x": round(x0), "y": round(y0), "w": round(x1 - x0), "h": round(y1 - y0),
                    "ocr": True} for x0, y0, x1, y1 in boxes]
            with _lock:
                if new:
                    job["marks"] = list(job.get("marks") or []) + new
                    state["found"].append(n)
                state["done"] = i + 1
        state["status"] = "done"
    except Exception as e:
        state.update(status="error", error=f"{type(e).__name__}: {e}"[:300])
    finally:
        state["current"] = None


def _blank_size(job) -> tuple:
    """ขนาดหน้ากระดาษเปล่า = ขนาดหน้าที่มีลายเซ็น ซึ่งหน้าเปล่าแทรกต่ออยู่
    (เดิมหน้าจอใช้ขนาดหน้าสุดท้าย แต่ไฟล์ใช้หน้าแรก ถ้าสองหน้านั้นขนาดไม่เท่ากัน ตำแหน่งจะคลาด)"""
    pg = next((p for p in job["pages"] if p["index"] == job.get("sig_page")), job["pages"][0])
    return pg["w"], pg["h"]


def _page_w(job, pi) -> int:
    """ความกว้างหน้าลำดับที่ pi บนหน้าจอ (ลำดับเกินจำนวนหน้า = หน้ากระดาษเปล่า)"""
    pi = int(pi or 0)
    return job["pages"][pi]["w"] if 0 <= pi < len(job["pages"]) else _blank_size(job)[0]


# ==========================================================
# วาดใหม่เมื่อผู้ใช้แก้
# ==========================================================
def render_box(job, box: dict) -> dict:
    """วาดกล่องคำเกษียณใหม่ตามข้อความ/ขนาด/ความกว้างที่ผู้ใช้ปรับ"""
    page_w = _page_w(job, box.get("page", 0))
    max_w = max(10, int(page_w * 0.42 * (box.get("wrap_pct", 100) / 100.0)))
    img = render_transparent_kasien(box.get("text", ""), max_w,
                                    box.get("size_pct", 100), box.get("indent_pct", 100),
                                    bool(box.get("bg")), bool(box.get("border")))
    return {"png": _png_uri(img), "w": img.width, "h": img.height}


def _num(value, default, lo, hi):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def fit_box(job, data: dict) -> dict:
    """ปุ่ม 📍 ย้ายไปใกล้ลายเซ็น — จัดกล่องคำเกษียณใหม่ให้ชิดลายเซ็นผู้ส่ง (กดเองเท่านั้น)

    ใช้หลังแก้ข้อความจนกล่องสูงขึ้นไปทับตัวหนังสือ หรือลากไปไว้ไกล — ขนาด/ความกว้างคงเดิม
    (ไม่ย่อ) หาที่ว่างก่อน ไม่มีที่ว่างใกล้ๆ ก็วางชิดลายเซ็นทั้งที่ทับตัวหนังสือบ้าง (fitted=False)
    data: page = หน้าที่กล่องอยู่ตอนนี้ — ถ้าเป็นหน้าเอกสาร หาที่บนหน้านั้น (AI ชี้หน้าลายเซ็นผิด
                 ผู้ใช้ลากไปหน้าที่ถูกแล้วกดได้) ถ้าอยู่บนกระดาษเปล่า กลับไปหน้าที่มีลายเซ็น
          box = ข้อความ/ขนาด/ความกว้าง/ย่อหน้า/พื้น/กรอบ/ตำแหน่ง ของกล่องนี้
          sig = {w, h} ลายเซ็นผู้ลงรับที่ย้ายตามไปไว้ใต้กล่อง (ไม่ส่งมา = ไม่ได้แทรก)
          avoid = [{page, left_cm, top_cm, w, h}] ของอื่นที่วางไว้แล้ว ห้ามทับ
    """
    box = data.get("box") or {}
    n_pages = len(job["pages"])
    cur = int(_num(data.get("page"), -1, -1, n_pages))
    kpage = cur if 0 <= cur < n_pages else next(
        (i for i, p in enumerate(job["pages"]) if p["index"] == job.get("sig_page")), 0)
    size = int(_num(box.get("size_pct"), 100, 20, 300))
    wrap = int(_num(box.get("wrap_pct"), 100, 30, 250))
    indent = int(_num(box.get("indent_pct"), 100, 0, 400))
    s = data.get("sig") if isinstance(data.get("sig"), dict) else None
    below = (int(_num(s.get("w"), 1, 1, 4000)), int(_num(s.get("h"), 1, 1, 4000))) if s else None
    others = []
    for o in (data.get("avoid") or [])[:20]:
        if isinstance(o, dict):
            x, y = _num(o.get("left_cm"), 0, 0, 100) * CM, _num(o.get("top_cm"), 0, 0, 100) * CM
            others.append((int(_num(o.get("page"), -1, -1, n_pages)),
                           (x, y, x + _num(o.get("w"), 0, 0, 5000), y + _num(o.get("h"), 0, 0, 5000))))
    text = str(box.get("text") or "")[:3000]       # คำเกษียณจริงไม่กี่บรรทัด กันวาดข้อความยาวผิดปกติ
    style = dict(indent_pct=indent, bg=bool(box.get("bg")), border=bool(box.get("border")),
                 below=below, dpi=DPI)

    def answer(page, x, y, size, wrap, fitted):
        out = render_box(job, dict(box, size_pct=size, wrap_pct=wrap, indent_pct=indent, page=page))
        out.update(page=page, left_cm=round(x / CM, 2), top_cm=round(y / CM, 2),
                   size_pct=size, wrap_pct=wrap, fitted=fitted)
        return out

    page_no = job["pages"][kpage]["index"]
    page_img = _render_pdf_page(job["pdf_path"], page_no, DPI)[0]
    avoid = [r for p, r in others if p == kpage]
    x, y, size, wrap = place_kasien(page_img, text, size_pct=size, wrap_pct=wrap,
                                    first_page=page_no == 1, avoid=avoid, **style)
    fitted = kasien_clear_at(page_img, text, x, y, size_pct=size, wrap_pct=wrap, avoid=avoid, **style)
    return answer(kpage, x, y, size, wrap, fitted)


def render_stamp(job, size_pct: int, date_str: str, time_str: str) -> dict:
    img = render_transparent_stamp(job["receipt_no"], size_pct, date_str, time_str)
    return {"png": _png_uri(img), "w": img.width, "h": img.height}


# ==========================================================
# บันทึก
# ==========================================================
def _claim_save(job: dict):
    """เปลี่ยน ready/save_error -> saving ในล็อกเดียว กันการกดพร้อมกันสองหน้าจอ"""
    with _lock:
        status = job.get("status", "")
        if status == "done":
            return job.get("result") or {"ok": True,
                                         "receipt_no": job.get("reserved_receipt") or
                                                       job.get("receipt_no", "")}
        if status == "saving":
            raise JobStateError("งานนี้กำลังบันทึกอยู่", status=status)
        if status in ("skipping", "skipped"):
            raise JobStateError("งานนี้กำลังถูกข้ามหรือข้ามไปแล้ว", status=status)
        if status not in ("ready", "save_error"):
            raise JobStateError("งานนี้ยังไม่พร้อมบันทึก", status=status)
        job.update(status="saving", step="กำลังสร้างไฟล์และลงทะเบียน...", error="")
    return None


def _reserve_for_job(job: dict, reserve_fn, store):
    """จองเลขเพียงครั้งเดียว; retry หลังพังต้องใช้เลขเดิมเสมอ"""
    receipt_no = str(job.get("reserved_receipt") or "")
    if not receipt_no:
        # ด่านสุดท้ายก่อนกินเลข — เรื่องนี้อาจถูกลงรับจากที่อื่นไปแล้วหลังงานนี้ถูกสร้าง
        # (เช่น มือถือส่งเรื่องเข้าคิวไว้ แล้วผู้ใช้ไปกดลงรับเรื่องเดียวกันจากเว็บ สพป. ในโหมด ๑)
        # ถ้าไม่เช็คตรงนี้ เรื่องเดียวจะกินเลขรับสองเลขและมีสองแถวในทะเบียน
        # ยกเว้น redo_no ซึ่งตั้งใจลงรับซ้ำด้วยเลขเดิมอยู่แล้ว
        if job.get("book_id") and not job.get("redo_no"):
            handled = _durable_phone_record(job["book_id"])
            if handled:
                raise AlreadyHandledError(handled.get("status", "registered"),
                                          handled.get("receipt_no", ""))
        if job.get("redo_no"):
            receipt_no = str(job["redo_no"])
            updated = store.update_registry_row(
                receipt_no, job["doc_no"], job["doc_date"], job["sender"], job["doc_title"])
            if not updated:
                raise RuntimeError(f"ไม่พบเลขรับ {receipt_no} ในทะเบียน จึงลงรับใหม่ไม่ได้")
        else:
            receipt_no = reserve_fn(
                doc_no=job["doc_no"], doc_date=job["doc_date"],
                sender=job["sender"], doc_title=job["doc_title"])
        _set(job, reserved_receipt=str(receipt_no), receipt_no=str(receipt_no))

    # เขียน book_id -> เลขรับทันทีหลังจองเลข เพื่อกันเครื่องดับ/รีสตาร์ตแล้วมือถือ
    # ส่งซ้ำและสร้างทะเบียนอีกแถว ไฟล์/LINE ที่ยังไม่เสร็จจะแสดงเป็น partial failure ใน job
    if job.get("book_id") and not job.get("durable_claimed"):
        store.mark_registered(job["book_id"], receipt_no)
        _set(job, durable_claimed=True)
    return str(receipt_no)


def finalize(job, payload: dict, reserve_fn, signature_png: bytes = None) -> dict:
    # ด่านตรวจก่อนแตะเลขรับ — ต้องมั่นใจว่าอัปไฟล์ขึ้นไดร์ฟได้จริงก่อน
    # เช็คตรงนี้ (ก่อน _claim_save) เพื่อให้งานยังอยู่สถานะ ready กดใหม่ได้เลย
    import drive as _dr
    st = _dr.ready()
    if not st.get("ok"):
        raise DriveNotReadyError(
            "อัปไฟล์ขึ้นไดร์ฟไม่ได้ จึงยังไม่ลงรับให้ เพราะจะได้เลขที่ไม่มีเอกสาร: "
            + str(st.get("error", ""))[:200])

    existing = _claim_save(job)
    if existing is not None:
        return existing
    try:
        result = _finalize_claimed(job, payload, reserve_fn, signature_png)
    except Exception as e:
        _set(job, status="save_error", step="บันทึกยังไม่สมบูรณ์",
             error=f"{type(e).__name__}: {e}")
        raise
    _set(job, status="done", step="บันทึกเรียบร้อย", error="", result=result,
         registered_at=now_th())
    # เรื่องจากมือถือเก็บต้นฉบับไว้อีก REDO_DAYS วัน ลงรับผิดจะได้กด ♻️ ลงรับใหม่ได้
    if not _keeps_original(job):
        _drop_backup(job)
    return result


def _highlighted(payload: dict, page_no: int) -> bool:
    """ป้ายสีชื่อโรงเรียนหน้านี้ไหม — highlight = เลขหน้าที่เปิดไฮไลท์ไว้บนจอ
    (หน้าเว็บรุ่นก่อนส่ง true/false ทั้งเรื่อง ยังรับอยู่ เผื่อเปิดค้างไว้ตอนอัปเดตระบบ)"""
    hl = payload.get("highlight")
    if isinstance(hl, list):
        return any(str(n).isdigit() and int(n) == page_no for n in hl)
    return hl is True


def _finalize_claimed(job, payload: dict, reserve_fn, signature_png: bytes = None) -> dict:
    """วางตรายาง+คำเกษียณ(+ลายเซ็น)ลงหน้าจริง รวมเป็น PDF ลงทะเบียน แล้วส่ง LINE"""
    date_str = normalize_typed_date(payload.get("date", "")) or get_thai_date()
    time_str = to_thai_digits(str(payload.get("time", "")).strip()) or get_thai_time_rounded()
    st = payload.get("stamp", {})
    boxes = payload.get("boxes", [])

    import store as _st
    store = _st.get_store()
    receipt_no = _reserve_for_job(job, reserve_fn, store)

    out_pages = {}          # index หน้าใน PDF -> รูปที่วางของเสร็จแล้ว
    total = job["total_pages"]
    used = set()            # เลขหน้าจริงที่มีตรายาง/คำเกษียณ/ลายเซ็น (total+1 = หน้ากระดาษเปล่า)
    on_blank = []           # ของที่ลากไปวางบนหน้ากระดาษเปล่า (ลำดับหน้าบนจอ = จำนวนหน้า)

    def pdf_page(pi):
        return total + 1 if pi >= len(job["pages"]) else job["pages"][pi]["index"]

    def layer(pi):
        if pi not in out_pages:
            out_pages[pi] = _render_pdf_page(job["pdf_path"], job["pages"][pi]["index"], DPI)[0].convert("RGBA")
        return out_pages[pi]

    def put(page, img, x, y):
        """วางของลงหน้าลำดับ page บนจอ — เกินจำนวนหน้า = หน้ากระดาษเปล่าที่ผู้ใช้กดแทรก"""
        pi = max(0, min(int(page or 0), len(job["pages"])))
        used.add(pdf_page(pi))
        if pi == len(job["pages"]):
            on_blank.append((img, x, y))
            return
        bg = layer(pi)
        bg.paste(img, (max(0, min(x, bg.width - img.width)), max(0, min(y, bg.height - img.height))), img)

    # ไฮไลท์ชื่อโรงเรียนเฉพาะหน้าที่เปิดไว้บนจอ — ป้ายก่อนวางของอื่น ตรายาง/คำเกษียณที่ทับจะได้ไม่ติดสีเหลือง
    for m in job.get("marks") or ():
        if 0 <= m["page"] < len(job["pages"]) and _highlighted(payload, job["pages"][m["page"]]["index"]):
            paint_marks(layer(m["page"]), [(m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])])

    stamp_img = render_transparent_stamp(receipt_no, int(st.get("size_pct", 100)), date_str, time_str)
    put(st.get("page", 0), stamp_img,
        int(float(st.get("left_cm", 1)) * CM), int(float(st.get("top_cm", 1)) * CM))

    for b in boxes:
        if not (b.get("text") or "").strip():
            continue
        max_w = max(10, int(_page_w(job, b.get("page", 0)) * 0.42 * (b.get("wrap_pct", 100) / 100.0)))
        k = render_transparent_kasien(b.get("text", ""), max_w, b.get("size_pct", 100),
                                      b.get("indent_pct", 100), bool(b.get("bg")), bool(b.get("border")))
        put(b.get("page", 0), k, int(float(b.get("left_cm", 1)) * CM), int(float(b.get("top_cm", 1)) * CM))

    # ลายเซ็น + วันที่ใต้ลายเซ็น (แทรกเฉพาะตอนผู้ใช้กดแทรก) — วางอิสระจากกล่องคำเกษียณ
    sig = payload.get("signature")
    if sig and signature_png:
        # วันที่ใต้ลายเซ็นใช้วันที่ลงรับ (ตัวเดียวกับที่ประทับบนตรายาง) เสมอ
        k = sigmod.render_block(signature_png, sigmod.short_date_of(date_str),
                                sig.get("size_pct", 100), CM)
        put(sig.get("page", 0), k, int(float(sig.get("left_cm", 1)) * CM), int(float(sig.get("top_cm", 1)) * CM))

    # ---- ประกอบเป็น PDF ----
    d = job["dir"]
    made = {}
    for pi, img in out_pages.items():
        p = os.path.join(d, f"page{pi}.pdf")
        img.convert("RGB").save(p, "PDF", resolution=float(DPI))
        made[job["pages"][pi]["index"]] = p

    # หน้ากระดาษเปล่ามีเฉพาะตอนผู้ใช้กดแทรกบนจอ (blank) — แทรกไว้แล้วไม่ได้วางอะไรก็ยังอยู่ในไฟล์
    # ตามที่เห็นบนจอ (เผื่อเว้นไว้เขียนสั่งการด้วยมือ) / หน้าเว็บรุ่นก่อนไม่ส่ง blank มา ดูจากของที่วางแทน
    blank_pdf = blank = None
    if on_blank or payload.get("blank") is True:
        blank = Image.new("RGB", _blank_size(job), "white")
        for k, x, y in on_blank:
            blank.paste(k, (max(0, min(x, blank.width - k.width)),
                            max(0, min(y, blank.height - k.height))), k)
        blank_pdf = os.path.join(d, "blank.pdf")
        blank.save(blank_pdf, "PDF", resolution=float(DPI))

    # หน้ากระดาษเปล่าแทรกต่อจากหน้าที่มีลายเซ็น คำเกษียณจะได้อยู่ติดลายเซ็น
    # (เดิมต่อท้ายเล่ม ไปอยู่หลังสิ่งที่ส่งมาด้วยทั้งหมด)
    order = page_order(total, (job.get("sig_page") or 1) if blank_pdf else None)
    merger = PdfMerger()
    for n in order:
        if n > total:
            merger.append(blank_pdf)
        elif n in made:
            merger.append(made[n])                       # หน้าที่วางของแล้ว
        else:
            merger.append(job["pdf_path"], pages=(n - 1, n))

    today = core.day_folder()          # "๒๕๖๙/๐๘ สิงหาคม/๒๘" — ซอยเป็น ปี/เดือน/วัน
    folder = os.path.join(core.OUTPUT_ROOT, today)
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, safe_output_filename(job.get("doc_no", ""), receipt_no))
    merger.write(out)
    merger.close()

    # บน hosting ดิสก์ถูกล้างเมื่อรีสตาร์ท จึงต้องส่งขึ้น Drive ให้อยู่ถาวร
    # (บนคอมที่มี Drive for Desktop ซิงก์อยู่แล้ว ตัวนี้จะถูกปิดไว้ ไม่ทำงานซ้ำซ้อน)
    drive_link = ""
    drive_error = ""
    try:
        import drive as _dr
        drive_link = (_dr.upload(out, day=today) or {}).get("link", "")
    except Exception as e:
        # เดิมเป็น except: pass เงียบสนิท ไฟล์ไม่ขึ้นไดร์ฟโดยไม่มีอะไรบอกสักตัว
        # (ที่เจอจริง: Google เลิกให้พื้นที่เก็บกับ service account แล้ว ตอบ 403
        #  "Service Accounts do not have storage quota" แต่ log ว่างเปล่า
        #  จนต้องไปไล่ถาม Drive API เองถึงรู้)
        drive_error = f"{type(e).__name__}: {e}"
        print("อัปไฟล์ขึ้นไดร์ฟไม่สำเร็จ: " + drive_error)

    # ---- ส่งเข้า LINE ----
    line_ok = None
    photo_ok = False          # รูปกับข้อความล้มแยกกันได้ ต้องรายงานแยกกัน
    try:
        pi_of = {p["index"]: i for i, p in enumerate(job["pages"])}
        chosen = payload.get("line_pages")
        if isinstance(chosen, list):            # เลือกเองบนหน้าจอ (ไม่เลือกสักหน้า = ข้อความอย่างเดียว)
            pages = chosen_line_pages(chosen, total, blank is not None, order)
        else:                                   # หน้าเว็บรุ่นเก่าไม่ได้ส่งมา ใช้กติกาเดิม
            pages = auto_line_pages(used, order)
        urls = []
        for n in pages:
            if n > total:                       # หน้ากระดาษเปล่าที่ลงเกษียณไว้
                img = blank
            elif n in pi_of:                    # หน้าที่เปิดให้วางของ (วางแล้วหรือยังก็ได้)
                img = layer(pi_of[n])
            else:                               # หน้าที่อยู่ระหว่างกลาง ไม่มีอะไรวาง
                img = _render_pdf_page(job["pdf_path"], n, DPI)[0]
            # เก็บรูปหน้าที่ส่ง LINE ไว้คู่กับไฟล์ PDF — โฟลเดอร์เดียวกัน ชื่อตามไฟล์ PDF + เลขหน้า
            # (เลขหน้าตามลำดับใน PDF ที่บันทึก หน้ากระดาษเปล่าอยู่ต่อจากหน้าลายเซ็น)
            p = os.path.join(folder, f"{os.path.splitext(os.path.basename(out))[0]}"
                                     f"_หน้า{to_thai_digits(order.index(n) + 1)}.jpg")
            img.convert("RGB").save(p, "JPEG", quality=88)
            # บน hosting ให้ LINE มาดึงจากเราเอง; ในเครื่องถอยไปใช้ imgbb เหมือนเดิม
            url = publish_line_image(p, day=today) or upload_to_imgbb(p)
            if url:
                urls.append(url)
        photo_ok = len(urls) == len(pages)
        dd = job["doc_date"]
        shown = (format_scraped_date(dd) if ("ม.ค." not in dd and "ก.พ." not in dd and dd != "-")
                 else to_thai_digits(dd))
        kasien_line = " | ".join(" ".join((b.get("text") or "").split())
                                 for b in boxes[:2] if (b.get("text") or "").strip())
        # ลงรับใหม่ด้วยเลขเดิม — กลุ่มจะเห็นเลขรับเดียวกันสองข้อความ ต้องบอกว่าอันไหนใช้จริง
        redo = "♻️ ลงรับใหม่ (แก้ไข) — ใช้ข้อความนี้แทนข้อความก่อนหน้า\n" if job.get("redo_no") else ""
        msg = (f"{redo}📌 เลขที่รับ {receipt_no}\n"
               f"{job['emoji']} {to_thai_digits(job['doc_no'])}\n"
               f"🆕เรื่อง: {job['doc_title']}\n"
               f"🌟หนังสือลงวันที่ : {shown}\n"
               f"⚠️คำเกษียนหนังสือ:{kasien_line}\n"
               f"{job['attach']}")
        line_ok = bool(send_line_with_image(msg, urls))
    except Exception:
        line_ok = False

    return {"ok": True, "receipt_no": receipt_no, "line_ok": line_ok,
            "photo_ok": photo_ok, "drive_error": drive_error[:300],
            "drive_link": drive_link,
            "filename": os.path.basename(out),
            # เข้ารหัส URL — ที่อยู่มีอักษรไทยและเว้นวรรค ("๐๘ สิงหาคม")
            "download": "/api/doc/download/" + quote(today) + "/" + quote(os.path.basename(out))}


def skip(job):
    """ไม่รับเอกสารนี้ — จำไว้ว่าข้ามแล้ว จะได้ไม่ดึงซ้ำ

    งานลงรับใหม่ (มี redo_no) คือเรื่องที่ลงรับไว้แล้ว กดปุ่มนี้แค่ "ยกเลิกการลงรับใหม่"
    ห้ามจดว่าข้าม — ของเดิมจดลงชีต เรื่องที่มีแถวในทะเบียนอยู่แล้วเลยกลายเป็น "ข้าม" ในรายการ
    """
    with _lock:
        status = job.get("status", "")
        if status == "skipped":
            return {"ok": True, "existing": True}
        if status in ("saving", "done", "skipping"):
            raise JobStateError("ข้ามงานนี้ไม่ได้ เพราะกำลังบันทึกหรือบันทึกแล้ว", status=status)
        if job.get("redo_no"):
            return _cancel_redo_locked(job)
        previous = status
        job.update(status="skipping", step="กำลังบันทึกว่าไม่รับเอกสาร...")
    try:
        if job.get("book_id"):
            import store as _st
            _st.get_store().mark_skipped(job["book_id"])
    except Exception:
        _set(job, status=previous, step="ข้ามเอกสารไม่สำเร็จ")
        raise
    result = {"ok": True}
    _set(job, status="skipped", step="ข้ามเอกสารแล้ว", result=result)
    _drop_backup(job)
    return result


def _cancel_redo_locked(job) -> dict:
    """เลิกลงรับใหม่ (ถือ _lock อยู่) — เรื่องนี้ยังลงรับไว้ด้วยเลขเดิมเหมือนก่อนกด ไม่แตะทะเบียน/ชีต"""
    no = str(job["redo_no"])
    if _keeps_original(job) and _has_original(job):
        # กลับเป็นเรื่องที่ลงรับแล้ว ต้นฉบับยังเก็บไว้ กด ♻️ ลงรับใหม่ได้อีกจนพ้นกำหนด
        job.update(status="done", step="ลงรับแล้ว", error="", receipt_no=no,
                   result=job.get("redo_of") or {"ok": True, "receipt_no": no})
    else:
        job.update(status="skipped", step="ยกเลิกการลงรับใหม่แล้ว", error="",
                   result={"ok": True, "cancelled": True, "receipt_no": no})
    return {"ok": True, "cancelled": True, "receipt_no": no}
