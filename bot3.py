"""bot3.py — ระบบลงรับหนังสือราชการ (เวอร์ชันเดสก์ท็อป Windows)

สมองของระบบทั้งหมดย้ายไปอยู่ที่ core.py แล้ว ไฟล์นี้เหลือแค่ส่วนหน้าจอ (Tkinter)
แก้ logic ที่ core.py ที่เดียว ทั้งเวอร์ชันเดสก์ท็อปและเวอร์ชันเว็บจะได้ตรงกันเสมอ
"""
import os
import re
import json
import shutil
import time
import threading
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime
from PIL import Image, ImageDraw, ImageTk

import sppweb

# สมองทั้งหมดอยู่ที่ core.py
from core import *
from core import _p, _w, _BASE_DIR, _preload_heavy_libs, _hide_console

_hide_console()   # ซ่อนหน้าต่าง Console สีดำ (เฉพาะเวอร์ชันเดสก์ท็อป)

# ==========================================
# ๖. ระบบ GUI ควบคุมหลัก (Main Application)
# ==========================================
class SarabanApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self._job_lock = threading.Lock()
        self._active_job = None
        self._cookie_lock = threading.Lock()
        self._cookie_worker_active = False
        self.spp_session_state = None
        self.cookie_driver = None
        self.title("ระบบลงรับหนังสืออัจฉริยะ (AI + มุมมองแบ่งหน้า Word Style)")
        self.configure(bg="#f0f0f0")
        self.center_window(560, 520)
        self.create_widgets()
        # ให้หน้าต่างวาดเสร็จก่อน ค่อยแอบโหลดไลบรารีหนักเบื้องหลัง
        self.after(150, lambda: threading.Thread(target=_preload_heavy_libs, daemon=True).start())
        # เปิดโปรแกรมแล้วไปดึง Cookie ให้อัตโนมัติเลย ไม่ต้องกดปุ่มเอง
        self.after(800, self.auto_start_cookie)

    def auto_start_cookie(self):
        """ทำงานอัตโนมัติตอนเปิดโปรแกรม: เปิด Chrome → ล็อกอิน → ดึง Cookie → ตรวจข่าวต่อ"""
        if self.cookie_entry.get().strip():
            return  # มี Cookie อยู่แล้ว ไม่ต้องดึงซ้ำ
        self.log("เริ่มดึง Cookie อัตโนมัติตอนเปิดโปรแกรม...")
        self.set_status("กำลังดึง Cookie อัตโนมัติ...")
        self.open_chrome_for_cookie()

    def center_window(self, w, h):
        """จัดให้หน้าต่างเปิดขึ้นที่กึ่งกลางหน้าจอ
        ถ้าจอเล็กกว่าขนาดที่ขอ ให้ย่อลงให้พอดีจอ (กันหน้าต่างล้นจนกดปุ่มล่างไม่ได้)"""
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(w, int(sw * 0.95))
        h = min(h, int(sh * 0.88))   # เผื่อที่ให้ taskbar ด้วย
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)    # ค่อนไปทางบนนิดหน่อย ดูสมดุลกว่ากลางเป๊ะ
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(520, 420)

    def create_widgets(self):
        tk.Label(self, text="📚 ระบบลงรับหนังสือราชการอัตโนมัติ", font=("Helvetica", 15, "bold"), bg="#f0f0f0", fg="#003399").pack(pady=(10, 6))

        frame1 = tk.LabelFrame(self, text=" โหมดที่ ๑: ตรวจสอบจากเว็บ สพป. ", font=("Helvetica", 10), bg="#f0f0f0", padx=10, pady=8)
        frame1.pack(fill="x", padx=20, pady=(0, 2))

        row1 = tk.Frame(frame1, bg="#f0f0f0")
        row1.pack()                       # ไม่ fill = กล่องจัดกึ่งกลางเอง
        self.cookie_entry = tk.Entry(row1, width=38, show="•")
        self.cookie_entry.pack(side="left")
        tk.Button(row1, text="📋 วาง", font=("Helvetica", 8), bg="#e0e0e0", command=self.paste_cookie).pack(side="left", padx=(3, 0))

        # ปุ่มหลักที่ใช้บ่อยที่สุด — ทำให้ใหญ่เต็มแถวและสีเด่น กันกดผิดปุ่ม
        self.web_btn = tk.Button(
            frame1, text="▶  ดึงข้อมูลเว็บ", font=("Helvetica", 15, "bold"),
            bg="#4CAF50", fg="white", activebackground="#43A047", activeforeground="white",
            relief="raised", bd=4, height=1, cursor="hand2", command=self.start_web_mode)
        self.web_btn.pack(fill="x", padx=5, pady=(8, 4))

        row2 = tk.Frame(frame1, bg="#f0f0f0")
        row2.pack()                       # ไม่ fill = ปุ่มคู่นี้จัดกึ่งกลางเอง
        tk.Button(row2, text="🌐 เปิด Chrome เข้าเว็บข่าว", font=("Helvetica", 8), bg="#FF9800", fg="white", command=self.open_chrome_for_cookie).pack(side="left", padx=(0, 6))
        tk.Button(row2, text="🍪 ดึง Cookie อัตโนมัติ", font=("Helvetica", 8), bg="#795548", fg="white", command=self.grab_cookie_from_chrome).pack(side="left")

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="วาง (Paste)", command=self.paste_cookie)
        self.cookie_entry.bind("<Button-3>", self.show_context_menu)
        self.cookie_entry.bind("<Button-2>", self.show_context_menu)
        self.cookie_entry.bind("<Control-v>", self.paste_cookie_event)
        self.cookie_entry.bind("<Control-V>", self.paste_cookie_event)

        # โหมดที่ ๒ กับ ๓ วางคู่กันบรรทัดเดียว แบ่งครึ่งเท่ากันเป๊ะด้วย grid + uniform
        modes = tk.Frame(self, bg="#f0f0f0")
        modes.pack(fill="x", padx=20, pady=(8, 6))
        modes.columnconfigure(0, weight=1, uniform="mode")
        modes.columnconfigure(1, weight=1, uniform="mode")

        frame2 = tk.LabelFrame(modes, text=" โหมดที่ ๒: นำเข้าไฟล์ (AI) ", font=("Helvetica", 9), bg="#f0f0f0", padx=6, pady=6)
        frame2.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.local_btn = tk.Button(frame2, text="📂 เลือกไฟล์จากเครื่อง", bg="#2196F3", fg="white", command=self.start_local_mode)
        self.local_btn.pack(pady=(2, 3))
        tk.Label(frame2, text="AI อ่าน + เกษียณให้\nพร้อมส่งเข้า LINE",
                 font=("Helvetica", 8), bg="#f0f0f0", fg="#777", justify="center").pack()

        frame3 = tk.LabelFrame(modes, text=" โหมดที่ ๓: ลงเลขรับบนกระดาษเปล่า ", font=("Helvetica", 9), bg="#f0f0f0", padx=6, pady=6)
        frame3.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.stamp_btn = tk.Button(frame3, text="🔢 ลงตรายางเลขรับ", bg="#9C27B0", fg="white", command=self.start_stamp_only_mode)
        self.stamp_btn.pack(pady=(2, 3))
        tk.Label(frame3, text="A4 เปล่า ไว้ปริ้นทับกระดาษ\nรันเลขต่อจาก Excel",
                 font=("Helvetica", 8), bg="#f0f0f0", fg="#777", justify="center").pack()

        btn_row = tk.Frame(self, bg="#f0f0f0")
        btn_row.pack(pady=(0, 5))
        tk.Button(btn_row, text="📁 เปิดโฟลเดอร์เก็บไฟล์", bg="#009688", fg="white", command=self.open_output_folder).pack(side="left", padx=4)
        tk.Button(btn_row, text="⚙️ ตั้งค่า API Key", bg="#607D8B", fg="white", command=self.open_settings).pack(side="left", padx=4)

        # กล่องแสดงสิ่งที่กำลังทำอยู่ (Activity Log)
        log_frame = tk.LabelFrame(self, text=" 📋 สิ่งที่กำลังทำอยู่ ", font=("Helvetica", 10), bg="#f0f0f0", padx=6, pady=4)
        log_frame.pack(fill="both", expand=True, padx=20, pady=(0, 4))
        log_scroll = tk.Scrollbar(log_frame)
        log_scroll.pack(side="right", fill="y")
        self.log_box = tk.Text(log_frame, height=5, wrap="word", state="disabled",
                               font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                               yscrollcommand=log_scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        log_scroll.config(command=self.log_box.yview)

        self.status_lbl = tk.Label(self, text="สถานะ: พร้อมทำงาน", font=("Helvetica", 9), bg="#f0f0f0", fg="#666", wraplength=520)
        self.status_lbl.pack(side="bottom", pady=(2, 6))
        self.log("พร้อมทำงาน")

    def open_output_folder(self):
        """เปิดโฟลเดอร์เก็บไฟล์ (ของวันนี้ถ้ามี) ใน File Explorer เพื่อสะดวกตอนปริ้น"""
        base = OUTPUT_ROOT
        today_str = datetime.now().strftime("%Y-%m-%d")
        target = os.path.join(base, today_str)
        folder = target if os.path.exists(target) else base
        try:
            if not os.path.exists(folder):
                os.makedirs(folder)
            os.startfile(folder)
            self.set_status(f"เปิดโฟลเดอร์: {folder}")
        except Exception as e:
            messagebox.showerror("ผิดพลาด", f"เปิดโฟลเดอร์ไม่สำเร็จ:\n{e}")
            self.set_status(f"เปิดโฟลเดอร์ไม่สำเร็จ: {e}")

    def open_settings(self):
        """หน้าต่างตั้งค่า API Key พร้อมระบบเซฟลง config.json"""
        cfg = load_config()
        win = tk.Toplevel(self)
        win.title("ตั้งค่า API Key")
        win.configure(bg="#f0f0f0")
        win.transient(self)
        win.grab_set()

        # จัดหน้าต่างย่อยให้อยู่กลางจอเช่นกัน
        w, h = 560, 560
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}")

        tk.Label(win, text="🔑 ตั้งค่า API Key", font=("Helvetica", 14, "bold"),
                 bg="#f0f0f0", fg="#003399").pack(pady=(15, 4))
        tk.Label(win, text="เว้นว่างไว้ = ใช้ค่าเริ่มต้นภายในโปรแกรม",
                 font=("Helvetica", 9), bg="#f0f0f0", fg="#888").pack(pady=(0, 10))

        fields = [
            ("LINE Access Token", "line_access_token"),
            ("LINE Group ID", "line_group_id"),
            ("imgBB API Key", "imgbb_api_key"),
            ("Gemini API Key", "gemini_api_key"),
            ("ชื่อผู้ใช้ เว็บ สพป. (Login)", "login_user"),
            ("รหัสผ่าน เว็บ สพป. (Login)", "login_pass"),
        ]
        entries = {}
        form = tk.Frame(win, bg="#f0f0f0")
        form.pack(fill="x", padx=20)
        for label_text, key in fields:
            row = tk.Frame(form, bg="#f0f0f0")
            row.pack(fill="x", pady=6)
            tk.Label(row, text=label_text, font=("Helvetica", 10), bg="#f0f0f0",
                     width=18, anchor="w").pack(anchor="w")
            ent = tk.Entry(row, width=58, show="•")
            ent.insert(0, cfg.get(key, ""))
            ent.pack(fill="x")
            entries[key] = ent

        # ปุ่มแสดง/ซ่อนข้อความ
        def toggle_show():
            show = "" if show_var.get() else "•"
            for e in entries.values():
                e.config(show=show)
        show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(win, text="แสดงข้อความ", variable=show_var, bg="#f0f0f0",
                       command=toggle_show).pack(anchor="w", padx=22, pady=(6, 0))

        def do_save():
            # รักษาค่า store/Sheets/Drive ที่หน้าต่างนี้ไม่ได้แสดงไว้ด้วย
            new_cfg = dict(cfg)
            new_cfg.update({k: entries[k].get().strip() for _, k in fields})
            try:
                save_config(new_cfg)
                apply_config()
                messagebox.showinfo("สำเร็จ", "บันทึก API Key เรียบร้อยแล้วครับ", parent=win)
                win.destroy()
            except Exception as e:
                messagebox.showerror("ผิดพลาด", f"บันทึกไม่สำเร็จ: {e}", parent=win)

        btn_frm = tk.Frame(win, bg="#f0f0f0")
        btn_frm.pack(pady=18)
        tk.Button(btn_frm, text="💾 บันทึก", font=("Helvetica", 11, "bold"),
                  bg="#4CAF50", fg="white", width=14, command=do_save).pack(side="left", padx=6)
        tk.Button(btn_frm, text="ยกเลิก", font=("Helvetica", 11),
                  bg="#e0e0e0", width=10, command=win.destroy).pack(side="left", padx=6)
        
    def paste_cookie(self):
        try:
            clipboard_data = self.clipboard_get()
            self.cookie_entry.delete(0, tk.END)
            self.cookie_entry.insert(0, clipboard_data)
        except tk.TclError: pass

    def paste_cookie_event(self, event):
        self.paste_cookie()
        return "break" 

    def show_context_menu(self, event):
        self.context_menu.tk_popup(event.x_root, event.y_root)
        
    def log(self, msg):
        """เพิ่มข้อความลงกล่องแสดงสิ่งที่กำลังทำอยู่ (พร้อมเวลา)"""
        if not hasattr(self, 'log_box'):
            return
        ts = datetime.now().strftime('%H:%M:%S')
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def set_status(self, msg):
        self.status_lbl.config(text=f"สถานะ: {msg}")
        self.log(msg)
        self.update_idletasks()

    def _set_main_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        for name in ("web_btn", "local_btn", "stamp_btn"):
            button = getattr(self, name, None)
            if button is not None:
                try:
                    button.config(state=state)
                except Exception:
                    pass

    def _claim_job(self, kind):
        """กันงานเดสก์ท็อปซ้อนกัน เพราะทุกโหมดใช้สถานะหน้าจอร่วมกัน"""
        with self._job_lock:
            if self._active_job is not None:
                return None
            base = _w("_desktop_jobs")
            os.makedirs(base, exist_ok=True)
            job = {
                "kind": kind,
                "dir": tempfile.mkdtemp(prefix=f"{kind}-", dir=base),
            }
            self._active_job = job
        self._set_main_buttons(False)
        return job

    def _finish_job(self, job, cleanup=True):
        """ปล่อยงานแบบ idempotent; worker เก่าห้ามปล่อยงานใหม่ที่มาแทน"""
        if not job:
            return
        released = False
        with self._job_lock:
            if self._active_job is job:
                self._active_job = None
                released = True
        if cleanup:
            shutil.rmtree(job.get("dir", ""), ignore_errors=True)
        if released:
            try:
                self.after(0, lambda: self._set_main_buttons(True))
            except Exception:
                pass

    def _job_path(self, job, name):
        return os.path.join(job["dir"], name)

    def _selenium_auto_login(self, driver, user, pwd):
        """พยายามล็อกอินอัตโนมัติด้วยรหัสที่บันทึกไว้
        คืนค่า True=สำเร็จ, False=ลองแล้วแต่ไม่สำเร็จ/หาช่องไม่เจอ
        ใช้การ 'รอจนหน้าพร้อมจริง' แทนการหน่วงเวลาตายตัว เพื่อกัน Chrome เปิดช้า"""
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
        except ImportError:
            return False
        try:
            # 1) รอให้เบราว์เซอร์โหลดหน้าเสร็จ (document.readyState == complete)
            self.after(0, lambda: self.set_status("รอหน้าเว็บโหลดให้เสร็จก่อน..."))
            try:
                WebDriverWait(driver, 30).until(
                    lambda d: d.execute_script("return document.readyState") == "complete")
            except Exception:
                pass
            time.sleep(2)  # เผื่อเวลาให้ JS เรนเดอร์ฟอร์มล็อกอินจนครบ

            # 2) รอช่องรหัสผ่านปรากฏจริง (สูงสุด 30 วินาที)
            self.after(0, lambda: self.set_status("กำลังรอช่องล็อกอินปรากฏ..."))
            try:
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
            except Exception:
                return False  # รอจนหมดเวลายังไม่เจอช่องรหัสผ่าน

            pwd_fields = [e for e in driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if e.is_displayed()]
            if not pwd_fields:
                return False
            pwd_el = pwd_fields[0]

            # 3) หาช่องชื่อผู้ใช้ที่อยู่ใกล้กัน
            user_el = None
            for sel in ["input[name*='user' i]", "input[name*='login' i]", "input[id*='user' i]",
                        "input[name*='name' i]", "input[type='text']", "input[type='email']"]:
                visible = [e for e in driver.find_elements(By.CSS_SELECTOR, sel) if e.is_displayed()]
                if visible:
                    user_el = visible[0]
                    break
            if user_el is None:
                return False

            # 4) กรอกและส่งฟอร์ม
            self.after(0, lambda: self.set_status("กำลังกรอกรหัสและเข้าสู่ระบบ..."))
            user_el.clear(); user_el.send_keys(user)
            pwd_el.clear(); pwd_el.send_keys(pwd)
            pwd_el.send_keys(Keys.ENTER)

            # 5) รอจนช่องรหัสผ่านหายไป = เข้าระบบสำเร็จ (สูงสุด 25 วินาที)
            try:
                WebDriverWait(driver, 25).until_not(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
                return True
            except Exception:
                still = [e for e in driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if e.is_displayed()]
                return len(still) == 0
        except Exception:
            return False

    def _fill_cookie_from_driver(self, driver, quiet=False):
        """ส่ง cookie ทั้งชุดและ User-Agent จาก Chrome แล้วพิสูจน์ว่า login จริง"""
        try:
            current = driver.current_url or ""
            if "task=main/receive_mobile" not in current:
                driver.get(sppweb.NEWS_URL)
                try:
                    from selenium.webdriver.support.ui import WebDriverWait
                    WebDriverWait(driver, 30).until(
                        lambda d: d.execute_script("return document.readyState") == "complete")
                except Exception:
                    pass
            cookies = driver.get_cookies()
            user_agent = driver.execute_script("return navigator.userAgent")
            sess = sppweb.new_session(cookies=cookies, user_agent=user_agent)
            if not sppweb.is_logged_in(sess):
                if not quiet:
                    self.after(0, lambda: self.set_status(
                        "ยังไม่ได้ล็อกอินเว็บ สพป. — กรุณาล็อกอินใน Chrome ให้เรียบร้อย"))
                return False
            state = sppweb.export_session(sess)
            phpsessid = sppweb.get_cookie(sess)
        except sppweb.LoginError:
            return False
        except sppweb.SPPWebError as e:
            if not quiet:
                self.after(0, lambda e=e: self.set_status(f"ตรวจ session จาก Chrome ไม่สำเร็จ: {e}"))
            return False
        except Exception as e:
            if not quiet:
                self.after(0, lambda e=e: self.set_status(f"อ่าน session จาก Chrome ไม่สำเร็จ: {e}"))
            return False

        self.spp_session_state = state

        def _fill():
            self.cookie_entry.delete(0, tk.END)
            self.cookie_entry.insert(0, phpsessid or "session-from-chrome")
            self.set_status("ยืนยัน session จาก Chrome สำเร็จ กำลังตรวจข่าวอัตโนมัติ...")
            self.start_web_mode()

        self.after(0, _fill)
        return True

    def _set_cookie_worker(self, active):
        with self._cookie_lock:
            self._cookie_worker_active = bool(active)

    def _start_cookie_watcher(self, driver):
        """รอให้ฟอร์มรหัสผ่านหาย แล้วค่อยตรวจ session จริงจากหน้ารายการข่าว"""
        def _watch():
            try:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                WebDriverWait(driver, 30).until(
                    lambda d: d.execute_script("return document.readyState") == "complete")
            except Exception:
                pass
            self.after(0, lambda: self.set_status(
                "Chrome พร้อมแล้ว — ล็อกอินให้เรียบร้อย ระบบจะตรวจ session ให้อัตโนมัติ"))

            try:
                deadline = time.time() + 300
                last_attempt = 0.0
                while time.time() < deadline:
                    if getattr(self, 'cookie_driver', None) is not driver:
                        return
                    try:
                        visible_password = any(
                            element.is_displayed()
                            for element in driver.find_elements(By.CSS_SELECTOR, "input[type='password']"))
                    except Exception:
                        return
                    if not visible_password and time.time() - last_attempt >= 8:
                        last_attempt = time.time()
                        if self._fill_cookie_from_driver(driver, quiet=True):
                            try:
                                driver.quit()
                            except Exception:
                                pass
                            self.cookie_driver = None
                            return
                    time.sleep(2)
                self.after(0, lambda: self.set_status(
                    "ยังยืนยัน session ไม่สำเร็จภายใน 5 นาที — Chrome ยังเปิดอยู่ ล็อกอินแล้วกดดึง Cookie อีกครั้ง"))
            finally:
                self._set_cookie_worker(False)

        threading.Thread(target=_watch, daemon=True).start()

    def open_chrome_for_cookie(self):
        """เปิด Chrome เข้าเว็บ สพป. แล้วลองล็อกอินอัตโนมัติด้วยรหัสที่บันทึกไว้
        ถ้าเข้าไม่ได้ ค่อยให้ผู้ใช้ล็อกอินเอง"""
        with self._cookie_lock:
            if self._cookie_worker_active:
                self.set_status("กำลังเปิด/ตรวจ Chrome อยู่แล้ว กรุณาใช้หน้าต่างเดิม")
                return
            self._cookie_worker_active = True

        def _run():
            watcher_started = False
            driver = None
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
            except ImportError:
                self.after(0, lambda: messagebox.showwarning(
                    "ต้องติดตั้ง Selenium ก่อน",
                    "ยังไม่ได้ติดตั้งไลบรารี Selenium\n\nเปิด CMD แล้วพิมพ์คำสั่งนี้:\n\npip install selenium"))
                self.after(0, lambda: self.set_status("ยังไม่ได้ติดตั้ง Selenium (pip install selenium)"))
            else:
              try:
                existing = getattr(self, 'cookie_driver', None)
                if existing is not None:
                    try: existing.quit()
                    except: pass
                self.after(0, lambda: self.set_status("กำลังเปิด Chrome..."))
                opts = Options()
                opts.add_argument("--start-maximized")
                opts.add_experimental_option("excludeSwitches", ["enable-automation"])
                self.cookie_driver = webdriver.Chrome(options=opts)
                driver = self.cookie_driver
                try: driver.set_page_load_timeout(60)
                except: pass
                self.after(0, lambda: self.set_status("กำลังเปิดเว็บ สพป. รอหน้าโหลด..."))
                try:
                    driver.get(sppweb.BASE)
                except Exception as e:
                    raise RuntimeError(f"เปิดหน้าเว็บ สพป. ไม่สำเร็จ: {e}") from e

                cfg = load_config()
                user = cfg.get('login_user', '').strip()
                pwd = cfg.get('login_pass', '').strip()

                if user and pwd:
                    self.after(0, lambda: self.set_status("มีรหัสบันทึกไว้ — กำลังล็อกอินอัตโนมัติ..."))
                    success = self._selenium_auto_login(driver, user, pwd)
                    if success and self._fill_cookie_from_driver(driver):
                        try: driver.quit()
                        except: pass
                        self.cookie_driver = None
                    else:
                        self.after(0, lambda: self.set_status("ล็อกอินอัตโนมัติไม่สำเร็จ — กรุณาล็อกอินเองในหน้า Chrome (ระบบจะดึง Cookie ให้เองเมื่อเข้าระบบเสร็จ)"))
                        watcher_started = True
                        self._start_cookie_watcher(driver)
                else:
                    self.after(0, lambda: self.set_status("ไม่มีรหัสบันทึกไว้ — ล็อกอินในหน้า Chrome ได้เลย ระบบจะดึง Cookie ให้เองเมื่อเข้าระบบเสร็จ"))
                    watcher_started = True
                    self._start_cookie_watcher(driver)
              except Exception as e:
                if driver is not None:
                    try: driver.quit()
                    except Exception: pass
                self.cookie_driver = None
                self.after(0, lambda e=e: messagebox.showerror(
                    "เปิด Chrome ไม่สำเร็จ",
                    f"{e}\n\nตรวจสอบว่าได้ติดตั้ง Google Chrome ไว้แล้ว"))
                self.after(0, lambda e=e: self.set_status(f"เปิด Chrome ไม่สำเร็จ: {e}"))
            finally:
                if not watcher_started:
                    self._set_cookie_worker(False)

        threading.Thread(target=_run, daemon=True).start()

    def grab_cookie_from_chrome(self):
        """ดึง Cookie อัตโนมัติแบบปุ่มเดียว:
        - ถ้ายังไม่ได้เปิด Chrome → เปิด Chrome + ล็อกอินอัตโนมัติ + ดึง Cookie ให้เลย
        - ถ้ามี Chrome เปิดอยู่แล้ว (ล็อกอินเอง) → อ่าน Cookie จากหน้านั้น"""
        driver = getattr(self, 'cookie_driver', None)
        if driver is None:
            self.open_chrome_for_cookie()
            return
        with self._cookie_lock:
            if self._cookie_worker_active:
                self.set_status("ระบบกำลังตรวจ session จาก Chrome ให้อัตโนมัติอยู่แล้ว")
                return
            self._cookie_worker_active = True

        def _run():
            try:
                self.after(0, lambda: self.set_status("กำลังตรวจ session จาก Chrome..."))
                if self._fill_cookie_from_driver(driver):
                    try: driver.quit()
                    except: pass
                    self.cookie_driver = None
                else:
                    self.after(0, lambda: messagebox.showwarning(
                        "session ยังใช้ไม่ได้",
                        "กรุณาล็อกอินในหน้า Chrome ให้เรียบร้อย แล้วลองอีกครั้งครับ"))
                    self.after(0, lambda: self.set_status("session ยังใช้ไม่ได้ — กรุณาล็อกอินก่อน"))
            except Exception as e:
                self.after(0, lambda e=e: messagebox.showerror(
                    "ตรวจ session ไม่สำเร็จ",
                    f"{e}\n\n(หน้าต่าง Chrome อาจถูกปิดไปแล้ว ลองเปิดใหม่อีกครั้ง)"))
                self.after(0, lambda e=e: self.set_status(f"ตรวจ session ไม่สำเร็จ: {e}"))
            finally:
                self._set_cookie_worker(False)

        threading.Thread(target=_run, daemon=True).start()

    def start_local_mode(self):
        filepath = filedialog.askopenfilename(filetypes=[("Documents", "*.pdf *.jpg *.jpeg *.png")])
        if not filepath:
            return
        job = self._claim_job("local")
        if job is None:
            messagebox.showwarning("มีงานกำลังทำอยู่", "กรุณาปิดหรือทำเอกสารปัจจุบันให้เสร็จก่อนครับ")
            return
        self.set_status("กำลังส่งให้ AI วิเคราะห์...")
        threading.Thread(target=self._run_local_job, args=(job, filepath), daemon=True).start()

    def _run_local_job(self, job, filepath):
        handed_to_preview = False
        try:
            handed_to_preview = self.process_file_thread(
                filepath, "-", "-", "-", "-", "🔵",
                "📥 นำเข้าไฟล์โดยผู้ใช้งาน (Manual Import)", job=job, book_id=None)
        except Exception as e:
            self.after(0, lambda e=e: self.log(f"❌ เปิดเอกสารไม่สำเร็จ: {type(e).__name__}: {e}"))
            self.after(0, lambda e=e: self.set_status(f"เปิดเอกสารไม่สำเร็จ: {e}"))
            self.after(0, lambda e=e: messagebox.showerror("เปิดเอกสารไม่สำเร็จ", str(e)))
        finally:
            if not handed_to_preview:
                self._finish_job(job)

    # ==========================================
    # ๖.๑ โหมดลงตรายางเลขรับบนกระดาษ A4 เปล่า
    #     (ไม่เกษียณ ไม่เรียก AI ไม่ส่ง LINE — ไว้ปริ้นทับเอกสารที่ส่งมาเป็นกระดาษ)
    # ==========================================
    # ขนาดกระดาษ A4 ที่ ๒๐๐ DPI = ๑๖๕๔ x ๒๓๓๙ px
    # เซฟ PDF ด้วย resolution เดียวกันนี้ หน้ากระดาษใน PDF จะได้เป็น A4 พอดีเป๊ะ
    # (สำคัญมาก — ปริ้นที่ ๑๐๐% แล้วตรายางจะลงตรงตำแหน่งเดียวกับที่เห็นในหน้าจอ)
    A4_DPI = 200
    A4_W, A4_H = 1654, 2339

    def start_stamp_only_mode(self):
        """เปิดกระดาษ A4 เปล่า → รันเลขรับต่อจาก Excel → ลากวางตรายาง → เซฟ PDF ไว้ปริ้นทับ"""
        job = self._claim_job("stamp")
        if job is None:
            messagebox.showwarning("มีงานกำลังทำอยู่", "กรุณาปิดหรือทำเอกสารปัจจุบันให้เสร็จก่อนครับ")
            return
        self.stamp_job = job
        self.log("โหมดลงตรายางเลขรับ: กำลังเตรียมกระดาษ A4 เปล่า...")
        self.set_status("กำลังเตรียมกระดาษเปล่าสำหรับลงตรายาง...")
        threading.Thread(target=self.stamp_only_thread, args=(job,), daemon=True).start()

    def stamp_only_thread(self, job):
        try:
            receipt_no_thai = get_next_receipt_no()
            self.stamp_only_info = {'receipt_no': receipt_no_thai, 'job': job}
            self.after(0, lambda: self.log(f"เลขรับที่จะลง: {receipt_no_thai} (รันต่อจากทะเบียน Excel)"))
            self.after(0, self.show_stamp_only_window)
        except Exception as e:
            self.after(0, lambda e=e: messagebox.showerror("เตรียมกระดาษไม่สำเร็จ", str(e)))
            self.after(0, lambda e=e: self.set_status(f"เตรียมกระดาษไม่สำเร็จ: {e}"))
            self._finish_job(job)

    def show_stamp_only_window(self):
        self.set_status("ลากตรายางไปวางตำแหน่งที่ต้องการ แล้วกดบันทึก")
        win = tk.Toplevel(self)
        self.stamp_only_win = win
        win.protocol("WM_DELETE_WINDOW", self.cancel_stamp_only)
        win.title(f"ลงตรายางเลขรับ {self.stamp_only_info['receipt_no']} บนกระดาษ A4 เปล่า — ไว้ปริ้นทับเอกสารกระดาษ")
        win.geometry("1000x800")
        try:
            win.state('zoomed')
        except Exception:
            pass

        self.stamp_pct = tk.IntVar(value=100)
        self.zoom_pct = tk.IntVar(value=30)   # ย่อให้เห็นกระดาษ A4 ทั้งแผ่น จะได้กะตำแหน่งถูก
        self.kasien_boxes = []          # โหมดนี้ไม่มีคำเกษียณ แต่ตัวลากใช้ตัวแปรนี้ร่วมกัน
        self.drag_data = {"item": None, "x": 0, "y": 0}

        left = tk.Frame(win)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(win, padx=8, pady=8, bg="#f9f9f9")
        right.pack(side="right", fill="y")

        vbar = tk.Scrollbar(left, orient="vertical")
        hbar = tk.Scrollbar(left, orient="horizontal")
        self.canvas = tk.Canvas(left, bg="#707070", yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        vbar.config(command=self.canvas.yview); hbar.config(command=self.canvas.xview)
        vbar.pack(side="right", fill="y"); hbar.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.bg_item = self.canvas.create_image(0, 0, anchor="nw")
        self.stamp_item = self.canvas.create_image(0, 0, anchor="nw", tags="draggable")
        self.canvas.tag_bind("draggable", "<ButtonPress-1>", self.on_drag_start)
        self.canvas.tag_bind("draggable", "<B1-Motion>", self.on_drag_motion)
        self.canvas.tag_bind("draggable", "<ButtonRelease-1>", lambda e: self._update_pos_readout())

        # ปุ่มล่างปักไว้ "ก่อน" ด้วย side=bottom → ติดขอบล่างเสมอ
        # ไม่ว่าเนื้อหาข้างบนจะยาวแค่ไหนหรือจอจะเตี้ยแค่ไหน ปุ่มบันทึกจะไม่หลุดจอ
        tk.Button(right, text="ยกเลิก", font=("Helvetica", 9), bg="#e0e0e0",
                  command=self.cancel_stamp_only).pack(side="bottom", fill="x")
        tk.Button(right, text="✅ ลงตรายาง & บันทึก", font=("Helvetica", 13, "bold"),
                  bg="#4CAF50", fg="white", height=2, cursor="hand2",
                  command=self.finalize_stamp_only).pack(side="bottom", fill="x", pady=(8, 4))

        # ส่วนกลางใส่ใน canvas ที่เลื่อนได้ เผื่อจอเตี้ยจนเนื้อหาลงไม่หมด
        mid = tk.Canvas(right, bg="#f9f9f9", highlightthickness=0, width=232)
        mid_bar = tk.Scrollbar(right, orient="vertical", command=mid.yview)
        mid.configure(yscrollcommand=mid_bar.set)
        mid_bar.pack(side="right", fill="y")
        mid.pack(side="left", fill="both", expand=True)
        panel = tk.Frame(mid, bg="#f9f9f9")
        mid.create_window((0, 0), window=panel, anchor="nw", width=232)
        panel.bind("<Configure>", lambda e: mid.configure(scrollregion=mid.bbox("all")))
        def _wheel(e):
            # หมุนล้อเมาส์ให้เลื่อนแผงขวา เฉพาะตอนเมาส์อยู่ฝั่งขวาเท่านั้น
            if e.x_root >= right.winfo_rootx():
                mid.yview_scroll(-1 * (e.delta // 120), "units")
        win.bind("<MouseWheel>", _wheel)   # ผูกกับหน้าต่างนี้ ปิดหน้าต่างแล้วหายไปเอง

        head = tk.Frame(panel, bg="#f9f9f9")
        head.pack(fill="x")
        tk.Label(head, text="เลขรับที่จะลง", font=("Helvetica", 9), bg="#f9f9f9", fg="#666").pack(side="left")
        tk.Label(head, text=self.stamp_only_info['receipt_no'], font=("Helvetica", 20, "bold"),
                 bg="#f9f9f9", fg="#9C27B0").pack(side="left", padx=(6, 0))
        tk.Label(panel, text="กระดาษ A4 เปล่า ไว้ปริ้นทับเอกสารกระดาษ", font=("Helvetica", 8),
                 bg="#f9f9f9", fg="#999", wraplength=228, justify="left").pack(anchor="w", pady=(0, 6))

        # --- วันที่/เวลาบนตรายาง: เติมของจริงมาให้ก่อน แก้ได้ถ้าต้องลงรับย้อนหลัง ---
        st = tk.LabelFrame(panel, text=" 🖋 วัน-เวลาบนตรายาง ", font=("Helvetica", 8),
                           bg="#f9f9f9", fg="#555", padx=5, pady=4)
        st.pack(fill="x", pady=(0, 6))
        tk.Label(st, text="วันที่:", bg="#f9f9f9", font=("Helvetica", 8), anchor="w").pack(fill="x")
        self.so_date_entry = tk.Entry(st, font=("Helvetica", 10))
        self.so_date_entry.insert(0, get_thai_date())
        self.so_date_entry.pack(fill="x")
        tk.Label(st, text="พิมพ์ 15/7/2569 ก็ได้ เดี๋ยวแปลงให้", bg="#f9f9f9", fg="#aaa",
                 font=("Helvetica", 7), anchor="w").pack(fill="x", pady=(0, 3))
        tk.Label(st, text="เวลา:", bg="#f9f9f9", font=("Helvetica", 8), anchor="w").pack(fill="x")
        self.so_time_entry = tk.Entry(st, font=("Helvetica", 10))
        self.so_time_entry.insert(0, get_thai_time_rounded())
        self.so_time_entry.pack(fill="x")
        tk.Button(st, text="↺ ใช้วัน-เวลาจริงตอนนี้", font=("Helvetica", 8), bg="#e0e0e0",
                  command=self.reset_stamp_datetime).pack(fill="x", pady=(4, 0))
        for e in (self.so_date_entry, self.so_time_entry):
            e.bind("<KeyRelease>", self.update_stamp_only_render)

        # --- ช่องพิมพ์ข้อมูลลงทะเบียน (พิมพ์ตรงนี้ได้เลย ไม่ต้องไปพิมพ์ใน Excel) ---
        reg = tk.LabelFrame(panel, text=" 📝 ข้อมูลลงทะเบียน (ไม่พิมพ์ก็ได้) ", font=("Helvetica", 8),
                            bg="#f9f9f9", fg="#555", padx=5, pady=4)
        reg.pack(fill="x", pady=(0, 6))
        self.so_fields = {}
        for key, label in [('doc_no', "ที่:"), ('doc_date', "ลงวันที่:"),
                           ('sender', "จาก:"), ('doc_title', "เรื่อง:")]:
            tk.Label(reg, text=label, bg="#f9f9f9", font=("Helvetica", 8), anchor="w").pack(fill="x")
            ent = tk.Entry(reg, font=("Helvetica", 10))
            ent.pack(fill="x", pady=(0, 3))
            self.so_fields[key] = ent
        tk.Label(reg, text="ลงวันที่พิมพ์ 15/7/2569 ก็ได้", bg="#f9f9f9", fg="#aaa",
                 font=("Helvetica", 7), anchor="w").pack(fill="x")

        self._box_adjuster(panel, "ขนาดตรายาง %:", self.stamp_pct, 5, self.update_stamp_only_render, lo=20, hi=300)
        self._box_adjuster(panel, "ซูมมุมมอง %:", self.zoom_pct, 10, self.update_stamp_only_render, lo=20, hi=200)

        # ระยะห่างจากขอบกระดาษจริง — ใช้เทียบกับกระดาษที่จะเอาไปปริ้นทับ
        self.so_pos_lbl = tk.Label(panel, text="", font=("Helvetica", 9, "bold"),
                                   bg="#f9f9f9", fg="#00695C", justify="left", wraplength=228)
        self.so_pos_lbl.pack(anchor="w", pady=(6, 0))
        tk.Button(panel, text="↺ ตำแหน่งมาตรฐาน (มุมขวาบน)", font=("Helvetica", 8), bg="#e0e0e0",
                  command=self.reset_stamp_position).pack(fill="x", pady=(3, 0))
        tk.Label(panel, text="✨ ลากตรายางวางตรงไหนก็ได้ โปรแกรมจำให้ครั้งหน้า",
                 fg="#e91e63", bg="#f9f9f9", font=("Helvetica", 8), wraplength=228,
                 justify="left").pack(anchor="w", pady=(4, 2))

        self._build_stamp_only_bg()
        self.update_stamp_only_render()

    def _stamp_margins_cm(self):
        """ระยะห่างของตรายางจากขอบกระดาษจริง (ซ้าย, บน, ขวา) หน่วยเซนติเมตร"""
        D = self.A4_DPI
        left = (self.stamp_orig_x - self.so_left) / D * 2.54
        top = (self.stamp_orig_y - self.so_top) / D * 2.54
        w = render_transparent_stamp(self.stamp_only_info['receipt_no'], self.stamp_pct.get()).width
        right = (self.so_W - (self.stamp_orig_x - self.so_left) - w) / D * 2.54
        return left, top, right

    def _update_pos_readout(self):
        left, top, right = self._stamp_margins_cm()
        warn = "  ⚠️ ชิดขอบเกิน อาจโดนตัด" if (top < 0.8 or right < 0.5 or left < 0.5) else ""
        self.so_pos_lbl.config(text=f"ห่างขอบบน  {top:.1f} ซม.\nห่างขอบขวา {right:.1f} ซม.{warn}",
                               fg="#C62828" if warn else "#00695C")

    def reset_stamp_position(self):
        """กลับไปตำแหน่งมาตรฐานมุมขวาบน (เผื่อลากเพลินจนหลุด)"""
        D = self.A4_DPI
        w = render_transparent_stamp(self.stamp_only_info['receipt_no'], self.stamp_pct.get()).width
        self.stamp_orig_x = self.so_left + self.so_W - w - int(STAMP_DEFAULT_RIGHT_CM / 2.54 * D)
        self.stamp_orig_y = self.so_top + int(STAMP_DEFAULT_TOP_CM / 2.54 * D)
        self.update_stamp_only_render()

    def _build_stamp_only_bg(self):
        """สร้างกระดาษ A4 เปล่าไว้เป็นพื้นหลัง พร้อมวางตรายางที่ตำแหน่งมาตรฐาน (มุมขวาบน)"""
        self.so_W, self.so_H = self.A4_W, self.A4_H
        self.so_left, self.so_top = 20, 30

        bg = Image.new('RGBA', (self.so_W + 40, self.so_H + 60), '#b0b0b0')
        draw = ImageDraw.Draw(bg)
        draw.rectangle([25, self.so_top + 5, self.so_left + self.so_W + 5, self.so_top + self.so_H + 5], fill='#888888')
        draw.rectangle([self.so_left, self.so_top, self.so_left + self.so_W, self.so_top + self.so_H], fill='white')
        self.stamp_only_bg = bg

        # ตำแหน่งตรายาง: ใช้ของครั้งล่าสุดที่จำไว้ ถ้ายังไม่เคยใช้ก็วางมุมขวาบนตามค่ามาตรฐาน
        D = self.A4_DPI
        tmp = render_transparent_stamp(self.stamp_only_info['receipt_no'], 100)
        saved = load_stamp_pos()
        if saved:
            left_cm, top_cm, size_pct = saved
            self.stamp_pct.set(size_pct)
        else:
            left_cm = (self.so_W - tmp.width) / D * 2.54 - STAMP_DEFAULT_RIGHT_CM
            top_cm = STAMP_DEFAULT_TOP_CM
        sx = max(0, min(int(left_cm / 2.54 * D), self.so_W - tmp.width))   # กันหลุดขอบกระดาษ
        sy = max(0, min(int(top_cm / 2.54 * D), self.so_H - tmp.height))
        self.stamp_orig_x, self.stamp_orig_y = sx + self.so_left, sy + self.so_top

    def _stamp_datetime(self):
        """วัน-เวลาที่จะพิมพ์ลงตรายาง — เว้นว่างไว้ = ใช้ของจริงตอนนี้"""
        d = normalize_typed_date(self.so_date_entry.get()) or get_thai_date()
        t = to_thai_digits(self.so_time_entry.get().strip()) or get_thai_time_rounded()
        return d, t

    def reset_stamp_datetime(self):
        """ดึงวัน-เวลาจริงตอนนี้กลับมาใส่ช่อง"""
        self.so_date_entry.delete(0, tk.END); self.so_date_entry.insert(0, get_thai_date())
        self.so_time_entry.delete(0, tk.END); self.so_time_entry.insert(0, get_thai_time_rounded())
        self.update_stamp_only_render()

    def update_stamp_only_render(self, *_):
        scale = self.zoom_pct.get() / 100.0
        bg = self.stamp_only_bg
        self.bg_tk = ImageTk.PhotoImage(
            bg.resize((max(1, int(bg.width * scale)), max(1, int(bg.height * scale))), Image.Resampling.LANCZOS))
        self.canvas.itemconfig(self.bg_item, image=self.bg_tk)
        self.canvas.config(scrollregion=self.canvas.bbox(tk.ALL))

        d, t = self._stamp_datetime()
        stamp_rgba = render_transparent_stamp(self.stamp_only_info['receipt_no'], self.stamp_pct.get(), d, t)
        sw, sh = max(1, int(stamp_rgba.width * scale)), max(1, int(stamp_rgba.height * scale))
        self.stamp_tk = ImageTk.PhotoImage(stamp_rgba.resize((sw, sh), Image.Resampling.LANCZOS))
        self.canvas.itemconfig(self.stamp_item, image=self.stamp_tk)
        self.canvas.coords(self.stamp_item, self.stamp_orig_x * scale, self.stamp_orig_y * scale)
        self._update_pos_readout()

    def cancel_stamp_only(self):
        job = self.stamp_only_info.get('job') if hasattr(self, 'stamp_only_info') else None
        try: self.stamp_only_win.destroy()
        except Exception: pass
        self.set_status("ยกเลิกการลงตรายางเลขรับ (ยังไม่ได้ใช้เลขรับนี้)")
        self._finish_job(job)

    def finalize_stamp_only(self):
        # อ่านค่าจากช่องพิมพ์ก่อนปิดหน้าต่าง (ปิดแล้ว Entry จะหายไป)
        self.stamp_only_info['fields'] = {k: e.get() for k, e in self.so_fields.items()}
        self.stamp_only_info['stamp_date'], self.stamp_only_info['stamp_time'] = self._stamp_datetime()
        self.stamp_only_info['size_pct'] = self.stamp_pct.get()
        # จำตำแหน่งที่วางไว้ ครั้งหน้าเปิดมาจะอยู่ที่เดิม
        left_cm, top_cm, _ = self._stamp_margins_cm()
        save_stamp_pos(left_cm, top_cm, self.stamp_pct.get())
        self.stamp_only_win.destroy()
        self.set_status("กำลังลงตรายางและสร้างไฟล์...")
        threading.Thread(target=self.finalize_stamp_only_thread, daemon=True).start()

    def finalize_stamp_only_thread(self):
        info = self.stamp_only_info
        job = info.get('job')
        receipt_no = info['receipt_no']
        try:
            final_bg = self.stamp_only_bg.copy()
            stamp_rgba = render_transparent_stamp(receipt_no, info['size_pct'],
                                                  info['stamp_date'], info['stamp_time'])
            final_bg.paste(stamp_rgba, (int(self.stamp_orig_x), int(self.stamp_orig_y)), stamp_rgba)

            page = final_bg.crop((self.so_left, self.so_top, self.so_left + self.so_W, self.so_top + self.so_H))

            today_str = datetime.now().strftime("%Y-%m-%d")
            save_folder = os.path.join(OUTPUT_ROOT, today_str)
            if not os.path.exists(save_folder): os.makedirs(save_folder)

            f = info.get('fields', {})
            # ถ้าพิมพ์ "ที่" มา ใช้ตั้งชื่อไฟล์ให้ด้วย จะได้หาไฟล์ง่าย
            base = (f.get('doc_no') or "").strip().replace("/", "_").replace(":", "").replace("\\", "_").strip()
            name = f"เลขรับ_{receipt_no}_{base}.pdf" if base else f"เลขรับ_{receipt_no}.pdf"
            final_pdf_path = os.path.join(save_folder, name)

            # เซฟด้วย resolution = A4_DPI → หน้ากระดาษใน PDF เป็น A4 พอดี
            # ปริ้นที่ ๑๐๐% (ห้ามใช้ Fit to page) ตรายางจะลงตรงตำแหน่งที่เห็นในหน้าจอเป๊ะ
            page.convert('RGB').save(final_pdf_path, "PDF", resolution=float(self.A4_DPI))

            append_excel_receipt_only(receipt_no, f.get('doc_no', ''), f.get('doc_date', ''),
                                      f.get('sender', ''), f.get('doc_title', ''), info['stamp_date'])

            self.after(0, lambda: self.log(f"ลงเลขรับ {receipt_no} บนกระดาษเปล่า → {final_pdf_path}"))
            self.after(0, lambda: self.set_status(f"✅ ลงเลขรับ {receipt_no} เรียบร้อย (ลงทะเบียน Excel แล้ว)"))
            filled = [k for k in ('doc_no', 'doc_date', 'sender', 'doc_title') if (f.get(k) or "").strip()]
            note = "ลงทะเบียน Excel ครบตามที่พิมพ์แล้ว" if len(filled) == 4 else \
                   (f"ลงทะเบียน Excel แล้ว ({len(filled)}/๔ ช่อง) — ช่องที่ไม่ได้พิมพ์เว้นว่างไว้"
                    if filled else "ลงทะเบียน Excel แล้ว (เฉพาะเลขรับกับวันที่ลงรับ)")
            self.after(0, lambda: messagebox.showinfo(
                "สำเร็จ",
                f"ลงตรายางเลขรับ {receipt_no} บนกระดาษ A4 เปล่าแล้ว\n\nไฟล์: {final_pdf_path}\n\n{note}\n\n"
                f"⚠️ ตอนปริ้น ให้ตั้งขนาดเป็น 100% หรือ 'ขนาดจริง (Actual size)'\n"
                f"อย่าใช้ 'Fit to page' ไม่งั้นตรายางจะเลื่อนตำแหน่ง"))
        except Exception as e:
            self.after(0, lambda e=e: messagebox.showerror("ลงตรายางไม่สำเร็จ", str(e)))
            self.after(0, lambda e=e: self.set_status(f"ลงตรายางไม่สำเร็จ: {e}"))
        finally:
            self._finish_job(job)

    def start_web_mode(self):
        state = self.spp_session_state
        if state is None:
            cookie = self.cookie_entry.get().strip()
            if not cookie:
                messagebox.showwarning("แจ้งเตือน", "กรุณาล็อกอินผ่าน Chrome หรือวาง Cookie ก่อนครับ")
                return
            state = cookie
        job = self._claim_job("web")
        if job is None:
            messagebox.showwarning("มีงานกำลังทำอยู่", "กรุณาปิดหรือทำเอกสารปัจจุบันให้เสร็จก่อนครับ")
            return
        self.set_status("กำลังตรวจสอบหน้าเว็บ...")
        threading.Thread(
            target=self.web_scraping_thread, args=(state, job), daemon=True).start()

    def web_scraping_thread(self, session_state, job):
        """ดึงเว็บผ่าน sppweb ชุดเดียวกับเซิร์ฟเวอร์ ห้ามมี requests scraper ซ้ำที่นี่"""
        handed_to_preview = False
        try:
            sess = sppweb.new_session(session_state)
            sppweb.assert_authenticated(sess)
            self.after(0, lambda: self.set_status("กำลังค้นหาหนังสือใหม่จากหน้าล่าสุด..."))
            documents = sppweb.list_new_documents(sess, pages=8)
            self.spp_session_state = sppweb.export_session(sess)
            if not documents:
                self.after(0, lambda: self.set_status("ตรวจสอบเรียบร้อย ไม่มีหนังสือเข้าใหม่ครับ"))
                return

            # ทำเรื่องเก่าสุดที่ยังค้างก่อน เพื่อให้เลขรับเรียงตามลำดับ
            for doc in reversed(documents):
                book_id = doc["book_id"]
                self.after(0, lambda b=book_id, t=doc["doc_title"]:
                           self.log(f"พบเรื่องใหม่ (ID: {b}) {t[:40]}"))
                self.after(0, lambda b=book_id:
                           self.set_status(f"กำลังเปิดรายละเอียดหนังสือ ID {b}..."))
                detail = sppweb.fetch_detail(sess, book_id)
                if not detail["main_pdf"]:
                    self.after(0, lambda b=book_id:
                               self.log(f"⚠️ ID {b} ยังไม่มี PDF แนบ — ยังไม่บันทึกว่าข้าม"))
                    continue

                source_pdf = self._job_path(job, "source.pdf")
                self.after(0, lambda n=len(detail["attachments"]):
                           self.set_status(f"กำลังดาวน์โหลด PDF (ไฟล์แนบ {n} รายการ)..."))
                sppweb.download(sess, detail["main_pdf"], source_pdf, expected_type="pdf")
                self.spp_session_state = sppweb.export_session(sess)
                self.after(0, lambda: self.set_status("ดาวน์โหลดสำเร็จ กำลังส่งให้ AI..."))
                handed_to_preview = self.process_file_thread(
                    source_pdf,
                    doc["doc_no"], doc["doc_title"], doc["doc_date"], doc["sender"],
                    detail["emoji"], sppweb.attach_text(detail["attachments"]),
                    job=job, book_id=book_id,
                )
                return

            self.after(0, lambda: self.set_status(
                "พบหนังสือใหม่ แต่ยังไม่มีไฟล์ PDF แนบ กรุณาลองใหม่ภายหลัง"))
        except Exception as e:
            try:
                import traceback
                with open(_w("ai_error.log"), "a", encoding="utf-8") as logf:
                    logf.write("[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] ดึงข้อมูลเว็บ" + os.linesep)
                    logf.write(traceback.format_exc())
            except Exception:
                pass
            self.after(0, lambda e=e: self.log(f"❌ ดึงข้อมูลไม่สำเร็จ: {type(e).__name__}: {e}"))
            self.after(0, lambda e=e: self.set_status(f"❌ ดึงข้อมูลไม่สำเร็จ: {e}"))
        finally:
            if not handed_to_preview:
                self._finish_job(job)

    def _mark_history_skip(self, book_id=None):
        """บันทึกว่า "ข้าม" เรื่องนี้ ลงที่เก็บกลาง เพื่อไม่ให้ดึงซ้ำ
        (ใช้เฉพาะโหมดดึงจากเว็บ — โหมดนำเข้าไฟล์เองไม่มี book_id ก็ข้ามไป)"""
        if not book_id:
            return True
        try:
            import store
            store.get_store().mark_skipped(str(book_id))
            return True
        except Exception as e:
            self.after(0, lambda e=e: self.log(f"❌ บันทึกสถานะข้ามไม่สำเร็จ: {e}"))
            return False

    def _ask_register_decision(self, recipient_line, category, document_path):
        """เด้งหน้าต่างให้ผู้ใช้ตัดสินใจ (ทำงานข้ามเธรดอย่างปลอดภัย)
        คืนค่า 'ai' = ส่งให้ AI และลงรับต่อ, 'skip' = ไม่ลงรับ"""
        ev = threading.Event()
        result = {'choice': 'skip'}

        def _show():
            win = tk.Toplevel(self)
            win.title("ตรวจสอบก่อนลงรับ")
            win.configure(bg="#fff8e1")
            win.transient(self); win.grab_set()
            w, h = 580, 340
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            win.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}")

            tk.Label(win, text="⚠️ เอกสารนี้ต้องตรวจสอบก่อนลงรับ",
                     font=("Helvetica", 14, "bold"), bg="#fff8e1", fg="#e65100").pack(pady=(16, 8))
            if category == 'unknown':
                info = ("อ่านบรรทัด 'เรียน' ในเครื่องไม่ได้ (เอกสารอาจเป็นรูปสแกน)\n"
                        "กรุณากด 'ดูเอกสาร' เพื่ออ่านเอง แล้วตัดสินใจว่าจะลงรับหรือไม่")
            else:
                info = (f"เอกสารนี้เรียนถึง:\n\n{recipient_line}\n\n"
                        "ซึ่งอาจไม่ต้องลงรับ (เช่น เฉพาะโรงเรียนตามรายชื่อแนบท้าย)\n"
                        "ต้องการส่งให้ AI วิเคราะห์และลงรับหรือไม่?")
            tk.Label(win, text=info, font=("Helvetica", 11), bg="#fff8e1", fg="#333",
                     wraplength=540, justify="left").pack(pady=8, padx=20)

            def choose(c):
                result['choice'] = c
                try: win.destroy()
                except Exception: pass
                ev.set()

            def view_doc():
                try:
                    os.startfile(document_path)
                except Exception as e:
                    messagebox.showerror("เปิดเอกสารไม่ได้", str(e), parent=win)

            btnf = tk.Frame(win, bg="#fff8e1")
            btnf.pack(pady=18)
            tk.Button(btnf, text="👁 ดูเอกสาร", bg="#1976D2", fg="white",
                      command=view_doc).pack(side="left", padx=6)
            tk.Button(btnf, text="✅ ส่งให้ AI และลงรับ", bg="#4CAF50", fg="white",
                      font=("Helvetica", 11, "bold"), command=lambda: choose('ai')).pack(side="left", padx=6)
            tk.Button(btnf, text="🚫 ไม่ลงรับ", bg="#e53935", fg="white",
                      command=lambda: choose('skip')).pack(side="left", padx=6)
            win.protocol("WM_DELETE_WINDOW", lambda: choose('skip'))

        self.after(0, _show)
        ev.wait()
        return result['choice']

    def process_file_thread(self, filepath, doc_no, doc_title, doc_date, sender, emoji, attach,
                            *, job, book_id=None):
        ext = filepath.lower().split('.')[-1]
        temp_pdf = self._job_path(job, "temp_work.pdf")
        if ext in ['jpg', 'jpeg', 'png']: Image.open(filepath).convert('RGB').save(temp_pdf)
        else: shutil.copy(filepath, temp_pdf)

        # --- คัดกรองบรรทัด "เรียน" ในเครื่องก่อน (ฟรี ใช้ได้กับ PDF ที่มี text layer) ---
        self.after(0, lambda: self.set_status("กำลังตรวจบรรทัด 'เรียน' ในเครื่อง..."))
        recipient_line = extract_recipient_line(temp_pdf)
        category = classify_recipient(recipient_line)
        decided = False
        if category == 'check':
            # อ่านได้และเป็นกลุ่มเฉพาะ → ถาม "ก่อน" เพื่อไม่ให้เปลือง AI call
            self.after(0, lambda: self.set_status("เอกสารนี้ต้องตรวจสอบก่อนลงรับ — รอการยืนยัน..."))
            if self._ask_register_decision(recipient_line, 'check', temp_pdf) != 'ai':
                saved = self._mark_history_skip(book_id)
                msg = ("⏭ ไม่ลงรับเอกสารนี้ (บันทึกไว้แล้ว จะไม่ดึงซ้ำ)"
                       if book_id and saved else "⏭ ไม่ลงรับเอกสารนี้")
                self.after(0, lambda msg=msg: self.set_status(msg))
                return False
            decided = True
        elif category == 'auto':
            decided = True
        # category == 'unknown' (เอกสารสแกน อ่านในเครื่องไม่ได้) → ให้ AI ช่วยอ่านผู้รับแล้วค่อยตัดสิน

        self.after(0, lambda: self.set_status("กำลังส่งให้ AI วิเคราะห์..."))
        ai_text, sig_page, ai_no, ai_title, ai_date, ai_sender, ai_recipient = generate_kasien_text(temp_pdf)

        # --- เอกสารสแกน: ใช้บรรทัด "เรียน" ที่ AI อ่านมา มาจัดประเภท (ไม่เพิ่ม API call) ---
        if not decided:
            ai_rec = ai_recipient if (ai_recipient and ai_recipient != "-") else None
            if classify_recipient(ai_rec) != 'auto':
                self.after(0, lambda: self.set_status("เอกสารนี้อาจต้องตรวจก่อนลงรับ — รอการยืนยัน..."))
                if self._ask_register_decision(
                        ai_rec, classify_recipient(ai_rec), temp_pdf) != 'ai':
                    saved = self._mark_history_skip(book_id)
                    msg = ("⏭ ไม่ลงรับเอกสารนี้ (บันทึกไว้แล้ว จะไม่ดึงซ้ำ)"
                           if book_id and saved else "⏭ ไม่ลงรับเอกสารนี้")
                    self.after(0, lambda msg=msg: self.set_status(msg))
                    return False
        doc_no = doc_no if doc_no != "-" else ai_no
        doc_title = doc_title if doc_title != "-" else ai_title
        doc_date = doc_date if doc_date != "-" else ai_date
        sender = sender if sender != "-" else ai_sender
        
        try:
            reader = PdfReader(temp_pdf)
            total_pages = len(reader.pages)
            if sig_page > total_pages: sig_page = total_pages
        except: total_pages = 1

        receipt_no_thai = get_next_receipt_no()
        
        page1_path = self._job_path(job, "page1.jpg")
        page_sig_path = self._job_path(job, "page_sig.jpg")
        self.doc_info = {
            'pdf_path': temp_pdf, 'ai_text': ai_text, 'sig_page': sig_page, 'total_pages': total_pages,
            'doc_no': doc_no, 'doc_title': doc_title, 'doc_date': doc_date, 'sender': sender,
            'emoji': emoji, 'attach': attach, 'receipt_no': receipt_no_thai,
            'book_id': str(book_id) if book_id else None, 'job': job,
            'page1_path': page1_path, 'page_sig_path': page_sig_path,
        }
        
        images = convert_from_path(temp_pdf, first_page=1, last_page=1, poppler_path=POPPLER_PATH)
        images[0].save(page1_path, "JPEG")
        if sig_page > 1:
            img_sig = convert_from_path(temp_pdf, first_page=sig_page, last_page=sig_page, poppler_path=POPPLER_PATH)
            img_sig[0].save(page_sig_path, "JPEG")

        # ล้าง cache รูปต้นฉบับของเอกสารเดิม (เริ่มเอกสารใหม่)
        self._p1_orig_cache = None
        self._psig_orig_cache = None

        self.after(0, self.show_preview_window)
        return True

    # ==========================================
    # ๗. หน้าต่าง Live Editor (มุมมองแบบ MS Word Multi-page)
    # ==========================================
    def show_preview_window(self):
        self.set_status("เปิดหน้าต่างแก้ไขและปรับแต่ง...")
        self.preview_win = tk.Toplevel(self)
        self.preview_win.protocol("WM_DELETE_WINDOW", self.reject_document)
        self.preview_win.title("จัดหน้าเอกสาร (ลากย้ายอิสระ, ย่อ/ขยาย %, มุมมองแบ่งหน้า)")
        self.preview_win.geometry("1150x820")
        try:
            self.preview_win.state('zoomed')
        except Exception:
            try:
                self.preview_win.attributes('-zoomed', True)
            except Exception:
                sw = self.preview_win.winfo_screenwidth()
                sh = self.preview_win.winfo_screenheight()
                self.preview_win.geometry(f"{sw}x{sh}+0+0")
        
        self.doc_w_pct = tk.IntVar(value=100)       
        self.doc_h_pct = tk.IntVar(value=100)       
        self.doc_x_off = tk.IntVar(value=0)         
        self.doc_y_off = tk.IntVar(value=0)         
        
        self.stamp_pct = tk.IntVar(value=100)       
        
        self.zoom_pct = tk.IntVar(value=60)         
        self.editor_font_size = tk.IntVar(value=16) 
        
        self.kasien_boxes = []
        
        left_frame = tk.Frame(self.preview_win)
        left_frame.pack(side="left", fill="both", expand=True)
        right_frame = tk.Frame(self.preview_win, padx=15, pady=10, bg="#f9f9f9")
        right_frame.pack(side="right", fill="y")
        
        vbar = tk.Scrollbar(left_frame, orient="vertical")
        hbar = tk.Scrollbar(left_frame, orient="horizontal")
        self.canvas = tk.Canvas(left_frame, xscrollcommand=hbar.set, yscrollcommand=vbar.set, bg="#555555")
        vbar.config(command=self.canvas.yview)
        hbar.config(command=self.canvas.xview)
        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        
        self.bg_item = self.canvas.create_image(0, 0, anchor="nw")
        self.stamp_item = self.canvas.create_image(0, 0, anchor="nw", tags="draggable")
        
        tk.Label(right_frame, text="✏️ คำเกษียณ (เพิ่มได้หลายช่อง)", font=("Helvetica", 11, "bold"), bg="#f9f9f9").pack(anchor="w")
        tk.Button(right_frame, text="➕ เพิ่มช่องเกษียณ", bg="#673AB7", fg="white",
                  command=self.add_kasien_box).pack(fill="x", pady=(2, 6))
        
        kasien_outer = tk.Frame(right_frame, bg="#f9f9f9")
        kasien_outer.pack(fill="x")
        self.kasien_scroll = tk.Canvas(kasien_outer, bg="#f9f9f9", height=280, width=320, highlightthickness=0)
        ksb = tk.Scrollbar(kasien_outer, orient="vertical", command=self.kasien_scroll.yview)
        self.kasien_scroll.configure(yscrollcommand=ksb.set)
        ksb.pack(side="right", fill="y")
        self.kasien_scroll.pack(side="left", fill="both", expand=True)
        self.kasien_list_frame = tk.Frame(self.kasien_scroll, bg="#f9f9f9")
        self.kasien_scroll.create_window((0, 0), window=self.kasien_list_frame, anchor="nw")
        self.kasien_list_frame.bind("<Configure>", lambda e: self.kasien_scroll.configure(scrollregion=self.kasien_scroll.bbox("all")))
        
        def create_adjuster(parent, label_text, int_var, step=10, command=None):
            frm = tk.Frame(parent, bg="#f9f9f9")
            frm.pack(pady=1, fill="x")
            tk.Label(frm, text=label_text, bg="#f9f9f9", width=22, anchor="w").pack(side="left")
            tk.Button(frm, text=" - ", command=lambda: [int_var.set(max(10, int_var.get() - step)), command() if command else None]).pack(side="left")
            tk.Label(frm, textvariable=int_var, width=4, bg="#f9f9f9").pack(side="left")
            tk.Button(frm, text=" + ", command=lambda: [int_var.set(min(300, int_var.get() + step)), command() if command else None]).pack(side="left")

        def create_offset_adjuster(parent, label_text, int_var, neg_txt, pos_txt, step=2, lo=-90, hi=90, command=None):
            frm = tk.Frame(parent, bg="#f9f9f9")
            frm.pack(pady=1, fill="x")
            tk.Label(frm, text=label_text, bg="#f9f9f9", width=22, anchor="w").pack(side="left")
            tk.Button(frm, text=neg_txt, command=lambda: [int_var.set(max(lo, int_var.get() - step)), command() if command else None]).pack(side="left")
            tk.Label(frm, textvariable=int_var, width=4, bg="#f9f9f9").pack(side="left")
            tk.Button(frm, text=pos_txt, command=lambda: [int_var.set(min(hi, int_var.get() + step)), command() if command else None]).pack(side="left")

        tk.Label(right_frame, text="📄 ปรับขนาดเนื้อหาข่าว (กระดาษคง A4)", font=("Helvetica", 10, "bold"), fg="#1976D2", bg="#f9f9f9").pack(anchor="w", pady=(10,0))
        create_adjuster(right_frame, "ความกว้างเนื้อหา (X) %:", self.doc_w_pct, step=5, command=lambda: self.update_render(rebuild_bg=True))
        create_adjuster(right_frame, "ความสูงเนื้อหา (Y) %:", self.doc_h_pct, step=5, command=lambda: self.update_render(rebuild_bg=True))
        create_offset_adjuster(right_frame, "เลื่อน ซ้าย/ขวา (X) %:", self.doc_x_off, " ◀ ", " ▶ ", step=2, command=lambda: self.update_render(rebuild_bg=True))
        create_offset_adjuster(right_frame, "เลื่อน ขึ้น/ลง (Y) %:", self.doc_y_off, " ▲ ", " ▼ ", step=2, command=lambda: self.update_render(rebuild_bg=True))
        
        tk.Label(right_frame, text="📝 ปรับขนาดตรายาง", font=("Helvetica", 10, "bold"), fg="#388E3C", bg="#f9f9f9").pack(anchor="w", pady=(10,0))
        create_adjuster(right_frame, "ขนาดตรายาง %:", self.stamp_pct, step=5, command=self.update_render)
        
        tk.Label(right_frame, text="🔍 มุมมองโปรแกรม (View)", font=("Helvetica", 10, "bold"), fg="#E64A19", bg="#f9f9f9").pack(anchor="w", pady=(10,0))
        create_adjuster(right_frame, "ซูมเข้า/ออก (Zoom) %:", self.zoom_pct, step=10, command=self.update_render)
        create_adjuster(right_frame, "Aa ขนาดฟอนต์ช่องพิมพ์:", self.editor_font_size, step=2, command=self.update_editor_font)

        tk.Label(right_frame, text="\n✨ ลากตรายาง/คำเกษียณบนภาพได้อิสระ\nและลากข้ามหน้ากระดาษได้เลย!", fg="#e91e63", bg="#f9f9f9", font=("Helvetica", 9)).pack(pady=3)
        tk.Button(right_frame, text="✅ ยืนยันและสร้างไฟล์", font=("Helvetica", 12, "bold"), bg="#4CAF50", fg="white", height=2, command=self.finalize_document).pack(pady=(8, 4), fill="x")
        tk.Button(right_frame, text="🚫 ไม่รับเอกสารนี้", font=("Helvetica", 11, "bold"), bg="#e53935", fg="white", command=self.reject_document).pack(pady=(0, 8), fill="x")

        self.drag_data = {"x": 0, "y": 0, "item": None}
        self.canvas.tag_bind("draggable", "<ButtonPress-1>", self.on_drag_start)
        self.canvas.tag_bind("draggable", "<B1-Motion>", self.on_drag_motion)
        
        self.add_kasien_box(text=self.doc_info['ai_text'], render=False)
        self.is_first_load = True
        self.update_render(rebuild_bg=True)

    def _box_adjuster(self, parent, label, var, step, command, lo=10, hi=300):
        frm = tk.Frame(parent, bg="#f9f9f9")
        frm.pack(fill="x")
        tk.Label(frm, text=label, bg="#f9f9f9", width=12, anchor="w").pack(side="left")
        tk.Button(frm, text=" - ", command=lambda: [var.set(max(lo, var.get() - step)), command()]).pack(side="left")
        tk.Label(frm, textvariable=var, width=4, bg="#f9f9f9").pack(side="left")
        tk.Button(frm, text=" + ", command=lambda: [var.set(min(hi, var.get() + step)), command()]).pack(side="left")

    def add_kasien_box(self, text=None, render=True):
        n = len(self.kasien_boxes)
        if text is None:
            text = "เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ\n(พิมพ์ข้อความเกษียณเพิ่มเติม)\nเพื่อโปรดทราบ"
        box = {
            'text': text,
            'pct': tk.IntVar(value=100),
            'wrap_pct': tk.IntVar(value=100),
            'indent_pct': tk.IntVar(value=(100 if n == 0 else 0)),  
            'draw_bg': tk.BooleanVar(value=False),    
            'draw_border': tk.BooleanVar(value=False), 
            'x': 100.0, 'y': 200.0,
            'item': self.canvas.create_image(0, 0, anchor="nw", tags=("draggable", "kasien")),
            'tk': None,
        }
        if n > 0 and hasattr(self, 'W'):
            anchor = self.kasien_boxes[0]
            box['x'] = anchor['x'] + 40 * n
            box['y'] = anchor['y'] + 40 * n
        self.kasien_boxes.append(box)
        self._build_box_editor(box)
        self._refresh_box_titles()
        if render:
            self.update_render(rebuild_bg=False)

    def _build_box_editor(self, box):
        frm = tk.LabelFrame(self.kasien_list_frame, text=" ช่องเกษียณ ", bg="#f9f9f9", padx=4, pady=4)
        frm.pack(fill="x", pady=3, padx=2)
        box['frame'] = frm
        
        ed = tk.Text(frm, height=4, width=34, font=("TH Sarabun New", self.editor_font_size.get()))
        ed.pack()
        ed.insert(tk.END, box['text'])
        ed.bind("<KeyRelease>", lambda e, b=box: self.on_box_text_change(b))
        box['editor'] = ed
        
        chk_frm = tk.Frame(frm, bg="#f9f9f9")
        chk_frm.pack(fill="x", pady=3)
        tk.Checkbutton(chk_frm, text="พื้นหลังขาว", variable=box['draw_bg'], bg="#f9f9f9", command=lambda: self.update_render(False)).pack(side="left", padx=5)
        tk.Checkbutton(chk_frm, text="มีเส้นกรอบ", variable=box['draw_border'], bg="#f9f9f9", command=lambda: self.update_render(False)).pack(side="left")

        self._box_adjuster(frm, "ขนาดอักษร %", box['pct'], 10, self.update_render)
        self._box_adjuster(frm, "กว้างกรอบ %", box['wrap_pct'], 10, self.update_render)
        self._box_adjuster(frm, "ย่อหน้า %", box['indent_pct'], 10, self.update_render, lo=0, hi=400)
        
        del_btn = tk.Button(frm, text="🗑 ลบช่องนี้", fg="white", bg="#e53935", command=lambda b=box: self.remove_kasien_box(b))
        del_btn.pack(pady=(3, 0), fill="x")
        box['del_btn'] = del_btn

    def on_box_text_change(self, box):
        box['text'] = box['editor'].get("1.0", tk.END).strip()
        if self.kasien_boxes and box is self.kasien_boxes[0]:
            self.doc_info['ai_text'] = box['text']   
        self.update_render(rebuild_bg=False)

    def remove_kasien_box(self, box):
        if len(self.kasien_boxes) <= 1:
            messagebox.showinfo("แจ้งเตือน", "ต้องมีช่องเกษียณอย่างน้อย ๑ ช่องครับ")
            return
        try: self.canvas.delete(box['item'])
        except: pass
        box['frame'].destroy()
        self.kasien_boxes.remove(box)
        self._refresh_box_titles()
        self.update_render(rebuild_bg=False)

    def _refresh_box_titles(self):
        for i, b in enumerate(self.kasien_boxes, start=1):
            if 'frame' in b:
                b['frame'].config(text=f" ช่องเกษียณ #{to_thai_digits(i)} ")
            if 'del_btn' in b:
                b['del_btn'].config(state="disabled" if len(self.kasien_boxes) == 1 else "normal")

    def update_editor_font(self):
        for b in self.kasien_boxes:
            if 'editor' in b:
                b['editor'].configure(font=("TH Sarabun New", self.editor_font_size.get()))

    def build_stitched_bg(self):
        scale_w = self.doc_w_pct.get() / 100.0
        scale_h = self.doc_h_pct.get() / 100.0
        off_x_pct = self.doc_x_off.get() / 100.0
        off_y_pct = self.doc_y_off.get() / 100.0
        
        if getattr(self, '_p1_orig_cache', None) is None:
            self._p1_orig_cache = Image.open(self.doc_info['page1_path']).convert('RGBA')
        p1_orig = self._p1_orig_cache
        paper1_w, paper1_h = p1_orig.width, p1_orig.height
        cw = max(1, int(paper1_w * scale_w))
        ch = max(1, int(paper1_h * scale_h))
        content1 = p1_orig.resize((cw, ch), Image.Resampling.LANCZOS) 
        p1 = Image.new('RGBA', (paper1_w, paper1_h), 'white')         
        ox1 = int(paper1_w * off_x_pct)
        oy1 = int(paper1_h * off_y_pct)
        p1.paste(content1, (ox1, oy1), content1)                      
        self.p1_h = paper1_h
        self.W = paper1_w
        
        psig = None
        self.psig_h = 0
        if self.doc_info['sig_page'] > 1:
            if getattr(self, '_psig_orig_cache', None) is None:
                self._psig_orig_cache = Image.open(self.doc_info['page_sig_path']).convert('RGBA')
            ps_orig = self._psig_orig_cache
            papers_w, papers_h = ps_orig.width, ps_orig.height
            cw2 = max(1, int(papers_w * scale_w))
            ch2 = max(1, int(papers_h * scale_h))
            content2 = ps_orig.resize((cw2, ch2), Image.Resampling.LANCZOS)
            psig = Image.new('RGBA', (papers_w, papers_h), 'white')
            ox2 = int(papers_w * off_x_pct)
            oy2 = int(papers_h * off_y_pct)
            psig.paste(content2, (ox2, oy2), content2)
            self.psig_h = papers_h
            self.W = max(self.W, papers_w)
            
        self.page_top = 70   
        self.gap = 70        
        self.blank_h = int(self.W * 1.414) 
        total_H = self.page_top + self.p1_h + self.gap + (self.psig_h + self.gap if self.psig_h else 0) + self.blank_h + 20
        
        stitched = Image.new('RGBA', (self.W + 40, total_H), '#b0b0b0')
        draw = ImageDraw.Draw(stitched)
        
        def draw_page_layer(y_offset, h):
            draw.rectangle([25, y_offset+5, 20+self.W+5, y_offset+h+5], fill='#888888') 
            draw.rectangle([20, y_offset, 20+self.W, y_offset+h], fill='white') 

        draw_page_layer(self.page_top, self.p1_h)
        stitched.paste(p1, (20, self.page_top), p1)
        
        curr_y = self.page_top + self.p1_h + self.gap
        if psig:
            draw_page_layer(curr_y, self.psig_h)
            stitched.paste(psig, (20, curr_y), psig)
            curr_y += self.psig_h + self.gap
            
        self.blank_start_y = curr_y
        draw_page_layer(self.blank_start_y, self.blank_h)
        
        self.original_bg = stitched
        
        if self.is_first_load:
            self.is_first_load = False
            
            tmp_stamp = render_transparent_stamp(self.doc_info['receipt_no'], 100)
            p1_region = self.original_bg.crop((20, self.page_top, 20 + self.W, self.page_top + self.p1_h))
            sx, sy = find_stamp_pos(p1_region, tmp_stamp.width, tmp_stamp.height)
            self.stamp_orig_x, self.stamp_orig_y = sx + 20, sy + self.page_top
            
            tmp_kasien = render_transparent_kasien(self.kasien_boxes[0]['text'], int(self.W * 0.42), 100)
            if self.doc_info['sig_page'] == 1:
                s_y, e_y = self.page_top + int(self.p1_h * 0.40), self.page_top + int(self.p1_h * 0.95)
            else:
                s_y = self.page_top + self.p1_h + self.gap + int(self.psig_h * 0.15)
                e_y = self.page_top + self.p1_h + self.gap + int(self.psig_h * 0.95)
            left_x = 20 + int(self.W * 0.08)
            kx, ky = find_kasien_pos(self.original_bg, tmp_kasien.width, tmp_kasien.height, s_y, e_y, left_x)
            self.kasien_boxes[0]['x'], self.kasien_boxes[0]['y'] = kx, ky

    def update_render(self, rebuild_bg=False):
        if rebuild_bg:
            self.build_stitched_bg()
            
        scale = self.zoom_pct.get() / 100.0
        
        disp_w, disp_h = int(self.original_bg.width * scale), int(self.original_bg.height * scale)
        bg_resized = self.original_bg.resize((disp_w, disp_h), Image.Resampling.LANCZOS)
        self.bg_tk = ImageTk.PhotoImage(bg_resized)
        self.canvas.itemconfig(self.bg_item, image=self.bg_tk)
        self.canvas.config(scrollregion=self.canvas.bbox(tk.ALL))
        
        stamp_rgba = render_transparent_stamp(self.doc_info['receipt_no'], self.stamp_pct.get())
        sw, sh = int(stamp_rgba.width * scale), int(stamp_rgba.height * scale)
        self.stamp_tk = ImageTk.PhotoImage(stamp_rgba.resize((max(1, sw), max(1, sh)), Image.Resampling.LANCZOS))
        self.canvas.itemconfig(self.stamp_item, image=self.stamp_tk)
        self.canvas.coords(self.stamp_item, self.stamp_orig_x * scale, self.stamp_orig_y * scale)
        
        for box in self.kasien_boxes:
            wrap = box['wrap_pct'].get() / 100.0
            max_w_orig = max(10, int(self.W * 0.42 * wrap))
            kasien_rgba = render_transparent_kasien(box['text'], max_w_orig, box['pct'].get(), box['indent_pct'].get(), box['draw_bg'].get(), box['draw_border'].get())
            box['render_h'] = kasien_rgba.height  
            kw, kh = int(kasien_rgba.width * scale), int(kasien_rgba.height * scale)
            box['tk'] = ImageTk.PhotoImage(kasien_rgba.resize((max(1, kw), max(1, kh)), Image.Resampling.LANCZOS))
            self.canvas.itemconfig(box['item'], image=box['tk'])
            self.canvas.coords(box['item'], box['x'] * scale, box['y'] * scale)

    def on_drag_start(self, event):
        items = self.canvas.find_withtag("current")
        if not items or "draggable" not in self.canvas.gettags(items[0]): return
        self.drag_data["item"] = items[0]
        self.drag_data["x"] = event.x
        self.drag_data["y"] = event.y

    def on_drag_motion(self, event):
        if not self.drag_data["item"]: return
        dx, dy = event.x - self.drag_data["x"], event.y - self.drag_data["y"]
        self.canvas.move(self.drag_data["item"], dx, dy)
        self.drag_data["x"], self.drag_data["y"] = event.x, event.y
        
        scale = self.zoom_pct.get() / 100.0
        coords = self.canvas.coords(self.drag_data["item"])
        if self.drag_data["item"] == self.stamp_item:
            self.stamp_orig_x, self.stamp_orig_y = coords[0] / scale, coords[1] / scale
        else:
            for b in self.kasien_boxes:
                if b['item'] == self.drag_data["item"]:
                    b['x'], b['y'] = coords[0] / scale, coords[1] / scale
                    break

    def reject_document(self):
        """ไม่รับเอกสารนี้จากหน้ารีวิว — ปิดหน้าต่าง ไม่ลงทะเบียน ไม่ส่ง LINE
        และบันทึกลง history กันดึงซ้ำ"""
        doc = getattr(self, 'doc_info', {}) or {}
        book_id = doc.get('book_id')
        job = doc.get('job')
        repeat_note = "\n• จะไม่ดึงเรื่องนี้ขึ้นมาอีก" if book_id else ""
        if not messagebox.askyesno(
                "ยืนยันไม่รับเอกสาร",
                "ไม่รับเอกสารนี้ใช่ไหม?\n\n• จะไม่ลงทะเบียนรับ และไม่ส่งเข้า LINE" + repeat_note):
            return
        try:
            self.preview_win.destroy()
        except Exception:
            pass
        saved = self._mark_history_skip(book_id)
        if book_id and saved:
            self.set_status("🚫 ไม่รับเอกสารนี้ (บันทึกไว้แล้ว จะไม่ดึงซ้ำ)")
        elif book_id:
            self.set_status("⚠️ ไม่รับเอกสาร แต่บันทึกสถานะข้ามไม่สำเร็จ")
        else:
            self.set_status("🚫 ยกเลิกเอกสารนำเข้าแล้ว")
        self._finish_job(job)

    # ==========================================
    # ๘. ตัดแบ่งหน้าและสร้าง PDF (Exporting)
    # ==========================================
    def finalize_document(self):
        # อ่าน Tk variables บน UI thread ให้หมดก่อนส่งงานหนักไป background thread
        snapshot = {
            'doc': dict(self.doc_info),
            'background': self.original_bg.copy(),
            'stamp_pct': self.stamp_pct.get(),
            'stamp_x': self.stamp_orig_x,
            'stamp_y': self.stamp_orig_y,
            'boxes': [{
                'text': box['text'], 'x': box['x'], 'y': box['y'],
                'pct': box['pct'].get(), 'wrap_pct': box['wrap_pct'].get(),
                'indent_pct': box['indent_pct'].get(),
                'draw_bg': box['draw_bg'].get(),
                'draw_border': box['draw_border'].get(),
            } for box in self.kasien_boxes],
            'W': self.W, 'page_top': self.page_top, 'p1_h': self.p1_h,
            'psig_h': self.psig_h, 'gap': self.gap,
            'blank_start_y': self.blank_start_y, 'blank_h': self.blank_h,
        }
        self.preview_win.destroy()
        self.set_status("กำลังประมวลผลและสร้างไฟล์...")
        threading.Thread(target=self.finalize_thread, args=(snapshot,), daemon=True).start()

    def finalize_thread(self, snapshot):
        doc = snapshot['doc']
        job = doc.get('job')
        registered = False
        history_marked = False
        final_pdf_path = None
        try:
            # ถ้าจองเลขไม่ได้ ต้องหยุดทันที ห้ามใช้เลข preview ต่อ
            doc['receipt_no'] = register_document(
                doc['doc_no'], doc['doc_date'], doc['sender'], doc['doc_title'])
            registered = True

            # กันหนังสือเดิมถูกหยิบไปลงรับเลขใหม่ หากขั้นส่ง LINE/สร้างไฟล์ภายหลังพลาด
            if doc.get('book_id'):
                import store
                store.get_store().mark_registered(doc['book_id'], doc['receipt_no'])
                history_marked = True

            final_bg = snapshot['background'].copy()
            stamp_rgba = render_transparent_stamp(doc['receipt_no'], snapshot['stamp_pct'])
            final_bg.paste(
                stamp_rgba, (int(snapshot['stamp_x']), int(snapshot['stamp_y'])), stamp_rgba)

            has_blank_page = False
            for box in snapshot['boxes']:
                max_w_orig = max(10, int(snapshot['W'] * 0.42 * box['wrap_pct'] / 100.0))
                kasien_rgba = render_transparent_kasien(
                    box['text'], max_w_orig, box['pct'], box['indent_pct'],
                    box['draw_bg'], box['draw_border'])
                final_bg.paste(kasien_rgba, (int(box['x']), int(box['y'])), kasien_rgba)
                if box['y'] + kasien_rgba.height > snapshot['blank_start_y']:
                    has_blank_page = True

            p1_path = self._job_path(job, "p1.pdf")
            psig_path = self._job_path(job, "psig.pdf")
            blank_path = self._job_path(job, "blank.pdf")
            final_jpg = self._job_path(job, "page1_final.jpg")
            p1_crop = final_bg.crop((
                20, snapshot['page_top'], 20 + snapshot['W'],
                snapshot['page_top'] + snapshot['p1_h']))
            p1_crop.convert('RGB').save(p1_path, "PDF", resolution=100.0)

            if snapshot['psig_h'] > 0:
                psig_top = snapshot['page_top'] + snapshot['p1_h'] + snapshot['gap']
                psig_crop = final_bg.crop((
                    20, psig_top, 20 + snapshot['W'], psig_top + snapshot['psig_h']))
                psig_crop.convert('RGB').save(psig_path, "PDF", resolution=100.0)
            if has_blank_page:
                blank_crop = final_bg.crop((
                    20, snapshot['blank_start_y'], 20 + snapshot['W'],
                    snapshot['blank_start_y'] + snapshot['blank_h']))
                blank_crop.convert('RGB').save(blank_path, "PDF", resolution=100.0)

            merger = PdfMerger()
            try:
                sig_page, total = doc['sig_page'], doc['total_pages']
                merger.append(p1_path)
                if sig_page == 1:
                    if total > 1:
                        merger.append(doc['pdf_path'], pages=(1, total))
                else:
                    if sig_page > 2:
                        merger.append(doc['pdf_path'], pages=(1, sig_page - 1))
                    merger.append(psig_path)
                    if total > sig_page:
                        merger.append(doc['pdf_path'], pages=(sig_page, total))
                if has_blank_page:
                    merger.append(blank_path)

                today_str = datetime.now().strftime("%Y-%m-%d")
                save_folder = os.path.join(OUTPUT_ROOT, today_str)
                os.makedirs(save_folder, exist_ok=True)
                safe_name = re.sub(r'[<>:"/\\|?*]+', '_', str(doc['doc_no'])).strip(' ._')
                if not safe_name or safe_name == "-":
                    safe_name = "เอกสารนำเข้า"
                final_pdf_path = os.path.join(
                    save_folder, f"{safe_name}_รับ_{doc['receipt_no']}.pdf")
                merger.write(final_pdf_path)
            finally:
                merger.close()

            # LINE เป็นงานเสริม ความล้มเหลวตรงนี้ต้องไม่ย้อนสถานะทะเบียน/ไฟล์
            try:
                p1_crop.convert('RGB').save(final_jpg, "JPEG")
                img_url = upload_to_imgbb(final_jpg)
                kasien_parts = [
                    " ".join(box['text'].split()) for box in snapshot['boxes'][:2]
                    if box['text'].strip()]
                ai_text_line = " | ".join(kasien_parts)
                doc_date = doc['doc_date']
                formatted_line_date = (
                    format_scraped_date(doc_date)
                    if "ม.ค." not in doc_date and "ก.พ." not in doc_date and doc_date != "-"
                    else to_thai_digits(doc_date))
                msg = (
                    f"📌 เลขที่รับ {doc['receipt_no']}\n"
                    f"{doc['emoji']} {to_thai_digits(doc['doc_no'])}\n"
                    f"🆕เรื่อง: {doc['doc_title']}\n"
                    f"🌟หนังสือลงวันที่ : {formatted_line_date}\n"
                    f"⚠️คำเกษียนหนังสือ:{ai_text_line}\n"
                    f"{doc['attach']}")
                if send_line_with_image(msg, img_url):
                    self.after(0, lambda: self.log("ส่งเข้ากลุ่ม LINE แล้ว"))
                else:
                    self.after(0, lambda: self.log(
                        "⚠️ ส่ง LINE ไม่สำเร็จ (PDF และทะเบียนบันทึกแล้ว)"))
            except Exception as line_error:
                self.after(0, lambda e=line_error: self.log(
                    f"⚠️ ส่ง LINE ไม่สำเร็จ แต่ PDF และทะเบียนบันทึกแล้ว: {e}"))

            self.after(0, lambda p=final_pdf_path:
                       self.set_status(f"✅ เสร็จสิ้น บันทึกไฟล์: {p}"))
            self.after(0, lambda p=final_pdf_path, n=doc['receipt_no']:
                       messagebox.showinfo("สำเร็จ", f"ลงทะเบียนเลขรับ {n} และบันทึกไฟล์แล้ว\n\n{p}"))
        except Exception as e:
            stage = (
                f"ลงทะเบียนเลขรับ {doc.get('receipt_no')} แล้ว แต่ขั้นตอนถัดไปไม่สำเร็จ"
                if registered else "ลงทะเบียนไม่สำเร็จ จึงยังไม่ได้สร้างหรือส่งเอกสาร")
            if registered and doc.get('book_id') and not history_marked:
                try:
                    import store
                    store.get_store().mark_registered(doc['book_id'], doc['receipt_no'])
                    history_marked = True
                except Exception:
                    pass
            self.after(0, lambda e=e, stage=stage:
                       self.log(f"❌ {stage}: {type(e).__name__}: {e}"))
            self.after(0, lambda e=e, stage=stage:
                       self.set_status(f"❌ {stage}: {e}"))
            self.after(0, lambda e=e, stage=stage:
                       messagebox.showerror("สร้างเอกสารไม่สำเร็จ", f"{stage}\n\n{e}"))
        finally:
            self._finish_job(job)

if __name__ == '__main__':
    app = SarabanApp()
    app.mainloop()
