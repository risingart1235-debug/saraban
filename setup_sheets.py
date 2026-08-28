"""setup_sheets.py — ตั้งค่าและย้ายทะเบียนขึ้น Google Sheets

ทำไมต้องใช้ Sheets:
  ถ้าเอาระบบไปรันบน hosting ฟรี ดิสก์จะถูกล้างทุกครั้งที่เซิร์ฟเวอร์รีสตาร์ท
  ทะเบียน .xlsx จะหาย เลขรับเด้งกลับเป็น ๑ — Sheets อยู่บนคลาวด์จึงไม่หาย
  และรองรับหลายคนเขียนพร้อมกันได้ด้วย

วิธีใช้:
  python setup_sheets.py steps      แสดงขั้นตอนเตรียมกุญแจจาก Google
  python setup_sheets.py set <ไฟล์กุญแจ.json> <ลิงก์หรือรหัสสเปรดชีต>
  python setup_sheets.py check      ทดสอบว่าต่อได้จริง
  python setup_sheets.py migrate    คัดลอกทะเบียน+ประวัติของเดิมขึ้น Sheets
  python setup_sheets.py on         เปลี่ยนมาใช้ Sheets
  python setup_sheets.py off        กลับไปใช้ไฟล์ในเครื่อง
  python setup_sheets.py status     ดูว่าตอนนี้ใช้อะไรอยู่

อัปไฟล์ PDF ขึ้น Drive (จำเป็นเมื่อรันบน hosting เท่านั้น):
  python setup_sheets.py drive <URL โฟลเดอร์ Drive>
  python setup_sheets.py drive-on    เปิดใช้
  python setup_sheets.py drive-off   ปิด (ค่าเริ่มต้น — คอมที่มี Drive for Desktop ไม่ต้องใช้)
"""
import os
import re
import sys
import json
import shutil

# บังคับให้พิมพ์ภาษาไทยออกหน้าจอได้ทุก terminal (บางตัวตั้ง codepage เป็น cp874)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core
import store

SA_DEST = core._p("service_account.json")   # เก็บกุญแจไว้ข้างโค้ด (.gitignore กันไว้แล้ว)


def _p(*a):
    print(*a)


def steps():
    _p(__doc__.split("วิธีใช้:")[0])
    _p("ขั้นตอนเตรียมกุญแจจาก Google (ทำครั้งเดียว ~๕ นาที)")
    _p("=" * 62)
    _p("""
 ๑. เปิด  https://console.cloud.google.com/projectcreate
    ตั้งชื่อโปรเจกต์อะไรก็ได้ เช่น saraban  แล้วกด CREATE

 ๒. เปิดใช้ Google Sheets API
    https://console.cloud.google.com/apis/library/sheets.googleapis.com
    เลือกโปรเจกต์ที่เพิ่งสร้าง แล้วกด ENABLE

 ๓. สร้าง service account (บัญชีสำหรับโปรแกรม)
    https://console.cloud.google.com/iam-admin/serviceaccounts/create
    ตั้งชื่อ เช่น saraban-bot  ->  CREATE AND CONTINUE  ->  DONE
    (ไม่ต้องใส่ role อะไร)

 ๔. สร้างกุญแจ
    คลิกที่ service account ที่เพิ่งสร้าง -> แท็บ KEYS
    ADD KEY -> Create new key -> เลือก JSON -> CREATE
    ไฟล์ .json จะถูกดาวน์โหลดลงเครื่อง (เก็บให้ดี ห้ามให้ใครเห็น)

 ๕. สร้างสเปรดชีตเปล่า
    เปิด  https://sheets.new   แล้วตั้งชื่อ เช่น "ทะเบียนหนังสือรับ"
    คัดลอก URL ไว้

 ๖. แชร์สเปรดชีตให้ service account
    ในสเปรดชีต กดปุ่ม "แชร์" (Share)
    วางอีเมลของ service account (ลงท้าย .iam.gserviceaccount.com
    ดูได้ในไฟล์ .json ช่อง client_email)
    ตั้งสิทธิ์เป็น "ผู้แก้ไข" (Editor) แล้วกดส่ง

 ๗. กลับมาที่นี่ แล้วรัน:
    python setup_sheets.py set "C:\\path\\ที่โหลดมา.json" "<URL สเปรดชีต>"
    python setup_sheets.py check
    python setup_sheets.py migrate
    python setup_sheets.py on
""")


def sheet_id_from(text: str) -> str:
    """รับได้ทั้ง URL เต็มและรหัสเปล่าๆ"""
    text = (text or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    return m.group(1) if m else text


def cmd_set(sa_path: str, sheet: str):
    if not os.path.exists(sa_path):
        _p(f"!! ไม่พบไฟล์ {sa_path}")
        return
    try:
        info = json.load(open(sa_path, encoding="utf-8"))
    except Exception as e:
        _p(f"!! อ่านไฟล์กุญแจไม่ได้: {e}")
        return
    if info.get("type") != "service_account":
        _p("!! ไฟล์นี้ไม่ใช่กุญแจ service account (ต้องมี type: service_account)")
        return

    if os.path.abspath(sa_path) != os.path.abspath(SA_DEST):
        shutil.copy2(sa_path, SA_DEST)
        _p(f"คัดลอกกุญแจมาไว้ที่ {SA_DEST}")

    sid = sheet_id_from(sheet)
    cfg = core.load_config()
    cfg["sa_file"] = SA_DEST
    cfg["sheet_id"] = sid
    core.save_config(cfg)
    _p(f"บันทึกค่าแล้ว | sheet_id = {sid}")
    _p(f"อีเมลที่ต้องแชร์สเปรดชีตให้: {info.get('client_email')}")
    _p("ถ้ายังไม่ได้แชร์ ให้แชร์เป็น 'ผู้แก้ไข' ก่อน แล้วรัน: python setup_sheets.py check")


def cmd_check():
    cfg = core.load_config()
    _p(f"sa_file  : {cfg.get('sa_file') or '(ยังไม่ตั้ง)'}")
    _p(f"sheet_id : {cfg.get('sheet_id') or '(ยังไม่ตั้ง)'}")
    if not cfg.get("sa_file") or not cfg.get("sheet_id"):
        _p("!! ยังตั้งค่าไม่ครบ — รัน: python setup_sheets.py steps")
        return False
    try:
        st = store.SheetsStore()
    except Exception as e:
        _p(f"!! ต่อไม่ได้: {e}")
        _p("   เช็คว่าแชร์สเปรดชีตให้ service account เป็น 'ผู้แก้ไข' แล้วหรือยัง")
        return False
    _p(f"ต่อสำเร็จ | บัญชีโปรแกรม: {st.client_email}")
    _p(f"  แท็บทะเบียน  : {len(st.registry_rows())} แถว")
    _p(f"  แท็บประวัติ  : {len(st.history_ids())} รายการ")
    _p(f"  เลขรับถัดไป  : {st.peek_receipt_no()}")
    return True


def cmd_migrate():
    """คัดลอกทะเบียน + ประวัติจากไฟล์ในเครื่องขึ้น Sheets"""
    if not cmd_check():
        return
    st = store.SheetsStore()
    local = store.LocalStore()

    rows = local.registry_rows()
    have = st.registry_rows()
    _p(f"\nทะเบียนในเครื่อง {len(rows)} แถว | บน Sheets มีอยู่แล้ว {len(have)} แถว")
    if have:
        _p("!! บน Sheets มีข้อมูลอยู่แล้ว — ยกเลิกเพื่อกันข้อมูลซ้ำ")
        _p("   ถ้าต้องการเริ่มใหม่ ให้ลบแถวในแท็บ 'ทะเบียนรับ' บน Sheets ให้เหลือแต่หัวตารางก่อน")
        return
    if rows:
        CHUNK = 500          # ส่งทีละก้อน กัน request ใหญ่เกิน
        for i in range(0, len(rows), CHUNK):
            part = [[("" if c is None else str(c)) for c in r] for r in rows[i:i + CHUNK]]
            st._append(st.TAB_REG, part)
            _p(f"  ส่งทะเบียนแล้ว {min(i + CHUNK, len(rows))}/{len(rows)} แถว")

    ids = sorted(local.history_ids())
    recs = local.doc_records()
    hist_have = st.history_ids()
    todo = [i for i in ids if i not in hist_have]
    _p(f"\nประวัติในเครื่อง {len(ids)} รายการ | ต้องส่งเพิ่ม {len(todo)}")
    if todo:
        lines = []
        for b in todo:
            r = recs.get(b, {})
            lines.append([b, r.get("when", ""), r.get("status", ""), r.get("receipt_no", "")])
        CHUNK = 500
        for i in range(0, len(lines), CHUNK):
            st._append(st.TAB_HIST, lines[i:i + CHUNK])
            _p(f"  ส่งประวัติแล้ว {min(i + CHUNK, len(lines))}/{len(lines)} รายการ")

    _p("\nย้ายเสร็จแล้ว — ตรวจซ้ำ:")
    st2 = store.SheetsStore()
    _p(f"  ทะเบียนบน Sheets : {len(st2.registry_rows())} แถว")
    _p(f"  ประวัติบน Sheets : {len(st2.history_ids())} รายการ")
    _p(f"  เลขรับถัดไป      : {st2.peek_receipt_no()}")
    _p("\nถ้าตัวเลขตรงกับของเดิม ให้รัน: python setup_sheets.py on")


def cmd_switch(on: bool):
    cfg = core.load_config()
    if on and not cmd_check():
        return
    cfg["store_mode"] = "sheets" if on else "local"
    core.save_config(cfg)
    store.reset_store()
    _p(f"\nเปลี่ยนเป็น: {cfg['store_mode']}")
    _p("อย่าลืมรีสตาร์ทเซิร์ฟเวอร์เว็บและโปรแกรมเดสก์ท็อป")


def cmd_drive(url=None, on=None):
    """ตั้งโฟลเดอร์ Drive ปลายทาง / เปิด-ปิดการอัปไฟล์ขึ้น Drive"""
    import drive
    cfg = core.load_config()
    if url:
        cfg["drive_folder_id"] = drive.folder_id_from(url)
        if url.startswith("http"):
            cfg["drive_url"] = url
        core.save_config(cfg)
        _p(f"ตั้งโฟลเดอร์ปลายทาง: {cfg['drive_folder_id']}")
    if on is not None:
        cfg["drive_upload"] = "on" if on else ""
        core.save_config(cfg)
        _p(f"อัปไฟล์ขึ้น Drive: {'เปิด' if on else 'ปิด'}")

    drive.reset()
    cfg = core.load_config()
    _p("")
    _p(f"โฟลเดอร์ปลายทาง : {cfg.get('drive_folder_id') or '(ยังไม่ตั้ง)'}")
    _p(f"เปิดใช้งาน       : "
       f"{'ใช่' if drive.enabled() else 'ไม่ (คอมเครื่องนี้มี Drive for Desktop ซิงก์ให้อยู่แล้ว)'}")
    if not cfg.get("drive_folder_id"):
        return
    r = drive.check()
    if r.get("ok"):
        _p(f"ทดสอบเข้าถึง    : สำเร็จ — โฟลเดอร์ \"{r['name']}\"")
    else:
        _p(f"ทดสอบเข้าถึง    : ไม่สำเร็จ — {r.get('error')}")
        sa = cfg.get("sa_file")
        if sa and os.path.exists(sa):
            info = json.load(open(sa, encoding="utf-8"))
            _p("   ต้องแชร์โฟลเดอร์ใน Drive ให้อีเมลนี้เป็น 'ผู้แก้ไข' ก่อน:")
            _p(f"   {info.get('client_email')}")


def cmd_status():
    cfg = core.load_config()
    mode = cfg.get("store_mode") or "local"
    _p(f"โหมดที่ตั้งไว้ : {mode}")
    _p(f"sheet_id      : {cfg.get('sheet_id') or '(ยังไม่ตั้ง)'}")
    _p(f"sa_file       : {cfg.get('sa_file') or '(ยังไม่ตั้ง)'}")
    try:
        st = store.get_store()
        _p(f"ใช้งานจริง    : {st.kind}")
        _p(f"  ทะเบียน     : {len(st.registry_rows())} แถว")
        _p(f"  ประวัติ     : {len(st.history_ids())} รายการ")
        _p(f"  เลขรับถัดไป : {st.peek_receipt_no()}")
    except Exception as e:
        _p(f"!! เปิดที่เก็บไม่ได้: {e}")


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "help"):
        _p(__doc__); return
    c = a[0]
    if c == "steps":       steps()
    elif c == "set" and len(a) >= 3: cmd_set(a[1], a[2])
    elif c == "check":     cmd_check()
    elif c == "migrate":   cmd_migrate()
    elif c == "on":        cmd_switch(True)
    elif c == "off":       cmd_switch(False)
    elif c == "status":    cmd_status()
    elif c == "drive":     cmd_drive(a[1] if len(a) > 1 else None)
    elif c == "drive-on":  cmd_drive(None, True)
    elif c == "drive-off": cmd_drive(None, False)
    else:                  _p(__doc__)


if __name__ == "__main__":
    main()
