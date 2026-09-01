#!/usr/bin/env python3
"""setup_drive_oauth.py — ขอสิทธิ์อัปไฟล์ขึ้น Google Drive "ครั้งเดียว" แล้วใช้ได้ตลอด

ทำไมต้องมีไฟล์นี้
------------------------------------------------------------------
เดิมอัปไฟล์ขึ้นไดร์ฟด้วย service account แต่ Google เลิกให้พื้นที่เก็บกับ
service account ไปแล้ว อัปกี่ครั้งก็ได้ 403 กลับมา:

    "Service Accounts do not have storage quota.
     Leverage shared drives, or use OAuth delegation instead."

ไดรฟ์ที่แชร์ (shared drive) ต้องมี Google Workspace ซึ่งบัญชี Gmail ธรรมดา
สร้างไม่ได้ จึงเหลือทางเดียวคือ OAuth — ให้ไฟล์ไปอยู่ในพื้นที่ ๑๕ GB
ของบัญชี Gmail เจ้าของโรงเรียนเอง

ต้องเตรียมอะไรก่อน (ทำครั้งเดียว)
------------------------------------------------------------------
๑. เข้า https://console.cloud.google.com/  เลือกโปรเจกต์เดิม (ตัวเดียวกับที่ทำ Sheets)

๒. เมนู "APIs & Services" -> "Library" -> เปิดใช้ "Google Drive API" (ถ้ายังไม่เปิด)

๓. เมนู "OAuth consent screen"
     - User Type เลือก  External
     - กรอกชื่อแอป/อีเมลติดต่อ ตามจริง
     - *** สำคัญที่สุด: หน้า "Publishing status" ต้องกด PUBLISH APP ***
       ให้เป็น "In production" ไม่ใช่ "Testing"
       เพราะถ้าเป็น Testing กุญแจที่ได้จะ "หมดอายุใน ๗ วัน" ต้องมาทำใหม่ทุกอาทิตย์
       สิทธิ์ที่เราขอ (drive.file = เห็นเฉพาะไฟล์ที่โปรแกรมนี้สร้างเอง) เป็นสิทธิ์
       ระดับไม่อ่อนไหว จึงกด publish ได้เลย ไม่ต้องส่งให้ Google ตรวจ

๔. เมนู "Credentials" -> Create credentials -> OAuth client ID
     - Application type: Desktop app
     - กด DOWNLOAD JSON แล้วเอาไฟล์มาวางไว้โฟลเดอร์เดียวกับไฟล์นี้

วิธีใช้
------------------------------------------------------------------
    python setup_drive_oauth.py

เบราว์เซอร์จะเปิดให้ล็อกอิน Google แล้วกดอนุญาต  เสร็จแล้วสคริปต์จะ
พิมพ์ค่าที่ต้องเอาไปใส่ตัวแปรระบบ SARABAN_DRIVE_OAUTH บน Render ให้
(ถ้าเครื่องยังไม่มีไลบรารี:  pip install google-auth-oauthlib)
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def die(msg):
    print("\n‼️  " + msg)
    sys.exit(1)


def find_client_file():
    """หาไฟล์ OAuth client ที่โหลดมาจาก Google Cloud Console"""
    if len(sys.argv) > 1:
        return sys.argv[1]
    hits = sorted(glob.glob("client_secret*.json")) or sorted(glob.glob("*oauth*client*.json"))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        die("ไม่เจอไฟล์ client_secret*.json ในโฟลเดอร์นี้\n"
            "   โหลดจาก Google Cloud Console -> Credentials -> OAuth client ID (Desktop app)\n"
            "   แล้ววางไว้ข้างไฟล์นี้ หรือสั่ง:  python setup_drive_oauth.py <ที่อยู่ไฟล์>")
    print("เจอหลายไฟล์:")
    for i, h in enumerate(hits, 1):
        print(f"  {i}. {h}")
    pick = input("เลือกหมายเลข: ").strip()
    try:
        return hits[int(pick) - 1]
    except Exception:
        die("เลือกไม่ถูกต้อง")


def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        die("ยังไม่มีไลบรารีที่ต้องใช้ สั่งติดตั้งก่อน:  pip install google-auth-oauthlib")

    path = find_client_file()
    print(f"ใช้ไฟล์กุญแจ: {path}")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    block = raw.get("installed") or raw.get("web")
    if not block:
        die("ไฟล์นี้ไม่ใช่ OAuth client ของ Desktop app "
            "(ต้องมีหัวข้อ \"installed\" ข้างใน) โหลดใหม่โดยเลือก Application type = Desktop app")

    print("\nกำลังเปิดเบราว์เซอร์ให้กดอนุญาต... (ถ้าไม่เด้งเอง ให้ก๊อปลิงก์ที่ขึ้นไปเปิดเอง)")
    flow = InstalledAppFlow.from_client_config(raw, SCOPES)
    # prompt="consent" บังคับให้ Google ออก refresh_token ใหม่ทุกครั้ง
    # (ถ้าเคยอนุญาตไปแล้วและไม่ใส่บรรทัดนี้ จะได้ค่าว่างกลับมา แล้วงงว่าทำไมไม่มี)
    creds = flow.run_local_server(port=0, prompt="consent",
                                  authorization_prompt_message="",
                                  success_message="เรียบร้อยแล้ว กลับไปที่หน้าต่างคำสั่งได้เลย")
    if not creds.refresh_token:
        die("Google ไม่ได้ให้ refresh token กลับมา ลองใหม่อีกครั้ง")

    payload = {"client_id": block["client_id"],
               "client_secret": block["client_secret"],
               "refresh_token": creds.refresh_token}
    one_line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    print("\n" + "=" * 68)
    print("ได้สิทธิ์แล้ว — เอาค่าข้างล่างนี้ไปใส่ที่")
    print("Render Dashboard -> Environment -> Add Environment Variable")
    print()
    print("    ชื่อ  : SARABAN_DRIVE_OAUTH")
    print("    ค่า   : (ทั้งบรรทัดข้างล่าง ก๊อปให้ครบ)")
    print()
    print(one_line)
    print("=" * 68)
    print("\nอย่าลืมตั้ง SARABAN_DRIVE_UPLOAD=on ด้วย ไม่งั้นโปรแกรมจะไม่อัปขึ้นไดร์ฟเลย")

    if input("\nบันทึกลง config.json ในเครื่องนี้ด้วยไหม (จะได้ทดสอบในเครื่องได้) [y/N]: ").strip().lower() == "y":
        import core
        cfg = core.load_config()
        cfg["drive_oauth"] = one_line
        core.save_config(cfg)
        print("บันทึกลง config.json แล้ว (ไฟล์นี้ถูกกันไม่ให้ขึ้น GitHub อยู่แล้ว)")

    print("\nกำลังทดสอบอัปไฟล์จริง...")
    os.environ["SARABAN_DRIVE_OAUTH"] = one_line
    os.environ.setdefault("SARABAN_DRIVE_UPLOAD", "on")
    import drive
    drive.reset()
    result = drive.probe()
    if result.get("ok"):
        print("✅ อัปไฟล์ขึ้นไดร์ฟได้แล้ว (อัปไฟล์ทดสอบแล้วลบทิ้งเรียบร้อย)")
    else:
        print("❌ ยังอัปไม่ได้: " + str(result.get("error"))[:400])


if __name__ == "__main__":
    main()
