"""Offline tests for the saraban project."""

# หลายเทสต์ใช้ patch.dict(sys.modules, ...) ซึ่งตอนจบจะลบโมดูลที่ถูก import ระหว่างเทสต์ทิ้ง
# numpy/cv2/onnxruntime (ตัวอ่าน OCR) import ซ้ำในโปรเซสเดียวไม่ได้ ("cannot load module more than once")
# จึงโหลดไว้ก่อนเทสต์ไหนจะเริ่ม — เครื่องที่ไม่ได้ลงไว้ข้ามไป เทสต์ OCR ข้ามเอง
try:
    import numpy  # noqa: F401
    import cv2  # noqa: F401
    import onnxruntime  # noqa: F401
except ImportError:
    pass
