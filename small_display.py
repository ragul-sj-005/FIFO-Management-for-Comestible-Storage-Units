"""
small_display.py  —  v3
────────────────────────────────────────
Secondary display — writes directly to /dev/fb1 (3.5" SPI screen, 480×320).

Launch:
    sudo python3 small_display.py

Display modes
─────────────
  startup      → SYSTEM READY
  idle         → SCAN TO REMOVE  + active/expired counts
  setup        → SPLIT SCREEN: top=item name, bottom=weight  (encoder input)
  update       → SPLIT SCREEN: top=item name, bottom=weight  (weight edit)
  camera       → live feed from FRAME_FILE
  printing     → ITEM ADDED
  fresh_scan   → FRESH REMOVED
  expired_scan → EXPIRED DISCARD
  timeout      → TIMEOUT CHECK ITEM

Encoder GPIO (BCM)
───────────────────
  ENC1_CLK = 17   ENC1_DT = 4    ENC1_SW = 5    ← Weight
  ENC2_CLK = 23   ENC2_DT = 24   ENC2_SW = 6    ← Item

External buttons (keyboard keys for now)
──────────────────────────────────────────
  SPACE  → confirm + print QR   (Button 1)
  DELETE → remove scanned item  (Button 2)

Buzzer behaviour
─────────────────
  Rings when item expires (once per item).
  Any button press silences it — but it re-rings after 60s if item still expired.
  Scanning the expired item's QR stops it permanently for that item.
"""

import os
import sys
import time
import threading
import logging
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import RPi.GPIO as GPIO

import shared_state as state

logging.basicConfig(
    filename="/tmp/fifo_errors.log",
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fifo.small")

# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY CONFIG
# ══════════════════════════════════════════════════════════════════════════════
FB_DEV    = "/dev/fb1"
FB_WIDTH  = 480
FB_HEIGHT = 320
POLL_MS   = 200
CAM_MS    = 50

C_BG     = ( 10,  14,  20)
C_PANEL  = ( 22,  27,  34)
C_WHITE  = (230, 237, 243)
C_GREEN  = ( 63, 185,  80)
C_RED    = (248,  81,  73)
C_ORANGE = (210, 153,  34)
C_ACCENT = (  0, 212, 170)
C_AMBER  = (234, 179,   8)
C_SUB    = (100, 116, 139)
C_BLACK  = (  0,   0,   0)

STATE_MAP = {
    "startup":      (["SYSTEM",  "READY"],        C_ACCENT),
    "idle":         (["SCAN TO", "REMOVE"],        C_WHITE),
    "setup":        (["SELECT",  "ITEM+WT"],       C_AMBER),
    "update":       (["ADJUST",  "WEIGHT"],        C_ACCENT),
    "printing":     (["ITEM",    "ADDED"],         C_GREEN),
    "fresh_scan":   (["FRESH",   "REMOVED"],       C_GREEN),
    "expired_scan": (["EXPIRED", "DISCARD"],       C_RED),
    "timeout":      (["TIMEOUT", "CHECK ITEM"],    C_ORANGE),
}

# ══════════════════════════════════════════════════════════════════════════════
# GPIO — ENCODERS
# Polled in main loop — same approach as the working sample code.
# Local variables _local_item_idx and _local_weight are the single source of
# truth for encoder values inside small_display. They are written to shared
# state only on confirmation (ENC1_SW press or SPACE from hdmi_display).
# This prevents hdmi_display's periodic state writes from overwriting changes.
# ══════════════════════════════════════════════════════════════════════════════
ENC1_CLK = 19
ENC1_DT  = 20
ENC1_SW  = 5

ENC2_CLK = 21
ENC2_DT  = 26
ENC2_SW  = 6

# Local encoder state — owned by small_display only
_local_item_idx = 0
_local_weight   = 500
_enc1_last_clk  = 1
_enc2_last_clk  = 1
_enc1_sw_last   = 1
_enc2_sw_last   = 1

def _setup_gpio():
    GPIO.setwarnings(False)
    try:
        GPIO.cleanup()
    except Exception:
        pass
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in [ENC1_CLK, ENC1_DT, ENC1_SW, ENC2_CLK, ENC2_DT, ENC2_SW]:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def _poll_encoders():
    """
    Poll both encoders and push buttons.
    Called every 5ms from main loop — same as the working sample code.
    Updates local variables directly; writes to shared state only on SW press.
    Returns True if any value changed (triggers redraw).
    """
    global _local_item_idx, _local_weight
    global _enc1_last_clk, _enc2_last_clk
    global _enc1_sw_last,  _enc2_sw_last
    changed = False
    enc_mode = state.get("encoder_mode")

    # ── Encoder 1 — Weight ────────────────────────────────────────────────────
    clk1 = GPIO.input(ENC1_CLK)
    if clk1 != _enc1_last_clk:
        dt1 = GPIO.input(ENC1_DT)
        if enc_mode in ("setup", "update"):
            if dt1 != clk1:
                _local_weight = min(_local_weight + 100, 5000)
            else:
                _local_weight = max(_local_weight - 100, 100)
            # Write to shared state immediately so hdmi_display sees it
            state.set_val("weight_grams", _local_weight)
            state.write_state_file()
            changed = True
    _enc1_last_clk = clk1

    # ── Encoder 2 — Item ──────────────────────────────────────────────────────
    clk2 = GPIO.input(ENC2_CLK)
    if clk2 != _enc2_last_clk:
        dt2 = GPIO.input(ENC2_DT)
        if enc_mode == "setup":
            if dt2 != clk2:
                _local_item_idx = (_local_item_idx + 1) % len(state.ITEM_NAMES)
            else:
                _local_item_idx = (_local_item_idx - 1) % len(state.ITEM_NAMES)
            state.set_val("selected_item", state.ITEM_NAMES[_local_item_idx])
            state.write_state_file()
            changed = True
    _enc2_last_clk = clk2

    # ── Encoder 1 SW — confirm weight update / silence buzzer ─────────────────
    sw1 = GPIO.input(ENC1_SW)
    if sw1 == 0 and _enc1_sw_last == 1:   # falling edge = button pressed
        if enc_mode == "update":
            _confirm_weight_update()
        _silence_buzzer_temp()
        changed = True
    _enc1_sw_last = sw1

    # ── Encoder 2 SW — silence buzzer ─────────────────────────────────────────
    sw2 = GPIO.input(ENC2_SW)
    if sw2 == 0 and _enc2_sw_last == 1:
        _silence_buzzer_temp()
    _enc2_sw_last = sw2

    return changed

def _sync_local_from_state():
    """
    Sync local encoder vars from shared state when entering setup/update mode.
    Ensures local vars start from the current shared state values.
    """
    global _local_item_idx, _local_weight
    item = state.get("selected_item")
    if item in state.ITEM_NAMES:
        _local_item_idx = state.ITEM_NAMES.index(item)
    _local_weight = state.get("weight_grams") or 500

# ══════════════════════════════════════════════════════════════════════════════
# BUZZER SILENCE
# ══════════════════════════════════════════════════════════════════════════════
_buzzer_silence_timer = None

def _silence_buzzer_temp():
    """
    Signal hdmi_display to silence the buzzer via shared state flag.
    small_display does NOT own the buzzer GPIO — only hdmi_display does.
    """
    global _buzzer_silence_timer
    state.set_val("buzzer_active", False)
    state.set_val("buzzer_silenced_request", True)
    state.write_state_file()
    if _buzzer_silence_timer is not None:
        _buzzer_silence_timer.cancel()
    log.warning("Buzzer silence requested via encoder button")

def _confirm_weight_update():
    """Update weight of the active QR item and return to idle."""
    qr_id = state.get("active_qr_id")
    if not qr_id:
        return
    db = state.get_food_db()
    if qr_id in db:
        db[qr_id]["weight_grams"] = state.get("weight_grams")
        state.update_food_db(db)
        log.warning("Weight updated: %s → %dg", qr_id, state.get("weight_grams"))
    state.set_val("encoder_mode",  None)
    state.set_val("active_qr_id", None)
    state.set_val("display_mode", "idle")
    state.write_state_file(force=True)   # force — ensure hdmi_display gets update

# ══════════════════════════════════════════════════════════════════════════════
# FRAMEBUFFER WRITER
# ══════════════════════════════════════════════════════════════════════════════
def write_to_fb(img_rgb: np.ndarray):
    h, w = img_rgb.shape[:2]
    if h != FB_HEIGHT or w != FB_WIDTH:
        pil = Image.fromarray(img_rgb.astype(np.uint8)).resize(
            (FB_WIDTH, FB_HEIGHT), Image.BILINEAR)
        img_rgb = np.array(pil)

    r = img_rgb[:, :, 0].astype(np.uint16)
    g = img_rgb[:, :, 1].astype(np.uint16)
    b = img_rgb[:, :, 2].astype(np.uint16)
    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

    try:
        with open(FB_DEV, "wb") as fb:
            fb.write(rgb565.astype(np.uint16).tobytes())
    except PermissionError:
        print("[fb1] Permission denied — run with sudo"); sys.exit(1)
    except FileNotFoundError:
        print(f"[fb1] {FB_DEV} not found"); sys.exit(1)
    except Exception as e:
        log.error("write_to_fb: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# FONTS
# ══════════════════════════════════════════════════════════════════════════════
def _load_font(size):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

FONT_XL     = _load_font(58)
FONT_LARGE  = _load_font(44)
FONT_MEDIUM = _load_font(26)
FONT_SMALL  = _load_font(17)
FONT_TINY   = _load_font(13)

# ══════════════════════════════════════════════════════════════════════════════
# RENDERERS
# ══════════════════════════════════════════════════════════════════════════════
def _canvas():
    img  = Image.new("RGB", (FB_WIDTH, FB_HEIGHT), C_BG)
    draw = ImageDraw.Draw(img)
    return img, draw

def _cx(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return (FB_WIDTH - (bb[2] - bb[0])) // 2

def _draw_divider(draw, y, colour=C_ACCENT):
    draw.rectangle([(0, y), (FB_WIDTH, y + 2)], fill=colour)

def _draw_topbar(draw, colour):
    draw.rectangle([(0, 0), (FB_WIDTH, 5)], fill=colour)

def _draw_bottombar(draw, colour):
    draw.rectangle([(0, FB_HEIGHT - 5), (FB_WIDTH, FB_HEIGHT)], fill=colour)

def _draw_timestamp(draw):
    ts = datetime.now().strftime("%H:%M:%S")
    draw.text((_cx(draw, ts, FONT_TINY), FB_HEIGHT - 20),
              ts, font=FONT_TINY, fill=C_SUB)


def render_message(mode: str):
    """Full-screen message for non-split states."""
    lines, colour = STATE_MAP.get(mode, (["...", ""], C_WHITE))
    img, draw = _canvas()
    _draw_topbar(draw, colour)
    _draw_bottombar(draw, colour)
    draw.ellipse([(FB_WIDTH - 26, 12), (FB_WIDTH - 10, 28)], fill=colour)

    line_h  = 64
    total_h = len(lines) * line_h
    start_y = (FB_HEIGHT - total_h) // 2 - 10
    for i, line in enumerate(lines):
        x = _cx(draw, line, FONT_XL)
        y = start_y + i * line_h
        draw.text((x + 2, y + 2), line, font=FONT_XL, fill=C_BLACK)
        draw.text((x, y),         line, font=FONT_XL, fill=colour)

    _draw_timestamp(draw)
    write_to_fb(np.array(img))


def render_idle(db: dict):
    """Idle: SCAN TO REMOVE + live counters."""
    img, draw = _canvas()
    _draw_topbar(draw, C_ACCENT)
    _draw_bottombar(draw, C_ACCENT)

    for i, line in enumerate(["SCAN TO", "REMOVE"]):
        x = _cx(draw, line, FONT_XL)
        y = 55 + i * 68
        draw.text((x + 2, y + 2), line, font=FONT_XL, fill=C_BLACK)
        draw.text((x, y),         line, font=FONT_XL, fill=C_WHITE)

    total   = len(db)
    expired = sum(1 for v in db.values() if v["expired"])
    active  = total - expired

    draw.rectangle([(12, 218), (FB_WIDTH - 12, 262)], fill=C_PANEL)
    draw.text((22,  226), f"ACTIVE : {active}",  font=FONT_MEDIUM, fill=C_GREEN)
    draw.text((258, 226), f"EXPIRED: {expired}", font=FONT_MEDIUM, fill=C_RED)

    _draw_timestamp(draw)
    write_to_fb(np.array(img))


def render_encoder_screen(item: str, weight: int, mode: str):
    """
    Split screen for setup and update modes.
    ┌──────────────────────────┐
    │   ITEM  (top half)       │  ← Encoder 2 controls (setup only)
    │   CHICKEN                │
    ├──────────────────────────┤
    │   WEIGHT (bottom half)   │  ← Encoder 1 controls always
    │   1200 g                 │
    └──────────────────────────┘
    """
    img, draw = _canvas()

    top_h    = FB_HEIGHT // 2       # 160px
    bot_y    = top_h + 3            # below divider

    # ── Top region — item ────────────────────────────────────────────────────
    top_colour = C_AMBER if mode == "setup" else C_ACCENT

    draw.rectangle([(0, 0), (FB_WIDTH, top_h)], fill=C_PANEL)
    _draw_topbar(draw, top_colour)

    label_top = "SELECT ITEM" if mode == "setup" else "ITEM"
    draw.text((12, 10), label_top, font=FONT_TINY, fill=top_colour)

    # Item name — centred in top half
    ix = _cx(draw, item, FONT_LARGE)
    iy = top_h // 2 - 22
    draw.text((ix + 2, iy + 2), item, font=FONT_LARGE, fill=C_BLACK)
    draw.text((ix, iy),         item, font=FONT_LARGE, fill=C_WHITE)

    # Shelf life hint
    shelf = state.ITEMS.get(item, 60)
    hint  = f"Shelf life: {shelf}s"
    draw.text((_cx(draw, hint, FONT_TINY), top_h - 22),
              hint, font=FONT_TINY, fill=C_SUB)

    # ── Divider ──────────────────────────────────────────────────────────────
    _draw_divider(draw, top_h, top_colour)

    # ── Bottom region — weight ───────────────────────────────────────────────
    bot_colour = C_GREEN
    draw.rectangle([(0, bot_y), (FB_WIDTH, FB_HEIGHT)], fill=C_PANEL)

    draw.text((12, bot_y + 6), "WEIGHT", font=FONT_TINY, fill=bot_colour)

    w_str = f"{weight} g"
    wx = _cx(draw, w_str, FONT_LARGE)
    wy = bot_y + (FB_HEIGHT - bot_y) // 2 - 22
    draw.text((wx + 2, wy + 2), w_str, font=FONT_LARGE, fill=C_BLACK)
    draw.text((wx, wy),         w_str, font=FONT_LARGE, fill=C_WHITE)

    # Arrows hint
    arrows = "◄ rotate to adjust ►"
    draw.text((_cx(draw, arrows, FONT_TINY), FB_HEIGHT - 28),
              arrows, font=FONT_TINY, fill=C_SUB)

    _draw_bottombar(draw, bot_colour)

    # ── Footer instruction ────────────────────────────────────────────────────
    if mode == "setup":
        footer = "SPACE to confirm + print"
    else:
        footer = "ENC1 press to save weight"
    draw.text((_cx(draw, footer, FONT_TINY), FB_HEIGHT - 16),
              footer, font=FONT_TINY, fill=top_colour)

    write_to_fb(np.array(img))


def render_camera():
    try:
        frame = np.load(state.FRAME_FILE)
    except Exception:
        return False

    img  = Image.fromarray(frame.astype(np.uint8)).resize(
        (FB_WIDTH, FB_HEIGHT), Image.BILINEAR)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (FB_WIDTH - 1, FB_HEIGHT - 1)],
                   outline=C_ACCENT, width=4)
    draw.rectangle([(0, 0), (185, 26)], fill=C_BLACK)
    draw.text((8, 4), "CAMERA ACTIVE", font=FONT_SMALL, fill=C_ACCENT)
    write_to_fb(np.array(img))
    return True

# ══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════
def main():
    print(f"[small_display] Starting — {FB_DEV}")
    _setup_gpio()
    render_message("startup")

    last_mode      = "startup"
    last_sig       = None
    last_enc_mode  = None   # detect transitions into setup/update

    while True:
        try:
            raw = state.read_state_file()

            mode     = raw.get("display_mode", "idle")
            db       = raw.get("food_db", {})
            enc_mode = raw.get("encoder_mode")

            # ── Sync local encoder vars when entering setup/update mode ───────
            if enc_mode in ("setup", "update") and last_enc_mode not in ("setup", "update"):
                _sync_local_from_state()
            last_enc_mode = enc_mode

            # ── Camera ────────────────────────────────────────────────────────
            if mode == "camera":
                render_camera()
                last_mode = "camera"
                time.sleep(CAM_MS / 1000)
                continue

            # ── Encoder split screen (setup / update) ─────────────────────────
            if enc_mode in ("setup", "update"):
                # Poll encoders — use LOCAL vars for display (fast response)
                enc_changed = _poll_encoders()
                item   = state.ITEM_NAMES[_local_item_idx]
                weight = _local_weight
                sig    = (item, weight, enc_mode)
                if sig != last_sig or enc_changed:
                    render_encoder_screen(item, weight, enc_mode)
                    last_sig  = sig
                last_mode = enc_mode
                time.sleep(0.005)   # 5ms poll — same as working sample code
                continue

            # ── Poll encoders even in non-encoder modes (for SW button) ───────
            _poll_encoders()

            # ── Idle ──────────────────────────────────────────────────────────
            if mode == "idle":
                total   = len(db)
                expired = sum(1 for v in db.values() if v["expired"])
                sig     = (total, expired)
                if mode != last_mode or sig != last_sig:
                    render_idle(db)
                    last_sig  = sig
                last_mode = mode
                time.sleep(POLL_MS / 1000)
                continue

            # ── All other message modes ────────────────────────────────────────
            if mode != last_mode:
                render_message(mode)
                last_mode = mode
                last_sig  = None

            time.sleep(POLL_MS / 1000)

        except Exception as e:
            log.error("main loop: %s", e)
            time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        GPIO.cleanup()
        print("\n[small_display] Stopped.")
