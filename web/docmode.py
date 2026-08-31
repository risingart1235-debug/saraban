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
import shutil
import threading
import traceback
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


def _cleanup():
    """ลบงานเก่าที่ค้างไว้ กันหน่วยความจำบวม"""
    now = datetime.now()
    with _lock:
        for jid in [j for j, v in _jobs.items() if now - v["created"] > JOB_TTL]:
            v = _jobs.pop(jid)
            shutil.rmtree(v.get("dir", ""), ignore_errors=True)


def get_job(job_id: str, user: str):
    with _lock:
        j = _jobs.get(job_id)
    if not j or j["user"] != user:
        return None
    return j


# ==========================================================
# เปิดงานใหม่
# ==========================================================
def create_job(user: str) -> dict:
    _cleanup()
    jid = uuid.uuid4().hex
    d = os.path.join(core._w("_jobs"), jid)
    os.makedirs(d, exist_ok=True)
    job = {"id": jid, "user": user, "created": datetime.now(),
           "status": "analyzing", "step": "กำลังเตรียมเอกสาร...", "dir": d}
    with _lock:
        _jobs[jid] = job
    return job


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
            _set(job, step="กำลังเข้าสู่ระบบเว็บ สพป. ...")
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

    threading.Thread(target=work, daemon=True).start()
    return job


def start_from_upload(user: str, filename: str, data: bytes) -> dict:
    """โหมดที่ ๒ — อัปโหลดไฟล์เอง"""
    job = create_job(user)

    def work():
        try:
            _set(job, step="กำลังเตรียมไฟล์...")
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

    threading.Thread(target=work, daemon=True).start()
    return job


def start_from_phone(user: str, pdf_bytes: bytes, meta: dict) -> dict:
    """โหมดที่ ๑ แต่คนไปดึงจาก สพป. คือ *มือถือ* (อุปกรณ์ที่เว็บอนุญาต)

    มือถือล็อกอิน/โหลด PDF เองจากเน็ตที่ผ่านด่านได้ แล้วส่งไฟล์ + ข้อมูล
    หนังสือมาที่นี่ เซิร์ฟเวอร์รับช่วงทำ AI เกษียณ + ตรายาง + LINE + ทะเบียน ต่อ
    โดยไม่ต้องแตะเว็บ สพป. เลย — ใช้ _prepare ตัวเดียวกับโหมด ๑ ปกติ
    """
    job = create_job(user)

    def work():
        try:
            _set(job, step="กำลังรับไฟล์จากมือถือ...")
            pdf = os.path.join(job["dir"], "doc.pdf")
            with open(pdf, "wb") as f:
                f.write(pdf_bytes)
            _prepare(job, pdf,
                     doc_no=meta.get("doc_no") or "-",
                     doc_title=meta.get("doc_title") or "-",
                     doc_date=meta.get("doc_date") or "-",
                     sender=meta.get("sender") or "-",
                     emoji=meta.get("emoji") or "🔵",
                     attach=meta.get("attach") or "",
                     book_id=(str(meta["book_id"]) if meta.get("book_id") else None),
                     redo_no=(meta.get("redo_no") or None))
        except Exception as e:
            _fail(job, e)

    threading.Thread(target=work, daemon=True).start()
    return job


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
def finalize(job, payload: dict, reserve_fn) -> dict:
    """วางตรายาง+คำเกษียณลงหน้าจริง รวมเป็น PDF ลงทะเบียน แล้วส่ง LINE"""
    date_str = normalize_typed_date(payload.get("date", "")) or get_thai_date()
    time_str = to_thai_digits(str(payload.get("time", "")).strip()) or get_thai_time_rounded()
    st = payload.get("stamp", {})
    boxes = payload.get("boxes", [])

    import store as _st
    if job.get("redo_no"):
        # ลงรับใหม่ด้วยเลขเดิม — เขียนทับแถวเดิม ไม่กินเลขใหม่
        receipt_no = str(job["redo_no"])
        _st.get_store().update_registry_row(
            receipt_no, job["doc_no"], job["doc_date"], job["sender"], job["doc_title"])
    else:
        receipt_no = reserve_fn(
            doc_no=job["doc_no"], doc_date=job["doc_date"],
            sender=job["sender"], doc_title=job["doc_title"])

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
    safe = (job["doc_no"] or "").replace("/", "_").replace(":", "").replace("\\", "_").strip()
    if not safe or safe == "-":
        safe = f"เอกสารนำเข้า_{receipt_no}"
    out = os.path.join(folder, f"{safe}.pdf")
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

    if job.get("book_id"):
        _st.get_store().mark_registered(job["book_id"], receipt_no)

    _set(job, status="done")
    return {"ok": True, "receipt_no": receipt_no, "line_ok": line_ok,
            "drive_link": drive_link,
            "filename": os.path.basename(out),
            "download": f"/api/doc/download/{today}/{os.path.basename(out)}"}


def skip(job):
    """ไม่รับเอกสารนี้ — จำไว้ว่าข้ามแล้ว จะได้ไม่ดึงซ้ำ"""
    if job.get("book_id"):
        import store as _st
        _st.get_store().mark_skipped(job["book_id"])
    _set(job, status="skipped")
