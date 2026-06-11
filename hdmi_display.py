"""
hdmi_display.py  —  v4  (corrected)
═══════════════════════════════════════════════════════════════════
Main control panel — HDMI display, 800×480, fullscreen.

Hardware
─────────
  • USB Webcam       cv2.VideoCapture(0)
  • Inductive Sensor GPIO 16 BCM — active HIGH, PUD_DOWN
  • Buzzer           GPIO 12 BCM — active HIGH
  • LED              GPIO 13 BCM — active HIGH
  • Printer          /dev/ttyUSB0  9600 baud  FT232R adapter

Keyboard (temporary — replace with GPIO buttons later)
───────────────────────────────────────────────────────
  SPACE  → Button 1 — enter setup mode / confirm + print QR
  DELETE → Button 2 — remove table-selected item completely
  ESC    → Button 3 — remove active_qr_id (last scanned item) from database

Workflow
────────
  1. Startup      → GUI shown, table empty, camera blank
  2. Press SPACE  → enter setup mode (small display shows split screen)
                    Encoder 2 selects item, Encoder 1 sets weight
  3. Press SPACE  → confirm → print QR → item added to table
  4. Sensor HIGH  → camera ON, QR scanning active
  5. Scan existing QR → small display shows item+weight, Encoder 1 adjusts
                        weight, Encoder 1 SW confirms update
  6. Scan expired QR  → item removed, buzzer stops permanently for that item
  7. Press DELETE while item highlighted → remove item immediately

Table columns: QR ID | Item | Weight | Shelf Life | Time Left | Status

Buzzer
───────
  Rings once per expired item.
  Any button press silences — re-rings 60s later if item still expired.
  Scanning expired QR stops buzzer permanently for that item.

Fixes vs v3
───────────
  • GPIO.cleanup() removed from _gpio_init() — was resetting small_display's
    encoder pins at startup, causing random encoder death.
    GPIO.cleanup() is now ONLY called in the final shutdown finally block.
  • _buzzer_silenced_at defined and properly wired: silence_buzzer() records
    the timestamp so check_expiry_loop's BUZZER_RERING guard works correctly.
    Previously _buzzer_silenced_at was used but never defined → NameError crash.
  • buzzer_silenced_request flag is cleared immediately after being acted on,
    preventing it from repeatedly triggering silence_buzzer() on every poll.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta

import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

import RPi.GPIO as GPIO
from pyzbar.pyzbar import decode
from escpos.printer import Serial

import shared_state as state

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fifo.hdmi")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
PIN_SENSOR      = 16
PIN_BUZZER      = 12
PIN_LED         = 13
BUZZER_DURATION = 10       # seconds buzzer rings before auto-off
BUZZER_RERING   = 60       # seconds before re-ring if item still expired

PRINTER_PORT    = "/dev/ttyUSB0"
PRINTER_BAUD    = 9600
CAMERA_INDEX    = 0
SCAN_COOLDOWN   = 2.0
MAX_ITEMS       = 20

# ══════════════════════════════════════════════════════════════════════════════
# GPIO SETUP
# ──────────────────────────────────────────────────────────────────────────────
# FIX: GPIO.cleanup() is NOT called here.
# Calling cleanup() at startup would reset the encoder pins that small_display
# already configured, causing random encoder failures and unstable pullups.
# cleanup() is only called once — in the final shutdown (see main() finally).
# ══════════════════════════════════════════════════════════════════════════════
def _gpio_init():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_SENSOR, GPIO.IN,  pull_up_down=GPIO.PUD_UP)   # NC sensor — resting HIGH, LOW = triggered
    GPIO.setup(PIN_BUZZER, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(PIN_LED,    GPIO.OUT, initial=GPIO.LOW)
    GPIO.output(PIN_BUZZER, GPIO.LOW)
    GPIO.output(PIN_LED,    GPIO.LOW)
    time.sleep(0.2)

# ══════════════════════════════════════════════════════════════════════════════
# PRINTER
# ══════════════════════════════════════════════════════════════════════════════
printer    = None
printer_ok = False

def _init_printer():
    global printer, printer_ok
    try:
        printer    = Serial(devfile=PRINTER_PORT, baudrate=PRINTER_BAUD, timeout=1)
        time.sleep(0.5)
        printer_ok = True
        log.info("Printer ready on %s", PRINTER_PORT)
    except Exception as e:
        printer_ok = False
        log.warning("Printer offline: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# BUZZER
# ──────────────────────────────────────────────────────────────────────────────
# FIX: _buzzer_silenced_at tracks when the buzzer was last silenced.
# This is used in check_expiry_loop to enforce BUZZER_RERING cooldown after
# a manual silence. Previously this variable was referenced but never defined,
# causing a NameError crash on the first expiry event.
# ══════════════════════════════════════════════════════════════════════════════
_buzzer_lock       = threading.Lock()
_buzzer_ringing    = False
_buzzer_silenced_at = [0.0]   # mutable container so inner functions can write it

def _buzzer_off_worker():
    global _buzzer_ringing
    time.sleep(BUZZER_DURATION)
    GPIO.output(PIN_BUZZER, GPIO.LOW)
    state.set_val("buzzer_active", False)
    state.write_state_file()
    with _buzzer_lock:
        _buzzer_ringing = False
    log.info("Buzzer auto-off")

def trigger_buzzer():
    global _buzzer_ringing
    with _buzzer_lock:
        if _buzzer_ringing:
            return
        _buzzer_ringing = True
    GPIO.output(PIN_BUZZER, GPIO.HIGH)
    state.set_val("buzzer_active", True)
    state.write_state_file()
    threading.Thread(target=_buzzer_off_worker, daemon=True).start()
    log.info("Buzzer triggered")

def silence_buzzer():
    """
    Silence buzzer on any button press.
    Records the silence timestamp so re-ring cooldown works correctly.
    Re-ring happens after BUZZER_RERING seconds if item is still expired.
    """
    global _buzzer_ringing
    GPIO.output(PIN_BUZZER, GPIO.LOW)
    state.set_val("buzzer_active", False)
    state.clear_all_buzzer_notified()
    state.write_state_file()
    with _buzzer_lock:
        _buzzer_ringing = False
    # FIX: record the time of silence so check_expiry_loop respects BUZZER_RERING
    _buzzer_silenced_at[0] = time.time()
    log.info("Buzzer silenced by button — will re-ring in %ds", BUZZER_RERING)

# ══════════════════════════════════════════════════════════════════════════════
# CAMERA
# ══════════════════════════════════════════════════════════════════════════════
camera = None

def _start_camera():
    global camera
    for attempt in range(5):
        # Force V4L2 backend — avoids GStreamer pipeline errors on Pi
        cam = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
        if cam.isOpened():
            cam.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # reduce buffer lag
            for _ in range(5):
                cam.read()
            camera = cam
            log.info("Camera started via V4L2 (attempt %d)", attempt + 1)
            return
        cam.release()
        time.sleep(1)
    log.warning("Camera not available — continuing without camera")

def _write_frame(frame_rgb):
    try:
        np.save(state.FRAME_FILE, frame_rgb)
    except Exception as e:
        log.warning("Frame write: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# QR DECODE — multi-strategy
# ══════════════════════════════════════════════════════════════════════════════
_last_scan = {}
_scan_lock = threading.Lock()

def _decode_qr(frame_bgr):
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    r = decode(blur)
    if r: return r
    eq = cv2.equalizeHist(blur)
    r  = decode(eq)
    if r: return r
    _, th = cv2.threshold(eq, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return decode(th)

def process_frame(frame_bgr, ui_ref=None):
    db  = state.get_food_db()
    now = time.time()
    for qr in _decode_qr(frame_bgr):
        qr_id = qr.data.decode("utf-8").strip()
        x, y, w, h = qr.rect
        cv2.rectangle(frame_bgr, (x, y), (x + w, y + h), (0, 255, 0), 3)
        cv2.putText(frame_bgr, qr_id, (x, y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        with _scan_lock:
            if now - _last_scan.get(qr_id, 0) < SCAN_COOLDOWN:
                continue
            _last_scan[qr_id] = now
        log.info("QR scanned: %s", qr_id)
        if qr_id in db:
            _handle_qr_scan(qr_id, db[qr_id], ui_ref)

def _handle_qr_scan(qr_id, item, ui_ref):
    """
    Scanned existing QR:
      - If expired → remove item, stop buzzer permanently
      - If fresh   → enter update mode so user can adjust weight
    """
    if item["expired"]:
        # Expired scan → remove immediately, stop buzzer for this item
        remove_item(qr_id, ui_ref=ui_ref)
        state.clear_buzzer_notified(qr_id)
        GPIO.output(PIN_BUZZER, GPIO.LOW)
        state.set_val("buzzer_active", False)
        state.write_state_file()
    else:
        # Fresh scan → enter update mode so user can adjust weight
        state.set_val("weight_grams",  item.get("weight_grams", 500))
        state.set_val("encoder_mode",  "update")
        state.set_val("active_qr_id",  qr_id)
        state.set_val("display_mode",  "update")
        state.write_state_file()
        if ui_ref:
            ui_ref.set_status(f"Updating {qr_id} — adjust weight then press ENC1", "#d29922")

# ══════════════════════════════════════════════════════════════════════════════
# ENCODER STATE SYNC
# ══════════════════════════════════════════════════════════════════════════════
def _sync_encoder_state_from_json():
    """
    Pull weight_grams and selected_item from the JSON state file into
    hdmi_display's in-process _state.

    small_display patches these keys directly into the JSON using
    _patch_state_file().  hdmi_display's in-process _state is never touched by
    those patches, so state.get("weight_grams") and state.get("selected_item")
    would otherwise always return stale startup defaults — breaking the HDMI
    UI labels AND print_qr().

    MUST be called immediately before any write_state_file() call that fires
    while encoder_mode is "setup" or "update".  Without syncing first,
    write_state_file() serialises the stale in-process values back to JSON,
    overwriting small_display's most recent encoder patch (the "bulldozer"
    race that made item changes appear to be ignored).
    """
    try:
        _raw = state.read_state_file()
        _w = _raw.get("weight_grams")
        _i = _raw.get("selected_item")
        if _w is not None:
            state.set_val("weight_grams",  _w)
        if _i is not None:
            state.set_val("selected_item", _i)
    except Exception as _e:
        log.warning("_sync_encoder_state_from_json: %s", _e)


# ══════════════════════════════════════════════════════════════════════════════
# CORE LOGIC
# ══════════════════════════════════════════════════════════════════════════════
def print_qr(ui_ref=None):
    global printer_ok
    db = state.get_food_db()
    if len(db) >= MAX_ITEMS:
        messagebox.showwarning("Full", f"Maximum {MAX_ITEMS} items reached.")
        return

    item_name  = state.get("selected_item")
    weight     = state.get("weight_grams")
    shelf_life = state.ITEMS.get(item_name, 60)
    qr_id      = state.next_qr_id(item_name)
    now        = datetime.now()
    expiry_dt  = now + timedelta(seconds=shelf_life)

    db[qr_id] = {
        "id":           qr_id,
        "item":         item_name,
        "weight_grams": weight,
        "shelf_life":   shelf_life,
        "created":      now,
        "expiry":       expiry_dt,
        "expired":      False,
    }
    state.update_food_db(db)

    # Exit setup mode
    state.set_val("encoder_mode", None)
    state.set_val("display_mode", "printing")
    state.write_state_file()

    if ui_ref:
        ui_ref.set_status(f"Printing {qr_id}...", "#d29922")

    if not printer_ok:
        _init_printer()

    if printer_ok:
        try:
            printer.set(align="center")
            printer.qr(qr_id, native=True, size=8)
            printer.text(
                f"\nITEM  : {item_name}\n"
                f"WEIGHT: {weight}g\n"
                f"SHELF : {shelf_life}s\n"
                f"ID    : {qr_id}\n"
                f"TIME  : {now.strftime('%H:%M:%S')}\n"
            )
            printer.ln(4)
            printer.cut()
            printer.device.flush()
            log.info("Printed: %s", qr_id)
            if ui_ref:
                ui_ref.set_status(f"Printed {qr_id} ✓", "#22c55e")
        except Exception as e:
            printer_ok = False
            log.error("Print error: %s", e)
            if ui_ref:
                ui_ref.set_status(f"Printer error: {e}", "#ef4444")
    else:
        if ui_ref:
            ui_ref.set_status("QR saved — printer offline", "#f59e0b")

    threading.Timer(2.0, lambda: (
        state.set_val("display_mode", "idle"),
        state.write_state_file()
    )).start()


def remove_item(qr_id: str, ui_ref=None):
    db = state.get_food_db()
    if qr_id not in db:
        return
    was_expired = db[qr_id]["expired"]
    del db[qr_id]
    state.clear_buzzer_notified(qr_id)
    state.update_food_db(db)
    mode   = "expired_scan" if was_expired else "fresh_scan"
    colour = "#ef4444" if was_expired else "#22c55e"
    label  = "EXPIRED removed" if was_expired else f"{qr_id} removed ✓"
    state.set_val("display_mode", mode)
    state.set_val("encoder_mode", None)
    state.set_val("active_qr_id", None)
    state.write_state_file()
    if ui_ref:
        ui_ref.set_status(label, colour)
    log.info("Removed %s", qr_id)
    threading.Timer(2.0, lambda: (
        state.set_val("display_mode", "idle"),
        state.write_state_file()
    )).start()

# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND THREADS
# ══════════════════════════════════════════════════════════════════════════════
def check_expiry_loop():
    """
    Runs every second. Marks items expired and triggers the buzzer.

    Re-ring logic:
      - never_buzzed  → ring immediately
      - rering_ok     → ring again only if BOTH:
          (a) BUZZER_RERING seconds have passed since last ring, AND
          (b) BUZZER_RERING seconds have passed since the buzzer was
              manually silenced (so pressing silence gives a full 60s
              of quiet, not just until the next 60s ring window)
    """
    last_buzz_time = {}    # qr_id → time of last buzz (for re-ring)
    while True:
        try:
            now    = datetime.now()
            db     = state.get_food_db()
            changed  = False
            to_buzz  = []
            now_ts   = time.time()

            for qr_id, item in db.items():
                # Parse expiry safely — may be datetime or ISO string after restart
                _exp = (item["expiry"] if isinstance(item["expiry"], datetime)
                        else datetime.fromisoformat(str(item["expiry"])))
                if not item["expired"] and now >= _exp:
                    item["expired"] = True
                    changed = True

                if item["expired"]:
                    last_t       = last_buzz_time.get(qr_id, 0)
                    silenced     = _buzzer_silenced_at[0]
                    never_buzzed = (last_t == 0)
                    # Both the last ring AND the last silence must be old enough
                    rering_ok    = (
                        (now_ts - last_t   >= BUZZER_RERING) and
                        (now_ts - silenced >= BUZZER_RERING)
                    )
                    if never_buzzed or rering_ok:
                        to_buzz.append(qr_id)

            if changed:
                state.update_food_db(db)
                if state.get("display_mode") == "idle":
                    state.set_val("display_mode", "timeout")
                state.write_state_file()
                threading.Timer(5.0, lambda: (
                    state.set_val("display_mode", "idle")
                    if state.get("display_mode") == "timeout" else None,
                    state.write_state_file()
                )).start()

            for qr_id in to_buzz:
                state.mark_buzzer_notified(qr_id)
                last_buzz_time[qr_id] = time.time()
                trigger_buzzer()

        except Exception as e:
            log.error("expiry_loop: %s", e)
        time.sleep(1)


def proximity_loop(ui_ref=None):
    prev = None
    while True:
        try:
            s1 = GPIO.input(PIN_SENSOR) == GPIO.LOW
            time.sleep(0.02)
            s2 = GPIO.input(PIN_SENSOR) == GPIO.LOW
            detected = s1 and s2

            if detected != prev:
                prev = detected
                state.set_val("proximity_detected", detected)
                GPIO.output(PIN_LED, GPIO.HIGH if detected else GPIO.LOW)

                if detected:
                    state.set_val("display_mode", "camera")
                    if ui_ref:
                        ui_ref.root.after(0, lambda: ui_ref.set_status(
                            "Object detected — scanning...", "#00d4aa"))
                else:
                    if state.get("display_mode") == "camera":
                        state.set_val("display_mode", "idle")
                    if ui_ref:
                        ui_ref.root.after(0, lambda: ui_ref.set_status(
                            "Ready", "#9ca3af"))

                state.write_state_file()
        except Exception as e:
            log.error("proximity_loop: %s", e)
        time.sleep(0.05)

# ══════════════════════════════════════════════════════════════════════════════
# GUI
# ══════════════════════════════════════════════════════════════════════════════
class FIFOApp:
    BG      = "#0d1117"
    HDR     = "#161b22"
    PANEL   = "#161b22"
    ACCENT  = "#00d4aa"
    TEXT    = "#f0f6fc"
    SUB     = "#8b949e"
    GREEN   = "#22c55e"
    RED     = "#ef4444"
    ORANGE  = "#f59e0b"
    AMBER   = "#eab308"

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("FIFO Tracking System")
        root.attributes("-fullscreen", True)   # true fullscreen — covers entire display
        root.configure(bg=self.BG)

        self._build_header()
        self._build_left()
        self._build_right()
        self._build_statusbar()
        self._refresh_ui()

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self.root, bg=self.HDR, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        tk.Label(hdr, text="▶", bg=self.HDR, fg=self.ACCENT,
                 font=("Courier", 13, "bold")).pack(side="left", padx=(10, 4))
        tk.Label(hdr, text="FIFO INVENTORY TRACKING",
                 bg=self.HDR, fg=self.TEXT,
                 font=("Courier", 14, "bold")).pack(side="left")

        self.clock_lbl = tk.Label(hdr, bg=self.HDR, fg=self.SUB,
                                   font=("Courier", 10))
        self.clock_lbl.pack(side="right", padx=12)
        self._tick_clock()

        self.prox_dot = tk.Label(hdr, text="●", bg=self.HDR,
                                  fg=self.SUB, font=("Arial", 11))
        self.prox_dot.pack(side="right", padx=(0, 4))
        tk.Label(hdr, text="SENSOR", bg=self.HDR, fg=self.SUB,
                 font=("Courier", 9)).pack(side="right")

        self.buzzer_lbl = tk.Label(hdr, text="🔔 ALARM", bg=self.HDR,
                                    fg=self.RED, font=("Courier", 10, "bold"))

    def _tick_clock(self):
        self.clock_lbl.config(text=datetime.now().strftime("%d-%b-%Y  %H:%M:%S"))
        self.root.after(1000, self._tick_clock)

    # ── Left: camera + controls ───────────────────────────────────────────────
    def _build_left(self):
        left = tk.Frame(self.root, bg=self.BG, width=340)
        left.pack(side="left", fill="y", padx=(8, 0), pady=4)
        left.pack_propagate(False)

        # Camera
        cam_border = tk.Frame(left, bg=self.ACCENT, padx=2, pady=2)
        cam_border.pack(pady=(2, 6))
        self.cam_lbl = tk.Label(cam_border, bg="black")
        self.cam_lbl.config(width=316, height=220)
        self.cam_lbl.pack()

        # Encoder status panel
        enc_frame = tk.Frame(left, bg=self.PANEL, padx=8, pady=6)
        enc_frame.pack(fill="x", pady=(0, 6))

        tk.Label(enc_frame, text="ENCODER SELECTION",
                 bg=self.PANEL, fg=self.ACCENT,
                 font=("Courier", 9, "bold")).pack(anchor="w")

        self.item_lbl = tk.Label(enc_frame, text="Item   : —",
                                  bg=self.PANEL, fg=self.TEXT,
                                  font=("Courier", 11))
        self.item_lbl.pack(anchor="w")

        self.weight_lbl = tk.Label(enc_frame, text="Weight : —",
                                    bg=self.PANEL, fg=self.TEXT,
                                    font=("Courier", 11))
        self.weight_lbl.pack(anchor="w")

        self.enc_mode_lbl = tk.Label(enc_frame, text="Mode   : IDLE",
                                      bg=self.PANEL, fg=self.SUB,
                                      font=("Courier", 10))
        self.enc_mode_lbl.pack(anchor="w")

        # Buttons
        btn_frame = tk.Frame(left, bg=self.BG)
        btn_frame.pack(fill="x")

        self.print_btn = tk.Button(
            btn_frame,
            text="SPACE → SETUP / PRINT QR",
            bg=self.ACCENT, fg="#000000",
            font=("Courier", 10, "bold"),
            relief="flat", pady=7, cursor="hand2",
            activebackground="#00b899",
            command=self._on_space,
        )
        self.print_btn.pack(fill="x", pady=(0, 4))

        self.esc_btn = tk.Button(
            btn_frame,
            text="ESC → REMOVE SCANNED ITEM",
            bg=self.RED, fg="#ffffff",
            font=("Courier", 10, "bold"),
            relief="flat", pady=7, cursor="hand2",
            activebackground="#c53030",
            command=self._on_escape,
        )
        self.esc_btn.pack(fill="x", pady=(0, 4))

        self.printer_lbl = tk.Label(
            btn_frame,
            text="PRINTER: CHECKING...",
            bg=self.BG, fg=self.SUB,
            font=("Courier", 9)
        )
        self.printer_lbl.pack(anchor="w")

    # ── Right: item table ─────────────────────────────────────────────────────
    def _build_right(self):
        right = tk.Frame(self.root, bg=self.BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 8), pady=4)

        self.alarm_lbl = tk.Label(right, text="● NORMAL",
                                   bg=self.BG, fg=self.GREEN,
                                   font=("Courier", 13, "bold"))
        self.alarm_lbl.pack(anchor="w", pady=(0, 4))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("F.Treeview",
                         background=self.PANEL, foreground=self.TEXT,
                         rowheight=30, fieldbackground=self.PANEL,
                         font=("Courier", 11), borderwidth=0)
        style.configure("F.Treeview.Heading",
                         background=self.HDR, foreground=self.ACCENT,
                         font=("Courier", 11, "bold"), relief="flat")
        style.map("F.Treeview", background=[("selected", "#1f3a5f")])

        # Wrap treeview + scrollbar together so scrollbar stays attached
        tree_frame = tk.Frame(right, bg=self.BG)
        tree_frame.pack(fill="both", expand=True)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("id", "item", "weight", "shelf", "timeleft", "status"),
            show="headings", style="F.Treeview",
            yscrollcommand=scrollbar.set
        )
        # height= is NOT set — let fill="both"/expand=True control the height
        scrollbar.config(command=self.tree.yview)

        cols = [
            ("id",       "QR ID",       200, "w"),
            ("item",     "Item",        120, "center"),
            ("weight",   "Weight",       90, "center"),
            ("shelf",    "Shelf(s)",     80, "center"),
            ("timeleft", "Time Left",    90, "center"),
            ("status",   "Status",       90, "center"),
        ]
        for col, heading, width, anchor in cols:
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, minwidth=width, anchor=anchor)

        self.tree.tag_configure("active",  foreground=self.GREEN)
        self.tree.tag_configure("warning", foreground=self.ORANGE)
        self.tree.tag_configure("expired", foreground=self.RED)
        self.tree.pack(side="left", fill="both", expand=True)

        self.summary_lbl = tk.Label(
            right, text="Items: 0   Active: 0   Expired: 0",
            bg=self.BG, fg=self.SUB, font=("Courier", 10)
        )
        self.summary_lbl.pack(anchor="w", pady=(4, 0))

    # ── Status bar ────────────────────────────────────────────────────────────
    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=self.HDR, height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_lbl = tk.Label(bar, text="SYSTEM READY",
                                    bg=self.HDR, fg=self.GREEN,
                                    font=("Courier", 10))
        self.status_lbl.pack(side="left", padx=10)

    def set_status(self, text, color=None):
        self.root.after(0, lambda: self.status_lbl.config(
            text=text, fg=(color or self.TEXT)))

    # ── Keyboard bindings ─────────────────────────────────────────────────────
    def bind_keys(self):
        self.root.bind("<space>",  self._on_space)
        self.root.bind("<Delete>", self._on_delete)
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind_all("<space>",  self._on_space)
        self.root.bind_all("<Delete>", self._on_delete)
        self.root.bind_all("<Escape>", self._on_escape)
        self.root.bind_all("<KeyPress-space>",  self._on_space)
        self.root.bind_all("<KeyPress-Delete>", self._on_delete)
        self.root.bind_all("<KeyPress-Escape>", self._on_escape)
        # Q / q → graceful shutdown (exits fullscreen and stops mainloop)
        self.root.bind_all("<KeyPress-q>", self._on_quit)
        self.root.bind_all("<KeyPress-Q>", self._on_quit)
        self.root.focus_force()
        self.root.after(200,  self.root.focus_force)
        self.root.after(600,  self.root.focus_force)
        self.root.after(1200, self.root.focus_force)
        self.root.after(2500, self.root.focus_force)

    def _on_quit(self, event=None):
        """Q — exit fullscreen and close the application."""
        log.info("Q pressed — shutting down")
        self.root.attributes("-fullscreen", False)
        self.root.destroy()

    def _on_space(self, event=None):
        """SPACE — toggle setup mode or confirm+print."""
        if state.get("buzzer_active"):
            silence_buzzer()
        enc_mode = state.get("encoder_mode")
        if enc_mode is None:
            state.set_val("encoder_mode", "setup")
            state.set_val("display_mode", "setup")
            # Sync encoder keys FROM JSON before writing state back, so we never
            # overwrite weight_grams/selected_item patches from small_display.
            _sync_encoder_state_from_json()
            state.write_state_file()
            self.set_status("Setup mode — use encoders, SPACE to print", "#00d4aa")
        elif enc_mode == "setup":
            # Sync once more before print_qr reads selected_item / weight_grams
            _sync_encoder_state_from_json()
            print_qr(ui_ref=self)

    def _on_delete(self, event=None):
        """DELETE — remove selected item from table, or active_qr_id."""
        if state.get("buzzer_active"):
            silence_buzzer()
        selected = self.tree.selection()
        if selected:
            qr_id = self.tree.item(selected[0])["values"][0]
            remove_item(qr_id, ui_ref=self)
        else:
            qr_id = state.get("active_qr_id")
            if qr_id:
                remove_item(qr_id, ui_ref=self)

    def _on_escape(self, event=None):
        """
        ESC — remove the active_qr_id (last scanned item) from the database
        entirely, regardless of its expiry status.

        Use case: after scanning a QR in update/camera mode you decide the item
        should be discarded immediately.  ESC pulls it from food_db and resets
        encoder/display state back to idle — exactly like scanning an expired
        QR but triggered by keyboard instead of the scan result.

        Priority order:
          1. If an item is highlighted in the table → remove that one.
          2. Else if active_qr_id is set (last scan) → remove that.
          3. Else if encoder_mode is "setup" → cancel setup, return to idle.
        """
        if state.get("buzzer_active"):
            silence_buzzer()
        # Priority 1: table row highlighted
        selected = self.tree.selection()
        if selected:
            qr_id = self.tree.item(selected[0])["values"][0]
            remove_item(qr_id, ui_ref=self)
            self.set_status(f"ESC: {qr_id} removed from database", "#ef4444")
            return
        # Priority 2: last scanned item still active
        qr_id = state.get("active_qr_id")
        if qr_id:
            remove_item(qr_id, ui_ref=self)
            self.set_status(f"ESC: {qr_id} removed from database", "#ef4444")
            return
        # Priority 3: cancel setup mode without printing
        if state.get("encoder_mode") == "setup":
            state.set_val("encoder_mode", None)
            state.set_val("display_mode", "idle")
            state.write_state_file()
            self.set_status("Setup cancelled — ESC pressed", "#9ca3af")

    # ── UI refresh ────────────────────────────────────────────────────────────
    def _refresh_ui(self):
        try:
            db   = state.get_food_db()
            now  = datetime.now()
            self.tree.delete(*self.tree.get_children())
            expired_count = 0

            for qr_id, item in sorted(db.items()):
                _exp = (item["expiry"] if isinstance(item["expiry"], datetime)
                        else datetime.fromisoformat(str(item["expiry"])))
                secs = int((_exp - now).total_seconds())
                w    = item.get("weight_grams", 0)
                sh   = item.get("shelf_life",  60)

                if item["expired"]:
                    tag, status, secs_str = "expired", "EXPIRED", "0"
                    expired_count += 1
                elif secs <= 15:
                    tag, status, secs_str = "warning", "WARNING", str(secs)
                else:
                    tag, status, secs_str = "active",  "ACTIVE",  str(secs)

                self.tree.insert("", "end",
                    values=(qr_id, item["item"], f"{w}g", f"{sh}s",
                            secs_str, status),
                    tags=(tag,))

            total  = len(db)
            active = total - expired_count

            self.alarm_lbl.config(
                text="● EXPIRED" if expired_count > 0 else "● NORMAL",
                fg=self.RED if expired_count > 0 else self.GREEN
            )
            self.summary_lbl.config(
                text=f"Items: {total}   Active: {active}   Expired: {expired_count}"
            )

            enc_mode = state.get("encoder_mode")
            item_sel = state.get("selected_item") or "—"
            weight   = state.get("weight_grams")  or 0
            mode_str = (enc_mode or "IDLE").upper()

            self.item_lbl.config(  text=f"Item   : {item_sel}")
            self.weight_lbl.config(text=f"Weight : {weight}g")
            self.enc_mode_lbl.config(
                text=f"Mode   : {mode_str}",
                fg=self.AMBER if enc_mode else self.SUB
            )

            prox = state.get("proximity_detected")
            self.prox_dot.config(fg=self.GREEN if prox else self.SUB)

            if state.get("buzzer_active"):
                self.buzzer_lbl.pack(side="right", padx=10)
            else:
                self.buzzer_lbl.pack_forget()

            self.printer_lbl.config(
                text=f"PRINTER: {'READY' if printer_ok else 'OFFLINE'}",
                fg=self.GREEN if printer_ok else self.RED
            )

        except Exception as e:
            log.error("refresh_ui: %s", e)
        finally:
            # Reschedule in finally — guarantees the loop NEVER dies silently
            # even if an exception fires halfway through the refresh above.
            self.root.after(500, self._refresh_ui)

    def update_camera(self, frame_bgr):
        try:
            rgb   = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            img   = Image.fromarray(rgb).resize((316, 220))
            imgtk = ImageTk.PhotoImage(img)
            self.cam_lbl.imgtk = imgtk
            self.cam_lbl.config(image=imgtk)
        except Exception as e:
            log.warning("update_camera: %s", e)

    def clear_camera(self):
        try:
            blank = Image.new("RGB", (316, 220), (0, 0, 0))
            imgtk = ImageTk.PhotoImage(blank)
            self.cam_lbl.imgtk = imgtk
            self.cam_lbl.config(image=imgtk)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    _gpio_init()
    _init_printer()
    _start_camera()

    state.set_val("display_mode", "idle")
    state.set_val("encoder_mode", None)
    state.set_val("buzzer_silenced_request", False)
    state.write_state_file(force=True)

    root = tk.Tk()
    ui   = FIFOApp(root)
    ui.bind_keys()

    threading.Thread(target=check_expiry_loop,          daemon=True).start()
    threading.Thread(target=proximity_loop, args=(ui,), daemon=True).start()

    ui.set_status("SYSTEM READY — Press SPACE to begin", "#00d4aa")

    frame_count = [0]

    def camera_loop():
        # Sync encoder-driven keys every tick so in-process state stays current.
        _sync_encoder_state_from_json()

        # ── FIX: poll buzzer silence request and clear the flag immediately ───
        # Previously the flag was read but never cleared, so silence_buzzer()
        # would be called on every camera_loop tick until hdmi overwrote state.
        if state.get("buzzer_silenced_request"):
            state.set_val("buzzer_silenced_request", False)
            state.write_state_file()
            silence_buzzer()

        # ── Poll weight confirm request from small_display ENC1 SW ───────────
        # small_display cannot safely write food_db (its copy is always stale).
        # Instead it sets weight_confirm_request=True and writes weight_grams.
        # hdmi_display applies the weight to its live food_db here.
        if state.get("weight_confirm_request"):
            state.set_val("weight_confirm_request", False)
            qr_id  = state.get("active_qr_id")
            weight = state.get("weight_grams")
            if qr_id and weight:
                db = state.get_food_db()
                if qr_id in db:
                    db[qr_id]["weight_grams"] = weight
                    state.update_food_db(db)
                    log.info("Weight applied: %s → %dg", qr_id, weight)
            # encoder_mode, active_qr_id, display_mode already set by small_display
            state.write_state_file(force=True)

        sensor_on = GPIO.input(PIN_SENSOR) == GPIO.LOW   # NC sensor: LOW = object detected

        if sensor_on:
            if camera is None:
                _start_camera()

            if camera is not None:
                ret, frame_bgr = camera.read()
                if ret:
                    frame_count[0] += 1
                    if frame_count[0] % 5 == 0:
                        process_frame(frame_bgr, ui_ref=ui)
                    ui.update_camera(frame_bgr)
                    _write_frame(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
                else:
                    ui.clear_camera()
            else:
                ui.clear_camera()
        else:
            ui.clear_camera()

        root.after(30, camera_loop)

    def _force_focus():
        root.focus_force()
    root.after(100,  _force_focus)
    root.after(500,  _force_focus)
    root.after(1000, _force_focus)
    root.after(2000, _force_focus)

    camera_loop()

    try:
        root.mainloop()
    finally:
        # GPIO.cleanup() is ONLY here — at final shutdown.
        # Never call it at startup; it would reset small_display's encoder pins.
        GPIO.output(PIN_BUZZER, GPIO.LOW)
        GPIO.output(PIN_LED,    GPIO.LOW)
        GPIO.cleanup()
        if camera:
            camera.release()
        log.info("Shutdown complete")

if __name__ == "__main__":
    main()
