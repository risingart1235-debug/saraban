"""core.py — สมองของระบบลงรับหนังสือ (ไม่ผูกกับหน้าจอ)

ใช้ร่วมกันทั้งเวอร์ชันเดสก์ท็อป (bot3.py) และเวอร์ชันเว็บ (web/main.py)
ในไฟล์นี้ต้องไม่มี tkinter และต้องไม่มีอะไรที่ใช้ได้เฉพาะ Windows
เพื่อให้รันบนเซิร์ฟเวอร์ Linux / Docker ได้ด้วย
"""
import os
import re
import json
import time
import threading
import importlib
from datetime import datetime, timedelta, timezone

# ==========================================
# เวลาราชการไทยเสมอ ไม่ว่าเครื่องที่รันจะตั้งโซนเวลาอะไรไว้
# ==========================================
# hosting ต่างประเทศ (Render) ตั้งเครื่องเป็น UTC ถ้าอ่านเวลาเครื่องตรงๆ
# ตรายางเลขรับจะพิมพ์เวลาผิดไป ๗ ชั่วโมง และถ้าลงรับก่อน ๐๗:๐๐ น.
# "วันที่" จะเพี้ยนไปหนึ่งวันด้วย — เป็นเอกสารราชการที่แก้ทีหลังยาก
# รวมถึงชื่อโฟลเดอร์รายวันที่เก็บไฟล์ก็จะแยกคนละวันกับเครื่องที่โรงเรียน
#
# ไทยไม่มี DST และใช้ UTC+7 มาตลอด จึงตรึงค่าไว้ได้เลย
# ไม่ต้องพึ่งฐานข้อมูลโซนเวลาของเครื่อง (Windows ไม่มีมาให้ ต้องลง tzdata เพิ่ม)
THAI_TZ = timezone(timedelta(hours=7))


def now_th():
    """เวลาปัจจุบันตามเวลาไทย — ใช้แทน datetime.now() ทุกที่ที่ผู้ใช้เห็นผล"""
    return datetime.now(THAI_TZ)

# ==========================================
# ๐. โหลดไลบรารีหนักแบบ "ใช้เมื่อไหร่ค่อยโหลด" (Lazy Import)
# ==========================================
# ไลบรารีชุดนี้รวมกันกินเวลาเปิดโปรแกรมประมาณ ๖ วินาที แต่ไม่มีตัวไหนจำเป็น
# ตอนวาดหน้าต่างเลย จึงเลื่อนไปโหลดจริงตอนถูกเรียกใช้ครั้งแรก
# (และมี _preload_heavy_libs() แอบโหลดไว้เบื้องหลังให้อีกชั้น เวลาใช้จริงจะไม่หน่วง)

class _LazyModule:
    """ตัวแทนโมดูล — แตะใช้ครั้งแรกเมื่อไหร่ ค่อย import จริงตอนนั้น"""
    def __init__(self, name):
        self._name = name
        self._mod = None
    def __getattr__(self, attr):
        if self._mod is None:
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)

class _LazyFunc:
    """ตัวแทนฟังก์ชัน/คลาสในโมดูลหนัก — เรียกครั้งแรกเมื่อไหร่ ค่อย import จริงตอนนั้น"""
    def __init__(self, module, attr):
        self._module = module
        self._attr = attr
        self._real = None
    def __call__(self, *args, **kwargs):
        if self._real is None:
            self._real = getattr(importlib.import_module(self._module), self._attr)
        return self._real(*args, **kwargs)

requests          = _LazyModule('requests')
genai             = _LazyModule('google.genai')
# PIL เป็นโค้ดเนทีฟ — iOS โหลดไม่ได้ ทำเป็น lazy เพื่อให้ sppweb (ที่ import core)
# รันบนมือถือได้ ส่วนเรนเดอร์ภาพไม่เคยถูกเรียกบนมือถือ จึงไม่โหลด PIL เลย
Image             = _LazyModule('PIL.Image')
ImageDraw         = _LazyModule('PIL.ImageDraw')
ImageFont         = _LazyModule('PIL.ImageFont')
BeautifulSoup     = _LazyFunc('bs4', 'BeautifulSoup')
PdfMerger         = _LazyFunc('PyPDF2', 'PdfMerger')
PdfReader         = _LazyFunc('PyPDF2', 'PdfReader')
Workbook          = _LazyFunc('openpyxl', 'Workbook')
load_workbook     = _LazyFunc('openpyxl', 'load_workbook')
word_tokenize     = _LazyFunc('thaiwords', 'tokenize')   # เบากว่า pythainlp ๑๐ เท่า

def _preload_heavy_libs():
    """แอบโหลดไลบรารีหนักไว้เบื้องหลังหลังหน้าต่างขึ้นแล้ว
    ผู้ใช้จะได้เห็นหน้าต่างทันที ส่วนตอนกดใช้งานจริงก็ไม่ต้องรอโหลด"""
    for name in ('requests', 'openpyxl', 'PyPDF2', 'pymupdf', 'bs4',
                 'thaiwords', 'google.genai'):
        try:
            importlib.import_module(name)
        except Exception:
            pass

# ==========================================
# ซ่อนหน้าต่าง Console สีดำ (เฉพาะ Windows) — ให้เห็นแต่หน้าต่างโปรแกรม
# ==========================================
def _hide_console():
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # 0 = SW_HIDE
    except Exception:
        pass
# (_hide_console ใช้เฉพาะเวอร์ชันเดสก์ท็อป — bot3.py เรียกเอง)

# ==========================================
# ๑. ส่วนตั้งค่า (Configurations)
# ==========================================
NEWS_URL = 'https://office.sakonarea1.go.th/index.php?option=book&task=main/receive_mobile&saraban_index=19'
PDF_RENDER_DPI = 200

# --- ค่าเริ่มต้นภายในโปรแกรม (ค่าสำรอง / fallback) ---
# หมายเหตุ: ค่าจริงทั้งหมดย้ายไปเก็บที่ไฟล์ config.json แล้ว
# ค่าด้านล่างนี้เว้นว่างไว้ (ใช้เป็นค่าสำรองเฉพาะกรณี config.json หาย/ว่าง)
DEFAULT_LINE_ACCESS_TOKEN = ''
DEFAULT_LINE_GROUP_ID = ''
DEFAULT_IMGBB_API_KEY = ''
DEFAULT_GEMINI_API_KEY = ''

# --- ค่าที่ใช้งานจริง (โหลดจาก config.json ผ่าน apply_config) ---
LINE_ACCESS_TOKEN = DEFAULT_LINE_ACCESS_TOKEN
LINE_GROUP_ID = DEFAULT_LINE_GROUP_ID
IMGBB_API_KEY = DEFAULT_IMGBB_API_KEY
GEMINI_API_KEY = DEFAULT_GEMINI_API_KEY

# ที่อยู่ไฟล์เซฟ API Key (อยู่โฟลเดอร์เดียวกับสคริปต์)
try:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _BASE_DIR = os.getcwd()

def _p(name):
    """เติมโฟลเดอร์ของสคริปต์ให้ชื่อไฟล์ — ทุกไฟล์ที่โปรแกรมอ่าน/เขียนต้องผ่านตัวนี้
    ถ้าใช้ชื่อไฟล์เปล่าๆ Python จะไปหาที่ 'โฟลเดอร์ปัจจุบัน' (cwd) ซึ่งเวลาเปิดโปรแกรม
    จากช็อตคัตหน้าจอ/เมนู Start ที่ตั้ง 'Start in' เป็นโฟลเดอร์อื่น จะหาไฟล์ไม่เจอ
    (เช่น ฟอนต์ไทยหาย → ตรายางกลายเป็นสี่เหลี่ยม, ไฟล์ชั่วคราวไปโผล่ผิดที่)"""
    return os.path.join(_BASE_DIR, name)

CONFIG_FILE = _p("config.json")

# โฟลเดอร์สำหรับไฟล์ที่โปรแกรมเขียนตอนทำงาน (log, ไฟล์ชั่วคราว, ตำแหน่งตรายาง)
# บนเครื่องนี้ = โฟลเดอร์โปรแกรม เหมือนเดิม
# บน hosting โฟลเดอร์โปรแกรมมักเขียนไม่ได้ ให้ตั้ง SARABAN_WORK=/tmp หรือ /data
WORK_DIR = os.environ.get("SARABAN_WORK") or _BASE_DIR
try:
    os.makedirs(WORK_DIR, exist_ok=True)
except Exception:
    WORK_DIR = _BASE_DIR

def _w(name):
    """ที่อยู่ไฟล์ที่ต้องเขียนได้จริง"""
    return os.path.join(WORK_DIR, name)

# ที่เก็บไฟล์งาน — ตั้งผ่านตัวแปรระบบ SARABAN_OUTPUT ได้
# เครื่องที่ทำงานอยู่ใช้ C:\แฟ้มเสนอ_ผอ เหมือนเดิม ส่วนเซิร์ฟเวอร์ Linux ตั้งเป็นที่อื่น
OUTPUT_ROOT = os.environ.get("SARABAN_OUTPUT") or (
    r"C:\แฟ้มเสนอ_ผอ" if os.name == "nt" else os.path.join(_BASE_DIR, "แฟ้มเสนอ_ผอ")
)
REGISTRY_XLSX = os.path.join(OUTPUT_ROOT, "ทะเบียนหนังสือรับ.xlsx")

def load_config():
    """อ่านค่า API Key ที่เซฟไว้ คืนค่าเป็น dict (ค่าเริ่มต้นว่างทุกช่อง)"""
    cfg = {'line_access_token': '', 'line_group_id': '', 'imgbb_api_key': '', 'gemini_api_key': '',
           'login_user': '', 'login_pass': '',
           'drive_url': '',   # ลิงก์โฟลเดอร์ Google Drive ที่แบ็กอัพไฟล์ไว้
           'store_mode': '',  # ว่าง/local = เก็บเป็นไฟล์ในเครื่อง | sheets = Google Sheets
           'sheet_id': '',    # รหัสสเปรดชีต (เอามาจาก URL)
           'sa_file': '',     # ที่อยู่ไฟล์กุญแจ service account (.json)
           'drive_upload': '', # on = อัป PDF ขึ้น Drive ผ่าน API (จำเป็นเมื่อรันบน hosting)
           'drive_folder_id': ''}  # โฟลเดอร์ปลายทางบน Drive
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: (data.get(k) or '') for k in cfg})
        except Exception:
            pass

    # ตัวแปรระบบมาก่อนไฟล์เสมอ — บนเซิร์ฟเวอร์จะไม่มี config.json
    # (ไฟล์นั้นมีรหัสผ่านจริง จึงถูกกันไม่ให้ขึ้น GitHub)
    # ความลับบน hosting จึงต้องส่งผ่านตัวแปรระบบแทน เช่น SARABAN_GEMINI_API_KEY
    for k in cfg:
        v = os.environ.get('SARABAN_' + k.upper(), '').strip()
        if v:
            cfg[k] = v
    return cfg

def save_config(cfg):
    """เซฟค่า API Key ลงไฟล์ config.json

    บน hosting ค่าพวกนี้มาจากตัวแปรระบบและมักเขียนไฟล์ไม่ได้
    จึงโยน error ที่อ่านรู้เรื่องแทนที่จะพังแบบงงๆ
    """
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise RuntimeError(
            "บันทึกการตั้งค่าลงไฟล์ไม่ได้ (บนเซิร์ฟเวอร์ให้ตั้งผ่านตัวแปรระบบแทน): "
            f"{e}") from e

# --- ตำแหน่งตรายางบนกระดาษเปล่า (โหมดที่ ๓) ---
# เก็บแยกไฟล์ เพราะ load_config จะกรอง key ที่ไม่รู้จักทิ้ง
# เก็บเป็น "เซนติเมตรจากขอบกระดาษ" ไม่ใช่พิกเซล จะได้ไม่เพี้ยนถ้าเปลี่ยน DPI ทีหลัง
STAMP_POS_FILE = _w("stamp_pos.json")
STAMP_DEFAULT_RIGHT_CM = 1.0   # ตรายางห่างขอบขวา
STAMP_DEFAULT_TOP_CM = 1.5     # ตรายางห่างขอบบน (เผื่อขอบที่เครื่องปริ้นปริ้นไม่ถึง)

def load_stamp_pos():
    """ตำแหน่ง/ขนาดตรายางที่ใช้ครั้งล่าสุด คืน (ห่างซ้าย ซม., ห่างบน ซม., ขนาด %) หรือ None"""
    try:
        with open(STAMP_POS_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return float(d['left_cm']), float(d['top_cm']), int(d['size_pct'])
    except Exception:
        return None

def save_stamp_pos(left_cm, top_cm, size_pct):
    """จำตำแหน่งไว้ ครั้งหน้าเปิดมาจะอยู่ที่เดิมเลย ไม่ต้องลากใหม่ทุกครั้ง"""
    try:
        with open(STAMP_POS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'left_cm': round(left_cm, 2), 'top_cm': round(top_cm, 2),
                       'size_pct': int(size_pct)}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def apply_config():
    """โหลด config มาใช้งานจริง ช่องไหนว่างให้ใช้ค่าเริ่มต้นภายในโปรแกรมแทน"""
    global LINE_ACCESS_TOKEN, LINE_GROUP_ID, IMGBB_API_KEY, GEMINI_API_KEY, client
    cfg = load_config()
    LINE_ACCESS_TOKEN = cfg.get('line_access_token') or DEFAULT_LINE_ACCESS_TOKEN
    LINE_GROUP_ID = cfg.get('line_group_id') or DEFAULT_LINE_GROUP_ID
    IMGBB_API_KEY = cfg.get('imgbb_api_key') or DEFAULT_IMGBB_API_KEY
    GEMINI_API_KEY = cfg.get('gemini_api_key') or DEFAULT_GEMINI_API_KEY
    client = None   # ล้างของเดิม เดี๋ยวสร้างใหม่ตอนเรียก AI ครั้งถัดไป (ดู get_ai_client)
    return cfg

client = None

def get_ai_client():
    """สร้าง Gemini client ตอนจะใช้จริงครั้งแรกเท่านั้น
    (เดิมสร้างตอนเปิดโปรแกรม ทำให้ต้องรอโหลด google.genai ~๒ วินาทีทุกครั้ง)"""
    global client
    if client is None and GEMINI_API_KEY:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception:
            client = None
    return client

# โหลดค่าจาก config.json มาใช้ทันทีตอนเปิดโปรแกรม (ยังไม่สร้าง AI client)
apply_config()

# ==========================================
# ๒. ฟังก์ชันสนับสนุนพื้นฐาน (Helper Functions)
# ==========================================
def to_thai_digits(text):
    if text is None: return ""
    thai_map = str.maketrans('0123456789', '๐๑๒๓๔๕๖๗๘๙')
    return str(text).translate(thai_map)

def to_arabic_digits(text):
    if text is None: return ""
    arabic_map = str.maketrans('๐๑๒๓๔๕๖๗๘๙', '0123456789')
    return str(text).translate(arabic_map)

def check_link(url, headers):
    try:
        r = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
        return r.status_code == 200
    except: return False

def format_scraped_date(raw_date):
    if not raw_date or raw_date == "-": return "-"
    clean_str = raw_date.replace('/', ' ').replace('.', '')
    parts = clean_str.split()
    if len(parts) >= 3:
        day, month_raw, year = parts[0], parts[1], parts[2]
        month_map = {"มค": "ม.ค.", "กพ": "ก.พ.", "มีค": "มี.ค.", "เมย": "เม.ย.", "พค": "พ.ค.", "มิย": "มิ.ย.", "กค": "ก.ค.", "สค": "ส.ค.", "กย": "ก.ย.", "ตค": "ต.ค.", "พย": "พ.ย.", "ธค": "ธ.ค."}
        month_std = month_map.get(month_raw, month_raw)
        return f"{to_thai_digits(day)} {month_std} {to_thai_digits(year)}"
    return to_thai_digits(raw_date) 

THAI_MONTHS_ABBR = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
THAI_MONTHS_FULL = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
                    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]


def day_folder(when=None) -> str:
    """ที่เก็บไฟล์ของวันนั้น เป็น path ย่อยใต้ OUTPUT_ROOT — เช่น "๒๕๖๙/๐๘ สิงหาคม/๒๘"

    เดิมทุกวันเป็นโฟลเดอร์เดียวกองรวมกันที่ชั้นบนสุด ปีหนึ่งได้ ๒๐๐ กว่าโฟลเดอร์
    เลื่อนหาลำบากมากบน Google Drive จึงซอยเป็น ปี/เดือน/วัน
    ชั้นบนสุดจะเหลือแค่ปีละ ๑ โฟลเดอร์

    ใช้เลขไทยให้เข้ากับที่อื่นในระบบ และเรียงลำดับได้ถูกต้อง
    เพราะรหัสอักขระเลขไทย ๐-๙ เรียงติดกันเหมือนเลขอารบิก
    (เติมศูนย์หน้าเดือน/วันด้วย ไม่งั้น "๑๐" จะมาก่อน "๙")

    คั่นด้วย "/" เสมอ (ไม่ใช่ os.sep) เพราะค่านี้ถูกเอาไปต่อเป็น URL ดาวน์โหลดด้วย
    ส่วนการอ่าน/เขียนไฟล์ Python รับ "/" ได้ทั้งบน Windows และ Linux
    """
    now = when or now_th()
    return "/".join((
        to_thai_digits(str(now.year + 543)),
        to_thai_digits(f"{now.month:02d}") + " " + THAI_MONTHS_FULL[now.month - 1],
        to_thai_digits(f"{now.day:02d}"),
    ))

def get_thai_date():
    now = now_th()
    thai_year = now.year + 543
    month_name = THAI_MONTHS_ABBR[now.month - 1]
    date_str = f"{now.day} {month_name} {thai_year}"
    return to_thai_digits(date_str)

def normalize_typed_date(raw):
    """แปลงวันที่ที่ผู้ใช้พิมพ์เองให้เป็นวันที่ไทยมาตรฐาน
    รองรับ '๑๕ ก.ค. ๒๕๖๙' | '15 ก.ค. 2569' | '15/7/2569' | '15-7-2026' | '๑๕/๗/๒๕๖๙'
    และ '2026-08-31' (รูปแบบที่ช่องปฏิทินของเบราว์เซอร์ส่งมา — ปีมาก่อน)"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if any(m in raw for m in THAI_MONTHS_ABBR):
        return to_thai_digits(raw)              # พิมพ์เป็นวันที่ไทยมาแล้ว ใส่ตรงๆ
    parts = [p for p in re.split(r"[/\-.\s]+", to_arabic_digits(raw)) if p]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        # ช่องปฏิทิน (input type=date) ส่งมาเป็น ปี-เดือน-วัน ต้องสลับก่อน
        # ไม่งั้น '2026-08-31' จะถูกอ่านเป็นวันที่ ๒๐๒๖ แล้วแปลงไม่ออก
        if len(parts[0]) == 4 and int(parts[0]) > 31:
            parts = [parts[2], parts[1], parts[0]]
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        if 1 <= m <= 12 and 1 <= d <= 31:
            if y < 100: y += 2500               # พิมพ์ปีสองหลัก เช่น 69 → ๒๕๖๙
            elif y < 2400: y += 543             # พิมพ์เป็น ค.ศ. → แปลงเป็น พ.ศ.
            try:
                datetime(y - 543, m, d)         # มีวันนี้จริงไหม (กัน ๓๐ ก.พ.)
            except ValueError:
                return to_thai_digits(raw)      # ไม่มีจริง — คืนตามที่พิมพ์ ไม่เดาให้
            return to_thai_digits(f"{d} {THAI_MONTHS_ABBR[m-1]} {y}")
        # ตัวเลขไม่สมเหตุผล (เดือน ๑๓, วันที่ ๐/๓๒) — คืนตามที่พิมพ์มา
        # แปลงเลขเป็นไทยให้หมด จะได้ไม่ปนไทย/อารบิก อย่างที่ตัวแปลงเดิมทำ
        return to_thai_digits(raw)
    return format_scraped_date(raw)             # รูปแบบอื่น (เดือนเป็นตัวหนังสือ) ให้ตัวเดิมจัดการ

def get_thai_time_rounded():
    """เวลาลงรับแบบปัดเข้าครึ่งชั่วโมงที่ใกล้ที่สุด (ลงท้ายด้วย ๐๐ หรือ ๓๐ เท่านั้น)
    เช่น ๐๙:๑๔ → ๐๙:๐๐ | ๐๙:๒๐ → ๐๙:๓๐ | ๐๙:๕๐ → ๑๐:๐๐"""
    now = now_th()
    slot = (((now.hour * 60 + now.minute) + 15) // 30) * 30
    slot %= 24 * 60          # ถ้าเลย ๒๓:๔๕ แล้วปัดขึ้น ให้วนกลับเป็น ๐๐:๐๐
    return to_thai_digits(f"{slot // 60:02d}:{slot % 60:02d}")

def append_excel_registry(receipt_no_thai, doc_no, doc_date, sender, doc_title):
    """(ของเดิม) เขียนแถวทะเบียนโดยระบุเลขรับมาเอง — ใช้ตอนจองเลขไว้ก่อนแล้ว"""
    return _store().register_with_no(receipt_no_thai, doc_no, doc_date, sender, doc_title)


def append_excel_receipt_only(receipt_no_thai, doc_no="", doc_date="", sender="", doc_title="", receive_date=""):
    """(ของเดิม) แบบลงเฉพาะเลขรับ — ช่องที่ไม่ได้กรอกเว้นว่างไว้"""
    return _store().register_with_no(receipt_no_thai, doc_no, doc_date, sender, doc_title, receive_date)


def _store():
    import store
    return store.get_store()


def get_next_receipt_no():
    """เลขรับถัดไป (ดูเฉยๆ ยังไม่จอง)"""
    return _store().peek_receipt_no()


def register_document(doc_no="", doc_date="", sender="", doc_title="", receive_date=""):
    """จองเลขรับ + เขียนแถวทะเบียน ในจังหวะเดียวแบบกันชนกัน คืนเลขรับที่ได้"""
    return _store().register(doc_no, doc_date, sender, doc_title, receive_date)


def reserve_receipt_once(state, register_fn=register_document, **fields):
    """จองเลขรับครั้งเดียวต่อหนึ่งงาน แล้วนำเลขเดิมกลับมาใช้เมื่อ retry.

    ``state`` ต้องเป็น dict ที่อยู่ตลอดอายุของงานนั้น การบันทึกเลขลง state
    เกิดทันทีหลังที่ store ยืนยันว่าลงทะเบียนสำเร็จ ดังนั้นความผิดพลาดในขั้น
    สร้าง PDF ภายหลังจะไม่ทำให้การลองใหม่ไปกินเลขรับถัดไปอีกเลขหนึ่ง
    """
    reserved = state.get("reserved_receipt_no")
    if reserved:
        return reserved
    reserved = register_fn(**fields)
    state["reserved_receipt_no"] = reserved
    return reserved


def upload_to_imgbb(image_path):
    try:
        with open(image_path, "rb") as img:
            res = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_API_KEY}, files={"image": img}, timeout=60)
            return res.json()['data']['url']
    except: return None

def send_line_with_image(text, image_url=None):
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {LINE_ACCESS_TOKEN}'}
    if image_url:
        payload = {'to': LINE_GROUP_ID, 'messages': [{'type': 'text', 'text': text}, {'type': 'image', 'originalContentUrl': image_url, 'previewImageUrl': image_url}]}
    else:
        payload = {'to': LINE_GROUP_ID, 'messages': [{'type': 'text', 'text': text}]}
    try:
        r = requests.post('https://api.line.me/v2/bot/message/push', headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"LINE ส่งไม่สำเร็จ {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as ex:
        print(f"LINE ส่งไม่สำเร็จ: {ex}")
        return False

# ==========================================
# ๓. ฟังก์ชัน AI
# ==========================================
def generate_kasien_text(pdf_path):
    try:
        ai = get_ai_client()
        sample_file = ai.files.upload(file=pdf_path)
        prompt = """
        อ่านหนังสือราชการฉบับนี้ แล้วทำหน้าที่เป็นผู้ช่วยธุรการโรงเรียน โดยต้องวิเคราะห์และสกัดข้อมูลดังนี้:
        
        บังคับให้ส่งคืนข้อความในรูปแบบ 9 บรรทัดเป๊ะๆ ดังนี้ (ไม่ต้องพิมพ์เว้นวรรคย่อหน้า และไม่ต้องมีคำอธิบายอื่นเพิ่ม):
        บรรทัดที่ 1: เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ
        บรรทัดที่ 2: [สรุปใจความสำคัญของเอกสาร กระชับ อ่านรู้เรื่อง บังคับให้จบภายใน 1 บรรทัดเท่านั้น ***หากมีตัวเลขในบรรทัดนี้ ให้ใช้เลขไทย (๑, ๒, ๓) เท่านั้น***]
        บรรทัดที่ 3: [เลือกข้อความใดข้อความหนึ่งจาก 3 ตัวเลือกนี้เท่านั้น: "เพื่อโปรดทราบ" หรือ "เพื่อพิจารณาสั่งการ" หรือ "เพื่อพิจารณาอนุมัติ"]
        บรรทัดที่ 4: [ระบุตัวเลขหน้าของเอกสารที่มีลายเซ็นผู้ส่งหรือผู้อำนวยการเขตพื้นที่ฯ ตอบแค่ตัวเลข เช่น 1 หรือ 2]
        บรรทัดที่ 5: [สกัดเลขที่หนังสือ (ที่) เช่น ศธ 04137/123 ถ้าหาไม่พบให้ตอบ -]
        บรรทัดที่ 6: [สกัดเรื่องของหนังสือ ถ้าหาไม่พบให้ตอบ -]
        บรรทัดที่ 7: [สกัดวันที่ลงในหนังสือ เช่น 15 มกราคม 2567 ถ้าหาไม่พบให้ตอบ -]
        บรรทัดที่ 8: [สกัดชื่อหน่วยงานผู้ส่ง/จากใคร เช่น สพป.สกลนคร เขต 1 ถ้าหาไม่พบให้ตอบ -]
        บรรทัดที่ 9: [สกัดบรรทัด "เรียน" ของหนังสือต้นฉบับว่าส่งถึงใคร — ตอบเฉพาะข้อความผู้รับ ไม่ต้องมีคำว่า "เรียน" เช่น "ผู้อำนวยการโรงเรียนในสังกัดทุกแห่ง" หรือ "ผู้อำนวยการโรงเรียนในสังกัด (โรงเรียนมาตรฐานสากล)" หรือ "ผู้อำนวยการโรงเรียนในโครงการพระราชดำริฯ ทั้ง 27 โรงเรียน" ถ้าหาไม่พบให้ตอบ -]
        """
        response = ai.models.generate_content(model='gemini-2.5-flash', contents=[sample_file, prompt])
        lines = [line.strip() for line in response.text.strip().split('\n') if line.strip()]
        
        sig_page = 1
        ai_no, ai_title, ai_date, ai_sender, ai_recipient = "-", "-", "-", "-", "-"
        if len(lines) >= 8:
            try: sig_page = int(re.search(r'\d+', lines[3]).group())
            except: pass

            def clean_ai_line(txt):
                return re.sub(r'^บรรทัดที่ \d+:\s*', '', txt).replace('[', '').replace(']', '').strip()

            ai_no = clean_ai_line(lines[4])
            ai_title = clean_ai_line(lines[5])
            ai_date = clean_ai_line(lines[6])
            ai_sender = clean_ai_line(lines[7])
            if len(lines) >= 9:
                ai_recipient = clean_ai_line(lines[8])

            text_out = "\n".join([clean_ai_line(l) for l in lines[:3]])
        else:
            text_out = "\n".join(lines[:3]) if len(lines) >= 3 else response.text.strip()

        for phrase in ["เพื่อโปรดทราบ", "เพื่อพิจารณาสั่งการ", "เพื่อพิจารณาอนุมัติ"]:
            if phrase in text_out:
                text_out = text_out.split(phrase)[0].strip() + "\n" + phrase
                break

        return to_thai_digits(text_out), sig_page, ai_no, ai_title, ai_date, ai_sender, ai_recipient
    except Exception as e:
        # บันทึก error เต็ม (traceback) ลงไฟล์ ai_error.log เพื่อตรวจสาเหตุได้ภายหลัง
        try:
            import traceback
            with open(_w("ai_error.log"), "a", encoding="utf-8") as logf:
                logf.write("\n[" + now_th().strftime("%Y-%m-%d %H:%M:%S") + "]\n")
                logf.write(traceback.format_exc())
        except Exception:
            pass
        short = str(e).replace("\n", " ")
        low = short.lower()
        if ("429" in short) or ("resource_exhausted" in low) or ("quota" in low) or ("exceeded" in low):
            reason = "โควต้า/วงเงินรายเดือนของ Gemini หมด — ตรวจสอบที่ Google AI Studio/Cloud Billing"
        elif ("api key" in low) or ("api_key" in low) or ("401" in short) or ("403" in short) or ("permission" in low) or ("unauthenticated" in low):
            reason = "API Key ไม่ถูกต้อง/ไม่มีสิทธิ์ — ตรวจสอบที่ ⚙️ ตั้งค่า"
        elif ("404" in short) or ("not found" in low) or ("model" in low):
            reason = "ไม่พบโมเดล — ตรวจสอบชื่อโมเดลในโค้ด"
        else:
            reason = short[:90]
        return (f"เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ\n(AI ผิดพลาด: {reason})\nเพื่อโปรดทราบ",
                1, "-", "-", "-", "-", "-")

# =====================================================================
# ๓.๑ ตรวจบรรทัด "เรียน" ในเครื่อง (ไม่ใช้ AI) เพื่อคัดกรองก่อนลงรับ
# =====================================================================
def extract_recipient_line(pdf_path):
    """ดึงบรรทัด 'เรียน...' จากหน้าแรกของ PDF ด้วย text layer (ไม่เรียก AI)
    คืนค่าเป็นข้อความบรรทัดเรียน หรือ None ถ้าอ่านไม่ได้ (เช่น เป็นรูปสแกน)"""
    try:
        reader = PdfReader(pdf_path)
        text = reader.pages[0].extract_text() or ""
    except Exception:
        return None
    if not text.strip():
        return None
    # หาบรรทัดที่ขึ้นต้นด้วย "เรียน"
    for line in text.split('\n'):
        s = line.strip()
        if s.startswith("เรียน"):
            return s
    # เผื่อ extract_text รวมเป็นบรรทัดเดียว ใช้ regex ดึงช่วงหลัง "เรียน"
    m = re.search(r'เรียน[ \t]*([^\n]+)', text)
    if m:
        return "เรียน " + m.group(1).strip()
    return None

# คำที่บ่งบอกว่าเป็น "กลุ่มโรงเรียนเฉพาะ" แม้จะมีคำว่า ในสังกัด/ทุกแห่ง ก็ต้องตรวจก่อน
# (เพิ่ม/แก้รายการนี้ได้ตามที่เจอบ่อย)
SPECIFIC_GROUP_KEYWORDS = (
    "ขยายโอกาส",        # โรงเรียนขยายโอกาสทางการศึกษา
    "มาตรฐานสากล",      # โรงเรียนมาตรฐานสากล
    "คุณภาพ",           # โรงเรียนคุณภาพ
    "พระราชดำริ",       # โครงการพระราชดำริ
    "โครงการ",          # ในโครงการ...
    "ขนาดเล็ก",         # โรงเรียนขนาดเล็ก
    "ขนาดใหญ่",
    "อนุบาลประจำ",
)

def classify_recipient(recipient_line):
    """จัดประเภทผู้รับจากบรรทัด 'เรียน'
       'auto'    = เรียนถึงทุกหน่วยในสังกัด → ลงรับอัตโนมัติ
       'check'   = มีรายชื่อแนบท้าย/ผู้รับเฉพาะ/กลุ่มเฉพาะ/อื่นๆ → ต้องให้ผู้ใช้ตรวจ
       'unknown' = อ่านบรรทัดเรียนไม่ได้ (อาจเป็นรูปสแกน) → ต้องให้ผู้ใช้ตรวจ"""
    if not recipient_line:
        return 'unknown'
    txt = recipient_line.replace(" ", "")
    # ๑) มี "รายชื่อ...แนบท้าย" = เฉพาะบางหน่วย → ต้องตรวจ (เช็คก่อนเสมอ)
    if ("แนบท้าย" in txt) or ("รายชื่อ" in txt and "แนบ" in txt):
        return 'check'
    # ๒) มีวงเล็บกำกับกลุ่มผู้รับ เช่น (โรงเรียนมาตรฐานสากล), (โรงเรียนคุณภาพ)
    #    = เจาะจงเฉพาะบางกลุ่ม → ต้องตรวจ   (ยกเว้นวงเล็บที่บอกว่า "ทุก..." = ทุกแห่ง)
    m = re.search(r'\(([^)]*)\)', txt)
    if m and ("ทุก" not in m.group(1)):
        return 'check'
    # ๓) มีคำบ่งชี้ "กลุ่มโรงเรียนเฉพาะ" เช่น ขยายโอกาส/มาตรฐานสากล/คุณภาพ → ต้องตรวจ
    #    แม้จะลงท้ายด้วย "ในสังกัดทุกแห่ง" ก็ตาม
    if any(k in txt for k in SPECIFIC_GROUP_KEYWORDS):
        return 'check'
    # ๔) เรียนถึงทุกหน่วยในสังกัด → ลงรับอัตโนมัติ
    #    รองรับหลายสำนวน เช่น "ในสังกัด", "ในสังกัดทุกแห่ง", "ทุกโรงเรียนในสังกัด", "ทุกแห่ง"
    if ("ในสังกัด" in txt) or ("ทุกโรงเรียน" in txt) or ("ทุกแห่ง" in txt):
        return 'auto'
    return 'check'


def render_pdf_page(pdf_path, page_number=1, dpi=PDF_RENDER_DPI):
    """เรนเดอร์หน้า PDF เป็น PIL Image ด้วย PyMuPDF โดยไม่ต้องติดตั้ง Poppler.

    ``page_number`` เริ่มนับจาก 1 ให้ตรงกับเลขหน้าที่ผู้ใช้และ AI เห็น
    ส่วน ``dpi`` ระบุชัดเจนเพื่อรักษาขนาดกระดาษเมื่อบันทึกกลับเป็น PDF
    """
    if page_number < 1:
        raise ValueError("page_number must start at 1")
    if dpi <= 0:
        raise ValueError("dpi must be greater than zero")

    import pymupdf

    document = pymupdf.open(pdf_path)
    try:
        if page_number > document.page_count:
            raise IndexError(
                f"PDF has {document.page_count} page(s); page {page_number} was requested")
        page = document.load_page(page_number - 1)
        pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    finally:
        document.close()


def save_image_as_pdf(pil_image, output_path, dpi=PDF_RENDER_DPI):
    """บันทึกภาพเป็น PDF โดยรักษาสเกลพิกเซลตาม DPI ที่ระบุ."""
    if dpi <= 0:
        raise ValueError("dpi must be greater than zero")
    pil_image.convert("RGB").save(output_path, "PDF", resolution=float(dpi))

# =====================================================================
# ๔. ระบบสแกนอัจฉริยะ (Auto-Scan Functions)
# =====================================================================
def find_stamp_pos(pil_img, stamp_w, stamp_h):
    gray = pil_img.convert('L')
    W, H = gray.size
    # ระยะห่างจากขอบขวา: เพิ่มขึ้นเล็กน้อยเพื่อขยับตรายางมาทางซ้าย
    # กันกรอบลงเลขรับตกขอบกระดาษเวลาปริ้น
    RIGHT_MARGIN = 70
    threshold = (stamp_w * stamp_h) * 0.005
    for y in range(20, min(300, H), 20):
        for x in range(W - stamp_w - RIGHT_MARGIN, int(W * 0.5), -40):
            box = (x, y, x + stamp_w, y + stamp_h)
            try:
                region = gray.crop(box)
                dark = 0
                for p in region.getdata():
                    if p < 200:
                        dark += 1
                        if dark >= threshold:
                            break       # บริเวณนี้มีตัวอักษรเยอะ ข้ามไปจุดถัดไป
                else:
                    return x, y          # วนจบโดยไม่ทะลุ threshold = บริเวณว่างพอ
            except: pass
    return W - stamp_w - RIGHT_MARGIN, 20

def find_kasien_pos(pil_img, kasien_w, kasien_h, start_y, end_y, left_x, return_fit=False):
    """หาช่องว่างที่พอวางคำเกษียณได้

    return_fit=True จะคืน (x, y, พอดีไหม) เพิ่มมาด้วย
    ถ้าไม่พอดี ผู้เรียกจะได้ตัดสินใจเองว่าจะย้ายไปหน้ากระดาษเปล่าแทน
    """
    gray = pil_img.convert('L')
    px = gray.load()   # เข้าถึงพิกเซลโดยตรง เร็วกว่า getpixel มาก
    W, H = gray.size
    empty_streaks = []
    current_start = -1
    for y in range(start_y, end_y, 15):
        dark = 0
        for x in range(left_x, left_x + kasien_w, 5):
            if 0 <= x < W and 0 <= y < H and px[x, y] < 180:
                dark += 1
                if dark > 5: break
        if dark <= 5:
            if current_start == -1: current_start = y
        else:
            if current_start != -1:
                empty_streaks.append((current_start, y, y - current_start))
                current_start = -1
                
    if current_start != -1: 
        empty_streaks.append((current_start, end_y, end_y - current_start))
    
    # ไม่มีช่องไหนสูงพอ ก็เอาช่องที่ว่างมากที่สุดเท่าที่มี
    # (เดิมถอยไปวางที่ start_y เฉยๆ ซึ่งมักไปทับตัวหนังสือพอดี)
    if empty_streaks:
        empty_streaks.sort(key=lambda x: x[2], reverse=True)
        best = empty_streaks[0]
        fit = best[2] >= kasien_h
        y = best[0] + 30
    else:
        fit, y = False, start_y + 30

    return (left_x, y, fit) if return_fit else (left_x, y)

# =====================================================================
# ๕. ระบบ Render รูปภาพ
# =====================================================================
# --- Cache เพื่อความเร็ว (ใช้ซ้ำแทนการสร้าง/โหลดใหม่ทุกครั้ง) ---
_FONT_CACHE = {}          # เก็บฟอนต์ตามขนาด ไม่ต้องโหลดไฟล์ซ้ำ
_TOKENIZE_CACHE = {}      # เก็บผลตัดคำภาษาไทยตามข้อความ ไม่ต้องตัดซ้ำ
_MEASURE_DRAW = None      # ตัววัดความกว้าง สร้างตอนใช้ครั้งแรก (ไม่สร้างตอน import เพราะ PIL เป็น lazy)

def _measure_draw():
    global _MEASURE_DRAW
    if _MEASURE_DRAW is None:
        _MEASURE_DRAW = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    return _MEASURE_DRAW

def get_font(font_size, path="THSarabunNew.ttf"):
    """โหลดฟอนต์แบบ cache — โหลดจากไฟล์แค่ครั้งเดียวต่อขนาด
    ชื่อไฟล์เปล่าๆ ให้หาในโฟลเดอร์เดียวกับสคริปต์เสมอ (ดู _p) ไม่ใช่โฟลเดอร์ปัจจุบัน
    ถ้าโหลดไม่ได้จะตกไปใช้ฟอนต์เริ่มต้นซึ่งไม่มีตัวอักษรไทย ข้อความจะกลายเป็นสี่เหลี่ยม"""
    key = (path, font_size)
    font = _FONT_CACHE.get(key)
    if font is None:
        full_path = path if os.path.isabs(path) else _p(path)
        try:
            font = ImageFont.truetype(full_path, font_size)
        except Exception:
            font = ImageFont.load_default()
        _FONT_CACHE[key] = font
    return font

def cached_tokenize(line):
    """ตัดคำภาษาไทยแบบ cache — ข้อความเดิมไม่ต้องตัดซ้ำ"""
    toks = _TOKENIZE_CACHE.get(line)
    if toks is None:
        toks = word_tokenize(line)
        _TOKENIZE_CACHE[line] = toks
    return toks

def get_text_w(txt, font):
    try: return _measure_draw().textlength(txt, font=font)
    except: return font.getsize(txt)[0]

def render_transparent_stamp(receipt_no, percent_size, date_str=None, time_str=None):
    """date_str / time_str: ไม่ส่งมา = ใช้วันที่-เวลาจริงตอนนี้ (ส่งมาคือแก้เอง เช่น ลงรับย้อนหลัง)"""
    base_font_size = 46
    font_size = max(10, int(base_font_size * (percent_size / 100.0)))
    stamp_font = get_font(font_size)

    l1_txt = "โรงเรียนบ้านโพนทองประชาอุทิศ"
    line2_txt = "เลขที่รับ............................................"
    line3_txt = "วันที่.................................................."
    line4_txt = "เวลา...............................................น."
    
    padding = max(5, int(15 * (font_size / 46.0)))
    step = int(font_size * 1.2)
    stamp_w = int(get_text_w(l1_txt, stamp_font) + (80 * (font_size / 46.0)))
    stamp_h = (padding * 2) + (step * 4) 
    
    img = Image.new('RGBA', (stamp_w, stamp_h), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    text_color = (0, 51, 153, 255) 
    
    draw.rectangle([0, 0, stamp_w-1, stamp_h-1], outline=text_color, width=max(1, int(3 * (font_size / 46.0))))
    y_lines = [padding + (step * i) for i in range(4)]
    
    draw.text(((stamp_w - get_text_w(l1_txt, stamp_font))/2, y_lines[0]), l1_txt, font=stamp_font, fill=text_color)
    draw.text((padding, y_lines[1]), line2_txt, font=stamp_font, fill=text_color)
    draw.text((padding, y_lines[2]), line3_txt, font=stamp_font, fill=text_color)
    draw.text((padding, y_lines[3]), line4_txt, font=stamp_font, fill=text_color)
    
    float_y = int(-12 * (font_size / 46.0))
    
    if receipt_no:
        w_label = get_text_w("เลขที่รับ", stamp_font)
        w_total = get_text_w(line2_txt, stamp_font)
        cx = padding + w_label + ((w_total - w_label) / 2)
        draw.text((cx - get_text_w(str(receipt_no), stamp_font)/2, y_lines[1] + float_y), str(receipt_no), font=stamp_font, fill=text_color)
    
    date_str = date_str or get_thai_date()
    w_label = get_text_w("วันที่", stamp_font)
    w_total = get_text_w(line3_txt, stamp_font)
    cx = padding + w_label + ((w_total - w_label) / 2)
    draw.text((cx - get_text_w(date_str, stamp_font)/2, y_lines[2] + float_y), date_str, font=stamp_font, fill=text_color)
    
    time_str = time_str or get_thai_time_rounded()
    w_label = get_text_w("เวลา", stamp_font)
    w_total = get_text_w("เวลา...............................................", stamp_font)
    cx = padding + w_label + ((w_total - w_label) / 2)
    draw.text((cx - get_text_w(time_str, stamp_font)/2, y_lines[3] + float_y), time_str, font=stamp_font, fill=text_color)
    
    return img

def render_transparent_kasien(text, max_w_orig, percent_size, indent_pct=100, draw_bg=False, draw_border=False):
    base_font_size = 46 
    font_size = max(10, int(base_font_size * (percent_size / 100.0)))
    font = get_font(font_size)
    
    raw_lines = [line.strip() for line in text.split('\n') if line.strip()]
    step = int(font_size * 1.2)
    max_w_orig = max(10, max_w_orig)
    
    base_indent = get_text_w("        ", font)
    indent_px = max(0, int(base_indent * (indent_pct / 100.0)))
    
    arranged_lines = []
    actual_w = 0 
    
    for line in raw_lines:
        if line.startswith("เรียน ผอ."):
            arranged_lines.append((line, 0))
            tw = get_text_w(line, font)
            if tw > actual_w: actual_w = tw
        else:
            words = cached_tokenize(line)
            current_text = ""
            cur_indent = indent_px 
            for w in words:
                avail = max_w_orig - cur_indent
                if get_text_w(current_text + w, font) > avail and current_text.strip():
                    arranged_lines.append((current_text, cur_indent))
                    tw = get_text_w(current_text, font) + cur_indent
                    if tw > actual_w: actual_w = tw
                    current_text = w
                    cur_indent = 0 
                else:
                    current_text += w
            if current_text.strip():
                arranged_lines.append((current_text, cur_indent))
                tw = get_text_w(current_text, font) + cur_indent
                if tw > actual_w: actual_w = tw
                
    pad = int(15 * (font_size / 46.0))
    # ระยะกันตัวอักษรขาด: ข้อความถูกวาดเลื่อนขวา pad/2 จึงต้องเผื่อขอบขวาเพิ่ม
    # และเผื่อขอบล่างสำหรับสระล่าง/วรรณยุกต์ของบรรทัดสุดท้าย
    left_margin = int(pad / 2)
    right_safe = max(8, int(font_size * 0.45))
    bottom_safe = int(font_size * 0.35)
    needed_w = int(actual_w + left_margin + right_safe)   # ความกว้างจริงที่ต้องใช้วาดให้ครบ
    canvas_w = max(max_w_orig, needed_w)
    box_w = min(canvas_w, needed_w)
    required_h = max(10, len(arranged_lines) * step + pad + bottom_safe)

    img = Image.new('RGBA', (canvas_w, required_h), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)

    if draw_bg:
        draw.rectangle([0, 0, box_w, required_h], fill=(255, 255, 255, 235))
    if draw_border:
        draw.rectangle([0, 0, box_w-1, required_h-1], outline=(0, 51, 153, 255), width=max(1, int(2 * font_size / 46.0)))

    cy = int(pad / 2)
    for line, xoff in arranged_lines:
        draw.text((xoff + left_margin, cy), line, font=font, fill=(0, 51, 153, 255))
        cy += step

    return img

