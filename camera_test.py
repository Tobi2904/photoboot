import subprocess
import os

def capture_photo():
    # 1. Tạo folder lưu ảnh
    save_dir = "photos"
    os.makedirs(save_dir, exist_ok=True)
    
    # 2. Định dạng tên file theo thời gian để không bị ghi đè
    filename = os.path.join(save_dir, "snap_%Y%m%d_%H%M%S.jpg")
    
    print("📸 Đang lấy nét và chụp...")
    
    # 3. Lệnh gphoto2 (Lưu ý: Bạn có thể cần sudo nếu chưa cấu hình udev)
    command = [
        "sudo", "gphoto2", 
        "--capture-image-and-download", 
        "--filename", filename,
        "--force-overwrite"
    ]
    
    try:
        # Chạy lệnh và đợi kết quả
        subprocess.run(command, check=True)
        print(f"✅ Đã chụp thành công! Ảnh lưu tại: {save_dir}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Lỗi chụp ảnh: {e}")

if __name__ == "__main__":
    capture_photo()