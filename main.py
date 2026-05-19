# Import statements
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import sleep, strftime
from math import floor
import argparse
import json
import logging
import math
import os
import subprocess
import sys
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog

import hardware
import styling
import validation
import config_store
from styling import FONTS, PALETTE, apply_style, make_centrifuge_tube_canvas, primary_button
from well_plate import WellPlateProgress, format_snapshot_log
from run_logger import RunLogger, _fmt_hms

# GitHub URL displayed (clickable) in the About dialog. Hard-coded here so
# the About dialog has a single source of truth.
_GITHUB_URL = "https://github.com/RoliWilhelm/autoSIP-controller"

__version__ = "0.2.0"

logger = logging.getLogger("autosip")

##  GLOBAL CONSTANTS  ##
# - NEMA 17 data taken from motor datasheet
# - Lead screw pitch is as designed
NEMA_17_STEPS_PER_DEGREE = 3200.0 / 360.0
LEAD_SCREW_PITCH_IN_CM = 4.0

MODE_ORDER = ["Automated", "Manual", "Cleaning"]


class StepperMotor:
	"""Track stepper motor state and translate cm/degree moves into microsteps.

	Receives a ``StepperBackend``-shaped object (``onestep`` + ``release``)
	from ``hardware.get_backends()`` rather than importing MotorKit directly,
	so the same class works against real hardware or mocks.
	"""

	def __init__(self, motor, steps_per_degree, lead_screw_pitch, reverse=False, name=None):
		# Backend stepper object (real kit.stepperN or a mock from hardware.py)
		self.motor = motor
		self.name = name or "stepper"
		self.angle = 0.0
		self.steps_per_degree = steps_per_degree
		self.cm_per_deg = lead_screw_pitch / 360.0

		# Whether the motor is reversed -- i.e. which way it turns to push
		# the slider in a specific direction.
		self.reverse = reverse

		# Direction the needle is moving during fractionation
		self.forwards = True

	def get_angle(self):
		"""Return the current shaft angle in degrees (unbounded)."""
		return self.angle

	def tare(self):
		"""Reset the tracked angle to zero without moving the motor."""
		self.angle = 0.0

	def release(self):
		"""Release the motor coils to prevent overheating."""
		self.motor.release()

	def move_relative(self, angle):
		"""Turn the shaft so the slider moves by ``angle`` degrees' worth.

		On a direction reversal, the motor must first rotate through the
		lead-screw nut's mechanical play (backlash) before the slider
		engages. We add that one-shot ``backlash`` rotation to what the
		motor actually drives, but it produces no slider motion -- so
		``self.angle`` (which tracks the slider's commanded position and
		is read by ``move_absolute`` and the Manual-mode readout)
		accumulates only the intended portion. Conflating motor-shaft
		rotation with slider position used to make a 1 mm jog after a
		direction change display as ~4 mm in the readout.

		Intent steps and backlash steps are kept as separate integer
		microstep counts so summing them is exact -- folding them into a
		single float angle before flooring lost a microstep at certain
		step sizes (e.g. ``floor(8.888… × 27.9°) = 247`` instead of 248).
		"""
		# Backlash takeup, expressed as an exact microstep count. For the
		# stock geometry this evaluates to 240 microsteps (= 27°, = 0.3 cm
		# of motor rotation that engages the nut before the slider moves).
		backlash_steps = round(0.3 * self.steps_per_degree / self.cm_per_deg)

		intent_steps = floor(self.steps_per_degree * angle)
		extra_steps = 0
		if self.forwards and angle < 0:
			extra_steps = -backlash_steps
			self.forwards = False
		elif not self.forwards and angle > 0:
			extra_steps = backlash_steps
			self.forwards = True

		total_steps = intent_steps + extra_steps

		# FORWARD if net motion is positive and motor not reversed, or
		# negative and motor reversed. Sign of total_steps matches sign of
		# ``angle`` since extra_steps is signed to match.
		direction = (
			hardware.FORWARD
			if (total_steps > 0 and not self.reverse) or (total_steps < 0 and self.reverse)
			else hardware.BACKWARD
		)

		logger.debug(
			"%s move_relative angle=%.3f° steps=%d (intent=%d + backlash=%d) direction=%s",
			self.name, angle, abs(total_steps), intent_steps, extra_steps, direction,
		)

		for _ in range(0, abs(total_steps)):
			self.motor.onestep(direction=direction, style=hardware.MICROSTEP)
			sleep(0.0001)

		# Only the intent portion advanced the slider; the backlash portion
		# took up gear play. Accumulate intent_steps so self.angle stays in
		# lock-step with the slider's quantized physical position.
		self.angle = self.angle + intent_steps / self.steps_per_degree

		self.release()

	def move_absolute(self, angle):
		"""Turn the shaft to ``angle`` degrees relative to its initial position."""
		delta_angle = angle - self.angle
		self.move_relative(delta_angle)

	def move_dist_relative(self, dist):
		"""Move the slider ``dist`` cm relative to its current position."""
		logger.debug("%s move_dist_relative dist=%.3f cm", self.name, dist)
		self.move_relative(dist / self.cm_per_deg)

	def move_dist_absolute(self, dist):
		"""Move the slider to ``dist`` cm from its initial position."""
		logger.debug("%s move_dist_absolute dist=%.3f cm", self.name, dist)
		self.move_absolute(dist / self.cm_per_deg)


class Tooltip:
	"""Lightweight hover-tooltip helper for any Tk widget.

	Binds Enter/Leave/ButtonPress on the target widget; shows a small yellow
	Toplevel below it on hover. Multiple Tooltip instances per widget are
	safe (``add="+"`` on the bindings).
	"""

	def __init__(self, widget, text, delay_ms=400):
		self.widget = widget
		self.text = text
		self.delay_ms = delay_ms
		self._toplevel = None
		self._after_id = None
		widget.bind("<Enter>", self._on_enter, add="+")
		widget.bind("<Leave>", self._on_leave, add="+")
		widget.bind("<ButtonPress>", self._on_leave, add="+")

	def _on_enter(self, _event=None):
		self._cancel_pending()
		self._after_id = self.widget.after(self.delay_ms, self._show)

	def _on_leave(self, _event=None):
		self._cancel_pending()
		self._hide()

	def _cancel_pending(self):
		if self._after_id is not None:
			try:
				self.widget.after_cancel(self._after_id)
			except tk.TclError:
				pass
			self._after_id = None

	def _show(self):
		if self._toplevel is not None:
			return
		try:
			x = self.widget.winfo_rootx() + 16
			y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
			self._toplevel = tk.Toplevel(self.widget)
			self._toplevel.wm_overrideredirect(True)
			self._toplevel.wm_geometry(f"+{x}+{y}")
			tk.Label(
				self._toplevel, text=self.text, justify="left",
				bg="#ffffe1", fg="#222", relief="solid", borderwidth=1,
				font=("TkDefaultFont", 9), padx=6, pady=3, wraplength=320,
			).pack()
		except tk.TclError:
			self._toplevel = None

	def _hide(self):
		if self._toplevel is not None:
			try:
				self._toplevel.destroy()
			except tk.TclError:
				pass
			self._toplevel = None


class TextEntry(tk.Frame):
	"""Label + Entry pair with an inline red error label.

	The error label is hidden until ``show_error(msg)`` is called and
	disappears again on ``clear_error()``. Each parent grids the whole
	``TextEntry`` as a single cell; the internal label/entry/error layout
	is managed inside this Frame.
	"""

	def __init__(self, parent, text, *, textvariable=None):
		"""Build a label + entry pair. If ``textvariable`` is provided, the
		entry binds to that externally-owned StringVar so multiple TextEntry
		instances in different frames can stay in sync (e.g. Cleaning mode's
		waste-bin coords mirror Automated mode's). Otherwise a private
		StringVar is created.
		"""
		super().__init__(parent)
		self.label = tk.Label(self, text=text)
		self.label.grid(row=0, column=0, sticky="w")
		self.var = textvariable if textvariable is not None else tk.StringVar()
		self.entry = tk.Entry(self, textvariable=self.var)
		self.entry.grid(row=0, column=1, sticky="we")

		# Stays unmapped until show_error() grids it; takes no vertical
		# space when there's no error.
		self.error_label = tk.Label(self, text="", fg="red", anchor="w", wraplength=400)

		self.grid_columnconfigure(1, weight=1)

	def get(self):
		"""Return the current string the user has entered."""
		return self.var.get()

	def set(self, text):
		"""Replace the entry contents with ``text``."""
		# Set via the StringVar so the change propagates to the bound Entry
		# AND to any test stub that only tracks var state.
		self.var.set(text)

	def show_error(self, msg):
		self.error_label["text"] = msg
		self.error_label.grid(row=1, column=0, columnspan=2, sticky="w")

	def clear_error(self):
		self.error_label["text"] = ""
		self.error_label.grid_remove()


class StringVarLogHandler(logging.Handler):
	"""Push each log record's first line into a Tk ``StringVar``.

	A Label bound to that variable shows the most recent ``autosip`` log
	line in the status bar without polling. Multi-line messages are
	truncated to the first line; long lines are truncated to ``max_len``.
	"""

	def __init__(self, string_var, max_len=140):
		super().__init__()
		self.string_var = string_var
		self.max_len = max_len

	def emit(self, record):
		try:
			msg = record.getMessage().split("\n", 1)[0]
			if len(msg) > self.max_len:
				msg = msg[: self.max_len - 3] + "..."
			self.string_var.set(f"{record.levelname}: {msg}")
		except Exception:
			# StringVar can raise TclError if the Tk root has been destroyed
			# (shutdown). Suppress so we don't break the logging system.
			self.handleError(record)


class PumpController:
	"""Single-relay manager with claim tracking for the two-pump UX.

	Both pumps (Razel R-200 syringe and Adafruit 3910 peristaltic) share
	the same Digital Loggers IoT relay on GPIO 5; only one is physically
	plugged into the outlet at a time. The GUI exposes two semantic
	buttons (``Fractionate`` / ``Purge``) so labels match operator intent;
	this controller enforces an interlock so only one logical pump can
	hold the relay claim at a time.

	State machine flow (Automated runs):
	  start_run        -> claim_for("fractionate")
	  pump_liquid      -> set_relay(True)
	  stop_pump (wait) -> set_relay(False)   # claim stays
	  run end          -> release()           # claim cleared

	Manual flow (user click):
	  click ON  -> claim_for(name); set_relay(True)
	  click OFF -> set_relay(False); release()
	"""

	def __init__(self, relay):
		self._relay = relay
		self.claimant = None   # None | "fractionate" | "purge"
		self.relay_on = False
		self._listeners = []

	def subscribe(self, fn):
		"""Register ``fn(claimant, relay_on)`` to fire on every state change."""
		self._listeners.append(fn)

	def _notify(self):
		for fn in self._listeners:
			fn(self.claimant, self.relay_on)

	def claim_for(self, name):
		"""Claim the relay for ``name`` (idempotent for same name).

		Returns True if the claim is now held by ``name``, False if
		another claimant already holds it.
		"""
		if self.claimant is None:
			self.claimant = name
			self._notify()
			return True
		return self.claimant == name

	def release(self):
		"""Turn the relay off (if on) and clear the claim."""
		if self.relay_on:
			self._relay.off()
			self.relay_on = False
			logger.info("[pump:%s] relay OFF", self.claimant)
		self.claimant = None
		self._notify()

	def set_relay(self, on):
		"""Drive the relay ON/OFF. No-op if not currently claimed."""
		if self.claimant is None:
			return
		if on and not self.relay_on:
			self._relay.on()
			self.relay_on = True
			logger.info("[pump:%s] relay ON", self.claimant)
		elif not on and self.relay_on:
			self._relay.off()
			self.relay_on = False
			logger.info("[pump:%s] relay OFF", self.claimant)
		self._notify()

	def is_available_for(self, name):
		"""True if ``name`` can claim (i.e. relay is free or already ours)."""
		return self.claimant is None or self.claimant == name


# Pump-button color palette.
# OFF + available uses the primary accent so Fractionate/Purge read as
# primary action buttons (consistent with Begin Fractionation / Move /
# Home). ON flips to green for unmistakable "relay is live" feedback;
# locked uses a dim gray so the interlocked state is visibly disabled.
_PUMP_BTN_ON_BG = "#27a72c"
_PUMP_BTN_OFF_BG = PALETTE["accent"]
_PUMP_BTN_LOCKED_BG = "#bdbdbd"


def _set_pump_btn_style(btn, bg):
	"""Apply matching foreground + active styling whenever we flip a pump
	button's background color, so contrast stays WCAG-compliant
	(white on accent or green; dark on locked-gray)."""
	if bg == _PUMP_BTN_LOCKED_BG:
		fg = PALETTE["fg_text"]
		active = "#a8a8a8"
	else:
		fg = PALETTE["accent_fg"]
		active = PALETTE["accent_hover"] if bg == _PUMP_BTN_OFF_BG else "#1e7d20"
	btn["bg"] = bg
	btn["fg"] = fg
	btn["activebackground"] = active
	btn["activeforeground"] = fg
	btn["disabledforeground"] = fg


def _update_pump_button(btn, name, claimant, relay_on, in_run):
	"""Sync a Fractionate/Purge button's label, color, and enabled state.

	``in_run`` disables BOTH pump buttons across the UI: the state machine
	owns the relay during an automated run, so direct user clicks would
	interfere. Outside a run, the standard interlock applies: only the
	currently-claiming button is clickable; the opposite-name button is
	greyed out until the claim is released.
	"""
	display = name.title()
	if in_run:
		# State machine owns the pump for the entire run.
		if claimant == name and relay_on:
			btn["text"] = f"{display}: ON (run)"
			_set_pump_btn_style(btn, _PUMP_BTN_ON_BG)
		else:
			btn["text"] = f"{display}: OFF"
			_set_pump_btn_style(btn, _PUMP_BTN_OFF_BG)
		btn["state"] = tk.DISABLED
		return

	if claimant is None:
		btn["text"] = f"{display}: OFF"
		_set_pump_btn_style(btn, _PUMP_BTN_OFF_BG)
		btn["state"] = tk.NORMAL
	elif claimant == name:
		if relay_on:
			btn["text"] = f"{display}: ON"
			_set_pump_btn_style(btn, _PUMP_BTN_ON_BG)
		else:
			# Claim held by us with relay off -- only reachable through the
			# state machine (paused-mid-run). User clicks always pair on/off
			# with claim/release.
			btn["text"] = f"{display}: OFF (claim held)"
			_set_pump_btn_style(btn, _PUMP_BTN_OFF_BG)
		btn["state"] = tk.NORMAL
	else:
		# Interlock: opposite pump has the claim, this button is locked out.
		btn["text"] = f"{display}: OFF"
		_set_pump_btn_style(btn, _PUMP_BTN_LOCKED_BG)
		btn["state"] = tk.DISABLED


class StopSignButton(tk.Canvas):
	"""Octagonal stop-sign-shaped button.

	Click anywhere INSIDE the red octagon (or on the text) to fire
	``command``. Clicks in the canvas corners outside the polygon are
	ignored, so a near-miss won't trigger the action. Pair with a
	confirmation dialog at the callback side for true "hard to hit"
	semantics.
	"""

	def __init__(self, parent, command=None, size=44, text="STOP", font_size=None):
		# Match the parent's bg so the canvas's bounding-box corners blend
		# in (the octagon's corners look "transparent").
		try:
			bg = parent.cget("bg")
		except Exception:
			bg = ""
		super().__init__(
			parent, width=size, height=size,
			highlightthickness=0, bd=0,
			**({"bg": bg} if bg else {}),
		)
		self.command = command

		cx, cy = size / 2, size / 2
		r = size / 2 - 1
		# 8 vertices at math-angle 22.5° + 45°·i so two vertices straddle the
		# top -- producing a flat-top edge (i.e. a true stop-sign orientation).
		# Tk's y axis points down, so subtract the sin component.
		verts = []
		for i in range(8):
			angle = math.radians(22.5 + 45 * i)
			verts.extend([
				cx + r * math.cos(angle),
				cy - r * math.sin(angle),
			])
		self._octagon = self.create_polygon(
			verts, fill="#cc0000", outline="white", width=2,
		)
		# Default font scales with size; longer-text callers (e.g. multi-line
		# labels) can pass an explicit font_size to fit.
		if font_size is None:
			font_size = max(8, int(size / 5))
		self._text_id = self.create_text(
			cx, cy, text=text, fill="white", justify="center",
			font=("TkDefaultFont", font_size, "bold"),
		)
		# Hit-test only on the polygon and its text -- dead corners ignore
		# stray clicks. Cursor change on hover signals clickability.
		for item in (self._octagon, self._text_id):
			self.tag_bind(item, "<Button-1>", self._on_click)
			self.tag_bind(item, "<Enter>", lambda e: self.config(cursor="hand2"))
			self.tag_bind(item, "<Leave>", lambda e: self.config(cursor=""))

	def _on_click(self, event):
		if self.command is not None:
			self.command()


@dataclass
class FractionatorState:
	"""Run-time state shared across the App and the mode frames.

	Lives on ``App.state``. Frames read/write through the App rather than
	holding their own copies, so a single source of truth tracks the
	in-progress fractionation regardless of which mode the user is viewing.
	"""
	# Plate geometry (set on Begin / manual step from the entry widgets)
	ROWS: int = 0
	COLS: int = 0
	well_size: float = 0.0
	pump_time: float = 0.0
	# Post-pump drip wait, seconds. Used to be coupled to pump_time;
	# now operator-controlled via the Drip wait time entry.
	drip_wait_time: float = 1.0
	# Volume per well in cc -- displayed by the WellPlateProgress header
	# ("Dispensing into B4 — 0.22 cc"). Stored explicitly because pump_time
	# alone can't recover it without also knowing pump_rate.
	volume_per_well: float = 0.0

	# Current needle position in the well grid
	x: int = 0
	y: int = 0
	carriage_forwards: bool = True

	# Automated-flow state machine: "idle" | "pump" | "wait" | "move"
	state: str = "idle"
	taskId: object = None
	is_paused: bool = False

	# Run identification, mirrored from the AutomatedFrame entry boxes by
	# trace_add callbacks. These are NOT frozen at run start -- the logger
	# reads them fresh per well so mid-run tube swaps (Sample ID) and typo
	# fixes (Project) are reflected in log.csv going forward.
	project: str = ""
	current_sample_id: str = ""

	# Discard + collection phase machinery. The run executes Phase 1
	# (discards to a waste bin, if D>0) then Phase 2 (snake-path collection
	# of N-D wells to the plate). When ``discards_done + wells_collected``
	# reaches ``number_of_fractions``, the run auto-pauses at "total_reached"
	# and waits for the user's explicit End Run click.
	phase: str = "idle"                  # idle | discard | collect | total_reached
	number_of_fractions: int = 0          # N -- frozen at run start
	discards_planned: int = 0             # D -- frozen at run start (metadata)
	# Per-series snapshot of D. Updated at series start (movement() or
	# continue_to_next_sample()). Used for label computation + auto-pause
	# threshold so an edit to the Discard fractions entry affects only the
	# NEXT series, not the current one.
	discards_at_series_start: int = 0
	discards_done: int = 0                # 0..discards_at_series_start counter
	wells_collected: int = 0              # 0..(N-D_this_series) counter
	waste_bin_table: float = 0.0          # cm, waste-bin target table position
	waste_bin_carriage: float = 0.0       # cm, waste-bin target carriage position
	table_start_cm: float = 0.0           # cm, plate-start table position
	carriage_start_cm: float = 0.0        # cm, plate-start carriage position

	# Multi-sample run support. ``series_index`` is the 1-based count of
	# series within the run (each Continue-to-Next-Sample increments).
	# ``current_series_sequence`` is the 1-based count of WELLS COLLECTED
	# within the current series (resets per series; discards don't count).
	# ``well_records`` is the source of truth the progress widget reads to
	# repaint after a resize without having to mine log.csv at render time.
	series_index: int = 0
	current_series_sequence: int = 0
	well_records: list = field(default_factory=list)

	# Multi-plate run support. ``current_plate_id`` updates on each plate
	# swap. ``wells_on_current_plate`` resets to 0 on each swap so the
	# plate-full detection works on the live plate, not historical totals.
	# ``plate_full_with_sample_complete`` distinguishes the "both finished
	# simultaneously" auto-pause from a plain plate-full auto-pause so the
	# button matrix can enable Continue to Next Sample appropriately.
	# ``plates_used`` is a chronological list of unique plate IDs.
	# ``plate_swaps_done`` is a 1-based counter used for plate_swap_<N>
	# breadcrumb naming.
	current_plate_id: str = ""
	wells_on_current_plate: int = 0
	plate_full_with_sample_complete: bool = False
	plates_used: list = field(default_factory=list)
	plate_swaps_done: int = 0


class HeaderFrame(tk.Frame):
	"""Top bar: three side-by-side mode tabs.

	Each tab is a button labeled with one of the three modes (Automated,
	Manual, Cleaning). The active mode is rendered in the accent color
	so the current mode is obvious at a glance; the other two appear as
	subdued secondary buttons that the operator can click to jump
	directly to that mode (no cycling). The paused-run override prompt
	still fires via ``App.request_mode_change``.

	The Return-to-Start-Coords / Pause / Continue-to-Next-Sample /
	End-Run buttons all live in AutomatedFrame's run-control row. The
	big red STOP octagon still lives at the bottom-right of the status
	bar.
	"""

	# Legacy color names retained so external imports (tests, the
	# _update_run_control_buttons App method) don't break.
	_PAUSE_RUNNING_BG = "#27a72c"
	_PAUSE_PAUSED_BG = "#6F4E37"

	# Subdued styling for inactive tabs -- light gray bg, dark text;
	# still high-contrast (>= 12:1) and visibly clickable.
	_TAB_INACTIVE_BG = "#e0e0e0"
	_TAB_INACTIVE_FG = PALETTE["fg_text"]
	_TAB_INACTIVE_ACTIVE_BG = "#cfcfcf"

	def __init__(self, master, app):
		super().__init__(master)
		self.app = app
		self.configure(bg=PALETTE["bg_window"])

		self._tab_buttons = {}
		for col, name in enumerate(MODE_ORDER):
			# Bind ``name`` at lambda-creation time so each button captures
			# its own label rather than the loop's final value.
			btn = tk.Button(
				self, text=name,
				command=lambda n=name: app.request_mode_change(n),
				font=FONTS["bold"], relief="flat", borderwidth=0,
				highlightthickness=0, padx=10, pady=8, cursor="hand2",
			)
			btn.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 0), pady=2)
			self.grid_columnconfigure(col, weight=1, uniform="modetabs")
			self._tab_buttons[name] = btn

	def set_mode_label(self, mode_name):
		"""Highlight the active tab and subdue the others."""
		for name, btn in self._tab_buttons.items():
			if name == mode_name:
				btn["bg"] = PALETTE["accent"]
				btn["fg"] = PALETTE["accent_fg"]
				btn["activebackground"] = PALETTE["accent_hover"]
				btn["activeforeground"] = PALETTE["accent_fg"]
				btn["disabledforeground"] = PALETTE["accent_fg"]
			else:
				btn["bg"] = self._TAB_INACTIVE_BG
				btn["fg"] = self._TAB_INACTIVE_FG
				btn["activebackground"] = self._TAB_INACTIVE_ACTIVE_BG
				btn["activeforeground"] = self._TAB_INACTIVE_FG
				btn["disabledforeground"] = self._TAB_INACTIVE_FG


class StatusBarFrame(tk.Frame):
	"""Bottom bar, always visible across mode switches.

	Layout (left to right):
	  [Mode: X] [● Pump ON/OFF] [...per-phase status...] [LEVEL: last log line]
	"""

	# Pump indicator colors reflect the PumpController's claim + relay state:
	#   idle (no claim)                  -> gray, "Pump: idle"
	#   claim held, relay on             -> green, "Pump: <Name> (ON)"
	#   claim held, relay off (run wait) -> amber, "Pump: <Name> (OFF)"
	_PUMP_IDLE_COLOR = "#888888"
	_PUMP_ON_COLOR = "#27a72c"
	_PUMP_WAIT_COLOR = "#e8b800"

	def __init__(self, master, app):
		super().__init__(master, bd=1, relief="sunken")
		self.app = app

		# Terminate Run: stop-sign-shaped, far bottom-right corner. Packed
		# FIRST with side=RIGHT so it claims the rightmost slot. Geographically
		# distant from Pause (top-right) so the two can't be confused.
		self.terminate_btn = StopSignButton(
			self, command=app.terminate_run, size=80,
			text="Terminate\nrun", font_size=10,
		)
		self.terminate_btn.pack(side=tk.RIGHT, padx=4, pady=2)

		# Mode (left)
		self.mode_lbl = tk.Label(self, text="Mode: Automated", anchor="w")
		self.mode_lbl.pack(side=tk.LEFT, padx=(6, 12))

		# Pump indicator: small canvas + label, just right of the mode
		self.pump_canvas = tk.Canvas(self, width=14, height=14, highlightthickness=0)
		self._pump_dot = self.pump_canvas.create_oval(
			2, 2, 12, 12, fill=self._PUMP_IDLE_COLOR, outline="",
		)
		self.pump_canvas.pack(side=tk.LEFT, padx=(0, 4))
		self.pump_state_lbl = tk.Label(self, text="Pump: idle", anchor="w")
		self.pump_state_lbl.pack(side=tk.LEFT, padx=(0, 12))

		# Last log line (right of status, left of the STOP button) -- bound
		# to a StringVar that StringVarLogHandler updates from the autosip
		# logger.
		self.log_var = tk.StringVar(value="")
		self.log_lbl = tk.Label(self, textvariable=self.log_var, anchor="e", fg="#555")
		self.log_lbl.pack(side=tk.RIGHT, padx=(8, 6))

		# Per-phase status (kept from the previous version) fills the middle.
		self.status_lbl = tk.Label(self, text="System idle.", anchor="w")
		self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

	def set_text(self, text):
		"""Set the per-phase status message (middle of the bar)."""
		self.status_lbl["text"] = text

	def set_mode(self, mode_name):
		self.mode_lbl["text"] = f"Mode: {mode_name}"

	def set_terminate_visible(self, visible):
		"""Show/hide the stop-sign Terminate Run button. Hidden outside
		Automated mode since terminate only makes sense for runs."""
		if visible:
			# `before=log_lbl` puts terminate back in the rightmost slot
			# (log_lbl was originally packed AFTER terminate, so it sits to
			# the left of it -- pack-before reclaims the same position).
			self.terminate_btn.pack(side=tk.RIGHT, padx=4, pady=2, before=self.log_lbl)
		else:
			self.terminate_btn.pack_forget()

	def set_pump_state(self, claimant, relay_on):
		"""Update the indicator from PumpController state.

		``claimant`` is ``None`` (idle, gray), ``"fractionate"``, or
		``"purge"``. ``relay_on`` determines whether the dot is green (on)
		or amber (claim held but relay off -- e.g. inter-dispense wait
		during an automated run).
		"""
		if claimant is None:
			color = self._PUMP_IDLE_COLOR
			text = "Pump: idle"
		elif relay_on:
			color = self._PUMP_ON_COLOR
			text = f"Pump: {claimant.title()} (ON)"
		else:
			color = self._PUMP_WAIT_COLOR
			text = f"Pump: {claimant.title()} (OFF)"
		self.pump_canvas.itemconfig(self._pump_dot, fill=color)
		self.pump_state_lbl["text"] = text


class AutomatedFrame(tk.Frame):
	"""Automated fractionation: JSON loader, plate inputs, Move / Begin / Pause,
	pump toggle, and the progress canvas."""

	def __init__(self, master, app):
		super().__init__(master)
		self.app = app

		# Cached info from the most recent successful load_json() so the
		# RunLogger can include the labware path + inline contents in
		# metadata.json. Both None means the operator entered values
		# manually without loading a file.
		self._loaded_labware_path = None
		self._loaded_labware_data = None

		for i in range(3):
			self.grid_columnconfigure(i, weight=1, uniform="auto")

		# Run-control button row: always visible from app startup, state-
		# driven enable/disable. Placed at row 0 (top-right of the frame)
		# so the four controls are the first thing visible.
		ctrl = tk.Frame(self)
		ctrl.grid(row=0, column=0, columnspan=3, sticky="e", pady=(0, 4))
		self.return_btn = tk.Button(ctrl, text="Return to Start Coords", command=app.return_to_home)
		self.return_btn.pack(side=tk.LEFT, padx=2)
		self.pause_btn = tk.Button(ctrl, text="Pause", command=app.toggle_pause,
			bg="#27a72c", fg="white", activebackground="#3ac640", activeforeground="white",
			font=("TkDefaultFont", 10, "bold"))
		self.pause_btn.pack(side=tk.LEFT, padx=2)
		self.continue_btn = tk.Button(
			ctrl, text="Continue to Next Sample",
			command=app.continue_to_next_sample)
		self.continue_btn.pack(side=tk.LEFT, padx=2)
		self.continue_plate_btn = tk.Button(
			ctrl, text="Continue to Next Plate",
			command=app.continue_to_next_plate)
		self.continue_plate_btn.pack(side=tk.LEFT, padx=2)
		self.end_run_btn = tk.Button(ctrl, text="End Run", command=self.end_run_clicked)
		self.end_run_btn.pack(side=tk.LEFT, padx=2)
		# Default disabled-button bg used when the Pause button is disabled
		# (its custom green/brown bgs override the system disabled style).
		self._pause_default_bg = "#d9d9d9"

		# ----- Run Parameters (top) --------------------------------------
		# Project + Sample ID stay user-editable while a run is in progress
		# so the operator can update Sample ID a moment before clicking
		# Resume after a tube swap. Number of fractions / discards are
		# frozen at run start. Volume per well moved here from the old
		# pump-and-volume row because it's per-fraction metadata.
		runp = tk.LabelFrame(self, text="Run Parameters", padx=8, pady=4)
		runp.grid(row=1, column=0, columnspan=3, sticky="we", padx=2, pady=(0, 2))
		runp.grid_columnconfigure(0, weight=1)
		self.project_te = TextEntry(runp, "Project name:")
		self.project_te.grid(row=0, column=0, sticky="we")
		Tooltip(
			self.project_te.entry,
			"Applied to all output files for this run. "
			"Set once, rarely changed mid-run.",
		)
		self.sample_id_te = TextEntry(runp, "Sample ID:")
		self.sample_id_te.grid(row=1, column=0, sticky="we")
		Tooltip(
			self.sample_id_te.entry,
			"Identifies the source tube. Change during a pause when "
			"swapping tubes.",
		)
		self.plate_id_te = TextEntry(runp, "Plate ID:")
		self.plate_id_te.grid(row=2, column=0, sticky="we")
		# First-launch default. last_used (loaded after frames build) will
		# override this if the operator already used a different Plate ID.
		self.plate_id_te.set("Plate-1")
		Tooltip(
			self.plate_id_te.entry,
			"Identifies the physical plate currently on the stage. "
			"Auto-incremented at each plate swap.",
		)
		self.n_fractions_te = TextEntry(runp, "Number of fractions:")
		self.n_fractions_te.grid(row=3, column=0, sticky="we")
		Tooltip(
			self.n_fractions_te.entry,
			"TOTAL fractions including discards. The first N–D fractions "
			"that aren't discarded land on the plate; max = rows × cols.",
		)
		self.discard_te = TextEntry(runp, "Discard fractions:")
		self.discard_te.grid(row=4, column=0, sticky="we")
		Tooltip(
			self.discard_te.entry,
			"Initial fractions pumped to a waste bin before plate collection "
			"begins (e.g., low-density buffer above the band of interest). "
			"Set 0 to skip the discard phase.",
		)
		self.vol_text_entry = TextEntry(runp, "Volume per well (cc, e.g., 0.22):")
		self.vol_text_entry.grid(row=5, column=0, sticky="we")

		# ----- Plate Parameters (middle) ---------------------------------
		# Plate geometry + the two coordinate pairs (plate-start and
		# waste-bin). The plate-start fields used to live in the table/carriage
		# move row; they're now part of plate definition.
		platep = tk.LabelFrame(self, text="Plate Parameters", padx=8, pady=4)
		platep.grid(row=2, column=0, columnspan=3, sticky="we", padx=2, pady=(0, 2))
		platep.grid_columnconfigure(0, weight=1)
		self.rows_text_entry = TextEntry(platep, "Number of rows (1–16):")
		self.rows_text_entry.grid(row=0, column=0, sticky="we")
		self.cols_text_entry = TextEntry(platep, "Number of columns (1–24):")
		self.cols_text_entry.grid(row=1, column=0, sticky="we")
		self.ws_text_entry = TextEntry(platep, "Well width (cm):")
		self.ws_text_entry.grid(row=2, column=0, sticky="we")

		self.table_te = TextEntry(platep, "Starting point (x-axis):")
		self.table_te.grid(row=3, column=0, sticky="we")
		self.carriage_te = TextEntry(platep, "Starting point (y-axis):")
		self.carriage_te.grid(row=4, column=0, sticky="we")

		tk.Label(platep, text="Waste bin:", anchor="w").grid(
			row=5, column=0, sticky="w", pady=(6, 0))
		self.waste_table_te = TextEntry(
			platep, "  table position (cm, 0–20):",
			textvariable=app.waste_bin_table_var,
		)
		self.waste_table_te.grid(row=6, column=0, sticky="we")
		self.waste_carriage_te = TextEntry(
			platep, "  carriage position (cm, 0–15):",
			textvariable=app.waste_bin_carriage_var,
		)
		self.waste_carriage_te.grid(row=7, column=0, sticky="we")
		Tooltip(
			self.waste_table_te.entry,
			"Waste-bin position used during the discard phase. "
			"Ignored when Discard fractions = 0.",
		)
		Tooltip(
			self.waste_carriage_te.entry,
			"Waste-bin position used during the discard phase. "
			"Ignored when Discard fractions = 0.",
		)

		# ----- Pump ------------------------------------------------------
		pumpp = tk.LabelFrame(self, text="Pump", padx=8, pady=4)
		pumpp.grid(row=3, column=0, columnspan=3, sticky="we", padx=2, pady=(0, 2))
		pumpp.grid_columnconfigure(0, weight=1)
		self.pump_rate_text_entry = TextEntry(pumpp, "Pump rate (cc/hr — see your syringe pump spec):")
		self.pump_rate_text_entry.grid(row=0, column=0, sticky="we")
		self.drip_wait_te = TextEntry(pumpp, "Drip wait time (s):")
		self.drip_wait_te.grid(row=1, column=0, sticky="we")
		self.drip_wait_te.set("1.0")
		Tooltip(
			self.drip_wait_te.entry,
			"Wait time between pump-off and moving to the next well. "
			"Longer waits improve volume consistency; shorter waits run faster.",
		)

		# JSON loader (own row, no LabelFrame to keep it compact)
		tk.Label(self, text="Load well plate file: ").grid(row=4, column=0, columnspan=1, pady=(6, 0))
		self.json_entry = tk.Entry(self)
		self.json_entry.grid(row=4, column=1, columnspan=1, pady=(6, 0))
		tk.Button(self, text="Load", command=self.load_json).grid(
			row=4, column=2, columnspan=1, sticky="we", pady=(6, 0),
		)

		# Begin Fractionation -- the run-launch button. (The previous
		# "Move (jog to Plate-start coords)" button was removed because the
		# Return to Start Coords button in the run-controls row already
		# moves to the same position.)
		#
		# Visually this is a composite: a small Canvas drawing a 45-deg
		# ultracentrifuge tube on the left, and the action button on the
		# right, both sharing the accent color so they read as one unit.
		# Clicks on the tube canvas fall through to begin_clicked too so
		# the entire region behaves like a single button.
		begin_frame = tk.Frame(self, bg=PALETTE["accent"], bd=0, highlightthickness=0)
		begin_frame.grid(row=5, column=0, columnspan=3, sticky="we", pady=(4, 0))
		begin_frame.grid_columnconfigure(1, weight=1)
		self.begin_tube_canvas = make_centrifuge_tube_canvas(begin_frame, size=40)
		self.begin_tube_canvas.grid(row=0, column=0, padx=(8, 4), pady=4, sticky="w")
		self.begin_tube_canvas.bind("<Button-1>", lambda _e: self.begin_clicked())
		self.begin_btn = primary_button(
			begin_frame, text="Begin Fractionation",
			command=self.begin_clicked, anchor="w",
		)
		self.begin_btn.grid(row=0, column=1, sticky="we", padx=(0, 8), pady=4)

		# Progress view -- to-scale well plate, color-blind-safe palette,
		# header showing current well + count + elapsed/remaining time.
		self.progress = WellPlateProgress(self, min_width=500, min_height=300)
		self.progress.grid(row=6, column=0, columnspan=3, sticky="nsew")
		self.grid_rowconfigure(6, weight=1)

		# Mirror Project/Sample ID entry text into state.project /
		# state.current_sample_id on every keystroke (trace_add). Focus-out
		# does the validation + mid-run confirm-or-revert dance.
		self._project_last_committed = ""
		self.project_te.var.trace_add("write", lambda *_: self._on_project_text_changed())
		self.sample_id_te.var.trace_add("write", lambda *_: self._on_sample_id_text_changed())
		# Plate ID changes mirror to state so the logger's get_current_run_id
		# callback sees the latest value on every CSV write.
		self.plate_id_te.var.trace_add("write", lambda *_: self._on_plate_id_text_changed())
		self.project_te.entry.bind("<FocusOut>", self._on_project_focus_out, add="+")

		# Persist field values to ~/.autosip/config.json on every focus-out
		# so the next launch can repopulate. Bound on each entry widget --
		# programmatic .set() doesn't trigger FocusOut, so loading a profile
		# saves explicitly via App._load_profile.
		self._bind_focus_out_save()

	# -- Profile / config persistence -----------------------------------

	# Map between config_store FIELDS and the widgets that hold each value.
	# Keeping the mapping in one place means get_values / set_values stay
	# trivial and the order matches config_store.FIELDS for clarity.
	def _entry_for(self, field):
		return {
			"project": self.project_te,
			"sample_id": self.sample_id_te,
			"plate_id": self.plate_id_te,
			"number_of_fractions": self.n_fractions_te,
			"discard_fractions": self.discard_te,
			"rows": self.rows_text_entry,
			"cols": self.cols_text_entry,
			"well_size": self.ws_text_entry,
			"pump_rate": self.pump_rate_text_entry,
			"drip_wait_time": self.drip_wait_te,
			"volume_per_well": self.vol_text_entry,
			"table_start": self.table_te,
			"carriage_start": self.carriage_te,
			"waste_bin_table": self.waste_table_te,
			"waste_bin_carriage": self.waste_carriage_te,
		}.get(field)

	def end_run_clicked(self):
		self.app.end_run()

	def get_values(self):
		"""Snapshot every persistable field as a {field: str} dict."""
		out = {}
		for field in config_store.FIELDS:
			if field == "labware_file":
				out[field] = self.json_entry.get()
			else:
				w = self._entry_for(field)
				out[field] = w.get() if w is not None else ""
		return out

	def set_values(self, values):
		"""Populate fields from a {field: str} dict. Missing keys leave the
		corresponding widget untouched."""
		for field in config_store.FIELDS:
			if field not in values:
				continue
			val = values[field] or ""
			if field == "labware_file":
				self.json_entry.delete(0, tk.END)
				if val:
					self.json_entry.insert(0, val)
			else:
				w = self._entry_for(field)
				if w is not None:
					w.set(val)

	def _bind_focus_out_save(self):
		for field in config_store.FIELDS:
			if field == "labware_file":
				self.json_entry.bind("<FocusOut>", self._on_field_focus_out, add="+")
			else:
				w = self._entry_for(field)
				if w is not None:
					w.entry.bind("<FocusOut>", self._on_field_focus_out, add="+")

	def _on_field_focus_out(self, _event=None):
		try:
			config_store.save_last_used(self.get_values())
		except OSError as exc:
			logger.warning("Failed to save last_used config: %s", exc)

	# -- Project / Sample ID live mirroring ----------------------------

	def _on_project_text_changed(self):
		"""Mirror the Project entry into state on every keystroke + clear
		any inline error that's now stale."""
		text = self.project_te.get()
		self.app.state.project = text
		ok, _ = validation.project(text)
		if ok:
			self.project_te.clear_error()

	def _on_sample_id_text_changed(self):
		"""Mirror the Sample ID entry into state on every keystroke."""
		text = self.sample_id_te.get()
		self.app.state.current_sample_id = text
		ok, _ = validation.sample_id(text)
		if ok:
			self.sample_id_te.clear_error()

	def _on_plate_id_text_changed(self):
		"""Mirror the Plate ID entry into state on every keystroke."""
		text = self.plate_id_te.get()
		self.app.state.current_plate_id = text
		ok, _ = validation.plate_id(text)
		if ok:
			self.plate_id_te.clear_error()

	def _on_project_focus_out(self, _event=None):
		"""If the Project changed mid-run, prompt for confirmation; revert
		on No. Always falls through to the standard last_used save."""
		new = self.project_te.get().strip()
		old = self._project_last_committed
		if new != old and self._is_run_in_progress():
			confirmed = messagebox.askyesno(
				"Project name changed mid-run",
				"Project name changed mid-run. This affects log entries "
				"going forward, but files already written keep their "
				"original Project name.\n\nContinue?",
				parent=self,
			)
			if not confirmed:
				# Revert (trace will sync state.project back)
				self.project_te.set(old)
				return
		self._project_last_committed = new
		self._on_field_focus_out()

	def _is_run_in_progress(self):
		"""True if the state machine is mid-fractionation (running OR paused)."""
		return self.app.state.state != "idle" or self.app.state.is_paused

	def refresh(self):
		"""Re-sync widgets and state when the Automated frame becomes active."""
		s = self.app.state
		s.x = 0
		s.y = 0
		s.carriage_forwards = True
		self._clear_all_errors()
		self.app.set_status("System idle.")

	def _clear_all_errors(self):
		for te in (
			self.project_te, self.sample_id_te, self.plate_id_te,
			self.n_fractions_te, self.discard_te,
			self.rows_text_entry, self.cols_text_entry, self.ws_text_entry,
			self.pump_rate_text_entry, self.drip_wait_te, self.vol_text_entry,
			self.table_te, self.carriage_te,
			self.waste_table_te, self.waste_carriage_te,
		):
			te.clear_error()

	def set_controls_enabled(self, enabled):
		"""Enable/disable buttons that command motion or start a run."""
		state = tk.NORMAL if enabled else tk.DISABLED
		self.begin_btn["state"] = state

	def load_json(self):
		"""Pop a file dialog, load the Opentrons-format JSON, populate inputs.

		Catches the three failure modes the user is likely to hit -- missing
		file, malformed JSON, missing Opentrons keys -- and surfaces each via
		``messagebox.showerror`` with a description of what went wrong. Full
		tracebacks are logged at DEBUG.
		"""
		# Seed the dialog with whatever's already in the entry, so a user who
		# pasted a path doesn't lose their place.
		typed = self.json_entry.get().strip()
		initial_dir = None
		initial_file = None
		if typed:
			if os.path.isdir(typed):
				initial_dir = typed
			elif os.path.exists(typed):
				initial_dir = os.path.dirname(typed) or None
				initial_file = os.path.basename(typed)

		path = filedialog.askopenfilename(
			parent=self,
			title="Select Opentrons well-plate JSON",
			filetypes=[("Opentrons JSON", "*.json"), ("All files", "*.*")],
			initialdir=initial_dir,
			initialfile=initial_file,
		)
		if not path:
			# User cancelled the dialog -- leave existing inputs alone.
			return

		try:
			with open(path) as json_spec:
				data = json.load(json_spec)
			# Pull every value we need up front so any KeyError fires before
			# we start populating widgets (avoids half-loaded state).
			ordering = data["ordering"]
			n_rows = len(ordering[0])
			n_cols = len(ordering)
			wells = data["wells"]
			a1 = wells["A1"]
			b1 = wells["B1"]
			a1_x = a1["x"]
			a1_y = a1["y"]
			b1_y = b1["y"]
			y_dim = data["dimensions"]["yDimension"]
		except FileNotFoundError:
			logger.debug("load_json: file not found at %s", path, exc_info=True)
			messagebox.showerror(
				"Load failed",
				f"File not found:\n{path}",
				parent=self,
			)
			return
		except json.JSONDecodeError as exc:
			logger.debug("load_json: JSON decode error in %s", path, exc_info=True)
			messagebox.showerror(
				"Load failed",
				f"Could not parse JSON in {os.path.basename(path)}:\n"
				f"{exc.msg} (line {exc.lineno}, column {exc.colno})",
				parent=self,
			)
			return
		except (KeyError, IndexError, TypeError) as exc:
			logger.debug("load_json: malformed Opentrons structure in %s", path, exc_info=True)
			messagebox.showerror(
				"Load failed",
				f"Opentrons JSON is missing or malformed: {exc!r}\n\n"
				"Expected top-level 'ordering' (non-empty), 'dimensions' "
				"with 'yDimension', and 'wells' containing 'A1' and 'B1' "
				"each with 'x' and 'y'.",
				parent=self,
			)
			return

		self.json_entry.delete(0, tk.END)
		self.json_entry.insert(0, path)

		self.rows_text_entry.set(str(n_rows))
		self.cols_text_entry.set(str(n_cols))
		self.ws_text_entry.set(str(abs(a1_y - b1_y) / 10.0))
		self.table_te.set(str(15 - a1_x * 0.1))
		self.carriage_te.set(str(0.1 * (y_dim - a1_y) - 0.5))

		# Remember what was loaded so the RunLogger can include the file
		# path + inline JSON contents in metadata.json.
		self._loaded_labware_path = path
		self._loaded_labware_data = data

	def begin_clicked(self):
		"""Validate every Begin-time input; show inline + summary errors on
		failure; cross-check N/D and plate capacity; warn on waste/plate
		overlap; then dispatch to ``app.start_run``."""
		# Single-field validation. Order matches displayed order so the
		# messagebox bullets read top-to-bottom.
		fields = [
			(self.project_te, validation.project),
			(self.sample_id_te, validation.sample_id),
			(self.plate_id_te, validation.plate_id),
			(self.n_fractions_te, validation.number_of_fractions),
			(self.discard_te, validation.discard_fractions),
			(self.vol_text_entry, validation.volume),
			(self.rows_text_entry, validation.rows),
			(self.cols_text_entry, validation.cols),
			(self.ws_text_entry, validation.well_size),
			(self.table_te, validation.table_pos),
			(self.carriage_te, validation.carriage_pos),
			(self.pump_rate_text_entry, validation.pump_rate),
			(self.drip_wait_te, validation.drip_wait_time),
		]
		parsed = []
		errors = []
		for te, validate in fields:
			ok, result = validate(te.get())
			if ok:
				te.clear_error()
				parsed.append(result)
			else:
				te.show_error(result)
				errors.append(result)

		# Cross-field checks (run only if all single-field parses passed --
		# otherwise the comparisons would be against None/garbage).
		if not errors:
			(project_v, sample_v, plate_v, n_v, d_v, vol_v,
				rows_v, cols_v, ws_v, table_v, carriage_v, rate_v,
				drip_v) = parsed
			capacity = rows_v * cols_v

			# N must fit on the plate
			if n_v > capacity:
				msg = (
					f"Number of fractions ({n_v}) exceeds plate capacity "
					f"({rows_v} × {cols_v} = {capacity} wells)."
				)
				self.n_fractions_te.show_error(msg)
				errors.append(msg)

			# D must be strictly less than N
			if d_v >= n_v:
				msg = "Discard count must be less than total number of fractions."
				self.discard_te.show_error(msg)
				errors.append(msg)

			# Waste-bin coords required if D > 0; optional otherwise.
			waste_x = None
			waste_y = None
			if d_v > 0:
				wx_ok, wx_val = validation.table_pos(self.waste_table_te.get())
				wy_ok, wy_val = validation.carriage_pos(self.waste_carriage_te.get())
				if not wx_ok:
					self.waste_table_te.show_error(wx_val)
					errors.append(wx_val)
				else:
					waste_x = wx_val
				if not wy_ok:
					self.waste_carriage_te.show_error(wy_val)
					errors.append(wy_val)
				else:
					waste_y = wy_val
			else:
				# D == 0: empty waste-bin entries are fine; non-empty must
				# still parse so they're not garbage in the config.
				wx_ok, wx_val = validation.table_pos(self.waste_table_te.get(), allow_empty=True)
				wy_ok, wy_val = validation.carriage_pos(self.waste_carriage_te.get(), allow_empty=True)
				if not wx_ok:
					self.waste_table_te.show_error(wx_val)
					errors.append(wx_val)
				if not wy_ok:
					self.waste_carriage_te.show_error(wy_val)
					errors.append(wy_val)
				waste_x = wx_val if wx_ok else None
				waste_y = wy_val if wy_ok else None

		if errors:
			messagebox.showerror(
				"Cannot start fractionation",
				"Please correct the following:\n\n"
				+ "\n".join("• " + e for e in errors),
				parent=self,
			)
			return

		# Overlap warning: waste bin inside the plate footprint
		if d_v > 0 and waste_x is not None and waste_y is not None:
			plate_x_lo = table_v
			plate_x_hi = table_v + cols_v * ws_v
			plate_y_lo = carriage_v
			plate_y_hi = carriage_v + rows_v * ws_v
			# Footprint rectangle is the convex hull of the snake travel,
			# allowing for either direction; use min/max for symmetry.
			lo_x, hi_x = sorted((plate_x_lo, plate_x_hi))
			lo_y, hi_y = sorted((plate_y_lo, plate_y_hi))
			if lo_x <= waste_x <= hi_x and lo_y <= waste_y <= hi_y:
				go = messagebox.askyesno(
					"Waste bin overlaps plate footprint",
					f"Waste bin position ({waste_x} cm, {waste_y} cm) appears "
					f"to overlap the plate footprint. Discarded fractions will "
					f"be dispensed onto a plate well. Continue anyway?",
					default=messagebox.NO,
					parent=self,
				)
				if not go:
					return

		# Lock in the project as "committed" so subsequent edits trigger the
		# mid-run confirmation dialog.
		self._project_last_committed = project_v
		self.app.start_run(
			rows_v, cols_v, ws_v, rate_v, vol_v,
			project=project_v, sample_id_at_start=sample_v,
			plate_id_at_start=plate_v,
			number_of_fractions=n_v, discard_fractions=d_v,
			waste_bin_table=waste_x if waste_x is not None else 0.0,
			waste_bin_carriage=waste_y if waste_y is not None else 0.0,
			table_start=table_v, carriage_start=carriage_v,
			drip_wait_time=drip_v,
		)

	# -- WellPlateProgress shortcuts (called by App's state machine) ----

	def begin_run(self, cols, rows, volume_per_well, pump_time):
		self.progress.begin_run(cols, rows, volume_per_well, pump_time)

	def well_dispensing(self, x, y):
		self.progress.well_dispensing(x, y)

	def well_waiting(self, x, y):
		self.progress.well_waiting(x, y)

	def well_completed(self, x, y, color=None, sequence=None,
			sample_id=None, color_name=None):
		"""Forward per-sample color/sequence info to the progress widget so
		completed wells render with the right palette + within-series number."""
		self.progress.well_completed(
			x, y, color=color, sequence=sequence,
			sample_id=sample_id, color_name=color_name,
		)

	def stop_progress_view(self):
		"""Stop the well-plate widget's pulse + clock without clearing the
		grid -- called when the state machine's run is finishing or being
		halted. (App.end_run() is a different entry point.)"""
		self.progress.end_run()


class ManualFrame(tk.Frame):
	"""Manual mode -- free needle positioning + manual pump operation.

	For testing, alignment, and troubleshooting only. The fractionation
	sequence (rows/cols/well size + Begin/Pause) lives in Automated mode;
	this frame does NOT carry those inputs anymore.
	"""

	# Available jog step sizes (label shown to the user, cm value stored
	# in self.step_var). 1 mm is the default per the spec.
	_STEPS = [("0.1 mm", 0.01), ("1 mm", 0.1), ("10 mm", 1.0)]

	# Soft travel limits (matches validation.TABLE_POS_MAX / CARRIAGE_POS_MAX
	# but enforced here on the jog path rather than at submit time).
	# The Y range is [-15, 0] (not [0, 15]) so from home (motor=0) the
	# down arrow (Y-) is the one that moves the needle into valid travel;
	# the up arrow (Y+) is refused, matching the plate's upper-left origin.
	_X_MIN, _X_MAX = 0.0, 20.0
	_Y_MIN, _Y_MAX = -15.0, 0.0

	def __init__(self, master, app):
		super().__init__(master)
		self.app = app

		self.grid_columnconfigure(0, weight=1)

		# ---- Jog Controls ----
		jog = tk.LabelFrame(self, text="Jog Controls", padx=8, pady=8)
		jog.grid(row=0, column=0, sticky="new", padx=4, pady=4)
		jog.grid_columnconfigure(0, weight=1)

		# Plus-pad of directional buttons. Corners empty.
		pad = tk.Frame(jog)
		pad.grid(row=0, column=0, pady=(0, 8))
		self.y_plus_btn = tk.Button(
			pad, text="▲ Y+", width=8,
			command=lambda: self._jog("y", +1),
		)
		self.y_plus_btn.grid(row=0, column=1, padx=2, pady=2)
		self.x_minus_btn = tk.Button(
			pad, text="◀ X−", width=8,
			command=lambda: self._jog("x", -1),
		)
		self.x_minus_btn.grid(row=1, column=0, padx=2, pady=2)
		self.x_plus_btn = tk.Button(
			pad, text="X+ ▶", width=8,
			command=lambda: self._jog("x", +1),
		)
		self.x_plus_btn.grid(row=1, column=2, padx=2, pady=2)
		self.y_minus_btn = tk.Button(
			pad, text="Y− ▼", width=8,
			command=lambda: self._jog("y", -1),
		)
		self.y_minus_btn.grid(row=2, column=1, padx=2, pady=2)

		# Step-size radio group
		self.step_var = tk.DoubleVar(value=0.1)  # default 1 mm
		step_row = tk.Frame(jog)
		step_row.grid(row=1, column=0, sticky="w", pady=(0, 6))
		tk.Label(step_row, text="Step:").pack(side=tk.LEFT, padx=(0, 6))
		for label, value in self._STEPS:
			tk.Radiobutton(
				step_row, text=label, variable=self.step_var, value=value,
			).pack(side=tk.LEFT, padx=2)

		# Home button -- drives carriage_return(). Primary action: the
		# operator's calibration anchor for Manual jogging.
		self.home_btn = primary_button(jog, text="Home", command=self._home_clicked)
		self.home_btn.grid(row=2, column=0, sticky="we", pady=(0, 6))

		# Position readout
		self.position_var = tk.StringVar(value="Position: X = 0.000 cm, Y = 0.000 cm")
		self.position_lbl = tk.Label(jog, textvariable=self.position_var, anchor="w")
		self.position_lbl.grid(row=3, column=0, sticky="we")

		# ---- Pump Controls ----
		pump = tk.LabelFrame(self, text="Pump Controls", padx=8, pady=8)
		pump.grid(row=1, column=0, sticky="new", padx=4, pady=(0, 4))
		pump.grid_columnconfigure(0, weight=1)
		pump.grid_columnconfigure(1, weight=1)

		# Layout each pump button as a (button + hint) stack so the
		# "(Space)" hint can be gridded immediately to the right of the
		# button without shifting button widths. Only one hint is gridded
		# at a time -- the one for app.last_pump_used.
		frac_wrap = tk.Frame(pump)
		frac_wrap.grid(row=0, column=0, sticky="we", padx=(0, 4), pady=4)
		frac_wrap.grid_columnconfigure(0, weight=1)
		self.fractionate_btn = tk.Button(
			frac_wrap, text="Fractionate: OFF",
			command=lambda: app._handle_pump_click("fractionate", parent=self),
			font=FONTS["bold"], relief="flat", borderwidth=0,
			highlightthickness=0, padx=10, pady=6, cursor="hand2",
		)
		self.fractionate_btn.grid(row=0, column=0, sticky="we")
		self.fractionate_space_lbl = tk.Label(
			frac_wrap, text="(Space)", fg="gray40",
		)
		# Not gridded by default; ``_set_space_hint`` decides which side
		# is visible based on app.last_pump_used.

		purge_wrap = tk.Frame(pump)
		purge_wrap.grid(row=0, column=1, sticky="we", padx=(4, 0), pady=4)
		purge_wrap.grid_columnconfigure(0, weight=1)
		self.purge_btn = tk.Button(
			purge_wrap, text="Purge: OFF",
			command=lambda: app._handle_pump_click("purge", parent=self),
			font=FONTS["bold"], relief="flat", borderwidth=0,
			highlightthickness=0, padx=10, pady=6, cursor="hand2",
		)
		self.purge_btn.grid(row=0, column=0, sticky="we")
		self.purge_space_lbl = tk.Label(
			purge_wrap, text="(Space)", fg="gray40",
		)

	def refresh(self):
		"""Re-sync the position readout and clear the per-visit pump-confirm
		set so the first click on EACH pump (Fractionate, Purge) after
		switching INTO Manual mode shows the relay-activation warning once
		for that pump. Also re-anchor the "(Space)" hint label next to the
		currently-active pump so it's visible immediately on mode entry.
		"""
		self.app._manual_pumps_confirmed.clear()
		self.refresh_position_readout()
		self._set_space_hint(self.app.last_pump_used)

	def _jog(self, axis, sign):
		"""Move one step in (axis, sign) direction; refuse if it would exceed limits.

		The step distance is read live from ``step_var`` (cm) and passed to
		``motor.move_dist_relative``, the same entry point Automated mode's
		Move button uses -- so a 10 mm jog and a 1.0 cm Automated move
		travel identical physical distances. The prospective target is
		computed from the *live* motor angle so the limit check is robust
		even if the position label ever drifts from the motor state.
		"""
		step_cm = self.step_var.get() * sign
		if axis == "x":
			motor = self.app.table_motor
			lo, hi, label = self._X_MIN, self._X_MAX, "X-axis"
		else:
			motor = self.app.carriage_motor
			lo, hi, label = self._Y_MIN, self._Y_MAX, "Y-axis"

		current_cm = motor.get_angle() * motor.cm_per_deg
		target_cm = current_cm + step_cm
		if target_cm < lo:
			self.app.set_status(f"{label} at soft limit: {lo:.1f} cm")
			return
		if target_cm > hi:
			self.app.set_status(f"{label} at soft limit: {hi:.1f} cm")
			return

		motor.move_dist_relative(step_cm)
		self.refresh_position_readout()

	def _home_clicked(self):
		"""Return both axes to physical home AND snap the software angle
		counters to zero. The physical-home position is the ground truth;
		taring after the move guarantees the readout reads exactly
		``0.000 cm`` even if step-counting drift accumulated during the
		session.
		"""
		self.app.carriage_return()
		self.app.table_motor.tare()
		self.app.carriage_motor.tare()
		self.refresh_position_readout()

	def refresh_position_readout(self):
		"""Re-render the Position: X = ..., Y = ... label from live motor
		angles. Called by ``_jog``, ``_home_clicked``, and ``refresh`` so
		any motion the user can initiate from this frame updates the
		display immediately.
		"""
		x = self.app.table_motor.get_angle() * self.app.table_motor.cm_per_deg
		y = self.app.carriage_motor.get_angle() * self.app.carriage_motor.cm_per_deg
		self.position_var.set(f"Position: X = {x:.3f} cm, Y = {y:.3f} cm")

	def _set_space_hint(self, pump_name):
		"""Show the "(Space)" hint label next to ``pump_name``'s button and
		hide it next to the other one. Called on mode entry and any time
		the operator clicks one of the pump buttons via the App-level
		``_handle_pump_click`` (which writes ``last_pump_used``).
		"""
		if pump_name == "fractionate":
			self.fractionate_space_lbl.grid(row=0, column=1, padx=(4, 0))
			self.purge_space_lbl.grid_remove()
		else:
			self.purge_space_lbl.grid(row=0, column=1, padx=(4, 0))
			self.fractionate_space_lbl.grid_remove()

	def set_controls_enabled(self, enabled):
		"""Disable jog + Home buttons after Terminate; pump buttons are
		separately managed by ``refresh_pump_buttons``."""
		state = tk.NORMAL if enabled else tk.DISABLED
		for btn in (
			self.y_plus_btn, self.x_minus_btn, self.x_plus_btn, self.y_minus_btn,
			self.home_btn,
		):
			btn["state"] = state

	def refresh_pump_buttons(self, claimant, relay_on, in_run):
		_update_pump_button(self.fractionate_btn, "fractionate", claimant, relay_on, in_run)
		_update_pump_button(self.purge_btn, "purge", claimant, relay_on, in_run)


class CleaningFrame(tk.Frame):
	"""Cleaning mode: move the needle to the waste bin and run the Purge
	pump to flush the lines. The waste-bin coords are the SAME ones the
	operator entered in Automated mode (shared via App-level StringVars),
	so the waste container's physical position is configured once."""

	def __init__(self, master, app):
		super().__init__(master)
		self.app = app

		self.grid_columnconfigure(0, weight=1)
		self.grid_columnconfigure(1, weight=0)

		# Waste-bin coords -- bound to the same App-level StringVars as
		# Automated mode's Waste bin entries, so an edit in either mode
		# propagates automatically.
		bin_frame = tk.LabelFrame(self, text="Waste bin", padx=8, pady=4)
		bin_frame.grid(row=0, column=0, columnspan=2, sticky="we", padx=2, pady=(2, 4))
		bin_frame.grid_columnconfigure(0, weight=1)
		self.waste_table_te = TextEntry(
			bin_frame, "Waste bin: table position (cm):",
			textvariable=app.waste_bin_table_var,
		)
		self.waste_table_te.grid(row=0, column=0, sticky="we")
		self.waste_carriage_te = TextEntry(
			bin_frame, "Waste bin: carriage position (cm):",
			textvariable=app.waste_bin_carriage_var,
		)
		self.waste_carriage_te.grid(row=1, column=0, sticky="we")

		self.move_btn = primary_button(
			self, text="Move to Waste Bin", command=self.move_clicked,
		)
		self.move_btn.grid(row=1, column=0, columnspan=2, sticky="we", padx=2, pady=2)
		self.purge_btn = tk.Button(
			self, text="Purge: OFF",
			command=lambda: app._handle_pump_click("purge", parent=self),
			font=FONTS["bold"], relief="flat", borderwidth=0,
			highlightthickness=0, padx=10, pady=6, cursor="hand2",
		)
		self.purge_btn.grid(row=2, column=0, columnspan=2, sticky="we", padx=2, pady=2)

	def refresh(self):
		self.waste_table_te.clear_error()
		self.waste_carriage_te.clear_error()
		s = self.app.state
		s.carriage_forwards = True
		self.app.set_status("System idle.")

	def set_controls_enabled(self, enabled):
		self.move_btn["state"] = tk.NORMAL if enabled else tk.DISABLED

	def refresh_pump_buttons(self, claimant, relay_on, in_run):
		_update_pump_button(self.purge_btn, "purge", claimant, relay_on, in_run)

	def move_clicked(self):
		"""Move the needle to the waste-bin position. Both coords are
		validated; either being out-of-range surfaces inline + halts the move.
		Empty fields are allowed (only the populated axis moves)."""
		t_ok, t_val = validation.table_pos(self.waste_table_te.get(), allow_empty=True)
		c_ok, c_val = validation.carriage_pos(self.waste_carriage_te.get(), allow_empty=True)
		(self.waste_table_te.clear_error if t_ok else lambda: self.waste_table_te.show_error(t_val))()
		(self.waste_carriage_te.clear_error if c_ok else lambda: self.waste_carriage_te.show_error(c_val))()
		if not (t_ok and c_ok):
			return
		if t_val is None and c_val is None:
			return
		self.app.move_to_positions(table_dist=t_val, carriage_dist=c_val)


class App(tk.Tk):
	"""Main fractionator GUI.

	Owns the hardware backends, the StepperMotor wrappers, the pump relay, a
	``FractionatorState`` instance, and one instance of each mode frame. Mode
	switching is done by ``grid_remove()`` / ``grid()`` on whole frames; no
	widget is destroyed or rebuilt at runtime.
	"""

	def __init__(self, backends):
		super().__init__()
		# Apply the unified style BEFORE constructing any widgets --
		# option_add defaults inside apply_style are consulted at widget
		# creation time, not retroactively, so this must come first.
		apply_style(self)
		# Title is updated dynamically in set_mode; this initial value
		# matches the format the user sees the moment the GUI paints.
		self.title(f"autoSIP Controller v{__version__} — Automated Mode")
		# Lock in a stable initial size so Tk doesn't auto-resize as label
		# text widths fluctuate ("Well 9 of 96" → "Well 10 of 96", clock
		# ticks, etc.). Without this, the WellPlateProgress header labels
		# can drive the root window into a width-oscillation feedback loop.
		# (apply_style also enforces a font-derived minsize floor.)
		self.geometry("900x950")
		self.backends = backends

		# Hardware wrappers -- constructed once, reused across mode switches.
		self.table_motor = StepperMotor(
			backends.stepper2, NEMA_17_STEPS_PER_DEGREE, LEAD_SCREW_PITCH_IN_CM,
			reverse=True, name="table",
		)
		self.carriage_motor = StepperMotor(
			backends.stepper1, NEMA_17_STEPS_PER_DEGREE, LEAD_SCREW_PITCH_IN_CM,
			reverse=True, name="carriage",
		)

		self.state = FractionatorState()
		self.mode = None
		self._active_frame = None
		self._terminated = False
		# Track which pumps have shown the "Activating the relay" confirm at
		# least once during the current Manual-mode visit. Each pump prompts
		# on its first activation; subsequent activations of THE SAME pump
		# are suppressed. Switching to/from Manual clears the set.
		self._manual_pumps_confirmed = set()
		# Which pump the space-bar shortcut targets in Manual mode. Updated
		# every time the user clicks (or space-activates) one of the two
		# pump buttons, and persisted across launches via config_store.
		self.last_pump_used = config_store.load_last_pump_used()
		# Per-run on-disk logger. None when no run is active. Set on
		# start_run, cleared on run end / terminate.
		self.run_logger = None
		# Path of the most recent run's folder, for "Open last run folder".
		self._last_run_path = None

		# Shared Tk variables for fields that appear in more than one frame.
		# Both Automated mode's Waste bin entries and Cleaning mode's Waste
		# bin entries bind to these via TextEntry's textvariable= argument,
		# so an edit in either mode propagates automatically -- the waste
		# container's physical position is one fact, not a per-mode setting.
		# Must be created BEFORE the frames so each frame can reference
		# them in its constructor.
		self.waste_bin_table_var = tk.StringVar()
		self.waste_bin_carriage_var = tk.StringVar()

		# Root layout: 3 rows (header / mode body / status) in a single column
		# that expands with the window.
		self.grid_columnconfigure(0, weight=1)
		self.grid_rowconfigure(1, weight=1)

		self.header = HeaderFrame(self, self)
		self.header.grid(row=0, column=0, sticky="we")

		self.automated_frame = AutomatedFrame(self, self)
		self.manual_frame = ManualFrame(self, self)
		self.cleaning_frame = CleaningFrame(self, self)
		self._frames = {
			"Automated": self.automated_frame,
			"Manual": self.manual_frame,
			"Cleaning": self.cleaning_frame,
		}

		self.status_bar = StatusBarFrame(self, self)
		self.status_bar.grid(row=2, column=0, sticky="we")

		# Pump goes through PumpController so every state change updates both
		# the status-bar indicator and the per-frame pump buttons. The state
		# machine (pump_liquid / stop_pump) and the user-click handler
		# (_handle_pump_click) both go through this controller -- never the
		# raw relay backend.
		self.pump_controller = PumpController(backends.relay)
		self.pump_controller.subscribe(self._on_pump_state_change)

		# Mirror autosip log lines into the status bar's right-hand label.
		self._log_handler = StringVarLogHandler(self.status_bar.log_var)
		logging.getLogger("autosip").addHandler(self._log_handler)

		self.set_mode("Automated")
		# Sync every frame's pump button to the controller's initial idle state.
		self._refresh_pump_buttons()

		# Menubar (File > Profiles, Tools > Open last run folder, Help > About)
		self._build_menu()

		# Seed any starter profiles that ship with the repo (only copies
		# missing ones; idempotent across launches), then restore the
		# operator's last-used entry values.
		try:
			config_store.seed_starter_profiles(Path(__file__).parent / "profiles")
		except OSError as exc:
			logger.warning("Could not seed starter profiles: %s", exc)
		last_used = config_store.load_last_used()
		if last_used:
			self.automated_frame.set_values(last_used)

		# One-time INFO breadcrumb if the old ~/.autosip/runs/ tree exists
		# from earlier versions. We do NOT auto-migrate -- moving the
		# operator's files would surprise them.
		old_runs = Path.home() / ".autosip" / "runs"
		if old_runs.exists():
			import run_logger as _rl
			logger.info(
				"Older run logs found at %s. New runs will be written to %s. "
				"Move the old data manually if you want it alongside.",
				old_runs, _rl.DEFAULT_LOGS_DIR,
			)

		# Intercept window-close so a run in progress is recorded as
		# "manual_abort" rather than just orphaned on disk.
		self.protocol("WM_DELETE_WINDOW", self._on_close)

		# Space-bar shortcut: toggle the most-recently-used pump in Manual
		# mode. Bound at root with ``bind_all`` so it fires regardless of
		# which widget has focus, but the handler self-gates on mode and
		# on the focused widget type (passes through to text-entry widgets
		# so users can still type a space character there).
		self.bind_all("<KeyPress-space>", self._on_space)

		# Seed the run-control button row to its idle state.
		self._update_run_control_buttons()

		# One-time initialization wiggle to seat the lead screws against a
		# known direction of backlash. Lives here (not in a mode's refresh)
		# because the motors themselves persist across mode switches.
		self.table_motor.move_dist_relative(-0.1)
		self.carriage_motor.move_dist_relative(-0.1)

	def _build_menu(self):
		"""Top-level menubar with File + Tools + Help."""
		menubar = tk.Menu(self)

		file_menu = tk.Menu(menubar, tearoff=False)
		file_menu.add_command(label="Save current as profile...", command=self._save_profile_as)
		file_menu.add_command(label="Load profile...", command=self._load_profile)
		file_menu.add_command(label="Delete profile...", command=self._delete_profile)
		menubar.add_cascade(label="File", menu=file_menu)

		tools = tk.Menu(menubar, tearoff=False)
		tools.add_command(label="Open last run folder", command=self._open_last_run)
		menubar.add_cascade(label="Tools", menu=tools)

		help_menu = tk.Menu(menubar, tearoff=False)
		help_menu.add_command(label="About", command=self._show_about_dialog)
		menubar.add_cascade(label="Help", menu=help_menu)

		self.config(menu=menubar)

	def _show_about_dialog(self):
		"""Custom About window: version + clickable GitHub link + the
		citation reminder. Uses a Toplevel (not ``messagebox.showinfo``)
		because the citation text needs to read as a distinct section
		and the repo URL needs to be clickable."""
		win = tk.Toplevel(self)
		win.title("About autoSIP")
		win.configure(bg=PALETTE["bg_frame"])
		win.resizable(False, False)
		win.transient(self)

		container = tk.Frame(win, bg=PALETTE["bg_frame"], padx=24, pady=20)
		container.pack(fill=tk.BOTH, expand=True)

		tk.Label(container, text="autoSIP Controller",
			font=FONTS["heading"], bg=PALETTE["bg_frame"],
			fg=PALETTE["fg_text"]).pack(anchor="w")
		tk.Label(container, text=f"Version {__version__}",
			font=FONTS["body"], bg=PALETTE["bg_frame"],
			fg=PALETTE["fg_muted"]).pack(anchor="w", pady=(0, 12))
		tk.Label(container,
			text="Open-source Raspberry-Pi-controlled robotic gradient\n"
			     "fractionator for stable isotope probing experiments.",
			font=FONTS["body"], bg=PALETTE["bg_frame"],
			fg=PALETTE["fg_text"], justify="left",
		).pack(anchor="w", pady=(0, 12))

		# Repository link -- clickable, underlined, accent-colored.
		repo_section = tk.Frame(container, bg=PALETTE["bg_frame"])
		repo_section.pack(anchor="w", pady=(0, 12), fill=tk.X)
		tk.Label(repo_section, text="Source: ", font=FONTS["body"],
			bg=PALETTE["bg_frame"], fg=PALETTE["fg_text"],
		).pack(side=tk.LEFT)
		link = tk.Label(repo_section, text=_GITHUB_URL,
			font=(FONTS["family"], FONTS["size"], "underline"),
			bg=PALETTE["bg_frame"], fg=PALETTE["accent"], cursor="hand2")
		link.pack(side=tk.LEFT)
		link.bind("<Button-1>", lambda _e: webbrowser.open(_GITHUB_URL))

		# Citation reminder -- its own LabelFrame so the text is set
		# apart from the link block above. Wording is exact per spec.
		citation_box = tk.LabelFrame(
			container, text="Citation",
			font=FONTS["bold"], bg=PALETTE["bg_frame"],
			fg=PALETTE["fg_text"], padx=12, pady=10,
		)
		citation_box.pack(fill=tk.X, pady=(4, 12))
		tk.Label(citation_box,
			text="If you use autoSIP, please cite\n"
			     "Laud et al. 2026 (in preparation, HardwareX).",
			font=FONTS["body"], bg=PALETTE["bg_frame"],
			fg=PALETTE["fg_text"], justify="left",
		).pack(anchor="w")

		primary_button(container, text="Close", command=win.destroy,
		).pack(anchor="e")

		# Center over the main window. Done after pack so the requested
		# size is known.
		win.update_idletasks()
		x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
		y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
		win.geometry(f"+{max(0, x)}+{max(0, y)}")
		win.grab_set()

	# -- Profile menu handlers ------------------------------------------

	def _save_profile_as(self):
		"""Prompt for a name, write the current field values to a profile."""
		raw = simpledialog.askstring(
			"Save profile as",
			"Profile name:",
			parent=self,
		)
		if raw is None:
			return
		# Strip path separators and the .json suffix so the user's input
		# can't escape the profiles directory.
		name = Path(raw.strip()).name
		if name.endswith(".json"):
			name = name[:-5]
		if not name:
			messagebox.showerror(
				"Invalid name",
				"Profile name cannot be empty.",
				parent=self,
			)
			return
		if name in config_store.list_profiles():
			if not messagebox.askyesno(
				"Overwrite?",
				f"Profile {name!r} already exists. Overwrite?",
				parent=self,
			):
				return
		try:
			config_store.save_profile(name, self.automated_frame.get_values())
			logger.info("Saved profile %s", name)
		except OSError as exc:
			messagebox.showerror(
				"Save failed",
				f"Could not save profile {name!r}:\n{exc}",
				parent=self,
			)

	def _load_profile(self):
		"""Show a picker, load the chosen profile's values into the entries."""
		name = self._pick_profile_dialog("Load profile", action_label="Load")
		if not name:
			return
		try:
			values = config_store.load_profile(name)
		except (OSError, json.JSONDecodeError, ValueError) as exc:
			messagebox.showerror(
				"Load failed",
				f"Could not load profile {name!r}:\n{exc}",
				parent=self,
			)
			return
		self.automated_frame.set_values(values)
		# Loading is a deliberate "use these values now" action; persist
		# them as last_used so a relaunch picks up the same set.
		try:
			config_store.save_last_used(self.automated_frame.get_values())
		except OSError as exc:
			logger.warning("Failed to save last_used after load: %s", exc)
		logger.info("Loaded profile %s", name)

	def _delete_profile(self):
		name = self._pick_profile_dialog("Delete profile", action_label="Delete")
		if not name:
			return
		if not messagebox.askyesno(
			"Delete profile?",
			f"Permanently delete profile {name!r}?",
			parent=self,
		):
			return
		try:
			config_store.delete_profile(name)
			logger.info("Deleted profile %s", name)
		except OSError as exc:
			messagebox.showerror(
				"Delete failed",
				f"Could not delete profile {name!r}:\n{exc}",
				parent=self,
			)

	def _pick_profile_dialog(self, title, action_label="OK"):
		"""Modal Listbox of profile names. Returns the chosen name, or None
		if the user cancelled or there are no profiles."""
		names = config_store.list_profiles()
		if not names:
			messagebox.showinfo(
				"No profiles",
				"No saved profiles yet. Use 'Save current as profile...' first.",
				parent=self,
			)
			return None

		dlg = tk.Toplevel(self)
		dlg.title(title)
		dlg.transient(self)
		dlg.grab_set()

		tk.Label(dlg, text=title, padx=8, pady=4).pack()
		lb = tk.Listbox(dlg, height=min(10, max(3, len(names))))
		for n in names:
			lb.insert(tk.END, n)
		lb.pack(padx=8, pady=4, fill=tk.BOTH, expand=True)
		lb.selection_set(0)

		result = {"name": None}

		def _ok():
			sel = lb.curselection()
			if sel:
				result["name"] = names[sel[0]]
			dlg.destroy()

		def _cancel():
			dlg.destroy()

		btn_row = tk.Frame(dlg)
		btn_row.pack(pady=(0, 6))
		tk.Button(btn_row, text=action_label, command=_ok).pack(side=tk.LEFT, padx=4)
		tk.Button(btn_row, text="Cancel", command=_cancel).pack(side=tk.LEFT, padx=4)
		# Enter activates OK; double-click in the list also activates.
		dlg.bind("<Return>", lambda e: _ok())
		dlg.bind("<Escape>", lambda e: _cancel())
		lb.bind("<Double-Button-1>", lambda e: _ok())

		self.wait_window(dlg)
		return result["name"]

	def _open_last_run(self):
		"""Open the most recent run folder in the OS file browser. If no
		run has happened yet, open the logs/ root (create it if missing)."""
		import run_logger as _rl
		if self._last_run_path is not None and Path(self._last_run_path).exists():
			path = str(self._last_run_path)
		else:
			# Create logs/ on demand so the menu item works pre-first-run.
			logs_root = _rl.DEFAULT_LOGS_DIR
			try:
				logs_root.mkdir(parents=True, exist_ok=True)
			except OSError as exc:
				messagebox.showerror(
					"Open folder failed",
					f"Could not create logs directory:\n{logs_root}\n\n{exc}",
					parent=self,
				)
				return
			path = str(logs_root)
		try:
			if sys.platform == "darwin":
				subprocess.Popen(["open", path])
			elif sys.platform.startswith("linux"):
				subprocess.Popen(["xdg-open", path])
			else:
				# Windows / unknown: fall back to showing the path.
				messagebox.showinfo(
					"Run folder",
					f"Run folder is at:\n{path}",
					parent=self,
				)
		except (FileNotFoundError, OSError) as exc:
			messagebox.showerror(
				"Open folder failed",
				f"Could not open:\n{path}\n\n{exc}",
				parent=self,
			)

	def _on_close(self):
		"""WM_DELETE_WINDOW handler. Records a run-in-flight as
		``manual_abort`` so end.json + summary.md still land on disk before
		the window goes away."""
		if self.run_logger is not None:
			# If a well or discard cycle was in the middle of dispense/wait,
			# commit it so log.csv has an entry rather than silently dropping.
			if self.state.state in ("pump", "wait"):
				if self.state.phase == "discard":
					self.run_logger.discard_emergency_stopped(
						self.state.series_index,
						self.state.discards_done + 1)
				else:
					self.run_logger.well_emergency_stopped(
						self.state.x, self.state.y)
			try:
				snap = self.automated_frame.progress.snapshot()
				self.run_logger.end("manual_abort", snapshot=snap,
				plates_used=self.state.plates_used,
				well_records=self.state.well_records)
			except Exception as exc:
				logger.warning("Run logger failed to close on window close: %s", exc)
			self.run_logger = None
		self.destroy()

	# -- Mode switching ---------------------------------------------------

	def set_mode(self, name):
		"""Show the frame for ``name`` and call its ``refresh()``.

		Pause and Terminate Run buttons are hidden in non-Automated modes
		since they only operate on fractionation runs; Return-to-home stays
		visible across all modes.
		"""
		if self._active_frame is not None:
			self._active_frame.grid_remove()
		frame = self._frames[name]
		frame.grid(row=1, column=0, sticky="nsew")
		self._active_frame = frame
		self.mode = name
		self.title(f"autoSIP Controller v{__version__} — {name} Mode")
		self.header.set_mode_label(name)
		self.status_bar.set_mode(name)
		self.status_bar.set_terminate_visible(name == "Automated")
		frame.refresh()
		logger.debug("Switched to %s mode", name)

	def request_mode_change(self, name):
		"""Switch to mode ``name`` after confirming any paused-run override.

		Both the header tabs and ``cycle_mode`` route through here so the
		"Fractionation is paused. Switching modes will lose progress"
		prompt fires regardless of which mechanic triggered the change.
		No-op when ``name`` matches the current mode.
		"""
		if name == self.mode:
			return
		if self.state.is_paused and self.state.state != "idle":
			if not messagebox.askyesno(
				"Switch modes while paused?",
				"Fractionation is paused. Switching modes will lose progress. Continue?",
				parent=self,
			):
				return
		self.set_mode(name)

	def cycle_mode(self):
		"""Cycle Automated -> Manual -> Cleaning -> Automated.

		Retained for keyboard-shortcut / programmatic callers; the header
		tabs use ``request_mode_change`` directly. Both paths share the
		paused-confirm dialog.
		"""
		next_idx = (MODE_ORDER.index(self.mode) + 1) % len(MODE_ORDER)
		self.request_mode_change(MODE_ORDER[next_idx])

	# -- Terminate run ---------------------------------------------------

	def terminate_run(self):
		"""Hard-halt: cancel motion, kill the pump, release motors, lock down
		controls. Pause is the preferred mid-run interrupt; this is the
		heavy hammer for "something is wrong, I'll verify hardware". The
		button is stop-sign-shaped, lives in the far bottom-right of the
		status bar, and requires explicit confirmation -- so it can't be
		hit by accident. Stays in halted mode until the user clicks Return
		to home.
		"""
		if not messagebox.askyesno(
			"Terminate run?",
			"Are you sure that you wish to stop the whole run?",
			parent=self,
		):
			return

		# Cancel any pending pump/move callback
		if self.state.taskId is not None:
			self.after_cancel(self.state.taskId)
			self.state.taskId = None

		# Snapshot the pre-reset state machine state for the run logger
		# (we need to know which well or discard cycle, if any, was in
		# flight when the user hit terminate).
		pre_state = self.state.state
		pre_phase = self.state.phase
		pre_x = self.state.x
		pre_y = self.state.y
		pre_discards_done = self.state.discards_done

		# Reset run state BEFORE releasing the pump so the resulting
		# controller notification sees state.state == "idle" and frame
		# pump buttons come out of "in-run disabled" mode.
		s = self.state
		s.state = "idle"
		s.phase = "idle"
		s.is_paused = False

		# Pump off + claim cleared, motors released
		self.pump_controller.release()
		self.table_motor.release()
		self.carriage_motor.release()

		# Close out the on-disk run log. If terminate landed mid-dispense or
		# mid-wait, mark the in-flight entry as emergency_stopped before the
		# end() call so it shows up in log.csv with that status.
		if self.run_logger is not None:
			if pre_state in ("pump", "wait"):
				if pre_phase == "discard":
					self.run_logger.discard_emergency_stopped(
						self.state.series_index, pre_discards_done + 1)
				else:
					self.run_logger.well_emergency_stopped(pre_x, pre_y)
			snap = self.automated_frame.progress.snapshot()
			self.run_logger.end("emergency_stopped", snapshot=snap,
				plates_used=self.state.plates_used,
				well_records=self.state.well_records)
			self.run_logger = None

		# Run-control buttons reflect the new idle/estopped state.
		self._update_run_control_buttons()

		# Offer to save a plate-state snapshot to a file BEFORE we clear the
		# view -- the snapshot is the only record of where the run got to.
		# The file dialog's Cancel button = "don't save" (no separate y/n
		# step, just one click to dismiss). Default filename embeds a
		# timestamp so multiple terminates don't overwrite each other.
		snap = self.automated_frame.progress.snapshot()
		if snap["rows"] and snap["cols"]:
			default_name = f"autosip_snapshot_{strftime('%Y%m%d_%H%M%S')}.txt"
			path = filedialog.asksaveasfilename(
				parent=self,
				title="Save plate snapshot (Cancel to skip)",
				defaultextension=".txt",
				initialfile=default_name,
				filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
			)
			if path:
				try:
					with open(path, "w") as f:
						f.write(format_snapshot_log(snap))
						f.write("\n")
					logger.info("Plate snapshot saved to %s", path)
				except OSError as exc:
					logger.warning("Failed to save snapshot to %s: %s", path, exc)
					messagebox.showerror(
						"Snapshot save failed",
						f"Could not write snapshot to:\n{path}\n\n{exc}",
						parent=self,
					)

		# Reset the progress view (clears the plate AND stops pulse/clock).
		self.automated_frame.progress.reset()

		# Disable motion buttons and reset the run-control button row to
		# its e-stopped layout (everything disabled).
		self._set_controls_enabled(False)
		self.set_status(
			"Run terminated — click Return to Start Coords to re-enable controls.",
		)
		self._terminated = True
		self._update_run_control_buttons()
		logger.warning("Run terminated")

	def return_to_home(self):
		"""Move both stages to (0, 0) and re-enable controls if disabled.

		Always available, not gated on a prior Terminate. Useful both as
		the recovery action after a Terminate AND as a "recenter the rig"
		convenience between runs. Despite the method name (kept for
		backward compatibility), in Automated mode this is the
		"Return to Start Coords" button -- it ends up at the operator's
		plate-start position only because the App always tares the
		motor counters at run start, so motor 0 == plate-start coords.
		"""
		logger.info("Returning to start coords")
		self.carriage_return()
		self._set_controls_enabled(True)
		self._terminated = False
		self.set_status("Returned to start coords.")
		self._update_run_control_buttons()

	# -- Run-control button state machine -------------------------------

	_PAUSE_RUNNING_BG = "#27a72c"
	_PAUSE_PAUSED_BG = "#6F4E37"

	def _classify_ui_state(self):
		"""Map (state.state, is_paused, _terminated) to a single UI bucket.

		idle / running / paused_manual / paused_total / paused_plate_full /
		estopped
		"""
		if self._terminated:
			return "estopped"
		s = self.state
		if s.state == "plate_full":
			return "paused_plate_full"
		if s.state == "total_reached":
			return "paused_total"
		if s.is_paused:
			return "paused_manual"
		if s.state in ("pump", "wait", "move"):
			return "running"
		return "idle"

	def _update_run_control_buttons(self):
		"""Sync the five run-control buttons in AutomatedFrame to the current
		state machine state. Called at every state transition so the user
		sees an immediate response."""
		af = self.automated_frame
		s = self.state
		bucket = self._classify_ui_state()

		# Defaults; per-bucket overrides follow.
		ret_state = tk.NORMAL
		pause_state = tk.DISABLED
		pause_text = "Pause"
		pause_bg = af._pause_default_bg
		cont_state = tk.DISABLED
		cont_plate_state = tk.DISABLED
		end_state = tk.DISABLED

		if bucket == "idle":
			ret_state = tk.NORMAL
			pause_state = tk.DISABLED
			pause_bg = af._pause_default_bg
		elif bucket == "running":
			ret_state = tk.DISABLED
			pause_state = tk.NORMAL
			pause_text = "Pause"
			pause_bg = self._PAUSE_RUNNING_BG
			end_state = tk.NORMAL
		elif bucket == "paused_manual":
			ret_state = tk.DISABLED
			pause_state = tk.NORMAL
			pause_text = "Resume"
			pause_bg = self._PAUSE_PAUSED_BG
			end_state = tk.NORMAL
		elif bucket == "paused_total":
			# Sample complete, plate not full -- next action is Continue
			# to Next Sample (or End Run).
			ret_state = tk.DISABLED
			pause_state = tk.DISABLED
			pause_text = "Paused"
			pause_bg = af._pause_default_bg
			cont_state = tk.NORMAL
			end_state = tk.NORMAL
		elif bucket == "paused_plate_full":
			# Plate full -- Continue to Next Plate is the primary action.
			# Continue to Next Sample becomes available only if the sample
			# ALSO wrapped up on this well; otherwise it stays disabled
			# until after the plate swap resolves.
			ret_state = tk.DISABLED
			pause_state = tk.DISABLED
			pause_text = "Paused"
			pause_bg = af._pause_default_bg
			cont_plate_state = tk.NORMAL
			cont_state = tk.NORMAL if s.plate_full_with_sample_complete else tk.DISABLED
			end_state = tk.NORMAL
		elif bucket == "estopped":
			ret_state = tk.DISABLED
			pause_state = tk.DISABLED
			pause_bg = af._pause_default_bg

		af.return_btn["state"] = ret_state
		af.pause_btn["state"] = pause_state
		af.pause_btn["text"] = pause_text
		af.pause_btn["bg"] = pause_bg
		# fg on Pause button: white on the colored bgs, default-dark on gray.
		af.pause_btn["fg"] = "white" if pause_bg != af._pause_default_bg else "#222"
		af.continue_btn["state"] = cont_state
		af.continue_plate_btn["state"] = cont_plate_state
		af.end_run_btn["state"] = end_state

	def _set_controls_enabled(self, enabled):
		"""Toggle every frame's dangerous buttons in one call."""
		for frame in self._frames.values():
			if hasattr(frame, "set_controls_enabled"):
				frame.set_controls_enabled(enabled)

	# -- Status bar helper ------------------------------------------------

	def set_status(self, text):
		self.status_bar.set_text(text)

	# -- Manual jog -------------------------------------------------------

	def move_to_positions(self, table_dist=None, carriage_dist=None):
		"""Move table and/or carriage to absolute positions (cm)."""
		if table_dist is not None:
			self.table_motor.move_dist_absolute(table_dist)
		if carriage_dist is not None:
			self.carriage_motor.move_dist_absolute(carriage_dist)

	# -- Pump / pause -----------------------------------------------------

	def _handle_pump_click(self, name, parent=None):
		"""User-driven click on a Fractionate or Purge button.

		``name`` is ``"fractionate"`` or ``"purge"``. Walks through the same
		guards the buttons themselves enforce (interlock + in-run lockout)
		as a defensive fallback, then -- if turning ON -- shows the
		home-position warning (Fractionate only, outside Cleaning) and the
		"Activating the relay" confirmation before claiming + powering on.

		In Manual mode, also records the click as the most-recently-used
		pump for the space-bar shortcut (only after the click passes the
		early-out guards so a no-op click doesn't move the hint).
		"""
		pc = self.pump_controller
		parent = parent or self

		# State machine owns the pump during a run -- ignore user clicks.
		if self.state.state != "idle":
			return
		# Interlock: the opposite pump already holds the claim.
		if not pc.is_available_for(name):
			return

		# Manual-mode UX: this click counts as user intent, so it becomes
		# the new space-bar target. Save BEFORE the confirm prompt so even
		# a cancelled activation still moves the hint -- the user clearly
		# meant to operate this pump.
		if self.mode == "Manual" and name != self.last_pump_used:
			self.last_pump_used = name
			try:
				config_store.save_last_pump_used(name)
			except OSError as exc:
				logger.warning("Could not persist last_pump_used: %s", exc)
			self.manual_frame._set_space_hint(name)

		# We currently hold the claim with relay on -> click means turn off.
		if pc.claimant == name and pc.relay_on:
			pc.set_relay(False)
			pc.release()
			return

		# Claim held by us but relay off (only reachable through unusual
		# state-machine paths; defensive). Clear it.
		if pc.claimant == name and not pc.relay_on:
			pc.release()
			return

		# claimant is None and we want to turn this pump ON.
		# Relay-activation confirmation -- in Manual mode, prompt only the
		# first time EACH pump is activated per visit (Fractionate and Purge
		# track their confirms independently).
		suppress = self.mode == "Manual" and name in self._manual_pumps_confirmed
		if not suppress:
			if not messagebox.askyesno(
				"Activate pump",
				self._pump_confirm_text(name),
				parent=parent,
			):
				return
			if self.mode == "Manual":
				self._manual_pumps_confirmed.add(name)

		pc.claim_for(name)
		pc.set_relay(True)
		if self.mode == "Cleaning" and name == "purge":
			self.set_status("System purging.")

	def _on_space(self, event):
		"""Space-bar shortcut: toggle the last-used pump in Manual mode.

		Self-gates on mode and on the type of widget currently holding
		keyboard focus -- if the user is typing into an Entry or Text,
		space is a literal character there and we must NOT consume it.
		Returns ``"break"`` to prevent default activation on whichever
		button might be focused (which would otherwise fire a second
		toggle on the same key press).
		"""
		if self.mode != "Manual":
			return None
		focused = self.focus_get()
		if isinstance(focused, (tk.Entry, tk.Text)):
			return None
		self._handle_pump_click(self.last_pump_used, parent=self.manual_frame)
		return "break"

	def _pump_confirm_text(self, name):
		"""Body text for the relay-activation askyesno dialog."""
		if name == "fractionate":
			return (
				"Activating the relay (GPIO 5). Confirm before continuing:\n"
				"  • The Razel R-200 syringe pump is plugged into the relay outlet.\n"
				"  • Any other pumps are unplugged or switched off at their own switch.\n"
				"  • Tubing is routed to your intended container.\n"
				"\n"
				"Power on?"
			)
		# purge
		return (
			"Activating the relay (GPIO 5). Confirm before continuing:\n"
			"  • The Adafruit 3910 peristaltic pump is plugged into the relay outlet.\n"
			"  • Any other pumps are unplugged or switched off at their own switch.\n"
			"  • Tubing is routed to your intended container (waste or rinse).\n"
			"\n"
			"Power on?"
		)

	def _on_pump_state_change(self, claimant, relay_on):
		"""PumpController callback: sync the status bar + per-frame buttons."""
		self.status_bar.set_pump_state(claimant, relay_on)
		self._refresh_pump_buttons()

	def _refresh_pump_buttons(self):
		"""Push the controller's current state to every frame's pump buttons."""
		pc = self.pump_controller
		in_run = self.state.state != "idle"
		for frame in self._frames.values():
			if hasattr(frame, "refresh_pump_buttons"):
				frame.refresh_pump_buttons(pc.claimant, pc.relay_on, in_run)

	def toggle_pause(self):
		"""Pause/unpause fractionation, cancelling any in-flight after() task."""
		# Auto-pause-at-total-reached: the button is supposed to be disabled
		# but if a callback somehow fires anyway, just refresh the UI.
		if self.state.state == "total_reached":
			self.state.is_paused = True
			self.set_status(
				"Total reached. Click End Run to finalize, or Terminate to halt."
			)
			self._update_run_control_buttons()
			return

		self.state.is_paused = not self.state.is_paused

		if self.state.is_paused:
			# Cancel the pending pump/move callback and force the relay off.
			# Claim stays held by "fractionate" so the run can resume from
			# the same point on unpause without re-confirming.
			if self.state.taskId is not None:
				self.after_cancel(self.state.taskId)
				self.pump_controller.set_relay(False)
			self.set_status("Fractionation paused...")
		else:
			self.set_status("Fractionation in progress...")

			# Resume breadcrumb: drop a status="resume" row to the CSV
			# pinned to the well that's about to be dispensed (the well
			# after the next snake step) and the CURRENT Project/Sample ID.
			# Forensically: prior rows under the old Sample ID, one resume
			# row at the changeover, then subsequent completed rows under
			# the new Sample ID. Skip during the discard phase -- there's
			# no "next well" concept and the per-discard rows already
			# capture the same provenance.
			if self.run_logger is not None and self.state.phase == "collect":
				next_x, next_y = self._next_well_after_resume()
				self.run_logger.resume_breadcrumb(next_x, next_y)

			# Resume from the same point in the pump -> wait -> move cycle.
			if self.state.state == "pump":
				self.stop_pump()
			elif self.state.state == "wait":
				self.move()
			elif self.state.state == "move":
				self.pump_liquid()

		self._update_run_control_buttons()

	def _next_well_after_resume(self):
		"""Pure mirror of move()'s advancing logic so we can name the next
		well without firing any motors. Used only by the resume breadcrumb."""
		s = self.state
		x, y, fwd = s.x, s.y, s.carriage_forwards
		if fwd:
			y = y + 1
			if y >= s.ROWS:
				y = s.ROWS - 1
				x = x + 1
		else:
			y = y - 1
			if y < 0:
				y = 0
				x = x + 1
		# Clamp x so the resume row still names a valid well even if we
		# paused at the very last well of the run.
		if s.COLS:
			x = min(x, s.COLS - 1)
		return x, y

	# -- Automated fractionation flow ------------------------------------

	def start_run(self, rows, cols, well_size, pump_rate, volume,
			project, sample_id_at_start, plate_id_at_start,
			number_of_fractions, discard_fractions,
			waste_bin_table, waste_bin_carriage,
			table_start, carriage_start, drip_wait_time):
		"""Begin a fractionation run with already-validated, parsed inputs.

		Cross-field rules (N ≤ rows·cols, D < N, waste-bin coords required
		if D > 0, overlap warning) are enforced in AutomatedFrame.begin_clicked
		BEFORE this method is called. By the time we get here every input is
		known-good.

		Run shape: optional discard phase (D cycles into a waste bin), then
		snake-path collection of (N−D) plate wells, then auto-pause at
		"total reached" for the operator's explicit End Run.
		"""
		# Refuse to start if any pump is currently running. The state machine
		# is about to claim "fractionate" exclusively for the duration of
		# the run; an existing claim would either fail (purge held) or
		# silently inherit a manual claim, both of which are confusing.
		pc = self.pump_controller
		if pc.claimant is not None:
			messagebox.showerror(
				"Cannot start fractionation",
				f"The {pc.claimant.title()} pump is currently active. "
				"Stop it before starting a fractionation run.",
				parent=self,
			)
			return

		pump_time = volume / (pump_rate / 3600)
		# Runtime estimate: each fraction = pump_time + drip_wait_time
		# (per-well wait is now operator-controlled, not coupled to pump_time)
		# plus, for plate fractions, an estimated per-well move. One extra
		# move at start of each phase (to waste bin and to A1).
		from well_plate import ESTIMATED_MOVE_TIME_S
		plate_count = number_of_fractions - discard_fractions
		per_dispense = pump_time + drip_wait_time
		discard_seconds = discard_fractions * per_dispense + (
			ESTIMATED_MOVE_TIME_S if discard_fractions > 0 else 0
		)
		plate_seconds = plate_count * (per_dispense + ESTIMATED_MOVE_TIME_S) + (
			ESTIMATED_MOVE_TIME_S if discard_fractions > 0 else 0
		)
		estimated_total_s = discard_seconds + plate_seconds

		# Pre-build the confirmation dialog text. D == 0 omits both the
		# discard line and the waste-container reminder.
		discard_lines = ""
		waste_reminder = ""
		if discard_fractions > 0:
			discard_lines = (
				f"    - {discard_fractions} will be discarded to waste at "
				f"({waste_bin_table:g} cm, {waste_bin_carriage:g} cm)\n"
			)
			waste_reminder = (
				f"  - Verify a waste container is positioned at "
				f"({waste_bin_table:g} cm, {waste_bin_carriage:g} cm).\n"
			)
		plate_line = (
			f"    - {plate_count} will be collected to the plate, starting at A1\n"
		)
		runtime_breakdown = (
			f"  (discard phase: {_fmt_hms(discard_seconds)}; "
			f"plate phase: {_fmt_hms(plate_seconds)})"
		)
		summary = (
			f"Begin fractionation:\n"
			f"  • Project: {project}\n"
			f"  • Sample ID: {sample_id_at_start}\n"
			f"  • Plate ID: {plate_id_at_start}\n"
			f"  • Total fractions: {number_of_fractions}\n"
			f"{discard_lines}"
			f"{plate_line}"
			f"  • Volume per fraction: {volume:g} cc\n"
			f"  • Pump rate: {pump_rate:g} cc/hr\n"
			f"  • Drip wait: {drip_wait_time:g} s\n"
			f"  • Estimated total runtime: {_fmt_hms(estimated_total_s)}\n"
			f"{runtime_breakdown}\n"
			"\n"
			"Before continuing:\n"
			f"{waste_reminder}"
			f"  - Verify the plate is positioned with A1 at "
			f"({table_start:g} cm, {carriage_start:g} cm).\n"
			"Continue?"
		)
		if not messagebox.askyesno(
			"Begin fractionation", summary, parent=self,
		):
			return

		# Persist the entry values to ~/.autosip/config.json before the run
		# starts so the next launch repopulates whatever the operator just
		# used (catches the case where they hit Begin without tabbing out
		# of the last edited field).
		try:
			config_store.save_last_used(self.automated_frame.get_values())
		except OSError as exc:
			logger.warning("Failed to save last_used config at run start: %s", exc)

		# Claim the relay for the whole run.
		pc.claim_for("fractionate")

		s = self.state
		s.ROWS = rows
		s.COLS = cols
		s.well_size = well_size
		s.pump_time = pump_time
		s.drip_wait_time = drip_wait_time
		s.volume_per_well = volume
		s.project = project
		s.current_sample_id = sample_id_at_start
		s.current_plate_id = plate_id_at_start
		s.plates_used = [plate_id_at_start]
		s.plate_swaps_done = 0
		s.wells_on_current_plate = 0
		s.plate_full_with_sample_complete = False
		s.number_of_fractions = number_of_fractions
		s.discards_planned = discard_fractions
		s.discards_done = 0
		s.wells_collected = 0
		s.waste_bin_table = waste_bin_table
		s.waste_bin_carriage = waste_bin_carriage
		s.table_start_cm = table_start
		s.carriage_start_cm = carriage_start

		# Start the on-disk per-run logger.
		self._start_run_logger(rows, cols, well_size, pump_rate, volume,
			pump_time, project, sample_id_at_start, plate_id_at_start,
			number_of_fractions, discard_fractions,
			waste_bin_table, waste_bin_carriage,
			table_start, carriage_start, estimated_total_s,
			drip_wait_time)

		# Remember the Sample ID at this series's start so Continue-to-Next-
		# Sample can prompt if the operator forgot to update it.
		self._series_start_sample_id = sample_id_at_start

		self.movement()
		# Run-control buttons reflect the new "running" state.
		self._update_run_control_buttons()

	def _start_run_logger(self, rows, cols, well_size, pump_rate, volume,
			pump_time, project, sample_id_at_start, plate_id_at_start,
			number_of_fractions, discard_fractions,
			waste_bin_table, waste_bin_carriage,
			table_start, carriage_start, estimated_total_s,
			drip_wait_time):
		"""Build the run metadata and create a RunLogger directory."""
		af = self.automated_frame
		metadata = {
			# ms precision; matches run_logger._now_iso() so derived ranges
			# (e.g. actual_total_time_s in end.json) line up cleanly.
			"timestamp_start": datetime.now().isoformat(timespec="milliseconds"),
			"software_version": __version__,
			"project": project,
			"sample_id_at_start": sample_id_at_start,
			"labware_file": af._loaded_labware_path,
			"labware_definition": af._loaded_labware_data,
			"parameters": {
				"rows": rows,
				"cols": cols,
				"well_size_cm": well_size,
				"pump_rate": pump_rate,
				"pump_rate_units": "cc/hr",
				"drip_wait_time_s": drip_wait_time,
				"volume_per_well_cc": volume,
				"table_start_cm": table_start,
				"carriage_start_cm": carriage_start,
				"number_of_fractions": number_of_fractions,
				"discard_fractions": discard_fractions,
				"waste_bin_table_cm": waste_bin_table,
				"waste_bin_carriage_cm": waste_bin_carriage,
				"plate_id_at_start": plate_id_at_start,
			},
			"estimated_total_time_s": estimated_total_s,
		}
		# The logger reads project + sample_id + plate_id via this callback
		# each time it writes a row, so mid-run edits flow into subsequent
		# CSV rows (Sample ID on tube swap, Plate ID on plate swap).
		def _current_run_id():
			return {
				"project": self.state.project,
				"sample_id": self.state.current_sample_id,
				"plate_id": self.state.current_plate_id,
			}
		self.run_logger = RunLogger(get_current_run_id=_current_run_id)
		try:
			run_dir = self.run_logger.start(metadata)
			self._last_run_path = run_dir
			logger.info("Run logging to %s", run_dir)
		except OSError as exc:
			logger.warning("Could not create run log directory: %s", exc)
			self.run_logger = None

	def movement(self):
		"""Dispatch to discard phase (Phase 1) or directly to collection (Phase 2)."""
		s = self.state
		# Reset progress view and seed the plate dims.
		self.automated_frame.begin_run(s.COLS, s.ROWS, s.volume_per_well, s.pump_time)
		self.automated_frame.progress.set_plate_label(s.current_plate_id)
		self.update()
		s.carriage_forwards = True
		s.x = 0
		s.y = 0
		s.discards_done = 0
		s.wells_collected = 0
		# First series of this run.
		s.series_index = 1
		s.current_series_sequence = 0
		s.well_records = []
		# Snapshot D for THIS series. Subsequent series re-read from the
		# entry box on continue_to_next_sample so a mid-run edit to the
		# Discard fractions field affects only the next series.
		s.discards_at_series_start = s.discards_planned

		if s.discards_at_series_start > 0:
			s.phase = "discard"
			self.set_status(
				f"Discard phase: moving to waste bin "
				f"({s.waste_bin_table:g} cm, {s.waste_bin_carriage:g} cm)..."
			)
			self.move_to_positions(
				table_dist=s.waste_bin_table,
				carriage_dist=s.waste_bin_carriage,
			)
			self.automated_frame.progress.set_discard_status(0, s.discards_at_series_start)
			self.pump_liquid()
		else:
			s.phase = "collect"
			self.set_status("Moving to plate A1...")
			self.move_to_positions(
				table_dist=s.table_start_cm,
				carriage_dist=s.carriage_start_cm,
			)
			self.set_status("Fractionation in progress...")
			self.pump_liquid()

	def pump_liquid(self):
		"""Pump-on phase. Behavior depends on s.phase (discard vs collect)."""
		s = self.state
		s.state = "pump"
		self.pump_controller.set_relay(True)
		if s.phase == "discard":
			idx = s.discards_done + 1
			if self.run_logger is not None:
				self.run_logger.discard_dispense_start(
					s.series_index, idx,
					s.waste_bin_table, s.waste_bin_carriage)
			self.set_status(
				f"Discard {idx} of {s.discards_at_series_start}: pumping to waste..."
			)
			self.automated_frame.progress.set_discard_status(idx, s.discards_at_series_start)
		else:
			self.automated_frame.well_dispensing(s.x, s.y)
			if self.run_logger is not None:
				self.run_logger.dispense_start(s.x, s.y)
			self.set_status(f"Pumping well (col {s.x + 1}, row {s.y + 1})...")
		s.taskId = self.after(round(s.pump_time * 1000), self.stop_pump)

	def stop_pump(self):
		"""End the pump-on phase; schedule the drip-wait then move()."""
		s = self.state
		s.state = "wait"
		self.pump_controller.set_relay(False)
		if s.phase == "discard":
			idx = s.discards_done + 1
			if self.run_logger is not None:
				self.run_logger.discard_dispense_end(s.series_index, idx)
			self.set_status(
				f"Discard {idx} of {s.discards_at_series_start}: drip wait..."
			)
		else:
			self.automated_frame.well_waiting(s.x, s.y)
			if self.run_logger is not None:
				self.run_logger.dispense_end(s.x, s.y)
			self.set_status(
				f"Drip wait at well (col {s.x + 1}, row {s.y + 1}) before next move..."
			)
		# Post-pump drip wait is operator-controlled, not coupled to pump_time.
		s.taskId = self.after(round(s.drip_wait_time * 1000), self.move)

	def move(self):
		"""Advance to the next cycle. In discard phase: increment the counter
		(no motion -- needle is already at the waste bin) and either run
		another discard cycle or transition to the collection phase. In
		collection phase: snake-step the carriage/table and pump the next
		well. Auto-pause when total fractions reached."""
		s = self.state

		if s.phase == "discard":
			idx = s.discards_done + 1
			if self.run_logger is not None:
				self.run_logger.discard_committed(s.series_index, idx)
			s.discards_done += 1
			s.state = "move"
			if s.is_paused:
				return
			if s.discards_done >= s.discards_at_series_start:
				s.phase = "collect"
				if s.series_index == 1:
					# First series: move to plate A1 (absolute).
					self.set_status("Moving to plate A1...")
					self.move_to_positions(
						table_dist=s.table_start_cm,
						carriage_dist=s.carriage_start_cm,
					)
				else:
					# Subsequent series: snake-step from the last collected
					# well to the next available one.
					if not self._snake_step():
						self._auto_pause_total_reached()
						return
				self.set_status("Fractionation in progress...")
				self.pump_liquid()
			else:
				# Another discard cycle at the same waste-bin position.
				self.pump_liquid()
			return

		# --- Collection phase ---
		# Mark just-finished well as completed BEFORE advancing x/y. Record
		# the per-sample color + within-series sequence so the well shows the
		# sample's number and the right palette color.
		from well_plate import color_for_series
		s.current_series_sequence += 1
		color_hex, color_name = color_for_series(s.series_index)
		well_id = f"{chr(ord('A') + s.y)}{s.x + 1}"
		# Fraction index counts ALL fractions from this sample (discards
		# included), so the first collected well is labeled D+1 even when
		# D > 0. Captured here, never re-read from the entry box, so a
		# mid-run edit to Discard fractions never rewrites past labels.
		fraction_index = s.discards_at_series_start + s.current_series_sequence
		record = {
			"well_id": well_id,
			"sample_id": s.current_sample_id,
			"plate_id": s.current_plate_id,
			"series_index": s.series_index,
			"sequence_within_series": s.current_series_sequence,
			"fraction_index_in_sample": fraction_index,
			"color": color_hex,
			"color_name": color_name,
		}
		s.well_records.append(record)
		self.automated_frame.well_completed(
			s.x, s.y,
			color=color_hex,
			sequence=fraction_index,
			sample_id=s.current_sample_id,
			color_name=color_name,
		)
		if self.run_logger is not None:
			self.run_logger.well_completed(s.x, s.y)
		s.wells_collected += 1
		s.wells_on_current_plate += 1
		s.state = "move"

		# Detect "plate full" BEFORE the sample-target auto-pause so that
		# end-of-plate AND end-of-sample at the same well surfaces the
		# plate-swap action as the primary user choice. The sample-complete
		# flag rides along so the button matrix can enable both Continue
		# buttons.
		plate_capacity = s.ROWS * s.COLS
		plate_target = s.number_of_fractions - s.discards_at_series_start
		sample_complete = s.wells_collected >= plate_target
		if s.wells_on_current_plate >= plate_capacity:
			self._auto_pause_plate_full(sample_complete)
			return
		if sample_complete:
			self._auto_pause_total_reached()
			return

		# Snake-step to next well.
		if s.carriage_forwards:
			s.y = s.y + 1
			if s.y < s.ROWS:
				self.carriage_motor.move_dist_relative(s.well_size)
			else:
				s.y = s.ROWS - 1
				self.table_motor.move_dist_relative(-s.well_size)
				s.x = s.x + 1
				s.carriage_forwards = not s.carriage_forwards
		else:
			s.y = s.y - 1
			if s.y >= 0:
				self.carriage_motor.move_dist_relative(-s.well_size)
			else:
				s.y = 0
				self.table_motor.move_dist_relative(-s.well_size)
				s.x = s.x + 1
				s.carriage_forwards = not s.carriage_forwards

		if s.is_paused:
			return

		if s.x == s.COLS:
			# Plate fully traversed -- should be unreachable since the
			# auto-pause above fires first when wells_collected reaches its
			# target (and validation ensures N <= rows*cols). Defensive:
			# fall through to auto-pause-total-reached so the run still
			# finalizes cleanly.
			self._auto_pause_total_reached()
		else:
			self.pump_liquid()

	def _auto_pause_total_reached(self):
		"""Hold the run at the last collected position; await End Run."""
		s = self.state
		# Cancel any pending after (defensive -- shouldn't be one here).
		if s.taskId is not None:
			self.after_cancel(s.taskId)
			s.taskId = None
		self.pump_controller.set_relay(False)
		s.state = "total_reached"
		s.is_paused = True
		self.automated_frame.progress.set_total_reached(s.number_of_fractions)
		self.set_status(
			f"Total of {s.number_of_fractions} fractions reached. "
			"Click End Run or Continue to Next Sample."
		)
		# Button row picks up the paused_total layout (Pause disabled →
		# "Paused"; Continue + End Run enabled).
		self._update_run_control_buttons()

	def _auto_pause_plate_full(self, sample_complete):
		"""Hold the run at the last well of a now-full plate; await
		Continue to Next Plate (and possibly Continue to Next Sample if the
		sample also wrapped up on this well)."""
		s = self.state
		if s.taskId is not None:
			self.after_cancel(s.taskId)
			s.taskId = None
		self.pump_controller.set_relay(False)
		s.state = "plate_full"
		s.is_paused = True
		s.plate_full_with_sample_complete = bool(sample_complete)
		self.set_status(
			f"Plate {s.current_plate_id} is full. Click Continue to Next "
			"Plate to swap plates and continue."
		)
		# Button row picks up the paused_plate_full layout.
		self._update_run_control_buttons()

	def end_run(self):
		"""Handle the End Run button click.

		Asks the operator to choose Save (finalize with timestamped files)
		or Discard (skip finalization; leave metadata.json + log.csv on disk
		untouched). Either way the run transitions to idle: motors released,
		pump claim cleared, visuals reset, FractionatorState run counters
		zeroed so a fresh Begin Fractionation starts from a clean slate.

		There is no Cancel path -- this is a one-way exit. Operators who
		clicked End Run by mistake can re-enter inputs and click Begin
		Fractionation again.
		"""
		s = self.state
		# Nothing to end if no run is active.
		if self.run_logger is None and s.state == "idle":
			return

		project_at_click = s.project or "(unset)"
		sample_at_click = s.current_sample_id or "(unset)"
		save = messagebox.askyesno(
			"End Run",
			f"Save the run logs for project '{project_at_click}' / "
			f"sample '{sample_at_click}'?\n\n"
			"Yes: finalize and write end_*.json + summary*.md with a "
			"timestamp suffix.\n"
			"No: discard finalization (metadata.json + log.csv remain "
			"on disk; delete manually if not needed).",
			parent=self,
		)

		# Cancel any pending after()
		if s.taskId is not None:
			self.after_cancel(s.taskId)
			s.taskId = None

		# Determine final status: "completed" iff we hit total_reached
		# before End Run; "manual_abort" otherwise.
		final_status = "completed" if s.state == "total_reached" else "manual_abort"

		# Reset state BEFORE pump release (button-refresh sees idle).
		pre_state = s.state
		pre_x, pre_y = s.x, s.y
		s.state = "idle"
		s.is_paused = False
		s.phase = "idle"

		# Pump off + claim cleared, motors released.
		self.pump_controller.release()
		self.table_motor.release()
		self.carriage_motor.release()

		# Close out the run logger. On Save: finalize with a timestamped
		# suffix so multiple End Runs in one session don't overwrite each
		# other. On Discard: drop the logger reference without writing
		# end/summary; the run dir keeps metadata.json + log.csv as-is.
		discarded_run_dir = None
		if self.run_logger is not None:
			# If we ended mid-dispense/wait of a plate well or discard, commit
			# that in-flight entry as emergency_stopped so log.csv has a row.
			if pre_state in ("pump", "wait") and final_status == "manual_abort":
				if s.phase == "discard":
					self.run_logger.discard_emergency_stopped(
						s.series_index, s.discards_done + 1)
				else:
					self.run_logger.well_emergency_stopped(pre_x, pre_y)
			if save:
				# Filesystem-safe ISO timestamp (colons -> hyphens) so the
				# suffix is portable across Windows/macOS/Linux.
				end_ts = datetime.now().isoformat(timespec="seconds").replace(":", "-")
				snap = self.automated_frame.progress.snapshot()
				self.run_logger.end(final_status, snapshot=snap,
					plates_used=self.state.plates_used,
					well_records=self.state.well_records,
					file_suffix=end_ts)
			else:
				discarded_run_dir = self._last_run_path
				# Close the CSV cleanly so the partial log is well-formed
				# even though we're not writing the summary.
				self.run_logger.close_without_summary()
			self.run_logger = None

		# Reset run-flow counters so a fresh Begin Fractionation starts
		# clean. Persistent fields (project/sample/plate IDs, plate geometry,
		# waste-bin, drip wait, etc.) are kept -- they'll be reused or
		# updated from the entry boxes on the next Begin.
		s.x = 0
		s.y = 0
		s.carriage_forwards = True
		s.number_of_fractions = 0
		s.discards_planned = 0
		s.discards_at_series_start = 0
		s.discards_done = 0
		s.wells_collected = 0
		s.series_index = 0
		s.current_series_sequence = 0
		s.well_records = []
		s.wells_on_current_plate = 0
		s.plate_full_with_sample_complete = False
		s.plates_used = []
		s.plate_swaps_done = 0

		# Reset visuals: plate canvas to UNVISITED, plate label to the
		# current Plate ID input, header text to the ready message.
		af = self.automated_frame
		plate_id = af.plate_id_te.get().strip() or "Plate-1"
		af.progress.reset()
		af.progress.set_plate_label(plate_id)
		af.progress.current_lbl["text"] = "Ready. Click Begin Fractionation to start."
		self._update_run_control_buttons()
		if save:
			self.set_status(f"Run ended ({final_status}). Logs saved.")
		else:
			self.set_status(
				f"Run discarded. Partial log files at {discarded_run_dir} "
				"may be deleted manually."
			)

	def continue_to_next_sample(self):
		"""Start a new series within the current run. Used after auto-pause-
		at-total-reached to begin collection for the next ultracentrifuge tube.

		Pre-flight 1: confirm the operator changed Sample ID (or accepted the
		warning). Pre-flight 2: hard-block if the new series wouldn't fit in
		the remaining plate wells. Otherwise: reset per-series counters, emit
		a resume breadcrumb, optionally run a discard phase, and snake-step
		to the first new plate well.
		"""
		s = self.state
		# Only meaningful from the auto-pause-at-total-reached state.
		if s.state != "total_reached":
			return

		# Pre-flight 1: Sample ID unchanged warning
		current_sample = self.automated_frame.sample_id_te.get().strip()
		prior_sample = getattr(self, "_series_start_sample_id", s.current_sample_id)
		if current_sample == prior_sample:
			if not messagebox.askyesno(
				"Sample ID unchanged",
				f"Sample ID is still '{current_sample}'. Did you mean to update "
				"it for the new sample? Continue anyway?",
				parent=self,
			):
				return

		# Re-read D from the entry box so a mid-run edit takes effect on the
		# NEW series (per spec: "Only NEW wells filled after the edit ...
		# would use the new D value").
		new_d_ok, new_d_val = validation.discard_fractions(
			self.automated_frame.discard_te.get())
		if not new_d_ok:
			messagebox.showerror(
				"Invalid discard fractions",
				str(new_d_val) + "\n\n"
				"Update the field or end the run.",
				parent=self,
			)
			return
		if new_d_val >= s.number_of_fractions:
			messagebox.showerror(
				"Invalid discard count",
				f"Discard count ({new_d_val}) must be less than total fractions "
				f"({s.number_of_fractions}). Update the field or end the run.",
				parent=self,
			)
			return

		# Pre-flight 2: per-current-plate capacity check. With multi-plate
		# support, a sample is ALLOWED to span plates -- so this is now an
		# informational notice rather than a hard block. autoSIP will
		# auto-pause for a plate swap when the current plate fills.
		capacity = s.ROWS * s.COLS
		remaining_on_plate = capacity - s.wells_on_current_plate
		required = s.number_of_fractions - new_d_val
		if required > remaining_on_plate:
			messagebox.showinfo(
				"Sample will span plates",
				f"This sample requires {required} wells. Only {remaining_on_plate} "
				f"remain on plate {s.current_plate_id}. autoSIP will prompt for "
				"a plate swap when the current plate fills.",
				parent=self,
			)

		# Start the new series. Snapshot the per-series D so labels and
		# the discard-cycle count come from the value that was set when
		# the operator clicked Continue, not whatever the entry holds
		# later (e.g. if they edit it again mid-series).
		s.series_index += 1
		s.discards_done = 0
		s.wells_collected = 0
		s.current_series_sequence = 0
		s.discards_at_series_start = new_d_val
		s.is_paused = False
		self._series_start_sample_id = current_sample
		logger.info("Starting series %d: sample %s (D=%d)",
			s.series_index, current_sample, new_d_val)

		# Resume breadcrumb -- documents the sample handoff in log.csv.
		if self.run_logger is not None:
			next_x, next_y = self._next_well_after_resume()
			self.run_logger.resume_breadcrumb(next_x, next_y)

		self.set_status(f"Starting series {s.series_index}: sample {current_sample}")

		if s.discards_at_series_start > 0:
			s.phase = "discard"
			self.move_to_positions(
				table_dist=s.waste_bin_table,
				carriage_dist=s.waste_bin_carriage,
			)
			self.automated_frame.progress.set_discard_status(0, s.discards_at_series_start)
			self.pump_liquid()
		else:
			# No discards this series: snake-step to next well and pump.
			s.phase = "collect"
			if not self._snake_step():
				# Off the plate -- shouldn't happen post-capacity-check, but
				# fall back to auto-pause if it does.
				self._auto_pause_total_reached()
				return
			self.pump_liquid()

		self._update_run_control_buttons()

	def continue_to_next_plate(self):
		"""Open the plate-swap dialog. The dialog walks the operator through
		removing the full plate, optionally homing the needle, placing a new
		plate, and entering its Plate ID. On Continue, we update state, emit
		a plate_swap breadcrumb, safety-home if needed, move to A1 of the
		new plate, and resume whichever phase was active when the plate
		filled."""
		s = self.state
		if s.state != "plate_full":
			return
		suggested = validation.auto_increment_plate_id(s.current_plate_id)
		new_plate_id = self._show_plate_swap_dialog(s.current_plate_id, suggested)
		if not new_plate_id:
			# User cancelled the dialog (clicked "Cancel Run" routes through
			# end_run() inside the dialog and returns None here).
			return
		self._commit_plate_swap(new_plate_id)

	def _show_plate_swap_dialog(self, old_plate_id, suggested_new_id):
		"""Modal Toplevel for the plate-swap flow.

		Returns the validated new Plate ID on Continue, or None if the user
		cancels / aborts the run. The dialog's "Move Needle to Home" button
		actually moves the carriage; the result dict keeps track of whether
		the user took that step so the post-swap safety-home can be skipped
		when redundant.
		"""
		result = {"plate_id": None, "needle_at_home": False}

		dlg = tk.Toplevel(self)
		dlg.title(f"Plate Full — {old_plate_id}")
		dlg.transient(self)
		dlg.grab_set()
		dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # X disabled; force a choice

		body = tk.Frame(dlg, padx=12, pady=12)
		body.pack(fill=tk.BOTH, expand=True)

		tk.Label(body, anchor="w", justify="left",
			text="The current plate is full. Follow these steps in order:",
		).grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, 8))

		tk.Label(body, anchor="w", justify="left", wraplength=480,
			text=f"  1. Remove the current plate ({old_plate_id}) from the "
			"stage and store it for downstream processing.",
		).grid(row=1, column=0, columnspan=2, sticky="we", pady=2)

		tk.Label(body, anchor="w", justify="left",
			text="  2. Return the dispensing needle to home position:",
		).grid(row=2, column=0, columnspan=2, sticky="we", pady=(8, 2))

		home_btn = tk.Button(body, text="Move Needle to Home")
		def _home_click():
			self.carriage_return()
			result["needle_at_home"] = True
			home_btn["text"] = "✓ Needle at home"
			home_btn["state"] = tk.DISABLED
		home_btn["command"] = _home_click
		home_btn.grid(row=3, column=0, columnspan=2, sticky="w", padx=20, pady=2)

		tk.Label(body, anchor="w", justify="left",
			text="  3. Place a new plate on the stage.",
		).grid(row=4, column=0, columnspan=2, sticky="we", pady=(8, 2))

		tk.Label(body, anchor="w", justify="left",
			text="  4. Enter the new Plate ID:",
		).grid(row=5, column=0, columnspan=2, sticky="we", pady=(8, 2))

		plate_te = TextEntry(body, "  Plate ID:")
		plate_te.grid(row=6, column=0, columnspan=2, sticky="we", padx=20)
		plate_te.set(suggested_new_id)
		tk.Label(body, anchor="w", text=f"  (suggested: {suggested_new_id})",
			fg="#666",
		).grid(row=7, column=0, columnspan=2, sticky="we", padx=20)

		tk.Label(body, anchor="w", justify="left",
			text="  5. Click Continue to resume fractionation.",
		).grid(row=8, column=0, columnspan=2, sticky="we", pady=(8, 8))

		btn_row = tk.Frame(body)
		btn_row.grid(row=9, column=0, columnspan=2, sticky="we", pady=(8, 0))
		btn_row.grid_columnconfigure(0, weight=1)
		btn_row.grid_columnconfigure(1, weight=1)

		def _continue():
			ok, val = validation.plate_id(plate_te.get())
			if not ok:
				plate_te.show_error(val)
				return
			plate_te.clear_error()
			result["plate_id"] = val
			dlg.destroy()

		def _cancel_run():
			# Forward to End Run; it has its own confirmation dialog.
			dlg.destroy()
			self.end_run()

		tk.Button(btn_row, text="Cancel Run", command=_cancel_run).grid(
			row=0, column=0, sticky="w", padx=4)
		tk.Button(btn_row, text="Continue", command=_continue).grid(
			row=0, column=1, sticky="e", padx=4)

		# Modal -- block until destroyed.
		self.wait_window(dlg)
		# Stash the home-clicked flag on self so _commit_plate_swap can read
		# it without dragging another argument through the call chain.
		self._plate_swap_pre_homed = result["needle_at_home"]
		return result["plate_id"]

	def _commit_plate_swap(self, new_plate_id):
		"""Apply a confirmed Plate ID change: update state + plates_used,
		emit breadcrumb, safety-home if the operator skipped step 2, move
		to A1 of the new plate, and resume the appropriate phase."""
		s = self.state
		# State updates BEFORE the breadcrumb so the logger callback sees
		# the new plate_id.
		s.current_plate_id = new_plate_id
		# Mirror to the entry box so subsequent CSV rows + the visible
		# Plate ID field stay in sync.
		self.automated_frame.plate_id_te.set(new_plate_id)
		if new_plate_id not in s.plates_used:
			s.plates_used.append(new_plate_id)
		s.plate_swaps_done += 1

		# Safety-home if the operator skipped step 2.
		if not getattr(self, "_plate_swap_pre_homed", False):
			self.carriage_return()

		# Move to plate A1 (absolute) and reset snake position.
		self.move_to_positions(
			table_dist=s.table_start_cm,
			carriage_dist=s.carriage_start_cm,
		)
		s.x = 0
		s.y = 0
		s.carriage_forwards = True
		s.wells_on_current_plate = 0

		# Breadcrumb -- recorded AFTER state.current_plate_id is updated
		# so the row's plate_id column reflects the NEW plate.
		if self.run_logger is not None:
			self.run_logger.plate_swap_breadcrumb(s.plate_swaps_done)
			# A new plate means well_ids restart -- the per-well dedup
			# guard inside RunLogger must reset so e.g. a second "A1"
			# row (on the new plate) actually gets written.
			self.run_logger.reset_for_new_plate()

		# Visual reset of the well-plate canvas for the new plate.
		self.automated_frame.progress.reset_plate(new_plate_id)

		# Decide what happens next:
		# - If the sample ALSO completed on the now-full plate, transition
		#   to total_reached so Continue to Next Sample is the next action.
		# - Else continue the current sample's collection from this fresh
		#   plate's well A1.
		if s.plate_full_with_sample_complete:
			s.plate_full_with_sample_complete = False
			# Match the total_reached layout for the next user action.
			s.state = "total_reached"
			self.automated_frame.progress.set_total_reached(s.number_of_fractions)
			self.set_status(
				f"Plate swap to {new_plate_id} complete. Sample also finished — "
				"click Continue to Next Sample or End Run."
			)
			self._update_run_control_buttons()
		else:
			# Resume same-sample collection on the new plate.
			s.is_paused = False
			s.plate_full_with_sample_complete = False
			s.phase = "collect"
			self.set_status(f"Resuming on plate {new_plate_id}...")
			self.pump_liquid()
			self._update_run_control_buttons()

	def _snake_step(self):
		"""Advance s.x/s.y one snake-step AND fire the corresponding motor
		moves. Returns True if we stayed on the plate, False if we walked off
		(s.x reached COLS) -- the caller decides what to do then."""
		s = self.state
		if s.carriage_forwards:
			s.y = s.y + 1
			if s.y < s.ROWS:
				self.carriage_motor.move_dist_relative(s.well_size)
			else:
				s.y = s.ROWS - 1
				self.table_motor.move_dist_relative(-s.well_size)
				s.x = s.x + 1
				s.carriage_forwards = not s.carriage_forwards
		else:
			s.y = s.y - 1
			if s.y >= 0:
				self.carriage_motor.move_dist_relative(-s.well_size)
			else:
				s.y = 0
				self.table_motor.move_dist_relative(-s.well_size)
				s.x = s.x + 1
				s.carriage_forwards = not s.carriage_forwards
		return s.x < s.COLS

	def carriage_return(self):
		"""Return the needle to the starting position."""
		self.table_motor.move_dist_absolute(0.0)
		self.carriage_motor.move_dist_absolute(0.0)


def parse_args(argv=None):
	parser = argparse.ArgumentParser(description="autoSIP Robotic Fractionator GUI")
	parser.add_argument("--debug", action="store_true",
		help="enable DEBUG-level logging (implies --simulate unless --no-simulate is also given)")
	parser.add_argument("--simulate", action="store_true",
		help="force mock hardware backends regardless of HAT/GPIO availability")
	parser.add_argument("--no-simulate", action="store_true",
		help="force real hardware backends (overrides --debug's implied --simulate)")
	return parser.parse_args(argv)


def main(argv=None):
	args = parse_args(argv)

	level = logging.DEBUG if args.debug else logging.INFO
	logging.basicConfig(
		level=level,
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	)

	# --debug implies --simulate so the same flag is useful on a laptop
	# without HAT/GPIO. --no-simulate lets the Pi developer keep --debug
	# while still talking to real hardware.
	if args.no_simulate:
		simulate = False
	elif args.simulate or args.debug:
		simulate = True
	else:
		simulate = False

	backends = hardware.get_backends(force_simulate=simulate)
	logger.info("Starting autoSIP GUI (simulated=%s)", backends.simulated)

	app = App(backends)
	try:
		app.mainloop()
	finally:
		# Once the loop is done and the application is closed,
		# release the motors to prevent overheating.
		app.table_motor.release()
		app.carriage_motor.release()


if __name__ == "__main__":
	main()
