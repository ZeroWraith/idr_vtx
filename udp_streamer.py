#!/usr/bin/env python3
"""
Raspberry Pi 5 UDP Streamer
Captures from USB capture card via V4L2, hardware encodes with v4l2h264enc
(falls back to x264enc), streams RTP/UDP directly to VLC.
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

CONFIG_PATH = Path(__file__).with_name('config.yaml')

def load_config():
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f)

config = load_config()

log_level = getattr(logging, config['streamer'].get('log_level', 'INFO').upper())
logging.basicConfig(
    level=log_level,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('udp_streamer')

def list_video_devices():
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

def get_device_modes(device):
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
            import re
            m = re.search(r'(\d+)x(\d+)', stripped)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                current_size = (w, h)
        elif stripped.startswith('Interval:') and current_fmt and current_size:
            import re
            m = re.search(r'\(([\d.]+)\s*fps\)', stripped)
            if m:
                fps = float(m.group(1))
                w, h = current_size
                modes.append((w, h, fps, current_fmt))
    return modes

def pick_capture_device(cfg):
    desired = cfg['capture'].get('device')
    cap = cfg['capture']
    target_w = cap['width']
    target_h = cap['height']
    target_fps = cap['framerate']
    preferred_formats = cap.get('preferred_formats', ['MJPG', 'NV12', 'YUYV', 'UYVY'])

    def choose_mode(modes):
        for w, h, fps, fmt in modes:
            if w == target_w and h == target_h and abs(fps - target_fps) < 0.5 and fmt in preferred_formats:
                return w, h, fps, fmt
        for w, h, fps, fmt in modes:
            if w == target_w and h == target_h and fmt in preferred_formats:
                return w, h, fps, fmt
        for w, h, fps, fmt in modes:
            if fmt in preferred_formats:
                return w, h, fps, fmt
        if modes:
            return modes[0]
        return None

    if desired:
        if os.path.exists(desired):
            modes = get_device_modes(desired)
            choice = choose_mode(modes)
            if choice:
                w, h, fps, fmt = choice
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
            w, h, fps, fmt = choice
            log.info(f'Selected device {dev} with {w}x{h}@{fps}fps {fmt}')
            return dev, w, h, fps, fmt
        else:
            log.warning(f'Device {dev} does not have usable modes')
    return None, None, None, None, None

def build_pipeline(cfg, device, width, height, fps, fmt):
    enc_cfg = cfg['encoder']
    udp_cfg = cfg['udp']

    bitrate = enc_cfg['bitrate']
    profile_map = {'baseline': 1, 'main': 2, 'high': 4}
    profile = profile_map.get(enc_cfg.get('profile', 'baseline').lower(), 1)
    level_map = {'3.1': 13, '4.0': 20, '4.1': 21}
    level = level_map.get(str(enc_cfg.get('level', '3.1')), 13)
    gop = enc_cfg.get('gop_size', fps)
    io_mode = enc_cfg.get('io_mode', '')

    host = udp_cfg['host']
    port = udp_cfg['port']
    encoder_type = udp_cfg.get('encoder', 'hardware')

    src = f"v4l2src device={device}"
    if io_mode:
        src += f" io-mode={io_mode}"

    caps = f"video/x-raw,width={width},height={height},framerate={int(fps)}/1,format={fmt}"

    if encoder_type == 'hardware':
        log.info('Using hardware encoder (v4l2h264enc)')
        encoder_str = (
            f"v4l2h264enc extra-controls=\"controls,video_bitrate={bitrate},"
            f"h264_profile={profile},h264_level={level}\" ! "
            f"h264parse config-interval=1 ! "
        )
    else:
        log.info('Using software encoder (x264enc)')
        encoder_str = (
            f"x264enc tune=zerolatency bitrate={bitrate//1000} "
            f"speed-preset=veryfast key-int-max={gop} ! "
            f"h264parse config-interval=1 ! "
        )

    if fmt.upper() in ('MJPG', 'JPEG'):
        pipeline_str = (
            f"{src} ! "
            f"image/jpeg,width={width},height={height},framerate={int(fps)}/1 ! "
            f"jpegdec ! "
            f"videoconvert ! "
            f"{encoder_str}"
            f"rtph264pay config-interval=1 pt=96 ! "
            f"udpsink host={host} port={port} sync=false async=false"
        )
    else:
        pipeline_str = (
            f"{src} ! "
            f"{caps} ! "
            f"videoconvert ! "
            f"{encoder_str}"
            f"rtph264pay config-interval=1 pt=96 ! "
            f"udpsink host={host} port={port} sync=false async=false"
        )

    log.debug(f'Pipeline: {pipeline_str}')
    return pipeline_str

class UDPStreamer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.device = None
        self.pipeline = None
        self.loop = GLib.MainLoop()
        self.running = False
        self.encoder_type = cfg['udp'].get('encoder', 'hardware')

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

    def _try_build_pipeline(self, encoder_type):
        self.cfg['udp']['encoder'] = encoder_type
        pipe_str = build_pipeline(self.cfg, self.device, self.width, self.height, self.fps, self.fmt)
        self.pipeline = Gst.parse_launch(pipe_str)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_bus_message)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error(f'Failed to start pipeline with {encoder_type} encoder')
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            return False
        log.info(f'Streaming started with {encoder_type} encoder')
        return True

    def _build_and_run_pipeline(self):
        if self._try_build_pipeline(self.encoder_type):
            return True

        if self.encoder_type == 'hardware':
            log.warning('Hardware encoder failed, falling back to software (x264enc)...')
            self.encoder_type = 'software'
            return self._try_build_pipeline('software')

        return False

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error(f'GStreamer error: {err} - {debug}')
            self._try_fallback()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            log.warning(f'GStreamer warning: {warn} - {debug}')
        elif t == Gst.MessageType.EOS:
            log.info('End of stream')
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old, new, _ = message.parse_state_changed()
                log.debug(f'State changed: {old.value_nick} -> {new.value_nick}')

    def _try_fallback(self):
        if not self.running:
            return
        if self.encoder_type == 'hardware':
            log.warning('Hardware encoder error, attempting software fallback...')
            self._cleanup_pipeline()
            self.encoder_type = 'software'
            if not self._try_build_pipeline('software'):
                log.error('Software encoder also failed')
                self.running = False
                self.loop.quit()
        else:
            log.error('Software encoder error, stopping')
            self.running = False
            self.loop.quit()

    def _cleanup_pipeline(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _run_loop(self):
        if not self._build_and_run_pipeline():
            log.error('Failed to start pipeline with any encoder')
            return
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

def signal_handler(signum, frame):
    log.info(f'Received signal {signum}, shutting down')
    streamer.stop()

if __name__ == '__main__':
    Gst.init(None)
    GObject.threads_init()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    streamer = UDPStreamer(config)
    streamer.start()