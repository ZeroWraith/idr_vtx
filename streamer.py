#!/usr/bin/env python3
"""
Raspberry Pi 5 RTMP Streamer
Captures from USB capture card via V4L2, hardware encodes with v4l2h264enc,
streams to MediaMTX RTMP server.
"""

import sys
import os
import signal
import time
import logging
import subprocess
import yaml
from pathlib import Path

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GObject', '2.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GObject, GLib

# ----------------------------------------------------------------------
# Configuration loading
# ----------------------------------------------------------------------
CONFIG_PATH = Path(__file__).with_name('config.yaml')

def load_config():
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f)

config = load_config()

# ----------------------------------------------------------------------
# Logging setup
# ----------------------------------------------------------------------
log_level = getattr(logging, config['streamer'].get('log_level', 'INFO').upper())
logging.basicConfig(
    level=log_level,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('pi_streamer')

# ----------------------------------------------------------------------
# V4L2 device detection
# ----------------------------------------------------------------------
def list_video_devices():
    """Return list of (device_path, friendly_name) for video capture devices."""
    try:
        out = subprocess.check_output(['v4l2-ctl', '--list-devices'], text=True)
    except FileNotFoundError:
        log.error('v4l2-ctl not found. Install v4l-utils.')
        return []
    devices = []
    lines = out.strip().split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line and not line.startswith('/dev/video'):
            name = line
            i += 1
            while i < len(lines) and lines[i].strip().startswith('/dev/video'):
                dev = lines[i].strip()
                devices.append((dev, name))
                i += 1
        else:
            i += 1
    return devices

def get_device_formats(device):
    """Return supported formats for a device as list of dicts."""
    try:
        out = subprocess.check_output(['v4l2-ctl', '-d', device, '--list-formats-ext'], text=True)
    except subprocess.CalledProcessError:
        return []
    # Simple parse: we just need to know if desired resolution exists.
    return out

def get_device_modes(device):
    """Return list of (width, height, fps, fmt) supported by device."""
    try:
        out = subprocess.check_output(['v4l2-ctl', '-d', device, '--list-formats-ext'], text=True)
    except subprocess.CalledProcessError:
        return []
    modes = []
    lines = out.splitlines()
    current_fmt = None
    current_size = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[') and ']:' in stripped:
            parts = stripped.split("'")
            if len(parts) >= 2:
                current_fmt = parts[1]
            current_size = None
        elif stripped.startswith('Size:') and current_fmt:
            # e.g. Size: Discrete 1280x720
            import re
            m = re.search(r'(\d+)x(\d+)', stripped)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                current_size = (w, h)
        elif stripped.startswith('Interval:') and current_fmt and current_size:
            # e.g. Interval: Discrete 0.033s (30.000 fps)
            import re
            m = re.search(r'\(([\d.]+)\s*fps\)', stripped)
            if m:
                fps = float(m.group(1))
                w, h = current_size
                modes.append((w, h, fps, current_fmt))
    return modes


def pick_capture_device(cfg):
    """Select capture device per config or auto-detect first suitable.
    Returns (device_path, width, height, fps, format_string) or (None, None, None, None, None)."""
    desired = cfg['capture'].get('device')
    cap = cfg['capture']
    target_w = cap['width']
    target_h = cap['height']
    target_fps = cap['framerate']
    preferred_formats = cap.get('preferred_formats', ['MJPG','NV12','YUYV','UYVY'])
    
    def choose_mode(modes):
        # First try exact match
        for w,h,fps,fmt in modes:
            if w == target_w and h == target_h and abs(fps - target_fps) < 0.5 and fmt in preferred_formats:
                return w,h,fps,fmt
        # Then same resolution, any fps
        for w,h,fps,fmt in modes:
            if w == target_w and h == target_h and fmt in preferred_formats:
                return w,h,fps,fmt
        # Then any preferred format
        for w,h,fps,fmt in modes:
            if fmt in preferred_formats:
                return w,h,fps,fmt
        # Fallback first mode
        if modes:
            return modes[0]
        return None
    
    if desired:
        if os.path.exists(desired):
            modes = get_device_modes(desired)
            choice = choose_mode(modes)
            if choice:
                w,h,fps,fmt = choice
                log.info(f'Using configured device: {desired} with {w}x{h}@{fps}fps {fmt}')
                return desired, w, h, fps, fmt
            else:
                log.error(f'Configured device {desired} has no suitable mode')
                return None, None, None, None, None
        else:
            log.warning(f'Configured device {desired} not found, falling back to auto-detect')
    devices = list_video_devices()
    if not devices:
        log.error('No video capture devices found')
        return None, None, None, None, None
    for dev, name in devices:
        log.info(f'Found capture device: {dev} ({name})')
        modes = get_device_modes(dev)
        choice = choose_mode(modes)
        if choice:
            w,h,fps,fmt = choice
            log.info(f'Selected device {dev} with {w}x{h}@{fps}fps {fmt}')
            return dev, w, h, fps, fmt
        else:
            log.warning(f'Device {dev} does not have usable modes')
    return None, None, None, None, None

# ----------------------------------------------------------------------
# GStreamer pipeline construction
# ----------------------------------------------------------------------
def build_pipeline(cfg, device, width, height, fps, fmt):
    enc = cfg['encoder']
    rtmp = cfg['rtmp']

    bitrate = enc['bitrate']
    profile_map = {'baseline': 1, 'main': 2, 'high': 4}
    profile = profile_map.get(enc.get('profile', 'baseline').lower(), 1)
    level_map = {'3.1': 13, '4.0': 20, '4.1': 21}
    level = level_map.get(str(enc.get('level', '3.1')), 13)
    gop = enc.get('gop_size', fps)
    io_mode = enc.get('io_mode', '')

    rtmp_url = f"rtmp://{rtmp['host']}:{rtmp['port']}/{rtmp['app']}/{rtmp['stream_key']}"

    src = f"v4l2src device={device}"
    if io_mode:
        src += f" io-mode={io_mode}"

    if fmt.upper() in ('MJPG', 'JPEG'):
        pipeline_str = (
            f"{src} ! "
            f"image/jpeg,width={width},height={height},framerate={int(fps)}/1 ! "
            f"jpegdec ! "
            f"videoconvert ! "
            f"x264enc tune=zerolatency bitrate={bitrate//1000} speed-preset=veryfast key-int-max={gop} ! "
            f"h264parse config-interval=1 ! "
            f"flvmux streamable=true ! "
            f"rtmpsink location=\"{rtmp_url} live=1\" sync=false"
        )
    else:
        pipeline_str = (
            f"{src} ! "
            f"video/x-raw,width={width},height={height},framerate={int(fps)}/1,format={fmt} ! "
            f"videoconvert ! "
            f"x264enc tune=zerolatency bitrate={bitrate//1000} speed-preset=veryfast key-int-max={gop} ! "
            f"h264parse config-interval=1 ! "
            f"flvmux streamable=true ! "
            f"rtmpsink location=\"{rtmp_url} live=1\" sync=false"
        )
    log.debug(f'Pipeline: {pipeline_str}')
    return pipeline_str

# ----------------------------------------------------------------------
# Streamer class handling pipeline lifecycle & reconnection
# ----------------------------------------------------------------------
class Streamer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.device = None
        self.pipeline = None
        self.loop = GLib.MainLoop()
        self.reconnect_delay = cfg['streamer'].get('reconnect_delay', 5)
        self.running = False

    def start(self):
        self.running = True
        self._setup_device()
        self._run_loop()

    def _setup_device(self):
        while self.running:
            self.device, self.width, self.height, self.fps, self.fmt = pick_capture_device(self.cfg)
            if self.device:
                break
            log.warning('No capture device found, retrying in 5s...')
            time.sleep(5)

    def _build_and_run_pipeline(self):
        pipe_str = build_pipeline(self.cfg, self.device, self.width, self.height, self.fps, self.fmt)
        self.pipeline = Gst.parse_launch(pipe_str)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_bus_message)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error('Failed to start pipeline')
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            return False
        log.info('Streaming started')
        return True

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error(f'GStreamer error: {err} - {debug}')
            self._schedule_reconnect()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            log.warning(f'GStreamer warning: {warn} - {debug}')
        elif t == Gst.MessageType.EOS:
            log.info('End of stream')
            self._schedule_reconnect()
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old, new, _ = message.parse_state_changed()
                log.debug(f'State changed: {old.value_nick} -> {new.value_nick}')

    def _schedule_reconnect(self):
        if not self.running:
            return
        log.info(f'Reconnecting in {self.reconnect_delay}s...')
        self._cleanup_pipeline()
        GLib.timeout_add_seconds(self.reconnect_delay, self._reconnect_attempt)

    def _reconnect_attempt(self):
        if not self.running:
            return False
        # re-verify device still exists
        if not os.path.exists(self.device):
            log.warning('Capture device disappeared, re-detecting...')
            self._setup_device()
        if self._build_and_run_pipeline():
            return False  # stop timeout
        return True  # keep retrying

    def _cleanup_pipeline(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _run_loop(self):
        # initial pipeline
        if not self._build_and_run_pipeline():
            self._schedule_reconnect()
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        log.info('Stopping streamer...')
        self.running = False
        self._cleanup_pipeline()
        self.loop.quit()

# ----------------------------------------------------------------------
# Signal handling
# ----------------------------------------------------------------------
def signal_handler(signum, frame):
    log.info(f'Received signal {signum}, shutting down')
    streamer.stop()

# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
if __name__ == '__main__':
    Gst.init(None)
    GObject.threads_init()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    streamer = Streamer(config)
    streamer.start()