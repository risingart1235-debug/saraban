"""show_url.py — แสดงที่อยู่เว็บตอนเปิดเซิร์ฟเวอร์

ทำไมข้อความภาษาไทยต้องอยู่ในไฟล์นี้ ไม่ใช่ในไฟล์ .bat
--------------------------------------------------------
cmd.exe อ่านไฟล์ .bat ด้วยรหัสภาษาเก่าของวินโดวส์ (cp874) ไม่ใช่ UTF-8
ถ้าใส่ภาษาไทยลงไปตรงๆ ตัวอักษรจะเพี้ยน แล้ว cmd จะพยายามรันเศษข้อความ
เป็นคำสั่ง เกิด error แบบ "'...' is not recognized as an internal command"

ไฟล์ .bat จึงเก็บไว้เป็นภาษาอังกฤษล้วน แล้วให้ Python พิมพ์ภาษาไทยแทน
(Python สั่งให้หน้าจอรับ UTF-8 ได้เอง)

หา IP ในวงแลนให้อัตโนมัติด้วย เพราะ IP อาจเปลี่ยนเมื่อเปลี่ยนเราเตอร์
หรือย้ายเครื่อง ถ้าให้จำเลขเองแล้วมันเปลี่ยน จะงงว่าทำไมมือถือเข้าไม่ได้
"""
import socket
import sys

# บังคับให้หน้าจอรับภาษาไทยได้ ไม่ว่าเครื่องจะตั้งรหัสภาษาไว้แบบไหน
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
except Exception:
    pass

LINE = "  " + "=" * 50
PORT = 8000


def lan_ip() -> str:
    """IP ของเครื่องนี้ในวงแลน (ที่มือถือจะใช้เข้า)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # ไม่ได้ส่งข้อมูลจริง แค่ให้ระบบเลือกการ์ดเน็ตที่ใช้ออกเน็ต
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


def stopped():
    print()
    print(LINE)
    print("     เซิร์ฟเวอร์หยุดทำงานแล้ว")
    print()
    print("     ถ้าไม่ได้ตั้งใจปิด ให้ดูข้อความข้างบนว่ามีอะไรผิดพลาด")
    print("     แล้วเปิดไฟล์นี้ใหม่อีกครั้ง")
    print(LINE)


def banner():
    ip = lan_ip()
    print()
    print(LINE)
    print("     ระบบลงรับหนังสือราชการ")
    print(LINE)
    print()
    if ip:
        print("     เปิดจากมือถือ (ต่อวายฟายเดียวกับคอมเครื่องนี้):")
        print()
        print(f"         http://{ip}:{PORT}")
    else:
        print("     หา IP ของเครื่องไม่เจอ — ตรวจว่าต่อเน็ตอยู่ไหม")
    print()
    print("     เปิดจากคอมเครื่องนี้:")
    print()
    print(f"         http://localhost:{PORT}")
    print()
    print("  " + "-" * 50)
    print("     ห้ามปิดหน้าต่างนี้ — ปิดเมื่อไหร่เว็บหยุดทำงาน")
    print("     (ย่อเก็บไว้ที่แถบล่างได้)")
    print("  " + "-" * 50)


def wait_and_open(timeout: float = 60.0):
    """รอจนเซิร์ฟเวอร์พร้อม แล้วค่อยเปิดเบราว์เซอร์

    ต้องรอก่อน เพราะถ้าเปิดทันทีตอนสั่งรัน เซิร์ฟเวอร์ยังไม่ขึ้น
    เบราว์เซอร์จะขึ้นหน้า "เชื่อมต่อไม่ได้" แล้วผู้ใช้ต้องมากดรีเฟรชเอง
    """
    import time
    import webbrowser

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                webbrowser.open(f"http://localhost:{PORT}")
                return
        time.sleep(0.4)


if __name__ == "__main__":
    if "--stopped" in sys.argv:
        stopped()
    elif "--open" in sys.argv:
        wait_and_open()
    else:
        banner()
