#!/usr/bin/env python3
"""
# ==============================================================================
#  OLED Monitor for OpenWrt (Dell DW5821e / ModemManager)
#  Optimized & Customized by: DEJEDE (085236578999 - Photo & Video Services)
# ==============================================================================
OLED Monitor v6 untuk OpenWrt - SSD1306 128x64 (Auto Operator Real & Anti-Freeze)
Layout:
  [kuning] bar sinyal + persentase (%) + nama operator asli
  [biru]   kecepatan upload/download + panah animasi
  [biru]   teks berjalan (marquee)
"""

import sys
import os
import fcntl
import signal
import time
import math
import subprocess
import json
import logging

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.core.error import DeviceNotFoundError
from luma.oled.device import ssd1306
from PIL import Image, ImageDraw

WIDTH, HEIGHT = 128, 64
LOCK_PATH = "/var/run/oled_monitor.lock"
LOG_PATH = "/var/log/oled_monitor.log"

MARQUEE_TEXT = ">> DEJEDE : 085236578999 - PHOTO & VIDEO SERVICES <<         "
MARQUEE_SPEED = 40    # Ubah dari 100 menjadi 40 (atau 30 jika ingin lebih lambat lagi)
BOUNCE_HZ = 0.04       

TARGET_FRAME_TIME = 0.05    
DATA_REFRESH_INTERVAL = 2.0   

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("oled_monitor")

# --- Single instance lock ---
_lock_fp = open(LOCK_PATH, "w")
try:
    fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    log.error("Instance lain masih berjalan. Keluar.")
    sys.exit(1)

running = True

def _handle_signal(signum, frame):
    global running
    running = False

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

try:
    serial = i2c(port=2, address=0x3C)
    device = ssd1306(serial, width=128, height=64)
except DeviceNotFoundError as e:
    log.error("OLED tidak terdeteksi: %s", e)
    sys.exit(1)


# ---------- Pembacaan Operator Otomatis dari Sistem Modeminfo / UCI ----------

def get_modem_info_smart():
    """Mengambil data asli dari ModemManager. Jika gagal, tampilkan pesan error yang jelas."""
    signal_pct = 0
    bar_level = 0
    operator = "NO SERVICE"

    try:
        cmd = subprocess.run(
            ["mmcli", "-m", "0", "-J"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=0.4
        )
        if cmd.returncode == 0:
            data = json.loads(cmd.stdout.decode())
            modem_info = data.get("modem", {})
            
            # 1. Ambil Nama Operator Asli
            gpp_3 = modem_info.get("3gpp", {})
            operator = gpp_3.get("operator-name", "NO SERVICE")
            if not operator or operator == "--":
                operator = "NO SERVICE"
            else:
                operator = operator.upper()

            # 2. Cek Status Koneksi Modem
            generic_info = modem_info.get("generic", {})
            modem_state = generic_info.get("state", "")

            if modem_state == "connected":
                signal_pct = 100  # Menyelaraskan dengan indikator optimal
            else:
                sig_dict = generic_info.get("signal-quality", {})
                val_str = sig_dict.get("value", "0")
                signal_pct = int(val_str) if val_str.isdigit() else 0

        else:
            # Jika perintah mmcli gagal dieksekusi oleh sistem
            operator = "MMCLI ERROR"
            signal_pct = 0

        # Konversi persentase sinyal ke bar indikator (0 s.d 4)
        if signal_pct >= 75:
            bar_level = 4
        elif signal_pct >= 50:
            bar_level = 3
        elif signal_pct >= 25:
            bar_level = 2
        elif signal_pct > 0:
            bar_level = 1
        else:
            bar_level = 0

    except subprocess.TimeoutExpired:
        # Khusus jika terjadi timeout (terlalu lama merespons)
        operator = "TIMEOUT"
        signal_pct = 0
        bar_level = 0
    except Exception:
        # Jika terjadi error tak terduga lainnya
        operator = "SYSTEM ERROR"
        signal_pct = 0
        bar_level = 0

    return bar_level, signal_pct, operator

class NetMonitor:
    def __init__(self, iface="wwan0"):
        self.iface = iface
        self.prev_rx = self.prev_tx = None
        self.prev_t = None
        self.rx_kbps = 0.0
        self.tx_kbps = 0.0

    def update(self):
        try:
            if not os.path.exists(f"/sys/class/net/{self.iface}/statistics"):
                for alt_iface in ["wwan0", "rmnet_data0", "eth0", "usb0"]:
                    if os.path.exists(f"/sys/class/net/{alt_iface}/statistics"):
                        self.iface = alt_iface
                        break

            base = f"/sys/class/net/{self.iface}/statistics"
            if not os.path.exists(base):
                return
                
            with open(f"{base}/rx_bytes") as f:
                rx = int(f.read())
            with open(f"{base}/tx_bytes") as f:
                tx = int(f.read())
            now = time.time()
            if self.prev_t is not None:
                dt = max(now - self.prev_t, 0.001)
                self.rx_kbps = (rx - self.prev_rx) / 1024.0 / dt
                self.tx_kbps = (tx - self.prev_tx) / 1024.0 / dt
            self.prev_rx, self.prev_tx, self.prev_t = rx, tx, now
        except Exception:
            pass


def draw_signal_bars(draw, x, y, level):
    heights = [3, 6, 9, 12]
    for i, h in enumerate(heights):
        bx = x + i * 4
        by = y + (12 - h)
        if i < level:
            draw.rectangle((bx, by, bx + 2, y + 12), fill="white")
        else:
            draw.rectangle((bx, by, bx + 2, y + 12), outline="white")


def draw_arrow_up(draw, x, y, bounce):
    yy = y - bounce
    draw.polygon([(x, yy + 6), (x + 4, yy), (x + 8, yy + 6)], fill="white")
    draw.line((x + 4, yy + 6, x + 4, yy + 10), fill="white")


def draw_arrow_down(draw, x, y, bounce):
    yy = y + bounce
    draw.polygon([(x, yy), (x + 4, yy + 6), (x + 8, yy)], fill="white")
    draw.line((x + 4, yy - 4, x + 4, yy), fill="white")


def measure_text_width(text):
    tmp_draw = ImageDraw.Draw(Image.new("1", (1, 1)))
    try:
        bbox = tmp_draw.textbbox((0, 0), text)
        return bbox[2] - bbox[0]
    except AttributeError:
        w, _ = tmp_draw.textsize(text)
        return w


def main():
    log.info("DEJEDE OLED Monitor v6 mulai berjalan...")
    device.clear()

    net = NetMonitor(iface="wwan0")
    signal_level = 0
    signal_pct = 0
    operator = "STARTING"
    last_refresh = 0.0

    marquee_text_w = measure_text_width(MARQUEE_TEXT)
    marquee_x = float(WIDTH)
    bounce_phase = 0.0
    last_frame_t = time.monotonic()

    while running:
        now_t = time.monotonic()
        dt = now_t - last_frame_t
        last_frame_t = now_t
        dt = min(dt, 0.2)

        if now_t - last_refresh >= DATA_REFRESH_INTERVAL:
            try:
                signal_level, signal_pct, operator = get_modem_info_smart()
                net.update()
            except Exception:
                pass
            last_refresh = now_t

        marquee_x -= MARQUEE_SPEED * dt
        if marquee_x < -marquee_text_w:
            marquee_x = float(WIDTH)

        bounce_phase += dt * BOUNCE_HZ * 2 * math.pi
        bounce = int(round(2 * abs(math.sin(bounce_phase))))

        try:
            with canvas(device) as draw:
                draw_signal_bars(draw, 2, 2, signal_level)
                draw.text((20, 3), f"{signal_pct}%", fill="white")
                draw.text((55, 3), operator, fill="white")
                draw.line((0, 16, 127, 16), fill="white")

                draw_arrow_up(draw, 4, 22, bounce)
                draw.text((18, 22), f"UP  {net.tx_kbps:5.1f} KB/s", fill="white")
                draw_arrow_down(draw, 4, 34, bounce)
                draw.text((18, 34), f"DWN {net.rx_kbps:5.1f} KB/s", fill="white")
                draw.line((0, 47, 127, 47), fill="white")

                draw.text((int(marquee_x), 51), MARQUEE_TEXT, fill="white")
        except Exception:
            time.sleep(0.5)

        render_time = time.monotonic() - now_t
        sleep_time = TARGET_FRAME_TIME - render_time
        if sleep_time > 0:
            time.sleep(sleep_time)

    device.clear()
    log.info("OLED Monitor berhenti.")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            fcntl.flock(_lock_fp, fcntl.LOCK_UN)
            _lock_fp.close()
            os.remove(LOCK_PATH)
        except Exception:
            pass
