import fitz  # PyMuPDF
import os

def pdf_to_jpeg(pdf_path):
    # ดึงชื่อไฟล์และที่อยู่เพื่อใช้สร้างโฟลเดอร์เก็บรูป
    base_name = os.path.basename(pdf_path)
    file_name_without_ext = os.path.splitext(base_name)[0]
    dir_name = os.path.dirname(pdf_path)
    
    # สร้างโฟลเดอร์ใหม่ชื่อเดียวกับไฟล์ PDF 
    output_folder = os.path.join(dir_name, f"{file_name_without_ext}_images")

    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    try:
        pdf_document = fitz.open(pdf_path)
        print(f"\nกำลังแปลงไฟล์: {base_name} (ทั้งหมด {len(pdf_document)} หน้า)...")
        
        for page_number in range(len(pdf_document)):
            page = pdf_document.load_page(page_number)
            
            # ซูม 2 เท่าเพื่อให้ภาพคมชัด
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            
            output_file = os.path.join(output_folder, f"page_{page_number + 1}.jpg")
            pix.save(output_file)
            print(f" - บันทึกหน้า {page_number + 1} เรียบร้อย")
            
        pdf_document.close()
        print(f"\nแปลงไฟล์เสร็จสิ้น! สามารถดูรูปภาพได้ที่โฟลเดอร์:\n{output_folder}")
        
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการเปิดหรือแปลงไฟล์: {e}")

if __name__ == "__main__":
    print("=== โปรแกรมแปลง PDF เป็น JPEG ===")
    
    # รอรับไฟล์จากการลากวางใน CMD
    user_input = input("กรุณาลากไฟล์ PDF มาวางที่นี่ แล้วกด Enter: ")
    
    # ลบเครื่องหมายคำพูดและช่องว่างที่อาจติดมาด้วย
    pdf_path = user_input.strip('"').strip("'").strip()
    
    # ตรวจสอบว่าไฟล์มีอยู่จริงและเป็นไฟล์ PDF หรือไม่
    if os.path.exists(pdf_path) and pdf_path.lower().endswith('.pdf'):
        pdf_to_jpeg(pdf_path)
    else:
        print("\nข้อผิดพลาด: ไม่พบไฟล์ดังกล่าว หรือไฟล์ที่ลากมาไม่ใช่ไฟล์ PDF")
        
    # หยุดหน้าจอไว้ไม่ให้ปิดทันทีเมื่อแปลงเสร็จ
    input("\nกด Enter เพื่อปิดโปรแกรม...")