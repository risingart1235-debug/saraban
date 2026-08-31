#!/usr/bin/env python3
"""จัดแฟ้มใหม่.py — ย้ายโฟลเดอร์รายวันแบบเก่าเข้าโครงสร้าง ปี/เดือน/วัน

ของเดิม  แฟ้มเสนอ_ผอ/2026-08-28/ไฟล์.pdf        <- ปีละ ๒๐๐ กว่าโฟลเดอร์กองชั้นบนสุด
ของใหม่  แฟ้มเสนอ_ผอ/๒๕๖๙/๐๘ สิงหาคม/๒๘/ไฟล์.pdf  <- ชั้นบนสุดเหลือปีละ ๑ โฟลเดอร์

วิธีใช้ — ดูผลก่อนเสมอ ยังไม่ย้ายจริง:
    python จัดแฟ้มใหม่.py

ย้ายจริงเมื่อดูแล้วโอเค:
    python จัดแฟ้มใหม่.py --ทำจริง

ความปลอดภัย:
- ไม่ลบอะไรทั้งสิ้น ใช้การย้าย (rename) เท่านั้น
- ถ้าปลายทางมีไฟล์ชื่อซ้ำอยู่แล้ว จะข้ามและรายงาน ไม่เขียนทับ
- โฟลเดอร์ที่ไม่ใช่รูปแบบ YYYY-MM-DD จะไม่ถูกแตะ (เช่น .tmp.drivedownload ของ Drive)
- ไฟล์ที่อยู่ชั้นบนสุด (ทะเบียน, history) ไม่ถูกแตะ

ถ้าใช้ Google Drive for Desktop ซิงก์อยู่ ให้รอซิงก์เสร็จก่อนและหลังย้าย
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DAY_DIR = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def target_for(y: int, m: int, d: int) -> str:
    """ใช้ตัวเดียวกับที่โปรแกรมใช้ตอนบันทึกจริง จะได้ไม่มีทางหลุดจากกัน"""
    from datetime import datetime
    return core.day_folder(datetime(y, m, d, tzinfo=core.THAI_TZ))


def plan(root: str):
    """คืนรายการ (โฟลเดอร์เดิม, โฟลเดอร์ใหม่, จำนวนไฟล์) ที่จะย้าย"""
    jobs = []
    for name in sorted(os.listdir(root)):
        src = os.path.join(root, name)
        if not os.path.isdir(src):
            continue
        mt = DAY_DIR.match(name)
        if not mt:
            continue                      # ไม่ใช่โฟลเดอร์รายวัน ไม่แตะ
        y, m, d = (int(x) for x in mt.groups())
        jobs.append((name, target_for(y, m, d), len(os.listdir(src))))
    return jobs


def move_one(root: str, src_name: str, dst_rel: str) -> tuple[int, list]:
    """ย้ายไฟล์ทีละไฟล์ ไม่เขียนทับของเดิม คืน (ย้ายสำเร็จกี่ไฟล์, รายการที่ข้าม)"""
    src = os.path.join(root, src_name)
    dst = os.path.join(root, dst_rel)
    os.makedirs(dst, exist_ok=True)
    moved, skipped = 0, []
    for fn in sorted(os.listdir(src)):
        s, t = os.path.join(src, fn), os.path.join(dst, fn)
        if os.path.exists(t):
            skipped.append(fn)            # ชื่อซ้ำ — ไม่เขียนทับเด็ดขาด
            continue
        shutil.move(s, t)
        moved += 1
    # ลบโฟลเดอร์เดิมเฉพาะตอนว่างจริงๆ (rmdir พังเองถ้ายังมีของ = ปลอดภัย)
    try:
        os.rmdir(src)
    except OSError:
        pass
    return moved, skipped


def main():
    real = "--ทำจริง" in sys.argv or "--apply" in sys.argv
    root = core.OUTPUT_ROOT
    print("โฟลเดอร์ที่จัด:", root)
    if not os.path.isdir(root):
        print("‼️  ไม่พบโฟลเดอร์นี้")
        return

    jobs = plan(root)
    if not jobs:
        print("\n✅ ไม่มีโฟลเดอร์รายวันแบบเก่าเหลือแล้ว — จัดเรียบร้อยอยู่แล้ว")
        return

    print("\nจะย้าย %d โฟลเดอร์ (%d ไฟล์):\n" % (jobs and len(jobs), sum(j[2] for j in jobs)))
    for src, dst, n in jobs:
        print("   %-12s ->  %s   (%d ไฟล์)" % (src, dst, n))

    if not real:
        print("\n" + "-" * 60)
        print("นี่คือการดูผลล่วงหน้า ยังไม่ได้ย้ายอะไรเลย")
        print("ถ้าโอเคแล้วให้รัน:   python จัดแฟ้มใหม่.py --ทำจริง")
        return

    print("\nกำลังย้าย...")
    total, all_skipped = 0, []
    for src, dst, _ in jobs:
        moved, skipped = move_one(root, src, dst)
        total += moved
        all_skipped += [(src, f) for f in skipped]
        print("   %-12s ย้าย %d ไฟล์" % (src, moved))

    print("\n✅ ย้ายเสร็จ %d ไฟล์" % total)
    if all_skipped:
        print("\n⚠️  ข้าม %d ไฟล์เพราะปลายทางมีชื่อซ้ำ (ของเดิมยังอยู่ที่เดิม):" % len(all_skipped))
        for src, fn in all_skipped:
            print("     %s/%s" % (src, fn))


if __name__ == "__main__":
    main()
