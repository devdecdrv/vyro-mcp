"""
vyro-mcp — Windows AI Control Server
Full GUI dashboard + HTTP API. AI connects via HTTPS tunnel.
Run as Administrator for full access.
"""

import asyncio
import base64
import ctypes
import io
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Auto-install dependencies ─────────────────────────────────────────────────
def _pip(*pkgs):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *pkgs],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def _ensure_deps():
    missing = []
    checks = {
        "flask": "flask",
        "flask_cors": "flask-cors",
        "PIL": "pillow",
        "cv2": "opencv-python",
        "psutil": "psutil",
        "pyttsx3": "pyttsx3",
    }
    for mod, pkg in checks.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Installing: {', '.join(missing)}")
        _pip(*missing)

_ensure_deps()

# ── Imports after ensure ──────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, scrolledtext, font as tkfont
import threading

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from PIL import Image, ImageGrab, ImageTk
import psutil
import pyttsx3

# Optional
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import pyaudio  # type: ignore
    HAS_AUDIO = True
except Exception:
    HAS_AUDIO = False
    pyaudio = None

# Windows-only
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    import winreg
    try:
        import comtypes.client
        from comtypes.gen import UIAutomationClient as UIA
        HAS_UIA = True
    except Exception:
        HAS_UIA = False
    try:
        import pygetwindow as gw
        HAS_GW = True
    except Exception:
        HAS_GW = False
    try:
        import pyautogui
        HAS_GUI = True
        pyautogui.FAILSAFE = False
    except Exception:
        HAS_GUI = False
else:
    HAS_UIA = HAS_GW = HAS_GUI = False

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("vyro-mcp")

# ── Global state ───────────────────────────────────────────────────────────────
_camera_running = False
_camera_frame: bytes | None = None
_tunnel_proc: subprocess.Popen | None = None
_tunnel_url: str = ""
_api_port: int = 7832
_tts_engine = None
_app_log: list[str] = []
_connected_clients: int = 0
_request_count: int = 0

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _app_log.append(line)
    if len(_app_log) > 500:
        _app_log.pop(0)
    print(line)

# ════════════════════════════════════════════════════════════════════════════
#  CORE CAPABILITIES
# ════════════════════════════════════════════════════════════════════════════

# ── Screen ────────────────────────────────────────────────────────────────────
def capture_screen(monitor: int = 0, region: dict | None = None) -> bytes:
    if region:
        box = (region["x"], region["y"],
               region["x"] + region["w"], region["y"] + region["h"])
        img = ImageGrab.grab(bbox=box, all_screens=True)
    else:
        img = ImageGrab.grab(all_screens=True)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()

def capture_screen_b64(**kw) -> str:
    return base64.b64encode(capture_screen(**kw)).decode()

# ── Camera ────────────────────────────────────────────────────────────────────
def _camera_loop(device: int, fps: int):
    global _camera_running, _camera_frame
    if not HAS_CV2:
        return
    cap = cv2.VideoCapture(device, cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    delay = 1.0 / fps
    while _camera_running:
        ok, frame = cap.read()
        if ok:
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            _camera_frame = buf.tobytes()
        time.sleep(delay)
    cap.release()

def camera_snap(device: int = 0) -> bytes:
    if not HAS_CV2:
        raise RuntimeError("opencv not available")
    cap = cv2.VideoCapture(device, cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("Camera read failed")
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()

# ── TTS ───────────────────────────────────────────────────────────────────────
def speak(text: str, rate: int = 175, voice: str = ""):
    global _tts_engine
    def _run():
        global _tts_engine
        try:
            if _tts_engine is None:
                _tts_engine = pyttsx3.init()
            _tts_engine.setProperty("rate", rate)
            if voice:
                for v in _tts_engine.getProperty("voices"):
                    if voice.lower() in v.name.lower():
                        _tts_engine.setProperty("voice", v.id)
                        break
            _tts_engine.say(text)
            _tts_engine.runAndWait()
        except Exception as e:
            log.error(f"TTS error: {e}")
            if IS_WINDOWS:
                subprocess.run(["powershell", "-Command",
                    f'Add-Type -AssemblyName System.Speech; '
                    f'$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                    f'$s.Speak("{text}")'], capture_output=True)
    threading.Thread(target=_run, daemon=True).start()

# ── Audio record ──────────────────────────────────────────────────────────────
def record_audio(seconds: float, rate: int = 44100, channels: int = 1) -> bytes:
    if not HAS_AUDIO:
        raise RuntimeError("pyaudio not available")
    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=channels,
                     rate=rate, input=True, frames_per_buffer=1024)
    frames = []
    for _ in range(int(rate / 1024 * seconds)):
        frames.append(stream.read(1024, exception_on_overflow=False))
    stream.stop_stream(); stream.close(); pa.terminate()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()

# ── Shell ─────────────────────────────────────────────────────────────────────
def shell_exec(cmd: str, cwd: str = None, timeout: int = 30) -> dict:
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=cwd, timeout=timeout,
        env={**os.environ}
    )
    return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}

# ── Windows UI Automation ─────────────────────────────────────────────────────
def get_ui_tree(window_title: str = None, depth: int = 3) -> dict:
    """Walk the Windows UI Automation tree — gives AI access to every button,
    text field, menu item, and control in any app."""
    if not IS_WINDOWS:
        return {"error": "Windows only"}
    try:
        import comtypes
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen.UIAutomationClient import (
            CUIAutomation, IUIAutomation, IUIAutomationElement,
            TreeScope_Children, TreeScope_Subtree,
            UIA_NamePropertyId, UIA_AutomationIdPropertyId,
            UIA_ClassNamePropertyId, UIA_ControlTypePropertyId,
            UIA_IsEnabledPropertyId, UIA_BoundingRectanglePropertyId,
        )
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=IUIAutomation
        )

        def _elem_to_dict(elem, cur_depth):
            if elem is None or cur_depth < 0:
                return None
            try:
                name = elem.GetCurrentPropertyValue(UIA_NamePropertyId) or ""
                aid  = elem.GetCurrentPropertyValue(UIA_AutomationIdPropertyId) or ""
                cls  = elem.GetCurrentPropertyValue(UIA_ClassNamePropertyId) or ""
                rect = elem.GetCurrentPropertyValue(UIA_BoundingRectanglePropertyId)
                enabled = elem.GetCurrentPropertyValue(UIA_IsEnabledPropertyId)
                node = {
                    "name": str(name),
                    "automation_id": str(aid),
                    "class": str(cls),
                    "enabled": bool(enabled),
                    "rect": {"l": int(rect.left), "t": int(rect.top),
                             "r": int(rect.right), "b": int(rect.bottom)}
                    if rect else {},
                }
                if cur_depth > 0:
                    children = []
                    walker = uia.ControlViewWalker
                    child = walker.GetFirstChildElement(elem)
                    while child:
                        d = _elem_to_dict(child, cur_depth - 1)
                        if d:
                            children.append(d)
                        child = walker.GetNextSiblingElement(child)
                    if children:
                        node["children"] = children
                return node
            except Exception:
                return None

        if window_title:
            cond = uia.CreatePropertyCondition(UIA_NamePropertyId, window_title)
            elem = uia.RootElement.FindFirst(TreeScope_Children, cond)
            if elem is None:
                # fuzzy search
                walker = uia.ControlViewWalker
                child = walker.GetFirstChildElement(uia.RootElement)
                while child:
                    n = child.GetCurrentPropertyValue(UIA_NamePropertyId) or ""
                    if window_title.lower() in str(n).lower():
                        elem = child
                        break
                    child = walker.GetNextSiblingElement(child)
        else:
            elem = uia.RootElement

        return _elem_to_dict(elem, depth) or {"error": "Element not found"}
    except Exception as e:
        # Fallback: use powershell to dump window info
        try:
            ps = (
                'Add-Type -AssemblyName UIAutomationClient; '
                'Add-Type -AssemblyName UIAutomationTypes; '
                '$ae=[System.Windows.Automation.AutomationElement]; '
                '$root=$ae::RootElement; '
                '$cond=$ae::ContentViewCondition; '
                '$all=$root.FindAll([System.Windows.Automation.TreeScope]::Children,$cond); '
                '$all | ForEach-Object { $_.Current.Name } | ConvertTo-Json'
            )
            r = subprocess.run(["powershell", "-Command", ps],
                               capture_output=True, text=True, timeout=10)
            return {"windows": json.loads(r.stdout or "[]")}
        except Exception as e2:
            return {"error": str(e2)}

def list_windows() -> list:
    if not IS_WINDOWS:
        return []
    try:
        ps = (
            'Get-Process | Where-Object {$_.MainWindowTitle} | '
            'Select-Object Name,Id,MainWindowTitle | ConvertTo-Json'
        )
        r = subprocess.run(["powershell", "-Command", ps],
                           capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception as e:
        return [{"error": str(e)}]

def click_ui_element(automation_id: str = None, name: str = None,
                     x: int = None, y: int = None) -> dict:
    if not IS_WINDOWS:
        return {"error": "Windows only"}
    if x is not None and y is not None:
        if HAS_GUI:
            import pyautogui
            pyautogui.click(x, y)
            return {"clicked": f"coordinates ({x},{y})"}
        else:
            # Win32 SendInput
            ctypes.windll.user32.SetCursorPos(x, y)
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # LBUTTONDOWN
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # LBUTTONUP
            return {"clicked": f"coordinates ({x},{y})"}
    return {"error": "Provide x,y coordinates or use UI tree to find element"}

def type_text(text: str) -> dict:
    if not IS_WINDOWS:
        return {"error": "Windows only"}
    if HAS_GUI:
        import pyautogui
        pyautogui.typewrite(text, interval=0.02)
    else:
        # PowerShell SendKeys
        ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{text}")'
        subprocess.run(["powershell", "-Command", ps], capture_output=True)
    return {"typed": text}

def send_key(key: str) -> dict:
    if HAS_GUI:
        import pyautogui
        pyautogui.press(key)
        return {"key": key}
    if IS_WINDOWS:
        ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{key}")'
        subprocess.run(["powershell", "-Command", ps], capture_output=True)
        return {"key": key}
    return {"error": "Not supported"}

def focus_window(title: str) -> dict:
    if not IS_WINDOWS:
        return {"error": "Windows only"}
    ps = (
        f'Add-Type -TypeDefinition \'using System; using System.Runtime.InteropServices; '
        f'public class Win32 {{ [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd); }}\'; '
        f'$p = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{title}*"}} | Select-Object -First 1; '
        f'if ($p) {{ [Win32]::SetForegroundWindow($p.MainWindowHandle) }}'
    )
    subprocess.run(["powershell", "-Command", ps], capture_output=True)
    return {"focused": title}

# ── Tunnel ────────────────────────────────────────────────────────────────────
def _ensure_cloudflared():
    if shutil.which("cloudflared"):
        return True
    if IS_WINDOWS:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        dst = Path(os.environ.get("TEMP", "C:\\Temp")) / "cloudflared.exe"
        import urllib.request
        try:
            urllib.request.urlretrieve(url, dst)
            os.environ["PATH"] = str(dst.parent) + os.pathsep + os.environ["PATH"]
            # copy to PATH
            shutil.copy(dst, Path(sys.executable).parent / "cloudflared.exe")
            return True
        except Exception:
            return False
    return False

def start_tunnel(port: int, provider: str = "cloudflare",
                 cf_token: str = "", ngrok_token: str = "",
                 ngrok_domain: str = "") -> str:
    global _tunnel_proc, _tunnel_url
    if provider == "cloudflare":
        _ensure_cloudflared()
        cf_bin = shutil.which("cloudflared") or str(Path(sys.executable).parent / "cloudflared.exe")
        if cf_token:
            cmd = [cf_bin, "tunnel", "--no-autoupdate", "run", "--token", cf_token]
        else:
            cmd = [cf_bin, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]
        _tunnel_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True)
        for line in _tunnel_proc.stdout:
            _log(f"CF: {line.strip()}")
            m = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
            if m:
                _tunnel_url = m.group(0)
                break
            m2 = re.search(r"https://\S+\.cfargotunnel\.com", line)
            if m2:
                _tunnel_url = m2.group(0)
                break
    elif provider == "ngrok":
        ngrok_bin = shutil.which("ngrok")
        if not ngrok_bin:
            return "ngrok not found"
        if ngrok_token:
            subprocess.run([ngrok_bin, "config", "add-authtoken", ngrok_token],
                           capture_output=True)
        cmd = [ngrok_bin, "http", str(port), "--log", "stdout"]
        if ngrok_domain:
            cmd += ["--domain", ngrok_domain]
        _tunnel_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True)
        for _ in range(30):
            time.sleep(1)
            try:
                import urllib.request
                with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels") as r:
                    data = json.loads(r.read())
                    tunnels = data.get("tunnels", [])
                    if tunnels:
                        _tunnel_url = tunnels[0]["public_url"]
                        return _tunnel_url
            except Exception:
                pass
    return _tunnel_url

# ════════════════════════════════════════════════════════════════════════════
#  HTTP API  (Flask)
# ════════════════════════════════════════════════════════════════════════════
api = Flask("vyro-mcp-api")
CORS(api)

def _ok(data: Any) -> Response:
    global _request_count
    _request_count += 1
    return jsonify({"ok": True, "data": data})

def _err(msg: str, code: int = 400) -> Response:
    return jsonify({"ok": False, "error": msg}), code

@api.before_request
def _log_request():
    _log(f"API {request.method} {request.path} from {request.remote_addr}")

# ── Screen ────────────────────────────────────────────────────────────────────
@api.route("/screen", methods=["GET"])
def api_screen():
    region = None
    if request.args.get("x"):
        region = {k: int(request.args[k]) for k in ("x","y","w","h")}
    return _ok({"jpeg_b64": capture_screen_b64(region=region)})

@api.route("/screen/stream")
def api_screen_stream():
    """MJPEG stream of screen — open in browser or img tag."""
    def gen():
        while True:
            frame = capture_screen()
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            time.sleep(0.1)  # 10fps
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

# ── Camera ────────────────────────────────────────────────────────────────────
@api.route("/camera/snap", methods=["GET"])
def api_camera_snap():
    device = int(request.args.get("device", 0))
    try:
        frame = camera_snap(device)
        return _ok({"jpeg_b64": base64.b64encode(frame).decode()})
    except Exception as e:
        return _err(str(e))

@api.route("/camera/stream")
def api_camera_stream():
    global _camera_running
    if not _camera_running:
        _camera_running = True
        device = int(request.args.get("device", 0))
        fps = int(request.args.get("fps", 15))
        threading.Thread(target=_camera_loop, args=(device, fps), daemon=True).start()
    def gen():
        while True:
            if _camera_frame:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + _camera_frame + b"\r\n")
            time.sleep(0.067)  # ~15fps
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

@api.route("/camera/stop", methods=["POST"])
def api_camera_stop():
    global _camera_running
    _camera_running = False
    return _ok("stopped")

# ── TTS / Audio ───────────────────────────────────────────────────────────────
@api.route("/speak", methods=["POST"])
def api_speak():
    d = request.json or {}
    speak(d.get("text",""), d.get("rate", 175), d.get("voice",""))
    return _ok("speaking")

@api.route("/audio/record", methods=["POST"])
def api_audio_record():
    d = request.json or {}
    try:
        wav = record_audio(float(d.get("seconds", 3)),
                           int(d.get("rate", 44100)),
                           int(d.get("channels", 1)))
        return _ok({"wav_b64": base64.b64encode(wav).decode()})
    except Exception as e:
        return _err(str(e))

# ── Shell ─────────────────────────────────────────────────────────────────────
@api.route("/shell", methods=["POST"])
def api_shell():
    d = request.json or {}
    cmd = d.get("cmd", "")
    if not cmd:
        return _err("cmd required")
    try:
        result = shell_exec(cmd, d.get("cwd"), int(d.get("timeout", 30)))
        return _ok(result)
    except subprocess.TimeoutExpired:
        return _err("timeout")
    except Exception as e:
        return _err(str(e))

# ── Filesystem ────────────────────────────────────────────────────────────────
@api.route("/fs/read", methods=["POST"])
def api_fs_read():
    d = request.json or {}
    path = d.get("path","")
    try:
        p = Path(path)
        if d.get("binary"):
            data = p.read_bytes()
            return _ok({"b64": base64.b64encode(data).decode()})
        return _ok({"text": p.read_text(errors="replace")})
    except Exception as e:
        return _err(str(e))

@api.route("/fs/write", methods=["POST"])
def api_fs_write():
    d = request.json or {}
    try:
        p = Path(d["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        content = d["content"]
        if d.get("binary"):
            p.write_bytes(base64.b64decode(content))
        else:
            mode = "a" if d.get("append") else "w"
            with open(p, mode) as f:
                f.write(content)
        return _ok(str(p))
    except Exception as e:
        return _err(str(e))

@api.route("/fs/list", methods=["POST"])
def api_fs_list():
    d = request.json or {}
    try:
        p = Path(d["path"])
        entries = []
        for item in p.iterdir():
            entries.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0,
                "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat()
            })
        return _ok(entries)
    except Exception as e:
        return _err(str(e))

@api.route("/fs/delete", methods=["POST"])
def api_fs_delete():
    d = request.json or {}
    try:
        p = Path(d["path"])
        if p.is_dir() and d.get("recursive"):
            shutil.rmtree(p)
        elif p.is_dir():
            p.rmdir()
        else:
            p.unlink()
        return _ok(f"deleted {p}")
    except Exception as e:
        return _err(str(e))

# ── Windows UI Automation ─────────────────────────────────────────────────────
@api.route("/ui/tree", methods=["POST"])
def api_ui_tree():
    d = request.json or {}
    return _ok(get_ui_tree(d.get("window"), int(d.get("depth", 3))))

@api.route("/ui/windows", methods=["GET"])
def api_ui_windows():
    return _ok(list_windows())

@api.route("/ui/click", methods=["POST"])
def api_ui_click():
    d = request.json or {}
    return _ok(click_ui_element(
        d.get("automation_id"), d.get("name"),
        d.get("x"), d.get("y")
    ))

@api.route("/ui/type", methods=["POST"])
def api_ui_type():
    d = request.json or {}
    return _ok(type_text(d.get("text", "")))

@api.route("/ui/key", methods=["POST"])
def api_ui_key():
    d = request.json or {}
    return _ok(send_key(d.get("key", "")))

@api.route("/ui/focus", methods=["POST"])
def api_ui_focus():
    d = request.json or {}
    return _ok(focus_window(d.get("title", "")))

# ── System info ───────────────────────────────────────────────────────────────
@api.route("/sysinfo", methods=["GET"])
def api_sysinfo():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return _ok({
        "os": platform.platform(),
        "hostname": socket.gethostname(),
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_cores": psutil.cpu_count(),
        "memory": {"total_gb": round(mem.total/1e9,1),
                   "used_gb":  round(mem.used/1e9,1),
                   "percent":  mem.percent},
        "disk":   {"total_gb": round(disk.total/1e9,1),
                   "used_gb":  round(disk.used/1e9,1),
                   "percent":  disk.percent},
        "uptime": str(datetime.now() - datetime.fromtimestamp(psutil.boot_time())),
    })

@api.route("/processes", methods=["GET"])
def api_processes():
    flt = request.args.get("filter","").lower()
    procs = []
    for p in psutil.process_iter(["pid","name","status","cpu_percent","memory_percent"]):
        if not flt or flt in p.info["name"].lower():
            procs.append(p.info)
    return _ok(procs)

@api.route("/processes/kill", methods=["POST"])
def api_kill():
    d = request.json or {}
    try:
        pid = d.get("pid")
        if pid:
            os.kill(int(pid), 9)
            return _ok(f"killed {pid}")
        name = d.get("name","")
        killed = []
        for p in psutil.process_iter(["pid","name"]):
            if name.lower() in p.info["name"].lower():
                os.kill(p.pid, 9)
                killed.append(p.pid)
        return _ok(killed)
    except Exception as e:
        return _err(str(e))

# ── Tunnel control ────────────────────────────────────────────────────────────
@api.route("/tunnel/start", methods=["POST"])
def api_tunnel_start():
    d = request.json or {}
    def _run():
        url = start_tunnel(
            d.get("port", _api_port),
            d.get("provider", "cloudflare"),
            d.get("cf_token",""),
            d.get("ngrok_token",""),
            d.get("ngrok_domain",""),
        )
        _log(f"Tunnel URL: {url}")
    threading.Thread(target=_run, daemon=True).start()
    return _ok("tunnel starting...")

@api.route("/tunnel/status", methods=["GET"])
def api_tunnel_status():
    return _ok({"running": _tunnel_proc is not None and _tunnel_proc.poll() is None,
                "url": _tunnel_url})

@api.route("/tunnel/stop", methods=["POST"])
def api_tunnel_stop():
    global _tunnel_proc, _tunnel_url
    if _tunnel_proc:
        _tunnel_proc.terminate()
        _tunnel_proc = None
        _tunnel_url = ""
    return _ok("stopped")

# ── Clipboard ─────────────────────────────────────────────────────────────────
@api.route("/clipboard", methods=["GET","POST"])
def api_clipboard():
    if request.method == "GET":
        if IS_WINDOWS:
            r = subprocess.run(["powershell", "-Command", "Get-Clipboard"],
                               capture_output=True, text=True)
            return _ok(r.stdout.strip())
        return _err("Windows only")
    d = request.json or {}
    text = d.get("text","")
    if IS_WINDOWS:
        subprocess.run(["powershell", "-Command", f"Set-Clipboard '{text}'"],
                       capture_output=True)
    return _ok("set")

# ── Status / health ───────────────────────────────────────────────────────────
@api.route("/", methods=["GET"])
def api_root():
    return _ok({
        "name": "vyro-mcp",
        "version": "2.0.0",
        "platform": platform.platform(),
        "tunnel_url": _tunnel_url,
        "requests_served": _request_count,
        "endpoints": [
            "GET  /screen", "GET  /screen/stream (MJPEG)",
            "GET  /camera/snap", "GET  /camera/stream (MJPEG)",
            "POST /speak", "POST /audio/record",
            "POST /shell", "POST /fs/read", "POST /fs/write",
            "POST /fs/list", "POST /fs/delete",
            "POST /ui/tree", "GET  /ui/windows",
            "POST /ui/click", "POST /ui/type", "POST /ui/key",
            "POST /ui/focus",
            "GET  /sysinfo", "GET  /processes",
            "POST /processes/kill",
            "POST /tunnel/start", "GET  /tunnel/status",
            "GET/POST /clipboard",
        ]
    })

# ════════════════════════════════════════════════════════════════════════════
#  GUI DASHBOARD  (Tkinter)
# ════════════════════════════════════════════════════════════════════════════
class Dashboard:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("vyro-mcp  —  AI Control Server")
        self.root.geometry("900x650")
        self.root.configure(bg="#0d0d0d")
        self.root.resizable(True, True)

        # Fonts
        f_title = tkfont.Font(family="Segoe UI", size=16, weight="bold")
        f_mono  = tkfont.Font(family="Consolas", size=9)
        f_label = tkfont.Font(family="Segoe UI", size=10)
        f_big   = tkfont.Font(family="Segoe UI", size=22, weight="bold")

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg="#111", height=56)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚡ vyro-mcp", font=f_title,
                 fg="#00e5ff", bg="#111").pack(side="left", padx=16, pady=12)
        self.lbl_status = tk.Label(hdr, text="● STARTING",
                                   font=f_label, fg="#ff9800", bg="#111")
        self.lbl_status.pack(side="right", padx=16)

        # ── Main area ─────────────────────────────────────────────────────────
        main = tk.Frame(self.root, bg="#0d0d0d")
        main.pack(fill="both", expand=True, padx=12, pady=8)

        # Left column — controls
        left = tk.Frame(main, bg="#0d0d0d", width=300)
        left.pack(side="left", fill="y", padx=(0,8))
        left.pack_propagate(False)

        # API URL card
        card1 = tk.LabelFrame(left, text=" Local API ", font=f_label,
                               fg="#00e5ff", bg="#161616",
                               relief="flat", bd=1,
                               highlightbackground="#333", highlightthickness=1)
        card1.pack(fill="x", pady=(0,8))
        self.lbl_local = tk.Label(card1, text=f"http://localhost:{_api_port}",
                                  font=f_mono, fg="#a0ffa0", bg="#161616",
                                  cursor="hand2")
        self.lbl_local.pack(padx=10, pady=8)
        self.lbl_local.bind("<Button-1>", lambda e: self._copy(self.lbl_local.cget("text")))

        # Tunnel card
        card2 = tk.LabelFrame(left, text=" Tunnel URL ", font=f_label,
                               fg="#00e5ff", bg="#161616",
                               relief="flat", bd=1,
                               highlightbackground="#333", highlightthickness=1)
        card2.pack(fill="x", pady=(0,8))
        self.lbl_tunnel = tk.Label(card2, text="Not started",
                                   font=f_mono, fg="#888", bg="#161616",
                                   wraplength=270, cursor="hand2")
        self.lbl_tunnel.pack(padx=10, pady=6)
        self.lbl_tunnel.bind("<Button-1>", lambda e: self._copy(self.lbl_tunnel.cget("text")))

        # Tunnel controls
        tframe = tk.Frame(left, bg="#0d0d0d")
        tframe.pack(fill="x", pady=(0,8))
        tk.Label(tframe, text="Provider:", font=f_label,
                 fg="#aaa", bg="#0d0d0d").pack(side="left")
        self.var_provider = tk.StringVar(value="cloudflare")
        tk.OptionMenu(tframe, self.var_provider, "cloudflare", "ngrok").pack(side="left", padx=4)

        tk.Label(left, text="Token (optional):", font=f_label,
                 fg="#aaa", bg="#0d0d0d").pack(anchor="w")
        self.ent_token = tk.Entry(left, font=f_mono, bg="#1a1a1a",
                                  fg="#fff", insertbackground="#fff", show="•")
        self.ent_token.pack(fill="x", pady=(0,8))

        btn_frame = tk.Frame(left, bg="#0d0d0d")
        btn_frame.pack(fill="x", pady=(0,12))
        tk.Button(btn_frame, text="▶ Start Tunnel",
                  font=f_label, bg="#005f73", fg="white",
                  relief="flat", padx=8, pady=4,
                  command=self._start_tunnel).pack(side="left", expand=True, fill="x", padx=(0,4))
        tk.Button(btn_frame, text="■ Stop",
                  font=f_label, bg="#400", fg="white",
                  relief="flat", padx=8, pady=4,
                  command=self._stop_tunnel).pack(side="left", expand=True, fill="x")

        # Stats
        stats = tk.Frame(left, bg="#161616")
        stats.pack(fill="x", pady=(0,8))
        self.lbl_reqs = tk.Label(stats, text="Requests: 0",
                                 font=f_label, fg="#aaa", bg="#161616")
        self.lbl_reqs.pack(anchor="w", padx=8, pady=4)
        self.lbl_cpu = tk.Label(stats, text="CPU: —  RAM: —",
                                font=f_label, fg="#aaa", bg="#161616")
        self.lbl_cpu.pack(anchor="w", padx=8, pady=2)

        # Screen preview toggle
        tk.Button(left, text="📷 Preview Screen",
                  font=f_label, bg="#1a1a2e", fg="#00e5ff",
                  relief="flat", pady=4,
                  command=self._toggle_preview).pack(fill="x", pady=(0,4))

        tk.Button(left, text="🌐 Open API Docs",
                  font=f_label, bg="#1a1a2e", fg="#00e5ff",
                  relief="flat", pady=4,
                  command=lambda: self._open_browser(f"http://localhost:{_api_port}/")).pack(fill="x")

        # Right column — log + preview
        right = tk.Frame(main, bg="#0d0d0d")
        right.pack(side="left", fill="both", expand=True)

        # Notebook tabs
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)

        # Log tab
        log_frame = tk.Frame(nb, bg="#0d0d0d")
        nb.add(log_frame, text=" Activity Log ")
        self.log_text = scrolledtext.ScrolledText(
            log_frame, font=f_mono, bg="#0a0a0a", fg="#b0ffb0",
            insertbackground="#fff", relief="flat", state="disabled"
        )
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        # Screen preview tab
        preview_frame = tk.Frame(nb, bg="#000")
        nb.add(preview_frame, text=" Screen Preview ")
        self.canvas = tk.Canvas(preview_frame, bg="#000",
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._preview_active = False
        self._preview_img = None

        # Endpoints tab
        ep_frame = tk.Frame(nb, bg="#0a0a0a")
        nb.add(ep_frame, text=" Endpoints ")
        ep_text = scrolledtext.ScrolledText(
            ep_frame, font=f_mono, bg="#0a0a0a", fg="#88aaff",
            relief="flat", state="normal"
        )
        ep_text.pack(fill="both", expand=True, padx=4, pady=4)
        endpoints = """
 SCREEN
   GET  /screen              → JPEG screenshot (base64)
   GET  /screen/stream       → MJPEG live stream (open in browser)
   GET  /screen?x=0&y=0&w=800&h=600  → region capture

 CAMERA
   GET  /camera/snap         → webcam snapshot
   GET  /camera/stream       → MJPEG webcam stream

 VOICE
   POST /speak               → {"text":"hello","rate":175}
   POST /audio/record        → {"seconds":3}  → WAV base64

 SHELL
   POST /shell               → {"cmd":"dir","cwd":"C:\\\\","timeout":30}

 FILESYSTEM
   POST /fs/read             → {"path":"C:\\\\file.txt"}
   POST /fs/write            → {"path":"...","content":"...","append":false}
   POST /fs/list             → {"path":"C:\\\\Users"}
   POST /fs/delete           → {"path":"...","recursive":false}

 WINDOWS UI TREE
   POST /ui/tree             → {"window":"Notepad","depth":3}
   GET  /ui/windows          → list all open windows
   POST /ui/click            → {"x":100,"y":200}
   POST /ui/type             → {"text":"hello world"}
   POST /ui/key              → {"key":"enter"}
   POST /ui/focus            → {"title":"Chrome"}

 SYSTEM
   GET  /sysinfo             → CPU, RAM, disk, uptime
   GET  /processes           → running processes
   POST /processes/kill      → {"pid":1234} or {"name":"notepad"}

 CLIPBOARD
   GET  /clipboard           → read clipboard
   POST /clipboard           → {"text":"copy this"}

 TUNNEL
   POST /tunnel/start        → {"provider":"cloudflare","cf_token":"..."}
   GET  /tunnel/status       → current URL
   POST /tunnel/stop
"""
        ep_text.insert("1.0", endpoints)
        ep_text.configure(state="disabled")

        # Style the notebook
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook", background="#0d0d0d", borderwidth=0)
        style.configure("TNotebook.Tab", background="#1a1a1a",
                        foreground="#aaa", padding=[10,4])
        style.map("TNotebook.Tab",
                  background=[("selected","#161616")],
                  foreground=[("selected","#00e5ff")])

        # Start update loop
        self.root.after(1000, self._tick)

    def _copy(self, text: str):
        if text and text != "Not started":
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            _log(f"Copied: {text}")

    def _start_tunnel(self):
        provider = self.var_provider.get()
        token = self.ent_token.get().strip()
        port = _api_port
        self.lbl_tunnel.config(text="Starting...", fg="#ff9800")
        def _run():
            url = start_tunnel(port, provider,
                               token if provider == "cloudflare" else "",
                               token if provider == "ngrok" else "")
            self.lbl_tunnel.config(text=url or "Failed", fg="#a0ffa0" if url else "#f44")
        threading.Thread(target=_run, daemon=True).start()

    def _stop_tunnel(self):
        global _tunnel_proc, _tunnel_url
        if _tunnel_proc:
            _tunnel_proc.terminate()
            _tunnel_proc = None
            _tunnel_url = ""
        self.lbl_tunnel.config(text="Stopped", fg="#888")

    def _toggle_preview(self):
        self._preview_active = not self._preview_active
        if self._preview_active:
            self._update_preview()

    def _update_preview(self):
        if not self._preview_active:
            return
        try:
            img = ImageGrab.grab(all_screens=True)
            w = self.canvas.winfo_width() or 400
            h = self.canvas.winfo_height() or 300
            img.thumbnail((w, h), Image.LANCZOS)
            self._preview_img = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self._preview_img)
        except Exception:
            pass
        self.root.after(200, self._update_preview)  # 5fps preview

    def _open_browser(self, url: str):
        import webbrowser
        webbrowser.open(url)

    def _tick(self):
        # Update log
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(_app_log[-100:]))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        # Update stats
        self.lbl_reqs.config(text=f"Requests: {_request_count}")
        try:
            cpu = psutil.cpu_percent()
            mem = psutil.virtual_memory().percent
            self.lbl_cpu.config(text=f"CPU: {cpu:.0f}%  RAM: {mem:.0f}%")
        except Exception:
            pass

        # Update tunnel URL
        if _tunnel_url and self.lbl_tunnel.cget("text") != _tunnel_url:
            self.lbl_tunnel.config(text=_tunnel_url, fg="#a0ffa0")

        # Update status
        self.lbl_status.config(text="● RUNNING", fg="#00e5ff")

        self.root.after(1000, self._tick)

    def run(self):
        self.root.mainloop()


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════
def _run_api():
    _log(f"API server starting on port {_api_port}")
    api.run(host="0.0.0.0", port=_api_port, debug=False,
            use_reloader=False, threaded=True)

if __name__ == "__main__":
    # Auto-install Windows extras
    if IS_WINDOWS:
        for pkg in ["pygetwindow", "pyautogui"]:
            try:
                __import__(pkg.replace("-","_"))
            except ImportError:
                _pip(pkg)

    _log("vyro-mcp starting...")
    _log(f"Platform: {platform.platform()}")
    _log(f"Python: {sys.version.split()[0]}")

    # Start Flask in background thread
    api_thread = threading.Thread(target=_run_api, daemon=True)
    api_thread.start()
    time.sleep(0.5)
    _log(f"API ready → http://localhost:{_api_port}")

    # Start GUI (main thread)
    dash = Dashboard()
    dash.run()
