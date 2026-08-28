"""check_access.py — ตรวจว่าเน็ตเส้นนี้ถูก Cloudflare กันหรือเปล่า

ใช้ตอนเจออาการ "เน็ตโรงเรียนเข้าไม่ได้ แต่ hotspot มือถือเข้าได้"
เพื่อแยกให้ออกว่าสาเหตุคือ IP ต้นทาง หรือคือรูปแบบการยิงของโปรแกรมเราเอง

วิธีใช้ — รันบนเน็ตทั้งสองเส้นแล้วเทียบผลกัน:
    python check_access.py

ไม่ต้องล็อกอิน ไม่แตะข้อมูลหนังสือ ยิงแค่หน้าแรก 3 ครั้ง
"""
import sys
import time

import requests

import sppweb

# Console ภาษาไทยบน Windows เป็น cp874 ซึ่งพิมพ์สัญลักษณ์นอกตารางไม่ได้
# ถ้าไม่บังคับเป็น UTF-8 สคริปต์จะพังกลางทางด้วย UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TRIES = 3
GAP = 1.0          # เว้นจังหวะระหว่างครั้ง (วินาที)


def _verdict(response, text):
    """อ่านคำตอบแล้วบอกว่าใครเป็นคนปฏิเสธ — Cloudflare หรือตัวเว็บเอง"""
    ray = response.headers.get("cf-ray", "")
    mitigated = response.headers.get("cf-mitigated", "")
    status = response.status_code
    low = (text or "").lower()
    challenge = any(m in low for m in sppweb._CHALLENGE_MARKERS)

    real = sppweb._has_real_content(text)

    if mitigated.lower() == "challenge" or (challenge and not real):
        return "[ถูกกัน] Cloudflare ส่งหน้า challenge มา — IP เส้นนี้ถูกกัน"
    if status in (403, 429) and not real:
        return f"[ถูกกัน] ถูกปฏิเสธ (HTTP {status}) — IP เส้นนี้น่าจะถูกกัน"
    if status in (403, 429):
        return (f"[ไม่ใช่ CF] HTTP {status} แต่ได้หน้าจริงมาครบ "
                "— ตัวเว็บ PHP ปฏิเสธเอง ไม่ใช่ Cloudflare")
    if status >= 500:
        return f"[เว็บล่ม] HTTP {status} — ไม่เกี่ยวกับ Cloudflare"
    if status >= 400:
        return f"[ผิดพลาด] เว็บตอบ HTTP {status}"
    if not real:
        return ("[น่าสงสัย] HTTP 200 แต่ไม่ใช่หน้าล็อกอินหรือหน้ารายการหนังสือ "
                "— หน้าเว็บอาจเปลี่ยนรูปแบบ")
    return "[ผ่าน] เน็ตเส้นนี้เข้าเว็บ สพป. ได้ปกติ"


def main():
    print("กำลังตรวจการเข้าถึงเว็บ สพป. จากเน็ตเส้นนี้")
    print(f"ปลายทาง: {sppweb.BASE}")

    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
        print(f"IP ขาออกของเน็ตเส้นนี้: {ip}")
    except Exception:
        print("IP ขาออก: ดูไม่ได้ (ไม่เป็นไร ข้ามได้)")

    print(f"\nจะยิงหน้าแรก {TRIES} ครั้ง ห่างกัน {GAP} วินาที")
    print("-" * 62)

    blocked = 0
    for n in range(1, TRIES + 1):
        sess = sppweb.new_session()
        started = time.time()
        try:
            response = sess.get(sppweb.BASE, timeout=20)
            response.encoding = "utf-8"
            text = response.text or ""
        except Exception as e:
            print(f"ครั้งที่ {n}: ต่อไม่ติดเลย — {type(e).__name__}: {e}")
            blocked += 1
            continue
        finally:
            sess.close()

        line = _verdict(response, text)
        ray = response.headers.get("cf-ray", "-")
        print(f"ครั้งที่ {n}: HTTP {response.status_code} | "
              f"{time.time() - started:.1f} วิ | cf-ray: {ray}")
        print(f"           {line}")
        if line.startswith("[ถูกกัน]"):
            blocked += 1
        if n < TRIES:
            time.sleep(GAP)

    print("-" * 62)
    if blocked == TRIES:
        print("สรุป: เน็ตเส้นนี้ถูกกันทุกครั้ง — ปัญหาอยู่ที่ IP ต้นทาง")
        print("      แก้โค้ดอย่างเดียวไม่พอ ต้องขอ allowlist IP จาก สพป.")
        print("      หรือย้ายไปรันบนเน็ตเส้นที่ผ่าน")
    elif blocked:
        print(f"สรุป: ผ่านบ้างไม่ผ่านบ้าง ({blocked}/{TRIES} ครั้งถูกกัน)")
        print("      แบบนี้คือโดน rate limit ไม่ใช่แบน IP ถาวร")
        print("      แก้ได้ด้วยการลดจำนวน request และเว้นจังหวะให้ห่างขึ้น")
    else:
        print("สรุป: เน็ตเส้นนี้ใช้ได้ปกติ")
        print("      ถ้าตัวโปรแกรมยังโดนอยู่ แปลว่าเป็นเพราะยิงถี่เกินไป")
        print("      ไม่ใช่เพราะ IP — ให้ลดจำนวน request ลง")
    return 0 if blocked < TRIES else 1


if __name__ == "__main__":
    sys.exit(main())
