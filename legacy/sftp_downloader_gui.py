import tkinter as tk
from tkinter import messagebox, ttk, simpledialog, filedialog
import customtkinter as ctk
import paramiko
import threading
from queue import Queue
import json
import os
import re
import requests
import pyperclip
import time
import shlex
import io
import socket
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from PIL import Image, ImageTk
from datetime import datetime, timedelta

# Set appearance and theme
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Modern UI Constants
COLOR_BG = "#0B0E14"
COLOR_SIDEBAR = "#06080A"
COLOR_CARD = "#161B22"
COLOR_ACCENT = "#38BDF8"
COLOR_TEXT = "#F8FAFC"
COLOR_TEXT_DIM = "#94A3B8"
COLOR_BORDER = "#30363D"
COLOR_SUCCESS = "#10B981"
COLOR_DANGER = "#EF4444"

_config_dir = os.path.join(os.environ.get("APPDATA", os.getcwd()), "SFTP Studio") if os.name == "nt" else os.getcwd()
os.makedirs(_config_dir, exist_ok=True)
CONFIG_FILE = os.path.join(_config_dir, "config.json")
# Keep direct compatibility with older portable installs.
if os.path.exists("config.json"):
    CONFIG_FILE = "config.json"

class FirefoxDownloadHandler(BaseHTTPRequestHandler):
    """Local-only bridge used by the Firefox extension."""
    app = None

    def _headers(self, status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
        self.end_headers()

    def do_OPTIONS(self):
        self._headers(204)

    def do_GET(self):
        if self.path.rstrip("/") == "/api/firefox/health":
            self._headers()
            self.wfile.write(json.dumps({"ok": True, "service": "Firefox Downloader"}).encode())
        else:
            self._headers(404)
            self.wfile.write(b'{"ok":false,"error":"not_found"}')

    def do_POST(self):
        if self.path.rstrip("/") != "/api/firefox/download":
            self._headers(404); self.wfile.write(b'{"ok":false,"error":"not_found"}'); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            url = str(payload.get("url", "")).strip()
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("Only http/https URLs are accepted")
            if self.app is None:
                raise RuntimeError("Application is not ready")
            self.app.after(0, lambda p=payload: self.app.receive_firefox_download(p))
            self._headers(202)
            self.wfile.write(json.dumps({"ok": True, "queued": True}).encode())
        except Exception as exc:
            self._headers(400)
            self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode())

    def log_message(self, fmt, *args):
        return

def normalize_download_url(url):
    """Use HTTP for remote downloader requests when the user enabled this policy."""
    value = str(url or "").strip()
    if value.lower().startswith("https://"):
        return "http://" + value[8:]
    return value

class SSHSocks5Proxy:
    """Local SOCKS5 endpoint; outbound connections are opened through SSH."""
    def __init__(self, host, port, user, password):
        self.host, self.port, self.user, self.password = host, int(port), user, password
        self.ssh = None; self.transport = None; self.listener = None; self.port_local = None; self.running = False

    def start(self):
        self.ssh = paramiko.SSHClient(); self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(self.host, port=self.port, username=self.user, password=self.password, timeout=15)
        self.transport = self.ssh.get_transport()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM); self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0)); self.listener.listen(40); self.port_local = self.listener.getsockname()[1]; self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start(); return self.port_local

    def _accept_loop(self):
        while self.running:
            try: client, addr = self.listener.accept()
            except OSError: break
            threading.Thread(target=self._handle_client, args=(client, addr), daemon=True).start()

    def _handle_client(self, client, addr):
        channel = None
        try:
            client.settimeout(20)
            if client.recv(1) != b"\x05": return
            methods = client.recv(1); count = methods[0] if methods else 0; client.recv(count); client.sendall(b"\x05\x00")
            header = client.recv(4)
            if len(header) != 4 or header[1] != 1: return
            atyp = header[3]
            if atyp == 1: host = socket.inet_ntoa(client.recv(4))
            elif atyp == 3:
                size = client.recv(1)[0]; host = client.recv(size).decode("idna")
            elif atyp == 4: host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
            else: return
            dest_port = int.from_bytes(client.recv(2), "big")
            channel = self.transport.open_channel("direct-tcpip", (host, dest_port), addr)
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"); client.settimeout(None); channel.settimeout(None)
            self._relay(client, channel)
        except Exception:
            try: client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            except Exception: pass
        finally:
            for sock in (client, channel):
                try:
                    if sock: sock.close()
                except Exception: pass

    def _relay(self, left, right):
        def pipe(src, dst):
            try:
                while self.running:
                    data = src.recv(65536)
                    if not data: break
                    dst.sendall(data)
            except Exception: pass
            try: dst.shutdown(socket.SHUT_WR)
            except Exception: pass
        thread = threading.Thread(target=pipe, args=(left, right), daemon=True); thread.start(); pipe(right, left)

    def stop(self):
        self.running = False
        try: self.listener.close()
        except Exception: pass
        try: self.ssh.close()
        except Exception: pass

def local_debug_log(msg):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_file = os.path.join(os.getcwd(), "debug_commands.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n{'-'*80}\n")
    except: pass

TRANSLATIONS = {
    "Arabic": {
        "title": "استوديو SFTP & M3U & Transcode الاحترافي",
        "sidebar_title": "SFTP STUDIO",
        "tab_downloader": "استوديو التحميل",
        "tab_transcoder": "استوديو المعالجة",
        "tab_m3u": "مستخرج M3U والتحميل الذكي",
        "tab_mavin": "Mavin Smart Downloader",
        "tab_cobra": "Cobra Smart Downloader",
        "tab_cobra": "Cobra Smart Downloader",
        "current_profile": "السيرفر الحالي:",
        "host": "Host / IP:",
        "port": "Port:",
        "user": "Username:",
        "pass": "Password:",
        "save_settings": "حفظ الإعدادات",
        "add_server": "إضافة سيرفر جديد",
        "del_server": "حذف السيرفر",
        "lang_label": "اللغة (Language):",
        "trans_settings_title": " إعدادات معالجة الفيديو (FFmpeg) ",
        "logo_tr": "لوجو (أعلى-يمين):",
        "logo_tl": "لوجو (أعلى-يسار):",
        "logo_br": "لوجو (أسفل-يمين):",
        "logo_bl": "لوجو (أسفل-يسار):",
        "intro_path": "مسار صورة البداية (Intro):",
        "intro_toggle": "تفعيل الانترو",
        "trim_toggle": "حذف بداية الفيديو",
        "trim_seconds": "الثواني المراد حذفها:",
        "auto_transcode_downloads": "تطبيق البروفايل على تنزيلات الأقسام",
        "m3u_auto_transcode": "تطبيق الترانسكود على تنزيلات M3U",
        "mavin_auto_transcode": "تطبيق الترانسكود على تنزيلات Mavin",
        "cobra_auto_transcode": "تطبيق الترانسكود على تنزيلات Cobra",
        "resolution": "دقة العرض:",
        "bitrate": "البيت ريت (kbps):",
        "preset": "سرعة المعالجة (CPU):",
        "bulk_title": " معالجة انتقائية للمجلد (استبدال الأصل) ",
        "bulk_path_label": "مسار المجلد على السيرفر:",
        "btn_scan_bulk": "فحص المجلد وعرض الملفات",
        "btn_select_all": "تحديد الكل",
        "btn_select_none": "إلغاء الكل",
        "btn_start_bulk": "بدء معالجة الملفات المختارة",
        "btn_view_log": "Live Debug Console",
        "links_title": " قائمة التحميلات (10 روابط) ",
        "col_process": "معالجة؟",
        "col_m3u8": "M3U8?",
        "col_url": "رابط الملف (URL / M3U8)",
        "col_path": "مسار الحفظ النهائي",
        "col_name": "الاسم المخصص",
        "mode_seq": "متتالي",
        "mode_par": "متوازي",
        "btn_start": "بدء التحميل",
        "btn_monitor": "تحديث الحالة الحية",
        "btn_clear": "مسح الروابط",
        "btn_load_file": "تحميل من ملف links.txt",
        "auto_refresh": "تحديث تلقائي (5 ثوانٍ)",
        "monitor_title": " مراقبة العمليات الحالية على السيرفر ",
        "mon_col_file": "الملف",
        "mon_col_status": "الحالة",
        "mon_col_info": "التفاصيل",
        "m3u_title": " إدارة وفحص روابط M3U & IPTV ",
        "m3u_url_label": "رابط M3U:",
        "m3u_cat_label": "القسم (Category):",
        "m3u_search_label": "كلمة البحث:",
        "btn_m3u_search": "فحص",
        "m3u_path_label": "مسارات الحفظ الذكية (20 مساراً مقسمة):",
        "mode_series": "مسارات المسلسلات",
        "mode_vod": "مسارات الأفلام",
        "path_mode_series": "Series",
        "path_mode_vod": "VOD",
        "btn_m3u_select_all": "تحديد الكل",
        "btn_m3u_select_none": "إلغاء التحديد",
        "btn_m3u_download": "بدء تحميل المختار (متتالي)",
        "btn_check_new": "Check for New",
        "btn_m3u_clear": "مسح النتائج",
        "m3u_results_title": " النتائج المستخرجة (اختر الحلقات والمسار) ",
        "msg_success": "تم حفظ الإعدادات بنجاح!",
        "msg_start": "[+] تم بدء العمليات بنظام: ",
        "msg_bulk_start": "[+] بدأت معالجة الملفات المختارة... راقب السجل لمعرفة التفاصيل",
        "msg_m3u_start": "[+] بدأ تحميل الحلقات المختارة من M3U بشكل متتالي مع التسمية التلقائية...",
        "msg_error": "خطأ: ",
        "placeholder_url": "رابط مباشر أو M3U8...",
        "placeholder_path": "/home/downloads/",
        "placeholder_name": "اسم اختياري...",
        "m3u_searching": "جاري البحث في ملفات M3U... يرجى الانتظار",
        "menu_cut": "قص",
        "menu_copy": "نسخ",
        "menu_paste": "لصق",
        "menu_select_all": "تحديد الكل",
        "path_no": "مسار",
        "btn_fetch_cats": "تحديث الأقسام",
        "fetching_cats": "جاري جلب الأقسام...",
        "auto_check": "فحص آلي",
        "interval": "كل:",
        "codec_label": "كوديك الفيديو:",
        "bit_depth_label": "عمق الألوان (Bit Depth):",
        "xtream_title": " Xtream API (بيانات السيرفر) ",
        "xtream_host": "رابط السيرفر (Host):",
        "xtream_user": "المستخدم:",
        "xtream_pass": "كلمة المرور:",
        "tab_firefox": "Firefox Downloader",
        "firefox_title": "Firefox Downloader - تنزيل مباشر من المتصفح",
        "firefox_path": "مجلد الحفظ على السيرفر:",
        "firefox_auto": "التنزيل التلقائي عند استقبال الرابط",
        "firefox_port": "منفذ استقبال الإضافة:",
        "firefox_start": "تشغيل جسر Firefox",
        "firefox_stop": "إيقاف جسر Firefox",
        "firefox_status": "حالة الجسر:",
        "firefox_pending": "الروابط المستلمة",
        "firefox_install": "ملفات إضافة Firefox موجودة في مجلد Firefox_extension",
        "firefox_empty_path": "حدد مجلد الحفظ أولاً"
        ,"tab_browser": "متصفح السيرفر الهجين"
        ,"browser_title": "متصفح محلي عبر اتصال السيرفر"
        ,"browser_profile": "ملف المتصفح المحلي:"
        ,"browser_start": "تشغيل المتصفح عبر السيرفر"
        ,"browser_stop": "إيقاف المتصفح والنفق"
        ,"browser_status": "حالة المتصفح:"
    },
    "English": {
        "title": "Manus SFTP, M3U & Transcode Studio",
        "sidebar_title": "SFTP STUDIO",
        "tab_downloader": "Downloader",
        "tab_transcoder": "Transcode Studio",
        "tab_m3u": "M3U Scraper & Smart Downloader",
        "tab_mavin": "Mavin Smart Downloader",
        "tab_cobra": "Cobra Smart Downloader",
        "current_profile": "Current Profile:",
        "host": "Host / IP:",
        "port": "Port:",
        "user": "Username:",
        "pass": "Password:",
        "save_settings": "Save Settings",
        "add_server": "Add New Server",
        "del_server": "Delete Server",
        "lang_label": "Language:",
        "trans_settings_title": " Video Processing Settings (FFmpeg) ",
        "logo_tr": "Logo (Top-Right):",
        "logo_tl": "Logo (Top-Left):",
        "logo_br": "Logo (Bottom-Right):",
        "logo_bl": "Logo (Bottom-Left):",
        "intro_path": "Intro Image Path:",
        "intro_toggle": "Enable Intro",
        "trim_toggle": "Trim Video Start",
        "trim_seconds": "Seconds to remove:",
        "auto_transcode_downloads": "Use profile for section downloads",
        "m3u_auto_transcode": "Transcode M3U downloads",
        "mavin_auto_transcode": "Transcode Mavin downloads",
        "cobra_auto_transcode": "Transcode Cobra downloads",
        "resolution": "Resolution:",
        "bitrate": "Bitrate (kbps):",
        "preset": "Processing Speed (CPU):",
        "bulk_title": " Selective Folder Processing (Overwrite Original) ",
        "bulk_path_label": "Server Directory Path:",
        "btn_scan_bulk": "Scan Folder & Show Files",
        "btn_select_all": "Select All",
        "btn_select_none": "Select None",
        "btn_start_bulk": "Start Processing Selected Files",
        "btn_view_log": "Live Debug Console",
        "links_title": " Download List (10 Links) ",
        "col_process": "Process?",
        "col_m3u8": "M3U8?",
        "col_url": "File URL / M3U8",
        "col_path": "Final Save Path",
        "col_name": "Custom Name",
        "mode_seq": "Sequential",
        "mode_par": "Parallel",
        "btn_start": "Start Download",
        "btn_monitor": "Live Monitor Update",
        "btn_clear": "Clear URLs",
        "btn_load_file": "Load from links.txt",
        "auto_refresh": "Auto Refresh (5s)",
        "monitor_title": " Live Server Operations Monitor ",
        "mon_col_file": "File",
        "mon_col_status": "Status",
        "mon_col_info": "Details",
        "m3u_title": " M3U & IPTV Links Manager ",
        "m3u_url_label": "M3U URL:",
        "m3u_cat_label": "Category:",
        "m3u_search_label": "Keyword:",
        "btn_m3u_search": "Scan",
        "m3u_path_label": "Smart Save Paths (20 Split Paths):",
        "mode_series": "Series Paths",
        "mode_vod": "Movies (VOD) Paths",
        "path_mode_series": "Series",
        "path_mode_vod": "VOD",
        "btn_m3u_select_all": "Select All",
        "btn_m3u_select_none": "Select None",
        "btn_m3u_download": "Start Download Selected (Sequential)",
        "btn_check_new": "Check for New",
        "btn_m3u_clear": "Clear Results",
        "m3u_results_title": " Extracted Results (Select Episodes & Path) ",
        "msg_success": "Settings saved successfully!",
        "msg_start": "[+] Operations started in mode: ",
        "msg_bulk_start": "[+] Selected files processing started... Check log for details",
        "msg_m3u_start": "[+] Started sequential M3U download with auto-renaming...",
        "msg_error": "Error: ",
        "placeholder_url": "Direct link or M3U8...",
        "placeholder_path": "/home/downloads/",
        "placeholder_name": "Optional name...",
        "m3u_searching": "Searching M3U files... Please wait",
        "menu_cut": "Cut",
        "menu_copy": "Copy",
        "menu_paste": "Paste",
        "menu_select_all": "Select All",
        "path_no": "Path",
        "btn_fetch_cats": "Refresh Categories",
        "fetching_cats": "Fetching categories...",
        "auto_check": "Auto Check",
        "interval": "Every:",
        "codec_label": "Video Codec:",
        "bit_depth_label": "Bit Depth:",
        "xtream_title": " Xtream API (Server Data) ",
        "xtream_host": "Server Host:",
        "xtream_user": "Username:",
        "xtream_pass": "Password:",
        "tab_firefox": "Firefox Downloader",
        "firefox_title": "Firefox Downloader - Direct Browser Capture",
        "firefox_path": "Server save folder:",
        "firefox_auto": "Automatically download received links",
        "firefox_port": "Extension listener port:",
        "firefox_start": "Start Firefox Bridge",
        "firefox_stop": "Stop Firefox Bridge",
        "firefox_status": "Bridge status:",
        "firefox_pending": "Received links",
        "firefox_install": "Firefox extension files are in the Firefox_extension folder",
        "firefox_empty_path": "Select a server save folder first"
        ,"tab_browser": "Hybrid Server Browser"
        ,"browser_title": "Local Browser over Server Connection"
        ,"browser_profile": "Local browser profile:"
        ,"browser_start": "Start Browser via Server"
        ,"browser_stop": "Stop Browser and Tunnel"
        ,"browser_status": "Browser status:"
    }
}

class ToolTip:
    def __init__(self, widget, text, image_url=None, large_text=None):
        self.widget = widget; self.text = text; self.image_url = image_url; self.large_text = large_text
        self.tip_window = None; self.image_obj = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)
        self.widget.bind("<Button-1>", self.hide_tip) # Hide on click
    def show_tip(self, event=None):
        if self.tip_window: return
        # Add a small delay to avoid flicker
        self.widget.after(300, self._create_tip)
        
    def _create_tip(self):
        # Check if mouse is still over widget
        x, y = self.widget.winfo_pointerxy()
        widget_x = self.widget.winfo_rootx()
        widget_y = self.widget.winfo_rooty()
        if not (widget_x <= x <= widget_x + self.widget.winfo_width() and widget_y <= y <= widget_y + self.widget.winfo_height()):
            return

        if self.tip_window: return
        tx = self.widget.winfo_rootx() + 20; ty = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tip_window = tw = tk.Toplevel(self.widget); tw.wm_overrideredirect(True); tw.wm_geometry(f"+{tx}+{ty}"); tw.attributes("-topmost", True); tw.configure(bg="#1e293b")
        
        main_f = tk.Frame(tw, bg="#1e293b", padx=10, pady=10, highlightthickness=1, highlightbackground="#38BDF8")
        main_f.pack()

        txt = self.large_text if self.large_text else self.text
        font = ("Segoe UI", 12, "bold") if self.large_text else ("Segoe UI", 9, "bold")
        self.label = tk.Label(main_f, text=txt, justify=tk.LEFT, background="#1e293b", foreground="#f8fafc", font=font, wraplength=300)
        self.label.pack(side="top", pady=5)

        if self.image_url:
            threading.Thread(target=self.load_image, args=(main_f,), daemon=True).start()

    def load_image(self, parent):
        try:
            r = requests.get(self.image_url, timeout=5); img_data = r.content
            img = Image.open(io.BytesIO(img_data)); img.thumbnail((150, 220))
            self.image_obj = ImageTk.PhotoImage(img)
            if self.tip_window:
                img_lbl = tk.Label(parent, image=self.image_obj, bg="#1e293b")
                img_lbl.pack(side="bottom", pady=5)
        except: pass

    def hide_tip(self, event=None):
        tw = self.tip_window; self.tip_window = None
        if tw: tw.destroy()

class LiveLogWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title(" Manus SFTP - Professional Live Debug Console ")
        self.geometry("1000x600")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.text = tk.Text(self, bg="#0c0c0c", fg="#F8FAFC", font=("Consolas", 10), padx=10, pady=10, insertbackground="white")
        self.text.pack(fill="both", expand=True)
        
        self.is_active = True
        self.last_progress_index = None # For updating progress in-place
        threading.Thread(target=self.stream_logs, daemon=True).start()

    def log_local(self, msg, color="#00ff00", is_progress=False):
        timestamp = datetime.now().strftime("%H:%M:%S")
        tag_name = f"tag_{color.replace('#', '')}"
        self.text.tag_config(tag_name, foreground=color)
        
        if is_progress and self.last_progress_index:
            # Update the same compact progress line instead of adding a new line.
            self.text.delete(self.last_progress_index, f"{self.last_progress_index} lineend")
            self.text.insert(self.last_progress_index, f"[{timestamp}] {msg}", tag_name)
        else:
            self.text.insert(tk.END, f"[{timestamp}] {msg}\n", tag_name)
            if is_progress:
                self.last_progress_index = self.text.index("end-2c linestart")
            else:
                self.last_progress_index = None
        self.text.see(tk.END)

    @staticmethod
    def _parse_curl_size(value):
        """Convert curl's human-readable size (k/M/G) to bytes."""
        match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?)(?:B)?$", value.strip(), re.I)
        if not match:
            return None
        number = float(match.group(1))
        unit = match.group(2).upper()
        multipliers = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
        return int(number * multipliers[unit])

    @staticmethod
    def _format_size(size):
        if size is None or size < 0:
            return "--"
        units = ("B", "KB", "MB", "GB", "TB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    def _curl_summary(self, line):
        """Return only the real total and remaining sizes from curl progress."""
        # Example: 34 295M 34 100M 0 0 ...
        match = re.search(r"\b\d+\s+([0-9.]+[KMGTP]?B?)\s+\d+\s+([0-9.]+[KMGTP]?B?)\b", line, re.I)
        if not match:
            return None
        total = self._parse_curl_size(match.group(1))
        received = self._parse_curl_size(match.group(2))
        if total is None or received is None or total <= 0:
            return None
        remaining = max(total - received, 0)
        return f"TOTAL {self._format_size(total)}    REMAIN {self._format_size(remaining)}"

    def stream_logs(self):
        while self.is_active:
            host, port, user, pw = self.parent.host_entry.get(), int(self.parent.port_entry.get() or 22), self.parent.user_entry.get(), self.parent.pass_entry.get()
            if not host: time.sleep(2); continue
            try:
                ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()); ssh.connect(host, port=port, username=user, password=pw, timeout=5)
                stdin, stdout, stderr = ssh.exec_command("tail -f -n 100 ~/wget_monitor.log")
                for line in stdout:
                    if not self.is_active: break
                    clean_line = line.strip()
                    if not clean_line: continue
                    
                    if clean_line.startswith("[EXEC]"):
                        self.after(0, lambda l=f"⚙️ COMMAND: {clean_line[6:].strip()}": self.log_local(l, "#ccff00"))
                    elif clean_line.startswith("% Total") or clean_line.startswith("Dload  Upload"):
                        # Hide curl's column headers from the compact UI.
                        continue
                    elif "Dload" in clean_line and "Upload" in clean_line:
                        continue
                    elif re.search(r"\b\d+\s+[0-9.]+[KMGTP]?B?\s+\d+\s+[0-9.]+[KMGTP]?B?\b", clean_line, re.I):
                        summary = self._curl_summary(clean_line)
                        if summary:
                            self.after(0, lambda l=f"📊 {summary}": self.log_local(l, "#00d4ff", is_progress=True))
                    elif "Downloading" in clean_line or "Processing" in clean_line:
                        self.after(0, lambda l=f"📦 TASK: {clean_line}": self.log_local(l, "#ffcc00"))
                    elif "Finished" in clean_line or "Success" in clean_line:
                        self.after(0, lambda l=f"✅ DONE: {clean_line}": self.log_local(l, "#28a745"))
                    elif "FAILED" in clean_line or "Error" in clean_line:
                        self.after(0, lambda l=f"❌ ALERT: {clean_line}": self.log_local(l, "#dc3545"))
                    else:
                        # Suppress raw curl progress, speed, and transfer columns in Live Debug.
                        continue
                ssh.close()
            except: time.sleep(5)

    def on_close(self):
        self.is_active = False
        self.parent.live_log_win = None
        self.destroy()

class SFTPDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.config = self.load_config()
        self.lang = self.config.get("global", {}).get("language", "Arabic")
        
        self.title(TRANSLATIONS[self.lang]["title"])
        self.geometry("1450x980")
        self.configure(fg_color=COLOR_BG)

        self.current_profile = tk.StringVar(value="Default")
        self.auto_refresh_active = False
        self.bulk_files = []
        self.m3u_items = [] 
        self.cached_m3u_content = {}
        self.live_log_win = None
        self.is_downloading = False
        # One FIFO queue shared by manual, M3U, and automatic download requests.
        self.download_queue = Queue()
        self.queue_pending = 0
        self.queue_lock = threading.Lock()
        self.firefox_server = None
        self.firefox_server_thread = None
        self.firefox_pending = []
        self.firefox_port = 8765
        self.browser_proxy = None
        self.browser_process = None
        threading.Thread(target=self._download_queue_worker, daemon=True).start()

        # Main Grid Layout
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Professional Sidebar ---
        self.sidebar_frame = ctk.CTkFrame(self, width=280, corner_radius=0, fg_color=COLOR_SIDEBAR, border_width=1, border_color=COLOR_BORDER)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(10, weight=1) # Spacer
        
        row_idx = 0
        # Logo Section
        logo_container = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        logo_container.grid(row=row_idx, column=0, padx=20, pady=(30, 20), sticky="ew"); row_idx += 1
        
        self.logo_icon = ctk.CTkLabel(logo_container, text="⚡", font=ctk.CTkFont(size=28))
        self.logo_icon.pack(side="left", padx=(0, 10))
        self.logo_label = ctk.CTkLabel(logo_container, text=TRANSLATIONS[self.lang]["sidebar_title"], font=ctk.CTkFont(size=22, weight="bold"), text_color=COLOR_ACCENT)
        self.logo_label.pack(side="left")

        # Navigation Buttons
        self.nav_btns = []
        nav_items = [
            ("tab_downloader", "📥", self.show_downloader),
            ("tab_transcoder", "🎬", self.show_transcoder),
            ("tab_m3u", "📺", self.show_m3u),
            ("tab_mavin", "🔐", self.show_mavin),
            ("tab_cobra", "🐍", self.show_cobra),
            ("tab_firefox", "🦊", self.show_firefox),
            ("tab_browser", "🌐", self.show_browser)
        ]
        
        for key, icon, cmd in nav_items:
            btn = ctk.CTkButton(self.sidebar_frame, text=f"  {icon}  {TRANSLATIONS[self.lang][key]}", 
                                 anchor="w", height=45, fg_color="transparent", text_color=COLOR_TEXT_DIM,
                                 hover_color=COLOR_CARD, font=ctk.CTkFont(size=14), command=cmd)
            btn.grid(row=row_idx, column=0, padx=15, pady=5, sticky="ew"); row_idx += 1
            self.nav_btns.append(btn)

        # Server Settings Section (Sidebar Bottom)
        row_idx = 11
        ctk.CTkLabel(self.sidebar_frame, text="⎯" * 20, text_color=COLOR_BORDER).grid(row=row_idx, column=0, pady=10); row_idx += 1
        
        self.prof_label = ctk.CTkLabel(self.sidebar_frame, text=TRANSLATIONS[self.lang]["current_profile"], font=ctk.CTkFont(size=12), text_color=COLOR_TEXT_DIM)
        self.prof_label.grid(row=row_idx, column=0, padx=20, sticky="w"); row_idx += 1
        self.profile_combo = ctk.CTkOptionMenu(self.sidebar_frame, values=list(self.config.keys()), command=self.on_profile_change, fg_color=COLOR_CARD, button_color=COLOR_ACCENT, button_hover_color=COLOR_ACCENT)
        self.profile_combo.grid(row=row_idx, column=0, padx=20, pady=(5, 15), sticky="ew"); row_idx += 1
        
        # Mini Entry Grid for Server
        server_grid = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        server_grid.grid(row=row_idx, column=0, padx=15, sticky="ew"); row_idx += 1
        
        self.host_entry = ctk.CTkEntry(server_grid, placeholder_text="Host", height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER)
        self.host_entry.pack(fill="x", pady=2); self.add_context_menu(self.host_entry)
        
        self.port_entry = ctk.CTkEntry(server_grid, placeholder_text="Port (22)", height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER)
        self.port_entry.pack(fill="x", pady=2); self.add_context_menu(self.port_entry)

        self.user_entry = ctk.CTkEntry(server_grid, placeholder_text="User", height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER)
        self.user_entry.pack(fill="x", pady=2); self.add_context_menu(self.user_entry)
        
        self.pass_entry = ctk.CTkEntry(server_grid, placeholder_text="Pass", show="*", height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER)
        self.pass_entry.pack(fill="x", pady=2); self.add_context_menu(self.pass_entry)

        self.save_btn = ctk.CTkButton(self.sidebar_frame, text=TRANSLATIONS[self.lang]["save_settings"], command=lambda: self.save_current_settings(silent=False), fg_color=COLOR_ACCENT, text_color=COLOR_BG, font=ctk.CTkFont(weight="bold"))
        self.save_btn.grid(row=row_idx, column=0, padx=20, pady=(15, 5), sticky="ew"); row_idx += 1
        
        # Profile Management Buttons
        prof_btn_grid = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        prof_btn_grid.grid(row=row_idx, column=0, padx=20, pady=5, sticky="ew"); row_idx += 1
        
        self.add_prof_btn = ctk.CTkButton(prof_btn_grid, text=TRANSLATIONS[self.lang]["add_server"], height=30, fg_color="transparent", border_width=1, border_color=COLOR_BORDER, command=self.add_new_profile, font=ctk.CTkFont(size=11))
        self.add_prof_btn.pack(side="left", fill="x", expand=True, padx=(0, 2))
        
        self.del_prof_btn = ctk.CTkButton(prof_btn_grid, text=TRANSLATIONS[self.lang]["del_server"], height=30, fg_color="transparent", border_width=1, border_color=COLOR_DANGER, text_color=COLOR_DANGER, command=self.delete_profile, font=ctk.CTkFont(size=11))
        self.del_prof_btn.pack(side="left", fill="x", expand=True, padx=(2, 0))

        # Language Toggle at the very bottom
        self.lang_btn = ctk.CTkButton(self.sidebar_frame, text=f"🌐 {self.lang}", height=30, fg_color="transparent", border_width=1, border_color=COLOR_BORDER, command=self.toggle_language)
        self.lang_btn.grid(row=row_idx, column=0, padx=20, pady=(10, 20), sticky="ew")

        # --- Main Content Area ---
        self.main_container = ctk.CTkFrame(self, fg_color=COLOR_BG, corner_radius=0)
        self.main_container.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(1, weight=1)

        # Header Section
        self.header_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        
        self.header_title = ctk.CTkLabel(self.header_frame, text="Dashboard", font=ctk.CTkFont(size=24, weight="bold"))
        self.header_title.pack(side="left")
        
        self.server_status_dot = ctk.CTkLabel(self.header_frame, text="●", text_color=COLOR_DANGER, font=ctk.CTkFont(size=20))
        self.server_status_dot.pack(side="right", padx=10)
        self.server_status_text = ctk.CTkLabel(self.header_frame, text="Server Offline", font=ctk.CTkFont(size=12), text_color=COLOR_TEXT_DIM)
        self.server_status_text.pack(side="right")
        self.queue_status_lbl = ctk.CTkLabel(self.header_frame, text="Queue: 0", font=ctk.CTkFont(size=12, weight="bold"), text_color=COLOR_ACCENT)
        self.queue_status_lbl.pack(side="right", padx=(10, 4))

        # View Frames
        self.view_downloader = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.view_transcoder = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.view_m3u = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.view_mavin = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.view_cobra = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.view_firefox = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.view_browser = ctk.CTkFrame(self.main_container, fg_color="transparent")

        self.setup_downloader_tab()
        self.setup_transcoder_tab()
        self.setup_m3u_tab()
        self.setup_mavin_tab()
        self.setup_cobra_tab()
        self.setup_firefox_tab()
        self.setup_browser_tab()
        
        self.show_downloader() # Default view
        self.load_profile_to_ui("Default")
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.start_firefox_bridge()
        
        threading.Thread(target=self.background_scheduler_loop, daemon=True).start()
        threading.Thread(target=self.mavin_background_loop, daemon=True).start()
        threading.Thread(target=self.check_server_health_loop, daemon=True).start()

    def show_downloader(self):
        self.header_title.configure(text=TRANSLATIONS[self.lang]["tab_downloader"])
        self.view_transcoder.grid_forget(); self.view_m3u.grid_forget(); self.view_mavin.grid_forget(); self.view_cobra.grid_forget(); self.view_firefox.grid_forget(); self.view_browser.grid_forget()
        self.view_downloader.grid(row=1, column=0, sticky="nsew")
        self.update_nav_highlight(0)

    def show_transcoder(self):
        self.header_title.configure(text=TRANSLATIONS[self.lang]["tab_transcoder"])
        self.view_downloader.grid_forget(); self.view_m3u.grid_forget(); self.view_mavin.grid_forget(); self.view_cobra.grid_forget(); self.view_firefox.grid_forget(); self.view_browser.grid_forget()
        self.view_transcoder.grid(row=1, column=0, sticky="nsew")
        self.update_nav_highlight(1)

    def show_m3u(self):
        self.header_title.configure(text=TRANSLATIONS[self.lang]["tab_m3u"])
        self.view_downloader.grid_forget(); self.view_transcoder.grid_forget(); self.view_mavin.grid_forget(); self.view_cobra.grid_forget(); self.view_firefox.grid_forget(); self.view_browser.grid_forget()
        self.view_m3u.grid(row=1, column=0, sticky="nsew")
        self.update_nav_highlight(2)

    def show_mavin(self):
        self.header_title.configure(text=TRANSLATIONS[self.lang].get("tab_mavin", "Mavin Smart Downloader"))
        self.view_downloader.grid_forget(); self.view_transcoder.grid_forget(); self.view_m3u.grid_forget(); self.view_cobra.grid_forget(); self.view_firefox.grid_forget(); self.view_browser.grid_forget()
        try: self.mavin_path_leave()
        except Exception: pass
        try: self.cobra_path_leave()
        except Exception: pass
        self.view_mavin.grid(row=1, column=0, sticky="nsew")
        self.update_nav_highlight(3)

    def show_cobra(self):
        self.header_title.configure(text=TRANSLATIONS[self.lang].get("tab_cobra", "Cobra Smart Downloader"))
        self.view_downloader.grid_forget(); self.view_transcoder.grid_forget(); self.view_m3u.grid_forget(); self.view_mavin.grid_forget(); self.view_firefox.grid_forget(); self.view_browser.grid_forget()
        try: self.mavin_path_leave()
        except Exception: pass
        try: self.cobra_path_leave()
        except Exception: pass
        self.view_cobra.grid(row=1, column=0, sticky="nsew")
        self.update_nav_highlight(4)

    def show_firefox(self):
        self.header_title.configure(text=TRANSLATIONS[self.lang]["tab_firefox"])
        for view in (self.view_downloader, self.view_transcoder, self.view_m3u, self.view_mavin, self.view_cobra): view.grid_forget()
        self.view_firefox.grid(row=1, column=0, sticky="nsew")
        self.update_nav_highlight(5)

    def show_browser(self):
        self.header_title.configure(text=TRANSLATIONS[self.lang]["tab_browser"])
        for view in (self.view_downloader, self.view_transcoder, self.view_m3u, self.view_mavin, self.view_cobra, self.view_firefox): view.grid_forget()
        self.view_browser.grid(row=1, column=0, sticky="nsew")
        self.update_nav_highlight(6)

    def update_nav_highlight(self, index):
        for i, btn in enumerate(self.nav_btns):
            if i == index:
                btn.configure(fg_color=COLOR_CARD, text_color=COLOR_ACCENT)
            else:
                btn.configure(fg_color="transparent", text_color=COLOR_TEXT_DIM)

    def toggle_language(self):
        new_lang = "English" if self.lang == "Arabic" else "Arabic"
        self.change_language(new_lang)
        self.lang_btn.configure(text=f"🌐 {self.lang}")

    def check_server_health_loop(self):
        while True:
            host, port, user, pw = self.host_entry.get(), int(self.port_entry.get() or 22), self.user_entry.get(), self.pass_entry.get()
            if host:
                try:
                    ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(host, port=port, username=user, password=pw, timeout=5)
                    ssh.close()
                    self.after(0, lambda: self.server_status_dot.configure(text_color=COLOR_SUCCESS))
                    self.after(0, lambda: self.server_status_text.configure(text="Server Online"))
                except:
                    self.after(0, lambda: self.server_status_dot.configure(text_color=COLOR_DANGER))
                    self.after(0, lambda: self.server_status_text.configure(text="Server Offline"))
            time.sleep(30)

    def open_live_log(self):
        if self.live_log_win is None or not self.live_log_win.winfo_exists():
            self.live_log_win = LiveLogWindow(self)
        else:
            self.live_log_win.focus()

    def log(self, m, is_cmd=False):
        self.log_text.insert(tk.END, m + "\n")
        self.log_text.see(tk.END)
        if self.live_log_win:
            color = "#ffcc00" if is_cmd else "#00ff00"
            self.live_log_win.log_local(m, color)

    def handle_clipboard(self, event_or_widget, action):
        # Support both Event objects and direct widget references
        widget = event_or_widget.widget if hasattr(event_or_widget, 'widget') else event_or_widget
        try:
            if action == "paste":
                text = pyperclip.paste()
                if text:
                    if isinstance(widget, tk.Text):
                        try: widget.delete("sel.first", "sel.last")
                        except: pass
                        widget.insert(tk.INSERT, text)
                    elif hasattr(widget, 'insert'):
                        # For CustomTkinter / Entry / CTkEntry, check if internal entry has selection
                        target = widget._entry if hasattr(widget, '_entry') else widget
                        try:
                            start = target.index("sel.first")
                            end = target.index("sel.last")
                            target.delete(start, end)
                        except: pass
                        target.insert(tk.INSERT, text)
                    for row in self.m3u_rows:
                        if widget == row['url']:
                            self.after(100, lambda r=row: self.fetch_categories_async(r, force=False))
            elif action == "copy":
                text = ""
                if isinstance(widget, tk.Text):
                    try: text = widget.get("sel.first", "sel.last")
                    except: pass
                else:
                    target = widget._entry if hasattr(widget, '_entry') else widget
                    try:
                        start = target.index("sel.first")
                        end = target.index("sel.last")
                        text = target.get()[start:end]
                    except:
                        try: text = target.selection_get()
                        except: pass
                if text: pyperclip.copy(text)
            elif action == "cut":
                text = ""
                if isinstance(widget, tk.Text):
                    try:
                        text = widget.get("sel.first", "sel.last")
                        widget.delete("sel.first", "sel.last")
                    except: pass
                else:
                    target = widget._entry if hasattr(widget, '_entry') else widget
                    try:
                        start = target.index("sel.first")
                        end = target.index("sel.last")
                        text = target.get()[start:end]
                        target.delete(start, end)
                    except: pass
                if text: pyperclip.copy(text)
        except Exception as e:
            pass
        return "break"

    def handle_key_press(self, event):
        if event.state & 0x0004: # Control key
            key = event.keysym.lower()
            if key in ('v', 'r', 'Arabic_reh', 'quotedbl'): return self.handle_clipboard(event, "paste")
            elif key in ('c', 'w', 'Arabic_waw', 'comma'): return self.handle_clipboard(event, "copy")
            elif key in ('x', 'q', 'Arabic_hamzaonyeh', 'period'): return self.handle_clipboard(event, "cut")
            elif key in ('a', 'f', 'Arabic_sheen', 'h'): return self.select_all_widget(event.widget)
        return None

    def add_context_menu(self, widget):
        menu = tk.Menu(self, tearoff=0, bg="#2b2b2b", fg="white", activebackground="#28a745", activeforeground="white", bd=0)
        t = TRANSLATIONS[self.lang]
        menu.add_command(label=t["menu_cut"], command=lambda: self.handle_clipboard(widget, "cut"))
        menu.add_command(label=t["menu_copy"], command=lambda: self.handle_clipboard(widget, "copy"))
        menu.add_command(label=t["menu_paste"], command=lambda: self.handle_clipboard(widget, "paste"))
        menu.add_separator()
        menu.add_command(label=t["menu_select_all"], command=lambda: self.select_all_widget(widget))
        
        def show_menu(e):
            try:
                # Focus widget on right click
                widget.focus()
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show_menu)
        widget.bind("<Control-KeyPress>", self.handle_key_press)

    def select_all_widget(self, widget):
        if hasattr(widget, 'select_range'): widget.select_range(0, tk.END)
        elif hasattr(widget, 'tag_add'): widget.tag_add("sel", "1.0", "end")
        return "break"

    def setup_downloader_tab(self):
        self.down_container = ctk.CTkScrollableFrame(self.view_downloader, fg_color="transparent")
        self.down_container.pack(fill="both", expand=True)
        
        # Links Card
        self.links_card = ctk.CTkFrame(self.down_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.links_card.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.links_title_lbl = ctk.CTkLabel(self.links_card, text=TRANSLATIONS[self.lang]["links_title"], font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT)
        self.links_title_lbl.pack(pady=(15, 10))
        
        self.header_grid = ctk.CTkFrame(self.links_card, fg_color="transparent")
        self.header_grid.pack(fill="x", padx=15)
        
        cols = [
            (TRANSLATIONS[self.lang]["col_process"], 60),
            (TRANSLATIONS[self.lang]["col_m3u8"], 60),
            (TRANSLATIONS[self.lang]["col_url"], 380),
            (TRANSLATIONS[self.lang]["col_path"], 280),
            (TRANSLATIONS[self.lang]["col_name"], 180)
        ]
        for i, (txt, w) in enumerate(cols):
            lbl = ctk.CTkLabel(self.header_grid, text=txt, width=w, font=ctk.CTkFont(size=11, weight="bold"), text_color=COLOR_TEXT_DIM)
            lbl.grid(row=0, column=i, padx=5)
            if i == 0: self.h_proc = lbl
            elif i == 1: self.h_m3u8 = lbl
            elif i == 2: self.h_url = lbl
            elif i == 3: self.h_path = lbl
            elif i == 4: self.h_name = lbl

        self.rows = []
        for i in range(10):
            row_frame = ctk.CTkFrame(self.links_card, fg_color="transparent")
            row_frame.pack(fill="x", padx=15, pady=3)
            
            pr_v = tk.BooleanVar(value=False); chk_pr = ctk.CTkCheckBox(row_frame, text="", variable=pr_v, width=40, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
            m3_v = tk.BooleanVar(value=False); chk_m3 = ctk.CTkCheckBox(row_frame, text="", variable=m3_v, width=40, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
            url_e = ctk.CTkEntry(row_frame, width=380, height=35, fg_color=COLOR_BG, border_color=COLOR_BORDER, placeholder_text=TRANSLATIONS[self.lang]["placeholder_url"])
            path_e = ctk.CTkEntry(row_frame, width=280, height=35, fg_color=COLOR_BG, border_color=COLOR_BORDER, placeholder_text=TRANSLATIONS[self.lang]["placeholder_path"])
            name_e = ctk.CTkEntry(row_frame, width=180, height=35, fg_color=COLOR_BG, border_color=COLOR_BORDER, placeholder_text=TRANSLATIONS[self.lang]["placeholder_name"])
            
            chk_pr.grid(row=0, column=0, padx=5); chk_m3.grid(row=0, column=1, padx=5); url_e.grid(row=0, column=2, padx=5); path_e.grid(row=0, column=3, padx=5); name_e.grid(row=0, column=4, padx=5)
            self.add_context_menu(url_e); self.add_context_menu(path_e); self.add_context_menu(name_e)
            self.rows.append({'url': url_e, 'path': path_e, 'name': name_e, 'process': pr_v, 'is_m3u8': m3_v})

        # Control Panel Card
        self.btn_container = ctk.CTkFrame(self.down_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.btn_container.pack(fill="x", padx=10, pady=10)
        
        self.mode_var = tk.StringVar(value="sequential")
        mode_frame = ctk.CTkFrame(self.btn_container, fg_color="transparent")
        mode_frame.pack(side="left", padx=20, pady=15)
        
        self.r_seq = ctk.CTkRadioButton(mode_frame, text=TRANSLATIONS[self.lang]["mode_seq"], variable=self.mode_var, value="sequential", fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.r_seq.pack(side="left", padx=10)
        self.r_par = ctk.CTkRadioButton(mode_frame, text=TRANSLATIONS[self.lang]["mode_par"], variable=self.mode_var, value="parallel", fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.r_par.pack(side="left", padx=10)

        self.exec_btn = ctk.CTkButton(self.btn_container, text=f"🚀 {TRANSLATIONS[self.lang]['btn_start']}", command=self.start_batch_download, 
                                       fg_color=COLOR_ACCENT, text_color=COLOR_BG, font=ctk.CTkFont(size=14, weight="bold"), height=45, width=180)
        self.exec_btn.pack(side="left", padx=20)
        
        self.load_file_btn = ctk.CTkButton(self.btn_container, text=f"📂 {TRANSLATIONS[self.lang]['btn_load_file']}", command=self.load_links_from_file, 
                                            fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT, height=40)
        self.load_file_btn.pack(side="left", padx=10)
        
        self.mon_btn = ctk.CTkButton(self.btn_container, text=f"📊 {TRANSLATIONS[self.lang]['btn_monitor']}", command=self.monitor_downloads, 
                                      fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT, height=40)
        self.mon_btn.pack(side="left", padx=10)
        
        self.auto_refresh_var = tk.BooleanVar(value=False)
        self.auto_refresh_chk = ctk.CTkCheckBox(self.btn_container, text=TRANSLATIONS[self.lang]["auto_refresh"], variable=self.auto_refresh_var, 
                                                 command=self.toggle_auto_refresh, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.auto_refresh_chk.pack(side="left", padx=10)
        
        self.clear_btn = ctk.CTkButton(self.btn_container, text=f"🗑️ {TRANSLATIONS[self.lang]['btn_clear']}", command=self.clear_urls, 
                                        fg_color=COLOR_DANGER, text_color=COLOR_TEXT, height=40)
        self.clear_btn.pack(side="right", padx=20)

        # Monitor Card
        self.monitor_card = ctk.CTkFrame(self.down_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.monitor_card.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.mon_title_lbl = ctk.CTkLabel(self.monitor_card, text=TRANSLATIONS[self.lang]["monitor_title"], font=ctk.CTkFont(size=15, weight="bold"), text_color=COLOR_ACCENT)
        self.mon_title_lbl.pack(pady=(15, 5))
        
        self.progress_bar = ctk.CTkProgressBar(self.monitor_card, fg_color=COLOR_BG, progress_color=COLOR_ACCENT, height=10)
        self.progress_bar.pack(fill="x", padx=30, pady=10); self.progress_bar.set(0)
        
        # Modern Treeview Styling
        tree_frame = ctk.CTkFrame(self.monitor_card, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=15, pady=10)
        
        style = ttk.Style()
        style.configure("Custom.Treeview", background=COLOR_CARD, foreground=COLOR_TEXT, fieldbackground=COLOR_CARD, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Custom.Treeview.Heading", background=COLOR_SIDEBAR, foreground=COLOR_ACCENT, font=("Segoe UI", 10, "bold"))
        style.map("Custom.Treeview", background=[('selected', COLOR_ACCENT)], foreground=[('selected', COLOR_BG)])

        self.tree = ttk.Treeview(tree_frame, columns=("file", "status", "info"), show="headings", height=8, style="Custom.Treeview")
        self.tree.heading("file", text=TRANSLATIONS[self.lang]["mon_col_file"])
        self.tree.heading("status", text=TRANSLATIONS[self.lang]["mon_col_status"])
        self.tree.heading("info", text=TRANSLATIONS[self.lang]["mon_col_info"])
        self.tree.column("file", width=400); self.tree.column("status", width=150); self.tree.column("info", width=500)
        self.tree.pack(side="left", fill="both", expand=True)
        
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")

        self.log_text = tk.Text(self.down_container, height=10, bg=COLOR_SIDEBAR, fg=COLOR_SUCCESS, font=("Consolas", 10), borderwidth=1, relief="flat", padx=10, pady=10)
        self.log_text.pack(fill="x", padx=10, pady=20); self.add_context_menu(self.log_text)

    def setup_browser_tab(self):
        self.browser_container = ctk.CTkScrollableFrame(self.view_browser, fg_color="transparent")
        self.browser_container.pack(fill="both", expand=True)
        card = ctk.CTkFrame(self.browser_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="x", padx=10, pady=10)
        self.browser_title_lbl = ctk.CTkLabel(card, text=TRANSLATIONS[self.lang]["browser_title"], font=ctk.CTkFont(size=18, weight="bold"), text_color=COLOR_ACCENT)
        self.browser_title_lbl.pack(pady=(18, 12))
        row = ctk.CTkFrame(card, fg_color="transparent"); row.pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(row, text=TRANSLATIONS[self.lang]["browser_profile"], width=220, anchor="w").pack(side="left")
        self.browser_profile_entry = ctk.CTkEntry(row, height=36); self.browser_profile_entry.insert(0, os.path.join(os.getcwd(), "firefox_sftp_profile")); self.browser_profile_entry.pack(side="left", fill="x", expand=True, padx=8); self.add_context_menu(self.browser_profile_entry)
        row2 = ctk.CTkFrame(card, fg_color="transparent"); row2.pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(row2, text="Firefox executable:", width=220, anchor="w").pack(side="left")
        self.browser_exe_entry = ctk.CTkEntry(row2, height=36, placeholder_text="firefox.exe"); self.browser_exe_entry.pack(side="left", fill="x", expand=True, padx=8); self.add_context_menu(self.browser_exe_entry)
        controls = ctk.CTkFrame(card, fg_color="transparent"); controls.pack(fill="x", padx=20, pady=(12, 18))
        self.browser_start_btn = ctk.CTkButton(controls, text="🌐 " + TRANSLATIONS[self.lang]["browser_start"], command=self.start_browser_session, fg_color=COLOR_SUCCESS, text_color=COLOR_BG); self.browser_start_btn.pack(side="left", padx=(0, 8))
        self.browser_stop_btn = ctk.CTkButton(controls, text=TRANSLATIONS[self.lang]["browser_stop"], command=self.stop_browser_session, fg_color=COLOR_DANGER); self.browser_stop_btn.pack(side="left", padx=8)
        self.browser_status_lbl = ctk.CTkLabel(controls, text=TRANSLATIONS[self.lang]["browser_status"] + " stopped", text_color=COLOR_TEXT_DIM); self.browser_status_lbl.pack(side="right")
        ctk.CTkLabel(self.browser_container, text="Install the Firefox bridge extension in this profile, then enable Firefox Downloader auto mode.", text_color=COLOR_TEXT_DIM).pack(pady=10)

    def start_browser_session(self):
        if self.browser_proxy: return
        try:
            host, port = self.host_entry.get().strip(), int(self.port_entry.get() or 22)
            if not host: raise RuntimeError("Server host is empty")
            profile = self.browser_profile_entry.get().strip() or os.path.join(os.getcwd(), "firefox_sftp_profile")
            os.makedirs(profile, exist_ok=True)
            self.browser_proxy = SSHSocks5Proxy(host, port, self.user_entry.get(), self.pass_entry.get())
            local_port = self.browser_proxy.start()
            user_js = os.path.join(profile, "user.js")
            with open(user_js, "w", encoding="utf-8") as f:
                f.write("user_pref('network.proxy.type', 1);\n")
                f.write("user_pref('network.proxy.socks', '127.0.0.1');\n")
                f.write(f"user_pref('network.proxy.socks_port', {local_port});\n")
                f.write("user_pref('network.proxy.socks_remote_dns', true);\n")
                f.write("user_pref('network.proxy.no_proxies_on', '127.0.0.1,localhost');\n")
            exe = self.browser_exe_entry.get().strip() or shutil.which("firefox") or shutil.which("firefox.exe")
            if not exe:
                for candidate in (r"C:\\Program Files\\Mozilla Firefox\\firefox.exe", r"C:\\Program Files (x86)\\Mozilla Firefox\\firefox.exe"):
                    if os.path.exists(candidate): exe = candidate; break
            if not exe: raise RuntimeError("Firefox executable was not found")
            self.browser_process = subprocess.Popen([exe, "-no-remote", "-profile", profile])
            self.browser_status_lbl.configure(text=f"{TRANSLATIONS[self.lang]['browser_status']} SOCKS5 127.0.0.1:{local_port}", text_color=COLOR_SUCCESS)
            self.log(f"Browser started through server IP; local SOCKS5 port {local_port}")
        except Exception as exc:
            if self.browser_proxy: self.browser_proxy.stop(); self.browser_proxy = None
            self.browser_status_lbl.configure(text=f"Browser error: {exc}", text_color=COLOR_DANGER)
            self.log(f"Browser tunnel error: {exc}")

    def stop_browser_session(self):
        try:
            if self.browser_process and self.browser_process.poll() is None: self.browser_process.terminate()
        except Exception: pass
        self.browser_process = None
        if self.browser_proxy: self.browser_proxy.stop(); self.browser_proxy = None
        if hasattr(self, "browser_status_lbl"): self.browser_status_lbl.configure(text=TRANSLATIONS[self.lang]["browser_status"] + " stopped", text_color=COLOR_TEXT_DIM)

    def setup_firefox_tab(self):
        self.firefox_container = ctk.CTkScrollableFrame(self.view_firefox, fg_color="transparent")
        self.firefox_container.pack(fill="both", expand=True)
        card = ctk.CTkFrame(self.firefox_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="x", padx=10, pady=10)
        self.firefox_title_lbl = ctk.CTkLabel(card, text=TRANSLATIONS[self.lang]["firefox_title"], font=ctk.CTkFont(size=18, weight="bold"), text_color=COLOR_ACCENT)
        self.firefox_title_lbl.pack(pady=(18, 12))
        row = ctk.CTkFrame(card, fg_color="transparent"); row.pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(row, text=TRANSLATIONS[self.lang]["firefox_path"], width=220, anchor="w").pack(side="left")
        self.firefox_path_entry = ctk.CTkEntry(row, height=36, placeholder_text="/home/downloads/firefox/")
        self.firefox_path_entry.insert(0, "/home/downloads/firefox/")
        self.firefox_path_entry.pack(side="left", fill="x", expand=True, padx=8); self.add_context_menu(self.firefox_path_entry)
        row2 = ctk.CTkFrame(card, fg_color="transparent"); row2.pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(row2, text=TRANSLATIONS[self.lang]["firefox_port"], width=220, anchor="w").pack(side="left")
        self.firefox_port_entry = ctk.CTkEntry(row2, width=100, height=36); self.firefox_port_entry.insert(0, "8765"); self.firefox_port_entry.pack(side="left", padx=8)
        self.firefox_auto_v = tk.BooleanVar(value=True)
        self.firefox_auto_chk = ctk.CTkCheckBox(row2, text=TRANSLATIONS[self.lang]["firefox_auto"], variable=self.firefox_auto_v, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.firefox_auto_chk.pack(side="left", padx=18)
        controls = ctk.CTkFrame(card, fg_color="transparent"); controls.pack(fill="x", padx=20, pady=(10, 18))
        self.firefox_start_btn = ctk.CTkButton(controls, text="🦊 " + TRANSLATIONS[self.lang]["firefox_start"], command=self.start_firefox_bridge, fg_color=COLOR_SUCCESS, text_color=COLOR_BG)
        self.firefox_start_btn.pack(side="left", padx=(0, 8))
        self.firefox_stop_btn = ctk.CTkButton(controls, text=TRANSLATIONS[self.lang]["firefox_stop"], command=self.stop_firefox_bridge, fg_color=COLOR_DANGER)
        self.firefox_stop_btn.pack(side="left", padx=8)
        self.firefox_save_btn = ctk.CTkButton(controls, text=TRANSLATIONS[self.lang]["save_settings"], command=lambda: self.save_current_settings(silent=False), fg_color=COLOR_ACCENT, text_color=COLOR_BG)
        self.firefox_save_btn.pack(side="left", padx=8)
        self.firefox_status_lbl = ctk.CTkLabel(controls, text=TRANSLATIONS[self.lang]["firefox_status"] + " stopped", text_color=COLOR_TEXT_DIM)
        self.firefox_status_lbl.pack(side="right")
        ctk.CTkLabel(self.firefox_container, text=TRANSLATIONS[self.lang]["firefox_install"], text_color=COLOR_TEXT_DIM).pack(pady=(4, 8))
        pending_card = ctk.CTkFrame(self.firefox_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        pending_card.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkLabel(pending_card, text=TRANSLATIONS[self.lang]["firefox_pending"], font=ctk.CTkFont(size=15, weight="bold"), text_color=COLOR_ACCENT).pack(pady=10)
        self.firefox_pending_box = tk.Listbox(pending_card, bg=COLOR_SIDEBAR, fg=COLOR_TEXT, height=12, borderwidth=0, highlightthickness=0)
        self.firefox_pending_box.pack(fill="both", expand=True, padx=15, pady=8)
        self.firefox_download_pending_btn = ctk.CTkButton(pending_card, text="🚀 Download pending", command=self.download_firefox_pending, fg_color=COLOR_ACCENT, text_color=COLOR_BG)
        self.firefox_download_pending_btn.pack(pady=(4, 14))

    def start_firefox_bridge(self):
        try:
            if self.firefox_server:
                return
            port = int(self.firefox_port_entry.get().strip() or 8765) if hasattr(self, "firefox_port_entry") else 8765
            handler = type("BoundFirefoxDownloadHandler", (FirefoxDownloadHandler,), {})
            handler.app = self
            self.firefox_server = ThreadingHTTPServer(("127.0.0.1", port), handler)
            self.firefox_port = port
            self.firefox_server_thread = threading.Thread(target=self.firefox_server.serve_forever, daemon=True)
            self.firefox_server_thread.start()
            if hasattr(self, "firefox_status_lbl"): self.firefox_status_lbl.configure(text=f"{TRANSLATIONS[self.lang]['firefox_status']} 127.0.0.1:{port}", text_color=COLOR_SUCCESS)
            self.log(f"Firefox bridge started on 127.0.0.1:{port}")
        except Exception as exc:
            self.firefox_server = None
            if hasattr(self, "firefox_status_lbl"): self.firefox_status_lbl.configure(text=f"Bridge error: {exc}", text_color=COLOR_DANGER)
            self.log(f"Firefox bridge error: {exc}")

    def stop_firefox_bridge(self):
        if self.firefox_server:
            try: self.firefox_server.shutdown(); self.firefox_server.server_close()
            except Exception: pass
            self.firefox_server = None
        if hasattr(self, "firefox_status_lbl"): self.firefox_status_lbl.configure(text=TRANSLATIONS[self.lang]["firefox_status"] + " stopped", text_color=COLOR_TEXT_DIM)

    def close_app(self):
        self.stop_browser_session()
        self.stop_firefox_bridge()
        self.is_active = False
        self.destroy()

    def receive_firefox_download(self, payload):
        original_url = str(payload.get("url", "")).strip()
        url = normalize_download_url(original_url)
        path = self.firefox_path_entry.get().strip() or "/home/downloads/firefox/"
        name = str(payload.get("filename", "")).strip()
        if not name:
            name = os.path.basename(urlparse(url).path) or f"firefox_{int(time.time())}"
        name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip() or f"firefox_{int(time.time())}"
        is_m3u8 = ".m3u8" in url.lower() or "mpegurl" in str(payload.get("mime", "")).lower()
        # Keep the original signed URL; the server will try HTTP first and
        # fall back to the original HTTPS URL if the token/CDN rejects HTTP.
        task = (original_url, path, name, False, is_m3u8)
        self.log(f"Firefox received: {name} -> {path} ({'M3U8' if is_m3u8 else 'Direct'})", is_cmd=True)
        if original_url != url:
            self.log(f"Firefox URL normalized: {original_url} -> {url}", is_cmd=True)
        if hasattr(self, "firefox_pending_box"):
            self.firefox_pending_box.insert(tk.END, f"QUEUED | {name} | {'M3U8' if is_m3u8 else 'Direct'} | {url}")
        if self.firefox_auto_v.get():
            self.enqueue_download_job(self.execute_firefox_download, *task, count=1)
            self.log(f"Firefox queued: {name} ({'M3U8' if is_m3u8 else 'Direct'})", is_cmd=True)
        else:
            self.firefox_pending.append(task)
            self.log(f"Firefox received (waiting): {name}")

    def download_firefox_pending(self):
        pending, self.firefox_pending = self.firefox_pending, []
        for task in pending: self.enqueue_download_job(self.execute_firefox_download, *task, count=1)
        self.firefox_pending_box.delete(0, tk.END)

    def execute_firefox_download(self, url, path, name, process=False, is_m3u8=False):
        """Run a Firefox-captured URL as one explicit, synchronous server job."""
        host, port = self.host_entry.get(), int(self.port_entry.get() or 22)
        user, pw = self.user_entry.get(), self.pass_entry.get()
        original_url = str(url or "").strip()
        url = normalize_download_url(original_url)
        task_id = int(time.time() * 1000)
        ram_dir = f"/dev/shm/sftp_studio/firefox_{task_id}"
        url_file = f"{ram_dir}/url.txt"
        https_url_file = f"{ram_dir}/url_https.txt"
        safe_name = name or f"firefox_{task_id}.bin"
        if is_m3u8 and not safe_name.lower().endswith((".mp4", ".mkv")):
            safe_name += ".mp4"
        remote_output = f"{ram_dir}/{safe_name}"
        final_output = f"{path.rstrip('/')}/{safe_name}"
        referer_match = re.search(r"(https?://[^/]+)", url)
        referer = (referer_match.group(1) + "/") if referer_match else "https://google.com/"
        user_agent = "IPTVSmartersPlayer"
        self.log(f"Firefox server job starting: {safe_name} -> {host}:{port}", is_cmd=True)
        try:
            ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, port=port, username=user, password=pw, timeout=15)
            sftp = ssh.open_sftp()
            ssh.exec_command(f"mkdir -p {shlex.quote(ram_dir)} {shlex.quote(path)}")
            with sftp.file(url_file, "w") as remote_file:
                remote_file.write(url)
            with sftp.file(https_url_file, "w") as remote_file:
                remote_file.write(original_url)
            sftp.close()
            if is_m3u8:
                ffmpeg_http = (
                    f"ffmpeg -nostdin -hide_banner -loglevel error -y "
                    f"-protocol_whitelist file,http,https,tcp,tls,crypto -reconnect 1 "
                    f"-reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 2 "
                    f"-user_agent {shlex.quote(user_agent)} -headers {shlex.quote('Referer: ' + referer)} "
                    f"-i \"$(cat {shlex.quote(url_file)})\" -c copy {shlex.quote(remote_output)}"
                )
                ffmpeg_https = ffmpeg_http.replace(
                    f"$(cat {shlex.quote(url_file)})", f"$(cat {shlex.quote(https_url_file)})")
                download_cmd = ffmpeg_http if url == original_url else (
                    f"({ffmpeg_http}) || (rm -f {shlex.quote(remote_output)}; {ffmpeg_https})")
            else:
                curl_base = (
                    f"curl -f -L -C - --retry 10 --max-redirs 30 "
                    f"-A {shlex.quote(user_agent)} -e {shlex.quote(referer)} "
                    f"-H 'Accept: */*' -H 'Accept-Language: en-US,en;q=0.9' "
                    f"--compressed --tcp-nodelay --keepalive-time 60 "
                    f"-o {shlex.quote(remote_output + '.part')} URL_VALUE "
                    f"&& mv -f {shlex.quote(remote_output + '.part')} {shlex.quote(remote_output)}"
                )
                curl_http = curl_base.replace("URL_VALUE", f'"$(cat {shlex.quote(url_file)})"')
                curl_https = curl_base.replace("URL_VALUE", f'"$(cat {shlex.quote(https_url_file)})"')
                download_cmd = curl_http if url == original_url else (
                    f"({curl_http}) || (rm -f {shlex.quote(remote_output + '.part')} {shlex.quote(remote_output)}; {curl_https})")
            remote_cmd = (
                f"set -o pipefail; mkdir -p {shlex.quote(ram_dir)} {shlex.quote(path)}; "
                f"echo '[FIREFOX] START {shlex.quote(safe_name)}'; {download_cmd}; "
                f"test -s {shlex.quote(remote_output)}; mv -f {shlex.quote(remote_output)} {shlex.quote(final_output)}; "
                f"rm -rf {shlex.quote(ram_dir)}; echo '[FIREFOX] DONE {shlex.quote(final_output)}'"
            )
            local_debug_log(f"FIREFOX DOWNLOAD\nURL: {url}\nCOMMAND:\n{remote_cmd}")
            stdin, stdout, stderr = ssh.exec_command(f"bash -lc {shlex.quote(remote_cmd)}")
            exit_code = stdout.channel.recv_exit_status()
            output = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")
            if output.strip(): self.log(output.strip())
            if exit_code == 0:
                self.log(f"Firefox download completed: {safe_name}")
            else:
                self.log(f"Firefox download FAILED exit={exit_code}: {safe_name}")
            ssh.close()
        except Exception as exc:
            self.log(f"Firefox server job error: {exc}")

    def setup_transcoder_tab(self):
        self.trans_container = ctk.CTkScrollableFrame(self.view_transcoder, fg_color="transparent")
        self.trans_container.pack(fill="both", expand=True)
        
        # Initialize variables first
        self.logo_tr_v = tk.BooleanVar(value=False); self.logo_tl_v = tk.BooleanVar(value=False)
        self.logo_br_v = tk.BooleanVar(value=False); self.logo_bl_v = tk.BooleanVar(value=False)
        self.intro_v = tk.BooleanVar(value=False)
        self.trim_v = tk.BooleanVar(value=False)
        self.auto_transcode_downloads_v = tk.BooleanVar(value=False)
        
        # FFmpeg Settings Card
        self.trans_card = ctk.CTkFrame(self.trans_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.trans_card.pack(fill="x", padx=10, pady=10)
        
        self.trans_title_lbl = ctk.CTkLabel(self.trans_card, text=TRANSLATIONS[self.lang]["trans_settings_title"], font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT)
        self.trans_title_lbl.pack(pady=15)
        
        trans_grid = ctk.CTkFrame(self.trans_card, fg_color="transparent")
        trans_grid.pack(fill="x", padx=20, pady=5)
        
        # Logo Section in Settings
        logo_configs = [
            ("logo_tr", self.logo_tr_v, 0),
            ("logo_tl", self.logo_tl_v, 1),
            ("logo_br", self.logo_br_v, 2),
            ("logo_bl", self.logo_bl_v, 3)
        ]
        
        for key, var, row in logo_configs:
            chk = ctk.CTkCheckBox(trans_grid, text="", variable=var, width=40, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
            chk.grid(row=row, column=0, padx=5, pady=5)
            lbl = ctk.CTkLabel(trans_grid, text=TRANSLATIONS[self.lang][key], text_color=COLOR_TEXT)
            lbl.grid(row=row, column=1, padx=5, pady=5, sticky="w")
            ent = ctk.CTkEntry(trans_grid, width=450, height=35, fg_color=COLOR_BG, border_color=COLOR_BORDER)
            ent.grid(row=row, column=2, padx=5, pady=5, sticky="w"); self.add_context_menu(ent)
            
            if key == "logo_tr": self.logo_tr_lbl = lbl; self.logo_tr_entry = ent; self.logo_tr_chk = chk
            elif key == "logo_tl": self.logo_tl_lbl = lbl; self.logo_tl_entry = ent; self.logo_tl_chk = chk
            elif key == "logo_br": self.logo_br_lbl = lbl; self.logo_br_entry = ent; self.logo_br_chk = chk
            elif key == "logo_bl": self.logo_bl_lbl = lbl; self.logo_bl_entry = ent; self.logo_bl_chk = chk

        # Additional Settings Row
        self.intro_lbl = ctk.CTkLabel(trans_grid, text=TRANSLATIONS[self.lang]["intro_path"], text_color=COLOR_TEXT)
        self.intro_lbl.grid(row=4, column=1, padx=5, pady=5, sticky="w")
        self.intro_path_entry = ctk.CTkEntry(trans_grid, width=450, height=35, fg_color=COLOR_BG, border_color=COLOR_BORDER)
        self.intro_path_entry.grid(row=4, column=2, padx=5, pady=5, sticky="w"); self.add_context_menu(self.intro_path_entry)
        self.intro_chk = ctk.CTkCheckBox(trans_grid, text=TRANSLATIONS[self.lang]["intro_toggle"], variable=self.intro_v, width=130, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.intro_chk.grid(row=4, column=3, padx=10, pady=5, sticky="w")

        self.trim_chk = ctk.CTkCheckBox(trans_grid, text=TRANSLATIONS[self.lang]["trim_toggle"], variable=self.trim_v, width=150, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.trim_chk.grid(row=5, column=1, padx=5, pady=5, sticky="w")
        self.trim_seconds_lbl = ctk.CTkLabel(trans_grid, text=TRANSLATIONS[self.lang]["trim_seconds"], text_color=COLOR_TEXT)
        self.trim_seconds_lbl.grid(row=5, column=2, padx=5, pady=5, sticky="w")
        self.trim_seconds_entry = ctk.CTkEntry(trans_grid, width=90, height=35, fg_color=COLOR_BG, border_color=COLOR_BORDER)
        self.trim_seconds_entry.insert(0, "0")
        self.trim_seconds_entry.grid(row=5, column=3, padx=5, pady=5, sticky="w")
        self.add_context_menu(self.trim_seconds_entry)
        self.auto_transcode_downloads_chk = ctk.CTkCheckBox(trans_grid, text=TRANSLATIONS[self.lang]["auto_transcode_downloads"], variable=self.auto_transcode_downloads_v, width=260, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.auto_transcode_downloads_chk.grid(row=6, column=1, columnspan=3, padx=5, pady=(2, 8), sticky="w")
        
        # Technical Specs Row
        specs_grid = ctk.CTkFrame(trans_grid, fg_color="transparent")
        specs_grid.grid(row=7, column=1, columnspan=3, sticky="ew", pady=10)
        
        self.res_lbl = ctk.CTkLabel(specs_grid, text=TRANSLATIONS[self.lang]["resolution"], text_color=COLOR_TEXT_DIM)
        self.res_lbl.pack(side="left", padx=5)
        self.res_var = tk.StringVar(value="720p")
        self.res_combo = ctk.CTkOptionMenu(specs_grid, variable=self.res_var, values=["Original", "1080p", "720p", "480p"], width=120, fg_color=COLOR_CARD, button_color=COLOR_ACCENT)
        self.res_combo.pack(side="left", padx=5)
        
        self.bit_lbl = ctk.CTkLabel(specs_grid, text=TRANSLATIONS[self.lang]["bitrate"], text_color=COLOR_TEXT_DIM)
        self.bit_lbl.pack(side="left", padx=15)
        self.bitrate_entry = ctk.CTkEntry(specs_grid, width=80, height=35, fg_color=COLOR_BG, border_color=COLOR_BORDER)
        self.bitrate_entry.insert(0, "820"); self.bitrate_entry.pack(side="left", padx=5); self.add_context_menu(self.bitrate_entry)
        
        self.codec_lbl = ctk.CTkLabel(specs_grid, text=TRANSLATIONS[self.lang]["codec_label"], text_color=COLOR_TEXT_DIM)
        self.codec_lbl.pack(side="left", padx=15)
        self.codec_var = tk.StringVar(value="libx265 (HEVC)")
        self.codec_combo = ctk.CTkOptionMenu(specs_grid, variable=self.codec_var, values=["libx264 (H.264)", "libx265 (HEVC)"], width=150, fg_color=COLOR_CARD, button_color=COLOR_ACCENT)
        self.codec_combo.pack(side="left", padx=5)
        
        self.bitdepth_lbl = ctk.CTkLabel(specs_grid, text=TRANSLATIONS[self.lang]["bit_depth_label"], text_color=COLOR_TEXT_DIM)
        self.bitdepth_lbl.pack(side="left", padx=15)
        self.bitdepth_var = tk.StringVar(value="10-bit")
        self.bitdepth_combo = ctk.CTkOptionMenu(specs_grid, variable=self.bitdepth_var, values=["8-bit", "10-bit"], width=100, fg_color=COLOR_CARD, button_color=COLOR_ACCENT)
        self.bitdepth_combo.pack(side="left", padx=5)

        self.preset_lbl = ctk.CTkLabel(specs_grid, text=TRANSLATIONS[self.lang]["preset"], text_color=COLOR_TEXT_DIM)
        self.preset_lbl.pack(side="left", padx=15)
        self.preset_var = tk.StringVar(value="medium")
        self.preset_combo = ctk.CTkOptionMenu(specs_grid, variable=self.preset_var, values=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"], width=120, fg_color=COLOR_CARD, button_color=COLOR_ACCENT)
        self.preset_combo.pack(side="left", padx=5)

        # Bulk Folder Card
        self.bulk_card = ctk.CTkFrame(self.trans_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.bulk_card.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.bulk_title_lbl = ctk.CTkLabel(self.bulk_card, text=TRANSLATIONS[self.lang]["bulk_title"], font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT)
        self.bulk_title_lbl.pack(pady=15)
        
        bulk_grid = ctk.CTkFrame(self.bulk_card, fg_color="transparent")
        bulk_grid.pack(fill="x", padx=20, pady=5)
        
        self.bulk_path_lbl = ctk.CTkLabel(bulk_grid, text=TRANSLATIONS[self.lang]["bulk_path_label"], text_color=COLOR_TEXT)
        self.bulk_path_lbl.pack(side="left", padx=5)
        self.bulk_path_entry = ctk.CTkEntry(bulk_grid, width=500, height=35, fg_color=COLOR_BG, border_color=COLOR_BORDER, placeholder_text="/path/to/videos/")
        self.bulk_path_entry.pack(side="left", padx=5); self.add_context_menu(self.bulk_path_entry)
        
        self.scan_btn = ctk.CTkButton(bulk_grid, text=f"🔍 {TRANSLATIONS[self.lang]['btn_scan_bulk']}", command=self.scan_bulk_directory, 
                                       fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT, height=35)
        self.scan_btn.pack(side="left", padx=10)
        
        self.bulk_list_frame = ctk.CTkScrollableFrame(self.bulk_card, height=250, fg_color=COLOR_BG, border_width=1, border_color=COLOR_BORDER)
        self.bulk_list_frame.pack(fill="both", expand=True, padx=20, pady=15)
        
        self.bulk_btn_frame = ctk.CTkFrame(self.bulk_card, fg_color="transparent")
        self.bulk_btn_frame.pack(pady=15)
        
        self.select_all_btn = ctk.CTkButton(self.bulk_btn_frame, text=TRANSLATIONS[self.lang]["btn_select_all"], command=self.select_all_bulk_files, 
                                             fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT, width=120)
        self.select_all_btn.pack(side="left", padx=5)
        
        self.select_none_btn = ctk.CTkButton(self.bulk_btn_frame, text=TRANSLATIONS[self.lang]["btn_select_none"], command=self.select_none_bulk_files, 
                                              fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT, width=120)
        self.select_none_btn.pack(side="left", padx=5)
        
        self.bulk_btn = ctk.CTkButton(self.bulk_btn_frame, text=f"🎬 {TRANSLATIONS[self.lang]['btn_start_bulk']}", command=self.start_bulk_transcode, 
                                       fg_color=COLOR_ACCENT, text_color=COLOR_BG, font=ctk.CTkFont(weight="bold"), height=40, width=200)
        self.bulk_btn.pack(side="left", padx=20)
        
        self.view_log_btn = ctk.CTkButton(self.bulk_btn_frame, text=f"📜 {TRANSLATIONS[self.lang]['btn_view_log']}", command=self.open_live_log, 
                                           fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT, height=40)
        self.view_log_btn.pack(side="left", padx=5)

    def setup_m3u_tab(self):
        self.m3u_container = ctk.CTkScrollableFrame(self.view_m3u, fg_color="transparent")
        self.m3u_container.pack(fill="both", expand=True)
        self.m3u_auto_transcode_v = tk.BooleanVar(value=False)
        
        # Xtream API Card
        self.xtream_card = ctk.CTkFrame(self.m3u_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.xtream_card.pack(fill="x", padx=10, pady=5)
        
        self.xtream_title_lbl = ctk.CTkLabel(self.xtream_card, text=TRANSLATIONS[self.lang]["xtream_title"], font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT)
        self.xtream_title_lbl.pack(pady=15)
        
        self.xtream_rows = []
        for i in range(3):
            xt_row_f = ctk.CTkFrame(self.xtream_card, fg_color=COLOR_BG, corner_radius=10, border_width=1, border_color=COLOR_BORDER)
            xt_row_f.pack(fill="x", padx=20, pady=8)
            
            # Header row (Always Visible)
            header = ctk.CTkFrame(xt_row_f, fg_color="transparent")
            header.pack(fill="x", padx=10, pady=5)
            
            toggle_btn = ctk.CTkButton(header, text="⚙️", width=30, height=30, fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT_DIM)
            toggle_btn.pack(side="left", padx=5)
            
            lbl_h_main = ctk.CTkLabel(header, text=f"Server {i+1}", font=ctk.CTkFont(size=12, weight="bold"), text_color=COLOR_ACCENT)
            lbl_h_main.pack(side="left", padx=5)
            
            s_ent = ctk.CTkEntry(header, width=400, height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER, placeholder_text="Quick Search Keyword...")
            s_ent.pack(side="left", padx=20); self.add_context_menu(s_ent)
            
            scan_b = ctk.CTkButton(header, text=f"🔍 {TRANSLATIONS[self.lang]['btn_m3u_search']}", width=120, height=35, 
                                    command=lambda idx=i: self.scan_xtream_source(idx), fg_color=COLOR_ACCENT, text_color=COLOR_BG, font=ctk.CTkFont(weight="bold"))
            scan_b.pack(side="left", padx=5)
            
            # Collapsible Config Section
            config_f = ctk.CTkFrame(xt_row_f, fg_color="transparent")
            # config_f is hidden by default
            
            grid = ctk.CTkFrame(config_f, fg_color="transparent")
            grid.pack(fill="x", padx=10, pady=10)
            
            lbl_h = ctk.CTkLabel(grid, text=TRANSLATIONS[self.lang]['xtream_host'], font=ctk.CTkFont(size=11))
            lbl_h.grid(row=0, column=0, padx=5, pady=5)
            h_ent = ctk.CTkEntry(grid, width=300, height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER); h_ent.grid(row=0, column=1, padx=5, pady=5); self.add_context_menu(h_ent)
            
            lbl_u = ctk.CTkLabel(grid, text=TRANSLATIONS[self.lang]["xtream_user"], font=ctk.CTkFont(size=11))
            lbl_u.grid(row=0, column=2, padx=5, pady=5)
            u_ent = ctk.CTkEntry(grid, width=150, height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER); u_ent.grid(row=0, column=3, padx=5, pady=5); self.add_context_menu(u_ent)
            
            lbl_p = ctk.CTkLabel(grid, text=TRANSLATIONS[self.lang]["xtream_pass"], font=ctk.CTkFont(size=11))
            lbl_p.grid(row=0, column=4, padx=5, pady=5)
            p_ent = ctk.CTkEntry(grid, width=150, height=35, show="*", fg_color=COLOR_CARD, border_color=COLOR_BORDER); p_ent.grid(row=0, column=5, padx=5, pady=5); self.add_context_menu(p_ent)
            
            controls = ctk.CTkFrame(config_f, fg_color="transparent")
            controls.pack(fill="x", padx=15, pady=(0, 10))
            vod_v = tk.BooleanVar(value=True); ctk.CTkCheckBox(controls, text="Movies (VOD)", variable=vod_v, width=120, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER).pack(side="left", padx=10)
            ser_v = tk.BooleanVar(value=True); ctk.CTkCheckBox(controls, text="Series", variable=ser_v, width=100, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER).pack(side="left", padx=10)
            
            def toggle_xt(f=config_f, b=toggle_btn):
                if f.winfo_ismapped(): f.pack_forget(); b.configure(text_color=COLOR_TEXT_DIM, border_color=COLOR_BORDER)
                else: f.pack(fill="x", before=None); b.configure(text_color=COLOR_ACCENT, border_color=COLOR_ACCENT)
            toggle_btn.configure(command=toggle_xt)
            
            self.xtream_rows.append({'host': h_ent, 'user': u_ent, 'pass': p_ent, 'vod_v': vod_v, 'ser_v': ser_v, 'search': s_ent, 'lbl_h': lbl_h, 'lbl_u': lbl_u, 'lbl_p': lbl_p, 'lbl_s': None, 'scan_b': scan_b, 'config_f': config_f, 'toggle_b': toggle_btn, 'lbl_h_main': lbl_h_main})

        # M3U Links Card
        self.m3u_card = ctk.CTkFrame(self.m3u_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.m3u_card.pack(fill="x", padx=10, pady=10)
        
        self.m3u_title_lbl = ctk.CTkLabel(self.m3u_card, text=TRANSLATIONS[self.lang]["m3u_title"], font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT)
        self.m3u_title_lbl.pack(pady=15)
        self.m3u_auto_transcode_chk = ctk.CTkCheckBox(self.m3u_card, text=TRANSLATIONS[self.lang]["m3u_auto_transcode"], variable=self.m3u_auto_transcode_v, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.m3u_auto_transcode_chk.pack(anchor="w", padx=20, pady=(0, 8))
        
        self.m3u_rows = []
        for i in range(3):
            row_f = ctk.CTkFrame(self.m3u_card, fg_color=COLOR_BG, corner_radius=10, border_width=1, border_color=COLOR_BORDER)
            row_f.pack(fill="x", padx=20, pady=5)
            
            header = ctk.CTkFrame(row_f, fg_color="transparent")
            header.pack(fill="x", padx=10, pady=10)
            
            toggle_btn = ctk.CTkButton(header, text="🔗", width=30, height=30, fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT_DIM)
            toggle_btn.pack(side="left", padx=5)
            
            lbl_m_main = ctk.CTkLabel(header, text=f"M3U {i+1}", font=ctk.CTkFont(size=12, weight="bold"), text_color=COLOR_ACCENT)
            lbl_m_main.pack(side="left", padx=5)
            
            cat_var = tk.StringVar(value="All Categories")
            cat_opt = ctk.CTkOptionMenu(header, variable=cat_var, values=["All Categories"], width=180, fg_color=COLOR_CARD, button_color=COLOR_ACCENT)
            cat_opt.pack(side="left", padx=(20, 5))
            
            search_e = ctk.CTkEntry(header, width=250, height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER, placeholder_text="Keyword...")
            search_e.pack(side="left", padx=5); self.add_context_menu(search_e)
            
            scan_btn = ctk.CTkButton(header, text=f"🔍 {TRANSLATIONS[self.lang]['btn_m3u_search']}", width=100, height=35, 
                                      command=lambda r_idx=i: self.start_m3u_scrape_single(r_idx), fg_color=COLOR_ACCENT, text_color=COLOR_BG, font=ctk.CTkFont(weight="bold"))
            scan_btn.pack(side="left", padx=10)
            
            # Collapsible Config
            config_f = ctk.CTkFrame(row_f, fg_color="transparent")
            
            lbl1 = ctk.CTkLabel(config_f, text=TRANSLATIONS[self.lang]["m3u_url_label"], font=ctk.CTkFont(size=11))
            lbl1.pack(side="left", padx=10, pady=10)
            url_e = ctk.CTkEntry(config_f, width=600, height=35, fg_color=COLOR_CARD, border_color=COLOR_BORDER); url_e.pack(side="left", padx=5); self.add_context_menu(url_e)
            url_e.bind("<FocusOut>", lambda e, r_idx=i: self.fetch_categories_async(self.m3u_rows[r_idx], force=False))
            
            fetch_btn = ctk.CTkButton(config_f, text="↻", width=35, height=35, fg_color="transparent", border_width=1, border_color=COLOR_BORDER, 
                                       command=lambda r_idx=i: self.fetch_categories_async(self.m3u_rows[r_idx], force=True))
            fetch_btn.pack(side="left", padx=5)
            
            def toggle_m3u(f=config_f, b=toggle_btn):
                if f.winfo_ismapped(): f.pack_forget(); b.configure(text_color=COLOR_TEXT_DIM, border_color=COLOR_BORDER)
                else: f.pack(fill="x", before=None); b.configure(text_color=COLOR_ACCENT, border_color=COLOR_ACCENT)
            toggle_btn.configure(command=toggle_m3u)
            
            self.m3u_rows.append({'url': url_e, 'cat_var': cat_var, 'cat_opt': cat_opt, 'search': search_e, 'lbl1': lbl1, 'lbl_cat': None, 'lbl2': None, 'fetch_btn': fetch_btn, 'scan_btn': scan_btn, 'config_f': config_f, 'toggle_b': toggle_btn, 'lbl_m_main': lbl_m_main})

        # Smart Paths Card
        self.m3u_paths_card = ctk.CTkFrame(self.m3u_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.m3u_paths_card.pack(fill="x", padx=10, pady=10)
        
        self.m3u_path_lbl = ctk.CTkLabel(self.m3u_paths_card, text=TRANSLATIONS[self.lang]["m3u_path_label"], font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT)
        self.m3u_path_lbl.pack(pady=15)
        
        self.m3u_path_mode = tk.StringVar(value="Series")
        self.m3u_mode_switch = ctk.CTkSegmentedButton(self.m3u_paths_card, values=[TRANSLATIONS[self.lang]["path_mode_series"], TRANSLATIONS[self.lang]["path_mode_vod"]], 
                                                       command=self.switch_m3u_path_mode, variable=self.m3u_path_mode, fg_color=COLOR_BG, selected_color=COLOR_ACCENT, unselected_color=COLOR_CARD)
        self.m3u_mode_switch.pack(pady=(8, 4))

        # Six independent path groups: Series 1-3 and VOD 1-3.
        self.m3u_path_group = tk.StringVar(value="Series 1")
        # Tracks the group whose values are currently loaded in the ten visible widgets.
        # CTkSegmentedButton updates its variable before invoking the callback, so this
        # must not be inferred from m3u_path_group while switching.
        self._active_path_group = "Series 1"
        self.m3u_group_switch = ctk.CTkSegmentedButton(
            self.m3u_paths_card,
            values=["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"],
            command=self.switch_m3u_path_group,
            variable=self.m3u_path_group,
            fg_color=COLOR_BG, selected_color=COLOR_ACCENT, unselected_color=COLOR_CARD)
        self.m3u_group_switch.pack(pady=(0, 8))
        
        self.m3u_path_entries = []
        path_grid = ctk.CTkFrame(self.m3u_paths_card, fg_color="transparent")
        path_grid.pack(fill="x", padx=10, pady=10)
        for i in range(5): path_grid.grid_columnconfigure(i, weight=1)

        for i in range(10):
            r, c = divmod(i, 5)
            # Compact Slot Card
            slot_f = ctk.CTkFrame(path_grid, fg_color=COLOR_BG, corner_radius=10, border_width=1, border_color=COLOR_BORDER)
            slot_f.grid(row=r, column=c, padx=5, pady=5, sticky="nsew")
            
            # Slot Header: #ID and Status Dot
            header = ctk.CTkFrame(slot_f, fg_color="transparent")
            header.pack(fill="x", padx=8, pady=(5, 2))
            
            lbl = ctk.CTkLabel(header, text=f"PATH {i+1}", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_TEXT_DIM)
            lbl.pack(side="left")
            
            status_lbl = ctk.CTkLabel(header, text="●", text_color="gray", font=ctk.CTkFont(size=14))
            status_lbl.pack(side="right")
            
            # Slot Body: Path Entry
            ent = ctk.CTkEntry(slot_f, height=30, font=ctk.CTkFont(size=11), fg_color=COLOR_CARD, border_color=COLOR_BORDER, placeholder_text=f"Path {i+1}...")
            ent.pack(fill="x", padx=8, pady=2); self.add_context_menu(ent)
            
            # Slot Footer: Controls
            footer = ctk.CTkFrame(slot_f, fg_color="transparent")
            footer.pack(fill="x", padx=5, pady=(2, 5))
            
            check_btn = ctk.CTkButton(footer, text="✨", command=lambda idx=i: self.check_for_new_items_automated(idx), 
                                       width=30, height=28, fg_color="transparent", border_width=1, border_color=COLOR_ACCENT, text_color=COLOR_ACCENT)
            check_btn.pack(side="left", padx=2)
            check_btn.bind("<Enter>", lambda e, b=check_btn: b.configure(fg_color="#facc15", text_color="#0f172a", border_color="#facc15"))
            check_btn.bind("<Leave>", lambda e, b=check_btn: b.configure(fg_color="transparent", text_color=COLOR_ACCENT, border_color=COLOR_ACCENT))
            ToolTip(check_btn, TRANSLATIONS[self.lang]["btn_check_new"])
            
            auto_v = tk.BooleanVar(value=False)
            auto_chk = ctk.CTkCheckBox(footer, text="🤖", variable=auto_v, font=ctk.CTkFont(size=10), width=30, checkbox_width=18, checkbox_height=18, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
            auto_chk.pack(side="left", padx=2)
            auto_chk.bind("<Enter>", lambda e, c=auto_chk: c.configure(text_color="#4ade80"))
            auto_chk.bind("<Leave>", lambda e, c=auto_chk: c.configure(text_color=COLOR_TEXT))
            ToolTip(auto_chk, TRANSLATIONS[self.lang]["auto_check"])
            
            int_var = tk.StringVar(value="12h")
            int_opt = ctk.CTkOptionMenu(footer, variable=int_var, values=["6h", "12h", "24h"], width=60, height=28, font=ctk.CTkFont(size=9), fg_color=COLOR_CARD, button_color=COLOR_ACCENT)
            int_opt.pack(side="right", padx=2)
            
            self.m3u_path_entries.append({'ent': ent, 'lbl': lbl, 'btn': check_btn, 'auto_v': auto_v, 'int_var': int_var, 'auto_chk': auto_chk, 'status_lbl': status_lbl, 'footer': footer})

        # Results Card
        self.m3u_results_card = ctk.CTkFrame(self.m3u_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        self.m3u_results_card.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.m3u_res_title = ctk.CTkLabel(self.m3u_results_card, text=TRANSLATIONS[self.lang]["m3u_results_title"], font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT)
        self.m3u_res_title.pack(pady=15)
        
        tree_frame = ctk.CTkFrame(self.m3u_results_card, fg_color=COLOR_BG, border_width=1, border_color=COLOR_BORDER)
        tree_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.m3u_tree = ttk.Treeview(tree_frame, columns=("name", "path", "url"), show="headings", height=15, style="Custom.Treeview")
        self.m3u_tree.heading("name", text="Content Name")
        self.m3u_tree.heading("path", text="Target Path")
        self.m3u_tree.heading("url", text="Stream URL")
        self.m3u_tree.column("name", width=450); self.m3u_tree.column("path", width=150); self.m3u_tree.column("url", width=300)
        
        tree_scrolly = ttk.Scrollbar(tree_frame, orient="vertical", command=self.m3u_tree.yview)
        self.m3u_tree.configure(yscrollcommand=tree_scrolly.set)
        self.m3u_tree.pack(side="left", fill="both", expand=True); tree_scrolly.pack(side="right", fill="y")
        
        # Quick Assign Panel
        path_panel = ctk.CTkFrame(self.m3u_results_card, fg_color=COLOR_BG, corner_radius=10, border_width=1, border_color=COLOR_BORDER)
        path_panel.pack(fill="x", padx=20, pady=10)
        ctk.CTkLabel(path_panel, text="⚡ Quick Assign Path:", font=ctk.CTkFont(size=12, weight="bold"), text_color=COLOR_ACCENT).pack(side="left", padx=20, pady=15)
        for i in range(10):
            btn = ctk.CTkButton(path_panel, text=str(i+1), width=40, height=35, fg_color=COLOR_CARD, hover_color=COLOR_ACCENT, border_width=1, border_color=COLOR_BORDER,
                                 command=lambda idx=i: self.assign_path_to_selected(idx))
            btn.pack(side="left", padx=3)

        m3u_btn_frame = ctk.CTkFrame(self.m3u_results_card, fg_color="transparent")
        m3u_btn_frame.pack(pady=20)
        
        self.m3u_sel_all_btn = ctk.CTkButton(m3u_btn_frame, text=TRANSLATIONS[self.lang]["btn_m3u_select_all"], command=self.select_all_m3u_items, fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT, width=120)
        self.m3u_sel_all_btn.pack(side="left", padx=10)
        
        self.m3u_sel_none_btn = ctk.CTkButton(m3u_btn_frame, text=TRANSLATIONS[self.lang]["btn_m3u_select_none"], command=self.select_none_m3u_items, fg_color="transparent", border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT, width=120)
        self.m3u_sel_none_btn.pack(side="left", padx=10)
        
        self.m3u_down_btn = ctk.CTkButton(m3u_btn_frame, text=f"🚀 {TRANSLATIONS[self.lang]['btn_m3u_download']}", command=self.start_m3u_download_selected, fg_color=COLOR_ACCENT, text_color=COLOR_BG, font=ctk.CTkFont(weight="bold"), height=45, width=250)
        self.m3u_down_btn.pack(side="left", padx=20)
        
        self.m3u_clear_res_btn = ctk.CTkButton(m3u_btn_frame, text=f"🗑️ {TRANSLATIONS[self.lang]['btn_m3u_clear']}", command=self.clear_m3u_results, fg_color=COLOR_DANGER, text_color=COLOR_TEXT, height=40)
        self.m3u_clear_res_btn.pack(side="left", padx=10)

    def setup_mavin_tab(self):
        self.mavin_container = ctk.CTkScrollableFrame(self.view_mavin, fg_color="transparent")
        self.mavin_container.pack(fill="both", expand=True)
        self.mavin_auto_transcode_v = tk.BooleanVar(value=False)
        card = ctk.CTkFrame(self.mavin_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(card, text="🔐 Mavin IPTV • Series & Movies Only", font=ctk.CTkFont(size=18, weight="bold"), text_color=COLOR_ACCENT).pack(pady=(14, 8))
        self.mavin_auto_transcode_chk = ctk.CTkCheckBox(card, text=TRANSLATIONS[self.lang]["mavin_auto_transcode"], variable=self.mavin_auto_transcode_v, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.mavin_auto_transcode_chk.pack(anchor="w", padx=20, pady=(0, 8))
        auth = ctk.CTkFrame(card, fg_color="transparent"); auth.pack(fill="x", padx=16, pady=8)
        self.mavin_api_entry = ctk.CTkEntry(auth, width=330, placeholder_text="API URL"); self.mavin_api_entry.pack(side="left", padx=4); self.add_context_menu(self.mavin_api_entry)
        self.mavin_code_entry = ctk.CTkEntry(auth, width=150, placeholder_text="Active Code"); self.mavin_code_entry.pack(side="left", padx=4); self.add_context_menu(self.mavin_code_entry)
        self.mavin_sn_entry = ctk.CTkEntry(auth, width=150, placeholder_text="Serial Number"); self.mavin_sn_entry.pack(side="left", padx=4); self.add_context_menu(self.mavin_sn_entry)
        self.mavin_mac_entry = ctk.CTkEntry(auth, width=150, placeholder_text="MAC Address"); self.mavin_mac_entry.pack(side="left", padx=4); self.add_context_menu(self.mavin_mac_entry)
        self.mavin_activate_btn = ctk.CTkButton(auth, text="Activate", width=110, command=self.mavin_activate, fg_color=COLOR_ACCENT, text_color=COLOR_BG); self.mavin_activate_btn.pack(side="left", padx=8)
        self.mavin_status_lbl = ctk.CTkLabel(card, text="Not activated", text_color=COLOR_TEXT_DIM); self.mavin_status_lbl.pack(anchor="w", padx=20, pady=(0, 10))

        search_card = ctk.CTkFrame(self.mavin_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER); search_card.pack(fill="x", padx=10, pady=8)
        top = ctk.CTkFrame(search_card, fg_color="transparent"); top.pack(fill="x", padx=16, pady=12)
        self.mavin_type_var = tk.StringVar(value="Series")
        self.mavin_type_switch = ctk.CTkSegmentedButton(top, values=["Series", "Movies"], variable=self.mavin_type_var, command=lambda _: self.mavin_load_categories()); self.mavin_type_switch.pack(side="left", padx=4)
        self.mavin_cat_var = tk.StringVar(value="All Categories")
        self.mavin_cat_opt = ctk.CTkOptionMenu(top, variable=self.mavin_cat_var, values=["All Categories"], width=220); self.mavin_cat_opt.pack(side="left", padx=8)
        self.mavin_search_entry = ctk.CTkEntry(top, width=330, placeholder_text="Search Series or Movie..."); self.mavin_search_entry.pack(side="left", padx=4); self.add_context_menu(self.mavin_search_entry)
        ctk.CTkButton(top, text="🔍 Search", width=110, command=self.mavin_search, fg_color=COLOR_ACCENT, text_color=COLOR_BG).pack(side="left", padx=8)
        ctk.CTkButton(top, text="↻ Categories", width=120, command=self.mavin_load_categories, fg_color="transparent", border_width=1, border_color=COLOR_BORDER).pack(side="left", padx=4)

        result_card = ctk.CTkFrame(self.mavin_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER); result_card.pack(fill="both", expand=True, padx=10, pady=8)
        self.mavin_results_tree = ttk.Treeview(result_card, columns=("name", "type", "season", "path", "url"), show="headings", height=18, style="Custom.Treeview")
        for col, title, width in (("name", "Name", 360), ("type", "Type", 90), ("season", "Year / Season", 130), ("path", "Path", 85), ("url", "URL", 500)):
            self.mavin_results_tree.heading(col, text=title); self.mavin_results_tree.column(col, width=width)
        self.mavin_results_tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        sb = ttk.Scrollbar(result_card, orient="vertical", command=self.mavin_results_tree.yview); sb.pack(side="right", fill="y", padx=10, pady=10); self.mavin_results_tree.configure(yscrollcommand=sb.set)
        self.mavin_results_tree.bind("<Double-1>", self.mavin_open_selected)
        self.mavin_results_tree.bind("<<TreeviewSelect>>", self.mavin_show_selected_poster)
        self.mavin_result_items = {}
        self.mavin_poster_label = tk.Label(result_card, text="", bg=COLOR_CARD, fg=COLOR_TEXT_DIM, width=18, height=12)
        self.mavin_poster_label.pack(side="right", padx=12, pady=10)
        self.mavin_poster_image = None

        controls = ctk.CTkFrame(self.mavin_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER); controls.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(controls, text="Target Group:", text_color=COLOR_TEXT_DIM).pack(side="left", padx=(16, 4))
        self.mavin_group_var = tk.StringVar(value="Series 1")
        ctk.CTkOptionMenu(controls, variable=self.mavin_group_var, values=["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"], command=self.mavin_select_target_group).pack(side="left", padx=5)
        self.mavin_path_var = tk.StringVar(value="Path 1")
        self.mavin_path_opt = ctk.CTkOptionMenu(controls, variable=self.mavin_path_var, values=[f"Path {i+1}" for i in range(10)], command=self.mavin_select_target_path); self.mavin_path_opt.pack(side="left", padx=5)
        ctk.CTkButton(controls, text="Select All", width=100, command=lambda: self.mavin_select_results(True), fg_color="transparent", border_width=1, border_color=COLOR_BORDER).pack(side="left", padx=(20, 4))
        ctk.CTkButton(controls, text="Select None", width=100, command=lambda: self.mavin_select_results(False), fg_color="transparent", border_width=1, border_color=COLOR_BORDER).pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Clear Results", width=110, command=self.mavin_clear_results, fg_color="transparent", border_width=1, border_color=COLOR_BORDER).pack(side="left", padx=4)
        ctk.CTkButton(controls, text="🚀 Download Selected", width=170, command=self.mavin_download_selected, fg_color=COLOR_ACCENT, text_color=COLOR_BG).pack(side="left", padx=15)

        paths_card = ctk.CTkFrame(self.mavin_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER); paths_card.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(paths_card, text="Mavin Smart Save Paths • 60 Independent Paths", font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT).pack(pady=(12, 5))
        group_bar = ctk.CTkFrame(paths_card, fg_color="transparent"); group_bar.pack(fill="x", padx=12, pady=5)
        self.mavin_path_group_var = tk.StringVar(value="Series 1"); self.mavin_group_buttons = {}
        for group_name in ["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"]:
            btn = ctk.CTkButton(group_bar, text=group_name, width=95, height=30, fg_color=COLOR_ACCENT if group_name == "Series 1" else COLOR_CARD, text_color=COLOR_BG if group_name == "Series 1" else COLOR_TEXT, border_width=1, border_color=COLOR_BORDER, command=lambda g=group_name: self.mavin_switch_path_group(g)); btn.pack(side="left", padx=3); self.mavin_group_buttons[group_name] = btn
        self.mavin_path_frames = {}; self.mavin_path_entries = {}; self.mavin_path_slots = {}; self.mavin_active_path_group = None
        for group_name in ["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"]:
            frame = ctk.CTkFrame(paths_card, fg_color="transparent"); self.mavin_path_frames[group_name] = frame
            grid = ctk.CTkFrame(frame, fg_color="transparent"); grid.pack(fill="x", padx=8, pady=5)
            for col in range(5): grid.grid_columnconfigure(col, weight=1)
            entries = []
            for i in range(10):
                r, c = divmod(i, 5); slot = ctk.CTkFrame(grid, fg_color=COLOR_BG, corner_radius=8, border_width=1, border_color=COLOR_BORDER); slot.grid(row=r, column=c, padx=4, pady=4, sticky="ew")
                self.mavin_path_slots[(group_name, i)] = slot
                slot.bind("<Enter>", lambda e, g=group_name, idx=i: self.mavin_path_hover(g, idx)); slot.bind("<Leave>", self.mavin_path_leave)
                head = ctk.CTkFrame(slot, fg_color="transparent"); head.pack(fill="x", padx=6, pady=(4, 1))
                ctk.CTkLabel(head, text=f"PATH {i+1}", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_TEXT_DIM).pack(side="left")
                last = ctk.CTkLabel(head, text="Last: —", font=ctk.CTkFont(size=9), text_color=COLOR_ACCENT); last.pack(side="right")
                ent = ctk.CTkEntry(slot, height=28, placeholder_text=f"/home/downloads/mavin/{group_name.lower().replace(' ', '_')}/{i+1}"); ent.pack(fill="x", padx=6, pady=3); self.add_context_menu(ent)
                foot = ctk.CTkFrame(slot, fg_color="transparent"); foot.pack(fill="x", padx=5, pady=(0, 4))
                check = ctk.CTkButton(foot, text="✨ Check", width=62, height=24, fg_color="transparent", border_width=1, border_color=COLOR_ACCENT, text_color=COLOR_ACCENT, command=lambda g=group_name, idx=i: self.mavin_check_for_new(g, idx)); check.pack(side="left", padx=2)
                auto = tk.BooleanVar(value=False); ctk.CTkCheckBox(foot, text="Auto", variable=auto, width=54, checkbox_width=16, checkbox_height=16, fg_color=COLOR_ACCENT).pack(side="left", padx=2)
                interval = tk.StringVar(value="12h"); ctk.CTkOptionMenu(foot, variable=interval, values=["6h", "12h", "24h"], width=62, height=24, command=lambda v, g=group_name, idx=i: self.mavin_save_interval(g, idx, v)).pack(side="left", padx=2)
                for child in (head, last, ent, foot, check):
                    child.bind("<Enter>", lambda e, g=group_name, idx=i: self.mavin_path_hover(g, idx)); child.bind("<Leave>", self.mavin_path_leave)
                ent.bind("<FocusOut>", lambda e, g=group_name, idx=i: self.mavin_save_slot_path(g, idx))
                entries.append({"ent": ent, "last": last, "check": check, "auto": auto, "interval": interval, "slot": slot})
            self.mavin_path_entries[group_name] = entries
        self.mavin_switch_path_group("Series 1")

    def _mavin_refresh_slot_highlights(self):
        active_group = self.mavin_group_var.get(); active_index = int(self.mavin_path_var.get().split()[-1]) - 1
        for (group, index), slot in self.mavin_path_slots.items():
            selected = group == active_group and index == active_index
            slot.configure(border_color=COLOR_SUCCESS if selected else COLOR_BORDER, border_width=2 if selected else 1)

    def mavin_select_target_group(self, group):
        self.mavin_group_var.set(group); self.mavin_switch_path_group(group); self.mavin_select_target_path(self.mavin_path_var.get())

    def mavin_select_target_path(self, path):
        self.mavin_path_var.set(path); self._mavin_refresh_slot_highlights()
        group = self.mavin_group_var.get(); index = int(path.split()[-1]) - 1
        if group in self.mavin_path_entries and index < len(self.mavin_path_entries[group]):
            self.mavin_path_entries[group][index]["slot"].configure(border_color=COLOR_SUCCESS, border_width=2)

    def mavin_path_hover(self, group, index):
        self.mavin_path_leave()
        data = self._mavin_profile().get("mavin_paths", self._mavin_default_paths()).get(group, [])
        item = data[index] if index < len(data) else {}; meta = item.get("history", {})
        win = tk.Toplevel(self); self.mavin_hover_window = win; win.overrideredirect(True); win.attributes("-topmost", True); win.configure(bg="#0f172a")
        x, y = self.winfo_pointerx() + 18, self.winfo_pointery() + 18; win.geometry(f"320x300+{x}+{y}")
        box = tk.Frame(win, bg="#0f172a", highlightthickness=2, highlightbackground=COLOR_SUCCESS); box.pack(fill="both", expand=True)
        tk.Label(box, text=meta.get("name") or item.get("last") or "No download recorded", bg="#0f172a", fg="#f8fafc", font=("Segoe UI", 16, "bold"), wraplength=290).pack(pady=(12, 5))
        tk.Label(box, text=f"{group} / Path {index+1}", bg="#0f172a", fg=COLOR_SUCCESS, font=("Segoe UI", 10, "bold")).pack()
        poster = meta.get("poster")
        if poster:
            def load_poster(url=poster, target=win, container=box):
                try:
                    response = requests.get(url, timeout=8); response.raise_for_status(); image = Image.open(io.BytesIO(response.content)); image.thumbnail((140, 190)); photo = ImageTk.PhotoImage(image)
                    if getattr(self, "mavin_hover_window", None) is target and target.winfo_exists():
                        target._mavin_photo = photo; tk.Label(container, image=photo, bg="#0f172a").pack(pady=8)
                except Exception: pass
            threading.Thread(target=load_poster, daemon=True).start()

    def mavin_path_leave(self, event=None):
        win = getattr(self, "mavin_hover_window", None); self.mavin_hover_window = None
        if win:
            try: win.destroy()
            except Exception: pass

    def mavin_background_loop(self):
        while True:
            try:
                time.sleep(60)
                if not hasattr(self, "mavin_path_entries"): continue
                p = self._mavin_profile(); paths = p.get("mavin_paths", {})
                now = time.time()
                changed = False
                for group, entries in paths.items():
                    for index, item in enumerate(entries[:10]):
                        if not item.get("auto") or not item.get("history"): continue
                        interval = item.get("interval", "12h")
                        hours = int(str(interval).rstrip("h")) if str(interval).rstrip("h").isdigit() else 12
                        if now - float(item.get("last_check", 0) or 0) >= hours * 3600:
                            item["last_check"] = now; changed = True
                            self.mavin_check_for_new(group, index, silent=True)
                if changed:
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)
            except Exception as exc:
                local_debug_log(f"MAVIN BACKGROUND CHECK ERROR: {exc}")

    def _mavin_default_paths(self):
        return {group: [{"path": f"/home/downloads/mavin/{group.lower().replace(' ', '_')}/{i+1}", "auto": False, "interval": "12h", "last": "", "history": {}} for i in range(10)] for group in ["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"]}

    def mavin_save_interval(self, group, index, value):
        p = self._mavin_profile(); data = p.setdefault("mavin_paths", self._mavin_default_paths()); data.setdefault(group, self._mavin_default_paths()[group])
        data[group][index]["interval"] = value
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)

    def mavin_save_slot_path(self, group, index):
        p = self._mavin_profile(); data = p.setdefault("mavin_paths", self._mavin_default_paths())
        data.setdefault(group, self._mavin_default_paths()[group])
        data[group][index]["path"] = self.mavin_path_entries[group][index]["ent"].get().strip()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)

    def _mavin_save_paths(self):
        p = self._mavin_profile(); data = p.setdefault("mavin_paths", self._mavin_default_paths())
        group = getattr(self, "mavin_active_path_group", None)
        if group and group in self.mavin_path_entries:
            entries = self.mavin_path_entries[group]
            data.setdefault(group, self._mavin_default_paths()[group])
            for i, item in enumerate(entries):
                data[group][i]["path"] = item["ent"].get().strip()
                data[group][i]["auto"] = bool(item["auto"].get())
        p["mavin_paths"] = data
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)

    def mavin_switch_path_group(self, group):
        if group not in self.mavin_path_frames: return
        self._mavin_save_paths()
        for frame in self.mavin_path_frames.values(): frame.pack_forget()
        data = self._mavin_profile().get("mavin_paths", self._mavin_default_paths()).get(group, [])
        for i, entry in enumerate(self.mavin_path_entries[group]):
            item = data[i] if i < len(data) else {}
            entry["ent"].delete(0, tk.END); entry["ent"].insert(0, item.get("path", ""))
            entry["auto"].set(item.get("auto", False)); entry["interval"].set(item.get("interval", "12h")); entry["last"].configure(text=f"Last: {item.get('last', '') or '—'}")
        self.mavin_path_frames[group].pack(fill="x")
        self.mavin_path_group_var.set(group)
        self.mavin_active_path_group = group
        for name, button in self.mavin_group_buttons.items():
            active = name == group
            button.configure(fg_color=COLOR_SUCCESS if active else COLOR_CARD, text_color=COLOR_BG if active else COLOR_TEXT, border_color=COLOR_SUCCESS if active else COLOR_BORDER)
        self.mavin_group_var.set(group)

    def mavin_check_for_new(self, group, index, silent=False):
        p = self._mavin_profile(); paths = p.get("mavin_paths", self._mavin_default_paths()); item = paths.get(group, [{}] * 10)[index] if index < len(paths.get(group, [])) else {}
        meta = item.get("history", {})
        if not meta.get("series_id") and not meta.get("movie_id"):
            if not silent: messagebox.showinfo("Mavin Check for New", "No previous Mavin download is recorded for this path yet.")
            return
        def work():
            try:
                if meta.get("series_id"):
                    remote = self._mavin_request({"mode": "episode", "series_id": meta["series_id"]})
                else:
                    remote = []
                existing_names = set()
                host = self.host_entry.get().strip(); port = int(self.port_entry.get() or 22); user = self.user_entry.get().strip(); pw = self.pass_entry.get()
                if host and item.get("path"):
                    ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()); ssh.connect(host, port=port, username=user, password=pw, timeout=10)
                    _, out, _ = ssh.exec_command(f"find {shlex.quote(item['path'])} -maxdepth 1 -type f -printf '%f\\n'"); existing_names = {os.path.splitext(x.strip())[0].lower() for x in out.read().splitlines() if x.strip()}; ssh.close()
                def number_value(value):
                    match = re.search(r"\d+", str(value or "")); return int(match.group(0)) if match else None
                def episode_key(value, season=None, order=None):
                    season_num, order_num = number_value(season), number_value(order)
                    if season_num is not None and order_num is not None: return (season_num, order_num)
                    match = re.search(r"s\s*0*(\d+).*?(?:e|ep)\s*0*(\d+)", str(value), re.I)
                    return (int(match.group(1)), int(match.group(2))) if match else None
                existing_keys = {episode_key(name) for name in existing_names if episode_key(name)}
                saved_keys = set()
                for saved in meta.get("episodes", []):
                    if isinstance(saved, (list, tuple)) and len(saved) == 2: saved_keys.add((int(saved[0]), int(saved[1])))
                saved_key = episode_key(meta.get("name", ""), meta.get("season"), meta.get("order"))
                if saved_key: saved_keys.add(saved_key)
                known_keys = existing_keys | saved_keys
                baseline = max(known_keys, default=None)
                all_items = []
                for x in remote:
                    season_value = x.get("season") or x.get("season_num")
                    order_value = x.get("order") or x.get("episode") or x.get("episode_num")
                    key = episode_key(x.get("name", ""), season_value, order_value)
                    normalized = {**x, "season": number_value(season_value), "order": number_value(order_value)}
                    normalized["_is_new"] = bool(key and baseline is not None and key > baseline)
                    all_items.append(normalized)
                all_items.sort(key=lambda x: episode_key(x.get("name", ""), x.get("season"), x.get("order")) or (999999, 999999))
                new_items = [x for x in all_items if x.get("_is_new")]
                if baseline is None:
                    local_debug_log(f"MAVIN CHECK BLOCKED: no valid episode keys; group={group}, path={index+1}, target={item.get('path','')}")
                local_debug_log(f"MAVIN CHECK: group={group}, path={index+1}, target={item.get('path','')}, server_keys={sorted(existing_keys)}, saved_keys={sorted(saved_keys)}, baseline={baseline}, new={len(new_items)}")
                rows = [{"name": x.get("name", ""), "url": x.get("url", ""), "type": "Series", "season": f"S{x.get('season', '')} E{x.get('order', '')}", "order": x.get("order", ""), "season_number": x.get("season", ""), "poster": meta.get("poster"), "series_id": meta.get("series_id"), "group": group, "path_label": f"{group} / Path {index+1}", "_is_new": x.get("_is_new", False)} for x in all_items]
                def finish_check():
                    self.mavin_fill_episode_results(rows)
                    new_ids = [iid for iid in self.mavin_results_tree.get_children() if self.mavin_result_items.get(iid, {}).get("_is_new")]
                    self.mavin_results_tree.selection_set(new_ids)
                    self.mavin_status_lbl.configure(text=f"Mavin Check: {len(rows)} total, {len(new_ids)} new selected in {group} Path {index+1}", text_color=COLOR_SUCCESS)
                self.after(0, finish_check)
            except Exception as exc: self.after(0, lambda: self.mavin_status_lbl.configure(text=f"Mavin Check error: {exc}", text_color=COLOR_DANGER))
        threading.Thread(target=work, daemon=True).start()

    def _mavin_profile(self):
        p = self.current_profile.get()
        self.config.setdefault(p, {})
        return self.config[p]

    @staticmethod
    def _mavin_xor(data):
        key = b"9yLxnB0XIO"
        return bytes(value ^ key[i % len(key)] for i, value in enumerate(data))

    def _mavin_encode(self, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        import base64
        encoded = base64.b64encode(self._mavin_xor(raw)).decode("ascii")
        return encoded.translate(str.maketrans({"+": "!", "/": ")", "=": "("}))

    def _mavin_decode(self, content):
        import base64
        for candidate in (content, content.decode("utf-8", errors="replace").strip().encode("utf-8")):
            try: return json.loads(self._mavin_xor(candidate).decode("utf-8"))
            except Exception: pass
            try:
                normalized = candidate.replace(b"!", b"+").replace(b")", b"/").replace(b"(", b"=")
                return json.loads(self._mavin_xor(base64.b64decode(normalized)).decode("utf-8"))
            except Exception: pass
            try: return json.loads(candidate.decode("utf-8"))
            except Exception: pass
        return {"status": "ERROR", "message": "Invalid server response"}

    def _mavin_request(self, payload):
        api = self.mavin_api_entry.get().strip() or "http://maveniptv.xyz/mvpro/maven.php"
        data = {"mode": payload.pop("mode", ""), "code": self.mavin_code_entry.get().strip(), "mac": self.mavin_mac_entry.get().strip(), "sn": self.mavin_sn_entry.get().strip()}
        data.update(payload)
        response = requests.post(api, files={"json": (None, self._mavin_encode(data))}, headers={"User-Agent": "IPTVSmartersPlayer", "Accept-Encoding": "gzip, deflate"}, timeout=30)
        result = self._mavin_decode(response.content)
        local_debug_log(f"MAVIN API\nURL: {api}\nREQUEST: {json.dumps(data, ensure_ascii=False)}\nHTTP: {response.status_code}\nRESPONSE: {json.dumps(result, ensure_ascii=False)}")
        return result

    def mavin_activate(self):
        def work():
            try:
                p = self._mavin_profile(); p.update({"mavin_api": self.mavin_api_entry.get().strip() or "http://maveniptv.xyz/mvpro/maven.php", "mavin_code": self.mavin_code_entry.get().strip(), "mavin_sn": self.mavin_sn_entry.get().strip(), "mavin_mac": self.mavin_mac_entry.get().strip()})
                result = self._mavin_request({"mode": "active"})
                status = str(result.get("status", "")).upper()
                ok = status in ("ACTIVE", "100")
                if ok:
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)
                    msg = f"ACTIVE • {result.get('expire', '')}"
                    self.after(0, lambda: self.mavin_status_lbl.configure(text=msg, text_color=COLOR_SUCCESS))
                    self.after(0, self.mavin_load_categories)
                else:
                    self.after(0, lambda: self.mavin_status_lbl.configure(text=f"Activation failed: {result.get('message', status)}", text_color=COLOR_DANGER))
            except Exception as exc:
                self.after(0, lambda: self.mavin_status_lbl.configure(text=f"Error: {exc}", text_color=COLOR_DANGER))
        threading.Thread(target=work, daemon=True).start()

    def mavin_load_categories(self):
        if not self.mavin_code_entry.get().strip(): return
        kind = "series" if self.mavin_type_var.get() == "Series" else "movie"
        def work():
            try:
                result = self._mavin_request({"mode": "category", "cat_type": kind})
                categories = result if isinstance(result, list) else []
                self.mavin_categories = categories
                names = ["All Categories"] + [str(x.get("name", "")) for x in categories if x.get("name")]
                self.after(0, lambda: self.mavin_cat_opt.configure(values=names))
                self.after(0, lambda: self.mavin_cat_var.set(names[0] if names else "All Categories"))
            except Exception as exc: self.after(0, lambda: self.mavin_status_lbl.configure(text=f"Category error: {exc}", text_color=COLOR_DANGER))
        threading.Thread(target=work, daemon=True).start()

    def mavin_search(self):
        keyword = self.mavin_search_entry.get().strip().lower(); kind = "series" if self.mavin_type_var.get() == "Series" else "movie"
        cat_id = None
        for item in getattr(self, "mavin_categories", []):
            if item.get("name") == self.mavin_cat_var.get(): cat_id = item.get("id"); break
        def work():
            try:
                result = self._mavin_request({"mode": kind, "cat_id": cat_id or "", "from": 0, "lenght": 100})
                items = result if isinstance(result, list) else []
                if keyword: items = [x for x in items if keyword in str(x.get("name", "")).lower()]
                self.after(0, lambda: self.mavin_fill_results(items, kind))
            except Exception as exc: self.after(0, lambda: self.mavin_status_lbl.configure(text=f"Search error: {exc}", text_color=COLOR_DANGER))
        threading.Thread(target=work, daemon=True).start()

    def mavin_fill_results(self, items, kind):
        for iid in self.mavin_results_tree.get_children(): self.mavin_results_tree.delete(iid)
        self.mavin_result_items = {}
        for index, item in enumerate(items):
            iid = self.mavin_results_tree.insert("", "end", values=(item.get("name", ""), "Series" if kind == "series" else "Movie", item.get("year", ""), item.get("path_label", "—"), item.get("url", "")))
            self.mavin_result_items[iid] = {**item, "mavin_kind": kind}

    def mavin_open_selected(self, event=None):
        selected = self.mavin_results_tree.selection()
        if not selected: return
        item = self.mavin_result_items.get(selected[0], {})
        if item.get("mavin_kind") == "series" and item.get("id"):
            def work():
                try:
                    episodes = self._mavin_request({"mode": "episode", "series_id": item.get("id")})
                    rows = []
                    for ep in episodes if isinstance(episodes, list) else []:
                        rows.append({"name": ep.get("name", ""), "url": ep.get("url", ""), "type": "Series", "season": f"S{ep.get('season', '')} E{ep.get('order', '')}", "season_number": ep.get("season", ""), "order": ep.get("order", ""), "poster": item.get("logo"), "group": self.mavin_group_var.get(), "series_id": item.get("id"), "path_label": f"{self.mavin_group_var.get()} / {self.mavin_path_var.get()}"})
                    self.after(0, lambda: self.mavin_fill_episode_results(rows))
                except Exception as exc: self.after(0, lambda: self.mavin_status_lbl.configure(text=f"Episode error: {exc}", text_color=COLOR_DANGER))
            threading.Thread(target=work, daemon=True).start()
        elif item.get("mavin_kind") == "movie" and item.get("id"):
            def work_movie():
                try:
                    info = self._mavin_request({"mode": "movie", "id": item.get("id")})
                    if isinstance(info, dict): info.update({"mavin_kind": "movie", "type": "Movie"})
                    self.after(0, lambda: self.mavin_fill_results([info], "movie"))
                except Exception as exc: self.after(0, lambda: self.mavin_status_lbl.configure(text=f"Movie error: {exc}", text_color=COLOR_DANGER))
            threading.Thread(target=work_movie, daemon=True).start()

    def mavin_fill_episode_results(self, items):
        for iid in self.mavin_results_tree.get_children(): self.mavin_results_tree.delete(iid)
        self.mavin_result_items = {}
        for item in items:
            iid = self.mavin_results_tree.insert("", "end", values=(item.get("name", ""), item.get("type", "Series"), item.get("season", ""), item.get("path_label", f"{item.get('group', self.mavin_group_var.get())} / {self.mavin_path_var.get()}"), item.get("url", "")))
            self.mavin_result_items[iid] = item

    def mavin_show_selected_poster(self, event=None):
        selected = self.mavin_results_tree.selection()
        if not selected: return
        item = self.mavin_result_items.get(selected[0], {}); url = item.get("poster") or item.get("logo")
        if not url:
            self.mavin_poster_label.configure(image="", text="No poster")
            return
        def load():
            try:
                response = requests.get(url, timeout=8); response.raise_for_status(); image = Image.open(io.BytesIO(response.content)); image.thumbnail((140, 190)); photo = ImageTk.PhotoImage(image)
                self.after(0, lambda: (setattr(self, "mavin_poster_image", photo), self.mavin_poster_label.configure(image=photo, text="")))
            except Exception:
                self.after(0, lambda: self.mavin_poster_label.configure(image="", text="Poster unavailable"))
        threading.Thread(target=load, daemon=True).start()

    def mavin_select_results(self, select=True):
        ids = self.mavin_results_tree.get_children()
        self.mavin_results_tree.selection_set(ids if select else ())

    def mavin_clear_results(self):
        for iid in self.mavin_results_tree.get_children(): self.mavin_results_tree.delete(iid)
        self.mavin_result_items = {}
        self.mavin_poster_image = None
        self.mavin_poster_label.configure(image="", text="")
        self.mavin_status_lbl.configure(text="Mavin results cleared", text_color=COLOR_TEXT_DIM)

    def mavin_refresh_paths(self, group=None):
        self.mavin_path_opt.configure(values=[f"Path {i+1}" for i in range(10)])

    def _mavin_normalize_episode_name(self, name, season=None, episode=None):
        """Normalize Mavin episode tokens only: S1 E1/SE1 EP1 -> S01 E01."""
        text = str(name or "")
        # Accept S1 E1, S01E01, SE1 EP1, and SE01 EP02.
        # Values are read from the name itself so the filename remains faithful to the item.
        pattern = re.compile(r"(?i)\bS\s*E?\s*0*(\d+)\s*[-_. ]*\s*(?:E|EP)\s*0*(\d+)\b")
        match = pattern.search(text)
        if not match: return text
        try:
            season_num, episode_num = int(match.group(1)), int(match.group(2))
        except (TypeError, ValueError):
            return text
        return pattern.sub(f"S{season_num:02d} E{episode_num:02d}", text, count=1)

    def mavin_download_selected(self):
        ids = self.mavin_results_tree.selection()
        if not ids: messagebox.showwarning("Mavin", "No content selected."); return
        p = self._mavin_profile(); self._mavin_save_paths(); group = self.mavin_group_var.get(); path_idx = int(self.mavin_path_var.get().split()[-1]) - 1
        paths = p.get("mavin_paths", self._mavin_default_paths()).get(group, [])
        target = (paths[path_idx].get("path") if len(paths) > path_idx else "")
        if not target: messagebox.showwarning("Mavin", "The selected target path is empty."); return
        selected = []
        for iid in ids:
            item = self.mavin_result_items.get(iid, {}); url = item.get("url", "")
            if url:
                season_value = item.get("season_number", item.get("season", "")); order_value = item.get("order", item.get("episode", ""))
                display_name = self._mavin_normalize_episode_name(item.get("name", "Untitled"), season_value, order_value)
                selected.append({"name": display_name, "url": url, "path": target, "group": group, "poster": item.get("poster") or item.get("logo"), "id": item.get("id", ""), "series_id": item.get("series_id", ""), "season": season_value, "order": order_value, "section_transcode": self.mavin_auto_transcode_v.get()})
        if selected:
            path_data = p.setdefault("mavin_paths", self._mavin_default_paths())
            slot = path_data.setdefault(group, self._mavin_default_paths()[group])[path_idx]
            def episode_pair(item):
                try:
                    if item.get("season") not in (None, "") and item.get("order") not in (None, ""): return (int(item.get("season")), int(item.get("order")))
                except (TypeError, ValueError): pass
                m = re.search(r"s\s*0*(\d+).*?(?:e|ep)\s*0*(\d+)", str(item.get("name", "")), re.I)
                return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)
            first = selected[0]; last_item = max(selected, key=episode_pair); last_pair = episode_pair(last_item)
            slot["last"] = last_item["name"]
            slot["history"] = {"series_id": first.get("series_id", ""), "movie_id": first.get("id", ""), "name": last_item.get("name", ""), "season": last_pair[0] if last_pair[0] >= 0 else "", "order": last_pair[1] if last_pair[1] >= 0 else "", "poster": first.get("poster", ""), "episodes": [[pair[0], pair[1]] for pair in (episode_pair(x) for x in selected) if pair[0] >= 0], "updated": time.time()}
            with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)
            self.mavin_path_entries[group][path_idx]["last"].configure(text=f"Last: {last_item['name']}")
            self.enqueue_download_job(self.mavin_execute_queued_download, selected, count=len(selected))

    def mavin_execute_queued_download(self, selected_items):
        self.execute_m3u_download_sequential(selected_items)
        if not selected_items: return
        group = selected_items[0].get("group", "Series 1"); path = selected_items[0].get("path", "")
        p = self._mavin_profile(); data = p.setdefault("mavin_paths", self._mavin_default_paths())
        for index, slot in enumerate(data.get(group, [])):
            if slot.get("path") == path:
                item = selected_items[-1]
                slot["last"] = item.get("name", slot.get("last", ""))
                history = slot.setdefault("history", {}); history.update({"name": slot["last"], "updated": time.time()})
                completed = history.setdefault("completed_episodes", [])
                for queued in selected_items:
                    season, order = queued.get("season"), queued.get("order")
                    if season not in (None, "") and order not in (None, ""):
                        pair = [int(season), int(order)]
                        if pair not in completed: completed.append(pair)
                history["episodes"] = completed
                if group in self.mavin_path_entries and index < len(self.mavin_path_entries[group]):
                    self.after(0, lambda i=index, g=group, n=slot["last"]: self.mavin_path_entries[g][i]["last"].configure(text=f"Last: {n}"))
                break
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)


    def setup_cobra_tab(self):
        self.cobra_container = ctk.CTkScrollableFrame(self.view_cobra, fg_color="transparent")
        self.cobra_container.pack(fill="both", expand=True)
        self.cobra_auto_transcode_v = tk.BooleanVar(value=False)
        card = ctk.CTkFrame(self.cobra_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(card, text="🔐 Cobra IPTV • Series & Movies Only", font=ctk.CTkFont(size=18, weight="bold"), text_color=COLOR_ACCENT).pack(pady=(14, 8))
        self.cobra_auto_transcode_chk = ctk.CTkCheckBox(card, text=TRANSLATIONS[self.lang]["cobra_auto_transcode"], variable=self.cobra_auto_transcode_v, fg_color=COLOR_ACCENT, border_color=COLOR_BORDER)
        self.cobra_auto_transcode_chk.pack(anchor="w", padx=20, pady=(0, 8))
        auth = ctk.CTkFrame(card, fg_color="transparent"); auth.pack(fill="x", padx=16, pady=8)
        self.cobra_api_entry = ctk.CTkEntry(auth, width=330, placeholder_text="API URL"); self.cobra_api_entry.pack(side="left", padx=4); self.add_context_menu(self.cobra_api_entry)
        self.cobra_code_entry = ctk.CTkEntry(auth, width=150, placeholder_text="Active Code"); self.cobra_code_entry.pack(side="left", padx=4); self.add_context_menu(self.cobra_code_entry)
        self.cobra_sn_entry = ctk.CTkEntry(auth, width=150, placeholder_text="Serial Number"); self.cobra_sn_entry.pack(side="left", padx=4); self.add_context_menu(self.cobra_sn_entry)
        self.cobra_mac_entry = ctk.CTkEntry(auth, width=150, placeholder_text="MAC Address"); self.cobra_mac_entry.pack(side="left", padx=4); self.add_context_menu(self.cobra_mac_entry)
        self.cobra_activate_btn = ctk.CTkButton(auth, text="Activate", width=110, command=self.cobra_activate, fg_color=COLOR_ACCENT, text_color=COLOR_BG); self.cobra_activate_btn.pack(side="left", padx=8)
        self.cobra_status_lbl = ctk.CTkLabel(card, text="Not activated", text_color=COLOR_TEXT_DIM); self.cobra_status_lbl.pack(anchor="w", padx=20, pady=(0, 10))

        search_card = ctk.CTkFrame(self.cobra_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER); search_card.pack(fill="x", padx=10, pady=8)
        top = ctk.CTkFrame(search_card, fg_color="transparent"); top.pack(fill="x", padx=16, pady=12)
        self.cobra_type_var = tk.StringVar(value="Series")
        self.cobra_type_switch = ctk.CTkSegmentedButton(top, values=["Series", "Movies"], variable=self.cobra_type_var, command=lambda _: self.cobra_load_categories()); self.cobra_type_switch.pack(side="left", padx=4)
        self.cobra_cat_var = tk.StringVar(value="All Categories")
        self.cobra_cat_opt = ctk.CTkOptionMenu(top, variable=self.cobra_cat_var, values=["All Categories"], width=220); self.cobra_cat_opt.pack(side="left", padx=8)
        self.cobra_search_entry = ctk.CTkEntry(top, width=330, placeholder_text="Search Series or Movie..."); self.cobra_search_entry.pack(side="left", padx=4); self.add_context_menu(self.cobra_search_entry)
        ctk.CTkButton(top, text="🔍 Search", width=110, command=self.cobra_search, fg_color=COLOR_ACCENT, text_color=COLOR_BG).pack(side="left", padx=8)
        ctk.CTkButton(top, text="↻ Categories", width=120, command=self.cobra_load_categories, fg_color="transparent", border_width=1, border_color=COLOR_BORDER).pack(side="left", padx=4)

        result_card = ctk.CTkFrame(self.cobra_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER); result_card.pack(fill="both", expand=True, padx=10, pady=8)
        self.cobra_results_tree = ttk.Treeview(result_card, columns=("name", "type", "season", "path", "url"), show="headings", height=18, style="Custom.Treeview")
        for col, title, width in (("name", "Name", 360), ("type", "Type", 90), ("season", "Year / Season", 130), ("path", "Path", 85), ("url", "URL", 500)):
            self.cobra_results_tree.heading(col, text=title); self.cobra_results_tree.column(col, width=width)
        self.cobra_results_tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        sb = ttk.Scrollbar(result_card, orient="vertical", command=self.cobra_results_tree.yview); sb.pack(side="right", fill="y", padx=10, pady=10); self.cobra_results_tree.configure(yscrollcommand=sb.set)
        self.cobra_results_tree.bind("<Double-1>", self.cobra_open_selected)
        self.cobra_results_tree.bind("<<TreeviewSelect>>", self.cobra_show_selected_poster)
        self.cobra_result_items = {}
        self.cobra_poster_label = tk.Label(result_card, text="", bg=COLOR_CARD, fg=COLOR_TEXT_DIM, width=18, height=12)
        self.cobra_poster_label.pack(side="right", padx=12, pady=10)
        self.cobra_poster_image = None

        controls = ctk.CTkFrame(self.cobra_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER); controls.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(controls, text="Target Group:", text_color=COLOR_TEXT_DIM).pack(side="left", padx=(16, 4))
        self.cobra_group_var = tk.StringVar(value="Series 1")
        ctk.CTkOptionMenu(controls, variable=self.cobra_group_var, values=["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"], command=self.cobra_select_target_group).pack(side="left", padx=5)
        self.cobra_path_var = tk.StringVar(value="Path 1")
        self.cobra_path_opt = ctk.CTkOptionMenu(controls, variable=self.cobra_path_var, values=[f"Path {i+1}" for i in range(10)], command=self.cobra_select_target_path); self.cobra_path_opt.pack(side="left", padx=5)
        ctk.CTkButton(controls, text="Select All", width=100, command=lambda: self.cobra_select_results(True), fg_color="transparent", border_width=1, border_color=COLOR_BORDER).pack(side="left", padx=(20, 4))
        ctk.CTkButton(controls, text="Select None", width=100, command=lambda: self.cobra_select_results(False), fg_color="transparent", border_width=1, border_color=COLOR_BORDER).pack(side="left", padx=4)
        ctk.CTkButton(controls, text="Clear Results", width=110, command=self.cobra_clear_results, fg_color="transparent", border_width=1, border_color=COLOR_BORDER).pack(side="left", padx=4)
        ctk.CTkButton(controls, text="🚀 Download Selected", width=170, command=self.cobra_download_selected, fg_color=COLOR_ACCENT, text_color=COLOR_BG).pack(side="left", padx=15)

        paths_card = ctk.CTkFrame(self.cobra_container, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER); paths_card.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(paths_card, text="Cobra Smart Save Paths • 60 Independent Paths", font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_ACCENT).pack(pady=(12, 5))
        group_bar = ctk.CTkFrame(paths_card, fg_color="transparent"); group_bar.pack(fill="x", padx=12, pady=5)
        self.cobra_path_group_var = tk.StringVar(value="Series 1"); self.cobra_group_buttons = {}
        for group_name in ["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"]:
            btn = ctk.CTkButton(group_bar, text=group_name, width=95, height=30, fg_color=COLOR_ACCENT if group_name == "Series 1" else COLOR_CARD, text_color=COLOR_BG if group_name == "Series 1" else COLOR_TEXT, border_width=1, border_color=COLOR_BORDER, command=lambda g=group_name: self.cobra_switch_path_group(g)); btn.pack(side="left", padx=3); self.cobra_group_buttons[group_name] = btn
        self.cobra_path_frames = {}; self.cobra_path_entries = {}; self.cobra_path_slots = {}; self.cobra_active_path_group = None
        for group_name in ["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"]:
            frame = ctk.CTkFrame(paths_card, fg_color="transparent"); self.cobra_path_frames[group_name] = frame
            grid = ctk.CTkFrame(frame, fg_color="transparent"); grid.pack(fill="x", padx=8, pady=5)
            for col in range(5): grid.grid_columnconfigure(col, weight=1)
            entries = []
            for i in range(10):
                r, c = divmod(i, 5); slot = ctk.CTkFrame(grid, fg_color=COLOR_BG, corner_radius=8, border_width=1, border_color=COLOR_BORDER); slot.grid(row=r, column=c, padx=4, pady=4, sticky="ew")
                self.cobra_path_slots[(group_name, i)] = slot
                slot.bind("<Enter>", lambda e, g=group_name, idx=i: self.cobra_path_hover(g, idx)); slot.bind("<Leave>", self.cobra_path_leave)
                head = ctk.CTkFrame(slot, fg_color="transparent"); head.pack(fill="x", padx=6, pady=(4, 1))
                ctk.CTkLabel(head, text=f"PATH {i+1}", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_TEXT_DIM).pack(side="left")
                last = ctk.CTkLabel(head, text="Last: —", font=ctk.CTkFont(size=9), text_color=COLOR_ACCENT); last.pack(side="right")
                ent = ctk.CTkEntry(slot, height=28, placeholder_text=f"/home/downloads/cobra/{group_name.lower().replace(' ', '_')}/{i+1}"); ent.pack(fill="x", padx=6, pady=3); self.add_context_menu(ent)
                foot = ctk.CTkFrame(slot, fg_color="transparent"); foot.pack(fill="x", padx=5, pady=(0, 4))
                check = ctk.CTkButton(foot, text="✨ Check", width=62, height=24, fg_color="transparent", border_width=1, border_color=COLOR_ACCENT, text_color=COLOR_ACCENT, command=lambda g=group_name, idx=i: self.cobra_check_for_new(g, idx)); check.pack(side="left", padx=2)
                auto = tk.BooleanVar(value=False); ctk.CTkCheckBox(foot, text="Auto", variable=auto, width=54, checkbox_width=16, checkbox_height=16, fg_color=COLOR_ACCENT).pack(side="left", padx=2)
                interval = tk.StringVar(value="12h"); ctk.CTkOptionMenu(foot, variable=interval, values=["6h", "12h", "24h"], width=62, height=24, command=lambda v, g=group_name, idx=i: self.cobra_save_interval(g, idx, v)).pack(side="left", padx=2)
                for child in (head, last, ent, foot, check):
                    child.bind("<Enter>", lambda e, g=group_name, idx=i: self.cobra_path_hover(g, idx)); child.bind("<Leave>", self.cobra_path_leave)
                ent.bind("<FocusOut>", lambda e, g=group_name, idx=i: self.cobra_save_slot_path(g, idx))
                entries.append({"ent": ent, "last": last, "check": check, "auto": auto, "interval": interval, "slot": slot})
            self.cobra_path_entries[group_name] = entries
        self.cobra_switch_path_group("Series 1")

    def _cobra_refresh_slot_highlights(self):
        active_group = self.cobra_group_var.get(); active_index = int(self.cobra_path_var.get().split()[-1]) - 1
        for (group, index), slot in self.cobra_path_slots.items():
            selected = group == active_group and index == active_index
            slot.configure(border_color=COLOR_SUCCESS if selected else COLOR_BORDER, border_width=2 if selected else 1)

    def cobra_select_target_group(self, group):
        self.cobra_group_var.set(group); self.cobra_switch_path_group(group); self.cobra_select_target_path(self.cobra_path_var.get())

    def cobra_select_target_path(self, path):
        self.cobra_path_var.set(path); self._cobra_refresh_slot_highlights()
        group = self.cobra_group_var.get(); index = int(path.split()[-1]) - 1
        if group in self.cobra_path_entries and index < len(self.cobra_path_entries[group]):
            self.cobra_path_entries[group][index]["slot"].configure(border_color=COLOR_SUCCESS, border_width=2)

    def cobra_path_hover(self, group, index):
        self.cobra_path_leave()
        data = self._cobra_profile().get("cobra_paths", self._cobra_default_paths()).get(group, [])
        item = data[index] if index < len(data) else {}; meta = item.get("history", {})
        win = tk.Toplevel(self); self.cobra_hover_window = win; win.overrideredirect(True); win.attributes("-topmost", True); win.configure(bg="#0f172a")
        x, y = self.winfo_pointerx() + 18, self.winfo_pointery() + 18; win.geometry(f"320x300+{x}+{y}")
        box = tk.Frame(win, bg="#0f172a", highlightthickness=2, highlightbackground=COLOR_SUCCESS); box.pack(fill="both", expand=True)
        tk.Label(box, text=meta.get("name") or item.get("last") or "No download recorded", bg="#0f172a", fg="#f8fafc", font=("Segoe UI", 16, "bold"), wraplength=290).pack(pady=(12, 5))
        tk.Label(box, text=f"{group} / Path {index+1}", bg="#0f172a", fg=COLOR_SUCCESS, font=("Segoe UI", 10, "bold")).pack()
        poster = meta.get("poster")
        if poster:
            def load_poster(url=poster, target=win, container=box):
                try:
                    response = requests.get(url, timeout=8); response.raise_for_status(); image = Image.open(io.BytesIO(response.content)); image.thumbnail((140, 190)); photo = ImageTk.PhotoImage(image)
                    if getattr(self, "cobra_hover_window", None) is target and target.winfo_exists():
                        target._cobra_photo = photo; tk.Label(container, image=photo, bg="#0f172a").pack(pady=8)
                except Exception: pass
            threading.Thread(target=load_poster, daemon=True).start()

    def cobra_path_leave(self, event=None):
        win = getattr(self, "cobra_hover_window", None); self.cobra_hover_window = None
        if win:
            try: win.destroy()
            except Exception: pass

    def cobra_background_loop(self):
        while True:
            try:
                time.sleep(60)
                if not hasattr(self, "cobra_path_entries"): continue
                p = self._cobra_profile(); paths = p.get("cobra_paths", {})
                now = time.time()
                changed = False
                for group, entries in paths.items():
                    for index, item in enumerate(entries[:10]):
                        if not item.get("auto") or not item.get("history"): continue
                        interval = item.get("interval", "12h")
                        hours = int(str(interval).rstrip("h")) if str(interval).rstrip("h").isdigit() else 12
                        if now - float(item.get("last_check", 0) or 0) >= hours * 3600:
                            item["last_check"] = now; changed = True
                            self.cobra_check_for_new(group, index, silent=True)
                if changed:
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)
            except Exception as exc:
                local_debug_log(f"MAVIN BACKGROUND CHECK ERROR: {exc}")

    def _cobra_default_paths(self):
        return {group: [{"path": f"/home/downloads/cobra/{group.lower().replace(' ', '_')}/{i+1}", "auto": False, "interval": "12h", "last": "", "history": {}} for i in range(10)] for group in ["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"]}

    def cobra_save_interval(self, group, index, value):
        p = self._cobra_profile(); data = p.setdefault("cobra_paths", self._cobra_default_paths()); data.setdefault(group, self._cobra_default_paths()[group])
        data[group][index]["interval"] = value
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)

    def cobra_save_slot_path(self, group, index):
        p = self._cobra_profile(); data = p.setdefault("cobra_paths", self._cobra_default_paths())
        data.setdefault(group, self._cobra_default_paths()[group])
        data[group][index]["path"] = self.cobra_path_entries[group][index]["ent"].get().strip()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)

    def _cobra_save_paths(self):
        p = self._cobra_profile(); data = p.setdefault("cobra_paths", self._cobra_default_paths())
        group = getattr(self, "cobra_active_path_group", None)
        if group and group in self.cobra_path_entries:
            entries = self.cobra_path_entries[group]
            data.setdefault(group, self._cobra_default_paths()[group])
            for i, item in enumerate(entries):
                data[group][i]["path"] = item["ent"].get().strip()
                data[group][i]["auto"] = bool(item["auto"].get())
        p["cobra_paths"] = data
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)

    def cobra_switch_path_group(self, group):
        if group not in self.cobra_path_frames: return
        self._cobra_save_paths()
        for frame in self.cobra_path_frames.values(): frame.pack_forget()
        data = self._cobra_profile().get("cobra_paths", self._cobra_default_paths()).get(group, [])
        for i, entry in enumerate(self.cobra_path_entries[group]):
            item = data[i] if i < len(data) else {}
            entry["ent"].delete(0, tk.END); entry["ent"].insert(0, item.get("path", ""))
            entry["auto"].set(item.get("auto", False)); entry["interval"].set(item.get("interval", "12h")); entry["last"].configure(text=f"Last: {item.get('last', '') or '—'}")
        self.cobra_path_frames[group].pack(fill="x")
        self.cobra_path_group_var.set(group)
        self.cobra_active_path_group = group
        for name, button in self.cobra_group_buttons.items():
            active = name == group
            button.configure(fg_color=COLOR_SUCCESS if active else COLOR_CARD, text_color=COLOR_BG if active else COLOR_TEXT, border_color=COLOR_SUCCESS if active else COLOR_BORDER)
        self.cobra_group_var.set(group)

    def cobra_check_for_new(self, group, index, silent=False):
        p = self._cobra_profile(); paths = p.get("cobra_paths", self._cobra_default_paths()); item = paths.get(group, [{}] * 10)[index] if index < len(paths.get(group, [])) else {}
        meta = item.get("history", {})
        if not meta.get("series_id") and not meta.get("movie_id"):
            if not silent: messagebox.showinfo("Cobra Check for New", "No previous Cobra download is recorded for this path yet.")
            return
        def work():
            try:
                if meta.get("series_id"):
                    remote = self._cobra_request({"mode": "episode", "series_id": meta["series_id"]})
                else:
                    remote = []
                existing_names = set()
                host = self.host_entry.get().strip(); port = int(self.port_entry.get() or 22); user = self.user_entry.get().strip(); pw = self.pass_entry.get()
                if host and item.get("path"):
                    ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()); ssh.connect(host, port=port, username=user, password=pw, timeout=10)
                    _, out, _ = ssh.exec_command(f"find {shlex.quote(item['path'])} -maxdepth 1 -type f -printf '%f\\n'"); existing_names = {os.path.splitext(x.strip())[0].lower() for x in out.read().splitlines() if x.strip()}; ssh.close()
                def number_value(value):
                    match = re.search(r"\d+", str(value or "")); return int(match.group(0)) if match else None
                def episode_key(value, season=None, order=None):
                    season_num, order_num = number_value(season), number_value(order)
                    if season_num is not None and order_num is not None: return (season_num, order_num)
                    match = re.search(r"s\s*0*(\d+).*?(?:e|ep)\s*0*(\d+)", str(value), re.I)
                    return (int(match.group(1)), int(match.group(2))) if match else None
                existing_keys = {episode_key(name) for name in existing_names if episode_key(name)}
                saved_keys = set()
                for saved in meta.get("episodes", []):
                    if isinstance(saved, (list, tuple)) and len(saved) == 2: saved_keys.add((int(saved[0]), int(saved[1])))
                saved_key = episode_key(meta.get("name", ""), meta.get("season"), meta.get("order"))
                if saved_key: saved_keys.add(saved_key)
                known_keys = existing_keys | saved_keys
                baseline = max(known_keys, default=None)
                all_items = []
                for x in remote:
                    season_value = x.get("season") or x.get("season_num")
                    order_value = x.get("order") or x.get("episode") or x.get("episode_num")
                    key = episode_key(x.get("name", ""), season_value, order_value)
                    normalized = {**x, "season": number_value(season_value), "order": number_value(order_value)}
                    normalized["_is_new"] = bool(key and baseline is not None and key > baseline)
                    all_items.append(normalized)
                all_items.sort(key=lambda x: episode_key(x.get("name", ""), x.get("season"), x.get("order")) or (999999, 999999))
                new_items = [x for x in all_items if x.get("_is_new")]
                if baseline is None:
                    local_debug_log(f"MAVIN CHECK BLOCKED: no valid episode keys; group={group}, path={index+1}, target={item.get('path','')}")
                local_debug_log(f"MAVIN CHECK: group={group}, path={index+1}, target={item.get('path','')}, server_keys={sorted(existing_keys)}, saved_keys={sorted(saved_keys)}, baseline={baseline}, new={len(new_items)}")
                rows = [{"name": x.get("name", ""), "url": x.get("url", ""), "type": "Series", "season": f"S{x.get('season', '')} E{x.get('order', '')}", "order": x.get("order", ""), "season_number": x.get("season", ""), "poster": meta.get("poster"), "series_id": meta.get("series_id"), "group": group, "path_label": f"{group} / Path {index+1}", "_is_new": x.get("_is_new", False)} for x in all_items]
                def finish_check():
                    self.cobra_fill_episode_results(rows)
                    new_ids = [iid for iid in self.cobra_results_tree.get_children() if self.cobra_result_items.get(iid, {}).get("_is_new")]
                    self.cobra_results_tree.selection_set(new_ids)
                    self.cobra_status_lbl.configure(text=f"Cobra Check: {len(rows)} total, {len(new_ids)} new selected in {group} Path {index+1}", text_color=COLOR_SUCCESS)
                self.after(0, finish_check)
            except Exception as exc: self.after(0, lambda: self.cobra_status_lbl.configure(text=f"Cobra Check error: {exc}", text_color=COLOR_DANGER))
        threading.Thread(target=work, daemon=True).start()

    def _cobra_profile(self):
        p = self.current_profile.get()
        self.config.setdefault(p, {})
        return self.config[p]

    @staticmethod
    def _cobra_xor(data):
        key = b"9yLxnB0XIO"
        return bytes(value ^ key[i % len(key)] for i, value in enumerate(data))

    def _cobra_encode(self, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        import base64
        encoded = base64.b64encode(self._cobra_xor(raw)).decode("ascii")
        return encoded.translate(str.maketrans({"+": "!", "/": ")", "=": "("}))

    def _cobra_decode(self, content):
        import base64
        for candidate in (content, content.decode("utf-8", errors="replace").strip().encode("utf-8")):
            try: return json.loads(self._cobra_xor(candidate).decode("utf-8"))
            except Exception: pass
            try:
                normalized = candidate.replace(b"!", b"+").replace(b")", b"/").replace(b"(", b"=")
                return json.loads(self._cobra_xor(base64.b64decode(normalized)).decode("utf-8"))
            except Exception: pass
            try: return json.loads(candidate.decode("utf-8"))
            except Exception: pass
        return {"status": "ERROR", "message": "Invalid server response"}

    def _cobra_request(self, payload):
        api = self.cobra_api_entry.get().strip() or "http://cobra66.net/cobraplus/V1/API-V1.php"
        data = {"mode": payload.pop("mode", ""), "code": self.cobra_code_entry.get().strip(), "mac": self.cobra_mac_entry.get().strip(), "sn": self.cobra_sn_entry.get().strip()}
        data.update(payload)
        response = requests.post(api, files={"json": (None, self._cobra_encode(data))}, headers={"User-Agent": "IPTVSmartersPlayer", "Accept-Encoding": "gzip, deflate"}, timeout=30)
        result = self._cobra_decode(response.content)
        local_debug_log(f"MAVIN API\nURL: {api}\nREQUEST: {json.dumps(data, ensure_ascii=False)}\nHTTP: {response.status_code}\nRESPONSE: {json.dumps(result, ensure_ascii=False)}")
        return result

    def cobra_activate(self):
        def work():
            try:
                p = self._cobra_profile(); p.update({"cobra_api": self.cobra_api_entry.get().strip() or "http://cobra66.net/cobraplus/V1/API-V1.php", "cobra_code": self.cobra_code_entry.get().strip(), "cobra_sn": self.cobra_sn_entry.get().strip(), "cobra_mac": self.cobra_mac_entry.get().strip()})
                result = self._cobra_request({"mode": "active"})
                status = str(result.get("status", "")).upper()
                ok = status in ("ACTIVE", "100")
                if ok:
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)
                    msg = f"ACTIVE • {result.get('expire', '')}"
                    self.after(0, lambda: self.cobra_status_lbl.configure(text=msg, text_color=COLOR_SUCCESS))
                    self.after(0, self.cobra_load_categories)
                else:
                    self.after(0, lambda: self.cobra_status_lbl.configure(text=f"Activation failed: {result.get('message', status)}", text_color=COLOR_DANGER))
            except Exception as exc:
                self.after(0, lambda: self.cobra_status_lbl.configure(text=f"Error: {exc}", text_color=COLOR_DANGER))
        threading.Thread(target=work, daemon=True).start()

    def cobra_load_categories(self):
        if not self.cobra_code_entry.get().strip(): return
        kind = "series" if self.cobra_type_var.get() == "Series" else "movie"
        def work():
            try:
                result = self._cobra_request({"mode": "category", "cat_type": kind})
                categories = result if isinstance(result, list) else []
                self.cobra_categories = categories
                names = ["All Categories"] + [str(x.get("name", "")) for x in categories if x.get("name")]
                self.after(0, lambda: self.cobra_cat_opt.configure(values=names))
                self.after(0, lambda: self.cobra_cat_var.set(names[0] if names else "All Categories"))
            except Exception as exc: self.after(0, lambda: self.cobra_status_lbl.configure(text=f"Category error: {exc}", text_color=COLOR_DANGER))
        threading.Thread(target=work, daemon=True).start()

    def cobra_search(self):
        keyword = self.cobra_search_entry.get().strip().lower(); kind = "series" if self.cobra_type_var.get() == "Series" else "movie"
        cat_id = None
        for item in getattr(self, "cobra_categories", []):
            if item.get("name") == self.cobra_cat_var.get(): cat_id = item.get("id"); break
        def work():
            try:
                result = self._cobra_request({"mode": kind, "cat_id": cat_id or "", "from": 0, "lenght": 100})
                items = result if isinstance(result, list) else []
                if keyword: items = [x for x in items if keyword in str(x.get("name", "")).lower()]
                self.after(0, lambda: self.cobra_fill_results(items, kind))
            except Exception as exc: self.after(0, lambda: self.cobra_status_lbl.configure(text=f"Search error: {exc}", text_color=COLOR_DANGER))
        threading.Thread(target=work, daemon=True).start()

    def cobra_fill_results(self, items, kind):
        for iid in self.cobra_results_tree.get_children(): self.cobra_results_tree.delete(iid)
        self.cobra_result_items = {}
        for index, item in enumerate(items):
            iid = self.cobra_results_tree.insert("", "end", values=(item.get("name", ""), "Series" if kind == "series" else "Movie", item.get("year", ""), item.get("path_label", "—"), item.get("url", "")))
            self.cobra_result_items[iid] = {**item, "cobra_kind": kind}

    def cobra_open_selected(self, event=None):
        selected = self.cobra_results_tree.selection()
        if not selected: return
        item = self.cobra_result_items.get(selected[0], {})
        if item.get("cobra_kind") == "series" and item.get("id"):
            def work():
                try:
                    episodes = self._cobra_request({"mode": "episode", "series_id": item.get("id")})
                    rows = []
                    for ep in episodes if isinstance(episodes, list) else []:
                        rows.append({"name": ep.get("name", ""), "url": ep.get("url", ""), "type": "Series", "season": f"S{ep.get('season', '')} E{ep.get('order', '')}", "season_number": ep.get("season", ""), "order": ep.get("order", ""), "poster": item.get("logo"), "group": self.cobra_group_var.get(), "series_id": item.get("id"), "path_label": f"{self.cobra_group_var.get()} / {self.cobra_path_var.get()}"})
                    self.after(0, lambda: self.cobra_fill_episode_results(rows))
                except Exception as exc: self.after(0, lambda: self.cobra_status_lbl.configure(text=f"Episode error: {exc}", text_color=COLOR_DANGER))
            threading.Thread(target=work, daemon=True).start()
        elif item.get("cobra_kind") == "movie" and item.get("id"):
            def work_movie():
                try:
                    info = self._cobra_request({"mode": "movie", "id": item.get("id")})
                    if isinstance(info, dict): info.update({"cobra_kind": "movie", "type": "Movie"})
                    self.after(0, lambda: self.cobra_fill_results([info], "movie"))
                except Exception as exc: self.after(0, lambda: self.cobra_status_lbl.configure(text=f"Movie error: {exc}", text_color=COLOR_DANGER))
            threading.Thread(target=work_movie, daemon=True).start()

    def cobra_fill_episode_results(self, items):
        for iid in self.cobra_results_tree.get_children(): self.cobra_results_tree.delete(iid)
        self.cobra_result_items = {}
        for item in items:
            iid = self.cobra_results_tree.insert("", "end", values=(item.get("name", ""), item.get("type", "Series"), item.get("season", ""), item.get("path_label", f"{item.get('group', self.cobra_group_var.get())} / {self.cobra_path_var.get()}"), item.get("url", "")))
            self.cobra_result_items[iid] = item

    def cobra_show_selected_poster(self, event=None):
        selected = self.cobra_results_tree.selection()
        if not selected: return
        item = self.cobra_result_items.get(selected[0], {}); url = item.get("poster") or item.get("logo")
        if not url:
            self.cobra_poster_label.configure(image="", text="No poster")
            return
        def load():
            try:
                response = requests.get(url, timeout=8); response.raise_for_status(); image = Image.open(io.BytesIO(response.content)); image.thumbnail((140, 190)); photo = ImageTk.PhotoImage(image)
                self.after(0, lambda: (setattr(self, "cobra_poster_image", photo), self.cobra_poster_label.configure(image=photo, text="")))
            except Exception:
                self.after(0, lambda: self.cobra_poster_label.configure(image="", text="Poster unavailable"))
        threading.Thread(target=load, daemon=True).start()

    def cobra_select_results(self, select=True):
        ids = self.cobra_results_tree.get_children()
        self.cobra_results_tree.selection_set(ids if select else ())

    def cobra_clear_results(self):
        for iid in self.cobra_results_tree.get_children(): self.cobra_results_tree.delete(iid)
        self.cobra_result_items = {}
        self.cobra_poster_image = None
        self.cobra_poster_label.configure(image="", text="")
        self.cobra_status_lbl.configure(text="Cobra results cleared", text_color=COLOR_TEXT_DIM)

    def cobra_refresh_paths(self, group=None):
        self.cobra_path_opt.configure(values=[f"Path {i+1}" for i in range(10)])

    def cobra_download_selected(self):
        ids = self.cobra_results_tree.selection()
        if not ids: messagebox.showwarning("Cobra", "No content selected."); return
        p = self._cobra_profile(); self._cobra_save_paths(); group = self.cobra_group_var.get(); path_idx = int(self.cobra_path_var.get().split()[-1]) - 1
        paths = p.get("cobra_paths", self._cobra_default_paths()).get(group, [])
        target = (paths[path_idx].get("path") if len(paths) > path_idx else "")
        if not target: messagebox.showwarning("Cobra", "The selected target path is empty."); return
        selected = []
        for iid in ids:
            item = self.cobra_result_items.get(iid, {}); url = item.get("url", "")
            if url: selected.append({"name": item.get("name", "Untitled"), "url": url, "path": target, "group": group, "poster": item.get("poster") or item.get("logo"), "id": item.get("id", ""), "series_id": item.get("series_id", ""), "season": item.get("season_number", ""), "order": item.get("order", ""), "section_transcode": self.cobra_auto_transcode_v.get()})
        if selected:
            path_data = p.setdefault("cobra_paths", self._cobra_default_paths())
            slot = path_data.setdefault(group, self._cobra_default_paths()[group])[path_idx]
            def episode_pair(item):
                try:
                    if item.get("season") not in (None, "") and item.get("order") not in (None, ""): return (int(item.get("season")), int(item.get("order")))
                except (TypeError, ValueError): pass
                m = re.search(r"s\s*0*(\d+).*?(?:e|ep)\s*0*(\d+)", str(item.get("name", "")), re.I)
                return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)
            first = selected[0]; last_item = max(selected, key=episode_pair); last_pair = episode_pair(last_item)
            slot["last"] = last_item["name"]
            slot["history"] = {"series_id": first.get("series_id", ""), "movie_id": first.get("id", ""), "name": last_item.get("name", ""), "season": last_pair[0] if last_pair[0] >= 0 else "", "order": last_pair[1] if last_pair[1] >= 0 else "", "poster": first.get("poster", ""), "episodes": [[pair[0], pair[1]] for pair in (episode_pair(x) for x in selected) if pair[0] >= 0], "updated": time.time()}
            with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)
            self.cobra_path_entries[group][path_idx]["last"].configure(text=f"Last: {last_item['name']}")
            self.enqueue_download_job(self.cobra_execute_queued_download, selected, count=len(selected))

    def cobra_execute_queued_download(self, selected_items):
        self.execute_m3u_download_sequential(selected_items)
        if not selected_items: return
        group = selected_items[0].get("group", "Series 1"); path = selected_items[0].get("path", "")
        p = self._cobra_profile(); data = p.setdefault("cobra_paths", self._cobra_default_paths())
        for index, slot in enumerate(data.get(group, [])):
            if slot.get("path") == path:
                item = selected_items[-1]
                slot["last"] = item.get("name", slot.get("last", ""))
                history = slot.setdefault("history", {}); history.update({"name": slot["last"], "updated": time.time()})
                completed = history.setdefault("completed_episodes", [])
                for queued in selected_items:
                    season, order = queued.get("season"), queued.get("order")
                    if season not in (None, "") and order not in (None, ""):
                        pair = [int(season), int(order)]
                        if pair not in completed: completed.append(pair)
                history["episodes"] = completed
                if group in self.cobra_path_entries and index < len(self.cobra_path_entries[group]):
                    self.after(0, lambda i=index, g=group, n=slot["last"]: self.cobra_path_entries[g][i]["last"].configure(text=f"Last: {n}"))
                break
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=4)

    def change_language(self, new_lang):
        self.lang = new_lang; t = TRANSLATIONS[self.lang]; self.title(t["title"])
        self.logo_label.configure(text=t["sidebar_title"]); self.prof_label.configure(text=t["current_profile"])
        self.save_btn.configure(text=t["save_settings"])
        self.add_prof_btn.configure(text=t["add_server"]); self.del_prof_btn.configure(text=t["del_server"])
        
        # Update Nav Buttons
        nav_keys = ["tab_downloader", "tab_transcoder", "tab_m3u", "tab_mavin", "tab_cobra", "tab_firefox", "tab_browser"]
        icons = ["📥", "🎬", "📺", "🔐", "🐍", "🦊", "🌐"]
        for i, (key, icon) in enumerate(zip(nav_keys, icons)):
            self.nav_btns[i].configure(text=f"  {icon}  {t[key]}")

        self.trans_title_lbl.configure(text=t["trans_settings_title"]); self.logo_tr_lbl.configure(text=t["logo_tr"]); self.logo_tl_lbl.configure(text=t["logo_tl"]); self.logo_br_lbl.configure(text=t["logo_br"]); self.logo_bl_lbl.configure(text=t["logo_bl"]); self.intro_lbl.configure(text=t["intro_path"]); self.intro_chk.configure(text=t["intro_toggle"]); self.trim_chk.configure(text=t["trim_toggle"]); self.trim_seconds_lbl.configure(text=t["trim_seconds"]); self.auto_transcode_downloads_chk.configure(text=t["auto_transcode_downloads"]); self.m3u_auto_transcode_chk.configure(text=t["m3u_auto_transcode"]); self.mavin_auto_transcode_chk.configure(text=t["mavin_auto_transcode"]); self.cobra_auto_transcode_chk.configure(text=t["cobra_auto_transcode"]); self.res_lbl.configure(text=t["resolution"]); self.bit_lbl.configure(text=t["bitrate"]); self.preset_lbl.configure(text=t["preset"]); self.codec_lbl.configure(text=t["codec_label"]); self.bitdepth_lbl.configure(text=t["bit_depth_label"])
        self.xtream_title_lbl.configure(text=t["xtream_title"])
        for i, xr in enumerate(self.xtream_rows):
            xr['lbl_h_main'].configure(text=f"Server {i+1}")
            xr['lbl_h'].configure(text=t['xtream_host'])
            xr['lbl_u'].configure(text=t["xtream_user"])
            xr['lbl_p'].configure(text=t["xtream_pass"])
            xr['scan_b'].configure(text=f"🔍 {t['btn_m3u_search']}")
            xr['search'].configure(placeholder_text=f"Search Server {i+1}...")
            
        self.bulk_title_lbl.configure(text=t["bulk_title"]); self.bulk_path_lbl.configure(text=t["bulk_path_label"]); self.scan_btn.configure(text=f"🔍 {t['btn_scan_bulk']}")
        self.select_all_btn.configure(text=t["btn_select_all"]); self.select_none_btn.configure(text=t["btn_select_none"]); self.bulk_btn.configure(text=f"🎬 {t['btn_start_bulk']}"); self.view_log_btn.configure(text=f"📜 {t['btn_view_log']}")
        
        self.links_title_lbl.configure(text=t["links_title"]); self.h_proc.configure(text=t["col_process"]); self.h_m3u8.configure(text=t["col_m3u8"]); self.h_url.configure(text=t["col_url"]); self.h_path.configure(text=t["col_path"]); self.h_name.configure(text=t["col_name"])
        self.r_seq.configure(text=t["mode_seq"]); self.r_par.configure(text=t["mode_par"]); self.exec_btn.configure(text=f"🚀 {t['btn_start']}")
        self.load_file_btn.configure(text=f"📂 {t['btn_load_file']}"); self.mon_btn.configure(text=f"📊 {t['btn_monitor']}"); self.auto_refresh_chk.configure(text=t["auto_refresh"]); self.clear_btn.configure(text=f"🗑️ {t['btn_clear']}"); self.mon_title_lbl.configure(text=t["monitor_title"])
        self.tree.heading("file", text=t["mon_col_file"]); self.tree.heading("status", text=t["mon_col_status"]); self.tree.heading("info", text=t["mon_col_info"])
        self.firefox_title_lbl.configure(text=t["firefox_title"]); self.firefox_auto_chk.configure(text=t["firefox_auto"]); self.firefox_start_btn.configure(text="🦊 " + t["firefox_start"]); self.firefox_stop_btn.configure(text=t["firefox_stop"]); self.firefox_save_btn.configure(text=t["save_settings"]); self.firefox_status_lbl.configure(text=t["firefox_status"] + (" stopped" if not self.firefox_server else f" 127.0.0.1:{self.firefox_port}"))
        self.browser_title_lbl.configure(text=t["browser_title"]); self.browser_start_btn.configure(text="🌐 " + t["browser_start"]); self.browser_stop_btn.configure(text=t["browser_stop"]); self.browser_status_lbl.configure(text=t["browser_status"] + (" stopped" if not self.browser_proxy else " running"))
        for r in self.rows: r['url'].configure(placeholder_text=t["placeholder_url"]); r['path'].configure(placeholder_text=t["placeholder_path"]); r['name'].configure(placeholder_text=t["placeholder_name"])
        self.m3u_title_lbl.configure(text=t["m3u_title"])
        for i, m in enumerate(self.m3u_rows):
            m['lbl_m_main'].configure(text=f"M3U {i+1}")
            m['lbl1'].configure(text=t["m3u_url_label"])
            m['scan_btn'].configure(text=f"🔍 {t['btn_m3u_search']}")
            m['search'].configure(placeholder_text=f"Search M3U {i+1}...")
        self.m3u_res_title.configure(text=t["m3u_results_title"]); self.m3u_path_lbl.configure(text=t["m3u_path_label"]); self.m3u_sel_all_btn.configure(text=t["btn_m3u_select_all"]); self.m3u_sel_none_btn.configure(text=t["btn_m3u_select_none"]); self.m3u_down_btn.configure(text=f"🚀 {t['btn_m3u_download']}"); self.m3u_clear_res_btn.configure(text=f"🗑️ {t['btn_m3u_clear']}")
        for i, pe in enumerate(self.m3u_path_entries): 
            pe['lbl'].configure(text=f"PATH {i+1}")
            pe['ent'].configure(placeholder_text=f"Path {i+1}...")
            # We use icons in the compact matrix, so we only update tooltips if we had them, 
            # but for now we keep the layout consistent.
        
        # Update current view header
        current_view = self.header_title.cget("text")
        if "Downloader" in current_view or "التحميل" in current_view: self.header_title.configure(text=t["tab_downloader"])
        elif "Transcode" in current_view or "المعالجة" in current_view: self.header_title.configure(text=t["tab_transcoder"])
        elif "M3U" in current_view or "مستخرج" in current_view: self.header_title.configure(text=t["tab_m3u"])
        
        self.save_current_settings(silent=True)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # Normalize old 20-path configurations. Every group receives its
                # own newly-created list and its own item dictionaries.
                d_paths = [{"path": f"/home/downloads/folder{i+1}/", "auto": False, "interval": "12h"} for i in range(10)]
                for profile_name, profile in data.items():
                    if profile_name == "global" or not isinstance(profile, dict):
                        continue
                    legacy_series = profile.get("m3u_paths_series")
                    legacy_vod = profile.get("m3u_paths_vod")
                    old_schema = profile.get("m3u_path_schema", 0)
                    for kind, legacy in (("series", legacy_series), ("vod", legacy_vod)):
                        first_key = f"m3u_paths_{kind}_1"
                        if first_key not in profile:
                            profile[first_key] = [dict(x) for x in (legacy or d_paths)]
                        for n in (2, 3):
                            key = f"m3u_paths_{kind}_{n}"
                            # These keys were produced by the faulty shared-path
                            # implementation when no schema marker existed.
                            # Reset them once, then keep them permanently separate.
                            if old_schema < 2 or key not in profile:
                                profile[key] = [{"path": f"/home/downloads/{kind}_{n}_{i+1}/", "auto": False, "interval": "12h"} for i in range(10)]
                    # Keep old keys as aliases only for backward compatibility;
                    # the live UI never uses them for groups 2 or 3.
                    profile["m3u_path_schema"] = 2
                return data
            except Exception:
                pass
        d_paths = [{"path": f"/home/downloads/folder{i+1}/", "auto": False, "interval": "12h"} for i in range(10)]
        groups = {f"{kind}_{n}": [dict(x) for x in d_paths] for kind in ("series", "vod") for n in range(1, 4)}
        return {"Default": {"host": "", "port": "22", "user": "root", "pass": "", "logo_tr": "", "logo_tl": "", "logo_br": "", "logo_bl": "", "logo_tr_v": False, "logo_tl_v": False, "logo_br_v": False, "logo_bl_v": False, "intro": "", "intro_v": False, "trim_v": False, "trim_seconds": "0", "auto_transcode_downloads": False, "m3u_auto_transcode": False, "mavin_auto_transcode": False, "cobra_auto_transcode": False, "res": "720p", "bitrate": "820", "preset": "medium", "paths": ["/home/downloads/"] * 10, "m3u": [{"url": "", "search": "", "category": "All Categories"} for _ in range(3)], "bulk_path": "", **{f"m3u_paths_{k}": v for k, v in groups.items()}, "m3u_paths_series": groups["series_1"], "m3u_paths_vod": groups["vod_1"], "m3u_history": {}}, "global": {"language": "Arabic"}}

    def _path_group_key(self, group=None):
        """Return stable storage key: series_1, series_2, series_3, vod_1, ..."""
        raw = (group or self.m3u_path_group.get()).strip().lower().replace(" ", "_")
        if raw in ("series", "vod"):
            raw += "_1"
        return raw

    def _path_history_key(self, idx, group=None):
        return f"{self._path_group_key(group)}_path_{idx+1}"

    def _default_group_paths(self, group=None):
        key = self._path_group_key(group)
        return [{"path": f"/home/downloads/{key}_{i+1}/", "auto": False, "interval": "12h"} for i in range(10)]

    def save_current_settings(self, silent=True):
        p = self.current_profile.get()
        if p not in self.config: self.config[p] = {}
        history = self.config.get(p, {}).get("m3u_history", {})
        m3u_data = [{"url": m['url'].get(), "search": m['search'].get(), "category": m['cat_var'].get()} for m in self.m3u_rows]
        
        group_key = self._path_group_key(getattr(self, "_active_path_group", self.m3u_path_group.get()))
        current_paths = [{"path": pe['ent'].get(), "auto": pe['auto_v'].get(), "interval": pe['int_var'].get()} for pe in self.m3u_path_entries]
        self.config[p][f"m3u_paths_{group_key}"] = current_paths
        # Keep legacy keys synchronized for Series 1/VOD 1 compatibility.
        if group_key == "series_1": self.config[p]["m3u_paths_series"] = current_paths
        if group_key == "vod_1": self.config[p]["m3u_paths_vod"] = current_paths

        self.config[p].update({
            "m3u_path_schema": 2,
            "host": self.host_entry.get(), "port": self.port_entry.get(), "user": self.user_entry.get(), "pass": self.pass_entry.get(),
            "logo_tr": self.logo_tr_entry.get(), "logo_tl": self.logo_tl_entry.get(), "logo_br": self.logo_br_entry.get(), "logo_bl": self.logo_bl_entry.get(),
            "logo_tr_v": self.logo_tr_v.get(), "logo_tl_v": self.logo_tl_v.get(), "logo_br_v": self.logo_br_v.get(), "logo_bl_v": self.logo_bl_v.get(),
            "intro": self.intro_path_entry.get(), "intro_v": self.intro_v.get(), "trim_v": self.trim_v.get(), "trim_seconds": self.trim_seconds_entry.get(), "auto_transcode_downloads": self.auto_transcode_downloads_v.get(), "m3u_auto_transcode": self.m3u_auto_transcode_v.get(), "mavin_auto_transcode": self.mavin_auto_transcode_v.get(), "cobra_auto_transcode": self.cobra_auto_transcode_v.get(), "res": self.res_var.get(), "bitrate": self.bitrate_entry.get(), "preset": self.preset_var.get(),
            "codec": self.codec_var.get(), "bitdepth": self.bitdepth_var.get(),
            "mavin_api": self.mavin_api_entry.get(), "mavin_code": self.mavin_code_entry.get(),
            "mavin_sn": self.mavin_sn_entry.get(), "mavin_mac": self.mavin_mac_entry.get(),
            "xtream_list": [{"host": r['host'].get(), "user": r['user'].get(), "pass": r['pass'].get(), "vod_v": r['vod_v'].get(), "ser_v": r['ser_v'].get(), "search": r['search'].get()} for r in self.xtream_rows],
            "paths": [row['path'].get() for row in self.rows], "m3u": m3u_data, "bulk_path": self.bulk_path_entry.get(), "m3u_history": history, "firefox_path": self.firefox_path_entry.get(), "firefox_auto": self.firefox_auto_v.get(), "firefox_port": self.firefox_port_entry.get(), "browser_profile": self.browser_profile_entry.get()
        })
        self.config["global"] = {"language": self.lang}
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(self.config, f, indent=4)
        if not silent: messagebox.showinfo("Success", TRANSLATIONS[self.lang]["msg_success"])

    def add_new_profile(self):
        name = simpledialog.askstring("Server", "Enter server name:")
        if name and name not in self.config:
            d_paths = [{"path": f"/home/downloads/folder{i+1}/", "auto": False, "interval": "12h"} for i in range(10)]
            self.config[name] = {
                "host": "", "port": "22", "user": "root", "pass": "", 
                "logo_tr": "", "logo_tl": "", "logo_br": "", "logo_bl": "", 
                "logo_tr_v": False, "logo_tl_v": False, "logo_br_v": False, "logo_bl_v": False, 
                "intro": "", "intro_v": False, "trim_v": False, "trim_seconds": "0", "auto_transcode_downloads": False, "m3u_auto_transcode": False, "mavin_auto_transcode": False, "cobra_auto_transcode": False, "res": "720p", "bitrate": "820", "preset": "medium", 
                "paths": ["/home/downloads/"] * 10, 
                "m3u": [{"url": "", "search": "", "category": "All Categories"} for _ in range(3)], 
                "xtream_list": [{"host": "", "user": "", "pass": "", "vod_v": True, "ser_v": True, "search": ""} for _ in range(3)], 
                "bulk_path": "", 
                **{f"m3u_paths_{kind}_{n}": [dict(x) for x in d_paths] for kind in ("series", "vod") for n in range(1, 4)},
                "m3u_paths_series": [dict(x) for x in d_paths], "m3u_paths_vod": [dict(x) for x in d_paths],
                "m3u_history": {}
            }
            self.profile_combo.configure(values=list(self.config.keys())); self.profile_combo.set(name); self.load_profile_to_ui(name)

    def delete_profile(self):
        name = self.current_profile.get()
        if name != "Default" and messagebox.askyesno("Delete", f"Delete {name}?"): del self.config[name]; self.profile_combo.configure(values=list(self.config.keys())); self.profile_combo.set("Default"); self.load_profile_to_ui("Default")

    def on_profile_change(self, name): self.current_profile.set(name); self.load_profile_to_ui(name)

    def switch_m3u_path_mode(self, mode_label):
        mode = "Series" if mode_label == TRANSLATIONS[self.lang]["path_mode_series"] else "VOD"
        current_group = self.m3u_path_group.get()
        suffix = current_group.split(" ")[-1] if current_group.lower().startswith(mode.lower()) else "1"
        self.switch_m3u_path_group(f"{mode} {suffix}")

    def switch_m3u_path_group(self, group_label):
        p = self.current_profile.get()
        if p not in self.config: return
        # Save the values currently visible under the group they actually belong to.
        # The segmented button variable may already contain the destination group here.
        previous_group = getattr(self, "_active_path_group", "Series 1")
        self.m3u_path_group.set(previous_group)
        self.save_current_settings(silent=True)
        if group_label not in ["Series 1", "Series 2", "Series 3", "VOD 1", "VOD 2", "VOD 3"]:
            group_label = "Series 1" if group_label.lower().startswith("series") else "VOD 1"
        self.m3u_path_group.set(group_label)
        self.m3u_path_mode.set("Series" if group_label.lower().startswith("series") else "VOD")
        if hasattr(self, "m3u_mode_switch"):
            self.m3u_mode_switch.set(TRANSLATIONS[self.lang]["path_mode_series"] if self.m3u_path_mode.get() == "Series" else TRANSLATIONS[self.lang]["path_mode_vod"])
        group_key = self._path_group_key(group_label)
        new_paths = self.config[p].get(f"m3u_paths_{group_key}")
        if new_paths is None and group_key == "series_1": new_paths = self.config[p].get("m3u_paths_series")
        if new_paths is None and group_key == "vod_1": new_paths = self.config[p].get("m3u_paths_vod")
        if new_paths is None: new_paths = self._default_group_paths(group_label)
        for i in range(10):
            d = new_paths[i] if i < len(new_paths) else self._default_group_paths(group_label)[i]
            self.m3u_path_entries[i]['ent'].delete(0, tk.END); self.m3u_path_entries[i]['ent'].insert(0, d.get("path", ""))
            self.m3u_path_entries[i]['auto_v'].set(d.get("auto", False)); self.m3u_path_entries[i]['int_var'].set(d.get("interval", "12h"))
        # Mark the newly loaded widgets as belonging to the destination group.
        self._active_path_group = group_label
        self.m3u_path_group.set(group_label)
        self.update_active_path_highlights()

    def load_profile_to_ui(self, name):
        d = self.config.get(name, {}); self.host_entry.delete(0, tk.END); self.host_entry.insert(0, d.get("host", "")); self.port_entry.delete(0, tk.END); self.port_entry.insert(0, d.get("port", "22")); self.user_entry.delete(0, tk.END); self.user_entry.insert(0, d.get("user", "root")); self.pass_entry.delete(0, tk.END);         self.pass_entry.insert(0, d.get("pass", "")); self.logo_tr_entry.delete(0, tk.END); self.logo_tr_entry.insert(0, d.get("logo_tr", "")); self.logo_tl_entry.delete(0, tk.END); self.logo_tl_entry.insert(0, d.get("logo_tl", "")); self.logo_br_entry.delete(0, tk.END); self.logo_br_entry.insert(0, d.get("logo_br", "")); self.logo_bl_entry.delete(0, tk.END); self.logo_bl_entry.insert(0, d.get("logo_bl", "")); self.logo_tr_v.set(d.get("logo_tr_v", False)); self.logo_tl_v.set(d.get("logo_tl_v", False)); self.logo_br_v.set(d.get("logo_br_v", False)); self.logo_bl_v.set(d.get("logo_bl_v", False)); self.intro_path_entry.delete(0, tk.END); self.intro_path_entry.insert(0, d.get("intro", "")); self.intro_v.set(d.get("intro_v", bool(d.get("intro", "")))); self.trim_v.set(d.get("trim_v", False)); self.trim_seconds_entry.delete(0, tk.END); self.trim_seconds_entry.insert(0, str(d.get("trim_seconds", "0"))); self.auto_transcode_downloads_v.set(d.get("auto_transcode_downloads", False)); self.m3u_auto_transcode_v.set(d.get("m3u_auto_transcode", False)); self.mavin_auto_transcode_v.set(d.get("mavin_auto_transcode", False)); self.cobra_auto_transcode_v.set(d.get("cobra_auto_transcode", False)); self.res_var.set(d.get("res", "720p")); self.bitrate_entry.delete(0, tk.END); self.bitrate_entry.insert(0, d.get("bitrate", "820")); self.preset_var.set(d.get("preset", "medium"));         self.codec_var.set(d.get("codec", "libx265 (HEVC)")); self.bitdepth_var.set(d.get("bitdepth", "10-bit")); self.bulk_path_entry.delete(0, tk.END); self.bulk_path_entry.insert(0, d.get("bulk_path", "")); self.firefox_path_entry.delete(0, tk.END); self.firefox_path_entry.insert(0, d.get("firefox_path") or "/home/downloads/firefox/"); self.firefox_auto_v.set(d.get("firefox_auto", True)); self.firefox_port_entry.delete(0, tk.END); self.firefox_port_entry.insert(0, str(d.get("firefox_port", "8765"))); self.browser_profile_entry.delete(0, tk.END); self.browser_profile_entry.insert(0, d.get("browser_profile") or os.path.join(os.getcwd(), "firefox_sftp_profile"))
        if hasattr(self, "mavin_api_entry"):
            self.mavin_api_entry.delete(0, tk.END); self.mavin_api_entry.insert(0, d.get("mavin_api", "http://maveniptv.xyz/mvpro/maven.php"))
            self.mavin_code_entry.delete(0, tk.END); self.mavin_code_entry.insert(0, d.get("mavin_code", ""))
            self.mavin_sn_entry.delete(0, tk.END); self.mavin_sn_entry.insert(0, d.get("mavin_sn", ""))
            self.mavin_mac_entry.delete(0, tk.END); self.mavin_mac_entry.insert(0, d.get("mavin_mac", ""))
            if d.get("mavin_paths"):
                for group, entries in self.mavin_path_entries.items():
                    saved = d["mavin_paths"].get(group, [])
                    for i, entry in enumerate(entries):
                        item = saved[i] if i < len(saved) else {}
                        entry["ent"].delete(0, tk.END); entry["ent"].insert(0, item.get("path", ""))
                        entry["auto"].set(item.get("auto", False)); entry["interval"].set(item.get("interval", "12h")); entry["last"].configure(text=f"Last: {item.get('last', '') or '—'}")

        if hasattr(self, "cobra_api_entry"):
            self.cobra_api_entry.delete(0, tk.END); self.cobra_api_entry.insert(0, d.get("cobra_api", "http://cobra66.net/cobraplus/V1/API-V1.php"))
            self.cobra_code_entry.delete(0, tk.END); self.cobra_code_entry.insert(0, d.get("cobra_code", ""))
            self.cobra_sn_entry.delete(0, tk.END); self.cobra_sn_entry.insert(0, d.get("cobra_sn", ""))
            self.cobra_mac_entry.delete(0, tk.END); self.cobra_mac_entry.insert(0, d.get("cobra_mac", ""))
            if d.get("cobra_paths"):
                for group, entries in self.cobra_path_entries.items():
                    saved = d["cobra_paths"].get(group, [])
                    for i, entry in enumerate(entries):
                        item = saved[i] if i < len(saved) else {}
                        entry["ent"].delete(0, tk.END); entry["ent"].insert(0, item.get("path", ""))
                        entry["auto"].set(item.get("auto", False)); entry["interval"].set(item.get("interval", "12h")); entry["last"].configure(text=f"Last: {item.get('last', '') or '—'}")

        xt_list = d.get("xtream_list", [])
        for i in range(min(len(xt_list), 3)):
            xt = xt_list[i]; r = self.xtream_rows[i]
            r['host'].delete(0, tk.END); r['host'].insert(0, xt.get("host", ""))
            r['user'].delete(0, tk.END); r['user'].insert(0, xt.get("user", ""))
            r['pass'].delete(0, tk.END); r['pass'].insert(0, xt.get("pass", ""))
            r['vod_v'].set(xt.get("vod_v", True)); r['ser_v'].set(xt.get("ser_v", True))
            r['search'].delete(0, tk.END); r['search'].insert(0, xt.get("search", ""))
        
        group = getattr(self, "_active_path_group", self.m3u_path_group.get())
        self.m3u_path_group.set(group)
        group_key = self._path_group_key(group)
        m3u_paths = d.get(f"m3u_paths_{group_key}")
        if m3u_paths is None and group_key == "series_1": m3u_paths = d.get("m3u_paths_series")
        if m3u_paths is None and group_key == "vod_1": m3u_paths = d.get("m3u_paths_vod")
        if m3u_paths is None: m3u_paths = self._default_group_paths(group)
        for i in range(10): 
            d_path = m3u_paths[i] if i < len(m3u_paths) else self._default_group_paths(group)[i]
            self.m3u_path_entries[i]['ent'].delete(0, tk.END); self.m3u_path_entries[i]['ent'].insert(0, d_path.get("path", ""))
            self.m3u_path_entries[i]['auto_v'].set(d_path.get("auto", False))
            self.m3u_path_entries[i]['int_var'].set(d_path.get("interval", "12h"))
            
        ps = d.get("paths", ["/home/downloads/"] * 10)
        for i in range(10): self.rows[i]['path'].delete(0, tk.END); self.rows[i]['path'].insert(0, ps[i] if i < len(ps) else "/home/downloads/")
        m3u = d.get("m3u", [{"url": "", "search": "", "category": "All Categories"} for _ in range(3)])
        for i in range(min(len(m3u), 3)): 
            self.m3u_rows[i]['url'].delete(0, tk.END); self.m3u_rows[i]['url'].insert(0, m3u[i].get("url", "")); self.m3u_rows[i]['search'].delete(0, tk.END); self.m3u_rows[i]['search'].insert(0, m3u[i].get("search", "")); self.m3u_rows[i]['cat_var'].set(m3u[i].get("category", "All Categories"))
            if m3u[i].get("url"): self.fetch_categories_async(self.m3u_rows[i], force=False)

    def clear_urls(self):
        for r in self.rows: r['url'].delete(0, tk.END); r['name'].delete(0, tk.END); r['process'].set(False); r['is_m3u8'].set(False)

    def load_links_from_file(self):
        path = filedialog.askopenfilename(filetypes=[("Text Files", "*.txt")])
        if path:
            with open(path, 'r', encoding='utf-8') as f: links = [line.strip() for line in f if line.strip()]
            for i, link in enumerate(links[:10]): self.rows[i]['url'].delete(0, tk.END); self.rows[i]['url'].insert(0, link); 
            if "m3u8" in link.lower(): self.rows[i]['is_m3u8'].set(True)
            messagebox.showinfo("Loaded", f"Loaded {len(links)} links from file.")

    def toggle_auto_refresh(self):
        if self.auto_refresh_var.get(): self.auto_refresh_active = True; self.auto_update_monitor()
        else: self.auto_refresh_active = False

    def auto_update_monitor(self):
        if self.auto_refresh_active: self.monitor_downloads(); self.after(5000, self.auto_update_monitor)

    def scan_bulk_directory(self): threading.Thread(target=self.do_scan_bulk, daemon=True).start()

    def do_scan_bulk(self):
        host, port, user, pw = self.host_entry.get(), int(self.port_entry.get() or 22), self.user_entry.get(), self.pass_entry.get(); dir_path = self.bulk_path_entry.get().strip()
        if not dir_path: return
        try:
            ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()); ssh.connect(host, port=port, username=user, password=pw, timeout=10)
            stdin, stdout, stderr = ssh.exec_command(f"find {shlex.quote(dir_path)} -maxdepth 1 -type f \\( -iname \"*.mp4\" -o -iname \"*.mkv\" -o -iname \"*.avi\" \\) -printf \"%f\\n\""); files = sorted([f.strip() for f in stdout.readlines() if f.strip()]); ssh.close(); self.after(0, lambda: self.update_bulk_list(files))
        except Exception as e: self.after(0, lambda e=e: messagebox.showerror("Error", str(e)))

    def update_bulk_list(self, files):
        for f_item in self.bulk_files: f_item['chk'].destroy()
        self.bulk_files = []
        for f in files: var = tk.BooleanVar(value=True); chk = ctk.CTkCheckBox(self.bulk_list_frame, text=f, variable=var); chk.pack(anchor="w", padx=10, pady=2); self.bulk_files.append({'name': f, 'var': var, 'chk': chk})

    def select_all_bulk_files(self):
        for f in self.bulk_files: f['var'].set(True)

    def select_none_bulk_files(self):
        for f in self.bulk_files: f['var'].set(False)

    def get_ffmpeg_filter(self, scale, intro, logos):
        inputs = []; filter_chain = []
        if scale:
            filter_chain.append(f"[0:v]scale={scale}[v0]")
            curr_v = "v0"
        else:
            curr_v = "0:v"
        idx = 1
        if intro: inputs.append(f"-loop 1 -t 6 -i {shlex.quote(intro)}"); filter_chain.append(f"[{idx}:v]scale={scale if scale else 'iw:ih'}[intro];[intro]fade=t=in:st=0:d=1,fade=t=out:st=5:d=1[intro_faded];[{curr_v}][intro_faded]overlay=enable='between(t,0,6)'[v_intro]"); curr_v = "v_intro"; idx += 1
        pos_map = {"tr": "W-w-10:10", "tl": "10:10", "br": "W-w-10:H-h-10", "bl": "10:H-h-10"}
        for pos_key, path in logos.items():
            if path and path.strip(): inputs.append(f"-i {shlex.quote(path.strip())}"); filter_chain.append(f"[{curr_v}][{idx}:v]overlay={pos_map[pos_key]}[v_{pos_key}]"); curr_v = f"v_{pos_key}"; idx += 1
        if filter_chain:
            filter_args = f"-filter_complex \"{';'.join(filter_chain)}\" -map [{curr_v}]"
        else:
            filter_args = "-map 0:v"
        return " ".join(inputs), f"{filter_args} -map 0:a?"

    def build_auto_transcode_enqueue(self, remote_file, job_id, enabled=None):
        """Queue one durable server-side transcode job and start its FIFO worker."""
        if enabled is None:
            enabled = self.auto_transcode_downloads_v.get()
        if not enabled:
            return ""
        logos = {}
        for key, var, entry in [("tr", self.logo_tr_v, self.logo_tr_entry), ("tl", self.logo_tl_v, self.logo_tl_entry), ("br", self.logo_br_v, self.logo_br_entry), ("bl", self.logo_bl_v, self.logo_bl_entry)]:
            if var.get() and entry.get().strip(): logos[key] = entry.get().strip()
        intro = self.intro_path_entry.get().strip() if self.intro_v.get() else ""
        try: trim_seconds = max(0.0, float(self.trim_seconds_entry.get().strip() or 0)) if self.trim_v.get() else 0
        except ValueError: trim_seconds = 0
        trim = f"-ss {trim_seconds:g}" if trim_seconds > 0 else ""
        scale = {"1080p": "1920:1080", "720p": "1280:720", "480p": "854:480"}.get(self.res_var.get(), "")
        codec = self.codec_var.get().split(" ")[0]; pixel = "yuv420p10le" if self.bitdepth_var.get() == "10-bit" else "yuv420p"
        profile = "-profile:v main10" if pixel == "yuv420p10le" and codec == "libx265" else ("-profile:v high10" if pixel == "yuv420p10le" else "")
        try: bitrate = int(float(self.bitrate_entry.get().strip() or "820"))
        except ValueError: bitrate = 820
        extra_inputs, filter_cmd = self.get_ffmpeg_filter(scale, intro, logos)
        source = shlex.quote(remote_file)
        temp_base, temp_ext = os.path.splitext(remote_file)
        video_ext = temp_ext or ".mkv"
        disk_temp = shlex.quote(f"{temp_base}.sftp_transcoded_{job_id}{video_ext}")
        ram_root = "/dev/shm/sftp_studio/autotranscode"
        ram_dir_raw = f"{ram_root}/{job_id}"
        ram_dir = shlex.quote(ram_dir_raw)
        ram_input = shlex.quote(f"{ram_root}/{job_id}/input{video_ext}")
        ram_output = shlex.quote(f"{ram_root}/{job_id}/output{video_ext}")
        ffmpeg_direct = f"ffmpeg -nostdin -hide_banner -loglevel verbose {trim} -i {ram_input} {extra_inputs} {filter_cmd} -preset {shlex.quote(self.preset_var.get())} -threads 0 {profile} -b:v {bitrate}k -maxrate {bitrate * 2}k -bufsize {bitrate * 4}k -c:v {codec} -pix_fmt {pixel} -c:a copy -y {ram_output}"
        ffmpeg_as_service_user = f"su -s /bin/bash - xtreamaster -c {shlex.quote(ffmpeg_direct)}"
        ffmpeg_log = '"$HOME/ffmpeg_detailed.log"'
        ffmpeg_run = f"FFLOG={ffmpeg_log}; printf '\\n===== AUTOTRANSCODE {job_id} =====\\n' >> $FFLOG; echo \"USER=$(id -un) UID=$(id -u) HOST=$(hostname)\" >> $FFLOG; echo \"COMMAND: {ffmpeg_direct}\" >> $FFLOG; command -v ffmpeg >> $FFLOG 2>&1 || true; if {ffmpeg_direct} >> $FFLOG 2>&1; then echo '[AUTOTRANSCODE] DIRECT FFMPEG SUCCESS' >> $FFLOG; else code=$?; echo \"[AUTOTRANSCODE] DIRECT FFMPEG FAILED exit=$code; retrying as xtreamaster\" | tee -a $FFLOG; rm -f {ram_output}; echo \"SERVICE COMMAND: {ffmpeg_as_service_user}\" >> $FFLOG; if {ffmpeg_as_service_user} >> $FFLOG 2>&1; then echo '[AUTOTRANSCODE] XTREAMASTER FFMPEG SUCCESS' >> $FFLOG; else code=$?; echo \"[AUTOTRANSCODE] XTREAMASTER FFMPEG FAILED exit=$code\" | tee -a $FFLOG; exit $code; fi; fi"
        ffmpeg_cmd = f"mkdir -p {ram_dir} && chmod 777 {ram_dir} && cp -f {source} {ram_input} && chmod 666 {ram_input} && {ffmpeg_run} && cp -f {ram_output} {disk_temp} && mv -f {disk_temp} {source} && rm -rf {ram_dir}"
        root = "$HOME/.sftp_studio_autotranscode"; job_file = f"{root}/queue/{job_id}.job"
        job_body = f"#!/bin/bash\nset -e\nRAM_JOB_DIR={shlex.quote(ram_dir_raw)}\ntrap 'rm -rf \"$RAM_JOB_DIR\"' EXIT\n" + ffmpeg_cmd + "\n"
        worker_body = "root=\"$HOME/.sftp_studio_autotranscode\"\ncd \"$HOME\" || exit 1\nexec >> \"$root/worker.log\" 2>&1\necho \"[$(date '+%F %T')] WORKER STARTED (PID $$)\"\ntrap 'echo \"[$(date \"+%F %T\")] WORKER EXIT\"; rm -rf \"$root/worker.lock\" 2>/dev/null || true' EXIT\nwhile :; do\n    job=$(find \"$root/queue\" -maxdepth 1 -type f -name '*.job' -printf '%f\\n' 2>/dev/null | sort | head -n 1)\n    [ -n \"$job\" ] || break\n    echo \"[$(date '+%F %T')] START JOB $job\"\n    if ! mv \"$root/queue/$job\" \"$root/running/$job\" 2>/dev/null; then continue; fi\n    if bash \"$root/running/$job\"; then echo \"[$(date '+%F %T')] DONE JOB $job\"; mv \"$root/running/$job\" \"$root/done/$job\"; else code=$?; echo \"[$(date '+%F %T')] FAILED JOB $job (exit $code)\"; mv \"$root/running/$job\" \"$root/failed/$job\"; fi\ndone"
        return (f"AUTO_ROOT={root}; mkdir -p \"$AUTO_ROOT/queue\" \"$AUTO_ROOT/running\" \"$AUTO_ROOT/done\" \"$AUTO_ROOT/failed\"; "
                f"cat > \"$AUTO_ROOT/queue/{job_id}.job.tmp\" <<'AUTO_JOB_{job_id}'\n{job_body}AUTO_JOB_{job_id}\n"
                f"mv -f \"$AUTO_ROOT/queue/{job_id}.job.tmp\" \"$AUTO_ROOT/queue/{job_id}.job\"; "
                f"cat > \"$AUTO_ROOT/worker.sh.tmp\" <<'AUTO_WORKER_{job_id}'\n{worker_body}\nAUTO_WORKER_{job_id}\n"
                f"mv -f \"$AUTO_ROOT/worker.sh.tmp\" \"$AUTO_ROOT/worker.sh\"; chmod 700 \"$AUTO_ROOT/worker.sh\"; "
                f"if [ -d \"$AUTO_ROOT/worker.lock\" ] && ( [ ! -f \"$AUTO_ROOT/worker.lock/pid\" ] || ! kill -0 \"$(cat \"$AUTO_ROOT/worker.lock/pid\" 2>/dev/null)\" 2>/dev/null ); then rm -rf \"$AUTO_ROOT/worker.lock\"; fi; "
                f"echo '[AUTOTRANSCODE] JOB QUEUED: {job_id}' >> \"$AUTO_ROOT/worker.log\"; "
                f"if mkdir \"$AUTO_ROOT/worker.lock\" 2>/dev/null; then nohup bash \"$AUTO_ROOT/worker.sh\" </dev/null >/dev/null 2>&1 & worker_pid=$!; echo $worker_pid > \"$AUTO_ROOT/worker.lock/pid\"; echo '[AUTOTRANSCODE] WORKER LAUNCHED' >> \"$AUTO_ROOT/worker.log\"; else echo '[AUTOTRANSCODE] WORKER ALREADY RUNNING' >> \"$AUTO_ROOT/worker.log\"; fi")

    def _update_queue_indicator(self):
        count = self.queue_pending
        self.after(0, lambda c=count: self.queue_status_lbl.configure(text=f"Queue: {c}"))

    def enqueue_download_job(self, func, *args, count=1):
        with self.queue_lock:
            self.queue_pending += max(1, int(count))
            self._update_queue_indicator()
        self.download_queue.put((func, args, max(1, int(count))))

    def _download_queue_worker(self):
        while True:
            func, args, count = self.download_queue.get()
            try:
                func(*args)
            except Exception as exc:
                self.after(0, lambda e=exc: self.log(f"Queue error: {e}"))
            finally:
                with self.queue_lock:
                    self.queue_pending = max(0, self.queue_pending - count)
                    self._update_queue_indicator()
                self.download_queue.task_done()

    def start_batch_download(self):
        self.save_current_settings(silent=True)
        task_specs = [(r['url'].get().strip(), r['path'].get().strip(), r['name'].get().strip(), r['process'].get(), r['is_m3u8'].get()) for r in self.rows if r['url'].get().strip()]
        # Queue each direct-link item separately so Queue decreases after every
        # completed file, while the single worker still guarantees FIFO order.
        for task in task_specs:
            self.enqueue_download_job(self.execute_batch, [task], count=1)

    def execute_batch(self, tasks_override=None):
        host, port, user, pw = self.host_entry.get(), int(self.port_entry.get() or 22), self.user_entry.get(), self.pass_entry.get(); logos = {}
        for k, v, e in [("tr", self.logo_tr_v, self.logo_tr_entry), ("tl", self.logo_tl_v, self.logo_tl_entry), ("br", self.logo_br_v, self.logo_br_entry), ("bl", self.logo_bl_v, self.logo_bl_entry)]:
            if v.get(): logos[k] = e.get()
        intro = self.intro_path_entry.get().strip() if self.intro_v.get() else ""
        trim_seconds = 0
        if self.trim_v.get():
            try: trim_seconds = max(0.0, float(self.trim_seconds_entry.get().strip() or 0))
            except ValueError: trim_seconds = 0
        trim_input = f"-ss {trim_seconds:g}" if trim_seconds > 0 else ""
        res, bit, preset = self.res_var.get(), self.bitrate_entry.get(), self.preset_var.get(); mode = "sequential"; codec_choice = self.codec_var.get().split(" ")[0]; bitdepth_choice = self.bitdepth_var.get(); tasks = tasks_override if tasks_override is not None else [(r['url'].get().strip(), r['path'].get().strip(), r['name'].get().strip(), r['process'].get(), r['is_m3u8'].get()) for r in self.rows if r['url'].get().strip()]
        if not tasks: return
        try:
            ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()); ssh.connect(host, port=port, username=user, password=pw, timeout=10); sftp = ssh.open_sftp(); RAM_DISK = "/dev/shm/sftp_studio"; scale = {"1080p": "1920:1080", "720p": "1280:720", "480p": "854:480"}.get(res, ""); user_agent = "IPTVSmartersPlayer"; task_cmds = []
            pixel_format = "yuv420p10le" if bitdepth_choice == "10-bit" else "yuv420p"
            for u, p, n, pr, force_m3u8 in tasks:
                original_u = u
                u = normalize_download_url(u)
                if original_u != u:
                    self.log(f"URL normalized HTTPS -> HTTP: {original_u} -> {u}", is_cmd=True)
                task_id = int(time.time() * 1000) % 1000000; task_dir = f"{RAM_DISK}/task_{task_id}"; ssh.exec_command(f"mkdir -p \"{task_dir}\"")
                is_m3u8 = force_m3u8 or "m3u8" in u.lower()
                
                # Smart Referer extraction
                referer = "https://google.com/"
                ref_match = re.search(r'(https?://[^/]+)', u)
                if ref_match: referer = ref_match.group(1) + "/"
                
                # Write URL to a remote file to avoid command length issues
                url_file = f"{task_dir}/url.txt"
                with sftp.file(url_file, 'w') as f: f.write(u)
                
                if is_m3u8:
                    final_name = n if n else f"stream_{task_id}.mp4"
                    if not (final_name.endswith(".mp4") or final_name.endswith(".mkv")): final_name += ".mp4"
                    ffmpeg_base = (f"ffmpeg -nostdin -hide_banner -loglevel error -protocol_whitelist file,http,https,tcp,tls,crypto -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 2 -user_agent {shlex.quote(user_agent)} -headers {shlex.quote(f'Referer: {referer}')}")
                    
                    mkdir_cmd = f"echo {shlex.quote('[EXEC] mkdir -p ' + p)} && mkdir -p {shlex.quote(p)}"
                    ffmpeg_cmd_raw = f"{ffmpeg_base} {trim_input} -i \"$(cat {url_file})\""
                    ffmpeg_cmd = f"echo {shlex.quote('[EXEC] ' + ffmpeg_cmd_raw)} && {ffmpeg_cmd_raw}"
                    rm_cmd = f"echo {shlex.quote('[EXEC] rm -rf ' + task_dir)} && rm -rf {shlex.quote(task_dir)}"
                    
                    if pr: 
                        extra_inputs, f_cmd = self.get_ffmpeg_filter(scale, intro, logos)
                        prof = ""
                        if pixel_format == "yuv420p10le":
                            if codec_choice == "libx265": prof = "-profile:v main10"
                            else: prof = "-profile:v high10"
                        cmd = f"({mkdir_cmd} && {ffmpeg_cmd} {extra_inputs} {f_cmd} -preset {preset} -threads 0 {prof} -b:v {bit}k -maxrate {int(bit)*2}k -bufsize {int(bit)*4}k -c:v {codec_choice} -pix_fmt {pixel_format} -c:a aac -b:a 128k \"{p.rstrip('/')}/{final_name}\" && {rm_cmd})"
                    else: 
                        direct_output = f"{p.rstrip('/')}/{final_name}"
                        auto_cmd = self.build_auto_transcode_enqueue(direct_output, f"direct_{task_id}")
                        cmd = f"({mkdir_cmd} && {ffmpeg_cmd} -c copy \"{direct_output}\" && {auto_cmd} && {rm_cmd})" if auto_cmd else f"({mkdir_cmd} && {ffmpeg_cmd} -c copy \"{direct_output}\" && {rm_cmd})"
                    task_cmds.append(cmd)
                else:
                    mkdir_cmd = f"echo {shlex.quote('[EXEC] mkdir -p ' + p)} && mkdir -p {shlex.quote(p)}"
                    # Robust direct download: do not use -OJ with -C -.
                    # Some CDN servers fail when curl must create a filename from
                    # Content-Disposition while resuming a URL containing [] and *.
                    headers = [
                        f"-H 'Accept: */*'",
                        f"-H 'Accept-Language: en-US,en;q=0.9'",
                        f"-H 'Connection: keep-alive'",
                        f"--compressed",
                        f"--tcp-nodelay",
                        f"--keepalive-time 60"
                    ]
                    temp_file = '.sftp_download.part'
                    curl_cmd_raw = f"curl -f -L -C - --retry 10 -A {shlex.quote(user_agent)} -e {shlex.quote(referer)} {' '.join(headers)} -D .response_headers -o {shlex.quote(temp_file)} -w '%{{url_effective}}' \"$(cat url.txt)\" > .effective_url"
                    rename_downloaded = "EFFECTIVE_URL=$(cat .effective_url); FILE_NAME=$(grep -i '^content-disposition:' .response_headers | tail -n 1 | sed -n 's/.*filename=\\\"\\\\([^\\\"]*\\\\)\\\".*/\\\\1/p'); if [ -z \"$FILE_NAME\" ]; then URL_PATH=${EFFECTIVE_URL%%\\?*}; FILE_NAME=${URL_PATH##*/}; fi; [ -n \"$FILE_NAME\" ] || FILE_NAME=download_file; mv -f " + shlex.quote(temp_file) + " \"$FILE_NAME\"; FILE=$(pwd)/\"$FILE_NAME\""
                    wget_cmd = f"echo {shlex.quote('[EXEC] Golden Camouflage curl download with safe filename')} && cd {shlex.quote(task_dir)} && {curl_cmd_raw} && {rename_downloaded}"
                    rm_cmd = f"echo {shlex.quote('[EXEC] rm -rf ' + task_dir)} && rm -rf {shlex.quote(task_dir)}"
                    
                    if pr: 
                        extra_inputs, f_cmd = self.get_ffmpeg_filter(scale, intro, logos)
                        prof = ""
                        if pixel_format == "yuv420p10le":
                            if codec_choice == "libx265": prof = "-profile:v main10"
                            else: prof = "-profile:v high10"
                        f_msg = f"[EXEC] ffmpeg processing with {codec_choice} 10-bit"
                        output_name = n if n else "${FILE##*/}.processed"
                        task_cmds.append(f"({mkdir_cmd} && {wget_cmd} && echo {shlex.quote(f_msg)} && ffmpeg -nostdin -hide_banner -loglevel error {trim_input} -i \"$FILE\" {extra_inputs} {f_cmd} -preset {preset} -threads 0 {prof} -b:v {bit}k -maxrate {int(bit)*2}k -bufsize {int(bit)*4}k -c:v {codec_choice} -pix_fmt {pixel_format} -c:a copy \"{p.rstrip('/')}/{output_name}\" && {rm_cmd})")
                    else: 
                        destination = n if n else '${FILE##*/}'
                        mv_full = f"mv $FILE to {p.rstrip('/')}/{destination}"
                        auto_cmd = self.build_auto_transcode_enqueue(f"{p.rstrip('/')}/{n}", f"direct_{task_id}") if n else ""
                        task_cmds.append(f"({mkdir_cmd} && {wget_cmd} && echo {shlex.quote('[EXEC] ' + mv_full)} && mv \"$FILE\" \"{p.rstrip('/')}/{destination}\" {'&& ' + auto_cmd if auto_cmd else ''} && {rm_cmd})")
            final_chain = (" & ".join(task_cmds) + " & wait") if mode == "parallel" else " ; ".join(task_cmds)
            full_script = f"echo '--- NEW BATCH STARTED ({mode}) ---' > ~/wget_monitor.log\n" + \
                          f"echo 'FULL COMMAND: {final_chain}' >> ~/wget_monitor.log\n" + \
                          f"{final_chain}"
            self.log(f"Executing Batch Command (Check debug_commands.log for details)", is_cmd=True)
            local_debug_log(f"BATCH DOWNLOAD ({mode})\nPROFILE: {self.current_profile.get()}\nCOMMAND:\n{final_chain}")
            # Run synchronously inside the queue worker. The previous nohup/&
            # returned immediately, allowing the next queued item to overlap.
            stdin, stdout, stderr = ssh.exec_command(f"bash -c {shlex.quote(full_script)}")
            stdout.channel.recv_exit_status()
            sftp.close(); ssh.close()
            self.after(0, lambda: self.log(TRANSLATIONS[self.lang]["msg_start"] + mode))
        except Exception as e: self.after(0, lambda e=e: self.log(TRANSLATIONS[self.lang]["msg_error"] + str(e)))

    def start_bulk_transcode(self):
        selected = [f['name'] for f in self.bulk_files if f['var'].get()]
        if not selected: messagebox.showwarning("Warning", "No files selected!"); return
        self.save_current_settings(silent=True); threading.Thread(target=self.execute_bulk_transcode, args=(selected,), daemon=True).start()

    def execute_bulk_transcode(self, selected_files):
        """Run a portable bulk FFmpeg pass without embedding Python expressions in f-strings."""
        host = self.host_entry.get()
        port = int(self.port_entry.get() or 22)
        user = self.user_entry.get()
        pw = self.pass_entry.get()
        dir_path = self.bulk_path_entry.get().strip()
        res = self.res_var.get()
        bitrate = self.bitrate_entry.get().strip() or "820"
        preset = self.preset_var.get()
        codec_choice = self.codec_var.get().split(" ")[0]
        scale_map = {"1080p": "1920:1080", "720p": "1280:720", "480p": "854:480"}
        scale = scale_map.get(res, "")
        try:
            bitrate_int = int(float(bitrate))
        except ValueError:
            bitrate_int = 820
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, port=port, username=user, password=pw, timeout=10)
            ram_dir = "/dev/shm/sftp_studio/bulk/bulk_%d" % int(time.time() * 1000)
            ssh.exec_command("mkdir -p %s" % shlex.quote(ram_dir))
            script_lines = [
                "#!/bin/bash",
                "set -u",
                "exec > ~/wget_monitor.log 2>&1",
                "TARGET_DIR=%s" % shlex.quote(dir_path),
                "RAM_DIR=%s" % shlex.quote(ram_dir),
                "mkdir -p \"$RAM_DIR\"",
                "cd \"$TARGET_DIR\" || exit 1",
            ]
            for index, filename in enumerate(selected_files):
                safe_name = shlex.quote(filename)
                temp_name = ".sftp_bulk_%d.%s" % (index, filename.rsplit(".", 1)[-1] if "." in filename else "mkv")
                temp_path = "$RAM_DIR/%s" % temp_name
                args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", "$RAM_DIR/input_%d" % index]
                if scale:
                    args.extend(["-vf", "scale=%s" % scale])
                args.extend(["-preset", preset, "-b:v", "%dk" % bitrate_int, "-maxrate", "%dk" % (bitrate_int * 2), "-bufsize", "%dk" % (bitrate_int * 4), "-c:v", codec_choice, "-c:a", "copy", temp_path])
                command = " ".join(shlex.quote(x) if "$RAM_DIR" not in x else x for x in args)
                script_lines.extend([
                    "echo %s" % shlex.quote("Processing: " + filename),
                    "cp -f -- %s \"$RAM_DIR/input_%d\"" % (safe_name, index),
                    command,
                    "if [ -f %s ]; then mv -f %s %s; fi" % (temp_path, temp_path, safe_name),
                    "rm -f \"$RAM_DIR/input_%d\"" % index,
                ])
            script_lines.extend(["rm -rf \"$RAM_DIR\"", "echo %s" % shlex.quote("--- Bulk Process Finished ---")])
            bulk_bash_script = "\n".join(script_lines)
            self.log("Executing Bulk Transcode (Check debug_commands.log for details)", is_cmd=True)
            local_debug_log("BULK TRANSCODE\nDIRECTORY: %s\nSCRIPT:\n%s" % (dir_path, bulk_bash_script))
            ssh.exec_command("nohup bash -c %s > ~/wget_monitor.log 2>&1 &" % shlex.quote(bulk_bash_script))
            self.after(0, lambda: self.log(TRANSLATIONS[self.lang]["msg_bulk_start"]))
            ssh.close()
        except Exception as exc:
            self.after(0, lambda exc=exc: self.log(TRANSLATIONS[self.lang]["msg_error"] + str(exc)))

    def monitor_downloads(self): threading.Thread(target=self.update_monitor, daemon=True).start()

    def update_monitor(self):
        host, port, user, pw = self.host_entry.get(), int(self.port_entry.get() or 22), self.user_entry.get(), self.pass_entry.get()
        try:
            ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()); ssh.connect(host, port=port, username=user, password=pw, timeout=10); stdin, stdout, stderr = ssh.exec_command("tail -n 100 ~/wget_monitor.log"); log = stdout.read().decode('utf-8'); stdin, stdout, stderr = ssh.exec_command("ps -eo args | grep -E 'wget|ffmpeg' | grep -v grep"); active = stdout.readlines(); self.after(0, lambda: self.tree.delete(*self.tree.get_children())); prog = re.findall(r"(\d+)%\s+([\d.]+[KMG]B?/s)\s+([\w\d]+s?)", log)
            for idx, proc in enumerate(active):
                status, info = "Processing..." if "ffmpeg" in proc else "Downloading...", "Working..."
                if "wget" in proc and idx < len(prog): p, s, e = prog[-(idx+1)]; info = f"{p}% | {s} | {e}"
                self.after(0, lambda s=status, i=info: self.tree.insert("", "end", values=("Active Task", s, i)))
            if prog: total_p = sum([int(p) for p, s, e in prog]) / len(prog); self.after(0, lambda v=total_p/100: self.progress_bar.set(v))
            elif not active: self.after(0, lambda: self.progress_bar.set(0))
            ssh.close()
        except: pass

    def fetch_categories_async(self, row, force=False):
        url = row['url'].get().strip()
        if not url or not url.startswith("http"): return
        std_url = url
        if "type=m3u" in std_url and "type=m3u_plus" not in std_url: std_url = std_url.replace("type=m3u", "type=m3u_plus")
        if not force and std_url in self.cached_m3u_content:
            cats = self.cached_m3u_content[std_url]; row['cat_opt'].configure(values=cats)
            if row['cat_var'].get() not in cats: row['cat_var'].set("All Categories")
            return
        threading.Thread(target=self.do_fetch_categories, args=(row, std_url), daemon=True).start()

    def do_fetch_categories(self, row, url):
        try:
            self.after(0, lambda: row['cat_opt'].configure(values=[TRANSLATIONS[self.lang]["fetching_cats"]]))
            session = requests.Session()
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            session.headers.update({"User-Agent": ua, "Referer": url})
            # Initial handshake to get cookies/bypass WAF
            session.get(url, timeout=15, stream=True)
            response = session.get(url, timeout=25, stream=True)
            if response.status_code == 200:
                categories = set(["All Categories"]); lines_count = 0
                for line in response.iter_lines(decode_unicode=True, chunk_size=4096):
                    if line and "#EXTINF" in line:
                        match = re.search(r'group-title="([^"]+)"', line, re.I)
                        if match: categories.add(match.group(1).strip())
                    lines_count += 1
                    if lines_count > 500000: break
                sorted_cats = sorted(list(categories)); self.cached_m3u_content[url] = sorted_cats; self.after(0, lambda: row['cat_opt'].configure(values=sorted_cats)); self.after(0, lambda: row['cat_var'].set("All Categories"))
        except Exception as e:
            self.after(0, lambda: row['cat_opt'].configure(values=["All Categories"]))
            self.after(0, lambda: self.log(f"Category Fetch Error: {str(e)}"))

    def scan_xtream_source(self, row_idx):
        r = self.xtream_rows[row_idx]
        h, u, p = r['host'].get().strip(), r['user'].get().strip(), r['pass'].get().strip()
        if not (h and u and p): messagebox.showwarning("Xtream API", "Please enter Host, Username and Password."); return
        keyword = r['search'].get().strip()
        if not keyword: messagebox.showwarning("Warning", "Please enter a keyword to search."); return
        self.clear_m3u_results()
        self.save_current_settings(silent=True)
        threading.Thread(target=self.do_scan_xtream_api, args=(h, u, p, keyword, r['vod_v'].get(), r['ser_v'].get()), daemon=True).start()

    def do_scan_xtream_api(self, h, u, p, keyword, do_vod, do_ser):
        try:
            self.after(0, lambda: self.log(TRANSLATIONS[self.lang]["m3u_searching"]))
            base_url = f"{h.rstrip('/')}/player_api.php?username={u}&password={p}"
            ua = "IPTVSmartersPlayer"
            found_any = False; results = []
            source_meta = {"url": base_url, "u": u, "p": p, "keyword": keyword}
            
            if do_vod:
                self.after(0, lambda: self.log("Xtream API: Fetching Movies..."))
                try:
                    res = requests.get(f"{base_url}&action=get_vod_streams", headers={"User-Agent": ua}, timeout=30)
                    if res.status_code == 200:
                        for item in res.json():
                            name = item.get("name", "")
                            if keyword.lower() in name.lower():
                                stream_id = item.get("stream_id")
                                ext = item.get("container_extension", "mkv")
                                file_url = f"{h.rstrip('/')}/movie/{u}/{p}/{stream_id}.{ext}"
                                results.append((name, (file_url, item.get("stream_icon"))))
                                found_any = True
                except: pass
            
            if do_ser:
                self.after(0, lambda: self.log("Xtream API: Fetching Series..."))
                try:
                    res = requests.get(f"{base_url}&action=get_series", headers={"User-Agent": ua}, timeout=30)
                    if res.status_code == 200:
                        series_list = res.json()
                        matches = [s for s in series_list if keyword.lower() in s.get("name", "").lower()]
                        for item in matches:
                            series_name = item.get("name", "")
                            self.after(0, lambda n=series_name: self.log(f"Xtream API: Getting episodes for {n}..."))
                            ep_res = requests.get(f"{base_url}&action=get_series_info&series_id={item['series_id']}", headers={"User-Agent": ua}, timeout=20)
                            if ep_res.status_code == 200:
                                ep_data = ep_res.json().get("episodes", {})
                                for s_num in ep_data:
                                    for ep in ep_data[s_num]:
                                        ep_name = f"{series_name} - S{int(s_num):02d}E{int(ep.get('episode_num', 0)):02d}"
                                        stream_id = ep.get("id")
                                        ext = ep.get("container_extension", "mkv")
                                        file_url = f"{h.rstrip('/')}/series/{u}/{p}/{stream_id}.{ext}"
                                        # Use series icon if episode icon is missing
                                        ep_icon = ep.get("stream_icon") or item.get("last_episode", {}).get("stream_icon") or item.get("cover")
                                        results.append((ep_name, (file_url, ep_icon)))
                                        found_any = True
                except: pass
            
            if results:
                self.after(0, lambda n=len(results): self.log(f"Xtream API: Found {n} items. Displaying..."))
                self.batch_add_results(results, source_meta)
            elif not found_any:
                self.after(0, lambda: self.log("Xtream API: No matching results found."))
        except Exception as e:
            self.after(0, lambda e=e: self.log(f"Xtream API Error: {str(e)}"))

    def scrape_m3u_logic(self, url, keyword, selected_cat, source_name):
        found_any = False
        source_meta = {"url": url, "keyword": keyword, "category": selected_cat}
        try:
            self.after(0, lambda: self.log(TRANSLATIONS[self.lang]["m3u_searching"]))
            session = requests.Session()
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            session.headers.update({"User-Agent": ua, "Referer": url})
            session.get(url, timeout=15) # Handshake
            response = session.get(url, timeout=40)
            if response.status_code == 200:
                self.after(0, self.clear_m3u_results)
                lines = response.text.splitlines()
                results = []
                self.after(0, lambda: self.log(f"{source_name}: Processing {len(lines)} lines..."))
                for i in range(len(lines)):
                    line = lines[i].strip()
                    if line.startswith("#EXTINF"):
                        group_match = re.search(r'group-title="([^"]*)"', line, re.IGNORECASE)
                        current_group = group_match.group(1).strip() if group_match else "Uncategorized"
                        name = line.split(",", 1)[1].strip()
                        if (selected_cat == "All Categories" or current_group.lower() == selected_cat.lower()) and (not keyword or keyword.lower() in name.lower()):
                            if i + 1 < len(lines):
                                file_url = lines[i+1].strip()
                                if file_url.startswith("http"):
                                    results.append((name, file_url))
                                    found_any = True
                if results:
                    self.after(0, lambda n=len(results): self.log(f"{source_name}: Found {n} items. Displaying..."))
                    self.batch_add_results(results, source_meta)
                elif not found_any: self.after(0, lambda: self.log(f"{source_name} Scraper: No matching results found."))
        except Exception as e: self.after(0, lambda e=e: self.log(f"{source_name} Error: {str(e)}"))

    def start_m3u_scrape_single(self, row_idx):
        self.clear_m3u_results()
        threading.Thread(target=self.scrape_m3u_single, args=(row_idx,), daemon=True).start()

    def scrape_m3u_single(self, row_idx):
        row = self.m3u_rows[row_idx]; url, keyword, selected_cat = row['url'].get().strip(), row['search'].get().strip(), row['cat_var'].get()
        if not url: messagebox.showwarning("Warning", "Please enter an M3U URL first!"); return
        if "type=m3u" in url and "type=m3u_plus" not in url: url = url.replace("type=m3u", "type=m3u_plus")
        threading.Thread(target=self.scrape_m3u_logic, args=(url, keyword, selected_cat, "M3U"), daemon=True).start()

    def add_m3u_item(self, name, url, source_meta, assigned_path="Path 1", poster=None):
        item_id = self.m3u_tree.insert("", "end", values=(name, assigned_path, url))
        self.m3u_items.append({'id': item_id, 'name': name, 'url': url, 'path': assigned_path, 'group': self.m3u_path_group.get(), 'source_meta': source_meta, 'poster': poster})
        self.update_active_path_highlights()

    def assign_path_to_selected(self, idx):
        selected = self.m3u_tree.selection()
        if not selected: messagebox.showinfo("Assign Path", "Please select items from the list first."); return
        new_path = f"Path {idx+1}"
        for sel_id in selected:
            for item in self.m3u_items:
                if item['id'] == sel_id:
                    item['path'] = new_path
                    self.m3u_tree.set(sel_id, "path", new_path)
                    break
        self.update_active_path_highlights()

    def update_active_path_highlights(self):
        active_indices = set()
        for item in self.m3u_items:
            try:
                idx = int(item['path'].split(" ")[1]) - 1
                active_indices.add(idx)
            except: pass
        
        p = self.current_profile.get()
        mode = self.m3u_path_mode.get().lower()
        if mode not in ["series", "vod"]: mode = "series"
        history = self.config.get(p, {}).get("m3u_history", {})

        for i in range(10):
            # Update History Label & Tooltip
            path_key = self._path_history_key(i)
            h_data = history.get(path_key, {})
            if not h_data:
                h_data = history.get(f"{mode}_path_{i+1}", {})
            last_name = h_data.get("name", "")
            last_poster = h_data.get("poster", "")
            
            # Remove old tooltips from label/entry to refresh
            for w in [self.m3u_path_entries[i]['lbl'], self.m3u_path_entries[i]['ent']]:
                w.unbind("<Enter>"); w.unbind("<Leave>")
            
            if last_name:
                display_name = (last_name[:15] + "..") if len(last_name) > 15 else last_name
                self.m3u_path_entries[i]['lbl'].configure(text=f"PATH {i+1} ({display_name})", text_color=COLOR_ACCENT)
                ToolTip(self.m3u_path_entries[i]['lbl'], last_name, image_url=last_poster, large_text=last_name)
                ToolTip(self.m3u_path_entries[i]['ent'], last_name, image_url=last_poster, large_text=last_name)
            else:
                self.m3u_path_entries[i]['lbl'].configure(text=f"PATH {i+1}", text_color=COLOR_TEXT_DIM)

            # Update Highlights
            if i in active_indices:
                self.m3u_path_entries[i]['ent'].configure(border_color="#28a745", border_width=2)
            else:
                self.m3u_path_entries[i]['ent'].configure(border_color=["#979da2", "#565b5e"], border_width=1)

    def select_all_m3u_items(self):
        self.m3u_tree.selection_set(self.m3u_tree.get_children())

    def select_none_m3u_items(self):
        self.m3u_tree.selection_remove(self.m3u_tree.get_children())

    def clear_m3u_results(self):
        self.m3u_tree.delete(*self.m3u_tree.get_children())
        self.m3u_items = []
        self.update_active_path_highlights()

    def batch_add_results(self, results, source_meta, is_checked=False, path_idx=0):
        # Initial clear to be safe
        if not is_checked: self.clear_m3u_results()
        
        # Immediate display of first 50 to feel responsive
        first_batch = results[:50]
        self.after(0, lambda: self.process_ui_batch(first_batch, source_meta, is_checked, path_idx))
        
        # Process remaining in batches
        if len(results) > 50:
            batch_size = 50
            for i in range(50, len(results), batch_size):
                batch = results[i:i+batch_size]
                delay = (i // batch_size) * 100 # 100ms between batches
                self.after(delay, lambda b=batch: self.process_ui_batch(b, source_meta, is_checked, path_idx))

    def process_ui_batch(self, batch, source_meta, is_checked, path_idx):
        for item in batch:
            try:
                name, url_data = item
                if is_checked:
                    f_url, is_new, poster = url_data
                    self.add_m3u_item_checked(name, f_url, is_new, path_idx, source_meta, poster=poster)
                else:
                    f_url, poster = url_data if isinstance(url_data, tuple) else (url_data, None)
                    self.add_m3u_item(name, f_url, source_meta, poster=poster)
            except: pass

    def check_for_new_items_automated(self, path_idx, silent=False):
        p = self.current_profile.get()
        if p not in self.config or "m3u_history" not in self.config[p]:
            if not silent: messagebox.showwarning("Check for New", "No download history found for this profile.")
            return
        mode = self.m3u_path_mode.get().lower()
        if mode not in ["series", "vod"]: mode = "series"
        history = self.config[p]["m3u_history"];             path_key = self._path_history_key(path_idx)
        legacy_key = f"{mode}_path_{path_idx+1}"
        if path_key not in history and legacy_key in history:
            history[path_key] = history[legacy_key]
        if path_key not in history or not history[path_key].get("url"):
            if not silent: messagebox.showwarning("Check for New", f"No M3U history linked to this {mode} path.")
            return
        meta = history[path_key]; target_path = self.m3u_path_entries[path_idx]['ent'].get().strip()
        if not target_path: return
        host, port, user, pw = self.host_entry.get(), int(self.port_entry.get() or 22), self.user_entry.get(), self.pass_entry.get()
        if not host: return
        threading.Thread(target=self.do_check_automated, args=(path_idx, target_path, meta, host, port, user, pw, silent, self.m3u_path_group.get()), daemon=True).start()

    def update_path_status(self, idx, color, tooltip=""):
            self.after(0, lambda: self.m3u_path_entries[idx]['status_lbl'].configure(text_color=color))

    def do_check_automated(self, path_idx, target_path, meta, host, port, user, pw, silent=False, group=None):
        try:
            self.update_path_status(path_idx, "#007bff") # Blue: Scanning
            ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()); ssh.connect(host, port=port, username=user, password=pw, timeout=10)
            # Get list of files on remote server
            stdin, stdout, stderr = ssh.exec_command(f"ls -1 {shlex.quote(target_path)}")
            remote_files = [f.strip() for f in stdout.readlines() if f.strip()]
            ssh.close()
            
            url, keyword, cat = meta.get("url"), meta.get("keyword", ""), meta.get("category", "All Categories")
            is_xtream = "player_api.php" in url
            
            matching_m3u_items = []
            if is_xtream:
                # Xtream API logic
                ua = "IPTVSmartersPlayer"
                base_url = url.split("&action=")[0]
                if meta.get("is_series", True):
                    res = requests.get(f"{base_url}&action=get_series", headers={"User-Agent": ua}, timeout=30)
                    if res.status_code == 200:
                        for item in res.json():
                            if keyword.lower() in item.get("name", "").lower():
                                s_res = requests.get(f"{base_url}&action=get_series_info&series_id={item['series_id']}", headers={"User-Agent": ua}, timeout=20)
                                if s_res.status_code == 200:
                                    ep_data = s_res.json().get("episodes", {})
                                    for s_num in ep_data:
                                        for ep in ep_data[s_num]:
                                            ep_name = f"{item['name']} - S{int(s_num):02d}E{int(ep.get('episode_num', 0)):02d}"
                                            f_url = f"{url.split('/player_api.php')[0]}/series/{meta['u']}/{meta['p']}/{ep['id']}.{ep.get('container_extension', 'mkv')}"
                                            ep_icon = ep.get("stream_icon") or item.get("last_episode", {}).get("stream_icon") or item.get("cover")
                                            matching_m3u_items.append((ep_name, (f_url, ep_icon)))
            else:
                # Standard M3U logic
                res = requests.get(url, timeout=30)
                if res.status_code == 200:
                    lines = res.text.splitlines()
                    for i in range(len(lines)):
                        line = lines[i].strip()
                        if line.startswith("#EXTINF"):
                            g_match = re.search(r'group-title="([^"]*)"', line, re.I); current_g = g_match.group(1).strip() if g_match else "Uncategorized"; name = line.split(",", 1)[1].strip()
                            if (cat == "All Categories" or current_g.lower() == cat.lower()) and (not keyword or keyword.lower() in name.lower()):
                                if i + 1 < len(lines):
                                    f_url = lines[i+1].strip()
                                    if f_url.startswith("http"): 
                                        # Extract logo from M3U if exists
                                        logo_match = re.search(r'tvg-logo="([^"]+)"', line, re.I)
                                        logo_url = logo_match.group(1) if logo_match else None
                                        matching_m3u_items.append((name, (f_url, logo_url)))

            if not silent: self.after(0, self.clear_m3u_results)
            
            new_found_count = 0; new_to_download = []
            results_for_ui = []
            for name, data in matching_m3u_items:
                f_url, poster = data if isinstance(data, tuple) else (data, None)
                clean_name = re.sub(r'[\\\\/*?:"<>|]', "", name).strip()
                pattern_match = re.search(r'S\d+E\d+', name, re.I)
                pattern = pattern_match.group(0).lower() if pattern_match else None
                
                exists = False
                for rf in remote_files:
                    if clean_name.lower() in rf.lower(): exists = True; break
                    if pattern and pattern in rf.lower(): exists = True; break
                
                is_new = not exists
                if is_new: 
                    new_found_count += 1
                    new_to_download.append({'name': name, 'url': f_url, 'path': target_path, 'poster': poster})
                
                if not silent:
                    results_for_ui.append((name, (f_url, is_new, poster)))

            if not silent and results_for_ui:
                self.batch_add_results(results_for_ui, meta, is_checked=True, path_idx=path_idx)

            if new_found_count > 0:
                p = self.current_profile.get()
                if p in self.config and "m3u_history" in self.config[p]:
                    mode = self.m3u_path_mode.get().lower()
                    if mode not in ["series", "vod"]: mode = "series"
                    path_key = self._path_history_key(path_idx, group)
                    if path_key in self.config[p]["m3u_history"]:
                        self.config[p]["m3u_history"][path_key]["attempts"] = 0
                        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(self.config, f, indent=4)
                self.update_path_status(path_idx, "#28a745") # Green: Found/Downloading
                if silent:
                    self.log(f"Background Auto-Check: Path {path_idx+1} found {new_found_count} new items. Adding them to the download queue.")
                    for item in new_to_download:
                        self.enqueue_download_job(self.execute_m3u_download_sequential, [item], count=1)
                else:
                    self.after(0, lambda: messagebox.showinfo("Check for New", f"Checked Path {path_idx+1}: Found {new_found_count} new episodes!"))
            else:
                self.update_path_status(path_idx, "gray") # Gray: No new items
                if silent: self.log(f"Background Auto-Check: Path {path_idx+1} - No new items found.")
                elif not silent:
                    self.after(0, lambda: messagebox.showinfo("Check for New", f"Checked Path {path_idx+1}: All episodes already exist."))
            ssh.close()
        except Exception as e:
            self.update_path_status(path_idx, "#dc3545")
            self.after(0, lambda e=e: self.log(f"Check for New Error: {str(e)}"))

    def add_m3u_item_checked(self, name, url, is_new, path_idx, source_meta, poster=None):
        path_name = f"Path {path_idx+1}"
        item_id = self.m3u_tree.insert("", "end", values=(name, path_name, url))
        if is_new: self.m3u_tree.selection_add(item_id)
        self.m3u_items.append({'id': item_id, 'name': name, 'url': url, 'path': path_name, 'group': self.m3u_path_group.get(), 'source_meta': source_meta, 'poster': poster})
        self.update_active_path_highlights()

    def start_m3u_download_selected(self):
        selected_ids = self.m3u_tree.selection()
        if not selected_ids: messagebox.showwarning("Warning", "No items selected!"); return
        
        selected = []; path_map = {f"Path {i+1}": self.m3u_path_entries[i]['ent'].get().strip() for i in range(10)}; p = self.current_profile.get(); selected_group = self.m3u_path_group.get()
        if p not in self.config: self.config[p] = {}
        if "m3u_history" not in self.config[p]: self.config[p]["m3u_history"] = {}
        
        for sel_id in selected_ids:
            for item in self.m3u_items:
                if item['id'] == sel_id:
                    target_path = path_map.get(item['path'])
                    if not target_path: messagebox.showwarning("Warning", f"Path for '{item['name']}' is empty!"); return
                    selected.append({'name': item['name'], 'url': item['url'], 'path': target_path, 'group': item.get('group', selected_group), 'poster': item.get('poster'), 'section_transcode': self.m3u_auto_transcode_v.get()})
                    
                    # Update History with last downloaded item name
                    p_idx = int(item['path'].split(" ")[1]) - 1
                    mode = self.m3u_path_mode.get().lower()
                    if mode not in ["series", "vod"]: mode = "series"
                    
                    if "m3u_history" not in self.config[p]: self.config[p]["m3u_history"] = {}
                    path_key = self._path_history_key(p_idx, item.get('group', selected_group))
                    
                    # Update the metadata and include the name & poster for display
                    meta = item.get('source_meta', {}).copy()
                    meta['name'] = item['name']
                    meta['poster'] = item.get('poster')
                    meta['group'] = item.get('group', selected_group)
                    self.config[p]["m3u_history"][path_key] = meta
                    break
        
        if not selected: messagebox.showwarning("Warning", "No items selected!"); return
        self.save_current_settings(silent=True)
        self.update_active_path_highlights() # Refresh UI to show new names immediately
        # Enqueue each item separately so the indicator decreases after every
        # completed file, while the worker guarantees strict FIFO execution.
        for item in selected:
            self.enqueue_download_job(self.execute_m3u_download_sequential, [item], count=1)

    def execute_m3u_download_sequential(self, selected_items):
        # The central queue already serializes this operation. Keep the guard
        # for compatibility with older callers, but do not wait while holding
        # a queue worker slot.
        self.is_downloading = True
        host, port, user, pw = self.host_entry.get(), int(self.port_entry.get() or 22), self.user_entry.get(), self.pass_entry.get()
        try:
            ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()); ssh.connect(host, port=port, username=user, password=pw, timeout=10); RAM_DISK = "/dev/shm/sftp_studio/m3u_batch"; ssh.exec_command(f"mkdir -p \"{RAM_DISK}\""); batch_script_lines = ["#!/bin/bash", "exec > ~/wget_monitor.log 2>&1", f"RAM_DIR={shlex.quote(RAM_DISK)}", "echo '--- M3U Smart Sequential Download Started ---'", "mkdir -p \"$RAM_DIR\"", "cd \"$RAM_DIR\" || exit 1"]
            for idx, item in enumerate(selected_items):
                clean_name = re.sub(r'[\\\\/*?:"<>|]', "", item['name']).strip(); url = item['url']; ext = url.split("?")[0].split(".")[-1]
                if len(ext) > 4 or not ext.isalnum(): ext = "mkv"
                final_filename = f"{clean_name}.{ext}"
                
                # Smart Referer extraction
                referer = "https://google.com/"
                ref_match = re.search(r'(https?://[^/]+)', url)
                if ref_match: referer = ref_match.group(1) + "/"
                
                # Use a local file on server to store URL for safety
                u_file = f"$RAM_DIR/u_{idx}.txt"
                batch_script_lines.append(f"echo {shlex.quote(url)} > {u_file}")
                
                ua = "IPTVSmartersPlayer"
                item_path = item["path"]
                mkdir_cmd = f"echo {shlex.quote('[EXEC] mkdir -p ' + item_path)} && mkdir -p {shlex.quote(item_path)}"
                remote_output = f"{item_path.rstrip('/')}/{final_filename}"
                move_log = shlex.quote("[EXEC] mv %s to %s" % (final_filename, item_path))
                mv_cmd = f"echo {move_log} && mv \"$RAM_DIR/{final_filename}\" {shlex.quote(remote_output)}"
                auto_cmd = self.build_auto_transcode_enqueue(remote_output, f"m3u_{int(time.time() * 1000)}_{idx}", enabled=bool(item.get('section_transcode', False)))
                
                if "m3u8" in url.lower(): 
                    ffmpeg_m3u8 = f"ffmpeg -nostdin -hide_banner -loglevel error -protocol_whitelist file,http,https,tcp,tls,crypto -user_agent {shlex.quote(ua)} -headers {shlex.quote(f'Referer: {referer}')} -i \"$(cat {u_file})\" -c copy \"$RAM_DIR/{final_filename}\""
                    dl_cmd = f"echo {shlex.quote('[EXEC] ' + ffmpeg_m3u8)} && {ffmpeg_m3u8}"
                else:
                    # Golden Camouflage curl: Mimicking real media player behavior
                    headers = "-H 'Accept: */*' -H 'Accept-Language: en-US,en;q=0.9' -H 'Connection: keep-alive' --compressed --tcp-nodelay --keepalive-time 60"
                    dl_cmd = f"echo {shlex.quote('[EXEC] Golden Camouflage curl download...')} && curl -f -L -C - --retry 10 -A {shlex.quote(ua)} -e {shlex.quote(referer)} {headers} -o \"$RAM_DIR/{final_filename}\" \"$(cat {u_file})\""
                
                msg = f"Downloading [{idx+1}/{len(selected_items)}]: {clean_name}"
                after_download = f"if [ -f \"$RAM_DIR/{final_filename}\" ]; then {mv_cmd}; {auto_cmd}; fi" if auto_cmd else f"if [ -f \"$RAM_DIR/{final_filename}\" ]; then {mv_cmd}; fi"
                batch_script_lines.extend([f"echo {shlex.quote(msg)}", mkdir_cmd, dl_cmd, after_download, f"rm -f {u_file}"])
            batch_script_lines.extend(["echo '--- M3U Smart Sequential Download Finished ---'", f"rm -rf \"$RAM_DIR\""]); sftp = ssh.open_sftp(); script_path = f"/tmp/run_m3u_{int(time.time())}.sh"
            self.log(f"Executing M3U Download: {len(selected_items)} items (Check debug_commands.log for details)", is_cmd=True)
            local_debug_log(f"M3U SEQUENTIAL DOWNLOAD\nITEMS: {len(selected_items)}\nSCRIPT:\n" + "\n".join(batch_script_lines))
            # Add full command logging to the M3U script too
            batch_script_lines.insert(1, f"echo '--- NEW M3U BATCH STARTED ---' > ~/wget_monitor.log")
            batch_script_lines.insert(2, f"echo 'TOTAL ITEMS: {len(selected_items)}' >> ~/wget_monitor.log")
            with sftp.file(script_path, 'w') as f: f.write("\n".join(batch_script_lines))
            # Do not detach this script: the queue must wait until every
            # command in this M3U job has completed before taking the next job.
            stdin, stdout, stderr = ssh.exec_command(f"bash {script_path}")
            stdout.channel.recv_exit_status()
            sftp.close(); self.after(0, lambda: self.log(TRANSLATIONS[self.lang]["msg_m3u_start"])); ssh.close()
        except Exception as e: self.after(0, lambda e=e: self.log(TRANSLATIONS[self.lang]["msg_error"] + str(e)))
        finally: self.is_downloading = False

    def background_scheduler_loop(self):
        while True:
            time.sleep(60) # Check every minute
            p = self.current_profile.get()
            if p not in self.config: continue
            history = self.config[p].get("m3u_history", {})
            
            # Check all six independent groups.
            for group in ["series_1", "series_2", "series_3", "vod_1", "vod_2", "vod_3"]:
                paths_data = self.config[p].get(f"m3u_paths_{group}")
                if paths_data is None and group == "series_1": paths_data = self.config[p].get("m3u_paths_series", [])
                if paths_data is None and group == "vod_1": paths_data = self.config[p].get("m3u_paths_vod", [])
                for i, d in enumerate(paths_data or []):
                    if d.get("auto", False):
                        path_key = f"{group}_path_{i+1}"
                        meta = history.get(path_key) or history.get(f"{group.split('_')[0]}_path_{i+1}", {})
                        if not meta: continue
                        
                        last_check = meta.get("last_check_time", 0)
                        attempts = meta.get("attempts", 0)
                        interval_hours = int(d.get("interval", "12h").replace("h", ""))
                        
                        if time.time() - last_check > (interval_hours * 3600):
                            if attempts < 2:
                                self.log(f"Background Auto-Check: Starting for {group.replace('_', ' ').title()} Path {i+1} (Attempt {attempts+1}/2)...")
                                target_path = d.get("path")
                                host, port, user, pw = self.config[p].get("host"), int(self.config[p].get("port", 22)), self.config[p].get("user"), self.config[p].get("pass")
                                if host and target_path:
                                    threading.Thread(target=self.do_check_automated, args=(i, target_path, meta, host, port, user, pw, True, group.replace('_', ' ').title()), daemon=True).start()
                                    meta["last_check_time"] = time.time()
                                    meta["attempts"] = attempts + 1
                                    self.config[p]["m3u_history"][path_key] = meta
                                    with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(self.config, f, indent=4)
                            elif attempts == 2:
                                # Only update UI status if current mode matches
                                curr_ui_mode = self.m3u_path_mode.get().lower()
                                if mode == curr_ui_mode: self.update_path_status(i, "#ffc107")
                                
                                if time.time() - last_check > 86400: 
                                    meta["attempts"] = 0
                                    self.config[p]["m3u_history"][path_key] = meta
                                    if mode == curr_ui_mode: self.update_path_status(i, "gray")

if __name__ == "__main__": SFTPDownloaderApp().mainloop()
