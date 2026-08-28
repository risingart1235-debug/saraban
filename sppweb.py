"""ตัวเชื่อมต่อเว็บ สพป.สกลนคร เขต ๑ ที่ใช้ร่วมกันทั้งเดสก์ท็อปและเว็บเซิร์ฟเวอร์

โมดูลนี้เป็นจุดเดียวที่รับผิดชอบเรื่อง session, การล็อกอิน, การตรวจ Cloudflare/
หน้า login, การอ่านรายการหนังสือ และการดาวน์โหลดไฟล์แนบ ห้ามให้หน้าจอแต่ละแบบ
เขียน scraper แยกของตัวเอง เพราะจะทำให้การตรวจข้อผิดพลาดไม่ตรงกัน
"""
from __future__ import annotations

import os
import re
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

import core
from core import requests, BeautifulSoup, to_thai_digits

BASE = "https://office.sakonarea1.go.th/"
NEWS_URL = core.NEWS_URL
DETAIL_URL = BASE + "modules/book/main/bookdetail_school_total.php?b_id={}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

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


def _is_cloudflare_block(response, text: str) -> bool:
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True                      # Cloudflare บอกมาเองตรงๆ
    low = (text or "").lower()
    if any(marker in low for marker in _CHALLENGE_MARKERS):
        return True
    if status in (403, 429):
        # 403/429 มาจากตัวเว็บ PHP เองก็ได้ (เช่นเดา path ไฟล์แนบผิด)
        # ถ้าได้หน้าจริงกลับมาด้วย แปลว่าไม่ได้ถูก Cloudflare กัน
        return not _has_real_content(text)
    return status == 503 and "cloudflare" in low


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
        raise AccessBlockedError(
            f"เว็บ สพป. ปฏิเสธการเชื่อมต่อ (Cloudflare/WAF, HTTP {status}) — "
            "ถ้ารันบนเซิร์ฟเวอร์ให้ขอ allowlist IP หรือใช้ตัวดึงข้อมูลจากเครือข่ายโรงเรียน")
    if status >= 500:
        raise SiteUnavailableError(f"เว็บ สพป. ขัดข้องระหว่าง{context} (HTTP {status})")
    if status >= 400:
        raise UpstreamResponseError(f"เว็บ สพป. ตอบผิดพลาดระหว่าง{context} (HTTP {status})")


def _request_html(sess, method: str, url: str, *, context: str,
                  timeout: int = 20, authenticated: bool = False, **kwargs):
    try:
        call = getattr(sess, method.lower())
        response = call(url, timeout=timeout, **kwargs)
    except SPPWebError:
        raise
    except Exception as e:
        raise SiteUnavailableError(f"เชื่อมต่อเว็บ สพป. ไม่สำเร็จระหว่าง{context}: {e}") from e

    text = _response_text(response)
    soup = BeautifulSoup(text, "html.parser")
    # ตรวจหน้า login ก่อนดูรหัสสถานะ — เว็บ PHP บางหน้าตอบ 403 พร้อมหน้าล็อกอิน
    # ซึ่งความหมายจริงคือ "session หมดอายุ" ไม่ใช่ "เว็บพัง"
    if authenticated and _is_login_page(soup):
        raise SessionExpiredError("session เว็บ สพป. หมดอายุหรือยังไม่ได้ล็อกอิน")
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


def find_last_page(sess) -> int:
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

    probe_url = f"{NEWS_URL}&page={last + 1}"
    _, probe_soup = _request_html(
        sess, "get", probe_url, context="ตรวจหน้าถัดไป", timeout=20,
        authenticated=True,
    )
    probe_ids = _book_ids(probe_soup)
    if probe_ids and probe_ids != _book_ids(soup):
        return last + 1
    return last


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
    last = find_last_page(sess)
    count = max(1, int(pages))
    wanted = range(max(1, last - count + 1), last + 1)
    out, seen = [], set()
    for page in wanted:
        _, soup = _request_html(
            sess, "get", f"{NEWS_URL}&page={page}",
            context=f"เปิดรายการหนังสือหน้า {page}", timeout=20,
            authenticated=True,
        )
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


def _attachment_candidates(href: str, detail_url: str) -> list[str]:
    raw = str(href or "").strip()
    if not raw or _attachment_ext(raw) not in FILE_EXTENSIONS:
        return []
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.scheme.lower() not in ("http", "https"):
        return []

    candidates = []

    def add(value):
        if value and value not in candidates:
            candidates.append(value)

    if parsed.scheme or raw.startswith("//") or raw.startswith("/"):
        add(urljoin(detail_url, raw))
    elif raw.startswith("."):
        add(urljoin(detail_url, raw))
        add(urljoin(BASE, raw.lstrip("./")))
    else:
        clean = raw.lstrip("./")
        if clean.startswith("bookregister/") or clean.startswith("book/"):
            add(urljoin(BASE + "modules/", clean))
        add(urljoin(BASE + "modules/bookregister/", clean))
        add(urljoin(BASE + "modules/book/", clean))
        add(urljoin(detail_url, raw))
        add(urljoin(BASE, raw))
    return candidates


def _pick_attachment_url(sess, candidates: list[str], *, timeout: int = 20) -> str | None:
    for candidate in candidates:
        response = None
        try:
            response = sess.get(
                candidate, timeout=timeout, stream=True, allow_redirects=True,
                headers={"Range": "bytes=0-1023"},
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status in (404, 410):
                continue
            content_type = str((getattr(response, "headers", {}) or {}).get("Content-Type", "")).lower()
            text = _response_text(response) if "html" in content_type else ""
            _raise_for_response(response, text, "ตรวจลิงก์ไฟล์แนบ")
            if text:
                soup = BeautifulSoup(text, "html.parser")
                if _is_login_page(soup):
                    raise SessionExpiredError("session หมดอายุระหว่างตรวจไฟล์แนบ")
                continue
            return getattr(response, "url", candidate) or candidate
        except SPPWebError:
            raise
        except Exception as e:
            raise SiteUnavailableError(f"ตรวจลิงก์ไฟล์แนบไม่สำเร็จ: {e}") from e
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
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
    prefix = bytearray()
    try:
        response = sess.get(url, timeout=120, stream=True, allow_redirects=True)
        _raise_for_response(response, "", "ดาวน์โหลดไฟล์แนบ")
        length = str((getattr(response, "headers", {}) or {}).get("Content-Length", "")).strip()
        if length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
            raise DownloadError(
                f"ไฟล์แนบใหญ่เกินกำหนด {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")

        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with open(part, "wb") as out:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise DownloadError(
                        f"ไฟล์แนบใหญ่เกินกำหนด {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
                if len(prefix) < 8192:
                    prefix.extend(chunk[:8192 - len(prefix)])
                out.write(chunk)

        if total == 0:
            raise DownloadError("ไฟล์แนบที่ดาวน์โหลดมาว่างเปล่า")
        probe_text = bytes(prefix).decode("utf-8", errors="ignore")
        if _is_cloudflare_block(response, probe_text):
            raise AccessBlockedError("Cloudflare ส่งหน้า Challenge กลับมาแทนไฟล์แนบ")
        probe_soup = BeautifulSoup(probe_text, "html.parser")
        if _is_login_page(probe_soup):
            raise SessionExpiredError("session หมดอายุระหว่างดาวน์โหลดไฟล์แนบ")
        if expected in ("pdf", ".pdf") and b"%PDF-" not in bytes(prefix[:1024]):
            raise DownloadError("เว็บส่งข้อมูลที่ไม่ใช่ PDF กลับมา (อาจเป็นหน้า login หรือ error)")
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
