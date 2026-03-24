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
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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

# ---------------------------------------------------------------------- #
#  App lifespan (startup / shutdown hooks)                                #
# ---------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on startup; clean up on shutdown."""
    # Ensure storage directories exist
    (STORAGE_DIR / "temp_sessions").mkdir(parents=True, exist_ok=True)
    (STORAGE_DIR / "final_outputs").mkdir(parents=True, exist_ok=True)

    # Start the cleanup scheduler in the background
    start_cleanup_scheduler(
        interval_minutes=CLEANUP_INTERVAL,
        ttl_minutes=SESSION_TTL,
    )
    logger.info("Photobooth Backend started.")
    yield
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


# ====================================================================== #
#  API Routes                                                             #
# ====================================================================== #

@app.get("/api/health")
async def health_check():
    """Simple liveness probe."""
    return {"status": "ok"}


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
    global active_session_id

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
    global active_session_id

    if active_session_id is None:
        raise HTTPException(
            status_code=400,
            detail="No active session. Call /api/start_session first.",
        )

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
