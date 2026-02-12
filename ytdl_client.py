#!/usr/bin/env python3
"""
YouTube Download Client for Sonarr
===================================
A qBittorrent-compatible API server that uses yt-dlp to download YouTube content.
Sonarr connects to this as if it were qBittorrent, but downloads come from YouTube.

This allows Sonarr to manage YouTube series (like Kurzgesagt) through its standard
download client interface.

Author: Ioannis Kokkinis
License: MIT
GitHub: https://github.com/ioanniskokkinis
"""

import os
import sys
import json
import hashlib
import threading
import subprocess
import time
import re
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import logging

# Helper function to safely parse port number from environment variable
def _get_port(env_var, default):
    """Safely parse port number from environment variable."""
    try:
        return int(os.getenv(env_var, default))
    except (ValueError, TypeError):
        logger_temp = logging.getLogger(__name__)
        logger_temp.warning(f"Invalid port value in {env_var}, using default: {default}")
        return int(default)

# Configuration - reads from environment variables with fallback to defaults
CONFIG = {
    "host": os.getenv("YTDL_HOST", "0.0.0.0"),
    "port": _get_port("YTDL_PORT", "8181"),
    "username": os.getenv("YTDL_USERNAME", "admin"),
    "password": os.getenv("YTDL_PASSWORD", "adminadmin"),
    "download_path": os.getenv("YTDL_DOWNLOAD_PATH", "/Volumes/MEDIA/TV"),
    "yt_dlp_path": os.getenv("YTDL_BIN_PATH", "yt-dlp"),
    "log_level": os.getenv("YTDL_LOG_LEVEL", "INFO"),
}

# In-memory storage for downloads
downloads = {}  # hash -> download info
sessions = {}   # SID -> session info

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"]),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def generate_hash(url):
    """Generate a torrent-like hash from URL"""
    return hashlib.sha1(url.encode()).hexdigest()[:40]


def generate_sid():
    """Generate a session ID"""
    return hashlib.sha256(os.urandom(32)).hexdigest()[:32]


class Download:
    """Represents a YouTube download job"""

    STATES = {
        "queued": "stalledDL",
        "downloading": "downloading",
        "completed": "uploading",  # Sonarr expects this for completed
        "error": "error",
        "paused": "pausedDL",
    }

    def __init__(self, url, name, save_path, category=""):
        self.hash = generate_hash(url + str(time.time()))
        self.url = url
        self.name = name
        self.save_path = save_path
        self.category = category
        self.state = "queued"
        self.progress = 0.0
        self.size = 0
        self.downloaded = 0
        self.added_on = int(time.time())
        self.completion_on = 0
        self.dlspeed = 0
        self.eta = 8640000
        self.content_path = ""
        self.error = ""
        self.thread = None

    def to_dict(self):
        """Convert to qBittorrent-compatible dict"""
        return {
            "hash": self.hash,
            "name": self.name,
            "magnet_uri": self.url,
            "size": self.size,
            "progress": self.progress,
            "dlspeed": self.dlspeed,
            "upspeed": 0,
            "priority": 0,
            "num_seeds": 100,
            "num_complete": 100,
            "num_leechs": 0,
            "num_incomplete": 0,
            "ratio": 0,
            "eta": self.eta,
            "state": self.STATES.get(self.state, "unknown"),
            "seq_dl": False,
            "f_l_piece_prio": False,
            "category": self.category,
            "tags": "",
            "super_seeding": False,
            "force_start": False,
            "save_path": self.save_path,
            "content_path": self.content_path or self.save_path,
            "added_on": self.added_on,
            "completion_on": self.completion_on,
            "tracker": "youtube.com",
            "trackers_count": 1,
            "dl_limit": 0,
            "up_limit": 0,
            "downloaded": self.downloaded,
            "uploaded": 0,
            "downloaded_session": self.downloaded,
            "uploaded_session": 0,
            "amount_left": max(0, self.size - self.downloaded),
            "completed": self.downloaded,
            "max_ratio": -1,
            "max_seeding_time": -1,
            "auto_tmm": False,
            "time_active": int(time.time()) - self.added_on,
            "seeding_time": 0,
            "last_activity": int(time.time()),
            "availability": 1,
        }

    def start_download(self):
        """Start the download in a background thread"""
        self.thread = threading.Thread(target=self._download_worker)
        self.thread.daemon = True
        self.thread.start()

    def _download_worker(self):
        """Worker thread that runs yt-dlp"""
        self.state = "downloading"
        self.dlspeed = 1000000  # Fake 1MB/s

        try:
            # Ensure save path exists
            os.makedirs(self.save_path, exist_ok=True)

            # Build yt-dlp command
            output_template = os.path.join(
                self.save_path,
                "%(title)s.%(ext)s"
            )

            cmd = [
                CONFIG["yt_dlp_path"],
                "--no-playlist",
                "-f", "bestvideo[width<=1920]+bestaudio/best[width<=1920]",
                "--merge-output-format", "mkv",
                "-o", output_template,
                "--progress",
                "--newline",
                self.url
            ]

            logger.info(f"Starting download: {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )

            for line in process.stdout:
                line = line.strip()
                logger.debug(f"yt-dlp: {line}")

                # Parse progress
                if "[download]" in line and "%" in line:
                    match = re.search(r'(\d+\.?\d*)%', line)
                    if match:
                        self.progress = float(match.group(1)) / 100.0

                # Parse file size
                if "of ~" in line or "of " in line:
                    match = re.search(r'of ~?\s*(\d+\.?\d*)\s*(MiB|GiB|KiB)', line)
                    if match:
                        size = float(match.group(1))
                        unit = match.group(2)
                        if unit == "GiB":
                            self.size = int(size * 1024 * 1024 * 1024)
                        elif unit == "MiB":
                            self.size = int(size * 1024 * 1024)
                        elif unit == "KiB":
                            self.size = int(size * 1024)
                        self.downloaded = int(self.size * self.progress)

                # Parse speed
                if "at" in line:
                    match = re.search(r'at\s+(\d+\.?\d*)\s*(MiB|KiB|GiB)/s', line)
                    if match:
                        speed = float(match.group(1))
                        unit = match.group(2)
                        if unit == "GiB":
                            self.dlspeed = int(speed * 1024 * 1024 * 1024)
                        elif unit == "MiB":
                            self.dlspeed = int(speed * 1024 * 1024)
                        elif unit == "KiB":
                            self.dlspeed = int(speed * 1024)

                # Check for destination file
                if "[Merger]" in line or "Destination:" in line or "[download] Destination:" in line:
                    match = re.search(r'Destination:\s*(.+)$', line)
                    if match:
                        self.content_path = match.group(1).strip()

            process.wait()

            if process.returncode == 0:
                self.state = "completed"
                self.progress = 1.0
                self.downloaded = self.size
                self.completion_on = int(time.time())
                self.dlspeed = 0
                self.eta = 0
                logger.info(f"Download completed: {self.name}")
            else:
                self.state = "error"
                self.error = f"yt-dlp exited with code {process.returncode}"
                logger.error(f"Download failed: {self.error}")

        except Exception as e:
            self.state = "error"
            self.error = str(e)
            logger.exception(f"Download error: {e}")


class QBittorrentAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler that mimics qBittorrent's Web API"""

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")

    def _send_json(self, data, status=200):
        """Send JSON response"""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_text(self, text, status=200):
        """Send text response"""
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def _check_auth(self):
        """Check if request is authenticated"""
        cookie = self.headers.get("Cookie", "")
        if "SID=" in cookie:
            sid = cookie.split("SID=")[1].split(";")[0]
            return sid in sessions
        return False

    def _get_post_data(self):
        """Parse POST data"""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            body = self.rfile.read(content_length).decode()
            return parse_qs(body)
        return {}

    def do_GET(self):
        """Handle GET requests"""
        parsed = urlparse(self.path)
        path = parsed.path

        # API version
        if path == "/api/v2/app/webapiVersion":
            self._send_text("2.8.3")
            return

        # App version
        if path == "/api/v2/app/version":
            self._send_text("v4.5.0")
            return

        # Build info
        if path == "/api/v2/app/buildInfo":
            self._send_json({
                "qt": "6.5.0",
                "libtorrent": "2.0.8.0",
                "boost": "1.81.0",
                "openssl": "3.0.8",
                "bitness": 64
            })
            return

        if not self._check_auth():
            self.send_response(403)
            self.end_headers()
            return

        # Get preferences
        if path == "/api/v2/app/preferences":
            self._send_json({
                "save_path": CONFIG["download_path"],
                "temp_path_enabled": False,
                "temp_path": "",
                "scan_dirs": {},
                "export_dir": "",
                "export_dir_fin": "",
                "mail_notification_enabled": False,
                "mail_notification_sender": "",
                "mail_notification_email": "",
                "mail_notification_smtp": "",
                "mail_notification_ssl_enabled": False,
                "mail_notification_auth_enabled": False,
                "mail_notification_username": "",
                "mail_notification_password": "",
                "autorun_enabled": False,
                "autorun_program": "",
                "queueing_enabled": True,
                "max_active_downloads": 3,
                "max_active_torrents": 5,
                "max_active_uploads": 3,
            })
            return

        # List torrents
        if path == "/api/v2/torrents/info":
            torrents = [d.to_dict() for d in downloads.values()]
            self._send_json(torrents)
            return

        # Get torrent properties
        if path.startswith("/api/v2/torrents/properties"):
            params = parse_qs(parsed.query)
            hash_id = params.get("hash", [""])[0]
            if hash_id in downloads:
                self._send_json(downloads[hash_id].to_dict())
            else:
                self._send_json({})
            return

        # Get torrent files
        if path.startswith("/api/v2/torrents/files"):
            params = parse_qs(parsed.query)
            hash_id = params.get("hash", [""])[0]
            if hash_id in downloads:
                d = downloads[hash_id]
                self._send_json([{
                    "index": 0,
                    "name": d.name,
                    "size": d.size,
                    "progress": d.progress,
                    "priority": 1,
                    "is_seed": d.state == "completed",
                    "piece_range": [0, 100],
                    "availability": 1,
                }])
            else:
                self._send_json([])
            return

        # Sync maindata (for Sonarr polling)
        if path == "/api/v2/sync/maindata":
            self._send_json({
                "rid": 1,
                "full_update": True,
                "torrents": {d.hash: d.to_dict() for d in downloads.values()},
                "categories": {},
                "tags": [],
                "server_state": {
                    "dl_info_speed": sum(d.dlspeed for d in downloads.values()),
                    "dl_info_data": sum(d.downloaded for d in downloads.values()),
                    "up_info_speed": 0,
                    "up_info_data": 0,
                    "dl_rate_limit": 0,
                    "up_rate_limit": 0,
                    "dht_nodes": 0,
                    "connection_status": "connected",
                }
            })
            return

        # Categories
        if path == "/api/v2/torrents/categories":
            self._send_json({
                "tv-sonarr": {"name": "tv-sonarr", "savePath": CONFIG["download_path"]}
            })
            return

        self._send_text("Not found", 404)

    def do_POST(self):
        """Handle POST requests"""
        parsed = urlparse(self.path)
        path = parsed.path
        data = self._get_post_data()

        # Login
        if path == "/api/v2/auth/login":
            username = data.get("username", [""])[0]
            password = data.get("password", [""])[0]

            if username == CONFIG["username"] and password == CONFIG["password"]:
                sid = generate_sid()
                sessions[sid] = {"created": time.time()}
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Set-Cookie", f"SID={sid}; Path=/")
                self.end_headers()
                self.wfile.write(b"Ok.")
                logger.info(f"Login successful for user: {username}")
            else:
                self._send_text("Fails.", 401)
                logger.warning(f"Login failed for user: {username}")
            return

        if not self._check_auth():
            self.send_response(403)
            self.end_headers()
            return

        # Add torrent/download
        if path == "/api/v2/torrents/add":
            urls = data.get("urls", [""])[0]
            category = data.get("category", [""])[0]
            save_path = data.get("savepath", [CONFIG["download_path"]])[0]

            # Parse URLs (one per line)
            for url in urls.strip().split("\n"):
                url = url.strip()
                if not url:
                    continue

                # Check if this looks like a YouTube URL
                if "youtube.com" in url or "youtu.be" in url:
                    # Extract video title (we'll update it later)
                    name = f"YouTube Video {generate_hash(url)[:8]}"

                    download = Download(url, name, save_path, category)
                    downloads[download.hash] = download
                    download.start_download()

                    logger.info(f"Added YouTube download: {url}")
                else:
                    logger.warning(f"Non-YouTube URL ignored: {url}")

            self._send_text("Ok.")
            return

        # Delete torrent
        if path == "/api/v2/torrents/delete":
            hashes = data.get("hashes", [""])[0].split("|")
            delete_files = data.get("deleteFiles", ["false"])[0] == "true"

            for h in hashes:
                if h in downloads:
                    d = downloads[h]
                    if delete_files and d.content_path and os.path.exists(d.content_path):
                        os.remove(d.content_path)
                        logger.info(f"Deleted file: {d.content_path}")
                    del downloads[h]
                    logger.info(f"Removed download: {h}")

            self._send_text("Ok.")
            return

        # Pause
        if path == "/api/v2/torrents/pause":
            hashes = data.get("hashes", [""])[0].split("|")
            for h in hashes:
                if h in downloads:
                    downloads[h].state = "paused"
            self._send_text("Ok.")
            return

        # Resume
        if path == "/api/v2/torrents/resume":
            hashes = data.get("hashes", [""])[0].split("|")
            for h in hashes:
                if h in downloads:
                    if downloads[h].state == "paused":
                        downloads[h].state = "queued"
                        downloads[h].start_download()
            self._send_text("Ok.")
            return

        # Set location
        if path == "/api/v2/torrents/setLocation":
            hashes = data.get("hashes", [""])[0].split("|")
            location = data.get("location", [""])[0]
            for h in hashes:
                if h in downloads:
                    downloads[h].save_path = location
            self._send_text("Ok.")
            return

        # Create category
        if path == "/api/v2/torrents/createCategory":
            self._send_text("Ok.")
            return

        self._send_text("Not found", 404)


def main():
    """Main entry point"""
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         YouTube Download Client for Sonarr                  ║
║         qBittorrent-compatible API Server                   ║
║                                                              ║
║              Created by Ioannis Kokkinis                     ║
╠══════════════════════════════════════════════════════════════╣
║  Add this to Sonarr as a qBittorrent download client:       ║
║                                                              ║
║  Host: localhost                                             ║
║  Port: {CONFIG["port"]}                                              ║
║  Username: {CONFIG["username"]}                                          ║
║  Password: {CONFIG["password"]}                                    ║
║  Category: tv-sonarr                                         ║
╚══════════════════════════════════════════════════════════════╝
    """)

    server = HTTPServer((CONFIG["host"], CONFIG["port"]), QBittorrentAPIHandler)
    logger.info(f"Starting server on {CONFIG['host']}:{CONFIG['port']}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
