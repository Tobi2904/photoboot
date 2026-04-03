"""
image_service.py — High-resolution image merging with Pillow.

Handles:
- Loading selected session photos.
- Resizing / cropping them to fit predefined frame layouts.
- Compositing with a transparent PNG frame overlay.
- Saving the final output in JPEG at maximum quality to preserve
  the Sony a6400 image fidelity.
"""

import logging
from pathlib import Path
from PIL import Image

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_DIR = BASE_DIR / "storage"
TEMP_SESSIONS_DIR = STORAGE_DIR / "temp_sessions" # chứa ảnh thô 
FINAL_OUTPUTS_DIR = STORAGE_DIR / "final_outputs" # chứa ảnh đã merge xong, sẵn sàng upload lên S3
FRAMES_DIR = BASE_DIR / "backend" / "frames"          # chứa các file PNG khung hình (nếu có)

# ------------------------FRAMES_DIR----------------------------------------------- #
#  Frame layout definitions                                                #
# ----------------------------------------------------------------------- #
# Each template maps to a canvas size (px) + a list of photo slots.
# Slots are (x, y, width, height) tuples on the canvas.
# Add more templates as the product evolves.

FRAME_TEMPLATES: dict[str, dict] = {
    "frame_2": {
        "display_name": "Cham Di San",
        "canvas_size": (3543, 5315),
        "slots": [
            (305, 420, 1374, 910),
            (305, 1515, 1374, 905),
            (303, 2614, 1375, 905),
            (305, 3713, 1371, 825),
        ],
        "frame_file": "Frame-2.png",
    },
    "frame_3": {
        "display_name": "Am Vang Bau Truc",
        "canvas_size": (3543, 5315),
        "slots": [
            (316, 440, 1355, 887),
            (316, 1530, 1355, 887),
            (316, 2630, 1359, 888),
            (316, 3728, 1354, 806),
        ],
        "frame_file": "Frame-3.png",
    },
}


class MergeError(Exception):
    """Raised when the merge operation fails."""


def _crop_to_fill(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """
    Giống thuộc tính CSS `object-fit: cover`.
    Cắt giữa ảnh, giữ nguyên tỉ lệ, không làm méo hình.
    """
    src_w, src_h = img.size
    # 1. Tính tỉ lệ scale sao cho ảnh vừa khít cạnh ngắn nhất của ô
    scale = max(target_w / src_w, target_h / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    # 2. Resize ảnh theo tỉ lệ mới (Dùng thuật toán LANCZOS để ảnh nét nhất)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    # 3. Tính toán để cắt lấy phần CHÍNH GIỮA ảnh (Center Crop)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    # 4. Trả về ảnh đã cắt xong
    return img.crop((left, top, left + target_w, top + target_h))


def merge_photos(
    session_id: str,
    selected_filenames: list[str],
    frame_template_id: str,
) -> dict:
    """
    Merge selected session photos into a single high-res JPEG.

    Parameters
    ----------
    session_id          : UUID4 string identifying the session.
    selected_filenames  : e.g. ["photo_1.jpg", "photo_3.jpg", "photo_5.jpg"]
    frame_template_id   : Key in FRAME_TEMPLATES dict.

    Returns
    -------
    dict  {"output_filename": "final_{session_id}.jpg",
           "output_path": "<absolute path>"}
    """
    # kiểm tra template có tồn tại không, nếu không có thì trả về lỗi
    template = FRAME_TEMPLATES.get(frame_template_id)
    if template is None:
        raise MergeError(
            f"Unknown frame template '{frame_template_id}'. "
            f"Available: {list(FRAME_TEMPLATES.keys())}"
        )
    # kiểm tra nếu số lượng ảnh lớn hơn slot của template thì trả về lỗi
    slots = template["slots"]
    if len(selected_filenames) > len(slots):
        raise MergeError(
            f"Template '{frame_template_id}' supports {len(slots)} photos, "
            f"but {len(selected_filenames)} were provided."
        )

    # Bảo mật để tránh path traversal, chỉ lấy phần cuối của session_id làm tên thư mục, sau đó ghép với TEMP_SESSIONS_DIR
    safe_id = Path(session_id).name
    session_dir = TEMP_SESSIONS_DIR / safe_id

    # kiểm tra hình trên ổ đĩa có tồn tại hay không 
    photo_paths: list[Path] = []
    for fname in selected_filenames:
        safe_fname = Path(fname).name
        p = session_dir / safe_fname
        if not p.is_file():
            raise MergeError(f"Photo not found: {p}")
        photo_paths.append(p)

    # --------------------------------------------------------------- #
    #  Build the composite canvas                                      #
    # --------------------------------------------------------------- #
    # tạo giấy trắng có kích thước bằng tample để chuẩn bị dán hình ảnh vào 
    canvas_w, canvas_h = template["canvas_size"]
    canvas = Image.new("RGB", (canvas_w, canvas_h), color=(255, 255, 255))


    # cắt và dán ảnh 
    for idx, photo_path in enumerate(photo_paths):
        # lấy thông tin của slot tương ứng với ảnh thứ idx trong template
        x, y, slot_w, slot_h = slots[idx]
        # load ảnh lên Ram và cắt theo tỉ lệ để vừa khít với slot, và chuyển ảnh sang bộ màu RGB (để đảm bảo tương thích khi dán lên canvas)
        img = Image.open(photo_path).convert("RGB")
        # cắt ảnh khít với ô mà không làm méo hình
        img = _crop_to_fill(img, slot_w, slot_h)
        # dán ảnh đã cắt vào canvas tại vị trí (x, y)
        canvas.paste(img, (x, y))
        logger.debug("Placed %s at slot %d", photo_path.name, idx)

    # dán khung hình lên layer trên (frame overlay)
    frame_file = FRAMES_DIR / template.get("frame_file", "")
    if frame_file.is_file():
        # mở file khung lên và chuyển sang RGBA để giữ kênh alpha (độ trong suốt)
        #(A = Alpha = Trong suốt)
        frame_img = Image.open(frame_file).convert("RGBA")
        # resize nếu fame không đúng kích thước canvas cho chắc 
        frame_img = frame_img.resize((canvas_w, canvas_h), Image.LANCZOS)
        # dán lên layer trên 
        # mask=frame_img nghĩa là: Dùng chính độ trong suốt của cái khung làm mặt nạ.
        # - Chỗ nào khung trong suốt -> Không dán (hiện ảnh ở dưới).
        # - Chỗ nào khung có màu -> Dán đè lên (che ảnh ở dưới).
        canvas.paste(frame_img, (0, 0), mask=frame_img)
        logger.info("Applied frame overlay: %s", frame_file.name)

    # --------------------------------------------------------------- #
    #  Save final output                                               #
    # --------------------------------------------------------------- #
    FINAL_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    output_filename = f"final_{safe_id}.jpg"
    output_path = FINAL_OUTPUTS_DIR / output_filename
    # Lưu xuống ổ cứng
    # - format="JPEG": Định dạng phổ thông.
    # - quality=95: Nén rất ít, ảnh cực nét.
    # - dpi=(300, 300): Thiết lập metadata để máy in hiểu là cần in nét cao.
    canvas.save(output_path, format="JPEG", quality=95, dpi=(300, 300))
    logger.info("Merged image saved: %s", output_path)

    return {
        "output_filename": output_filename,
        "output_path": str(output_path),
    }
