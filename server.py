"""
vyro-mcp v3.0 — Advanced Windows AI Control Server
Full GUI dashboard + HTTP API + MCP-over-SSE

New in v3:
  - Mouse drag, scroll, right-click, double-click
  - Keyboard combos (Ctrl+C, Alt+Tab, Win+D, etc.)
  - Working UI tree via pywinauto (name-based click, not pixel guess)
  - Change-detection screen loop (only alerts on meaningful changes)
  - Batch action executor (sequence of actions in one call)
  - Self-verification after every action (before/after diff)
  - PC memory/notes file (AI writes discoveries, reads them back)
  - Window management (move, resize, minimize, maximize, close)
  - Clipboard image support
  - Chrome DevTools Protocol integration
  - Event hooks (download complete, popup, process crash notifications)
  - CREATE_NO_WINDOW on all subprocesses — nothing ever pops up
  - MCP-over-SSE endpoint at /mcp for Claude Desktop
"""

import asyncio, base64, ctypes, hashlib, io, json, logging
import os, platform, queue, re, shutil, signal, socket
import struct, subprocess, sys, tempfile, threading, time, uuid, wave
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Auto-install ──────────────────────────────────────────────────────────────
_NO_WIN = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

def _pip(*pkgs):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *pkgs],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **_NO_WIN
    )

def _ensure_deps():
    checks = {
        "flask": "flask", "flask_cors": "flask-cors",
        "PIL": "pillow", "cv2": "opencv-python",
        "psutil": "psutil", "pyttsx3": "pyttsx3",
    }
    missing = [pkg for mod, pkg in checks.items()
               if not __import__("importlib").util.find_spec(mod)]
    if missing:
        print(f"Installing: {', '.join(missing)}")
        _pip(*missing)

_ensure_deps()

# ── Core imports ──────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, scrolledtext, font as tkfont
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from PIL import Image, ImageGrab, ImageTk
import psutil, pyttsx3

try:
    import cv2; HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import pyaudio; HAS_AUDIO = True
except Exception:
    HAS_AUDIO = False; pyaudio = None

IS_WINDOWS = sys.platform == "win32"

# Windows-specific imports
HAS_UIA = HAS_GW = HAS_GUI = HAS_WINAUTO = False
if IS_WINDOWS:
    try:
        import comtypes.client
        HAS_UIA = True
    except Exception:
        pass
    try:
        import pygetwindow as gw; HAS_GW = True
    except Exception:
        pass
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        HAS_GUI = True
    except Exception:
        pass
    try:
        import pywinauto
        from pywinauto import Application, Desktop
        from pywinauto.findwindows import ElementNotFoundError
        HAS_WINAUTO = True
    except Exception:
        pass

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("vyro-mcp")

# ── Global state ──────────────────────────────────────────────────────────────
_camera_running = False
_camera_frame: bytes | None = None
_tunnel_proc: subprocess.Popen | None = None
_tunnel_url: str = ""
_api_port: int = 7832
_tts_engine = None
_app_log: list[str] = []
_request_count: int = 0

# Screen state
_screen_frame: bytes | None = None
_screen_running = False
_screen_hash: str = ""
_screen_change_callbacks: list = []

# Event hook state
_event_queue: list[dict] = []
_watched_processes: dict[str, int] = {}  # name -> pid

# Memory/notes file — AI writes discoveries here
_NOTES_PATH = Path(os.path.expanduser("~")) / "vyro-mcp-notes.md"

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _app_log.append(line)
    if len(_app_log) > 500:
        _app_log.pop(0)
    print(line)

def _push_event(kind: str, data: dict):
    _event_queue.append({"time": datetime.now().isoformat(), "kind": kind, **data})
    if len(_event_queue) > 100:
        _event_queue.pop(0)

# ════════════════════════════════════════════════════════════════════════════
#  SCREEN
# ════════════════════════════════════════════════════════════════════════════
def capture_screen(region: dict | None = None, quality: int = 82) -> bytes:
    if region:
        box = (region["x"], region["y"],
               region["x"] + region["w"], region["y"] + region["h"])
        img = ImageGrab.grab(bbox=box, all_screens=True)
    else:
        img = ImageGrab.grab(all_screens=True)
    if img.width > 1920:
        ratio = 1920 / img.width
        img = img.resize((1920, int(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()

def capture_screen_b64(**kw) -> str:
    return base64.b64encode(capture_screen(**kw)).decode()

def _screen_loop():
    """Background: captures every 500ms, detects meaningful changes."""
    global _screen_frame, _screen_running, _screen_hash
    while _screen_running:
        try:
            frame = capture_screen(quality=75)
            # Hash a downsampled version for change detection
            thumb = Image.open(io.BytesIO(frame)).resize((160, 90))
            new_hash = hashlib.md5(thumb.tobytes()).hexdigest()
            _screen_frame = frame
            if new_hash != _screen_hash:
                _screen_hash = new_hash
                _push_event("screen_change", {"hash": new_hash[:8]})
        except Exception:
            pass
        time.sleep(0.5)

def _start_screen_loop():
    global _screen_running
    if not _screen_running:
        _screen_running = True
        threading.Thread(target=_screen_loop, daemon=True).start()

# ════════════════════════════════════════════════════════════════════════════
#  CAMERA
# ════════════════════════════════════════════════════════════════════════════
def _camera_loop(device: int, fps: int):
    global _camera_running, _camera_frame
    if not HAS_CV2:
        return
    cap = cv2.VideoCapture(device, cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    while _camera_running:
        ok, frame = cap.read()
        if ok:
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            _camera_frame = buf.tobytes()
        time.sleep(1.0 / fps)
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

# ════════════════════════════════════════════════════════════════════════════
#  TTS
# ════════════════════════════════════════════════════════════════════════════
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
            if IS_WINDOWS:
                subprocess.run(
                    ["powershell", "-Command",
                     f'Add-Type -AssemblyName System.Speech; '
                     f'$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                     f'$s.Speak("{text}")'],
                    capture_output=True, **_NO_WIN)
    threading.Thread(target=_run, daemon=True).start()

# ════════════════════════════════════════════════════════════════════════════
#  SHELL  — never opens a terminal window
# ════════════════════════════════════════════════════════════════════════════
def shell_exec(cmd: str, cwd: str = None, timeout: int = 30) -> dict:
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=cwd, timeout=timeout, env={**os.environ}, **_NO_WIN
    )
    return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}

# ════════════════════════════════════════════════════════════════════════════
#  MOUSE — drag, scroll, right-click, double-click
# ════════════════════════════════════════════════════════════════════════════
def mouse_click(x: int, y: int, button: str = "left", double: bool = False) -> dict:
    if HAS_GUI:
        import pyautogui
        if double:
            pyautogui.doubleClick(x, y, button=button)
        else:
            pyautogui.click(x, y, button=button)
        return {"clicked": f"{button} at ({x},{y})", "double": double}
    if IS_WINDOWS:
        # Win32 fallback
        ctypes.windll.user32.SetCursorPos(x, y)
        down_map = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}
        up_map   = {"left": 0x0004, "right": 0x0010, "middle": 0x0040}
        n = 2 if double else 1
        for _ in range(n):
            ctypes.windll.user32.mouse_event(down_map.get(button, 0x0002), 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(up_map.get(button, 0x0004), 0, 0, 0, 0)
        return {"clicked": f"{button} at ({x},{y})", "double": double}
    return {"error": "Not supported"}

def mouse_drag(x1: int, y1: int, x2: int, y2: int,
               button: str = "left", duration: float = 0.5) -> dict:
    if HAS_GUI:
        import pyautogui
        pyautogui.mouseDown(x1, y1, button=button)
        pyautogui.moveTo(x2, y2, duration=duration)
        pyautogui.mouseUp(x2, y2, button=button)
        return {"dragged": f"({x1},{y1}) → ({x2},{y2})"}
    if IS_WINDOWS:
        down_map = {"left": 0x0002, "right": 0x0008}
        up_map   = {"left": 0x0004, "right": 0x0010}
        ctypes.windll.user32.SetCursorPos(x1, y1)
        ctypes.windll.user32.mouse_event(down_map.get(button, 0x0002), 0, 0, 0, 0)
        steps = 20
        for i in range(steps + 1):
            nx = int(x1 + (x2 - x1) * i / steps)
            ny = int(y1 + (y2 - y1) * i / steps)
            ctypes.windll.user32.SetCursorPos(nx, ny)
            time.sleep(duration / steps)
        ctypes.windll.user32.mouse_event(up_map.get(button, 0x0004), 0, 0, 0, 0)
        return {"dragged": f"({x1},{y1}) → ({x2},{y2})"}
    return {"error": "Not supported"}

def mouse_scroll(x: int, y: int, dx: int = 0, dy: int = 3) -> dict:
    if HAS_GUI:
        import pyautogui
        pyautogui.moveTo(x, y)
        if dy:
            pyautogui.scroll(dy, x=x, y=y)
        return {"scrolled": f"dx={dx} dy={dy} at ({x},{y})"}
    if IS_WINDOWS:
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, dy * 120, 0)  # MOUSEEVENTF_WHEEL
        return {"scrolled": f"dy={dy} at ({x},{y})"}
    return {"error": "Not supported"}

def mouse_move(x: int, y: int) -> dict:
    if HAS_GUI:
        import pyautogui
        pyautogui.moveTo(x, y)
    elif IS_WINDOWS:
        ctypes.windll.user32.SetCursorPos(x, y)
    return {"moved": f"({x},{y})"}

# ════════════════════════════════════════════════════════════════════════════
#  KEYBOARD — combos, hotkeys, full text
# ════════════════════════════════════════════════════════════════════════════
def keyboard_combo(keys: str) -> dict:
    """
    Press a key combination. Format: 'ctrl+c', 'alt+tab', 'win+d',
    'ctrl+shift+esc', 'ctrl+alt+delete' etc.
    Keys joined by '+', case-insensitive.
    """
    parts = [k.strip().lower() for k in keys.split("+")]
    if HAS_GUI:
        import pyautogui
        pyautogui.hotkey(*parts)
        return {"combo": keys}
    if IS_WINDOWS:
        # Map to virtual key codes
        VK = {
            "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
            "win": 0x5B, "windows": 0x5B, "tab": 0x09, "enter": 0x0D,
            "esc": 0x1B, "escape": 0x1B, "space": 0x20, "back": 0x08,
            "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
            "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
            "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
            "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
            "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
            "printscreen": 0x2C, "insert": 0x2D,
        }
        KEYEVENTF_KEYDOWN = 0
        KEYEVENTF_KEYUP   = 0x0002
        codes = []
        for p in parts:
            if p in VK:
                codes.append(VK[p])
            elif len(p) == 1:
                codes.append(ctypes.windll.user32.VkKeyScanW(ord(p)) & 0xFF)
        for c in codes:
            ctypes.windll.user32.keybd_event(c, 0, KEYEVENTF_KEYDOWN, 0)
        for c in reversed(codes):
            ctypes.windll.user32.keybd_event(c, 0, KEYEVENTF_KEYUP, 0)
        return {"combo": keys}
    return {"error": "Not supported"}

def keyboard_type(text: str, interval: float = 0.02) -> dict:
    if HAS_GUI:
        import pyautogui
        pyautogui.typewrite(text, interval=interval)
        return {"typed": text}
    if IS_WINDOWS:
        ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{text}")'
        subprocess.run(["powershell", "-Command", ps], capture_output=True, **_NO_WIN)
        return {"typed": text}
    return {"error": "Not supported"}

def keyboard_key(key: str) -> dict:
    if HAS_GUI:
        import pyautogui
        pyautogui.press(key)
        return {"key": key}
    if IS_WINDOWS:
        ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{key}")'
        subprocess.run(["powershell", "-Command", ps], capture_output=True, **_NO_WIN)
        return {"key": key}
    return {"error": "Not supported"}

# ════════════════════════════════════════════════════════════════════════════
#  WINDOW MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════
def window_action(title: str, action: str,
                  x: int = None, y: int = None,
                  w: int = None, h: int = None) -> dict:
    """
    action: focus | minimize | maximize | restore | close | move | resize | info
    """
    if not IS_WINDOWS:
        return {"error": "Windows only"}

    if HAS_GW:
        import pygetwindow as gw
        wins = gw.getWindowsWithTitle(title)
        if not wins:
            return {"error": f"Window '{title}' not found"}
        win = wins[0]
        if action == "focus":    win.activate()
        elif action == "minimize":  win.minimize()
        elif action == "maximize":  win.maximize()
        elif action == "restore":   win.restore()
        elif action == "close":     win.close()
        elif action == "move" and x is not None:
            win.moveTo(x, y)
        elif action == "resize" and w is not None:
            win.resizeTo(w, h)
        elif action == "info":
            return {"title": win.title, "left": win.left, "top": win.top,
                    "width": win.width, "height": win.height,
                    "visible": win.visible, "isMinimized": win.isMinimized,
                    "isMaximized": win.isMaximized}
        return {"action": action, "window": win.title}

    # PowerShell fallback
    action_map = {
        "minimize": "ShowWindow($p.MainWindowHandle, 6)",
        "maximize": "ShowWindow($p.MainWindowHandle, 3)",
        "restore":  "ShowWindow($p.MainWindowHandle, 9)",
        "focus":    "SetForegroundWindow($p.MainWindowHandle)",
        "close":    "$p.CloseMainWindow()",
    }
    if action not in action_map:
        return {"error": f"Action '{action}' needs pygetwindow"}
    ps = (
        'Add-Type -TypeDefinition \'using System; using System.Runtime.InteropServices; '
        'public class W32 { '
        '[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); '
        '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n); }\'; '
        f'$p = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{title}*"}} | Select -First 1; '
        f'if ($p) {{ [W32]::{action_map[action]} }}'
    )
    subprocess.run(["powershell", "-Command", ps], capture_output=True, **_NO_WIN)
    return {"action": action, "window": title}

def list_windows() -> list:
    if not IS_WINDOWS:
        return []
    if HAS_GW:
        import pygetwindow as gw
        wins = []
        for w in gw.getAllWindows():
            if w.title.strip():
                wins.append({"title": w.title, "left": w.left, "top": w.top,
                             "width": w.width, "height": w.height,
                             "visible": w.visible})
        return wins
    try:
        ps = ('Get-Process | Where-Object {$_.MainWindowTitle} | '
              'Select-Object Name,Id,MainWindowTitle | ConvertTo-Json')
        r = subprocess.run(["powershell", "-Command", ps],
                           capture_output=True, text=True, timeout=10, **_NO_WIN)
        data = json.loads(r.stdout or "[]")
        return data if isinstance(data, list) else [data]
    except Exception as e:
        return [{"error": str(e)}]

# ════════════════════════════════════════════════════════════════════════════
#  UI AUTOMATION — name-based click via pywinauto + UIA fallback
# ════════════════════════════════════════════════════════════════════════════
def ui_find_and_click(window_title: str, element_name: str,
                      element_type: str = None) -> dict:
    """
    Click a UI element by name, not pixel coordinate.
    Uses pywinauto first (most reliable), falls back to UIA comtypes.
    """
    if not IS_WINDOWS:
        return {"error": "Windows only"}

    if HAS_WINAUTO:
        try:
            app = Application(backend="uia").connect(title_re=f".*{window_title}.*")
            win = app.top_window()
            if element_type:
                ctrl = win.child_window(title=element_name, control_type=element_type)
            else:
                ctrl = win.child_window(title=element_name)
            ctrl.click_input()
            rect = ctrl.rectangle()
            return {"clicked": element_name, "at": {"x": rect.mid_point().x,
                                                      "y": rect.mid_point().y}}
        except Exception as e:
            return {"error": f"pywinauto: {e}"}

    # UIA comtypes fallback
    if HAS_UIA:
        try:
            import comtypes.client
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen.UIAutomationClient import (
                CUIAutomation, IUIAutomation,
                TreeScope_Subtree, UIA_NamePropertyId,
            )
            uia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=IUIAutomation)
            cond = uia.CreatePropertyCondition(UIA_NamePropertyId, element_name)
            elem = uia.RootElement.FindFirst(TreeScope_Subtree, cond)
            if elem:
                rect = elem.CurrentBoundingRectangle
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                mouse_click(cx, cy)
                return {"clicked": element_name, "at": {"x": cx, "y": cy}}
            return {"error": f"Element '{element_name}' not found"}
        except Exception as e:
            return {"error": f"UIA: {e}"}

    return {"error": "No UI automation available — install pywinauto"}

def get_ui_tree(window_title: str = None, depth: int = 3) -> dict:
    """Get UI element tree. pywinauto is much more reliable than raw UIA."""
    if not IS_WINDOWS:
        return {"error": "Windows only"}

    if HAS_WINAUTO:
        try:
            if window_title:
                app = Application(backend="uia").connect(title_re=f".*{window_title}.*")
            else:
                desktop = Desktop(backend="uia")
                # Get top window
                wins = desktop.windows()
                if not wins:
                    return {"error": "No windows found"}
                app = Application(backend="uia").connect(handle=wins[0].handle)
            win = app.top_window()

            def _walk(ctrl, cur_depth):
                if cur_depth < 0:
                    return None
                try:
                    rect = ctrl.rectangle()
                    node = {
                        "name": ctrl.window_text(),
                        "type": ctrl.friendly_class_name(),
                        "enabled": ctrl.is_enabled(),
                        "visible": ctrl.is_visible(),
                        "rect": {"l": rect.left, "t": rect.top,
                                 "r": rect.right, "b": rect.bottom,
                                 "cx": rect.mid_point().x,
                                 "cy": rect.mid_point().y},
                    }
                    if cur_depth > 0:
                        children = []
                        for child in ctrl.children():
                            d = _walk(child, cur_depth - 1)
                            if d:
                                children.append(d)
                        if children:
                            node["children"] = children
                    return node
                except Exception:
                    return None

            return _walk(win, depth) or {"error": "Failed to walk tree"}
        except Exception as e:
            return {"error": f"pywinauto tree: {e}"}

    # UIA comtypes fallback
    if not HAS_UIA:
        return {"error": "No UI automation — install pywinauto"}
    try:
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen.UIAutomationClient import (
            CUIAutomation, IUIAutomation, IUIAutomationElement,
            TreeScope_Children, UIA_NamePropertyId, UIA_AutomationIdPropertyId,
            UIA_ClassNamePropertyId, UIA_IsEnabledPropertyId,
            UIA_BoundingRectanglePropertyId,
        )
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=IUIAutomation)

        def _elem(e, d):
            if e is None or d < 0:
                return None
            try:
                name = str(e.GetCurrentPropertyValue(UIA_NamePropertyId) or "")
                rect = e.GetCurrentPropertyValue(UIA_BoundingRectanglePropertyId)
                node = {
                    "name": name,
                    "automation_id": str(e.GetCurrentPropertyValue(UIA_AutomationIdPropertyId) or ""),
                    "class": str(e.GetCurrentPropertyValue(UIA_ClassNamePropertyId) or ""),
                    "enabled": bool(e.GetCurrentPropertyValue(UIA_IsEnabledPropertyId)),
                    "rect": {"l": int(rect.left), "t": int(rect.top),
                             "r": int(rect.right), "b": int(rect.bottom),
                             "cx": (int(rect.left)+int(rect.right))//2,
                             "cy": (int(rect.top)+int(rect.bottom))//2}
                    if rect else {},
                }
                if d > 0:
                    children = []
                    walker = uia.ControlViewWalker
                    child = walker.GetFirstChildElement(e)
                    while child:
                        dc = _elem(child, d - 1)
                        if dc:
                            children.append(dc)
                        child = walker.GetNextSiblingElement(child)
                    if children:
                        node["children"] = children
                return node
            except Exception:
                return None

        root = uia.RootElement
        if window_title:
            cond = uia.CreatePropertyCondition(UIA_NamePropertyId, window_title)
            root = uia.RootElement.FindFirst(TreeScope_Children, cond) or root
        return _elem(root, depth) or {"error": "Not found"}
    except Exception as e:
        return {"error": str(e)}

# ════════════════════════════════════════════════════════════════════════════
#  BATCH ACTION EXECUTOR
#  Takes a list of actions, runs them sequentially, returns results
# ════════════════════════════════════════════════════════════════════════════
def batch_execute(actions: list[dict]) -> list[dict]:
    """
    Run a sequence of actions. Each action: {"type": ..., ...params}
    Types: click, double_click, right_click, drag, scroll, type, key, combo,
           screenshot, shell, wait, focus, move_mouse, ui_click
    Returns list of results with before/after screenshots for verification.
    """
    results = []
    for i, action in enumerate(actions):
        t = action.get("type", "")
        before = None
        result = {}

        # Capture before-state for verification
        verify = action.get("verify", False)
        if verify:
            before = capture_screen(quality=65)

        try:
            if t == "click":
                result = mouse_click(action["x"], action["y"],
                                     action.get("button", "left"), False)
            elif t == "double_click":
                result = mouse_click(action["x"], action["y"], "left", True)
            elif t == "right_click":
                result = mouse_click(action["x"], action["y"], "right", False)
            elif t == "drag":
                result = mouse_drag(action["x1"], action["y1"],
                                    action["x2"], action["y2"],
                                    action.get("button", "left"),
                                    float(action.get("duration", 0.5)))
            elif t == "scroll":
                result = mouse_scroll(action["x"], action["y"],
                                      int(action.get("dx", 0)),
                                      int(action.get("dy", 3)))
            elif t == "move_mouse":
                result = mouse_move(action["x"], action["y"])
            elif t == "type":
                result = keyboard_type(action["text"],
                                       float(action.get("interval", 0.02)))
            elif t == "key":
                result = keyboard_key(action["key"])
            elif t == "combo":
                result = keyboard_combo(action["keys"])
            elif t == "screenshot":
                frame = capture_screen()
                result = {"jpeg_b64": base64.b64encode(frame).decode()}
            elif t == "shell":
                result = shell_exec(action["cmd"],
                                    action.get("cwd"),
                                    int(action.get("timeout", 30)))
            elif t == "wait":
                time.sleep(float(action.get("seconds", 0.5)))
                result = {"waited": action.get("seconds", 0.5)}
            elif t == "focus":
                result = window_action(action["title"], "focus")
            elif t == "ui_click":
                result = ui_find_and_click(action.get("window", ""),
                                           action["element"],
                                           action.get("element_type"))
            else:
                result = {"error": f"Unknown action type: {t}"}

        except Exception as e:
            result = {"error": str(e)}

        entry = {"step": i, "type": t, "result": result}

        # Capture after-state for verification
        if verify:
            time.sleep(0.2)  # let UI settle
            after = capture_screen(quality=65)
            # Quick diff — did anything change?
            b1 = hashlib.md5(Image.open(io.BytesIO(before)).resize((80,45)).tobytes()).hexdigest()
            b2 = hashlib.md5(Image.open(io.BytesIO(after)).resize((80,45)).tobytes()).hexdigest()
            entry["changed"] = b1 != b2
            entry["after_b64"] = base64.b64encode(after).decode()

        results.append(entry)

        # Small gap between actions
        if i < len(actions) - 1:
            time.sleep(float(action.get("delay_after", 0.1)))

    return results

# ════════════════════════════════════════════════════════════════════════════
#  CLIPBOARD — text and image
# ════════════════════════════════════════════════════════════════════════════
def clipboard_get(as_image: bool = False) -> dict:
    if not IS_WINDOWS:
        return {"error": "Windows only"}
    if as_image:
        try:
            img = ImageGrab.grabclipboard()
            if img:
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return {"image_b64": base64.b64encode(buf.getvalue()).decode(),
                        "format": "png"}
            return {"error": "No image in clipboard"}
        except Exception as e:
            return {"error": str(e)}
    r = subprocess.run(["powershell", "-Command", "Get-Clipboard"],
                       capture_output=True, text=True, **_NO_WIN)
    return {"text": r.stdout.strip()}

def clipboard_set(text: str = None, image_b64: str = None) -> dict:
    if not IS_WINDOWS:
        return {"error": "Windows only"}
    if image_b64:
        try:
            img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
            # Use win32clipboard via ctypes
            import win32clipboard
            output = io.BytesIO()
            img.convert("RGB").save(output, "BMP")
            data = output.getvalue()[14:]
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
            win32clipboard.CloseClipboard()
            return {"set": "image"}
        except Exception as e:
            return {"error": f"Image clipboard: {e}"}
    text = text or ""
    subprocess.run(["powershell", "-Command",
                    f"Set-Clipboard -Value '{text.replace(chr(39), chr(39)*2)}'"],
                   capture_output=True, **_NO_WIN)
    return {"set": "text"}

# ════════════════════════════════════════════════════════════════════════════
#  PC MEMORY / NOTES — AI writes discoveries, reads them back
# ════════════════════════════════════════════════════════════════════════════
def notes_read() -> str:
    try:
        if _NOTES_PATH.exists():
            return _NOTES_PATH.read_text(encoding="utf-8")
        return "# vyro-mcp Notes\n\n(empty — write discoveries here)\n"
    except Exception as e:
        return f"Error reading notes: {e}"

def notes_write(content: str) -> dict:
    try:
        _NOTES_PATH.write_text(content, encoding="utf-8")
        return {"written": str(_NOTES_PATH), "bytes": len(content)}
    except Exception as e:
        return {"error": str(e)}

def notes_append(content: str) -> dict:
    try:
        with open(_NOTES_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n{content}\n")
        return {"appended": len(content)}
    except Exception as e:
        return {"error": str(e)}

# ════════════════════════════════════════════════════════════════════════════
#  CHROME DEVTOOLS PROTOCOL
# ════════════════════════════════════════════════════════════════════════════
def chrome_command(method: str, params: dict = None) -> dict:
    """
    Send a CDP command to Chrome (must be running with --remote-debugging-port=9222).
    Examples: Page.captureScreenshot, Runtime.evaluate, DOM.querySelector
    """
    try:
        import urllib.request
        # Get list of targets
        with urllib.request.urlopen("http://localhost:9222/json") as r:
            targets = json.loads(r.read())
        if not targets:
            return {"error": "No Chrome tabs found"}
        # Use first page target
        page = next((t for t in targets if t.get("type") == "page"), targets[0])
        ws_url = page.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return {"error": "No WebSocket debugger URL"}

        # Simple WebSocket client
        import socket as _sock
        from urllib.parse import urlparse
        parsed = urlparse(ws_url)
        s = _sock.create_connection((parsed.hostname, parsed.port or 80), timeout=5)

        # WebSocket handshake
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        s.send(handshake)
        resp = s.recv(4096)
        if b"101" not in resp:
            s.close()
            return {"error": "WebSocket upgrade failed"}

        # Send CDP command
        msg = json.dumps({"id": 1, "method": method,
                          "params": params or {}}).encode()
        # Frame it (simple WebSocket text frame)
        length = len(msg)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        else:
            header = bytes([0x81, 0xFE, length >> 8, length & 0xFF])
        mask = os.urandom(4)
        masked = bytes([msg[i] ^ mask[i % 4] for i in range(length)])
        s.send(header + mask + masked)

        # Read response
        s.settimeout(5)
        data = s.recv(65536)
        s.close()
        # Strip WebSocket frame header
        if len(data) > 2:
            payload_start = 2
            pl = data[1] & 0x7F
            if pl == 126:
                payload_start = 4
            return json.loads(data[payload_start:])
        return {"error": "Empty response"}
    except Exception as e:
        return {"error": f"CDP: {e}"}

def chrome_eval(js: str) -> dict:
    """Evaluate JavaScript in Chrome's current page."""
    return chrome_command("Runtime.evaluate", {
        "expression": js,
        "returnByValue": True,
        "awaitPromise": True,
    })

def chrome_screenshot() -> bytes | None:
    """Take screenshot via CDP — bypasses OS, works even if window is covered."""
    result = chrome_command("Page.captureScreenshot", {"format": "jpeg", "quality": 85})
    if "result" in result and "data" in result["result"]:
        return base64.b64decode(result["result"]["data"])
    return None

# ════════════════════════════════════════════════════════════════════════════
#  EVENT HOOKS — process watcher, download detector
# ════════════════════════════════════════════════════════════════════════════
def _event_watcher_loop():
    """Background: watches for process crashes, new windows, downloads."""
    known_pids: set[int] = set()
    download_folder = Path(os.path.expanduser("~/Downloads"))
    known_downloads: set[str] = set()

    if download_folder.exists():
        known_downloads = {f.name for f in download_folder.iterdir()}

    while True:
        try:
            # Check watched processes
            for name, pid in list(_watched_processes.items()):
                try:
                    p = psutil.Process(pid)
                    if not p.is_running():
                        _push_event("process_exited", {"name": name, "pid": pid})
                        del _watched_processes[name]
                except psutil.NoSuchProcess:
                    _push_event("process_crashed", {"name": name, "pid": pid})
                    _watched_processes.pop(name, None)

            # Check new downloads
            if download_folder.exists():
                current = {f.name for f in download_folder.iterdir()
                           if not f.name.endswith(".crdownload") and
                              not f.name.endswith(".tmp")}
                new_files = current - known_downloads
                for f in new_files:
                    _push_event("download_complete", {"file": f,
                                "path": str(download_folder / f)})
                known_downloads = current

        except Exception:
            pass
        time.sleep(2)

# ════════════════════════════════════════════════════════════════════════════
#  TUNNEL
# ════════════════════════════════════════════════════════════════════════════
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
        cmd = ([cf_bin, "tunnel", "--no-autoupdate", "run", "--token", cf_token]
               if cf_token else
               [cf_bin, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"])
        _tunnel_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True,
                                         **_NO_WIN)
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
                           capture_output=True, **_NO_WIN)
        cmd = [ngrok_bin, "http", str(port), "--log", "stdout"]
        if ngrok_domain:
            cmd += ["--domain", ngrok_domain]
        _tunnel_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True,
                                         **_NO_WIN)
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
def _log_req():
    _log(f"API {request.method} {request.path}")

# Screen
@api.route("/screen")
def api_screen():
    region = None
    if request.args.get("x"):
        region = {k: int(request.args[k]) for k in ("x","y","w","h")}
    return _ok({"jpeg_b64": capture_screen_b64(region=region)})

@api.route("/screen/latest")
def api_screen_latest():
    frame = _screen_frame or capture_screen()
    return Response(frame, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})

@api.route("/screen/stream")
def api_screen_stream():
    def gen():
        while True:
            frame = _screen_frame or capture_screen()
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(0.1)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

# Camera
@api.route("/camera/snap")
def api_camera_snap():
    device = int(request.args.get("device", 0))
    try:
        return _ok({"jpeg_b64": base64.b64encode(camera_snap(device)).decode()})
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
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + _camera_frame + b"\r\n"
            time.sleep(0.067)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

# Voice
@api.route("/speak", methods=["POST"])
def api_speak():
    d = request.json or {}
    speak(d.get("text",""), d.get("rate", 175), d.get("voice",""))
    return _ok("speaking")

# Shell
@api.route("/shell", methods=["POST"])
def api_shell():
    d = request.json or {}
    if not d.get("cmd"):
        return _err("cmd required")
    try:
        return _ok(shell_exec(d["cmd"], d.get("cwd"), int(d.get("timeout", 30))))
    except subprocess.TimeoutExpired:
        return _err("timeout")
    except Exception as e:
        return _err(str(e))

# Filesystem
@api.route("/fs/read", methods=["POST"])
def api_fs_read():
    d = request.json or {}
    try:
        p = Path(d["path"])
        if d.get("binary"):
            return _ok({"b64": base64.b64encode(p.read_bytes()).decode()})
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
        entries = [{"name": i.name,
                    "type": "dir" if i.is_dir() else "file",
                    "size": i.stat().st_size if i.is_file() else 0,
                    "modified": datetime.fromtimestamp(i.stat().st_mtime).isoformat()}
                   for i in p.iterdir()]
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

# Mouse
@api.route("/mouse/click", methods=["POST"])
def api_mouse_click():
    d = request.json or {}
    return _ok(mouse_click(int(d["x"]), int(d["y"]),
                            d.get("button","left"), bool(d.get("double"))))

@api.route("/mouse/drag", methods=["POST"])
def api_mouse_drag():
    d = request.json or {}
    return _ok(mouse_drag(int(d["x1"]), int(d["y1"]),
                           int(d["x2"]), int(d["y2"]),
                           d.get("button","left"), float(d.get("duration", 0.5))))

@api.route("/mouse/scroll", methods=["POST"])
def api_mouse_scroll():
    d = request.json or {}
    return _ok(mouse_scroll(int(d.get("x",0)), int(d.get("y",0)),
                             int(d.get("dx",0)), int(d.get("dy",3))))

@api.route("/mouse/move", methods=["POST"])
def api_mouse_move():
    d = request.json or {}
    return _ok(mouse_move(int(d["x"]), int(d["y"])))

# Keyboard
@api.route("/keyboard/type", methods=["POST"])
def api_keyboard_type():
    d = request.json or {}
    return _ok(keyboard_type(d.get("text",""), float(d.get("interval",0.02))))

@api.route("/keyboard/key", methods=["POST"])
def api_keyboard_key():
    d = request.json or {}
    return _ok(keyboard_key(d.get("key","")))

@api.route("/keyboard/combo", methods=["POST"])
def api_keyboard_combo():
    d = request.json or {}
    return _ok(keyboard_combo(d.get("keys","")))

# Batch executor
@api.route("/batch", methods=["POST"])
def api_batch():
    d = request.json or {}
    actions = d.get("actions", [])
    if not actions:
        return _err("actions list required")
    try:
        results = batch_execute(actions)
        return _ok(results)
    except Exception as e:
        return _err(str(e))

# UI
@api.route("/ui/tree", methods=["POST"])
def api_ui_tree():
    d = request.json or {}
    return _ok(get_ui_tree(d.get("window"), int(d.get("depth", 3))))

@api.route("/ui/windows")
def api_ui_windows():
    return _ok(list_windows())

@api.route("/ui/click", methods=["POST"])
def api_ui_click():
    d = request.json or {}
    if "element" in d:
        return _ok(ui_find_and_click(d.get("window",""), d["element"],
                                     d.get("element_type")))
    return _ok(mouse_click(int(d.get("x",0)), int(d.get("y",0)),
                            d.get("button","left")))

@api.route("/ui/type", methods=["POST"])
def api_ui_type():
    d = request.json or {}
    return _ok(keyboard_type(d.get("text","")))

@api.route("/ui/key", methods=["POST"])
def api_ui_key():
    d = request.json or {}
    return _ok(keyboard_key(d.get("key","")))

@api.route("/ui/combo", methods=["POST"])
def api_ui_combo():
    d = request.json or {}
    return _ok(keyboard_combo(d.get("keys","")))

@api.route("/ui/focus", methods=["POST"])
def api_ui_focus():
    d = request.json or {}
    return _ok(window_action(d.get("title",""), "focus"))

@api.route("/ui/window", methods=["POST"])
def api_ui_window():
    d = request.json or {}
    return _ok(window_action(d.get("title",""), d.get("action","info"),
                              d.get("x"), d.get("y"), d.get("w"), d.get("h")))

# System
@api.route("/sysinfo")
def api_sysinfo():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return _ok({
        "os": platform.platform(), "hostname": socket.gethostname(),
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_cores": psutil.cpu_count(),
        "memory": {"total_gb": round(mem.total/1e9,1),
                   "used_gb": round(mem.used/1e9,1), "percent": mem.percent},
        "disk": {"total_gb": round(disk.total/1e9,1),
                 "used_gb": round(disk.used/1e9,1), "percent": disk.percent},
        "uptime": str(datetime.now() - datetime.fromtimestamp(psutil.boot_time())),
    })

@api.route("/processes")
def api_processes():
    flt = request.args.get("filter","").lower()
    procs = []
    for p in psutil.process_iter(["pid","name","status","cpu_percent","memory_percent"]):
        if not flt or flt in (p.info.get("name") or "").lower():
            procs.append(p.info)
    return _ok(procs)

@api.route("/processes/kill", methods=["POST"])
def api_kill():
    d = request.json or {}
    try:
        if d.get("pid"):
            os.kill(int(d["pid"]), 9)
            return _ok(f"killed {d['pid']}")
        killed = []
        for p in psutil.process_iter(["pid","name"]):
            if d.get("name","").lower() in (p.info.get("name") or "").lower():
                os.kill(p.pid, 9); killed.append(p.pid)
        return _ok(killed)
    except Exception as e:
        return _err(str(e))

# Clipboard
@api.route("/clipboard", methods=["GET","POST"])
def api_clipboard():
    if request.method == "GET":
        img = request.args.get("image","false").lower() == "true"
        return _ok(clipboard_get(img))
    d = request.json or {}
    if d.get("image_b64"):
        return _ok(clipboard_set(image_b64=d["image_b64"]))
    return _ok(clipboard_set(text=d.get("text","")))

# PC Memory / Notes
@api.route("/notes", methods=["GET"])
def api_notes_get():
    return _ok({"content": notes_read()})

@api.route("/notes", methods=["POST"])
def api_notes_set():
    d = request.json or {}
    if d.get("append"):
        return _ok(notes_append(d.get("content","")))
    return _ok(notes_write(d.get("content","")))

# Chrome DevTools
@api.route("/chrome/command", methods=["POST"])
def api_chrome_cmd():
    d = request.json or {}
    return _ok(chrome_command(d.get("method",""), d.get("params",{})))

@api.route("/chrome/eval", methods=["POST"])
def api_chrome_eval():
    d = request.json or {}
    return _ok(chrome_eval(d.get("js","")))

@api.route("/chrome/screenshot")
def api_chrome_screenshot():
    frame = chrome_screenshot()
    if frame:
        return Response(frame, mimetype="image/jpeg")
    return _err("Chrome not reachable — start with --remote-debugging-port=9222")

# Events
@api.route("/events")
def api_events():
    since = int(request.args.get("since", 0))
    return _ok(_event_queue[since:])

@api.route("/events/watch", methods=["POST"])
def api_events_watch():
    d = request.json or {}
    name = d.get("name","")
    # find pid of process
    for p in psutil.process_iter(["pid","name"]):
        if name.lower() in (p.info.get("name") or "").lower():
            _watched_processes[name] = p.pid
            return _ok(f"watching {name} (pid {p.pid})")
    return _err(f"Process '{name}' not found")

# Tunnel
@api.route("/tunnel/start", methods=["POST"])
def api_tunnel_start():
    d = request.json or {}
    def _run():
        url = start_tunnel(d.get("port", _api_port), d.get("provider","cloudflare"),
                           d.get("cf_token",""), d.get("ngrok_token",""), d.get("ngrok_domain",""))
        _log(f"Tunnel: {url}")
    threading.Thread(target=_run, daemon=True).start()
    return _ok("starting")

@api.route("/tunnel/status")
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

@api.route("/", methods=["GET"])
def api_root():
    return _ok({
        "name": "vyro-mcp", "version": "3.0.0",
        "platform": platform.platform(),
        "tunnel_url": _tunnel_url,
        "requests_served": _request_count,
        "capabilities": {
            "screen": True, "camera": HAS_CV2, "audio": HAS_AUDIO,
            "ui_automation": HAS_WINAUTO or HAS_UIA,
            "pywinauto": HAS_WINAUTO, "mouse": True,
            "keyboard_combos": True, "batch": True,
            "notes": True, "chrome_devtools": True, "events": True,
        }
    })

# ════════════════════════════════════════════════════════════════════════════
#  MCP-over-SSE  (Claude Desktop connects here)
# ════════════════════════════════════════════════════════════════════════════
_mcp_clients: dict = {}

_MCP_TOOLS = [
    {"name":"screen_capture",
     "description":"Take a screenshot. Returns the actual screen image so you can SEE what's on the PC.",
     "inputSchema":{"type":"object","properties":{"region":{"type":"object","description":"Optional {x,y,w,h}","properties":{"x":{"type":"integer"},"y":{"type":"integer"},"w":{"type":"integer"},"h":{"type":"integer"}}}},"required":[]}},
    {"name":"shell_exec",
     "description":"Run a shell/PowerShell command silently in the background. No terminal opens.",
     "inputSchema":{"type":"object","properties":{"cmd":{"type":"string"},"cwd":{"type":"string"},"timeout":{"type":"integer","default":30}},"required":["cmd"]}},
    {"name":"batch_actions",
     "description":"Run a sequence of mouse/keyboard/UI actions in one shot. Much faster than one at a time. Each action: {type, ...params}. Types: click, double_click, right_click, drag, scroll, move_mouse, type, key, combo, focus, ui_click, shell, wait, screenshot.",
     "inputSchema":{"type":"object","properties":{"actions":{"type":"array","items":{"type":"object"},"description":"List of actions to execute"}},"required":["actions"]}},
    {"name":"mouse_click",
     "description":"Click at screen coordinates. button: left/right/middle. double: true for double-click.",
     "inputSchema":{"type":"object","properties":{"x":{"type":"integer"},"y":{"type":"integer"},"button":{"type":"string","enum":["left","right","middle"],"default":"left"},"double":{"type":"boolean","default":False}},"required":["x","y"]}},
    {"name":"mouse_drag",
     "description":"Click and drag from one point to another. Use for selecting text, moving windows, sliders.",
     "inputSchema":{"type":"object","properties":{"x1":{"type":"integer"},"y1":{"type":"integer"},"x2":{"type":"integer"},"y2":{"type":"integer"},"duration":{"type":"number","default":0.5}},"required":["x1","y1","x2","y2"]}},
    {"name":"mouse_scroll",
     "description":"Scroll the mouse wheel at a position. dy positive = scroll up, negative = scroll down.",
     "inputSchema":{"type":"object","properties":{"x":{"type":"integer"},"y":{"type":"integer"},"dy":{"type":"integer","default":3}},"required":["x","y"]}},
    {"name":"keyboard_type",
     "description":"Type text into the focused window.",
     "inputSchema":{"type":"object","properties":{"text":{"type":"string"},"interval":{"type":"number","default":0.02}},"required":["text"]}},
    {"name":"keyboard_combo",
     "description":"Press a keyboard shortcut. Format: 'ctrl+c', 'alt+tab', 'win+d', 'ctrl+shift+esc', 'ctrl+alt+delete'. Keys joined by +.",
     "inputSchema":{"type":"object","properties":{"keys":{"type":"string","description":"e.g. 'ctrl+c' or 'alt+tab'"}},"required":["keys"]}},
    {"name":"keyboard_key",
     "description":"Press a single key: enter, tab, escape, f5, delete, home, end, pageup, pagedown, up, down, left, right.",
     "inputSchema":{"type":"object","properties":{"key":{"type":"string"}},"required":["key"]}},
    {"name":"ui_find_click",
     "description":"Find a UI element by name and click it — no pixel guessing. Much more reliable than coordinate clicks. Uses Windows UI Automation.",
     "inputSchema":{"type":"object","properties":{"window":{"type":"string","description":"Window title (partial)"},"element":{"type":"string","description":"Button/control name"},"element_type":{"type":"string","description":"Optional: Button, Edit, MenuItem, etc."}},"required":["element"]}},
    {"name":"ui_tree",
     "description":"Get the UI element tree of a window — lists all buttons, fields, menus with their names and positions. Use this to discover element names before clicking.",
     "inputSchema":{"type":"object","properties":{"window":{"type":"string"},"depth":{"type":"integer","default":3}},"required":[]}},
    {"name":"ui_windows",
     "description":"List all open windows with titles and positions.",
     "inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"window_action",
     "description":"Control a window: focus, minimize, maximize, restore, close, move, resize, info.",
     "inputSchema":{"type":"object","properties":{"title":{"type":"string"},"action":{"type":"string","enum":["focus","minimize","maximize","restore","close","move","resize","info"]},"x":{"type":"integer"},"y":{"type":"integer"},"w":{"type":"integer"},"h":{"type":"integer"}},"required":["title","action"]}},
    {"name":"fs_read",
     "description":"Read a file from the PC.",
     "inputSchema":{"type":"object","properties":{"path":{"type":"string"},"binary":{"type":"boolean","default":False}},"required":["path"]}},
    {"name":"fs_write",
     "description":"Write or append to a file.",
     "inputSchema":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"append":{"type":"boolean","default":False}},"required":["path","content"]}},
    {"name":"fs_list",
     "description":"List files in a directory.",
     "inputSchema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name":"fs_delete",
     "description":"Delete a file or folder.",
     "inputSchema":{"type":"object","properties":{"path":{"type":"string"},"recursive":{"type":"boolean","default":False}},"required":["path"]}},
    {"name":"speak",
     "description":"Make the PC say something out loud using text-to-speech.",
     "inputSchema":{"type":"object","properties":{"text":{"type":"string"},"rate":{"type":"integer","default":175}},"required":["text"]}},
    {"name":"sysinfo",
     "description":"Get CPU, RAM, disk, hostname, OS, uptime.",
     "inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"processes",
     "description":"List running processes. Optional name filter.",
     "inputSchema":{"type":"object","properties":{"filter":{"type":"string"}},"required":[]}},
    {"name":"process_kill",
     "description":"Kill a process by PID or name.",
     "inputSchema":{"type":"object","properties":{"pid":{"type":"integer"},"name":{"type":"string"}},"required":[]}},
    {"name":"clipboard_get",
     "description":"Read Windows clipboard. as_image=true returns clipboard image as JPEG.",
     "inputSchema":{"type":"object","properties":{"as_image":{"type":"boolean","default":False}},"required":[]}},
    {"name":"clipboard_set",
     "description":"Write text or image to Windows clipboard.",
     "inputSchema":{"type":"object","properties":{"text":{"type":"string"},"image_b64":{"type":"string"}},"required":[]}},
    {"name":"notes_read",
     "description":"Read your PC memory notes — discoveries, coordinates, app quirks you wrote in previous sessions.",
     "inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"notes_write",
     "description":"Write to PC memory notes. Use this to save useful discoveries: 'Paint canvas starts at x=180,y=120 on this PC', 'Store search box at x=960,y=85'. Append=true adds without replacing.",
     "inputSchema":{"type":"object","properties":{"content":{"type":"string"},"append":{"type":"boolean","default":True}},"required":["content"]}},
    {"name":"chrome_eval",
     "description":"Run JavaScript in Chrome. Chrome must be open. No pixel clicking needed — works directly with the DOM.",
     "inputSchema":{"type":"object","properties":{"js":{"type":"string"}},"required":["js"]}},
    {"name":"chrome_screenshot",
     "description":"Take a screenshot via Chrome DevTools. Works even if Chrome window is behind other windows.",
     "inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"events_get",
     "description":"Get event notifications: downloads completed, processes crashed, screen changes.",
     "inputSchema":{"type":"object","properties":{"since":{"type":"integer","default":0,"description":"Get events after this index"}},"required":[]}},
    {"name":"tunnel_start",
     "description":"Start a Cloudflare or ngrok tunnel to expose this server to the internet.",
     "inputSchema":{"type":"object","properties":{"provider":{"type":"string","enum":["cloudflare","ngrok"],"default":"cloudflare"},"cf_token":{"type":"string","default":""},"ngrok_token":{"type":"string","default":""},"ngrok_domain":{"type":"string","default":""}},"required":[]}},
    {"name":"tunnel_status",
     "description":"Get current tunnel URL and status.",
     "inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"camera_snap",
     "description":"Take a webcam photo. Returns the image.",
     "inputSchema":{"type":"object","properties":{"device":{"type":"integer","default":0}},"required":[]}},
]

def _mcp_call_tool(name: str, args: dict) -> list:
    """Execute a tool call. Returns list of MCP content blocks."""
    try:
        if name == "screen_capture":
            frame = (_screen_frame if not args.get("region")
                     else capture_screen(region=args["region"]))
            if frame is None:
                frame = capture_screen(region=args.get("region"))
            return [{"type":"image","data":base64.b64encode(frame).decode(),
                     "mimeType":"image/jpeg"}]

        elif name == "shell_exec":
            r = shell_exec(args["cmd"], args.get("cwd"), int(args.get("timeout",30)))
            return [{"type":"text","text":json.dumps(r, indent=2)}]

        elif name == "batch_actions":
            results = batch_execute(args.get("actions",[]))
            # Collect any after screenshots
            content = [{"type":"text","text":json.dumps(
                [{k:v for k,v in r.items() if k!="after_b64"} for r in results],
                indent=2)}]
            # Append last screenshot if available
            last = next((r for r in reversed(results) if r.get("after_b64")), None)
            if last:
                content.append({"type":"image","data":last["after_b64"],
                                 "mimeType":"image/jpeg"})
            return content

        elif name == "mouse_click":
            r = mouse_click(int(args["x"]), int(args["y"]),
                            args.get("button","left"), bool(args.get("double")))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "mouse_drag":
            r = mouse_drag(int(args["x1"]), int(args["y1"]),
                           int(args["x2"]), int(args["y2"]),
                           args.get("button","left"),
                           float(args.get("duration",0.5)))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "mouse_scroll":
            r = mouse_scroll(int(args.get("x",0)), int(args.get("y",0)),
                             0, int(args.get("dy",3)))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "keyboard_type":
            r = keyboard_type(args.get("text",""), float(args.get("interval",0.02)))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "keyboard_combo":
            r = keyboard_combo(args.get("keys",""))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "keyboard_key":
            r = keyboard_key(args.get("key",""))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "ui_find_click":
            r = ui_find_and_click(args.get("window",""), args["element"],
                                  args.get("element_type"))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "ui_tree":
            return [{"type":"text","text":json.dumps(
                get_ui_tree(args.get("window"), int(args.get("depth",3))), indent=2)}]

        elif name == "ui_windows":
            return [{"type":"text","text":json.dumps(list_windows(), indent=2)}]

        elif name == "window_action":
            r = window_action(args.get("title",""), args.get("action","info"),
                              args.get("x"), args.get("y"),
                              args.get("w"), args.get("h"))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "fs_read":
            p = Path(args["path"])
            if args.get("binary"):
                return [{"type":"text","text":base64.b64encode(p.read_bytes()).decode()}]
            return [{"type":"text","text":p.read_text(errors="replace")}]

        elif name == "fs_write":
            p = Path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if args.get("append") else "w"
            with open(p, mode, encoding="utf-8") as f:
                f.write(args["content"])
            return [{"type":"text","text":f"Written: {p}"}]

        elif name == "fs_list":
            p = Path(args["path"])
            entries = [{"name": i.name, "type": "dir" if i.is_dir() else "file",
                        "size": i.stat().st_size if i.is_file() else 0}
                       for i in p.iterdir()]
            return [{"type":"text","text":json.dumps(entries, indent=2)}]

        elif name == "fs_delete":
            p = Path(args["path"])
            if p.is_dir() and args.get("recursive"):
                shutil.rmtree(p)
            elif p.is_dir():
                p.rmdir()
            else:
                p.unlink()
            return [{"type":"text","text":f"Deleted: {p}"}]

        elif name == "speak":
            speak(args["text"], int(args.get("rate",175)))
            return [{"type":"text","text":"Speaking..."}]

        elif name == "sysinfo":
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            return [{"type":"text","text":json.dumps({
                "os": platform.platform(), "hostname": socket.gethostname(),
                "cpu_percent": psutil.cpu_percent(interval=0.3),
                "cpu_cores": psutil.cpu_count(),
                "memory": {"total_gb": round(mem.total/1e9,1),
                           "used_gb": round(mem.used/1e9,1), "percent": mem.percent},
                "disk": {"total_gb": round(disk.total/1e9,1),
                         "used_gb": round(disk.used/1e9,1), "percent": disk.percent},
            }, indent=2)}]

        elif name == "processes":
            flt = args.get("filter","").lower()
            procs = [p.info for p in psutil.process_iter(["pid","name","status",
                                                           "cpu_percent","memory_percent"])
                     if not flt or flt in (p.info.get("name") or "").lower()]
            return [{"type":"text","text":json.dumps(procs[:100], indent=2)}]

        elif name == "process_kill":
            if args.get("pid"):
                os.kill(int(args["pid"]), 9)
                return [{"type":"text","text":f"Killed PID {args['pid']}"}]
            killed = []
            for proc in psutil.process_iter(["pid","name"]):
                if args.get("name","").lower() in (proc.info.get("name") or "").lower():
                    os.kill(proc.pid, 9); killed.append(proc.pid)
            return [{"type":"text","text":f"Killed: {killed}"}]

        elif name == "clipboard_get":
            r = clipboard_get(bool(args.get("as_image")))
            if r.get("image_b64"):
                return [{"type":"image","data":r["image_b64"],"mimeType":"image/png"}]
            return [{"type":"text","text":r.get("text","") or json.dumps(r)}]

        elif name == "clipboard_set":
            r = clipboard_set(args.get("text"), args.get("image_b64"))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "notes_read":
            return [{"type":"text","text":notes_read()}]

        elif name == "notes_write":
            if args.get("append", True):
                r = notes_append(args.get("content",""))
            else:
                r = notes_write(args.get("content",""))
            return [{"type":"text","text":json.dumps(r)}]

        elif name == "chrome_eval":
            r = chrome_eval(args.get("js",""))
            return [{"type":"text","text":json.dumps(r, indent=2)}]

        elif name == "chrome_screenshot":
            frame = chrome_screenshot()
            if frame:
                return [{"type":"image","data":base64.b64encode(frame).decode(),
                         "mimeType":"image/jpeg"}]
            return [{"type":"text","text":"Chrome not available or --remote-debugging-port=9222 not set"}]

        elif name == "events_get":
            since = int(args.get("since", 0))
            return [{"type":"text","text":json.dumps(_event_queue[since:], indent=2)}]

        elif name == "tunnel_start":
            def _run():
                start_tunnel(_api_port, args.get("provider","cloudflare"),
                             args.get("cf_token",""), args.get("ngrok_token",""),
                             args.get("ngrok_domain",""))
            threading.Thread(target=_run, daemon=True).start()
            return [{"type":"text","text":"Tunnel starting..."}]

        elif name == "tunnel_status":
            running = _tunnel_proc is not None and _tunnel_proc.poll() is None
            return [{"type":"text","text":json.dumps({"running":running,"url":_tunnel_url})}]

        elif name == "camera_snap":
            frame = camera_snap(int(args.get("device",0)))
            return [{"type":"image","data":base64.b64encode(frame).decode(),
                     "mimeType":"image/jpeg"}]

        else:
            return [{"type":"text","text":f"Unknown tool: {name}"}]

    except Exception as e:
        return [{"type":"text","text":f"Error in {name}: {e}"}]

def _mcp_handle(msg: dict) -> dict | None:
    method = msg.get("method","")
    mid    = msg.get("id")
    params = msg.get("params", {})

    def ok(result):
        return {"jsonrpc":"2.0","id":mid,"result":result}
    def err(code, text):
        return {"jsonrpc":"2.0","id":mid,"error":{"code":code,"message":text}}

    if method == "initialize":
        return ok({"protocolVersion":"2024-11-05",
                   "capabilities":{"tools":{}},"serverInfo":{"name":"vyro-mcp","version":"3.0.0"}})
    elif method == "initialized":
        return None
    elif method == "tools/list":
        return ok({"tools": _MCP_TOOLS})
    elif method == "tools/call":
        name = params.get("name","")
        args = params.get("arguments", {})
        _log(f"MCP: {name}")
        return ok({"content": _mcp_call_tool(name, args)})
    elif method == "ping":
        return ok({})
    else:
        if mid is not None:
            return err(-32601, f"Method not found: {method}")
        return None

@api.route("/mcp", methods=["GET"])
def mcp_sse():
    client_id = uuid.uuid4().hex
    q = queue.Queue()
    _mcp_clients[client_id] = q

    def stream():
        post_url = f"{request.url_root.rstrip('/')}mcp"
        yield f"event: endpoint\ndata: {post_url}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            _mcp_clients.pop(client_id, None)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no",
                             "Access-Control-Allow-Origin":"*"})

@api.route("/mcp", methods=["POST"])
def mcp_post():
    body = request.get_json(force=True, silent=True)
    if body is None:
        return jsonify({"jsonrpc":"2.0","id":None,
                        "error":{"code":-32700,"message":"Parse error"}}), 400
    if isinstance(body, list):
        responses = [r for r in (_mcp_handle(m) for m in body) if r is not None]
        return jsonify(responses) if responses else ("", 204)
    resp = _mcp_handle(body)
    if resp is None:
        return "", 204
    return jsonify(resp)

# ════════════════════════════════════════════════════════════════════════════
#  GUI DASHBOARD  (Tkinter)
# ════════════════════════════════════════════════════════════════════════════
class Dashboard:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("vyro-mcp v3  —  AI Control Server")
        self.root.geometry("960x680")
        self.root.configure(bg="#0d0d0d")
        self.root.resizable(True, True)

        f_title = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        f_mono  = tkfont.Font(family="Consolas", size=9)
        f_label = tkfont.Font(family="Segoe UI", size=10)

        # Header
        hdr = tk.Frame(self.root, bg="#111", height=52)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚡ vyro-mcp v3", font=f_title,
                 fg="#00e5ff", bg="#111").pack(side="left", padx=16, pady=10)
        caps = []
        if HAS_WINAUTO: caps.append("UIA✓")
        if HAS_GUI:     caps.append("GUI✓")
        if HAS_CV2:     caps.append("CAM✓")
        cap_str = "  ".join(caps)
        tk.Label(hdr, text=cap_str, font=f_mono, fg="#888", bg="#111").pack(side="left", padx=8)
        self.lbl_status = tk.Label(hdr, text="● STARTING", font=f_label,
                                   fg="#ff9800", bg="#111")
        self.lbl_status.pack(side="right", padx=16)

        main = tk.Frame(self.root, bg="#0d0d0d")
        main.pack(fill="both", expand=True, padx=12, pady=8)

        # Left panel
        left = tk.Frame(main, bg="#0d0d0d", width=295)
        left.pack(side="left", fill="y", padx=(0,8))
        left.pack_propagate(False)

        def card(parent, label):
            f = tk.LabelFrame(parent, text=f" {label} ", font=f_label,
                              fg="#00e5ff", bg="#161616", relief="flat", bd=0,
                              highlightbackground="#2a2a2a", highlightthickness=1)
            f.pack(fill="x", pady=(0,6))
            return f

        c1 = card(left, "Local API")
        self.lbl_local = tk.Label(c1, text=f"http://localhost:{_api_port}",
                                  font=f_mono, fg="#a0ffa0", bg="#161616", cursor="hand2")
        self.lbl_local.pack(padx=10, pady=6)
        self.lbl_local.bind("<Button-1>", lambda e: self._copy(self.lbl_local.cget("text")))

        c2 = card(left, "Tunnel URL")
        self.lbl_tunnel = tk.Label(c2, text="Not started", font=f_mono,
                                   fg="#888", bg="#161616", wraplength=270, cursor="hand2")
        self.lbl_tunnel.pack(padx=10, pady=6)
        self.lbl_tunnel.bind("<Button-1>", lambda e: self._copy(self.lbl_tunnel.cget("text")))

        tf = tk.Frame(left, bg="#0d0d0d")
        tf.pack(fill="x", pady=(0,6))
        tk.Label(tf, text="Provider:", font=f_label, fg="#aaa", bg="#0d0d0d").pack(side="left")
        self.var_provider = tk.StringVar(value="cloudflare")
        tk.OptionMenu(tf, self.var_provider, "cloudflare","ngrok").pack(side="left", padx=4)

        tk.Label(left, text="Token (optional):", font=f_label, fg="#aaa", bg="#0d0d0d").pack(anchor="w")
        self.ent_token = tk.Entry(left, font=f_mono, bg="#1a1a1a", fg="#fff",
                                  insertbackground="#fff", show="•")
        self.ent_token.pack(fill="x", pady=(0,6))

        bf = tk.Frame(left, bg="#0d0d0d")
        bf.pack(fill="x", pady=(0,10))
        tk.Button(bf, text="▶ Start Tunnel", font=f_label, bg="#005f73", fg="white",
                  relief="flat", padx=8, pady=4,
                  command=self._start_tunnel).pack(side="left", expand=True, fill="x", padx=(0,4))
        tk.Button(bf, text="■ Stop", font=f_label, bg="#400", fg="white",
                  relief="flat", padx=8, pady=4,
                  command=self._stop_tunnel).pack(side="left", expand=True, fill="x")

        c3 = card(left, "Stats")
        self.lbl_reqs = tk.Label(c3, text="Requests: 0", font=f_label, fg="#aaa", bg="#161616")
        self.lbl_reqs.pack(anchor="w", padx=8, pady=2)
        self.lbl_cpu = tk.Label(c3, text="CPU: —  RAM: —", font=f_label, fg="#aaa", bg="#161616")
        self.lbl_cpu.pack(anchor="w", padx=8, pady=2)
        self.lbl_caps = tk.Label(c3,
            text=f"pywinauto: {'✓' if HAS_WINAUTO else '✗'}  "
                 f"pyautogui: {'✓' if HAS_GUI else '✗'}  "
                 f"OpenCV: {'✓' if HAS_CV2 else '✗'}",
            font=f_mono, fg="#666", bg="#161616")
        self.lbl_caps.pack(anchor="w", padx=8, pady=2)

        tk.Button(left, text="📷 Preview Screen", font=f_label, bg="#1a1a2e",
                  fg="#00e5ff", relief="flat", pady=4,
                  command=self._toggle_preview).pack(fill="x", pady=(0,3))
        tk.Button(left, text="📝 Open Notes", font=f_label, bg="#1a1a2e",
                  fg="#00e5ff", relief="flat", pady=4,
                  command=lambda: self._open_browser(f"http://localhost:{_api_port}/notes")).pack(fill="x", pady=(0,3))
        tk.Button(left, text="🌐 API Docs", font=f_label, bg="#1a1a2e",
                  fg="#00e5ff", relief="flat", pady=4,
                  command=lambda: self._open_browser(f"http://localhost:{_api_port}/")).pack(fill="x")

        # Right panel
        right = tk.Frame(main, bg="#0d0d0d")
        right.pack(side="left", fill="both", expand=True)

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)

        # Log tab
        lf = tk.Frame(nb, bg="#0d0d0d")
        nb.add(lf, text=" Activity Log ")
        self.log_text = scrolledtext.ScrolledText(lf, font=f_mono, bg="#0a0a0a",
                                                   fg="#b0ffb0", relief="flat", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        # Screen preview tab
        pf = tk.Frame(nb, bg="#000")
        nb.add(pf, text=" Screen Preview ")
        self.canvas = tk.Canvas(pf, bg="#000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._preview_active = False
        self._preview_img = None

        # Tools tab
        tf2 = tk.Frame(nb, bg="#0a0a0a")
        nb.add(tf2, text=" Tools (31) ")
        ep_text = scrolledtext.ScrolledText(tf2, font=f_mono, bg="#0a0a0a",
                                             fg="#88aaff", relief="flat")
        ep_text.pack(fill="both", expand=True, padx=4, pady=4)
        ep_text.insert("1.0", """
 SCREEN & VISION
   screen_capture       → screenshot (image returned directly)
   /screen/latest       → raw JPEG endpoint
   /screen/stream       → MJPEG live stream

 MOUSE
   mouse_click          → x, y, button, double
   mouse_drag           → x1,y1 → x2,y2 (select text, move windows, sliders)
   mouse_scroll         → x, y, dy (+up / -down)

 KEYBOARD
   keyboard_type        → text
   keyboard_key         → single key (enter, tab, f5...)
   keyboard_combo       → ctrl+c, alt+tab, win+d, ctrl+shift+esc...

 UI AUTOMATION (by name, not pixels)
   ui_find_click        → window + element name → click
   ui_tree              → full control tree with positions
   ui_windows           → all open windows
   window_action        → focus/minimize/maximize/move/resize/close

 BATCH EXECUTOR
   batch_actions        → run full action sequence in one call
                          types: click, drag, scroll, type, combo,
                                 focus, ui_click, shell, wait, screenshot

 FILESYSTEM
   fs_read / fs_write / fs_list / fs_delete

 SYSTEM
   shell_exec           → silent background shell
   sysinfo / processes / process_kill

 CLIPBOARD
   clipboard_get        → text or image
   clipboard_set        → text or image (base64)

 PC MEMORY / NOTES
   notes_read           → read discoveries from previous sessions
   notes_write          → save layout discoveries, quirks, coords

 CHROME DEVTOOLS
   chrome_eval          → run JS in Chrome tab
   chrome_screenshot    → capture via CDP (works covered)

 EVENTS
   events_get           → download complete, process crash, screen change

 TUNNEL
   tunnel_start / tunnel_status
""")
        ep_text.configure(state="disabled")

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook", background="#0d0d0d", borderwidth=0)
        style.configure("TNotebook.Tab", background="#1a1a1a", foreground="#aaa", padding=[10,4])
        style.map("TNotebook.Tab",
                  background=[("selected","#161616")],
                  foreground=[("selected","#00e5ff")])

        self.root.after(1000, self._tick)

    def _copy(self, text):
        if text and text not in ("Not started",):
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def _start_tunnel(self):
        provider = self.var_provider.get()
        token = self.ent_token.get().strip()
        self.lbl_tunnel.config(text="Starting...", fg="#ff9800")
        def _run():
            url = start_tunnel(_api_port, provider,
                               token if provider == "cloudflare" else "",
                               token if provider == "ngrok" else "")
            self.lbl_tunnel.config(text=url or "Failed",
                                   fg="#a0ffa0" if url else "#f44")
        threading.Thread(target=_run, daemon=True).start()

    def _stop_tunnel(self):
        global _tunnel_proc, _tunnel_url
        if _tunnel_proc:
            _tunnel_proc.terminate()
            _tunnel_proc = None; _tunnel_url = ""
        self.lbl_tunnel.config(text="Stopped", fg="#888")

    def _toggle_preview(self):
        self._preview_active = not self._preview_active
        if self._preview_active:
            self._update_preview()

    def _update_preview(self):
        if not self._preview_active:
            return
        try:
            frame = _screen_frame or capture_screen()
            img = Image.open(io.BytesIO(frame))
            w = self.canvas.winfo_width() or 500
            h = self.canvas.winfo_height() or 350
            img.thumbnail((w, h), Image.LANCZOS)
            self._preview_img = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self._preview_img)
        except Exception:
            pass
        self.root.after(500, self._update_preview)

    def _open_browser(self, url):
        import webbrowser
        webbrowser.open(url)

    def _tick(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(_app_log[-100:]))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        self.lbl_reqs.config(text=f"Requests: {_request_count}  |  Events: {len(_event_queue)}")
        try:
            cpu = psutil.cpu_percent()
            mem = psutil.virtual_memory().percent
            self.lbl_cpu.config(text=f"CPU: {cpu:.0f}%  RAM: {mem:.0f}%")
        except Exception:
            pass

        if _tunnel_url and self.lbl_tunnel.cget("text") != _tunnel_url:
            self.lbl_tunnel.config(text=_tunnel_url, fg="#a0ffa0")

        self.lbl_status.config(text="● RUNNING", fg="#00e5ff")
        self.root.after(1000, self._tick)

    def run(self):
        self.root.mainloop()

# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════
def _run_api():
    _log(f"API server on port {_api_port}")
    api.run(host="0.0.0.0", port=_api_port, debug=False,
            use_reloader=False, threaded=True)

if __name__ == "__main__":
    # Auto-install Windows extras silently
    if IS_WINDOWS:
        extras = ["pygetwindow", "pyautogui", "pywinauto"]
        for pkg in extras:
            mod = pkg.replace("-","_")
            try:
                __import__(mod)
            except ImportError:
                try:
                    _pip(pkg)
                except Exception:
                    pass

    _log("vyro-mcp v3.0 starting...")
    _log(f"Platform: {platform.platform()}")
    _log(f"Python:   {sys.version.split()[0]}")
    _log(f"pywinauto: {HAS_WINAUTO}  pyautogui: {HAS_GUI}  OpenCV: {HAS_CV2}")

    # Start continuous screen loop
    _start_screen_loop()
    _log("Screen capture loop active")

    # Start event watcher
    threading.Thread(target=_event_watcher_loop, daemon=True).start()
    _log("Event watcher active")

    # Init notes file
    if not _NOTES_PATH.exists():
        _NOTES_PATH.write_text(
            f"# vyro-mcp Notes\n"
            f"# Created: {datetime.now().isoformat()}\n"
            f"# Platform: {platform.platform()}\n"
            f"# This file stores AI discoveries about this PC.\n"
            f"# AI reads this at the start of each session for context.\n\n",
            encoding="utf-8")
    _log(f"Notes file: {_NOTES_PATH}")

    # Start Flask
    api_thread = threading.Thread(target=_run_api, daemon=True)
    api_thread.start()
    time.sleep(0.5)
    _log(f"API ready → http://localhost:{_api_port}")
    _log(f"MCP endpoint → http://localhost:{_api_port}/mcp")

    # Start GUI on main thread
    dash = Dashboard()
    dash.run()
