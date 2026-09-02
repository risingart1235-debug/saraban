"""docmode.py — โหมดที่ ๑ (ดึงจากเว็บ สพป.) และโหมดที่ ๓ (อัปโหลดไฟล์เอง)

งานหนัก (โหลดไฟล์ + ให้ AI อ่าน) ใช้เวลาหลายสิบวินาที ถ้าให้เบราว์เซอร์รอ
คำตอบเดียวจนจบ มือถือมักตัดการเชื่อมต่อก่อน จึงทำเป็น "งาน" (job):
  ๑. เปิดงาน -> คืน job_id ทันที แล้วไปทำเบื้องหลัง
  ๒. เบราว์เซอร์ถามสถานะเป็นระยะจนกว่าจะพร้อม
"""
import os
import io
import re
import base64
import queue
import secrets
import shutil
import threading
import traceback
import unicodedata
import uuid
from datetime import datetime, timedelta

from urllib.parse import quote

import core
from core import now_th
import sppweb
from core import (
    Image,
    render_transparent_stamp, render_transparent_kasien,
    find_stamp_pos, find_kasien_pos,
    generate_kasien_text, extract_recipient_line, classify_recipient,
    get_thai_date, get_thai_time_rounded, normalize_typed_date, to_thai_digits,
    format_scraped_date, get_next_receipt_no,
    upload_to_imgbb, send_line_with_image, PdfMerger,
)

DPI = 200                      # ความละเอียดที่ใช้ทำงานจริง (เท่ากับเวอร์ชันเดสก์ท็อป)
PREVIEW_DPI = 100              # ความละเอียดภาพที่ส่งให้เบราว์เซอร์ (เล็กลงครึ่ง โหลดเร็วบนมือถือ)
MAX_REVIEW_PAGES = 20          # เรื่องที่ต้องตรวจ แสดงได้ถึงหน้านี้ (กันเอกสารหนามากจนโหลดไม่ไหว)
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


class JobStateError(RuntimeError):
    def __init__(self, message: str, *, status: str = "", result=None):
        super().__init__(message)
        self.status = status
        self.result = result


def _worker_loop():
    while True:
        work = _work_queue.get()
        try:
            work()
        except Exception:
            # work แต่ละตัวต้องเก็บข้อผิดพลาดไว้ใน job ของตนเองอยู่แล้ว
            # guard นี้กัน worker ตายถ้ามี bug ในตัวจัดการ error เอง
            traceback.print_exc()
        finally:
            _work_queue.task_done()


for _worker_no in range(JOB_WORKERS):
    threading.Thread(target=_worker_loop, name=f"saraban-worker-{_worker_no + 1}",
                     daemon=True).start()


# ==========================================================
# เครื่องมือช่วย
# ==========================================================
def _png_uri(img) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _jpg_uri(img) -> str:
    """รูปหน้าเอกสารแบบ JPEG — ใช้กับหน้าที่เอาไว้ "ดูเพื่อตรวจ" เท่านั้น

    หน้าเอกสารสแกนถ้าเก็บเป็น PNG จะหนักราว ๕๘๐ KB ต่อหน้า (วัดจากของจริง)
    เอกสาร ๖ หน้าก็ ๓.๔ MB แล้ว โหลดบนมือถือช้ามาก
    JPEG คุณภาพ ๗๐ เหลือ ~๑๑๖ KB ต่อหน้า เล็กลง ๕ เท่า และยังอ่านชื่อโรงเรียน
    ในบัญชีรายชื่อแนบท้ายออกสบาย ซึ่งเป็นเหตุผลเดียวที่ต้องดูหน้าพวกนี้
    """
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _render_pdf_page(pdf_path: str, page_no: int, dpi: int = DPI):
    """แปลงหน้า PDF เป็นรูป — ใช้ PyMuPDF จึงไม่ต้องลง Poppler บนเซิร์ฟเวอร์"""
    import fitz
    with fitz.open(pdf_path) as doc:
        page_no = max(1, min(page_no, len(doc)))
        px = doc[page_no - 1].get_pixmap(dpi=dpi)
        return Image.frombytes("RGB", (px.width, px.height), px.samples), len(doc)


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
_LINE_IMG_DIR = core._w("_lineimg")
_LINE_IMG_KEEP = 60            # เก็บรูปล่าสุดเท่านี้ เกินแล้วลบตัวเก่าทิ้ง
_line_images = {}              # โทเคน -> ที่อยู่ไฟล์


def public_base_url() -> str:
    """ที่อยู่สาธารณะของเซิร์ฟเวอร์ — ว่างแปลว่ารันในเครื่อง

    RENDER_EXTERNAL_URL เป็นตัวแปรที่ Render ใส่ให้เองอัตโนมัติ ไม่ต้องตั้งเพิ่ม
    ถ้าย้ายไป hosting เจ้าอื่นค่อยตั้ง SARABAN_PUBLIC_URL เอง
    """
    return (os.environ.get("SARABAN_PUBLIC_URL", "").strip()
            or os.environ.get("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")


def publish_line_image(src_path: str) -> str:
    """คัดลอกรูปไปที่ที่เปิดให้ดึงได้ แล้วคืน URL — คืน "" ถ้ารันในเครื่อง

    คัดลอกออกมาแทนที่จะเสิร์ฟจากโฟลเดอร์งานโดยตรง เพราะ _cleanup() ลบโฟลเดอร์งาน
    ทิ้งตาม JOB_TTL ถ้า LINE มาดึงช้ากว่านั้นจะได้รูปหาย
    """
    base = public_base_url()
    if not base:
        return ""
    os.makedirs(_LINE_IMG_DIR, exist_ok=True)
    token = secrets.token_urlsafe(24)
    dest = os.path.join(_LINE_IMG_DIR, token + ".jpg")
    shutil.copyfile(src_path, dest)
    with _lock:
        _line_images[token] = dest
        while len(_line_images) > _LINE_IMG_KEEP:
            old = _line_images.pop(next(iter(_line_images)))
            try:
                os.remove(old)
            except OSError:
                pass
    return f"{base}/api/line-image/{token}.jpg"


def line_image_path(token: str) -> str:
    """หาที่อยู่ไฟล์จากโทเคน — ดูจากทะเบียนในหน่วยความจำเท่านั้น

    ไม่เอาโทเคนไปต่อเป็นชื่อไฟล์ตรงๆ จะได้ไม่มีทางหลุดไปอ่านไฟล์อื่นในเครื่อง
    """
    with _lock:
        return _line_images.get(token, "")


# ==========================================================
# คิวที่รอดตอนเซิร์ฟเวอร์เกิดใหม่
# ==========================================================
# ของเดิมเก็บงานไว้ใน _jobs (หน่วยความจำ) กับดิสก์ชั่วคราว ซึ่ง Render ล้างทิ้ง
# ทุกครั้งที่ deploy หรือหลับแล้วตื่น เรื่องที่รอลงรับหายหมด ต้องให้มือถือไปดึง
# จาก สพป. มาส่งใหม่ทั้งชุด (เซิร์ฟเวอร์ดึงเองไม่ได้ เว็บ สพป. บล็อก IP ศูนย์ข้อมูล)


def _backup_to_drive(job: dict, pdf: str):
    """ฝากไฟล์ไว้บนไดร์ฟเบื้องหลัง ไม่ให้มือถือต้องรอ

    ทำในเธรดแยกเพราะมือถือส่งรวดเดียวหลายเรื่อง ถ้ารออัปทีละไฟล์จะช้ามาก
    แลกกับช่องว่างสั้นๆ ถ้าเครื่องดับพอดีในช่วงไม่กี่วินาทีนั้น เรื่องนั้นจะไม่ถูกสำรอง
    """
    def work():
        try:
            import drive as _dr
            meta = {k: job.get(k) for k in ("book_id", "doc_no", "doc_title", "doc_date",
                                            "sender", "emoji", "attach", "redo_no", "user")}
            meta["created"] = job["created"].isoformat()
            fid = _dr.queue_put(pdf, meta)
            if fid:
                _set(job, drive_file_id=fid)
        except Exception as e:
            print(f"ฝากไฟล์คิวไว้บนไดร์ฟไม่สำเร็จ: {type(e).__name__}: {e}")

    threading.Thread(target=work, name="saraban-queue-backup", daemon=True).start()


def _drop_backup(job: dict):
    """เรื่องนี้จบแล้ว (ลงรับหรือข้าม) เอาของที่ฝากไว้ออก"""
    fid = job.get("drive_file_id")
    if not fid:
        return
    def work():
        import drive as _dr
        if _dr.queue_drop(fid):
            _set(job, drive_file_id="")

    threading.Thread(target=work, name="saraban-queue-drop", daemon=True).start()


def restore_queue() -> int:
    """ดึงคิวที่ฝากไว้กลับมาตอนเปิดเซิร์ฟเวอร์ คืนจำนวนเรื่องที่กู้ได้

    กู้กลับมาเป็นสถานะ "รอเปิดอ่าน" เหมือนตอนมือถือเพิ่งส่งเข้ามา ไม่ได้เก็บผล
    วิเคราะห์ของ AI ไว้ด้วย เรื่องที่เคยกดเปิดอ่านแล้วจึงต้องให้ AI อ่านใหม่
    (เก็บด้วยก็ได้แต่ซับซ้อนขึ้นมากและกินที่ ไม่คุ้มกับที่ประหยัดได้)

    ตัวไฟล์ยังไม่ดึงลงมาตอนนี้ รอจนผู้ใช้กดเปิดเรื่องนั้นจริงถึงค่อยดึง
    (เหตุผลเดียวกับ prepare_stored — ไม่เปลืองแรงกับเรื่องที่ยังไม่มีใครดู)
    """
    try:
        import drive as _dr
        if not _dr.enabled():
            return 0
        items = _dr.queue_list()
    except Exception as e:
        print(f"กู้คิวจากไดร์ฟไม่สำเร็จ: {type(e).__name__}: {e}")
        return 0

    restored = 0
    for item in items:
        meta = item["meta"]
        bid = str(meta.get("book_id") or "")
        try:
            # ลงรับ/ข้ามไปแล้วระหว่างที่เซิร์ฟเวอร์ดับ ก็ไม่ต้องกู้ เก็บกวาดทิ้งเลย
            if _durable_phone_record(bid):
                _dr.queue_drop(item["id"])
                continue
        except Exception:
            pass          # อ่านสถานะไม่ได้ก็กู้ไว้ก่อน ดีกว่าทำหาย
        with _lock:
            if any(j.get("source") == "phone" and j.get("book_id") == bid
                   for j in _jobs.values()):
                continue
            fields = {k: meta.get(k) or d for k, d in (
                ("doc_no", "-"), ("doc_title", "-"), ("doc_date", "-"),
                ("sender", "-"), ("emoji", "🔵"), ("attach", ""))}
            job = _new_job_locked(meta.get("user") or "phone", source="phone",
                                  status="stored", step="รอเปิดอ่าน",
                                  book_id=bid, redo_no=meta.get("redo_no") or None,
                                  drive_file_id=item["id"], **fields)
            try:                        # ให้เรียงตามเวลาที่มือถือส่งจริง ไม่ใช่เวลาที่กู้
                job["created"] = datetime.fromisoformat(meta["created"])
            except Exception:
                pass
        restored += 1
    if restored:
        print(f"กู้คิวรอลงรับจากไดร์ฟกลับมาได้ {restored} เรื่อง")
    return restored


def _cleanup():
    """ลบงานเก่าที่ค้างไว้ กันหน่วยความจำบวม

    ยกเว้นเรื่องที่ยังมีของฝากอยู่บนไดร์ฟ (drive_file_id) เพราะนั่นแปลว่ายังไม่ได้
    ลงรับและยังไม่ได้ข้าม — เป็นงานค้างจริงที่ต้องคาไว้ให้เห็น ไม่ใช่ขยะ
    สำคัญกับเรื่องที่กู้กลับมาหลังเซิร์ฟเวอร์เกิดใหม่ ซึ่งเวลาสร้างเป็นของเมื่อวาน
    ถ้าไม่ยกเว้นไว้ พอมีใครนำเข้าไฟล์ในโหมด ๓ ทีเดียว คิวที่เพิ่งกู้มาจะหายเกลี้ยง
    """
    now = now_th()
    with _lock:
        stale = [j for j, v in _jobs.items()
                 if now - v["created"] > JOB_TTL and not v.get("drive_file_id")]
        for jid in stale:
            v = _jobs.pop(jid)
            shutil.rmtree(v.get("dir", ""), ignore_errors=True)


def _new_job_locked(user: str, **initial) -> dict:
    jid = uuid.uuid4().hex
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
                     book_id=job["book_id"], redo_no=job.get("redo_no"))
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
    """อะตอมมิกต่อโปรเซส: คืน (job, created) และไม่สร้างงานซ้ำจาก book_id เดียวกัน"""
    clean = sanitize_phone_meta(meta)
    durable = _durable_phone_record(clean["book_id"])
    with _lock:
        matching = [j for j in _jobs.values()
                    if j.get("source") == "phone" and j.get("book_id") == clean["book_id"]]
        matching.sort(key=lambda j: j.get("created") or _OLDEST, reverse=True)
        for existing in matching:
            status = existing.get("status", "")
            if status in ("done", "skipped"):
                raise AlreadyHandledError(
                    "registered" if status == "done" else "skipped",
                    existing.get("reserved_receipt") or existing.get("receipt_no", ""))
            if status == "error" and retry_failed:
                existing.update(clean)
                existing.update(user=user, status="uploading", error="",
                                step="กำลังรับไฟล์จากมือถือ...")
                return existing, True
            # uploading/queued/analyzing/ready/saving/save_error/error ล้วนต้องใช้ job เดิม
            return existing, False
        if durable and not clean.get("redo_no"):
            # มี redo_no = ตั้งใจลงรับซ้ำด้วยเลขเดิม จึงไม่ใช่การลงรับซ้ำโดยพลาด
            raise AlreadyHandledError(durable.get("status", "registered"),
                                      durable.get("receipt_no", ""))
        job = _new_job_locked(user, source="phone", status="uploading",
                              step="กำลังรับไฟล์จากมือถือ...", **clean)
        return job, True


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


def phone_queue() -> list:
    """งานที่มือถือส่งเข้ามาและยังไม่จบ (ยังไม่ done/skip) — ใหม่สุดอยู่บน

    ต้องรวม status "error" ด้วย ไม่งั้นเรื่องที่ประมวลผลพังจะหายเงียบ
    ผู้ใช้จะเห็นแค่หน้าว่างทั้งที่สคริปต์บอกว่าส่งสำเร็จ — หาสาเหตุไม่ได้เลย
    """
    rows = []
    with _lock:
        for j in _jobs.values():
            if j.get("source") != "phone" or j.get("status") not in (
                    "uploading", "stored", "queued", "analyzing", "ready", "saving",
                    "skipping", "error", "save_error"):
                continue
            rows.append((j.get("created"), {
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
            }))
    rows.sort(key=lambda r: r[0] or _OLDEST, reverse=True)
    return [r[1] for r in rows]


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
            sppweb.download(sess, det["main_pdf"], pdf)

            _prepare(job, pdf,
                     doc_no=meta["doc_no"], doc_title=meta["doc_title"],
                     doc_date=meta["doc_date"], sender=meta["sender"],
                     emoji=det["emoji"], attach=sppweb.attach_text(det["attachments"]),
                     book_id=str(book_id), redo_no=redo_no)
        except Exception as e:
            _fail(job, e)

    _enqueue(job, work)
    return job


def start_from_upload(user: str, filename: str, data: bytes) -> dict:
    """โหมดที่ ๓ — อัปโหลดไฟล์เอง"""
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
                          retry_failed: bool = False):
    """โหมดที่ ๑ แต่คนไปดึงจาก สพป. คือ *มือถือ* (อุปกรณ์ที่เว็บอนุญาต)

    มือถือล็อกอิน/โหลด PDF เองจากเน็ตที่ผ่านด่านได้ แล้วส่งไฟล์ + ข้อมูล
    หนังสือมาที่นี่ เซิร์ฟเวอร์รับช่วงทำ AI เกษียณ + ตรายาง + LINE + ทะเบียน ต่อ
    โดยไม่ต้องแตะเว็บ สพป. เลย — ใช้ _prepare ตัวเดียวกับโหมด ๑ ปกติ
    """
    job, created = claim_phone_job(user, meta, retry_failed=retry_failed)
    if not created:
        return job, False

    pdf = os.path.join(job["dir"], "doc.pdf")
    try:
        os.replace(source_path, pdf)
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
def _prepare(job, pdf_path, *, doc_no, doc_title, doc_date, sender, emoji, attach, book_id, redo_no=None):
    # คัดกรองผู้รับก่อน — ถ้าอ่านในเครื่องได้และไม่ใช่ของเรา จะได้ไม่เปลือง AI
    _set(job, step="กำลังตรวจบรรทัด 'เรียน' ในไฟล์...")
    recipient = extract_recipient_line(pdf_path)
    category = classify_recipient(recipient)

    _set(job, step="กำลังให้ AI อ่านและเกษียณหนังสือ (ใช้เวลาสักครู่)...")
    ai_text, sig_page, ai_no, ai_title, ai_date, ai_sender, ai_recipient = generate_kasien_text(pdf_path)

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
    sig_page = max(1, min(sig_page, total))
    psig = _render_pdf_page(pdf_path, sig_page, DPI)[0] if sig_page > 1 else None

    # ลงรับใหม่ -> ใช้เลขเดิม / ลงรับครั้งแรก -> ดูเลขถัดไปเฉยๆ (จองจริงตอนกดบันทึก)
    receipt_no = str(redo_no) if redo_no else get_next_receipt_no()

    # ตำแหน่งตรายาง — หาที่ว่างมุมขวาบนของหน้าแรก
    stamp = render_transparent_stamp(receipt_no, 100)
    sx, sy = find_stamp_pos(p1, stamp.width, stamp.height)

    # ตำแหน่งคำเกษียณ — หาที่ว่างในหน้าที่มีลายเซ็น
    target = psig if psig is not None else p1
    kbox = render_transparent_kasien(ai_text, int(target.width * 0.42), 100)
    s_y = int(target.height * (0.15 if psig is not None else 0.40))
    e_y = int(target.height * 0.95)
    kx, ky, fits = find_kasien_pos(target, kbox.width, kbox.height, s_y, e_y,
                                   int(target.width * 0.08), return_fit=True)

    pages = [{"name": "หน้าแรก", "png": _png_uri(_render_pdf_page(pdf_path, 1, PREVIEW_DPI)[0]),
              "w": p1.width, "h": p1.height, "index": 1}]
    if psig is not None:
        pages.append({"name": f"หน้าที่มีลายเซ็น (หน้า {sig_page})",
                      "png": _png_uri(_render_pdf_page(pdf_path, sig_page, PREVIEW_DPI)[0]),
                      "w": psig.width, "h": psig.height, "index": sig_page})

    # เรื่องที่ขึ้นคำเตือน "อาจไม่ใช่หนังสือของโรงเรียนเรา" ต้องเห็นครบทุกหน้า
    # เพราะบัญชีรายชื่อที่ส่งมาด้วยมักอยู่หน้าท้ายๆ ไม่ใช่หน้าแรก ถ้าเห็นแค่หน้าแรก
    # กับหน้าลายเซ็น ก็ตัดสินไม่ได้ว่าโรงเรียนเราอยู่ในรายชื่อหรือเปล่า
    # เรื่องที่เรียนถึงทุกแห่งในสังกัด (auto) ไม่ต้องทำ เพราะไม่ต้องตรวจอะไร
    all_pages = False
    if category != "auto" and total > len(pages):
        shown = {pg["index"] for pg in pages}
        scale = DPI // PREVIEW_DPI          # w/h ต้องเป็นขนาดที่ DPI เต็ม เพราะใช้คิดตำแหน่งเป็นซม.
        for n in range(1, min(total, MAX_REVIEW_PAGES) + 1):
            if n in shown:
                continue
            img = _render_pdf_page(pdf_path, n, PREVIEW_DPI)[0]
            pages.append({"name": f"หน้า {n}", "png": _jpg_uri(img),
                          "w": img.width * scale, "h": img.height * scale, "index": n})
        pages.sort(key=lambda pg: pg["index"])
        all_pages = True

    # หาลำดับของหน้าที่มีลายเซ็นจากลิสต์จริง "หลังเรียงแล้ว" ไม่ใช่เดาว่าเป็นตัวที่ ๑
    # ไม่งั้นพอแทรกหน้าอื่นเข้ามา คำเกษียณจะไปตกผิดหน้า
    kpage = (next((i for i, pg in enumerate(pages) if pg["index"] == sig_page), 0)
             if psig is not None else 0)

    _set(job,
         status="ready", step="พร้อมแล้ว",
         pdf_path=pdf_path, book_id=book_id, total_pages=total, sig_page=sig_page,
         redo_no=(str(redo_no) if redo_no else None),
         doc_no=doc_no, doc_title=doc_title, doc_date=doc_date, sender=sender,
         emoji=emoji, attach=attach, recipient=recipient, category=category,
         all_pages=all_pages,
         receipt_no=receipt_no, pages=pages,
         stamp={"size_pct": 100, "page": 0,
                "left_cm": round(sx / CM, 2), "top_cm": round(sy / CM, 2),
                "png": _png_uri(stamp), "w": stamp.width, "h": stamp.height},
         boxes=[{"id": "b1", "text": ai_text, "size_pct": 100, "wrap_pct": 100,
                 "indent_pct": 100, "bg": False, "border": False,
                 # หน้าเอกสารไม่มีที่ว่างพอ -> วางบนหน้ากระดาษเปล่าท้ายเล่มแทน
                 # (index เท่ากับจำนวนหน้าที่ส่งไป = หน้าเปล่าที่หน้าจอสร้างต่อท้าย)
                 "page": kpage if fits else len(pages),
                 "left_cm": round(kx / CM, 2) if fits else 2.0,
                 "top_cm": round(ky / CM, 2) if fits else 3.0,
                 "fitted": fits,
                 "png": _png_uri(kbox), "w": kbox.width, "h": kbox.height}])


# ==========================================================
# วาดใหม่เมื่อผู้ใช้แก้
# ==========================================================
def render_box(job, box: dict) -> dict:
    """วาดกล่องคำเกษียณใหม่ตามข้อความ/ขนาด/ความกว้างที่ผู้ใช้ปรับ"""
    page_w = job["pages"][min(box.get("page", 0), len(job["pages"]) - 1)]["w"]
    max_w = max(10, int(page_w * 0.42 * (box.get("wrap_pct", 100) / 100.0)))
    img = render_transparent_kasien(box.get("text", ""), max_w,
                                    box.get("size_pct", 100), box.get("indent_pct", 100),
                                    bool(box.get("bg")), bool(box.get("border")))
    return {"png": _png_uri(img), "w": img.width, "h": img.height}


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
        # (เช่น มือถือส่งเข้าโหมด ๒ ไว้ แล้วผู้ใช้ไปกดลงรับเรื่องเดียวกันในโหมด ๑)
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


def finalize(job, payload: dict, reserve_fn) -> dict:
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
        result = _finalize_claimed(job, payload, reserve_fn)
    except Exception as e:
        _set(job, status="save_error", step="บันทึกยังไม่สมบูรณ์",
             error=f"{type(e).__name__}: {e}")
        raise
    _set(job, status="done", step="บันทึกเรียบร้อย", error="", result=result)
    _drop_backup(job)
    return result


def _finalize_claimed(job, payload: dict, reserve_fn) -> dict:
    """วางตรายาง+คำเกษียณลงหน้าจริง รวมเป็น PDF ลงทะเบียน แล้วส่ง LINE"""
    date_str = normalize_typed_date(payload.get("date", "")) or get_thai_date()
    time_str = to_thai_digits(str(payload.get("time", "")).strip()) or get_thai_time_rounded()
    st = payload.get("stamp", {})
    boxes = payload.get("boxes", [])

    import store as _st
    store = _st.get_store()
    receipt_no = _reserve_for_job(job, reserve_fn, store)

    out_pages = {}          # index หน้าใน PDF -> รูปที่วางของเสร็จแล้ว

    def layer(pi):
        if pi not in out_pages:
            out_pages[pi] = _render_pdf_page(job["pdf_path"], job["pages"][pi]["index"], DPI)[0].convert("RGBA")
        return out_pages[pi]

    stamp_img = render_transparent_stamp(receipt_no, int(st.get("size_pct", 100)), date_str, time_str)
    pi = min(int(st.get("page", 0)), len(job["pages"]) - 1)
    bg = layer(pi)
    bg.paste(stamp_img,
             (max(0, min(int(float(st.get("left_cm", 1)) * CM), bg.width - stamp_img.width)),
              max(0, min(int(float(st.get("top_cm", 1)) * CM), bg.height - stamp_img.height))),
             stamp_img)

    extra_pages = []        # กล่องที่ลากไปหน้ากระดาษเปล่า
    for b in boxes:
        if not (b.get("text") or "").strip():
            continue
        pi = int(b.get("page", 0))
        img_w = job["pages"][min(pi, len(job["pages"]) - 1)]["w"]
        max_w = max(10, int(img_w * 0.42 * (b.get("wrap_pct", 100) / 100.0)))
        k = render_transparent_kasien(b.get("text", ""), max_w, b.get("size_pct", 100),
                                      b.get("indent_pct", 100), bool(b.get("bg")), bool(b.get("border")))
        x = int(float(b.get("left_cm", 1)) * CM)
        y = int(float(b.get("top_cm", 1)) * CM)
        if pi >= len(job["pages"]):          # หน้ากระดาษเปล่าท้ายเล่ม
            extra_pages.append((k, x, y))
            continue
        bgp = layer(pi)
        bgp.paste(k, (max(0, min(x, bgp.width - k.width)),
                      max(0, min(y, bgp.height - k.height))), k)

    # ---- ประกอบเป็น PDF ----
    d = job["dir"]
    made = {}
    for pi, img in out_pages.items():
        p = os.path.join(d, f"page{pi}.pdf")
        img.convert("RGB").save(p, "PDF", resolution=float(DPI))
        made[job["pages"][pi]["index"]] = p

    blank_pdf = None
    if extra_pages:
        blank = Image.new("RGB", (job["pages"][0]["w"], job["pages"][0]["h"]), "white")
        for k, x, y in extra_pages:
            blank.paste(k, (max(0, min(x, blank.width - k.width)),
                            max(0, min(y, blank.height - k.height))), k)
        blank_pdf = os.path.join(d, "blank.pdf")
        blank.save(blank_pdf, "PDF", resolution=float(DPI))

    merger = PdfMerger()
    total = job["total_pages"]
    for n in range(1, total + 1):
        if n in made:
            merger.append(made[n])                       # หน้าที่วางของแล้ว
        else:
            merger.append(job["pdf_path"], pages=(n - 1, n))
    if blank_pdf:
        merger.append(blank_pdf)

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
        p1 = os.path.join(d, "line.jpg")
        layer(0).convert("RGB").save(p1, "JPEG", quality=88)
        # บน hosting ให้ LINE มาดึงจากเราเอง; ในเครื่องถอยไปใช้ imgbb เหมือนเดิม
        url = publish_line_image(p1) or upload_to_imgbb(p1)
        photo_ok = bool(url)
        dd = job["doc_date"]
        shown = (format_scraped_date(dd) if ("ม.ค." not in dd and "ก.พ." not in dd and dd != "-")
                 else to_thai_digits(dd))
        kasien_line = " | ".join(" ".join((b.get("text") or "").split())
                                 for b in boxes[:2] if (b.get("text") or "").strip())
        msg = (f"📌 เลขที่รับ {receipt_no}\n"
               f"{job['emoji']} {to_thai_digits(job['doc_no'])}\n"
               f"🆕เรื่อง: {job['doc_title']}\n"
               f"🌟หนังสือลงวันที่ : {shown}\n"
               f"⚠️คำเกษียนหนังสือ:{kasien_line}\n"
               f"{job['attach']}")
        line_ok = bool(send_line_with_image(msg, url))
    except Exception:
        line_ok = False

    return {"ok": True, "receipt_no": receipt_no, "line_ok": line_ok,
            "photo_ok": photo_ok, "drive_error": drive_error[:300],
            "drive_link": drive_link,
            "filename": os.path.basename(out),
            # เข้ารหัส URL — ที่อยู่มีอักษรไทยและเว้นวรรค ("๐๘ สิงหาคม")
            "download": "/api/doc/download/" + quote(today) + "/" + quote(os.path.basename(out))}


def skip(job):
    """ไม่รับเอกสารนี้ — จำไว้ว่าข้ามแล้ว จะได้ไม่ดึงซ้ำ"""
    with _lock:
        status = job.get("status", "")
        if status == "skipped":
            return {"ok": True, "existing": True}
        if status in ("saving", "done", "skipping"):
            raise JobStateError("ข้ามงานนี้ไม่ได้ เพราะกำลังบันทึกหรือบันทึกแล้ว", status=status)
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
