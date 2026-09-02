"""main.py — ระบบลงรับหนังสือราชการ (เวอร์ชันเว็บ ใช้ได้ทั้งคอมและมือถือ)

สมองทั้งหมดใช้ร่วมกับเวอร์ชันเดสก์ท็อปที่ core.py — แก้ที่เดียวตรงกันทั้งคู่
รันด้วย:  python -m uvicorn web.main:app --host 0.0.0.0 --port 8000
"""
import os
import sys
import io
import json
import base64
import asyncio
import copy
import secrets
import hashlib
import tempfile
import threading
from datetime import datetime

# ให้ import core.py ที่อยู่โฟลเดอร์แม่ได้
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, Form, HTTPException, Depends, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from urllib.parse import quote

import core
from core import now_th
from core import (
    Image,
    get_next_receipt_no, register_document,
    render_transparent_stamp,
    get_thai_date, get_thai_time_rounded, normalize_typed_date, to_thai_digits,
    load_stamp_pos, save_stamp_pos,
    STAMP_DEFAULT_RIGHT_CM, STAMP_DEFAULT_TOP_CM,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


def _page(name: str) -> FileResponse:
    """ส่งหน้า HTML โดยบังคับให้เบราว์เซอร์ถามเซิร์ฟเวอร์ก่อนใช้ของเก่าเสมอ

    เดิมหน้าพวกนี้ไม่ได้ส่งหัวข้อมูลเรื่องแคชเลยสักตัว (ไม่มีทั้ง cache-control,
    etag, last-modified) เบราว์เซอร์จึงเดาเองว่าจะเก็บไว้นานแค่ไหน มือถือมักเดายาว
    แล้วหยิบของเก่ามาใช้โดยไม่ถามซ้ำ พอแก้หน้าเว็บแล้ว deploy ผู้ใช้จึงยังเห็นหน้าเดิม
    ทั้งที่เซิร์ฟเวอร์เปลี่ยนไปแล้ว — หาสาเหตุยากมากเพราะฝั่งเซิร์ฟเวอร์ถูกทุกอย่าง

    no-cache ไม่ได้แปลว่า "ห้ามเก็บ" แต่แปลว่า "เก็บได้ แต่ต้องถามก่อนใช้"
    ไฟล์ static (css/รูป) ไม่ต้องทำ เพราะ StaticFiles ใส่ etag ให้ตรวจซ้ำอยู่แล้ว
    """
    return FileResponse(os.path.join(STATIC_DIR, name),
                        headers={"Cache-Control": "no-cache"})

# ขนาดกระดาษ A4 ที่ ๒๐๐ DPI — ต้องตรงกับเวอร์ชันเดสก์ท็อป
A4_DPI = 200
A4_W, A4_H = 1654, 2339

# ปริ้นที่ ๑๐๐% ตรายางถึงจะลงตรงตำแหน่ง จึงต้องคุม DPI ให้ตรงกันทั้งระบบ
CM = A4_DPI / 2.54          # ๑ เซนติเมตร = กี่พิกเซล

app = FastAPI(title="ระบบลงรับหนังสือราชการ")

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024


@app.exception_handler(Exception)
async def _json_errors(request: Request, exc: Exception):
    """ตอบข้อผิดพลาดเป็น JSON เสมอ

    ถ้าปล่อยให้ FastAPI ส่งหน้า HTML 500 กลับไป ฝั่งมือถือจะ parse ไม่ได้
    แล้วขึ้นข้อความกำกวมอย่าง "The string did not match the expected pattern"
    ซึ่งไม่บอกสาเหตุอะไรเลยว่าพังตรงไหน
    """
    import traceback
    try:
        log = core._w("ai_error.log")
        stamp = now_th().strftime("%Y-%m-%d %H:%M:%S")
        with open(log, "a", encoding="utf-8") as f:
            f.write("[" + stamp + "] " + str(request.url.path) + '\n')
            f.write(traceback.format_exc())
    except Exception:
        pass
    return JSONResponse({"ok": False, "detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


# ==========================================================
# ๑. ผู้ใช้และการล็อกอิน
# ==========================================================
_sessions = {}                      # token -> ชื่อผู้ใช้
_session_lock = threading.Lock()


def _hash(password: str, salt: str) -> str:
    """เก็บรหัสผ่านแบบแฮชพร้อมเกลือ ไม่เก็บรหัสจริงลงไฟล์"""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


class StoreDown(Exception):
    """ที่เก็บข้อมูลเข้าไม่ถึง (เช่น ต่อ Google Sheets ไม่ได้)"""


def load_users(raise_on_error: bool = False) -> dict:
    """อ่านรายชื่อผู้ใช้จากที่เก็บกลาง

    โหมด local = users.json ในเครื่อง | โหมด sheets = แท็บ "ผู้ใช้เว็บ"
    บน hosting ต้องเป็น sheets ไม่งั้นผู้ใช้หายทุกครั้งที่เซิร์ฟเวอร์รีสตาร์ท

    raise_on_error=True ใช้ตอนล็อกอิน — ถ้าต่อที่เก็บไม่ได้ต้องบอกตรงๆ
    ไม่ใช่คืนรายชื่อว่างแล้วไปขึ้นว่า "รหัสผ่านไม่ถูกต้อง" ซึ่งทำให้เข้าใจผิด
    """
    import store as _s
    try:
        return _s.get_store().load_users()
    except Exception as e:
        if raise_on_error:
            raise StoreDown(str(e)) from e
        return {}


def save_users(users: dict):
    import store as _s
    _s.get_store().save_users(users)


def _make_user(username: str, password: str, display: str, role: str, status: str) -> dict:
    salt = secrets.token_hex(16)
    return {
        "salt": salt,
        "hash": _hash(password, salt),
        "display": display or username,
        "role": role,        # admin = อนุมัติคนอื่นได้ | user = ใช้งานอย่างเดียว
        "status": status,    # pending = รออนุมัติ | approved = ใช้ได้ | rejected = ไม่อนุญาต
        "created": now_th().strftime("%Y-%m-%d %H:%M"),
    }


def add_user(username: str, password: str, display: str = "", admin: bool = False):
    """เพิ่มผู้ใช้และอนุมัติให้เลย (เรียกจาก manage_users.py — คนสร้างคือเจ้าของระบบ)"""
    users = load_users()
    users[username] = _make_user(username, password, display,
                                 "admin" if admin else "user", "approved")
    save_users(users)


def register_user(username: str, password: str, display: str = "") -> tuple[bool, str]:
    """สมัครสมาชิกเอง — ยังใช้ไม่ได้จนกว่าผู้ดูแลจะกดอนุมัติ"""
    username = (username or "").strip()
    if len(username) < 3:
        return False, "ชื่อผู้ใช้ต้องยาวอย่างน้อย ๓ ตัวอักษร"
    if len(password or "") < 6:
        return False, "รหัสผ่านต้องยาวอย่างน้อย ๖ ตัวอักษร"
    users = load_users()
    if username in users:
        return False, "ชื่อผู้ใช้นี้มีคนใช้แล้ว กรุณาเปลี่ยนชื่ออื่น"
    # คนแรกสุดของระบบให้เป็นผู้ดูแลและใช้ได้เลย (ไม่งั้นจะไม่มีใครอนุมัติใครได้)
    first = not users
    users[username] = _make_user(username, password, display,
                                 "admin" if first else "user",
                                 "approved" if first else "pending")
    save_users(users)
    return True, ("สมัครสำเร็จ! คุณเป็นผู้ดูแลระบบคนแรก เข้าใช้งานได้เลย" if first
                  else "สมัครเรียบร้อยแล้ว — รอผู้ดูแลอนุมัติก่อนจึงจะเข้าใช้งานได้")


def verify_user(username: str, password: str) -> tuple[bool, str]:
    """คืน (ผ่านไหม, เหตุผลถ้าไม่ผ่าน)"""
    try:
        users = load_users(raise_on_error=True)
    except StoreDown as e:
        # ต่อที่เก็บข้อมูลไม่ได้ — ไม่ใช่ความผิดของคนกรอกรหัส ต้องบอกให้ตรง
        return False, ("เซิร์ฟเวอร์เชื่อมต่อ Google Sheets ไม่ได้ "
                       "(ตรวจตัวแปร SARABAN_SA_JSON และ SARABAN_SHEET_ID): " + str(e)[:120])
    if not users:
        return False, ("ยังไม่มีบัญชีผู้ใช้ในระบบ — ไปที่หน้า /register "
                       "เพื่อสมัคร (คนแรกจะเป็นผู้ดูแลอัตโนมัติ)")
    u = users.get(username)
    # เทียบรหัสเสมอแม้ไม่มีชื่อนี้ เพื่อไม่ให้เดาได้จากเวลาตอบว่าชื่อนี้มีอยู่จริงไหม
    ok_pw = secrets.compare_digest(
        _hash(password, u["salt"]) if u else _hash(password, "x"),
        u["hash"] if u else "-")
    if not u or not ok_pw:
        return False, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"
    status = u.get("status", "approved")   # บัญชีเก่าก่อนมีระบบอนุมัติ ถือว่าอนุมัติแล้ว
    if status == "pending":
        return False, "บัญชีนี้ยังรอผู้ดูแลอนุมัติอยู่ กรุณาติดต่อธุรการ"
    if status == "rejected":
        return False, "บัญชีนี้ไม่ได้รับอนุญาตให้ใช้งาน"
    return True, ""


# --- จำกัดการเดารหัสผ่าน ---
# ถ้าไม่จำกัด คนที่เข้าถึงเว็บได้จะลองรหัสได้ไม่จำกัดจนกว่าจะเดาถูก
# (สำคัญมากถ้าเปิดเว็บออกอินเทอร์เน็ต)
_login_fails = {}                      # ip -> [เวลาที่ลองผิด]
_login_lock = threading.Lock()
LOGIN_WINDOW = 300                     # นับย้อนหลัง ๕ นาที
LOGIN_MAX = 8                          # ผิดเกิน ๘ ครั้งใน ๕ นาที = พักก่อน


def _login_blocked(ip: str) -> int:
    """คืนจำนวนวินาทีที่ต้องรอ (0 = ยังลองได้)"""
    import time
    now = time.time()
    with _login_lock:
        fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
        _login_fails[ip] = fails
        if len(fails) >= LOGIN_MAX:
            return int(LOGIN_WINDOW - (now - fails[0])) + 1
    return 0


def _login_failed(ip: str):
    import time
    with _login_lock:
        _login_fails.setdefault(ip, []).append(time.time())


def _login_ok(ip: str):
    with _login_lock:
        _login_fails.pop(ip, None)


def current_user(session: str = Cookie(default=None)) -> str:
    """ตัวกันหน้าที่ต้องล็อกอินก่อน"""
    with _session_lock:
        name = _sessions.get(session)
    if not name:
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบก่อน")
    u = load_users().get(name)
    if not u or u.get("status", "approved") != "approved":
        raise HTTPException(status_code=403, detail="บัญชีนี้ถูกระงับหรือยังไม่ได้รับอนุมัติ")
    return name


def current_admin(user: str = Depends(current_user)) -> str:
    """ตัวกันหน้าที่เฉพาะผู้ดูแลเข้าได้"""
    if load_users().get(user, {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="เฉพาะผู้ดูแลระบบเท่านั้น")
    return user


def is_admin(name: str) -> bool:
    return load_users().get(name, {}).get("role") == "admin"


# ==========================================================
# ๒. จองเลขรับแบบกันชนกัน (หลายคนใช้พร้อมกัน)
# ==========================================================
_receipt_lock = threading.Lock()


def reserve_receipt_no(**fields) -> str:
    """อ่านเลขรับถัดไปแล้วเขียนลงทะเบียนทันทีในล็อกเดียว

    ถ้าไม่ล็อก สองคนที่กดพร้อมกันจะได้เลขซ้ำกัน เพราะต่างคนต่างอ่านเลขเดิม
    """
    with _receipt_lock:
        return core.register_document(**fields)


def peek_receipt_no() -> str:
    """ดูเลขรับถัดไปเฉยๆ ยังไม่จอง (ไว้โชว์ในหน้าจอ)"""
    with _receipt_lock:
        return get_next_receipt_no()


# ==========================================================
# ๓. วาดภาพ
# ==========================================================
def _png_data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def render_stamp_png(receipt_no: str, size_pct: int, date_str: str, time_str: str):
    """วาดตรายางเป็น PNG พื้นหลังโปร่ง คืน (data-uri, กว้าง, สูง) หน่วยพิกเซล A4"""
    img = render_transparent_stamp(receipt_no, size_pct, date_str, time_str)
    return _png_data_uri(img), img.width, img.height


def default_stamp_pos(stamp_w: int, stamp_h: int):
    """ตำแหน่งเริ่มต้นของตรายาง (ซม. จากขอบซ้าย/บน) — ใช้ค่าที่จำไว้ก่อน"""
    saved = load_stamp_pos()
    if saved:
        left_cm, top_cm, size_pct = saved
    else:
        left_cm = (A4_W - stamp_w) / CM - STAMP_DEFAULT_RIGHT_CM
        top_cm = STAMP_DEFAULT_TOP_CM
        size_pct = 100
    # กันหลุดขอบกระดาษ
    left_cm = max(0.0, min(left_cm, (A4_W - stamp_w) / CM))
    top_cm = max(0.0, min(top_cm, (A4_H - stamp_h) / CM))
    return left_cm, top_cm, size_pct


# ==========================================================
# ขอเลขหนังสือส่ง — ใช้ได้โดยไม่ต้องล็อกอิน (ครูขอเองจากมือถือได้)
# ==========================================================
# เลขจะออกก็ต่อเมื่อกรอกข้อมูลครบเท่านั้น เพราะปัญหาเดิมคือมีคนกดขอเลขไว้
# แล้วไม่กรอกอะไรเลย ทะเบียนจึงมีแถวที่มีแต่เลขลอยๆ ตามหาไม่ได้ว่าเป็นหนังสืออะไร
#
# เปิดสาธารณะจึงต้องจำกัดจำนวนครั้ง ไม่งั้นใครกดรัวก็เผาเลขทะเบียนราชการทิ้งได้
_send_hits = {}
_send_lock = threading.Lock()
SEND_WINDOW = 3600          # นับย้อนหลัง ๑ ชั่วโมง
SEND_MAX = 10               # ขอได้ไม่เกิน ๑๐ เลขต่อชั่วโมงต่อ ๑ ไอพี


def _send_rate_ok(ip: str) -> bool:
    import time
    now = time.time()
    with _send_lock:
        hits = [t for t in _send_hits.get(ip, []) if now - t < SEND_WINDOW]
        if len(hits) >= SEND_MAX:
            _send_hits[ip] = hits
            return False
        hits.append(now)
        _send_hits[ip] = hits
        return True


@app.post("/api/send/request")
async def api_send_request(request: Request):
    """ขอเลขหนังสือส่ง — ต้องกรอกครบทุกช่องก่อน ถึงจะได้เลข"""
    d = await request.json()
    # ผู้ขอ = ครูเจ้าของเรื่อง เก็บไว้ติดตามเท่านั้น ไม่ได้ลงช่อง "จาก" ในทะเบียน
    # เพราะหนังสือส่งออกนอกโรงเรียน ผู้ส่งคือ ผอ. เสมอ
    fields = {
        "requester": ("ผู้ขอ / เจ้าของเรื่อง", str(d.get("requester", d.get("sender", ""))).strip()),
        "to": ("เรียน / ถึง", str(d.get("to", "")).strip()),
        "title": ("เรื่อง", str(d.get("title", "")).strip()),
    }
    missing = [label for label, val in fields.values() if not val]
    if missing:
        raise HTTPException(status_code=400,
                            detail="กรอกให้ครบก่อนจึงจะออกเลขให้: " + " · ".join(missing))
    for label, val in fields.values():
        if len(val) > 300:
            raise HTTPException(status_code=400, detail=f"ช่อง{label}ยาวเกินไป")

    ip = request.client.host if request.client else "?"
    if not _send_rate_ok(ip):
        raise HTTPException(status_code=429,
                            detail=f"ขอเลขบ่อยเกินไป (จำกัด {SEND_MAX} เลข/ชั่วโมง) กรุณารอสักครู่")

    import store as _s
    try:
        no = await asyncio.to_thread(
            _s.get_store().send_register,
            str(d.get("doc_date", "")).strip(),
            fields["requester"][1], fields["to"][1], fields["title"][1],
            str(d.get("note", "")).strip())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"บันทึกทะเบียนไม่สำเร็จ: {e}")

    return {"ok": True, "no": no, "doc_no": _s.SEND_PREFIX + str(no),
            "date": core.normalize_typed_date(str(d.get("doc_date", "")).strip()) or get_thai_date()}


@app.get("/api/line-image/{token}.jpg")
def api_line_image(token: str):
    """ให้เซิร์ฟเวอร์ของ LINE มาดึงรูปหน้าที่ลงรับ

    ต้องเปิดสาธารณะ ไม่มีล็อกอิน เพราะ LINE มาดึงแบบไม่มีตัวตน กันด้วยโทเคน
    สุ่ม ๒๔ ไบต์แทน เดาไม่ได้ ไม่ถูกลิสต์ที่ไหน และหายไปเองเมื่อเซิร์ฟเวอร์รีสตาร์ท
    ปลอดภัยกว่าเดิมที่อัปหนังสือราชการขึ้นเว็บฝากรูปสาธารณะแบบถาวร
    """
    path = docmode.line_image_path(token)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="ไม่พบรูป")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/healthz")
def healthz():
    """จุดให้ "ตัวปลุก" เรียกเป็นระยะ กัน hosting ฟรีพักเครื่อง

    ต้องเบาที่สุด — ไม่แตะ Google Sheets, ไม่แตะดิสก์, ไม่ต้องล็อกอิน
    เพราะถูกเรียกทุกไม่กี่นาทีตลอดวัน ถ้าไปดึงข้อมูลจริงจะเปลืองโควตา API ฟรี

    บอกจำนวนงานที่ค้างอยู่ด้วย เผื่ออยากดูว่าเซิร์ฟเวอร์ถูกรีสตาร์ทไปหรือยัง
    (งานเก็บในหน่วยความจำ ถ้าเลขกลับเป็น ๐ เอง แปลว่าเพิ่งรีสตาร์ท)
    """
    return {"ok": True, "jobs": len(docmode._jobs),
            "time": now_th().strftime("%Y-%m-%d %H:%M:%S")}


# ==========================================================
# ๔. หน้าเว็บ
# ==========================================================
@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    """นโยบายความเป็นส่วนตัว — ต้องเปิดสาธารณะ ไม่ต้องล็อกอิน

    Google บังคับให้กรอก Homepage + Privacy policy + Authorized domains ที่หน้า
    Branding ก่อนถึงจะกด "Publish app" ได้ ทั้งที่ไม่ได้ติดดาวว่าเป็นช่องบังคับ
    (บั๊กของคอนโซล มีคนเจอตรงกันหลายรายช่วงปลายสิงหาคม ๒๕๖๙) หน้านี้จึงมีไว้
    ให้มี URL จริงไปกรอก และเพื่อบอกครูตามจริงว่าระบบเก็บอะไรไว้บ้าง
    """
    return _page("privacy.html")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _page("login.html")


@app.get("/register", response_class=HTMLResponse)
def register_page():
    return _page("register.html")


@app.post("/register")
def do_register(username: str = Form(...), password: str = Form(...),
                display: str = Form(default="")):
    ok, msg = register_user(username, password, display)
    return JSONResponse({"ok": ok, "message" if ok else "error": msg},
                        status_code=200 if ok else 400)


@app.post("/login")
def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    wait = _login_blocked(ip)
    if wait:
        return JSONResponse(
            {"ok": False, "error": f"ลองผิดหลายครั้งเกินไป กรุณารออีก {wait} วินาที"},
            status_code=429)

    ok, reason = verify_user(username, password)
    if not ok:
        _login_failed(ip)
        return JSONResponse({"ok": False, "error": reason}, status_code=401)
    _login_ok(ip)
    token = secrets.token_urlsafe(32)
    with _session_lock:
        _sessions[token] = username
    resp = JSONResponse({"ok": True})
    # httponly กัน JavaScript อ่านคุกกี้ / samesite=lax กัน CSRF ข้ามเว็บ
    resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 12)
    return resp


@app.post("/logout")
def do_logout(session: str = Cookie(default=None)):
    with _session_lock:
        _sessions.pop(session, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.get("/", response_class=HTMLResponse)
def home(session: str = Cookie(default=None)):
    with _session_lock:
        if session not in _sessions:
            return RedirectResponse("/login", status_code=302)
    return _page("index.html")


@app.get("/stamp", response_class=HTMLResponse)
def stamp_page(session: str = Cookie(default=None)):
    with _session_lock:
        if session not in _sessions:
            return RedirectResponse("/login", status_code=302)
    return _page("stamp.html")


# ==========================================================
# ๕. API
# ==========================================================
@app.get("/admin", response_class=HTMLResponse)
def admin_page(session: str = Cookie(default=None)):
    with _session_lock:
        name = _sessions.get(session)
    if not name:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(name):
        return HTMLResponse("<h3 style='font-family:sans-serif;padding:40px'>"
                            "เฉพาะผู้ดูแลระบบเท่านั้น <a href='/'>กลับหน้าแรก</a></h3>",
                            status_code=403)
    return _page("admin.html")


@app.post("/api/me/password")
async def api_change_password(request: Request, user: str = Depends(current_user)):
    """เปลี่ยนรหัสผ่านของตัวเอง — ต้องยืนยันรหัสเดิมก่อน

    เปลี่ยนแล้วจะเตะ session อื่นๆ ของบัญชีนี้ออกทั้งหมด
    (เผื่อกรณีรหัสเดิมรั่ว คนที่แอบล็อกอินอยู่จะถูกตัดทันที)
    """
    d = await request.json()
    old = str(d.get("old", ""))
    new = str(d.get("new", ""))
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="รหัสผ่านใหม่ต้องยาวอย่างน้อย ๘ ตัวอักษร")
    if new == old:
        raise HTTPException(status_code=400, detail="รหัสผ่านใหม่ต้องไม่ซ้ำกับรหัสเดิม")
    ok, _ = verify_user(user, old)
    if not ok:
        raise HTTPException(status_code=401, detail="รหัสผ่านเดิมไม่ถูกต้อง")

    users = load_users()
    u = users[user]
    u["salt"] = secrets.token_hex(16)
    u["hash"] = _hash(new, u["salt"])
    u.pop("must_change", None)
    save_users(users)

    # ตัด session เดิมทั้งหมดของบัญชีนี้ แล้วออกอันใหม่ให้คนที่เพิ่งเปลี่ยน
    token = secrets.token_urlsafe(32)
    with _session_lock:
        for t in [t for t, n in _sessions.items() if n == user]:
            _sessions.pop(t, None)
        _sessions[token] = user
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 12)
    return resp


@app.get("/api/me")
def api_me(user: str = Depends(current_user)):
    u = load_users().get(user, {})
    pending = sum(1 for x in load_users().values() if x.get("status") == "pending")
    return {"user": user, "display": u.get("display", user),
            "is_admin": u.get("role") == "admin",
            "pending": pending if u.get("role") == "admin" else 0,
            "drive_url": core.load_config().get("drive_url", "").strip(),
            "must_change": bool(u.get("must_change"))}


@app.post("/api/admin/drive")
async def api_admin_drive(request: Request, admin: str = Depends(current_admin)):
    """ตั้ง/แก้ลิงก์โฟลเดอร์ Google Drive ที่แสดงบนหน้าแรก"""
    d = await request.json()
    url = str(d.get("url", "")).strip()
    if url and not url.startswith(("https://drive.google.com/", "https://docs.google.com/")):
        raise HTTPException(status_code=400, detail="ต้องเป็นลิงก์ของ Google Drive เท่านั้น")
    cfg = core.load_config()
    cfg["drive_url"] = url
    core.save_config(cfg)
    return {"ok": True, "drive_url": url}


@app.get("/api/admin/users")
def api_admin_users(admin: str = Depends(current_admin)):
    users = load_users()
    order = {"pending": 0, "approved": 1, "rejected": 2}   # คนรออนุมัติขึ้นก่อน
    rows = [{"username": n, "display": u.get("display", n),
             "role": u.get("role", "user"), "status": u.get("status", "approved"),
             "created": u.get("created", "-"), "is_me": n == admin}
            for n, u in users.items()]
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["created"]))
    return {"users": rows, "me": admin}


@app.post("/api/admin/set")
async def api_admin_set(request: Request, admin: str = Depends(current_admin)):
    """อนุมัติ / ไม่อนุญาต / ตั้งเป็นผู้ดูแล / ลบ"""
    d = await request.json()
    name, action = d.get("username", ""), d.get("action", "")
    users = load_users()
    if name not in users:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้นี้")
    # กันเผลอถอนสิทธิ์/ลบตัวเอง แล้วไม่เหลือผู้ดูแลเลย
    if name == admin and action in ("reject", "delete", "demote"):
        raise HTTPException(status_code=400, detail="ทำกับบัญชีตัวเองไม่ได้")

    if action == "approve":
        users[name]["status"] = "approved"
    elif action == "reject":
        users[name]["status"] = "rejected"
    elif action == "promote":
        users[name]["role"] = "admin"
    elif action == "demote":
        users[name]["role"] = "user"
    elif action == "delete":
        users.pop(name)
    else:
        raise HTTPException(status_code=400, detail="คำสั่งไม่ถูกต้อง")
    save_users(users)

    # ถูกระงับ/ลบแล้ว ต้องเตะออกจากระบบทันที ไม่ให้ใช้ session เดิมต่อ
    if action in ("reject", "delete"):
        with _session_lock:
            for tok in [t for t, n in _sessions.items() if n == name]:
                _sessions.pop(tok, None)
    return {"ok": True}


@app.get("/api/stamp/new")
def api_stamp_new(user: str = Depends(current_user)):
    """เปิดงานลงตรายางใหม่บนกระดาษ A4 เปล่า — ยังไม่จองเลข จนกว่าจะกดบันทึก"""
    receipt_no = peek_receipt_no()
    date_str, time_str = get_thai_date(), get_thai_time_rounded()
    png, w, h = render_stamp_png(receipt_no, 100, date_str, time_str)
    left_cm, top_cm, size_pct = default_stamp_pos(w, h)
    if size_pct != 100:
        png, w, h = render_stamp_png(receipt_no, size_pct, date_str, time_str)
        left_cm, top_cm, _ = default_stamp_pos(w, h)
    return {
        "receipt_no": receipt_no,
        "date": date_str,
        "time": time_str,
        "size_pct": size_pct,
        "stamp": {"png": png, "w": w, "h": h},
        "page": {"w": A4_W, "h": A4_H, "dpi": A4_DPI, "cm": CM},
        "pos": {"left_cm": round(left_cm, 2), "top_cm": round(top_cm, 2)},
    }


@app.post("/api/stamp/preview")
async def api_stamp_preview(request: Request, user: str = Depends(current_user)):
    """วาดตรายางใหม่ตามวัน-เวลา-ขนาดที่ผู้ใช้แก้ (เรียกตอนพิมพ์/กดปรับขนาด)"""
    d = await request.json()
    date_str = normalize_typed_date(d.get("date", "")) or get_thai_date()
    time_str = to_thai_digits(str(d.get("time", "")).strip()) or get_thai_time_rounded()
    size_pct = max(20, min(300, int(d.get("size_pct", 100))))
    png, w, h = render_stamp_png(d.get("receipt_no", ""), size_pct, date_str, time_str)
    return {"stamp": {"png": png, "w": w, "h": h}, "date": date_str, "time": time_str}


@app.post("/api/stamp/save")
async def api_stamp_save(request: Request, user: str = Depends(current_user)):
    """ลงตรายางบนกระดาษ A4 เปล่า เซฟเป็น PDF แล้วลงทะเบียน"""
    d = await request.json()
    date_str = normalize_typed_date(d.get("date", "")) or get_thai_date()
    time_str = to_thai_digits(str(d.get("time", "")).strip()) or get_thai_time_rounded()
    size_pct = max(20, min(300, int(d.get("size_pct", 100))))
    left_cm = float(d.get("left_cm", STAMP_DEFAULT_RIGHT_CM))
    top_cm = float(d.get("top_cm", STAMP_DEFAULT_TOP_CM))
    f = d.get("fields", {}) or {}

    # จองเลขรับ + ลงทะเบียน Excel ในล็อกเดียว กันเลขซ้ำเวลาหลายคนกดพร้อมกัน
    receipt_no = reserve_receipt_no(
        doc_no=f.get("doc_no", ""), doc_date=f.get("doc_date", ""),
        sender=f.get("sender", ""), doc_title=f.get("doc_title", ""),
        receive_date=date_str,
    )

    # วาดกระดาษ A4 เปล่า แล้ววางตรายางตามตำแหน่งที่ลาก
    page = Image.new("RGB", (A4_W, A4_H), "white")
    stamp = render_transparent_stamp(receipt_no, size_pct, date_str, time_str)
    x = max(0, min(int(left_cm * CM), A4_W - stamp.width))
    y = max(0, min(int(top_cm * CM), A4_H - stamp.height))
    page.paste(stamp, (x, y), stamp)

    today = core.day_folder()          # "๒๕๖๙/๐๘ สิงหาคม/๒๘" — ซอยเป็น ปี/เดือน/วัน
    folder = os.path.join(core.OUTPUT_ROOT, today)
    os.makedirs(folder, exist_ok=True)
    name = docmode.safe_output_filename(f.get("doc_no", ""), receipt_no)
    path = os.path.join(folder, name)
    # resolution ต้องเท่า A4_DPI หน้ากระดาษใน PDF จะได้เป็น A4 พอดี
    page.save(path, "PDF", resolution=float(A4_DPI))

    drive_link = ""
    try:
        import drive as _dr
        drive_link = (_dr.upload(path, day=today) or {}).get("link", "")
    except Exception:
        pass

    save_stamp_pos(left_cm, top_cm, size_pct)   # จำตำแหน่งไว้ใช้ครั้งหน้า

    filled = sum(1 for k in ("doc_no", "doc_date", "sender", "doc_title") if (f.get(k) or "").strip())
    return {
        "ok": True,
        "receipt_no": receipt_no,
        "filename": name,
        "path": path,
        "filled": filled,
        # เข้ารหัส URL — ที่อยู่มีอักษรไทยและเว้นวรรค ("๐๘ สิงหาคม")
        "download": "/api/stamp/download/" + quote(today) + "/" + quote(name),
        "drive_link": drive_link,
    }


@app.get("/api/stamp/download/{day:path}/{name}")
def api_stamp_download(day: str, name: str, user: str = Depends(current_user)):
    """ดาวน์โหลด PDF ที่เพิ่งสร้าง (กันเรียกไฟล์นอกโฟลเดอร์ด้วยการเช็ก path)"""
    path = docmode.contained_path(core.OUTPUT_ROOT, day, name)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์")
    return FileResponse(path, media_type="application/pdf", filename=name)


# ==========================================================
# ๖. โหมดที่ ๑ (เว็บ สพป.) และโหมดที่ ๓ (อัปโหลดไฟล์)
# ==========================================================
import sppweb
from web import docmode

_spp_session_state = None       # cookie ทุกตัว + User-Agent; ไม่แชร์ requests.Session ข้ามเธรด
_spp_session_lock = threading.Lock()


def _with_spp_session(operation):
    """เรียกงานเว็บ สพป. ด้วย session ที่ใช้ได้ แล้วจำสถานะล่าสุดไว้รอบถัดไป

    requests.Session ไม่ปลอดภัยสำหรับการใช้พร้อมกันหลายเธรด จึงเก็บเฉพาะ
    สถานะที่ export แล้ว และสร้าง Session ใหม่ทุกครั้ง ล็อกนี้ยังช่วยกันหลาย request
    ล็อกอินซ้อนกันหรือเขียน cookie ชุดเก่าทับชุดใหม่
    """
    global _spp_session_state
    with _spp_session_lock:
        sess = sppweb.new_session(_spp_session_state) if _spp_session_state else None
        if sess is None or not sppweb.is_logged_in(sess):
            sess = sppweb.login()

        try:
            result = operation(sess)
        except sppweb.SessionExpiredError:
            # session อาจหมดอายุหลังตรวจแต่ก่อนเปิดหน้าถัดไป ล็อกอินใหม่แล้วลองซ้ำครั้งเดียว
            sess = sppweb.login()
            result = operation(sess)

        _spp_session_state = sppweb.export_session(sess)
        return result


def _spp_session_snapshot():
    """สำเนาสถานะสำหรับส่งให้งานเบื้องหลัง โดยไม่เปิด Session ร่วมกัน"""
    with _spp_session_lock:
        return copy.deepcopy(_spp_session_state)


@app.get("/doc", response_class=HTMLResponse)
def doc_page(session: str = Cookie(default=None)):
    with _session_lock:
        if session not in _sessions:
            return RedirectResponse("/login", status_code=302)
    return _page("doc.html")


@app.get("/api/spp/check")
def api_spp_check(user: str = Depends(current_user)):
    """ดูว่ามีหนังสือใหม่ที่ยังไม่ได้ลงรับกี่เรื่อง"""
    try:
        docs = _with_spp_session(
            lambda sess: sppweb.list_new_documents(sess, pages=2))
        return {"ok": True, "count": len(docs), "docs": docs}
    except sppweb.LoginError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.get("/news", response_class=HTMLResponse)
def news_page(session: str = Cookie(default=None)):
    with _session_lock:
        if session not in _sessions:
            return RedirectResponse("/login", status_code=302)
    return _page("news.html")


@app.get("/api/spp/list")
def api_spp_list(pages: int = 3, user: str = Depends(current_user)):
    """รายการหนังสือทั้งหมด พร้อมสถานะ จัดกลุ่มตามวัน — รวมสองแหล่งไว้ที่เดียว

    เดิมแยกเป็นโหมด ๑ (เซิร์ฟเวอร์ดึงจากเว็บ สพป. เอง) กับโหมด ๒ (มือถือดึงมาส่งให้)
    ผู้ใช้ต้องจำว่าเรื่องไหนอยู่หน้าไหน และบนมือถือได้แค่รายการเปล่าๆ ไม่มีตัวกรอง
    ไม่มีปุ่มข้าม ทั้งที่ขั้นตอนลงรับใช้ทางเดียวกันอยู่แล้ว

    ที่แยกกันแต่แรกเพราะเว็บ สพป. อยู่หลัง Cloudflare ซึ่งบล็อก IP ของศูนย์ข้อมูล
    เซิร์ฟเวอร์คลาวด์จึงดึงเองไม่ได้ ต้องให้มือถือไปเอามาให้ — ข้อจำกัดนี้ยังอยู่
    แต่ไม่จำเป็นต้องโผล่มาเป็นสองเมนูให้ผู้ใช้ต้องเลือกเอง

    รวมตาม book_id เรื่องที่มือถือส่งไฟล์มาแล้วจะพ่วง job_id ไปด้วย หน้าเว็บจะได้
    เปิดงานเดิมตรงๆ ไม่ต้องสั่งเซิร์ฟเวอร์ไปโหลดซ้ำ (ซึ่งบนคลาวด์ก็โหลดไม่ได้อยู่ดี)
    """
    import store as _s
    spp_error, blocked, fetched = "", False, []
    try:
        fetched = _with_spp_session(
            lambda sess: sppweb.list_documents(sess, pages=max(1, min(pages, 8))))
    except sppweb.LoginError as e:
        spp_error = str(e)
        blocked = "Cloudflare" in spp_error or "403" in spp_error
    except Exception as e:
        spp_error = f"{type(e).__name__}: {e}"

    jobs = {r["book_id"]: r for r in docmode.phone_queue() if r.get("book_id")}
    docs, seen = [], set()
    for d in fetched:
        d = dict(d)
        bid = str(d.get("book_id", ""))
        seen.add(bid)
        d["source"] = "spp"
        if bid in jobs:                    # ไฟล์อยู่บนเซิร์ฟเวอร์แล้ว เปิดได้เลย
            d["job_id"] = jobs[bid]["job_id"]
        docs.append(d)
    for bid, r in jobs.items():
        if bid in seen:
            continue
        docs.append({"book_id": bid, "doc_no": r["doc_no"], "doc_title": r["doc_title"],
                     "doc_date": r["doc_date"], "sender": r["sender"],
                     "sent_key": r["sent_key"], "sent_date": r["sent_date"],
                     "sent_time": r["sent_time"], "source": "phone",
                     "job_id": r["job_id"]})

    # ถูกเว็บ สพป. ปิดกั้น + คิวว่าง = "ยังไม่มีเรื่องรอลงรับ" ไม่ใช่ความผิดพลาด
    # บนคลาวด์การดึงเว็บไม่ได้เป็นสภาพปกติตลอดเวลา ถ้าขึ้นหน้าแดงทุกครั้งที่คิวว่าง
    # ก็เท่ากับยกโหมดเก่ากลับมาทั้งที่เพิ่งรวมหน้าไป ให้หน้าเว็บไปขึ้นสถานะว่างแทน
    # คืน error ไว้เฉพาะกรณีพังจริงที่ไม่ใช่การถูกปิดกั้น เพราะนั่นต้องให้คนเห็น
    if not docs and spp_error and not blocked:
        return JSONResponse({"ok": False, "error": spp_error, "blocked": blocked},
                            status_code=502)
    docs = _s.get_store().status_of(docs)

    # จัดกลุ่มตามวันที่เว็บอัปโหลด ใหม่สุดขึ้นก่อน
    groups = {}
    for d in docs:
        g = groups.setdefault(d["sent_key"] or "-", {
            "key": d["sent_key"] or "-", "label": d["sent_date"] or "(ไม่ทราบวันที่)",
            "docs": [], "registered": 0, "skipped": 0, "new": 0})
        g["docs"].append(d)
        g[{"registered": "registered", "skipped": "skipped"}.get(d["status"], "new")] += 1
    ordered = sorted(groups.values(), key=lambda g: g["key"], reverse=True)
    for g in ordered:
        g["docs"].sort(key=lambda d: d["sent_time"], reverse=True)

    total = {"registered": sum(g["registered"] for g in ordered),
             "skipped": sum(g["skipped"] for g in ordered),
             "new": sum(g["new"] for g in ordered)}
    # ห้ามส่ง blocked ไปกับคำตอบที่สำเร็จ — หน้าเว็บเช็ค d.blocked ก่อนเช็ค d.ok
    # ถ้าติดไปด้วยจะเด้งเข้าหน้าแดง "ดึงเว็บไม่ได้" ทั้งที่รายการโหลดสำเร็จแล้ว
    # ใช้ spp_ok บอกสถานะแทน (หน้าเว็บเอาไปขึ้นแถบว่าเห็นเฉพาะของจากมือถือ)
    return {"ok": True, "days": ordered, "total": total, "count": len(docs),
            "spp_ok": not spp_error, "spp_error": spp_error,
            "phone_count": len(jobs)}


@app.post("/api/spp/skip")
async def api_spp_skip(request: Request, user: str = Depends(current_user)):
    """ข้ามเรื่องจากหน้ารายการโดยตรง โดยไม่ต้องเปิดงาน (ไม่เปลือง AI)"""
    import store as _s
    d = await request.json()
    bid = str(d.get("book_id", "")).strip()
    if not bid:
        raise HTTPException(status_code=400, detail="ไม่ได้ระบุเรื่อง")
    _s.get_store().mark_skipped(bid)
    return {"ok": True}


@app.post("/api/doc/open")
async def api_doc_open(request: Request, user: str = Depends(current_user)):
    d = await request.json()
    try:
        job = docmode.start_from_spp(user, str(d.get("book_id", "")), _spp_session_snapshot(),
                                     redo_no=(d.get("redo_no") or None))
    except docmode.QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e), headers={"Retry-After": "30"})
    return {"job_id": job["id"]}


# ==========================================================
# โหมด ๑ ผ่านมือถือ — มือถือเป็นคนดึงจาก สพป. (อุปกรณ์ที่เว็บอนุญาต)
# แล้วส่งไฟล์มาที่นี่ เซิร์ฟเวอร์ทำ AI/ตรายาง/LINE/ทะเบียนต่อ ไม่แตะ สพป.
#
# ยืนยันตัวด้วยโทเคนลับ (SARABAN_PHONE_TOKEN) แทนการเอารหัสเว็บไปไว้ในมือถือ
# ตั้งโทเคนที่ Render → Environment ให้ตรงกับที่ใส่ในสคริปต์มือถือ
# ==========================================================
def _check_phone_token(request: Request) -> str:
    want = (os.environ.get("SARABAN_PHONE_TOKEN") or "").strip()
    if not want:
        raise HTTPException(
            status_code=503,
            detail="เซิร์ฟเวอร์ยังไม่ได้ตั้ง SARABAN_PHONE_TOKEN — ตั้งที่ Render ก่อนใช้โหมดมือถือ")
    got = (request.headers.get("X-Phone-Token") or "").strip()
    if not secrets.compare_digest(got, want):
        raise HTTPException(status_code=401, detail="โทเคนมือถือไม่ถูกต้อง")
    return (os.environ.get("SARABAN_PHONE_USER") or "phone").strip()


@app.middleware("http")
async def _phone_upload_guard(request: Request, call_next):
    """ปฏิเสธ token/ขนาดที่ผิดก่อน Starlette เริ่มแยก multipart เท่าที่ header ทำได้"""
    if request.url.path == "/api/phone/submit" and request.method == "POST":
        try:
            _check_phone_token(request)
        except HTTPException as e:
            return JSONResponse({"ok": False, "detail": e.detail}, status_code=e.status_code)
        raw_length = request.headers.get("content-length", "")
        if raw_length:
            try:
                # multipart มี header/metadata เพิ่มจากตัว PDF จึงเผื่อ ๑ MB;
                # ตัว stream ด้านล่างยังบังคับเพดานไฟล์จริง ๔๐ MB ซ้ำอีกชั้น
                if int(raw_length) > MAX_UPLOAD_BYTES + 1024 * 1024:
                    return JSONResponse({"ok": False, "detail": "ไฟล์ใหญ่เกิน ๔๐ MB"},
                                        status_code=413)
            except ValueError:
                return JSONResponse({"ok": False, "detail": "Content-Length ไม่ถูกต้อง"},
                                    status_code=400)
    return await call_next(request)


async def _stream_upload(upload, path: str, allowed: str = "pdf") -> int:
    """stream ลงดิสก์ทีละก้อน พร้อมเพดานจริงและตรวจ magic bytes"""
    total = 0
    prefix = b""
    with open(path, "wb") as out:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="ไฟล์ใหญ่เกิน ๔๐ MB")
            if len(prefix) < 8:
                prefix = (prefix + chunk)[:8]
            out.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="ไฟล์ว่างเปล่า")
    is_pdf = prefix.startswith(b"%PDF-")
    is_jpeg = prefix.startswith(b"\xff\xd8\xff")
    is_png = prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if allowed == "pdf" and not is_pdf:
        raise HTTPException(status_code=400, detail="รับเฉพาะไฟล์ PDF ที่ถูกต้อง")
    if allowed == "document" and not (is_pdf or is_jpeg or is_png):
        raise HTTPException(status_code=400, detail="รับเฉพาะ PDF, JPG หรือ PNG ที่ถูกต้อง")
    return total


def _new_incoming_path() -> str:
    folder = core._w("_incoming")
    os.makedirs(folder, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="upload_", suffix=".part", dir=folder)
    os.close(fd)
    return path


@app.get("/api/phone/history")
def api_phone_history(request: Request):
    """คืน book_id ที่จัดการไปแล้ว (รับแล้ว+ข้าม) ให้มือถือกรองก่อนโหลด"""
    _check_phone_token(request)
    import store as _s
    try:
        done = sorted(_s.get_store().history_ids())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"อ่านประวัติไม่ได้: {e}")
    return {"ok": True, "done": done}


@app.post("/api/phone/submit")
async def api_phone_submit(request: Request):
    """รับ PDF ที่มือถือโหลดจาก สพป. มาแล้ว สร้างงานให้รอลงรับ (ทบทวนในเบราว์เซอร์)"""
    user = _check_phone_token(request)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not callable(getattr(upload, "read", None)):
        raise HTTPException(status_code=400, detail="ไม่ได้แนบไฟล์ PDF")
    meta = {key: form.get(key, default) for key, default in (
        ("book_id", ""), ("doc_no", "-"), ("doc_title", "-"),
        ("doc_date", "-"), ("sender", "-"), ("emoji", "🔵"), ("attach", ""),
        ("redo_no", ""))}
    retry_failed = str(form.get("retry_failed", "")).strip().lower() in ("1", "true", "yes")
    incoming = _new_incoming_path()
    try:
        await _stream_upload(upload, incoming, "pdf")
        job, created = docmode.start_from_phone_path(
            user, incoming, meta, retry_failed=retry_failed)
    except docmode.AlreadyHandledError as e:
        raise HTTPException(status_code=409, detail={
            "message": str(e), "status": e.status, "receipt_no": e.receipt_no})
    except docmode.QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e), headers={"Retry-After": "30"})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            await upload.close()
        except Exception:
            pass
        if os.path.exists(incoming):
            try:
                os.remove(incoming)
            except OSError:
                pass
    pub = (os.environ.get("SARABAN_PUBLIC_URL") or "").strip().rstrip("/")
    return {"ok": True, "created": created, "status": job.get("status"),
            "job_id": job["id"], "doc_no": job.get("doc_no", "-"),
            "doc_title": job.get("doc_title", "-"),
            "review_url": (f"{pub}/doc?job={job['id']}" if pub else f"/doc?job={job['id']}")}


@app.get("/queue")
def queue_page_moved():
    """รวมเข้าหน้าเดียวกับ /news แล้ว — คงเส้นทางเดิมไว้เผื่อมีคนคั่นหน้าไว้"""
    return RedirectResponse("/news", status_code=302)


@app.get("/queue-old", response_class=HTMLResponse)
def queue_page(session: str = Cookie(default=None)):
    with _session_lock:
        if session not in _sessions:
            return RedirectResponse("/login", status_code=302)
    return _page("queue.html")


@app.post("/api/doc/{job_id}/prepare")
def api_doc_prepare(job_id: str, user: str = Depends(current_user)):
    """เริ่มอ่าน/เกษียณเรื่องที่มือถือเก็บไว้ — เรียกตอนผู้ใช้แตะเปิดเรื่องนั้น

    แยกจากตอนรับไฟล์ เพราะมือถือส่งมารวดเดียวหลายเรื่อง ถ้าเข้าคิวทันทีที่รับ
    เครื่องจะไล่ประมวลผลทุกเรื่องรวมถึงเรื่องที่ยังไม่มีใครเปิด กินแรงเปล่า
    """
    job = _job_or_404(job_id, user)
    try:
        docmode.prepare_stored(job)       # เริ่มไปแล้ว/เสร็จแล้ว จะไม่ทำซ้ำ
    except docmode.QueueFullError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "status": job.get("status")}


@app.get("/api/phone/queue")
def api_phone_queue(request: Request, session: str = Cookie(default=None)):
    """รายการเรื่องที่มือถือดึงเข้ามา รอลงรับ (โหมด ๒)

    รับได้ทั้งคุกกี้ล็อกอิน (หน้าเว็บเรียก) และโทเคนมือถือ (สคริปต์เรียกเช็กเอง
    ว่างานที่ส่งไปสถานะอะไร — จำเป็นตอนหาสาเหตุเวลางานไม่โผล่)
    """
    with _session_lock:
        ok = session in _sessions
    if not ok:
        _check_phone_token(request)      # ไม่ใช่คนล็อกอิน ก็ต้องมีโทเคนที่ถูกต้อง
    return {"ok": True, "jobs": docmode.phone_queue()}


@app.post("/api/doc/upload")
async def api_doc_upload(request: Request):
    # ตรวจ session ก่อน parse multipart เพื่อไม่ให้ผู้ไม่ล็อกอินใช้ RAM/ดิสก์รับไฟล์
    user = current_user(request.cookies.get("session"))
    form = await request.form()
    upload = form.get("file")
    if upload is None or not callable(getattr(upload, "read", None)):
        raise HTTPException(status_code=400, detail="ไม่ได้แนบไฟล์")
    # ลงรับใหม่ด้วยเลขเดิม — ต้องมีแถวนั้นอยู่จริงก่อน ไม่งั้นผู้ใช้จะรอ AI อ่านจนจบ
    # แล้วค่อยมาเจอว่าเลขไม่มีในทะเบียน เสียเวลาเปล่า
    redo_no = str(form.get("redo_no") or "").strip()
    if redo_no:
        want = core.to_arabic_digits(redo_no)
        st = _store_mod.get_store()
        have = any(r and r[0] and core.to_arabic_digits(str(r[0]).strip()) == want
                   for r in st.registry_rows())
        if not have:
            raise HTTPException(status_code=404,
                                detail=f"ไม่พบเลขรับ {redo_no} ในทะเบียน จึงลงรับใหม่ด้วยเลขนี้ไม่ได้")

    incoming = _new_incoming_path()
    try:
        await _stream_upload(upload, incoming, "document")
        try:
            job = docmode.start_from_upload_path(
                user, getattr(upload, "filename", "") or "upload.pdf", incoming,
                redo_no=redo_no or None)
        except docmode.QueueFullError as e:
            raise HTTPException(status_code=429, detail=str(e), headers={"Retry-After": "30"})
    finally:
        try:
            await upload.close()
        except Exception:
            pass
        if os.path.exists(incoming):
            try:
                os.remove(incoming)
            except OSError:
                pass
    return {"job_id": job["id"]}


def _job_or_404(job_id: str, user: str):
    job = docmode.get_job(job_id, user)
    if not job:
        raise HTTPException(status_code=404, detail="ไม่พบงานนี้ (อาจหมดอายุแล้ว)")
    return job


@app.get("/api/doc/{job_id}")
def api_doc_status(job_id: str, user: str = Depends(current_user)):
    job = _job_or_404(job_id, user)
    if job["status"] == "stored":
        # เก็บไฟล์ไว้แล้วแต่ยังไม่ได้ประมวลผล — หน้าจอจะสั่ง /prepare ให้เริ่ม
        return {"status": "stored", "step": job.get("step", "")}
    if job["status"] in ("uploading", "queued", "analyzing", "saving", "skipping"):
        return {"status": "analyzing", "step": job.get("step", "")}
    if job["status"] in ("error", "save_error"):
        return {"status": "error", "error": job.get("error", ""),
                "receipt_no": job.get("reserved_receipt", "")}
    # ส่งเฉพาะที่หน้าจอต้องใช้ (ตัด pdf_path และของภายในออก)
    # book_id บอกหน้าจอว่าเข้ามาจากทางไหน (มี = โหมด ๑ / ไม่มี = อัปโหลดเอง)
    # เพื่อให้ทำเสร็จแล้วกลับไปที่เดิม ไม่ใช่เด้งหน้าแรกทุกครั้ง
    keys = ("status", "receipt_no", "doc_no", "doc_title", "doc_date", "sender",
            "emoji", "recipient", "category", "pages", "stamp", "boxes",
            "total_pages", "sig_page", "redo_no", "book_id", "source")
    out = {k: job.get(k) for k in keys}
    out["date"] = get_thai_date()
    out["time"] = get_thai_time_rounded()
    out["cm"] = docmode.CM
    return out


@app.post("/api/doc/{job_id}/box")
async def api_doc_box(job_id: str, request: Request, user: str = Depends(current_user)):
    """วาดกล่องคำเกษียณใหม่หลังผู้ใช้แก้ข้อความหรือปรับขนาด"""
    job = _job_or_404(job_id, user)
    return docmode.render_box(job, await request.json())


@app.post("/api/doc/{job_id}/stamp")
async def api_doc_stamp(job_id: str, request: Request, user: str = Depends(current_user)):
    job = _job_or_404(job_id, user)
    d = await request.json()
    date_str = normalize_typed_date(d.get("date", "")) or get_thai_date()
    time_str = to_thai_digits(str(d.get("time", "")).strip()) or get_thai_time_rounded()
    out = docmode.render_stamp(job, int(d.get("size_pct", 100)), date_str, time_str)
    out.update(date=date_str, time=time_str)
    return out


@app.post("/api/doc/{job_id}/save")
async def api_doc_save(job_id: str, request: Request, user: str = Depends(current_user)):
    job = _job_or_404(job_id, user)
    payload = await request.json()

    def reserve(**fields):
        """จองเลขรับ + เขียนแถวทะเบียนในจังหวะเดียว กันเลขซ้ำเวลาหลายคนกดพร้อมกัน"""
        with _receipt_lock:
            return core.register_document(**fields)

    try:
        # แปลง/รวม PDF, อัปโหลด Drive และส่ง LINE เป็นงาน blocking ทั้งหมด
        # ต้องออกจาก async event loop เพื่อให้ request อื่นยังตอบได้
        return await asyncio.to_thread(docmode.finalize, job, payload, reserve)
    except docmode.AlreadyHandledError as e:
        # เรื่องนี้ถูกลงรับ/ข้ามจากที่อื่นไปแล้วระหว่างที่เปิดหน้านี้ค้างไว้
        # (เช่นเปิดค้างในโหมด ๒ แล้วไปกดลงรับเรื่องเดียวกันในโหมด ๑)
        raise HTTPException(status_code=409, detail=str(e))
    except docmode.DriveNotReadyError as e:
        # ๕๐๓ = ยังไม่ได้ทำอะไรเลย เลขรับไม่ถูกกิน กดใหม่ได้เมื่อไดร์ฟกลับมา
        raise HTTPException(status_code=503, detail=str(e))
    except docmode.JobStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/doc/{job_id}/skip")
def api_doc_skip(job_id: str, user: str = Depends(current_user)):
    try:
        return docmode.skip(_job_or_404(job_id, user))
    except docmode.JobStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ==========================================================
# ๗. ถอยกลับเวลาลงรับผิด (แก้เลขออก แล้วดึงเรื่องเดิมมาทำใหม่)
# ==========================================================
import store as _store_mod


@app.get("/fix", response_class=HTMLResponse)
def fix_page(session: str = Cookie(default=None)):
    with _session_lock:
        if session not in _sessions:
            return RedirectResponse("/login", status_code=302)
    return _page("fix.html")


@app.get("/api/fix/recent")
def api_fix_recent(user: str = Depends(current_user)):
    """รายการที่ลงรับล่าสุด ไว้เลือกถอย"""
    st = _store_mod.get_store()
    rows = st.registry_rows(limit=15)
    out = []
    for r in reversed(rows):                      # ใหม่สุดขึ้นก่อน
        r = list(r) + [""] * (9 - len(r))
        out.append({"receipt_no": r[0], "doc_no": r[1], "doc_date": r[2],
                    "sender": r[3], "doc_title": r[5], "receive_date": r[8]})
    return {"rows": out, "next_no": st.peek_receipt_no(), "store": st.kind}


@app.post("/api/fix/undo")
async def api_fix_undo(request: Request, user: str = Depends(current_user)):
    """ลบแถวทะเบียนตามเลขรับ และถอนเรื่องออกจากประวัติ (ถ้าระบุ book_id มา)

    ผลลัพธ์: เลขรับถูกปล่อยคืน และหนังสือเรื่องนั้นจะกลับมาขึ้นในรายการ 'หนังสือใหม่'
    """
    d = await request.json()
    receipt_no = str(d.get("receipt_no", "")).strip()
    book_id = str(d.get("book_id", "")).strip()
    st = _store_mod.get_store()

    result = {"ok": True, "deleted": None, "unmarked": False}
    with _receipt_lock:
        if receipt_no:
            res = st.delete_receipt(receipt_no)
            if not res:
                raise HTTPException(status_code=404, detail=f"ไม่พบเลขรับ {receipt_no} ในทะเบียน")
            result["deleted"] = {"row": [str(x) if x is not None else "" for x in res["row"]],
                                 "reusable": res["reusable"]}
        if book_id:
            result["unmarked"] = sppweb.unmark(book_id)
    result["next_no"] = st.peek_receipt_no()
    return result


@app.get("/api/fix/history")
def api_fix_history(q: str = "", user: str = Depends(current_user)):
    """ค้นเลขหนังสือในประวัติ (ไว้ถอยเฉพาะประวัติ โดยไม่แตะทะเบียน)"""
    ids = sorted(_store_mod.get_store().history_ids(), reverse=True)
    if q.strip():
        ids = [i for i in ids if q.strip() in i]
    return {"total": len(ids), "ids": ids[:30]}


@app.get("/api/doc/download/{day:path}/{name}")
def api_doc_download(day: str, name: str, user: str = Depends(current_user)):
    path = docmode.contained_path(core.OUTPUT_ROOT, day, name)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์")
    return FileResponse(path, media_type="application/pdf", filename=name)


@app.on_event("startup")
def _warm_up():
    """อุ่นเครื่องไว้ตั้งแต่เปิดเซิร์ฟเวอร์ ไม่ให้คนแรกที่ล็อกอินต้องรอ

    วัดจริงบน Render: ล็อกอินครั้งแรกหลัง deploy ใหม่ใช้ ๕.๔ วินาที
    ครั้งถัดไปเหลือ ๐.๗ วินาที — ส่วนต่างคือการสร้างตัวเชื่อม Google API
    (โหลดกุญแจ + ต่อ discovery) ซึ่งทำครั้งเดียวต่อโปรเซส
    ย้ายมาทำตอนเปิดเซิร์ฟเวอร์แทน ผู้ใช้จะได้ไม่ต้องเป็นคนจ่ายเวลานั้น

    ทำในเธรดแยกและกลืน error ทั้งหมด — ถ้าต่อ Sheets ไม่ได้ตอนเปิด
    เซิร์ฟเวอร์ต้องขึ้นได้ตามปกติ แล้วค่อยไปแจ้ง error ตอนใช้งานจริง
    """
    def work():
        try:
            import store as _s
            _s.get_store().load_users()
        except Exception:
            pass
        # ตรวจไดร์ฟไว้ล่วงหน้าด้วย เพราะตอนลงรับมีด่านตรวจว่าอัปไฟล์ได้จริงไหม
        # ก่อนจะกินเลขรับ ถ้าไม่อุ่นไว้ คนแรกที่กดลงรับต้องรอ ~๕ วินาที
        # ผลตรวจยังโผล่ใน log ตอนเปิดเซิร์ฟเวอร์ด้วย รู้ตั้งแต่ต้นว่าไดร์ฟพังไหม
        try:
            import drive as _dr
            st = _dr.ready()
            # ต้องแยก "ผ่านเพราะใช้ได้" กับ "ผ่านเพราะไม่ได้เปิดใช้" ให้ออกจากกัน
            # ไม่งั้นเครื่องที่บ้านซึ่งไม่ได้ใช้ API จะขึ้นว่าพร้อมใช้งานทั้งที่ไม่ได้เปิดเลย
            if st.get("skipped"):
                msg = "ไม่ได้เปิดใช้ (ไฟล์ซิงก์ผ่าน Drive for Desktop ในเครื่อง)"
            elif st.get("ok"):
                msg = "พร้อมใช้งาน"
            else:
                msg = "ใช้ไม่ได้ — " + str(st.get("error"))[:200]
            print("ตรวจไดร์ฟตอนเปิดเซิร์ฟเวอร์: " + msg)
        except Exception as e:
            print(f"ตรวจไดร์ฟตอนเปิดเซิร์ฟเวอร์ไม่ได้: {type(e).__name__}: {e}")
        # กู้คิวที่ค้างไว้ก่อนเซิร์ฟเวอร์เกิดใหม่ ต้องทำหลังตรวจไดร์ฟ เพราะใช้ไดร์ฟอ่าน
        try:
            docmode.restore_queue()
        except Exception as e:
            print(f"กู้คิวไม่สำเร็จ: {type(e).__name__}: {e}")

    threading.Thread(target=work, name="saraban-warmup", daemon=True).start()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
