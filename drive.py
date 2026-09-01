"""drive.py — อัปไฟล์ PDF ขึ้น Google Drive

ทำไมต้องมี
----------
บนคอมเครื่องนี้ Google Drive for Desktop ซิงก์โฟลเดอร์ C:\\แฟ้มเสนอ_ผอ ให้อยู่แล้ว
จึงไม่ต้องใช้ไฟล์นี้เลย

แต่ถ้าย้ายไปรันบน hosting จะไม่มีโปรแกรม Drive อยู่บนเครื่องนั้น
และดิสก์ก็ถูกล้างทุกครั้งที่รีสตาร์ท ไฟล์ PDF ที่ลงรับไว้จะหายหมด
จึงต้องส่งขึ้น Drive ผ่าน API แทน

การตั้งค่า (ใช้ service account ตัวเดียวกับที่ทำ Sheets)
  ๑. เอาอีเมล service account ไปแชร์ "โฟลเดอร์" ใน Drive เป็นผู้แก้ไข
     (ตอนนี้แชร์แค่สเปรดชีต ต้องแชร์โฟลเดอร์เพิ่ม)
  ๒. รัน: python setup_sheets.py drive <URL โฟลเดอร์>
"""
import os
import re
import io
import json
import threading

import core

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_lock = threading.Lock()
_svc = None


def folder_id_from(text: str) -> str:
    """แกะรหัสโฟลเดอร์จากลิงก์ Drive"""
    text = (text or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", text)
    return m.group(1) if m else text


def target_folder() -> str:
    cfg = core.load_config()
    return (os.environ.get("SARABAN_DRIVE_FOLDER", "").strip()
            or cfg.get("drive_folder_id", "").strip()
            or folder_id_from(cfg.get("drive_url", "")))


def enabled() -> bool:
    """เปิดใช้การอัปขึ้น Drive หรือยัง"""
    cfg = core.load_config()
    on = (os.environ.get("SARABAN_DRIVE_UPLOAD", "").strip().lower()
          or cfg.get("drive_upload", "").strip().lower())
    return on in ("1", "true", "yes", "on") and bool(target_folder())


def oauth_info():
    """สิทธิ์ OAuth ของบัญชี Google คนจริง — คืน dict หรือ None ถ้ายังไม่ได้ตั้ง

    ทำไมต้องมี: Google เลิกให้พื้นที่เก็บกับ service account แล้ว สร้างโฟลเดอร์ได้
    (โฟลเดอร์ไม่กินพื้นที่) แต่พออัปไฟล์จริงจะได้ 403 "Service Accounts do not have
    storage quota" ทุกครั้ง ไฟล์จึงต้องไปอยู่ในพื้นที่ของบัญชีคนจริงแทน
    ขอสิทธิ์ครั้งเดียวด้วย  python setup_drive_oauth.py
    """
    raw = (core.load_config().get("drive_oauth") or "").strip()
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"ค่า SARABAN_DRIVE_OAUTH ไม่ใช่ JSON ที่ถูกต้อง: {e}") from e
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not d.get(k)]
    if missing:
        raise RuntimeError("ค่า SARABAN_DRIVE_OAUTH ขาดข้อมูล: " + ", ".join(missing))
    return d


def _service():
    """สร้างตัวเชื่อม Drive — ใช้สิทธิ์ OAuth ก่อน ถ้าไม่มีค่อยถอยไปใช้ service account"""
    global _svc
    with _lock:
        if _svc is not None:
            return _svc
        from googleapiclient.discovery import build

        oauth = oauth_info()
        if oauth:
            from google.oauth2.credentials import Credentials as UserCredentials
            creds = UserCredentials(
                None, refresh_token=oauth["refresh_token"],
                client_id=oauth["client_id"], client_secret=oauth["client_secret"],
                token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
            _svc = build("drive", "v3", credentials=creds, cache_discovery=False)
            return _svc

        from google.oauth2.service_account import Credentials

        cfg = core.load_config()
        raw = os.environ.get("SARABAN_SA_JSON", "").strip()
        if raw:
            info = json.loads(raw)
        else:
            path = (os.environ.get("SARABAN_SA_FILE", "").strip()
                    or cfg.get("sa_file", "").strip())
            if not path or not os.path.exists(path):
                raise RuntimeError("ยังไม่มีไฟล์กุญแจ service account")
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        return _svc


def reset():
    global _svc
    with _lock:
        _svc = None


def _run(req, tries=3):
    """ยิงคำสั่งพร้อมลองซ้ำเมื่อเน็ตสะดุด (เหมือนฝั่ง Sheets)"""
    import time
    for i in range(tries):
        try:
            return req.execute()
        except Exception as e:
            msg = f"{type(e).__name__}: {e}".lower()
            temporary = any(k in msg for k in ("transport", "servernotfound", "timeout",
                                               "timed out", "connection", "ssl",
                                               "500", "502", "503", "504", "rate"))
            if not temporary or i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def ensure_subfolder(name: str, parent: str = None) -> str:
    """หาโฟลเดอร์ย่อยตามชื่อ ถ้าไม่มีก็สร้าง (ใช้แยกตามวันที่เหมือนในเครื่อง)"""
    svc = _service()
    parent = parent or target_folder()
    safe = name.replace("'", "")
    q = (f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
         f"and '{parent}' in parents and trashed = false")
    res = _run(svc.files().list(q=q, fields="files(id,name)", pageSize=1,
                                supportsAllDrives=True, includeItemsFromAllDrives=True))
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    made = _run(svc.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder",
              "parents": [parent]},
        fields="id", supportsAllDrives=True))
    return made["id"]


def upload(local_path: str, day: str = None, name: str = None) -> dict:
    """ส่งไฟล์ขึ้น Drive คืน {'id','link'} — ถ้าไม่ได้เปิดใช้จะคืน {} เฉยๆ"""
    if not enabled():
        return {}
    from googleapiclient.http import MediaFileUpload

    svc = _service()
    name = name or os.path.basename(local_path)
    parent = ensure_subfolder(day, target_folder()) if day else target_folder()

    media = MediaFileUpload(local_path, mimetype="application/pdf", resumable=False)
    f = _run(svc.files().create(
        body={"name": name, "parents": [parent]},
        media_body=media, fields="id, webViewLink", supportsAllDrives=True))
    return {"id": f.get("id"), "link": f.get("webViewLink")}


def probe() -> dict:
    """ทดสอบ "อัปไฟล์จริง" แล้วลบทิ้ง — เป็นวิธีเดียวที่บอกความจริงได้

    ทำไมไม่ใช้ check() อย่างเดียว: สิทธิ์ที่เราขอคือ drive.file ซึ่งมองเห็นเฉพาะ
    ไฟล์ที่โปรแกรมนี้สร้างเอง พอไปเรียก files.get กับโฟลเดอร์ที่ "คน" สร้างไว้
    Google จะตอบ 404 ทั้งที่สิทธิ์เขียนมีครบ อ่านผลแล้วเข้าใจผิดได้ง่ายมาก
    ส่วนการอัปไฟล์จริงจะเจอปัญหาที่แท้จริง เช่น service account ไม่มีพื้นที่เก็บ
    """
    import tempfile
    if not target_folder():
        return {"ok": False, "error": "ยังไม่ได้ตั้งโฟลเดอร์ปลายทาง"}
    tmp = os.path.join(tempfile.gettempdir(), "_saraban_probe.pdf")
    fid = None
    try:
        with open(tmp, "wb") as f:
            f.write(b"%PDF-1.4 saraban upload probe")
        from googleapiclient.http import MediaFileUpload
        svc = _service()
        made = _run(svc.files().create(
            body={"name": "_ทดสอบสิทธิ์อัปไฟล์.pdf", "parents": [target_folder()]},
            media_body=MediaFileUpload(tmp, mimetype="application/pdf", resumable=False),
            fields="id", supportsAllDrives=True))
        fid = made["id"]
        return {"ok": True, "mode": "oauth" if oauth_info() else "service account"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if fid:                      # ไฟล์ทดสอบต้องไม่ค้างอยู่ในไดร์ฟของครู
            try:
                _run(_service().files().delete(fileId=fid, supportsAllDrives=True))
            except Exception:
                pass
        try:
            os.remove(tmp)
        except OSError:
            pass


def check() -> dict:
    """ทดสอบว่าเข้าถึงโฟลเดอร์ได้จริงไหม

    หมายเหตุ: ด้วยสิทธิ์ drive.file การเรียก files.get กับโฟลเดอร์ที่คนสร้างไว้เอง
    จะได้ 404 เสมอ แม้เขียนไฟล์ลงไปได้จริง ถ้าอยากรู้ว่า "อัปได้จริงไหม" ใช้ probe()
    """
    fid = target_folder()
    if not fid:
        return {"ok": False, "error": "ยังไม่ได้ตั้งโฟลเดอร์ปลายทาง"}
    try:
        svc = _service()
        info = _run(svc.files().get(fileId=fid, fields="id,name,mimeType",
                                    supportsAllDrives=True))
        is_folder = info.get("mimeType") == "application/vnd.google-apps.folder"
        return {"ok": is_folder, "name": info.get("name"), "id": info.get("id"),
                "error": "" if is_folder else "รหัสนี้ไม่ใช่โฟลเดอร์"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
