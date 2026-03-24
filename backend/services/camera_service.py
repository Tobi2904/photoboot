"""
camera_service.py — Dùng python-gphoto2 binding (C library) cho Sony a6400.

Handles:
- Giữ kết nối camera liên tục (không load driver lại mỗi lần chụp).
- Capturing images and downloading to a session folder.
- Counting existing photos to enforce the per-session limit.
- Tự động reconnect nếu mất kết nối.
"""
import time
import os
import logging
from pathlib import Path
import gphoto2 as gp

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------- #
#  Configuration (resolved relative to the repo root)                      #
# ----------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent.parent          # repo root
STORAGE_DIR = BASE_DIR / "storage"
TEMP_SESSIONS_DIR = STORAGE_DIR / "temp_sessions"


class CameraError(Exception):
    """Raised when gphoto2 reports a hardware / USB error."""


class SessionLimitError(Exception):
    """Raised when the session has already reached the photo cap."""


# ----------------------------------------------------------------------- #
#  Persistent camera connection                                            #
# ----------------------------------------------------------------------- #
# Giữ kết nối camera mở liên tục → bỏ overhead load driver (~1.5s) mỗi lần
_camera: gp.Camera | None = None


def _get_camera() -> gp.Camera:
    """
    Lấy camera instance đã kết nối. Tự động init nếu chưa có
    hoặc reconnect nếu mất kết nối.
    """
    global _camera

    if _camera is not None:
        try:
            # Test xem camera còn sống không
            _camera.get_summary()
            return _camera
        except gp.GPhoto2Error:
            logger.warning("Camera connection lost, reconnecting...")
            try:
                _camera.exit()
            except Exception:
                pass
            _camera = None

    try:
        camera = gp.Camera()
        camera.init()
        logger.info("Camera connected successfully.")
        _camera = camera
        return _camera
    except gp.GPhoto2Error as exc:
        error_msg = str(exc)
        if "Could not detect" in error_msg or "no camera" in error_msg.lower():
            raise CameraError("Camera not found. Check USB connection.") from exc
        if "Could not claim" in error_msg or "USB" in error_msg:
            raise CameraError(
                "USB busy — another process may be using the camera."
            ) from exc
        raise CameraError(f"Camera init failed: {error_msg}") from exc


def release_camera():
    """Giải phóng camera (gọi khi shutdown server)."""
    global _camera
    if _camera is not None:
        try:
            _camera.exit()
        except Exception:
            pass
        _camera = None
        logger.info("Camera released.")


def _session_dir(session_id: str) -> Path:
    """Return the session folder path, creating it if needed."""
    # Sanitise session_id to prevent path-traversal attacks
    safe_id = Path(session_id).name
    folder = TEMP_SESSIONS_DIR / safe_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _count_photos(session_dir: Path) -> int:
    """Count existing photo_*.jpg files in a session directory."""
    return len(list(session_dir.glob("photo_*.jpg")))


def capture_photo(session_id: str, max_photos: int = 5) -> dict:
    """
    Chụp ảnh sử dụng cơ chế Event-Driven của libgphoto2.
    - Trigger lấy nét & chụp.
    - Chờ sự kiện FILE_ADDED để đảm bảo ảnh đã sẵn sàng (tránh lỗi file rỗng/out nét).
    - Không tự động cấu hình capturetarget (do đã chỉnh trên camera).
    """
    session_dir = _session_dir(session_id)
    current_count = _count_photos(session_dir)

    if current_count >= max_photos:
        raise SessionLimitError(f"Session {session_id} full.")

    next_index = current_count + 1
    filename = f"photo_{next_index}.jpg"
    filepath = session_dir / filename

    logger.info("Starting Focus & Capture sequence → %s", filepath)

    try:
        camera = _get_camera()
        
        # 1. Kích hoạt nút chụp (Trigger Capture)
        # Máy ảnh sẽ tự động chạy quy trình AF (Auto Focus)
        try:
            camera.trigger_capture()
        except gp.GPhoto2Error as exc:
            logger.error("Trigger failed: %s", exc)
            raise CameraError("Máy ảnh đang bận, vui lòng thử lại.")

        # 2. Vòng lặp chờ sự kiện (The Waiting Loop) - Tối đa 5s
        timeout_start = time.time()
        file_path_on_camera = None
        
        while True:
            # Nếu chờ quá 5s mà chưa có ảnh -> Timeout
            if time.time() - timeout_start > 5.0:
                raise CameraError("Hết thời gian chờ lấy nét (Timeout).")

            # Chờ sự kiện trong 500ms
            event_type, event_data = camera.wait_for_event(500)

            if event_type == gp.GP_EVENT_FILE_ADDED:
                file_path_on_camera = event_data
                logger.info("Image Captured! Path: %s/%s", 
                            file_path_on_camera.folder, file_path_on_camera.name)
                break 
            
            elif event_type == gp.GP_EVENT_TIMEOUT:
                continue 

        # 3. Tải ảnh về (Download)
        if file_path_on_camera:
            camera_file = camera.file_get(
                file_path_on_camera.folder, 
                file_path_on_camera.name, 
                gp.GP_FILE_TYPE_NORMAL
            )
            camera_file.save(str(filepath))

            # 4. Dọn dẹp RAM máy ảnh
            try:
                camera.file_delete(file_path_on_camera.folder, file_path_on_camera.name)
            except gp.GPhoto2Error:
                pass
        else:
            raise CameraError("Không tìm thấy file ảnh sau khi chụp.")

    except (gp.GPhoto2Error, CameraError) as exc:
        # Dọn dẹp file rác trên ổ cứng nếu có lỗi
        if filepath.exists():
            try:
                os.remove(filepath)
            except OSError:
                pass
        
        error_msg = str(exc)
        logger.error("Capture Loop Failed: %s", error_msg)
        raise CameraError(f"Lỗi chụp ảnh: {error_msg}")

    logger.info("Photo saved successfully: %s", filepath)
    return {
        "filename": filename,
        "path": str(filepath),
        "count": next_index,
    }
