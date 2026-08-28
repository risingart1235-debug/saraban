"""thaiwords.py — ตัดคำไทยแบบประหยัดหน่วยความจำ

ทำไมไม่ใช้ pythainlp ตรงๆ
--------------------------
โปรแกรมใช้การตัดคำเพื่ออย่างเดียว คือ "รู้ว่าขึ้นบรรทัดใหม่ตรงไหนได้"
เวลาวาดคำเกษียณลงกระดาษ

แต่ pythainlp กิน RAM ถึง ๒๒๔ MB ตอนโหลด ทั้งที่คลังคำจริงมีแค่ ๑.๕ MB
(ส่วนเกินคือโครงสร้าง Trie กับของอื่นที่เราไม่ได้ใช้)

ทำให้โปรแกรมทั้งระบบใช้ RAM ๓๘๕ MB ซึ่งเกือบเต็มโควตา ๕๑๒ MB
ของ hosting ฟรีแทบทุกเจ้า และเสี่ยงล่มเวลามีคนใช้พร้อมกัน

จึงดึงเฉพาะ "รายการคำ" ออกมาเก็บเป็นไฟล์ข้อความ แล้วเขียนตัวตัดคำเองแบบง่าย
(จับคู่คำที่ยาวที่สุดจากซ้ายไปขวา — maximal matching)
ผลลัพธ์ใกล้เคียงของเดิมมาก แต่ใช้ RAM น้อยกว่าสิบเท่า

ถ้าไฟล์คลังคำหาย จะถอยไปใช้ pythainlp ให้อัตโนมัติ
"""
import os
import re

_DICT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thai_words.txt")

_words = None
_maxlen = 1
_by_first = None          # อักษรตัวแรก -> ความยาวคำที่เป็นไปได้ (เรียงมากไปน้อย)

# ตัวอักษรที่ห้ามขึ้นบรรทัดใหม่นำหน้า (สระบน/ล่าง วรรณยุกต์ ฯลฯ)
_NON_STARTER = set("ะัาำิีึืุู็่้๊๋์ๆฯๅ")


def _load():
    global _words, _maxlen, _by_first
    if _words is not None:
        return
    try:
        with open(_DICT_FILE, encoding="utf-8") as f:
            _words = {w.strip() for w in f if w.strip()}
    except Exception:
        _words = set()

    if _words:
        _maxlen = max(len(w) for w in _words)
        _by_first = {}
        for w in _words:
            _by_first.setdefault(w[0], set()).add(len(w))
        _by_first = {k: sorted(v, reverse=True) for k, v in _by_first.items()}


def available() -> bool:
    _load()
    return bool(_words)


def tokenize(text: str) -> list:
    """ตัดข้อความไทยเป็นคำ

    วิธี: ไล่จากซ้ายไปขวา แต่ละตำแหน่งลองจับคำที่ยาวที่สุดที่มีในคลัง
    ถ้าไม่เจอเลยก็กินทีละตัวอักษรไปก่อน (แล้วรวบตัวที่ไม่รู้จักติดกันเป็นก้อนเดียว)
    """
    _load()
    if not _words:
        # ไม่มีคลังคำ -> ใช้ pythainlp แทน (ช้าและกินแรมกว่า แต่ยังทำงานได้)
        from pythainlp.tokenize import word_tokenize
        return word_tokenize(text, engine="newmm")

    out = []
    i, n = 0, len(text)
    unknown = ""
    while i < n:
        ch = text[i]
        # ช่องว่างและอักขระที่ไม่ใช่ไทย ปล่อยผ่านเป็นก้อน
        if not ("฀" <= ch <= "๿"):
            if unknown:
                out.append(unknown); unknown = ""
            j = i
            while j < n and not ("฀" <= text[j] <= "๿"):
                j += 1
            out.append(text[i:j])
            i = j
            continue

        hit = None
        for ln in _by_first.get(ch, ()):
            if ln > n - i:
                continue
            cand = text[i:i + ln]
            if cand in _words:
                # อย่าตัดถ้าตัวถัดไปเป็นสระ/วรรณยุกต์ที่ต้องเกาะคำหน้า
                nxt = text[i + ln] if i + ln < n else ""
                if nxt and nxt in _NON_STARTER:
                    continue
                hit = cand
                break
        if hit:
            if unknown:
                out.append(unknown); unknown = ""
            out.append(hit)
            i += len(hit)
        else:
            unknown += ch
            i += 1
    if unknown:
        out.append(unknown)
    return out


def build_dict_file(dest: str = None) -> str:
    """ดึงคลังคำจาก pythainlp มาเก็บเป็นไฟล์ (รันครั้งเดียวตอนเตรียมโปรเจกต์)"""
    from pythainlp.corpus.common import thai_words
    dest = dest or _DICT_FILE
    words = sorted(thai_words())
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(words))
    return dest


if __name__ == "__main__":
    p = build_dict_file()
    print(f"สร้างคลังคำแล้ว: {p} ({os.path.getsize(p)/1024:.0f} KB)")
