"""show_url.py — แสดงที่อยู่เว็บให้เห็นชัดตอนเปิดเซิร์ฟเวอร์

หา IP ของเครื่องในวงแลนให้อัตโนมัติ เพราะ IP อาจเปลี่ยนเมื่อเปลี่ยนเราเตอร์
หรือย้ายเครื่อง ถ้าให้จำเลขเองแล้วมันเปลี่ยน จะงงว่าทำไมมือถือเข้าไม่ได้
"""
import socket
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # ไม่ได้ส่งข้อมูลจริง แค่ให้ระบบเลือกการ์ดเน็ตที่ใช้ออกเน็ต
        return s.getsockname()[0]
    except Exception:
        return "(หา IP ไม่เจอ)"
    finally:
        s.close()


ip = lan_ip()
print()
print("  ==================================================")
print("     ระบบลงรับหนังสือราชการ")
print("  ==================================================")
print()
print("     เปิดจากมือถือ (ต่อวายฟายเดียวกับคอมเครื่องนี้):")
print()
print(f"         http://{ip}:8000")
print()
print("     เปิดจากคอมเครื่องนี้:")
print()
print("         http://localhost:8000")
print()
print("  --------------------------------------------------")
print("     ห้ามปิดหน้าต่างนี้ - ปิดเมื่อไหร่เว็บหยุดทำงาน")
print("     (ย่อเก็บไว้ที่แถบล่างได้)")
print("  --------------------------------------------------")
