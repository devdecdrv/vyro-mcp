#!/usr/bin/env bash
# vyro-mcp installer — run as root for full kernel access
set -e

echo "=== vyro-mcp installer ==="

# Python deps
pip install mcp psutil opencv-python-headless pyaudio pyttsx3 \
    --break-system-packages -q 2>/dev/null || \
pip install mcp psutil opencv-python-headless pyaudio pyttsx3 -q

# System tools
if command -v apt &>/dev/null; then
    apt-get install -y -q espeak scrot xclip portaudio19-dev \
        i2c-tools lsusb usbutils 2>/dev/null || true
elif command -v pacman &>/dev/null; then
    pacman -S --noconfirm espeak scrot xclip portaudio 2>/dev/null || true
elif command -v dnf &>/dev/null; then
    dnf install -y espeak scrot xclip portaudio-devel 2>/dev/null || true
fi

echo "=== Done. Run: sudo python3 server.py ==="
