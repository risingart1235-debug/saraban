# -*- coding: utf-8 -*-
# bot3.pyw — ตัวเปิดโปรแกรมแบบไม่มีหน้าต่าง Console สีดำ
# ดับเบิลคลิกไฟล์นี้เพื่อเปิดโปรแกรม (Windows จะรันด้วย pythonw อัตโนมัติ = ไม่มีจอดำ)
import os
import runpy

_here = os.path.dirname(os.path.abspath(__file__))
runpy.run_path(os.path.join(_here, "bot3.py"), run_name="__main__")
