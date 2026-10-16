# server.py
import os
import sqlite3
import subprocess
import json
import shlex
import signal
import time
import threading
import uuid
import shutil
import mimetypes
import re
import base64
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request, send_from_directory, send_file

app = Flask(__name__)
DB_FILE = "termux_gui.db"
TERMUX_PREFIX = "/data/data/com.termux/files/usr"
TERMUX_BIN = f"{TERMUX_PREFIX}/bin"
TERMUX_HOME = "/data/data/com.termux/files/home"
KALI_PREFIX = f"{TERMUX_PREFIX}/opt/kali-arm64"
KALI_BIN = f"{KALI_PREFIX}/bin"
KALI_ROOT = f"{KALI_PREFIX}/root"
XFCE_CONFIG_DIR = os.path.expanduser("~/.config/xfce4")

# Media file extensions for viewer
IMAGE_EXTS = {'jpg','jpeg','png','gif','webp','svg','bmp','ico','tiff','tif','heic','heif','avif'}
VIDEO_EXTS = {'mp4','mkv','avi','mov','webm','flv','wmv','m4v','3gp','mpg','mpeg','ogv'}
AUDIO_EXTS = {'mp3','wav','ogg','flac','m4a','aac','opus','wma','amr','mid','midi'}

# Commands that are long-running (dev servers) — use extended timeout
LONG_RUNNING_COMMANDS = {
    'npm', 'npx', 'yarn', 'pnpm', 'node', 'python', 'python3', 'pip', 'pip3',
    'flask', 'django-admin', 'uvicorn', 'gunicorn', 'hypercorn',
    'vite', 'webpack', 'next', 'nuxt', 'gatsby', 'parcel', 'rollup',
    'go', 'cargo', 'rustc', 'ruby', 'rails', 'bundle', 'php', 'composer',
    'deno', 'bun', 'gradle', 'mvn', 'dotnet', 'serve', 'http-server',
}

# Security: allowed base directories for file operations
ALLOWED_BASES = [
    TERMUX_HOME,
    "/sdcard",
    "/storage/emulated/0",
    "/data/data/com.termux/files",
    "/tmp",
    os.path.expanduser("~"),
]

# ============================================================
# DATABASE INITIALIZATION
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            args TEXT,
            output TEXT,
            status TEXT,
            executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            position INTEGER DEFAULT 0,
            wallpaper TEXT,
            accent_color TEXT DEFAULT '#38bdf8',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS installed_apps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            icon TEXT,
            category TEXT DEFAULT 'Utilities',
            executable TEXT NOT NULL,
            args TEXT DEFAULT '',
            terminal BOOLEAN DEFAULT 0,
            desktop_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            path TEXT NOT NULL,
            icon TEXT DEFAULT 'fa-folder',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS clipboard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS editor_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            editor TEXT NOT NULL,
            content TEXT,
            cursor_line INTEGER DEFAULT 1,
            cursor_col INTEGER DEFAULT 1,
            dirty INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS recent_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            editor TEXT,
            accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # NEW: Window state persistence
    c.execute('''
        CREATE TABLE IF NOT EXISTS window_states (
            app_id TEXT PRIMARY KEY,
            x INTEGER,
            y INTEGER,
            width INTEGER,
            height INTEGER,
            state TEXT DEFAULT 'normal',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # NEW: Uploaded wallpapers
    c.execute('''
        CREATE TABLE IF NOT EXISTS custom_wallpapers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            data_uri TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # NEW: README detection cache
    c.execute('''
        CREATE TABLE IF NOT EXISTS project_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            dev_command TEXT,
            dev_port TEXT,
            has_readme INTEGER DEFAULT 0,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    defaults = {
        'theme': 'kali-dark',
        'wallpaper': 'solid-dark',
        'accent_color': '#38bdf8',
        'taskbar_position': 'bottom',
        'window_animations': 'true',
        'terminal_font': 'Fira Code',
        'terminal_font_size': '13',
        'auto_hide_taskbar': 'false',
        'desktop_icons': 'true',
        'compositor': 'true',
        'workspace_count': '4',
        'default_editor': 'ask',
        'confirm_delete': 'true',
        'show_hidden_files': 'false',
        'editor_font_size': '13',
        'editor_tab_size': '4',
        'editor_wrap': 'true',
        'restore_window_state': 'true',
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO preferences (key, value) VALUES (?, ?)", (k, v))
    
    c.execute("SELECT COUNT(*) FROM workspaces")
    if c.fetchone()[0] == 0:
        for i, name in enumerate(['Main', 'Web', 'Dev', 'Media']):
            c.execute("INSERT INTO workspaces (name, position) VALUES (?, ?)", (name, i))
    
    conn.commit()
    conn.close()

# ============================================================
# PATH SECURITY
# ============================================================
def is_path_allowed(path):
    try:
        real = os.path.realpath(os.path.expanduser(path))
        for base in ALLOWED_BASES:
            real_base = os.path.realpath(base)
            if real == real_base or real.startswith(real_base + os.sep):
                return True
        return False
    except Exception:
        return False

def safe_path(path):
    if not path:
        return None
    p = os.path.expanduser(path)
    if not is_path_allowed(p):
        return None
    return p

# ============================================================
# TERMUX / KALI COMMAND DISCOVERY
# ============================================================
def get_installed_termux_commands():
    commands = set()
    search_dirs = [TERMUX_BIN, KALI_BIN, "/system/bin", "/system/xbin"]
    for d in search_dirs:
        if os.path.isdir(d):
            try:
                for fname in os.listdir(d):
                    if (fname.startswith("termux-") or fname in [
                        "monkey", "am", "pm", "xfce4-session", "xfce4-terminal",
                        "xfce4-panel", "nano", "vim", "vi", "emacs", "micro",
                        "neovim", "nvim", "code", "thunar", "mousepad"
                    ]):
                        full = os.path.join(d, fname)
                        if os.path.isfile(full) and os.access(full, os.X_OK):
                            commands.add(fname)
            except PermissionError:
                continue
    return sorted(list(commands))

def get_kali_desktop_environments():
    des = []
    kali_paths = [
        "/usr/bin/xfce4-session", "/usr/bin/startxfce4", "/usr/bin/xfce4-terminal",
        "/usr/bin/xfce4-panel", "/usr/bin/xfdesktop", "/usr/bin/xfwm4",
        "/usr/bin/xfsettingsd", "/usr/bin/xfce4-power-manager",
        "/usr/bin/xfce4-screenshooter", "/usr/bin/thunar", "/usr/bin/mousepad",
        "/usr/bin/ristretto", "/usr/bin/parole", "/usr/bin/xfce4-taskmanager",
    ]
    for p in kali_paths:
        if os.path.exists(p):
            des.append(os.path.basename(p))
    return des

def get_available_editors():
    editors = []
    candidates = [
        ("nano", "Nano", "fa-feather"),
        ("vim", "Vim", "fa-terminal"),
        ("nvim", "Neovim", "fa-terminal"),
        ("vi", "Vi", "fa-terminal"),
        ("emacs", "Emacs", "fa-terminal"),
        ("micro", "Micro", "fa-feather"),
        ("code", "VS Code", "fa-code"),
        ("mousepad", "Mousepad", "fa-file-pen"),
    ]
    for cmd, name, icon in candidates:
        if os.path.exists(os.path.join(TERMUX_BIN, cmd)) and os.access(os.path.join(TERMUX_BIN, cmd), os.X_OK):
            editors.append({"cmd": cmd, "name": name, "icon": icon, "source": "termux"})
        elif os.path.exists(os.path.join(KALI_BIN, cmd)) and os.access(os.path.join(KALI_BIN, cmd), os.X_OK):
            editors.append({"cmd": cmd, "name": name, "icon": icon, "source": "kali"})
        elif shutil.which(cmd):
            editors.append({"cmd": cmd, "name": name, "icon": icon, "source": "system"})
    return editors

# ============================================================
# EXECUTION ENGINE
# ============================================================
def build_env():
    env = os.environ.copy()
    current_path = env.get("PATH", "")
    system_paths = "/system/bin:/system/xbin:/vendor/bin"
    env["PATH"] = f"{TERMUX_BIN}:{current_path}:{system_paths}"
    env["PREFIX"] = TERMUX_PREFIX
    env["LD_LIBRARY_PATH"] = f"{TERMUX_PREFIX}/lib"
    env["TERM"] = "xterm-256color"
    env["DISPLAY"] = ":0"
    if os.path.isdir(KALI_ROOT):
        env["KALI_PREFIX"] = KALI_PREFIX
        env["KALI_HOME"] = KALI_ROOT
        env["PATH"] = f"{KALI_BIN}:{env['PATH']}"
    return env

def is_long_running(command):
    """Detect dev/server commands that need extended timeout."""
    if not command:
        return False
    base = os.path.basename(command.strip().split()[0]) if command.strip() else ''
    return base in LONG_RUNNING_COMMANDS

def execute_command(command, args, timeout=30, cwd=None):
    if not command:
        return {"status": "error", "output": "No command provided."}
    
    full_cmd = f"{command} {args}".strip() if args else command
    env = build_env()
    
    # Auto-extend timeout for long-running dev commands (5 minutes)
    if is_long_running(command) or is_long_running(full_cmd):
        timeout = max(timeout, 300)
    
    if cwd and not os.path.isdir(os.path.expanduser(cwd)):
        cwd = None
    
    try:
        res = subprocess.run(
            full_cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd
        )
        output = res.stdout if res.returncode == 0 else (res.stderr or res.stdout or "Command executed successfully with no output.")
        status = "success" if res.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        status = "error"
        output = (
            "⚠️ Command Timed Out\n\n"
            "Troubleshooting Steps:\n"
            "1. Ensure 'pkg install termux-api' is installed.\n"
            "2. Ensure the 'Termux:API' companion app is installed from F-Droid.\n"
            "3. Grant permissions (Contacts, SMS, Phone) to Termux:API.\n"
            "4. Disable battery optimization for Termux:API.\n"
            "5. For Kali XFCE4, ensure 'startxfce4' is available in chroot.\n"
            "6. Dev/server commands may run indefinitely — check the terminal output for activity."
        )
    except Exception as e:
        output = str(e)
        status = "error"
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO execution_logs (command, args, output, status) VALUES (?, ?, ?, ?)",
        (command, args, output, status)
    )
    conn.commit()
    conn.close()
    
    return {"status": status, "output": output, "command": command}

# ============================================================
# FILE OPERATIONS ENGINE
# ============================================================
def fs_stat(path):
    try:
        st = os.lstat(path)
        is_link = os.path.islink(path)
        target = os.path.realpath(path) if is_link else None
        return {
            "name": os.path.basename(path),
            "path": path,
            "is_dir": os.path.isdir(path),
            "is_file": os.path.isfile(path),
            "is_link": is_link,
            "link_target": target,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "ctime": datetime.fromtimestamp(st.st_ctime).isoformat(),
            "atime": datetime.fromtimestamp(st.st_atime).isoformat(),
            "perms": oct(st.st_mode)[-3:],
            "uid": st.st_uid,
            "gid": st.st_gid,
            "mode": st.st_mode,
        }
    except (OSError, PermissionError) as e:
        return None

def fs_list(path, show_hidden=False):
    path = safe_path(path) or os.path.expanduser("~")
    if not os.path.isdir(path):
        return {"status": "error", "output": f"Not a directory: {path}"}
    try:
        items = []
        for entry in os.listdir(path):
            if not show_hidden and entry.startswith('.'):
                continue
            full = os.path.join(path, entry)
            info = fs_stat(full)
            if info:
                items.append(info)
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        parent = os.path.dirname(path) if path != "/" else None
        return {
            "status": "success",
            "path": path,
            "parent": parent,
            "items": items
        }
    except PermissionError:
        return {"status": "error", "output": f"Permission denied: {path}"}
    except Exception as e:
        return {"status": "error", "output": str(e)}

def fs_read(path, max_size=5*1024*1024):
    p = safe_path(path)
    if not p:
        return {"status": "error", "output": "Path not allowed."}
    if not os.path.isfile(p):
        return {"status": "error", "output": "Not a file."}
    size = os.path.getsize(p)
    if size > max_size:
        return {"status": "error", "output": f"File too large ({size} bytes). Max: {max_size}."}
    mime, _ = mimetypes.guess_type(p)
    try:
        with open(p, 'rb') as f:
            raw = f.read(8192)
        is_binary = b'\x00' in raw
    except Exception as e:
        return {"status": "error", "output": str(e)}
    if is_binary:
        return {"status": "error", "output": "Binary file cannot be opened in text editor.", "binary": True}
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return {"status": "success", "content": content, "path": p, "size": size, "mime": mime}
    except Exception as e:
        return {"status": "error", "output": str(e)}

def fs_write(path, content):
    p = safe_path(path)
    if not p:
        return {"status": "error", "output": "Path not allowed."}
    try:
        parent = os.path.dirname(p)
        if not os.path.isdir(parent):
            return {"status": "error", "output": f"Parent directory does not exist: {parent}"}
        with open(p, 'w', encoding='utf-8') as f:
            f.write(content)
        return {"status": "success", "output": f"Saved {p}", "path": p}
    except Exception as e:
        return {"status": "error", "output": str(e)}

def fs_create(path, is_dir=False, content=""):
    p = safe_path(path)
    if not p:
        return {"status": "error", "output": "Path not allowed."}
    if os.path.exists(p):
        return {"status": "error", "output": f"Already exists: {p}"}
    try:
        parent = os.path.dirname(p)
        if not os.path.isdir(parent):
            return {"status": "error", "output": f"Parent directory does not exist: {parent}"}
        if is_dir:
            os.makedirs(p, exist_ok=False)
            return {"status": "success", "output": f"Created directory: {p}", "path": p}
        else:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(content)
            return {"status": "success", "output": f"Created file: {p}", "path": p}
    except Exception as e:
        return {"status": "error", "output": str(e)}

def fs_delete(path, recursive=True):
    p = safe_path(path)
    if not p:
        return {"status": "error", "output": "Path not allowed."}
    if not os.path.exists(p):
        return {"status": "error", "output": f"Not found: {p}"}
    if p in ["/", "/sdcard", TERMUX_HOME, "/data/data/com.termux/files"]:
        return {"status": "error", "output": "Refusing to delete critical directory."}
    try:
        if os.path.isdir(p):
            if recursive:
                shutil.rmtree(p)
            else:
                os.rmdir(p)
            return {"status": "success", "output": f"Deleted directory: {p}"}
        else:
            os.remove(p)
            return {"status": "success", "output": f"Deleted file: {p}"}
    except Exception as e:
        return {"status": "error", "output": str(e)}

def fs_rename(old_path, new_path):
    old = safe_path(old_path)
    new = safe_path(new_path)
    if not old or not new:
        return {"status": "error", "output": "Path not allowed."}
    if not os.path.exists(old):
        return {"status": "error", "output": f"Not found: {old}"}
    if os.path.exists(new):
        return {"status": "error", "output": f"Destination exists: {new}"}
    try:
        os.rename(old, new)
        return {"status": "success", "output": f"Renamed to: {new}", "path": new}
    except Exception as e:
        return {"status": "error", "output": str(e)}

def fs_copy(src, dst, recursive=True):
    s = safe_path(src)
    d = safe_path(dst)
    if not s or not d:
        return {"status": "error", "output": "Path not allowed."}
    if not os.path.exists(s):
        return {"status": "error", "output": f"Source not found: {s}"}
    if os.path.exists(d):
        return {"status": "error", "output": f"Destination exists: {d}"}
    try:
        if os.path.isdir(s):
            if not recursive:
                return {"status": "error", "output": "Cannot copy directory without recursive."}
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
        return {"status": "success", "output": f"Copied to: {d}", "path": d}
    except Exception as e:
        return {"status": "error", "output": str(e)}

def fs_move(src, dst):
    return fs_rename(src, dst)

def fs_mkdir(path):
    return fs_create(path, is_dir=True)

def fs_touch(path):
    return fs_create(path, is_dir=False, content="")

def fs_file_info(path):
    p = safe_path(path)
    if not p:
        return {"status": "error", "output": "Path not allowed."}
    info = fs_stat(p)
    if not info:
        return {"status": "error", "output": "Cannot stat file."}
    if info["is_file"] and info["size"] < 100000:
        mime, _ = mimetypes.guess_type(p)
        info["mime"] = mime
        try:
            with open(p, 'rb') as f:
                raw = f.read(512)
            info["is_binary"] = b'\x00' in raw
        except Exception:
            info["is_binary"] = True
    return {"status": "success", "info": info}

def fs_search(path, query, max_results=200):
    p = safe_path(path)
    if not p:
        return {"status": "error", "output": "Path not allowed."}
    if not os.path.isdir(p):
        return {"status": "error", "output": "Not a directory."}
    results = []
    query_lower = query.lower()
    try:
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.git')]
            for name in files + dirs:
                if query_lower in name.lower():
                    full = os.path.join(root, name)
                    info = fs_stat(full)
                    if info:
                        results.append(info)
                    if len(results) >= max_results:
                        return {"status": "success", "results": results, "truncated": True}
    except Exception as e:
        return {"status": "error", "output": str(e)}
    return {"status": "success", "results": results, "truncated": False}

# ============================================================
# MEDIA SERVING
# ============================================================
def get_media_type(path):
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    if ext in IMAGE_EXTS: return 'image'
    if ext in VIDEO_EXTS: return 'video'
    if ext in AUDIO_EXTS: return 'audio'
    return None

def fs_media_info(path):
    """Return media metadata for serving."""
    p = safe_path(path)
    if not p or not os.path.isfile(p):
        return {"status": "error", "output": "Not a file or path not allowed."}
    mt = get_media_type(p)
    if not mt:
        return {"status": "error", "output": "Not a supported media file."}
    size = os.path.getsize(p)
    mime, _ = mimetypes.guess_type(p)
    return {
        "status": "success",
        "path": p,
        "name": os.path.basename(p),
        "type": mt,
        "mime": mime or f"{mt}/*",
        "size": size,
        "url": f"/api/fs/media?path={shlex.quote(p)}" if False else f"/api/fs/media?path={p}"
    }

# ============================================================
# PROJECT README DETECTION
# ============================================================
README_NAMES = ['README.md', 'readme.md', 'README.MD', 'README.txt', 'README',
                'Readme.md', 'README.rst', 'readme.txt', 'Readme.txt']

def find_readme(folder):
    """Find README file in a folder."""
    p = safe_path(folder)
    if not p or not os.path.isdir(p):
        return None
    try:
        entries = os.listdir(p)
    except Exception:
        return None
    # Exact matches first
    for rn in README_NAMES:
        if rn in entries:
            return os.path.join(p, rn)
    # Case-insensitive fallback
    for e in entries:
        if e.lower().startswith('readme') and e.lower().endswith(('.md', '.txt', '.rst', '')):
            return os.path.join(p, e)
    return None

def detect_dev_command(folder):
    """
    Inspect a project folder for common dev configurations.
    Returns dict with keys: dev_command, dev_port, reasons
    """
    p = safe_path(folder)
    if not p or not os.path.isdir(p):
        return None
    result = {"dev_command": None, "dev_port": None, "reasons": [], "files": []}
    
    def _read_json(fname):
        try:
            with open(os.path.join(p, fname), 'r', encoding='utf-8', errors='replace') as f:
                return json.load(f)
        except Exception:
            return None
    
    def _read_text(fname, limit=8192):
        try:
            with open(os.path.join(p, fname), 'r', encoding='utf-8', errors='replace') as f:
                return f.read(limit)
        except Exception:
            return None
    
    try:
        entries = os.listdir(p)
    except Exception:
        return result
    
    # --- Node.js / npm / yarn / pnpm ---
    if 'package.json' in entries:
        result["files"].append('package.json')
        pkg = _read_json('package.json') or {}
        scripts = pkg.get('scripts', {}) or {}
        for key in ('dev', 'start:dev', 'serve', 'develop'):
            if key in scripts:
                result["dev_command"] = f"npm run {key}"
                result["reasons"].append(f"package.json has scripts.{key} = {scripts[key]!r}")
                break
        if not result["dev_command"]:
            for key in ('start', 'serve'):
                if key in scripts:
                    result["dev_command"] = f"npm run {key}"
                    result["reasons"].append(f"package.json has scripts.{key} = {scripts[key]!r}")
                    break
        if not result["dev_command"]:
            result["dev_command"] = "npm start"
            result["reasons"].append("package.json detected — assuming `npm start`")
        result["dev_port"] = _guess_port_from_scripts(scripts)
    
    # --- Python ---
    elif any(f in entries for f in ('requirements.txt', 'pyproject.toml', 'setup.py', 'Pipfile', 'manage.py', 'app.py', 'main.py')):
        for f in ('requirements.txt', 'pyproject.toml', 'setup.py', 'Pipfile'):
            if f in entries:
                result["files"].append(f)
        if 'manage.py' in entries:
            result["dev_command"] = "python manage.py runserver"
            result["reasons"].append("Django manage.py detected")
            result["dev_port"] = "8000"
        elif 'app.py' in entries:
            app_txt = _read_text('app.py') or ''
            if 'flask' in app_txt.lower() or 'Flask' in app_txt:
                result["dev_command"] = "flask run --host=0.0.0.0"
                result["reasons"].append("Flask app.py detected")
                result["dev_port"] = "5000"
            elif 'fastapi' in app_txt.lower() or 'uvicorn' in app_txt.lower():
                result["dev_command"] = "uvicorn app:app --host 0.0.0.0 --reload"
                result["reasons"].append("FastAPI app.py detected")
                result["dev_port"] = "8000"
            else:
                result["dev_command"] = "python app.py"
                result["reasons"].append("Python app.py detected")
        elif 'main.py' in entries:
            result["dev_command"] = "python main.py"
            result["reasons"].append("Python main.py detected")
    
    # --- Go ---
    if 'go.mod' in entries:
        result["files"].append('go.mod')
        result["dev_command"] = result["dev_command"] or "go run ."
        result["reasons"].append("go.mod detected")
    
    # --- Rust ---
    if 'Cargo.toml' in entries:
        result["files"].append('Cargo.toml')
        result["dev_command"] = result["dev_command"] or "cargo run"
        result["reasons"].append("Cargo.toml detected")
    
    # --- Docker ---
    if 'docker-compose.yml' in entries or 'docker-compose.yaml' in entries:
        result["files"].append('docker-compose.yml')
        result["dev_command"] = result["dev_command"] or "docker-compose up"
        result["reasons"].append("docker-compose detected")
    if 'Dockerfile' in entries:
        result["files"].append('Dockerfile')
    
    return result

def _guess_port_from_scripts(scripts):
    """Scan script values for common dev ports."""
    if not isinstance(scripts, dict):
        return None
    for v in scripts.values():
        if not isinstance(v, str):
            continue
        m = re.search(r'(?:--port[=\s]+|-p\s+)(\d{2,5})', v)
        if m:
            return m.group(1)
        m = re.search(r'PORT=(\d{2,5})', v)
        if m:
            return m.group(1)
        m = re.search(r':(\d{4,5})\b', v)
        if m:
            return m.group(1)
    return None

# ============================================================
# EDITOR SESSION MANAGEMENT
# ============================================================
def record_recent_file(path, editor=None):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO recent_files (path, editor, accessed_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (path, editor))
        c.execute("DELETE FROM recent_files WHERE id NOT IN (SELECT id FROM recent_files ORDER BY accessed_at DESC LIMIT 50)")
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_recent_files(limit=20):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT path, editor, accessed_at FROM recent_files ORDER BY accessed_at DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"path": r[0], "editor": r[1], "accessed_at": r[2]} for r in rows]

# ============================================================
# XFCE4 DESKTOP SESSION MANAGEMENT
# ============================================================
class XFCE4Session:
    def __init__(self):
        self.processes = {}
        self.lock = threading.Lock()
    
    def start(self, session_type="xfce4"):
        with self.lock:
            sid = str(uuid.uuid4())[:8]
            self.processes[sid] = {
                "type": session_type,
                "started": datetime.now().isoformat(),
                "status": "running"
            }
            return sid
    
    def stop(self, sid):
        with self.lock:
            if sid in self.processes:
                self.processes[sid]["status"] = "stopped"
                return True
            return False
    
    def list_sessions(self):
        with self.lock:
            return list(self.processes.values())

xfce_session = XFCE4Session()

# ============================================================
# API ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/commands", methods=["GET"])
def api_commands():
    return jsonify({
        "commands": get_installed_termux_commands(),
        "kali_de": get_kali_desktop_environments(),
        "editors": get_available_editors()
    })

@app.route("/api/execute", methods=["POST"])
def api_execute():
    data = request.json or {}
    command = data.get("command", "").strip()
    args = data.get("args", "").strip()
    timeout = int(data.get("timeout", 30))
    cwd = data.get("cwd")
    result = execute_command(command, args, timeout=timeout, cwd=cwd)
    return jsonify(result)

@app.route("/api/logs", methods=["GET"])
def api_logs():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, command, args, output, status, executed_at FROM execution_logs ORDER BY id DESC LIMIT 50")
    rows = c.fetchall()
    conn.close()
    logs = [{
        "id": r[0], "command": r[1], "args": r[2],
        "output": r[3], "status": r[4], "executed_at": r[5]
    } for r in rows]
    return jsonify({"logs": logs})

@app.route("/api/preferences", methods=["GET"])
def api_get_preferences():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT key, value FROM preferences")
    prefs = {k: v for k, v in c.fetchall()}
    conn.close()
    return jsonify(prefs)

@app.route("/api/preferences", methods=["POST"])
def api_set_preferences():
    data = request.json or {}
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for k, v in data.items():
        c.execute("INSERT OR REPLACE INTO preferences (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/workspaces", methods=["GET"])
def api_workspaces():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, position, wallpaper, accent_color FROM workspaces ORDER BY position")
    rows = c.fetchall()
    conn.close()
    return jsonify({"workspaces": [{"id": r[0], "name": r[1], "position": r[2], "wallpaper": r[3], "accent_color": r[4]} for r in rows]})

@app.route("/api/workspaces", methods=["POST"])
def api_create_workspace():
    data = request.json or {}
    name = data.get("name", "Workspace")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM workspaces")
    pos = c.fetchone()[0]
    c.execute("INSERT INTO workspaces (name, position) VALUES (?, ?)", (name, pos))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/workspaces/<int:wid>", methods=["DELETE"])
def api_delete_workspace(wid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM workspaces WHERE id = ?", (wid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/apps", methods=["GET"])
def api_apps():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, app_id, name, icon, category, executable, args, terminal FROM installed_apps ORDER BY name")
    rows = c.fetchall()
    conn.close()
    return jsonify({"apps": [{
        "id": r[0], "app_id": r[1], "name": r[2], "icon": r[3],
        "category": r[4], "executable": r[5], "args": r[6], "terminal": bool(r[7])
    } for r in rows]})

@app.route("/api/apps", methods=["POST"])
def api_register_app():
    data = request.json or {}
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO installed_apps 
                 (app_id, name, icon, category, executable, args, terminal) 
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (data.get("app_id"), data.get("name"), data.get("icon", "fa-cube"),
               data.get("category", "Utilities"), data.get("executable"),
               data.get("args", ""), 1 if data.get("terminal") else 0))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/apps/<app_id>", methods=["DELETE"])
def api_delete_app(app_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM installed_apps WHERE app_id = ?", (app_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/bookmarks", methods=["GET"])
def api_bookmarks():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, label, path, icon FROM bookmarks ORDER BY label")
    rows = c.fetchall()
    conn.close()
    return jsonify({"bookmarks": [{"id": r[0], "label": r[1], "path": r[2], "icon": r[3]} for r in rows]})

@app.route("/api/bookmarks", methods=["POST"])
def api_add_bookmark():
    data = request.json or {}
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO bookmarks (label, path, icon) VALUES (?, ?, ?)",
              (data.get("label"), data.get("path"), data.get("icon", "fa-folder")))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/bookmarks/<int:bid>", methods=["DELETE"])
def api_delete_bookmark(bid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM bookmarks WHERE id = ?", (bid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ============================================================
# FILE SYSTEM API
# ============================================================
@app.route("/api/fs/list", methods=["POST"])
def api_fs_list():
    data = request.json or {}
    path = data.get("path", os.path.expanduser("~"))
    show_hidden = data.get("show_hidden", False)
    result = fs_list(path, show_hidden=show_hidden)
    return jsonify(result)

@app.route("/api/fs/info", methods=["POST"])
def api_fs_info():
    data = request.json or {}
    path = data.get("path")
    result = fs_file_info(path)
    return jsonify(result)

@app.route("/api/fs/read", methods=["POST"])
def api_fs_read():
    data = request.json or {}
    path = data.get("path")
    result = fs_read(path)
    return jsonify(result)

@app.route("/api/fs/write", methods=["POST"])
def api_fs_write():
    data = request.json or {}
    path = data.get("path")
    content = data.get("content", "")
    result = fs_write(path, content)
    if result.get("status") == "success":
        record_recent_file(path)
    return jsonify(result)

@app.route("/api/fs/create", methods=["POST"])
def api_fs_create():
    data = request.json or {}
    path = data.get("path")
    is_dir = data.get("is_dir", False)
    content = data.get("content", "")
    result = fs_create(path, is_dir=is_dir, content=content)
    return jsonify(result)

@app.route("/api/fs/delete", methods=["POST"])
def api_fs_delete():
    data = request.json or {}
    path = data.get("path")
    recursive = data.get("recursive", True)
    result = fs_delete(path, recursive=recursive)
    return jsonify(result)

@app.route("/api/fs/rename", methods=["POST"])
def api_fs_rename():
    data = request.json or {}
    old = data.get("old_path")
    new = data.get("new_path")
    result = fs_rename(old, new)
    return jsonify(result)

@app.route("/api/fs/copy", methods=["POST"])
def api_fs_copy():
    data = request.json or {}
    src = data.get("src")
    dst = data.get("dst")
    recursive = data.get("recursive", True)
    result = fs_copy(src, dst, recursive=recursive)
    return jsonify(result)

@app.route("/api/fs/move", methods=["POST"])
def api_fs_move():
    data = request.json or {}
    src = data.get("src")
    dst = data.get("dst")
    result = fs_move(src, dst)
    return jsonify(result)

@app.route("/api/fs/mkdir", methods=["POST"])
def api_fs_mkdir():
    data = request.json or {}
    path = data.get("path")
    result = fs_mkdir(path)
    return jsonify(result)

@app.route("/api/fs/touch", methods=["POST"])
def api_fs_touch():
    data = request.json or {}
    path = data.get("path")
    result = fs_touch(path)
    return jsonify(result)

@app.route("/api/fs/search", methods=["POST"])
def api_fs_search():
    data = request.json or {}
    path = data.get("path", os.path.expanduser("~"))
    query = data.get("query", "")
    if not query:
        return jsonify({"status": "error", "output": "No query."})
    result = fs_search(path, query)
    return jsonify(result)

@app.route("/api/fs/download", methods=["POST"])
def api_fs_download():
    data = request.json or {}
    path = data.get("path")
    p = safe_path(path)
    if not p or not os.path.isfile(p):
        return jsonify({"status": "error", "output": "Not a file or path not allowed."})
    return send_file(p, as_attachment=True)

@app.route("/api/fs/recent", methods=["GET"])
def api_fs_recent():
    return jsonify({"recent": get_recent_files()})

# NEW: Media serving endpoint — supports range requests for video/audio
@app.route("/api/fs/media", methods=["GET"])
def api_fs_media():
    path = request.args.get("path", "")
    p = safe_path(path)
    if not p or not os.path.isfile(p):
        return jsonify({"status": "error", "output": "Not a file or path not allowed."}), 404
    mime, _ = mimetypes.guess_type(p)
    if not mime:
        mt = get_media_type(p)
        mime = f"{mt}/*" if mt else "application/octet-stream"
    # send_file handles range requests automatically with conditional=True
    return send_file(p, mimetype=mime, conditional=True)

# NEW: Media info endpoint
@app.route("/api/fs/media_info", methods=["POST"])
def api_fs_media_info():
    data = request.json or {}
    path = data.get("path")
    result = fs_media_info(path)
    return jsonify(result)

# NEW: Save uploaded media (for wallpaper upload / file upload from device)
@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Accept multipart upload or base64 JSON upload, save into ~/uploads/."""
    try:
        target_dir = os.path.join(TERMUX_HOME, "uploads")
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:
        return jsonify({"status": "error", "output": f"Cannot create uploads dir: {e}"})
    
    saved_path = None
    
    if 'file' in request.files:
        f = request.files['file']
        if f.filename:
            safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', f.filename)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            saved_path = os.path.join(target_dir, f"{ts}_{safe_name}")
            f.save(saved_path)
    else:
        data = request.get_json(silent=True) or {}
        name = data.get('name', 'upload.bin')
        content_b64 = data.get('content_b64', '')
        if content_b64:
            safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', name)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            saved_path = os.path.join(target_dir, f"{ts}_{safe_name}")
            try:
                raw = base64.b64decode(content_b64.split(',')[-1] if ',' in content_b64 else content_b64)
                with open(saved_path, 'wb') as f:
                    f.write(raw)
            except Exception as e:
                return jsonify({"status": "error", "output": f"Decode failed: {e}"})
    
    if not saved_path:
        return jsonify({"status": "error", "output": "No file provided."})
    
    return jsonify({"status": "success", "path": saved_path, "output": f"Saved to {saved_path}"})

# ============================================================
# PROJECT DETECTION API (README)
# ============================================================
@app.route("/api/project/detect", methods=["POST"])
def api_project_detect():
    data = request.json or {}
    folder = data.get("path", "")
    p = safe_path(folder)
    if not p or not os.path.isdir(p):
        return jsonify({"status": "error", "output": "Not a directory or path not allowed."})
    
    readme_path = find_readme(p)
    dev_info = detect_dev_command(p)
    
    # Cache result
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO project_cache 
                     (path, dev_command, dev_port, has_readme, detected_at)
                     VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)''',
                  (p, (dev_info or {}).get('dev_command'), (dev_info or {}).get('dev_port'),
                   1 if readme_path else 0))
        conn.commit()
        conn.close()
    except Exception:
        pass
    
    return jsonify({
        "status": "success",
        "folder": p,
        "readme": readme_path,
        "readme_name": os.path.basename(readme_path) if readme_path else None,
        "dev": dev_info,
    })

@app.route("/api/project/read_readme", methods=["POST"])
def api_project_read_readme():
    data = request.json or {}
    path = data.get("path")
    p = safe_path(path)
    if not p or not os.path.isfile(p):
        return jsonify({"status": "error", "output": "README not found."})
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(200000)
        return jsonify({"status": "success", "content": content, "path": p, "name": os.path.basename(p)})
    except Exception as e:
        return jsonify({"status": "error", "output": str(e)})

# ============================================================
# WINDOW STATE PERSISTENCE API
# ============================================================
@app.route("/api/window_states", methods=["GET"])
def api_window_states():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT app_id, x, y, width, height, state FROM window_states")
    rows = c.fetchall()
    conn.close()
    return jsonify({"states": {r[0]: {
        "x": r[1], "y": r[2], "width": r[3], "height": r[4], "state": r[5]
    } for r in rows}})

@app.route("/api/window_states", methods=["POST"])
def api_save_window_state():
    data = request.json or {}
    app_id = data.get("app_id")
    if not app_id:
        return jsonify({"status": "error", "output": "app_id required"})
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO window_states 
                 (app_id, x, y, width, height, state, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)''',
              (app_id, data.get("x"), data.get("y"), data.get("width"),
               data.get("height"), data.get("state", "normal")))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/window_states/<app_id>", methods=["DELETE"])
def api_delete_window_state(app_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM window_states WHERE app_id = ?", (app_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ============================================================
# WALLPAPER API (custom uploaded + solid dark)
# ============================================================
@app.route("/api/wallpapers", methods=["GET"])
def api_wallpapers():
    """Return only solid dark default + user-uploaded wallpapers. No external URLs."""
    wallpapers = [
        {"id": "solid-dark", "name": "Solid Dark", "url": "", "type": "solid", "color": "#090d16"},
        {"id": "solid-black", "name": "Solid Black", "url": "", "type": "solid", "color": "#000000"},
        {"id": "solid-navy", "name": "Solid Navy", "url": "", "type": "solid", "color": "#0a0e1a"},
        {"id": "solid-deep", "name": "Solid Deep", "url": "", "type": "solid", "color": "#0d0518"},
    ]
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, data_uri FROM custom_wallpapers ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    for r in rows:
        wallpapers.append({
            "id": f"custom-{r[0]}",
            "name": r[1],
            "url": r[2],  # data URI
            "type": "custom"
        })
    return jsonify({"wallpapers": wallpapers})

@app.route("/api/wallpapers/upload", methods=["POST"])
def api_upload_wallpaper():
    """Accept a base64 data URI image and store it."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "Custom Wallpaper").strip()[:100]
    data_uri = data.get("data_uri", "")
    if not data_uri or not data_uri.startswith("data:image/"):
        return jsonify({"status": "error", "output": "Invalid image data URI."})
    # Size guard (max ~8MB)
    if len(data_uri) > 8 * 1024 * 1024 * 1.4:
        return jsonify({"status": "error", "output": "Image too large (max ~8MB)."})
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO custom_wallpapers (name, data_uri) VALUES (?, ?)", (name, data_uri))
    conn.commit()
    wid = c.lastrowid
    conn.close()
    return jsonify({"status": "success", "id": f"custom-{wid}", "name": name})

@app.route("/api/wallpapers/<int:wid>", methods=["DELETE"])
def api_delete_wallpaper(wid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM custom_wallpapers WHERE id = ?", (wid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/themes", methods=["GET"])
def api_themes():
    themes = [
        {"id": "kali-dark", "name": "Kali Dark", "accent": "#38bdf8", "bg": "#090d16", "card": "#131c2e"},
        {"id": "kali-blue", "name": "Kali Blue", "accent": "#1e90ff", "bg": "#0a0e1a", "card": "#0f1a2e"},
        {"id": "matrix", "name": "Matrix Green", "accent": "#4af626", "bg": "#000000", "card": "#0a1a0a"},
        {"id": "blood", "name": "Blood Red", "accent": "#ef4444", "bg": "#0d0505", "card": "#1a0a0a"},
        {"id": "purple", "name": "Royal Purple", "accent": "#a855f7", "bg": "#0d0518", "card": "#160a28"},
        {"id": "amber", "name": "Amber", "accent": "#f59e0b", "bg": "#0d0a05", "card": "#1a140a"},
        {"id": "cyberpunk", "name": "Cyberpunk", "accent": "#f0f", "bg": "#0a0014", "card": "#14001f"},
        {"id": "mono", "name": "Monochrome", "accent": "#e5e5e5", "bg": "#0a0a0a", "card": "#1a1a1a"},
    ]
    return jsonify({"themes": themes})

@app.route("/api/editor/save", methods=["POST"])
def api_editor_save():
    data = request.json or {}
    path = data.get("path")
    content = data.get("content", "")
    editor = data.get("editor", "nano")
    result = fs_write(path, content)
    if result.get("status") == "success":
        record_recent_file(path, editor)
    return jsonify(result)

@app.route("/api/editor/open", methods=["POST"])
def api_editor_open():
    data = request.json or {}
    path = safe_path(data.get("path"))
    editor = data.get("editor", "nano")
    if not path:
        return jsonify({"status": "error", "output": "Path not allowed."})
    if not os.path.exists(path):
        return jsonify({"status": "error", "output": "File not found."})
    record_recent_file(path, editor)
    return jsonify({
        "status": "success",
        "editor": editor,
        "path": path,
        "command": f"{editor} {shlex.quote(path)}"
    })

@app.route("/api/xfce/start", methods=["POST"])
def api_xfce_start():
    sid = xfce_session.start()
    return jsonify({"status": "ok", "session_id": sid})

@app.route("/api/xfce/sessions", methods=["GET"])
def api_xfce_sessions():
    return jsonify({"sessions": xfce_session.list_sessions()})

@app.route("/api/clipboard", methods=["GET"])
def api_clipboard_get():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, content, created_at FROM clipboard ORDER BY id DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    return jsonify({"items": [{"id": r[0], "content": r[1], "created_at": r[2]} for r in rows]})

@app.route("/api/clipboard", methods=["POST"])
def api_clipboard_set():
    data = request.json or {}
    content = data.get("content", "")
    if content:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO clipboard (content) VALUES (?)", (content,))
        conn.commit()
        conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/clipboard/<int:cid>", methods=["DELETE"])
def api_clipboard_delete(cid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM clipboard WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ============================================================
# HTML TEMPLATE
# ============================================================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Termux WebOS — Kali XFCE4</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
    --bg: #090d16;
    --panel: #131c2e;
    --panel-2: #0c121e;
    --border: #23314a;
    --accent: #38bdf8;
    --accent-2: #0284c7;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --success: #22c55e;
    --danger: #ef4444;
    --warning: #f59e0b;
    --radius: 10px;
    --taskbar-h: 48px;
    --topbar-h: 32px;
    --font-mono: 'Fira Code', 'Courier New', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
html, body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    overflow: hidden;
    user-select: none;
}
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--accent); }

#desktop {
    position: fixed;
    inset: 0 0 var(--taskbar-h) 0;
    background: var(--bg);
    background-size: cover;
    background-position: center;
    overflow: hidden;
}
#desktop.no-taskbar { inset: 0; }
#desktop[data-wallpaper-type="custom"] { background-size: cover; background-repeat: no-repeat; }

.desktop-icons {
    position: absolute;
    top: 16px; left: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 1;
}
.desk-icon {
    width: 84px;
    padding: 10px 6px;
    border-radius: 8px;
    text-align: center;
    cursor: pointer;
    color: var(--text);
    transition: background 0.15s;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
}
.desk-icon:hover { background: rgba(56,189,248,0.15); }
.desk-icon i { font-size: 26px; color: var(--accent); }
.desk-icon span { font-size: 11px; font-weight: 500; text-shadow: 0 1px 3px rgba(0,0,0,0.9); word-break: break-word; }

#topbar {
    position: fixed;
    top: 0; left: 0; right: 0;
    height: var(--topbar-h);
    background: rgba(12,18,30,0.92);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    padding: 0 10px;
    gap: 8px;
    z-index: 5000;
    font-size: 12px;
}
.topbar-section { display: flex; align-items: center; gap: 6px; }
.topbar-spacer { flex: 1; }
.topbar-item {
    padding: 4px 8px;
    border-radius: 6px;
    color: var(--text);
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 5px;
    transition: background 0.15s;
    white-space: nowrap;
}
.topbar-item:hover { background: rgba(56,189,248,0.15); }
.topbar-item i { color: var(--accent); }
#clock-display { font-family: var(--font-mono); font-size: 11px; color: var(--muted); }
.workspace-indicator { display: flex; gap: 4px; padding: 0 6px; }
.ws-dot {
    width: 22px; height: 18px;
    border-radius: 4px;
    background: rgba(255,255,255,0.08);
    border: 1px solid var(--border);
    cursor: pointer;
    font-size: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--muted);
    transition: all 0.15s;
}
.ws-dot.active { background: var(--accent); color: var(--bg); border-color: var(--accent); font-weight: 700; }
.ws-dot:hover { border-color: var(--accent); }

#taskbar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    height: var(--taskbar-h);
    background: rgba(12,18,30,0.95);
    backdrop-filter: blur(12px);
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    padding: 0 8px;
    gap: 6px;
    z-index: 5000;
    overflow-x: auto;
    overflow-y: hidden;
}
#taskbar::-webkit-scrollbar { height: 0; }
.tb-btn {
    min-width: 42px;
    height: 36px;
    padding: 0 12px;
    border-radius: 8px;
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    color: var(--text);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font-size: 12px;
    transition: all 0.15s;
    flex-shrink: 0;
}
.tb-btn:hover { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.tb-btn.active { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.tb-btn i { font-size: 14px; }
.tb-task {
    max-width: 160px;
    justify-content: flex-start;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.tb-task.active { background: rgba(56,189,248,0.2); border-color: var(--accent); color: var(--accent); }
.tb-sep { width: 1px; height: 24px; background: var(--border); margin: 0 4px; flex-shrink: 0; }

#start-menu {
    position: fixed;
    bottom: calc(var(--taskbar-h) + 8px);
    left: 8px;
    width: 380px;
    max-width: calc(100vw - 16px);
    max-height: 70vh;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.9);
    display: none;
    flex-direction: column;
    z-index: 6000;
    overflow: hidden;
}
#start-menu.show { display: flex; }
.start-header {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--panel-2);
}
.start-avatar {
    width: 38px; height: 38px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    color: var(--bg);
    font-size: 15px;
}
.start-user { font-weight: 700; font-size: 14px; }
.start-host { font-size: 11px; color: var(--muted); font-family: var(--font-mono); }
.start-search { padding: 10px 12px; border-bottom: 1px solid var(--border); }
.start-search input {
    width: 100%;
    padding: 9px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 13px;
    outline: none;
}
.start-search input:focus { border-color: var(--accent); }
.start-body { flex: 1; overflow-y: auto; padding: 8px; }
.start-category {
    font-size: 10px;
    font-weight: 700;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    padding: 10px 10px 4px;
}
.start-app {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 9px 12px;
    border-radius: 8px;
    cursor: pointer;
    transition: background 0.15s;
}
.start-app:hover { background: rgba(56,189,248,0.15); }
.start-app i { width: 22px; text-align: center; color: var(--accent); font-size: 15px; }
.start-app-name { font-size: 13px; font-weight: 500; }
.start-app-desc { font-size: 11px; color: var(--muted); }
.start-footer {
    padding: 8px;
    border-top: 1px solid var(--border);
    display: flex;
    gap: 6px;
    background: var(--panel-2);
}
.start-footer .tb-btn { flex: 1; }

.os-window {
    position: absolute;
    min-width: 280px;
    min-height: 140px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.85);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    z-index: 100;
    transition: box-shadow 0.2s;
}
.os-window.maximized {
    top: var(--topbar-h) !important;
    left: 0 !important;
    width: 100% !important;
    height: calc(100vh - var(--topbar-h) - var(--taskbar-h)) !important;
    border-radius: 0;
    border-left: none;
    border-right: none;
}
.os-window.minimized { display: none; }
.os-window.focused { box-shadow: 0 25px 80px rgba(56,189,248,0.25), 0 20px 60px rgba(0,0,0,0.85); border-color: var(--accent); }
.window-header {
    background: var(--panel-2);
    padding: 8px 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    cursor: grab;
    user-select: none;
    flex-shrink: 0;
}
.window-header:active { cursor: grabbing; }
.window-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--accent);
    display: flex;
    gap: 8px;
    align-items: center;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.window-controls { display: flex; gap: 4px; flex-shrink: 0; }
.win-ctrl {
    width: 22px; height: 22px;
    border-radius: 5px;
    border: none;
    background: rgba(255,255,255,0.06);
    color: var(--muted);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    transition: all 0.15s;
}
.win-ctrl:hover { background: var(--accent); color: var(--bg); }
.win-ctrl.close:hover { background: var(--danger); color: #fff; }
.win-ctrl.min:hover { background: var(--warning); color: #000; }
.win-ctrl.max:hover { background: var(--success); color: #000; }
.window-content {
    padding: 12px;
    flex: 1;
    overflow-y: auto;
    font-size: 13px;
    min-height: 0;
}
.window-resize {
    position: absolute;
    bottom: 0; right: 0;
    width: 16px; height: 16px;
    cursor: nwse-resize;
    background: linear-gradient(135deg, transparent 50%, var(--border) 50%);
}

.terminal {
    background: #04060a;
    color: #4af626;
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    white-space: pre-wrap;
    word-break: break-all;
    min-height: 100px;
    max-height: 60vh;
    overflow-y: auto;
    line-height: 1.5;
}
.terminal .prompt { color: var(--accent); }
.terminal .err { color: var(--danger); }
.terminal .ok { color: var(--success); }
.cmd-preview {
    background: #04060a;
    color: #4af626;
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
    margin-bottom: 12px;
    word-break: break-all;
}

label { font-size: 12px; color: var(--muted); margin-bottom: 5px; display: block; font-weight: 500; }
input, textarea, select {
    width: 100%;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    margin-bottom: 12px;
    font-size: 13px;
    outline: none;
    font-family: inherit;
}
input:focus, textarea:focus, select:focus { border-color: var(--accent); }
button.primary {
    width: 100%;
    padding: 11px;
    background: var(--accent);
    color: var(--bg);
    font-weight: 700;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.2s;
    font-size: 13px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
}
button.primary:hover { opacity: 0.9; transform: translateY(-1px); }
button.primary:active { transform: translateY(0); }
button.secondary {
    width: 100%;
    padding: 10px;
    background: rgba(255,255,255,0.05);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    transition: all 0.15s;
}
button.secondary:hover { background: var(--border); color: var(--accent); }
button.danger {
    width: 100%;
    padding: 10px;
    background: rgba(239,68,68,0.15);
    color: var(--danger);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: 8px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    transition: all 0.15s;
}
button.danger:hover { background: var(--danger); color: #fff; }

#context-menu {
    position: fixed;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 6px;
    min-width: 220px;
    box-shadow: 0 15px 40px rgba(0,0,0,0.9);
    display: none;
    z-index: 7000;
}
#context-menu.show { display: block; }
.ctx-item {
    padding: 8px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text);
}
.ctx-item:hover { background: var(--accent); color: var(--bg); }
.ctx-item.disabled { opacity: 0.4; pointer-events: none; }
.ctx-item i { width: 16px; text-align: center; }
.ctx-sep { height: 1px; background: var(--border); margin: 4px 6px; }

.modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    backdrop-filter: blur(4px);
    z-index: 8000;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 16px;
}
.modal-backdrop.show { display: flex; }
.modal {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    width: 100%;
    max-width: 520px;
    max-height: 85vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    box-shadow: 0 25px 80px rgba(0,0,0,0.9);
}
.modal.wide { max-width: 760px; }
.modal-header {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-weight: 700;
    font-size: 14px;
}
.modal-body { padding: 16px; overflow-y: auto; flex: 1; }
.modal-footer {
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    display: flex;
    gap: 8px;
    justify-content: flex-end;
}
.modal-footer button { width: auto; padding: 9px 18px; }

.os-dropdown-container { position: relative; margin-bottom: 12px; }
.os-dropdown-btn {
    width: 100%;
    padding: 11px 14px;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 13px;
    font-weight: 500;
}
.os-dropdown-menu {
    display: none;
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    width: 100%;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    max-height: 300px;
    overflow-y: auto;
    z-index: 9000;
    box-shadow: 0 15px 40px rgba(0,0,0,0.9);
}
.os-dropdown-menu.show { display: block; }
.dropdown-item {
    padding: 10px 14px;
    cursor: pointer;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    font-size: 12px;
    color: var(--text);
    background: transparent;
    border: none;
    width: 100%;
    text-align: left;
    display: flex;
    align-items: center;
    gap: 10px;
    transition: background 0.12s;
}
.dropdown-item.preset-item { background: rgba(56,189,248,0.08); color: var(--accent); font-weight: 600; }
.dropdown-item:hover { background: var(--accent); color: var(--bg); }
.dropdown-item:last-child { border-bottom: none; }

.contact-card, .sms-card, .log-item, .file-item, .app-card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
    margin-bottom: 8px;
}
.contact-card { display: flex; justify-content: space-between; align-items: center; }
.contact-info { display: flex; flex-direction: column; gap: 3px; }
.contact-name { font-weight: 700; font-size: 13px; }
.contact-num { font-size: 11px; color: var(--muted); font-family: var(--font-mono); }
.contact-actions { display: flex; gap: 6px; }
.action-btn {
    padding: 5px 9px;
    border-radius: 6px;
    border: none;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 4px;
}
.action-call { background: rgba(34,197,94,0.2); color: var(--success); }
.action-call:hover { background: var(--success); color: #000; }
.action-msg { background: rgba(56,189,248,0.2); color: var(--accent); }
.action-msg:hover { background: var(--accent); color: #000; }

.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th, .data-table td { text-align: left; padding: 7px; border-bottom: 1px solid var(--border); }
.data-table th { color: var(--accent); width: 35%; font-weight: 600; }

.file-item { display: flex; align-items: center; gap: 10px; cursor: pointer; transition: background 0.12s; }
.file-item:hover { background: rgba(56,189,248,0.1); }
.file-item.selected { background: rgba(56,189,248,0.18); border-color: var(--accent); }
.file-item i { font-size: 18px; color: var(--accent); width: 22px; text-align: center; }
.file-item .file-name { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-item .file-meta { font-size: 10px; color: var(--muted); font-family: var(--font-mono); }
.file-item .file-actions { display: flex; gap: 4px; }
.file-item .file-actions button {
    width: 26px; height: 26px;
    border-radius: 6px;
    border: none;
    background: rgba(255,255,255,0.06);
    color: var(--muted);
    cursor: pointer;
    font-size: 11px;
    display: flex;
    align-items: center;
    justify-content: center;
}
.file-item .file-actions button:hover { background: var(--accent); color: var(--bg); }
.file-item .file-actions button.del:hover { background: var(--danger); color: #fff; }

.app-card {
    display: flex;
    align-items: center;
    gap: 12px;
    cursor: pointer;
    transition: background 0.12s;
}
.app-card:hover { background: rgba(56,189,248,0.1); }
.app-card i { font-size: 22px; color: var(--accent); width: 30px; text-align: center; }
.app-card .app-name { font-weight: 600; font-size: 13px; }
.app-card .app-desc { font-size: 11px; color: var(--muted); }

.tab-bar { display: flex; gap: 4px; margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 8px; flex-wrap: wrap; }
.tab-bar .tab-btn {
    flex: 1;
    min-width: 80px;
    padding: 8px;
    background: transparent;
    border: 1px solid transparent;
    color: var(--muted);
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    transition: all 0.15s;
}
.tab-bar .tab-btn.active { background: var(--accent); color: var(--bg); }
.tab-bar .tab-btn:hover:not(.active) { background: rgba(255,255,255,0.05); color: var(--text); }

.status-tag { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 10px; font-weight: 700; text-transform: uppercase; }
.status-success { background: rgba(34,197,94,0.15); color: var(--success); }
.status-failed, .status-error { background: rgba(239,68,68,0.15); color: var(--danger); }

.empty-state { text-align: center; padding: 30px 16px; color: var(--muted); font-size: 13px; }
.empty-state i { font-size: 42px; margin-bottom: 12px; display: block; color: var(--border); }

.hidden { display: none !important; }
.mt-8 { margin-top: 8px; }
.mt-12 { margin-top: 12px; }
.mb-8 { margin-bottom: 8px; }
.mb-12 { margin-bottom: 12px; }
.text-muted { color: var(--muted); }
.text-accent { color: var(--accent); }
.text-danger { color: var(--danger); }
.flex { display: flex; }
.flex-between { display: flex; justify-content: space-between; align-items: center; }
.gap-6 { gap: 6px; }
.gap-8 { gap: 8px; }

/* EDITOR STYLES */
.editor-container { display: flex; flex-direction: column; height: 100%; background: #04060a; }
.editor-toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: var(--panel-2);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
}
.editor-toolbar .editor-path {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--accent);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.editor-toolbar button {
    padding: 5px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: rgba(255,255,255,0.05);
    color: var(--text);
    cursor: pointer;
    font-size: 11px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 5px;
    white-space: nowrap;
}
.editor-toolbar button:hover { background: var(--accent); color: var(--bg); }
.editor-toolbar button.save { background: rgba(34,197,94,0.15); color: var(--success); border-color: rgba(34,197,94,0.3); }
.editor-toolbar button.save:hover { background: var(--success); color: #000; }
.editor-textarea {
    flex: 1;
    width: 100%;
    background: #04060a;
    color: #e2e8f0;
    border: none;
    outline: none;
    padding: 12px;
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 1.6;
    resize: none;
    tab-size: 4;
    white-space: pre;
    overflow: auto;
    min-height: 0;
}
.editor-textarea.wrap { white-space: pre-wrap; word-break: break-word; }
.editor-status {
    display: flex;
    gap: 16px;
    padding: 6px 12px;
    background: var(--panel-2);
    border-top: 1px solid var(--border);
    font-size: 11px;
    color: var(--muted);
    font-family: var(--font-mono);
}
.editor-status .dirty { color: var(--warning); font-weight: 700; }

.terminal-editor {
    background: #000;
    color: #fff;
    font-family: var(--font-mono);
    font-size: 13px;
    padding: 0;
    height: 100%;
    display: flex;
    flex-direction: column;
}
.terminal-editor .te-header {
    background: #1a1a1a;
    color: #4af626;
    padding: 4px 12px;
    font-size: 12px;
    display: flex;
    justify-content: space-between;
}
.terminal-editor .te-body {
    flex: 1;
    padding: 8px 12px;
    overflow: auto;
    white-space: pre-wrap;
    line-height: 1.5;
    color: #e0e0e0;
    min-height: 0;
    outline: none;
}
.terminal-editor .te-footer {
    background: #1a1a1a;
    color: #4af626;
    padding: 4px 12px;
    font-size: 11px;
    display: flex;
    justify-content: space-between;
}

/* MEDIA VIEWER */
.media-viewer {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: #000;
    overflow: hidden;
}
.media-stage {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: auto;
    padding: 12px;
    min-height: 0;
    position: relative;
}
.media-stage img, .media-stage video {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    border-radius: 6px;
    box-shadow: 0 10px 40px rgba(0,0,0,0.8);
}
.media-stage audio {
    width: 100%;
    max-width: 540px;
}
.media-controls {
    display: flex;
    gap: 6px;
    padding: 8px 12px;
    background: var(--panel-2);
    border-top: 1px solid var(--border);
    align-items: center;
    flex-wrap: wrap;
}
.media-controls button {
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: rgba(255,255,255,0.05);
    color: var(--text);
    cursor: pointer;
    font-size: 11px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 5px;
}
.media-controls button:hover { background: var(--accent); color: var(--bg); }
.media-info {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--muted);
    margin-left: auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 50%;
}
.media-zoom-badge {
    position: absolute;
    top: 12px; right: 12px;
    background: rgba(0,0,0,0.75);
    color: var(--accent);
    padding: 3px 10px;
    border-radius: 6px;
    font-family: var(--font-mono);
    font-size: 11px;
}

/* AUDIO VIEWER */
.audio-viewer {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 20px;
    height: 100%;
    background: linear-gradient(135deg, #0a0e1a, #131c2e);
    color: var(--text);
    padding: 24px;
}
.audio-art {
    width: 160px; height: 160px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 60px;
    color: var(--bg);
    box-shadow: 0 10px 50px rgba(56,189,248,0.4);
    animation: spin 8s linear infinite;
    animation-play-state: paused;
}
.audio-art.playing { animation-play-state: running; }
@keyframes spin { to { transform: rotate(360deg); } }
.audio-title { font-size: 16px; font-weight: 700; text-align: center; }
.audio-sub { font-size: 12px; color: var(--muted); font-family: var(--font-mono); }

/* README MODAL */
.readme-content {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 50vh;
    overflow-y: auto;
    color: #e2e8f0;
}

@keyframes fadeIn { from { opacity: 0; transform: scale(0.96); } to { opacity: 1; transform: scale(1); } }
.os-window { animation: fadeIn 0.18s ease-out; }
@keyframes slideUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
#start-menu.show { animation: slideUp 0.18s ease-out; }

@media (max-width: 600px) {
    #start-menu { width: calc(100vw - 16px); left: 8px; }
    .desktop-icons { flex-direction: row; flex-wrap: wrap; max-width: calc(100vw - 32px); }
    .desk-icon { width: 72px; }
    .topbar-item .label { display: none; }
    .modal { max-width: 100%; }
}
</style>
</head>
<body>

<div id="desktop" data-wallpaper-type="solid">
    <div class="desktop-icons" id="desktop-icons"></div>
</div>

<div id="topbar">
    <div class="topbar-item" onclick="toggleStartMenu()" title="Menu">
        <i class="fa-solid fa-bars"></i>
        <span class="label">Menu</span>
    </div>
    <div class="topbar-item" onclick="openApp('terminal')" title="Terminal">
        <i class="fa-solid fa-terminal"></i>
        <span class="label">Terminal</span>
    </div>
    <div class="topbar-item" onclick="openApp('files')" title="Files">
        <i class="fa-solid fa-folder"></i>
        <span class="label">Files</span>
    </div>
    <div class="topbar-item" onclick="openApp('media')" title="Media Viewer">
        <i class="fa-solid fa-photo-film"></i>
        <span class="label">Media</span>
    </div>
    <div class="topbar-sep"></div>
    <div class="workspace-indicator" id="workspace-indicator"></div>
    <div class="topbar-spacer"></div>
    <div class="topbar-item" id="clock-display" onclick="openApp('settings')">
        <i class="fa-solid fa-clock"></i>
        <span id="clock-text">--:--</span>
    </div>
    <div class="topbar-item" onclick="openApp('settings')" title="Settings">
        <i class="fa-solid fa-gear"></i>
    </div>
</div>

<div id="taskbar">
    <button class="tb-btn" onclick="toggleStartMenu()" title="Start Menu">
        <i class="fa-solid fa-bars"></i>
    </button>
    <div class="tb-sep"></div>
    <div id="task-buttons" style="display:flex; gap:6px; flex:1; overflow-x:auto;"></div>
    <div class="tb-sep"></div>
    <button class="tb-btn" onclick="openApp('terminal')" title="Terminal">
        <i class="fa-solid fa-terminal"></i>
    </button>
    <button class="tb-btn" onclick="openApp('files')" title="Files">
        <i class="fa-solid fa-folder"></i>
    </button>
    <button class="tb-btn" onclick="openApp('media')" title="Media Viewer">
        <i class="fa-solid fa-photo-film"></i>
    </button>
    <button class="tb-btn" onclick="openApp('settings')" title="Settings">
        <i class="fa-solid fa-gear"></i>
    </button>
    <button class="tb-btn" onclick="showDesktop()" title="Show Desktop">
        <i class="fa-solid fa-desktop"></i>
    </button>
</div>

<div id="start-menu">
    <div class="start-header">
        <div class="start-avatar">K</div>
        <div>
            <div class="start-user">kali</div>
            <div class="start-host">termux-webos@localhost</div>
        </div>
    </div>
    <div class="start-search">
        <input type="text" id="start-search-input" placeholder="Search applications..." oninput="filterStartApps(this.value)">
    </div>
    <div class="start-body" id="start-body"></div>
    <div class="start-footer">
        <button class="tb-btn" onclick="openApp('terminal')"><i class="fa-solid fa-terminal"></i> Terminal</button>
        <button class="tb-btn" onclick="openApp('settings')"><i class="fa-solid fa-gear"></i> Settings</button>
        <button class="tb-btn" onclick="lockScreen()"><i class="fa-solid fa-lock"></i> Lock</button>
    </div>
</div>

<div id="context-menu"></div>

<div class="modal-backdrop" id="modal-backdrop" onclick="if(event.target===this) closeModal()">
    <div class="modal" id="modal-content">
        <div class="modal-header">
            <span id="modal-title">Dialog</span>
            <button class="win-ctrl close" onclick="closeModal()"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <div class="modal-body" id="modal-body"></div>
        <div class="modal-footer" id="modal-footer"></div>
    </div>
</div>

<input type="file" id="hidden-file-input" style="display:none">
<input type="file" id="hidden-wallpaper-input" accept="image/*" style="display:none">

<script>
/* ============================================================
   GLOBAL STATE
   ============================================================ */
const State = {
    zIndex: 100,
    windowOffset: 0,
    windows: {},
    activeWindowId: null,
    workspaces: [],
    activeWorkspace: 0,
    preferences: {},
    apps: [],
    commands: [],
    kaliDE: [],
    editors: [],
    wallpapers: [],
    themes: [],
    startMenuOpen: false,
    contextMenuOpen: false,
    clipboard: [],
    fileCwd: '~',
    selectedFile: null,
    clipboardFile: null,
    clipboardOp: null,
    windowStates: {},
};

/* ============================================================
   APP REGISTRY
   ============================================================ */
const APP_REGISTRY = {
    terminal: { name: 'Terminal', icon: 'fa-terminal', category: 'System', render: renderTerminalApp, width: 720, height: 480 },
    files: { name: 'File Manager', icon: 'fa-folder', category: 'System', render: renderFilesApp, width: 760, height: 540 },
    editor: { name: 'Text Editor', icon: 'fa-file-pen', category: 'Development', render: renderEditorApp, width: 820, height: 580 },
    media: { name: 'Media Viewer', icon: 'fa-photo-film', category: 'Multimedia', render: renderMediaApp, width: 820, height: 580 },
    imageviewer: { name: 'Image Viewer', icon: 'fa-image', category: 'Multimedia', render: renderImageViewer, width: 820, height: 600 },
    videoplayer: { name: 'Video Player', icon: 'fa-video', category: 'Multimedia', render: renderVideoPlayer, width: 860, height: 560 },
    audioplayer: { name: 'Audio Player', icon: 'fa-music', category: 'Multimedia', render: renderAudioPlayer, width: 520, height: 520 },
    settings: { name: 'Settings', icon: 'fa-gear', category: 'System', render: renderSettingsApp, width: 640, height: 520 },
    commands: { name: 'Command Center', icon: 'fa-code', category: 'System', render: renderCommandsApp, width: 640, height: 560 },
    sms: { name: 'SMS Inbox', icon: 'fa-inbox', category: 'Communication', render: renderSMSApp, width: 600, height: 520 },
    contacts: { name: 'Contacts', icon: 'fa-address-book', category: 'Communication', render: renderContactsApp, width: 600, height: 520 },
    logs: { name: 'Execution Logs', icon: 'fa-list-check', category: 'System', render: renderLogsApp, width: 680, height: 520 },
    apps: { name: 'App Launcher', icon: 'fa-rocket', category: 'System', render: renderAppsApp, width: 600, height: 520 },
    bookmarks: { name: 'Bookmarks', icon: 'fa-bookmark', category: 'System', render: renderBookmarksApp, width: 560, height: 480 },
    clipboard: { name: 'Clipboard', icon: 'fa-clipboard', category: 'System', render: renderClipboardApp, width: 520, height: 480 },
    xfce: { name: 'XFCE4 Desktop', icon: 'fa-desktop', category: 'Desktop', render: renderXFCEApp, width: 760, height: 540 },
    about: { name: 'About WebOS', icon: 'fa-circle-info', category: 'System', render: renderAboutApp, width: 520, height: 420 }
};

/* ============================================================
   UTILITY FUNCTIONS
   ============================================================ */
function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => {
        if (k === 'class') e.className = v;
        else if (k === 'html') e.innerHTML = v;
        else if (k === 'text') e.textContent = v;
        else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
        else e.setAttribute(k, v);
    });
    (Array.isArray(children) ? children : [children]).forEach(c => {
        if (c == null) return;
        e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return e;
}

function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtBytes(n) {
    if (n == null) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
    if (n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + ' MB';
    return (n/1024/1024/1024).toFixed(1) + ' GB';
}

function nowTime() {
    const d = new Date();
    return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function uid() {
    return 'win-' + Math.random().toString(36).slice(2, 10);
}

function basename(p) {
    if (!p) return '';
    return p.split('/').filter(Boolean).pop() || p;
}

function dirname(p) {
    if (!p) return '/';
    const parts = p.split('/');
    parts.pop();
    return parts.join('/') || '/';
}

function joinPath(a, b) {
    if (!a) return b;
    if (a.endsWith('/')) return a + b;
    return a + '/' + b;
}

function extOf(name) {
    return (name.split('.').pop() || '').toLowerCase();
}

function mediaKind(path) {
    const e = extOf(path);
    if (['jpg','jpeg','png','gif','webp','svg','bmp','ico','tiff','tif','heic','heif','avif'].includes(e)) return 'image';
    if (['mp4','mkv','avi','mov','webm','flv','wmv','m4v','3gp','mpg','mpeg','ogv'].includes(e)) return 'video';
    if (['mp3','wav','ogg','flac','m4a','aac','opus','wma','amr','mid','midi'].includes(e)) return 'audio';
    return null;
}

function toast(msg, type = 'info') {
    const colors = { info: 'var(--accent)', success: 'var(--success)', error: 'var(--danger)', warning: 'var(--warning)' };
    const t = el('div', {
        style: {
            position: 'fixed', bottom: 'calc(var(--taskbar-h) + 16px)', right: '16px',
            background: 'var(--panel)', border: '1px solid ' + (colors[type] || colors.info),
            color: 'var(--text)', padding: '12px 18px', borderRadius: '10px',
            boxShadow: '0 10px 30px rgba(0,0,0,0.8)', zIndex: '9999',
            fontSize: '13px', fontWeight: '500', maxWidth: '340px',
            transition: 'opacity 0.3s, transform 0.3s',
            borderLeft: '4px solid ' + (colors[type] || colors.info)
        }
    }, msg);
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateX(20px)'; }, 2800);
    setTimeout(() => t.remove(), 3200);
}

/* ============================================================
   API WRAPPERS
   ============================================================ */
const API = {
    async get(url) {
        const r = await fetch(url);
        return r.json();
    },
    async post(url, data) {
        const r = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data || {})
        });
        return r.json();
    },
    async del(url) {
        const r = await fetch(url, { method: 'DELETE' });
        return r.json();
    },
    async execute(command, args = '', timeout = 30, cwd = null) {
        return this.post('/api/execute', { command, args, timeout, cwd });
    },
    async fsList(path, showHidden = false) {
        return this.post('/api/fs/list', { path, show_hidden: showHidden });
    },
    async fsCreate(path, isDir = false, content = '') {
        return this.post('/api/fs/create', { path, is_dir: isDir, content });
    },
    async fsDelete(path, recursive = true) {
        return this.post('/api/fs/delete', { path, recursive });
    },
    async fsRename(oldPath, newPath) {
        return this.post('/api/fs/rename', { old_path: oldPath, new_path: newPath });
    },
    async fsCopy(src, dst, recursive = true) {
        return this.post('/api/fs/copy', { src, dst, recursive });
    },
    async fsMove(src, dst) {
        return this.post('/api/fs/move', { src, dst });
    },
    async fsRead(path) {
        return this.post('/api/fs/read', { path });
    },
    async fsWrite(path, content) {
        return this.post('/api/fs/write', { path, content });
    },
    async fsInfo(path) {
        return this.post('/api/fs/info', { path });
    },
    async fsSearch(path, query) {
        return this.post('/api/fs/search', { path, query });
    },
    async mediaInfo(path) {
        return this.post('/api/fs/media_info', { path });
    },
    async projectDetect(path) {
        return this.post('/api/project/detect', { path });
    },
    async projectReadReadme(path) {
        return this.post('/api/project/read_readme', { path });
    },
    async saveWindowState(appId, x, y, w, h, state) {
        return this.post('/api/window_states', { app_id: appId, x, y, width: w, height: h, state });
    },
    async deleteWindowState(appId) {
        return this.del('/api/window_states/' + encodeURIComponent(appId));
    },
    async uploadWallpaper(name, dataUri) {
        return this.post('/api/wallpapers/upload', { name, data_uri: dataUri });
    },
    async deleteWallpaper(id) {
        const numId = String(id).replace(/^custom-/, '');
        return this.del('/api/wallpapers/' + numId);
    },
    async uploadFile(file) {
        const fd = new FormData();
        fd.append('file', file);
        const r = await fetch('/api/upload', { method: 'POST', body: fd });
        return r.json();
    }
};

/* ============================================================
   INITIALIZATION
   ============================================================ */
async function boot() {
    const [prefs, ws, apps, cmds, wallpapers, themes, bookmarks, clipboard, winStates] = await Promise.all([
        API.get('/api/preferences'),
        API.get('/api/workspaces'),
        API.get('/api/apps'),
        API.get('/api/commands'),
        API.get('/api/wallpapers'),
        API.get('/api/themes'),
        API.get('/api/bookmarks'),
        API.get('/api/clipboard'),
        API.get('/api/window_states')
    ]);
    
    State.preferences = prefs;
    State.workspaces = ws.workspaces || [];
    State.apps = apps.apps || [];
    State.commands = cmds.commands || [];
    State.kaliDE = cmds.kali_de || [];
    State.editors = cmds.editors || [];
    State.wallpapers = wallpapers.wallpapers || [];
    State.themes = themes.themes || [];
    State.bookmarks = bookmarks.bookmarks || [];
    State.clipboard = clipboard.items || [];
    State.windowStates = winStates.states || {};
    
    applyTheme(State.preferences.theme || 'kali-dark');
    applyWallpaper(State.preferences.wallpaper || 'solid-dark');
    
    renderDesktopIcons();
    renderStartMenu();
    renderWorkspaceIndicator();
    renderTaskbarTasks();
    startClock();
    
    document.addEventListener('click', (e) => {
        if (!e.target.closest('#start-menu') && !e.target.closest('[onclick*="toggleStartMenu"]')) {
            closeStartMenu();
        }
        if (!e.target.closest('#context-menu')) closeContextMenu();
        if (!e.target.closest('.os-dropdown-container')) {
            document.querySelectorAll('.os-dropdown-menu').forEach(m => m.classList.remove('show'));
        }
    });
    
    document.addEventListener('contextmenu', (e) => {
        if (e.target.closest('.os-window') || e.target.closest('#start-menu') || e.target.closest('#topbar') || e.target.closest('#taskbar')) return;
        e.preventDefault();
        showContextMenu(e.clientX, e.clientY);
    });
    
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeStartMenu();
            closeContextMenu();
            closeModal();
        }
        if (e.ctrlKey && e.altKey && e.key.toLowerCase() === 't') {
            e.preventDefault();
            openApp('terminal');
        }
        if (e.ctrlKey && e.altKey && e.key.toLowerCase() === 'm') {
            e.preventDefault();
            openApp('media');
        }
    });
    
    for (const [id, app] of Object.entries(APP_REGISTRY)) {
        if (!State.apps.find(a => a.app_id === id)) {
            await API.post('/api/apps', {
                app_id: id, name: app.name, icon: app.icon,
                category: app.category, executable: id, args: '', terminal: 0
            });
        }
    }
    
    const apps2 = await API.get('/api/apps');
    State.apps = apps2.apps || [];
    
    // Persist window state on unload
    window.addEventListener('beforeunload', () => {
        Object.values(State.windows).forEach(w => {
            if (w.state && w.state !== 'closed' && w.element && w.element.parentNode) {
                persistWindowState(w);
            }
        });
    });
}

/* ============================================================
   CLOCK
   ============================================================ */
function startClock() {
    const update = () => { document.getElementById('clock-text').textContent = nowTime(); };
    update();
    setInterval(update, 10000);
}

/* ============================================================
   THEME & WALLPAPER
   ============================================================ */
function applyTheme(themeId) {
    const theme = State.themes.find(t => t.id === themeId) || State.themes[0];
    if (!theme) return;
    const root = document.documentElement;
    root.style.setProperty('--accent', theme.accent);
    root.style.setProperty('--bg', theme.bg);
    root.style.setProperty('--panel', theme.card);
    root.style.setProperty('--panel-2', shade(theme.card, -8));
    State.preferences.theme = themeId;
    API.post('/api/preferences', { theme: themeId });
}

function shade(hex, percent) {
    const n = parseInt(hex.replace('#',''), 16);
    const r = Math.max(0, Math.min(255, (n >> 16) + percent));
    const g = Math.max(0, Math.min(255, ((n >> 8) & 0xFF) + percent));
    const b = Math.max(0, Math.min(255, (n & 0xFF) + percent));
    return '#' + ((r << 16) | (g << 8) | b).toString(16).padStart(6, '0');
}

function applyWallpaper(wpId) {
    const wp = State.wallpapers.find(w => w.id === wpId) || State.wallpapers[0];
    const desktop = document.getElementById('desktop');
    if (!wp) {
        desktop.style.background = 'var(--bg)';
        desktop.dataset.wallpaperType = 'solid';
        return;
    }
    if (wp.type === 'custom' && wp.url) {
        desktop.style.background = 'var(--bg)';
        desktop.style.backgroundImage = `linear-gradient(rgba(9,13,22,0.35), rgba(9,13,22,0.55)), url('${wp.url}')`;
        desktop.dataset.wallpaperType = 'custom';
    } else {
        // solid
        desktop.style.backgroundImage = 'none';
        desktop.style.background = wp.color || '#090d16';
        desktop.dataset.wallpaperType = 'solid';
    }
    State.preferences.wallpaper = wpId;
    API.post('/api/preferences', { wallpaper: wpId });
}

/* ============================================================
   DESKTOP ICONS
   ============================================================ */
function renderDesktopIcons() {
    const container = document.getElementById('desktop-icons');
    container.innerHTML = '';
    const desktopApps = ['terminal', 'files', 'editor', 'media', 'commands', 'settings', 'xfce', 'about'];
    desktopApps.forEach(id => {
        const app = APP_REGISTRY[id];
        if (!app) return;
        container.appendChild(el('div', {
            class: 'desk-icon',
            ondblclick: () => openApp(id),
            onclick: () => {}
        }, [
            el('i', { class: `fa-solid ${app.icon}` }),
            el('span', { text: app.name })
        ]));
    });
}

/* ============================================================
   WORKSPACE INDICATOR
   ============================================================ */
function renderWorkspaceIndicator() {
    const container = document.getElementById('workspace-indicator');
    container.innerHTML = '';
    State.workspaces.forEach((ws, idx) => {
        container.appendChild(el('div', {
            class: 'ws-dot' + (idx === State.activeWorkspace ? ' active' : ''),
            title: ws.name,
            onclick: () => switchWorkspace(idx)
        }, String(idx + 1)));
    });
}

function switchWorkspace(idx) {
    State.activeWorkspace = idx;
    renderWorkspaceIndicator();
}

/* ============================================================
   START MENU
   ============================================================ */
function renderStartMenu() {
    const body = document.getElementById('start-body');
    body.innerHTML = '';
    
    const categories = {};
    State.apps.forEach(app => {
        if (!categories[app.category]) categories[app.category] = [];
        categories[app.category].push(app);
    });
    
    Object.entries(APP_REGISTRY).forEach(([id, app]) => {
        if (!State.apps.find(a => a.app_id === id)) {
            if (!categories[app.category]) categories[app.category] = [];
            categories[app.category].push({ app_id: id, name: app.name, icon: app.icon, category: app.category });
        }
    });
    
    Object.entries(categories).sort().forEach(([cat, apps]) => {
        body.appendChild(el('div', { class: 'start-category', text: cat }));
        apps.sort((a,b) => a.name.localeCompare(b.name)).forEach(app => {
            body.appendChild(el('div', {
                class: 'start-app',
                'data-app-name': app.name.toLowerCase(),
                onclick: () => { openApp(app.app_id); closeStartMenu(); }
            }, [
                el('i', { class: `fa-solid ${app.icon || 'fa-cube'}` }),
                el('div', {}, [
                    el('div', { class: 'start-app-name', text: app.name }),
                    el('div', { class: 'start-app-desc', text: app.executable || app.app_id })
                ])
            ]));
        });
    });
}

function filterStartApps(query) {
    const q = query.toLowerCase().trim();
    document.querySelectorAll('.start-app').forEach(el => {
        const name = el.getAttribute('data-app-name') || '';
        el.style.display = name.includes(q) ? '' : 'none';
    });
}

function toggleStartMenu() {
    State.startMenuOpen = !State.startMenuOpen;
    document.getElementById('start-menu').classList.toggle('show', State.startMenuOpen);
    if (State.startMenuOpen) setTimeout(() => document.getElementById('start-search-input')?.focus(), 50);
}

function closeStartMenu() {
    State.startMenuOpen = false;
    document.getElementById('start-menu').classList.remove('show');
}

/* ============================================================
   CONTEXT MENU
   ============================================================ */
function showContextMenu(x, y, items) {
    const menu = document.getElementById('context-menu');
    menu.innerHTML = '';
    if (!items) {
        items = [
            { icon: 'fa-terminal', label: 'Open Terminal', action: () => openApp('terminal') },
            { icon: 'fa-folder', label: 'Open Files', action: () => openApp('files') },
            { icon: 'fa-photo-film', label: 'Open Media Viewer', action: () => openApp('media') },
            { icon: 'fa-file-pen', label: 'Open Editor', action: () => openApp('editor') },
            { icon: 'fa-code', label: 'Command Center', action: () => openApp('commands') },
            { sep: true },
            { icon: 'fa-plus', label: 'New File', action: () => quickCreateFile() },
            { icon: 'fa-folder-plus', label: 'New Folder', action: () => quickCreateFolder() },
            { sep: true },
            { icon: 'fa-image', label: 'Change Wallpaper', action: () => openApp('settings') },
            { icon: 'fa-palette', label: 'Change Theme', action: () => openApp('settings') },
            { sep: true },
            { icon: 'fa-arrows-rotate', label: 'Refresh Desktop', action: () => location.reload() },
            { icon: 'fa-circle-info', label: 'About WebOS', action: () => openApp('about') },
        ];
    }
    items.forEach(item => {
        if (item.sep) {
            menu.appendChild(el('div', { class: 'ctx-sep' }));
        } else {
            menu.appendChild(el('div', {
                class: 'ctx-item' + (item.disabled ? ' disabled' : ''),
                onclick: () => { if (!item.disabled) { item.action(); closeContextMenu(); } }
            }, [
                el('i', { class: `fa-solid ${item.icon}` }),
                el('span', { text: item.label })
            ]));
        }
    });
    menu.style.left = Math.min(x, window.innerWidth - 240) + 'px';
    menu.style.top = Math.min(y, window.innerHeight - menu.offsetHeight - 10) + 'px';
    menu.classList.add('show');
    State.contextMenuOpen = true;
}

function closeContextMenu() {
    document.getElementById('context-menu').classList.remove('show');
    State.contextMenuOpen = false;
}

/* ============================================================
   MODAL DIALOGS
   ============================================================ */
function openModal(title, bodyHtml, footerButtons = [], wide = false) {
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').innerHTML = bodyHtml;
    const content = document.getElementById('modal-content');
    content.classList.toggle('wide', wide);
    const footer = document.getElementById('modal-footer');
    footer.innerHTML = '';
    footerButtons.forEach(btn => {
        footer.appendChild(el('button', {
            class: btn.primary ? 'primary' : (btn.danger ? 'danger' : 'secondary'),
            style: { width: 'auto', padding: '9px 18px' },
            onclick: () => btn.action && btn.action()
        }, btn.label));
    });
    document.getElementById('modal-backdrop').classList.add('show');
}

function closeModal() {
    document.getElementById('modal-backdrop').classList.remove('show');
}

/* ============================================================
   WINDOW MANAGEMENT (with state persistence)
   ============================================================ */
function persistWindowState(w) {
    if (!w || !w.element || !w.app) return;
    const e = w.element;
    const isMax = w.state === 'maximized';
    const isMin = w.state === 'minimized';
    const appKey = w.appKey || w.app;
    API.saveWindowState(
        appKey,
        isMax ? null : e.offsetLeft,
        isMax ? null : e.offsetTop,
        isMax ? null : e.offsetWidth,
        isMax ? null : e.offsetHeight,
        w.state || 'normal'
    );
}

function openApp(appId, options = {}) {
    const app = APP_REGISTRY[appId];
    if (!app) {
        const custom = State.apps.find(a => a.app_id === appId);
        if (custom) return openCustomApp(custom);
        return toast('App not found: ' + appId, 'error');
    }
    
    const appKey = options.appKey || appId;
    
    if (!options.forceNew) {
        const existingId = Object.keys(State.windows).find(id => 
            State.windows[id].app === appId && 
            State.windows[id].appKey === appKey &&
            !State.windows[id].filePath
        );
        if (existingId) {
            focusWindow(existingId);
            return existingId;
        }
    }
    
    const id = uid();
    
    // Restore saved geometry
    const saved = State.windowStates[appKey] || {};
    const w = (saved.width || app.width || 640);
    const h = (saved.height || app.height || 480);
    
    let top = (saved.y != null) ? saved.y : (60 + State.windowOffset);
    let left = (saved.x != null) ? saved.x : (20 + State.windowOffset);
    
    // Clamp to viewport
    top = Math.max(30, Math.min(top, window.innerHeight - 120));
    left = Math.max(0, Math.min(left, window.innerWidth - 120));
    
    const win = createWindowEl(id, options.title || app.name, app.icon, w, h, {top, left});
    State.windows[id] = {
        id, app: appId, appKey,
        title: options.title || app.name,
        element: win, state: saved.state || 'normal',
        filePath: options.filePath || null
    };
    
    if (saved.state === 'maximized') {
        win.classList.add('maximized');
        State.windows[id]._prev = {top: top+'px', left: left+'px', width: w+'px', height: h+'px'};
    }
    
    const content = win.querySelector('.window-content');
    app.render(content, id, options);
    
    if (saved.state === 'minimized') {
        win.classList.add('minimized');
    }
    
    if (!options.forceNew && options.title) {
        State.windowOffset = (State.windowOffset + 24) % 120;
    }
    
    focusWindow(id);
    return id;
}

function openCustomApp(app) {
    const id = uid();
    const win = createWindowEl(id, app.name, app.icon || 'fa-cube', 640, 480);
    State.windows[id] = { id, app: app.app_id, appKey: app.app_id, title: app.name, element: win, state: 'normal', filePath: null };
    const content = win.querySelector('.window-content');
    renderCustomApp(content, app);
    focusWindow(id);
}

function createWindowEl(id, title, icon, width, height, pos = {}) {
    const win = el('div', { class: 'os-window', id });
    win.style.width = Math.min(width, window.innerWidth - 20) + 'px';
    win.style.height = Math.min(height, window.innerHeight - 100) + 'px';
    win.style.top = (pos.top != null ? pos.top : (60 + State.windowOffset)) + 'px';
    win.style.left = (pos.left != null ? pos.left : (20 + State.windowOffset)) + 'px';
    win.style.zIndex = ++State.zIndex;
    
    const header = el('div', { class: 'window-header' }, [
        el('div', { class: 'window-title' }, [
            el('i', { class: `fa-solid ${icon}` }),
            el('span', { text: title })
        ]),
        el('div', { class: 'window-controls' }, [
            el('button', { class: 'win-ctrl min', title: 'Minimize', onclick: () => minimizeWindow(id) }, [
                el('i', { class: 'fa-solid fa-minus' })
            ]),
            el('button', { class: 'win-ctrl max', title: 'Maximize', onclick: () => toggleMaximize(id) }, [
                el('i', { class: 'fa-solid fa-expand' })
            ]),
            el('button', { class: 'win-ctrl close', title: 'Close', onclick: () => closeWindow(id) }, [
                el('i', { class: 'fa-solid fa-xmark' })
            ])
        ])
    ]);
    
    const content = el('div', { class: 'window-content' });
    const resize = el('div', { class: 'window-resize' });
    
    win.appendChild(header);
    win.appendChild(content);
    win.appendChild(resize);
    
    document.getElementById('desktop').appendChild(win);
    
    makeDraggable(win, header, id);
    makeResizable(win, resize, id);
    win.addEventListener('mousedown', () => focusWindow(id));
    
    renderTaskbarTasks();
    return win;
}

function closeWindow(id) {
    const w = State.windows[id];
    if (!w) return;
    
    const doClose = () => {
        persistWindowState(w);
        // Delete saved state (close = forget position for next open)
        // Actually we want to keep position for next open, so don't delete.
        w.element.remove();
        delete State.windows[id];
        if (State.activeWindowId === id) State.activeWindowId = null;
        renderTaskbarTasks();
    };
    
    const isDirty = (w.isDirty && w.isDirty()) || w.dirty;
    if (isDirty) {
        openModal('Unsaved Changes', 
            `<p style="font-size:13px">"${escapeHtml(w.title)}" has unsaved changes. Close anyway?</p>`,
            [
                { label: 'Cancel', action: closeModal },
                { label: 'Discard & Close', danger: true, action: () => { closeModal(); doClose(); } }
            ]
        );
        return;
    }
    doClose();
}

function minimizeWindow(id) {
    const w = State.windows[id];
    if (!w) return;
    w.state = 'minimized';
    w.element.classList.add('minimized');
    persistWindowState(w);
    renderTaskbarTasks();
}

function restoreWindow(id) {
    const w = State.windows[id];
    if (!w) return;
    w.state = 'normal';
    w.element.classList.remove('minimized');
    persistWindowState(w);
    focusWindow(id);
    renderTaskbarTasks();
}

function toggleMaximize(id) {
    const w = State.windows[id];
    if (!w) return;
    if (w.state === 'maximized') {
        w.state = 'normal';
        w.element.classList.remove('maximized');
        if (w._prev) {
            w.element.style.top = w._prev.top;
            w.element.style.left = w._prev.left;
            w.element.style.width = w._prev.width;
            w.element.style.height = w._prev.height;
        }
    } else {
        w._prev = {
            top: w.element.style.top, left: w.element.style.left,
            width: w.element.style.width, height: w.element.style.height
        };
        w.state = 'maximized';
        w.element.classList.add('maximized');
    }
    persistWindowState(w);
    renderTaskbarTasks();
}

function focusWindow(id) {
    const w = State.windows[id];
    if (!w) return;
    State.activeWindowId = id;
    w.element.style.zIndex = ++State.zIndex;
    Object.values(State.windows).forEach(x => x.element.classList.remove('focused'));
    w.element.classList.add('focused');
    if (w.state === 'minimized') {
        w.state = 'normal';
        w.element.classList.remove('minimized');
    }
    renderTaskbarTasks();
}

function showDesktop() {
    Object.values(State.windows).forEach(w => minimizeWindow(w.id));
}

function setWindowTitle(id, title) {
    const w = State.windows[id];
    if (!w) return;
    w.title = title;
    const titleEl = w.element.querySelector('.window-title span');
    if (titleEl) titleEl.textContent = title;
    renderTaskbarTasks();
}

function makeDraggable(element, handle, winId) {
    let pos1=0, pos2=0, pos3=0, pos4=0;
    const onEnd = () => {
        const w = State.windows[winId];
        if (w) persistWindowState(w);
    };
    handle.onmousedown = (e) => {
        if (e.target.closest('button')) return;
        e.preventDefault();
        pos3 = e.clientX; pos4 = e.clientY;
        document.onmouseup = () => { document.onmouseup = null; document.onmousemove = null; onEnd(); };
        document.onmousemove = (ev) => {
            ev.preventDefault();
            pos1 = pos3 - ev.clientX;
            pos2 = pos4 - ev.clientY;
            pos3 = ev.clientX; pos4 = ev.clientY;
            let newTop = element.offsetTop - pos2;
            let newLeft = element.offsetLeft - pos1;
            if (newTop < 30) newTop = 30;
            if (newLeft < 0) newLeft = 0;
            if (newLeft > window.innerWidth - 60) newLeft = window.innerWidth - 60;
            element.style.top = newTop + 'px';
            element.style.left = newLeft + 'px';
        };
    };
    handle.ontouchstart = (e) => {
        const t = e.touches[0];
        pos3 = t.clientX; pos4 = t.clientY;
        const onMove = (ev) => {
            const tt = ev.touches[0];
            pos1 = pos3 - tt.clientX;
            pos2 = pos4 - tt.clientY;
            pos3 = tt.clientX; pos4 = tt.clientY;
            let newTop = element.offsetTop - pos2;
            let newLeft = element.offsetLeft - pos1;
            if (newTop < 30) newTop = 30;
            if (newLeft < 0) newLeft = 0;
            element.style.top = newTop + 'px';
            element.style.left = newLeft + 'px';
        };
        const onEndT = () => {
            document.removeEventListener('touchmove', onMove);
            document.removeEventListener('touchend', onEndT);
            onEnd();
        };
        document.addEventListener('touchmove', onMove, {passive: false});
        document.addEventListener('touchend', onEndT);
    };
}

function makeResizable(element, handle, winId) {
    let startX, startY, startW, startH;
    const onDown = (e) => {
        e.preventDefault();
        const t = e.touches ? e.touches[0] : e;
        startX = t.clientX; startY = t.clientY;
        startW = element.offsetWidth; startH = element.offsetHeight;
        const onMove = (ev) => {
            const tt = ev.touches ? ev.touches[0] : ev;
            element.style.width = Math.max(280, startW + (tt.clientX - startX)) + 'px';
            element.style.height = Math.max(140, startH + (tt.clientY - startY)) + 'px';
        };
        const onUp = () => {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            document.removeEventListener('touchmove', onMove);
            document.removeEventListener('touchend', onUp);
            const w = State.windows[winId];
            if (w) persistWindowState(w);
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
        document.addEventListener('touchmove', onMove, {passive: false});
        document.addEventListener('touchend', onUp);
    };
    handle.addEventListener('mousedown', onDown);
    handle.addEventListener('touchstart', onDown);
}

/* ============================================================
   TASKBAR TASKS
   ============================================================ */
function renderTaskbarTasks() {
    const container = document.getElementById('task-buttons');
    container.innerHTML = '';
    Object.values(State.windows).forEach(w => {
        const btn = el('button', {
            class: 'tb-btn tb-task' + (State.activeWindowId === w.id ? ' active' : ''),
            title: w.title,
            onclick: () => {
                if (State.activeWindowId === w.id && w.state !== 'minimized') minimizeWindow(w.id);
                else { restoreWindow(w.id); focusWindow(w.id); }
            }
        }, [
            el('i', { class: `fa-solid ${APP_REGISTRY[w.app]?.icon || 'fa-cube'}` }),
            el('span', { text: w.title })
        ]);
        container.appendChild(btn);
    });
}

/* ============================================================
   FILE OPERATION HELPERS
   ============================================================ */
function promptNewFile(parentPath, onDone) {
    openModal('New File', `
        <label>File name</label>
        <input type="text" id="dlg-filename" placeholder="example.txt" autofocus>
        <label>Parent directory</label>
        <input type="text" id="dlg-parent" value="${escapeHtml(parentPath || '~')}">
    `, [
        { label: 'Cancel', action: closeModal },
        { label: 'Create', primary: true, action: async () => {
            const name = document.getElementById('dlg-filename').value.trim();
            const parent = document.getElementById('dlg-parent').value.trim();
            if (!name) return toast('Name required', 'error');
            const full = joinPath(parent, name);
            const res = await API.fsCreate(full, false, '');
            if (res.status === 'success') {
                toast('File created', 'success');
                closeModal();
                if (onDone) onDone(full);
            } else {
                toast(res.output, 'error');
            }
        }}
    ]);
}

function promptNewFolder(parentPath, onDone) {
    openModal('New Folder', `
        <label>Folder name</label>
        <input type="text" id="dlg-foldername" placeholder="myfolder" autofocus>
        <label>Parent directory</label>
        <input type="text" id="dlg-parent" value="${escapeHtml(parentPath || '~')}">
    `, [
        { label: 'Cancel', action: closeModal },
        { label: 'Create', primary: true, action: async () => {
            const name = document.getElementById('dlg-foldername').value.trim();
            const parent = document.getElementById('dlg-parent').value.trim();
            if (!name) return toast('Name required', 'error');
            const full = joinPath(parent, name);
            const res = await API.fsCreate(full, true);
            if (res.status === 'success') {
                toast('Folder created', 'success');
                closeModal();
                if (onDone) onDone(full);
            } else {
                toast(res.output, 'error');
            }
        }}
    ]);
}

function promptDelete(path, onDone) {
    const doDelete = async () => {
        const res = await API.fsDelete(path, true);
        if (res.status === 'success') {
            toast('Deleted', 'success');
            closeModal();
            if (onDone) onDone();
        } else {
            toast(res.output, 'error');
        }
    };
    if (State.preferences.confirm_delete === 'false') {
        doDelete();
        return;
    }
    openModal('Delete', `
        <p style="font-size:13px;margin-bottom:8px">Are you sure you want to permanently delete:</p>
        <div class="cmd-preview">${escapeHtml(path)}</div>
        <p style="font-size:12px;color:var(--danger)">This action cannot be undone.</p>
    `, [
        { label: 'Cancel', action: closeModal },
        { label: 'Delete', danger: true, action: doDelete }
    ]);
}

function promptRename(path, onDone) {
    const oldName = basename(path);
    openModal('Rename', `
        <label>New name</label>
        <input type="text" id="dlg-newname" value="${escapeHtml(oldName)}" autofocus>
    `, [
        { label: 'Cancel', action: closeModal },
        { label: 'Rename', primary: true, action: async () => {
            const newName = document.getElementById('dlg-newname').value.trim();
            if (!newName) return toast('Name required', 'error');
            const newPath = joinPath(dirname(path), newName);
            const res = await API.fsRename(path, newPath);
            if (res.status === 'success') {
                toast('Renamed', 'success');
                closeModal();
                if (onDone) onDone(newPath);
            } else {
                toast(res.output, 'error');
            }
        }}
    ]);
}

function promptFileInfo(path) {
    API.fsInfo(path).then(res => {
        if (res.status !== 'success') return toast(res.output || 'Cannot stat', 'error');
        const i = res.info;
        const rows = [
            ['Name', i.name],
            ['Path', i.path],
            ['Type', i.is_dir ? 'Directory' : (i.is_link ? 'Symlink' : 'File')],
            ['Size', i.is_dir ? '—' : fmtBytes(i.size)],
            ['Permissions', i.perms],
            ['Modified', i.mtime],
            ['Created', i.ctime],
            ['Accessed', i.atime],
            ['MIME', i.mime || '—'],
        ];
        openModal('File Info', `<table class="data-table">${rows.map(r => `<tr><th>${escapeHtml(r[0])}</th><td>${escapeHtml(String(r[1]))}</td></tr>`).join('')}</table>`, [
            { label: 'Close', primary: true, action: closeModal }
        ]);
    });
}

function promptCopyMove(path, mode, onDone) {
    const name = basename(path);
    openModal(mode === 'copy' ? 'Copy To' : 'Move To', `
        <label>Destination path (including name)</label>
        <input type="text" id="dlg-dest" value="${escapeHtml(joinPath(dirname(path), name))}" autofocus>
    `, [
        { label: 'Cancel', action: closeModal },
        { label: mode === 'copy' ? 'Copy' : 'Move', primary: true, action: async () => {
            const dest = document.getElementById('dlg-dest').value.trim();
            if (!dest) return toast('Destination required', 'error');
            const res = mode === 'copy' ? await API.fsCopy(path, dest) : await API.fsMove(path, dest);
            if (res.status === 'success') {
                toast(mode === 'copy' ? 'Copied' : 'Moved', 'success');
                closeModal();
                if (onDone) onDone();
            } else {
                toast(res.output, 'error');
            }
        }}
    ]);
}

async function quickCreateFile() {
    const p = State.fileCwd || '~';
    promptNewFile(p, () => {
        Object.values(State.windows).forEach(w => {
            if (w.app === 'files' && w.refresh) w.refresh();
        });
    });
}

async function quickCreateFolder() {
    const p = State.fileCwd || '~';
    promptNewFolder(p, () => {
        Object.values(State.windows).forEach(w => {
            if (w.app === 'files' && w.refresh) w.refresh();
        });
    });
}

/* ============================================================
   OPEN FILE WITH EDITOR CHOICE (also handles media)
   ============================================================ */
function openFileWithChoice(path, onDone) {
    const kind = mediaKind(path);
    if (kind === 'image') return openMediaViewer(path, 'image');
    if (kind === 'video') return openMediaViewer(path, 'video');
    if (kind === 'audio') return openMediaViewer(path, 'audio');
    
    const editors = State.editors.length ? State.editors : [
        { cmd: 'nano', name: 'Nano', icon: 'fa-feather' },
        { cmd: 'vim', name: 'Vim', icon: 'fa-terminal' }
    ];
    
    const defaultEd = State.preferences.default_editor || 'ask';
    if (defaultEd !== 'ask') {
        const ed = editors.find(e => e.cmd === defaultEd) || editors[0];
        openFileInEditor(path, ed.cmd, ed.name);
        if (onDone) onDone();
        return;
    }
    
    const body = `
        <p style="font-size:13px;margin-bottom:12px">Choose an editor to open <strong>${escapeHtml(basename(path))}</strong>:</p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            ${editors.map(e => `
                <button class="secondary" style="padding:14px" onclick="window._pickEditor('${escapeHtml(e.cmd)}','${escapeHtml(e.name)}')">
                    <i class="fa-solid ${e.icon}" style="font-size:20px;margin-bottom:6px"></i>
                    <div>${escapeHtml(e.name)}</div>
                    <div style="font-size:10px;color:var(--muted)">${escapeHtml(e.cmd)}</div>
                </button>
            `).join('')}
        </div>
        <div class="mt-12" style="font-size:11px;color:var(--muted)">
            Tip: Set a default editor in Settings → Editor to skip this dialog.
        </div>
    `;
    window._pickEditor = (cmd, name) => {
        closeModal();
        openFileInEditor(path, cmd, name);
        if (onDone) onDone();
    };
    openModal('Open With', body, [{ label: 'Cancel', action: closeModal }]);
}

function openFileInEditor(path, editorCmd, editorName) {
    const terminalEditors = ['nano', 'vim', 'vi', 'nvim', 'emacs'];
    if (terminalEditors.includes(editorCmd)) {
        openTerminalEditor(path, editorCmd, editorName);
    } else {
        openApp('editor', { forceNew: true, title: `${editorName} — ${basename(path)}`, filePath: path, editorCmd });
    }
}

/* ============================================================
   MEDIA VIEWER — dispatch to correct viewer app
   ============================================================ */
function openMediaViewer(path, kind, options = {}) {
    const appId = kind === 'image' ? 'imageviewer' : (kind === 'video' ? 'videoplayer' : 'audioplayer');
    return openApp(appId, {
        forceNew: true,
        filePath: path,
        title: `${basename(path)}`,
        kind,
        ...options
    });
}

/* ============================================================
   APP: MEDIA VIEWER (launcher hub)
   ============================================================ */
function renderMediaApp(container, winId) {
    container.innerHTML = '';
    container.style.padding = '0';
    container.style.display = 'flex';
    container.style.flexDirection = 'column';
    container.style.overflow = 'hidden';
    
    let cwd = State.fileCwd || '~';
    let selected = null;
    
    const bar = el('div', { style: { display: 'flex', gap: '6px', padding: '10px 12px', borderBottom: '1px solid var(--border)', flexWrap: 'wrap', alignItems: 'center' } }, [
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => load('~') }, [el('i', { class: 'fa-solid fa-house' })]),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => load('/sdcard/DCIM') }, [el('i', { class: 'fa-solid fa-camera' }), ' DCIM']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => load('/sdcard/Pictures') }, [el('i', { class: 'fa-solid fa-image' }), ' Pictures']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => load('/sdcard/Music') }, [el('i', { class: 'fa-solid fa-music' }), ' Music']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => load('/sdcard/Movies') }, [el('i', { class: 'fa-solid fa-video' }), ' Movies']),
        el('input', {
            type: 'text', value: cwd, id: `${winId}-m-path`,
            style: { flex: '1', minWidth: '120px', marginBottom: '0', fontFamily: 'var(--font-mono)' },
            onkeydown: (e) => { if (e.key === 'Enter') load(e.target.value); }
        }),
        el('button', { class: 'primary', style: { width: 'auto', padding: '0 14px' }, onclick: () => load(document.getElementById(`${winId}-m-path`).value) }, [el('i', { class: 'fa-solid fa-arrow-right' })]),
    ]);
    const list = el('div', { style: { flex: '1', overflowY: 'auto', padding: '8px 12px' } });
    const info = el('div', { style: { padding: '6px 12px', fontSize: '11px', color: 'var(--muted)', borderTop: '1px solid var(--border)', fontFamily: 'var(--font-mono)' } });
    
    container.appendChild(bar);
    container.appendChild(list);
    container.appendChild(info);
    
    async function load(path) {
        list.innerHTML = '<div class="empty-state"><i class="fa-solid fa-spinner fa-spin"></i>Loading...</div>';
        const res = await API.fsList(path, false);
        if (res.status !== 'success') {
            list.innerHTML = `<div class="empty-state"><i class="fa-solid fa-triangle-exclamation"></i>${escapeHtml(res.output || 'Error')}</div>`;
            return;
        }
        cwd = res.path;
        State.fileCwd = res.path;
        document.getElementById(`${winId}-m-path`).value = res.path;
        info.textContent = res.path;
        list.innerHTML = '';
        
        const mediaItems = res.items.filter(i => !i.is_dir && mediaKind(i.name));
        const dirItems = res.items.filter(i => i.is_dir);
        
        if (res.parent) {
            const up = el('div', { class: 'file-item' }, [
                el('i', { class: 'fa-solid fa-arrow-up' }),
                el('div', { class: 'file-name', text: '..' }),
                el('div', { class: 'file-meta', text: 'parent' })
            ]);
            up.onclick = () => load(res.parent);
            list.appendChild(up);
        }
        
        if (dirItems.length) {
            list.appendChild(el('div', { class: 'start-category', text: 'Folders' }));
            dirItems.forEach(it => {
                const row = el('div', { class: 'file-item' }, [
                    el('i', { class: 'fa-solid fa-folder' }),
                    el('div', { class: 'file-name', text: it.name }),
                    el('div', { class: 'file-meta', text: 'dir' })
                ]);
                row.onclick = () => load(it.path);
                list.appendChild(row);
            });
        }
        
        if (mediaItems.length) {
            list.appendChild(el('div', { class: 'start-category', text: 'Media' }));
            mediaItems.forEach(it => {
                const k = mediaKind(it.name);
                const icon = k === 'image' ? 'fa-image' : (k === 'video' ? 'fa-video' : 'fa-music');
                const row = el('div', { class: 'file-item' }, [
                    el('i', { class: `fa-solid ${icon}` }),
                    el('div', { style: { flex: '1', minWidth: '0' } }, [
                        el('div', { class: 'file-name', text: it.name }),
                        el('div', { class: 'file-meta', text: `${fmtBytes(it.size)} • ${k}` })
                    ]),
                    el('div', { class: 'file-actions' }, [
                        el('button', { title: 'Open', onclick: (e) => { e.stopPropagation(); openMediaViewer(it.path, k); } }, [el('i', { class: 'fa-solid fa-up-right-from-square' })]),
                    ])
                ]);
                row.ondblclick = () => openMediaViewer(it.path, k);
                row.onclick = () => {
                    document.querySelectorAll(`#${winId} .file-item`).forEach(r => r.classList.remove('selected'));
                    row.classList.add('selected');
                    selected = it;
                };
                list.appendChild(row);
            });
        }
        
        if (!dirItems.length && !mediaItems.length && !res.parent) {
            list.appendChild(el('div', { class: 'empty-state' }, [
                el('i', { class: 'fa-solid fa-photo-film' }),
                el('div', { text: 'No media files in this folder' })
            ]));
        }
    }
    
    load(cwd);
}

/* ============================================================
   APP: IMAGE VIEWER
   ============================================================ */
function renderImageViewer(container, winId, options = {}) {
    container.innerHTML = '';
    container.style.padding = '0';
    container.style.display = 'flex';
    container.style.flexDirection = 'column';
    container.style.overflow = 'hidden';
    
    const filePath = options.filePath;
    if (!filePath) {
        container.appendChild(el('div', { class: 'empty-state' }, [el('i', { class: 'fa-solid fa-image' }), 'No image selected']));
        return;
    }
    
    const viewer = el('div', { class: 'media-viewer' });
    const stage = el('div', { class: 'media-stage' });
    const img = el('img', { src: `/api/fs/media?path=${encodeURIComponent(filePath)}`, alt: basename(filePath) });
    let zoom = 1;
    const zoomBadge = el('div', { class: 'media-zoom-badge', text: '100%' });
    stage.appendChild(img);
    stage.appendChild(zoomBadge);
    
    function setZoom(z) {
        zoom = Math.max(0.1, Math.min(8, z));
        img.style.transform = `scale(${zoom})`;
        zoomBadge.textContent = Math.round(zoom * 100) + '%';
    }
    
    stage.addEventListener('wheel', (e) => {
        if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            setZoom(zoom * (e.deltaY < 0 ? 1.1 : 0.9));
        }
    }, { passive: false });
    
    const controls = el('div', { class: 'media-controls' }, [
        el('button', { onclick: () => setZoom(zoom * 1.25) }, [el('i', { class: 'fa-solid fa-magnifying-glass-plus' }), ' Zoom In']),
        el('button', { onclick: () => setZoom(zoom * 0.8) }, [el('i', { class: 'fa-solid fa-magnifying-glass-minus' }), ' Zoom Out']),
        el('button', { onclick: () => setZoom(1) }, [el('i', { class: 'fa-solid fa-1' }), ' 100%']),
        el('button', { onclick: () => { setZoom(1); stage.scrollTop = 0; stage.scrollLeft = 0; } }, [el('i', { class: 'fa-solid fa-arrows-to-dot' }), ' Center']),
        el('button', { onclick: () => openFileWithChoice(filePath) }, [el('i', { class: 'fa-solid fa-up-right-from-square' }), ' Open With']),
        el('button', { onclick: () => {
            const a = document.createElement('a');
            a.href = `/api/fs/media?path=${encodeURIComponent(filePath)}`;
            a.download = basename(filePath);
            a.click();
        }}, [el('i', { class: 'fa-solid fa-download' }), ' Download']),
        el('span', { class: 'media-info', text: filePath }),
    ]);
    
    viewer.appendChild(stage);
    viewer.appendChild(controls);
    container.appendChild(viewer);
    
    // Load info
    API.mediaInfo(filePath).then(r => {
        if (r.status === 'success') {
            const infoSpan = viewer.querySelector('.media-info');
            infoSpan.textContent = `${r.name} • ${fmtBytes(r.size)} • ${r.mime}`;
        }
    });
}

/* ============================================================
   APP: VIDEO PLAYER
   ============================================================ */
function renderVideoPlayer(container, winId, options = {}) {
    container.innerHTML = '';
    container.style.padding = '0';
    container.style.display = 'flex';
    container.style.flexDirection = 'column';
    container.style.overflow = 'hidden';
    
    const filePath = options.filePath;
    if (!filePath) {
        container.appendChild(el('div', { class: 'empty-state' }, [el('i', { class: 'fa-solid fa-video' }), 'No video selected']));
        return;
    }
    
    const viewer = el('div', { class: 'media-viewer' });
    const stage = el('div', { class: 'media-stage' });
    const video = el('video', {
        src: `/api/fs/media?path=${encodeURIComponent(filePath)}`,
        controls: 'controls',
        autoplay: 'autoplay',
        playsinline: 'playsinline',
        preload: 'metadata',
        style: { width: '100%', height: '100%', maxHeight: '100%' }
    });
    stage.appendChild(video);
    
    const controls = el('div', { class: 'media-controls' }, [
        el('button', { onclick: () => video.play() }, [el('i', { class: 'fa-solid fa-play' }), ' Play']),
        el('button', { onclick: () => video.pause() }, [el('i', { class: 'fa-solid fa-pause' }), ' Pause']),
        el('button', { onclick: () => { video.currentTime = Math.max(0, video.currentTime - 10); } }, [el('i', { class: 'fa-solid fa-backward' }), ' -10s']),
        el('button', { onclick: () => { video.currentTime = Math.min(video.duration || 0, video.currentTime + 10); } }, [el('i', { class: 'fa-solid fa-forward' }), ' +10s']),
        el('button', { onclick: () => {
            const a = document.createElement('a');
            a.href = `/api/fs/media?path=${encodeURIComponent(filePath)}`;
            a.download = basename(filePath);
            a.click();
        }}, [el('i', { class: 'fa-solid fa-download' }), ' Download']),
        el('span', { class: 'media-info', text: filePath }),
    ]);
    
    viewer.appendChild(stage);
    viewer.appendChild(controls);
    container.appendChild(viewer);
    
    API.mediaInfo(filePath).then(r => {
        if (r.status === 'success') {
            const infoSpan = viewer.querySelector('.media-info');
            infoSpan.textContent = `${r.name} • ${fmtBytes(r.size)} • ${r.mime}`;
        }
    });
}

/* ============================================================
   APP: AUDIO PLAYER
   ============================================================ */
function renderAudioPlayer(container, winId, options = {}) {
    container.innerHTML = '';
    container.style.padding = '0';
    container.style.overflow = 'hidden';
    
    const filePath = options.filePath;
    if (!filePath) {
        container.appendChild(el('div', { class: 'empty-state' }, [el('i', { class: 'fa-solid fa-music' }), 'No audio selected']));
        return;
    }
    
    const audio = el('audio', {
        src: `/api/fs/media?path=${encodeURIComponent(filePath)}`,
        controls: 'controls',
        autoplay: 'autoplay',
        preload: 'metadata'
    });
    
    const viewer = el('div', { class: 'audio-viewer' }, [
        el('div', { class: 'audio-art', id: `${winId}-art` }, [el('i', { class: 'fa-solid fa-music' })]),
        el('div', { class: 'audio-title', text: basename(filePath) }),
        el('div', { class: 'audio-sub', text: filePath }),
        audio,
    ]);
    container.appendChild(viewer);
    
    audio.addEventListener('play', () => document.getElementById(`${winId}-art`)?.classList.add('playing'));
    audio.addEventListener('pause', () => document.getElementById(`${winId}-art`)?.classList.remove('playing'));
    audio.addEventListener('ended', () => document.getElementById(`${winId}-art`)?.classList.remove('playing'));
    
    API.mediaInfo(filePath).then(r => {
        if (r.status === 'success') {
            const sub = viewer.querySelector('.audio-sub');
            sub.textContent = `${fmtBytes(r.size)} • ${r.mime}`;
        }
    });
}

/* ============================================================
   TERMINAL EDITOR
   ============================================================ */
function openTerminalEditor(path, editorCmd, editorName) {
    API.post('/api/editor/open', { path, editor: editorCmd }).then(meta => {
        _launchInteractiveEditor(path, editorCmd, editorName);
    }).catch(() => {
        _launchInteractiveEditor(path, editorCmd, editorName);
    });
}

function _launchInteractiveEditor(path, editorCmd, editorName) {
    const id = uid();
    const win = createWindowEl(id, `${editorName} — ${basename(path)}`, editorCmd === 'vim' ? 'fa-terminal' : 'fa-feather', 820, 580);
    State.windows[id] = { id, app: 'editor', appKey: 'editor', title: `${editorName} — ${basename(path)}`, element: win, state: 'normal', filePath: path };
    
    const content = win.querySelector('.window-content');
    content.style.padding = '0';
    content.style.display = 'flex';
    content.style.flexDirection = 'column';
    content.style.overflow = 'hidden';
    
    const container = el('div', { class: 'terminal-editor' });
    const header = el('div', { class: 'te-header' }, [
        el('span', {}, [el('i', { class: `fa-solid ${editorCmd === 'vim' ? 'fa-terminal' : 'fa-feather'}` }), ' ', editorName]),
        el('span', { text: path })
    ]);
    const body = el('div', { class: 'te-body', contenteditable: 'true', spellcheck: 'false' });
    const footer = el('div', { class: 'te-footer' }, [
        el('span', { id: `${id}-status`, text: 'Loading...' }),
        el('span', { id: `${id}-pos`, text: 'Ln 1, Col 1' })
    ]);
    container.appendChild(header);
    container.appendChild(body);
    container.appendChild(footer);
    content.appendChild(container);
    
    let dirty = false;
    
    function updateTitle() {
        const t = (dirty ? '● ' : '') + editorName + ' — ' + basename(path);
        setWindowTitle(id, t);
    }
    
    function updateStatus(msg) {
        const s = document.getElementById(`${id}-status`);
        if (s) s.textContent = msg;
    }
    
    function updateCursor() {
        const pos = document.getElementById(`${id}-pos`);
        if (!pos) return;
        const sel = window.getSelection();
        let line = 1, col = 1;
        if (sel && sel.rangeCount > 0) {
            const range = sel.getRangeAt(0);
            const pre = range.cloneRange();
            pre.selectNodeContents(body);
            pre.setEnd(range.endContainer, range.endOffset);
            const text = pre.toString();
            const lines = text.split('\n');
            line = lines.length;
            col = lines[lines.length - 1].length + 1;
        }
        pos.textContent = `Ln ${line}, Col ${col}`;
    }
    
    API.fsRead(path).then(res => {
        if (res.status === 'success') {
            body.textContent = res.content;
            updateStatus(`${editorCmd}  ${res.size} bytes`);
        } else {
            body.textContent = '';
            updateStatus('New file');
            if (res.binary) {
                body.textContent = '⚠ Binary file — editing disabled.';
                body.contentEditable = 'false';
            }
        }
        updateTitle();
    });
    
    body.addEventListener('input', () => { dirty = true; updateTitle(); State.windows[id].dirty = true; });
    body.addEventListener('keyup', updateCursor);
    body.addEventListener('click', updateCursor);
    body.addEventListener('keydown', (e) => {
        if (e.key === 'Tab') {
            e.preventDefault();
            document.execCommand('insertText', false, '    ');
        }
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
            e.preventDefault();
            saveFile();
        }
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'x') {
            e.preventDefault();
            closeWindow(id);
        }
    });
    
    async function saveFile() {
        const text = body.innerText;
        updateStatus('Saving...');
        const res = await API.fsWrite(path, text);
        if (res.status === 'success') {
            dirty = false;
            updateTitle();
            State.windows[id].dirty = false;
            updateStatus(`Saved ${text.length} bytes`);
            toast('File saved', 'success');
        } else {
            updateStatus('Save failed: ' + res.output);
            toast(res.output, 'error');
        }
    }
    
    State.windows[id].saveFn = saveFile;
    State.windows[id].isDirty = () => dirty;
    
    footer.appendChild(el('span', { style: { color: '#888', marginLeft: 'auto' }, text: 'Ctrl+S save • Ctrl+X close' }));
    
    focusWindow(id);
}

/* ============================================================
   APP: TERMINAL
   ============================================================ */
function renderTerminalApp(container, winId) {
    container.innerHTML = '';
    const out = el('div', { class: 'terminal', id: `${winId}-out` });
    out.innerHTML = `<span class="prompt">kali@termux-webos</span>:<span style="color:#38bdf8">~</span>$ Welcome to Termux WebOS Terminal\nType a command below. Use 'help' for assistance.\nDev commands (npm/pip/flask/etc.) automatically get a 5-minute timeout.\n\n`;
    
    const inputRow = el('div', { style: { display: 'flex', gap: '6px', marginTop: '8px' } }, [
        el('span', { style: { color: '#38bdf8', fontFamily: 'var(--font-mono)', fontSize: '13px', paddingTop: '10px' }, text: '$' }),
        el('input', {
            type: 'text', id: `${winId}-input`, placeholder: 'Enter command...',
            style: { flex: '1', marginBottom: '0' },
            onkeydown: (e) => { if (e.key === 'Enter') runTerminalCommand(winId); }
        }),
        el('button', {
            class: 'primary', style: { width: 'auto', padding: '0 14px' },
            onclick: () => runTerminalCommand(winId)
        }, [el('i', { class: 'fa-solid fa-play' })])
    ]);
    
    container.appendChild(out);
    container.appendChild(inputRow);
    setTimeout(() => document.getElementById(`${winId}-input`)?.focus(), 100);
}

async function runTerminalCommand(winId) {
    const input = document.getElementById(`${winId}-input`);
    const out = document.getElementById(`${winId}-out`);
    const cmd = input.value.trim();
    if (!cmd) return;
    input.value = '';
    
    out.innerHTML += `<span class="prompt">$</span> ${escapeHtml(cmd)}\n`;
    
    if (cmd === 'clear') { out.innerHTML = ''; return; }
    if (cmd === 'help') {
        out.innerHTML += `Available commands:\n  help          Show this help\n  clear         Clear terminal\n  ls [path]     List directory\n  pwd           Print working directory\n  whoami        Current user\n  date          Current date/time\n  open <file>   Open file with media viewer / editor\n  xfce4         Start XFCE4 desktop session\n  nano <file>   Edit file in Nano\n  vim <file>    Edit file in Vim\n  Any other command will be passed to the shell.\nDev commands (npm/pip/flask/etc.) get a 5-minute timeout automatically.\n\n`;
        return;
    }
    if (cmd === 'xfce4' || cmd === 'startxfce4') {
        out.innerHTML += `<span class="ok">Starting XFCE4 desktop session...</span>\n`;
        await API.post('/api/xfce/start', {});
        openApp('xfce');
        return;
    }
    const mOpen = cmd.match(/^open\s+(.+)$/);
    if (mOpen) {
        const p = mOpen[1].trim();
        const k = mediaKind(p);
        if (k) { openMediaViewer(p, k); out.innerHTML += `<span class="ok">Opening media: ${escapeHtml(p)}</span>\n`; }
        else { openFileWithChoice(p); out.innerHTML += `<span class="ok">Opening: ${escapeHtml(p)}</span>\n`; }
        return;
    }
    const m = cmd.match(/^(nano|vim|vi|nvim)\s+(.+)$/);
    if (m) {
        out.innerHTML += `<span class="ok">Opening ${m[1]} ${m[2]}...</span>\n`;
        openFileInEditor(m[2], m[1], m[1]);
        return;
    }
    
    const firstSpace = cmd.indexOf(' ');
    let command, args;
    if (firstSpace === -1) { command = cmd; args = ''; }
    else { command = cmd.slice(0, firstSpace); args = cmd.slice(firstSpace + 1); }
    
    out.innerHTML += `<span style="color:#94a3b8">Running...</span>\n`;
    try {
        const res = await API.execute(command, args);
        const cls = res.status === 'success' ? 'ok' : 'err';
        out.innerHTML += `<span class="${cls}">${escapeHtml(res.output || '(no output)')}</span>\n\n`;
    } catch (e) {
        out.innerHTML += `<span class="err">${escapeHtml(e.message)}</span>\n\n`;
    }
    out.scrollTop = out.scrollHeight;
}

/* ============================================================
   APP: FILE MANAGER
   ============================================================ */
function renderFilesApp(container, winId) {
    container.innerHTML = '';
    let cwd = State.fileCwd || '~';
    let showHidden = State.preferences.show_hidden_files === 'true';
    let selectedItem = null;
    
    const pathBar = el('div', { style: { display: 'flex', gap: '6px', marginBottom: '10px', flexWrap: 'wrap' } }, [
        el('button', { class: 'secondary', style: { width: 'auto', padding: '8px 12px' }, title: 'Home', onclick: () => load('~') }, [el('i', { class: 'fa-solid fa-house' })]),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '8px 12px' }, title: 'Internal Storage', onclick: () => load('/sdcard') }, [el('i', { class: 'fa-solid fa-sd-card' })]),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '8px 12px' }, title: 'Termux Home', onclick: () => load('/data/data/com.termux/files/home') }, [el('i', { class: 'fa-solid fa-terminal' })]),
        el('input', {
            type: 'text', id: `${winId}-path`, value: cwd,
            style: { flex: '1', minWidth: '120px', marginBottom: '0', fontFamily: 'var(--font-mono)' },
            onkeydown: (e) => { if (e.key === 'Enter') load(e.target.value); }
        }),
        el('button', {
            class: 'primary', style: { width: 'auto', padding: '0 14px' },
            onclick: () => load(document.getElementById(`${winId}-path`).value)
        }, [el('i', { class: 'fa-solid fa-arrow-right' })])
    ]);
    
    const toolbar = el('div', { style: { display: 'flex', gap: '6px', marginBottom: '10px', flexWrap: 'wrap' } }, [
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => promptNewFile(cwd, () => load(cwd)) }, [el('i', { class: 'fa-solid fa-file-circle-plus' }), ' New File']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => promptNewFolder(cwd, () => load(cwd)) }, [el('i', { class: 'fa-solid fa-folder-plus' }), ' New Folder']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => {
            showHidden = !showHidden;
            API.post('/api/preferences', { show_hidden_files: String(showHidden) });
            State.preferences.show_hidden_files = String(showHidden);
            load(cwd);
        }}, [el('i', { class: 'fa-solid fa-eye' }), ' Toggle Hidden']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => promptSearch(cwd) }, [el('i', { class: 'fa-solid fa-magnifying-glass' }), ' Search']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, onclick: () => {
            if (!selectedItem) return toast('Select a file first', 'warning');
            if (State.clipboardFile) {
                const dst = joinPath(cwd, basename(State.clipboardFile));
                const op = State.clipboardOp;
                (op === 'cut' ? API.fsMove(State.clipboardFile, dst) : API.fsCopy(State.clipboardFile, dst))
                    .then(res => {
                        if (res.status === 'success') {
                            toast(op === 'cut' ? 'Moved' : 'Pasted', 'success');
                            State.clipboardFile = null; State.clipboardOp = null;
                            load(cwd);
                        } else toast(res.output, 'error');
                    });
            }
        }}, [el('i', { class: 'fa-solid fa-paste' }), ' Paste']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, title: 'Detect Project (README)', onclick: () => {
            detectProjectInFolder(cwd);
        }}, [el('i', { class: 'fa-solid fa-diagram-project' }), ' Detect Project']),
        el('button', { class: 'secondary', style: { width: 'auto', padding: '6px 10px' }, title: 'Upload file', onclick: () => uploadToFolder(cwd, () => load(cwd)) }, [el('i', { class: 'fa-solid fa-upload' }), ' Upload']),
    ]);
    
    const list = el('div', { id: `${winId}-list` });
    const info = el('div', { class: 'text-muted', style: { fontSize: '11px', marginTop: '8px' } });
    
    container.appendChild(pathBar);
    container.appendChild(toolbar);
    container.appendChild(list);
    container.appendChild(info);
    
    async function load(path) {
        list.innerHTML = '<div class="empty-state"><i class="fa-solid fa-spinner fa-spin"></i>Loading...</div>';
        const res = await API.fsList(path, showHidden);
        if (res.status !== 'success') {
            list.innerHTML = `<div class="empty-state"><i class="fa-solid fa-triangle-exclamation"></i>${escapeHtml(res.output || 'Error')}</div>`;
            return;
        }
        cwd = res.path;
        State.fileCwd = res.path;
        document.getElementById(`${winId}-path`).value = res.path;
        list.innerHTML = '';
        info.textContent = `${res.items.length} items — ${res.path}`;
        
        if (res.parent) {
            const up = el('div', { class: 'file-item' }, [
                el('i', { class: 'fa-solid fa-arrow-up' }),
                el('div', { class: 'file-name', text: '..' }),
                el('div', { class: 'file-meta', text: 'parent' })
            ]);
            up.onclick = () => load(res.parent);
            list.appendChild(up);
        }
        
        if (!res.items.length) {
            list.appendChild(el('div', { class: 'empty-state' }, [
                el('i', { class: 'fa-solid fa-folder-open' }),
                el('div', { text: 'Empty directory' })
            ]));
            return;
        }
        
        res.items.forEach(item => {
            const kind = mediaKind(item.name);
            let icon;
            if (item.is_dir) icon = 'fa-folder';
            else if (item.is_link) icon = 'fa-link';
            else icon = fileIcon(item.name);
            
            const row = el('div', { class: 'file-item' }, [
                el('i', { class: `fa-solid ${icon}` }),
                el('div', { style: { flex: '1', minWidth: '0' } }, [
                    el('div', { class: 'file-name', text: item.name }),
                    el('div', { class: 'file-meta', text: item.is_dir ? 'dir' : fmtBytes(item.size) })
                ]),
                el('div', { class: 'file-actions' }, [
                    (!item.is_dir) ? el('button', { title: 'Open', onclick: (e) => { e.stopPropagation(); openFileWithChoice(item.path, () => {}); } }, [el('i', { class: 'fa-solid fa-up-right-from-square' })]) : null,
                    (!item.is_dir && kind) ? el('button', { title: 'Preview', onclick: (e) => { e.stopPropagation(); openMediaViewer(item.path, kind); } }, [el('i', { class: 'fa-solid fa-eye' })]) : null,
                    el('button', { title: 'Rename', onclick: (e) => { e.stopPropagation(); promptRename(item.path, () => load(cwd)); } }, [el('i', { class: 'fa-solid fa-pen' })]),
                    el('button', { title: 'Delete', class: 'del', onclick: (e) => { e.stopPropagation(); promptDelete(item.path, () => load(cwd)); } }, [el('i', { class: 'fa-solid fa-trash' })])
                ].filter(Boolean))
            ]);
            
            row.onclick = (e) => {
                if (e.target.closest('.file-actions')) return;
                document.querySelectorAll(`#${winId}-list .file-item`).forEach(r => r.classList.remove('selected'));
                row.classList.add('selected');
                selectedItem = item;
                if (item.is_dir) load(item.path);
            };
            
            row.ondblclick = () => {
                if (item.is_dir) load(item.path);
                else openFileWithChoice(item.path, () => {});
            };
            
            row.oncontextmenu = (e) => {
                e.preventDefault(); e.stopPropagation();
                document.querySelectorAll(`#${winId}-list .file-item`).forEach(r => r.classList.remove('selected'));
                row.classList.add('selected');
                selectedItem = item;
                const menuItems = [
                    (!item.is_dir && kind) ? { icon: 'fa-eye', label: 'Preview Media', action: () => openMediaViewer(item.path, kind) } : null,
                    (!item.is_dir) ? { icon: 'fa-file-pen', label: 'Open With...', action: () => openFileWithChoice(item.path, () => {}) } : null,
                    item.is_dir ? { icon: 'fa-folder-open', label: 'Open', action: () => load(item.path) } : null,
                    item.is_dir ? { icon: 'fa-diagram-project', label: 'Detect Project', action: () => detectProjectInFolder(item.path) } : null,
                    { icon: 'fa-circle-info', label: 'Properties', action: () => promptFileInfo(item.path) },
                    { sep: true },
                    { icon: 'fa-scissors', label: 'Cut', action: () => { State.clipboardFile = item.path; State.clipboardOp = 'cut'; toast('Cut: ' + item.name, 'info'); } },
                    { icon: 'fa-copy', label: 'Copy', action: () => { State.clipboardFile = item.path; State.clipboardOp = 'copy'; toast('Copied: ' + item.name, 'info'); } },
                    { icon: 'fa-arrow-right-arrow-left', label: 'Move...', action: () => promptCopyMove(item.path, 'move', () => load(cwd)) },
                    { icon: 'fa-pen', label: 'Rename', action: () => promptRename(item.path, () => load(cwd)) },
                    { sep: true },
                    { icon: 'fa-download', label: 'Download', action: () => { window.location.href = `/api/fs/media?path=${encodeURIComponent(item.path)}`; } },
                    { icon: 'fa-trash', label: 'Delete', action: () => promptDelete(item.path, () => load(cwd)) },
                ].filter(Boolean);
                showContextMenu(e.clientX, e.clientY, menuItems);
            };
            
            list.appendChild(row);
        });
    }
    
    function promptSearch(root) {
        openModal('Search Files', `
            <label>Search query (filename)</label>
            <input type="text" id="dlg-search" placeholder="e.g. config" autofocus>
            <label>Root directory</label>
            <input type="text" id="dlg-search-root" value="${escapeHtml(root)}">
        `, [
            { label: 'Cancel', action: closeModal },
            { label: 'Search', primary: true, action: async () => {
                const q = document.getElementById('dlg-search').value.trim();
                const r = document.getElementById('dlg-search-root').value.trim();
                if (!q) return;
                closeModal();
                const res = await API.fsSearch(r, q);
                if (res.status !== 'success') return toast(res.output || 'Search failed', 'error');
                const items = res.results || [];
                const html = items.length ? items.map(i => `
                    <div class="file-item" onclick="window._openSearchResult('${escapeHtml(i.path).replace(/'/g, "\\'")}')">
                        <i class="fa-solid ${i.is_dir ? 'fa-folder' : 'fa-file'}"></i>
                        <div style="flex:1;min-width:0">
                            <div class="file-name">${escapeHtml(i.name)}</div>
                            <div class="file-meta">${escapeHtml(i.path)}</div>
                        </div>
                    </div>
                `).join('') : '<div class="empty-state">No results</div>';
                window._openSearchResult = (p) => {
                    closeModal();
                    const d = dirname(p);
                    if (d) load(d);
                };
                openModal('Search Results', html, [{ label: 'Close', primary: true, action: closeModal }], true);
            }}
        ]);
    }
    
    State.windows[winId] = State.windows[winId] || {};
    State.windows[winId].refresh = () => load(cwd);
    
    load(cwd);
}

function fileIcon(name) {
    const ext = extOf(name);
    if (['jpg','jpeg','png','gif','webp','svg','bmp','ico','tiff','tif','heic','heif','avif'].includes(ext)) return 'fa-file-image';
    if (['mp3','wav','ogg','flac','m4a','aac','opus'].includes(ext)) return 'fa-file-audio';
    if (['mp4','mkv','avi','mov','webm','flv'].includes(ext)) return 'fa-file-video';
    if (['zip','tar','gz','rar','7z','xz','bz2'].includes(ext)) return 'fa-file-zipper';
    if (['py','js','ts','html','css','json','xml','yaml','yml','sh','bash','c','cpp','h','java','go','rs','rb','php','lua'].includes(ext)) return 'fa-file-code';
    if (['pdf'].includes(ext)) return 'fa-file-pdf';
    if (['doc','docx','odt','rtf'].includes(ext)) return 'fa-file-word';
    if (['xls','xlsx','ods','csv'].includes(ext)) return 'fa-file-excel';
    if (['ppt','pptx','odp'].includes(ext)) return 'fa-file-powerpoint';
    if (['txt','md','log','conf','cfg','ini'].includes(ext)) return 'fa-file-lines';
    return 'fa-file';
}

/* ============================================================
   PROJECT README DETECTION + DEV DIALOG
   ============================================================ */
async function detectProjectInFolder(folder) {
    if (!folder) folder = State.fileCwd || '~';
    const res = await API.projectDetect(folder);
    if (res.status !== 'success') {
        toast(res.output || 'Detection failed', 'error');
        return;
    }
    if (!res.readme) {
        toast('No README found in ' + folder, 'warning');
        return;
    }
    
    const dev = res.dev || {};
    const readmeName = res.readme_name || basename(res.readme);
    const canRunDev = !!(dev.dev_command);
    
    const body = `
        <div style="font-size:13px;margin-bottom:10px">
            Found <strong>${escapeHtml(readmeName)}</strong> in
            <code style="font-family:var(--font-mono);color:var(--accent)">${escapeHtml(folder)}</code>
        </div>
        ${canRunDev ? `
            <div class="app-card" style="margin-bottom:10px">
                <i class="fa-solid fa-play" style="color:var(--success)"></i>
                <div style="flex:1">
                    <div class="app-name">Development Mode Detected</div>
                    <div class="app-desc">Command: <code style="font-family:var(--font-mono);color:var(--accent)">${escapeHtml(dev.dev_command)}</code>${dev.dev_port ? ` • Port: <strong>${escapeHtml(dev.dev_port)}</strong>` : ''}</div>
                </div>
            </div>
            <div class="text-muted" style="font-size:11px;margin-bottom:10px">
                ${(dev.reasons || []).map(r => '• ' + escapeHtml(r)).join('<br>')}
            </div>
        ` : `
            <div class="text-muted" style="font-size:12px;margin-bottom:10px">No dev configuration detected. You can still view the README.</div>
        `}
        <label>README Preview</label>
        <div class="readme-content" id="readme-preview">Loading...</div>
    `;
    
    openModal('Project Detected', body, [
        { label: 'Cancel', action: closeModal },
        { label: 'View README', action: () => viewReadme(res.readme) },
        ...(canRunDev ? [{ label: 'Start Dev Server', primary: true, action: () => {
            closeModal();
            startDevServer(folder, dev.dev_command, dev.dev_port);
        }}] : [])
    ], true);
    
    // Load README content
    API.projectReadReadme(res.readme).then(r => {
        const preview = document.getElementById('readme-preview');
        if (!preview) return;
        if (r.status === 'success') {
            preview.textContent = r.content.slice(0, 8000) + (r.content.length > 8000 ? '\n\n... (truncated)' : '');
        } else {
            preview.textContent = 'Failed to read README: ' + (r.output || 'unknown error');
        }
    });
}

function viewReadme(path) {
    API.projectReadReadme(path).then(r => {
        if (r.status !== 'success') return toast(r.output || 'Failed', 'error');
        openModal(`README — ${basename(path)}`,
            `<div class="readme-content" style="max-height:60vh">${escapeHtml(r.content)}</div>`,
            [{ label: 'Close', primary: true, action: closeModal }], true);
    });
}

function startDevServer(folder, command, port) {
    // Open a terminal window pre-loaded with the command
    const id = openApp('terminal', { forceNew: true, title: `Dev: ${basename(folder)}` });
    setTimeout(async () => {
        const input = document.getElementById(`${id}-input`);
        const out = document.getElementById(`${id}-out`);
        if (input && out) {
            input.value = '';
            out.innerHTML += `<span class="ok">▶ Launching dev server in ${escapeHtml(folder)}</span>\n`;
            out.innerHTML += `<span class="prompt">$</span> cd ${escapeHtml(folder)} && ${escapeHtml(command)}\n`;
            out.innerHTML += `<span style="color:#94a3b8">Running (5-minute timeout)...</span>\n`;
            out.scrollTop = out.scrollHeight;
            
            // Split cd and command
            const cdRes = await API.execute('cd', `${folder}`, 10);
            const res = await API.execute(command.split(' ')[0], command.split(' ').slice(1).join(' '), 300, folder);
            out.innerHTML += `<span class="${res.status === 'success' ? 'ok' : 'err'}">${escapeHtml(res.output || '(no output)')}</span>\n\n`;
            if (port) {
                out.innerHTML += `<span class="ok">→ Server should be available at http://127.0.0.1:${port}</span>\n`;
            }
            out.scrollTop = out.scrollHeight;
        }
    }, 400);
    toast('Dev server launching...', 'info');
}

/* ============================================================
   UPLOAD HELPERS
   ============================================================ */
function uploadToFolder(folder, onDone) {
    const input = document.getElementById('hidden-file-input');
    input.value = '';
    input.onchange = async () => {
        const f = input.files[0];
        if (!f) return;
        toast('Uploading ' + f.name + '...', 'info');
        const res = await API.uploadFile(f);
        if (res.status === 'success') {
            toast('Uploaded: ' + basename(res.path), 'success');
            if (onDone) onDone();
        } else {
            toast(res.output || 'Upload failed', 'error');
        }
    };
    input.click();
}

/* ============================================================
   APP: EDITOR (GUI)
   ============================================================ */
function renderEditorApp(container, winId, options = {}) {
    container.innerHTML = '';
    container.style.padding = '0';
    container.style.display = 'flex';
    container.style.flexDirection = 'column';
    container.style.overflow = 'hidden';
    
    const filePath = options.filePath || null;
    
    const toolbar = el('div', { class: 'editor-toolbar' });
    const pathLabel = el('div', { class: 'editor-path', text: filePath || 'Untitled' });
    toolbar.appendChild(pathLabel);
    
    const openBtn = el('button', { onclick: () => openFilePicker() }, [el('i', { class: 'fa-solid fa-folder-open' }), ' Open']);
    const saveBtn = el('button', { class: 'save', onclick: () => save() }, [el('i', { class: 'fa-solid fa-floppy-disk' }), ' Save']);
    const saveAsBtn = el('button', { onclick: () => saveAs() }, [el('i', { class: 'fa-solid fa-file-export' }), ' Save As']);
    const wrapBtn = el('button', { onclick: () => toggleWrap() }, [el('i', { class: 'fa-solid fa-text-width' }), ' Wrap']);
    toolbar.appendChild(openBtn);
    toolbar.appendChild(saveBtn);
    toolbar.appendChild(saveAsBtn);
    toolbar.appendChild(wrapBtn);
    
    const textarea = el('textarea', { class: 'editor-textarea', spellcheck: 'false' });
    if (State.preferences.editor_wrap !== 'false') textarea.classList.add('wrap');
    
    const status = el('div', { class: 'editor-status' }, [
        el('span', { id: `${winId}-ed-status`, text: 'Ready' }),
        el('span', { id: `${winId}-ed-pos`, text: 'Ln 1, Col 1' }),
        el('span', { id: `${winId}-ed-len`, text: '0 chars' })
    ]);
    
    container.appendChild(toolbar);
    container.appendChild(textarea);
    container.appendChild(status);
    
    let dirty = false;
    let currentPath = filePath;
    let originalContent = '';
    
    function setDirty(v) {
        dirty = v;
        State.windows[winId].dirty = v;
        const base = currentPath ? basename(currentPath) : 'Untitled';
        setWindowTitle(winId, (v ? '● ' : '') + base + ' — Editor');
    }
    
    function updateStatus() {
        const val = textarea.value;
        const sel = textarea.selectionStart;
        const before = val.slice(0, sel);
        const line = before.split('\n').length;
        const col = before.split('\n').pop().length + 1;
        document.getElementById(`${winId}-ed-pos`).textContent = `Ln ${line}, Col ${col}`;
        document.getElementById(`${winId}-ed-len`).textContent = `${val.length} chars`;
    }
    
    async function loadFile(path) {
        const res = await API.fsRead(path);
        if (res.status === 'success') {
            textarea.value = res.content;
            originalContent = res.content;
            currentPath = path;
            pathLabel.textContent = path;
            setDirty(false);
            document.getElementById(`${winId}-ed-status`).textContent = `${res.size} bytes`;
            updateStatus();
        } else {
            toast(res.output || 'Failed to read', 'error');
        }
    }
    
    async function save() {
        if (!currentPath) return saveAs();
        const res = await API.fsWrite(currentPath, textarea.value);
        if (res.status === 'success') {
            originalContent = textarea.value;
            setDirty(false);
            document.getElementById(`${winId}-ed-status`).textContent = 'Saved';
            toast('Saved', 'success');
        } else {
            toast(res.output, 'error');
        }
    }
    
    function saveAs() {
        openModal('Save As', `
            <label>Path</label>
            <input type="text" id="dlg-saveas" value="${escapeHtml(currentPath || joinPath('~', 'untitled.txt'))}" autofocus>
        `, [
            { label: 'Cancel', action: closeModal },
            { label: 'Save', primary: true, action: async () => {
                const p = document.getElementById('dlg-saveas').value.trim();
                if (!p) return;
                const res = await API.fsWrite(p, textarea.value);
                if (res.status === 'success') {
                    currentPath = p;
                    pathLabel.textContent = p;
                    originalContent = textarea.value;
                    setDirty(false);
                    toast('Saved', 'success');
                    closeModal();
                } else toast(res.output, 'error');
            }}
        ]);
    }
    
    function openFilePicker() {
        openModal('Open File', `
            <label>Path</label>
            <input type="text" id="dlg-openpath" value="${escapeHtml(currentPath || '~')}" autofocus>
        `, [
            { label: 'Cancel', action: closeModal },
            { label: 'Open', primary: true, action: () => {
                const p = document.getElementById('dlg-openpath').value.trim();
                if (!p) return;
                closeModal();
                loadFile(p);
            }}
        ]);
    }
    
    function toggleWrap() {
        textarea.classList.toggle('wrap');
        API.post('/api/preferences', { editor_wrap: textarea.classList.contains('wrap') ? 'true' : 'false' });
    }
    
    textarea.addEventListener('input', () => {
        setDirty(textarea.value !== originalContent);
        updateStatus();
    });
    textarea.addEventListener('keyup', updateStatus);
    textarea.addEventListener('click', updateStatus);
    textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Tab') {
            e.preventDefault();
            const start = textarea.selectionStart;
            const end = textarea.selectionEnd;
            textarea.value = textarea.value.slice(0, start) + '    ' + textarea.value.slice(end);
            textarea.selectionStart = textarea.selectionEnd = start + 4;
            setDirty(true);
        }
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
            e.preventDefault();
            save();
        }
    });
    
    State.windows[winId].saveFn = save;
    State.windows[winId].isDirty = () => dirty;
    
    if (filePath) loadFile(filePath);
    else setDirty(false);
    
    setTimeout(() => textarea.focus(), 100);
}

/* ============================================================
   APP: SETTINGS (with wallpaper upload)
   ============================================================ */
function renderSettingsApp(container, winId) {
    container.innerHTML = '';
    const tabs = el('div', { class: 'tab-bar' });
    const tabNames = ['Appearance', 'Desktop', 'Editor', 'Workspaces', 'About'];
    const panels = {};
    
    tabNames.forEach((name, idx) => {
        const btn = el('button', { class: 'tab-btn' + (idx === 0 ? ' active' : ''), onclick: () => {
            container.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            Object.entries(panels).forEach(([k, p]) => p.style.display = k === name ? '' : 'none');
        }}, name);
        tabs.appendChild(btn);
    });
    container.appendChild(tabs);
    
    // Appearance
    const appearance = el('div', {});
    appearance.appendChild(el('label', { text: 'Theme' }));
    const themeSelect = el('select', { onchange: (e) => applyTheme(e.target.value) });
    State.themes.forEach(t => {
        const opt = el('option', { value: t.id, text: t.name });
        if (t.id === State.preferences.theme) opt.selected = true;
        themeSelect.appendChild(opt);
    });
    appearance.appendChild(themeSelect);
    
    appearance.appendChild(el('label', { text: 'Wallpaper' }));
    appearance.appendChild(el('div', { class: 'text-muted', style: { fontSize: '11px', marginBottom: '8px' } }, [
        'Only solid colors and your uploaded images. Upload from device storage below.'
    ]));
    
    // Upload button
    const uploadBtn = el('button', { class: 'primary', style: { marginBottom: '12px' }, onclick: () => {
        const input = document.getElementById('hidden-wallpaper-input');
        input.value = '';
        input.onchange = () => {
            const f = input.files[0];
            if (!f) return;
            if (f.size > 8 * 1024 * 1024) {
                toast('Image too large (max 8MB)', 'error');
                return;
            }
            const reader = new FileReader();
            reader.onload = async () => {
                const dataUri = reader.result;
                const name = prompt('Wallpaper name:', f.name.replace(/\.[^.]+$/, '')) || f.name;
                const res = await API.uploadWallpaper(name, dataUri);
                if (res.status === 'success') {
                    toast('Wallpaper uploaded', 'success');
                    const wp = await API.get('/api/wallpapers');
                    State.wallpapers = wp.wallpapers || [];
                    applyWallpaper(res.id);
                    renderSettingsApp(container, winId);
                } else {
                    toast(res.output || 'Upload failed', 'error');
                }
            };
            reader.readAsDataURL(f);
        };
        input.click();
    }}, [el('i', { class: 'fa-solid fa-upload' }), ' Upload Wallpaper from Storage']);
    appearance.appendChild(uploadBtn);
    
    const wpGrid = el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))', gap: '8px', marginBottom: '12px' } });
    State.wallpapers.forEach(wp => {
        const isActive = State.preferences.wallpaper === wp.id;
        const thumb = el('div', {
            style: {
                width: '100%', height: '70px',
                borderRadius: '6px', marginBottom: '4px',
                border: isActive ? '2px solid var(--accent)' : '1px solid var(--border)',
                background: wp.type === 'custom' ? 'var(--bg)' : (wp.color || '#090d16'),
                backgroundImage: wp.type === 'custom' ? `url('${wp.url}')` : 'none',
                backgroundSize: 'cover',
                backgroundPosition: 'center',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                position: 'relative',
                overflow: 'hidden'
            }
        }, wp.type === 'solid' ? [el('i', { class: 'fa-solid fa-fill-drip', style: { color: 'var(--muted)' } })] : []);
        
        const card = el('div', {
            style: { padding: '6px', cursor: 'pointer', textAlign: 'center', borderRadius: '8px', background: isActive ? 'rgba(56,189,248,0.1)' : 'transparent' },
            onclick: () => { applyWallpaper(wp.id); renderSettingsApp(container, winId); }
        }, [
            thumb,
            el('div', { style: { fontSize: '10px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, text: wp.name }),
            wp.type === 'custom' ? el('button', {
                class: 'secondary',
                style: { width: 'auto', padding: '2px 6px', fontSize: '10px', marginTop: '4px' },
                onclick: async (e) => {
                    e.stopPropagation();
                    if (!confirm('Delete this wallpaper?')) return;
                    await API.deleteWallpaper(wp.id);
                    const r = await API.get('/api/wallpapers');
                    State.wallpapers = r.wallpapers || [];
                    if (State.preferences.wallpaper === wp.id) applyWallpaper('solid-dark');
                    renderSettingsApp(container, winId);
                }
            }, [el('i', { class: 'fa-solid fa-trash' })]) : null
        ].filter(Boolean));
        wpGrid.appendChild(card);
    });
    appearance.appendChild(wpGrid);
    panels['Appearance'] = appearance;
    
    // Desktop
    const desktop = el('div', {});
    desktop.appendChild(el('label', { text: 'Desktop Settings' }));
    [
        { key: 'window_animations', label: 'Window Animations', options: ['true', 'false'] },
        { key: 'desktop_icons', label: 'Show Desktop Icons', options: ['true', 'false'] },
        { key: 'compositor', label: 'Compositor Effects', options: ['true', 'false'] },
        { key: 'confirm_delete', label: 'Confirm Before Delete', options: ['true', 'false'] },
        { key: 'show_hidden_files', label: 'Show Hidden Files', options: ['true', 'false'] },
        { key: 'restore_window_state', label: 'Restore Window State on Open', options: ['true', 'false'] },
    ].forEach(item => {
        const row = el('div', { style: { marginBottom: '10px' } }, [el('label', { text: item.label })]);
        const sel = el('select', { onchange: (e) => API.post('/api/preferences', { [item.key]: e.target.value }).then(() => { State.preferences[item.key] = e.target.value; }) });
        item.options.forEach(o => {
            const opt = el('option', { value: o, text: o });
            if (State.preferences[item.key] === o) opt.selected = true;
            sel.appendChild(opt);
        });
        row.appendChild(sel);
        desktop.appendChild(row);
    });
    desktop.appendChild(el('button', { class: 'danger mt-8', onclick: async () => {
        if (!confirm('Reset all saved window positions and sizes?')) return;
        const res = await API.get('/api/window_states');
        for (const k of Object.keys(res.states || {})) {
            await API.deleteWindowState(k);
        }
        State.windowStates = {};
        toast('Window states reset', 'success');
    }}, [el('i', { class: 'fa-solid fa-rotate-left' }), ' Reset Window Layouts']));
    panels['Desktop'] = desktop;
    
    // Editor
    const editorPanel = el('div', {});
    editorPanel.appendChild(el('label', { text: 'Default Editor' }));
    const edSel = el('select', { onchange: (e) => API.post('/api/preferences', { default_editor: e.target.value }).then(() => { State.preferences.default_editor = e.target.value; }) });
    [['ask', 'Ask every time'], ...State.editors.map(e => [e.cmd, e.name])].forEach(([v, l]) => {
        const opt = el('option', { value: v, text: l });
        if (State.preferences.default_editor === v) opt.selected = true;
        edSel.appendChild(opt);
    });
    editorPanel.appendChild(edSel);
    
    editorPanel.appendChild(el('label', { text: 'Detected Editors' }));
    if (State.editors.length === 0) {
        editorPanel.appendChild(el('div', { class: 'text-muted', style: { fontSize: '12px' }, text: 'No editors detected. Install nano or vim: pkg install nano vim' }));
    } else {
        State.editors.forEach(e => {
            editorPanel.appendChild(el('div', { class: 'app-card' }, [
                el('i', { class: `fa-solid ${e.icon}` }),
                el('div', {}, [
                    el('div', { class: 'app-name', text: e.name }),
                    el('div', { class: 'app-desc', text: `${e.cmd} (${e.source})` })
                ])
            ]));
        });
    }
    panels['Editor'] = editorPanel;
    
    // Workspaces
    const ws = el('div', {});
    ws.appendChild(el('label', { text: 'Workspaces' }));
    State.workspaces.forEach(w => {
        ws.appendChild(el('div', { class: 'app-card' }, [
            el('i', { class: 'fa-solid fa-layer-group' }),
            el('div', { style: { flex: '1' } }, [
                el('div', { class: 'app-name', text: w.name }),
                el('div', { class: 'app-desc', text: `Position ${w.position}` })
            ]),
            el('button', { class: 'secondary', style: { width: 'auto', padding: '4px 10px' }, onclick: async () => {
                if (State.workspaces.length <= 1) return toast('Cannot delete last workspace', 'warning');
                await API.del(`/api/workspaces/${w.id}`);
                const r = await API.get('/api/workspaces');
                State.workspaces = r.workspaces;
                renderWorkspaceIndicator();
                renderSettingsApp(container, winId);
            }}, [el('i', { class: 'fa-solid fa-trash' })])
        ]));
    });
    ws.appendChild(el('button', { class: 'primary mt-8', onclick: async () => {
        const name = prompt('Workspace name:', 'New Workspace');
        if (name) {
            await API.post('/api/workspaces', { name });
            const r = await API.get('/api/workspaces');
            State.workspaces = r.workspaces;
            renderWorkspaceIndicator();
            renderSettingsApp(container, winId);
        }
    }}, [el('i', { class: 'fa-solid fa-plus' }), ' Add Workspace']));
    panels['Workspaces'] = ws;
    
    // About
    panels['About'] = el('div', { class: 'empty-state' }, [
        el('i', { class: 'fa-brands fa-linux' }),
        el('div', { style: { fontSize: '16px', fontWeight: '700', color: 'var(--text)', marginBottom: '6px' }, text: 'Termux WebOS' }),
        el('div', { text: 'Version 2.5.0 — Kali XFCE4 Edition' }),
    ]);
    
    Object.entries(panels).forEach(([name, p]) => {
        p.style.display = name === 'Appearance' ? '' : 'none';
        container.appendChild(p);
    });
}

/* ============================================================
   APP: COMMAND CENTER
   ============================================================ */
function renderCommandsApp(container, winId) {
    container.innerHTML = '';
    const tabs = el('div', { class: 'tab-bar' });
    const tabNames = ['Presets', 'All Commands', 'Custom'];
    const panels = {};
    
    tabNames.forEach((name, idx) => {
        const btn = el('button', { class: 'tab-btn' + (idx === 0 ? ' active' : ''), onclick: () => {
            container.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            Object.entries(panels).forEach(([k, p]) => p.style.display = k === name ? '' : 'none');
        }}, name);
        tabs.appendChild(btn);
    });
    container.appendChild(tabs);
    
    const execPanel = el('div', {});
    execPanel.appendChild(el('label', { text: 'Select Command' }));
    const dropdownContainer = el('div', { class: 'os-dropdown-container' });
    const dropdownBtn = el('button', { class: 'os-dropdown-btn', onclick: (e) => { e.stopPropagation(); dropdownContainer.querySelector('.os-dropdown-menu').classList.toggle('show'); }}, [
        el('span', { text: 'Choose command...' }),
        el('i', { class: 'fa-solid fa-chevron-down' })
    ]);
    const dropdownMenu = el('div', { class: 'os-dropdown-menu' });
    const presets = [
        { id: 'custom-command', label: 'Run Custom Command', icon: 'fa-terminal' },
        { id: 'detect-project', label: 'Detect Project (README)', icon: 'fa-diagram-project' },
        { id: 'launch-app', label: 'Launch App', icon: 'fa-rocket' },
        { id: 'termux-contact-list', label: 'Contacts List', icon: 'fa-address-book' },
        { id: 'termux-sms-send', label: 'Send SMS', icon: 'fa-paper-plane' },
        { id: 'termux-sms-list', label: 'SMS Inbox', icon: 'fa-inbox' },
        { id: 'termux-notification', label: 'Show Notification', icon: 'fa-bell' },
        { id: 'termux-toast', label: 'Show Toast', icon: 'fa-comment-dots' },
        { id: 'termux-tts-speak', label: 'Text to Speech', icon: 'fa-volume-high' },
        { id: 'termux-open-url', label: 'Open URL', icon: 'fa-globe' },
        { id: 'termux-telephony-call', label: 'Make Call', icon: 'fa-phone' },
        { id: 'termux-vibrate', label: 'Vibrate', icon: 'fa-mobile-retro' },
        { id: 'termux-battery-status', label: 'Battery Status', icon: 'fa-battery-full' },
        { id: 'termux-camera-photo', label: 'Take Photo', icon: 'fa-camera' },
        { id: 'termux-location', label: 'Get Location', icon: 'fa-location-dot' },
        { id: 'termux-wifi-connectioninfo', label: 'WiFi Info', icon: 'fa-wifi' },
    ];
    presets.forEach(p => {
        dropdownMenu.appendChild(el('button', { class: 'dropdown-item preset-item', onclick: (e) => {
            e.stopPropagation();
            dropdownMenu.classList.remove('show');
            dropdownBtn.querySelector('span').textContent = p.label;
            renderPresetForm(execPanel, p.id);
        }}, [
            el('i', { class: `fa-solid ${p.icon}` }),
            el('span', { text: p.label })
        ]));
    });
    dropdownContainer.appendChild(dropdownBtn);
    dropdownContainer.appendChild(dropdownMenu);
    execPanel.appendChild(dropdownContainer);
    
    const formContainer = el('div', { id: `${winId}-preset-form` });
    execPanel.appendChild(formContainer);
    panels['Presets'] = execPanel;
    
    const allPanel = el('div', {});
    allPanel.appendChild(el('label', { text: 'Available Binaries' }));
    const cmdList = el('div', { style: { maxHeight: '350px', overflowY: 'auto' } });
    if (State.commands.length === 0) {
        cmdList.innerHTML = '<div class="empty-state">No commands discovered.</div>';
    } else {
        State.commands.forEach(cmd => {
            cmdList.appendChild(el('div', { class: 'app-card', onclick: () => {
                renderPresetForm(execPanel, 'custom-command', cmd);
                container.querySelectorAll('.tab-btn').forEach((b, i) => b.classList.toggle('active', i === 0));
                Object.entries(panels).forEach(([k, p]) => p.style.display = k === 'Presets' ? '' : 'none');
            }}, [
                el('i', { class: 'fa-solid fa-terminal' }),
                el('div', {}, [
                    el('div', { class: 'app-name', text: cmd }),
                    el('div', { class: 'app-desc', text: 'Binary in PATH' })
                ])
            ]));
        });
    }
    allPanel.appendChild(cmdList);
    panels['All Commands'] = allPanel;
    
    const customPanel = el('div', {});
    customPanel.appendChild(el('label', { text: 'Custom Command' }));
    const customInput = el('input', { type: 'text', placeholder: 'e.g. ls -la /sdcard', id: `${winId}-custom-cmd` });
    customPanel.appendChild(customInput);
    customPanel.appendChild(el('button', { class: 'primary', onclick: () => runCustomCommand(container, winId) }, [
        el('i', { class: 'fa-solid fa-play' }), ' Execute'
    ]));
    customPanel.appendChild(el('div', { class: 'text-muted mt-8', style: { fontSize: '11px' }, text: 'Dev/server commands (npm, pip, flask, python, node, vite, etc.) automatically get a 5-minute timeout.' }));
    panels['Custom'] = customPanel;
    
    Object.entries(panels).forEach(([name, p]) => {
        p.style.display = name === 'Presets' ? '' : 'none';
        container.appendChild(p);
    });
    
    renderPresetForm(execPanel, 'custom-command');
}

function renderPresetForm(container, presetId, initialCmd) {
    const formContainer = container.querySelector('div[id$="-preset-form"]');
    if (!formContainer) return;
    formContainer.innerHTML = '';
    
    const builders = {
        'custom-command': () => {
            const inp = el('input', { type: 'text', placeholder: 'Enter shell command...', value: initialCmd || '' });
            formContainer.appendChild(el('label', { text: 'Command' }));
            formContainer.appendChild(inp);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const cmd = inp.value.trim();
                if (!cmd) return;
                const sp = cmd.indexOf(' ');
                const command = sp === -1 ? cmd : cmd.slice(0, sp);
                const args = sp === -1 ? '' : cmd.slice(sp + 1);
                const res = await API.execute(command, args);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-play' }), ' Execute']));
        },
        'detect-project': () => {
            const inp = el('input', { type: 'text', placeholder: 'Path to project folder', value: State.fileCwd || '~' });
            formContainer.appendChild(el('label', { text: 'Folder' }));
            formContainer.appendChild(inp);
            formContainer.appendChild(el('button', { class: 'primary', onclick: () => {
                const p = inp.value.trim();
                if (!p) return;
                detectProjectInFolder(p);
            }}, [el('i', { class: 'fa-solid fa-diagram-project' }), ' Detect']));
        },
        'launch-app': () => {
            const inp = el('input', { type: 'text', placeholder: 'App name or package (e.g. com.whatsapp)' });
            formContainer.appendChild(el('label', { text: 'App Package or Name' }));
            formContainer.appendChild(inp);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const v = inp.value.trim();
                if (!v) return;
                const pkgs = { whatsapp:'com.whatsapp', youtube:'com.google.android.youtube', chrome:'com.android.chrome', settings:'com.android.settings', camera:'com.android.camera', calculator:'com.google.android.calculator', spotify:'com.spotify.music' };
                const pkg = pkgs[v.toLowerCase()] || v;
                const res = await API.execute('monkey', `-p ${pkg} 1`);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-rocket' }), ' Launch']));
        },
        'termux-contact-list': () => {
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-contact-list', '');
                openApp('contacts');
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-address-book' }), ' List Contacts']));
        },
        'termux-sms-send': () => {
            const num = el('input', { type: 'text', placeholder: '+1234567890' });
            const msg = el('textarea', { rows: 3, placeholder: 'Message body...' });
            formContainer.appendChild(el('label', { text: 'Recipient' }));
            formContainer.appendChild(num);
            formContainer.appendChild(el('label', { text: 'Message' }));
            formContainer.appendChild(msg);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-sms-send', `-n ${num.value} "${msg.value.replace(/"/g,'\\"')}"`);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-paper-plane' }), ' Send SMS']));
        },
        'termux-sms-list': () => {
            formContainer.appendChild(el('button', { class: 'primary', onclick: () => openApp('sms') }, [
                el('i', { class: 'fa-solid fa-inbox' }), ' View Inbox'
            ]));
        },
        'termux-notification': () => {
            const t = el('input', { type: 'text', placeholder: 'Title' });
            const c = el('input', { type: 'text', placeholder: 'Content' });
            formContainer.appendChild(el('label', { text: 'Title' }));
            formContainer.appendChild(t);
            formContainer.appendChild(el('label', { text: 'Content' }));
            formContainer.appendChild(c);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-notification', `-t "${t.value}" -c "${c.value}"`);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-bell' }), ' Send Notification']));
        },
        'termux-toast': () => {
            const m = el('input', { type: 'text', placeholder: 'Toast text' });
            formContainer.appendChild(el('label', { text: 'Message' }));
            formContainer.appendChild(m);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-toast', `"${m.value}"`);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-comment-dots' }), ' Show Toast']));
        },
        'termux-tts-speak': () => {
            const t = el('input', { type: 'text', placeholder: 'Text to speak' });
            formContainer.appendChild(el('label', { text: 'Text' }));
            formContainer.appendChild(t);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-tts-speak', `"${t.value}"`);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-volume-high' }), ' Speak']));
        },
        'termux-open-url': () => {
            const u = el('input', { type: 'text', placeholder: 'https://example.com' });
            formContainer.appendChild(el('label', { text: 'URL' }));
            formContainer.appendChild(u);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                let v = u.value.trim();
                if (v && !/^https?:\/\//.test(v)) v = 'https://' + v;
                const res = await API.execute('termux-open-url', v);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-globe' }), ' Open URL']));
        },
        'termux-telephony-call': () => {
            const p = el('input', { type: 'text', placeholder: 'Phone number' });
            formContainer.appendChild(el('label', { text: 'Number' }));
            formContainer.appendChild(p);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-telephony-call', p.value);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-phone' }), ' Call']));
        },
        'termux-vibrate': () => {
            const d = el('input', { type: 'number', value: '1000' });
            formContainer.appendChild(el('label', { text: 'Duration (ms)' }));
            formContainer.appendChild(d);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-vibrate', `-d ${d.value}`);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-mobile-retro' }), ' Vibrate']));
        },
        'termux-battery-status': () => {
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-battery-status', '');
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-battery-full' }), ' Get Battery']));
        },
        'termux-camera-photo': () => {
            const p = el('input', { type: 'text', value: '/sdcard/photo.jpg' });
            formContainer.appendChild(el('label', { text: 'Output Path' }));
            formContainer.appendChild(p);
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-camera-photo', `-c 0 ${p.value}`);
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-camera' }), ' Take Photo']));
        },
        'termux-location': () => {
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-location', '');
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-location-dot' }), ' Get Location']));
        },
        'termux-wifi-connectioninfo': () => {
            formContainer.appendChild(el('button', { class: 'primary', onclick: async () => {
                const res = await API.execute('termux-wifi-connectioninfo', '');
                alertOutput(res);
            }}, [el('i', { class: 'fa-solid fa-wifi' }), ' Get WiFi Info']));
        }
    };
    (builders[presetId] || builders['custom-command'])();
}

async function runCustomCommand(container, winId) {
    const inp = document.getElementById(`${winId}-custom-cmd`);
    const cmd = inp.value.trim();
    if (!cmd) return;
    const sp = cmd.indexOf(' ');
    const command = sp === -1 ? cmd : cmd.slice(0, sp);
    const args = sp === -1 ? '' : cmd.slice(sp + 1);
    const res = await API.execute(command, args);
    alertOutput(res);
}

function alertOutput(res) {
    openModal('Command Output', `<div class="terminal">${escapeHtml(res.output || '(no output)')}</div>`, [
        { label: 'Close', action: closeModal, primary: true }
    ]);
}

/* ============================================================
   APP: SMS, CONTACTS, LOGS, APPS, BOOKMARKS, CLIPBOARD, XFCE, ABOUT
   ============================================================ */
function renderSMSApp(container, winId) {
    container.innerHTML = '';
    container.appendChild(el('button', { class: 'primary mb-12', onclick: () => loadSMS(container) }, [
        el('i', { class: 'fa-solid fa-rotate-right' }), ' Refresh Inbox'
    ]));
    const list = el('div', { id: `${winId}-sms-list` });
    container.appendChild(list);
    loadSMS(container);
}

async function loadSMS(container) {
    const list = container.querySelector('[id$="-sms-list"]');
    list.innerHTML = '<div class="empty-state"><i class="fa-solid fa-spinner fa-spin"></i>Loading SMS...</div>';
    const res = await API.execute('termux-sms-list', '-l 20');
    try {
        const msgs = JSON.parse(res.output);
        if (!Array.isArray(msgs) || msgs.length === 0) {
            list.innerHTML = '<div class="empty-state"><i class="fa-solid fa-inbox"></i>No messages</div>';
            return;
        }
        list.innerHTML = '';
        msgs.forEach(m => {
            list.appendChild(el('div', { class: 'sms-card' }, [
                el('div', { class: 'flex-between', style: { fontSize: '11px', color: 'var(--muted)', fontWeight: '600' } }, [
                    el('span', {}, [el('i', { class: 'fa-solid fa-user' }), ' ' + escapeHtml(m.sender || m.number || 'Unknown')]),
                    el('span', { text: m.received || m.timestamp || '' })
                ]),
                el('div', { style: { fontSize: '13px', marginTop: '6px' }, text: m.body || '(no body)' })
            ]));
        });
    } catch (e) {
        list.innerHTML = `<div class="terminal">${escapeHtml(res.output)}</div>`;
    }
}

function renderContactsApp(container, winId) {
    container.innerHTML = '';
    container.appendChild(el('button', { class: 'primary mb-12', onclick: () => loadContacts(container) }, [
        el('i', { class: 'fa-solid fa-rotate-right' }), ' Load Contacts'
    ]));
    const list = el('div', { id: `${winId}-contacts-list` });
    container.appendChild(list);
    loadContacts(container);
}

async function loadContacts(container) {
    const list = container.querySelector('[id$="-contacts-list"]');
    list.innerHTML = '<div class="empty-state"><i class="fa-solid fa-spinner fa-spin"></i>Loading contacts...</div>';
    const res = await API.execute('termux-contact-list', '');
    try {
        const contacts = JSON.parse(res.output);
        if (!Array.isArray(contacts) || contacts.length === 0) {
            list.innerHTML = '<div class="empty-state"><i class="fa-solid fa-address-book"></i>No contacts</div>';
            return;
        }
        list.innerHTML = '';
        contacts.forEach(c => {
            const num = c.number || c.phone || '';
            list.appendChild(el('div', { class: 'contact-card' }, [
                el('div', { class: 'contact-info' }, [
                    el('div', { class: 'contact-name', html: `<i class="fa-solid fa-user"></i> ${escapeHtml(c.name || 'Unknown')}` }),
                    el('div', { class: 'contact-num', text: num || 'No number' })
                ]),
                el('div', { class: 'contact-actions' }, [
                    num ? el('button', { class: 'action-btn action-call', onclick: () => quickCall(num) }, [el('i', { class: 'fa-solid fa-phone' }), ' Call']) : null,
                    num ? el('button', { class: 'action-btn action-msg', onclick: () => quickSMS(num) }, [el('i', { class: 'fa-solid fa-comment-sms' }), ' SMS']) : null
                ].filter(Boolean))
            ]));
        });
    } catch (e) {
        list.innerHTML = `<div class="terminal">${escapeHtml(res.output)}</div>`;
    }
}

async function quickCall(number) {
    const res = await API.execute('termux-telephony-call', number);
    alertOutput(res);
}

function quickSMS(number) {
    openApp('commands');
    toast('Send SMS to ' + number + ' via Command Center', 'info');
}

function renderLogsApp(container, winId) {
    container.innerHTML = '';
    container.appendChild(el('button', { class: 'primary mb-12', onclick: () => loadLogs(container) }, [
        el('i', { class: 'fa-solid fa-rotate-right' }), ' Refresh'
    ]));
    const list = el('div', { id: `${winId}-logs-list` });
    container.appendChild(list);
    loadLogs(container);
}

async function loadLogs(container) {
    const list = container.querySelector('[id$="-logs-list"]');
    list.innerHTML = '<div class="empty-state"><i class="fa-solid fa-spinner fa-spin"></i>Loading logs...</div>';
    const res = await API.get('/api/logs');
    list.innerHTML = '';
    if (!res.logs || res.logs.length === 0) {
        list.innerHTML = '<div class="empty-state"><i class="fa-solid fa-list"></i>No logs</div>';
        return;
    }
    res.logs.forEach(log => {
        list.appendChild(el('div', { class: 'log-item' }, [
            el('div', { class: 'flex-between' }, [
                el('strong', { style: { fontSize: '12px' }, text: `${log.command} ${log.args || ''}` }),
                el('span', { class: `status-tag status-${log.status}`, text: log.status })
            ]),
            el('div', { class: 'text-muted', style: { fontSize: '10px', marginTop: '4px' }, text: log.executed_at })
        ]));
    });
}

function renderAppsApp(container, winId) {
    container.innerHTML = '';
    State.apps.forEach(app => {
        container.appendChild(el('div', { class: 'app-card', onclick: () => {
            if (APP_REGISTRY[app.app_id]) openApp(app.app_id);
            else openCustomApp(app);
        }}, [
            el('i', { class: `fa-solid ${app.icon || 'fa-cube'}` }),
            el('div', {}, [
                el('div', { class: 'app-name', text: app.name }),
                el('div', { class: 'app-desc', text: app.executable || app.app_id })
            ])
        ]));
    });
}

function renderCustomApp(container, app) {
    container.innerHTML = '';
    container.appendChild(el('div', { class: 'empty-state' }, [
        el('i', { class: `fa-solid ${app.icon || 'fa-cube'}` }),
        el('div', { style: { fontSize: '16px', fontWeight: '700', marginBottom: '8px' }, text: app.name }),
        el('div', { class: 'text-muted', style: { fontSize: '12px' }, text: `Executable: ${app.executable}` })
    ]));
    container.appendChild(el('button', { class: 'primary', onclick: async () => {
        const res = await API.execute(app.executable, app.args || '');
        alertOutput(res);
    }}, [el('i', { class: 'fa-solid fa-play' }), ' Run']));
}

function renderBookmarksApp(container, winId) {
    container.innerHTML = '';
    container.appendChild(el('button', { class: 'primary mb-12', onclick: () => {
        const label = prompt('Bookmark label:'); if (!label) return;
        const path = prompt('Path:', '~'); if (!path) return;
        API.post('/api/bookmarks', { label, path }).then(() => {
            API.get('/api/bookmarks').then(r => {
                State.bookmarks = r.bookmarks;
                renderBookmarksApp(container, winId);
            });
        });
    }}, [el('i', { class: 'fa-solid fa-plus' }), ' Add Bookmark']));
    if (!State.bookmarks.length) {
        container.appendChild(el('div', { class: 'empty-state' }, [el('i', { class: 'fa-solid fa-bookmark' }), 'No bookmarks']));
        return;
    }
    State.bookmarks.forEach(b => {
        container.appendChild(el('div', { class: 'app-card' }, [
            el('i', { class: `fa-solid ${b.icon || 'fa-folder'}` }),
            el('div', { style: { flex: '1' }, onclick: () => openApp('files') }, [
                el('div', { class: 'app-name', text: b.label }),
                el('div', { class: 'app-desc', text: b.path })
            ]),
            el('button', { class: 'secondary', style: { width: 'auto', padding: '4px 10px' }, onclick: async (e) => {
                e.stopPropagation();
                await API.del(`/api/bookmarks/${b.id}`);
                const r = await API.get('/api/bookmarks');
                State.bookmarks = r.bookmarks;
                renderBookmarksApp(container, winId);
            }}, [el('i', { class: 'fa-solid fa-trash' })])
        ]));
    });
}

function renderClipboardApp(container, winId) {
    container.innerHTML = '';
    container.appendChild(el('button', { class: 'primary mb-12', onclick: () => {
        const content = prompt('Clipboard content:'); if (!content) return;
        API.post('/api/clipboard', { content }).then(() => {
            API.get('/api/clipboard').then(r => {
                State.clipboard = r.items;
                renderClipboardApp(container, winId);
            });
        });
    }}, [el('i', { class: 'fa-solid fa-paste' }), ' Add Item']));
    if (!State.clipboard.length) {
        container.appendChild(el('div', { class: 'empty-state' }, [el('i', { class: 'fa-solid fa-clipboard' }), 'Clipboard empty']));
        return;
    }
    State.clipboard.forEach(item => {
        container.appendChild(el('div', { class: 'log-item' }, [
            el('div', { style: { fontSize: '12px', wordBreak: 'break-all' }, text: item.content }),
            el('div', { class: 'flex-between', style: { marginTop: '6px' } }, [
                el('div', { class: 'text-muted', style: { fontSize: '10px' }, text: item.created_at }),
                el('button', { class: 'secondary', style: { width: 'auto', padding: '2px 8px', fontSize: '10px' }, onclick: async () => {
                    await API.del(`/api/clipboard/${item.id}`);
                    const r = await API.get('/api/clipboard');
                    State.clipboard = r.items;
                    renderClipboardApp(container, winId);
                }}, [el('i', { class: 'fa-solid fa-trash' })])
            ])
        ]));
    });
}

function renderXFCEApp(container, winId) {
    container.innerHTML = '';
    const info = el('div', { class: 'terminal', style: { marginBottom: '12px' } });
    info.innerHTML = `<span class="ok">XFCE4 Desktop Environment — Kali Linux</span>\n\n`;
    info.innerHTML += `Detected XFCE4 components:\n`;
    if (State.kaliDE.length === 0) {
        info.innerHTML += `  (none detected — install Kali chroot: pkg install kali)\n`;
    } else {
        State.kaliDE.forEach(c => { info.innerHTML += `  ✓ ${c}\n`; });
    }
    container.appendChild(info);
    
    const grid = el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: '8px' } });
    const components = [
        { label: 'Start XFCE4 Session', icon: 'fa-play', cmd: 'startxfce4' },
        { label: 'XFCE4 Terminal', icon: 'fa-terminal', cmd: 'xfce4-terminal' },
        { label: 'File Manager', icon: 'fa-folder', cmd: 'thunar' },
        { label: 'Text Editor', icon: 'fa-file-pen', cmd: 'mousepad' },
        { label: 'Task Manager', icon: 'fa-list-check', cmd: 'xfce4-taskmanager' },
        { label: 'Screenshot', icon: 'fa-camera', cmd: 'xfce4-screenshooter' },
    ];
    components.forEach(c => {
        grid.appendChild(el('div', { class: 'app-card', onclick: async () => {
            const res = await API.execute(c.cmd, '');
            alertOutput(res);
        }}, [
            el('i', { class: `fa-solid ${c.icon}` }),
            el('div', {}, [
                el('div', { class: 'app-name', text: c.label }),
                el('div', { class: 'app-desc', text: c.cmd })
            ])
        ]));
    });
    container.appendChild(grid);
}

function renderAboutApp(container, winId) {
    container.innerHTML = `
        <div class="empty-state" style="padding:20px">
            <i class="fa-brands fa-linux" style="font-size:64px;color:var(--accent);margin-bottom:16px"></i>
            <div style="font-size:22px;font-weight:700;margin-bottom:8px">Termux WebOS</div>
            <div class="text-muted" style="margin-bottom:20px">Version 2.5.0 — Kali XFCE4 Edition</div>
            <div style="text-align:left;font-size:13px;line-height:1.8">
                <div><strong class="text-accent">Architecture:</strong> Flask + SQLite + Web OS Layer</div>
                <div><strong class="text-accent">Desktop:</strong> XFCE4 (Kali Linux themed)</div>
                <div><strong class="text-accent">Media:</strong> Image viewer, Video player, Audio player</div>
                <div><strong class="text-accent">Projects:</strong> Auto README + dev-server detection</div>
                <div><strong class="text-accent">Shortcuts:</strong></div>
                <div>• Ctrl+Alt+T — Terminal</div>
                <div>• Ctrl+Alt+M — Media Viewer</div>
                <div>• Escape — Close menus</div>
                <div>• Right-click — Context menu</div>
                <div>• Double-click — Open file/folder</div>
            </div>
        </div>
    `;
}

function lockScreen() {
    toast('Screen locked (simulated)', 'info');
}

/* ============================================================
   BOOT
   ============================================================ */
window.addEventListener('DOMContentLoaded', boot);
</script>
</body>
</html>
"""

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("  Termux WebOS — Kali XFCE4 Edition v2.5.0")
    print("=" * 60)
    print("  Access: http://127.0.0.1:5000")
    print("  Bind:   0.0.0.0:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
