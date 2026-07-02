# NOTE TO ANYBODY TRYING TO SELL THIS CODE (for some reason, its so bad vro)
# The source is under a AGPL-3.0 License, although I suspect you know that.
# If you try to sell this code, release it as closed source, or don't give credit, I will take it down.

import sys
import time
import threading
import keyboard
import cv2
import numpy as np
import bettercam
import ctypes
from colorama import Fore, Back, Style
import os
import json
import logging
import queue
import win32api
import win32gui
import win32con
from src.hold_detector import lane_strip_fill, apply_hysteresis, detect_note_bars, detect_gems, detect_beam_heights
from src.sustain_controller import SustainController
from src.lookahead_controller import LookaheadController
from src.note_queue import NoteEventQueue, GemTrackQueue
from src.settings import load_config, scale_config

# File logger for runtime monitoring/diagnostics (separate from the colored console UI).
# Millisecond timestamps + thread name matter here: presses fire from worker threads
# and held/dragged-note timing only makes sense when we can see press/release intervals.
# One timestamped file per session so past runs stay comparable side by side.
log = logging.getLogger("note-player")
log.setLevel(logging.DEBUG)
log.propagate = False  # keep noisy library loggers (bettercam) out of our file
os.makedirs("logs", exist_ok=True)
_log_path = os.path.join("logs", time.strftime("note-player-%Y-%m-%d-%H-%M-%S.log"))
_handler = logging.FileHandler(_log_path, mode="w", encoding="utf-8")
_handler.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
))
log.addHandler(_handler)

# Set console title
os.system("title note-player console")

# Clear the console
def cls():
    os.system('cls' if os.name == 'nt' else 'clear')
cls()

# Load configuration (user_settings.json over tuned engine defaults, src/settings.py)
try:
    config = load_config()
except Exception as e:
    print(Back.RED + Fore.WHITE + f"ERROR: Failed to load configuration. {str(e)}" + Back.RESET + Fore.RESET)
    exit()

monitor_width = ctypes.windll.user32.GetSystemMetrics(0)
monitor_height = ctypes.windll.user32.GetSystemMetrics(1)
# px/velocity values are tuned on a 1440p highway; map them to this monitor so the
# validated geometry ratios hold at any resolution (identity at 1440p). Scale EXACTLY
# once: a duplicated call compounds the factor (0.75 -> 0.5625 at 1080p) and shipped a
# 40%-slow scroll model to the first 1080p user -- a source-scan test now pins the
# single call site. Capture geometry below scales on its own 1080 anchor.
px_scale = monitor_height / 1440
config = scale_config(config, monitor_height)

hwnd = None   # console handle; stays None when the titled console window isn't found
if config.get("console_window_ontop") == "true":
    hwnd_list = []
    def findit(hwnd, ctx):
        if win32gui.GetWindowText(hwnd).find("note-player console") != -1:
            hwnd_list.append(hwnd)
    win32gui.EnumWindows(findit, None)
    if len(hwnd_list) == 1:
        hwnd = hwnd_list[0]
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception as e:
            print(Back.RED + Fore.WHITE + f"ERROR: {str(e)} while setting window position." + Back.RESET + Fore.RESET)
    else:
        print(Back.RED + Fore.WHITE + "ERROR: Console window handle not found or multiple windows detected." + Back.RESET + Fore.RESET)

# START
if not config["always_single_lanemode"] == "true":
    number_of_lanes = int(input("Number of lanes (4 = easy-hard, 5 = expert): "))
else:
    number_of_lanes = config["single_lanemode_lanes"]

assert number_of_lanes in [4, 5], Back.RED + Fore.WHITE + "Number of lanes must be 4 or 5" + Back.RESET + Fore.RESET

lane_keys = " ".join(config[f"key_{i}"] for i in range(1, number_of_lanes + 1))
assets_ok = os.path.isdir(os.path.join("assets", str(monitor_height)))
diagnostics_on = config["diagnostic_capture"] == "true" or config["log_detection"] == "true"

_BAR = Fore.LIGHTBLACK_EX + "=" * 62 + Fore.RESET

def _row(label, value):
    print(f"   {Fore.YELLOW}{label:<13}{Fore.RESET}{value}")

print(f"""
{_BAR}
   {Fore.CYAN}{Style.BRIGHT}N O T E - P L A Y E R{Style.RESET_ALL}
   {Fore.LIGHTBLACK_EX}rhythm-game autoplayer  |  AGPL-3 licensed{Fore.RESET}
{_BAR}""")
_row("Resolution", f"{monitor_width}x{monitor_height}"
     + (Fore.GREEN + "  (match images found)" + Fore.RESET if assets_ok
        else Back.RED + Fore.WHITE + "  NO match images for this height!" + Back.RESET + Fore.RESET))
_row("Lanes", f"{number_of_lanes}  (keys: {lane_keys})")
_row("Autoplay", "starts " + Fore.RED + "OFF" + Fore.RESET + "  -  press CAPS LOCK to start/stop")
_row("Exit", f"press {str.upper(config['exit_key'])}")
_row("Diagnostics", Fore.MAGENTA + "ON  (writing diag/ and logs/)" + Fore.RESET if diagnostics_on else "off")
print(_BAR + "\n")

if not assets_ok:
    exit()

# 1080p values:
region_width = 555 if number_of_lanes == 4 else 696 # width of the capture region
region_height = 180 # height of the capture region
height_offset = 190 # higher number is looking higher :O
# scale values
scale_factor = 1080 / monitor_height
min_tile_pixels_top_offset = int(config["min_tile_pixels_top_offset_scaled"] // scale_factor)
if scale_factor != 1:
    region_width = int(region_width // scale_factor)
    region_height = int(region_height // scale_factor)
    height_offset = int(height_offset // scale_factor)

region_fromleft = int(((monitor_width - region_width) // 2) + (8 // scale_factor) if number_of_lanes == 5 else ((monitor_width - region_width) // 2) + 1) # higher offset is looking more right. idk why this is needed since it should be centered. the game is slightly to the right?
region_fromtop = (monitor_height - region_height) - height_offset
width = region_fromleft + region_width
height = region_fromtop + region_height
section_size = region_width // number_of_lanes

log.info(f"startup resolution={monitor_width}x{monitor_height} lanes={number_of_lanes} scale_factor={scale_factor} tuning_scale={px_scale:.3f}")
log.info(f"region left={region_fromleft} top={region_fromtop} w={region_width} h={region_height} section_size={section_size}")
log.info(f"hit zone y>={min_tile_pixels_top_offset} capture_fps={config['capture_fps']}")

# Diagnostic mode captures a region wider/taller than the lane band so saved frames
# include the HUD (accuracy badge on the left, multiplier below the frets). Detection
# still runs on the lane band, cropped back out at a fixed offset within the region.
diagnostic_on = config["diagnostic_capture"] == "true"
if diagnostic_on:
    diag_left = max(0, region_fromleft - 320)
    diag_top = max(0, region_fromtop - 120)
    diag_right = min(monitor_width, width + 320)
    diag_bottom = min(monitor_height, height + 260)
    band_x = region_fromleft - diag_left
    band_y = region_fromtop - diag_top
    capture_region = (diag_left, diag_top, diag_right, diag_bottom)
    log.info(f"DIAGNOSTIC region={capture_region} band_offset=({band_x},{band_y})")
else:
    capture_region = (region_fromleft, region_fromtop, width, height)
# region must go to create(), not only start(): bettercam resets a start()-only region
# to full screen on display-mode change, killing its capture thread on a shape mismatch
main_camera = bettercam.create(region=capture_region, output_color="GRAY", max_buffer_len=512)
main_camera.start(region=capture_region, target_fps=config["capture_fps"])

_last_frame_t = time.time()

def _relaunch(reason):
    relaunches = int(os.environ.get("NOTE_PLAYER_RELAUNCHES", "0"))
    if relaunches >= 5:
        return False
    log.error(f"{reason} -- relaunching to re-anchor geometry/templates/tuning")
    for k in hold_lane_keys:
        try:
            key_up(k)
        except Exception:
            pass
    os.environ["NOTE_PLAYER_RELAUNCHES"] = str(relaunches + 1)
    _handler.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)

def _capture_watchdog():
    global _last_frame_t
    fail_logged = budget_logged = False
    below_since = None
    while True:
        time.sleep(0.5)
        # relaunch on a LOWER mode only: tabbing out to a larger desktop mode keeps the
        # region valid, so no relaunch ping-pong on every alt-tab
        cur_h = ctypes.windll.user32.GetSystemMetrics(1)
        if cur_h < monitor_height:
            if below_since is None:
                below_since = time.time()
            elif time.time() - below_since >= 1.0:
                if not _relaunch(f"display mode height {cur_h} below startup {monitor_height}") and not budget_logged:
                    log.error("relaunch budget exhausted -- restart the bot manually while the game is fullscreen")
                    budget_logged = True
        else:
            below_since = None
        if time.time() - _last_frame_t < 3.0:
            continue
        log.warning("capture stalled >3s -- restarting camera")
        try:
            main_camera.stop()
        except Exception as e:
            log.warning(f"camera stop during restart: {e}")
        try:
            # a COMError death leaves the duplicator built for the old mode; start() alone
            # dies again instantly, so rebuild the D3D objects first
            main_camera._on_output_change()
            main_camera.start(region=capture_region, target_fps=config["capture_fps"])
            _last_frame_t = time.time()
            fail_logged = False
        except ValueError as e:
            if not _relaunch(f"capture region invalid for the current display mode ({e})") and not budget_logged:
                log.error("relaunch budget exhausted -- restart the bot manually while the game is fullscreen")
                budget_logged = True
        except Exception as e:
            if not fail_logged:
                log.error(f"camera restart failed (retrying every 3s): {e}")
                fail_logged = True

threading.Thread(target=_capture_watchdog, name="capture-watchdog", daemon=True).start()
boxed_screenshot = main_camera.get_latest_frame()
_last_frame_t = time.time()
os.environ["NOTE_PLAYER_RELAUNCHES"] = "0"

def detection_band(frame):
    if diagnostic_on:
        return frame[band_y:band_y + region_height, band_x:band_x + region_width]
    return frame

tile = cv2.imread(f'assets/{monitor_height}/tile{config["tile_filename_suffix"]}.png', cv2.IMREAD_GRAYSCALE)
tile_width = tile.shape[1]
tile_height = tile.shape[0]
tile = tile.astype(np.uint8)
if config["use_white_tile"] == "true":
    white_tile = cv2.imread(f'assets/{monitor_height}/white{config["tile_filename_suffix"]}.png', cv2.IMREAD_GRAYSCALE)
    white_tile_width = white_tile.shape[1]
    white_tile_height = white_tile.shape[0]
    white_tile = white_tile.astype(np.uint8)
if config["use_battlestage_tile"] == "true":
    battlestage_tile = cv2.imread(f'assets/{monitor_height}/battlestage{config["tile_filename_suffix"]}.png', cv2.IMREAD_GRAYSCALE)
    battlestage_tile_width = battlestage_tile.shape[1]
    battlestage_tile_height = battlestage_tile.shape[0]
    battlestage_tile = battlestage_tile.astype(np.uint8)
if config["use_diamond_tile"] == "true":
    diamond_tile = cv2.imread(f'assets/{monitor_height}/diamond{config["tile_filename_suffix"]}.png', cv2.IMREAD_GRAYSCALE)
    diamond_tile_width = diamond_tile.shape[1]
    diamond_tile_height = diamond_tile.shape[0]
    diamond_tile = diamond_tile.astype(np.uint8)
hold_lane_keys = [config[f"key_{i + 1}"] for i in range(number_of_lanes)]

tap_templates = [(tile, tile_width, tile_height, config["min_confidence"])]
if config["use_white_tile"] == "true":
    tap_templates.append((white_tile, white_tile_width, white_tile_height, config["white_tile_min_confidence"]))
if config["use_battlestage_tile"] == "true":
    tap_templates.append((battlestage_tile, battlestage_tile_width, battlestage_tile_height, config["battlestage_tile_min_confidence"]))
if config["use_diamond_tile"] == "true":
    tap_templates.append((diamond_tile, diamond_tile_width, diamond_tile_height, config["diamond_tile_min_confidence"]))

def key_down(key):
    log.debug(f"key down {key}")
    keyboard.press(key)

def key_up(key):
    log.debug(f"key up {key}")
    keyboard.release(key)

# Reactive fallback controller (press-and-hold-then-confirm at the fret strip).
sustain_controller = SustainController(key_down, key_up, config["hold_probe_ms"] / 1000.0, config["hold_release_grace_ms"] / 1000.0)
# Proactive look-ahead controller: schedule presses at fret-arrival from an upper strip.
use_lookahead = config["use_lookahead"] == "true"
lookahead_controller = LookaheadController(key_down, key_up, config["lookahead_lead_ms"] / 1000.0, config["hold_release_grace_ms"] / 1000.0)
# Event-queue engine: parse whole highway per frame -> predicted-arrival press events.
use_queue = config["use_queue"] == "true"
fret_y = int(region_height * 0.85)
note_top = 0
note_bottom = int(region_height * config["note_detect_bottom_fraction"])
note_queue = NoteEventQueue(key_down, key_up, fret_y, config["scroll_px_per_s"],
                            config["note_latency_ms"] / 1000.0, config["hold_release_grace_ms"] / 1000.0,
                            config["note_match_tol_ms"] / 1000.0,
                            min_regap_s=config["note_regap_ms"] / 1000.0)
# Gem-track engine: track each gem as a physical object by position -> one press per gem.
# Best for dense same-lane streams (L L L) where presence stays continuously lit. Takes priority.
use_gem_track = config["use_gem_track"] == "true"
gem_queue = GemTrackQueue(key_down, key_up, fret_y, config["scroll_px_per_s"],
                          config["note_latency_ms"] / 1000.0, config["hold_release_grace_ms"] / 1000.0,
                          match_px=config["gem_match_px"],
                          restrum_gap_s=config["gem_restrum_gap_ms"] / 1000.0,
                          min_press_interval_s=config["gem_min_press_interval_ms"] / 1000.0,
                          flash_suppress_s=config["gem_flash_suppress_ms"] / 1000.0,
                          tap_hold_s=config["gem_tap_hold_ms"] / 1000.0,
                          beam_confirm_px=config["gem_beam_min_height"],
                          beam_sustain_px=config["gem_beam_sustain_height"],
                          max_hold_s=config["gem_max_hold_ms"] / 1000.0,
                          flash_settle_s=config["gem_flash_settle_ms"] / 1000.0,
                          flash_zone_px=config["gem_flash_zone_px"],
                          flash_zone_s=config["gem_flash_zone_ms"] / 1000.0,
                          trail_margin_px=config["gem_trail_margin_px"],
                          trail_certain_px=config["gem_trail_certain_px"],
                          trail_early_s=config["gem_trail_early_ms"] / 1000.0,
                          lift_window_s=config["gem_lift_window_ms"] / 1000.0,
                          lift_fwd_s=config["gem_lift_fwd_ms"] / 1000.0,
                          promote_v_min=config["gem_promote_v_min"],
                          promote_v_max=config["gem_promote_v_max"],
                          provisional_ttl_s=config["gem_provisional_ttl_ms"] / 1000.0,
                          promote_min_span_s=config["gem_promote_min_span_ms"] / 1000.0,
                          promote_min_dy_px=config["gem_promote_min_dy_px"],
                          promote_lead_px=config["gem_promote_lead_px"],
                          lift_beam_slack_px=config["gem_lift_beam_slack_px"],
                          px_scale=px_scale)

strip_top = int(region_height * config["strip_top_fraction"])
strip_bottom = int(region_height * config["strip_bottom_fraction"])
upper_top = int(region_height * config["upper_strip_top_fraction"])
upper_bottom = int(region_height * config["upper_strip_bottom_fraction"])
log.info(f"fret strip y=[{strip_top},{strip_bottom}] upper strip y=[{upper_top},{upper_bottom}] lookahead={use_lookahead} lead={config['lookahead_lead_ms']}ms")

engaged_present = set()
engaged_upper = set()

def _hyst_lanes(band, y0, y1, engaged):
    fills = lane_strip_fill(band, len(hold_lane_keys), section_size, y0, y1,
                            config["strip_brightness"], config["strip_inset"])
    return apply_hysteresis(fills, hold_lane_keys, engaged,
                            config["strip_min_fraction"], config["strip_sustain_fraction"])

def present_lanes(band):
    global engaged_present
    engaged_present = _hyst_lanes(band, strip_top, strip_bottom, engaged_present)
    return engaged_present

def upper_lanes(band):
    global engaged_upper
    engaged_upper = _hyst_lanes(band, upper_top, upper_bottom, engaged_upper)
    return engaged_upper

# Diagnostic frames are encoded and written on a SEPARATE thread: a synchronous full-frame
# PNG write in the loop throttled detection to ~18fps (measured), which starves tracking and
# caused the very misses we diagnose with these frames. The queue is bounded and DROPS frames
# under pressure — diagnostics must never block detection.
diag_dropped = 0
if config["diagnostic_capture"] == "true":
    diag_mult_dir = os.path.join("diag", "mult")
    os.makedirs(diag_mult_dir, exist_ok=True)
    # separate file: replay tools json-parse every detect.jsonl line
    with open(os.path.join("diag", "meta.json"), "w", encoding="utf-8") as _meta:
        json.dump({"monitor_width": monitor_width, "monitor_height": monitor_height,
                   "tuning_scale": px_scale, "region": list(capture_region),
                   "fret_y": fret_y, "scroll_px_per_s": config["scroll_px_per_s"],
                   "section_size": section_size}, _meta)
    diag_mult_index = open(os.path.join("diag", "mult.jsonl"), "w", encoding="utf-8")
    diag_frame_idx = 0
    diag_last_save = 0.0
    diag_interval = 1.0 / config["diagnostic_fps"]
    diag_queue = queue.Queue(maxsize=8)

    def _diag_writer():
        while True:
            item = diag_queue.get()
            if item is None:
                return
            idx, now, frame = item
            cv2.imwrite(os.path.join(diag_mult_dir, f"{idx:06d}.png"), frame,
                        [cv2.IMWRITE_PNG_COMPRESSION, 1])
            diag_mult_index.write(json.dumps({"idx": idx, "t": round(now, 3)}) + "\n")
            diag_mult_index.flush()

    threading.Thread(target=_diag_writer, name="diag-writer", daemon=True).start()
    log.info(f"DIAGNOSTIC on, full frames @ {config['diagnostic_fps']}fps via writer thread")

def diagnostic_record(full_frame, idx, now):
    global diag_dropped
    try:
        # copy: the capture library may reuse the frame buffer before the writer encodes it
        diag_queue.put_nowait((idx, now, full_frame.copy()))
    except queue.Full:
        diag_dropped += 1

# Full-rate detection log (notes/trails/held per frame) for the note count. Buffered
# flush so the per-frame I/O doesn't throttle the loop.
log_detection = config["log_detection"] == "true"
if log_detection:
    detect_log = open(os.path.join("diag", "detect.jsonl"), "w", encoding="utf-8")
    detect_writes = 0

# Autoplay starts OFF so launching the bot never presses keys before the player asks
# (client request); one capslock press arms it.
# Track only the TOGGLE bit (& 1): the raw GetKeyState value also carries the is-down
# bit, which changes on BOTH key edges — autoplay flipped off at key-down and straight
# back on at key-up (~250ms later), making the toggle a net no-op. Both client logs
# show the OFF/ON pair; nobody here had ever toggled mid-song, so it hid for weeks.
autoplay_active = False
prev_capslock = win32api.GetKeyState(0x14) & 1

# FAST PRESS CLOCK — decoupled from the (slow) detection loop.
# The detection loop only enqueues/refines predicted press events; this dedicated thread fires
# them at their exact predicted arrival. Windows' default sleep granularity is ~15ms, which would
# re-quantise us to the frame rate we're trying to escape, so request 1ms timer resolution.
try:
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass
_press_clock_stop = threading.Event()
def _press_clock():
    while not _press_clock_stop.is_set():
        if autoplay_active:
            if use_gem_track:
                gem_queue.fire_due(time.time())
            elif use_queue:
                note_queue.fire_due(time.time())
        time.sleep(0.001)
_press_clock_thread = threading.Thread(target=_press_clock, name="press-clock", daemon=True)
_press_clock_thread.start()
log.info("main loop started, autoplay OFF (press capslock to start); press-clock @ ~1ms")
# Loop fps is logged so detection starvation can never hide again: the whole pipeline was
# tuned at ~137fps and silently ran at 18fps when frame writes blocked the loop.
_fps_count, _fps_t0 = 0, time.time()
while not keyboard.is_pressed(config['exit_key']):
    try:
        screenshot_np = main_camera.get_latest_frame()
        _last_frame_t = time.time()
    except Exception:
        time.sleep(0.05)
        continue
    _fps_count += 1
    if time.time() - _fps_t0 >= 5.0:
        log.info(f"loop fps ~{_fps_count / (time.time() - _fps_t0):.0f}")
        _fps_count, _fps_t0 = 0, time.time()
    capslock_status = win32api.GetKeyState(0x14) & 1
    if capslock_status != prev_capslock:
        autoplay_active = not autoplay_active
        log.info(f"autoplay {'ON' if autoplay_active else 'OFF'} (capslock toggled)")
        prev_capslock = capslock_status
    if screenshot_np is not None:
        band = detection_band(screenshot_np)
        if autoplay_active:
            now = time.time()
            present = present_lanes(band)
            gems_log = []
            beams_log = {}
            if use_gem_track:
                gems = detect_gems(band, len(hold_lane_keys), section_size, note_top, note_bottom,
                                   config["strip_brightness"], config["note_min_row_fill"], config["strip_inset"],
                                   min_height=config["gem_min_height"], merge_gap=config["gem_merge_gap"],
                                   min_mean_brightness=config["gem_min_mean_brightness"],
                                   max_height=config["gem_max_height"],
                                   merge_center_px=config["gem_merge_center_px"],
                                   hollow_fill=config["gem_hollow_fill"],
                                   return_arrow=config["use_arrow_lifts"] == "true",
                                   px_scale=px_scale)
                gems_log = [list(g) for g in gems]
                gem_queue.observe([(hold_lane_keys[g[0]],) + tuple(g[1:]) for g in gems], now)
                beam_h = detect_beam_heights(band, hold_lane_keys, section_size, note_top, note_bottom,
                                             config["strip_brightness"], config["strip_inset"],
                                             min_width=config["beam_min_width"],
                                             max_width=config["beam_max_width"],
                                             group_min_px=config["gem_beam_sustain_height"],
                                             anchor_px=config["beam_anchor_px"])
                gem_queue.service_holds(beam_h, now)    # hold sustains by BEAM height; firing on press-clock
                beams_log = beam_h
                held = gem_queue.held_lanes()
            elif use_queue:
                bars = detect_note_bars(band, len(hold_lane_keys), section_size, note_top, note_bottom,
                                        config["strip_brightness"], config["note_min_row_fill"], config["strip_inset"],
                                        config["note_min_run"])
                note_queue.observe([(hold_lane_keys[l], y) for l, y in bars], now)
                note_queue.service_holds(present, now)  # firing runs on the fast press-clock thread
                held = note_queue.held_lanes()
            elif use_lookahead:
                upper = upper_lanes(band)
                lookahead_controller.update(upper, present, now)
                held = lookahead_controller.held_lanes()
            else:
                sustain_controller.update(present, present, now)
                held = sustain_controller.held_lanes()
            if log_detection:
                entry = {"t": round(now, 4), "present": sorted(present), "held": sorted(held), "gems": gems_log, "beams": beams_log}
                if use_gem_track:
                    lifts = gem_queue.drain_lifts()
                    if lifts:
                        entry["lifts"] = [[lane, round(tl, 4), src] for lane, tl, src in lifts]
                detect_log.write(json.dumps(entry) + "\n")
                detect_writes += 1
                if detect_writes % 20 == 0:
                    detect_log.flush()
        else:
            gem_queue.release_all()
            note_queue.release_all()
            lookahead_controller.release_all()
            sustain_controller.release_all()
        if diagnostic_on and diag_frame_idx < config["diagnostic_max_frames"] and (time.time() - diag_last_save) >= diag_interval:
            diagnostic_record(screenshot_np, diag_frame_idx, time.time())
            diag_frame_idx += 1
            diag_last_save = time.time()

# STOP
log.info("exit key pressed, stopping")
if diag_dropped:
    log.info(f"diag frames dropped under write pressure: {diag_dropped}")
_press_clock_stop.set()
try:
    ctypes.windll.winmm.timeEndPeriod(1)
except Exception:
    pass
gem_queue.release_all()
note_queue.release_all()
lookahead_controller.release_all()
sustain_controller.release_all()
if log_detection:
    detect_log.close()
if config["diagnostic_capture"] == "true":
    diag_mult_index.close()
    log.info(f"DIAGNOSTIC saved {diag_frame_idx} multiplier crops")
main_camera.stop()
if config["console_window_ontop"] == "true" and hwnd is not None:
    # only undo the topmost flag if startup actually found the console and set it
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    except Exception as e:
        log.warning(f"could not restore console window z-order: {e}")

print(Fore.WHITE + Back.RED + "Stop key pressed. Exiting note-player..." + Fore.RESET + Back.RESET)
log.info("stopped")