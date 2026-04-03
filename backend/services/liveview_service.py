"""
liveview_service.py — FFmpeg MJPEG liveview manager for USB Capture Card.

Muc tieu:
- Khong dung OpenCV VideoCapture.
- Doc luong V4L2 bang subprocess + ffmpeg.
- Tach frame JPEG tu byte stream bang marker FF D8 / FF D9.
- Cung cap async generator cho FastAPI StreamingResponse.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import shutil
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


class LiveviewError(Exception):
	"""Raised when liveview stream cannot be started."""


class FFmpegLiveviewManager:
	"""Singleton manager giu mot worker FFmpeg duy nhat cho liveview."""

	_instance: "FFmpegLiveviewManager | None" = None
	_instance_lock = threading.Lock()

	def __new__(cls):
		with cls._instance_lock:
			if cls._instance is None:
				cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self):
		if getattr(self, "_initialized", False):
			return

		self.device = os.getenv("LIVEVIEW_DEVICE", "/dev/video0")
		self._ready_timeout_seconds = self._read_positive_float(
			"LIVEVIEW_READY_TIMEOUT_SECONDS", 15.0
		)
		self._ready_min_frames = self._read_positive_int(
			"LIVEVIEW_READY_MIN_FRAMES", 3
		)
		self._reconnect_delay_seconds = self._read_positive_float(
			"LIVEVIEW_RECONNECT_DELAY_SECONDS", 1.5
		)
		self._frame_stall_timeout_seconds = self._read_positive_float(
			"LIVEVIEW_FRAME_STALL_TIMEOUT_SECONDS", 3.0
		)

		self.ffmpeg_bin = self._resolve_ffmpeg_binary()
		self.latest_frame: bytes | None = None
		self._last_frame_ts = 0.0
		self._frame_counter = 0
		self._latest_frame_lock = threading.Lock()

		self.process: subprocess.Popen[bytes] | None = None
		self._stderr_thread: threading.Thread | None = None
		self._process_lock = threading.Lock()
		self._stop_event = threading.Event()
		self._frame_ready_event = threading.Event()

		self._last_error = "Liveview chua san sang."
		self._max_buffer_bytes = 12 * 1024 * 1024

		self._reader_thread = threading.Thread(
			target=self._reader_loop,
			name="ffmpeg-liveview-reader",
			daemon=True,
		)

		self._start_ffmpeg_process()
		self._reader_thread.start()
		self._initialized = True

	def _read_positive_float(self, key: str, default: float) -> float:
		raw = os.getenv(key, "").strip()
		if not raw:
			return default

		try:
			value = float(raw)
			if value <= 0:
				raise ValueError
			return value
		except ValueError:
			logger.warning(
				"Gia tri %s=%r khong hop le, su dung mac dinh %.2f", key, raw, default
			)
			return default

	def _read_positive_int(self, key: str, default: int) -> int:
		raw = os.getenv(key, "").strip()
		if not raw:
			return default

		try:
			value = int(raw)
			if value <= 0:
				raise ValueError
			return value
		except ValueError:
			logger.warning(
				"Gia tri %s=%r khong hop le, su dung mac dinh %d", key, raw, default
			)
			return default

	def _resolve_ffmpeg_binary(self) -> str | None:
		custom_bin = os.getenv("FFMPEG_BIN", "").strip()
		if custom_bin:
			return custom_bin

		system_bin = shutil.which("ffmpeg")
		if system_bin:
			return system_bin

		# Fallback: dung ffmpeg binary dong goi tu imageio-ffmpeg.
		try:
			from imageio_ffmpeg import get_ffmpeg_exe

			return get_ffmpeg_exe()
		except Exception:
			return None

	def _build_ffmpeg_command(self) -> list[str]:
		ffmpeg_cmd = self.ffmpeg_bin or "ffmpeg"

		# Yeu cau bat buoc theo de bai:
		# ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x720
		# -framerate 30 -i {device} -c:v copy -f mjpeg pipe:1
		return [
			ffmpeg_cmd,
			"-hide_banner",
			"-loglevel",
			"warning",
			"-f",
			"v4l2",
			"-input_format",
			"mjpeg",
			"-video_size",
			"1280x720",
			"-framerate",
			"30",
			"-i",
			self.device,
			"-c:v",
			"copy",
			"-f",
			"mjpeg",
			"pipe:1",
		]

	def _start_ffmpeg_process(self):
		with self._process_lock:
			if self._stop_event.is_set():
				return

			# Neu process cu van song thi khong mo lai.
			if self.process is not None and self.process.poll() is None:
				return

			if self.ffmpeg_bin is None:
				self.ffmpeg_bin = self._resolve_ffmpeg_binary()

			if self.ffmpeg_bin is None:
				self.process = None
				self._last_error = "Khong tim thay ffmpeg (PATH hoac imageio-ffmpeg)."
				logger.error(self._last_error)
				return

			cmd = self._build_ffmpeg_command()
			try:
				self.process = subprocess.Popen(
					cmd,
					stdout=subprocess.PIPE,
					stderr=subprocess.PIPE,
					stdin=subprocess.DEVNULL,
					bufsize=0,
				)
				self._last_error = ""
				self._start_stderr_reader(self.process)
				logger.info("FFmpeg liveview started: %s", " ".join(cmd))
			except FileNotFoundError:
				self.process = None
				self._last_error = "Khong tim thay lenh ffmpeg trong he thong."
				logger.error(self._last_error)
			except Exception as exc:
				self.process = None
				self._last_error = f"Khoi dong FFmpeg that bai: {exc}"
				logger.error(self._last_error)

	def _start_stderr_reader(self, proc: subprocess.Popen[bytes]):
		if proc.stderr is None:
			return

		def consume_stderr() -> None:
			while not self._stop_event.is_set():
				try:
					line = proc.stderr.readline()
				except Exception:
					return

				if not line:
					return

				message = line.decode("utf-8", errors="replace").strip()
				if message:
					logger.warning("FFmpeg stderr: %s", message)

		self._stderr_thread = threading.Thread(
			target=consume_stderr,
			name="ffmpeg-liveview-stderr",
			daemon=True,
		)
		self._stderr_thread.start()

	def _terminate_process(self):
		with self._process_lock:
			proc = self.process
			self.process = None

		if proc is None:
			return

		try:
			proc.terminate()
			proc.wait(timeout=2)
		except subprocess.TimeoutExpired:
			proc.kill()
			proc.wait(timeout=2)
		except Exception:
			pass
		finally:
			try:
				if proc.stdout is not None:
					proc.stdout.close()
			except Exception:
				pass

			try:
				if proc.stderr is not None:
					proc.stderr.close()
			except Exception:
				pass

		stderr_thread = self._stderr_thread
		self._stderr_thread = None
		if stderr_thread is not None and stderr_thread.is_alive():
			stderr_thread.join(timeout=0.5)

	def _clear_latest_frame(self):
		with self._latest_frame_lock:
			self.latest_frame = None
		self._last_frame_ts = 0.0
		self._frame_ready_event.clear()

	def _save_latest_frame(self, frame_bytes: bytes):
		with self._latest_frame_lock:
			self.latest_frame = frame_bytes
			self._last_frame_ts = time.monotonic()
			self._frame_counter += 1
		self._frame_ready_event.set()

	def _is_stream_ready(self, min_frame_counter: int) -> bool:
		proc = self.process
		if proc is None or proc.poll() is not None:
			return False

		now = time.monotonic()
		with self._latest_frame_lock:
			last_frame_ts = self._last_frame_ts
			frame_counter = self._frame_counter

		if last_frame_ts <= 0:
			return False

		is_frame_fresh = (now - last_frame_ts) <= self._frame_stall_timeout_seconds
		has_enough_frames = frame_counter >= min_frame_counter
		return is_frame_fresh and has_enough_frames

	def _restart_ffmpeg(self, reason: str, reset_frame: bool = True):
		self._last_error = reason
		logger.warning(reason)

		if reset_frame:
			self._clear_latest_frame()

		self._terminate_process()
		time.sleep(self._reconnect_delay_seconds)
		self._start_ffmpeg_process()

	def _extract_jpeg_frames_from_buffer(self, buffer: bytearray):
		while True:
			# Tim marker bat dau anh JPEG (SOI = Start Of Image): FF D8
			start_idx = buffer.find(b"\xff\xd8")
			if start_idx == -1:
				# Khong co marker dau frame, giu lai 2 byte cuoi de tranh mat marker cat ngang chunk.
				if len(buffer) > 2:
					del buffer[:-2]
				return

			# Tim marker ket thuc anh JPEG (EOI = End Of Image): FF D9
			end_idx = buffer.find(b"\xff\xd9", start_idx + 2)
			if end_idx == -1:
				# Da thay dau frame nhung chua du du lieu ket thuc.
				# Cat bo phan rac truoc start_idx va doi chunk tiep theo.
				if start_idx > 0:
					del buffer[:start_idx]

				# Chan truong hop du lieu vo han khi stream loi.
				if len(buffer) > self._max_buffer_bytes:
					del buffer[:-2]
				return

			frame = bytes(buffer[start_idx : end_idx + 2])
			self._save_latest_frame(frame)

			# Loai bo frame da tach khoi buffer de tiep tuc parse frame sau.
			del buffer[: end_idx + 2]

	def _reader_loop(self):
		buffer = bytearray()

		while not self._stop_event.is_set():
			proc = self.process
			if proc is None:
				time.sleep(self._reconnect_delay_seconds)
				self._start_ffmpeg_process()
				continue

			if proc.poll() is not None:
				self._restart_ffmpeg(
					"Tien trinh FFmpeg da dung, dang reconnect liveview.",
					reset_frame=True,
				)
				buffer.clear()
				continue

			if proc.stdout is None:
				time.sleep(0.05)
				continue

			try:
				ready, _, _ = select.select([proc.stdout], [], [], 0.5)
			except Exception as exc:
				self._restart_ffmpeg(
					f"Loi theo doi stdout FFmpeg: {exc}",
					reset_frame=True,
				)
				buffer.clear()
				continue

			if not ready:
				if (
					self._last_frame_ts > 0
					and (time.monotonic() - self._last_frame_ts)
					> self._frame_stall_timeout_seconds
				):
					self._restart_ffmpeg(
						(
							"Khong nhan duoc frame moi trong "
							f"{self._frame_stall_timeout_seconds:.1f}s, dang khoi dong lai FFmpeg."
						),
						reset_frame=True,
					)
					buffer.clear()
				continue

			try:
				chunk = os.read(proc.stdout.fileno(), 4096)
			except Exception as exc:
				self._restart_ffmpeg(
					f"Loi doc stdout FFmpeg: {exc}",
					reset_frame=True,
				)
				buffer.clear()
				continue

			if not chunk:
				self._restart_ffmpeg(
					"FFmpeg khong tra du lieu stdout, dang reconnect.",
					reset_frame=True,
				)
				buffer.clear()
				continue

			buffer.extend(chunk)
			self._extract_jpeg_frames_from_buffer(buffer)

	def wait_until_ready(self, timeout: float = 2.0) -> bool:
		wait_timeout = timeout if timeout > 0 else self._ready_timeout_seconds
		with self._latest_frame_lock:
			target_frame_counter = self._frame_counter + self._ready_min_frames

		deadline = time.monotonic() + wait_timeout
		while not self._stop_event.is_set():
			if self._is_stream_ready(target_frame_counter):
				return True

			proc = self.process
			if proc is None or proc.poll() is not None:
				self._start_ffmpeg_process()

			remaining = deadline - time.monotonic()
			if remaining <= 0:
				break

			wait_for = min(0.25, remaining)
			if self._frame_ready_event.wait(timeout=wait_for):
				self._frame_ready_event.clear()

		self._last_error = (
			"Liveview chua san sang sau "
			f"{wait_timeout:.1f}s (device: {self.device})."
		)
		return False

	def ready_timeout_seconds(self) -> float:
		return self._ready_timeout_seconds

	def last_error(self) -> str:
		return self._last_error or "Liveview stream chua co frame."

	async def get_video_stream(self):
		while not self._stop_event.is_set():
			frame = None
			with self._latest_frame_lock:
				frame = self.latest_frame

			if frame is not None:
				yield (
					b"--frame\r\n"
					b"Content-Type: image/jpeg\r\n\r\n"
					+ frame
					+ b"\r\n"
				)

			# Giu nhip ~30fps, tranh day stream qua nhanh gay ngop FE va dot CPU.
			await asyncio.sleep(0.03)

	def stop(self):
		self._stop_event.set()
		self._terminate_process()

		if self._reader_thread.is_alive():
			self._reader_thread.join(timeout=2)

		self._clear_latest_frame()
		self._initialized = False

		with self.__class__._instance_lock:
			if self.__class__._instance is self:
				self.__class__._instance = None

