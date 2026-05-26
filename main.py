# Import statements
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep, strftime
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
from tkinter import filedialog, messagebox, simpledialog, ttk

import hardware
import styling
import validation
import config_store
from styling import (
	FONTS, PALETTE, apply_style,
	make_bimodal_distribution_canvas,
	make_centrifuge_tube_canvas, primary_button,
)
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


# --- Bulk Sample Submission helpers ------------------------------------
# Comment + column-header bytes written by Generate Template, and the
# recognized column names used by the importer.

_BULK_TEMPLATE_BYTES = (
	"# autoSIP Bulk Sample Submission Template\n"
	"# Required column: sample_id\n"
	"# Optional columns: plate_id, number_of_fractions, discard_fractions,\n"
	"#   volume_per_well_ml, notes\n"
	"# Blank optional values inherit the current Run Parameters values\n"
	"# from the GUI at the moment of import. Comment lines (starting with #)\n"
	"# and empty lines are skipped during import.\n"
	"# Add one row per sample, in the order they will be fractionated.\n"
	"sample_id,plate_id,number_of_fractions,discard_fractions,volume_per_well_ml,notes\n"
	"Tube-A01,Plate-1,20,2,0.22,Example: first gradient\n"
	"Tube-A02,Plate-1,20,2,0.22,\n"
	"Tube-A03,Plate-1,20,2,0.22,\n"
	"Tube-B01,Plate-2,20,2,0.22,Example: plate swap before this sample\n"
	"Tube-B02,Plate-2,20,2,0.22,\n"
)

_BULK_KNOWN_COLUMNS = {
	"sample_id", "plate_id", "number_of_fractions",
	"discard_fractions", "volume_per_well_ml", "notes",
}


def write_bulk_template(path):
	"""Write the Bulk Sample Submission template to ``path``. Overwrites
	an existing file. Raises OSError on filesystem failures."""
	with open(path, "w", newline="") as f:
		f.write(_BULK_TEMPLATE_BYTES)


def load_bulk_submission(path, *, gui_defaults):
	"""Parse a Bulk Sample Submission CSV from ``path``.

	Skips ``#``-prefixed and blank lines. Validates header + each row
	using the same constants the GUI's Run Parameters fields use.
	``gui_defaults`` is a dict carrying the operator's current GUI
	values for fields that are optional in the CSV (used to fill
	blanks at import time, so the spreadsheet-omitted defaults
	track what the operator set up before clicking Import).

	Returns ``(samples, errors)``: a list of dicts (one per valid
	row) and a list of ``(csv_row_number, message)`` tuples. If
	``errors`` is non-empty the caller should NOT activate bulk mode.
	"""
	import csv as _csv
	samples = []
	errors = []

	with open(path, newline="") as f:
		# Pre-strip comment + blank lines so DictReader sees a clean
		# header-then-data stream. Track original CSV line numbers so
		# error messages can quote the user's file accurately.
		raw_lines = []
		raw_indices = []
		for raw_idx, raw_line in enumerate(f, start=1):
			stripped = raw_line.strip()
			if not stripped or stripped.startswith("#"):
				continue
			raw_lines.append(raw_line)
			raw_indices.append(raw_idx)

	if not raw_lines:
		errors.append((0, "File contains no data rows."))
		return [], errors

	# DictReader on the filtered stream.
	from io import StringIO
	reader = _csv.DictReader(StringIO("".join(raw_lines)))
	if reader.fieldnames is None or "sample_id" not in (reader.fieldnames or []):
		errors.append((raw_indices[0] if raw_indices else 0,
			"Header row must contain a 'sample_id' column."))
		return [], errors

	for fname in reader.fieldnames:
		if fname and fname not in _BULK_KNOWN_COLUMNS:
			logger.warning(
				"Bulk submission: unrecognized column %r will be ignored.",
				fname,
			)

	for csv_row_idx, row in enumerate(reader):
		# raw_indices[0] is the header row; data rows start at index 1.
		file_line = raw_indices[csv_row_idx + 1] if csv_row_idx + 1 < len(raw_indices) else "?"
		ctx = f"Row {file_line}"

		def _add_err(msg):
			errors.append((file_line, f"{ctx}: {msg}"))

		sid_raw = (row.get("sample_id") or "").strip()
		ok, sid_val = validation.sample_id(sid_raw)
		if not ok:
			_add_err(str(sid_val))
			continue
		entry = {
			"sample_id": sid_val,
			"spreadsheet_sample_id": sid_val,
			"plate_id": "",
			"number_of_fractions": None,
			"discard_fractions": None,
			"volume_per_well_ml": None,
			"notes": "",
			"edited": False,
		}

		# Optional plate_id
		pid_raw = (row.get("plate_id") or "").strip()
		if pid_raw:
			ok, pid_val = validation.plate_id(pid_raw)
			if not ok:
				_add_err(str(pid_val))
				continue
			entry["plate_id"] = pid_val

		# Optional number_of_fractions
		nf_raw = (row.get("number_of_fractions") or "").strip()
		if nf_raw:
			ok, nf_val = validation.number_of_fractions(nf_raw)
			if not ok:
				_add_err(str(nf_val))
				continue
			entry["number_of_fractions"] = nf_val
		# Optional discard_fractions -- cross-checked against
		# row's N (or GUI's N if N omitted) for the upper bound.
		df_raw = (row.get("discard_fractions") or "").strip()
		if df_raw:
			ok, df_val = validation.discard_fractions(df_raw)
			if not ok:
				_add_err(str(df_val))
				continue
			n_for_check = entry["number_of_fractions"]
			if n_for_check is None:
				try:
					n_for_check = int(gui_defaults.get("number_of_fractions") or 0)
				except (TypeError, ValueError):
					n_for_check = 0
			if n_for_check and df_val >= n_for_check:
				_add_err(
					f"discard_fractions ({df_val}) must be less than "
					f"number_of_fractions ({n_for_check})."
				)
				continue
			entry["discard_fractions"] = df_val

		# Optional volume_per_well_ml
		v_raw = (row.get("volume_per_well_ml") or "").strip()
		if v_raw:
			ok, v_val = validation.volume(v_raw)
			if not ok:
				_add_err(str(v_val))
				continue
			entry["volume_per_well_ml"] = v_val

		entry["notes"] = (row.get("notes") or "").strip()
		samples.append(entry)

	if not samples and not errors:
		errors.append((0, "File contains a header but no sample rows."))
	return samples, errors


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
		self.entry = ttk.Entry(self, textvariable=self.var)
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


def _update_pump_button(btn, name, claimant, relay_on, in_run):
	"""Sync a Fractionate/Purge button's label, role-style, and enabled
	state.

	``in_run`` disables BOTH pump buttons across the UI: the state machine
	owns the relay during an automated run, so direct user clicks would
	interfere. Outside a run, the standard interlock applies: only the
	currently-claiming button is clickable; the opposite-name button is
	greyed out until the claim is released.

	The button is a ``ttk.Button``; color changes happen by swapping its
	``style`` (PumpOff/PumpOn/PumpLocked.TButton) rather than mutating
	``btn["bg"]`` directly, which ttk doesn't support.
	"""
	display = name.title()
	if in_run:
		# State machine owns the pump for the entire run.
		if claimant == name and relay_on:
			btn["text"] = f"{display}: ON (run)"
			btn.configure(style="PumpOn.TButton")
		else:
			btn["text"] = f"{display}: OFF"
			btn.configure(style="PumpOff.TButton")
		btn["state"] = tk.DISABLED
		return

	if claimant is None:
		btn["text"] = f"{display}: OFF"
		btn.configure(style="PumpOff.TButton")
		btn["state"] = tk.NORMAL
	elif claimant == name:
		if relay_on:
			btn["text"] = f"{display}: ON"
			btn.configure(style="PumpOn.TButton")
		else:
			# Claim held by us with relay off -- only reachable through the
			# state machine (paused-mid-run). User clicks always pair on/off
			# with claim/release.
			btn["text"] = f"{display}: OFF (claim held)"
			btn.configure(style="PumpOff.TButton")
		btn["state"] = tk.NORMAL
	else:
		# Interlock: opposite pump has the claim, this button is locked out.
		btn["text"] = f"{display}: OFF"
		btn.configure(style="PumpLocked.TButton")
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
		# Use the application body-font family so the Terminate Run text
		# reads in the same typeface as every ttk.Button in the GUI; only
		# the size shrinks to fit inside the octagon and the weight stays
		# bold (allowed for the Danger role per the visual spec).
		body_family = FONTS.get("family", "DejaVu Sans")
		body_size = FONTS.get("size", 11)
		if font_size is None:
			font_size = max(8, int(size / 5))
		# Cap at body size so the octagon never types LARGER than ordinary
		# UI buttons; floor at 9 so multi-line labels stay legible.
		font_size = min(body_size, max(9, font_size))
		self._text_id = self.create_text(
			cx, cy, text=text, fill="white", justify="center",
			font=(body_family, font_size, "bold"),
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
	# Inter-sample purge: each of two pump phases (wash, then air-clear)
	# runs for this many seconds between samples. Bypassed when
	# skip_intersample_purge is True.
	purge_time: float = 30.0
	skip_intersample_purge: bool = False
	# Peristaltic-pump flow rate (mL/min) for purge-claim pumping (Manual
	# Purge, Cleaning Purge, Purge Time Calibration, inter-sample purges).
	# Frozen at run start so mid-run edits don't retroactively recompute
	# waste deltas for in-flight pumps.
	peristaltic_rate_ml_per_min: float = 100.0
	# Max waste-bin capacity in mL. Auto-shutoff fires when the estimated
	# waste volume reaches this number.
	max_waste_volume_ml: float = 250.0
	# Volume per well in mL -- displayed by the WellPlateProgress header
	# ("Dispensing into B4 — 0.22 mL"). Stored explicitly because pump_time
	# alone can't recover it without also knowing pump_rate. 1 mL = 1 cm^3
	# = 1 cc; the unit label was updated for clarity but every numerical
	# value is unchanged.
	volume_per_well: float = 0.0

	# Current needle position in the well grid
	x: int = 0
	y: int = 0
	carriage_forwards: bool = True

	# Automated-flow state machine: "idle" | "pump" | "wait" | "move"
	state: str = "idle"
	taskId: object = None
	is_paused: bool = False

	# Mid-pause recalibration support. ``origin_returned_during_pause``
	# flips True the first time the operator clicks Return to Origin
	# during the current pause; on the matching Resume, the run drives
	# the needle back to (paused_table_cm, paused_carriage_cm) and pops
	# a Confirm Calibration dialog. Cleared on Resume-confirm, End Run,
	# Continue to Next Sample, and Continue to Next Plate.
	origin_returned_during_pause: bool = False
	paused_table_cm: float = 0.0
	paused_carriage_cm: float = 0.0

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

	def __init__(self, master, app):
		super().__init__(master)
		self.app = app
		self.configure(bg=PALETTE["bg_window"])

		self._tab_buttons = {}
		for col, name in enumerate(MODE_ORDER):
			# Bind ``name`` at lambda-creation time so each button captures
			# its own label rather than the loop's final value. Styles
			# ModeActive.TButton / ModeInactive.TButton are configured in
			# styling.apply_style and swapped per click.
			btn = ttk.Button(
				self, text=name, style="ModeInactive.TButton",
				command=lambda n=name: app.request_mode_change(n),
				cursor="hand2",
			)
			btn.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 0), pady=2)
			self.grid_columnconfigure(col, weight=1, uniform="modetabs")
			self._tab_buttons[name] = btn

	def set_mode_label(self, mode_name):
		"""Highlight the active tab and subdue the others."""
		for name, btn in self._tab_buttons.items():
			btn.configure(style="ModeActive.TButton" if name == mode_name
				else "ModeInactive.TButton")


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

		# (Terminate Run button removed -- End Run + Pause cover halt
		# needs; the underlying terminate_run code path is kept for
		# emergency call sites like the waste-bin auto-shutoff but has
		# no UI entry point.)

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

		# Waste-bin fill indicator + Reset button. Packed AFTER terminate
		# with side=RIGHT so they appear to its LEFT in the final layout
		# (pack-RIGHT items stack right-to-left in order of packing).
		# Reset is rightmost-of-the-trio, then readout, then flask canvas.
		self.waste_reset_btn = ttk.Button(self, text="Reset",
			command=app.reset_waste_counter)
		self.waste_reset_btn.pack(side=tk.RIGHT, padx=(4, 6), pady=2)
		Tooltip(self.waste_reset_btn,
			"Reset waste counter (click after physically emptying the "
			"waste container).")
		self.waste_readout_lbl = tk.Label(self, text="0 / 250 mL (0%)",
			anchor="e", fg="#4CAF50")
		self.waste_readout_lbl.pack(side=tk.RIGHT, padx=(2, 4))
		self.waste_canvas = tk.Canvas(self, width=28, height=42,
			highlightthickness=0, bd=0)
		self.waste_canvas.pack(side=tk.RIGHT, padx=(4, 2), pady=1)
		# "Waste:" label sits to the LEFT of the flask icon so the icon
		# isn't ambiguous on first glance. Packed AFTER the canvas with
		# side=RIGHT (stack-from-right) so it appears just to its left.
		self.waste_label = tk.Label(self, text="Waste:", anchor="e")
		self.waste_label.pack(side=tk.RIGHT, padx=(8, 0))
		# Build the flask geometry once; _draw_flask updates fill height
		# and color on every set_waste_state call without recreating items.
		self._flask_fill_item = None
		self._flask_outline_item = None
		self._flask_pulse_thick = False
		self._flask_pulse_after = None
		self._flask_outline_pts = self._compute_flask_outline()
		self._build_flask()
		# Initial paint (0% green).
		self.set_waste_state(0.0, 250.0)

		# Per-phase status fills the middle of the bar. The previous
		# diagnostic-log mirror (status_var fed by StringVarLogHandler) has
		# been removed -- the `autosip` logger continues writing to stdout
		# and per-run log files; only the in-window line is gone, which
		# also frees vertical space for the well-plate canvas above.
		self.status_lbl = tk.Label(self, text="System idle.", anchor="w")
		self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

	def set_text(self, text):
		"""Set the per-phase status message (middle of the bar)."""
		self.status_lbl["text"] = text

	def set_mode(self, mode_name):
		self.mode_lbl["text"] = f"Mode: {mode_name}"

	def set_terminate_visible(self, visible):
		"""No-op. The Terminate Run button was removed; this method is
		kept as a compatibility shim for the mode-switch handler."""
		pass

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

	# -- Waste-bin indicator -----------------------------------------

	_FLASK_W = 28
	_FLASK_H = 42
	# Coordinates are tuned for the 28x42 canvas. The flask is a closed
	# polygon: lip (top), neck (narrow), shoulders (slanted), body (wide),
	# base (bottom).
	_FLASK_NECK_HALF = 4    # half-width of the neck
	_FLASK_BODY_HALF = 11   # half-width of the body
	_FLASK_LIP_Y = 4        # top of the neck
	_FLASK_NECK_BOT_Y = 12  # bottom of the neck / top of the shoulder
	_FLASK_SHOULDER_BOT_Y = 22  # bottom of the slanted shoulder / top of body
	_FLASK_BODY_BOT_Y = 38  # bottom of the body
	# Fill threshold colors (Material Design swatches).
	_FILL_GREEN = "#4CAF50"
	_FILL_AMBER = "#FFC107"
	_FILL_ORANGE = "#FF9800"
	_FILL_RED = "#F44336"

	def _compute_flask_outline(self):
		"""Return the closed flask-outline polygon points."""
		cx = self._FLASK_W / 2
		return [
			(cx - self._FLASK_NECK_HALF, self._FLASK_LIP_Y),
			(cx + self._FLASK_NECK_HALF, self._FLASK_LIP_Y),
			(cx + self._FLASK_NECK_HALF, self._FLASK_NECK_BOT_Y),
			(cx + self._FLASK_BODY_HALF, self._FLASK_SHOULDER_BOT_Y),
			(cx + self._FLASK_BODY_HALF, self._FLASK_BODY_BOT_Y),
			(cx - self._FLASK_BODY_HALF, self._FLASK_BODY_BOT_Y),
			(cx - self._FLASK_BODY_HALF, self._FLASK_SHOULDER_BOT_Y),
			(cx - self._FLASK_NECK_HALF, self._FLASK_NECK_BOT_Y),
		]

	def _build_flask(self):
		"""Create the flask Canvas items (outline + fill placeholder).
		``set_waste_state`` mutates them on every update."""
		flat = [v for p in self._flask_outline_pts for v in p]
		# Fill rectangle drawn BEFORE outline so the outline overlays it.
		self._flask_fill_item = self.waste_canvas.create_rectangle(
			0, 0, 0, 0, fill=self._FILL_GREEN, outline="",
		)
		self._flask_outline_item = self.waste_canvas.create_polygon(
			flat, fill="", outline="#333333", width=1.5,
		)

	def _fill_color_for(self, pct):
		"""Material-style traffic-light color by percentage (0..1+)."""
		if pct < 0.60:
			return self._FILL_GREEN
		if pct < 0.80:
			return self._FILL_AMBER
		if pct < 0.95:
			return self._FILL_ORANGE
		return self._FILL_RED

	def set_waste_state(self, volume_ml, max_ml):
		"""Repaint the flask + readout with the new volume."""
		max_ml = max_ml if max_ml > 0 else 1.0
		pct = max(0.0, min(1.0, volume_ml / max_ml))
		color = self._fill_color_for(pct)

		# Fill rectangle clipped within the flask BODY (we don't try to
		# follow the angled shoulders for the fill; just paint the wide
		# body region proportionally). Anything above SHOULDER_BOT_Y is
		# the "neck" zone and stays empty for visual cleanliness.
		body_top = self._FLASK_SHOULDER_BOT_Y
		body_bot = self._FLASK_BODY_BOT_Y
		body_h = body_bot - body_top
		fill_h = body_h * pct
		fill_top = body_bot - fill_h
		cx = self._FLASK_W / 2
		self.waste_canvas.coords(
			self._flask_fill_item,
			cx - self._FLASK_BODY_HALF + 1, fill_top,
			cx + self._FLASK_BODY_HALF - 1, body_bot - 1,
		)
		self.waste_canvas.itemconfig(self._flask_fill_item, fill=color)

		# Outline pulses thicker when >= 95% to draw the eye.
		if pct >= 0.95:
			self._start_flask_pulse()
		else:
			self._stop_flask_pulse()
			self.waste_canvas.itemconfig(self._flask_outline_item, width=1.5)

		# Numeric readout. Match text color to fill color.
		self.waste_readout_lbl["text"] = (
			f"{volume_ml:.0f} / {max_ml:.0f} mL ({pct:.0%})"
		)
		self.waste_readout_lbl["fg"] = color

	def _start_flask_pulse(self):
		if self._flask_pulse_after is not None:
			return
		self._flask_pulse_thick = False

		def _tick():
			self._flask_pulse_thick = not self._flask_pulse_thick
			self.waste_canvas.itemconfig(
				self._flask_outline_item,
				width=3 if self._flask_pulse_thick else 1.5,
			)
			self._flask_pulse_after = self.after(500, _tick)

		_tick()

	def _stop_flask_pulse(self):
		if self._flask_pulse_after is not None:
			try:
				self.after_cancel(self._flask_pulse_after)
			except Exception:
				pass
			self._flask_pulse_after = None


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

		# Two columns so Run Parameters (left) and Plate Parameters (right)
		# can sit SIDE BY SIDE in the same row -- the stacked layout used
		# ~640 px of natural vertical space, which left the progress
		# canvas with only ~200 px on a maximized 1080p screen. The
		# side-by-side layout cuts the upper section's height roughly
		# in half so the canvas absorbs the bulk of the window's
		# vertical real estate.
		for i in range(2):
			self.grid_columnconfigure(i, weight=1, uniform="auto")

		# Run-control button row: always visible from app startup, state-
		# driven enable/disable. The cluster of five buttons is the first
		# thing visible. Gridded WITHOUT sticky so the un-stretched
		# subframe centers horizontally in the row-0 cell (which spans
		# both weighted columns), keeping the buttons balanced as the
		# window resizes.
		ctrl = tk.Frame(self)
		ctrl.grid(row=0, column=0, columnspan=2, pady=(0, 4))
		# Two distinct recovery buttons. "Return to Origin" matches Manual
		# mode's Home (move motors to 0,0 and tare), and is the
		# mid-pause recalibration entry point. "Return to Start Well"
		# moves to the entered plate-start (well A1) coords; it stays
		# disabled mid-run since interrupting the snake-path would be
		# destructive.
		self.return_origin_btn = ttk.Button(ctrl, text="Return to Origin",
			command=app.return_to_origin)
		self.return_origin_btn.pack(side=tk.LEFT, padx=2)
		Tooltip(
			self.return_origin_btn,
			"Move both motors to (0, 0) and re-tare the position "
			"counters. Mid-pause it captures the paused position so "
			"Resume can drive the needle back.",
		)
		self.return_well_btn = ttk.Button(ctrl, text="Return to Start Well",
			command=app.return_to_start_well)
		self.return_well_btn.pack(side=tk.LEFT, padx=2)
		Tooltip(
			self.return_well_btn,
			"Move the needle to the plate-start coordinates (well A1). "
			"Idle-only; disabled while a run is in flight.",
		)
		# Pause button starts in the default TButton style; toggle_pause /
		# _update_run_control_buttons swap it to PauseRunning.TButton or
		# PausePaused.TButton as the run state changes.
		self.pause_btn = ttk.Button(ctrl, text="Pause", command=app.toggle_pause,
			style="PauseRunning.TButton")
		self.pause_btn.pack(side=tk.LEFT, padx=2)
		Tooltip(
			self.pause_btn,
			"Pause an in-flight run (pump off, motors hold). Click again "
			"to resume from the same cycle phase.",
		)
		self.continue_btn = ttk.Button(
			ctrl, text="Continue to Next Sample",
			command=app.continue_to_next_sample)
		self.continue_btn.pack(side=tk.LEFT, padx=2)
		Tooltip(
			self.continue_btn,
			"After the auto-pause at Total reached, start the next "
			"sample series: optional discard phase, then collection at "
			"the next available well.",
		)
		self.continue_plate_btn = ttk.Button(
			ctrl, text="Continue to Next Plate",
			command=app.continue_to_next_plate)
		self.continue_plate_btn.pack(side=tk.LEFT, padx=2)
		Tooltip(
			self.continue_plate_btn,
			"After the auto-pause at Plate full, open the plate-swap "
			"checklist and resume on the new plate.",
		)
		self.end_run_btn = ttk.Button(ctrl, text="End Run",
			command=self.end_run_clicked, style="Danger.TButton")
		self.end_run_btn.pack(side=tk.LEFT, padx=2)
		Tooltip(
			self.end_run_btn,
			"Finalize the run. Save and End writes end_*.json + "
			"summary*.md; Don't Save leaves only the raw log files; "
			"Cancel stays in the run.",
		)

		# ----- Bulk Sample Submission (top of column 0) --------------
		# Lets the operator preload a multi-sample session via a CSV.
		# Sits above Run Parameters because it acts as a configuration
		# upstream of the per-sample fields below.
		bulk = tk.LabelFrame(self, text="Bulk Sample Submission", padx=8, pady=2)
		# Spans both columns so the two-column layout below (Run Params +
		# Syringe Pump | Plate Params + Cleaning Parameters) sits flush
		# beneath it.
		bulk.grid(row=1, column=0, columnspan=2, sticky="new",
			padx=(2, 2), pady=(0, 4))
		bulk.grid_columnconfigure(0, weight=1)
		self.bulk_status_var = tk.StringVar(
			value="Status: No bulk submission active."
		)
		tk.Label(bulk, textvariable=self.bulk_status_var, anchor="w",
			justify="left", wraplength=380,
		).grid(row=0, column=0, sticky="we")
		self.bulk_source_var = tk.StringVar(value="")
		# Source-path line is gridded into row 1 only when bulk is
		# active; bulk_source_lbl.grid_remove() hides it cleanly.
		self.bulk_source_lbl = tk.Label(bulk, textvariable=self.bulk_source_var,
			anchor="w", justify="left", wraplength=380, fg=PALETTE["fg_muted"])
		self.bulk_source_lbl.grid(row=1, column=0, sticky="we")
		self.bulk_source_lbl.grid_remove()
		bulk_btn_row = tk.Frame(bulk, bg=PALETTE["bg_frame"])
		bulk_btn_row.grid(row=2, column=0, sticky="w", pady=(4, 0))
		self.bulk_template_btn = ttk.Button(
			bulk_btn_row, text="Generate Template",
			command=app.generate_bulk_template,
		)
		self.bulk_template_btn.pack(side=tk.LEFT, padx=(0, 4))
		Tooltip(
			self.bulk_template_btn,
			"Write a starter CSV with header comments + example rows. "
			"Fill it in your spreadsheet editor before clicking Import.",
		)
		self.bulk_import_btn = ttk.Button(
			bulk_btn_row, text="Import Submission",
			command=app.import_bulk_submission,
		)
		self.bulk_import_btn.pack(side=tk.LEFT, padx=4)
		Tooltip(
			self.bulk_import_btn,
			"Load a Bulk Sample Submission CSV. Validates every row "
			"before populating Run Parameters; rejects the import "
			"whole if any row fails.",
		)
		self.bulk_exit_btn = ttk.Button(
			bulk_btn_row, text="Exit Bulk Mode",
			command=app.exit_bulk_mode, style="Danger.TButton",
		)
		Tooltip(
			self.bulk_exit_btn,
			"Discard the loaded bulk submission and re-enable manual "
			"editing of Run Parameters.",
		)
		# Exit button only appears when active; managed by
		# _refresh_bulk_panel.

		# ----- Run Parameters --------------------------------------------
		# Project + Sample ID stay user-editable while a run is in progress
		# so the operator can update Sample ID a moment before clicking
		# Resume after a tube swap. Number of fractions / discards are
		# frozen at run start. Volume per well moved here from the old
		# pump-and-volume row because it's per-fraction metadata.
		runp = tk.LabelFrame(self, text="Run Parameters", padx=8, pady=2)
		self.runp_frame = runp  # exposed so bulk activation can mutate the title
		# Row 2 col 0: Run Parameters (shifted down to make room for the
		# Bulk panel at row 1). Pump LabelFrame stacks directly beneath
		# in row 3 col 0; Plate Parameters at col 1 spans rows 1-3.
		runp.grid(row=2, column=0, sticky="new", padx=(2, 4), pady=(0, 0))
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
		# First-launch default: 0 (no discard phase). A persisted last_used
		# value, if any, overrides this via set_values() after __init__.
		self.discard_te.set("0")
		Tooltip(
			self.discard_te.entry,
			"Initial fractions pumped to a waste bin before plate collection "
			"begins (e.g., low-density buffer above the band of interest). "
			"Set 0 to skip the discard phase.",
		)
		self.vol_text_entry = TextEntry(runp, "Volume per well (mL, e.g., 0.22):")
		self.vol_text_entry.grid(row=5, column=0, sticky="we")
		Tooltip(
			self.vol_text_entry.entry,
			"Per-fraction dispense volume. The pump runs for "
			"volume / pump_rate seconds at each well.",
		)

		# ----- Plate Parameters (middle) ---------------------------------
		# Plate geometry + the two coordinate pairs (plate-start and
		# waste-bin). The plate-start fields used to live in the table/carriage
		# move row; they're now part of plate definition.
		platep = tk.LabelFrame(self, text="Plate Parameters", padx=8, pady=2)
		# Row 2 col 1: Plate Parameters sits opposite Run Parameters in
		# the two-column layout. Cleaning Parameters fills row 3 col 1
		# below it, mirroring Run Params + Syringe Pump on the left.
		platep.grid(row=2, column=1, sticky="new", padx=(4, 2), pady=(0, 2))
		platep.grid_columnconfigure(0, weight=1)
		# JSON loader -- first row of Plate Parameters because loading a
		# file populates the rows/cols/well-width/starting-point entries
		# below. Compact label + path entry + Load button on one row.
		loader_row = tk.Frame(platep, bg=PALETTE["bg_frame"])
		loader_row.grid(row=0, column=0, sticky="we", pady=(0, 6))
		loader_row.grid_columnconfigure(1, weight=1)
		tk.Label(loader_row, text="Load labware specs:").grid(
			row=0, column=0, sticky="w", padx=(0, 6))
		self.json_entry = ttk.Entry(loader_row)
		self.json_entry.grid(row=0, column=1, sticky="we")
		load_btn = ttk.Button(loader_row, text="Load", command=self.load_json)
		load_btn.grid(row=0, column=2, sticky="we", padx=(6, 0))
		Tooltip(
			load_btn,
			"Open the labware/ folder. Reads an Opentrons-format JSON "
			"file and auto-fills rows, columns, well width, and starting "
			"well position below.",
		)

		self.rows_text_entry = TextEntry(platep, "Number of rows (1–16):")
		self.rows_text_entry.grid(row=1, column=0, sticky="we")
		Tooltip(
			self.rows_text_entry.entry,
			"Number of well rows on the loaded labware. Auto-filled by "
			"Load Labware Specs.",
		)
		self.cols_text_entry = TextEntry(platep, "Number of columns (1–24):")
		self.cols_text_entry.grid(row=2, column=0, sticky="we")
		Tooltip(
			self.cols_text_entry.entry,
			"Number of well columns on the loaded labware. Auto-filled "
			"by Load Labware Specs.",
		)
		self.ws_text_entry = TextEntry(platep, "Well width (cm):")
		self.ws_text_entry.grid(row=3, column=0, sticky="we")
		Tooltip(
			self.ws_text_entry.entry,
			"Center-to-center spacing between adjacent wells. Auto-filled "
			"by Load Labware Specs.",
		)

		self.table_te = TextEntry(platep, "Starting well position (x-axis):")
		self.table_te.grid(row=4, column=0, sticky="we")
		Tooltip(
			self.table_te.entry,
			"X coordinate of well A1 in cm. Set via Manual mode's "
			"Position Calibration Tool.",
		)
		self.carriage_te = TextEntry(platep, "Starting well position (y-axis):")
		self.carriage_te.grid(row=5, column=0, sticky="we")
		Tooltip(
			self.carriage_te.entry,
			"Y coordinate of well A1 in cm. Set via Manual mode's "
			"Position Calibration Tool.",
		)

		# Waste bin entries -- the previous "Waste bin:" sub-header is gone
		# because each entry's own label now reads "Waste bin position
		# (x-axis):" / "(y-axis):" without the cm-range suffix. Validation
		# still enforces the X [0, 20] / Y [0, 15] bounds.
		self.waste_table_te = TextEntry(
			platep, "Waste bin position (x-axis):",
			textvariable=app.waste_bin_table_var,
		)
		self.waste_table_te.grid(row=6, column=0, sticky="we", pady=(6, 0))
		self.waste_carriage_te = TextEntry(
			platep, "Waste bin position (y-axis):",
			textvariable=app.waste_bin_carriage_var,
		)
		self.waste_carriage_te.grid(row=7, column=0, sticky="we")

		# Focus-out normalization: when the operator tabs away after
		# typing a coordinate, reformat to 2 decimals so e.g. "13.6" or
		# "13" become "13.60" / "13.00". Invalid input is left as-is so
		# the existing inline validator can flag it on Begin.
		def _normalize_coord_entry(te):
			def _on_focus_out(_e):
				raw = te.get().strip()
				if not raw:
					return
				try:
					te.set(f"{float(raw):.2f}")
				except ValueError:
					return
			te.entry.bind("<FocusOut>", _on_focus_out, add="+")
		for _coord_te in (self.table_te, self.carriage_te,
				self.waste_table_te, self.waste_carriage_te):
			_normalize_coord_entry(_coord_te)
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

		# ----- Fractionation Pump Parameters ----------------------------
		# Column 0, row 3 — directly under Run Parameters so the
		# fractionation-controlling stack reads top-down. The
		# Skip-inter-sample-purge toggle moved to Tools → Preferences.
		frac_pump = tk.LabelFrame(self, text="Fractionation Pump Parameters",
			padx=8, pady=2)
		frac_pump.grid(row=3, column=0, sticky="new", padx=(2, 4), pady=(0, 2))
		frac_pump.grid_columnconfigure(0, weight=1)
		self.pump_rate_text_entry = TextEntry(frac_pump,
			"Pump rate (mL/hr — see your syringe pump spec):")
		self.pump_rate_text_entry.grid(row=0, column=0, sticky="we")
		self.drip_wait_te = TextEntry(frac_pump, "Drip wait time (s):")
		self.drip_wait_te.grid(row=1, column=0, sticky="we")
		self.drip_wait_te.set("1.0")
		Tooltip(
			self.drip_wait_te.entry,
			"Wait time between pump-off and moving to the next well. "
			"Longer waits improve volume consistency; shorter waits run faster.",
		)

		# ----- Cleaning Parameters --------------------------------------
		# Column 1, row 3 — under Plate Parameters and opposite Syringe
		# Pump. Groups everything related to the peristaltic pump used
		# for inter-sample purges, manual purges, and Cleaning Purge.
		cleaning_params = tk.LabelFrame(self, text="Cleaning Parameters", padx=8, pady=2)
		cleaning_params.grid(row=3, column=1, sticky="new", padx=(4, 2), pady=(0, 2))
		cleaning_params.grid_columnconfigure(0, weight=1)
		self.purge_time_te = TextEntry(
			cleaning_params, "Purge time (s):", textvariable=app.purge_time_var,
		)
		self.purge_time_te.grid(row=0, column=0, sticky="we")
		Tooltip(
			self.purge_time_te.entry,
			"Per-phase duration of the inter-sample purge. Two pump phases "
			"run between samples (wash then air-clear), each lasting this "
			"many seconds. Use Cleaning mode's Purge Time Calibration to "
			"measure the right value for your tubing.",
		)
		self.peristaltic_rate_te = TextEntry(
			cleaning_params, "Peristaltic pump rate (mL/min):",
			textvariable=app.peristaltic_rate_var,
		)
		self.peristaltic_rate_te.grid(row=1, column=0, sticky="we")
		Tooltip(
			self.peristaltic_rate_te.entry,
			"Flow rate of the peristaltic pump used for purges. Drives "
			"the waste-bin estimate during purge operations (inter-sample "
			"purges, Manual Purge, Cleaning Purge, Purge Time Calibration).",
		)
		self.max_waste_te = TextEntry(
			cleaning_params, "Max waste bin volume (mL):",
			textvariable=app.max_waste_volume_var,
		)
		self.max_waste_te.grid(row=2, column=0, sticky="we")
		Tooltip(
			self.max_waste_te.entry,
			"Capacity of your waste container. autoSIP warns at 80% and "
			"halts all pump activity at 100% to prevent overflow. The "
			"estimate is based on configured pump rates × pump-on time, "
			"not a real measurement.",
		)

		# Begin Fractionation -- the run-launch button. (The previous
		# "Move (jog to Plate-start coords)" button was removed because the
		# Return to Start Well button in the run-controls row already
		# moves to the same position.)
		#
		# Visually this is a composite: a small Canvas drawing a 45-deg
		# ultracentrifuge tube on the left, and the action button on the
		# right, both sharing the accent color so they read as one unit.
		# Clicks on the tube canvas fall through to begin_clicked too so
		# the entire region behaves like a single button.
		begin_frame = tk.Frame(self, bg=PALETTE["accent"], bd=0, highlightthickness=0)
		begin_frame.grid(row=4, column=0, columnspan=2, sticky="we", pady=(4, 0))
		# Outer empty columns expand so the [tube | button | distribution]
		# trio sits centered as a group, with the icons bookending the
		# centered text immediately on either side of the button.
		begin_frame.grid_columnconfigure(0, weight=1)
		begin_frame.grid_columnconfigure(4, weight=1)
		self.begin_tube_canvas = make_centrifuge_tube_canvas(begin_frame, size=40)
		self.begin_tube_canvas.grid(row=0, column=1, padx=(0, 6), pady=4)
		self.begin_tube_canvas.bind("<Button-1>", lambda _e: self.begin_clicked())
		self.begin_btn = primary_button(
			begin_frame, text="Begin Fractionation",
			command=self.begin_clicked,
		)
		self.begin_btn.grid(row=0, column=2, pady=4)
		Tooltip(
			self.begin_btn,
			"Begin Fractionation: starts a new run from idle. To "
			"advance or end an in-progress run, use the Run Controls "
			"row above.",
		)
		# Bimodal-distribution canvas on the right: the SIP-experiment
		# readout (two density curves, one with a larger heavy-isotope
		# peak). Also click-through to begin_clicked for symmetry with
		# the tube canvas.
		self.begin_dist_canvas = make_bimodal_distribution_canvas(
			begin_frame, width=64, height=40,
		)
		self.begin_dist_canvas.grid(row=0, column=3, padx=(6, 0), pady=4)
		self.begin_dist_canvas.bind("<Button-1>", lambda _e: self.begin_clicked())

		# Progress view -- to-scale well plate, color-blind-safe palette,
		# header showing current well + count + elapsed/remaining time.
		# min_height bumped so the canvas claims a fair share of the
		# vertical budget at default window size -- otherwise the
		# LabelFrames above it shrink the canvas to ~70 px tall and the
		# plate clamps to its 12-px-per-well floor.
		self.progress = WellPlateProgress(self, min_width=500, min_height=400)
		self.progress.grid(row=5, column=0, columnspan=2, sticky="nsew")
		self.grid_rowconfigure(5, weight=1)

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
			"purge_time": self.purge_time_te,
			"peristaltic_rate": self.peristaltic_rate_te,
			"max_waste_volume": self.max_waste_te,
			"volume_per_well": self.vol_text_entry,
			"table_start": self.table_te,
			"carriage_start": self.carriage_te,
			"waste_bin_table": self.waste_table_te,
			"waste_bin_carriage": self.waste_carriage_te,
		}.get(field)

	def end_run_clicked(self):
		self.app.end_run()

	def get_values(self):
		"""Snapshot every persistable field as a {field: str} dict.

		The Skip checkbox is serialized as ``"true"`` / ``"false"`` so the
		same string-typed config file can round-trip it; ``set_values``
		parses it back into the BooleanVar.
		"""
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
		corresponding widget untouched.

		Empty-string values are also treated as "leave alone" so the
		first-launch defaults set in ``__init__`` (e.g. Discard fractions = 0,
		Plate ID = "Plate-1") survive a load_last_used() that returns a
		dict with mostly-empty fields (which happens when only some
		fields have ever been edited or when the volume-bounds migration
		rewrites config.json).
		"""
		_coord_fields = {
			"table_start", "carriage_start",
			"waste_bin_table", "waste_bin_carriage",
		}
		for field in config_store.FIELDS:
			if field not in values:
				continue
			val = values[field] or ""
			if val == "":
				continue
			if field == "labware_file":
				self.json_entry.delete(0, tk.END)
				self.json_entry.insert(0, val)
			else:
				# Coordinate fields are normalized to 2 decimals so a
				# legacy config.json with "13.650" or "12" displays as
				# "13.65" / "12.00" without the operator having to
				# re-save. Non-numeric values pass through unchanged.
				if field in _coord_fields:
					try:
						val = f"{float(val):.2f}"
					except (TypeError, ValueError):
						pass
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
		"""Re-sync widgets and state when the Automated frame becomes active.

		During an active run (state.state != "idle") the position
		counters and status text MUST NOT be reset -- mode-switching
		mid-run was zeroing s.x/s.y, which caused the next move to
		send the needle back to the plate origin and the next
		well_dispensing call to mark (0,0) as the active well, wiping
		the plate visualization back to the first column.
		"""
		s = self.app.state
		self._clear_all_errors()
		if s.state == "idle":
			s.x = 0
			s.y = 0
			s.carriage_forwards = True
			self.app.set_status("System idle.")

	def _clear_all_errors(self):
		for te in (
			self.project_te, self.sample_id_te, self.plate_id_te,
			self.n_fractions_te, self.discard_te,
			self.rows_text_entry, self.cols_text_entry, self.ws_text_entry,
			self.pump_rate_text_entry, self.drip_wait_te, self.purge_time_te,
			self.peristaltic_rate_te, self.max_waste_te,
			self.vol_text_entry,
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
		# pasted a path doesn't lose their place. Falls back to the repo's
		# labware/ folder (created on demand) so first-launch operators
		# land on the bundled Opentrons specs instead of $HOME.
		typed = self.json_entry.get().strip()
		initial_dir = None
		initial_file = None
		if typed:
			if os.path.isdir(typed):
				initial_dir = typed
			elif os.path.exists(typed):
				initial_dir = os.path.dirname(typed) or None
				initial_file = os.path.basename(typed)
		if initial_dir is None:
			labware_dir = Path(__file__).parent / "labware"
			labware_dir.mkdir(parents=True, exist_ok=True)
			initial_dir = str(labware_dir)

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
		# Coordinates display with 2 decimals to match the rest of the
		# UI; raw str() on these float expressions otherwise produces
		# 13.650000000000001-style readings.
		self.table_te.set(f"{15 - a1_x * 0.1:.2f}")
		self.carriage_te.set(f"{0.1 * (y_dim - a1_y) - 0.5:.2f}")

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
			(self.purge_time_te, validation.purge_time),
			(self.peristaltic_rate_te, validation.peristaltic_rate),
			(self.max_waste_te, validation.max_waste_volume),
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
				drip_v, purge_v, peri_v, max_waste_v) = parsed
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
					f"Waste bin position ({waste_x:.2f} cm, {waste_y:.2f} cm) appears "
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
			purge_time=purge_v,
			skip_intersample_purge=self.app.skip_intersample_purge_var.get(),
			peristaltic_rate_ml_per_min=peri_v,
			max_waste_volume_ml=max_waste_v,
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

		# Run-active banner: gridded only while an Automated run is in
		# flight (managed by set_run_active_lock). Amber background so
		# the operator's eye lands on it before reaching the (now
		# greyed-out) jog buttons below.
		self.run_active_banner = tk.Label(
			self, anchor="w", justify="left", wraplength=540,
			bg="#fff3cd", fg="#7a5d00",
			padx=8, pady=4,
			text=(
				"Automated run in progress — controls disabled to "
				"prevent interference. Return to Automated tab to manage "
				"the run."
			),
		)
		# Stays unmapped until set_run_active_lock(True) is called.
		self.run_active_banner.grid(row=0, column=0, sticky="we", padx=4, pady=(4, 0))
		self.run_active_banner.grid_remove()

		# ---- Jog Controls ----
		# Row 1 (was row 0 -- banner now occupies row 0).
		jog = tk.LabelFrame(self, text="Jog Controls", padx=8, pady=8)
		jog.grid(row=1, column=0, sticky="new", padx=4, pady=4)
		jog.grid_columnconfigure(0, weight=1)

		# Return to Origin sits ABOVE the directional pad so the
		# calibration-anchor action is the first thing the operator
		# reaches. Name + action match Automated mode's run-control
		# button by design -- they're redundant entry points to the
		# same "move to (0, 0) + tare" path.
		self.home_btn = primary_button(jog, text="Return to Origin",
			command=self._home_clicked)
		self.home_btn.grid(row=0, column=0, sticky="we", pady=(0, 6))
		Tooltip(
			self.home_btn,
			"Drive both motors to (0, 0) and re-tare the position "
			"counters. Use to recalibrate after suspected stepper drift "
			"by manually re-parking the carriage first.",
		)

		# Plus-pad of directional buttons. Corners empty.
		pad = tk.Frame(jog)
		pad.grid(row=1, column=0, pady=(0, 8))
		self.y_plus_btn = ttk.Button(
			pad, text="▲ Y+", width=8,
			command=lambda: self._jog("y", +1),
		)
		self.y_plus_btn.grid(row=0, column=1, padx=2, pady=2)
		Tooltip(self.y_plus_btn, "Jog one step toward Y origin (refused if at the limit).")
		self.x_minus_btn = ttk.Button(
			pad, text="◀ X−", width=8,
			command=lambda: self._jog("x", -1),
		)
		self.x_minus_btn.grid(row=1, column=0, padx=2, pady=2)
		Tooltip(self.x_minus_btn, "Jog one step in the −X direction.")
		self.x_plus_btn = ttk.Button(
			pad, text="X+ ▶", width=8,
			command=lambda: self._jog("x", +1),
		)
		self.x_plus_btn.grid(row=1, column=2, padx=2, pady=2)
		Tooltip(self.x_plus_btn, "Jog one step in the +X direction.")
		self.y_minus_btn = ttk.Button(
			pad, text="Y− ▼", width=8,
			command=lambda: self._jog("y", -1),
		)
		self.y_minus_btn.grid(row=2, column=1, padx=2, pady=2)
		Tooltip(self.y_minus_btn, "Jog one step away from Y origin (toward the plate).")

		# Step-size radio group
		self.step_var = tk.DoubleVar(value=0.1)  # default 1 mm
		step_row = tk.Frame(jog)
		step_row.grid(row=2, column=0, sticky="w", pady=(0, 6))
		tk.Label(step_row, text="Step size selector:").pack(side=tk.LEFT, padx=(0, 6))
		# Stash radios on self so set_run_active_lock can disable them.
		self.step_radios = []
		for label, value in self._STEPS:
			rb = tk.Radiobutton(
				step_row, text=label, variable=self.step_var, value=value,
			)
			rb.pack(side=tk.LEFT, padx=2)
			Tooltip(rb, f"Jog buttons move {label} per click.")
			self.step_radios.append(rb)

		# Position readout
		self.position_var = tk.StringVar(value="Position: X = 0.00 cm, Y = 0.00 cm")
		self.position_lbl = tk.Label(jog, textvariable=self.position_var, anchor="w")
		self.position_lbl.grid(row=3, column=0, sticky="we")

		# ---- Pump Controls ----
		pump = tk.LabelFrame(self, text="Pump Controls", padx=8, pady=8)
		pump.grid(row=2, column=0, sticky="new", padx=4, pady=(0, 4))
		pump.grid_columnconfigure(0, weight=1)
		pump.grid_columnconfigure(1, weight=1)

		# Layout each pump button as a (button + hint) stack so the
		# "(Space)" hint can be gridded immediately to the right of the
		# button without shifting button widths. Only one hint is gridded
		# at a time -- the one for app.last_pump_used.
		frac_wrap = tk.Frame(pump)
		frac_wrap.grid(row=0, column=0, sticky="we", padx=(0, 4), pady=4)
		frac_wrap.grid_columnconfigure(0, weight=1)
		self.fractionate_btn = ttk.Button(
			frac_wrap, text="Fractionate: OFF",
			command=lambda: app._handle_pump_click("fractionate", parent=self),
			style="PumpOff.TButton", cursor="hand2",
		)
		self.fractionate_btn.grid(row=0, column=0, sticky="we")
		Tooltip(
			self.fractionate_btn,
			"Toggle the relay assuming the syringe (Razel R-200) pump "
			"is wired in. Confirmation prompt the first time per session.",
		)
		self.fractionate_space_lbl = tk.Label(
			frac_wrap, text="(Space)", fg="gray40",
		)
		# Not gridded by default; ``_set_space_hint`` decides which side
		# is visible based on app.last_pump_used.

		purge_wrap = tk.Frame(pump)
		purge_wrap.grid(row=0, column=1, sticky="we", padx=(4, 0), pady=4)
		purge_wrap.grid_columnconfigure(0, weight=1)
		self.purge_btn = ttk.Button(
			purge_wrap, text="Purge: OFF",
			command=lambda: app._handle_pump_click("purge", parent=self),
			style="PumpOff.TButton", cursor="hand2",
		)
		self.purge_btn.grid(row=0, column=0, sticky="we")
		Tooltip(
			self.purge_btn,
			"Toggle the relay assuming the peristaltic (Adafruit 3910) "
			"pump is wired in. Confirmation prompt the first time per "
			"session.",
		)
		self.purge_space_lbl = tk.Label(
			purge_wrap, text="(Space)", fg="gray40",
		)

		# ---- Position Calibration ----
		# Captures the current X, Y position and writes it to Automated
		# mode's Starting Well Position or Waste Bin Position fields,
		# replacing the error-prone "read off the readout and re-type
		# in the other mode" workflow. The Cleaning-mode Waste bin
		# entries share variables with Automated's so the waste-bin
		# save propagates to both modes.
		cal = tk.LabelFrame(self, text="Position Calibration Tool", padx=8, pady=8)
		cal.grid(row=3, column=0, sticky="new", padx=4, pady=(0, 4))
		cal.grid_columnconfigure(0, weight=1)

		tk.Label(cal, anchor="w", justify="left", wraplength=540, text=(
			"Use the jog controls above to position the needle, then click "
			"the corresponding button to save the current position as a "
			"parameter used by Automated mode."
		)).grid(row=0, column=0, sticky="we", pady=(0, 6))

		tk.Label(cal, anchor="w", justify="left",
			text="1. Position the needle over well A1 of your plate.",
		).grid(row=1, column=0, sticky="w")
		self._cal_position_var = tk.StringVar(
			value="   Current position: X = 0.00 cm, Y = 0.00 cm")
		tk.Label(cal, textvariable=self._cal_position_var,
			anchor="w").grid(row=2, column=0, sticky="we", padx=(20, 0))
		self.save_start_btn = save_start_btn = primary_button(
			cal, text="Save as Starting Well Position",
			command=self._save_starting_well_position,
		)
		save_start_btn.grid(row=3, column=0, sticky="w", padx=(20, 0), pady=(2, 8))
		Tooltip(
			save_start_btn,
			"Write the current motor position to Automated mode's "
			"Starting well position (x-axis / y-axis) fields.",
		)

		tk.Label(cal, anchor="w", justify="left",
			text="2. Position the needle over the waste bin opening.",
		).grid(row=4, column=0, sticky="w")
		self.save_waste_btn = save_waste_btn = primary_button(
			cal, text="Save as Waste Bin Position",
			command=self._save_waste_bin_position,
		)
		save_waste_btn.grid(row=5, column=0, sticky="w", padx=(20, 0), pady=(2, 0))
		Tooltip(
			save_waste_btn,
			"Write the current motor position to the Waste bin position "
			"(x-axis / y-axis) fields shared with Cleaning mode.",
		)

	def refresh(self):
		"""Re-sync the position readout and the space-bar hint when this
		mode becomes active. (The pump-confirmation dialog is now
		session-scoped, not per-visit, so the previous reset of a
		per-Manual-visit set is gone.)
		"""
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

	def _current_position_cm(self):
		"""Live (x, y) in cm from the motor angles. Y is in motor frame
		(negative as the carriage moves down from origin); callers
		converting to the Automated-mode user frame use ``abs(y)``."""
		x = self.app.table_motor.get_angle() * self.app.table_motor.cm_per_deg
		y = self.app.carriage_motor.get_angle() * self.app.carriage_motor.cm_per_deg
		return x, y

	def _save_position_to(self, table_field, carriage_field, label):
		"""Common save path: read current position, defensively bounds-
		check against the Automated-mode validator ranges, write to the
		target StringVars, surface a status confirmation.
		"""
		x_motor, y_motor = self._current_position_cm()
		# Automated mode's Y validator is [0, 15]; Manual frames the
		# Y axis as motor-negative for "down from origin", so take the
		# absolute value for the saved parameter.
		x_save = x_motor
		y_save = abs(y_motor)
		# Defensive bounds check. Manual mode's jog soft-limits should
		# already keep us in range; this just guards against the
		# unlikely case where the readout drifted outside.
		if not (validation.TABLE_POS_MIN <= x_save <= validation.TABLE_POS_MAX):
			messagebox.showerror(
				"Position out of range",
				f"Position out of range: X-axis must be between "
				f"{validation.TABLE_POS_MIN} and {validation.TABLE_POS_MAX} cm. "
				f"Current value: {x_save:.2f} cm. Jog the needle into "
				"range before saving.",
				parent=self,
			)
			return
		if not (validation.CARRIAGE_POS_MIN <= y_save <= validation.CARRIAGE_POS_MAX):
			messagebox.showerror(
				"Position out of range",
				f"Position out of range: Y-axis must be between "
				f"{validation.CARRIAGE_POS_MIN} and "
				f"{validation.CARRIAGE_POS_MAX} cm. Current value: "
				f"{y_save:.2f} cm. Jog the needle into range before saving.",
				parent=self,
			)
			return
		table_field.set(f"{x_save:.2f}")
		carriage_field.set(f"{y_save:.2f}")
		self.app.set_status(
			f"{label} saved: X = {x_save:.2f} cm, Y = {y_save:.2f} cm"
		)

	def _save_starting_well_position(self):
		af = self.app.automated_frame
		self._save_position_to(af.table_te, af.carriage_te,
			"Starting well position")

	def _save_waste_bin_position(self):
		# These TextEntries are bound to App-level shared StringVars, so
		# writing through either widget updates both Automated and
		# Cleaning mode's mirrored fields.
		af = self.app.automated_frame
		self._save_position_to(af.waste_table_te, af.waste_carriage_te,
			"Waste bin position")

	def refresh_position_readout(self):
		"""Re-render the Position: X = ..., Y = ... label from live motor
		angles. Called by ``_jog``, ``_home_clicked``, and ``refresh`` so
		any motion the user can initiate from this frame updates the
		display immediately. Also updates the Position Calibration
		``Current position`` line so the operator can read the live
		coordinates while jogging toward a save target.
		"""
		x = self.app.table_motor.get_angle() * self.app.table_motor.cm_per_deg
		y = self.app.carriage_motor.get_angle() * self.app.carriage_motor.cm_per_deg
		self.position_var.set(f"Position: X = {x:.2f} cm, Y = {y:.2f} cm")
		# Mirror to the calibration panel's live readout. Show the
		# absolute Y so it matches what gets saved (Automated mode's
		# Y validator expects [0, 15]).
		self._cal_position_var.set(
			f"   Current position: X = {x:.2f} cm, Y = {abs(y):.2f} cm"
		)

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

	def set_run_active_lock(self, active):
		"""Toggle the visible enabled-state of every Manual control
		that could interfere with an active Automated run. The banner
		at the top of the frame is gridded when ``active`` and
		grid_remove'd otherwise, matching the disabled-controls cue.
		Pump buttons are governed by ``refresh_pump_buttons`` via the
		``in_run`` flag so they aren't touched here.
		"""
		if active:
			self.run_active_banner.grid()
		else:
			self.run_active_banner.grid_remove()
		state = tk.DISABLED if active else tk.NORMAL
		for btn in (
			self.y_plus_btn, self.x_minus_btn, self.x_plus_btn, self.y_minus_btn,
			self.home_btn, self.save_start_btn, self.save_waste_btn,
		):
			btn["state"] = state
		for rb in self.step_radios:
			rb["state"] = state


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

		# Run-active banner: gridded only while an Automated run is in
		# flight. Spans both columns and sits above all controls.
		self.run_active_banner = tk.Label(
			self, anchor="w", justify="left", wraplength=540,
			bg="#fff3cd", fg="#7a5d00",
			padx=8, pady=4,
			text=(
				"Automated run in progress — controls disabled to "
				"prevent interference. Return to Automated tab to manage "
				"the run."
			),
		)
		self.run_active_banner.grid(row=0, column=0, columnspan=2,
			sticky="we", padx=2, pady=(2, 0))
		self.run_active_banner.grid_remove()

		# Waste-bin coords -- bound to the same App-level StringVars as
		# Automated mode's Waste bin entries, so an edit in either mode
		# propagates automatically.
		bin_frame = tk.LabelFrame(self, text="Waste bin", padx=8, pady=4)
		bin_frame.grid(row=1, column=0, columnspan=2, sticky="we", padx=2, pady=(2, 4))
		bin_frame.grid_columnconfigure(0, weight=1)
		self.waste_table_te = TextEntry(
			bin_frame, "Waste bin position (x-axis):",
			textvariable=app.waste_bin_table_var,
		)
		self.waste_table_te.grid(row=0, column=0, sticky="we")
		Tooltip(
			self.waste_table_te.entry,
			"Mirrors Automated mode's Waste bin position (x-axis). "
			"Edits propagate in both directions.",
		)
		self.waste_carriage_te = TextEntry(
			bin_frame, "Waste bin position (y-axis):",
			textvariable=app.waste_bin_carriage_var,
		)
		self.waste_carriage_te.grid(row=1, column=0, sticky="we")
		Tooltip(
			self.waste_carriage_te.entry,
			"Mirrors Automated mode's Waste bin position (y-axis). "
			"Edits propagate in both directions.",
		)

		self.move_btn = primary_button(
			self, text="Move to Waste Bin", command=self.move_clicked,
		)
		self.move_btn.grid(row=2, column=0, columnspan=2, sticky="we", padx=2, pady=2)
		Tooltip(
			self.move_btn,
			"Drive the needle to the Waste bin coordinates above so "
			"you can flush wash through the tubing into the bin.",
		)
		# Purge button + Space-shortcut hint. The hint sits next to the
		# button (column 1 of the wrap frame) so the operator sees at a
		# glance that Space toggles Purge in Cleaning mode. Lives inside
		# CleaningFrame, so it's automatically hidden when the frame is
		# grid_remove'd on a mode switch.
		purge_wrap = tk.Frame(self)
		purge_wrap.grid(row=3, column=0, columnspan=2, sticky="we", padx=2, pady=2)
		purge_wrap.grid_columnconfigure(0, weight=1)
		self.purge_btn = ttk.Button(
			purge_wrap, text="Purge: OFF",
			command=lambda: app._handle_pump_click("purge", parent=self),
			style="PumpOff.TButton", cursor="hand2",
		)
		self.purge_btn.grid(row=0, column=0, sticky="we")
		Tooltip(
			self.purge_btn,
			"Toggle the peristaltic pump for a free-form cleaning purge. "
			"Watch the tubing and click again to stop when the line "
			"reads clean. Space-bar shortcut active in this mode.",
		)
		self.purge_space_lbl = tk.Label(
			purge_wrap, text="(Space)", fg="gray40",
		)
		self.purge_space_lbl.grid(row=0, column=1, padx=(4, 0))

		# Purge Time Calibration sub-panel. Measures how long wash takes
		# to fully replace one tubing-volume so the operator can save the
		# resulting value as Automated mode's Purge time parameter.
		cal = tk.LabelFrame(self, text="Purge Time Calibration Tool",
			padx=8, pady=6)
		cal.grid(row=4, column=0, columnspan=2, sticky="we", padx=2, pady=(8, 2))
		cal.grid_columnconfigure(0, weight=1)

		tk.Label(cal, anchor="w", justify="left", wraplength=540, text=(
			"Measure how long it takes wash solution to flow through your "
			"tubing setup. The result can be saved as the Purge time "
			"parameter used by Automated mode.\n"
			"  1. Place the inlet line in your wash solution container.\n"
			"  2. Click Start. The pump runs and a timer begins.\n"
			"  3. Watch the outlet. Click Stop the moment wash first "
			"appears at the outlet — this represents one full tubing volume."
		)).grid(row=0, column=0, sticky="we", pady=(0, 6))

		self._cal_elapsed_var = tk.StringVar(value="Elapsed: 0.0 s")
		tk.Label(cal, textvariable=self._cal_elapsed_var,
			anchor="w").grid(row=1, column=0, sticky="we")

		cal_btn_row = tk.Frame(cal)
		cal_btn_row.grid(row=2, column=0, sticky="w", pady=(4, 4))
		self.cal_start_btn = ttk.Button(cal_btn_row, text="Start",
			command=self._cal_start, style="Primary.TButton")
		self.cal_start_btn.pack(side=tk.LEFT, padx=(0, 4))
		Tooltip(self.cal_start_btn,
			"Start the pump + the elapsed-time timer. Click Stop when "
			"the wash first reaches the outlet.")
		self.cal_stop_btn = ttk.Button(cal_btn_row, text="Stop",
			command=self._cal_stop)
		self.cal_stop_btn.pack(side=tk.LEFT, padx=4)
		Tooltip(self.cal_stop_btn,
			"Stop the pump and freeze the elapsed time as the measured "
			"value. Save the measurement below or click Reset to retry.")
		self.cal_reset_btn = ttk.Button(cal_btn_row, text="Reset",
			command=self._cal_reset)
		self.cal_reset_btn.pack(side=tk.LEFT, padx=4)
		Tooltip(self.cal_reset_btn,
			"Discard the current measurement and re-arm Start.")

		self._cal_measured_var = tk.StringVar(value="Measured: -- s")
		tk.Label(cal, textvariable=self._cal_measured_var,
			anchor="w").grid(row=3, column=0, sticky="we", pady=(2, 0))
		self.cal_save_btn = ttk.Button(cal, text="Save as Purge Time",
			command=self._cal_save, style="Primary.TButton")
		self.cal_save_btn.grid(row=4, column=0, sticky="w", pady=(2, 0))
		Tooltip(self.cal_save_btn,
			"Write the measured time into Automated mode's Purge time "
			"(Cleaning Parameters) field.")

		# Calibration state
		self._cal_t_start = None
		self._cal_measured_s = None
		self._cal_tick_after = None
		self._cal_set_buttons_idle()

	# -- Calibration helpers ------------------------------------------

	def _cal_set_buttons_idle(self):
		self.cal_start_btn.state(["!disabled"])
		self.cal_stop_btn.state(["disabled"])
		self.cal_reset_btn.state(["!disabled"])
		self._cal_save_button_sync()

	def _cal_save_button_sync(self):
		"""Enable Save only when a measured value is in [PURGE_TIME_MIN, MAX]."""
		ok = (
			self._cal_measured_s is not None
			and validation.PURGE_TIME_MIN <= self._cal_measured_s <= validation.PURGE_TIME_MAX
		)
		if ok:
			self.cal_save_btn.state(["!disabled"])
		else:
			self.cal_save_btn.state(["disabled"])

	def _cal_start(self):
		"""Power the pump on and start the elapsed-time tick. This is
		an automated-workflow path (its own setup-style action), so we
		bypass the per-pump confirmation dialog and drive the relay
		directly via the PumpController. The operator is expected to
		have already verified the peristaltic pump is connected -- the
		manual Purge button on the same screen is the place to learn
		that, and its first-of-session confirmation covers that role.
		"""
		pc = self.app.pump_controller
		# If something else holds the claim, bail out -- the user sees
		# the interlock state on the Purge button.
		if not pc.is_available_for("purge"):
			messagebox.showinfo(
				"Pump in use",
				"The Fractionate claim is currently active. Stop it before "
				"running the calibration timer.",
				parent=self,
			)
			return
		if pc.claimant != "purge":
			pc.claim_for("purge")
		if not pc.relay_on:
			pc.set_relay(True)
		self._cal_t_start = monotonic()
		self.cal_start_btn.state(["disabled"])
		self.cal_stop_btn.state(["!disabled"])
		self._tick_cal()

	def _tick_cal(self):
		if self._cal_t_start is None:
			return
		elapsed = monotonic() - self._cal_t_start
		self._cal_elapsed_var.set(f"Elapsed: {elapsed:.1f} s")
		self._cal_tick_after = self.after(100, self._tick_cal)

	def _cal_stop(self):
		"""Power the pump off, record elapsed, freeze the timer."""
		if self._cal_t_start is None:
			return
		elapsed = monotonic() - self._cal_t_start
		self._cal_t_start = None
		if self._cal_tick_after is not None:
			try:
				self.after_cancel(self._cal_tick_after)
			except Exception:
				pass
			self._cal_tick_after = None
		pc = self.app.pump_controller
		if pc.claimant == "purge" and pc.relay_on:
			pc.set_relay(False)
			pc.release()
		self._cal_measured_s = elapsed
		self._cal_elapsed_var.set(f"Elapsed: {elapsed:.1f} s")
		self._cal_measured_var.set(f"Measured: {elapsed:.1f} s")
		self.cal_stop_btn.state(["disabled"])
		self.cal_start_btn.state(["!disabled"])
		self._cal_save_button_sync()

	def _cal_reset(self):
		"""Clear measurement state without running the pump. If the pump
		is currently on (mid-measurement), it stays on; only the UI
		resets. The operator can call _cal_stop separately."""
		# If we're mid-timer, stop the tick but leave pump alone (caller
		# decides). For simplicity here, we stop everything.
		if self._cal_t_start is not None:
			self._cal_stop()
		self._cal_t_start = None
		self._cal_measured_s = None
		self._cal_elapsed_var.set("Elapsed: 0.0 s")
		self._cal_measured_var.set("Measured: -- s")
		self._cal_save_button_sync()

	def _cal_save(self):
		"""Write the measured value to App.purge_time_var so Automated
		mode's Purge time entry picks it up."""
		if self._cal_measured_s is None:
			return
		val = f"{self._cal_measured_s:.1f}"
		self.app.purge_time_var.set(val)
		try:
			config_store.save_last_used(self.app.automated_frame.get_values())
		except OSError as exc:
			logger.warning("Failed to persist purge_time: %s", exc)
		messagebox.showinfo(
			"Purge time saved",
			f"Purge time saved: {val} s. Automated mode's Purge time "
			"parameter is now set to this value.",
			parent=self,
		)

	def refresh(self):
		"""Re-sync widgets when Cleaning mode becomes active.

		Same guard as ``AutomatedFrame.refresh``: never mutate run
		state or override the status text while a run is active --
		mode-switching mid-run was clobbering the fractionation
		state machine's direction flag and status display.
		"""
		self.waste_table_te.clear_error()
		self.waste_carriage_te.clear_error()
		s = self.app.state
		if s.state == "idle":
			s.carriage_forwards = True
			self.app.set_status("System idle.")

	def set_controls_enabled(self, enabled):
		self.move_btn["state"] = tk.NORMAL if enabled else tk.DISABLED

	def refresh_pump_buttons(self, claimant, relay_on, in_run):
		_update_pump_button(self.purge_btn, "purge", claimant, relay_on, in_run)

	def set_run_active_lock(self, active):
		"""Disable every Cleaning control that could interfere with an
		active Automated run, and grid the active-run banner. Waste-bin
		coordinate entries are made read-only because their Tk variable
		is shared with Automated mode's Waste bin entries -- a mid-run
		edit here would silently redirect the live run's waste target.
		"""
		if active:
			self.run_active_banner.grid()
		else:
			self.run_active_banner.grid_remove()
		state = tk.DISABLED if active else tk.NORMAL
		# Coord entries: use the ttk.Entry state so the operator can
		# still see the values, just not edit them.
		for te in (self.waste_table_te, self.waste_carriage_te):
			te.entry.configure(state="disabled" if active else "normal")
		for btn in (
			self.move_btn, self.purge_btn,
			self.cal_start_btn, self.cal_stop_btn,
			self.cal_reset_btn, self.cal_save_btn,
		):
			btn["state"] = state

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

	@property
	def bulk_mode_active(self):
		"""True iff a bulk submission is currently loaded. Cleared by
		Exit Bulk Mode, End Run, and Terminate Run."""
		return bool(self.bulk_samples)

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
		# Default window sized to fit comfortably on a 1080p screen with
		# room for window-manager chrome and a taskbar. The side-by-side
		# Run/Plate Parameters layout in AutomatedFrame means the upper
		# section only needs ~350 px, so this leaves ~600 px for the
		# progress canvas at default size.
		self.geometry("1200x1000")
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
		# Pump-confirmation latches. The "Activating the relay" dialog
		# fires only on the FIRST user-initiated activation of each pump
		# per session; subsequent toggles skip the dialog. Not persisted
		# to disk, so a fresh process starts with both flags False. Reset
		# back to False on Terminate Run, since the operator may have
		# changed hardware during the stop.
		self.fractionate_confirmed_this_session = False
		self.purge_confirmed_this_session = False
		# Which pump the space-bar shortcut targets in Manual mode. Updated
		# every time the user clicks (or space-activates) one of the two
		# pump buttons, and persisted across launches via config_store.
		self.last_pump_used = config_store.load_last_pump_used()

		# Whether the close handler should drive the needle back to
		# (0, 0) before the window goes away. Persistent top-level
		# preference; toggled via the Tools -> Preferences dialog.
		self.return_to_origin_on_exit = config_store.load_return_to_origin_on_exit()

		# Inter-sample purge protocol: "basic" (water → sample) or
		# "decontamination" (water → bleach → water → sample). Set via
		# Tools → Preferences and persisted to the top level of
		# config.json. Read at the start of each inter-sample purge so
		# a mid-session change applies to the next transition.
		self.purge_protocol = config_store.load_purge_protocol()

		# Bulk Sample Submission. Operator imports a CSV of sample
		# metadata before clicking Begin Fractionation. Each entry of
		# ``bulk_samples`` is a dict with keys:
		#   sample_id (required), plate_id, number_of_fractions,
		#   discard_fractions, volume_per_well_ml, notes,
		#   spreadsheet_sample_id (original CSV value before any
		#   transition-dialog edit), edited (bool).
		# Ephemeral -- not persisted. ``bulk_mode_active`` is the
		# convenience property below.
		self.bulk_samples = []
		self.bulk_current_index = 0
		self.bulk_source_path = ""
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
		# Inter-sample purge time. Owned at App level so the Cleaning-mode
		# Purge Time Calibration panel can write a measured value here and
		# Automated mode's Purge time entry picks it up immediately.
		self.purge_time_var = tk.StringVar(value="30.0")
		# Skip flag for the inter-sample purge workflow. Toggled via the
		# Tools → Preferences dialog and persisted as a top-level field
		# in config.json (not under last_used).
		self.skip_intersample_purge_var = tk.BooleanVar(
			value=config_store.load_skip_intersample_purge()
		)
		# Peristaltic pump rate (mL/min) used by all purge-claim waste
		# tracking. Live value -- mid-run edits affect subsequent waste
		# calculations.
		self.peristaltic_rate_var = tk.StringVar(value="100.0")
		# Max waste-bin volume in mL. Live value driving the auto-shutoff
		# and the status-bar fill-level indicator.
		self.max_waste_volume_var = tk.StringVar(value="250.0")

		# Waste-bin estimate. Initialized to 0 on every launch and NOT
		# persisted to disk -- users typically empty the bin between
		# sessions, so a fresh process starts with a fresh counter.
		# Reset() also returns this to 0.
		self.waste_volume_ml = 0.0
		self.waste_warned_80 = False
		self._waste_full = False
		# Counters surfaced in end.json + summary.md.
		self._waste_warnings_fired = 0
		self._waste_shutoffs_fired = 0
		self._waste_resets_during_run = 0
		self._waste_volume_at_run_start = 0.0
		# Real-time waste tracker. Started on relay ON via the
		# PumpController state callback; ticks every 500 ms while ON
		# and writes a final increment on relay OFF. Lives at App
		# level so all pump-on activity (Manual/Cleaning Purge,
		# inter-sample purge, Automated discards/dispenses, syringe
		# priming) feeds a single waste-volume accumulator.
		self._waste_tracker_after = None
		self._waste_tracker_last_mono = None
		self._waste_tracker_claimant = None
		# Threshold dialog state. ``_waste_threshold_dlg`` is the
		# active Toplevel (None when no threshold has tripped);
		# ``_waste_threshold_resume_btn`` is the dialog's Resume
		# button, which we re-enable from reset_waste_counter when
		# the operator clears the counter below 80%.
		self._waste_threshold_dlg = None
		self._waste_threshold_resume_btn = None
		# When True, the active inter-sample purge phase has been halted by
		# a waste-bin threshold trip; the tick callback uses this to stop
		# pumping mid-phase. Reset by waste-reset.
		self._purge_halted_for_waste = False

		# Root layout: 4 rows (header / separator / mode body / status) in
		# a single column that expands with the window. The separator
		# between the mode tabs (header) and the mode body's run-control
		# row keeps the two clickable rows visually distinct.
		self.grid_columnconfigure(0, weight=1)
		self.grid_rowconfigure(2, weight=1)

		self.header = HeaderFrame(self, self)
		self.header.grid(row=0, column=0, sticky="we", padx=4, pady=(4, 0))

		self._mode_separator = ttk.Separator(self, orient="horizontal")
		self._mode_separator.grid(row=1, column=0, sticky="we", padx=4, pady=(6, 4))

		self.automated_frame = AutomatedFrame(self, self)
		self.manual_frame = ManualFrame(self, self)
		self.cleaning_frame = CleaningFrame(self, self)
		self._frames = {
			"Automated": self.automated_frame,
			"Manual": self.manual_frame,
			"Cleaning": self.cleaning_frame,
		}

		self.status_bar = StatusBarFrame(self, self)
		self.status_bar.grid(row=3, column=0, sticky="we")

		# Pump goes through PumpController so every state change updates both
		# the status-bar indicator and the per-frame pump buttons. The state
		# machine (pump_liquid / stop_pump) and the user-click handler
		# (_handle_pump_click) both go through this controller -- never the
		# raw relay backend.
		self.pump_controller = PumpController(backends.relay)
		self.pump_controller.subscribe(self._on_pump_state_change)

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

		# Arrow-key jog shortcuts (Manual mode only). Same self-gating as
		# the space binding -- handler returns immediately when not in
		# Manual mode or when the focused widget is a text entry, so
		# typing in Sample ID etc. still moves the text cursor normally.
		for keysym, axis, sign in (
			("Up",    "y", +1),
			("Down",  "y", -1),
			("Left",  "x", -1),
			("Right", "x", +1),
		):
			self.bind_all(
				f"<KeyPress-{keysym}>",
				lambda e, a=axis, s=sign: self._on_arrow(e, a, s),
			)

		# Seed the run-control button row to its idle state.
		self._update_run_control_buttons()

		# One-time initialization wiggle to seat the lead screws against a
		# known direction of backlash. Lives here (not in a mode's refresh)
		# because the motors themselves persist across mode switches. Tare
		# immediately after so the Manual-mode Position readout starts at
		# exactly (0.000, 0.000) instead of (-0.100, -0.100).
		self.table_motor.move_dist_relative(-0.1)
		self.carriage_motor.move_dist_relative(-0.1)
		self.table_motor.tare()
		self.carriage_motor.tare()

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
		tools.add_separator()
		tools.add_command(label="Preferences…", command=self._show_preferences_dialog)
		menubar.add_cascade(label="Tools", menu=tools)

		help_menu = tk.Menu(menubar, tearoff=False)
		help_menu.add_command(label="About", command=self._show_about_dialog)
		menubar.add_cascade(label="Help", menu=help_menu)

		self.config(menu=menubar)

	def _show_preferences_dialog(self):
		"""Modal preferences dialog. OK persists each checkbox to
		config.json and applies immediately; Cancel discards."""
		dlg = tk.Toplevel(self)
		dlg.title("Preferences")
		dlg.transient(self)
		dlg.resizable(False, False)
		body = tk.Frame(dlg, padx=18, pady=14)
		body.pack(fill=tk.BOTH, expand=True)

		return_var = tk.BooleanVar(value=self.return_to_origin_on_exit)
		tk.Checkbutton(
			body, variable=return_var,
			text="Return needle to origin when closing the application",
		).pack(anchor="w", pady=(0, 8))

		skip_var = tk.BooleanVar(value=self.skip_intersample_purge_var.get())
		tk.Checkbutton(
			body, variable=skip_var,
			text="Skip inter-sample purge",
		).pack(anchor="w", pady=(0, 12))

		tk.Label(body, text="Inter-sample purge protocol:", anchor="w",
			).pack(anchor="w", pady=(0, 2))
		protocol_var = tk.StringVar(value=self.purge_protocol)
		tk.Radiobutton(body, variable=protocol_var, value="basic",
			text="Water only (water → sample)",
		).pack(anchor="w", padx=(16, 0))
		tk.Radiobutton(body, variable=protocol_var, value="decontamination",
			text="Decontamination (water → bleach → water → sample)",
		).pack(anchor="w", padx=(16, 0), pady=(0, 12))

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X)

		def _ok():
			new_return = bool(return_var.get())
			new_skip = bool(skip_var.get())
			new_protocol = protocol_var.get()
			self.return_to_origin_on_exit = new_return
			# Push into the live BooleanVar so the state-machine read
			# (state.skip_intersample_purge at Begin) sees the new value
			# without an app restart.
			self.skip_intersample_purge_var.set(new_skip)
			self.purge_protocol = (
				new_protocol if new_protocol in ("basic", "decontamination")
				else "basic"
			)
			try:
				config_store.save_return_to_origin_on_exit(new_return)
				config_store.save_skip_intersample_purge(new_skip)
				config_store.save_purge_protocol(self.purge_protocol)
			except Exception as exc:
				logger.warning("Could not persist preferences: %s", exc)
			dlg.destroy()

		def _cancel():
			dlg.destroy()

		ttk.Button(btn_row, text="Cancel", command=_cancel).pack(side=tk.RIGHT, padx=4)
		ttk.Button(btn_row, text="OK", command=_ok,
			style="Primary.TButton").pack(side=tk.RIGHT, padx=4)
		dlg.bind("<Return>", lambda _e: _ok())
		dlg.bind("<Escape>", lambda _e: _cancel())

		dlg.update_idletasks()
		x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
		y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
		dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
		dlg.grab_set()

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
		ttk.Button(btn_row, text=action_label, command=_ok).pack(side=tk.LEFT, padx=4)
		ttk.Button(btn_row, text="Cancel", command=_cancel).pack(side=tk.LEFT, padx=4)
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
		the window goes away.

		If ``return_to_origin_on_exit`` is True AND the system is idle
		(no active run, no e-stop, no waste-full lockdown), drive both
		motors back to (0, 0) and re-tare before the window closes so
		the operator finds the needle parked at a known position next
		launch. The move is skipped mid-run so we don't surprise the
		operator's experiment, and skipped when an e-stop / waste-full
		lockdown is active because those signal a hardware issue worth
		human inspection before any further motion.
		"""
		safe_to_home = (
			self.return_to_origin_on_exit
			and self.state.state == "idle"
			and not self._terminated
			and not self._waste_full
			and self.run_logger is None
		)
		if safe_to_home:
			try:
				self.set_status("Returning to origin…")
				self.update_idletasks()
				self.table_motor.move_dist_absolute(0.0)
				self.carriage_motor.move_dist_absolute(0.0)
				self.table_motor.tare()
				self.carriage_motor.tare()
			except Exception as exc:
				logger.warning("Origin-return on close failed: %s", exc)

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
				well_records=self.state.well_records,
				waste_context=self._waste_context(),
				bulk_context=self._bulk_context())
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

		On entering Automated, the well-plate canvas is force-refreshed
		from its own ``status_grid`` / ``well_records`` after the next
		idle so any rendering missed while the frame was hidden (e.g. a
		stray ``<Configure>`` with a tiny canvas size) is recovered.
		Run-control button states are reasserted too.
		"""
		if self._active_frame is not None:
			self._active_frame.grid_remove()
		frame = self._frames[name]
		frame.grid(row=2, column=0, sticky="nsew")
		self._active_frame = frame
		self.mode = name
		self.title(f"autoSIP Controller v{__version__} — {name} Mode")
		self.header.set_mode_label(name)
		self.status_bar.set_mode(name)
		self.status_bar.set_terminate_visible(name == "Automated")
		frame.refresh()
		if name == "Automated":
			logger.debug(
				"mode-switch ENTER Automated: well_records=%d phase=%s state=%s",
				len(self.state.well_records), self.state.phase, self.state.state,
			)
			# Wait for the frame to actually be laid out before forcing
			# the canvas redraw, otherwise winfo_width is still zero.
			self.after_idle(self.automated_frame.progress.refresh_from_state)
			self.after_idle(self._update_run_control_buttons)
		logger.debug("Switched to %s mode", name)

	def request_mode_change(self, name):
		"""Switch to mode ``name``. Mode switching is always allowed --
		mid-run Manual/Cleaning controls that could interfere with the
		Automated state machine are disabled by ``_apply_run_active_lock``,
		and the run keeps ticking in the background. No-op when ``name``
		matches the current mode.
		"""
		if name == self.mode:
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
		self._set_phase("idle")
		s.is_paused = False

		# Pump off + claim cleared, motors released
		self.pump_controller.release()
		self.table_motor.release()
		self.carriage_motor.release()

		# Freeze the Elapsed clock at the terminate point so the operator
		# sees the active-fractionation time accrued before the e-stop.
		self.automated_frame.progress.pause_elapsed()

		# E-stop may have occurred because of a hardware swap or
		# mis-plumbed pump; clear the per-pump confirmation latches so
		# the next user-initiated activation re-prompts the operator
		# to verify which pump is wired up.
		self.fractionate_confirmed_this_session = False
		self.purge_confirmed_this_session = False

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
				well_records=self.state.well_records,
				waste_context=self._waste_context(),
				bulk_context=self._bulk_context())
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
			"Run terminated — click Return to Origin to re-enable controls.",
		)
		self._terminated = True
		self._update_run_control_buttons()
		logger.warning("Run terminated")
		# Terminate implicitly exits bulk mode -- the remaining samples
		# need fresh consideration after whatever caused the e-stop.
		if self.bulk_mode_active:
			self._deactivate_bulk_mode()

	# -- Bulk Sample Submission ----------------------------------------

	def generate_bulk_template(self):
		"""Save-file dialog → write the standard template to the chosen
		path. Shows a confirmation messagebox on success."""
		path = filedialog.asksaveasfilename(
			parent=self,
			title="Save Bulk Sample Submission template",
			defaultextension=".csv",
			initialfile="autosip_bulk_template.csv",
			filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
		)
		if not path:
			return
		try:
			write_bulk_template(path)
		except OSError as exc:
			messagebox.showerror(
				"Could not write template",
				f"Failed to save template to {path}:\n\n{exc}",
				parent=self,
			)
			return
		messagebox.showinfo(
			"Template saved",
			f"Template saved to {path}.\n\n"
			"Open it in a spreadsheet program (Excel, LibreOffice, "
			"Google Sheets), fill out one row per sample, save as CSV, "
			"then click Import Submission.",
			parent=self,
		)

	def import_bulk_submission(self):
		"""Open-file dialog → parse + validate → activate bulk mode on
		success. Validation failures pop a messagebox listing every
		problem row by row; bulk mode does NOT activate in that case."""
		path = filedialog.askopenfilename(
			parent=self,
			title="Import Bulk Sample Submission",
			filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
		)
		if not path:
			return
		af = self.automated_frame
		gui_defaults = {
			"number_of_fractions": af.n_fractions_te.get(),
			"discard_fractions": af.discard_te.get(),
			"plate_id": af.plate_id_te.get(),
			"volume_per_well_ml": af.vol_text_entry.get(),
		}
		try:
			samples, errors = load_bulk_submission(path, gui_defaults=gui_defaults)
		except (OSError, UnicodeDecodeError) as exc:
			messagebox.showerror(
				"Could not read CSV",
				f"Failed to read {path}:\n\n{exc}",
				parent=self,
			)
			return
		if errors:
			lines = [msg for _, msg in errors]
			messagebox.showerror(
				"Bulk submission import failed",
				"Bulk submission import failed:\n\n" + "\n".join(lines),
				parent=self,
			)
			return
		# Success path.
		self.bulk_samples = samples
		self.bulk_current_index = 0
		self.bulk_source_path = path
		self._activate_bulk_mode()
		messagebox.showinfo(
			"Bulk submission loaded",
			f"Loaded {len(samples)} samples from {path}. "
			"Bulk mode is now active.",
			parent=self,
		)

	def exit_bulk_mode(self):
		"""Operator-initiated bulk-mode exit. Confirmation guard then
		clears state + re-enables Run Parameters."""
		if not self.bulk_mode_active:
			return
		if not messagebox.askyesno(
			"Exit bulk mode?",
			"Exit bulk mode? Run Parameters will become editable again. "
			"Any unprocessed samples in the loaded submission will be "
			"discarded.",
			parent=self,
		):
			return
		self._deactivate_bulk_mode()

	def _activate_bulk_mode(self):
		"""Populate Run Parameters from sample 0, disable every Run
		Parameters entry (including Project — bulk mode owns the whole
		Run Parameters block), retitle the frame, and refresh the bulk
		panel UI."""
		af = self.automated_frame
		self._apply_bulk_sample_to_fields(self.bulk_samples[0])
		for te in (af.project_te, af.sample_id_te, af.plate_id_te,
				af.n_fractions_te, af.discard_te, af.vol_text_entry):
			te.entry.configure(state="disabled")
		af.runp_frame.configure(text="Run Parameters (bulk mode)")
		self._refresh_bulk_panel()

	def _deactivate_bulk_mode(self):
		"""Inverse of _activate_bulk_mode. Always safe to call (a no-op
		if bulk wasn't active)."""
		af = self.automated_frame
		self.bulk_samples = []
		self.bulk_current_index = 0
		self.bulk_source_path = ""
		for te in (af.project_te, af.sample_id_te, af.plate_id_te,
				af.n_fractions_te, af.discard_te, af.vol_text_entry):
			te.entry.configure(state="normal")
		af.runp_frame.configure(text="Run Parameters")
		self._refresh_bulk_panel()

	def _apply_bulk_sample_to_fields(self, sample):
		"""Write one bulk-samples entry into the Run Parameters Tk
		fields. Blank optional spreadsheet values leave the current GUI
		value alone -- the operator's pre-import state is the fallback."""
		af = self.automated_frame
		# sample_id is required; always set.
		af.sample_id_te.set(sample["sample_id"])
		if sample.get("plate_id"):
			af.plate_id_te.set(sample["plate_id"])
		if sample.get("number_of_fractions") is not None:
			af.n_fractions_te.set(str(sample["number_of_fractions"]))
		if sample.get("discard_fractions") is not None:
			af.discard_te.set(str(sample["discard_fractions"]))
		if sample.get("volume_per_well_ml") is not None:
			af.vol_text_entry.set(f"{sample['volume_per_well_ml']:g}")

	def _refresh_bulk_panel(self):
		"""Sync the Bulk Sample Submission LabelFrame to the current
		bulk_* state. Toggles which buttons are visible and updates
		the status + source lines."""
		af = self.automated_frame
		if self.bulk_mode_active:
			n = len(self.bulk_samples)
			i = self.bulk_current_index + 1
			af.bulk_status_var.set(
				f"Status: Bulk mode — {n} samples loaded, sample {i} of {n}."
			)
			af.bulk_source_var.set(f"Loaded from: {self.bulk_source_path}")
			af.bulk_source_lbl.grid()
			# Hide template/import; show exit.
			af.bulk_template_btn.pack_forget()
			af.bulk_import_btn.pack_forget()
			af.bulk_exit_btn.pack(side=tk.LEFT, padx=(0, 4))
		else:
			af.bulk_status_var.set("Status: No bulk submission active.")
			af.bulk_source_var.set("")
			af.bulk_source_lbl.grid_remove()
			af.bulk_exit_btn.pack_forget()
			af.bulk_template_btn.pack(side=tk.LEFT, padx=(0, 4))
			af.bulk_import_btn.pack(side=tk.LEFT, padx=4)

	def return_to_origin(self):
		"""Move motors to (0, 0) and tare the angle counters.

		Three roles in one button:
		  (1) Idle-time recentering equivalent to Manual mode's Home.
		  (2) Mid-pause recalibration: captures the current motor
		      position on the FIRST click in this pause so the matching
		      Resume can drive back to it and pop a Confirm Calibration
		      dialog.
		  (3) Post-terminate recovery: clears the e-stopped lockdown
		      so the operator can start a fresh run.
		"""
		s = self.state
		# Pause-time capture (first click only -- subsequent clicks in
		# the same pause re-issue the move+tare without overwriting
		# the captured reference).
		if s.is_paused and not s.origin_returned_during_pause:
			s.paused_table_cm = (
				self.table_motor.get_angle() * self.table_motor.cm_per_deg
			)
			s.paused_carriage_cm = (
				self.carriage_motor.get_angle() * self.carriage_motor.cm_per_deg
			)
			s.origin_returned_during_pause = True
			logger.info(
				"Captured pause position for mid-run recalibration: "
				"(%.2f cm, %.2f cm)",
				s.paused_table_cm, s.paused_carriage_cm,
			)
		# Same physical action as Manual Home: drive to (0, 0) + tare.
		self.carriage_return()
		self.table_motor.tare()
		self.carriage_motor.tare()
		# Post-terminate recovery path.
		if self._terminated:
			self._set_controls_enabled(True)
			self._terminated = False
		if s.is_paused:
			self.set_status(
				"Returned to origin. Manually re-park the carriage "
				"against the upper-left limit, then click Resume."
			)
		else:
			self.set_status("Returned to origin.")
		self._update_run_control_buttons()

	def return_to_start_well(self):
		"""Move the needle to the entered Starting well position coords.

		Reads the LIVE values from the Starting well position entries
		so the button works pre-run too. Validates first; out-of-range
		input is surfaced inline and the move is refused. Disabled
		mid-run (the button-state matrix prevents the click, but the
		validation guard above is also a defensive backstop).
		"""
		af = self.automated_frame
		t_ok, t_val = validation.table_pos(af.table_te.get())
		c_ok, c_val = validation.carriage_pos(af.carriage_te.get())
		(af.table_te.clear_error if t_ok else lambda: af.table_te.show_error(t_val))()
		(af.carriage_te.clear_error if c_ok else lambda: af.carriage_te.show_error(c_val))()
		if not (t_ok and c_ok):
			return
		self.move_to_positions(table_dist=t_val, carriage_dist=c_val)
		self.set_status(
			f"Moved to starting well position "
			f"({t_val:.2f} cm, {c_val:.2f} cm)."
		)

	# -- Run-control button state machine -------------------------------

	def _classify_ui_state(self):
		"""Map (state.state, is_paused, _terminated, _waste_full) to a
		single UI bucket.

		idle / running / paused_manual / paused_total / paused_plate_full /
		estopped / waste_full
		"""
		if self._terminated:
			return "estopped"
		if self._waste_full:
			return "waste_full"
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
		"""Sync the six run-control buttons in AutomatedFrame to the
		current state machine state. Called at every state transition
		so the user sees an immediate response.

		Two recovery buttons replace the previous single Return:
		  - origin (Return to Origin): enabled at idle AND in every
		    paused/halted state so it can serve as the mid-pause
		    recalibration entry point. Disabled only while a run is
		    actively running (so the operator can't accidentally
		    interrupt the snake-path).
		  - start_well (Return to Start Well): enabled at idle only.
		    Disabled mid-run -- moving to A1 mid-run would lose the
		    operator's place.

		The Pause button's color is varied by swapping its ttk style
		(PauseRunning.TButton / PausePaused.TButton / TButton); the
		typography and border are inherited from TButton so all role
		styles read uniformly with the other run-control buttons.
		"""
		af = self.automated_frame
		s = self.state
		bucket = self._classify_ui_state()

		# Defaults; per-bucket overrides follow.
		origin_state = tk.NORMAL
		start_well_state = tk.NORMAL
		pause_state = tk.DISABLED
		pause_text = "Pause"
		pause_style = "TButton"
		cont_state = tk.DISABLED
		cont_plate_state = tk.DISABLED
		end_state = tk.DISABLED

		if bucket == "idle":
			pass  # Default values above.
		elif bucket == "running":
			origin_state = tk.DISABLED
			start_well_state = tk.DISABLED
			pause_state = tk.NORMAL
			pause_text = "Pause"
			pause_style = "PauseRunning.TButton"
			end_state = tk.NORMAL
		elif bucket == "paused_manual":
			origin_state = tk.NORMAL  # mid-pause recalibration path
			start_well_state = tk.DISABLED
			pause_state = tk.NORMAL
			pause_text = "Resume"
			pause_style = "PausePaused.TButton"
			end_state = tk.NORMAL
		elif bucket == "paused_total":
			# Sample complete, plate not full -- next action is Continue
			# to Next Sample (or End Run).
			origin_state = tk.NORMAL
			start_well_state = tk.DISABLED
			pause_state = tk.DISABLED
			pause_text = "Paused"
			cont_state = tk.NORMAL
			end_state = tk.NORMAL
		elif bucket == "paused_plate_full":
			# Plate full -- Continue to Next Plate is the primary action.
			# Continue to Next Sample becomes available only if the sample
			# ALSO wrapped up on this well; otherwise it stays disabled
			# until after the plate swap resolves.
			origin_state = tk.NORMAL
			start_well_state = tk.DISABLED
			pause_state = tk.DISABLED
			pause_text = "Paused"
			cont_plate_state = tk.NORMAL
			cont_state = tk.NORMAL if s.plate_full_with_sample_complete else tk.DISABLED
			end_state = tk.NORMAL
		elif bucket == "waste_full":
			# Waste-bin auto-shutoff: only End Run + Reset (in status
			# bar) work. Return-to-Origin stays available so an
			# operator who's emptied the bin AND wants to recalibrate
			# can do that in one halted state.
			origin_state = tk.NORMAL
			start_well_state = tk.DISABLED
			pause_state = tk.DISABLED
			pause_text = "Paused"
			end_state = tk.NORMAL
		elif bucket == "estopped":
			# After Terminate Run, clicking Return to Origin is the
			# recovery path: it tares + clears _terminated +
			# re-enables motion controls.
			origin_state = tk.NORMAL
			start_well_state = tk.DISABLED
			pause_state = tk.DISABLED

		af.return_origin_btn["state"] = origin_state
		af.return_well_btn["state"] = start_well_state
		af.pause_btn["state"] = pause_state
		af.pause_btn["text"] = pause_text
		af.pause_btn.configure(style=pause_style)
		af.continue_btn["state"] = cont_state
		af.continue_plate_btn["state"] = cont_plate_state
		af.end_run_btn["state"] = end_state

		# Begin Fractionation is the explicit "start a new run from
		# idle" entry point. Disable it whenever a run is in flight
		# (active dispense, drip wait, move, both auto-pause states,
		# operator-paused, waste-bin lockdown, e-stopped, inter-sample
		# purge modal open). Advancing or ending an existing run is
		# unambiguously the Run Controls row's job.
		af.begin_btn["state"] = tk.NORMAL if s.phase == "idle" else tk.DISABLED

	def _set_controls_enabled(self, enabled):
		"""Toggle every frame's dangerous buttons in one call."""
		for frame in self._frames.values():
			if hasattr(frame, "set_controls_enabled"):
				frame.set_controls_enabled(enabled)

	# -- Status bar helper ------------------------------------------------

	def _set_phase(self, value):
		"""Single entry point for ``state.phase`` mutations. Calls
		``_apply_run_active_lock`` after assigning so the Manual /
		Cleaning controls + banner re-evaluate themselves on every
		phase transition (idle → discard → collect → idle, plus the
		auto-pause / plate-swap flows). Cheaper than wiring a
		__setattr__ hook on the FractionatorState dataclass.
		"""
		self.state.phase = value
		self._apply_run_active_lock()

	def _automated_run_active(self):
		"""True while the fractionation state machine is past its idle
		state -- including operator-paused and auto-pause states.
		Manual / Cleaning controls that could interfere with the active
		run gate themselves on this helper via ``_apply_run_active_lock``.
		"""
		return self.state.phase != "idle"

	def _apply_run_active_lock(self):
		"""Push the current ``_automated_run_active`` value into the
		Manual and Cleaning frames so their controls re-evaluate their
		enabled state and the active-run banner shows/hides. Called
		from every state.phase mutation."""
		active = self._automated_run_active()
		mf = getattr(self, "manual_frame", None)
		if mf is not None and hasattr(mf, "set_run_active_lock"):
			mf.set_run_active_lock(active)
		cf = getattr(self, "cleaning_frame", None)
		if cf is not None and hasattr(cf, "set_run_active_lock"):
			cf.set_run_active_lock(active)

	def set_status(self, text):
		"""Route status text. The plate-area canvas header carries the
		detailed phase status while a run is active so the operator
		reads it next to the plate; the system status bar's middle
		area stays blank during runs and reads "System idle." when
		truly idle. Outside Automated mode there is no canvas header
		to mirror into, so the status bar takes the message directly.
		"""
		af = getattr(self, "automated_frame", None)
		idle = (self.state.state == "idle" and not self._terminated)
		if af is not None and not idle:
			af.progress.current_lbl["text"] = text
			self.status_bar.set_text("")
		else:
			if af is not None:
				af.progress.current_lbl["text"] = ""
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
		# Relay-activation confirmation: prompt once per pump per session
		# regardless of mode. Cancelling the dialog leaves the flag False
		# so the next attempt re-prompts. Terminate Run also resets the
		# flag (see terminate_run) since hardware may have been swapped.
		confirmed_attr = (
			"fractionate_confirmed_this_session" if name == "fractionate"
			else "purge_confirmed_this_session"
		)
		if not getattr(self, confirmed_attr):
			if not messagebox.askyesno(
				"Activate pump",
				self._pump_confirm_text(name),
				parent=parent,
			):
				return
			setattr(self, confirmed_attr, True)

		pc.claim_for(name)
		pc.set_relay(True)
		if self.mode == "Cleaning" and name == "purge":
			self.set_status("System purging.")

	def _on_space(self, event):
		"""Space-bar shortcut: toggle a pump in Manual or Cleaning mode.

		Manual mode toggles whichever pump was used most recently
		(tracked via ``last_pump_used``). Cleaning mode always toggles
		Purge -- it's the only pump button in that mode, so no
		last-used tracking is needed.

		Self-gates on mode (no-op in Automated) and on the type of
		widget currently holding keyboard focus -- if the user is
		typing into an Entry or Text, space is a literal character
		there and we must NOT consume it. Routes through
		``_handle_pump_click`` so the OFF→ON confirmation dialog still
		fires (Cleaning mode triggers the peristaltic-pump-connected
		check just like a button click would). Returns ``"break"`` so
		focus traversal / button activation on the same keypress
		doesn't double-fire.
		"""
		focused = self.focus_get()
		if isinstance(focused, (tk.Entry, tk.Text)):
			return None
		# Hard-block while an Automated run is in flight (or paused) so
		# the Space shortcut can't interfere with the running state
		# machine even when the Manual/Cleaning frame is visible.
		if self._automated_run_active():
			return "break"
		if self.mode == "Manual":
			self._handle_pump_click(self.last_pump_used, parent=self.manual_frame)
			return "break"
		if self.mode == "Cleaning":
			self._handle_pump_click("purge", parent=self.cleaning_frame)
			return "break"
		return None

	def _on_arrow(self, event, axis, sign):
		"""Arrow-key jog shortcut: routes to ManualFrame._jog so soft
		limits, position readout, and motor entry points stay in one
		place. Same self-gating as ``_on_space`` -- skip when not in
		Manual mode and skip when the focused widget is a text entry
		(so the cursor moves normally inside Sample ID etc.).
		Returns ``"break"`` so ttk's default arrow-key focus traversal
		doesn't also fire."""
		if self.mode != "Manual":
			return None
		focused = self.focus_get()
		if isinstance(focused, (tk.Entry, tk.Text)):
			return None
		# Disabled while an Automated run is in flight -- the visible
		# jog buttons are also greyed out by _apply_run_active_lock,
		# but defense in depth.
		if self._automated_run_active():
			return "break"
		self.manual_frame._jog(axis, sign)
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
		"""PumpController callback: sync the status bar + per-frame buttons,
		and drive the real-time waste-volume tracker. The tracker starts
		on every relay-ON transition (regardless of claimant) and stops
		on every relay-OFF transition with a final per-tick increment.

		Tracking every claimant -- including ``fractionate`` -- means
		Automated-mode discards + syringe priming are accounted for via
		the same code path that handles Manual/Cleaning Purge. Per-well
		plate dispenses also count toward the running total even though
		physically they land on the plate; operators set
		``Max waste bin volume`` to a value that accommodates that
		bookkeeping margin (or leave the default 250 mL).
		"""
		self.status_bar.set_pump_state(claimant, relay_on)
		self._refresh_pump_buttons()

		if relay_on and claimant is not None:
			self._waste_tracker_start(claimant)
		else:
			self._waste_tracker_stop()

	def _waste_tracker_start(self, claimant):
		"""Begin the periodic waste-volume tracker on relay ON. Captures
		the claimant so the per-tick rate uses the right pump. No-op if
		a tracker is already running (the new claimant overrides; the
		PumpController fires OFF/ON transitions cleanly between claim
		swaps via release()+claim_for())."""
		# Cancel any in-flight tick before starting fresh.
		if self._waste_tracker_after is not None:
			try:
				self.after_cancel(self._waste_tracker_after)
			except Exception:
				pass
			self._waste_tracker_after = None
		self._waste_tracker_claimant = claimant
		self._waste_tracker_last_mono = monotonic()
		self._waste_tracker_after = self.after(500, self._waste_tracker_tick)

	def _waste_tracker_tick(self):
		"""Periodic increment while the relay is ON. Adds the volume
		pumped since the last tick to ``waste_volume_ml``, refreshes
		the flask UI, and checks the 80% / 100% thresholds."""
		self._waste_tracker_after = None
		if self._waste_tracker_last_mono is None:
			return
		now = monotonic()
		delta_s = now - self._waste_tracker_last_mono
		self._waste_tracker_last_mono = now
		rate_ml_per_s = self._waste_tracker_rate_ml_per_s()
		self._add_waste(delta_s * rate_ml_per_s)
		# Reschedule unless a threshold trip cancelled us (which sets
		# _waste_tracker_last_mono = None via _waste_tracker_stop()).
		if self._waste_tracker_last_mono is not None:
			self._waste_tracker_after = self.after(
				500, self._waste_tracker_tick)

	def _waste_tracker_stop(self):
		"""End the tracker on relay OFF. One final per-tick increment
		(from the last tick to now) is added before the after() task
		is cancelled, so the contribution from the final sub-second
		of pump-on time is not lost."""
		if self._waste_tracker_after is not None:
			try:
				self.after_cancel(self._waste_tracker_after)
			except Exception:
				pass
			self._waste_tracker_after = None
		if self._waste_tracker_last_mono is None:
			return
		now = monotonic()
		delta_s = now - self._waste_tracker_last_mono
		rate_ml_per_s = self._waste_tracker_rate_ml_per_s()
		self._waste_tracker_last_mono = None
		if delta_s > 0 and rate_ml_per_s > 0:
			self._add_waste(delta_s * rate_ml_per_s)

	def _waste_tracker_rate_ml_per_s(self):
		"""Per-claimant pump rate in mL/sec for the real-time tracker.
		Falls back to 0 when no claimant is set."""
		if self._waste_tracker_claimant == "fractionate":
			return self._live_pump_rate_ml_per_min() / 60.0
		if self._waste_tracker_claimant == "purge":
			return self._live_peristaltic_rate() / 60.0
		return 0.0

	def _live_pump_rate_ml_per_min(self):
		"""Read the syringe pump rate (Fractionation Pump Parameters →
		Pump rate, mL/hr) and convert to mL/min so waste-volume math
		mirrors the peristaltic path. Falls back to 60 mL/hr (= 1 mL/min)
		when the field is blank or malformed."""
		raw = self.automated_frame.pump_rate_text_entry.get()
		try:
			val_hr = float(raw)
		except (TypeError, ValueError):
			val_hr = 60.0
		if val_hr <= 0:
			val_hr = 60.0
		return val_hr / 60.0

	def _live_peristaltic_rate(self):
		"""Read the peristaltic rate from the App-level StringVar with a
		fallback to the FractionatorState snapshot (set at run start)
		and finally to a safe default. Used by waste-tracking callsites
		that fire outside an active run."""
		raw = self.peristaltic_rate_var.get()
		try:
			val = float(raw)
		except (TypeError, ValueError):
			val = self.state.peristaltic_rate_ml_per_min or 100.0
		if val <= 0:
			val = 100.0
		return val

	def _live_max_waste_volume(self):
		"""Same idea for max bin capacity."""
		raw = self.max_waste_volume_var.get()
		try:
			val = float(raw)
		except (TypeError, ValueError):
			val = self.state.max_waste_volume_ml or 250.0
		if val <= 0:
			val = 250.0
		return val

	def _add_waste(self, ml):
		"""Increment the running waste estimate by ``ml`` and fire the
		80% auto-pause / 100% hard-stop transitions if thresholds were
		crossed during this update. ``ml`` may be 0 or slightly
		negative due to floating-point drift; clamp at 0 silently.
		"""
		if ml <= 0:
			# Refresh the status-bar indicator even on a zero update --
			# the max-waste-volume field may have been edited and the
			# percentage display needs to reflect that.
			self._refresh_waste_indicator()
			return
		self.waste_volume_ml += ml
		max_v = self._live_max_waste_volume()
		self._refresh_waste_indicator()
		# Order matters: check 100% BEFORE 80% so a single fast tick that
		# crosses both thresholds settles on the hard-stop variant.
		if self.waste_volume_ml >= max_v and not self._waste_full:
			self._trigger_waste_threshold(max_v, severity="100%")
		elif (not self.waste_warned_80
				and self.waste_volume_ml >= 0.80 * max_v):
			self._trigger_waste_threshold(max_v, severity="80%")

	def _trigger_waste_threshold(self, max_v, *, severity):
		"""Halt the pump, pause any active Automated run, log the event,
		and surface the threshold dialog. ``severity`` is ``"80%"`` or
		``"100%"``; the dialog title and the run-logger kind differ
		between the two but the rest of the path is identical.
		"""
		# Set the threshold flag BEFORE _waste_tracker_stop so its final
		# delta-tick doesn't re-enter _add_waste and re-fire the same
		# threshold trigger.
		if severity == "80%":
			self.waste_warned_80 = True
		else:
			self._waste_full = True
		self._waste_tracker_stop()
		self.pump_controller.set_relay(False)
		# Cancel any pending state-machine after() callback so the
		# Automated run halts cleanly.
		if self.state.taskId is not None:
			try:
				self.after_cancel(self.state.taskId)
			except Exception:
				pass
			self.state.taskId = None
		# Mark the run as paused if it was actively dispensing. The
		# existing Resume path (toggle_pause) re-arms the after() chain
		# from the same phase.
		if self.state.state in ("pump", "wait", "move"):
			self.state.is_paused = True
		# Signal any in-flight inter-sample purge phase to halt its tick.
		self._purge_halted_for_waste = True
		# Counter bookkeeping (the boolean flag was set above so the
		# tracker's final delta-tick can't recurse into this method).
		if severity == "80%":
			self._waste_warnings_fired += 1
			counter = self._waste_warnings_fired
			kind = "waste_autopause"
		else:
			self._waste_shutoffs_fired += 1
			counter = self._waste_shutoffs_fired
			kind = "waste_hardstop"
		if self.run_logger is not None:
			try:
				self.run_logger.waste_event(kind, counter)
			except Exception as exc:
				logger.warning("Failed to log %s row: %s", kind, exc)
		self._update_run_control_buttons()
		self.set_status(
			f"Waste bin at {severity} — pump auto-paused. "
			"Empty container and click Reset to resume."
		)
		self._show_waste_threshold_dialog(severity, max_v)

	def _show_waste_threshold_dialog(self, severity, max_v):
		"""Non-modal threshold dialog. Reset / Resume / End Operation.

		Resume is disabled while ``waste_volume_ml >= 0.80 × max``;
		``reset_waste_counter`` re-enables it via the cached button
		reference. The dialog uses no ``grab_set`` so the status-bar
		Reset button stays clickable while the dialog is open.
		"""
		# A previous threshold dialog may still be open if the operator
		# never acted on it -- swap it out so we don't pile up Toplevels.
		if self._waste_threshold_dlg is not None:
			try:
				self._waste_threshold_dlg.destroy()
			except Exception:
				pass
		dlg = tk.Toplevel(self)
		dlg.transient(self)
		dlg.resizable(False, False)
		if severity == "80%":
			dlg.title("⚠ Waste Bin at 80%")
			intro_extra = "Pump has been paused automatically."
		else:
			dlg.title("⚠ Waste Bin at 100% — Hard Stop")
			intro_extra = (
				"Pump halted by the 100% failsafe. The 80% auto-pause "
				"either did not fire or was overridden."
			)

		body = tk.Frame(dlg, padx=18, pady=14)
		body.pack(fill=tk.BOTH, expand=True)
		pct = self.waste_volume_ml / max_v if max_v else 0.0
		tk.Label(body, anchor="w", justify="left", wraplength=440, text=(
			f"Waste estimate: {self.waste_volume_ml:.0f} / "
			f"{max_v:.0f} mL ({pct:.0%})\n"
			f"{intro_extra}\n\n"
			"Empty the waste container, then click Reset. After reset, "
			"click Resume to continue the paused operation."
		)).pack(anchor="w", pady=(0, 12))

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X)

		def _on_reset():
			# Dialog Reset doesn't re-prompt -- the operator is already
			# in a Reset-confirmation context.
			self._perform_waste_reset(confirm=False)
		def _on_resume():
			self._waste_threshold_resume(dlg)
		def _on_end():
			self._waste_threshold_end_operation(dlg)
		def _on_close():
			# Closing via X behaves like End Operation -- the
			# operation cannot be silently resumed.
			self._waste_threshold_end_operation(dlg)

		reset_btn = ttk.Button(btn_row, text="Reset",
			command=_on_reset, style="Primary.TButton")
		reset_btn.pack(side=tk.LEFT, padx=4)
		resume_btn = ttk.Button(btn_row, text="Resume", command=_on_resume)
		resume_btn.pack(side=tk.LEFT, padx=4)
		ttk.Button(btn_row, text="End Operation",
			command=_on_end, style="Danger.TButton").pack(side=tk.LEFT, padx=4)

		# Resume is gated until the operator brings the counter below 80%.
		max_now = self._live_max_waste_volume()
		if self.waste_volume_ml >= 0.80 * max_now:
			resume_btn.state(["disabled"])

		dlg.protocol("WM_DELETE_WINDOW", _on_close)
		dlg.update_idletasks()
		self._center_over_main(dlg)
		# No grab_set -- status-bar Reset must stay clickable.
		self._waste_threshold_dlg = dlg
		self._waste_threshold_resume_btn = resume_btn

	def _waste_threshold_resume(self, dlg):
		"""Resume the operation that was halted by the threshold pause.
		The exact restart action depends on what was running:
		  - Automated run mid-cycle: route through toggle_pause (which
		    re-arms the state machine's after() chain).
		  - Otherwise (Manual/Cleaning Purge, inter-sample purge modal):
		    turn the relay back on; operator-visible state catches up
		    via the PumpController state callback.
		"""
		dlg.destroy()
		self._waste_threshold_dlg = None
		self._waste_threshold_resume_btn = None
		s = self.state
		if s.state in ("pump", "wait", "move", "discard") and s.is_paused:
			self.toggle_pause()
			return
		# Toggle-context restart: just turn the relay back on. The
		# operator-visible labels (Manual Purge button, inter-sample
		# purge modal) sync via the PumpController callback.
		if self.pump_controller.claimant is not None:
			self.pump_controller.set_relay(True)

	def _waste_threshold_end_operation(self, dlg):
		"""Close the dialog and abort the operation. Pump stays off.
		Automated runs land in the auto-paused state (End Run is the
		operator's exit); Cleaning/Manual purges just dismiss.
		"""
		dlg.destroy()
		self._waste_threshold_dlg = None
		self._waste_threshold_resume_btn = None
		# Ensure pump is OFF (defensive -- it already is, but make the
		# end-of-operation contract explicit).
		self.pump_controller.set_relay(False)

	def reset_waste_counter(self):
		"""Reset workflow triggered by the status-bar button. Confirms
		with the operator, then delegates to ``_perform_waste_reset``."""
		if not messagebox.askyesno(
			"Reset waste bin counter",
			"Reset waste bin counter to 0 mL?\n\n"
			"Confirm that you have emptied the waste container.",
			parent=self,
		):
			return
		self._perform_waste_reset(confirm=False)

	def _perform_waste_reset(self, *, confirm=True):
		"""Shared Reset implementation used by the status-bar button
		and the threshold dialog. ``confirm=False`` skips the
		askyesno prompt (the dialog's Reset is already in a
		confirmation context).
		"""
		if confirm and not messagebox.askyesno(
			"Reset waste bin counter",
			"Reset waste bin counter to 0 mL?\n\n"
			"Confirm that you have emptied the waste container.",
			parent=self,
		):
			return
		self.waste_volume_ml = 0.0
		self.waste_warned_80 = False
		# Only count resets that happen during a run so end.json reports
		# a sensible "Resets during run" tally.
		if self.run_logger is not None:
			self._waste_resets_during_run += 1
			try:
				self.run_logger.waste_event(
					"waste_reset", self._waste_resets_during_run,
				)
			except Exception as exc:
				logger.warning("Failed to log waste_reset row: %s", exc)
		if self._waste_full:
			self._waste_full = False
			self._purge_halted_for_waste = False
			# Re-enable Cleaning controls. (Run-control buttons will
			# re-classify naturally via _update_run_control_buttons.)
			cf = getattr(self, "cleaning_frame", None)
			if cf is not None:
				try:
					cf.cal_start_btn.state(["!disabled"])
					cf.move_btn.state(["!disabled"])
					cf._cal_save_button_sync()
				except Exception:
					pass
			self._update_run_control_buttons()
			self.set_status("Waste bin reset. Resume your run or End Run.")
		# Also clear the halt flag if only the 80% pause had fired.
		self._purge_halted_for_waste = False
		# Re-enable the threshold dialog's Resume button if it's open
		# and the counter is now below the 80% gate.
		if self._waste_threshold_resume_btn is not None:
			try:
				max_v = self._live_max_waste_volume()
				if self.waste_volume_ml < 0.80 * max_v:
					self._waste_threshold_resume_btn.state(["!disabled"])
			except Exception:
				pass
		self._refresh_waste_indicator()

	def _refresh_waste_indicator(self):
		"""Tell the status bar to repaint the flask + readout from the
		current waste_volume_ml / max_waste_volume_ml. Safe to call
		before the status bar is fully constructed (no-op in that case)."""
		if hasattr(self, "status_bar") and hasattr(self.status_bar, "set_waste_state"):
			self.status_bar.set_waste_state(
				self.waste_volume_ml, self._live_max_waste_volume(),
			)

	def _bulk_context(self):
		"""Dict passed to ``RunLogger.end`` so summary.md's Bulk
		submission section reflects the spreadsheet source + the
		as-run sequence (with edit markers). Returns None when no
		bulk submission is loaded -- the logger then omits the
		section entirely.
		"""
		if not self.bulk_mode_active and not self.bulk_source_path:
			return None
		# bulk_current_index is incremented to 1 after sample-1 completes
		# (in _auto_pause_total_reached), so it acts as the count of
		# completed samples at End Run time. Slice up to (but not
		# including) that index to get the as-run sequence.
		# If End Run is clicked mid-sample-1, bulk_current_index is
		# still 0 and we report an empty sequence (no samples completed
		# fractionation).
		completed = min(self.bulk_current_index, len(self.bulk_samples))
		sequence = []
		for s in self.bulk_samples[:completed]:
			label = s.get("sample_id", "")
			if s.get("edited"):
				label += "b"
			sequence.append(label)
		return {
			"source_path": self.bulk_source_path,
			"total_samples": len(self.bulk_samples),
			"sample_sequence": sequence,
		}

	def _waste_context(self):
		"""Dict passed to ``RunLogger.end`` so end.json + summary.md get
		the waste-bin bookkeeping for the run."""
		return {
			"waste_volume_ml_at_run_start": self._waste_volume_at_run_start,
			"waste_volume_ml_at_run_end": self.waste_volume_ml,
			"max_waste_volume_ml": self._live_max_waste_volume(),
			"waste_warnings_fired": self._waste_warnings_fired,
			"waste_shutoffs_fired": self._waste_shutoffs_fired,
			"waste_resets_during_run": self._waste_resets_during_run,
		}

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
			self.automated_frame.progress.pause_elapsed()
			self.set_status("Fractionation paused...")
			self._update_run_control_buttons()
			return

		# --- Resuming branch ---
		# If the operator clicked Return to Origin during this pause to
		# recalibrate against the upper-left mechanical limit, drive the
		# needle back to the captured position FIRST and pop a Confirm
		# Calibration dialog before re-arming the state machine.
		s = self.state
		if s.origin_returned_during_pause:
			self.set_status("Returning to paused position…")
			self.move_to_positions(
				table_dist=s.paused_table_cm,
				carriage_dist=s.paused_carriage_cm,
			)
			confirmed = self._show_calibration_confirm_dialog(
				s.paused_table_cm, s.paused_carriage_cm,
			)
			if not confirmed:
				# Cancel: leave the flag True and the run paused so the
				# operator can re-park and re-attempt without losing
				# their place. The needle stays at the captured position
				# (the operator is now in "verify the rig" mode).
				s.is_paused = True
				self.set_status(
					"Calibration not confirmed. Run remains paused."
				)
				self._update_run_control_buttons()
				return
			# Confirmed -- clear the flag so a subsequent ordinary
			# Pause+Resume doesn't re-fire the dialog.
			s.origin_returned_during_pause = False

		self.set_status("Fractionation in progress...")
		self.automated_frame.progress.resume_elapsed()

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

	# -- Dialog helpers ---------------------------------------------------

	def _center_over_main(self, dlg):
		"""Center a Toplevel over the main window. Call after the
		dialog has been packed/gridded and ``update_idletasks`` so
		``winfo_width()`` reflects the requested size."""
		x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
		y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
		dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

	def _build_kv_table(self, parent, rows, *, value_anchor="e"):
		"""Render a 2-column key/value table inside ``parent``. ``rows``
		is a list of ``(label, value, [fg])`` tuples; the optional
		``fg`` overrides the value cell's foreground (used for ⚠ rows).
		Returns the table Frame so callers can pack/grid it.
		"""
		table = tk.Frame(parent, bg="#cccccc", bd=1, relief="solid")
		for r, row in enumerate(rows):
			label, value = row[0], row[1]
			fg = row[2] if len(row) > 2 else None
			tk.Label(table, text=label, anchor="w", padx=8, pady=3,
				bg="white",
			).grid(row=r, column=0, sticky="nsew", padx=(0, 1), pady=(0 if r == 0 else 1, 0))
			tk.Label(table, text=value, anchor=value_anchor, padx=8, pady=3,
				bg="white", fg=fg or PALETTE["fg_text"],
			).grid(row=r, column=1, sticky="nsew", pady=(0 if r == 0 else 1, 0))
		table.grid_columnconfigure(0, weight=1)
		table.grid_columnconfigure(1, weight=0)
		return table

	def _build_checklist(self, parent, items, *,
			on_change=None, side_effects=None):
		"""Render a vertical checklist of items inside ``parent``.

		``items`` is a list of label strings. ``on_change(all_checked)``
		fires whenever a box toggles; the primary-action button uses
		this to enable itself once everything is ticked.
		``side_effects`` is an optional dict {index: callable} run by
		Select All to trigger button-style items (e.g. Move Needle).
		Returns ``(vars_list, frame)``.
		"""
		vars_list = []
		frame = tk.Frame(parent)
		for i, label in enumerate(items):
			var = tk.IntVar(value=0)
			vars_list.append(var)
			cb = ttk.Checkbutton(frame, text=label, variable=var)
			cb.grid(row=i, column=0, sticky="w", pady=1)

		def _evaluate(*_):
			all_checked = all(v.get() == 1 for v in vars_list)
			if on_change is not None:
				on_change(all_checked)
		for v in vars_list:
			v.trace_add("write", _evaluate)
		_evaluate()
		return vars_list, frame

	def _show_begin_fractionation_dialog(self, *,
			sample_id, plate_id,
			waste_now, waste_added, waste_projected, waste_max):
		"""Compact Begin Fractionation confirmation. Prompts for the
		Sample ID (the parameter most worth a final glance), shows a
		2-column waste-bin projection table, and offers Cancel /
		Begin Fractionation. All other run parameters are visible in
		the main window behind the dialog and are not duplicated.

		Returns True if Begin Fractionation is clicked, False on
		Cancel / Escape / window-close.
		"""
		dlg = tk.Toplevel(self)
		dlg.title("Begin Fractionation")
		dlg.transient(self)
		dlg.resizable(False, False)
		body = tk.Frame(dlg, padx=18, pady=14)
		body.pack(fill=tk.BOTH, expand=True)

		# Header: the two identifiers the operator is most likely to
		# have mis-set when starting a new run.
		id_table = self._build_kv_table(body, [
			("Sample ID:", sample_id),
			("Plate ID:", plate_id),
		], value_anchor="w")
		id_table.pack(fill=tk.X, pady=(0, 8))

		tk.Label(body, justify="left", anchor="w", wraplength=440,
			text=("Verify the Sample ID above is correct for this run. "
				"Other parameters are visible in the window behind this "
				"dialog — review and adjust them before continuing if "
				"needed."),
		).pack(anchor="w", pady=(0, 10))

		tk.Label(body, text="Waste bin projection:", anchor="w",
			font=FONTS["bold"]).pack(anchor="w", pady=(0, 4))
		waste_rows = [
			("At run start", f"{waste_now:.0f} mL"),
			("Estimated added this run", f"{waste_added:.0f} mL"),
			("Projected end-of-run", f"{waste_projected:.0f} mL"),
			("Capacity", f"{waste_max:.0f} mL"),
		]
		if waste_projected > waste_max and waste_max > 0:
			waste_rows.append(
				("⚠ Projected to exceed capacity", "Empty bin first?",
				 "#b25e09"),
			)
		self._build_kv_table(body, waste_rows).pack(fill=tk.X, pady=(0, 12))

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X)
		result = {"go": False}
		def _ok(_e=None):
			result["go"] = True
			dlg.destroy()
		def _cancel(_e=None):
			dlg.destroy()
		ttk.Button(btn_row, text="Cancel", command=_cancel).pack(side=tk.LEFT, padx=4)
		begin_btn = ttk.Button(btn_row, text="Begin Fractionation",
			command=_ok, style="Primary.TButton")
		begin_btn.pack(side=tk.RIGHT, padx=4)
		dlg.bind("<Return>", _ok)
		dlg.bind("<Escape>", _cancel)
		dlg.protocol("WM_DELETE_WINDOW", _cancel)

		dlg.update_idletasks()
		self._center_over_main(dlg)
		dlg.grab_set()
		begin_btn.focus_set()
		self.wait_window(dlg)
		return result["go"]

	def _show_end_run_dialog(self, *, project, sample_id, bulk_summary=None):
		"""Three-button End Run confirmation. Returns ``"save"``,
		``"discard"``, or ``"cancel"``. ``bulk_summary`` (when set)
		shows a short bulk-progress line in place of the
		project/sample prompt.
		"""
		dlg = tk.Toplevel(self)
		dlg.title("End Run")
		dlg.transient(self)
		dlg.resizable(False, False)
		body = tk.Frame(dlg, padx=18, pady=14)
		body.pack(fill=tk.BOTH, expand=True)

		if bulk_summary:
			prompt = bulk_summary
		else:
			prompt = (
				f"Save the logs for project '{project}' / "
				f"sample '{sample_id}'?"
			)
		tk.Label(body, text=prompt, justify="left", anchor="w",
			wraplength=420).pack(anchor="w", pady=(0, 12))

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X)
		result = {"choice": "cancel"}
		def _save(_e=None):
			result["choice"] = "save"; dlg.destroy()
		def _discard():
			result["choice"] = "discard"; dlg.destroy()
		def _cancel(_e=None):
			result["choice"] = "cancel"; dlg.destroy()
		ttk.Button(btn_row, text="Cancel", command=_cancel).pack(side=tk.LEFT, padx=4)
		save_btn = ttk.Button(btn_row, text="Save and End",
			command=_save, style="Primary.TButton")
		save_btn.pack(side=tk.RIGHT, padx=4)
		ttk.Button(btn_row, text="Don't Save",
			command=_discard, style="Danger.TButton").pack(side=tk.RIGHT, padx=4)
		dlg.bind("<Return>", _save)
		dlg.bind("<Escape>", _cancel)
		dlg.protocol("WM_DELETE_WINDOW", _cancel)

		dlg.update_idletasks()
		self._center_over_main(dlg)
		dlg.grab_set()
		save_btn.focus_set()
		self.wait_window(dlg)
		return result["choice"]

	def _show_calibration_confirm_dialog(self, paused_x_cm, paused_y_cm):
		"""Modal Toplevel asking the operator to verify the needle is
		correctly positioned over the expected well after a mid-pause
		Return-to-Origin recalibration. Returns True on Confirm, False
		on Cancel (X button, Escape key).
		"""
		dlg = tk.Toplevel(self)
		dlg.title("Confirm Calibration")
		dlg.transient(self)
		dlg.resizable(False, False)

		result = {"confirmed": False}

		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)
		tk.Label(
			body, justify="left", anchor="w", wraplength=440,
			text=(
				"The needle has returned to the position where the run "
				"was paused\n"
				f"(X = {paused_x_cm:.2f} cm, Y = {paused_y_cm:.2f} cm).\n\n"
				"Please verify visually that the needle is correctly "
				"positioned over the expected well before resuming.\n\n"
				"If calibration looks correct, click Confirm to resume "
				"fractionation. If not, click Cancel — the run stays "
				"paused so you can re-park the carriage and try again."
			),
		).pack(anchor="w", pady=(0, 10))

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X)

		def _cancel(_event=None):
			dlg.destroy()
		def _confirm(_event=None):
			result["confirmed"] = True
			dlg.destroy()

		ttk.Button(btn_row, text="Cancel", command=_cancel).pack(side=tk.LEFT, padx=4)
		ttk.Button(btn_row, text="Confirm", command=_confirm,
			style="Primary.TButton").pack(side=tk.RIGHT, padx=4)
		dlg.bind("<Escape>", _cancel)
		dlg.bind("<Return>", _confirm)
		dlg.protocol("WM_DELETE_WINDOW", _cancel)

		dlg.update_idletasks()
		x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
		y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
		dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
		dlg.grab_set()
		self.wait_window(dlg)
		return result["confirmed"]

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
			table_start, carriage_start, drip_wait_time,
			purge_time, skip_intersample_purge,
			peristaltic_rate_ml_per_min, max_waste_volume_ml):
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

		# Waste-bin projection table values. Operators get a compact
		# 2-column summary of bin state and projected per-sample
		# additions; the bin is the one parameter without a visible
		# main-window readout for its FORWARD projection.
		waste_now = self.waste_volume_ml
		waste_max = max_waste_volume_ml
		discard_per_sample_ml = discard_fractions * volume
		per_transition_ml = (
			0.0 if skip_intersample_purge
			else 2.0 * purge_time * (peristaltic_rate_ml_per_min / 60.0)
		)
		projected_end_ml = waste_now + discard_per_sample_ml

		if not self._show_begin_fractionation_dialog(
				sample_id=sample_id_at_start, plate_id=plate_id_at_start,
				waste_now=waste_now, waste_added=discard_per_sample_ml,
				waste_projected=projected_end_ml, waste_max=waste_max):
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
		s.purge_time = purge_time
		s.skip_intersample_purge = skip_intersample_purge
		s.peristaltic_rate_ml_per_min = peristaltic_rate_ml_per_min
		s.max_waste_volume_ml = max_waste_volume_ml
		s.volume_per_well = volume

		# Snapshot the waste counter at run start so end.json can report
		# how much waste this specific run added (independent of
		# mid-session resets).
		self._waste_volume_at_run_start = self.waste_volume_ml
		self._waste_warnings_fired = 0
		self._waste_shutoffs_fired = 0
		self._waste_resets_during_run = 0
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
			drip_wait_time, purge_time, skip_intersample_purge,
			peristaltic_rate_ml_per_min, max_waste_volume_ml)

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
			drip_wait_time, purge_time, skip_intersample_purge,
			peristaltic_rate_ml_per_min, max_waste_volume_ml):
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
				"pump_rate_units": "mL/hr",
				"drip_wait_time_s": drip_wait_time,
				"purge_time_s": purge_time,
				"skip_intersample_purge": skip_intersample_purge,
				"peristaltic_rate_ml_per_min": peristaltic_rate_ml_per_min,
				"max_waste_volume_ml": max_waste_volume_ml,
				"volume_per_well_ml": volume,
				"table_start_cm": round(table_start, 2),
				"carriage_start_cm": round(carriage_start, 2),
				"number_of_fractions": number_of_fractions,
				"discard_fractions": discard_fractions,
				"waste_bin_table_cm": round(waste_bin_table, 2),
				"waste_bin_carriage_cm": round(waste_bin_carriage, 2),
				"plate_id_at_start": plate_id_at_start,
			},
			"estimated_total_time_s": estimated_total_s,
		}
		if self.bulk_mode_active:
			first = self.bulk_samples[0]
			metadata["bulk_submission"] = {
				"source_path": self.bulk_source_path,
				"total_samples": len(self.bulk_samples),
				"this_sample_index": 1,
				"spreadsheet_sample_id": first.get("spreadsheet_sample_id", ""),
				"actual_sample_id": first.get("sample_id", ""),
				"notes": first.get("notes", ""),
				"samples": [
					{
						"index": i + 1,
						"spreadsheet_sample_id": s.get("spreadsheet_sample_id", ""),
						"plate_id": s.get("plate_id", ""),
						"number_of_fractions": s.get("number_of_fractions"),
						"discard_fractions": s.get("discard_fractions"),
						"volume_per_well_ml": s.get("volume_per_well_ml"),
						"notes": s.get("notes", ""),
					}
					for i, s in enumerate(self.bulk_samples)
				],
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
			self._set_phase("discard")
			self.set_status(
				f"Discard phase: moving to waste bin "
				f"({s.waste_bin_table:.2f} cm, {s.waste_bin_carriage:.2f} cm)..."
			)
			self.move_to_positions(
				table_dist=s.waste_bin_table,
				carriage_dist=s.waste_bin_carriage,
			)
			self.automated_frame.progress.set_discard_status(0, s.discards_at_series_start)
			self.pump_liquid()
		else:
			self._set_phase("collect")
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
			# Discard cycle waste volume is now charged by the real-time
			# tracker (relay ON → after(500) ticker → relay OFF) so the
			# per-cycle estimate stays in sync with the configured
			# Pump rate even mid-cycle.
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
				self._set_phase("collect")
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
		"""Hold the run at the last collected position; await End Run.

		In bulk mode: advance the bulk-sample index and auto-open the
		transition dialog so the operator doesn't have to click
		Continue to Next Sample to start the next sample's setup.
		"""
		s = self.state
		# Cancel any pending after (defensive -- shouldn't be one here).
		if s.taskId is not None:
			self.after_cancel(s.taskId)
			s.taskId = None
		self.pump_controller.set_relay(False)
		s.state = "total_reached"
		s.is_paused = True
		self.automated_frame.progress.pause_elapsed()
		self.automated_frame.progress.set_total_reached(s.number_of_fractions)
		self.set_status(
			f"Total of {s.number_of_fractions} fractions reached. "
			"Click End Run or Continue to Next Sample."
		)
		self._update_run_control_buttons()

		if self.bulk_mode_active:
			# bulk_current_index pointed at the just-completed sample;
			# advance to the next one before opening the dialog so the
			# dialog shows the "Sample N of total" copy.
			self.bulk_current_index += 1
			self._refresh_bulk_panel()
			# Defer the dialog so the GUI repaints the auto-pause state
			# before the modal grabs focus.
			self.after(50, self._handle_bulk_transition)

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
		self.automated_frame.progress.pause_elapsed()
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

		The confirmation dialog has three buttons: Save and End writes
		end_*.json + summary*.md; Don't Save leaves metadata.json + the
		raw log.csv on disk without finalization; Cancel returns out of
		end_run without changing run state.
		"""
		s = self.state
		# Nothing to end if no run is active.
		if self.run_logger is None and s.state == "idle":
			return

		project_at_click = s.project or "(unset)"
		sample_at_click = s.current_sample_id or "(unset)"
		if self.bulk_mode_active:
			completed = min(self.bulk_current_index, len(self.bulk_samples))
			bulk_prompt = (
				f"End bulk run with {completed} of "
				f"{len(self.bulk_samples)} samples completed?"
			)
			choice = self._show_end_run_dialog(
				project=project_at_click, sample_id=sample_at_click,
				bulk_summary=bulk_prompt,
			)
		else:
			choice = self._show_end_run_dialog(
				project=project_at_click, sample_id=sample_at_click,
			)
		if choice == "cancel":
			return
		save = (choice == "save")

		# Cancel any pending after()
		if s.taskId is not None:
			self.after_cancel(s.taskId)
			s.taskId = None

		# Clear the mid-pause recalibration flag -- ending the run
		# means no Resume confirmation will ever fire for this pause.
		s.origin_returned_during_pause = False

		# Determine final status: "completed" iff we hit total_reached
		# before End Run; "manual_abort" otherwise.
		final_status = "completed" if s.state == "total_reached" else "manual_abort"

		# Reset state BEFORE pump release (button-refresh sees idle).
		pre_state = s.state
		pre_x, pre_y = s.x, s.y
		s.state = "idle"
		s.is_paused = False
		self._set_phase("idle")

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
					file_suffix=end_ts,
					waste_context=self._waste_context(),
					bulk_context=self._bulk_context())
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
		# End Run implicitly exits bulk mode so the next run starts
		# with a clean Run Parameters slate.
		if self.bulk_mode_active:
			self._deactivate_bulk_mode()

	def _handle_bulk_transition(self):
		"""Open the bulk transition dialog. On Continue, drive through
		the standard continue_to_next_sample workflow. On Cancel, leave
		the run paused so the operator can re-open via the Continue to
		Next Sample button."""
		if not self.bulk_mode_active:
			return
		# If we just incremented past the last sample, show the "bulk
		# complete" final dialog and stop here -- the operator clicks
		# End Run to finalize.
		if self.bulk_current_index >= len(self.bulk_samples):
			self._show_bulk_transition_dialog()
			return
		if self._show_bulk_transition_dialog():
			# Dialog already wrote the (possibly edited) sample_id +
			# populated Run Parameters. Now run the standard
			# continue-to-next-sample flow, which the bulk pre-flight
			# below knows to short-circuit past its Sample ID check.
			self.continue_to_next_sample()

	def continue_to_next_sample(self):
		"""Start a new series within the current run. Used after auto-pause-
		at-total-reached to begin collection for the next ultracentrifuge tube.

		Pre-flight 1: confirm the operator changed Sample ID (or accepted the
		warning). Pre-flight 2: hard-block if the new series wouldn't fit in
		the remaining plate wells. Otherwise: reset per-series counters, emit
		a resume breadcrumb, optionally run a discard phase, and snake-step
		to the first new plate well.

		In bulk mode: if the operator clicks the Continue to Next Sample
		button DIRECTLY (rather than going through the auto-fired bulk
		transition dialog), re-open the transition dialog first.
		"""
		s = self.state
		# Only meaningful from the auto-pause-at-total-reached state.
		if s.state != "total_reached":
			return

		# Bulk mode: route the operator through the transition dialog
		# unless we just came from one (the bulk dialog applies its
		# edits + populates fields BEFORE calling back here, so by
		# this point Run Parameters already reflect the new sample).
		if self.bulk_mode_active and not getattr(
				self, "_bulk_transition_in_progress", False):
			self._bulk_transition_in_progress = True
			try:
				self._handle_bulk_transition()
			finally:
				self._bulk_transition_in_progress = False
			return

		# Pre-flight 1: Sample ID unchanged warning. The dialog binds to
		# the same StringVar as the main Sample ID entry, so edits in
		# either widget propagate in real time. Re-read the value after
		# the dialog so the rest of this method uses whatever the
		# operator (possibly) changed it to.
		# In bulk mode the transition dialog has already vetted Sample
		# ID, so skip the legacy unchanged-prompt to avoid stacking
		# modals.
		current_sample = self.automated_frame.sample_id_te.get().strip()
		prior_sample = getattr(self, "_series_start_sample_id", s.current_sample_id)
		if current_sample == prior_sample and not self.bulk_mode_active:
			if not self._show_sample_id_confirm_dialog():
				return
			current_sample = self.automated_frame.sample_id_te.get().strip()

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

		# Optional inter-sample purge workflow: three modal steps with
		# pump phases between them, gated by Skip inter-sample purge.
		# The purge keeps the run "logically paused" (state stays at
		# total_reached) until Phase 3 confirms; then _commit_new_series
		# runs the new sample's discard + collection.
		def _proceed():
			self._commit_new_series(current_sample, new_d_val)

		if s.skip_intersample_purge:
			_proceed()
		else:
			self._start_intersample_purge(
				new_sample_id=current_sample,
				next_series_index=s.series_index + 1,
				on_done=_proceed,
			)

	def _commit_new_series(self, sample_id, discards_val):
		"""Bookkeeping + first move of a new sample series. Called by
		``continue_to_next_sample`` either directly (when skip-purge is on)
		or via the purge workflow's on_done callback (after the three
		modal phases complete)."""
		s = self.state
		# Clear any mid-pause recalibration flag carried over from the
		# auto-pause -- Continue advances the run, so the Resume
		# confirmation path is no longer relevant.
		s.origin_returned_during_pause = False
		# Snapshot the per-series D so labels and the discard-cycle count
		# come from the value that was set when the operator clicked
		# Continue, not whatever the entry holds later.
		s.series_index += 1
		s.discards_done = 0
		s.wells_collected = 0
		s.current_series_sequence = 0
		s.discards_at_series_start = discards_val
		s.is_paused = False
		self._series_start_sample_id = sample_id
		# Active fractionation resumes -- restart the Elapsed clock that
		# pause_elapsed() froze at the prior auto-pause-at-total-reached.
		self.automated_frame.progress.resume_elapsed()
		logger.info("Starting series %d: sample %s (D=%d)",
			s.series_index, sample_id, discards_val)

		# Resume breadcrumb -- documents the sample handoff in log.csv.
		if self.run_logger is not None:
			next_x, next_y = self._next_well_after_resume()
			self.run_logger.resume_breadcrumb(next_x, next_y)

		self.set_status(f"Starting series {s.series_index}: sample {sample_id}")

		if s.discards_at_series_start > 0:
			self._set_phase("discard")
			self.move_to_positions(
				table_dist=s.waste_bin_table,
				carriage_dist=s.waste_bin_carriage,
			)
			self.automated_frame.progress.set_discard_status(0, s.discards_at_series_start)
			self.pump_liquid()
		else:
			# No discards this series: snake-step to next well and pump.
			self._set_phase("collect")
			if not self._snake_step():
				# Off the plate -- shouldn't happen post-capacity-check, but
				# fall back to auto-pause if it does.
				self._auto_pause_total_reached()
				return
			self.pump_liquid()

		self._update_run_control_buttons()

	def _start_intersample_purge(self, new_sample_id, next_series_index, on_done):
		"""Run the inter-sample purge workflow.

		Phase count depends on ``self.purge_protocol``:

		  "basic" (3 phases):
		     1. Connect inlet to water, flush  (peristaltic pump)
		     2. Disconnect from water (in air), clear  (peristaltic pump)
		     3. Connect to new sample, prime syringe  (syringe pump)

		  "decontamination" (5 phases):
		     1. Sterile water flush                   (peristaltic)
		     2. Bleach flush                          (peristaltic)
		     3. Sterile water rinse                   (peristaltic)
		     4. Air clear                             (peristaltic)
		     5. Connect to new sample, prime syringe  (syringe pump)

		Each phase opens with the pump OFF. Pressing Space toggles the
		pump on/off; the operator decides when enough fluid has flowed.
		Continue is disabled while the pump is currently ON. There is
		no fixed duration -- the operator may toggle as many times as
		needed; each on→off cycle writes its own log.csv row.

		Cancel turns the pump off if currently on, then aborts the
		workflow and returns the run to the auto-pause state.
		"""
		s = self.state

		# Move to waste bin first. Synchronous via move_to_positions.
		self.set_status("Moving to waste bin for inter-sample purge…")
		self.move_to_positions(
			table_dist=s.waste_bin_table,
			carriage_dist=s.waste_bin_carriage,
		)
		self.set_status("Inter-sample purge: awaiting user.")

		# The run normally holds a "fractionate" claim throughout.
		# Inter-sample phases that use the peristaltic pump need to
		# reflect that in the status bar's pump indicator so the
		# operator sees which pump should be plugged into the relay.
		# We swap the claim around as the workflow walks through the
		# phases; the priming phase re-claims "fractionate".
		def _claim(name):
			"""Force the relay claim to ``name``. State-machine driven
			(no operator-confirm prompt). Idempotent."""
			pc = self.pump_controller
			if pc.claimant == name:
				return
			if pc.claimant is not None:
				pc.release()
			pc.claim_for(name)

		ctx = {"cancelled": False, "modal": None,
			"is_pumping": False, "tick_after": None,
			"cycle_phase": None, "cycle_sub": "",
			"cycle_count": 0, "cycle_start_mono": None,
			"cycle_start_iso": None}

		def _stop_pump_and_log():
			"""Turn the relay off and (if a cycle is in flight) commit
			the per-cycle row to log.csv + charge the waste estimate.
			Idempotent: a no-op when the pump is already off."""
			if not ctx["is_pumping"]:
				return
			if ctx["tick_after"] is not None:
				try:
					self.after_cancel(ctx["tick_after"])
				except Exception:
					pass
				ctx["tick_after"] = None
			self.pump_controller.set_relay(False)
			ctx["is_pumping"] = False
			elapsed = monotonic() - ctx["cycle_start_mono"]
			end_iso = datetime.now().isoformat(timespec="milliseconds")
			phase = ctx["cycle_phase"]
			cycle = ctx["cycle_count"]
			sub = ctx["cycle_sub"]
			if self.run_logger is not None and phase:
				try:
					self.run_logger.purge_committed(
						phase=phase, series_index=next_series_index,
						waste_x_cm=s.waste_bin_table,
						waste_y_cm=s.waste_bin_carriage,
						start_iso=ctx["cycle_start_iso"],
						end_iso=end_iso, duration_s=elapsed,
						cycle=cycle, sub_phase=sub,
					)
				except Exception as exc:
					logger.warning(
						"Failed to log purge cycle row: %s", exc)
			# Per-cycle waste volume is charged by the real-time tracker
			# (started by the PumpController state callback on relay ON,
			# stopped on relay OFF). The end-of-cycle _add_waste call
			# that used to live here would double-count.

		def _cancel_and_close():
			"""Abort the purge workflow. Turns the pump off (commits the
			partial cycle to the log) and closes the modal. Re-claims
			"fractionate" so the run's auto-pause state is consistent
			with how it was before the purge started.
			"""
			if ctx["is_pumping"]:
				_stop_pump_and_log()
			ctx["cancelled"] = True
			if ctx["modal"] is not None:
				try:
					ctx["modal"].destroy()
				except Exception:
					pass
				ctx["modal"] = None
			_claim("fractionate")
			self.set_status(
				f"Inter-sample purge cancelled. Sample {s.current_sample_id} "
				"still at auto-pause; click Continue to Next Sample to retry."
			)

		def _build_modal(title, body_text, action_label, action_cmd, *,
				checklist=None, skip_context=None, note_text=None):
			"""Build a purge-phase modal.

			Layout: optional ``checklist`` block, then a body text label,
			then the pump-toggle status block (Pump: OFF/ON, This
			cycle, Total pumping), then a three-button row with
			``[Cancel] [Skip Checklist (Expert)] [Continue]``. The
			Skip button is omitted when there is no checklist.

			The action button is disabled when (a) the checklist isn't
			complete (and wasn't skipped) OR (b) the pump is currently
			ON. Returns ``(dlg, body_lbl, pump_lbl, cycle_lbl,
			total_lbl, action_btn, set_pump_gate)``. ``set_pump_gate``
			is a callable the caller invokes with True/False to update
			the pump-on gate that disables the action button.
			"""
			dlg = tk.Toplevel(self)
			dlg.title(title)
			dlg.transient(self)
			dlg.resizable(False, False)
			dlg.protocol("WM_DELETE_WINDOW", _cancel_and_close)
			dlg.bind("<Escape>", lambda _e: _cancel_and_close())

			body = tk.Frame(dlg, padx=14, pady=12)
			body.pack(fill=tk.BOTH, expand=True)

			check_vars = []
			bypass = {"skipped": False}
			if checklist:
				checklist_frame = tk.Frame(body)
				checklist_frame.pack(anchor="w", fill=tk.X, pady=(0, 8))
				for i, item in enumerate(checklist):
					v = tk.IntVar(value=0)
					check_vars.append(v)
					ttk.Checkbutton(checklist_frame, text=item, variable=v,
						).grid(row=i, column=0, sticky="w", pady=1)

			# Optional italic note rendered between the checklist and
			# the body text (e.g. the fresh-bleach preparation reminder
			# in the decontamination Phase 2 modal). Muted color +
			# italic font so it reads as guidance rather than a step.
			if note_text:
				tk.Label(body, text=note_text, justify="left",
					anchor="w", wraplength=460,
					fg=PALETTE["fg_muted"],
					font=(FONTS["family"], FONTS["size"], "italic"),
				).pack(anchor="w", pady=(0, 8))

			if body_text:
				body_lbl = tk.Label(body, text=body_text, justify="left",
					wraplength=460, anchor="w")
				body_lbl.pack(anchor="w", pady=(0, 10))
			else:
				body_lbl = None

			# Pump-toggle status block.
			pump_block = tk.Frame(body)
			pump_block.pack(anchor="w", fill=tk.X, pady=(0, 10))
			pump_lbl = tk.Label(pump_block, text="Pump: OFF",
				font=FONTS["bold"], anchor="w")
			pump_lbl.pack(anchor="w")
			cycle_lbl = tk.Label(pump_block, text="This cycle: 0.0 s",
				anchor="w")
			cycle_lbl.pack(anchor="w")
			total_lbl = tk.Label(pump_block, text="Total pumping: 0.0 s",
				anchor="w")
			total_lbl.pack(anchor="w")

			# Three-column button row.
			btn_row = tk.Frame(body)
			btn_row.pack(fill=tk.X)
			btn_row.grid_columnconfigure(0, weight=1)
			btn_row.grid_columnconfigure(1, weight=1)
			btn_row.grid_columnconfigure(2, weight=1)
			cancel_btn = ttk.Button(btn_row, text="Cancel",
				command=_cancel_and_close, style="Danger.TButton")
			cancel_btn.grid(row=0, column=0, sticky="w", padx=4)
			action_btn = ttk.Button(btn_row, text=action_label,
				command=action_cmd, style="Primary.TButton")
			action_btn.grid(row=0, column=2, sticky="e", padx=4)

			# Combined gate: action button enabled iff
			#   pump is OFF AND (no checklist OR all checked OR skipped).
			gate = {"pump_on": False}

			def _recompute_action_state():
				if gate["pump_on"]:
					action_btn.state(["disabled"])
					return
				if checklist and not bypass["skipped"]:
					if all(v.get() == 1 for v in check_vars):
						action_btn.state(["!disabled"])
					else:
						action_btn.state(["disabled"])
				else:
					action_btn.state(["!disabled"])

			def set_pump_gate(on):
				gate["pump_on"] = bool(on)
				_recompute_action_state()

			if checklist:
				def _skip():
					bypass["skipped"] = True
					if self.run_logger is not None and skip_context:
						try:
							self.run_logger.checklist_skipped(skip_context)
						except Exception as exc:
							logger.warning(
								"Failed to log skipped checklist: %s", exc)
					_recompute_action_state()
				skip_btn = ttk.Button(btn_row,
					text="Skip Checklist (Expert)", command=_skip)
				skip_btn.grid(row=0, column=1, padx=4)
				for v in check_vars:
					v.trace_add("write", lambda *_: _recompute_action_state())

			_recompute_action_state()
			ctx["modal"] = dlg
			dlg.update_idletasks()
			x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
			y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
			dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
			dlg.grab_set()
			return (dlg, body_lbl, pump_lbl, cycle_lbl, total_lbl,
				action_btn, set_pump_gate)

		def _run_toggle_phase(title, body_text, phase, sub_phase, pump_kind,
				on_advance, checklist, skip_context, note_text=None):
			"""Operator-toggled pump phase: each Space press starts a
			cycle; the next Space press stops it (and writes a
			per-cycle log row). Continue advances when the pump is OFF
			and the checklist is satisfied.

			``pump_kind`` selects which physical pump is expected to be
			plugged into the relay -- either ``"peristaltic"`` (water /
			bleach / clear) or ``"syringe"`` (priming). Used only for
			the relay-claim swap so the status bar's pump indicator
			tracks the operator-visible pump.
			"""
			if ctx["cancelled"]:
				return
			_claim("purge" if pump_kind == "peristaltic" else "fractionate")

			phase_state = {"total_s": 0.0, "cycle": 0}

			(dlg, _, pump_lbl, cycle_lbl, total_lbl,
				action_btn, set_pump_gate) = _build_modal(
					title, body_text, "Continue", lambda: _on_continue(),
					checklist=checklist, skip_context=skip_context,
					note_text=note_text,
				)

			def _tick():
				if ctx["cancelled"] or not ctx["is_pumping"]:
					ctx["tick_after"] = None
					return
				now_s = monotonic() - ctx["cycle_start_mono"]
				cycle_lbl.config(text=f"This cycle: {now_s:.1f} s")
				total_lbl.config(
					text=f"Total pumping: {phase_state['total_s'] + now_s:.1f} s")
				# Waste-bin auto-shutoff aborted this cycle mid-pump.
				if self._purge_halted_for_waste:
					_stop_pump_and_log()
					pump_lbl.config(text="Pump: OFF (waste bin full)")
					set_pump_gate(False)
					return
				ctx["tick_after"] = self.after(100, _tick)

			def _start_cycle():
				phase_state["cycle"] += 1
				ctx["cycle_phase"] = phase
				ctx["cycle_sub"] = sub_phase or ""
				ctx["cycle_count"] = phase_state["cycle"]
				ctx["cycle_start_mono"] = monotonic()
				ctx["cycle_start_iso"] = datetime.now().isoformat(
					timespec="milliseconds")
				ctx["is_pumping"] = True
				self.pump_controller.set_relay(True)
				pump_lbl.config(text="Pump: ON")
				cycle_lbl.config(text="This cycle: 0.0 s")
				set_pump_gate(True)
				_tick()

			def _stop_cycle():
				# Capture the cycle duration BEFORE _stop_pump_and_log
				# clears the cycle metadata, so we can update the
				# Total pumping label correctly.
				dur = monotonic() - ctx["cycle_start_mono"]
				_stop_pump_and_log()
				phase_state["total_s"] += dur
				pump_lbl.config(text="Pump: OFF")
				cycle_lbl.config(text="This cycle: 0.0 s")
				total_lbl.config(
					text=f"Total pumping: {phase_state['total_s']:.1f} s")
				set_pump_gate(False)

			def _on_space(_e=None):
				if ctx["cancelled"]:
					return "break"
				if ctx["is_pumping"]:
					_stop_cycle()
				else:
					_start_cycle()
				return "break"

			def _on_continue(_e=None):
				if ctx["is_pumping"] or ctx["cancelled"]:
					return
				# Gate also catches checklist-not-complete; the button
				# is disabled in that case so this code path only
				# fires from a legitimate click.
				try:
					dlg.unbind("<space>")
					dlg.unbind("<Return>")
				except Exception:
					pass
				if dlg.winfo_exists():
					dlg.destroy()
				ctx["modal"] = None
				if not ctx["cancelled"]:
					on_advance()

			dlg.bind("<space>", _on_space)
			dlg.bind("<Return>", _on_continue)
			# action_btn is the Continue button; the space override
			# prevents the focused button from stealing the toggle.
			action_btn.bind("<space>", _on_space)
			action_btn.config(command=_on_continue)

		# -- Phase definitions -------------------------------------------
		protocol = self.purge_protocol
		decon = (protocol == "decontamination")
		total = 5 if decon else 3

		def _wash_phase(step_no):
			_run_toggle_phase(
				title=f"Inter-sample Purge — Step {step_no} of {total}",
				body_text=(
					"Press Space to toggle the peristaltic pump. Pump "
					"sterile water through the tubing until the line "
					"reads clean."
				),
				phase="wash", sub_phase="", pump_kind="peristaltic",
				on_advance=_phase_next_after_wash,
				checklist=[
					"Disconnected inlet line from previous sample tube",
					"Placed inlet line in water container",
				],
				skip_context=f"purge_phase_{step_no}_{next_series_index}",
			)

		def _phase_next_after_wash():
			if decon:
				_bleach_phase()
			else:
				_clear_phase(step_no=2)

		def _bleach_phase():
			_run_toggle_phase(
				title=f"Inter-sample Purge — Step 2 of {total}",
				body_text=(
					"Press Space to toggle the peristaltic pump. Pump "
					"0.5% sodium hypochlorite (bleach) solution through "
					"the tubing to decontaminate."
				),
				phase="bleach", sub_phase="", pump_kind="peristaltic",
				on_advance=_rinse_phase,
				checklist=[
					"Removed inlet line from water container",
					"Placed inlet line in 0.5% bleach solution",
					"Connection is secure",
				],
				skip_context=f"purge_phase_2_{next_series_index}",
				note_text=(
					"Prepare the 0.5% bleach solution fresh on the day "
					"of use — dilute hypochlorite degrades within 24 hours."
				),
			)

		def _rinse_phase():
			_run_toggle_phase(
				title=f"Inter-sample Purge — Step 3 of {total}",
				body_text=(
					"Press Space to toggle the peristaltic pump. Pump "
					"sterile water through the tubing thoroughly to "
					"rinse out any residual bleach."
				),
				phase="wash", sub_phase="rinse", pump_kind="peristaltic",
				on_advance=lambda: _clear_phase(step_no=4),
				checklist=[
					"Removed inlet line from bleach solution",
					"Placed inlet line in sterile water",
				],
				skip_context=f"purge_phase_3_{next_series_index}",
			)

		def _clear_phase(step_no):
			_run_toggle_phase(
				title=f"Inter-sample Purge — Step {step_no} of {total}",
				body_text=(
					"Press Space to toggle the peristaltic pump. Push "
					"air through the tubing to clear residual liquid."
				),
				phase="clear", sub_phase="", pump_kind="peristaltic",
				on_advance=lambda: _prime_phase(step_no=step_no + 1),
				checklist=[
					"Removed inlet line from water container",
					"Line is in air, nothing dripping",
				],
				skip_context=f"purge_phase_{step_no}_{next_series_index}",
			)

		def _prime_phase(step_no):
			_run_toggle_phase(
				title=f"Inter-sample Purge — Step {step_no} of {total}",
				body_text=(
					"Step "
					+ str(step_no - 1)
					+ " cleared the tubing with air, leaving void space "
					"between the syringe pump and the dispensing needle. "
					"Before resuming fractionation, the fractionation "
					"fluid must displace this air so the dispense "
					"pressure is consistent across wells.\n\n"
					"Press Space to toggle the syringe pump on/off. "
					"Walk the fluid through the tubing until even "
					"droplets exit the needle. Click Continue when "
					"droplet behavior looks consistent."
				),
				phase="prime", sub_phase="", pump_kind="syringe",
				on_advance=_finish,
				checklist=[
					f"Connected inlet line to sample {new_sample_id}'s tube",
					"Connection is secure",
				],
				skip_context=f"purge_phase_{step_no}_{next_series_index}",
			)

		def _finish():
			# Workflow complete -- the syringe pump is the active
			# claimant (we claimed "fractionate" entering the priming
			# phase). The run continues into the new sample's discard
			# phase via the on_done callback.
			if not ctx["cancelled"]:
				on_done()

		_wash_phase(step_no=1)

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

	def _show_bulk_transition_dialog(self):
		"""Modal Toplevel: either prepare the next bulk sample or
		announce the bulk run complete. Returns True if the operator
		clicked Continue (next-sample case); False on Cancel; True on
		OK of the final "Bulk Run Complete" variant.

		Side-effect: when next-sample Continue is clicked, applies the
		(possibly edited) sample_id back to ``bulk_samples[next_idx]``
		and populates the Run Parameters fields. The caller still
		invokes ``continue_to_next_sample`` afterward.
		"""
		next_idx = self.bulk_current_index
		# Final-sample case.
		if next_idx >= len(self.bulk_samples):
			dlg = tk.Toplevel(self)
			dlg.title("Bulk Run Complete")
			dlg.transient(self)
			dlg.resizable(False, False)
			body = tk.Frame(dlg, padx=14, pady=12)
			body.pack(fill=tk.BOTH, expand=True)
			tk.Label(body, justify="left", anchor="w", wraplength=420,
				text=(
					f"All {len(self.bulk_samples)} samples in the bulk "
					"submission have completed fractionation.\n\n"
					"Click End Run to save the run logs and finalize "
					"the session."
				),
			).pack(anchor="w", pady=(0, 10))
			result = {"ok": False}
			def _ok(_e=None):
				result["ok"] = True
				dlg.destroy()
			ttk.Button(body, text="OK", command=_ok,
				style="Primary.TButton").pack(anchor="e")
			dlg.bind("<Return>", _ok)
			dlg.bind("<Escape>", _ok)
			dlg.protocol("WM_DELETE_WINDOW", _ok)
			dlg.update_idletasks()
			x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
			y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
			dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
			dlg.grab_set()
			self.wait_window(dlg)
			return result["ok"]

		# Next-sample case.
		next_sample = self.bulk_samples[next_idx]
		just_done_idx = next_idx - 1
		total = len(self.bulk_samples)

		current_plate = self.automated_frame.plate_id_te.get().strip()
		next_plate = next_sample.get("plate_id") or current_plate

		dlg = tk.Toplevel(self)
		dlg.title("Prepare Next Sample")
		dlg.transient(self)
		dlg.resizable(False, False)
		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)

		tk.Label(body, justify="left", anchor="w", wraplength=440,
			text=f"Sample {just_done_idx + 1} of {total} just completed.\n\n"
				 f"Please prepare Sample {next_idx + 1} for fractionation:",
		).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

		# Editable Sample ID.
		tk.Label(body, text="Sample ID:", anchor="w").grid(
			row=1, column=0, sticky="w", padx=(0, 8))
		sid_var = tk.StringVar(value=next_sample["sample_id"])
		sid_entry = ttk.Entry(body, textvariable=sid_var, width=28)
		sid_entry.grid(row=1, column=1, sticky="we")

		# Display-only metadata.
		def _detail(row, label, value):
			tk.Label(body, text=label, anchor="w").grid(
				row=row, column=0, sticky="w", padx=(0, 8))
			tk.Label(body, text=value, anchor="w").grid(
				row=row, column=1, sticky="w")

		_detail(2, "Plate ID:", next_plate or "(unset)")
		# N + D combined display
		n_display = (
			str(next_sample["number_of_fractions"])
			if next_sample.get("number_of_fractions") is not None
			else self.automated_frame.n_fractions_te.get() or "(current)"
		)
		d_display = (
			str(next_sample["discard_fractions"])
			if next_sample.get("discard_fractions") is not None
			else self.automated_frame.discard_te.get() or "(current)"
		)
		_detail(3, "Fractions:", f"{n_display}  (Discard: {d_display})")
		v_display = (
			f"{next_sample['volume_per_well_ml']:g} mL"
			if next_sample.get("volume_per_well_ml") is not None
			else f"{self.automated_frame.vol_text_entry.get() or '?'} mL"
		)
		_detail(4, "Volume per well:", v_display)
		row_next = 5
		notes = next_sample.get("notes", "")
		if notes:
			tk.Label(body, text="Notes:", anchor="w").grid(
				row=row_next, column=0, sticky="nw", padx=(0, 8))
			tk.Label(body, text=notes, anchor="w", wraplength=320,
				justify="left").grid(row=row_next, column=1, sticky="w")
			row_next += 1

		# Plate-ID comparison message.
		if not next_sample.get("plate_id") or next_plate == current_plate:
			plate_msg_text = "✓ Plate ID matches the current plate."
			plate_msg_fg = "#1e7d20"
		else:
			plate_msg_text = (
				f"⚠ Plate ID changes from {current_plate} to {next_plate}. "
				"After clicking Continue, you may need to perform a "
				"plate swap when the current plate fills."
			)
			plate_msg_fg = "#b25e09"
		tk.Label(body, text=plate_msg_text, wraplength=440, justify="left",
			anchor="w", fg=plate_msg_fg).grid(
			row=row_next, column=0, columnspan=2, sticky="we", pady=(8, 0))
		row_next += 1

		err_lbl = tk.Label(body, text="", fg="red", anchor="w", wraplength=440)
		err_lbl.grid(row=row_next, column=0, columnspan=2, sticky="we")
		row_next += 1

		btn_row = tk.Frame(body)
		btn_row.grid(row=row_next, column=0, columnspan=2, sticky="we", pady=(8, 0))

		result = {"confirmed": False}
		def _cancel(_e=None):
			dlg.destroy()
		def _continue(_e=None):
			ok, _ = validation.sample_id(sid_var.get())
			if not ok:
				return
			result["confirmed"] = True
			dlg.destroy()
		ttk.Button(btn_row, text="Cancel", command=_cancel).pack(side=tk.LEFT, padx=4)
		continue_btn = ttk.Button(btn_row, text="Continue",
			command=_continue, style="Primary.TButton")
		continue_btn.pack(side=tk.RIGHT, padx=4)

		def _sync(*_):
			ok, msg = validation.sample_id(sid_var.get())
			if ok:
				err_lbl.config(text="")
				continue_btn.state(["!disabled"])
			else:
				err_lbl.config(text=str(msg))
				continue_btn.state(["disabled"])
		sid_var.trace_add("write", _sync)
		_sync()

		dlg.bind("<Return>", _continue)
		dlg.bind("<Escape>", _cancel)
		dlg.protocol("WM_DELETE_WINDOW", _cancel)
		dlg.update_idletasks()
		x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
		y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
		dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
		dlg.grab_set()
		sid_entry.focus_set()
		sid_entry.select_range(0, tk.END)
		sid_entry.icursor(tk.END)
		self.wait_window(dlg)

		if not result["confirmed"]:
			return False
		# Apply edit + populate Run Parameters.
		new_sid = sid_var.get().strip()
		if new_sid != next_sample["spreadsheet_sample_id"]:
			next_sample["edited"] = True
		next_sample["sample_id"] = new_sid
		# Run Parameters fields are disabled in bulk mode; .set() goes
		# through the StringVar and works regardless of widget state.
		self._apply_bulk_sample_to_fields(next_sample)
		return True

	def _show_sample_id_confirm_dialog(self):
		"""Modal Toplevel for the unchanged-Sample-ID confirm path.

		The entry is bound to the same StringVar as the main Sample ID
		field in Run Parameters, so edits in either widget propagate in
		real time. Returns True on Confirm, False on Cancel (X button,
		Escape key). Enter activates Confirm only when the current
		Sample ID is valid; otherwise it's a no-op.
		"""
		dlg = tk.Toplevel(self)
		dlg.title("Confirm Sample ID")
		dlg.transient(self)
		dlg.resizable(False, False)

		result = {"confirmed": False}

		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)
		tk.Label(
			body, justify="left", anchor="w", wraplength=420,
			text=(
				"The Sample ID has not been changed since the previous "
				"series started. Update it now if this is a new sample, "
				"or confirm to continue with the same Sample ID."
			),
		).pack(anchor="w", pady=(0, 10))

		row = tk.Frame(body)
		row.pack(fill=tk.X, pady=(0, 4))
		tk.Label(row, text="Sample ID:").pack(side=tk.LEFT, padx=(0, 6))
		# Bind to the SAME StringVar as the main Sample ID entry so edits
		# in either widget sync via the underlying variable.
		entry = ttk.Entry(row,
			textvariable=self.automated_frame.sample_id_te.var, width=32)
		entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

		err_lbl = tk.Label(body, text="", fg="red", anchor="w", wraplength=420)
		err_lbl.pack(anchor="w")

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X, pady=(8, 0))

		def _cancel(_event=None):
			dlg.destroy()
		def _confirm(_event=None):
			# Block confirm when validation fails. (Enter binding routes
			# here too; this guard makes Enter a no-op on invalid input.)
			ok, _ = validation.sample_id(
				self.automated_frame.sample_id_te.var.get())
			if not ok:
				return
			result["confirmed"] = True
			dlg.destroy()

		cancel_btn = ttk.Button(btn_row, text="Cancel", command=_cancel)
		cancel_btn.pack(side=tk.LEFT, padx=4)
		confirm_btn = ttk.Button(btn_row, text="Confirm",
			command=_confirm, style="Primary.TButton")
		confirm_btn.pack(side=tk.RIGHT, padx=4)

		def _sync():
			"""Re-validate on every keystroke; show/hide the inline error
			and toggle the Confirm button accordingly."""
			ok, msg = validation.sample_id(
				self.automated_frame.sample_id_te.var.get())
			if ok:
				err_lbl.config(text="")
				confirm_btn.state(["!disabled"])
			else:
				err_lbl.config(text=str(msg))
				confirm_btn.state(["disabled"])
		trace_id = self.automated_frame.sample_id_te.var.trace_add(
			"write", lambda *_: _sync())

		# Initial sync (validates the pre-filled value).
		_sync()

		# Keyboard + close-box wiring.
		dlg.bind("<Escape>", _cancel)
		dlg.bind("<Return>", _confirm)
		dlg.protocol("WM_DELETE_WINDOW", _cancel)

		# Center the dialog over the main window.
		dlg.update_idletasks()
		x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
		y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
		dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
		dlg.grab_set()

		# Focus on the entry with current text selected so the user can
		# immediately overtype it.
		entry.focus_set()
		entry.select_range(0, tk.END)
		entry.icursor(tk.END)

		self.wait_window(dlg)
		# Clean up the trace so successive opens don't accumulate handlers.
		try:
			self.automated_frame.sample_id_te.var.trace_remove("write", trace_id)
		except Exception:
			pass
		return result["confirmed"]

	def _show_plate_swap_dialog(self, old_plate_id, suggested_new_id):
		"""Modal Toplevel for the plate-swap flow, presented as a
		clickable checklist. Continue stays disabled until every box
		is ticked (and the new Plate ID validates) OR the operator
		clicks Skip Checklist (Expert), which logs a
		``checklist_skipped_plate_swap_N`` row and enables Continue.

		Returns the validated new Plate ID on Continue, or None on
		Cancel Run. The "Move Needle to Home" checkbox doubles as the
		button that drives the physical move -- ticking it triggers
		``carriage_return``. The dialog records whether the home step
		was taken so the post-swap safety-home can be skipped when
		redundant.
		"""
		result = {"plate_id": None, "needle_at_home": False}

		dlg = tk.Toplevel(self)
		dlg.title(f"Plate Swap — {old_plate_id}")
		dlg.transient(self)
		dlg.grab_set()
		dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # X disabled; force a choice

		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)

		# Checklist rows. Indices 0..3 correspond to:
		#   0 removed old plate, 1 moved needle home, 2 placed new plate,
		#   3 new plate ID entered (auto-checked when validation passes).
		removed_var = tk.IntVar(value=0)
		home_var = tk.IntVar(value=0)
		placed_var = tk.IntVar(value=0)
		plateid_var = tk.IntVar(value=0)

		ttk.Checkbutton(body, variable=removed_var,
			text=f"Removed previous plate ({old_plate_id}) and stored it",
		).grid(row=0, column=0, columnspan=2, sticky="w", pady=2)

		def _drive_home():
			"""Run carriage_return + tick the home checkbox. Idempotent
			(second click while already home is a no-op move)."""
			self.carriage_return()
			result["needle_at_home"] = True
			home_var.set(1)
		home_cb = ttk.Checkbutton(body, variable=home_var,
			text="Moved needle to home")
		home_cb.grid(row=1, column=0, sticky="w", pady=2)
		# Ticking the box drives the physical move (instead of leaving the
		# operator unsure whether they need to also click a button).
		home_var.trace_add("write", lambda *_: (
			self.carriage_return() if home_var.get() == 1
				and not result["needle_at_home"] else None,
			result.__setitem__("needle_at_home",
				bool(result["needle_at_home"] or home_var.get() == 1)),
		))

		ttk.Checkbutton(body, variable=placed_var,
			text="Placed new plate on stage",
		).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)

		# Plate-ID entry row. Counted as "checked" the moment the field
		# holds a value that passes validation.plate_id (the auto-incremented
		# default already passes, so the box ticks on dialog open).
		id_row = tk.Frame(body)
		id_row.grid(row=3, column=0, columnspan=2, sticky="we", pady=2)
		id_cb = ttk.Checkbutton(id_row, variable=plateid_var,
			text="New plate ID:")
		id_cb.pack(side=tk.LEFT)
		id_cb.state(["disabled"])  # driven by validation, not user-clickable
		plate_te = TextEntry(id_row, "")  # label-less, just the entry
		plate_te.label.grid_remove()
		plate_te.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
		plate_te.set(suggested_new_id)
		tk.Label(body, anchor="w",
			text=f"   (suggested: {suggested_new_id})", fg="#666",
		).grid(row=4, column=0, columnspan=2, sticky="we", padx=(20, 0))

		# Select All / Skip Checklist (Expert) row.
		bulk_row = tk.Frame(body)
		bulk_row.grid(row=5, column=0, columnspan=2, sticky="we", pady=(10, 4))
		def _select_all():
			removed_var.set(1)
			if home_var.get() == 0:
				home_var.set(1)  # also fires the home move via trace
			placed_var.set(1)
		bypass = {"skipped": False}
		def _skip():
			bypass["skipped"] = True
			cont_btn.state(["!disabled"])
			if self.run_logger is not None:
				try:
					self.run_logger.checklist_skipped(
						f"plate_swap_{self.state.plate_swaps_done + 1}")
				except Exception as exc:
					logger.warning(
						"Failed to log skipped plate-swap checklist: %s", exc)
		ttk.Button(bulk_row, text="Select All",
			command=_select_all).pack(side=tk.LEFT, padx=4)
		ttk.Button(bulk_row, text="Skip Checklist (Expert)",
			command=_skip).pack(side=tk.LEFT, padx=4)

		btn_row = tk.Frame(body)
		btn_row.grid(row=6, column=0, columnspan=2, sticky="we", pady=(8, 0))
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
			dlg.destroy()
			self.end_run()

		ttk.Button(btn_row, text="Cancel Run", command=_cancel_run,
			style="Danger.TButton").grid(row=0, column=0, sticky="w", padx=4)
		cont_btn = ttk.Button(btn_row, text="Continue", command=_continue,
			style="Primary.TButton")
		cont_btn.grid(row=0, column=1, sticky="e", padx=4)
		cont_btn.state(["disabled"])

		def _evaluate(*_):
			if bypass["skipped"]:
				return
			ok, _ = validation.plate_id(plate_te.get())
			plateid_var.set(1 if ok else 0)
			all_checked = (removed_var.get() and home_var.get()
				and placed_var.get() and plateid_var.get())
			if all_checked:
				cont_btn.state(["!disabled"])
			else:
				cont_btn.state(["disabled"])
		for v in (removed_var, home_var, placed_var):
			v.trace_add("write", _evaluate)
		plate_te.var.trace_add("write", _evaluate)
		_evaluate()

		self.wait_window(dlg)
		self._plate_swap_pre_homed = result["needle_at_home"]
		return result["plate_id"]

	def _commit_plate_swap(self, new_plate_id):
		"""Apply a confirmed Plate ID change: update state + plates_used,
		emit breadcrumb, safety-home if the operator skipped step 2, move
		to A1 of the new plate, and resume the appropriate phase."""
		s = self.state
		# Clear any mid-pause recalibration flag carried over from the
		# plate-full auto-pause -- Continue advances the run.
		s.origin_returned_during_pause = False
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
			self._set_phase("collect")
			# Plate-full auto-pause froze Elapsed; the new plate is in
			# place and collection resumes -- restart the clock.
			self.automated_frame.progress.resume_elapsed()
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
