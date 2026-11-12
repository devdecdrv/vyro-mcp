# vyro-mcp

Full root/kernel/hardware MCP server. Gives any AI connected via MCP:

| Sense | What it can do |
|-------|---------------|
| 👁 Eyes | Webcam snapshot, live stream, screen capture |
| 👂 Ears | Microphone record (WAV), continuous audio stream |
| 🗣 Mouth | Text-to-speech (pyttsx3 / espeak), play audio files |
| 🤲 Hands | Full filesystem (read/write/delete/move/copy), shell exec, process control |
| 🧠 Brain | Hardware info, kernel module load/unload, sysctl, /dev/mem R/W |
| 🔌 Hardware | GPIO read/write, USB list, I2C scan |
| 🌐 Network | Interface info, port scanner, raw packet sender |
| 🚇 Tunnel | Cloudflare (instant, no account) + ngrok (persistent with token) |

---

## Install

```bash
# 1. Clone / copy this folder to your machine
# 2. Install deps
sudo bash install.sh

# 3. Test run
sudo python3 server.py
```

---

## Connect to Claude Desktop

Edit your Claude Desktop config (`claude_desktop_config.json`):

**Linux:** `~/.config/Claude/claude_desktop_config.json`  
**Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`  
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add this block (replace the path):

```json
{
  "mcpServers": {
    "vyro-mcp": {
      "command": "sudo",
      "args": ["python3", "/home/youruser/vyro-mcp/server.py"],
      "env": { "DISPLAY": ":0" }
    }
  }
}
```

Restart Claude Desktop. You'll see `vyro-mcp` appear in tools.

---

## Tunnel setup (persistent public URL)

### Cloudflare — no account needed (temporary URL)
Ask Claude: *"start tunnel on port 8080"*  
You get a `*.trycloudflare.com` URL instantly.

### Cloudflare — permanent URL (needs free account)
1. Sign up at cloudflare.com → Zero Trust → Tunnels → Create tunnel
2. Copy the tunnel token
3. Ask Claude: *"start tunnel on port 8080 with cloudflare token YOUR_TOKEN"*  
   Same URL every time.

### ngrok — permanent URL (needs free account)
1. Sign up at ngrok.com → copy your authtoken
2. Optionally create a static domain (free tier gives one)
3. Ask Claude: *"start ngrok tunnel on port 8080 with token YOUR_TOKEN domain your-domain.ngrok-free.app"*

---

## Tools available

```
camera_snapshot       camera_stream_start   camera_get_frame
camera_stream_stop    screen_capture
audio_record          audio_stream_start    audio_stream_stop
speak                 audio_play
fs_read               fs_write              fs_list
fs_delete             fs_move               fs_copy
shell_exec            process_list          process_kill
process_spawn
hw_info               mem_read              mem_write
kernel_module_load    kernel_module_unload  kernel_sysctl
gpio_write            gpio_read             usb_list          i2c_scan
net_interfaces        net_scan              net_packet_send
tunnel_start          tunnel_status         tunnel_stop
sys_info              sys_reboot            sys_shutdown
clipboard_get         clipboard_set
```

---

## Security note
This server runs as root and exposes everything. Use only on machines you own.
The tunnel is HTTPS encrypted but anyone with the URL can send tool calls.
Add token auth in front if you're exposing to the internet long-term.
