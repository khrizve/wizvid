import sys
import re
import json
import time
import yt_dlp
import os
import shutil
import zipfile
import tarfile
import urllib.request
import subprocess
import platform

from yt_dlp.utils import DownloadCancelled

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QTextEdit,
    QPushButton, QFileDialog, QProgressBar, QComboBox, QLineEdit, QFrame, QScrollArea,
    QStackedWidget, QSpinBox, QMessageBox, QSystemTrayIcon, QMenu, QAbstractButton,
)
from PyQt6.QtCore import (
    Qt, QUrl, QThread, pyqtSignal, QObject, QSettings, QVariantAnimation,
    QEasingCurve, QRectF, QByteArray, QSize,
)
from PyQt6.QtGui import QPixmap, QDesktopServices, QPainter, QPainterPath, QColor, QBrush, QIcon
from PyQt6.QtSvg import QSvgRenderer


class DownloadCancelledException(DownloadCancelled):
    """Must subclass yt-dlp's DownloadCancelled so yt-dlp re-raises it even
    when 'ignoreerrors' is enabled (i.e. for playlist downloads)."""


TERMINAL_STATUSES = frozenset({'Completed', 'Failed', 'Cancelled'})


# ---------------------------------------------------------------------------
# yt-dlp version checker / auto-updater
# ---------------------------------------------------------------------------

def _ytdlp_system_vendor_dir():
    """Directory the .deb package bundles yt-dlp into."""
    return '/usr/share/wizvid/vendor'


def _ytdlp_user_vendor_dir():
    """User-writable yt-dlp location; the launcher prefers it when present."""
    return os.path.join(os.path.expanduser('~'), '.local', 'share', 'wizvid', 'vendor')


def _ytdlp_is_system_bundled():
    """True when the loaded yt-dlp came from the packaged vendor directory."""
    return os.path.abspath(yt_dlp.__file__).startswith(
        os.path.abspath(_ytdlp_system_vendor_dir()) + os.sep)


class YtDlpUpdateWorker(QObject):
    """Checks PyPI for the latest yt-dlp version and upgrades if needed."""
    status = pyqtSignal(str)
    update_found = pyqtSignal(str, str)
    up_to_date = pyqtSignal(str)
    update_done = pyqtSignal(str)
    update_failed = pyqtSignal(str)

    def run(self):
        try:
            current_version = yt_dlp.version.__version__
            self.status.emit(f"Checking yt-dlp version (installed: {current_version}) ...")

            with urllib.request.urlopen(
                "https://pypi.org/pypi/yt-dlp/json", timeout=10
            ) as resp:
                data = resp.read()

            latest_version = json.loads(data)["info"]["version"]

            if latest_version == current_version:
                self.status.emit(f"yt-dlp is up to date ({current_version})")
                self.up_to_date.emit(current_version)
                return

            self.status.emit(
                f"New yt-dlp version available: {latest_version} "
                f"(installed: {current_version}). Updating ..."
            )
            self.update_found.emit(current_version, latest_version)

            if _ytdlp_is_system_bundled():
                # /usr/share is not writable and the system python is PEP 668
                # managed, so install into the user vendor dir instead. The
                # launcher puts that dir on PYTHONPATH ahead of the bundled copy.
                target = _ytdlp_user_vendor_dir()
                self.status.emit(f"Installing yt-dlp {latest_version} into {target} ...")
                shutil.rmtree(target, ignore_errors=True)
                cmd = [sys.executable, "-m", "pip", "install", "--quiet",
                       "--upgrade", "--target", target, "--no-deps",
                       f"yt-dlp=={latest_version}"]
            else:
                cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                self.status.emit(f"yt-dlp updated to {latest_version} successfully!")
                self.update_done.emit(latest_version)
            else:
                err = result.stderr.strip() or result.stdout.strip()
                self.status.emit(f"yt-dlp update failed: {err}")
                self.update_failed.emit(err)

        except Exception as exc:
            self.status.emit(f"Could not check yt-dlp version: {exc}")
            self.update_failed.emit(str(exc))


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def _ffmpeg_bin_name():
    return "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"


def _local_ffmpeg_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_bin")


def _find_system_ffmpeg():
    return shutil.which("ffmpeg")


def _get_ffmpeg_download_url():
    machine = platform.machine().lower()
    if sys.platform == "win32":
        url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        return url, "zip"
    else:
        arch = "amd64" if ("x86_64" in machine or "amd64" in machine) else "arm64"
        url = f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz"
        return url, "tar"


def _download_ffmpeg_to_local(status_callback=None):
    local_dir = _local_ffmpeg_dir()
    os.makedirs(local_dir, exist_ok=True)

    url, archive_type = _get_ffmpeg_download_url()
    archive_path = os.path.join(local_dir, "ffmpeg_archive" + (".zip" if archive_type == "zip" else ".tar.xz"))

    def _report(msg):
        if status_callback:
            status_callback(msg)

    _report(f"Downloading ffmpeg from {url} ...")

    try:
        def _reporthook(count, block_size, total_size):
            if total_size > 0 and count % 200 == 0:
                pct = min(100, int(count * block_size * 100 / total_size))
                _report(f"Downloading ffmpeg ... {pct}%")

        urllib.request.urlretrieve(url, archive_path, reporthook=_reporthook)
    except Exception as exc:
        _report(f"Failed to download ffmpeg: {exc}")
        return None

    _report("Extracting ffmpeg ...")

    try:
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(local_dir)
        else:
            with tarfile.open(archive_path, "r:xz") as tf:
                tf.extractall(local_dir)
    except Exception as exc:
        _report(f"Failed to extract ffmpeg: {exc}")
        return None

    bin_name = _ffmpeg_bin_name()
    for root, _dirs, files in os.walk(local_dir):
        if bin_name in files:
            ffmpeg_exe = os.path.join(root, bin_name)
            if sys.platform != "win32":
                os.chmod(ffmpeg_exe, 0o755)
            _report(f"ffmpeg installed at: {ffmpeg_exe}")
            try:
                os.remove(archive_path)
            except OSError:
                pass
            return ffmpeg_exe

    _report("Could not locate ffmpeg binary after extraction.")
    return None


def ensure_ffmpeg(status_callback=None):
    def _report(msg):
        if status_callback:
            status_callback(msg)

    sys_ffmpeg = _find_system_ffmpeg()
    if sys_ffmpeg:
        _report(f"System ffmpeg found: {sys_ffmpeg}")
        return sys_ffmpeg

    local_dir = _local_ffmpeg_dir()
    bin_name = _ffmpeg_bin_name()
    for root, _dirs, files in os.walk(local_dir):
        if bin_name in files:
            local_ffmpeg = os.path.join(root, bin_name)
            _report(f"Local ffmpeg found: {local_ffmpeg}")
            return local_ffmpeg

    _report("ffmpeg not found - downloading automatically ...")
    return _download_ffmpeg_to_local(status_callback=status_callback)


class FfmpegSetupWorker(QObject):
    finished = pyqtSignal(str)
    status = pyqtSignal(str)

    def run(self):
        path = ensure_ffmpeg(status_callback=self.status.emit)
        self.finished.emit(path or "")


# ---------------------------------------------------------------------------
# Download worker (runs one URL/playlist download in a background thread)
# ---------------------------------------------------------------------------

class DownloadWorker(QObject):
    progress_signal = pyqtSignal(dict)
    phase_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool)
    error_signal = pyqtSignal(str)
    playlist_name_signal = pyqtSignal(str)
    paused_signal = pyqtSignal()
    resumed_signal = pyqtSignal()
    cancelled_signal = pyqtSignal()

    def __init__(self, urls, options):
        super().__init__()
        self.urls = urls
        self.options = options
        self.is_playlist = False
        self._is_paused = False
        self._is_cancelled = False
        self.ydl_instance = None

    def run(self):
        try:
            self.options['progress_hooks'] = [self.progress_hook]
            self.options['postprocessor_hooks'] = [self.postprocessor_hook]
            temp_options = self.options.copy()
            temp_options['quiet'] = True
            temp_options['extract_flat'] = True
            with yt_dlp.YoutubeDL(temp_options) as ydl_info:
                info = ydl_info.extract_info(self.urls[0], download=False)
                if info and info.get('_type') == 'playlist' and ('entries' in info):
                    playlist_title = info.get('title')
                    if playlist_title:
                        self.is_playlist = True
                        self.playlist_name_signal.emit(playlist_title)
                        safe_playlist_name = re.sub(r'[\\/:*?"<>|]', '', playlist_title)
                        base_path = self.options['outtmpl']
                        original_download_dir = os.path.dirname(base_path)
                        if not original_download_dir:
                            original_download_dir = '.'
                        self.options['outtmpl'] = os.path.join(
                            original_download_dir, safe_playlist_name, '%(title)s.%(ext)s')
                        self.options['yes_playlist'] = True
                        self.options['ignoreerrors'] = True
            self._raise_if_cancelled()
            self.ydl_instance = yt_dlp.YoutubeDL(self.options)
            with self.ydl_instance as ydl:
                ydl.download(self.urls)
            if self._is_cancelled:
                self.cancelled_signal.emit()
            else:
                self.finished_signal.emit(self.is_playlist)
        except DownloadCancelled:
            self.cancelled_signal.emit()
        except Exception as e:
            self.error_signal.emit(str(e))

    def _raise_if_cancelled(self):
        if self._is_cancelled:
            raise DownloadCancelledException('Download cancelled by user.')

    def _wait_while_paused(self):
        while self._is_paused and not self._is_cancelled:
            time.sleep(0.1)

    def progress_hook(self, d):
        self._raise_if_cancelled()
        if self._is_paused:
            self._wait_while_paused()
            self._raise_if_cancelled()
        self.progress_signal.emit(dict(d))

    def postprocessor_hook(self, d):
        self._raise_if_cancelled()
        if d.get('status') == 'started':
            if self._is_paused:
                self._wait_while_paused()
                self._raise_if_cancelled()
            self.phase_signal.emit('Converting...')
        elif d.get('status') == 'finished':
            self.phase_signal.emit('Downloading')

    def pause(self):
        if self._is_paused or self._is_cancelled:
            return
        self._is_paused = True
        self.paused_signal.emit()

    def resume(self):
        if not self._is_paused or self._is_cancelled:
            return
        self._is_paused = False
        self.resumed_signal.emit()

    def cancel(self):
        self._is_cancelled = True


# ---------------------------------------------------------------------------
# Preview worker
# ---------------------------------------------------------------------------

class PreviewWorker(QObject):
    preview_ready = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            with yt_dlp.YoutubeDL({'quiet': True, 'socket_timeout': 10}) as ydl:
                info = ydl.extract_info(self.url, download=False)
                thumbnail_url = info.get('thumbnail', '')
                if thumbnail_url:
                    with urllib.request.urlopen(thumbnail_url, timeout=10) as response:
                        info['thumbnail_data'] = response.read()
                self.preview_ready.emit(info)
        except Exception as e:
            self.error_signal.emit(f"Failed to fetch info for '{self.url}': {str(e)}")


ANSI_RE = re.compile('\x1B(?:[@-Z\\\\-_]|\\[[0-?]*[ -/]*[@-~])')


def strip_ansi(text):
    return ANSI_RE.sub('', text or '')


def build_download_options(download_path, fmt, quality, audio, multi_threading=True, auto_convert=True):
    """Turn the Format / Quality / Audio choices into yt-dlp options."""
    options = {
        'outtmpl': os.path.join(download_path, '%(title)s.%(ext)s'),
        'noprogress': True,
        'external_downloader_args': ['-loglevel', 'error', '-y'],
    }
    if multi_threading:
        options['concurrent_fragment_downloads'] = 4

    match = re.search(r'(\d+)\s*kbps', audio)
    audio_kbps = match.group(1) if match else '128'

    if fmt.startswith('MP3'):
        options['format'] = 'bestaudio/best'
        options['extract_audio'] = True
        options['audio_format'] = 'mp3'
        options['postprocessors'] = [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': audio_kbps},
            {'key': 'FFmpegMetadata'},
        ]
    else:
        if quality.startswith('Best'):
            options['format'] = ('bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
                                  if auto_convert else 'bestvideo+bestaudio/best')
        else:
            height_match = re.search(r'(\d+)p', quality)
            h = height_match.group(1) if height_match else '1080'
            options['format'] = (
                f'bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/best[ext=mp4]/best'
                if auto_convert else f'bestvideo[height<={h}]+bestaudio/best'
            )
        if auto_convert:
            options['merge_output_format'] = 'mp4'
            options['postprocessor_args'] = {'ffmpeg': ['-b:a', f'{audio_kbps}k']}
    return options


# ---------------------------------------------------------------------------
# DownloadTask - a lightweight controller that owns one download's worker
# thread and can be observed by more than one widget at a time (e.g. the
# same download shows up on both the Home and Downloads pages).
# ---------------------------------------------------------------------------

class DownloadTask(QObject):
    progress_changed = pyqtSignal(float, str)   # percent, speed text
    status_changed = pyqtSignal(str)
    finished = pyqtSignal(bool, str)            # success, message

    def __init__(self, url, options, title, subtitle, thumb_pixmap=None,
                 ffmpeg_path=None, save_path=None):
        super().__init__()
        self.url = url
        self.options = options
        self.title = title
        self.subtitle = subtitle
        self.thumb_pixmap = thumb_pixmap
        self.ffmpeg_path = ffmpeg_path
        self.save_path = save_path
        self.percent = 0.0
        self.speed = ''
        self.status = 'Queued'
        self.started = False
        self.thread = None
        self.worker = None

    def start(self):
        if self.started or self.status in TERMINAL_STATUSES:
            return
        if self.ffmpeg_path and os.path.isfile(self.ffmpeg_path):
            self.options['ffmpeg_location'] = os.path.dirname(self.ffmpeg_path)

        self.started = True
        self.thread = QThread()
        self.worker = DownloadWorker([self.url], self.options)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.phase_signal.connect(self._set_status)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.error_signal.connect(self._on_error)
        self.worker.cancelled_signal.connect(self._on_cancelled)
        self.worker.paused_signal.connect(lambda: self._set_status('Paused'))
        self.worker.resumed_signal.connect(lambda: self._set_status('Downloading'))
        self.worker.playlist_name_signal.connect(
            lambda name: self._set_status(f'Playlist: {name}'))

        self.worker.finished_signal.connect(self.thread.quit)
        self.worker.error_signal.connect(self.thread.quit)
        self.worker.cancelled_signal.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self._set_status('Downloading')
        self.thread.start()

    def _set_status(self, status):
        if status == self.status:
            return
        if self.status in TERMINAL_STATUSES:
            return
        if self.status == 'Paused' and status not in TERMINAL_STATUSES:
            return
        self.status = status
        self.status_changed.emit(status)

    def _on_progress(self, d):
        if d.get('status') == 'downloading':
            if self.status == 'Converting...':
                self._set_status('Downloading')
            percent_str = strip_ansi(d.get('_percent_str', '0.0%'))
            try:
                percent = float(percent_str.replace('%', '').strip())
            except ValueError:
                percent = self.percent
            speed = strip_ansi(d.get('_speed_str', 'N/A'))
            self.percent = percent
            self.speed = speed
            self.progress_changed.emit(percent, speed)

    def _on_finished(self, is_playlist):
        if self.status in TERMINAL_STATUSES:
            return
        self.status = 'Completed'
        self.percent = 100.0
        self.progress_changed.emit(100.0, '')
        self.finished.emit(True, '')

    def _on_error(self, message):
        if self.status in TERMINAL_STATUSES:
            return
        self.status = 'Failed'
        self.finished.emit(False, message)

    def _on_cancelled(self):
        if self.status in TERMINAL_STATUSES:
            return
        self.status = 'Cancelled'
        self.finished.emit(False, 'Cancelled')

    def pause(self):
        if self.status in TERMINAL_STATUSES or self.status == 'Paused':
            return
        if not self.worker:
            return
        self.worker.pause()
        self._set_status('Paused')

    def resume(self):
        if self.status != 'Paused':
            return
        self.status = 'Downloading'
        self.status_changed.emit(self.status)
        if self.worker:
            self.worker.resume()

    def cancel(self):
        if self.status in TERMINAL_STATUSES:
            return
        if self.worker:
            self.worker.cancel()
        else:
            self._on_cancelled()


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

THEMES = {
    'Fantasy Purple': dict(
        bg1='#0a0e1c', bg2='#141b30', sidebar='#0d1224', panel='#141c33', card='#1a2340',
        input_bg='#121933', border='#2a3358', accent1='#8b5cf6', accent2='#4f8cff',
        text='#e9edff', muted='#8a93b8', danger='#ef4a67',
    ),
    'Midnight Blue': dict(
        bg1='#050b16', bg2='#0d1b2e', sidebar='#081121', panel='#0f1b30', card='#132339',
        input_bg='#0d1728', border='#22334d', accent1='#3b82f6', accent2='#22d3ee',
        text='#e6f1ff', muted='#7f93b3', danger='#f43f5e',
    ),
    'Dark Forest': dict(
        bg1='#0a140f', bg2='#12241a', sidebar='#0c1a13', panel='#122419', card='#173123',
        input_bg='#0f1e15', border='#264234', accent1='#22c55e', accent2='#14b8a6',
        text='#e7f5ec', muted='#82a294', danger='#f87171',
    ),
    'Crimson Dusk': dict(
        bg1='#160a10', bg2='#26121b', sidebar='#1a0c13', panel='#241220', card='#301826',
        input_bg='#1c0e17', border='#4a2436', accent1='#ef4444', accent2='#f97316',
        text='#ffeef1', muted='#c08a97', danger='#fca5a5',
    ),
    'Parchment Light': dict(
        bg1='#f6f1e7', bg2='#efe6d3', sidebar='#f1e9d8', panel='#fffaf0', card='#fbf3e2',
        input_bg='#fffdf7', border='#d8c9a3', accent1='#a855f7', accent2='#f59e0b',
        text='#3a2e22', muted='#8a7a5c', danger='#dc2626',
    ),
}


def build_stylesheet(t):
    return f"""
        QWidget {{
            background: transparent;
            color: {t['text']};
            font-family: 'Segoe UI', 'Ubuntu', sans-serif;
            font-size: 13px;
        }}
        QWidget#root {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t['bg1']}, stop:1 {t['bg2']});
        }}
        QDialog, QMessageBox {{
            background-color: {t['panel']};
        }}
        QWidget#sidebar {{
            background-color: {t['sidebar']};
            border-right: 1px solid {t['border']};
        }}
        QLabel#logoTitle {{ font-size: 20px; font-weight: 700; color: {t['text']}; }}
        QLabel#logoSubtitle {{ font-size: 10px; color: {t['muted']}; }}
        QPushButton#navButton {{
            text-align: left;
            padding: 10px 14px;
            border-radius: 10px;
            border: none;
            background: transparent;
            color: {t['muted']};
            font-weight: 600;
            font-size: 14px;
        }}
        QPushButton#navButton:hover {{ background-color: {t['card']}; color: {t['text']}; }}
        QPushButton#navButton:checked {{
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {t['accent1']}, stop:1 {t['accent2']});
            color: #ffffff;
        }}
        QFrame#banner {{
            border-radius: 18px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t['accent1']}, stop:1 {t['bg2']});
            border: 1px solid {t['border']};
        }}
        QLabel#pageTitle {{ font-size: 24px; font-weight: 700; color: {t['text']}; }}
        QLineEdit, QTextEdit {{
            background-color: {t['input_bg']};
            border: 1px solid {t['border']};
            border-radius: 10px;
            padding: 8px 12px;
            color: {t['text']};
        }}
        QLineEdit:focus, QTextEdit:focus {{ border: 1px solid {t['accent1']}; }}
        QComboBox {{
            background-color: {t['input_bg']};
            border: 1px solid {t['border']};
            border-radius: 10px;
            padding: 8px 10px;
            color: {t['text']};
            min-width: 130px;
        }}
        QComboBox QAbstractItemView {{
            background-color: {t['panel']};
            border: 1px solid {t['border']};
            color: {t['text']};
            selection-background-color: {t['accent1']};
        }}
        QSpinBox {{
            background-color: {t['input_bg']};
            border: 1px solid {t['border']};
            border-radius: 8px;
            padding: 4px 8px;
            color: {t['text']};
        }}
        QPushButton {{
            background-color: {t['card']};
            border: 1px solid {t['border']};
            border-radius: 10px;
            padding: 8px 14px;
            color: {t['text']};
            font-weight: 600;
        }}
        QPushButton:hover {{ border: 1px solid {t['accent1']}; }}
        QPushButton:checkable:checked {{
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {t['accent1']}, stop:1 {t['accent2']});
            color: #ffffff;
            border: none;
        }}
        QPushButton#primaryBtn {{
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {t['accent1']}, stop:1 {t['accent2']});
            border: none;
            color: #ffffff;
            font-size: 15px;
            padding: 12px;
            border-radius: 12px;
        }}
        QPushButton#iconBtn {{
            background-color: {t['card']};
            border-radius: 8px;
            padding: 6px;
            min-width: 26px;
            max-width: 34px;
        }}
        QPushButton#dangerBtn {{
            color: {t['danger']};
            border: 1px solid {t['danger']};
            background: transparent;
        }}
        QPushButton#tabBtn {{
            background: transparent;
            border: none;
            border-bottom: 2px solid transparent;
            padding: 6px 4px 8px 4px;
            color: {t['muted']};
            font-weight: 700;
            border-radius: 0px;
        }}
        QPushButton#tabBtn:checked {{
            color: {t['text']};
            border-bottom: 2px solid {t['accent1']};
            background: transparent;
        }}
        QFrame#card, QFrame#panel {{
            background-color: {t['panel']};
            border: 1px solid {t['border']};
            border-radius: 14px;
        }}
        QFrame#downloadItem {{
            background-color: {t['card']};
            border: 1px solid {t['border']};
            border-radius: 12px;
        }}
        QLabel#panelTitle, QLabel#sectionTitle {{ font-size: 14px; font-weight: 700; color: {t['text']}; }}
        QLabel#muted {{ color: {t['muted']}; font-size: 12px; }}
        QLabel#itemTitle {{ font-size: 13px; font-weight: 700; color: {t['text']}; }}
        QLabel#itemMeta {{ font-size: 11px; color: {t['muted']}; }}
        QProgressBar {{
            border: none;
            border-radius: 4px;
            background-color: {t['input_bg']};
            max-height: 8px;
            min-height: 8px;
        }}
        QProgressBar::chunk {{
            border-radius: 4px;
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {t['accent1']}, stop:1 {t['accent2']});
        }}
        QScrollArea {{ border: none; background: transparent; }}
        QScrollBar:vertical {{ width: 8px; background: transparent; }}
        QScrollBar::handle:vertical {{ background: {t['border']}; border-radius: 4px; min-height: 24px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }}
    """


# ---------------------------------------------------------------------------
# SVG icon helpers (assets/icons/*.svg)
# ---------------------------------------------------------------------------

ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'icons')
_icon_cache = {}
CURRENT_THEME = THEMES['Fantasy Purple']


def _resolve_color(color):
    """Map a theme key ('text', 'muted', 'danger', ...) or '#rrggbb' to a hex."""
    if not color:
        return None
    if color.startswith('#'):
        return color
    return CURRENT_THEME.get(color, color)


def icon_pixmap(name, size=20, color=None):
    """Load assets/icons/<name>.svg and render it at `size` x `size`.

    `color` may be a '#rrggbb' literal (already resolved); it replaces the
    SVG's currentColor (Lucide/Tabler are stroke-based). Brand icons pass
    color=None and keep their baked-in colors.
    """
    key = (name, size, color)
    if key in _icon_cache:
        return _icon_cache[key]
    path = os.path.join(ICONS_DIR, f'{name}.svg')
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            svg = fh.read()
        if color:
            svg = svg.replace('currentColor', color)
        renderer = QSvgRenderer(QByteArray(svg.encode('utf-8')))
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
    except Exception:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
    _icon_cache[key] = pixmap
    return pixmap


def apply_widget_icon(w):
    """(Re-)apply the icon stored in a widget's wiz_icon* properties."""
    name = w.property('wiz_icon')
    if not name:
        return
    size = int(w.property('wiz_icon_size') or 16)
    color = w.property('wiz_icon_color')
    checked_color = w.property('wiz_icon_checked_color')
    if checked_color and isinstance(w, QAbstractButton) and w.isChecked():
        color = checked_color
    pm = icon_pixmap(name, size, _resolve_color(color))
    if isinstance(w, QLabel):
        w.setPixmap(pm)
    else:
        w.setIcon(QIcon(pm))
        w.setIconSize(QSize(size, size))


def set_btn_icon(btn, name, size=16, color='text', checked_color=None):
    btn.setProperty('wiz_icon', name)
    btn.setProperty('wiz_icon_size', size)
    btn.setProperty('wiz_icon_color', color)
    if checked_color:
        btn.setProperty('wiz_icon_checked_color', checked_color)
        if not btn.property('wiz_icon_hooked'):
            btn.setProperty('wiz_icon_hooked', True)
            btn.toggled.connect(lambda _=False, b=btn: apply_widget_icon(b))
    apply_widget_icon(btn)


def icon_text(name, text, size=16, color='text', object_name=None):
    """A small widget with an icon followed by a text label."""
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    icon_lbl = QLabel()
    icon_lbl.setProperty('wiz_icon', name)
    icon_lbl.setProperty('wiz_icon_size', size)
    icon_lbl.setProperty('wiz_icon_color', color)
    apply_widget_icon(icon_lbl)
    row.addWidget(icon_lbl)
    text_lbl = QLabel(text)
    if object_name:
        text_lbl.setObjectName(object_name)
    row.addWidget(text_lbl)
    row.addStretch()
    return widget


def logo_pixmap(size=24):
    """App logo: accent-colored rounded badge with a white wand glyph."""
    color = _resolve_color('accent1')
    key = ('__logo__', size, color)
    if key in _icon_cache:
        return _icon_cache[key]
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(color)))
    painter.drawRoundedRect(pixmap.rect(), size * 0.25, size * 0.25)
    glyph = int(size * 0.68)
    painter.drawPixmap(
        (size - glyph) // 2, (size - glyph) // 2,
        icon_pixmap('wand-sparkles', glyph, '#ffffff'))
    painter.end()
    _icon_cache[key] = pixmap
    return pixmap


def make_placeholder_pixmap(w, h, icon_name='film'):
    pixmap = QPixmap(w, h)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor('#232c4d')))
    painter.drawRoundedRect(pixmap.rect(), 10, 10)
    icon_size = max(16, int(min(w, h) * 0.45))
    icon_pm = icon_pixmap(icon_name, icon_size, _resolve_color('muted'))
    painter.drawPixmap((w - icon_size) // 2, (h - icon_size) // 2, icon_pm)
    painter.end()
    return pixmap


class BannerWidget(QFrame):
    """Banner frame that cover-fills itself with an image (uniform scale,
    centered, cropped overflow) and keeps the stylesheet's rounded corners."""

    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.setObjectName('banner')
        self._banner_pixmap = QPixmap(image_path)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._banner_pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 18, 18)
        painter.setClipPath(clip)
        scaled = self._banner_pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(
            (self.width() - scaled.width()) // 2,
            (self.height() - scaled.height()) // 2,
            scaled)
        painter.end()


# ---------------------------------------------------------------------------
# Custom toggle switch (used instead of plain checkboxes)
# ---------------------------------------------------------------------------

class ToggleSwitch(QAbstractButton):
    def __init__(self, parent=None, checked=False):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._knob_pos = 23.0 if checked else 3.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._on_knob_moved)
        self.toggled.connect(self._animate)
        self._on_color = QColor('#8b5cf6')
        self._off_color = QColor('#2a3358')

    def _animate(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._knob_pos)
        self._anim.setEndValue(23.0 if checked else 3.0)
        self._anim.start()

    def _on_knob_moved(self, value):
        self._knob_pos = float(value)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        bg_color = self._on_color if self.isChecked() else self._off_color
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(0, 0, rect.width(), rect.height(), 12, 12)
        painter.setBrush(QBrush(QColor('#ffffff')))
        painter.drawEllipse(QRectF(self._knob_pos, 3, 18, 18))
        painter.end()


# ---------------------------------------------------------------------------
# A single row in an Active / Completed / History list
# ---------------------------------------------------------------------------

class DownloadItemWidget(QFrame):
    def __init__(self, task, mode='active', parent=None):
        super().__init__(parent)
        self.task = task
        self.mode = mode
        self.setObjectName('downloadItem')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.thumb = QLabel()
        self.thumb.setFixedSize(56, 56)
        self.thumb.setScaledContents(True)
        if task.thumb_pixmap is not None and not task.thumb_pixmap.isNull():
            self.thumb.setPixmap(task.thumb_pixmap)
        else:
            self.thumb.setPixmap(make_placeholder_pixmap(56, 56))
        layout.addWidget(self.thumb)

        info_col = QVBoxLayout()
        info_col.setSpacing(4)
        self.title_lbl = QLabel(task.title)
        self.title_lbl.setObjectName('itemTitle')
        self.title_lbl.setWordWrap(True)
        info_col.addWidget(self.title_lbl)
        self.meta_lbl = QLabel(task.subtitle)
        self.meta_lbl.setObjectName('itemMeta')
        info_col.addWidget(self.meta_lbl)

        if mode == 'active':
            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(int(task.percent))
            self.progress.setTextVisible(False)
            info_col.addWidget(self.progress)
        layout.addLayout(info_col, 1)

        right_col = QVBoxLayout()
        right_col.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        if mode == 'active':
            paused = task.status == 'Paused'
            self.percent_lbl = QLabel(f'{int(task.percent)}%')
            self.percent_lbl.setObjectName('itemMeta')
            self.percent_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.speed_lbl = QLabel('Paused' if paused else (task.speed or task.status))
            self.speed_lbl.setObjectName('itemMeta')
            self.speed_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            right_col.addWidget(self.percent_lbl)
            right_col.addWidget(self.speed_lbl)

            btn_row = QHBoxLayout()
            self.pause_btn = QPushButton()
            self.pause_btn.setObjectName('iconBtn')
            set_btn_icon(self.pause_btn, 'play' if paused else 'pause', 14, 'text')
            self.pause_btn.setEnabled(task.status != 'Queued')
            self.pause_btn.clicked.connect(self._toggle_pause)
            self.cancel_btn = QPushButton()
            self.cancel_btn.setObjectName('iconBtn')
            set_btn_icon(self.cancel_btn, 'x', 14, 'text')
            self.cancel_btn.clicked.connect(self.task.cancel)
            btn_row.addWidget(self.pause_btn)
            btn_row.addWidget(self.cancel_btn)
            right_col.addLayout(btn_row)

            task.progress_changed.connect(self._on_progress)
            task.status_changed.connect(self._on_status)
        else:
            status_text = 'Completed' if task.status == 'Completed' else task.status
            if task.status == 'Completed':
                status_w = icon_text('circle-check', status_text, 14, '#22c55e', 'itemMeta')
            else:
                status_w = icon_text('circle-x', status_text, 14, '#ef4444', 'itemMeta')
            right_col.addWidget(status_w)
            open_btn = QPushButton(' Open')
            set_btn_icon(open_btn, 'folder-open', 14, 'text')
            open_btn.setMinimumWidth(80)
            open_btn.clicked.connect(self._open_folder)
            right_col.addWidget(open_btn)

        layout.addLayout(right_col)

    def _toggle_pause(self):
        if self.task.status == 'Queued' or self.task.status in TERMINAL_STATUSES:
            return
        if self.task.status == 'Paused':
            self.task.resume()
        else:
            self.task.pause()

    def _on_progress(self, percent, speed):
        self.progress.setValue(int(percent))
        self.percent_lbl.setText(f'{int(percent)}%')
        if speed:
            self.speed_lbl.setText(speed)

    def _on_status(self, status):
        if status == 'Queued':
            self.pause_btn.setEnabled(False)
            self.speed_lbl.setText('Queued')
            return
        self.pause_btn.setEnabled(status not in TERMINAL_STATUSES)
        if status == 'Paused':
            set_btn_icon(self.pause_btn, 'play', 14, 'text')
            self.speed_lbl.setText('Paused')
        else:
            set_btn_icon(self.pause_btn, 'pause', 14, 'text')
            if status == 'Downloading':
                self.speed_lbl.setText(self.task.speed or 'Starting…')
            else:
                self.speed_lbl.setText(status)

    def _open_folder(self):
        path = self.task.save_path or os.path.expanduser('~')
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class VideoDownloader(QWidget):
    FORMAT_OPTIONS = ['MP4 (Video)', 'MP3 (Audio)']
    QUALITY_OPTIONS = ['Best', '2160p (4K)', '1440p (2K)', '1080p (Full HD)',
                        '720p (HD)', '480p (SD)', '360p']
    AUDIO_OPTIONS = ['AAC 128kbps', 'AAC 192kbps', 'AAC 256kbps', 'AAC 320kbps']

    def __init__(self):
        super().__init__()
        self.settings = QSettings('MyOrganization', 'WizVid')

        self.download_path = self.settings.value('download_path', os.path.expanduser('~'))
        self.default_format = self.settings.value('default_format', self.FORMAT_OPTIONS[0])
        self.default_quality = self.settings.value('default_quality', '1080p (Full HD)')
        self.default_audio = self.settings.value('default_audio', self.AUDIO_OPTIONS[0])
        self.auto_convert = self._bool(self.settings.value('auto_convert', True))
        self.multi_threading = self._bool(self.settings.value('multi_threading', True))
        self.notify_complete = self._bool(self.settings.value('notify_complete', True))
        self.auto_update_ytdlp = self._bool(self.settings.value('auto_update_ytdlp', True))
        self.minimize_to_tray = self._bool(self.settings.value('minimize_to_tray', False))
        self.max_concurrent = int(self.settings.value('max_concurrent', 2))
        self.keep_history = self._bool(self.settings.value('keep_history', True))
        self.history_retention = self.settings.value('history_retention', 'Never')
        self.theme_name = self.settings.value('theme_name', 'Fantasy Purple')
        if self.theme_name not in THEMES:
            self.theme_name = 'Fantasy Purple'

        self.ffmpeg_path = None
        self.current_preview = None
        self.active_tasks = []
        self.pending_tasks = []
        self.completed_tasks = []
        self.history = self._load_history()
        self.tray_icon = None
        self._cancelling_all = False

        self.init_ui()
        self.apply_theme(self.theme_name)
        self._prune_history_by_retention()
        self.refresh_active_list()
        self.refresh_completed_list()
        self.refresh_history_list()

        self._start_ffmpeg_setup()
        if self.auto_update_ytdlp:
            self._start_ytdlp_update_check()
        if self.minimize_to_tray:
            self._setup_tray_icon()

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bool(value):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'yes')

    @staticmethod
    def _short_path(path, limit=30):
        if len(path) <= limit:
            return path
        return f'{path[:12]}...{path[-14:]}'

    def _log(self, message):
        print(message)

    def _load_history(self):
        raw = self.settings.value('history_json', '[]')
        try:
            return json.loads(raw)
        except Exception:
            return []

    def _save_history(self):
        self.settings.setValue('history_json', json.dumps(self.history))

    def _prune_history_by_retention(self):
        if self.history_retention == 'Never' or not self.history:
            return
        days_map = {'After 7 days': 7, 'After 30 days': 30, 'After 90 days': 90}
        days = days_map.get(self.history_retention)
        if not days:
            return
        cutoff = time.time() - days * 86400
        kept = []
        for entry in self.history:
            try:
                ts = time.mktime(time.strptime(entry.get('date', ''), '%Y-%m-%d %H:%M'))
            except Exception:
                ts = time.time()
            if ts >= cutoff:
                kept.append(entry)
        if len(kept) != len(self.history):
            self.history = kept
            self._save_history()

    # ------------------------------------------------------------------
    # ffmpeg / yt-dlp background setup
    # ------------------------------------------------------------------

    def _start_ffmpeg_setup(self):
        self.ffmpeg_thread = QThread()
        self.ffmpeg_worker = FfmpegSetupWorker()
        self.ffmpeg_worker.moveToThread(self.ffmpeg_thread)
        self.ffmpeg_thread.started.connect(self.ffmpeg_worker.run)
        self.ffmpeg_worker.status.connect(self._log)
        self.ffmpeg_worker.finished.connect(self._on_ffmpeg_ready)
        self.ffmpeg_worker.finished.connect(self.ffmpeg_thread.quit)
        self.ffmpeg_thread.finished.connect(self.ffmpeg_worker.deleteLater)
        self.ffmpeg_thread.finished.connect(self.ffmpeg_thread.deleteLater)
        self.ffmpeg_thread.start()

    def _on_ffmpeg_ready(self, path):
        if path:
            self.ffmpeg_path = path
        else:
            self._log('ffmpeg could not be found or downloaded; '
                       'audio extraction/merging may not work.')

    def _start_ytdlp_update_check(self):
        self.ytdlp_update_thread = QThread()
        self.ytdlp_update_worker = YtDlpUpdateWorker()
        self.ytdlp_update_worker.moveToThread(self.ytdlp_update_thread)
        self.ytdlp_update_thread.started.connect(self.ytdlp_update_worker.run)
        self.ytdlp_update_worker.status.connect(self._log)
        self.ytdlp_update_worker.update_found.connect(
            lambda cur, latest: self._log(f'yt-dlp update found: {cur} -> {latest}'))
        self.ytdlp_update_worker.update_done.connect(self._on_ytdlp_update_done)
        self.ytdlp_update_worker.up_to_date.connect(
            lambda v: self._log(f'yt-dlp {v} is up to date.'))
        self.ytdlp_update_worker.update_failed.connect(
            lambda e: self._log(f'yt-dlp update check failed: {e}'))
        self.ytdlp_update_worker.update_done.connect(self.ytdlp_update_thread.quit)
        self.ytdlp_update_worker.update_failed.connect(self.ytdlp_update_thread.quit)
        self.ytdlp_update_worker.up_to_date.connect(self.ytdlp_update_thread.quit)
        self.ytdlp_update_thread.finished.connect(self.ytdlp_update_worker.deleteLater)
        self.ytdlp_update_thread.finished.connect(self.ytdlp_update_thread.deleteLater)
        self.ytdlp_update_thread.start()

    def _on_ytdlp_update_done(self, new_version):
        QMessageBox.information(
            self, 'yt-dlp Updated',
            f'yt-dlp has been updated to version {new_version}.\n'
            'Please restart WizVid to use the new version.')

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def init_ui(self):
        self.setWindowTitle('WizVid \u2014 Fantasy Downloader')
        self.setGeometry(200, 60, 1320, 840)
        self.setMinimumSize(1080, 680)
        self.setObjectName('root')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root_layout.addLayout(body)

        body.addWidget(self.build_sidebar())

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        self.home_page = self.build_home_page()
        self.downloads_page = self.build_downloads_page()
        self.history_page = self.build_history_page()
        self.settings_page = self.build_settings_page()

        self.stack.addWidget(self.home_page)
        self.stack.addWidget(self.downloads_page)
        self.stack.addWidget(self.history_page)
        self.stack.addWidget(self.settings_page)

    def build_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setFixedWidth(230)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 24)
        layout.setSpacing(6)

        logo_row = QHBoxLayout()
        logo_icon = QLabel()
        logo_icon.setProperty('wiz_logo', 24)
        logo_icon.setPixmap(logo_pixmap(24))
        logo_row.addWidget(logo_icon)
        logo_text = QVBoxLayout()
        logo_text.setSpacing(0)
        title = QLabel('Wiz<span style="color:#8b5cf6;">Vid</span>')
        title.setObjectName('logoTitle')
        subtitle = QLabel('FANTASY DOWNLOADER')
        subtitle.setObjectName('logoSubtitle')
        logo_text.addWidget(title)
        logo_text.addWidget(subtitle)
        logo_row.addLayout(logo_text)
        logo_row.addStretch()
        layout.addLayout(logo_row)
        layout.addSpacing(28)

        self.nav_buttons = {}
        nav_items = [
            ('home', 'house', 'Home'),
            ('downloads', 'inbox', 'Downloads'),
            ('history', 'history', 'History'),
            ('settings', 'settings', 'Settings'),
        ]
        for key, icon_name, label in nav_items:
            btn = QPushButton(label)
            btn.setObjectName('navButton')
            btn.setFixedHeight(44)
            set_btn_icon(btn, icon_name, 17, 'text', checked_color='#ffffff')
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, k=key: self.switch_page(k))
            layout.addWidget(btn)
            self.nav_buttons[key] = btn
        self.nav_buttons['home'].setChecked(True)

        layout.addStretch()
        quote = QLabel('"Better Videos.\nBigger Dreams."')
        quote.setObjectName('muted')
        quote.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(quote)
        version = QLabel('WizVid v2.0')
        version.setObjectName('muted')
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version)

        return sidebar

    def switch_page(self, key):
        pages = {'home': 0, 'downloads': 1, 'history': 2, 'settings': 3}
        for k, btn in self.nav_buttons.items():
            btn.setChecked(k == key)
        self.stack.setCurrentIndex(pages[key])

    # -- shared small builders -----------------------------------------

    def _labeled_combo(self, parent_layout, label_text, items, current):
        col = QVBoxLayout()
        col.setSpacing(4)
        lbl = QLabel(label_text)
        lbl.setObjectName('muted')
        col.addWidget(lbl)
        combo = QComboBox()
        combo.addItems(items)
        idx = combo.findText(current)
        if idx != -1:
            combo.setCurrentIndex(idx)
        col.addWidget(combo)
        parent_layout.addLayout(col)
        return combo

    def _make_list_container(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(200)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setSpacing(10)
        v.addStretch()
        scroll.setWidget(inner)
        return scroll, v

    def _rebuild_list(self, layout, tasks, mode, empty_text):
        while layout.count() > 1:
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        if not tasks:
            empty = QLabel(empty_text)
            empty.setObjectName('muted')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.insertWidget(0, empty)
        else:
            for i, task in enumerate(tasks):
                layout.insertWidget(i, DownloadItemWidget(task, mode))

    def _quick_row(self, icon_name, title, subtitle, action_widget):
        row = QFrame()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        icon_lbl = QLabel()
        icon_lbl.setProperty('wiz_icon', icon_name)
        icon_lbl.setProperty('wiz_icon_size', 16)
        icon_lbl.setProperty('wiz_icon_color', 'text')
        apply_widget_icon(icon_lbl)
        h.addWidget(icon_lbl)
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet('font-size: 12px; font-weight: 600;')
        text_col.addWidget(title_lbl)
        if isinstance(subtitle, QLabel):
            subtitle.setObjectName('itemMeta')
            text_col.addWidget(subtitle)
        elif isinstance(subtitle, str) and subtitle:
            sub_lbl = QLabel(subtitle)
            sub_lbl.setObjectName('itemMeta')
            text_col.addWidget(sub_lbl)
        h.addLayout(text_col, 1)
        h.addWidget(action_widget)
        return row

    def _toggle_row(self, title, subtitle, toggle_widget):
        row = QHBoxLayout()
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet('font-weight: 600; font-size: 13px;')
        sub_lbl = QLabel(subtitle)
        sub_lbl.setObjectName('itemMeta')
        text_col.addWidget(title_lbl)
        text_col.addWidget(sub_lbl)
        row.addLayout(text_col, 1)
        row.addWidget(toggle_widget)
        return row

    def _settings_field_row(self, label_text, inner_layout):
        col = QVBoxLayout()
        col.setSpacing(4)
        lbl = QLabel(label_text)
        lbl.setObjectName('muted')
        col.addWidget(lbl)
        col.addLayout(inner_layout)
        return col

    def _make_chevron_btn(self, callback):
        btn = QPushButton()
        btn.setObjectName('iconBtn')
        set_btn_icon(btn, 'chevron-right', 14, 'muted')
        btn.clicked.connect(callback)
        return btn

    # ------------------------------------------------------------------
    # Home page
    # ------------------------------------------------------------------

    def build_home_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 24)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setSpacing(18)

        banner_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'assets', 'wizvid_banner1.png')
        banner = BannerWidget(banner_path)
        banner.setFixedHeight(120)
        layout.addWidget(banner)

        columns = QHBoxLayout()
        columns.setSpacing(18)
        layout.addLayout(columns)

        left_col = QVBoxLayout()
        left_col.setSpacing(16)
        columns.addLayout(left_col, 2)

        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText('Paste video URL here...')
        self.url_input.setFixedHeight(46)
        self.url_input.setTextMargins(34, 0, 10, 0)
        self.url_input.returnPressed.connect(self.parse_url)
        url_icon = QLabel(self.url_input)
        url_icon.setProperty('wiz_icon', 'link')
        url_icon.setProperty('wiz_icon_size', 16)
        url_icon.setProperty('wiz_icon_color', 'muted')
        apply_widget_icon(url_icon)
        url_icon.move(12, (46 - 16) // 2)
        url_icon.show()
        url_row.addWidget(self.url_input, 1)
        self.parse_btn = QPushButton('Parse')
        self.parse_btn.setObjectName('primaryBtn')
        set_btn_icon(self.parse_btn, 'sparkles', 15, '#ffffff')
        self.parse_btn.setFixedHeight(46)
        self.parse_btn.setFixedWidth(130)
        self.parse_btn.clicked.connect(self.parse_url)
        url_row.addWidget(self.parse_btn)
        left_col.addLayout(url_row)

        self.preview_card = QFrame()
        self.preview_card.setObjectName('card')
        self.preview_card.setVisible(False)
        pc_layout = QHBoxLayout(self.preview_card)
        pc_layout.setContentsMargins(14, 14, 14, 14)
        pc_layout.setSpacing(14)
        self.preview_thumb = QLabel()
        self.preview_thumb.setFixedSize(150, 90)
        self.preview_thumb.setScaledContents(True)
        pc_layout.addWidget(self.preview_thumb)
        pc_text = QVBoxLayout()
        self.preview_title = QLabel('')
        self.preview_title.setObjectName('itemTitle')
        self.preview_title.setWordWrap(True)
        self.preview_meta = QLabel('')
        self.preview_meta.setObjectName('itemMeta')
        self.preview_desc = QLabel('')
        self.preview_desc.setObjectName('muted')
        self.preview_desc.setWordWrap(True)
        pc_text.addWidget(self.preview_title)
        pc_text.addWidget(self.preview_meta)
        pc_text.addWidget(self.preview_desc)
        pc_layout.addLayout(pc_text, 1)
        left_col.addWidget(self.preview_card)

        options_row = QHBoxLayout()
        options_row.setSpacing(14)
        self.format_combo = self._labeled_combo(
            options_row, 'Format', self.FORMAT_OPTIONS, self.default_format)
        self.quality_combo = self._labeled_combo(
            options_row, 'Quality', self.QUALITY_OPTIONS, self.default_quality)
        self.audio_combo = self._labeled_combo(
            options_row, 'Audio (for MP3)', self.AUDIO_OPTIONS, self.default_audio)
        self.format_combo.currentTextChanged.connect(self._on_settings_format_changed)
        self.quality_combo.currentTextChanged.connect(self._on_settings_quality_changed)
        self.audio_combo.currentTextChanged.connect(self._on_settings_audio_changed)
        left_col.addLayout(options_row)

        save_row = QVBoxLayout()
        save_row.setSpacing(4)
        save_label = QLabel('Save to')
        save_label.setObjectName('muted')
        save_row.addWidget(save_label)
        path_row = QHBoxLayout()
        self.path_field = QLineEdit(self.download_path)
        self.path_field.setReadOnly(True)
        path_row.addWidget(self.path_field, 1)
        folder_btn = QPushButton()
        folder_btn.setObjectName('iconBtn')
        set_btn_icon(folder_btn, 'folder-open', 15, 'text')
        folder_btn.clicked.connect(self.select_folder)
        path_row.addWidget(folder_btn)
        save_row.addLayout(path_row)
        left_col.addLayout(save_row)

        download_btn = QPushButton('  Download Now')
        download_btn.setObjectName('primaryBtn')
        set_btn_icon(download_btn, 'download', 17, '#ffffff')
        download_btn.setFixedHeight(48)
        download_btn.clicked.connect(self.start_download_from_home)
        left_col.addWidget(download_btn)

        tabs_row = QHBoxLayout()
        self.active_tab_btn = QPushButton('Active Downloads')
        self.active_tab_btn.setObjectName('tabBtn')
        self.active_tab_btn.setCheckable(True)
        self.active_tab_btn.setChecked(True)
        self.completed_tab_btn = QPushButton('Completed')
        self.completed_tab_btn.setObjectName('tabBtn')
        self.completed_tab_btn.setCheckable(True)
        self.active_tab_btn.clicked.connect(lambda: self._select_home_tab('active'))
        self.completed_tab_btn.clicked.connect(lambda: self._select_home_tab('completed'))
        tabs_row.addWidget(self.active_tab_btn)
        tabs_row.addWidget(self.completed_tab_btn)
        tabs_row.addStretch()
        clear_all_btn = QPushButton(' Clear All')
        clear_all_btn.setObjectName('dangerBtn')
        set_btn_icon(clear_all_btn, 'trash', 14, 'danger')
        clear_all_btn.clicked.connect(self.clear_home_tab)
        tabs_row.addWidget(clear_all_btn)
        left_col.addLayout(tabs_row)

        self.home_list_stack = QStackedWidget()
        self.home_active_container, self.home_active_layout = self._make_list_container()
        self.home_completed_container, self.home_completed_layout = self._make_list_container()
        self.home_list_stack.addWidget(self.home_active_container)
        self.home_list_stack.addWidget(self.home_completed_container)
        left_col.addWidget(self.home_list_stack)

        right_col = QVBoxLayout()
        right_col.setSpacing(18)
        columns.addLayout(right_col, 1)
        right_col.addWidget(self.build_platforms_panel())
        right_col.addWidget(self.build_quick_settings_panel())
        right_col.addStretch()

        layout.addStretch()
        return page

    def build_platforms_panel(self):
        panel = QFrame()
        panel.setObjectName('panel')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        title = icon_text('globe', 'Supported Platforms', 17, 'accent1', 'panelTitle')
        layout.addWidget(title)
        grid = QGridLayout()
        grid.setSpacing(10)
        platforms = [
            ('youtube', 'YouTube'), ('tiktok', 'TikTok'), ('instagram', 'Instagram'),
            ('x-twitter', 'Twitter / X'), ('facebook', 'Facebook'), ('ellipsis', 'More...'),
        ]
        for i, (icon_name, name) in enumerate(platforms):
            item = QFrame()
            item.setObjectName('downloadItem')
            v = QVBoxLayout(item)
            v.setContentsMargins(10, 10, 10, 10)
            v.setSpacing(4)
            icon_lbl = QLabel()
            icon_lbl.setProperty('wiz_icon', icon_name)
            icon_lbl.setProperty('wiz_icon_size', 22)
            if icon_name == 'ellipsis':
                icon_lbl.setProperty('wiz_icon_color', 'muted')
            apply_widget_icon(icon_lbl)
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_lbl = QLabel(name)
            name_lbl.setObjectName('itemMeta')
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.addWidget(icon_lbl)
            v.addWidget(name_lbl)
            grid.addWidget(item, i // 3, i % 3)
        layout.addLayout(grid)
        return panel

    def build_quick_settings_panel(self):
        panel = QFrame()
        panel.setObjectName('panel')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)
        title = icon_text('zap', 'Quick Settings', 17, 'accent1', 'panelTitle')
        layout.addWidget(title)

        self.qs_folder_label = QLabel(self._short_path(self.download_path))
        layout.addWidget(self._quick_row(
            'folder', 'Download Folder', self.qs_folder_label,
            self._make_chevron_btn(self.select_folder)))

        self.qs_format_combo = QComboBox()
        self.qs_format_combo.addItems(['MP4', 'MP3'])
        self.qs_format_combo.setCurrentText('MP4' if 'MP4' in self.default_format else 'MP3')
        self.qs_format_combo.currentTextChanged.connect(self._on_quick_format_changed)
        layout.addWidget(self._quick_row('film', 'Default Format', None, self.qs_format_combo))

        self.auto_convert_toggle = ToggleSwitch(checked=self.auto_convert)
        self.auto_convert_toggle.toggled.connect(self._on_auto_convert_toggled)
        layout.addWidget(self._quick_row('refresh-cw', 'Auto Convert', None, self.auto_convert_toggle))

        self.multi_thread_toggle = ToggleSwitch(checked=self.multi_threading)
        self.multi_thread_toggle.toggled.connect(self._on_multi_thread_toggled)
        layout.addWidget(self._quick_row(
            'layers', 'Multi-Threading', '(Faster downloads)', self.multi_thread_toggle))

        return panel

    def _select_home_tab(self, which):
        self.active_tab_btn.setChecked(which == 'active')
        self.completed_tab_btn.setChecked(which == 'completed')
        self.home_list_stack.setCurrentIndex(0 if which == 'active' else 1)

    def clear_home_tab(self):
        if self.active_tab_btn.isChecked():
            if not self.active_tasks:
                return
            resp = QMessageBox.question(
                self, 'Cancel All Downloads?',
                f'This will cancel all {len(self.active_tasks)} active download(s). Continue?')
            if resp == QMessageBox.StandardButton.Yes:
                self._cancel_all_downloads()
        else:
            self.completed_tasks.clear()
            self.refresh_completed_list()

    def _cancel_all_downloads(self, wait=False):
        self._cancelling_all = True
        try:
            for task in list(self.active_tasks):
                task.cancel()
        finally:
            self._cancelling_all = False
        if wait:
            for task in list(self.active_tasks):
                thread = task.thread
                if thread is not None:
                    try:
                        thread.wait(3000)
                    except RuntimeError:
                        pass

    # ------------------------------------------------------------------
    # Preview / download actions (Home)
    # ------------------------------------------------------------------

    def parse_url(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, 'Input Needed', 'Please paste a video URL first!')
            return
        self.parse_btn.setEnabled(False)
        self.parse_btn.setText('Parsing...')
        self.preview_thread = QThread()
        self.preview_worker = PreviewWorker(url)
        self.preview_worker.moveToThread(self.preview_thread)
        self.preview_thread.started.connect(self.preview_worker.run)
        self.preview_worker.preview_ready.connect(self._on_preview_ready)
        self.preview_worker.error_signal.connect(self._on_preview_error)
        self.preview_worker.preview_ready.connect(self.preview_thread.quit)
        self.preview_worker.error_signal.connect(self.preview_thread.quit)
        self.preview_thread.finished.connect(self.preview_worker.deleteLater)
        self.preview_thread.finished.connect(self.preview_thread.deleteLater)
        self.preview_thread.start()

    def _on_preview_ready(self, info):
        self.parse_btn.setEnabled(True)
        self.parse_btn.setText('Parse')
        self.current_preview = info
        if 'thumbnail_data' in info:
            pixmap = QPixmap()
            pixmap.loadFromData(info['thumbnail_data'])
        else:
            pixmap = make_placeholder_pixmap(150, 90)
        self.preview_thumb.setPixmap(pixmap)
        self.preview_title.setText(info.get('title', 'Unknown title'))
        duration = int(info.get('duration') or 0)
        minutes, seconds = divmod(duration, 60)
        extractor = info.get('extractor_key') or 'Source'
        height = info.get('height')
        quality_txt = f'{height}p' if height else ''
        meta_parts = [p for p in [extractor, f'{minutes}:{seconds:02d}', quality_txt] if p]
        self.preview_meta.setText('  \u2022  '.join(meta_parts))
        self.preview_desc.setText((info.get('description') or '')[:180])
        self.preview_card.setVisible(True)

    def _on_preview_error(self, error):
        self.parse_btn.setEnabled(True)
        self.parse_btn.setText('Parse')
        QMessageBox.critical(self, 'Preview Error', f'Failed to fetch preview:\n{error}')

    def start_download_from_home(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, 'Input Needed', 'Please paste a video URL first!')
            return
        fmt = self.format_combo.currentText()
        quality = self.quality_combo.currentText()
        audio = self.audio_combo.currentText()
        options = build_download_options(
            self.download_path, fmt, quality, audio, self.multi_threading, self.auto_convert)

        title = url
        thumb_pixmap = None
        if self.current_preview and self.current_preview.get('webpage_url') == url:
            title = self.current_preview.get('title', url)
            if 'thumbnail_data' in self.current_preview:
                thumb_pixmap = QPixmap()
                thumb_pixmap.loadFromData(self.current_preview['thumbnail_data'])

        subtitle = f"{fmt.split(' ')[0]} \u2022 {quality if fmt.startswith('MP4') else audio}"
        self._queue_task(url, options, title, subtitle, thumb_pixmap)
        self.url_input.clear()
        self.preview_card.setVisible(False)
        self.current_preview = None

    def _queue_task(self, url, options, title, subtitle, thumb_pixmap=None):
        task = DownloadTask(url, options, title, subtitle, thumb_pixmap,
                             self.ffmpeg_path, self.download_path)
        task.finished.connect(lambda success, msg, t=task: self._on_task_finished(t, success, msg))
        self.active_tasks.append(task)
        if self._running_count() < self.max_concurrent:
            task.start()
        else:
            self.pending_tasks.append(task)
        self.refresh_active_list()
        self._select_home_tab('active')

    def _running_count(self):
        return sum(1 for t in self.active_tasks if t.started)

    def _drain_pending(self):
        if self._cancelling_all:
            return
        while self.pending_tasks and self._running_count() < self.max_concurrent:
            task = self.pending_tasks.pop(0)
            if task.status in TERMINAL_STATUSES:
                continue
            task.start()

    def _on_task_finished(self, task, success, message):
        if task in self.active_tasks:
            self.active_tasks.remove(task)
        if task in self.pending_tasks:
            self.pending_tasks.remove(task)
        self.completed_tasks.insert(0, task)

        if self.keep_history:
            status = 'Completed' if success else ('Cancelled' if message == 'Cancelled' else 'Failed')
            entry = {
                'title': task.title,
                'url': task.url,
                'subtitle': task.subtitle,
                'status': status,
                'path': task.save_path,
                'date': time.strftime('%Y-%m-%d %H:%M'),
            }
            self.history.insert(0, entry)
            self._save_history()
            self.refresh_history_list()

        self.refresh_active_list()
        self.refresh_completed_list()

        if success and self.notify_complete:
            self._notify(task.title, 'Download completed!')
        elif not success and message != 'Cancelled':
            QMessageBox.warning(self, 'Download Failed', f'{task.title}\n\n{message}')

        self._drain_pending()

    def _notify(self, title, message):
        if self.tray_icon:
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 4000)

    def refresh_active_list(self):
        self._rebuild_list(self.home_active_layout, self.active_tasks, 'active',
                            'No downloads yet.')
        if hasattr(self, 'downloads_active_layout'):
            self._rebuild_list(self.downloads_active_layout, self.active_tasks, 'active',
                                'Nothing downloading right now. Add a URL above to get started.')
        self.active_tab_btn.setText(f'Active Downloads  ({len(self.active_tasks)})')
        if hasattr(self, 'downloads_active_count_label'):
            self.downloads_active_count_label.setText(f'Active Downloads  ({len(self.active_tasks)})')

    def refresh_completed_list(self):
        self._rebuild_list(self.home_completed_layout, self.completed_tasks, 'completed',
                            'Nothing completed this session.')
        self.completed_tab_btn.setText(f'Completed  ({len(self.completed_tasks)})')

    def refresh_history_list(self):
        self._rebuild_history_list()

    # ------------------------------------------------------------------
    # Downloads page
    # ------------------------------------------------------------------

    def build_downloads_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        header = QLabel('Downloads')
        header.setObjectName('pageTitle')
        layout.addWidget(header)
        sub = QLabel('Queue multiple links at once and manage everything downloading right now.')
        sub.setObjectName('muted')
        layout.addWidget(sub)

        batch_card = QFrame()
        batch_card.setObjectName('card')
        bc = QVBoxLayout(batch_card)
        bc.setContentsMargins(16, 16, 16, 16)
        bc.setSpacing(10)
        bc_title = icon_text('plus', 'Add URLs to Queue', 15, 'accent1', 'panelTitle')
        bc.addWidget(bc_title)
        self.batch_input = QTextEdit()
        self.batch_input.setPlaceholderText('Paste one or more video URLs, one per line...')
        self.batch_input.setFixedHeight(80)
        bc.addWidget(self.batch_input)

        options_row = QHBoxLayout()
        options_row.setSpacing(14)
        self.dl_format_combo = self._labeled_combo(
            options_row, 'Format', self.FORMAT_OPTIONS, self.default_format)
        self.dl_quality_combo = self._labeled_combo(
            options_row, 'Quality', self.QUALITY_OPTIONS, self.default_quality)
        self.dl_audio_combo = self._labeled_combo(
            options_row, 'Audio', self.AUDIO_OPTIONS, self.default_audio)
        bc.addLayout(options_row)

        add_btn = QPushButton('  Add to Queue && Start')
        add_btn.setObjectName('primaryBtn')
        set_btn_icon(add_btn, 'download', 15, '#ffffff')
        add_btn.clicked.connect(self._start_batch_download)
        bc.addWidget(add_btn)
        layout.addWidget(batch_card)

        self.downloads_active_count_label = QLabel(f'Active Downloads  ({len(self.active_tasks)})')
        self.downloads_active_count_label.setObjectName('panelTitle')
        layout.addWidget(self.downloads_active_count_label)
        self.downloads_active_container, self.downloads_active_layout = self._make_list_container()
        layout.addWidget(self.downloads_active_container, 1)

        return page

    def _start_batch_download(self):
        urls = [u.strip() for u in self.batch_input.toPlainText().split('\n') if u.strip()]
        if not urls:
            QMessageBox.warning(self, 'Input Needed', 'Please paste at least one video URL!')
            return
        fmt = self.dl_format_combo.currentText()
        quality = self.dl_quality_combo.currentText()
        audio = self.dl_audio_combo.currentText()
        subtitle = f"{fmt.split(' ')[0]} \u2022 {quality if fmt.startswith('MP4') else audio}"
        for url in urls:
            options = build_download_options(
                self.download_path, fmt, quality, audio, self.multi_threading, self.auto_convert)
            self._queue_task(url, options, url, subtitle)
        self.batch_input.clear()

    # ------------------------------------------------------------------
    # History page
    # ------------------------------------------------------------------

    def build_history_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        header_row = QHBoxLayout()
        header = QLabel('History')
        header.setObjectName('pageTitle')
        header_row.addWidget(header)
        header_row.addStretch()
        clear_btn = QPushButton(' Clear All')
        clear_btn.setObjectName('dangerBtn')
        set_btn_icon(clear_btn, 'trash', 14, 'danger')
        clear_btn.clicked.connect(self._clear_history)
        header_row.addWidget(clear_btn)
        layout.addLayout(header_row)

        sub = QLabel('Every video you have downloaded with WizVid, kept across sessions.')
        sub.setObjectName('muted')
        layout.addWidget(sub)

        self.history_container, self.history_layout = self._make_list_container()
        layout.addWidget(self.history_container, 1)
        return page

    def _rebuild_history_list(self):
        layout = self.history_layout
        while layout.count() > 1:
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        if not self.history:
            empty = QLabel('No downloads yet. Finished downloads will show up here.')
            empty.setObjectName('muted')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.insertWidget(0, empty)
            return
        for i, entry in enumerate(self.history):
            layout.insertWidget(i, self._make_history_row(entry))

    def _make_history_row(self, entry):
        row = QFrame()
        row.setObjectName('downloadItem')
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 12, 12, 12)
        completed = entry.get('status') == 'Completed'
        icon = QLabel()
        icon.setProperty('wiz_icon', 'circle-check' if completed else 'circle-x')
        icon.setProperty('wiz_icon_size', 16)
        icon.setProperty('wiz_icon_color', '#22c55e' if completed else '#ef4444')
        apply_widget_icon(icon)
        h.addWidget(icon)
        info = QVBoxLayout()
        title = QLabel(entry.get('title', 'Untitled'))
        title.setObjectName('itemTitle')
        title.setWordWrap(True)
        meta = QLabel(f"{entry.get('subtitle', '')}  \u2022  {entry.get('date', '')}")
        meta.setObjectName('itemMeta')
        info.addWidget(title)
        info.addWidget(meta)
        h.addLayout(info, 1)
        open_btn = QPushButton()
        open_btn.setObjectName('iconBtn')
        set_btn_icon(open_btn, 'folder-open', 15, 'text')
        path = entry.get('path') or os.path.expanduser('~')
        open_btn.clicked.connect(lambda checked=False, p=path: QDesktopServices.openUrl(QUrl.fromLocalFile(p)))
        h.addWidget(open_btn)
        return row

    def _clear_history(self):
        if not self.history:
            return
        resp = QMessageBox.question(
            self, 'Clear History', 'Delete all download history? This cannot be undone.')
        if resp == QMessageBox.StandardButton.Yes:
            self.history.clear()
            self._save_history()
            self.refresh_history_list()

    # ------------------------------------------------------------------
    # Settings page
    # ------------------------------------------------------------------

    def build_settings_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 24)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setSpacing(18)

        header = QLabel('Settings')
        header.setObjectName('pageTitle')
        layout.addWidget(header)

        # Appearance -----------------------------------------------------
        appearance_card = QFrame()
        appearance_card.setObjectName('card')
        ac = QVBoxLayout(appearance_card)
        ac.setContentsMargins(18, 18, 18, 18)
        ac.setSpacing(12)
        ac_title = icon_text('palette', 'Appearance', 15, 'accent1', 'sectionTitle')
        ac.addWidget(ac_title)
        theme_row = QHBoxLayout()
        theme_row.setSpacing(10)
        self.theme_buttons = {}
        for name in THEMES.keys():
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setChecked(name == self.theme_name)
            btn.clicked.connect(lambda checked, n=name: self.apply_theme(n))
            theme_row.addWidget(btn)
            self.theme_buttons[name] = btn
        ac.addLayout(theme_row)
        layout.addWidget(appearance_card)

        # Downloads --------------------------------------------------------
        dl_card = QFrame()
        dl_card.setObjectName('card')
        dc = QVBoxLayout(dl_card)
        dc.setContentsMargins(18, 18, 18, 18)
        dc.setSpacing(14)
        dc_title = icon_text('download', 'Downloads', 15, 'accent1', 'sectionTitle')
        dc.addWidget(dc_title)

        folder_row = QHBoxLayout()
        self.settings_path_field = QLineEdit(self.download_path)
        self.settings_path_field.setReadOnly(True)
        folder_row.addWidget(self.settings_path_field, 1)
        browse_btn = QPushButton(' Browse')
        set_btn_icon(browse_btn, 'folder-open', 14, 'text')
        browse_btn.clicked.connect(self.select_folder)
        folder_row.addWidget(browse_btn)
        dc.addLayout(self._settings_field_row('Default Download Folder', folder_row))

        combos_row = QHBoxLayout()
        combos_row.setSpacing(14)
        self.settings_format_combo = self._labeled_combo(
            combos_row, 'Default Format', self.FORMAT_OPTIONS, self.default_format)
        self.settings_quality_combo = self._labeled_combo(
            combos_row, 'Default Quality', self.QUALITY_OPTIONS, self.default_quality)
        self.settings_audio_combo = self._labeled_combo(
            combos_row, 'Default Audio Bitrate', self.AUDIO_OPTIONS, self.default_audio)
        self.settings_format_combo.currentTextChanged.connect(self._on_settings_format_changed)
        self.settings_quality_combo.currentTextChanged.connect(self._on_settings_quality_changed)
        self.settings_audio_combo.currentTextChanged.connect(self._on_settings_audio_changed)
        dc.addLayout(combos_row)

        concurrency_row = QHBoxLayout()
        concurrency_lbl = QLabel('Max Concurrent Downloads')
        concurrency_lbl.setObjectName('muted')
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 6)
        self.concurrency_spin.setValue(self.max_concurrent)
        self.concurrency_spin.valueChanged.connect(self._on_concurrency_changed)
        concurrency_row.addWidget(concurrency_lbl)
        concurrency_row.addStretch()
        concurrency_row.addWidget(self.concurrency_spin)
        dc.addLayout(concurrency_row)

        self.settings_auto_convert_toggle = ToggleSwitch(checked=self.auto_convert)
        self.settings_auto_convert_toggle.toggled.connect(self._on_auto_convert_toggled)
        dc.addLayout(self._toggle_row(
            'Auto Convert', 'Automatically remux/convert to the chosen format',
            self.settings_auto_convert_toggle))

        self.settings_multi_thread_toggle = ToggleSwitch(checked=self.multi_threading)
        self.settings_multi_thread_toggle.toggled.connect(self._on_multi_thread_toggled)
        dc.addLayout(self._toggle_row(
            'Multi-Threading', 'Use multiple connections for faster downloads',
            self.settings_multi_thread_toggle))

        layout.addWidget(dl_card)

        # Behavior -----------------------------------------------------
        beh_card = QFrame()
        beh_card.setObjectName('card')
        beh = QVBoxLayout(beh_card)
        beh.setContentsMargins(18, 18, 18, 18)
        beh.setSpacing(14)
        beh_title = icon_text('bell', 'Behavior', 15, 'accent1', 'sectionTitle')
        beh.addWidget(beh_title)

        self.notify_toggle = ToggleSwitch(checked=self.notify_complete)
        self.notify_toggle.toggled.connect(self._on_notify_toggled)
        beh.addLayout(self._toggle_row(
            'Notify on Completion', 'Show a notification when a download finishes',
            self.notify_toggle))

        self.auto_update_toggle = ToggleSwitch(checked=self.auto_update_ytdlp)
        self.auto_update_toggle.toggled.connect(self._on_auto_update_toggled)
        beh.addLayout(self._toggle_row(
            'Auto-check for Updates', 'Keep yt-dlp updated automatically on launch',
            self.auto_update_toggle))

        self.tray_toggle = ToggleSwitch(checked=self.minimize_to_tray)
        self.tray_toggle.toggled.connect(self._on_tray_toggled)
        beh.addLayout(self._toggle_row(
            'Minimize to Tray', 'Keep WizVid running in the system tray when closed',
            self.tray_toggle))

        layout.addWidget(beh_card)

        # History -----------------------------------------------------
        hist_card = QFrame()
        hist_card.setObjectName('card')
        hc = QVBoxLayout(hist_card)
        hc.setContentsMargins(18, 18, 18, 18)
        hc.setSpacing(14)
        hc_title = icon_text('history', 'History', 15, 'accent1', 'sectionTitle')
        hc.addWidget(hc_title)

        self.keep_history_toggle = ToggleSwitch(checked=self.keep_history)
        self.keep_history_toggle.toggled.connect(self._on_keep_history_toggled)
        hc.addLayout(self._toggle_row(
            'Keep Download History', 'Save a record of everything you download',
            self.keep_history_toggle))

        retention_row = QHBoxLayout()
        retention_lbl = QLabel('Auto-clear History')
        retention_lbl.setObjectName('muted')
        self.retention_combo = QComboBox()
        self.retention_combo.addItems(['Never', 'After 7 days', 'After 30 days', 'After 90 days'])
        idx = self.retention_combo.findText(self.history_retention)
        if idx != -1:
            self.retention_combo.setCurrentIndex(idx)
        self.retention_combo.currentTextChanged.connect(self._on_retention_changed)
        retention_row.addWidget(retention_lbl)
        retention_row.addStretch()
        retention_row.addWidget(self.retention_combo)
        hc.addLayout(retention_row)

        clear_hist_btn = QPushButton(' Clear History Now')
        clear_hist_btn.setObjectName('dangerBtn')
        set_btn_icon(clear_hist_btn, 'trash', 14, 'danger')
        clear_hist_btn.clicked.connect(self._clear_history)
        hc.addWidget(clear_hist_btn)

        layout.addWidget(hist_card)

        # About -----------------------------------------------------
        about_card = QFrame()
        about_card.setObjectName('card')
        abt = QVBoxLayout(about_card)
        abt.setContentsMargins(18, 18, 18, 18)
        abt.setSpacing(10)
        abt_title = icon_text('info', 'About', 15, 'accent1', 'sectionTitle')
        abt.addWidget(abt_title)
        version_row = QHBoxLayout()
        version_row.setSpacing(4)
        version_lbl = QLabel('WizVid v2.0 — Made with')
        version_lbl.setObjectName('muted')
        version_row.addWidget(version_lbl)
        heart_lbl = QLabel()
        heart_lbl.setProperty('wiz_icon', 'heart')
        heart_lbl.setProperty('wiz_icon_size', 13)
        heart_lbl.setProperty('wiz_icon_color', 'accent1')
        apply_widget_icon(heart_lbl)
        version_row.addWidget(heart_lbl)
        version_tail = QLabel('for dreamers')
        version_tail.setObjectName('muted')
        version_row.addWidget(version_tail)
        version_row.addStretch()
        version_wrap = QWidget()
        version_wrap.setLayout(version_row)
        abt.addWidget(version_wrap)
        credit_lbl = QLabel('<a href="https://rizve.netlify.app/" style="color:#8b5cf6;">Visit creator\'s page</a>')
        credit_lbl.setOpenExternalLinks(True)
        abt.addWidget(credit_lbl)
        about_btns = QHBoxLayout()
        check_update_btn = QPushButton(' Check for yt-dlp Updates')
        set_btn_icon(check_update_btn, 'refresh-cw', 14, 'text')
        check_update_btn.clicked.connect(self._start_ytdlp_update_check)
        reset_btn = QPushButton(' Reset All Settings')
        reset_btn.setObjectName('dangerBtn')
        set_btn_icon(reset_btn, 'rotate-ccw', 14, 'danger')
        reset_btn.clicked.connect(self._reset_settings)
        about_btns.addWidget(check_update_btn)
        about_btns.addWidget(reset_btn)
        abt.addLayout(about_btns)
        layout.addWidget(about_card)

        layout.addStretch()
        return page

    # ------------------------------------------------------------------
    # Settings change handlers (kept in sync across Home / Downloads / Settings)
    # ------------------------------------------------------------------

    def apply_theme(self, name):
        if name not in THEMES:
            return
        self.theme_name = name
        self.settings.setValue('theme_name', name)
        global CURRENT_THEME
        CURRENT_THEME = THEMES[name]
        _icon_cache.clear()
        self.setStyleSheet(build_stylesheet(THEMES[name]))
        if hasattr(self, 'theme_buttons'):
            for n, btn in self.theme_buttons.items():
                btn.setChecked(n == name)
        self._refresh_icons()

    def _refresh_icons(self):
        """Re-tint every icon widget after a theme change."""
        for w in self.findChildren(QWidget):
            if w.property('wiz_icon'):
                apply_widget_icon(w)
            elif w.property('wiz_logo'):
                w.setPixmap(logo_pixmap(int(w.property('wiz_logo'))))
        if hasattr(self, 'home_active_layout'):
            self.refresh_active_list()
            self.refresh_completed_list()
        if hasattr(self, 'history_layout'):
            self.refresh_history_list()
        if self.tray_icon is not None:
            self.tray_icon.setIcon(QIcon(logo_pixmap(64)))

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Select Download Folder', self.download_path)
        if folder:
            self.download_path = folder
            self.settings.setValue('download_path', folder)
            if hasattr(self, 'path_field'):
                self.path_field.setText(folder)
            if hasattr(self, 'settings_path_field'):
                self.settings_path_field.setText(folder)
            if hasattr(self, 'qs_folder_label'):
                self.qs_folder_label.setText(self._short_path(folder))

    def _on_settings_format_changed(self, text):
        self.default_format = text
        self.settings.setValue('default_format', text)
        short = 'MP4' if text.startswith('MP4') else 'MP3'
        for combo_name in ('format_combo', 'dl_format_combo', 'settings_format_combo'):
            combo = getattr(self, combo_name, None)
            if combo and combo.currentText() != text:
                idx = combo.findText(text)
                if idx != -1:
                    combo.setCurrentIndex(idx)
        if hasattr(self, 'qs_format_combo') and self.qs_format_combo.currentText() != short:
            self.qs_format_combo.setCurrentText(short)

    def _on_quick_format_changed(self, text):
        full = self.FORMAT_OPTIONS[0] if text == 'MP4' else self.FORMAT_OPTIONS[1]
        self._on_settings_format_changed(full)

    def _on_settings_quality_changed(self, text):
        self.default_quality = text
        self.settings.setValue('default_quality', text)
        for combo_name in ('quality_combo', 'dl_quality_combo', 'settings_quality_combo'):
            combo = getattr(self, combo_name, None)
            if combo and combo.currentText() != text:
                idx = combo.findText(text)
                if idx != -1:
                    combo.setCurrentIndex(idx)

    def _on_settings_audio_changed(self, text):
        self.default_audio = text
        self.settings.setValue('default_audio', text)
        for combo_name in ('audio_combo', 'dl_audio_combo', 'settings_audio_combo'):
            combo = getattr(self, combo_name, None)
            if combo and combo.currentText() != text:
                idx = combo.findText(text)
                if idx != -1:
                    combo.setCurrentIndex(idx)

    def _on_auto_convert_toggled(self, checked):
        self.auto_convert = checked
        self.settings.setValue('auto_convert', checked)
        for name in ('auto_convert_toggle', 'settings_auto_convert_toggle'):
            w = getattr(self, name, None)
            if w and w.isChecked() != checked:
                w.setChecked(checked)

    def _on_multi_thread_toggled(self, checked):
        self.multi_threading = checked
        self.settings.setValue('multi_threading', checked)
        for name in ('multi_thread_toggle', 'settings_multi_thread_toggle'):
            w = getattr(self, name, None)
            if w and w.isChecked() != checked:
                w.setChecked(checked)

    def _on_concurrency_changed(self, value):
        self.max_concurrent = value
        self.settings.setValue('max_concurrent', value)
        self._drain_pending()

    def _on_notify_toggled(self, checked):
        self.notify_complete = checked
        self.settings.setValue('notify_complete', checked)

    def _on_auto_update_toggled(self, checked):
        self.auto_update_ytdlp = checked
        self.settings.setValue('auto_update_ytdlp', checked)

    def _on_tray_toggled(self, checked):
        self.minimize_to_tray = checked
        self.settings.setValue('minimize_to_tray', checked)
        if checked and not self.tray_icon:
            self._setup_tray_icon()
        elif not checked and self.tray_icon:
            self.tray_icon.hide()
            self.tray_icon = None

    def _on_keep_history_toggled(self, checked):
        self.keep_history = checked
        self.settings.setValue('keep_history', checked)

    def _on_retention_changed(self, text):
        self.history_retention = text
        self.settings.setValue('history_retention', text)
        self._prune_history_by_retention()
        self.refresh_history_list()

    def _reset_settings(self):
        resp = QMessageBox.question(
            self, 'Reset Settings',
            'Reset all settings to their defaults? Your download history will be kept.')
        if resp == QMessageBox.StandardButton.Yes:
            history_json = self.settings.value('history_json', '[]')
            self.settings.clear()
            self.settings.setValue('history_json', history_json)
            QMessageBox.information(
                self, 'Settings Reset',
                'Settings have been reset. Please restart WizVid for all changes to apply.')

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _setup_tray_icon(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon_pixmap = logo_pixmap(64)
        self.tray_icon = QSystemTrayIcon(QIcon(icon_pixmap), self)
        menu = QMenu()
        show_action = menu.addAction('Show WizVid')
        show_action.triggered.connect(self.showNormal)
        quit_action = menu.addAction('Quit')
        quit_action.triggered.connect(self.quit_app)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(
            lambda reason: self.showNormal()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray_icon.show()

    def closeEvent(self, event):
        if self.minimize_to_tray and self.tray_icon:
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                'WizVid', 'Still running in the tray.',
                QSystemTrayIcon.MessageIcon.Information, 2000)
        else:
            if self.active_tasks:
                self._cancel_all_downloads(wait=True)
            event.accept()

    def quit_app(self):
        if self.active_tasks:
            self._cancel_all_downloads(wait=True)
        QApplication.quit()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = VideoDownloader()
    window.show()
    sys.exit(app.exec())