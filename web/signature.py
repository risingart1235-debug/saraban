"""ลายเซ็นของผู้ใช้ — ทำความสะอาดรูปที่อัปโหลด และวาดลายเซ็นพร้อมวันที่ไว้วางบนเอกสาร

รูปลายเซ็นส่วนใหญ่ถ่ายจากกล้องมือถือบนกระดาษขาว ถ้าวางทั้งรูป พื้นกระดาษจะเป็นสี่เหลี่ยม
ทับตัวหนังสือ จึงตัดพื้นออกให้เหลือแต่เส้นหมึก แล้วเก็บไว้ที่เดียวกับข้อมูลผู้ใช้ (ชีต/ไฟล์)
ใช้ซ้ำได้ทุกเรื่องจากทุกเครื่อง ไม่ต้องอัปใหม่
"""
import io

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

import core

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# ถอดรหัสภาพเต็มแล้วกิน RAM ราว ๔ ไบต์ต่อจุด เครื่องฟรีมีแค่ 512 MB (JPEG ถูกย่อตอนถอดรหัสอยู่แล้ว)
MAX_PIXELS = 16_000_000
# ฝากในชีตเป็น base64 — ช่องหนึ่งของ Google Sheets รับได้ไม่เกิน ๕๐,๐๐๐ ตัวอักษร
MAX_STORE_BYTES = 36 * 1024
SIG_WIDTH_CM = 4.0              # ความกว้างลายเซ็นบนเอกสารที่ขนาด ๑๐๐%
DATE_INK = (0, 51, 153, 255)    # สีวันที่ใต้ลายเซ็น เดียวกับคำเกษียณ


class SignatureError(ValueError):
    """รูปที่อัปโหลดใช้เป็นลายเซ็นไม่ได้ — ข้อความบอกผู้ใช้ได้ตรงๆ"""


def thai_short_date(dt=None) -> str:
    """วันที่ใต้ลายเซ็น เช่น ๑๒ ต.ค. ๖๙"""
    dt = dt or core.now_th()
    return core.to_thai_digits(
        f"{dt.day} {core.THAI_MONTHS_ABBR[dt.month - 1]} {(dt.year + 543) % 100:02d}")


def clean_upload(raw: bytes) -> bytes:
    """รูปลายเซ็นที่ผู้ใช้อัปโหลด -> PNG พื้นใส ตัดขอบ ขนาดพอเก็บในช่องชีตได้"""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise SignatureError("รูปใหญ่เกิน ๑๐ MB")
    try:
        img = Image.open(io.BytesIO(raw))
        img.draft("RGB", (1600, 1600))       # JPEG จากกล้องมือถือ ถอดรหัสแบบย่อเลย ประหยัด RAM
    except Exception:
        raise SignatureError("เปิดรูปไม่ได้ — ใช้ไฟล์ JPG หรือ PNG")
    if img.width * img.height > MAX_PIXELS:
        raise SignatureError("รูปใหญ่เกินไป — ลองครอปให้เหลือแต่ลายเซ็น")
    try:
        img = ImageOps.exif_transpose(img)    # มือถือเก็บรูปตะแคงไว้ แล้วบอกทิศใน EXIF
        img.load()
    except Exception:
        raise SignatureError("เปิดรูปไม่ได้ — ใช้ไฟล์ JPG หรือ PNG")
    img.thumbnail((1600, 1600))
    rgba = img.convert("RGBA")
    # PNG ที่ตัดพื้นมาแล้วใช้ตามนั้น ที่เหลือ (รูปถ่าย/สแกน) ต้องตัดพื้นกระดาษออกเอง
    sig = rgba if rgba.getchannel("A").getextrema()[0] < 250 else _remove_paper(rgba)
    box = sig.getchannel("A").point(lambda v: 255 if v > 60 else 0).getbbox()
    if not box:
        raise SignatureError("ไม่พบเส้นลายเซ็นในรูป — เซ็นด้วยปากกาสีเข้มบนกระดาษขาว แล้วถ่ายให้ชัด")
    pad = 4
    sig = sig.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                    min(sig.width, box[2] + pad), min(sig.height, box[3] + pad)))
    return _encode(sig)


def _remove_paper(rgba):
    """เหลือแต่เส้นหมึก พื้นกระดาษโปร่งใส

    รูปจากกล้องมือถือแสงไม่เท่ากันทั้งภาพ (มุมหนึ่งสว่าง อีกมุมมีเงา) ใช้เกณฑ์ความเข้ม
    ค่าเดียวทั้งภาพไม่ได้ จึงประมาณสีกระดาษของแต่ละบริเวณ: ย่อภาพ เอาค่าสว่างสุดแถวนั้น
    แล้วขยายกลับ (เส้นหมึกบางกว่าช่องที่ย่อมาก ค่าสว่างสุดจึงเป็นสีกระดาษเสมอ)
    หมึก = จุดที่เข้มกว่ากระดาษแถวนั้นชัดเจน
    """
    gray = rgba.convert("L")
    small = gray.resize((max(1, gray.width // 16), max(1, gray.height // 16)), Image.BOX)
    paper = small.filter(ImageFilter.MaxFilter(5)).resize(gray.size, Image.BILINEAR)
    ink = ImageChops.subtract(paper, gray)
    alpha = ink.point(lambda v: 0 if v < 30 else 255 if v > 100 else (v - 30) * 255 // 70)
    # ระบายเส้นด้วยสีหมึกสีเดียว (ค่าเฉลี่ยของจุดที่เข้มชัด) ได้เส้นคมสม่ำเสมอ ไม่ติดสีกระดาษ
    strong = alpha.point(lambda v: 255 if v >= 250 else 0)
    stat = ImageStat.Stat(rgba.convert("RGB"), mask=strong)
    color = tuple(int(c) for c in stat.mean) if stat.count[0] else (20, 30, 90)
    out = Image.new("RGBA", rgba.size, color + (0,))
    out.putalpha(alpha)
    return out


def _encode(sig) -> bytes:
    sig = sig.copy()
    sig.thumbnail((600, 300), Image.LANCZOS)
    for _ in range(8):
        buf = io.BytesIO()
        sig.quantize(colors=64, method=Image.Quantize.FASTOCTREE).save(buf, "PNG", optimize=True)
        if buf.tell() <= MAX_STORE_BYTES:
            return buf.getvalue()
        sig = sig.resize((max(1, int(sig.width * 0.8)), max(1, int(sig.height * 0.8))), Image.LANCZOS)
    raise SignatureError("รูปลายเซ็นละเอียดเกินไป — ลองครอปให้เหลือแต่ลายเซ็น")


def render_block(sig_png: bytes, date_text: str, size_pct, px_per_cm: float):
    """ลายเซ็น + วันที่กึ่งกลางใต้ลายเซ็น เป็นภาพพื้นใสก้อนเดียว ไว้วางบนหน้าเอกสาร"""
    try:
        pct = int(size_pct)
    except (TypeError, ValueError):
        pct = 100
    scale = max(20, min(300, pct)) / 100.0
    sig = Image.open(io.BytesIO(sig_png)).convert("RGBA")
    w = max(10, int(SIG_WIDTH_CM * px_per_cm * scale))
    h = max(1, round(sig.height * w / sig.width))
    sig = sig.resize((w, h), Image.LANCZOS)

    date = core.to_thai_digits(str(date_text or "").strip())
    font_size = max(10, int(46 * scale))         # เท่าตัวหนังสือคำเกษียณที่ขนาดเดียวกัน
    font = core.get_font(font_size)
    tw = int(core.get_text_w(date, font)) if date else 0
    gap = int(font_size * 0.15)
    # เผื่อสระล่าง/วรรณยุกต์ และขอบขวาไม่ให้ตัวอักษรขาด (แบบเดียวกับกล่องคำเกษียณ)
    line_h = int(font_size * 1.2) + int(font_size * 0.35)
    width = max(w, tw + int(font_size * 0.45))
    height = h + (gap + line_h if date else 0)

    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    img.paste(sig, ((width - w) // 2, 0), sig)
    if date:
        ImageDraw.Draw(img).text(((width - tw) // 2, h + gap), date, font=font, fill=DATE_INK)
    return img
