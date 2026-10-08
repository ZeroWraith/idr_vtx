# Raspberry Pi 5 RTMP Streamer

Captures video from a USB capture card, hardware-encodes H.264 via the Pi 5 VPU (v4l2h264enc), and pushes an RTMP stream to a MediaMTX server. Viewable in VLC.

## Quick start (on the Pi 5)

```bash
# 1. System packages (run once)
sudo apt update && sudo apt install -y \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  libgstreamer1.0-dev python3-gst-1.0 python3-yaml v4l-utils

# 2. Clone / copy this folder to the Pi
#    (e.g. ~/pi-streamer)

# 3. Install Python deps
pip3 install -r requirements.txt

# 4. Edit config.yaml
#    - Set rtmp.host to the IP of the machine running MediaMTX
#    - Adjust stream_key if needed
#    - Optionally set capture.device to a specific /dev/videoX

# 5. Run
python3 streamer.py
```

## MediaMTX (receiver side)

On the receiving machine (where you’ll watch with VLC):

```bash
# Download latest release for your arch
wget https://github.com/bluenviron/mediamtx/releases/latest/download/mediamtx_v1.8.4_linux_arm64v8.tar.gz
tar -xzf mediamtx_*.tar.gz
./mediamtx   # listens on rtmp://0.0.0.0:1935 by default
```

## Watching the stream (VLC)

```
Media → Open Network Stream → rtmp://<receiver-ip>/live/streamkey
```

Replace `<receiver-ip>` with the IP of the MediaMTX host (or `localhost` if running on same machine).

## Configuration reference (`config.yaml`)

| Section | Key | Meaning |
|---------|-----|---------|
| `rtmp` | `host` | MediaMTX host/IP |
| | `port` | RTMP port (default 1935) |
| | `app` | RTMP application name (default `live`) |
| | `stream_key` | Stream key (must match VLC URL) |
| `capture` | `device` | Specific `/dev/videoX` or empty for auto-detect |
| | `width` / `height` | Output resolution (720p = 1280×720) |
| | `framerate` | FPS (30) |
| | `format` | Pixel format from capture card (NV12, YUYV, etc.) |
| `encoder` | `bitrate` | Target bitrate in bits/sec (4 000 000 = 4 Mbps) |
| | `profile` | H.264 profile: `baseline`, `main`, `high` |
| | `level` | H.264 level string (`3.1`, `4.0`, …) |
| | `gop_size` | Keyframe interval (frames) |
| | `io_mode` | `dmabuf-import` for zero‑copy on Pi 5 |
| `streamer` | `reconnect_delay` | Seconds between reconnect attempts |
| | `log_level` | Python logging level |

## How it works

```
USB capture card → /dev/videoX (V4L2) → v4l2src (dmabuf) → v4l2h264enc (Pi 5 VPU)
 → h264parse → flvmux → rtmpsink → MediaMTX → VLC / any RTMP client
```

* Zero‑copy DMA buffers (`io-mode=dmabuf-import`) keep CPU usage low.
* `v4l2h264enc` uses the Pi 5’s hardware video encoder (no VAAPI/NVENC needed).
* The script auto‑detects the capture device, rebuilds the pipeline on error, and reconnects automatically.

## Systemd service (optional, auto-start on boot)

Create `/etc/systemd/system/pi-streamer.service`:

```ini
[Unit]
Description=Pi 5 RTMP Streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pi-streamer
ExecStart=/usr/bin/python3 /home/pi/pi-streamer/streamer.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pi-streamer
```

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `v4l2-ctl` not found | `sudo apt install v4l-utils` |
| No `/dev/videoX` | `ls /dev/video*` – ensure capture card is plugged in and kernel module loaded (`uvcvideo`) |
| Pipeline fails with “negotiation” | Verify capture card supports 1280×720@30 NV12; run `v4l2-ctl -d /dev/videoX --list-formats-ext` |
| RTMP connection refused | MediaMTX running? Correct IP/port? Firewall open on 1935? |
| High CPU / dropped frames | Ensure `io-mode=dmabuf-import` and `format=NV12`; lower bitrate or resolution |

## License

MIT – do whatever you want.