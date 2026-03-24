"""
qr_service.py — QR code generation for S3 download links.

Generates an in-memory QR image and returns it as a Base64-encoded
PNG string ready to embed directly in a JSON response or an <img> tag.
"""

import base64
import io
import logging

import qrcode
from qrcode.constants import ERROR_CORRECT_M

logger = logging.getLogger(__name__)


def generate_qr_base64(data: str, box_size: int = 10, border: int = 4) -> str:
    """
    Generate a QR code for *data* and return a Base64-encoded PNG string.

    Parameters
    ----------
    data     : The payload to encode (typically an S3 URL).
    box_size : Pixel size of each QR module.
    border   : Modules of white border around the QR code.

    Returns
    -------
    str  Base64-encoded PNG (no ``data:image/png;base64,`` prefix).
    """
    qr = qrcode.QRCode(
        version=None,                    # auto-size
        error_correction=ERROR_CORRECT_M,  # QR handle error 15%  # nghĩa là chỉ cần quét được 85% mã là vẫn đọc được, giúp QR dễ quét hơn khi in ra giấy có thể bị mờ hoặc hỏng một chút
        box_size=box_size,
        border=border,
    )
    qr.add_data(data) # nạp url S3 
    qr.make(fit=True) # tự động chọn version phù hợp với lượng data, nếu data nhiều sẽ tạo QR code lớn hơn (nhiều module hơn) để chứa đủ thông tin, còn nếu data ít sẽ tạo QR code nhỏ hơn, giúp dễ quét hơn

    img = qr.make_image(fill_color="black", back_color="white") # tạo ảnh QR code với màu đen cho phần mã và màu trắng cho nền
 

    buffer = io.BytesIO() # tạo file ảo trong RAM để lưu ảnh QR code dưới dạng PNG
    img.save(buffer, format="PNG") # lưu ảnh QR code vào buffer dưới định dạng PNG
    buffer.seek(0)

    b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    logger.info("QR code generated for: %s (length=%d)", data[:60], len(b64))
    return b64
