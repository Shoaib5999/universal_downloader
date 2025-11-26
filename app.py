from flask import Flask, render_template, request, jsonify, send_file, url_for
import yt_dlp
import os
import threading
import uuid
import time
import zipfile
import re

app = Flask(__name__)

# Ensure the static and templates directories exist
base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, 'templates')
static_dir = os.path.join(base_dir, 'static')
downloads_dir = os.path.join(static_dir, 'downloads')

os.makedirs(template_dir, exist_ok=True)
os.makedirs(static_dir, exist_ok=True)
os.makedirs(downloads_dir, exist_ok=True)

# In-memory job store for tracking download progress
DOWNLOAD_JOBS = {}
JOB_CLEANUP_SECONDS = 60 * 60  # 1 hour


class DownloadCancelled(Exception):
    """Signal that a download was cancelled by the user."""

    pass


def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe filename."""
    if not name:
        return "download"
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "download"


def build_format_and_postprocessors(quality: str):
    """
    Map a human-friendly quality to yt-dlp format string and postprocessors.
    Works for YouTube and most other sites; non-existing formats will gracefully
    fall back as yt-dlp resolves the closest match.
    """
    quality = (quality or "best").lower()
    postprocessors = []

    if quality == "audio":
        # Audio-only download, convert to mp3 if possible
        fmt = "bestaudio/best"
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        )
    elif quality == "1080p":
        fmt = (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=1080][ext=mp4]/best[height<=1080]"
        )
    elif quality == "720p":
        fmt = (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=720][ext=mp4]/best[height<=720]"
        )
    elif quality == "480p":
        fmt = (
            "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=480][ext=mp4]/best[height<=480]"
        )
    else:
        # Default best available
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    return fmt, postprocessors


def progress_hook_for(job_id: str):
    def hook(d):
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            return

        # Cooperative cancellation
        if job.get("cancel"):
            raise DownloadCancelled("DOWNLOAD_CANCELLED")

        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

            # Raw metrics
            job["downloaded_bytes"] = int(downloaded)
            job["total_bytes"] = int(total) if total else None
            job["speed"] = d.get("speed")  # bytes per second
            job["eta"] = d.get("eta")  # seconds remaining

            percent = 0.0
            if total:
                percent = downloaded / total * 100.0
            else:
                # Fallback if yt-dlp provides textual percent
                percent_str = d.get("_percent_str")
                if percent_str:
                    try:
                        percent = float(percent_str.strip().rstrip("%"))
                    except ValueError:
                        pass

            # Playlist-aware overall progress (if we have playlist info)
            playlist_index = d.get("playlist_index")
            playlist_count = d.get("playlist_count")
            if playlist_index and playlist_count:
                try:
                    idx = int(playlist_index)
                    cnt = int(playlist_count)
                    overall = ((idx - 1) + percent / 100.0) / max(cnt, 1) * 100.0
                except Exception:
                    overall = percent
            else:
                overall = percent

            job["status"] = "downloading"
            job["progress"] = round(min(max(overall, 0.0), 100.0), 2)
        elif status == "finished":
            # Individual file finished; playlist may still continue
            job["status"] = "processing"

    return hook


def download_worker(job_id: str):
    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        return

    url = job["url"]
    quality = job["quality"]
    download_type = job["download_type"]
    job["status"] = "starting"
    job["progress"] = 0.0

    # Decide playlist behavior
    if download_type == "single":
        noplaylist = True
    elif download_type == "playlist":
        noplaylist = False
    else:
        # auto-detect: allow playlists if present
        noplaylist = False

    fmt, postprocessors = build_format_and_postprocessors(quality)

    # Common yt-dlp options
    ydl_opts = {
        "outtmpl": os.path.join(downloads_dir, "%(title)s.%(ext)s"),
        "format": fmt,
        "merge_output_format": "mp4",
        "noplaylist": noplaylist,
        "progress_hooks": [progress_hook_for(job_id)],
        "postprocessors": postprocessors,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # Determine if this is a playlist
            is_playlist = False
            entries = None
            if isinstance(info, dict) and info.get("_type") == "playlist":
                is_playlist = True
                entries = info.get("entries") or []

            if is_playlist and not noplaylist:
                # Bundle playlist into a single zip file
                playlist_title = sanitize_filename(info.get("title") or "playlist")
                zip_filename = f"{playlist_title}.zip"
                zip_path = os.path.join(downloads_dir, zip_filename)

                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for entry in entries:
                        if not entry:
                            continue
                        filename = ydl.prepare_filename(entry)
                        if os.path.exists(filename):
                            zf.write(filename, arcname=os.path.basename(filename))

                job["filename"] = zip_filename
            else:
                # Single video or playlist treated as single result
                filename = ydl.prepare_filename(info)
                job["filename"] = os.path.basename(filename)

        job["status"] = "completed"
        job["progress"] = 100.0
        job["speed"] = None
        job["eta"] = None
    except Exception as e:
        if isinstance(e, DownloadCancelled) or "DOWNLOAD_CANCELLED" in str(e):
            job["status"] = "cancelled"
            job["error"] = None
        else:
            job["status"] = "error"
            job["error"] = str(e)
        # Preserve whatever progress we had so far
        job["progress"] = float(job.get("progress") or 0.0)


def cleanup_old_jobs():
    """Remove old jobs from memory."""
    now = time.time()
    to_delete = [
        job_id
        for job_id, job in DOWNLOAD_JOBS.items()
        if now - job.get("created_at", now) > JOB_CLEANUP_SECONDS
    ]
    for job_id in to_delete:
        DOWNLOAD_JOBS.pop(job_id, None)


@app.route("/", methods=["GET"])
def index():
    # Simple landing page; all actions handled via JS + JSON APIs
    return render_template("index.html")


@app.route("/api/start_download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "best").strip()
    download_type = (data.get("download_type") or "auto").strip().lower()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    if download_type not in {"auto", "single", "playlist"}:
        download_type = "auto"

    cleanup_old_jobs()

    job_id = str(uuid.uuid4())
    DOWNLOAD_JOBS[job_id] = {
        "id": job_id,
        "url": url,
        "quality": quality,
        "download_type": download_type,
        "status": "queued",
        "progress": 0.0,
        "filename": None,
        "error": None,
        "created_at": time.time(),
    }

    thread = threading.Thread(target=download_worker, args=(job_id,), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>", methods=["GET"])
def get_progress(job_id):
    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Invalid job id"}), 404

    return jsonify(
        {
            "id": job["id"],
            "status": job["status"],
            "progress": job["progress"],
            "filename": job.get("filename"),
            "error": job.get("error"),
            "download_url": url_for("download_file", filename=job["filename"])
            if job.get("filename")
            else None,
            "total_bytes": job.get("total_bytes"),
            "downloaded_bytes": job.get("downloaded_bytes"),
            "speed": job.get("speed"),
            "eta": job.get("eta"),
        }
    )


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_download(job_id):
    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Invalid job id"}), 404

    # If already finished, just return current state
    if job.get("status") in {"completed", "error", "cancelled"}:
        return jsonify({"status": job["status"]})

    job["cancel"] = True
    # Mark as cancelling unless it's still queued
    if job.get("status") not in {"queued", "starting"}:
        job["status"] = "cancelling"

    return jsonify({"status": job["status"]})

@app.route('/download/<filename>')
def download_file(filename):
    return send_file(os.path.join(downloads_dir, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)
