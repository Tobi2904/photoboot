"""
main.py — FastAPI entry point for the Photobooth Kiosk Backend.

Responsibilities:
  • Expose REST APIs for capture, merge/upload, and session listing.
  • Serve the shared /storage directory as static files so the Frontend
    can preview images instantly via URL.
  • Start the background cleanup scheduler on app startup.

Run with:
    cd backend && uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import uuid
import shutil
import logging
import subprocess
import json
import time
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------- #
#  Internal services & utils                                              #
# ---------------------------------------------------------------------- #
from services.camera_service import (
    capture_photo,
    release_camera,
    CameraError,
    SessionLimitError,
)
from services.image_service import merge_photos, MergeError, FRAME_TEMPLATES
from services.s3_service import upload_to_s3, S3UploadError
from services.qr_service import generate_qr_base64
from services.fallback_contact_service import (
    append_fallback_contact,
    FallbackContactError,
    PhoneValidationError,
)
from services.liveview_service import FFmpegLiveviewManager
from utils.cleanup import start_cleanup_scheduler

# ---------------------------------------------------------------------- #
#  Configuration                                                          #
# ---------------------------------------------------------------------- #
load_dotenv()  # load backend/.env

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")

CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL_MINUTES", "10"))
SESSION_TTL = int(os.getenv("SESSION_TTL_MINUTES", "30"))
MAX_PHOTOS = int(os.getenv("MAX_PHOTOS_PER_SESSION", "5"))

BASE_DIR = Path(__file__).resolve().parent.parent          # repo root
STORAGE_DIR = BASE_DIR / "storage"
FRAMES_DIR = BASE_DIR / "backend" / "frames"
FALLBACK_CONTACTS_DIR = STORAGE_DIR / "fallback_contacts"
WINDOWS_PRINTER_NAME = "DS-RX1"
PRINT_REQUEST_COOLDOWN_SECONDS = int(os.getenv("PRINT_REQUEST_COOLDOWN_SECONDS", "20"))

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------- #
#  Active session tracking                                                #
# ---------------------------------------------------------------------- #
# Kiosk chỉ phục vụ 1 khách tại 1 thời điểm → dùng biến global để lưu
# session_id hiện tại. Các API capture/process sẽ validate theo biến này.
active_session_id: str | None = None
liveview_manager: FFmpegLiveviewManager | None = None
recent_print_requests: dict[str, float] = {}
inflight_print_requests: set[str] = set()

# ---------------------------------------------------------------------- #
#  App lifespan (startup / shutdown hooks)                                #
# ---------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on startup; clean up on shutdown."""
    global liveview_manager

    # Ensure storage directories exist
    (STORAGE_DIR / "temp_sessions").mkdir(parents=True, exist_ok=True)
    (STORAGE_DIR / "final_outputs").mkdir(parents=True, exist_ok=True)

    # Start the cleanup scheduler in the background
    start_cleanup_scheduler(
        interval_minutes=CLEANUP_INTERVAL,
        ttl_minutes=SESSION_TTL,
    )

    # Liveview se duoc khoi tao lazy o lan goi /api/liveview dau tien,
    # tranh viec FFmpeg reconnect ngầm khi kiosk chua mo man hinh liveview.
    liveview_manager = None

    logger.info("Photobooth Backend started.")
    yield

    if liveview_manager is not None:
        liveview_manager.stop()
        liveview_manager = None

    # Giải phóng kết nối camera khi tắt server
    release_camera()
    logger.info("Photobooth Backend shutting down.")


# ---------------------------------------------------------------------- #
#  FastAPI application                                                    #
# ---------------------------------------------------------------------- #
app = FastAPI(
    title="Photobooth Kiosk API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow all origins (kiosk runs locally)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the shared storage folder as /static so the FE can load images
app.mount("/static", StaticFiles(directory=str(STORAGE_DIR)), name="static")
# Mount frame assets for frame previews in FE.
app.mount("/frame-assets", StaticFiles(directory=str(FRAMES_DIR)), name="frame-assets")


# ====================================================================== #
#  Request / Response schemas                                             #
# ====================================================================== #

class ProcessRequest(BaseModel):
    """Payload for the merge/upload endpoint."""
    selected_photos: list[str] = Field(
        ...,
        min_length=1,
        description="List of filenames, e.g. ['photo_1.jpg', 'photo_3.jpg']",
    )
    frame_template_id: str = Field(
        ...,
        description="One of the registered frame template IDs.",
    )


class ProcessResponse(BaseModel):
    qr_code_base64: str
    s3_url: str
    final_image_url: str


class CaptureResponse(BaseModel):
    filename: str
    preview_url: str
    count: int
    max_photos: int


class FallbackContactRequest(BaseModel):
    session_id: str = Field(..., min_length=8, description="Session UUID from /api/start_session")
    phone: str = Field(..., min_length=10, max_length=20, description="Vietnamese mobile number")
    error_reason: str = Field(..., min_length=1, max_length=120)
    frame_template_id: str = Field(..., min_length=1, max_length=64)
    selected_photos: list[str] = Field(..., min_length=1, description="Selected photo filenames")


class FallbackContactResponse(BaseModel):
    status: str
    file_name: str
    row_index: int


class PrintRequest(BaseModel):
    filename: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Final output filename, e.g. final_abc123.jpg",
    )


DETECTED_ERROR_STATE_MESSAGES: dict[int, tuple[str, str]] = {
    3: ("PAPER_LOW", "Máy in sắp hết giấy."),
    4: ("PAPER_OUT", "Máy in đang hết giấy."),
    5: ("INK_LOW", "Máy in sắp hết mực/ribbon."),
    6: ("INK_OUT", "Máy in đã hết mực/ribbon."),
    7: ("COVER_OPEN", "Nắp máy in đang mở."),
    8: ("PAPER_JAM", "Máy in đang kẹt giấy."),
    9: ("OFFLINE", "Máy in đang offline."),
    10: ("SERVICE_REQUIRED", "Máy in cần bảo trì."),
    11: ("OUTPUT_BIN_FULL", "Khay chứa giấy đầu ra đã đầy."),
}

PRINTER_STATUS_MESSAGES: dict[int, tuple[str, str]] = {
    6: ("STOPPED", "Máy in đã dừng hoạt động."),
    7: ("OFFLINE", "Máy in đang offline."),
}

EXTENDED_PRINTER_STATUS_MESSAGES: dict[int, tuple[str, str]] = {
    6: ("STOPPED", "Máy in đã dừng hoạt động."),
    7: ("OFFLINE", "Máy in đang offline."),
}


def get_windows_printer_status(printer_name: str) -> dict:
    """Read printer health flags from Windows host via PowerShell + WMI."""
    ps_safe_printer_name = printer_name.replace("'", "''")
    status_command = (
        "$ErrorActionPreference='Stop'; "
        f"$p = Get-CimInstance Win32_Printer -Filter \"Name='{ps_safe_printer_name}'\"; "
        "if ($null -eq $p) { throw 'PRINTER_NOT_FOUND' }; "
        "$result = [ordered]@{ "
        "Name = $p.Name; "
        "DriverName = [string]$p.DriverName; "
        "PortName = [string]$p.PortName; "
        "WorkOffline = [bool]$p.WorkOffline; "
        "PrinterStatus = [int]$p.PrinterStatus; "
        "ExtendedPrinterStatus = [int]$p.ExtendedPrinterStatus; "
        "DetectedErrorState = [int]$p.DetectedErrorState; "
        "PaperOut = [bool]$p.PaperOut; "
        "Status = [string]$p.Status; "
        "Availability = [int]$p.Availability; "
        "ErrorInformation = [int]$p.ErrorInformation; "
        "PrinterState = [int]$p.PrinterState; "
        "}; "
        "$result | ConvertTo-Json -Compress"
    )
    status_result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", status_command],
        check=True,
        capture_output=True,
        text=True,
    )

    raw_output = (status_result.stdout or "").strip()
    if not raw_output:
        raise RuntimeError("Không đọc được trạng thái máy in từ Windows host.")

    return json.loads(raw_output)


def detect_printer_issue(printer_status: dict) -> dict | None:
    """Return normalized printer issues when device is unavailable."""
    issues: list[dict[str, str]] = []

    def add_issue(code: str, message: str, detail: str):
        if any(item["code"] == code and item["detail"] == detail for item in issues):
            return
        issues.append(
            {
                "code": code,
                "message": message,
                "detail": detail,
            }
        )

    if bool(printer_status.get("WorkOffline")):
        add_issue("OFFLINE", "Máy in đang offline.", "WorkOffline=true")

    if bool(printer_status.get("PaperOut")):
        add_issue("PAPER_OUT", "Máy in đang hết giấy.", "PaperOut=true")

    printer_status_code = int(printer_status.get("PrinterStatus", 0) or 0)
    if printer_status_code in PRINTER_STATUS_MESSAGES:
        code, message = PRINTER_STATUS_MESSAGES[printer_status_code]
        add_issue(code, message, f"PrinterStatus={printer_status_code}")

    extended_status_code = int(printer_status.get("ExtendedPrinterStatus", 0) or 0)
    if extended_status_code in EXTENDED_PRINTER_STATUS_MESSAGES:
        code, message = EXTENDED_PRINTER_STATUS_MESSAGES[extended_status_code]
        add_issue(code, message, f"ExtendedPrinterStatus={extended_status_code}")

    detected_error_state = int(printer_status.get("DetectedErrorState", 0) or 0)
    if detected_error_state in DETECTED_ERROR_STATE_MESSAGES:
        code, message = DETECTED_ERROR_STATE_MESSAGES[detected_error_state]
        add_issue(code, message, f"DetectedErrorState={detected_error_state}")

    if issues:
        primary = issues[0]
        return {
            "code": primary["code"],
            "message": primary["message"],
            "detail": "; ".join(item["detail"] for item in issues),
            "issues": issues,
        }

    return None


def build_windows_printto_command(windows_path: str, printer_status: dict) -> str:
    """Create a PowerShell script using rundll32 ImageView_PrintTo pipeline."""
    printer_name = str(printer_status.get("Name") or WINDOWS_PRINTER_NAME)
    driver_name = str(printer_status.get("DriverName") or "")
    port_name = str(printer_status.get("PortName") or "")

    ps_safe_windows_path = windows_path.replace("'", "''")
    ps_safe_printer_name = printer_name.replace("'", "''")
    ps_safe_driver_name = driver_name.replace("'", "''")
    ps_safe_port_name = port_name.replace("'", "''")
    return (
        "$ErrorActionPreference='Stop'; "
        f"$imgPath='{ps_safe_windows_path}'; "
        f"$printerName='{ps_safe_printer_name}'; "
        f"$driverName='{ps_safe_driver_name}'; "
        f"$portName='{ps_safe_port_name}'; "
        "$rundllPath=\"$env:SystemRoot\\System32\\rundll32.exe\"; "
        "$entryPoint=\"$env:SystemRoot\\System32\\shimgvw.dll,ImageView_PrintTo\"; "
        "$args=@($entryPoint, '/pt', $imgPath, $printerName); "
        "if ($driverName) { $args += $driverName }; "
        "if ($portName) { $args += $portName }; "
        "$proc = Start-Process -FilePath $rundllPath -ArgumentList $args -PassThru -ErrorAction Stop; "
        "$proc.WaitForExit(); "
        "if ($proc.ExitCode -ne 0) { throw ('RUNDLL_PRINTTO_FAILED_' + $proc.ExitCode) }"
    )


# ====================================================================== #
#  API Routes                                                             #
# ====================================================================== #

@app.get("/api/health")
async def health_check():
    """Simple liveness probe."""
    return {"status": "ok"}


@app.get("/api/printer/status")
async def printer_status():
    """Return printer diagnostics without sending any print command."""
    try:
        printer_state = await run_in_threadpool(
            get_windows_printer_status,
            WINDOWS_PRINTER_NAME,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return {
            "status": "error",
            "ready": False,
            "printer": WINDOWS_PRINTER_NAME,
            "message": "Không đọc được trạng thái máy in từ Windows host.",
            "detail": detail,
            "supports_remaining_paper_count": False,
            "remaining_paper_count": None,
        }
    except Exception as exc:
        return {
            "status": "error",
            "ready": False,
            "printer": WINDOWS_PRINTER_NAME,
            "message": "Có lỗi xảy ra khi kiểm tra trạng thái máy in.",
            "detail": str(exc),
            "supports_remaining_paper_count": False,
            "remaining_paper_count": None,
        }

    issue = detect_printer_issue(printer_state)
    return {
        "status": "success" if issue is None else "error",
        "ready": issue is None,
        "printer": WINDOWS_PRINTER_NAME,
        "printer_status": printer_state,
        "issue": issue,
        "supports_remaining_paper_count": False,
        "remaining_paper_count": None,
        "note": "Driver DS-RX1 qua WMI thường không cung cấp số giấy còn lại chính xác.",
    }


@app.get("/api/frames")
async def list_frames():
    """Return available frame templates so the Frontend can render options."""
    frames = []
    for tid, template in FRAME_TEMPLATES.items():
        canvas_w, canvas_h = template["canvas_size"]
        slots = [
            {
                "x": x,
                "y": y,
                "width": w,
                "height": h,
            }
            for (x, y, w, h) in template["slots"]
        ]
        frame_file = template.get("frame_file", "")
        frames.append(
            {
                "id": tid,
                "name": template.get("display_name", tid),
                "preview_url": f"/frame-assets/{frame_file}" if frame_file else None,
                "output_width": canvas_w,
                "output_height": canvas_h,
                "slots": slots,
                "num_slots": len(slots),
            }
        )

    return {"frames": frames}


@app.get("/api/liveview")
async def api_liveview():
    """Stream MJPEG liveview from USB capture card via FFmpeg."""
    global liveview_manager

    if liveview_manager is None:
        liveview_manager = FFmpegLiveviewManager()

    is_ready = await run_in_threadpool(
        liveview_manager.wait_until_ready,
        liveview_manager.ready_timeout_seconds(),
    )
    if not is_ready:
        raise HTTPException(status_code=503, detail=liveview_manager.last_error())

    return StreamingResponse(
        liveview_manager.get_video_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ------------------------------------------------------------------ #
#  Session management                                                  #
# ------------------------------------------------------------------ #

@app.post("/api/start_session")
async def start_session():
    """
    Tạo một session mới với UUID do server sinh ra.

    - Nếu đang có session cũ chưa cancel → tự động hủy session cũ.
    - Trả về session_id (UUID4) để FE dùng cho các bước tiếp theo.
    """
    global active_session_id

    # Tự động dọn session cũ nếu còn tồn tại
    if active_session_id is not None:
        old_dir = STORAGE_DIR / "temp_sessions" / active_session_id
        if old_dir.is_dir():
            shutil.rmtree(old_dir, ignore_errors=True)
            logger.info("Auto-cancelled previous session: %s", active_session_id)

    active_session_id = uuid.uuid4().hex
    session_dir = STORAGE_DIR / "temp_sessions" / active_session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    logger.info("New session started: %s", active_session_id)
    return {"session_id": active_session_id}


@app.post("/api/cancel_session")
async def cancel_session():
    """
    Hủy session hiện tại — xóa toàn bộ ảnh tạm của khách.

    Dùng khi khách bấm hủy hoặc bỏ giữa chừng.
    """
    global active_session_id, liveview_manager

    # Nut quay lai o man hinh chup se goi API nay, nen can tat liveview ngay.
    if liveview_manager is not None:
        liveview_manager.stop()
        liveview_manager = None
        logger.info("Liveview stopped while cancelling session: %s", active_session_id)

    if active_session_id is None:
        raise HTTPException(status_code=404, detail="No active session to cancel.")

    session_dir = STORAGE_DIR / "temp_sessions" / active_session_id
    if session_dir.is_dir():
        shutil.rmtree(session_dir, ignore_errors=True)
        logger.info("Session cancelled & cleaned: %s", active_session_id)

    cancelled_id = active_session_id
    active_session_id = None
    return {"status": "cancelled", "session_id": cancelled_id}


# ------------------------------------------------------------------ #
#  Phase 1 — Capture                                                  #
# ------------------------------------------------------------------ #

@app.post("/api/capture", response_model=CaptureResponse)
async def api_capture():
    """
    Trigger a gphoto2 capture and return the preview URL.

    Sử dụng active_session_id được tạo bởi /api/start_session.
    The blocking subprocess call is delegated to a thread so the
    event loop stays responsive.
    """
    if active_session_id is None:
        raise HTTPException(
            status_code=400,
            detail="No active session. Call /api/start_session first.",
        )

    try:
        result = await run_in_threadpool(
            capture_photo, active_session_id, MAX_PHOTOS,
        )
    except SessionLimitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CameraError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    preview_url = (
        f"/static/temp_sessions/{active_session_id}/{result['filename']}"
    )
    return CaptureResponse(
        filename=result["filename"],
        preview_url=preview_url,
        count=result["count"],
        max_photos=MAX_PHOTOS,
    )


# ------------------------------------------------------------------ #
#  Phase 2 + 3 — Merge, Upload, QR                                    #
# ------------------------------------------------------------------ #

@app.post("/api/process", response_model=ProcessResponse)
async def api_process(body: ProcessRequest):
    """
    Merge selected photos with a frame, upload to S3, and return a QR code.

    Sử dụng active_session_id được tạo bởi /api/start_session.
    Heavy image processing and the S3 upload run in a thread pool to
    keep the async loop free.
    """
    global active_session_id, liveview_manager

    if active_session_id is None:
        raise HTTPException(
            status_code=400,
            detail="No active session. Call /api/start_session first.",
        )

    # Da ket thuc buoc chup, tat liveview de tranh FFmpeg chay nen khi dang process.
    if liveview_manager is not None:
        liveview_manager.stop()
        liveview_manager = None
        logger.info("Liveview stopped before processing session: %s", active_session_id)

    # --- Merge ---
    # run_in_threadpool giúp chạy hàm bất đồng bộ tại một thread khác, sẽ giúp main thread rảnh tay để có thể chạy take photto hoặc api khác 
    try:
        merge_result = await run_in_threadpool(
            merge_photos,
            active_session_id,
            body.selected_photos,
            body.frame_template_id,
        )
    except MergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # sau khi có merge_result tiếp tục chạy upload_to_s3 trong thread khác để tránh block main thread
    # --- Upload to S3 ---
    try:
        s3_result = await run_in_threadpool(
            upload_to_s3,
            merge_result["output_path"],
            S3_BUCKET_NAME,
            AWS_ACCESS_KEY_ID,
            AWS_SECRET_ACCESS_KEY,
            AWS_REGION,
        )
    except S3UploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # --- Generate QR code ---
    qr_b64 = await run_in_threadpool(generate_qr_base64, s3_result["s3_url"])

    # Local URL for the final image (in case FE wants to preview before QR)
    final_image_url = f"/static/final_outputs/final_{active_session_id}.jpg"

    # Session hoàn tất → reset để sẵn sàng cho khách tiếp theo
    completed_session = active_session_id
    active_session_id = None
    logger.info("Session completed: %s", completed_session)

    return ProcessResponse(
        qr_code_base64=qr_b64,
        s3_url=s3_result["s3_url"],
        final_image_url=final_image_url,
    )


@app.post("/api/print")
async def api_print(body: PrintRequest):
    """Send a print command from WSL to Windows Photo print handler."""
    final_outputs_dir = (STORAGE_DIR / "final_outputs").resolve()
    linux_path = (final_outputs_dir / body.filename).resolve()

    try:
        linux_path.relative_to(final_outputs_dir)
    except ValueError:
        return {
            "status": "error",
            "message": "filename không hợp lệ.",
        }

    if not linux_path.is_file():
        return {
            "status": "error",
            "message": "Không tìm thấy file ảnh cần in.",
        }

    now = time.monotonic()
    last_print_time = recent_print_requests.get(body.filename)
    if last_print_time is not None:
        remaining = int(PRINT_REQUEST_COOLDOWN_SECONDS - (now - last_print_time))
        if remaining > 0:
            return {
                "status": "error",
                "message": f"Ảnh này vừa gửi lệnh in. Vui lòng chờ {remaining}s để tránh in trùng.",
                "detail": "duplicate_print_guard",
                "retry_after_seconds": remaining,
            }

    if body.filename in inflight_print_requests:
        return {
            "status": "error",
            "message": "Yêu cầu in cho ảnh này đang được xử lý. Vui lòng chờ.",
            "detail": "print_request_in_progress",
        }

    inflight_print_requests.add(body.filename)

    try:
        cutoff = time.monotonic() - (PRINT_REQUEST_COOLDOWN_SECONDS * 10)
        for filename, ts in list(recent_print_requests.items()):
            if ts < cutoff:
                recent_print_requests.pop(filename, None)

        printer_status = await run_in_threadpool(
            get_windows_printer_status,
            WINDOWS_PRINTER_NAME,
        )
        issue = detect_printer_issue(printer_status)
        if issue is not None:
            logger.warning(
                "Printer not ready (%s): %s",
                WINDOWS_PRINTER_NAME,
                issue.get("detail", "unknown"),
            )
            return {
                "status": "error",
                "message": issue["message"],
                "issue_code": issue["code"],
                "printer": WINDOWS_PRINTER_NAME,
                "printer_status": printer_status,
                "detail": issue["detail"],
                "issues": issue["issues"],
            }

        wslpath_result = await run_in_threadpool(
            subprocess.run,
            ["wslpath", "-w", str(linux_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        windows_path = (wslpath_result.stdout or "").strip()
        if not windows_path:
            raise RuntimeError("wslpath trả về đường dẫn rỗng.")

        print_command = build_windows_printto_command(
            windows_path,
            printer_status,
        )
        await run_in_threadpool(
            subprocess.run,
            ["powershell.exe", "-NoProfile", "-Command", print_command],
            check=True,
            capture_output=True,
            text=True,
        )

        recent_print_requests[body.filename] = time.monotonic()

        logger.info(
            "Print command sent for %s to printer %s via Rundll32 ImageView_PrintTo",
            body.filename,
            WINDOWS_PRINTER_NAME,
        )
        return {
            "status": "success",
            "filename": body.filename,
            "printer": WINDOWS_PRINTER_NAME,
            "windows_path": windows_path,
            "engine": "Rundll32ImageViewPrintTo",
        }
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        logger.exception("Print command failed for %s", body.filename)
        return {
            "status": "error",
            "message": "Gửi lệnh in thất bại.",
            "detail": detail,
        }
    except Exception as exc:
        logger.exception("Unexpected print error for %s", body.filename)
        return {
            "status": "error",
            "message": "Có lỗi xảy ra khi gửi lệnh in.",
            "detail": str(exc),
        }
    finally:
        inflight_print_requests.discard(body.filename)


# ------------------------------------------------------------------ #
#  Session utilities                                                   #
# ------------------------------------------------------------------ #

@app.get("/api/session/photos")
async def list_session_photos():
    """List all captured photos in a session (for the selection UI)."""
    if active_session_id is None:
        raise HTTPException(
            status_code=400,
            detail="No active session. Call /api/start_session first.",
        )

    session_dir = STORAGE_DIR / "temp_sessions" / active_session_id
    if not session_dir.is_dir():
        raise HTTPException(status_code=404, detail="Session not found.")

    photos = sorted(session_dir.glob("photo_*.jpg"))
    return {
        "session_id": active_session_id,
        "photos": [
            {
                "filename": p.name,
                "preview_url": f"/static/temp_sessions/{active_session_id}/{p.name}",
            }
            for p in photos
        ],
    }


@app.post("/api/session/fallback-contact", response_model=FallbackContactResponse)
async def save_fallback_contact(body: FallbackContactRequest):
    """Store fallback contact data so staff can manually send Zalo after failures."""
    if body.frame_template_id not in FRAME_TEMPLATES:
        raise HTTPException(status_code=400, detail="frame_template_id không hợp lệ.")

    if any(not name.lower().endswith(".jpg") for name in body.selected_photos):
        raise HTTPException(status_code=400, detail="selected_photos phải là danh sách tên file .jpg.")

    try:
        result = await run_in_threadpool(
            append_fallback_contact,
            output_dir=FALLBACK_CONTACTS_DIR,
            session_id=body.session_id,
            phone=body.phone,
            error_reason=body.error_reason,
            frame_template_id=body.frame_template_id,
            selected_photos=body.selected_photos,
        )
    except PhoneValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except FallbackContactError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("Fallback contact saved for session: %s", body.session_id)
    return FallbackContactResponse(
        status="saved",
        file_name=result.file_path.name,
        row_index=result.row_index,
    )
