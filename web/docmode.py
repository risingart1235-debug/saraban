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
import queue
import shutil
import threading
import traceback
import unicodedata
import uuid
from datetime import datetime, timedelta

import core
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
CM = DPI / 2.54

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


def _cleanup():
    """ลบงานเก่าที่ค้างไว้ กันหน่วยความจำบวม"""
    now = datetime.now()
    with _lock:
        for jid in [j for j, v in _jobs.items() if now - v["created"] > JOB_TTL]:
            v = _jobs.pop(jid)
            shutil.rmtree(v.get("dir", ""), ignore_errors=True)


def _new_job_locked(user: str, **initial) -> dict:
    jid = uuid.uuid4().hex
    d = os.path.join(core._w("_jobs"), jid)
    os.makedirs(d, exist_ok=True)
    job = {"id": jid, "user": user, "created": datetime.now(),
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
        matching.sort(key=lambda j: j.get("created") or datetime.min, reverse=True)
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
        if durable:
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
            }))
    rows.sort(key=lambda r: r[0] or datetime.min, reverse=True)
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


def start_from_upload_path(user: str, filename: str, source_path: str) -> dict:
    """รับ ownership ของไฟล์ที่ API stream ลงดิสก์แล้ว จึงไม่ต้องเก็บทั้งไฟล์ใน RAM"""
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
                     book_id=None, redo_no=None)
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
            f.write("[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] เว็บ/เปิดงาน\n")
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

    kpage = 1 if psig is not None else 0
    pages = [{"name": "หน้าแรก", "png": _png_uri(_render_pdf_page(pdf_path, 1, PREVIEW_DPI)[0]),
              "w": p1.width, "h": p1.height, "index": 1}]
    if psig is not None:
        pages.append({"name": f"หน้าที่มีลายเซ็น (หน้า {sig_page})",
                      "png": _png_uri(_render_pdf_page(pdf_path, sig_page, PREVIEW_DPI)[0]),
                      "w": psig.width, "h": psig.height, "index": sig_page})

    _set(job,
         status="ready", step="พร้อมแล้ว",
         pdf_path=pdf_path, book_id=book_id, total_pages=total, sig_page=sig_page,
         redo_no=(str(redo_no) if redo_no else None),
         doc_no=doc_no, doc_title=doc_title, doc_date=doc_date, sender=sender,
         emoji=emoji, attach=attach, recipient=recipient, category=category,
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

    today = datetime.now().strftime("%Y-%m-%d")
    folder = os.path.join(core.OUTPUT_ROOT, today)
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, safe_output_filename(job.get("doc_no", ""), receipt_no))
    merger.write(out)
    merger.close()

    # บน hosting ดิสก์ถูกล้างเมื่อรีสตาร์ท จึงต้องส่งขึ้น Drive ให้อยู่ถาวร
    # (บนคอมที่มี Drive for Desktop ซิงก์อยู่แล้ว ตัวนี้จะถูกปิดไว้ ไม่ทำงานซ้ำซ้อน)
    drive_link = ""
    try:
        import drive as _dr
        drive_link = (_dr.upload(out, day=today) or {}).get("link", "")
    except Exception:
        pass

    # ---- ส่งเข้า LINE ----
    line_ok = None
    try:
        p1 = os.path.join(d, "line.jpg")
        layer(0).convert("RGB").save(p1, "JPEG", quality=88)
        url = upload_to_imgbb(p1)
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
            "drive_link": drive_link,
            "filename": os.path.basename(out),
            "download": f"/api/doc/download/{today}/{os.path.basename(out)}"}


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
    return result
