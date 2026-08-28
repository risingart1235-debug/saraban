"""manage_users.py — เพิ่ม/ลบ/ดูรายชื่อผู้ใช้เว็บ

  python manage_users.py add ชื่อผู้ใช้ รหัสผ่าน "ชื่อที่แสดง" [admin]
  python manage_users.py list
  python manage_users.py del ชื่อผู้ใช้
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from web.main import add_user, load_users, save_users

def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) >= 4:
        is_admin = "admin" in sys.argv[5:]
        add_user(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "", is_admin)
        print(f"เพิ่มผู้ใช้ '{sys.argv[2]}' แล้ว" + (" (เป็นผู้ดูแลระบบ)" if is_admin else ""))
    elif cmd == "list":
        users = load_users()
        if not users:
            print("ยังไม่มีผู้ใช้เลย — เพิ่มด้วย: python manage_users.py add ชื่อ รหัส")
        for name, u in users.items():
            st = {"pending":"รออนุมัติ","approved":"ใช้งานได้","rejected":"ไม่อนุญาต"}.get(u.get("status","approved"))
            role = " [ผู้ดูแล]" if u.get("role") == "admin" else ""
            print(f"  {name:14} {u.get('display',''):24} {st}{role}")
    elif cmd == "del" and len(sys.argv) >= 3:
        users = load_users()
        if users.pop(sys.argv[2], None) is None:
            print("ไม่พบผู้ใช้นี้")
        else:
            save_users(users); print(f"ลบผู้ใช้ '{sys.argv[2]}' แล้ว")
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
