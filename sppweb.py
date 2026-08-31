"""ตัวเชื่อมต่อเว็บ สพป.สกลนคร เขต ๑ ที่ใช้ร่วมกันทั้งเดสก์ท็อปและเว็บเซิร์ฟเวอร์

โมดูลนี้เป็นจุดเดียวที่รับผิดชอบเรื่อง session, การล็อกอิน, การตรวจ Cloudflare/
หน้า login, การอ่านรายการหนังสือ และการดาวน์โหลดไฟล์แนบ ห้ามให้หน้าจอแต่ละแบบ
เขียน scraper แยกของตัวเอง เพราะจะทำให้การตรวจข้อผิดพลาดไม่ตรงกัน
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

import core
from core import requests, BeautifulSoup, to_thai_digits

BASE = "https://office.sakonarea1.go.th/"
NEWS_URL = core.NEWS_URL
DETAIL_URL = BASE + "modules/book/main/bookdetail_school_total.php?b_id={}"

# Cloudflare ให้คะแนนความเป็นบอทกับเบราว์เซอร์เวอร์ชันเก่าเกินจริงค่อนข้างสูง
# ค่านี้จึงต้องตามเวอร์ชัน Chrome ที่ใช้จริงอยู่เรื่อยๆ ตั้งทับได้ด้วย
# SARABAN_USER_AGENT (ฝั่งเดสก์ท็อปดึง navigator.userAgent จาก Chrome จริงมาใช้อยู่แล้ว)
UA = os.environ.get("SARABAN_USER_AGENT", "").strip() or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "th,en;q=0.9",
    "Referer": BASE,
}

FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".jpg", ".jpeg", ".png",
}
MAX_DOWNLOAD_BYTES = int(os.environ.get("SARABAN_MAX_DOWNLOAD_MB", "80")) * 1024 * 1024

# ==========================================================
# เว้นจังหวะการยิง — กัน rate limit
# ==========================================================
# เว็บ สพป. เป็นเซิร์ฟเวอร์ของหน่วยงานเล็กๆ การยิงรัวไม่หยุดทำให้ทั้งเราและ
# โรงเรียนอื่นเดือดร้อน และเป็นสัญญาณบอทที่ Cloudflare จับได้ง่ายที่สุด
# ตั้งเป็น 0 เพื่อปิดได้ (เช่นตอนรันเทสต์)
REQUEST_GAP = float(os.environ.get("SARABAN_REQUEST_GAP", "0.7"))
RETRY_ON_BUSY = 2                 # ลองซ้ำกี่ครั้งเมื่อโดน 429/503
MAX_RETRY_AFTER = 30.0            # ไม่ค้าง worker รอตาม header ที่ยาวเกินไป
_pace_lock = threading.Lock()
_last_request_at = 0.0


def _pace():
    """หน่วงให้ทุก request ที่ยิงออกจากโปรแกรมนี้ห่างกันอย่างน้อย REQUEST_GAP วินาที"""
    if REQUEST_GAP <= 0:
        return
    global _last_request_at
    with _pace_lock:
        wait = REQUEST_GAP - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry_after(response, attempt: int, *, now: float | None = None) -> float | None:
    """คืนเวลารอจาก Retry-After หรือ ``None`` ถ้านานเกินที่ worker ควรค้าง

    RFC รองรับทั้งจำนวนวินาทีและ HTTP-date ห้ามตัดเวลาลงแล้วยิงซ้ำก่อน
    เวลาที่เว็บกำหนด ถ้าเว็บขอให้รอนานเกินเพดานให้ส่งคำตอบนั้นกลับไปให้
    ผู้เรียกลองใหม่ภายหลัง แทนการนอนค้างอยู่หรือลองก่อนเวลา
    """
    raw = str((getattr(response, "headers", {}) or {}).get("Retry-After", "")).strip()
    delay = None
    if raw.isdigit():
        delay = float(raw)
    elif raw:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = max(0.0, retry_at.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            delay = None

    if delay is None:
        delay = min(2.0 ** attempt, 8.0)
    if delay > MAX_RETRY_AFTER:
        return None
    return delay


def _send(sess, method: str, url: str, **kwargs):
    """ยิง request หนึ่งครั้งโดยเว้นจังหวะ และลองซ้ำให้เมื่อเว็บบอกว่ายุ่งอยู่"""
    call = getattr(sess, method.lower())
    for attempt in range(RETRY_ON_BUSY + 1):
        _pace()
        response = call(url, **kwargs)
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in (429, 503) or attempt == RETRY_ON_BUSY:
            return response
        delay = _retry_after(response, attempt)
        # Retry-After ที่นานเกินเพดาน: ห้ามลองก่อนเวลา และไม่ค้าง worker
        # รอนานเกินไป จึงคืนคำตอบนี้ให้ชั้นบนจัดคิวลองใหม่เอง
        if delay is None:
            return response
        # ทิ้งคำตอบนี้แล้ว ต้องปิดก่อน ไม่งั้น connection ค้างเมื่อใช้ stream=True
        try:
            response.close()
        except Exception:
            pass
        time.sleep(delay)
    return response


class SPPWebError(RuntimeError):
    """ข้อผิดพลาดจากการคุยกับเว็บ สพป."""


class LoginError(SPPWebError):
    """ล็อกอินไม่สำเร็จหรือ session ใช้ไม่ได้ (คงชื่อเดิมเพื่อรองรับโค้ดเก่า)"""


class AccessBlockedError(LoginError):
    """Cloudflare/WAF ปฏิเสธการเชื่อมต่อ"""


class SessionExpiredError(LoginError):
    """เว็บส่งหน้าล็อกอินกลับมาแทนหน้าที่ต้องยืนยันตัวตน"""


class SiteUnavailableError(SPPWebError):
    """เว็บล่ม หมดเวลา หรือเชื่อมต่อไม่ได้"""


class UnexpectedPageError(SPPWebError):
    """ได้ HTML ที่ไม่ใช่ทั้งหน้า login และหน้ารายการหนังสือ"""


class UpstreamResponseError(SPPWebError):
    """เว็บตอบ HTTP error ที่ไม่ใช่ Cloudflare/session หมดอายุ"""


class DownloadError(SPPWebError):
    """ไฟล์แนบที่ดาวน์โหลดมาไม่ใช่ไฟล์ที่คาดไว้หรือดาวน์โหลดไม่ครบ"""


# คำที่พบได้เฉพาะใน "หน้า challenge จริง" เท่านั้น
#
# ห้ามใส่คำว่า challenge-platform ลอยๆ กลับเข้ามาเด็ดขาด — Cloudflare แทรก
# <script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"> ลงใน "หน้าปกติ
# ที่สำเร็จ" ทุกหน้าเพื่อเก็บลายนิ้วมือเบราว์เซอร์ ไม่ใช่สัญญาณว่าถูกบล็อก
# ถ้าใส่กลับมา ทุก request ที่สำเร็จจะถูกตัดสินว่าโดน Cloudflare ทันที
# (เคยเป็นบั๊กนี้มาแล้ว: ยิงได้ HTTP 200 ได้หน้าล็อกอินครบ แต่โปรแกรมแจ้งว่าถูกกัน
#  และอาการจะโผล่เฉพาะบางเน็ต เพราะ Cloudflare แทรกสคริปต์ถี่ตามคะแนนของ IP)
_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "/cdn-cgi/challenge-platform/h/",      # path ของหน้า challenge ตัวจริง
    "cf_chl_opt", "cf-challenge-running",
    "just a moment", "attention required", "checking your browser",
    "enable javascript and cookies to continue",
)


# ==========================================================
# Session / Cookie
# ==========================================================
def _set_cookie_item(sess, item: dict):
    name = str(item.get("name") or "").strip()
    value = str(item.get("value") or "")
    if not name:
        return
    kw = {"path": str(item.get("path") or "/")}
    domain = str(item.get("domain") or "").strip()
    if domain:
        kw["domain"] = domain
    if item.get("secure") is not None:
        kw["secure"] = bool(item.get("secure"))
    expiry = item.get("expiry", item.get("expires"))
    if expiry not in (None, ""):
        try:
            kw["expires"] = int(expiry)
        except (TypeError, ValueError):
            pass
    sess.cookies.set(name, value, **kw)


def _cookie_header_items(value: str) -> list[dict]:
    jar = SimpleCookie()
    try:
        jar.load(value)
    except Exception:
        jar = SimpleCookie()
    if jar:
        return [{"name": key, "value": morsel.value, "path": "/"}
                for key, morsel in jar.items()]
    out = []
    for part in value.split(";"):
        if "=" not in part:
            continue
        name, val = part.split("=", 1)
        if name.strip():
            out.append({"name": name.strip(), "value": val.strip(), "path": "/"})
    return out


def new_session(cookie=None, *, cookies=None, user_agent: str | None = None):
    """สร้าง requests.Session จากสถานะเดิมหรือ cookie ของ Selenium

    รูปแบบที่รองรับเพื่อไม่ให้โค้ดเก่าพัง:
      new_session("ค่า-PHPSESSID")
      new_session("PHPSESSID=...; cf_clearance=...")
      new_session(export_session(sess))
      new_session(cookies=driver.get_cookies(), user_agent=browser_ua)
    """
    if isinstance(cookie, dict) and cookies is None:
        state = cookie
        cookies = state.get("cookies")
        user_agent = user_agent or state.get("user_agent")
        cookie = None
    elif isinstance(cookie, (list, tuple)) and cookies is None:
        cookies, cookie = cookie, None
    if cookie is not None and cookies is not None:
        raise ValueError("ระบุ cookie หรือ cookies อย่างใดอย่างหนึ่งเท่านั้น")

    sess = requests.Session()
    sess.headers.update(BASE_HEADERS)
    if user_agent:
        sess.headers["User-Agent"] = str(user_agent)

    if isinstance(cookies, dict):
        if "name" in cookies:
            _set_cookie_item(sess, cookies)
        else:
            for name, value in cookies.items():
                _set_cookie_item(sess, {"name": name, "value": value, "path": "/"})
    elif cookies:
        for item in cookies:
            if isinstance(item, dict):
                _set_cookie_item(sess, item)

    if isinstance(cookie, str) and cookie.strip():
        raw = cookie.strip()
        if re.match(r"^[^=;\s]+\s*=", raw):
            for item in _cookie_header_items(raw):
                item["domain"] = "office.sakonarea1.go.th"
                _set_cookie_item(sess, item)
        else:
            _set_cookie_item(sess, {
                "name": "PHPSESSID", "value": raw,
                "domain": "office.sakonarea1.go.th", "path": "/",
            })
    return sess


def export_session(sess) -> dict:
    """คืนสถานะที่สร้าง Session ใหม่ได้ โดยไม่แชร์ Session ข้ามเธรด"""
    cookies = []
    for c in sess.cookies:
        item = {
            "name": c.name, "value": c.value,
            "domain": c.domain or "", "path": c.path or "/",
            "secure": bool(c.secure),
        }
        if c.expires is not None:
            item["expires"] = int(c.expires)
        cookies.append(item)
    return {
        "version": 1,
        "user_agent": sess.headers.get("User-Agent", UA),
        "cookies": cookies,
    }


def get_cookie(sess) -> str:
    """คืน PHPSESSID แบบเดิมสำหรับโค้ด/หน้าจอรุ่นเก่า"""
    for c in sess.cookies:
        if c.name == "PHPSESSID":
            return c.value
    return ""


# ==========================================================
# ตรวจคำตอบจากเว็บ
# ==========================================================
def _response_text(response) -> str:
    try:
        response.encoding = "utf-8"
    except Exception:
        pass
    try:
        return response.text or ""
    except Exception:
        return ""


def _has_real_content(text: str) -> bool:
    """ได้หน้าจริงของเว็บ สพป. กลับมาไหม (หน้าล็อกอิน หรือหน้ารายการหนังสือ)

    หน้า challenge ของ Cloudflare ไม่มีทั้งฟอร์มรหัสผ่านและตารางหนังสือ
    ถ้าเจออย่างใดอย่างหนึ่ง แปลว่าทะลุถึงเว็บจริงแล้ว ไม่ได้ถูกกัน
    """
    if not text:
        return False
    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception:
        return False
    return _is_login_page(soup) or _is_news_page(soup)


def _has_explicit_cloudflare_challenge(response, text: str) -> bool:
    """มีหลักฐานจาก header/body ว่าเป็น Cloudflare challenge จริงหรือไม่"""
    headers = getattr(response, "headers", {}) or {}
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    low = (text or "").lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _is_cloudflare_block(response, text: str) -> bool:
    status = int(getattr(response, "status_code", 0) or 0)
    if _has_explicit_cloudflare_challenge(response, text):
        return True
    if status in (403, 429):
        # 403/429 มาจากตัวเว็บ PHP เองก็ได้ (เช่นเดา path ไฟล์แนบผิด)
        # ถ้าได้หน้าจริงกลับมาด้วย แปลว่าไม่ได้ถูก Cloudflare กัน
        return not _has_real_content(text)
    return status == 503 and "cloudflare" in (text or "").lower()


def _is_login_page(soup) -> bool:
    if soup.find("input", attrs={"type": re.compile(r"^password$", re.I)}):
        return True
    names = {str(x.get("name") or "").lower() for x in soup.find_all(["input", "button"])}
    return "username" in names and ("pass" in names or "password" in names) and "login_submit" in names


def _doc_links(soup):
    return soup.find_all("a", onclick=lambda v: v and "bookdetail" in v)


def _is_news_page(soup) -> bool:
    if _doc_links(soup):
        return True
    if soup.find("table", id=re.compile(r"book", re.I)):
        return True
    text = " ".join(soup.stripped_strings)
    return "เลขหนังสือ" in text and "เรื่อง" in text and (
        "ลงวันที่" in text or "รายละเอียด" in text)


def _raise_for_response(response, text: str, context: str):
    status = int(getattr(response, "status_code", 0) or 0)
    if _is_cloudflare_block(response, text):
        # แนบ Ray ID มาด้วย — เป็นรหัสอ้างอิงที่ผู้ดูแลเว็บ สพป. ใช้ค้นหาใน log ได้ว่า
        # คำขอของเราถูกกฎข้อไหนปัดตก จำเป็นมากเวลาต้องติดต่อขอเปิดสิทธิ์
        ray = str((getattr(response, "headers", {}) or {}).get("cf-ray", "")).strip()
        ref = f" [Ray ID: {ray}]" if ray else ""
        raise AccessBlockedError(
            f"เว็บ สพป. ปฏิเสธการเชื่อมต่อ (Cloudflare/WAF, HTTP {status}){ref} — "
            "ถ้ารันบนเซิร์ฟเวอร์ให้ขอ allowlist IP หรือใช้ตัวดึงข้อมูลจากเครือข่ายโรงเรียน")
    if status >= 500:
        raise SiteUnavailableError(f"เว็บ สพป. ขัดข้องระหว่าง{context} (HTTP {status})")
    if status >= 400:
        raise UpstreamResponseError(f"เว็บ สพป. ตอบผิดพลาดระหว่าง{context} (HTTP {status})")


def _request_html(sess, method: str, url: str, *, context: str,
                  timeout: int = 20, authenticated: bool = False, **kwargs):
    try:
        response = _send(sess, method, url, timeout=timeout, **kwargs)
    except SPPWebError:
        raise
    except Exception as e:
        raise SiteUnavailableError(f"เชื่อมต่อเว็บ สพป. ไม่สำเร็จระหว่าง{context}: {e}") from e

    text = _response_text(response)
    soup = BeautifulSoup(text, "html.parser")
    # ตรวจหน้า login ก่อนดูรหัสสถานะ — เว็บ PHP บางหน้าตอบ 403 พร้อมหน้าล็อกอิน
    # ซึ่งความหมายจริงคือ "session หมดอายุ" ไม่ใช่ "เว็บพัง"
    if _is_login_page(soup):
        if authenticated:
            raise SessionExpiredError("session เว็บ สพป. หมดอายุหรือยังไม่ได้ล็อกอิน")
        # is_logged_in ต้องตอบ False แม้ PHP จะตั้ง HTTP 403 ให้หน้าล็อกอิน
        # นี่เป็นคำตอบจากแอปจริง ไม่ใช่ Cloudflare challenge
        return response, soup
    _raise_for_response(response, text, context)
    return response, soup


def is_logged_in(sess) -> bool:
    """False เฉพาะหน้า login; 403/Challenge/หน้าประหลาดต้องแจ้ง error ชัดเจน"""
    _, soup = _request_html(
        sess, "get", NEWS_URL, context="ตรวจสอบ session", timeout=20,
        authenticated=False,
    )
    if _is_login_page(soup):
        return False
    if _is_news_page(soup):
        return True
    raise UnexpectedPageError(
        "เว็บตอบกลับมาแต่ไม่ใช่หน้าล็อกอินหรือหน้ารายการหนังสือ "
        "(หน้าเว็บอาจเปลี่ยนรูปแบบ)")


def assert_authenticated(sess):
    if not is_logged_in(sess):
        raise SessionExpiredError("session เว็บ สพป. หมดอายุหรือยังไม่ได้ล็อกอิน")
    return sess


def login(user: str | None = None, pwd: str | None = None):
    """ล็อกอินด้วยฟอร์มปกติ เหมาะกับเครื่อง/IP ที่ Cloudflare อนุญาต"""
    if user is None or pwd is None:
        cfg = core.load_config()
        user = user or cfg.get("login_user", "").strip()
        pwd = pwd or cfg.get("login_pass", "").strip()
    if not user or not pwd:
        raise LoginError("ยังไม่ได้ตั้งชื่อผู้ใช้/รหัสผ่านของเว็บ สพป. (ตั้งได้ที่หน้าตั้งค่า)")

    sess = new_session()
    response, soup = _request_html(
        sess, "get", BASE, context="เปิดหน้าล็อกอิน", timeout=20,
        authenticated=False,
    )
    pwd_input = soup.find("input", attrs={"type": re.compile(r"^password$", re.I)})
    form = pwd_input.find_parent("form") if pwd_input else None
    if form is None:
        raise UnexpectedPageError("หาฟอร์มล็อกอินของเว็บ สพป. ไม่พบ (หน้าเว็บอาจเปลี่ยนรูปแบบ)")

    data = {x.get("name"): (x.get("value") or "")
            for x in form.find_all("input") if x.get("name")}
    for button in form.find_all("button"):
        if button.get("name"):
            data[button["name"]] = button.get("value") or ""

    pwd_name = pwd_input.get("name") or "pass"
    user_input = form.find("input", attrs={
        "name": re.compile(r"^(username|user|login|login_user)$", re.I),
        "type": re.compile(r"^(text|email)?$", re.I),
    })
    if user_input is None:
        user_input = form.find("input", attrs={"type": re.compile(r"^(text|email)$", re.I)})
    user_name = user_input.get("name") if user_input and user_input.get("name") else "username"
    data[user_name] = user
    data[pwd_name] = pwd

    action = form.get("action") or "index.php"
    post_url = urljoin(getattr(response, "url", BASE) or BASE, action)
    _request_html(
        sess, "post", post_url, context="ส่งฟอร์มล็อกอิน", timeout=20,
        authenticated=False, data=data,
    )
    if not is_logged_in(sess):
        raise LoginError("ล็อกอินไม่ผ่าน — ตรวจสอบชื่อผู้ใช้และรหัสผ่านอีกครั้ง")
    return sess


# ==========================================================
# ดึงรายการหนังสือ
# ==========================================================
def _book_ids(soup) -> set[str]:
    out = set()
    for link in _doc_links(soup):
        match = re.search(r"b_id=(\d+)", link.get("onclick", ""))
        if match:
            out.add(match.group(1))
    return out


# เว็บอาจเพิ่มหน้าใหม่มาหลายหน้าตั้งแต่รอบก่อน จึงต้องเดินไปข้างหน้าเรื่อยๆ
# ตั้งเพดานกันวนไม่จบถ้าเว็บตอบแปลกๆ
MAX_PAGE_PROBE = 12


def _page_soup(sess, page: int):
    _, soup = _request_html(
        sess, "get", f"{NEWS_URL}&page={page}",
        context=f"เปิดรายการหนังสือหน้า {page}", timeout=20,
        authenticated=True,
    )
    return soup


def _scan_last_page(sess):
    """คืน (เลขหน้าสุดท้ายจริง, {เลขหน้า: soup ที่โหลดมาแล้ว})

    แถบเลขหน้าของเว็บเป็นหน้าต่างเลื่อน โชว์เลขไม่ครบ เลขมากสุดที่เห็นจึง
    ไม่ใช่หน้าสุดท้ายจริง ต้องเดินไปข้างหน้าจนกว่าจะไม่เจอเรื่องใหม่

    เว็บ "หนีบ" เลขหน้าที่เกินจริง — ขอหน้า ๔๐๙ ทั้งที่มีถึง ๔๐๘ ก็คืนเนื้อหา
    หน้า ๔๐๘ กลับมา จึงใช้ "ได้ id ชุดเดิม" เป็นสัญญาณว่าสุดทางแล้ว

    เดิมเขยิบแค่หน้าเดียวแล้วหยุด พอเว็บโตเกินไป ๑ หน้า หนังสือใหม่ก็หายเงียบ
    (เคยทำให้เรื่องของวันนี้ไม่ขึ้นในรายการ) ยิงเพิ่มอีกไม่กี่ request
    คุ้มกว่าปล่อยให้ตกหล่นมาก

    หน้าที่โหลดระหว่างเดินหา ส่งกลับไปให้ list_documents ใช้ต่อ เพราะมันต้องการ
    หน้าท้ายๆ ชุดเดียวกันพอดี จะได้ไม่ต้องโหลดซ้ำ
    """
    _, soup = _request_html(
        sess, "get", NEWS_URL, context="ค้นหาหน้าสุดท้าย", timeout=20,
        authenticated=True,
    )
    if not _is_news_page(soup):
        raise UnexpectedPageError("ไม่พบตารางรายการหนังสือในหน้าที่เว็บส่งกลับมา")
    pages = [1]
    for anchor in soup.find_all("a", href=re.compile(r"(?:[?&]|^)page=(\d+)")):
        match = re.search(r"(?:[?&]|^)page=(\d+)", anchor.get("href", ""))
        if match:
            pages.append(int(match.group(1)))
    last = max(pages)

    # เปิดหน้า last ตรงๆ เพื่อให้มีฐานเทียบที่รู้แน่ว่าเป็นหน้าไหน
    # (หน้า landing เอามาเทียบไม่ได้ เพราะไม่รู้ว่าเว็บกำลังโชว์หน้าไหนอยู่)
    seen = {last: _page_soup(sess, last)}
    last_ids = _book_ids(seen[last])

    for _ in range(MAX_PAGE_PROBE):
        probe_soup = _page_soup(sess, last + 1)
        probe_ids = _book_ids(probe_soup)
        if not probe_ids or probe_ids == last_ids:
            break
        last += 1
        seen[last], last_ids = probe_soup, probe_ids
    else:
        # ถ้าทุกหน้าที่ลองยังมี id ชุดใหม่ เรายังพิสูจน์ไม่ได้ว่าถึงหน้าสุดท้าย
        # ห้ามคืนค่า last ปลอมแล้วทำให้หนังสือใหม่หายไปแบบเงียบๆ
        raise UnexpectedPageError(
            "ค้นหาหน้าสุดท้ายไม่สำเร็จ: "
            f"แถบเลขหน้าล้าหลังเกิน {MAX_PAGE_PROBE} หน้า "
            "จึงหยุดเพื่อไม่ส่งรายการที่ไม่ครบ"
        )
    return last, seen


def find_last_page(sess) -> int:
    return _scan_last_page(sess)[0]


THAI_MON = {"มค": 1, "กพ": 2, "มีค": 3, "เมย": 4, "พค": 5, "มิย": 6,
            "กค": 7, "สค": 8, "กย": 9, "ตค": 10, "พย": 11, "ธค": 12}


def parse_sent_at(text: str):
    """แปลง '27 สค 2569 09:19:09 น.' เป็นคีย์วันที่/วันที่แสดง/เวลา"""
    value = (text or "").replace("น.", "").strip()
    match = re.match(r"(\d+)\s+([ก-ฮ.]+)\s+(\d+)(?:\s+(\d+):(\d+))?", value)
    if not match:
        return "", (text or "").strip(), ""
    day = int(match.group(1))
    month_raw = match.group(2).replace(".", "")
    year = int(match.group(3))
    month = THAI_MON.get(month_raw, 0)
    if not month:
        return "", value, ""
    ce_year = year - 543 if year > 2400 else year
    hour, minute = match.group(4), match.group(5)
    return (
        f"{ce_year:04d}-{month:02d}-{day:02d}",
        to_thai_digits(f"{day} {core.THAI_MONTHS_ABBR[month - 1]} {year}"),
        to_thai_digits(f"{hour}:{minute}") if hour else "",
    )


def _row_of(link) -> dict:
    row = link.find_parent("tr")
    cols = row.find_all("td") if row else []

    def col(index, default="-"):
        return cols[index].text.strip() if len(cols) > index else default

    key, shown, hhmm = parse_sent_at(col(6, ""))
    return {
        "doc_no": col(1),
        "doc_title": col(2),
        "doc_date": col(4),
        "sender": (col(5, "สพป.สกลนคร เขต 1").split("[")[0].strip()
                   or "สพป.สกลนคร เขต 1"),
        "sent_key": key,
        "sent_date": shown,
        "sent_time": hhmm,
    }


def list_documents(sess, pages: int = 2) -> list:
    """คืนหนังสือจากหน้าท้ายๆ เรียงใหม่สุดขึ้นก่อน"""
    last, cached = _scan_last_page(sess)
    count = max(1, int(pages))
    wanted = range(max(1, last - count + 1), last + 1)
    out, seen = [], set()
    for page in wanted:
        # หน้าที่โหลดไปแล้วตอนหาเลขหน้าสุดท้าย ใช้ซ้ำเลย ไม่ต้องยิงอีกรอบ
        soup = cached.get(page)
        if soup is None:
            soup = _page_soup(sess, page)
        if not _is_news_page(soup):
            raise UnexpectedPageError(f"ไม่พบตารางรายการหนังสือในหน้าที่ {page}")
        for link in _doc_links(soup):
            match = re.search(r"b_id=(\d+)", link.get("onclick", ""))
            if not match or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            out.append({"book_id": match.group(1), "page": page, **_row_of(link)})
    out.reverse()
    return out


def load_history() -> set:
    import store
    return store.get_store().history_ids()


def mark_done(book_id: str):
    import store
    store.get_store().add_history(book_id)


def unmark(book_id: str) -> bool:
    import store
    return store.get_store().remove_history(book_id)


def list_new_documents(sess, pages: int = 2) -> list:
    done = load_history()
    return [doc for doc in list_documents(sess, pages) if doc["book_id"] not in done]


# ==========================================================
# รายละเอียดหนังสือ + ไฟล์แนบ
# ==========================================================
def _urgency_emoji(text: str) -> str:
    if "ด่วนที่สุด" in text:
        return "🔴"
    if "ด่วนมาก" in text:
        return "🟠"
    if "ด่วน" in text:
        return "🟡"
    return "🟢"


def _attachment_ext(href: str) -> str:
    return os.path.splitext(urlsplit(str(href or "")).path)[1].lower()


# เว็บวางไฟล์แนบไว้หลายที่ ต้อง "เดา" path เอา แต่ทั้งเว็บใช้แบบเดียวกันตลอด
# จึงจำกฎที่เพิ่งใช้ได้ไว้ แล้วเอามาลองก่อนเป็นตัวแรกในครั้งถัดไป
# ทำให้จากเดิมยิงเดาสูงสุด ๕ ครั้งต่อไฟล์แนบ ๑ ตัว เหลือ ๑ ครั้งเกือบตลอด
_rule_hint = None
_rule_hint_lock = threading.Lock()


def _remember_rule(rule: str):
    global _rule_hint
    with _rule_hint_lock:
        _rule_hint = rule


def _attachment_candidates(href: str, detail_url: str) -> list[tuple[str, str]]:
    """คืน [(ชื่อกฎ, url), ...] เรียงตามกฎที่เพิ่งใช้ได้ก่อน"""
    raw = str(href or "").strip()
    if not raw or _attachment_ext(raw) not in FILE_EXTENSIONS:
        return []
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.scheme.lower() not in ("http", "https"):
        return []

    candidates = []
    seen = set()

    def add(rule, value):
        if value and value not in seen:
            seen.add(value)
            candidates.append((rule, value))

    if parsed.scheme or raw.startswith("//") or raw.startswith("/"):
        add("absolute", urljoin(detail_url, raw))
    elif raw.startswith("."):
        add("relative", urljoin(detail_url, raw))
        add("root", urljoin(BASE, raw.lstrip("./")))
    else:
        clean = raw.lstrip("./")
        if clean.startswith("bookregister/") or clean.startswith("book/"):
            add("modules", urljoin(BASE + "modules/", clean))
        add("bookregister", urljoin(BASE + "modules/bookregister/", clean))
        add("book", urljoin(BASE + "modules/book/", clean))
        add("relative", urljoin(detail_url, raw))
        add("root", urljoin(BASE, raw))

    with _rule_hint_lock:
        hint = _rule_hint
    if hint:
        candidates.sort(key=lambda item: item[0] != hint)
    return candidates


PROBE_PREFIX_BYTES = 8192


def _close_response(response):
    try:
        response.close()
    except Exception:
        pass


def _read_prefix(response, limit: int = PROBE_PREFIX_BYTES) -> bytes:
    """อ่าน body แค่ช่วงแรก แล้วปล่อยให้ผู้เรียกปิด response ทันที"""
    prefix = bytearray()
    for chunk in response.iter_content(chunk_size=min(limit, 8192)):
        if not chunk:
            continue
        prefix.extend(chunk[:limit - len(prefix)])
        if len(prefix) >= limit:
            break
    return bytes(prefix)


def _content_type(response) -> str:
    return str((getattr(response, "headers", {}) or {}).get("Content-Type", "")).lower()


def _headers_identify_file(response) -> bool:
    """HEAD ชัดเจนพอที่จะไม่ต้อง GET ซ้ำหรือไม่"""
    headers = getattr(response, "headers", {}) or {}
    disposition = str(headers.get("Content-Disposition", "")).lower()
    if "attachment" in disposition or "filename=" in disposition:
        return True
    content_type = _content_type(response).split(";", 1)[0].strip()
    return (
        content_type == "application/pdf"
        or content_type == "application/msword"
        or content_type.startswith("application/vnd.")
        or content_type.startswith("application/zip")
        or content_type.startswith("application/x-")
        or content_type.startswith("image/")
    )


def _prefix_is_html_or_error(prefix: bytes, content_type: str) -> bool:
    """แยกหน้า error แม้เว็บลืมหรือใส่ Content-Type ผิด"""
    if not prefix:
        return True
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    stripped = prefix.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    low = stripped[:PROBE_PREFIX_BYTES].lower()
    # เชื่อ magic bytes มากกว่า Content-Type: เว็บเก่าบางตัวส่ง PDF/Office เป็น text/plain
    if stripped.startswith((
        b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"Rar!\x1a\x07",
    )):
        return False
    if (
        low.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
        or b"<html" in low
        or b"<form" in low
    ):
        return True
    if kind.startswith("text/") or kind in {
        "application/json", "application/problem+json", "application/xml",
        "text/xml", "application/xhtml+xml",
    }:
        return True
    decoded = stripped[:PROBE_PREFIX_BYTES].decode("utf-8", errors="ignore").lower()
    if any(marker in decoded for marker in (
        "not found", "forbidden", "unauthorized", "access denied",
        "permission denied", "internal server error", "bad gateway",
        "service unavailable", "ไม่พบไฟล์", "ไม่มีสิทธิ์",
    )):
        return True
    if low.startswith((b"{", b"[")) and any(
            marker in low for marker in (b'"error"', b'"message"', b'"status"')):
        return True
    return False


def _probe_attachment(sess, url: str, *, timeout: int):
    """ตรวจลิงก์ด้วย HEAD และถอยไป ranged GET เมื่อคำตอบกำกวม

    HEAD 403 ไม่ได้แปลว่าถูก Cloudflare กั้นเสมอไป: PHP/เว็บเซิร์ฟเวอร์มัก
    ปิด HEAD สำหรับ path ที่เดาผิด จึงดู body ช่วงแรกด้วย GET ก่อนตัดสิน
    และปิดทุก response ในทุกทางออก
    """
    probe_url = url
    response = _send(sess, "head", url, timeout=timeout, allow_redirects=True)
    try:
        status = int(getattr(response, "status_code", 0) or 0)
        probe_url = getattr(response, "url", url) or url
        # HEAD ปกติไม่มี body แต่บางเซิร์ฟเวอร์ส่งมา อ่านเฉพาะเมื่อต้องแยก error
        head_text = _response_text(response) if status >= 400 else ""
        if _has_explicit_cloudflare_challenge(response, head_text):
            _raise_for_response(response, head_text, "ตรวจลิงก์ไฟล์แนบ")
        if head_text and _is_login_page(BeautifulSoup(head_text, "html.parser")):
            raise SessionExpiredError("session หมดอายุระหว่างตรวจไฟล์แนบ")
        if status in (404, 410):
            return "missing", probe_url
        if 200 <= status < 400 and _headers_identify_file(response):
            return "ok", probe_url
        if status == 429 or status >= 500:
            _raise_for_response(response, head_text, "ตรวจลิงก์ไฟล์แนบ")
        # 403/405/501 และ 2xx ที่ Content-Type กำกวม ต้องดู prefix ด้วย GET
    finally:
        _close_response(response)

    response = _send(
        sess, "get", probe_url, timeout=timeout, stream=True,
        allow_redirects=True, headers={"Range": f"bytes=0-{PROBE_PREFIX_BYTES - 1}"},
    )
    try:
        prefix = _read_prefix(response)
        text = prefix.decode("utf-8", errors="ignore")
        status = int(getattr(response, "status_code", 0) or 0)
        final_url = getattr(response, "url", probe_url) or probe_url
        if _has_explicit_cloudflare_challenge(response, text):
            _raise_for_response(response, text, "ตรวจลิงก์ไฟล์แนบ")
        if _is_login_page(BeautifulSoup(text, "html.parser")):
            raise SessionExpiredError("session หมดอายุระหว่างตรวจไฟล์แนบ")
        # path ที่เดาผิดอาจตอบ 401/403 จาก PHP โดยไม่ใช่ WAF
        if status in (400, 401, 403, 404, 410):
            return "missing", final_url
        _raise_for_response(response, text, "ตรวจลิงก์ไฟล์แนบ")
        if _prefix_is_html_or_error(prefix, _content_type(response)):
            return "html", final_url
        return "ok", final_url
    finally:
        _close_response(response)


def _pick_attachment_url(sess, candidates: list[tuple[str, str]], *,
                         timeout: int = 20) -> str | None:
    """หา url ของไฟล์แนบตัวจริง ยิงให้น้อยที่สุดเท่าที่ทำได้"""
    for rule, candidate in candidates:
        try:
            verdict, final_url = _probe_attachment(sess, candidate, timeout=timeout)
        except SPPWebError:
            raise
        except Exception as e:
            raise SiteUnavailableError(f"ตรวจลิงก์ไฟล์แนบไม่สำเร็จ: {e}") from e

        if verdict == "ok":
            _remember_rule(rule)
            return final_url
    return None


def fetch_detail(sess, book_id: str) -> dict:
    detail_url = DETAIL_URL.format(str(book_id))
    _, soup = _request_html(
        sess, "get", detail_url, context="เปิดรายละเอียดหนังสือ", timeout=30,
        authenticated=True,
    )

    detail_text = " ".join(soup.stripped_strings)
    has_file_link = bool(soup.find(
        "a", href=lambda value: value and _attachment_ext(value) in FILE_EXTENSIONS))
    if "รายละเอียดหนังสือ" not in detail_text and not has_file_link:
        raise UnexpectedPageError(
            "เว็บตอบกลับมาแต่ไม่ใช่หน้ารายละเอียดหนังสือ (หน้าเว็บอาจเปลี่ยนรูปแบบ)")

    attachments, main_pdf, seen = [], "", set()
    for tag in soup.find_all("a", href=lambda value: value and _attachment_ext(value) in FILE_EXTENSIONS):
        ext = _attachment_ext(tag.get("href", ""))
        final_url = _pick_attachment_url(
            sess, _attachment_candidates(tag.get("href", ""), detail_url), timeout=20,
        )
        if not final_url or final_url in seen:
            continue
        seen.add(final_url)
        name = re.sub(r"^\d+\.\s*", "", tag.text.strip()) or "เอกสารแนบ"
        attachments.append({"name": name, "url": final_url})
        if not main_pdf and ext == ".pdf":
            main_pdf = final_url
    return {
        "book_id": str(book_id),
        "emoji": _urgency_emoji(soup.text),
        "attachments": attachments,
        "main_pdf": main_pdf,
    }


def download(sess, url: str, dest: str, *, expected_type: str | None = None) -> str:
    """ดาวน์โหลดแบบ atomic และตรวจว่า PDF ไม่ใช่ HTML login/Challenge ปลอม"""
    expected = (expected_type or _attachment_ext(url) or os.path.splitext(dest)[1]).lower()
    part = dest + ".part"
    response = None
    total = 0
    try:
        response = _send(sess, "get", url, timeout=120, stream=True, allow_redirects=True)
        length = str((getattr(response, "headers", {}) or {}).get("Content-Length", "")).strip()
        if length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
            raise DownloadError(
                f"ไฟล์แนบใหญ่เกินกำหนด {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")

        # ดึงแค่ prefix มาแยก login/challenge/error ก่อนสร้างไฟล์ .part
        # สำคัญกับ HTTP 403: ถ้าดูแค่ status/body ว่าง หน้า login จะถูกเหมาผิดเป็น Cloudflare
        chunks = response.iter_content(chunk_size=PROBE_PREFIX_BYTES)
        buffered = []
        prefix = bytearray()
        while len(prefix) < PROBE_PREFIX_BYTES:
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            if not chunk:
                continue
            buffered.append(chunk)
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise DownloadError(
                    f"ไฟล์แนบใหญ่เกินกำหนด {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
            prefix.extend(chunk[:PROBE_PREFIX_BYTES - len(prefix)])

        prefix_bytes = bytes(prefix)
        probe_text = prefix_bytes.decode("utf-8", errors="ignore")
        if _has_explicit_cloudflare_challenge(response, probe_text):
            _raise_for_response(response, probe_text, "ดาวน์โหลดไฟล์แนบ")
        if _is_login_page(BeautifulSoup(probe_text, "html.parser")):
            raise SessionExpiredError("session หมดอายุระหว่างดาวน์โหลดไฟล์แนบ")
        _raise_for_response(response, probe_text, "ดาวน์โหลดไฟล์แนบ")
        if _prefix_is_html_or_error(prefix_bytes, _content_type(response)):
            raise DownloadError("เว็บส่งหน้า HTML/error กลับมาแทนไฟล์แนบ")
        if expected in ("pdf", ".pdf") and b"%PDF-" not in prefix_bytes[:1024]:
            raise DownloadError("เว็บส่งข้อมูลที่ไม่ใช่ PDF กลับมา (อาจเป็นหน้า login หรือ error)")

        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with open(part, "wb") as out:
            for chunk in buffered:
                out.write(chunk)
            for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise DownloadError(
                        f"ไฟล์แนบใหญ่เกินกำหนด {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
                out.write(chunk)

        if total == 0:
            raise DownloadError("ไฟล์แนบที่ดาวน์โหลดมาว่างเปล่า")
        os.replace(part, dest)
        return dest
    except SPPWebError:
        try:
            if os.path.exists(part):
                os.remove(part)
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            if os.path.exists(part):
                os.remove(part)
        except Exception:
            pass
        raise DownloadError(f"ดาวน์โหลดไฟล์แนบไม่สำเร็จ: {e}") from e
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def attach_text(attachments: list) -> str:
    if not attachments:
        return f"📥 โหลดไฟล์: {NEWS_URL}"
    return "\n".join(
        f"📥 {to_thai_digits(index)}. {item['name']}\n👉 {item['url']}"
        for index, item in enumerate(attachments, 1)
    )
