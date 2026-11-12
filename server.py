#!/usr/bin/env python3
"""
vyro-mcp — Full root/kernel/hardware MCP server
Gives an AI eyes, ears, mouth, hands, and full system control.
Run as root for kernel-level access.
"""

import asyncio
import base64
import ctypes
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
from datetime import datetime
from pathlib import Path
from typing import Any

# ── third-party (installed at startup) ──────────────────────────────────────
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
except ImportError:
    print("Installing mcp...", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "mcp", "-q",
                           "--break-system-packages"])
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types

# Optional heavy deps — imported lazily
cv2 = None
pyaudio = None
pyttsx3 = None
psutil = None

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("vyro-mcp")

# ── server instance ──────────────────────────────────────────────────────────
app = Server("vyro-mcp")

# ── global state ─────────────────────────────────────────────────────────────
_camera_stream: dict = {}          # thread + frame buffer
_audio_stream: dict = {}           # thread + audio buffer
_tunnel_proc: subprocess.Popen | None = None
_tunnel_url: str = ""


# ════════════════════════════════════════════════════════════════════════════
#  TOOL REGISTRY
# ════════════════════════════════════════════════════════════════════════════
@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # ── EYES ────────────────────────────────────────────────────────────
        types.Tool(
            name="camera_snapshot",
            description="Capture a single frame from the webcam. Returns base64 JPEG.",
            inputSchema={"type": "object", "properties": {
                "device": {"type": "integer", "default": 0,
                           "description": "Camera device index (0 = default)"},
                "width":  {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 720},
            }},
        ),
        types.Tool(
            name="camera_stream_start",
            description="Start continuous camera capture. Frames available via camera_get_frame.",
            inputSchema={"type": "object", "properties": {
                "device": {"type": "integer", "default": 0},
                "fps":    {"type": "integer", "default": 10},
            }},
        ),
        types.Tool(
            name="camera_get_frame",
            description="Get the latest frame from the running stream as base64 JPEG.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="camera_stream_stop",
            description="Stop the camera stream.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="screen_capture",
            description="Take a screenshot of the display. Returns base64 PNG.",
            inputSchema={"type": "object", "properties": {
                "display": {"type": "integer", "default": 0},
            }},
        ),

        # ── EARS ────────────────────────────────────────────────────────────
        types.Tool(
            name="audio_record",
            description="Record audio from the microphone for N seconds. Returns base64 WAV.",
            inputSchema={"type": "object", "required": ["seconds"], "properties": {
                "seconds":     {"type": "number"},
                "sample_rate": {"type": "integer", "default": 44100},
                "channels":    {"type": "integer", "default": 1},
            }},
        ),
        types.Tool(
            name="audio_stream_start",
            description="Start continuous microphone capture.",
            inputSchema={"type": "object", "properties": {
                "sample_rate": {"type": "integer", "default": 16000},
            }},
        ),
        types.Tool(
            name="audio_stream_stop",
            description="Stop the microphone stream.",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── MOUTH ───────────────────────────────────────────────────────────
        types.Tool(
            name="speak",
            description="Speak text aloud via TTS (pyttsx3 / espeak).",
            inputSchema={"type": "object", "required": ["text"], "properties": {
                "text":  {"type": "string"},
                "rate":  {"type": "integer", "default": 175,
                          "description": "Words per minute"},
                "voice": {"type": "string", "default": "",
                          "description": "Voice name or empty for default"},
            }},
        ),
        types.Tool(
            name="audio_play",
            description="Play a local audio file.",
            inputSchema={"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string"},
            }},
        ),

        # ── HANDS (filesystem) ──────────────────────────────────────────────
        types.Tool(
            name="fs_read",
            description="Read a file. Returns text or base64 for binary.",
            inputSchema={"type": "object", "required": ["path"], "properties": {
                "path":   {"type": "string"},
                "binary": {"type": "boolean", "default": False},
                "offset": {"type": "integer", "default": 0},
                "length": {"type": "integer", "default": -1},
            }},
        ),
        types.Tool(
            name="fs_write",
            description="Write content to a file.",
            inputSchema={"type": "object", "required": ["path", "content"], "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
                "binary":  {"type": "boolean", "default": False,
                            "description": "If true, content is base64"},
                "append":  {"type": "boolean", "default": False},
            }},
        ),
        types.Tool(
            name="fs_list",
            description="List directory contents.",
            inputSchema={"type": "object", "required": ["path"], "properties": {
                "path":      {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
            }},
        ),
        types.Tool(
            name="fs_delete",
            description="Delete a file or directory.",
            inputSchema={"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
            }},
        ),
        types.Tool(
            name="fs_move",
            description="Move / rename a file or directory.",
            inputSchema={"type": "object", "required": ["src", "dst"], "properties": {
                "src": {"type": "string"},
                "dst": {"type": "string"},
            }},
        ),
        types.Tool(
            name="fs_copy",
            description="Copy a file or directory.",
            inputSchema={"type": "object", "required": ["src", "dst"], "properties": {
                "src": {"type": "string"},
                "dst": {"type": "string"},
            }},
        ),

        # ── HANDS (shell / process) ─────────────────────────────────────────
        types.Tool(
            name="shell_exec",
            description="Execute a shell command. Full root access.",
            inputSchema={"type": "object", "required": ["cmd"], "properties": {
                "cmd":     {"type": "string"},
                "cwd":     {"type": "string", "default": "/"},
                "timeout": {"type": "integer", "default": 30},
                "env":     {"type": "object", "default": {}},
            }},
        ),
        types.Tool(
            name="process_list",
            description="List running processes.",
            inputSchema={"type": "object", "properties": {
                "filter": {"type": "string", "default": ""},
            }},
        ),
        types.Tool(
            name="process_kill",
            description="Kill a process by PID or name.",
            inputSchema={"type": "object", "properties": {
                "pid":  {"type": "integer"},
                "name": {"type": "string"},
                "sig":  {"type": "integer", "default": 15},
            }},
        ),
        types.Tool(
            name="process_spawn",
            description="Spawn a process in the background.",
            inputSchema={"type": "object", "required": ["cmd"], "properties": {
                "cmd":  {"type": "array", "items": {"type": "string"}},
                "cwd":  {"type": "string", "default": "/"},
                "env":  {"type": "object", "default": {}},
            }},
        ),

        # ── HARDWARE / KERNEL ───────────────────────────────────────────────
        types.Tool(
            name="hw_info",
            description="Return full hardware info: CPU, RAM, disks, GPU, network interfaces.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="mem_read",
            description="Read physical memory at address (requires root + /dev/mem). Returns hex.",
            inputSchema={"type": "object", "required": ["address", "length"], "properties": {
                "address": {"type": "integer"},
                "length":  {"type": "integer"},
            }},
        ),
        types.Tool(
            name="mem_write",
            description="Write bytes to physical memory address (requires root + /dev/mem).",
            inputSchema={"type": "object", "required": ["address", "data"], "properties": {
                "address": {"type": "integer"},
                "data":    {"type": "string", "description": "Hex string of bytes"},
            }},
        ),
        types.Tool(
            name="kernel_module_load",
            description="Load a kernel module (insmod/modprobe).",
            inputSchema={"type": "object", "required": ["module"], "properties": {
                "module": {"type": "string"},
                "params": {"type": "string", "default": ""},
            }},
        ),
        types.Tool(
            name="kernel_module_unload",
            description="Unload a kernel module (rmmod).",
            inputSchema={"type": "object", "required": ["module"], "properties": {
                "module": {"type": "string"},
            }},
        ),
        types.Tool(
            name="kernel_sysctl",
            description="Read or write a sysctl value.",
            inputSchema={"type": "object", "required": ["key"], "properties": {
                "key":   {"type": "string"},
                "value": {"type": "string", "default": ""},
            }},
        ),
        types.Tool(
            name="gpio_write",
            description="Write a value to a GPIO pin (Linux sysfs or /dev/gpiomem).",
            inputSchema={"type": "object", "required": ["pin", "value"], "properties": {
                "pin":   {"type": "integer"},
                "value": {"type": "integer", "description": "0 or 1"},
            }},
        ),
        types.Tool(
            name="gpio_read",
            description="Read a GPIO pin value.",
            inputSchema={"type": "object", "required": ["pin"], "properties": {
                "pin": {"type": "integer"},
            }},
        ),
        types.Tool(
            name="usb_list",
            description="List connected USB devices.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="i2c_scan",
            description="Scan an I2C bus for devices.",
            inputSchema={"type": "object", "properties": {
                "bus": {"type": "integer", "default": 1},
            }},
        ),

        # ── NETWORK ─────────────────────────────────────────────────────────
        types.Tool(
            name="net_interfaces",
            description="List network interfaces with IPs.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="net_scan",
            description="Port-scan a host.",
            inputSchema={"type": "object", "required": ["host"], "properties": {
                "host":       {"type": "string"},
                "ports":      {"type": "string", "default": "1-1024"},
                "timeout_ms": {"type": "integer", "default": 300},
            }},
        ),
        types.Tool(
            name="net_packet_send",
            description="Send a raw UDP/TCP packet.",
            inputSchema={"type": "object", "required": ["host", "port", "data"], "properties": {
                "host":     {"type": "string"},
                "port":     {"type": "integer"},
                "data":     {"type": "string", "description": "Hex or plain string"},
                "protocol": {"type": "string", "default": "udp"},
            }},
        ),

        # ── TUNNEL ──────────────────────────────────────────────────────────
        types.Tool(
            name="tunnel_start",
            description="Start a tunnel to expose local port. Supports cloudflare (quick, no account) or ngrok (persistent with token).",
            inputSchema={"type": "object", "required": ["port"], "properties": {
                "port":      {"type": "integer"},
                "provider":  {"type": "string", "default": "cloudflare",
                              "description": "cloudflare | ngrok"},
                "ngrok_token":    {"type": "string", "default": ""},
                "ngrok_domain":   {"type": "string", "default": "",
                                   "description": "Custom ngrok domain (paid)"},
                "cf_token":       {"type": "string", "default": "",
                                   "description": "Cloudflare tunnel token for persistent named tunnel"},
            }},
        ),
        types.Tool(
            name="tunnel_status",
            description="Get current tunnel URL and status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="tunnel_stop",
            description="Stop the running tunnel.",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── SYSTEM ──────────────────────────────────────────────────────────
        types.Tool(
            name="sys_info",
            description="Return OS, uptime, load, memory, disk usage.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="sys_reboot",
            description="Reboot the system.",
            inputSchema={"type": "object", "properties": {
                "delay_seconds": {"type": "integer", "default": 0},
            }},
        ),
        types.Tool(
            name="sys_shutdown",
            description="Shutdown the system.",
            inputSchema={"type": "object", "properties": {
                "delay_seconds": {"type": "integer", "default": 0},
            }},
        ),
        types.Tool(
            name="clipboard_get",
            description="Read the clipboard contents.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="clipboard_set",
            description="Write text to the clipboard.",
            inputSchema={"type": "object", "required": ["text"], "properties": {
                "text": {"type": "string"},
            }},
        ),
    ]


# ════════════════════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS
# ════════════════════════════════════════════════════════════════════════════

def _ok(data: Any) -> list[types.TextContent]:
    if not isinstance(data, str):
        data = json.dumps(data, default=str, indent=2)
    return [types.TextContent(type="text", text=data)]

def _err(msg: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"ERROR: {msg}")]


# ── lazy imports ─────────────────────────────────────────────────────────────
def _cv2():
    global cv2
    if cv2 is None:
        try:
            import cv2 as _c; cv2 = _c
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "opencv-python-headless", "-q",
                                   "--break-system-packages"])
            import cv2 as _c; cv2 = _c
    return cv2

def _psutil():
    global psutil
    if psutil is None:
        try:
            import psutil as _p; psutil = _p
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "psutil", "-q", "--break-system-packages"])
            import psutil as _p; psutil = _p
    return psutil

def _pyaudio():
    global pyaudio
    if pyaudio is None:
        try:
            import pyaudio as _a; pyaudio = _a
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "pyaudio", "-q", "--break-system-packages"])
            import pyaudio as _a; pyaudio = _a
    return pyaudio

def _pyttsx3():
    global pyttsx3
    if pyttsx3 is None:
        try:
            import pyttsx3 as _t; pyttsx3 = _t
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "pyttsx3", "-q", "--break-system-packages"])
            import pyttsx3 as _t; pyttsx3 = _t
    return pyttsx3


# ── CAMERA ───────────────────────────────────────────────────────────────────
def _grab_frame(device: int, width: int, height: int) -> bytes:
    cv = _cv2()
    cap = cv.VideoCapture(device)
    cap.set(cv.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, height)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("Could not read frame from camera")
    _, buf = cv.imencode(".jpg", frame, [cv.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()

def _stream_loop(device: int, fps: int):
    cv = _cv2()
    cap = cv.VideoCapture(device)
    delay = 1.0 / fps
    while _camera_stream.get("running"):
        ok, frame = cap.read()
        if ok:
            _, buf = cv.imencode(".jpg", frame, [cv.IMWRITE_JPEG_QUALITY, 70])
            _camera_stream["frame"] = buf.tobytes()
        time.sleep(delay)
    cap.release()


# ── AUDIO ────────────────────────────────────────────────────────────────────
def _record_audio(seconds: float, sample_rate: int, channels: int) -> bytes:
    import wave, io
    pa = _pyaudio()
    p = pa.PyAudio()
    stream = p.open(format=pa.paInt16, channels=channels,
                    rate=sample_rate, input=True,
                    frames_per_buffer=1024)
    frames = []
    for _ in range(int(sample_rate / 1024 * seconds)):
        frames.append(stream.read(1024))
    stream.stop_stream(); stream.close(); p.terminate()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


# ── TTS ───────────────────────────────────────────────────────────────────────
def _speak(text: str, rate: int, voice: str):
    # Try pyttsx3 first, fall back to espeak
    try:
        tts = _pyttsx3().init()
        tts.setProperty("rate", rate)
        if voice:
            for v in tts.getProperty("voices"):
                if voice.lower() in v.name.lower():
                    tts.setProperty("voice", v.id)
                    break
        tts.say(text)
        tts.runAndWait()
    except Exception:
        subprocess.run(["espeak", f"-s{rate}", text], check=False)


# ── TUNNEL ───────────────────────────────────────────────────────────────────
def _ensure_cloudflared():
    if shutil.which("cloudflared"):
        return
    arch = platform.machine()
    url = (f"https://github.com/cloudflare/cloudflared/releases/latest/download/"
           f"cloudflared-linux-{'arm64' if 'arm' in arch.lower() or 'aarch' in arch.lower() else 'amd64'}")
    subprocess.check_call(["curl", "-fsSL", url, "-o", "/usr/local/bin/cloudflared"])
    os.chmod("/usr/local/bin/cloudflared", 0o755)

def _ensure_ngrok():
    if shutil.which("ngrok"):
        return
    arch = platform.machine()
    pkg = f"ngrok-v3-stable-linux-{'arm64' if 'arm' in arch.lower() else 'amd64'}.tgz"
    url = f"https://bin.equinox.io/c/bNyj1mQVY4c/{pkg}"
    tmp = tempfile.mkdtemp()
    subprocess.check_call(["curl", "-fsSL", url, "-o", f"{tmp}/ngrok.tgz"])
    subprocess.check_call(["tar", "-xzf", f"{tmp}/ngrok.tgz", "-C", "/usr/local/bin"])
    os.chmod("/usr/local/bin/ngrok", 0o755)

def _start_cloudflare_tunnel(port: int, cf_token: str) -> str:
    global _tunnel_proc, _tunnel_url
    _ensure_cloudflared()
    if cf_token:
        # Named persistent tunnel via token
        cmd = ["cloudflared", "tunnel", "--no-autoupdate", "run",
               "--token", cf_token]
    else:
        # Quick tunnel — no account needed
        cmd = ["cloudflared", "tunnel", "--no-autoupdate",
               "--url", f"http://localhost:{port}"]
    _tunnel_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
    # Read until we see the URL
    for line in _tunnel_proc.stdout:
        m = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
        if m:
            _tunnel_url = m.group(0)
            break
        # Named tunnel URL pattern
        m2 = re.search(r"https://\S+\.cfargotunnel\.com", line)
        if m2:
            _tunnel_url = m2.group(0)
            break
        # Catch any https URL
        m3 = re.search(r"(https://[^\s]+)", line)
        if m3 and "trycloudflare" in m3.group(1) or "cfargotunnel" in m3.group(1):
            _tunnel_url = m3.group(1)
            break
    return _tunnel_url

def _start_ngrok_tunnel(port: int, token: str, domain: str) -> str:
    global _tunnel_proc, _tunnel_url
    _ensure_ngrok()
    if token:
        subprocess.run(["ngrok", "config", "add-authtoken", token],
                       capture_output=True)
    cmd = ["ngrok", "http", str(port), "--log", "stdout"]
    if domain:
        cmd += ["--domain", domain]
    _tunnel_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
    # Poll ngrok's local API for URL
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
    return "Could not retrieve ngrok URL — check ngrok dashboard"


# ── PORT SCAN ────────────────────────────────────────────────────────────────
def _port_scan(host: str, ports_str: str, timeout_ms: int) -> dict:
    timeout = timeout_ms / 1000
    open_ports = []
    if "-" in ports_str:
        lo, hi = ports_str.split("-")
        port_range = range(int(lo), int(hi) + 1)
    else:
        port_range = [int(p) for p in ports_str.split(",")]

    def check(p):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        if s.connect_ex((host, p)) == 0:
            open_ports.append(p)
        s.close()

    threads = [threading.Thread(target=check, args=(p,)) for p in port_range]
    for t in threads: t.start()
    for t in threads: t.join()
    return {"host": host, "open_ports": sorted(open_ports)}


# ════════════════════════════════════════════════════════════════════════════
#  CALL HANDLER
# ════════════════════════════════════════════════════════════════════════════
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        return await _dispatch(name, arguments)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


async def _dispatch(name: str, args: dict) -> list[types.TextContent]:
    # ── EYES ─────────────────────────────────────────────────────────────────
    if name == "camera_snapshot":
        frame = await asyncio.get_event_loop().run_in_executor(
            None, _grab_frame,
            args.get("device", 0), args.get("width", 1280), args.get("height", 720))
        return _ok({"format": "jpeg", "base64": base64.b64encode(frame).decode()})

    if name == "camera_stream_start":
        if _camera_stream.get("running"):
            return _ok("Stream already running")
        _camera_stream.update({"running": True, "frame": None})
        t = threading.Thread(target=_stream_loop,
                             args=(args.get("device", 0), args.get("fps", 10)),
                             daemon=True)
        _camera_stream["thread"] = t
        t.start()
        return _ok("Camera stream started")

    if name == "camera_get_frame":
        frame = _camera_stream.get("frame")
        if frame is None:
            return _err("No frame yet — start stream first")
        return _ok({"format": "jpeg", "base64": base64.b64encode(frame).decode()})

    if name == "camera_stream_stop":
        _camera_stream["running"] = False
        return _ok("Camera stream stopped")

    if name == "screen_capture":
        # Try scrot, then import (ImageMagick), then xwd
        tmp = tempfile.mktemp(suffix=".png")
        for cmd in [["scrot", tmp], ["import", "-window", "root", tmp],
                    ["gnome-screenshot", "-f", tmp]]:
            if shutil.which(cmd[0]):
                subprocess.run(cmd, check=True)
                break
        else:
            return _err("No screenshot tool found (install scrot or imagemagick)")
        data = Path(tmp).read_bytes()
        os.unlink(tmp)
        return _ok({"format": "png", "base64": base64.b64encode(data).decode()})

    # ── EARS ──────────────────────────────────────────────────────────────────
    if name == "audio_record":
        wav = await asyncio.get_event_loop().run_in_executor(
            None, _record_audio,
            args["seconds"], args.get("sample_rate", 44100), args.get("channels", 1))
        return _ok({"format": "wav", "base64": base64.b64encode(wav).decode()})

    if name == "audio_stream_start":
        return _ok("Audio streaming: use audio_record with short intervals")

    if name == "audio_stream_stop":
        return _ok("Audio stream stopped")

    # ── MOUTH ─────────────────────────────────────────────────────────────────
    if name == "speak":
        await asyncio.get_event_loop().run_in_executor(
            None, _speak, args["text"], args.get("rate", 175), args.get("voice", ""))
        return _ok(f"Spoke: {args['text']!r}")

    if name == "audio_play":
        path = args["path"]
        for player in ["mpv", "aplay", "paplay", "ffplay"]:
            if shutil.which(player):
                subprocess.Popen([player, path])
                return _ok(f"Playing {path} with {player}")
        return _err("No audio player found")

    # ── FILESYSTEM ────────────────────────────────────────────────────────────
    if name == "fs_read":
        p = Path(args["path"])
        offset = args.get("offset", 0)
        length = args.get("length", -1)
        if args.get("binary"):
            data = p.read_bytes()
            if offset: data = data[offset:]
            if length > 0: data = data[:length]
            return _ok({"base64": base64.b64encode(data).decode()})
        text = p.read_text(errors="replace")
        lines = text.splitlines()
        if offset: lines = lines[offset:]
        if length > 0: lines = lines[:length]
        return _ok("\n".join(lines))

    if name == "fs_write":
        p = Path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        content = args["content"]
        if args.get("binary"):
            content = base64.b64decode(content)
            mode = "ab" if args.get("append") else "wb"
            with open(p, mode) as f: f.write(content)
        else:
            mode = "a" if args.get("append") else "w"
            with open(p, mode) as f: f.write(content)
        return _ok(f"Written: {p}")

    if name == "fs_list":
        p = Path(args["path"])
        if args.get("recursive"):
            entries = [str(x) for x in p.rglob("*")]
        else:
            entries = [str(x) for x in p.iterdir()]
        return _ok(entries)

    if name == "fs_delete":
        p = Path(args["path"])
        if args.get("recursive") and p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink() if p.is_file() else p.rmdir()
        return _ok(f"Deleted: {p}")

    if name == "fs_move":
        shutil.move(args["src"], args["dst"])
        return _ok(f"Moved: {args['src']} → {args['dst']}")

    if name == "fs_copy":
        src, dst = Path(args["src"]), Path(args["dst"])
        if src.is_dir(): shutil.copytree(src, dst)
        else: shutil.copy2(src, dst)
        return _ok(f"Copied: {src} → {dst}")

    # ── SHELL / PROCESS ───────────────────────────────────────────────────────
    if name == "shell_exec":
        env = {**os.environ, **args.get("env", {})}
        result = subprocess.run(
            args["cmd"], shell=True, capture_output=True, text=True,
            cwd=args.get("cwd", "/"), timeout=args.get("timeout", 30), env=env)
        return _ok({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        })

    if name == "process_list":
        ps = _psutil()
        procs = []
        flt = args.get("filter", "").lower()
        for p in ps.process_iter(["pid", "name", "status", "cpu_percent", "memory_percent"]):
            info = p.info
            if not flt or flt in info["name"].lower():
                procs.append(info)
        return _ok(procs)

    if name == "process_kill":
        ps = _psutil()
        sig = args.get("sig", 15)
        if "pid" in args:
            os.kill(args["pid"], sig)
            return _ok(f"Sent signal {sig} to PID {args['pid']}")
        name_ = args.get("name", "")
        killed = []
        for p in ps.process_iter(["pid", "name"]):
            if name_.lower() in p.info["name"].lower():
                os.kill(p.pid, sig)
                killed.append(p.pid)
        return _ok({"killed_pids": killed})

    if name == "process_spawn":
        p = subprocess.Popen(args["cmd"], cwd=args.get("cwd", "/"),
                             env={**os.environ, **args.get("env", {})},
                             start_new_session=True)
        return _ok({"pid": p.pid})

    # ── HARDWARE / KERNEL ─────────────────────────────────────────────────────
    if name == "hw_info":
        ps = _psutil()
        cpu = {
            "physical_cores": ps.cpu_count(logical=False),
            "logical_cores":  ps.cpu_count(logical=True),
            "freq_mhz":       ps.cpu_freq()._asdict() if ps.cpu_freq() else {},
            "usage_percent":  ps.cpu_percent(interval=0.5),
        }
        mem = ps.virtual_memory()._asdict()
        swap = ps.swap_memory()._asdict()
        disks = [d._asdict() for d in ps.disk_partitions()]
        net = {k: v._asdict() for k, v in ps.net_if_addrs().items()}
        # GPU via nvidia-smi if available
        gpu = {}
        if shutil.which("nvidia-smi"):
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,temperature.gpu",
                                 "--format=csv,noheader"], capture_output=True, text=True)
            gpu = {"nvidia_smi": r.stdout.strip()}
        return _ok({"cpu": cpu, "memory": mem, "swap": swap,
                    "disks": disks, "network_interfaces": net, "gpu": gpu})

    if name == "mem_read":
        addr = args["address"]
        length = args["length"]
        with open("/dev/mem", "rb") as f:
            f.seek(addr)
            data = f.read(length)
        return _ok({"address": hex(addr), "hex": data.hex(), "length": length})

    if name == "mem_write":
        addr = args["address"]
        data = bytes.fromhex(args["data"])
        with open("/dev/mem", "r+b") as f:
            f.seek(addr)
            f.write(data)
        return _ok(f"Wrote {len(data)} bytes to {hex(addr)}")

    if name == "kernel_module_load":
        cmd = f"modprobe {args['module']} {args.get('params','')}"
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return _ok({"stdout": r.stdout, "stderr": r.stderr, "rc": r.returncode})

    if name == "kernel_module_unload":
        r = subprocess.run(["rmmod", args["module"]], capture_output=True, text=True)
        return _ok({"stdout": r.stdout, "stderr": r.stderr, "rc": r.returncode})

    if name == "kernel_sysctl":
        key = args["key"]
        value = args.get("value", "")
        if value:
            r = subprocess.run(["sysctl", "-w", f"{key}={value}"],
                               capture_output=True, text=True)
        else:
            r = subprocess.run(["sysctl", key], capture_output=True, text=True)
        return _ok({"stdout": r.stdout, "stderr": r.stderr})

    if name == "gpio_write":
        pin, val = args["pin"], args["value"]
        gpio_path = Path(f"/sys/class/gpio/gpio{pin}")
        if not gpio_path.exists():
            Path("/sys/class/gpio/export").write_text(str(pin))
            (gpio_path / "direction").write_text("out")
        (gpio_path / "value").write_text(str(val))
        return _ok(f"GPIO{pin} = {val}")

    if name == "gpio_read":
        pin = args["pin"]
        gpio_path = Path(f"/sys/class/gpio/gpio{pin}")
        if not gpio_path.exists():
            Path("/sys/class/gpio/export").write_text(str(pin))
            (gpio_path / "direction").write_text("in")
        val = (gpio_path / "value").read_text().strip()
        return _ok({"pin": pin, "value": int(val)})

    if name == "usb_list":
        r = subprocess.run(["lsusb"], capture_output=True, text=True)
        return _ok(r.stdout)

    if name == "i2c_scan":
        bus = args.get("bus", 1)
        r = subprocess.run(["i2cdetect", "-y", str(bus)], capture_output=True, text=True)
        return _ok(r.stdout)

    # ── NETWORK ───────────────────────────────────────────────────────────────
    if name == "net_interfaces":
        ps = _psutil()
        addrs = {k: [a._asdict() for a in v]
                 for k, v in ps.net_if_addrs().items()}
        stats = {k: v._asdict() for k, v in ps.net_if_stats().items()}
        return _ok({"addresses": addrs, "stats": stats})

    if name == "net_scan":
        result = await asyncio.get_event_loop().run_in_executor(
            None, _port_scan,
            args["host"], args.get("ports", "1-1024"), args.get("timeout_ms", 300))
        return _ok(result)

    if name == "net_packet_send":
        data_str = args["data"]
        try:
            data = bytes.fromhex(data_str)
        except ValueError:
            data = data_str.encode()
        proto = args.get("protocol", "udp").lower()
        sock_type = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
        s = socket.socket(socket.AF_INET, sock_type)
        if proto == "tcp":
            s.connect((args["host"], args["port"]))
        s.sendto(data, (args["host"], args["port"]))
        s.close()
        return _ok(f"Sent {len(data)} bytes via {proto.upper()} to {args['host']}:{args['port']}")

    # ── TUNNEL ────────────────────────────────────────────────────────────────
    if name == "tunnel_start":
        global _tunnel_url
        port     = args["port"]
        provider = args.get("provider", "cloudflare").lower()
        if provider == "ngrok":
            url = await asyncio.get_event_loop().run_in_executor(
                None, _start_ngrok_tunnel,
                port, args.get("ngrok_token", ""), args.get("ngrok_domain", ""))
        else:
            url = await asyncio.get_event_loop().run_in_executor(
                None, _start_cloudflare_tunnel,
                port, args.get("cf_token", ""))
        return _ok({"provider": provider, "url": url, "port": port})

    if name == "tunnel_status":
        if _tunnel_proc and _tunnel_proc.poll() is None:
            return _ok({"running": True, "url": _tunnel_url})
        return _ok({"running": False, "url": ""})

    if name == "tunnel_stop":
        global _tunnel_proc
        if _tunnel_proc:
            _tunnel_proc.terminate()
            _tunnel_proc = None
            _tunnel_url = ""
        return _ok("Tunnel stopped")

    # ── SYSTEM ────────────────────────────────────────────────────────────────
    if name == "sys_info":
        ps = _psutil()
        boot = datetime.fromtimestamp(ps.boot_time())
        uptime = datetime.now() - boot
        return _ok({
            "os":        platform.platform(),
            "hostname":  socket.gethostname(),
            "boot_time": str(boot),
            "uptime":    str(uptime),
            "load_avg":  os.getloadavg(),
            "memory":    ps.virtual_memory()._asdict(),
            "disk":      ps.disk_usage("/")._asdict(),
            "cpu_count": ps.cpu_count(),
        })

    if name == "sys_reboot":
        delay = args.get("delay_seconds", 0)
        subprocess.Popen(f"sleep {delay} && reboot", shell=True)
        return _ok(f"Rebooting in {delay}s")

    if name == "sys_shutdown":
        delay = args.get("delay_seconds", 0)
        subprocess.Popen(f"sleep {delay} && shutdown -h now", shell=True)
        return _ok(f"Shutting down in {delay}s")

    if name == "clipboard_get":
        for cmd in [["xclip", "-selection", "clipboard", "-o"],
                    ["xsel", "--clipboard", "--output"],
                    ["pbpaste"]]:
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True)
                return _ok(r.stdout)
        return _err("No clipboard tool found (install xclip or xsel)")

    if name == "clipboard_set":
        text = args["text"]
        for cmd, inp in [
            (["xclip", "-selection", "clipboard"], text),
            (["xsel", "--clipboard", "--input"], text),
            (["pbcopy"], text),
        ]:
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=inp, text=True)
                return _ok("Clipboard set")
        return _err("No clipboard tool found")

    return _err(f"Unknown tool: {name}")


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════
async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
