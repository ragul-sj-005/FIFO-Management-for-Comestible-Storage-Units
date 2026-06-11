"""
shared_state.py  —  v4  (final, ready-to-use)
══════════════════════════════════════════════════════════════════════════════
Single source of truth shared between hdmi_display.py and small_display.py.
Both programs import this module.  hdmi_display.py uses it in-process;
small_display.py reads the JSON file hdmi_display writes to disk.

IPC paths
──────────
  STATE_FILE  /tmp/fifo_state.json       — JSON, written atomically
  FRAME_FILE  /dev/shm/fifo_frame.npy   — camera frame stored in RAM
                                           (zero SD card wear, ~0.1 ms writes)

State keys
──────────
  food_db                  dict  qr_id → item dict
  display_mode             str   startup | idle | setup | update | camera |
                                 printing | fresh_scan | expired_scan | timeout
  proximity_detected       bool
  buzzer_active            bool
  qr_counter               int   auto-increment for QR IDs
  selected_item            str   current encoder-2 selection
  weight_grams             int   current encoder-1 selection
  encoder_mode             str   "setup" | "update" | None
  active_qr_id             str   qr_id being updated in update mode
  buzzer_notified          set   qr_ids that have already triggered the buzzer
  buzzer_silenced_request  bool  flag set by small_display, polled by hdmi_display

Pi 5 note
──────────
  RPi.GPIO does not fully support Pi 5.
  Install the drop-in shim:  pip install rpi-lgpio
  No code changes needed — it replaces RPi.GPIO transparently.
"""

import copy
import json
import logging
import os
import threading
from datetime import datetime

logging.basicConfig(
    filename="/tmp/fifo_errors.log",
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fifo")

# ── IPC paths ──────────────────────────────────────────────────────────────────
STATE_FILE = "/tmp/fifo_state.json"
FRAME_FILE = "/dev/shm/fifo_frame.npy"   # RAM-backed — zero SD card wear

# ── Item catalogue  {name: shelf_life_seconds} ────────────────────────────────
ITEMS = {
    "CHICKEN":  60,
    "RICE":     60,
    "MUTTON":   60,
    "FISH":     60,
    "MUSHROOM": 60,
    "PANEER":   60,
}
ITEM_NAMES = list(ITEMS.keys())   # ordered list for encoder cycling

# ── Weight config ──────────────────────────────────────────────────────────────
WEIGHT_MIN  = 100
WEIGHT_MAX  = 5000
WEIGHT_STEP = 100

# ── Internal lock ──────────────────────────────────────────────────────────────
_lock = threading.Lock()

# ── Master in-process state ────────────────────────────────────────────────────
_state = {
    "food_db":                 {},
    "display_mode":            "startup",
    "proximity_detected":      False,
    "buzzer_active":           False,
    "qr_counter":              1,
    "selected_item":           ITEM_NAMES[0],
    "weight_grams":            500,
    "encoder_mode":            None,
    "active_qr_id":            None,
    "buzzer_notified":         set(),
    "buzzer_silenced_request": False,
    "_last_written":           None,
}


# ══════════════════════════════════════════════════════════════════════════════
# THREAD-SAFE ACCESSORS
# ══════════════════════════════════════════════════════════════════════════════

def get(key):
    with _lock:
        return _state.get(key)

def set_val(key, value):
    with _lock:
        _state[key] = value

def get_food_db():
    with _lock:
        return copy.deepcopy(_state["food_db"])

def update_food_db(db):
    with _lock:
        _state["food_db"] = copy.deepcopy(db)

def next_qr_id(item_name):
    with _lock:
        idx = _state["qr_counter"]
        _state["qr_counter"] += 1
        return f"{item_name}_{idx}"

def alarm_active():
    with _lock:
        return any(item["expired"] for item in _state["food_db"].values())


# ── Buzzer notification helpers ───────────────────────────────────────────────

def mark_buzzer_notified(qr_id: str):
    with _lock:
        _state["buzzer_notified"].add(qr_id)

def is_buzzer_notified(qr_id: str) -> bool:
    with _lock:
        return qr_id in _state["buzzer_notified"]

def clear_buzzer_notified(qr_id: str):
    with _lock:
        _state["buzzer_notified"].discard(qr_id)

def clear_all_buzzer_notified():
    """Called when user silences buzzer — allows re-ring after BUZZER_RERING s."""
    with _lock:
        _state["buzzer_notified"].clear()


# ── Encoder helpers ───────────────────────────────────────────────────────────

def next_item():
    """Encoder 2 clockwise — advance item selection."""
    with _lock:
        idx = ITEM_NAMES.index(_state["selected_item"])
        _state["selected_item"] = ITEM_NAMES[(idx + 1) % len(ITEM_NAMES)]

def prev_item():
    """Encoder 2 counter-clockwise — go back in item list."""
    with _lock:
        idx = ITEM_NAMES.index(_state["selected_item"])
        _state["selected_item"] = ITEM_NAMES[(idx - 1) % len(ITEM_NAMES)]

def increase_weight():
    with _lock:
        _state["weight_grams"] = min(_state["weight_grams"] + WEIGHT_STEP, WEIGHT_MAX)

def decrease_weight():
    with _lock:
        _state["weight_grams"] = max(_state["weight_grams"] - WEIGHT_STEP, WEIGHT_MIN)


# ══════════════════════════════════════════════════════════════════════════════
# IPC — JSON STATE FILE  (atomic write via tmp → replace)
# ══════════════════════════════════════════════════════════════════════════════

def _serialize_db(db):
    out = {}
    for k, v in db.items():
        out[k] = {
            "id":           v["id"],
            "item":         v["item"],
            "weight_grams": v.get("weight_grams", 0),
            "shelf_life":   v.get("shelf_life", 60),
            "created":      v["created"].isoformat() if isinstance(v["created"], datetime) else str(v["created"]),
            "expiry":       v["expiry"].isoformat()  if isinstance(v["expiry"],  datetime) else str(v["expiry"]),
            "expired":      v["expired"],
        }
    return out

def _build_payload():
    return {
        "display_mode":            _state["display_mode"],
        "proximity_detected":      _state["proximity_detected"],
        "buzzer_active":           _state["buzzer_active"],
        "food_db":                 _serialize_db(_state["food_db"]),
        "selected_item":           _state["selected_item"],
        "weight_grams":            _state["weight_grams"],
        "encoder_mode":            _state["encoder_mode"],
        "active_qr_id":            _state["active_qr_id"],
        "buzzer_silenced_request": _state["buzzer_silenced_request"],
    }

def write_state_file(force: bool = False):
    """
    Write state to /tmp/fifo_state.json atomically.
    Uses tmp + os.replace() so small_display never reads a half-written file.
    Skips the write entirely if nothing has changed (unless force=True).
    """
    with _lock:
        payload     = _build_payload()
        payload_str = json.dumps(payload, sort_keys=True)
        if not force and _state["_last_written"] == payload_str:
            return
        _state["_last_written"] = payload_str
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(payload_str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error("write_state_file: %s", e)

def read_state_file():
    """
    Read and return the JSON state file as a dict.
    Returns a safe default dict on any read/parse failure so callers
    never receive None and never crash on .get() calls.
    """
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log.error("read_state_file failed: %s", e)
        return {
            "display_mode":  "startup",
            "food_db":       {},
            "selected_item": ITEM_NAMES[0],
            "weight_grams":  500,
            "encoder_mode":  None,
            "active_qr_id":  None,
        }
