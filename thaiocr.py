"""thaiocr.py — อ่านบรรทัด "เรียน" ของหนังสือสแกนในเครื่อง ด้วย OpenThai2.1-OCR-Text (iApp, Apache-2.0)

หนังสือจาก สพป. ส่วนใหญ่เป็นไฟล์สแกน ไม่มีตัวหนังสือในไฟล์ (๕ ใน ๖ เรื่องที่สุ่มดู พ.ย. ๒๕๖๙)
ของเดิมจึงต้องรอบรรทัด "เรียน" จาก Gemini อย่างเดียว ตัวนี้อ่านเองจากภาพหน้าแรก
ทดสอบกับหนังสือจริงจาก สพป. ๑๒ เรื่อง (สแกน ๑๑) อ่านถูกครบ ~๐.๗ วินาทีต่อหน้า (CPU เดียว)

ใช้ ONNX Runtime ไม่ใช้ OpenVINO: OpenVINO กินหน่วยความจำ ~๘๑๖ MB เกิน 512 MB ของ Render ฟรี
ONNX Runtime (ปิด memory arena ตาม config.json ของโมเดล) + ภาพตัวหาบรรทัด 800 จุด เพิ่มขณะอ่าน ~๑๔๐ MB
โหลดโมเดลครั้งแรกที่ใช้แล้วเก็บไว้ (~๖๘ MB) และให้อ่านทีละงาน (สองงานพร้อมกันหน่วยความจำจะเป็นสองเท่า)

ไม่มีโมเดล/แพ็กเกจในเครื่องนี้ (เช่น คอมที่ยังไม่ได้รัน fetch_ocr_model.py) คืน None เงียบๆ
ระบบกลับไปใช้บรรทัด "เรียน" ที่ Gemini อ่านมาเหมือนเดิม
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import urllib.request

MODEL_REPO = "iapp/OpenThai2.1-OCR-Text-20261118"
MODEL_REV = "v2.1.4"          # ตรึงรุ่นไว้ — อัปเดตเมื่อไหร่ผลอ่านอาจเปลี่ยนโดยไม่รู้ตัว
# sha256 ของไฟล์รุ่น v2.1.4 (ตรวจกับ SHA256SUMS ของโมเดลแล้ว ๒๒ พ.ย. ๒๕๖๙)
MODEL_FILES = {
    "det.onnx": "193bab7a04fca699a6c82e6abb5b81bdb28177f0abd4062552b04908dafb19f8",
    "rec.onnx": "82ec89533346faadcb30df96703dad2dd7521de65709e60d83fd9745b8968124",
    "rec2.onnx": "6ec0f96f5ade665fc7eb33a9800ed055bc220870cbf963cf97c4750e64cd27ce",
    "rec_dict.txt": "259ef2636cec0eeae225ae9fd9a63fa9305285650c22d7ec34fe8a82b24c2f1a",
    "config.json": "f5a38f5677784b10d4abcc2ad8a8b044826a8211bc51213b38c889ad792b417a",
    "runtime/line_engine.py": "6bbd83c590e66445a6844f69ed3029be98e3be0fd1fcc01e2147ace28748d54a",
    "runtime/thai_norm.py": "53a03ac139fb0de3e7aec0f5b8ab69b0b519fb5e636f7c1de09cb357b023344a",
    "NOTICE": "4c0e4f27877836de51e29aec3918b6c743c3028cd4163b6661fd540336db32b3",
}
MODEL_DIR = os.environ.get("SARABAN_OCR_MODEL") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ocr_model")
# อ่านทั้งหน้า ไม่ตัดครึ่งบน: ตัดแล้วคำ "เรียน" ที่อยู่โดดๆ หน้าแท็บหลุดหรือกลายเป็น "เรี๊ยน" (๒ ใน ๖ เรื่อง)
# ทั้งหน้าอ่านถูกครบ
DPI = 200
# ขนาดภาพที่ตัวหาบรรทัดใช้ (ด้านยาว) — หน่วยความจำขณะอ่านขึ้นกับค่านี้ ไม่ใช่ DPI
# วัดกับหนังสือจริง ๑๒ เรื่อง: 1280 (ค่าเดิมของโมเดล) +๑๘๓ MB · 800 +๑๔๓ MB · 640 +๑๐๙ MB อ่านถูกครบทุกค่า
# เลือก 800 เผื่อหนังสือตัวเล็ก
DET_LIMIT = 800
_TONES = str.maketrans("", "", "\u0e48\u0e49\u0e4a\u0e4b\u0e47\u0e4c ")   # ไม้เอก–จัตวา ไม้ไต่คู้ การันต์ ช่องว่าง


def _is_recipient(text: str) -> bool:
    """ขึ้นต้นด้วย "เรียน" — ไม่สนวรรณยุกต์เกิน (ตัวอ่านบางทีเห็นรอยเปื้อนเป็นไม้ตรี: "เรี๊ยน")"""
    return text.translate(_TONES).startswith("เรียน")

_lock = threading.Lock()
_engine = None
_failed = False


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch_model(dest: str = MODEL_DIR) -> str:
    """โหลดโมเดลจาก Hugging Face (รุ่นที่ตรึงไว้) ตรวจ sha256 ทุกไฟล์ — ไฟล์ไหนไม่ตรงหยุดเลย"""
    for name, want in MODEL_FILES.items():
        path = os.path.join(dest, name)
        if os.path.exists(path) and _sha256(path) == want:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REV}/{name}"
        part = path + ".part"
        urllib.request.urlretrieve(url, part)
        got = _sha256(part)
        if got != want:
            os.remove(part)
            raise RuntimeError(f"{name}: sha256 ไม่ตรง ({got})")
        os.replace(part, path)
    open(os.path.join(dest, "runtime", "__init__.py"), "a").close()
    return dest


def _get_engine():
    """โหลดตัวอ่านครั้งแรกที่ใช้ — ไม่มีโมเดล/แพ็กเกจ คืน None (และไม่ลองซ้ำทุกเรื่อง)"""
    global _engine, _failed
    if _engine is not None or _failed:
        return _engine
    if not os.path.exists(os.path.join(MODEL_DIR, "rec.onnx")):
        _failed = True
        return None
    try:
        if MODEL_DIR not in sys.path:
            sys.path.insert(0, MODEL_DIR)
        from runtime.line_engine import LineOCR
        _engine = LineOCR(MODEL_DIR, threads=1, det={"limit": DET_LIMIT},
                          runtime={"backend": "onnxruntime", "ort_low_memory": True})
    except Exception as e:                      # ไม่มี onnxruntime/opencv ฯลฯ
        print(f"โหลดตัวอ่าน OCR ไม่ได้ ใช้บรรทัดเรียนจาก AI แทน: {type(e).__name__}: {e}")
        _failed = True
    return _engine


def available() -> bool:
    with _lock:
        return _get_engine() is not None


def _left(line) -> float:
    return min(p[0] for p in line["box"])


def recipient_from_lines(lines) -> str | None:
    """แถวแรกที่ขึ้นต้นด้วย "เรียน" — รวมทุกชิ้นในแถวเดียวกัน (ตัวอ่านแยก "เรียน" กับข้อความหลังแท็บเป็นคนละชิ้น)"""
    rows = {}
    for line in lines:
        rows.setdefault(line["row"], []).append(line)
    for row in sorted(rows):
        text = " ".join(l["text"].strip() for l in sorted(rows[row], key=_left) if l["text"].strip())
        if _is_recipient(text):
            words = text.split()
            words[0] = "เรียน" if words[0].translate(_TONES) == "เรียน" else words[0]
            return " ".join(words)
    return None


def read_recipient(pdf_path: str) -> str | None:
    """บรรทัด "เรียน" จากหน้าแรกของไฟล์ (ภาพสแกน) หรือ None ถ้าอ่านไม่ได้/ไม่มีตัวอ่าน"""
    import core
    with _lock:
        engine = _get_engine()
        if engine is None:
            return None
        try:
            img = core.render_pdf_page(pdf_path, 1, DPI)
            return recipient_from_lines(engine.read(img)["lines"])
        except Exception as e:
            print(f"OCR บรรทัดเรียนไม่สำเร็จ: {type(e).__name__}: {e}")
            return None


if __name__ == "__main__":
    # python thaiocr.py fetch           โหลดโมเดล (Dockerfile เรียกตอน build / คอมโรงเรียนรันเองครั้งเดียว)
    # python thaiocr.py <ไฟล์.pdf>       ลองอ่านบรรทัดเรียน
    if sys.argv[1:] == ["fetch"]:
        print("โหลดโมเดลไว้ที่", fetch_model())
    else:
        for p in sys.argv[1:]:
            print(p, "→", read_recipient(p))
