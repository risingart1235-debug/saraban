#!/usr/bin/env python3
"""phone_fetch.py — ดึงหนังสือใหม่จากเว็บ สพป. "จากมือถือ" แล้วส่งเข้าระบบลงรับ

ทำไมมือถือทำได้ทั้งที่เซิร์ฟเวอร์คลาวด์ทำไม่ได้
--------------------------------------------------------
เว็บ สพป. (Cloudflare) บล็อก IP ของศูนย์ข้อมูล (เซิร์ฟเวอร์คลาวด์อย่าง Render, HTTP 403)
แต่ "อนุญาต" เครื่องบ้าน/มือถือบนเน็ตทั่วไป เหมือนคอมที่โรงเรียน
มือถือจึงดึงได้โดยไม่ต้องปลอมที่อยู่หรือหลบด่านใดๆ (ข้อแม้เดียว: อย่าใช้ User-Agent
ชื่อ "python-requests" ซึ่งเว็บขึ้นบัญชีดำไว้ — sppweb ตั้ง UA ให้เรียบร้อยแล้ว)

สคริปต์นี้ทำ ๔ อย่าง:
  ๑. ถามระบบ (บน Render) ว่าหนังสือไหนลงรับ/ข้ามไปแล้ว
  ๒. ล็อกอินเว็บ สพป. แล้วดูรายการหนังสือใหม่
  ๓. โหลด PDF ของเรื่องที่ยังไม่ได้ลงรับ
  ๔. ส่งเข้า Render ให้ทำ AI เกษียณ + ตรายาง + LINE + ทะเบียน ต่อ
     แล้วไปตรวจ/กดลงรับต่อที่หน้าเว็บ "โหมดที่ ๔ — ลงรับจากมือถือ"

วิธีใช้ใน a-Shell (iOS) — ตั้งครั้งเดียว:
  ๑. pip install requests beautifulsoup4
  ๒. วางไฟล์ ๓ ตัวไว้โฟลเดอร์เดียวกัน:  core.py, sppweb.py, phone_fetch.py
  ๓. แก้ RENDER_URL กับ PHONE_TOKEN ด้านล่างครั้งเดียว
ใช้งานทุกครั้ง:  python phone_fetch.py   (แล้วใส่รหัสเว็บ สพป. เมื่อถาม)
"""
import os
import sys
import tempfile

# ======== ตั้งค่า ========
# วิธีที่แนะนำ: สร้างไฟล์ phone_config.json ข้างไฟล์นี้ (ไม่ต้องแก้ .py) เช่น
#   {
#     "render_url": "https://saraban.onrender.com",
#     "token": "โทเคนลับให้ตรงกับ SARABAN_PHONE_TOKEN บน Render",
#     "spp_user": "ชื่อผู้ใช้เว็บ สพป.",
#     "spp_pass": "รหัสผ่านเว็บ สพป."
#   }
# ไฟล์นี้ถูกกันไม่ให้ขึ้น GitHub แล้ว (.gitignore) รหัสจึงอยู่ในเครื่องคุณเครื่องเดียว
# จะใส่แค่บางคีย์ก็ได้ — ที่ไม่ใส่ (เช่น spp_pass) สคริปต์จะถามตอนรัน
RENDER_URL  = "https://saraban.onrender.com"    # ค่าเริ่มต้น (phone_config.json ทับได้)
PHONE_TOKEN = ""                                 # รองรับของเดิม; แนะนำ env/config แทนการแก้ไฟล์นี้
MAX_FETCH   = 20                                 # เท่าคิวรอเริ่มต้นฝั่งเซิร์ฟเวอร์
MAX_UPLOAD_BYTES = 40 * 1024 * 1024              # ต้องตรงกับเพดานฝั่งเซิร์ฟเวอร์
# ==================================

# บังคับจอมือถือให้แสดงภาษาไทยไม่เพี้ยน
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def die(msg):
    print("\n‼️  " + msg)
    sys.exit(1)


# ไลบรารีที่ต้อง pip install; ถ้าขาดให้บอกชัดว่าติดตั้งยังไง
try:
    import requests
except ImportError:
    die("ยังไม่มีไลบรารี requests — พิมพ์:  pip install requests beautifulsoup4")

# sppweb ต้องมี core.py อยู่โฟลเดอร์เดียวกัน (import core ข้างใน)
try:
    import sppweb
except ImportError as e:
    die("หา sppweb.py / core.py ไม่เจอ — วางไว้โฟลเดอร์เดียวกับไฟล์นี้ (" + str(e) + ")")


def _load_config():
    """อ่าน phone_config.json ข้างสคริปต์ (ถ้ามี) — ที่เก็บ token/รหัสไว้ในเครื่อง"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "phone_config.json")
    if not os.path.exists(path):
        return {}
    try:
        import json
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        die("อ่าน phone_config.json ไม่ได้ (รูปแบบ JSON ผิด?): " + str(e))


CFG = _load_config()


def _resolve_token():
    """หาโทเคนจากแหล่งที่ไม่ต้อง commit ก่อน; ค่าฝังใน .py มีไว้รองรับรุ่นเก่าเท่านั้น"""
    env = os.environ.get("SARABAN_PHONE_TOKEN", "").strip()
    if env:
        return env
    if str(CFG.get("token", "")).strip():
        return str(CFG["token"]).strip()
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "token.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return PHONE_TOKEN.strip()


TOKEN = _resolve_token()
RENDER = (str(CFG.get("render_url", "")).strip() or RENDER_URL).rstrip("/")


def _headers():
    return {"X-Phone-Token": TOKEN}


def fetch_history():
    """ถาม Render ว่า book_id ไหนจัดการไปแล้ว จะได้ไม่โหลด/ส่งซ้ำ"""
    try:
        r = requests.get(RENDER + "/api/phone/history",
                         headers=_headers(), timeout=60)
    except requests.RequestException as e:
        die("ต่อ Render ไม่ได้: " + str(e))
    if r.status_code == 401:
        die("โทเคนไม่ถูกต้อง — ตรวจ PHONE_TOKEN ให้ตรงกับ SARABAN_PHONE_TOKEN บน Render")
    if r.status_code == 503:
        die("Render ยังไม่ได้ตั้ง SARABAN_PHONE_TOKEN — ไปตั้งที่ Render → Environment ก่อน")
    r.raise_for_status()
    return set(r.json().get("done", []))


def submit(pdf_path, meta):
    """ส่ง PDF + ข้อมูลหนังสือเข้า Render สร้างงานรอลงรับ"""
    fields = dict(meta)
    # เป็นคำสั่ง retry ที่ชัดเจน: ถ้า job เดิมล้มให้ใช้ job เดิมส่งใหม่;
    # แต่ถ้ายังทำอยู่ เซิร์ฟเวอร์จะคืน job เดิมและไม่เริ่มซ้ำ
    fields["retry_failed"] = "true"
    with open(pdf_path, "rb") as f:
        files = {"file": ("doc.pdf", f, "application/pdf")}
        r = requests.post(RENDER + "/api/phone/submit",
                          headers=_headers(), files=files, data=fields, timeout=180)
    if r.status_code == 409:
        # เกิดได้เมื่ออีกเครื่องลงรับ/ข้ามช่วงหลัง fetch_history — ไม่ใช่งานเสีย
        return {"ok": True, "already_handled": True, "created": False, "job_id": ""}
    if r.status_code == 429:
        raise RuntimeError("คิวเซิร์ฟเวอร์เต็ม กรุณารอสักครู่แล้วรันใหม่")
    r.raise_for_status()
    return r.json()


def ask_credentials():
    """รหัส สพป. จาก: ตัวแปรระบบ > phone_config.json > ถามตอนรัน (ถ้าไม่มีที่ไหนเลย)"""
    user = (os.environ.get("SPP_USER") or str(CFG.get("spp_user", "")).strip()
            or input("ชื่อผู้ใช้เว็บ สพป.: ").strip())
    pwd = os.environ.get("SPP_PASS") or str(CFG.get("spp_pass", "")).strip()
    if not pwd:
        try:
            import getpass
            pwd = getpass.getpass("รหัสผ่านเว็บ สพป. (พิมพ์แล้วจอไม่โชว์): ")
        except Exception:
            pwd = input("รหัสผ่านเว็บ สพป.: ")
    if not user or not pwd:
        die("ยังไม่ได้ใส่ชื่อผู้ใช้/รหัสผ่าน")
    return user, pwd


def main():
    if not TOKEN:
        die("ยังไม่มีโทเคน — ใส่ในไฟล์ phone_config.json (คีย์ \"token\") "
            "หรือดูตัวอย่างที่ phone_config.example.json")

    user, pwd = ask_credentials()

    print("\n•  กำลังถามระบบว่าลงรับอะไรไปแล้ว... (ถ้าช้า Render กำลังตื่นจากพักเครื่อง)")
    done = fetch_history()
    print("   ลงรับ/ข้ามไปแล้ว %d เรื่อง" % len(done))

    print("•  กำลังล็อกอินเว็บ สพป. ...")
    sess = sppweb.login(user, pwd)

    print("•  กำลังดูรายการหนังสือ...")
    docs = sppweb.list_documents(sess, pages=3)
    new = [d for d in docs if d["book_id"] not in done]
    print("   ทั้งหมด %d เรื่อง | ยังไม่ลงรับ %d เรื่อง" % (len(docs), len(new)))
    if not new:
        print("\n✅ ไม่มีเรื่องใหม่ — เรียบร้อย")
        return

    if len(new) > MAX_FETCH:
        print("   (ใหม่เยอะ ดึงแค่ %d เรื่องล่าสุดก่อน — รันซ้ำเพื่อดึงที่เหลือ)" % MAX_FETCH)
        new = new[:MAX_FETCH]

    ok, links = 0, []
    for i, d in enumerate(new, 1):
        bid = d["book_id"]
        title = (d.get("doc_title") or "")[:40]
        print("[%d/%d] %s  %s ..." % (i, len(new), bid, title), end=" ")
        tmp = ""
        try:
            det = sppweb.fetch_detail(sess, bid)
            if not det["main_pdf"]:
                print("ข้าม (ไม่มีไฟล์ PDF)")
                continue
            fd, tmp = tempfile.mkstemp(prefix="spp_", suffix=".pdf")
            os.close(fd)
            sppweb.download(sess, det["main_pdf"], tmp)
            if os.path.getsize(tmp) > MAX_UPLOAD_BYTES:
                print("ข้าม (ไฟล์ใหญ่เกิน ๔๐ MB)")
                continue
            meta = {"book_id": bid,
                    "doc_no": d.get("doc_no", "-"),
                    "doc_title": d.get("doc_title", "-"),
                    "doc_date": d.get("doc_date", "-"),
                    "sender": d.get("sender", "-"),
                    "emoji": det.get("emoji", "🔵"),
                    "attach": sppweb.attach_text(det["attachments"])}
            res = submit(tmp, meta)
            if res.get("already_handled"):
                print("จัดการไปแล้วโดยอีกเครื่อง")
            else:
                ok += 1
                if res.get("job_id"):
                    links.append(res["job_id"])
                print("อยู่ในคิวเดิม" if not res.get("created", True) else "ส่งแล้ว")
        except Exception as e:
            print("ผิดพลาด: %s" % e)
            if "คิวเซิร์ฟเวอร์เต็ม" in str(e):
                print("   หยุดส่งรอบนี้เพื่อไม่เพิ่มภาระเซิร์ฟเวอร์")
                break
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    print("\n✅ เสร็จ — ส่งเข้าระบบ %d/%d เรื่อง" % (ok, len(new)))
    if links:
        print("\nไปตรวจ/กดลงรับต่อที่  โหมดที่ ๔ — ลงรับจากมือถือ")
        print("   %s/queue" % RENDER)


if __name__ == "__main__":
    try:
        main()
    except sppweb.LoginError as e:
        die("ล็อกอินเว็บ สพป. ไม่ผ่าน: %s" % e)
    except KeyboardInterrupt:
        print("\nยกเลิก")
