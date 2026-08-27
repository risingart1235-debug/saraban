"""sppweb.py — ตัวคุยกับเว็บ สพป.สกลนคร เขต ๑ (ล็อกอิน + ดึงรายการหนังสือ)

ใช้ requests ล้วน ไม่ต้องพึ่ง Selenium/Chrome อีกแล้ว
(ตอนแรกใช้ Chrome เพราะคิดว่าล็อกอินต้องใช้ JS แต่จริงๆ เป็นฟอร์ม POST ธรรมดา
 แค่ต้องส่งชื่อปุ่ม login_submit ไปด้วย ฝั่ง PHP ถึงจะถือว่ากดปุ่มจริง)

ข้อดีของการตัด Chrome ทิ้ง:
  - รันบนเซิร์ฟเวอร์/Docker ได้ ไม่ต้องลง Chrome + ChromeDriver
  - เร็วขึ้นมาก (เดิมรอ Chrome เปิด ~30 วินาที)
  - กินแรมน้อยลงมาก เหมาะกับ hosting ฟรี
"""
import re
import os
import time

import core
from core import requests, BeautifulSoup, check_link, to_thai_digits

BASE = "https://office.sakonarea1.go.th/"
NEWS_URL = core.NEWS_URL
DETAIL_URL = BASE + "modules/book/main/bookdetail_school_total.php?b_id={}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# เว็บนี้ตอบ 403 ถ้า User-Agent เป็นค่าเริ่มต้นของ requests จึงต้องปลอมเป็นเบราว์เซอร์
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "th,en;q=0.9",
    "Referer": BASE,
}

FILE_RE = re.compile(r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|jpg|jpeg|png)$", re.IGNORECASE)


class LoginError(Exception):
    """ล็อกอินไม่สำเร็จ (รหัสผิด / เว็บล่ม / เว็บเปลี่ยนหน้าตา)"""


def new_session(cookie: str = None) -> requests.Session:
    """สร้าง Session — ถ้ามี PHPSESSID อยู่แล้วก็ใส่มาใช้ต่อได้เลย ไม่ต้องล็อกอินซ้ำ"""
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    if cookie:
        s.cookies.set("PHPSESSID", cookie, domain="office.sakonarea1.go.th")
    return s


def is_logged_in(sess: requests.Session) -> bool:
    """เช็คว่า session ยังใช้ได้อยู่ไหม — ดูว่าหน้ารายการยังโผล่ช่องรหัสผ่านหรือเปล่า"""
    try:
        r = sess.get(NEWS_URL, timeout=20)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        return soup.find("input", {"type": "password"}) is None
    except Exception:
        return False


def login(user: str = None, pwd: str = None) -> requests.Session:
    """ล็อกอินเว็บ สพป. คืน Session ที่พร้อมใช้ (ไม่ส่ง user/pwd = ใช้ค่าจาก config)"""
    if user is None or pwd is None:
        cfg = core.load_config()
        user = user or cfg.get("login_user", "").strip()
        pwd = pwd or cfg.get("login_pass", "").strip()
    if not user or not pwd:
        raise LoginError("ยังไม่ได้ตั้งชื่อผู้ใช้/รหัสผ่านของเว็บ สพป. (ตั้งได้ที่หน้าตั้งค่า)")

    sess = new_session()
    try:
        r = sess.get(BASE, timeout=20)
        r.encoding = "utf-8"
    except Exception as e:
        raise LoginError(f"เปิดเว็บ สพป. ไม่ได้: {e}") from e

    form = BeautifulSoup(r.text, "html.parser").find("form")
    if form is None:
        raise LoginError("หาฟอร์มล็อกอินไม่เจอ (เว็บอาจเปลี่ยนหน้าตา)")

    # เก็บทุกช่องในฟอร์มรวมช่องซ่อน (user_os, p) แล้วทับด้วยรหัสของเรา
    data = {x.get("name"): (x.get("value") or "")
            for x in form.find_all("input") if x.get("name")}
    for b in form.find_all("button"):
        if b.get("name"):
            data[b["name"]] = b.get("value") or ""   # ★ login_submit ขาดไม่ได้
    data["username"] = user
    data["pass"] = pwd

    action = form.get("action") or "index.php"
    try:
        sess.post(BASE + action.lstrip("./"), data=data, timeout=20)
    except Exception as e:
        raise LoginError(f"ส่งฟอร์มล็อกอินไม่สำเร็จ: {e}") from e

    if not is_logged_in(sess):
        raise LoginError("ล็อกอินไม่ผ่าน — ตรวจสอบชื่อผู้ใช้และรหัสผ่านอีกครั้ง")
    return sess


def get_cookie(sess: requests.Session) -> str:
    return sess.cookies.get("PHPSESSID", "")


# ==========================================================
# ดึงรายการหนังสือ
# ==========================================================
def find_last_page(sess: requests.Session) -> int:
    """หาว่าหน้าสุดท้ายคือหน้าไหน

    เว็บไม่ได้บอกตรงๆ จึงดูเลขหน้าที่มีในลิงก์ แล้วลองเปิดหน้าถัดไปอีกหนึ่ง
    ถ้ายังมีรายการอยู่แสดงว่าหน้าจริงมีมากกว่าที่ลิงก์บอก
    """
    r = sess.get(NEWS_URL, timeout=20)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    pages = [1]
    for a in soup.find_all("a", href=re.compile(r"page=(\d+)")):
        m = re.search(r"page=(\d+)", a["href"])
        if m:
            pages.append(int(m.group(1)))
    last = max(pages)

    probe = sess.get(f"{NEWS_URL}&page={last + 1}", timeout=20)
    probe.encoding = "utf-8"
    if _doc_links(BeautifulSoup(probe.text, "html.parser")):
        return last + 1
    return last


def _doc_links(soup):
    return soup.find_all("a", onclick=lambda v: v and "bookdetail" in v)


THAI_MON = {"มค":1, "กพ":2, "มีค":3, "เมย":4, "พค":5, "มิย":6,
            "กค":7, "สค":8, "กย":9, "ตค":10, "พย":11, "ธค":12}


def parse_sent_at(text: str):
    """แปลง 'วันเวลาที่ส่ง' ของเว็บ เช่น '27 สค 2569 09:19:09 น.'

    คืน (คีย์สำหรับเรียง 'YYYY-MM-DD', วันที่ไทยไว้แสดง, เวลา)
    ใช้คีย์แบบ ค.ศ. เพื่อให้เรียงและจัดกลุ่มตามวันได้ตรง
    """
    t = (text or "").replace("น.", "").strip()
    m = re.match(r"(\d+)\s+([ก-ฮ.]+)\s+(\d+)(?:\s+(\d+):(\d+))?", t)
    if not m:
        return "", (text or "").strip(), ""
    day, mon_raw, year = int(m.group(1)), m.group(2).replace(".", ""), int(m.group(3))
    mon = THAI_MON.get(mon_raw, 0)
    if not mon:
        return "", t, ""
    ce = year - 543 if year > 2400 else year
    hh, mm = m.group(4), m.group(5)
    return (f"{ce:04d}-{mon:02d}-{day:02d}",
            to_thai_digits(f"{day} {core.THAI_MONTHS_ABBR[mon-1]} {year}"),
            to_thai_digits(f"{hh}:{mm}") if hh else "")


def _row_of(link) -> dict:
    """อ่านข้อมูลหนังสือจากแถวตาราง

    คอลัมน์ของเว็บ: 0=ID 1=เลขหนังสือ 2=เรื่อง 3=รายละเอียด
                    4=ลงวันที่ 5=จาก 6=วันเวลาที่ส่ง
    """
    tr = link.find_parent("tr")
    cols = tr.find_all("td") if tr else []
    def col(i, default="-"):
        return cols[i].text.strip() if len(cols) > i else default
    key, shown, hhmm = parse_sent_at(col(6, ""))
    return {
        "doc_no": col(1),
        "doc_title": col(2),
        "doc_date": col(4),
        "sender": (col(5, "สพป.สกลนคร เขต 1").split("[")[0].strip()
                   or "สพป.สกลนคร เขต 1"),
        "sent_key": key,        # 'YYYY-MM-DD' ไว้จัดกลุ่ม/เรียง
        "sent_date": shown,     # วันที่ไทยไว้แสดง
        "sent_time": hhmm,
    }


def list_documents(sess: requests.Session, pages: int = 2) -> list:
    """คืนรายการหนังสือจากหน้าท้ายๆ (ใหม่สุดอยู่หน้าท้าย) เรียงใหม่สุดขึ้นก่อน"""
    last = find_last_page(sess)
    want = [p for p in range(max(1, last - pages + 1), last + 1)]
    out, seen = [], set()
    for p in want:
        r = sess.get(f"{NEWS_URL}&page={p}", timeout=20)
        r.encoding = "utf-8"
        for link in _doc_links(BeautifulSoup(r.text, "html.parser")):
            m = re.search(r"b_id=(\d+)", link.get("onclick", ""))
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            out.append({"book_id": m.group(1), "page": p, **_row_of(link)})
    out.reverse()             # ใหม่สุดขึ้นก่อน
    return out


def load_history() -> set:
    """เลขหนังสือที่เคยลงรับ/ข้ามไปแล้ว — อ่านจากที่เก็บกลาง ทุกเครื่องเห็นตรงกัน"""
    import store
    return store.get_store().history_ids()


def mark_done(book_id: str):
    import store
    store.get_store().add_history(book_id)


def unmark(book_id: str) -> bool:
    """ถอนออกจากประวัติ เพื่อให้เรื่องนี้กลับมาขึ้นในรายการหนังสือใหม่อีกครั้ง"""
    import store
    return store.get_store().remove_history(book_id)


def list_new_documents(sess: requests.Session, pages: int = 2) -> list:
    done = load_history()
    return [d for d in list_documents(sess, pages) if d["book_id"] not in done]


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


def fetch_detail(sess: requests.Session, book_id: str) -> dict:
    """เปิดหน้ารายละเอียด คืนความเร่งด่วนกับรายการไฟล์แนบ"""
    r = sess.get(DETAIL_URL.format(book_id), timeout=30)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    attachments, main_pdf = [], ""
    seen = set()
    for tag in soup.find_all("a", href=FILE_RE):
        raw = tag["href"].lstrip("./").lstrip("/")
        # ไฟล์อยู่ได้สองที่ ลองที่แรกก่อน ไม่เจอค่อยใช้ที่สอง
        opt1 = (BASE + "modules/bookregister/" + raw).replace("bookregister/bookregister/", "bookregister/")
        opt2 = (BASE + "modules/book/" + raw).replace("book/book/", "book/")
        url = opt1 if check_link(opt1, dict(sess.headers)) else opt2
        if url in seen:
            continue
        seen.add(url)
        name = re.sub(r"^\d+\.\s*", "", tag.text.strip()) or "เอกสารแนบ"
        attachments.append({"name": name, "url": url})
        if not main_pdf and url.lower().endswith(".pdf"):
            main_pdf = url
    return {
        "book_id": book_id,
        "emoji": _urgency_emoji(soup.text),
        "attachments": attachments,
        "main_pdf": main_pdf,
    }


def download(sess: requests.Session, url: str, dest: str) -> str:
    """โหลดไฟล์แนบมาเก็บไว้ที่ dest"""
    r = sess.get(url, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)
    return dest


def attach_text(attachments: list) -> str:
    """ข้อความรายการไฟล์แนบสำหรับส่งเข้า LINE"""
    if not attachments:
        return f"📥 โหลดไฟล์: {NEWS_URL}"
    return "\n".join(f"📥 {to_thai_digits(i)}. {a['name']}\n👉 {a['url']}"
                     for i, a in enumerate(attachments, 1))
