"""
ZapConvert media worker: one-shot conversions over HTTP.

POST /convert
  headers  X-Op: audio | mp4 | compress-video | gif | pdf-docx | pdf-xlsx
           X-Ext: input file extension (e.g. "mp4")
           X-Params: JSON object of op options (optional)
           X-Worker-Token: shared secret (only when WORKER_TOKEN is set)
  body     raw file bytes
GET /health

Jobs run inside a RAM-backed scratch dir (tmpfs at /work); each job's folder is removed the moment
the response is written, so no input or output outlives the request.

Copyright (C) 2026 ZapConvert. Licensed under the GNU Affero General Public License v3.0 (see
LICENSE), because it links PyMuPDF (AGPL-3.0) through pdf2docx. Users of a network deployment can
download this source from the deployment's /licenses page.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORK = os.environ.get("WORK_DIR", "/work")
TOKEN = os.environ.get("WORKER_TOKEN", "")
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "100")) * 1024 * 1024
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SEC", "300"))
SLOTS = threading.BoundedSemaphore(int(os.environ.get("MAX_JOBS", "2")))

OUTPUT = {
    "audio": ("mp3", "audio/mpeg"),
    "mp4": ("mp4", "video/mp4"),
    "compress-video": ("mp4", "video/mp4"),
    "gif": ("gif", "image/gif"),
    "pdf-docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pdf-xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}

HERE = os.path.dirname(os.path.abspath(__file__))


class UserError(Exception):
    """A failure the visitor can act on; its message is shown to them as-is."""

CRF = {"low": 24, "recommended": 28, "max": 33}


def scale_filter(params):
    height = int(float(params.get("height", 0) or 0))
    if height in (1080, 720, 480):
        # Height preset, never upscaled; even dimensions keep H.264 happy.
        return f"scale=-2:'trunc(min(ih,{height})/2)*2'"
    scale = float(params.get("scale", 1))
    scale = min(1.0, max(0.25, scale))
    return f"scale=trunc(iw*{scale}/2)*2:-2"


def trim_window(params):
    """(start, duration) in seconds from the optional start / end params; None where unset."""
    def num(key):
        try:
            value = float(params[key])
        except (KeyError, TypeError, ValueError):
            return None
        return value if 0 <= value < 86400 else None

    start, end = num("start"), num("end")
    duration = end - (start or 0) if end is not None and end > (start or 0) else None
    return start, duration


def input_args(src, params, max_seconds=None):
    start, duration = trim_window(params)
    if max_seconds is not None:
        duration = min(duration, max_seconds) if duration else max_seconds
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    return args + ["-i", src]


def ffmpeg_args(op, src, dst, params):
    base = input_args(src, params)
    if op == "audio":
        bitrate = int(params.get("bitrate", 192))
        bitrate = bitrate if bitrate in (128, 192, 320) else 192
        return base + ["-vn", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", dst]
    if op in ("mp4", "compress-video"):
        crf = CRF.get(params.get("strength", "recommended"), 28) if op == "compress-video" else 23
        return base + [
            "-vf", scale_filter(params),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst,
        ]
    if op == "gif":
        scale = min(1.0, max(0.25, float(params.get("scale", 1))))
        width = int(480 * scale)
        seconds = min(30, max(1, int(params.get("seconds", 15))))
        base = input_args(src, params, seconds)
        graph = (
            f"fps=12,scale='min({width},iw)':-2:flags=lanczos,split[a][b];"
            "[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
        )
        return base + ["-filter_complex", graph, "-loop", "0", dst]
    raise ValueError("unknown op")


def run_job(op, src, dst, params):
    if op == "pdf-docx":
        code = (
            "import sys\n"
            "from pdf2docx import Converter\n"
            "cv = Converter(sys.argv[1])\n"
            "cv.convert(sys.argv[2])\n"
            "cv.close()\n"
        )
        cmd = ["python3", "-c", code, src, dst]
    elif op == "pdf-xlsx":
        cmd = ["python3", os.path.join(HERE, "pdf_xlsx.py"), src, dst]
    else:
        cmd = ffmpeg_args(op, src, dst, params)
    proc = subprocess.run(cmd, capture_output=True, timeout=JOB_TIMEOUT)
    if op == "pdf-xlsx" and proc.returncode == 3:
        raise UserError("This PDF has no text layer (it is a scan). Run it through OCR PDF first, then convert.")
    if op == "pdf-xlsx" and proc.returncode == 4:
        raise UserError("This PDF is password-protected. Remove the password with Unlock PDF, then try again.")
    if proc.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-400:] or "conversion produced no output")


class Handler(BaseHTTPRequestHandler):
    server_version = "zapconvert-media/1"

    def log_message(self, fmt, *args):  # keep file names and paths out of the logs
        pass

    def reply_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.reply_json(200, {"status": "up"})
        self.reply_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/convert":
            return self.reply_json(404, {"error": "not found"})
        if TOKEN and self.headers.get("X-Worker-Token") != TOKEN:
            return self.reply_json(401, {"error": "unauthorized"})

        op = self.headers.get("X-Op", "")
        ext = re.sub(r"[^a-z0-9]", "", self.headers.get("X-Ext", "bin").lower())[:8] or "bin"
        length = int(self.headers.get("Content-Length") or 0)
        if op not in OUTPUT:
            return self.reply_json(400, {"error": "unknown operation"})
        if length <= 0 or length > MAX_BYTES:
            return self.reply_json(413, {"error": "file too large"})
        try:
            params = json.loads(self.headers.get("X-Params") or "{}")
        except json.JSONDecodeError:
            params = {}

        if not SLOTS.acquire(blocking=False):
            return self.reply_json(503, {"error": "busy"})
        job = tempfile.mkdtemp(dir=WORK)
        try:
            src = os.path.join(job, f"input.{ext}")
            out_ext, mime = OUTPUT[op]
            dst = os.path.join(job, f"output.{out_ext}")
            with open(src, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining > 0:
                return self.reply_json(400, {"error": "upload incomplete"})

            try:
                run_job(op, src, dst, params)
            except subprocess.TimeoutExpired:
                return self.reply_json(504, {"error": "timeout"})
            except UserError as err:
                return self.reply_json(422, {"error": str(err), "user": True})
            except Exception as err:  # noqa: BLE001
                return self.reply_json(422, {"error": "conversion failed", "detail": str(err)[-300:]})

            os.remove(src)
            size = os.path.getsize(dst)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(dst, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        finally:
            shutil.rmtree(job, ignore_errors=True)
            SLOTS.release()


if __name__ == "__main__":
    os.makedirs(WORK, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
