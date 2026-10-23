#!/usr/bin/env python3
"""
# ==============================================================================
#  Dejede OLEDMON for OpenWrt (Dell DW5821e / ModemManager)
#  Optimized & Customized by: DEJEDE (085236578999 - Photo & Video Services)
# ==============================================================================
Dejede OLEDMON v12.0 - SSD1306 128x64 - 3 scene + transisi + tema + kontrol LuCI

Scene (bergantian, hanya yang aktif):
  1. main    : sinyal modem, kecepatan jaringan, running text bawah     (default 60 dtk)
  2. weather : cuaca (Open-Meteo, tanpa API key)                        (default 20 dtk)
  3. ticker  : running text cyberpunk layar penuh                       (default 15 dtk)

Konfigurasi: /etc/config/oled_monitor (dibaca ulang otomatis tiap ~2 detik,
tanpa restart). Section bernama: global, main, weather, ticker.

  config global 'global'
      option theme 'normal'          # normal | inverted
      option contrast '255'          # 0..255
      option flip '0'                # 1 = layar diputar 180 derajat
      option transition 'slide_left' # none|random|slide_left|slide_right|slide_up|
                                     # slide_down|wipe|open|blinds|dissolve
      option transition_ms '600'     # lama transisi (ms), 0 = tanpa transisi

  config scene 'main'
      option enable '1'   option duration '60'
      option layout 'classic'        # classic | graph
      option marquee '1'             # tampilkan running text bawah
      option text '>> ... <<'        # isi running text
      option speed '40'              # px/detik

  config scene 'weather'
      option enable '1'   option duration '20'
      option layout 'classic'        # classic | detail
      option city 'Jakarta'  option lat ''  option lon ''
      option unit 'c'                # c | f
      option refresh '15'            # menit

  config scene 'ticker'
      option enable '1'   option duration '15'
      option style 'cyber'           # cyber | plain
      option text1 'DEJEDE'  option text2 'PHOTO & VIDEO'  option text3 '085236578999'
      option speed '50'   option glitch '1'

Aturan: scene nonaktif dilewati; kalau cuma satu yang aktif, layar tidak
berganti; kalau semua nonaktif, scene main tetap tampil. Scene cuaca otomatis
dilewati selama data cuaca belum ada.
"""

import sys
import os
import fcntl
import signal
import time
import math
import random
import glob
import shlex
import subprocess
import json
import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler

WIDTH, HEIGHT = 128, 64
I2C_PORT = 2
I2C_ADDRESS = 0x3C
NET_IFACE = "wwan0"

LOCK_PATH = "/var/run/oled_monitor.lock"
LOG_PATH = "/var/log/oled_monitor.log"
UCI_NAME = "oled_monitor"
UCI_PATH = "/etc/config/oled_monitor"
CFG_POLL_INTERVAL = 2.0         # cek perubahan konfigurasi (detik)

# Default running text scene utama (bawah layar)
MARQUEE_TEXT = ">> DEJEDE : 085236578999 - PHOTO & VIDEO SERVICES <<"
MARQUEE_GAP = 9                 # spasi di ujung teks sebelum mengulang
MARQUEE_SPEED = 40
BOUNCE_HZ = 0.04

TARGET_FRAME_TIME = 0.10        # ~10 FPS (normal)
TRANS_FRAME_TIME = 0.03         # secepat mungkin saat transisi
NET_REFRESH_INTERVAL = 2.0      # statistik jaringan
GRAPH_POINTS = 80               # titik grafik jaringan (1 px per titik, ~2,5 menit)
MODEM_REFRESH_INTERVAL = 10.0   # modeminfo (saat sehat); 1x panggil ~1 dtk CPU
MODEM_BACKOFF_MAX = 20.0        # jeda maksimum saat modeminfo gagal
FIRST_POLL_DELAY = 2.0          # tunda poll pertama supaya tidak rebutan saat boot
MODEMINFO_CMD = "modeminfo"
MODEMINFO_TIMEOUT = 10          # detik
MODEMINFO_BUSY_SKIPS = 3        # maks. berapa kali menunggu modeminfo lain selesai

I2C_WAIT_MAX = 60.0             # detik menunggu bus I2C/OLED siap
STALE_GRACE = 40.0              # detik

# Daftar scene (urutan tampil) + durasi default (detik)
SCENES = ("main", "weather", "ticker")
DEFAULT_DURATION = {"main": 60, "weather": 20, "ticker": 15}

# Transisi antar scene
TRANSITIONS = ("slide_left", "slide_right", "slide_up", "slide_down",
               "wipe", "open", "blinds", "dissolve")
TRANSITION_CHOICES = ("none", "random") + TRANSITIONS
DEFAULT_TRANSITION = "slide_left"
DEFAULT_TRANSITION_MS = 600

# Default cuaca (dipakai kalau UCI tidak mengisi kota/koordinat)
WEATHER_CITY = "JAKARTA"
WEATHER_LAT, WEATHER_LON = -6.2088, 106.8456
WEATHER_FIRST_DELAY = 20.0      # tunda fetch pertama setelah start (ringankan boot)
WEATHER_TIMEOUT = 10            # detik per request
WEATHER_MAX_AGE = 6 * 3600.0    # data lebih tua dari ini tidak ditampilkan

# Default running text cyberpunk
TICKER_PARTS_DEFAULT = ("DEJEDE", "PHOTO & VIDEO", "085236578999")
GLITCH_PERIOD = 4.5             # detik antar glitch
GLITCH_DURATION = 0.35          # lama satu glitch
PROGRESS_BLOCKS = 21            # jumlah blok progress bar waktu scene
STRIP_H = 12                    # tinggi strip teks (1x)

# Area layout scene utama
HEADER_H = 16
NET_X, NET_Y = 14, 17
NET_W, NET_H = WIDTH - NET_X, 30
MARQUEE_Y = 51

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=64 * 1024, backupCount=1),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("oled_monitor")

# --- Single instance lock (sebelum import berat) ---
_lock_fp = open(LOCK_PATH, "w")
try:
    fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    log.error("Instance lain masih berjalan. Keluar.")
    sys.exit(1)

running = True
stop_event = threading.Event()


def _handle_signal(signum, frame):
    global running
    running = False
    stop_event.set()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# Import berat (PIL + luma) baru setelah lock & signal siap
from luma.core.interface.serial import i2c          # noqa: E402
from luma.core.error import DeviceNotFoundError, DevicePermissionError  # noqa: E402
from luma.oled.device import ssd1306                 # noqa: E402
from PIL import Image, ImageDraw, ImageChops         # noqa: E402

_ROT180 = getattr(Image, "Transpose", Image).ROTATE_180
_NEAREST = getattr(Image, "Resampling", Image).NEAREST


def init_device():
    """Init OLED dengan retry: saat boot bus I2C bisa belum siap."""
    deadline = time.monotonic() + I2C_WAIT_MAX
    while not stop_event.is_set():
        try:
            serial = i2c(port=I2C_PORT, address=I2C_ADDRESS)
            return ssd1306(serial, width=WIDTH, height=HEIGHT)
        except (DeviceNotFoundError, DevicePermissionError, OSError) as e:
            if time.monotonic() > deadline:
                log.error("OLED tidak terdeteksi setelah %ds: %s", int(I2C_WAIT_MAX), e)
                sys.exit(1)
            log.warning("OLED belum siap, coba lagi: %s", e)
            stop_event.wait(2.0)
    sys.exit(0)


# ---------- Helper konversi ----------

def _to_int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v, default):
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _f(v, default, lo, hi):
    x = _num(v)
    if x is None:
        return float(default)
    return max(float(lo), min(float(hi), x))


def _choice(v, allowed, default):
    v = str(v).strip().lower() if v is not None else ""
    return v if v in allowed else default


def _clean(s, maxlen):
    """Teks untuk font bitmap bawaan PIL (Latin-1): karakter lain jadi '?'."""
    s = (s or "").strip()
    return s.encode("latin-1", "replace").decode("latin-1")[:maxlen]


# ---------- Konfigurasi UCI (/etc/config/oled_monitor) ----------

weather_state = {"data": None, "ts": 0.0}
weather_wake = threading.Event()


def read_uci():
    """Baca seluruh config lewat `uci -q show` -> {section: {option: value}}."""
    try:
        out = subprocess.run(["uci", "-q", "show", UCI_NAME],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    raw = {}
    for line in out.stdout.decode(errors="ignore").splitlines():
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        parts = key.split(".")
        if len(parts) != 3:          # lewati baris definisi section
            continue
        val = val.strip()
        try:                         # menangani tanda kutip di dalam teks ('\'' dari uci)
            toks = shlex.split(val)
            val = toks[0] if toks else ""
        except ValueError:
            if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                val = val[1:-1]
        raw.setdefault(parts[1], {})[parts[2]] = val
    return raw


def parse_cfg(raw):
    g, m, w, t = (raw.get(k, {}) for k in ("global",) + SCENES)
    parts = tuple(
        (_clean(t.get(f"text{i}"), 24).upper() or TICKER_PARTS_DEFAULT[i - 1])
        for i in (1, 2, 3)
    )
    return {
        "global": {
            "theme": _choice(g.get("theme"), ("normal", "inverted"), "normal"),
            "contrast": int(_f(g.get("contrast"), 255, 0, 255)),
            "flip": _b(g.get("flip"), False),
            "transition": _choice(g.get("transition"), TRANSITION_CHOICES, DEFAULT_TRANSITION),
            "transition_s": _f(g.get("transition_ms"), DEFAULT_TRANSITION_MS, 0, 2000) / 1000.0,
        },
        "main": {
            "enable": _b(m.get("enable"), True),
            "duration": _f(m.get("duration"), DEFAULT_DURATION["main"], 3, 3600),
            "layout": _choice(m.get("layout"), ("classic", "graph"), "classic"),
            "marquee": _b(m.get("marquee"), True),
            "text": _clean(m.get("text"), 160) or MARQUEE_TEXT,
            "speed": _f(m.get("speed"), MARQUEE_SPEED, 10, 150),
        },
        "weather": {
            "enable": _b(w.get("enable"), True),
            "duration": _f(w.get("duration"), DEFAULT_DURATION["weather"], 3, 3600),
            "layout": _choice(w.get("layout"), ("classic", "detail"), "classic"),
            "city": _clean(w.get("city"), 20),
            "lat": _num(w.get("lat")),
            "lon": _num(w.get("lon")),
            "unit": _choice(w.get("unit"), ("c", "f"), "c"),
            "refresh": _f(w.get("refresh"), 15, 1, 180) * 60.0,
        },
        "ticker": {
            "enable": _b(t.get("enable"), True),
            "duration": _f(t.get("duration"), DEFAULT_DURATION["ticker"], 3, 3600),
            "style": _choice(t.get("style"), ("cyber", "plain"), "cyber"),
            "parts": parts,
            "speed": _f(t.get("speed"), 50, 10, 150),
            "glitch": _b(t.get("glitch"), True),
        },
    }


cfg = parse_cfg({})
_cfg_mtime = [None]


def reload_cfg(force=False):
    """Muat ulang konfigurasi kalau file berubah. True kalau ada perubahan."""
    global cfg
    try:
        mt = os.stat(UCI_PATH).st_mtime
    except OSError:
        mt = None
    if not force and mt == _cfg_mtime[0]:
        return False
    _cfg_mtime[0] = mt
    old = cfg
    cfg = parse_cfg(read_uci() if mt is not None else {})

    def wkey(w):
        return (w["city"], w["lat"], w["lon"], w["unit"])

    ow, nw = old["weather"], cfg["weather"]
    if wkey(ow) != wkey(nw):
        weather_state["data"] = None      # lokasi/satuan berubah: jangan tampilkan data lama
        weather_wake.set()
    elif nw["enable"] and not ow["enable"]:
        weather_wake.set()
    log.info("Konfigurasi dimuat: main=%s weather=%s ticker=%s transisi=%s/%.2fs tema=%s",
             cfg["main"]["enable"], cfg["weather"]["enable"], cfg["ticker"]["enable"],
             cfg["global"]["transition"], cfg["global"]["transition_s"], cfg["global"]["theme"])
    return True


def scene_enabled(name):
    if not cfg[name]["enable"]:
        return False
    if name == "weather" and not weather_available():
        return False
    return True


def next_scene(cur, active):
    i = SCENES.index(cur) if cur in SCENES else -1
    for k in range(1, len(SCENES) + 1):
        n = SCENES[(i + k) % len(SCENES)]
        if n in active:
            return n
    return "main"


# ---------- Pembacaan modem (modeminfo) ----------

LTE_BANDS = [
    (0, 599, 1), (600, 1199, 2), (1200, 1949, 3), (1950, 2399, 4),
    (2400, 2649, 5), (2750, 3449, 7), (3450, 3799, 8), (5010, 5179, 12),
    (5180, 5279, 13), (5280, 5379, 14), (5730, 5849, 17), (5850, 5999, 18),
    (6000, 6149, 19), (6150, 6449, 20), (8040, 8689, 25), (8690, 9039, 26),
    (9210, 9659, 28), (9660, 9769, 29), (9770, 9869, 30), (9920, 10359, 32),
    (37750, 38249, 38), (38250, 38649, 39), (38650, 39649, 40),
    (39650, 41589, 41), (41590, 43589, 42), (43590, 45589, 43),
    (46790, 54539, 46), (66436, 67335, 66),
]


def lte_band_from_earfcn(earfcn):
    for lo, hi, band in LTE_BANDS:
        if lo <= earfcn <= hi:
            return band
    return None


def get_band_label(data):
    mode = str(data.get("mode", "")).strip().upper()
    if "LTE" not in mode:
        if "NR" in mode or "5G" in mode:
            return "5G"
        if any(k in mode for k in ("WCDMA", "UMTS", "HSPA", "HSDPA", "3G")):
            return "3G"
        if any(k in mode for k in ("GSM", "EDGE", "GPRS", "2G")):
            return "2G"
        return mode[:4] if mode and mode != "--" else ""
    earfcn = _to_int(data.get("arfcn"))
    if earfcn is None:
        return "LTE"
    band = lte_band_from_earfcn(earfcn)
    if band is None:
        return "LTE"
    label = f"B{band}"
    ca = _to_int(data.get("lteca"))
    if ca and ca > 0:
        label += "+"
    return label


def pct_to_bars(pct):
    if pct >= 75:
        return 4
    if pct >= 50:
        return 3
    if pct >= 25:
        return 2
    if pct > 0:
        return 1
    return 0


def _modeminfo_running():
    """True kalau ada proses modeminfo lain (LuCI, skrip lain) yang sedang jalan."""
    for p in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(p) as f:
                if f.read().strip() == MODEMINFO_CMD:
                    return True
        except OSError:
            pass
    return False


def fetch_modeminfo():
    # Process group sendiri: kalau timeout, bunuh SEMUA anak (mmcli, gcom, awk, dst.)
    proc = subprocess.Popen(
        [MODEMINFO_CMD],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=MODEMINFO_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        raise

    if proc.returncode != 0:
        raise RuntimeError(f"modeminfo returncode {proc.returncode}")

    raw = out.decode(errors="ignore")
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e < s:
        raise ValueError("output bukan JSON")
    data = json.loads(raw[s:e + 1])

    if isinstance(data, dict) and isinstance(data.get("modem"), list):
        data = data["modem"][0] if data["modem"] else {}
    if not isinstance(data, dict) or not data:
        raise ValueError("data kosong")

    if not str(data.get("reg", "")).strip() or not str(data.get("csq_per", "")).strip():
        raise ValueError("data belum lengkap")
    return data


def build_state(data):
    reg = str(data.get("reg", "")).strip()
    if reg not in ("1", "5"):
        return 0, 0, "NO SERVICE", "", None

    operator = str(data.get("cops", "")).strip().upper()
    if not operator or operator == "--":
        operator = "NO SERVICE"

    pct = _to_int(data.get("csq_per")) or 0
    pct = max(0, min(100, pct))
    return (pct_to_bars(pct), pct, operator, get_band_label(data),
            _to_int(data.get("chiptemp")))


modem_state = {"level": 0, "pct": 0, "operator": "STARTING", "band": "", "temp": None}


def modem_worker():
    last_ok = time.monotonic()
    fails = 0
    busy_skips = 0

    stop_event.wait(FIRST_POLL_DELAY)

    while not stop_event.is_set():
        # Scene utama dimatikan -> tidak perlu memanggil modeminfo (hemat CPU)
        if not cfg["main"]["enable"]:
            stop_event.wait(5.0)
            continue

        # Jangan numpuk dengan modeminfo lain (LuCI, skrip lain, dsb.)
        if busy_skips < MODEMINFO_BUSY_SKIPS and _modeminfo_running():
            busy_skips += 1
            stop_event.wait(1.0)
            continue
        busy_skips = 0

        delay = MODEM_REFRESH_INTERVAL
        try:
            lvl, pct, op, band, temp = build_state(fetch_modeminfo())
            modem_state.update(level=lvl, pct=pct, operator=op, band=band, temp=temp)
            fails = 0
            last_ok = time.monotonic()
        except FileNotFoundError:
            log.warning("Perintah '%s' tidak ditemukan", MODEMINFO_CMD)
            modem_state.update(level=0, pct=0, operator="NO MODEMINFO", band="", temp=None)
            delay = 30.0
        except subprocess.TimeoutExpired:
            fails += 1
            log.warning("Baca modeminfo timeout (gagal berturut-turut: %d)", fails)
        except Exception as e:
            fails += 1
            log.warning("Baca modeminfo gagal (berturut-turut: %d): %s", fails, e)

        if fails:
            # backoff ringan supaya ModemManager tidak dihajar saat modem belum siap
            delay = min(MODEM_REFRESH_INTERVAL + 2.0 * fails, MODEM_BACKOFF_MAX)
            if time.monotonic() - last_ok > STALE_GRACE:
                modem_state.update(level=0, pct=0, operator="NO DATA", band="", temp=None)

        stop_event.wait(delay)


# ---------- Monitor jaringan ----------

class NetMonitor:
    def __init__(self, iface=NET_IFACE):
        self.iface = iface
        self.prev_rx = self.prev_tx = None
        self.prev_t = None
        self.rx_kbps = 0.0
        self.tx_kbps = 0.0
        self.rx_hist = deque(maxlen=GRAPH_POINTS)
        self.tx_hist = deque(maxlen=GRAPH_POINTS)
        self.ver = 0                 # naik tiap sampel baru (untuk cache gambar)

    def _find_iface(self):
        if os.path.exists(f"/sys/class/net/{self.iface}/statistics"):
            return True
        cands = (glob.glob("/sys/class/net/wwan*/statistics")
                 + glob.glob("/sys/class/net/rmnet_data*/statistics")
                 + glob.glob("/sys/class/net/usb*/statistics")
                 + glob.glob("/sys/class/net/eth0/statistics"))
        if not cands:
            return False
        self.iface = cands[0].split("/")[4]
        self.prev_rx = self.prev_tx = self.prev_t = None
        log.info("Interface jaringan dipakai: %s", self.iface)
        return True

    def update(self):
        try:
            if not self._find_iface():
                return

            base = f"/sys/class/net/{self.iface}/statistics"
            with open(f"{base}/rx_bytes") as f:
                rx = int(f.read().strip())
            with open(f"{base}/tx_bytes") as f:
                tx = int(f.read().strip())

            now = time.monotonic()
            if self.prev_t is not None and self.prev_rx is not None and self.prev_tx is not None:
                dt = max(now - self.prev_t, 0.001)
                self.rx_kbps = max(0.0, (rx - self.prev_rx) / 1024.0 / dt)
                self.tx_kbps = max(0.0, (tx - self.prev_tx) / 1024.0 / dt)
                self.rx_hist.append(self.rx_kbps)
                self.tx_hist.append(self.tx_kbps)
                self.ver += 1

            self.prev_rx, self.prev_tx, self.prev_t = rx, tx, now
        except Exception:
            pass


# ---------- Gambar (scene utama) ----------

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


def draw_pixel_heart(draw, x, y):
    """Menggambar ikon Love ukuran 5x5 piksel"""
    heart_pixels = [
        (1, 0), (3, 0),
        (0, 1), (1, 1), (2, 1), (3, 1), (4, 1),
        (0, 2), (1, 2), (2, 2), (3, 2), (4, 2),
        (1, 3), (2, 3), (3, 3),
        (2, 4)
    ]
    for px, py in heart_pixels:
        draw.point((x + px, y + py), fill="white")


def text_w(draw, text):
    try:
        return int(round(draw.textlength(text)))
    except AttributeError:
        bbox = draw.textbbox((0, 0), text)
        return bbox[2] - bbox[0]


def draw_header(draw, level, pct, operator, band, temp):
    draw_signal_bars(draw, 2, 2, level)

    pct_txt = f"{pct}%"
    draw.text((20, 3), pct_txt, fill="white")
    x = 20 + text_w(draw, pct_txt) + 3

    band_txt = band or "--"
    temp_txt = f"{temp}°C" if temp is not None else "--°C"

    pad_sep = 2
    heart_w = 5
    sep_total = heart_w + (pad_sep * 2)

    avail = (WIDTH - 1) - x - (2 * sep_total) - text_w(draw, band_txt) - text_w(draw, temp_txt)

    op = operator
    while len(op) > 1 and text_w(draw, op) > avail:
        op = op[:-1]
    op = op.rstrip()

    draw.text((x, 3), op, fill="white")
    x += text_w(draw, op) + pad_sep
    draw_pixel_heart(draw, x, 5)
    x += heart_w + pad_sep
    draw.text((x, 3), band_txt, fill="white")
    x += text_w(draw, band_txt) + pad_sep
    draw_pixel_heart(draw, x, 5)
    x += heart_w + pad_sep
    draw.text((x, 3), temp_txt, fill="white")


def build_static_layer():
    img = Image.new("1", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    d.line((0, 16, 127, 16), fill="white")
    d.line((0, 47, 127, 47), fill="white")
    return img


def render_text_image(text):
    tmp = ImageDraw.Draw(Image.new("1", (1, 1)))
    try:
        bbox = tmp.textbbox((0, 0), text)
        w, h = bbox[2], bbox[3]
    except AttributeError:
        w, h = tmp.textsize(text)
    img = Image.new("1", (max(w, 1) + 2, max(h, 1) + 2))
    ImageDraw.Draw(img).text((0, 0), text, fill="white")
    return img


def fmt_rate(kbps):
    """Format ringkas kecepatan supaya muat di kolom label (maks. 7 karakter)."""
    if kbps >= 1000:
        return f"{kbps / 1024.0:.1f}M"
    if kbps >= 100:
        return f"{kbps:.0f}"
    return f"{kbps:.1f}"


def _line_chart(d, ry, label, value, hist):
    """Satu baris line chart (tinggi 15 px): label+nilai di kiri, grafik garis di kanan."""
    x0, x1 = 46, WIDTH - 3
    top, bot = ry + 1, ry + 13
    d.text((1, ry + 3), f"{label} {fmt_rate(value)}", fill="white")
    d.line((x0 - 2, top, x0 - 2, bot), fill="white")             # sumbu Y
    for x in range(x0 - 2, x1 + 1, 2):                           # sumbu X (putus-putus)
        d.point((x, bot + 1), fill="white")
    n = len(hist)
    if n == 0:
        return
    scale = max(10.0, max(hist))
    xs = x1 - (n - 1)                                            # data terbaru di kanan
    pts = [(xs + i, bot - int(round(v / scale * (bot - top)))) for i, v in enumerate(hist)]
    if n == 1:
        d.point(pts[0], fill="white")
    else:
        d.line(pts, fill="white")


def draw_net_graph(d, net):
    """Layout 'graph': dua line chart terpisah - UPLOAD (atas) dan DOWNLOAD (bawah)."""
    d.rectangle((0, 0, WIDTH - 1, 29), fill=0)
    _line_chart(d, 0, "UP", net.tx_kbps, list(net.tx_hist))
    _line_chart(d, 15, "DN", net.rx_kbps, list(net.rx_hist))


# ---------- Scene 3: running text cyberpunk layar penuh ----------

def _draw_sep(d, x):
    """Pemisah '|' bergaya jalur sirkuit: garis vertikal + dua takik."""
    d.line((x + 3, 1, x + 3, STRIP_H - 2), fill="white")
    d.line((x + 1, 3, x + 2, 3), fill="white")
    d.line((x + 4, 8, x + 5, 8), fill="white")


def build_cyber_strip(parts):
    """Strip 1x: [TAG] | teks | [nomor]  (dibuat sekali / saat teks berubah)."""
    tag, mid, num = parts
    tmp = ImageDraw.Draw(Image.new("1", (1, 1)))
    gap, sep_w, tail = 5, 7, 36
    w_tag = text_w(tmp, tag) + 7
    w_mid = text_w(tmp, mid)
    w_num = text_w(tmp, num) + 12
    total = 4 + w_tag + gap + sep_w + gap + w_mid + gap + sep_w + gap + w_num + tail

    img = Image.new("1", (total, STRIP_H))
    d = ImageDraw.Draw(img)
    x = 4
    d.rectangle((x, 0, x + w_tag - 1, STRIP_H - 1), fill="white")
    d.text((x + 3, 1), tag, fill="black")
    x += w_tag + gap
    _draw_sep(d, x)
    x += sep_w + gap
    d.text((x, 1), mid, fill="white")
    x += w_mid + gap
    _draw_sep(d, x)
    x += sep_w + gap
    xr = x + w_num - 1
    for bx, sx in ((x, 1), (xr, -1)):
        d.line((bx, 0, bx, STRIP_H - 1), fill="white")
        d.line((bx, 0, bx + 2 * sx, 0), fill="white")
        d.line((bx, STRIP_H - 1, bx + 2 * sx, STRIP_H - 1), fill="white")
    d.text((x + 6, 1), num, fill="white")
    return img


def build_big_strip(parts):
    """Strip diperbesar 2x supaya terbaca besar di layar penuh."""
    s = build_cyber_strip(parts)
    return s.resize((s.width * 2, s.height * 2), _NEAREST)


def build_plain_strip(parts):
    """Gaya 'plain': satu baris teks besar 2x, tanpa dekorasi."""
    s = render_text_image("   -   ".join(parts) + " " * 20)
    return s.resize((s.width * 2, s.height * 2), _NEAREST)


def glitch_band(band, seed, scale=1):
    """Efek glitch: irisan horizontal bergeser + satu blok ter-invert."""
    rnd = random.Random(seed)
    w, h = band.size
    out = band.copy()
    for _ in range(rnd.randint(1, 2)):
        hh = rnd.randint(2, 3) * scale
        y0 = rnd.randint(0, max(0, h - hh - 1))
        dx = rnd.choice((-3, -2, 2, 3)) * scale
        piece = band.crop((0, y0, w, y0 + hh))
        out.paste(0, (0, y0, w, y0 + hh))
        out.paste(piece, (dx, y0))
    cx = rnd.randint(10, max(10, w - 26))
    box = (cx, h // 10, min(w, cx + rnd.randint(5, 10) * scale), h - h // 10)
    out.paste(ImageChops.invert(out.crop(box)), box)
    return out


def build_ticker_frame(static_img, strip2, tx, t, phase, gseed, blocks):
    """Satu frame scene ticker: EQ di atas, teks besar di tengah, progress di bawah."""
    frame = static_img.copy()          # garis y=16 dan y=47 sudah ada
    d = ImageDraw.Draw(frame)

    for i in range(27):
        x = 10 + i * 4
        h = 2 + int(10 * abs(math.sin(i * 1.3 + t * 6.0) * math.cos(i * 0.4 - t * 2.1)))
        d.rectangle((x, 14 - h, x + 1, 14), fill="white")

    mid = Image.new("1", (WIDTH, 30))
    mid.paste(strip2, (tx, 3))
    if gseed is not None:
        mid = glitch_band(mid, gseed, 2)
    md = ImageDraw.Draw(mid)
    md.rectangle((0, 0, 7, 29), fill=0)
    md.rectangle((WIDTH - 8, 0, WIDTH - 1, 29), fill=0)
    for x0, sx in ((2, 1), (WIDTH - 3, -1)):
        md.line((x0, 2, x0, 27), fill="white")
        md.line((x0, 2, x0 + 3 * sx, 2), fill="white")
        md.line((x0, 27, x0 + 3 * sx, 27), fill="white")
    frame.paste(mid, (0, 17))

    for x in range(8, WIDTH - 8):
        if (x + phase) % 8 < 3:
            d.point((x, 51), fill="white")
    d.rectangle((8, 55, WIDTH - 9, 60), outline="white")
    for k in range(blocks):
        bx = 10 + k * 5
        d.rectangle((bx, 57, bx + 3, 58), fill="white")

    for cx, cy, sx, sy in ((0, 0, 1, 1), (WIDTH - 1, 0, -1, 1),
                           (0, HEIGHT - 1, 1, -1), (WIDTH - 1, HEIGHT - 1, -1, -1)):
        d.line((cx, cy, cx + 5 * sx, cy), fill="white")
        d.line((cx, cy, cx, cy + 5 * sy), fill="white")
    return frame


def build_plain_frame(strip2, tx, frac):
    frame = Image.new("1", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(frame)
    frame.paste(strip2, (tx, (HEIGHT - strip2.height) // 2 - 2))
    d.rectangle((0, HEIGHT - 3, int((WIDTH - 1) * frac), HEIGHT - 2), fill="white")
    return frame


# ---------- Scene 2: cuaca (Open-Meteo) ----------

def http_get_json(url, timeout=WEATHER_TIMEOUT):
    """GET JSON. Coba urllib dulu; kalau modul ssl tidak ada, pakai uclient-fetch/wget/curl."""
    last_err = None
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "dejede-oledmon/12.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        last_err = e

    for cmd in (["uclient-fetch", "-q", "-T", str(timeout), "-O", "-", url],
                ["wget", "-q", "-T", str(timeout), "-O", "-", url],
                ["curl", "-s", "-m", str(timeout), url]):
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=timeout + 5)
            if res.returncode == 0 and res.stdout:
                return json.loads(res.stdout.decode("utf-8", "ignore"))
        except Exception as e:
            last_err = e
    raise RuntimeError(f"gagal ambil cuaca: {last_err}")


_geo_cache = {}


def geocode_city(name):
    """Nama kota -> (lat, lon) lewat Open-Meteo Geocoding; hasil di-cache selama proses jalan."""
    key = name.strip().lower()
    if key in _geo_cache:
        return _geo_cache[key]
    from urllib.parse import quote
    url = ("https://geocoding-api.open-meteo.com/v1/search"
           f"?name={quote(name.strip())}&count=1&language=id&format=json")
    results = http_get_json(url).get("results") or []
    if not results:
        raise ValueError(f"kota '{name}' tidak ditemukan")
    _geo_cache[key] = (float(results[0]["latitude"]), float(results[0]["longitude"]))
    return _geo_cache[key]


def get_location():
    """Lokasi dari UCI (weather.city / lat / lon). Koordinat manual menang atas pencarian nama."""
    w = cfg["weather"]
    lat, lon = w["lat"], w["lon"]
    if lat is not None and lon is not None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("lat/lon di konfigurasi di luar rentang")
        return (w["city"] or "LOKASI").upper(), lat, lon
    if w["city"]:
        lat, lon = geocode_city(w["city"])
        return w["city"].upper(), lat, lon
    return WEATHER_CITY, WEATHER_LAT, WEATHER_LON


def fetch_weather():
    city, lat, lon = get_location()
    unit = cfg["weather"]["unit"]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
           "weather_code,wind_speed_10m,is_day,cloud_cover,precipitation&timezone=auto"
           + ("&temperature_unit=fahrenheit" if unit == "f" else ""))
    cur = http_get_json(url).get("current") or {}
    code = _to_int(cur.get("weather_code"))
    data = {
        "temp": _num(cur.get("temperature_2m")),
        "hum": _num(cur.get("relative_humidity_2m")),
        "feels": _num(cur.get("apparent_temperature")),
        "wind": _num(cur.get("wind_speed_10m")),
        "code": -1 if code is None else code,
        "is_day": 0 if _to_int(cur.get("is_day")) == 0 else 1,
        "cloud": _num(cur.get("cloud_cover")),
        "precip": _num(cur.get("precipitation")),
        "city": city,
        "unit": "F" if unit == "f" else "C",
        "lat": lat,
        "lon": lon,
    }
    if data["temp"] is None:
        raise ValueError("data cuaca kosong")
    return data


def weather_worker():
    stop_event.wait(WEATHER_FIRST_DELAY)
    fails = 0
    while not stop_event.is_set():
        weather_wake.clear()
        w = cfg["weather"]
        if not w["enable"]:                  # scene cuaca dimatikan: jangan fetch
            weather_wake.wait(5.0)
            continue
        try:
            weather_state["data"] = fetch_weather()
            d = weather_state["data"]
            log.info("Cuaca %s (lat=%s lon=%s): kode=%s suhu=%s awan=%s%% hujan=%smm -> %s",
                     d["city"], d["lat"], d["lon"], d["code"], d["temp"], d["cloud"], d["precip"],
                     weather_info(d["code"], d["is_day"], d["cloud"], d["precip"])[1])
            weather_state["ts"] = time.monotonic()
            fails = 0
            delay = w["refresh"]
        except Exception as e:
            fails += 1
            delay = min(60.0 * (2 ** (fails - 1)), 600.0)
            log.warning("Cuaca gagal (%d): %s", fails, e)
        weather_wake.wait(delay)             # bangun lebih awal kalau lokasi diubah


def weather_available():
    d = weather_state["data"]
    return d is not None and (time.monotonic() - weather_state["ts"]) < WEATHER_MAX_AGE


def weather_info(code, is_day, cloud=None, precip=None):
    """Kode WMO + tutupan awan + curah hujan -> (nama ikon, teks Indonesia).

    Kode WMO model cuaca sering 'CERAH' padahal langit tropis berawan/gerimis, jadi
    untuk kode 0-3 kondisi dikoreksi memakai curah hujan & persen tutupan awan."""
    if code in (0, 1, 2, 3) or code is None or code < 0:
        if precip is not None and precip >= 0.1:
            return "rain", ("GERIMIS" if precip < 0.5 else "HUJAN")
        if cloud is not None:
            if cloud < 25:
                return ("sun" if is_day else "moon"), "CERAH"
            if cloud < 75:
                return ("partly" if is_day else "cloud"), "BERAWAN"
            return "cloud", "MENDUNG"
    if code in (0, 1):
        return ("sun" if is_day else "moon"), "CERAH"
    if code == 2:
        return ("partly" if is_day else "cloud"), "BERAWAN"
    if code == 3:
        return "cloud", "MENDUNG"
    if code in (45, 48):
        return "fog", "KABUT"
    if 51 <= code <= 57:
        return "rain", "GERIMIS"
    if code in (65, 67, 82):
        return "rain", "HUJAN DERAS"
    if 61 <= code <= 67 or 80 <= code <= 81:
        return "rain", "HUJAN"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow", "SALJU"
    if code in (95, 96, 99):
        return "thunder", "BADAI PETIR"
    return "cloud", "--"


def _cloud(d, x, y, fill="white", g=0):
    """Awan 24x15 piksel; g = pelebaran (untuk 'halo' hitam di belakang awan)."""
    d.ellipse((x + 2 - g, y + 6 - g, x + 11 + g, y + 15 + g), fill=fill)
    d.ellipse((x + 7 - g, y + 1 - g, x + 18 + g, y + 12 + g), fill=fill)
    d.ellipse((x + 14 - g, y + 5 - g, x + 23 + g, y + 15 + g), fill=fill)
    d.rectangle((x + 6 - g, y + 9 - g, x + 19 + g, y + 15 + g), fill=fill)


def _sun(d, cx, cy, r, ray):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white")
    for i in range(8):
        a = i * math.pi / 4
        x1 = cx + int(round((r + 2) * math.cos(a)))
        y1 = cy + int(round((r + 2) * math.sin(a)))
        x2 = cx + int(round((r + 2 + ray) * math.cos(a)))
        y2 = cy + int(round((r + 2 + ray) * math.sin(a)))
        d.line((x1, y1, x2, y2), fill="white")


def icon_sun(d, x, y):
    _sun(d, x + 14, y + 12, 6, 4)


def icon_moon(d, x, y):
    cx, cy, r = x + 12, y + 12, 9
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white")
    d.ellipse((cx - r + 6, cy - r - 2, cx + r + 4, cy + r - 2), fill="black")
    d.point((x + 22, y + 5), fill="white")
    d.point((x + 25, y + 11), fill="white")


def icon_partly(d, x, y):
    _sun(d, x + 9, y + 8, 4, 2)
    _cloud(d, x + 3, y + 8, "black", 1)
    _cloud(d, x + 3, y + 8)


def icon_cloud(d, x, y):
    _cloud(d, x + 5, y + 2)
    _cloud(d, x + 1, y + 8, "black", 1)
    _cloud(d, x + 1, y + 8)


def icon_rain(d, x, y):
    _cloud(d, x + 2, y + 1)
    for i in range(4):
        bx = x + 6 + i * 5
        d.line((bx, y + 18, bx - 2, y + 23), fill="white")


def icon_thunder(d, x, y):
    _cloud(d, x + 2, y + 1)
    d.polygon([(x + 14, y + 16), (x + 9, y + 21), (x + 13, y + 21),
               (x + 11, y + 24), (x + 18, y + 19), (x + 14, y + 19),
               (x + 17, y + 16)], fill="white")


def icon_fog(d, x, y):
    for i in range(4):
        yy = y + 4 + i * 5
        off = 3 if i % 2 else 0
        d.line((x + 2 + off, yy, x + 25 - (3 - off), yy), fill="white", width=2)


def icon_snow(d, x, y):
    _cloud(d, x + 2, y + 1)
    for i in range(3):
        for j in range(2):
            px, py = x + 6 + i * 6 + (j * 3), y + 18 + j * 4
            d.rectangle((px, py, px + 1, py + 1), fill="white")


ICONS = {"sun": icon_sun, "moon": icon_moon, "partly": icon_partly, "cloud": icon_cloud,
         "rain": icon_rain, "thunder": icon_thunder, "fog": icon_fog, "snow": icon_snow}


def _fit(d, s, maxw):
    while len(s) > 1 and text_w(d, s) > maxw:
        s = s[:-1]
    return s.rstrip()


def build_weather_frame(data, static_img, layout="classic"):
    frame = static_img.copy()
    d = ImageDraw.Draw(frame)
    icon, desc = weather_info(data["code"], data["is_day"], data.get("cloud"), data.get("precip"))
    ICONS.get(icon, icon_cloud)(d, 6, 20)
    u = data.get("unit", "C")

    head = _fit(d, f"{data.get('city', WEATHER_CITY)} - {desc}", WIDTH - 4)
    d.text(((WIDTH - text_w(d, head)) // 2, 3), head, fill="white")

    temp = data["temp"]
    if layout == "detail":
        # Daftar rinci di kanan ikon: suhu, kelembapan, angin; "terasa" di baris bawah
        rows = [f"SUHU   {temp:.0f}°{u}"]
        if data["hum"] is not None:
            rows.append(f"LEMBAB {data['hum']:.0f}%")
        if data["wind"] is not None:
            rows.append(f"ANGIN  {data['wind']:.0f}km/h")
        for i, r in enumerate(rows[:3]):
            d.text((40, 18 + i * 9), _fit(d, r, WIDTH - 42), fill="white")
        if data["feels"] is not None:
            line = f"TERASA {data['feels']:.0f}°{u}"
            d.text(((WIDTH - text_w(d, line)) // 2, 51), line, fill="white")
        return frame

    t_txt = f"{temp:.0f}°{u}" if temp is not None else f"--°{u}"
    big = render_text_image(t_txt)
    big = big.resize((big.width * 2, big.height * 2), _NEAREST)
    frame.paste(big, (42, 19))

    parts = []
    if data["hum"] is not None:
        parts.append(f"H:{data['hum']:.0f}%")
    if data["wind"] is not None:
        parts.append(f"W:{data['wind']:.0f}km/h")
    if data["feels"] is not None:
        parts.append(f"F:{data['feels']:.0f}°")
    line = ""
    for p in parts:
        cand = f"{line}  {p}" if line else p
        if text_w(d, cand) > WIDTH - 4:
            break
        line = cand
    d.text(((WIDTH - text_w(d, line)) // 2, 51), line, fill="white")
    return frame


# ---------- Transisi antar scene ----------

_NOISE = [None]


def _noise():
    if _NOISE[0] is None:
        rnd = random.Random(1234)
        _NOISE[0] = Image.frombytes("L", (WIDTH, HEIGHT),
                                    bytes(rnd.randrange(256) for _ in range(WIDTH * HEIGHT)))
    return _NOISE[0]


def ease(p):
    p = max(0.0, min(1.0, p))
    return p * p * (3.0 - 2.0 * p)


def compose_transition(kind, a, b, p):
    """Gabungkan frame lama (a) dan baru (b) pada progres p (0..1)."""
    if p <= 0.0:
        return a
    if p >= 1.0:
        return b
    if kind in ("slide_left", "slide_right"):
        off = int(round(WIDTH * p))
        out = Image.new("1", (WIDTH, HEIGHT))
        if kind == "slide_left":          # scene baru masuk dari kanan
            out.paste(a, (-off, 0))
            out.paste(b, (WIDTH - off, 0))
        else:                             # scene baru masuk dari kiri
            out.paste(a, (off, 0))
            out.paste(b, (off - WIDTH, 0))
        return out
    if kind in ("slide_up", "slide_down"):
        off = int(round(HEIGHT * p))
        out = Image.new("1", (WIDTH, HEIGHT))
        if kind == "slide_up":            # masuk dari bawah
            out.paste(a, (0, -off))
            out.paste(b, (0, HEIGHT - off))
        else:                             # masuk dari atas
            out.paste(a, (0, off))
            out.paste(b, (0, off - HEIGHT))
        return out
    out = a.copy()
    if kind == "wipe":
        x = int(round(WIDTH * p))
        if x > 0:
            out.paste(b.crop((0, 0, x, HEIGHT)), (0, 0))
    elif kind == "open":
        w = int(round(WIDTH * p / 2))
        if w > 0:
            box = (WIDTH // 2 - w, 0, WIDTH // 2 + w, HEIGHT)
            out.paste(b.crop(box), box[:2])
    elif kind == "blinds":
        h = max(1, int(round(8 * p)))
        for y0 in range(0, HEIGHT, 8):
            out.paste(b.crop((0, y0, WIDTH, y0 + h)), (0, y0))
    elif kind == "dissolve":
        thr = int(256 * p)
        mask = _noise().point([255 if i < thr else 0 for i in range(256)])
        out = Image.composite(b, a, mask)
    else:
        return b
    return out


def finalize(frame):
    """Terapkan tema (invert) dan flip 180 derajat."""
    g = cfg["global"]
    if g["theme"] == "inverted":
        frame = ImageChops.invert(frame)
    if g["flip"]:
        frame = frame.transpose(_ROT180)
    return frame


# ---------- Penyaji scene (cache per scene) ----------

class Display:
    def __init__(self, net):
        self.net = net
        self.static = build_static_layer()
        self.header_img = Image.new("1", (WIDTH, HEADER_H))
        self.header_draw = ImageDraw.Draw(self.header_img)
        self.net_img = Image.new("1", (NET_W, NET_H))
        self.net_draw = ImageDraw.Draw(self.net_img)
        self.graph_img = Image.new("1", (WIDTH, 30))
        self.graph_draw = ImageDraw.Draw(self.graph_img)
        self.header_key = self.net_key = None
        self.marq_text = None
        self.marq_img = None
        self.marq_w = WIDTH
        self.strip_key = None
        self.strip_img = None
        self.marquee_x = float(WIDTH)
        self.ticker_x = float(WIDTH)
        self.bounce_phase = 0.0
        self.bounce = 0
        self.cache = {}

    def frame_of(self, scene):
        c = self.cache.get(scene)
        return c[1] if c else None

    def _cached(self, scene, key, build):
        c = self.cache.get(scene)
        if c and c[0] == key:
            return key, c[1]
        frame = build()
        self.cache[scene] = (key, frame)
        return key, frame

    def advance(self, dt, scene):
        self.marquee_x -= cfg["main"]["speed"] * dt
        if self.marquee_x < -self.marq_w:
            self.marquee_x = float(WIDTH)
        self.bounce_phase += dt * BOUNCE_HZ * 2 * math.pi
        self.bounce = int(round(2 * abs(math.sin(self.bounce_phase))))
        if scene == "ticker":
            sw = self.strip_img.width if self.strip_img is not None else WIDTH
            self.ticker_x -= cfg["ticker"]["speed"] * dt
            if self.ticker_x < -sw:
                self.ticker_x = float(WIDTH)

    def render(self, scene, now_t, scene_since):
        if scene == "weather":
            return self._weather()
        if scene == "ticker":
            return self._ticker(now_t, scene_since)
        return self._main()

    # --- scene utama ---
    def _main(self):
        mc = cfg["main"]
        if mc["text"] != self.marq_text:
            self.marq_text = mc["text"]
            self.marq_img = render_text_image(mc["text"] + " " * MARQUEE_GAP)
            self.marq_w = self.marq_img.width
            self.marquee_x = float(WIDTH)

        m = modem_state
        hk = (m["level"], m["pct"], m["operator"], m["band"], m["temp"])
        if hk != self.header_key:
            self.header_key = hk
            self.header_draw.rectangle((0, 0, WIDTH - 1, HEADER_H - 1), fill=0)
            draw_header(self.header_draw, *hk)

        net = self.net
        mx = int(self.marquee_x) if mc["marquee"] else None

        if mc["layout"] == "graph":
            nk = ("g", net.ver)
            if nk != self.net_key:
                self.net_key = nk
                draw_net_graph(self.graph_draw, net)
            key = ("main", "graph", mx, hk, nk)

            def build():
                f = self.static.copy()
                f.paste(self.header_img, (0, 0))
                f.paste(self.graph_img, (0, 17))
                if mx is not None:
                    f.paste(self.marq_img, (mx, MARQUEE_Y))
                return f
            return self._cached("main", key, build)

        tx_txt = f"UP  {net.tx_kbps:5.1f} KB/s"
        rx_txt = f"DWN {net.rx_kbps:5.1f} KB/s"
        nk = (tx_txt, rx_txt)
        if nk != self.net_key:
            self.net_key = nk
            self.net_draw.rectangle((0, 0, NET_W - 1, NET_H - 1), fill=0)
            self.net_draw.text((18 - NET_X, 22 - NET_Y), tx_txt, fill="white")
            self.net_draw.text((18 - NET_X, 34 - NET_Y), rx_txt, fill="white")
        bounce = self.bounce
        key = ("main", "classic", mx, bounce, hk, nk)

        def build():
            f = self.static.copy()
            fd = ImageDraw.Draw(f)
            f.paste(self.header_img, (0, 0))
            f.paste(self.net_img, (NET_X, NET_Y))
            draw_arrow_up(fd, 4, 22, bounce)
            draw_arrow_down(fd, 4, 34, bounce)
            if mx is not None:
                f.paste(self.marq_img, (mx, MARQUEE_Y))
            return f
        return self._cached("main", key, build)

    # --- scene cuaca ---
    def _weather(self):
        data = weather_state["data"]
        layout = cfg["weather"]["layout"]
        key = ("weather", layout, tuple(sorted(data.items())))
        return self._cached("weather", key,
                            lambda: build_weather_frame(data, self.static, layout))

    # --- scene running text ---
    def _ticker(self, now_t, scene_since):
        tk = cfg["ticker"]
        sk = (tk["style"], tk["parts"])
        if sk != self.strip_key:
            self.strip_key = sk
            self.strip_img = (build_plain_strip(tk["parts"]) if tk["style"] == "plain"
                              else build_big_strip(tk["parts"]))
            self.ticker_x = float(WIDTH)
        tx = int(self.ticker_x)
        frac = max(0.0, min(1.0, (now_t - scene_since) / tk["duration"]))
        strip = self.strip_img

        if tk["style"] == "plain":
            key = ("ticker", "plain", tx, int(frac * WIDTH))
            return self._cached("ticker", key, lambda: build_plain_frame(strip, tx, frac))

        phase = int(now_t * 12) % 8
        gseed = (int(now_t * 10)
                 if tk["glitch"] and (now_t % GLITCH_PERIOD) < GLITCH_DURATION else None)
        blocks = int(PROGRESS_BLOCKS * frac)
        key = ("ticker", "cyber", tx, phase, gseed, blocks, int(now_t * 10))
        return self._cached("ticker", key, lambda: build_ticker_frame(
            self.static, strip, tx, now_t, phase, gseed, blocks))


# ---------- Loop utama ----------

def main():
    log.info("Dejede OLEDMON v12.0 mulai berjalan...")

    device = init_device()
    device.clear()
    reload_cfg(force=True)

    net = NetMonitor(iface=NET_IFACE)

    # Thread dibuat DULU (prioritas normal), baru proses render di-nice.
    # Di Linux nice() berlaku per-thread, jadi modeminfo/mmcli tidak ikut lambat.
    threading.Thread(target=modem_worker, daemon=True).start()
    threading.Thread(target=weather_worker, daemon=True).start()
    try:
        os.nice(10)
    except Exception:
        pass

    disp = Display(net)
    scene = "main"
    scene_since = time.monotonic()
    trans = None
    last_key = None
    last_err = None
    applied_contrast = None
    last_refresh = 0.0
    last_cfg_check = time.monotonic()
    last_frame_t = time.monotonic()

    while running:
        now_t = time.monotonic()
        dt = min(now_t - last_frame_t, 0.2)
        last_frame_t = now_t

        # --- konfigurasi UCI berubah? (LuCI Save & Apply -> berlaku tanpa restart) ---
        if now_t - last_cfg_check >= CFG_POLL_INTERVAL:
            last_cfg_check = now_t
            try:
                reload_cfg()
            except Exception as e:
                log.warning("Gagal memuat konfigurasi: %s", e)

        g = cfg["global"]
        if g["contrast"] != applied_contrast:
            try:
                device.contrast(g["contrast"])
                applied_contrast = g["contrast"]
            except Exception as e:
                applied_contrast = g["contrast"]
                log.warning("Gagal set contrast: %s", e)

        if now_t - last_refresh >= NET_REFRESH_INTERVAL:
            net.update()
            last_refresh = now_t

        disp.advance(dt, scene)

        # --- pilih scene: main -> weather -> ticker -> ... (hanya yang aktif) ---
        active = [n for n in SCENES if scene_enabled(n)] or ["main"]
        new_scene = scene
        if scene not in active:
            new_scene = next_scene(scene, active)
        elif len(active) > 1 and now_t - scene_since >= cfg[scene]["duration"]:
            new_scene = next_scene(scene, active)

        if new_scene != scene:
            old_frame = disp.frame_of(scene)
            scene = new_scene
            kind = g["transition"]
            if kind == "random":
                kind = random.choice(TRANSITIONS)
            dur = g["transition_s"]
            if kind != "none" and dur >= 0.05 and old_frame is not None:
                trans = {"kind": kind, "t0": now_t, "dur": dur, "old": old_frame}
            else:
                trans = None
            # durasi scene dihitung setelah transisi selesai
            scene_since = now_t + (trans["dur"] if trans else 0.0)
            if scene == "ticker":
                disp.ticker_x = float(WIDTH)      # teks selalu masuk dari kanan
            log.debug("Scene -> %s (%s)", scene, kind)

        if scene == "weather" and weather_state["data"] is None:
            scene, scene_since, trans = "main", now_t, None   # data hilang -> balik ke main

        key, frame = disp.render(scene, now_t, scene_since)

        if trans:
            p = (now_t - trans["t0"]) / trans["dur"]
            if p >= 1.0:
                trans = None
                last_key = None                    # paksa tampilkan frame akhir
            else:
                frame = compose_transition(trans["kind"], trans["old"], frame, ease(p))
                key = ("trans", now_t)             # selalu berbeda -> selalu digambar

        disp_key = (key, g["theme"], g["flip"])
        if disp_key != last_key:
            try:
                device.display(finalize(frame))
                last_key = disp_key
            except Exception as e:
                if str(e) != last_err:
                    log.warning("Gagal menampilkan frame (%s): %s", scene, e)
                    last_err = str(e)
                stop_event.wait(0.5)

        render_time = time.monotonic() - now_t
        sleep_time = (TRANS_FRAME_TIME if trans else TARGET_FRAME_TIME) - render_time
        if sleep_time > 0:
            stop_event.wait(sleep_time)

    try:
        device.clear()
    except Exception:
        pass
    log.info("Dejede OLEDMON berhenti.")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            # Lock file sengaja tidak dihapus (flock otomatis lepas saat proses mati);
            # menghapusnya bisa memicu race dengan instance baru.
            fcntl.flock(_lock_fp, fcntl.LOCK_UN)
            _lock_fp.close()
        except Exception:
            pass
