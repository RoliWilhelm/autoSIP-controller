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
	FONTS, PALETTE, apply_style, bind_dynamic_wraplength,
	make_bimodal_distribution_canvas, make_bucket_canvas,
	make_centrifuge_tube_canvas, make_mop_canvas, primary_button,
)
from well_plate import WellPlateProgress, TableView, format_snapshot_log
import run_logger
from run_logger import RunLogger, _fmt_hms
import notifications

# GitHub URL displayed (clickable) in the About dialog. Hard-coded here so
# the About dialog has a single source of truth.
_GITHUB_URL = "https://github.com/RoliWilhelm/autoSIP-controller"

__version__ = "1.0.0"

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

	# Default inter-microstep sleep in seconds — slow enough that a drop
	# at the syringe tip stays put during transit. App can override via
	# ``configure_speeds``.
	DEFAULT_STEP_DELAY_S = 0.0001

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

		# Step-rate configuration. ``fractionation_step_delay`` is the
		# slow per-microstep sleep used for well-to-well dispensing
		# moves (and for every move when ``variable_speed_enabled`` is
		# False). ``transit_step_delay`` is the fast value used when
		# variable mode is enabled AND the caller passes ``is_transit=True``.
		# App pushes the active values via ``configure_speeds`` at init
		# and again whenever the operator changes Tools → Preferences.
		self.fractionation_step_delay = self.DEFAULT_STEP_DELAY_S
		self.transit_step_delay = self.DEFAULT_STEP_DELAY_S
		self.variable_speed_enabled = False

	def get_angle(self):
		"""Return the current shaft angle in degrees (unbounded)."""
		return self.angle

	def tare(self):
		"""Reset the tracked angle to zero without moving the motor."""
		self.angle = 0.0

	def release(self):
		"""Release the motor coils to prevent overheating."""
		self.motor.release()

	def configure_speeds(self, *, fractionation_step_delay,
			transit_step_delay, variable_speed_enabled):
		"""Push the active step-rate configuration into the motor.
		Called by App at init and whenever the operator changes the
		motor-speed preference. ``variable_speed_enabled=False`` means
		every move uses ``fractionation_step_delay`` regardless of the
		per-call ``is_transit`` flag — i.e. Slow speed mode."""
		self.fractionation_step_delay = max(0.0, float(fractionation_step_delay))
		self.transit_step_delay = max(0.0, float(transit_step_delay))
		self.variable_speed_enabled = bool(variable_speed_enabled)

	def _step_delay_for(self, is_transit):
		"""Return the inter-microstep sleep for this move. Variable
		mode + transit flag → fast delay; otherwise the slow
		fractionation delay."""
		if self.variable_speed_enabled and is_transit:
			return self.transit_step_delay
		return self.fractionation_step_delay

	def move_relative(self, angle, *, is_transit=False):
		"""Turn the shaft so the slider moves by ``angle`` degrees' worth.

		``is_transit`` selects the per-microstep delay: in Variable
		speed mode, transit moves (to/from waste bin, return to
		origin, plate swaps, manual jogs) get the faster
		``transit_step_delay``; well-to-well fractionation moves
		stay on the slower ``fractionation_step_delay`` so syringe
		droplets don't fling. In Slow speed mode (default) the flag
		is ignored — every move uses the slow delay.

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

		step_delay = self._step_delay_for(is_transit)
		for _ in range(0, abs(total_steps)):
			self.motor.onestep(direction=direction, style=hardware.MICROSTEP)
			sleep(step_delay)

		# Only the intent portion advanced the slider; the backlash portion
		# took up gear play. Accumulate intent_steps so self.angle stays in
		# lock-step with the slider's quantized physical position.
		self.angle = self.angle + intent_steps / self.steps_per_degree

		self.release()

	def move_absolute(self, angle, *, is_transit=False):
		"""Turn the shaft to ``angle`` degrees relative to its initial position."""
		delta_angle = angle - self.angle
		self.move_relative(delta_angle, is_transit=is_transit)

	def move_dist_relative(self, dist, *, is_transit=False):
		"""Move the slider ``dist`` cm relative to its current position.
		``is_transit`` selects the inter-step delay (see ``move_relative``)."""
		logger.debug("%s move_dist_relative dist=%.3f cm transit=%s",
			self.name, dist, is_transit)
		self.move_relative(dist / self.cm_per_deg, is_transit=is_transit)

	def move_dist_absolute(self, dist, *, is_transit=False):
		"""Move the slider to ``dist`` cm from its initial position.
		``is_transit`` selects the inter-step delay (see ``move_relative``)."""
		logger.debug("%s move_dist_absolute dist=%.3f cm transit=%s",
			self.name, dist, is_transit)
		self.move_absolute(dist / self.cm_per_deg, is_transit=is_transit)


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


class _StringVarHolder:
	"""TextEntry-shaped adapter around a bare ``tk.StringVar``.

	Used as a stand-in attribute on the AutomatedFrame for fields whose
	on-screen widget has moved to a Tools-menu dialog. Persistence and
	profile-load plumbing (``_entry_for``, ``get_values``, ``set_values``)
	calls ``.get()`` / ``.set()`` / ``.clear_error()`` / ``.show_error()``
	uniformly; this holder satisfies that contract without owning a
	visible widget. ``.entry`` is ``None`` because no live widget exists
	to receive focus or tooltip bindings; callers must guard accordingly.
	"""

	def __init__(self, stringvar):
		self.var = stringvar
		self.entry = None

	def get(self):
		return self.var.get()

	def set(self, value):
		self.var.set("" if value is None else str(value))

	def clear_error(self):
		return

	def show_error(self, _msg):
		return


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
	# an Origin Calibration dialog. Cleared on Resume-confirm, End Run,
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
	prime_time_s: float = 60.0            # auto-prime duration at run start
	# Per-series snapshot of D. Updated at series start (movement() or
	# continue_to_next_sample()). Used for label computation + auto-pause
	# threshold so an edit to the Discard fractions entry affects only the
	# NEXT series, not the current one.
	discards_at_series_start: int = 0
	discards_done: int = 0                # 0..discards_at_series_start counter
	wells_collected: int = 0              # 0..(N-D_this_series) counter
	# Waste-bin rectangle. ``waste_bin_table`` / ``waste_bin_carriage``
	# are the bin's CENTER in motor cm (the geometric centroid of the
	# rectangle the operator jogs the needle over during Manual-mode
	# calibration). The ``*_extent`` fields are the bin's full width
	# (X) and height (Y); the rectangle occupies [center − extent/2,
	# center + extent/2] on each axis. Default extent 0 → legacy
	# point-target behaviour (every move-to-waste goes to the center
	# itself, no shortest-path routing). Non-zero extents enable
	# shortest-path routing via ``App._waste_entry_for_current_position``.
	waste_bin_table: float = 0.0          # cm, bin center X (table axis)
	waste_bin_carriage: float = 0.0       # cm, bin center Y (carriage axis)
	waste_bin_x_extent: float = 0.0       # cm, bin full width along X (≥ 0)
	waste_bin_y_extent: float = 0.0       # cm, bin full height along Y (≥ 0)
	# Latest entry point used for a move-to-waste — populated by every
	# call site so the per-event log row records WHERE in the bin the
	# fluid actually went (not just the bin center). Defaults to the
	# center so legacy log rows still make sense before the first move.
	last_waste_entry_x: float = 0.0
	last_waste_entry_y: float = 0.0
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
		# system.start.state.json. Both None means the operator entered
		# values manually without loading a file.
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
		# ----- First-launch hint banner ---------------------------------
		# Subtle italic muted reminder pointing operators at the new
		# Tools-menu home of pump + cleaning parameters. Dismisses
		# itself once the relocated required fields are populated;
		# ``_refresh_config_banner`` toggles ``grid()`` / ``grid_remove()``.
		# Created here so it occupies row 0 above the run-controls bar.
		self.config_hint_banner = tk.Label(
			self,
			text=(
				"Configure pump and cleaning parameters in the Tools menu "
				"before starting a run."
			),
			anchor="w", justify="left",
			fg=PALETTE.get("fg_muted", "#666666"),
			font=("TkDefaultFont", 9, "italic"),
			bg=PALETTE.get("bg", self.cget("bg")),
		)
		self.config_hint_banner.grid(row=0, column=0, columnspan=2,
			sticky="we", padx=4, pady=(0, 4))
		self.config_hint_banner.grid_remove()

		ctrl = tk.Frame(self)
		ctrl.grid(row=1, column=0, columnspan=2, pady=(0, 4))
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

		# ----- Bulk Sample Submission --------------------------------
		# Tops the left column above Run Parameters; Fractionation Pump
		# Parameters sits below Run Params at row 3. Plate Parameters
		# (right column) spans rows 1-2 so the right column matches the
		# left column's combined height.
		bulk = tk.LabelFrame(self, text="Bulk Sample Submission", padx=8, pady=2)
		bulk.grid(row=2, column=0, sticky="new", padx=(2, 4), pady=(0, 0))
		bulk.grid_columnconfigure(0, weight=1)
		self.bulk_status_var = tk.StringVar(
			value="Status: No bulk submission active."
		)
		bulk_status_lbl = tk.Label(bulk, textvariable=self.bulk_status_var,
			anchor="w", justify="left", wraplength=380)
		bulk_status_lbl.grid(row=0, column=0, sticky="we")
		bind_dynamic_wraplength(bulk_status_lbl, bulk)
		self.bulk_source_var = tk.StringVar(value="")
		# Source-path line is gridded into row 1 only when bulk is
		# active; bulk_source_lbl.grid_remove() hides it cleanly.
		self.bulk_source_lbl = tk.Label(bulk, textvariable=self.bulk_source_var,
			anchor="w", justify="left", wraplength=380, fg=PALETTE["fg_muted"])
		self.bulk_source_lbl.grid(row=1, column=0, sticky="we")
		self.bulk_source_lbl.grid_remove()
		bind_dynamic_wraplength(self.bulk_source_lbl, bulk)
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
		# Left column stack: Bulk Sample Submission (row 1) → Run
		# Parameters (row 2) → Fractionation Pump Parameters (row 3).
		# Plate Parameters spans rows 1-2 on the right so the right
		# column matches the left column's combined height.
		runp.grid(row=3, column=0, sticky="new", padx=(2, 4), pady=(0, 0))
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
		# Spans rows 1-2 on the right so the bulk panel can sit
		# between Run Parameters and Fractionation Pump Parameters on
		# the left without leaving a gap on this column.
		platep.grid(row=2, column=1, rowspan=2, sticky="new",
			padx=(4, 2), pady=(0, 2))
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

		self.table_te = TextEntry(platep, "Starting well position (x-axis; cm):")
		self.table_te.grid(row=4, column=0, sticky="we")
		Tooltip(
			self.table_te.entry,
			"X coordinate of well A1 in cm. Set via Manual mode's "
			"Position Calibration Tool.",
		)
		self.carriage_te = TextEntry(platep, "Starting well position (y-axis; cm):")
		self.carriage_te.grid(row=5, column=0, sticky="we")
		Tooltip(
			self.carriage_te.entry,
			"Y coordinate of well A1 in cm. Set via Manual mode's "
			"Position Calibration Tool.",
		)

		# Waste-bin position + extent fields have moved to
		# ``Tools → Cleaning Parameters``. Plate Parameters is now
		# pure plate geometry (rows, cols, well width, A1 X, A1 Y).
		# The four App-level StringVars (waste_bin_table_var,
		# waste_bin_carriage_var, waste_bin_x_extent_var,
		# waste_bin_y_extent_var) still drive get_values / set_values
		# via the StringVarHolder adapters in ``_entry_for``.
		self.waste_table_te = _StringVarHolder(app.waste_bin_table_var)
		self.waste_carriage_te = _StringVarHolder(app.waste_bin_carriage_var)
		self.waste_x_extent_te = _StringVarHolder(app.waste_bin_x_extent_var)
		self.waste_y_extent_te = _StringVarHolder(app.waste_bin_y_extent_var)

		# Focus-out normalization on the remaining Plate-area coords
		# (Starting Well Position X / Y) — reformat to 2 decimals.
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
		for _coord_te in (self.table_te, self.carriage_te):
			_normalize_coord_entry(_coord_te)

		# ----- Pump + Cleaning parameters (moved to Tools menu) ----------
		# The previous "Fractionation Pump Parameters" and "Cleaning
		# Parameters" LabelFrames have been removed from the Automated
		# panel. Their fields now live in two modal dialogs under
		# Tools → Pump Parameters… and Tools → Cleaning Parameters…
		# (see ``_show_pump_parameters_dialog`` /
		# ``_show_cleaning_parameters_dialog``).
		#
		# To keep the existing persistence pipeline working
		# (``_entry_for`` / ``get_values`` / ``set_values`` / profile
		# round-trip), we install ``_StringVarHolder`` stand-ins that
		# expose the same .get/.set surface as TextEntry while wrapping
		# the App-level StringVars the dialogs bind to.
		self.pump_rate_text_entry = _StringVarHolder(app.pump_rate_var)
		self.drip_wait_te = _StringVarHolder(app.drip_wait_time_var)
		self.prime_time_te = _StringVarHolder(app.prime_time_var)
		self.purge_time_te = _StringVarHolder(app.purge_time_var)
		self.peristaltic_rate_te = _StringVarHolder(app.peristaltic_rate_var)
		self.max_waste_te = _StringVarHolder(app.max_waste_volume_var)

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
		self.progress.grid(row=5, column=0, sticky="nsew")
		# Whole-XY-table view to the right of the plate canvas. The
		# plate sits as one element inside it, alongside the operator's
		# reference markers (origin, waste bin) and — added in later
		# phases — a real-time crosshair tracking the dispenser
		# position. Phase 1 scope: static table outline + plate + empty
		# wells. Markers, crosshair, resize handling, and polish land
		# in phases 2-5.
		self.table_view = TableView(self, min_width=360, min_height=240)
		self.table_view.grid(row=5, column=1, sticky="nsew", padx=(4, 0))
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

		# Plate preview + table view: render whenever the underlying
		# parameters validate, so the operator can use the canvases
		# as a placement guide before clicking Begin Fractionation.
		# ``_refresh_plate_preview`` (LEFT canvas) gates on the idle
		# state so it doesn't clobber a live run. ``_refresh_table_view``
		# (RIGHT canvas) is unconditional — its content is static
		# reference info (table outline, plate footprint, origin
		# marker, waste-bin marker) that's useful at any time. Tracing
		# the parameter vars catches both keyboard edits and
		# programmatic ``.set()`` from the labware loader / profiles.
		for _te in (self.rows_text_entry, self.cols_text_entry,
				self.ws_text_entry, self.table_te, self.carriage_te):
			_te.var.trace_add("write",
				lambda *_a: (self._refresh_plate_preview(),
					self._refresh_table_view()))
		for _te in (self.waste_table_te, self.waste_carriage_te,
				self.waste_x_extent_te, self.waste_y_extent_te):
			_te.var.trace_add("write",
				lambda *_a: self._refresh_table_view())
		# Initial render once the frame is fully constructed and the
		# App has finished init — defer one event loop tick so loaded
		# prefs have had a chance to populate the entries.
		self.after_idle(self._refresh_plate_preview)
		self.after_idle(self._refresh_table_view)
		# Phase 3: poll the motor positions every 100 ms and move the
		# table-view crosshair to match. ``update_position`` shifts
		# the existing crosshair items in place when possible, so the
		# 10 Hz tick doesn't flicker the rest of the canvas.
		self._table_view_poll_ms = 100
		self.after(self._table_view_poll_ms, self._poll_dispenser_position)

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
			"prime_time": self.prime_time_te,
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
			"waste_bin_x_extent": self.waste_x_extent_te,
			"waste_bin_y_extent": self.waste_y_extent_te,
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
				# Relocated fields use _StringVarHolder adapters with no
				# live widget (w.entry is None) — their FocusOut-save is
				# handled inside the Tools dialog's Save handler instead.
				if w is not None and getattr(w, "entry", None) is not None:
					w.entry.bind("<FocusOut>", self._on_field_focus_out, add="+")

	def _on_field_focus_out(self, _event=None):
		self._save_last_used()

	def _save_last_used(self):
		"""Persist the current field values to ``config.json``. Called from
		``<FocusOut>`` on inline widgets and from the Tools-menu dialog
		Save handlers, since the relocated fields no longer have inline
		widgets to fire <FocusOut>."""
		try:
			config_store.save_last_used(self.get_values())
		except OSError as exc:
			logger.warning("Failed to save last_used config: %s", exc)

	# Set of required-and-must-be-non-blank fields whose absence
	# triggers the first-launch hint banner. Mirrors the pre-flight
	# check in ``begin_clicked``.
	_BANNER_REQUIRED_FIELDS = (
		"pump_rate_text_entry", "drip_wait_te", "prime_time_te",
		"purge_time_te", "peristaltic_rate_te", "max_waste_te",
	)

	def _refresh_config_banner(self):
		"""Show/hide the Tools-menu hint banner based on whether the
		relocated required fields are populated. Re-runs after profile
		load, ``set_values``, and Tools-dialog Save."""
		banner = getattr(self, "config_hint_banner", None)
		if banner is None:
			return
		any_blank = any(
			not getattr(self, attr).get().strip()
			for attr in self._BANNER_REQUIRED_FIELDS
		)
		if any_blank:
			banner.grid()
		else:
			banner.grid_remove()

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

	def _plate_parameters_valid(self):
		"""Return the parsed ``(rows, cols, well_size, table_start,
		carriage_start)`` tuple if all five Plate Parameters fields
		validate; otherwise return ``None``. Used to gate the plate
		preview render.
		"""
		checks = (
			(self.rows_text_entry, validation.rows),
			(self.cols_text_entry, validation.cols),
			(self.ws_text_entry, validation.well_size),
			(self.table_te, validation.table_pos),
			(self.carriage_te, validation.carriage_pos),
		)
		parsed = []
		for te, fn in checks:
			ok, val = fn(te.get())
			if not ok:
				return None
			parsed.append(val)
		return tuple(parsed)

	def _refresh_plate_preview(self, *_):
		"""Re-render the LEFT plate-only preview from the current
		Plate Parameters entries. Fires on every parameter edit (via
		``trace_add``), on initial frame construction (deferred via
		``after_idle``), and on plate-orientation switches in
		Preferences. No-op while a run is in flight — the live
		visualization owns that canvas then.
		"""
		if self.app.state.state != "idle":
			return
		params = self._plate_parameters_valid()
		if params is None:
			self.progress.clear_preview()
			return
		rows_v, cols_v, _ws, _tx, _ty = params
		self.progress.show_preview(
			cols=cols_v, rows=rows_v,
			orientation=self.app.plate_orientation,
		)

	def _waste_bin_valid(self):
		"""Return ``(waste_table_cm, waste_carriage_cm, x_extent_cm,
		y_extent_cm)`` if the anchor entries validate, otherwise
		``None``. The extents default to ``0.0`` when blank or invalid
		(legacy point-target behaviour). Anchor + extent overhang
		against the table dimensions is checked separately at
		Begin Fractionation time so the TableView can still re-render
		mid-edit while the operator is still typing.
		"""
		ok_x, vx = validation.table_pos(self.waste_table_te.get())
		if not ok_x:
			return None
		ok_y, vy = validation.carriage_pos(self.waste_carriage_te.get())
		if not ok_y:
			return None
		# Extents: blank or unparseable → 0 (legacy point target).
		try:
			ext_x = float(self.waste_x_extent_te.get())
		except (TypeError, ValueError):
			ext_x = 0.0
		try:
			ext_y = float(self.waste_y_extent_te.get())
		except (TypeError, ValueError):
			ext_y = 0.0
		return (vx, vy, max(0.0, ext_x), max(0.0, ext_y))

	def _refresh_table_view(self, *_):
		"""Re-render every whole-table view (Automated mode's
		bottom-right canvas AND Manual mode's bottom-right canvas)
		from the current Plate Parameters and Waste Bin Position
		entries. Each view is static reference info, so this refresh
		runs UNCONDITIONALLY — including during a run — and walks
		every ``TableView`` instance the App knows about. The Phase 3
		crosshair sits on top of all that, driven independently by
		``_poll_dispenser_position``.
		"""
		plate = self._plate_parameters_valid()
		waste = self._waste_bin_valid()
		for tv in self.app.iter_table_views():
			if plate is None:
				tv.clear()
			else:
				rows_v, cols_v, ws_v, tx_v, ty_v = plate
				tv.set_plate(
					rows=rows_v, cols=cols_v, well_size_cm=ws_v,
					table_start_cm=tx_v, carriage_start_cm=ty_v,
					orientation=self.app.plate_orientation,
				)
			if waste is None:
				tv.clear_waste_bin()
			else:
				tv.set_waste_bin(*waste)
		# Stash the latest valid waste tuple on the App for the
		# routing helper to read at every move-to-waste call site.
		# ``_waste_entry_for_current_position`` short-circuits on
		# extent==0 so legacy point-target behaviour is preserved.
		if waste is not None:
			anchor_x, anchor_y, ext_x, ext_y = waste
			self.app.state.waste_bin_table = anchor_x
			self.app.state.waste_bin_carriage = anchor_y
			self.app.state.waste_bin_x_extent = ext_x
			self.app.state.waste_bin_y_extent = ext_y

	def _poll_dispenser_position(self):
		"""Read the current motor positions and push them into every
		table-view crosshair. Reschedules itself every
		``_table_view_poll_ms`` (100 ms by default). Updates run
		regardless of which mode tab is currently visible — Tk's
		``canvas.coords()`` is cheap on a hidden widget, and a single
		shared poll keeps Manual mode's canvas in sync with Automated's
		without a second timer fighting the first.
		"""
		try:
			tm = self.app.table_motor
			cm = self.app.carriage_motor
			x_cm = tm.get_angle() * tm.cm_per_deg
			# Motor Y reading is signed: it goes NEGATIVE as the
			# carriage moves south of origin (Y range [-15, 0]).
			# The canvas and every stored cm field (Starting Well,
			# Waste Bin) treat south as a positive distance from
			# the upper-left origin (range [0, 15]). Take abs() so
			# the crosshair tracks south-of-origin travel downward
			# on the canvas instead of flying off the top edge.
			y_cm = abs(cm.get_angle() * cm.cm_per_deg)
			for tv in self.app.iter_table_views():
				tv.update_position(x_cm, y_cm)
		except Exception as exc:
			# Defensive: motor backends might transiently raise during
			# tear-down or re-init. Swallow + log so a transient
			# failure doesn't kill the polling loop.
			logger.debug(
				"crosshair poll skipped: %s", exc)
		# Re-schedule even on error so a transient blip recovers.
		self.after(self._table_view_poll_ms,
			self._poll_dispenser_position)

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
		# path + inline JSON contents in system.start.state.json.
		self._loaded_labware_path = path
		self._loaded_labware_data = data

	def begin_clicked(self):
		"""Validate every Begin-time input; show inline + summary errors on
		failure; cross-check N/D and plate capacity; warn on waste/plate
		overlap; then dispatch to ``app.start_run``."""
		# Pre-flight: catch the common "operator launched without ever
		# opening the Tools dialogs" case BEFORE the per-field
		# validation pass, so the error message can route the operator
		# to the right Tools dialog instead of just saying "X is empty".
		# Required-and-blank fields are grouped by dialog so the
		# operator sees a single targeted prompt.
		pump_dialog_fields = [
			("pump_rate_text_entry", "Pump rate"),
			("drip_wait_te", "Drip wait time"),
			("prime_time_te", "Prime time"),
		]
		cleaning_dialog_fields = [
			("purge_time_te", "Purge time"),
			("peristaltic_rate_te", "Peristaltic pump rate"),
			("max_waste_te", "Max waste bin volume"),
		]
		pump_blank = [label for attr, label in pump_dialog_fields
			if not getattr(self, attr).get().strip()]
		clean_blank = [label for attr, label in cleaning_dialog_fields
			if not getattr(self, attr).get().strip()]
		if pump_blank or clean_blank:
			lines = []
			if pump_blank:
				lines.append(
					"Settings → Fractionation Parameters…\n  • "
					+ "\n  • ".join(pump_blank)
				)
			if clean_blank:
				lines.append(
					"Settings → Cleaning Parameters…\n  • "
					+ "\n  • ".join(clean_blank)
				)
			messagebox.showerror(
				"Cannot start fractionation",
				"Configure the following parameters before starting a "
				"run:\n\n" + "\n\n".join(lines),
				parent=self,
			)
			return
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
			(self.prime_time_te, validation.prime_time),
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
				drip_v, purge_v, peri_v, max_waste_v, prime_v) = parsed
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

			# Waste-bin extents (cm). Blank → 0. Validate ≥ 0 and that
			# anchor + extent fits inside the physical table (in cm:
			# TABLE_WIDTH_MM/10 × TABLE_HEIGHT_MM/10).
			wex_ok, wex_val = validation.waste_bin_extent(
				self.waste_x_extent_te.get(), allow_empty=True)
			wey_ok, wey_val = validation.waste_bin_extent(
				self.waste_y_extent_te.get(), allow_empty=True)
			if not wex_ok:
				self.waste_x_extent_te.show_error(wex_val)
				errors.append(wex_val)
			if not wey_ok:
				self.waste_y_extent_te.show_error(wey_val)
				errors.append(wey_val)
			waste_x_extent = wex_val if (wex_ok and wex_val is not None) else 0.0
			waste_y_extent = wey_val if (wey_ok and wey_val is not None) else 0.0
			# Rectangle bounds check. The bin is center-anchored, so
			# the rectangle occupies [center − extent/2, center + extent/2]
			# on each axis. Both edges must lie within the physical
			# table — overhang on either the low (north/west) side or
			# the high (south/east) side rejects the configuration.
			from well_plate import TABLE_WIDTH_MM, TABLE_HEIGHT_MM
			table_x_max_cm = TABLE_WIDTH_MM / 10.0
			table_y_max_cm = TABLE_HEIGHT_MM / 10.0
			if waste_x is not None and waste_x_extent > 0:
				lo = waste_x - waste_x_extent / 2.0
				hi = waste_x + waste_x_extent / 2.0
				if lo < -1e-6 or hi > table_x_max_cm + 1e-6:
					msg = (
						f"Waste bin X rectangle ({lo:.2f} → {hi:.2f} cm) "
						f"overhangs the table's [0.00, {table_x_max_cm:.2f}] "
						f"cm X range."
					)
					self.waste_x_extent_te.show_error(msg)
					errors.append(msg)
			if waste_y is not None and waste_y_extent > 0:
				lo = waste_y - waste_y_extent / 2.0
				hi = waste_y + waste_y_extent / 2.0
				if lo < -1e-6 or hi > table_y_max_cm + 1e-6:
					msg = (
						f"Waste bin Y rectangle ({lo:.2f} → {hi:.2f} cm) "
						f"overhangs the table's [0.00, {table_y_max_cm:.2f}] "
						f"cm Y range."
					)
					self.waste_y_extent_te.show_error(msg)
					errors.append(msg)

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
			waste_bin_x_extent=waste_x_extent,
			waste_bin_y_extent=waste_y_extent,
			table_start=table_v, carriage_start=carriage_v,
			drip_wait_time=drip_v,
			purge_time=purge_v,
			prime_time_s=prime_v,
			skip_intersample_purge=self.app.skip_intersample_purge_var.get(),
			peristaltic_rate_ml_per_min=peri_v,
			max_waste_volume_ml=max_waste_v,
		)

	# -- WellPlateProgress shortcuts (called by App's state machine) ----

	def begin_run(self, cols, rows, volume_per_well, pump_time):
		self.progress.begin_run(cols, rows, volume_per_well, pump_time,
			orientation=self.app.plate_orientation)

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
	#
	# X axis: signed range [0, 20] cm from origin (upper-left
	# mechanical limit). Y axis: ±15 cm of physical travel from
	# origin — kept as a magnitude rather than a signed range
	# because motor.angle for the carriage can sit in either of two
	# valid representations of the same physical position:
	#   * Manual jogs accumulate negative deltas → motor.angle in
	#     [-15, 0] (south as negative).
	#   * Absolute moves via ``move_to_positions`` (Return to Start
	#     Well, move-to-waste-bin, snake-step's outer carriage
	#     advance, ``s.carriage_start_cm`` runs) drive the motor
	#     with the operator-stored positive magnitude → motor.angle
	#     in [0, 15] (south as positive).
	# Both produce the same physical motor position; the soft-limit
	# check in ``_jog`` therefore compares ``abs(target_cm)`` against
	# ``_Y_TRAVEL_MAX`` so jogs work regardless of which sign
	# convention the motor currently sits in.
	_X_MIN, _X_MAX = 0.0, 20.0
	_Y_TRAVEL_MAX = 15.0

	def __init__(self, master, app):
		super().__init__(master)
		self.app = app

		# Two-column grid: top sections (banner / Jog / Pump) span both
		# columns; the two calibration LabelFrames stack vertically in
		# col 0 (Position Calibration Tool above Prime Time Calibration),
		# and the XY-table view occupies col 1 spanning their rows so
		# the operator can watch the crosshair while jogging. The
		# ``uniform="manual"`` group forces a strict 50/50 split — same
		# pattern AutomatedFrame uses on its own 2-column layout — so
		# the table view renders at identical size in both modes.
		self.grid_columnconfigure(0, weight=1, uniform="manual")
		self.grid_columnconfigure(1, weight=1, uniform="manual")

		# Run-active banner: gridded only while an Automated run is in
		# flight (managed by set_run_active_lock). Amber background so
		# the operator's eye lands on it before reaching the (now
		# greyed-out) jog buttons below.
		self.run_active_banner = tk.Label(
			self, anchor="w", justify="left",
			bg="#fff3cd", fg="#7a5d00",
			padx=8, pady=4,
			text=(
				"Automated run in progress — controls disabled to "
				"prevent interference. Return to Automated tab to manage "
				"the run."
			),
		)
		# Stays unmapped until set_run_active_lock(True) is called.
		self.run_active_banner.grid(row=0, column=0, columnspan=2,
			sticky="we", padx=4, pady=(4, 0))
		self.run_active_banner.grid_remove()
		# Track the panel width on every <Configure> so the banner
		# stays single-line at any sensible width and degrades
		# gracefully at extreme narrow widths instead of being capped
		# by a static wraplength that's too small for the sentence.
		bind_dynamic_wraplength(self.run_active_banner, self)

		# ---- Jog Controls ----
		# Row 1 (was row 0 -- banner now occupies row 0). Spans both
		# columns so the directional pad + step radios stay full-width.
		jog = tk.LabelFrame(self, text="Jog Controls", padx=8, pady=8)
		jog.grid(row=1, column=0, columnspan=2, sticky="new", padx=4, pady=4)
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
		# Jog buttons drive the motors in fixed physical directions
		# regardless of plate orientation: +X moves east, +Y moves
		# south (away from the upper-left origin), and their inverses
		# the opposite way. Tooltip text is set once via
		# ``refresh_jog_tooltips``.
		self.y_plus_btn = ttk.Button(
			pad, text="▲ Y+", width=8,
			command=lambda: self._jog("y", +1),
		)
		self.y_plus_btn.grid(row=0, column=1, padx=2, pady=2)
		self._y_plus_tooltip = Tooltip(self.y_plus_btn, "")
		self.x_minus_btn = ttk.Button(
			pad, text="◀ X−", width=8,
			command=lambda: self._jog("x", -1),
		)
		self.x_minus_btn.grid(row=1, column=0, padx=2, pady=2)
		Tooltip(self.x_minus_btn, "Jog one step in the −X direction (left).")
		self.x_plus_btn = ttk.Button(
			pad, text="X+ ▶", width=8,
			command=lambda: self._jog("x", +1),
		)
		self.x_plus_btn.grid(row=1, column=2, padx=2, pady=2)
		Tooltip(self.x_plus_btn, "Jog one step in the +X direction (right).")
		self.y_minus_btn = ttk.Button(
			pad, text="Y− ▼", width=8,
			command=lambda: self._jog("y", -1),
		)
		self.y_minus_btn.grid(row=2, column=1, padx=2, pady=2)
		self._y_minus_tooltip = Tooltip(self.y_minus_btn, "")
		self.refresh_jog_tooltips()

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
		pump.grid(row=2, column=0, columnspan=2, sticky="new", padx=4, pady=(0, 4))
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
		# Row 3 col 0 — Prime Time Calibration stacks below it at row 4.
		cal.grid(row=3, column=0, sticky="new", padx=(4, 2), pady=(0, 4))
		cal.grid_columnconfigure(0, weight=1)

		pos_cal_desc = tk.Label(cal, anchor="w", justify="left",
			wraplength=320, text=(
				"Use the jog controls above to position the needle, then "
				"click the corresponding button to save the current "
				"position as a parameter used by Automated mode."
			))
		pos_cal_desc.grid(row=0, column=0, sticky="we", pady=(0, 6))
		bind_dynamic_wraplength(pos_cal_desc, cal)

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
			text="2. Position the needle over the CENTER of the waste bin.",
		).grid(row=4, column=0, sticky="w")
		self.save_waste_btn = save_waste_btn = primary_button(
			cal, text="Save as Waste Bin Position",
			command=self._save_waste_bin_position,
		)
		save_waste_btn.grid(row=5, column=0, sticky="w", padx=(20, 0), pady=(2, 0))
		Tooltip(
			save_waste_btn,
			"Write the current motor position to the Waste bin position "
			"(x-axis / y-axis) fields shared with Cleaning mode. The bin "
			"extends ± extent/2 around this center point.",
		)

		# ---- Prime Time Calibration ------------------------------------
		# Measures how long it takes the sample fractionation solution to
		# walk from the sample tube up the inlet line to ~5 cm below the
		# syringe dispenser. Saves the result to app.prime_time_var so
		# Automated mode's Prime time field picks it up immediately.
		prime_cal = tk.LabelFrame(self, text="Prime Time Calibration",
			padx=8, pady=8)
		# Row 4 col 0 — stacks directly below Position Calibration Tool;
		# col 1 is given over to the table view below.
		prime_cal.grid(row=4, column=0, sticky="new", padx=(4, 2), pady=(0, 4))
		prime_cal.grid_columnconfigure(0, weight=1)

		prime_cal_desc = tk.Label(prime_cal, anchor="w", justify="left",
			wraplength=320, text=(
				"Connect a sample tube, click Start, and watch the line "
				"as solution walks toward the dispenser. Click Stop when "
				"the solution reaches ~5 cm below the needle. Save to "
				"apply as Prime time in Run Parameters."
			))
		prime_cal_desc.grid(row=0, column=0, sticky="we", pady=(0, 6))
		bind_dynamic_wraplength(prime_cal_desc, prime_cal)

		self._prime_cal_elapsed_var = tk.StringVar(value="Elapsed: 0.0 s")
		tk.Label(prime_cal, textvariable=self._prime_cal_elapsed_var,
			anchor="w").grid(row=1, column=0, sticky="we")

		prime_cal_btn_row = tk.Frame(prime_cal)
		prime_cal_btn_row.grid(row=2, column=0, sticky="w", pady=(4, 0))
		self.prime_cal_start_btn = ttk.Button(prime_cal_btn_row, text="Start",
			command=self._prime_cal_start, style="Primary.TButton")
		self.prime_cal_start_btn.pack(side=tk.LEFT, padx=(0, 4))
		Tooltip(self.prime_cal_start_btn,
			"Start the fractionation pump and the elapsed-time timer. "
			"Click Stop when the solution reaches ~5 cm below the needle.")
		self.prime_cal_stop_btn = ttk.Button(prime_cal_btn_row, text="Stop",
			command=self._prime_cal_stop)
		self.prime_cal_stop_btn.pack(side=tk.LEFT, padx=4)
		Tooltip(self.prime_cal_stop_btn,
			"Stop the pump and freeze the elapsed time as the measured "
			"prime duration.")
		self.prime_cal_reset_btn = ttk.Button(prime_cal_btn_row, text="Reset",
			command=self._prime_cal_reset)
		self.prime_cal_reset_btn.pack(side=tk.LEFT, padx=4)
		Tooltip(self.prime_cal_reset_btn,
			"Clear the measured value back to 0.0 s.")
		self.prime_cal_save_btn = ttk.Button(prime_cal_btn_row,
			text="Save", command=self._prime_cal_save,
			style="Primary.TButton")
		self.prime_cal_save_btn.pack(side=tk.LEFT, padx=4)
		Tooltip(self.prime_cal_save_btn,
			"Write the measured time (rounded to the nearest second) "
			"into Automated mode's Prime time field.")

		# Calibration state
		self._prime_cal_t_start = None
		self._prime_cal_measured_s = 0.0
		self._prime_cal_tick_after = None
		self._prime_cal_set_buttons_idle()

		# ---- XY-table view ---------------------------------------------
		# Same widget class as Automated mode's bottom-right canvas, so
		# the operator can see the dispenser's position relative to the
		# table and plate while jogging in Manual mode. Driven by the
		# same trace_add + polling pipeline AutomatedFrame uses — its
		# refresh methods walk ``app.iter_table_views()`` so any change
		# in Plate Parameters / Waste Bin Position / motor position
		# repaints both canvases together.
		self.table_view = TableView(self, min_width=360, min_height=240)
		# The left column has two calibration panels (rows 3, 4) anchored
		# top (sticky="new") so they stack flush. Vertical slack lives
		# in a weighted spacer row (row 5) BELOW them — not between
		# them. The table view spans rows 3–5 on the right so it still
		# fills the full column height while col 0 keeps a tight stack.
		self.table_view.grid(row=3, column=1, rowspan=3, sticky="nsew",
			padx=(2, 4), pady=(0, 4))
		self.grid_rowconfigure(3, weight=0)
		self.grid_rowconfigure(4, weight=0)
		self.grid_rowconfigure(5, weight=1)
		# Initial paint from the latest state so the canvas isn't empty
		# the first time the user switches to Manual mode. Deferred so
		# the canvas has been laid out before the redraw fires.
		af = getattr(self.app, "automated_frame", None)
		if af is not None and hasattr(af, "_refresh_table_view"):
			self.after_idle(af._refresh_table_view)

	# -- Prime Time Calibration helpers ----------------------------------

	def _prime_cal_set_buttons_idle(self):
		"""All buttons in their non-running state. Start enabled iff the
		Automated run is idle; Stop disabled (nothing to stop); Reset
		enabled; Save enabled iff a non-zero measurement is present."""
		if self.app._automated_run_active():
			self.prime_cal_start_btn.state(["disabled"])
		else:
			self.prime_cal_start_btn.state(["!disabled"])
		self.prime_cal_stop_btn.state(["disabled"])
		self.prime_cal_reset_btn.state(["!disabled"])
		self._prime_cal_save_button_sync()

	def _prime_cal_save_button_sync(self):
		"""Save enabled only when a measurement > 0 is on display."""
		if self._prime_cal_measured_s and self._prime_cal_measured_s > 0:
			self.prime_cal_save_btn.state(["!disabled"])
		else:
			self.prime_cal_save_btn.state(["disabled"])

	def _prime_cal_start(self):
		"""Begin the calibration: fire the same pump-confirmation flow
		Fractionate uses, then start the elapsed-time tick. Reuses
		_handle_pump_click so the once-per-session relay-activation
		prompt fires exactly as it does for the Fractionate button.
		"""
		pc = self.app.pump_controller
		# Bail if anything else is driving the pump.
		if not pc.is_available_for("fractionate"):
			messagebox.showinfo(
				"Pump in use",
				"The Purge claim is currently active. Stop it before "
				"running the prime calibration timer.",
				parent=self,
			)
			return
		# Bail if an Automated run is in progress -- the button should
		# already be disabled in that case, but defend.
		if self.app._automated_run_active():
			return
		# Route through the standard pump click so the confirmation
		# dialog fires the first time per session. _handle_pump_click
		# claims fractionate and turns on the relay; we then start
		# the timer once the relay is actually on.
		self.app._handle_pump_click("fractionate", parent=self)
		if not pc.relay_on:
			# Confirmation declined; nothing to time.
			return
		self._prime_cal_t_start = monotonic()
		self.prime_cal_start_btn.state(["disabled"])
		self.prime_cal_reset_btn.state(["disabled"])
		self.prime_cal_save_btn.state(["disabled"])
		self.prime_cal_stop_btn.state(["!disabled"])
		self._prime_cal_tick()

	def _prime_cal_tick(self):
		if self._prime_cal_t_start is None:
			return
		elapsed = monotonic() - self._prime_cal_t_start
		self._prime_cal_elapsed_var.set(f"Elapsed: {elapsed:.1f} s")
		self._prime_cal_tick_after = self.after(
			100, self._prime_cal_tick)

	def _prime_cal_stop(self):
		"""Stop the pump + freeze the elapsed value as the measurement."""
		if self._prime_cal_t_start is None:
			return
		elapsed = monotonic() - self._prime_cal_t_start
		self._prime_cal_t_start = None
		if self._prime_cal_tick_after is not None:
			try:
				self.after_cancel(self._prime_cal_tick_after)
			except Exception:
				pass
			self._prime_cal_tick_after = None
		pc = self.app.pump_controller
		if pc.claimant == "fractionate" and pc.relay_on:
			pc.set_relay(False)
			pc.release()
		self._prime_cal_measured_s = elapsed
		self._prime_cal_elapsed_var.set(f"Elapsed: {elapsed:.1f} s")
		self._prime_cal_set_buttons_idle()

	def _prime_cal_reset(self):
		"""Clear the measurement back to 0.0 s. If the timer is running,
		_cal_stop should have been clicked first -- defensively stop
		the pump here too so we never leave the relay on by accident."""
		if self._prime_cal_t_start is not None:
			self._prime_cal_stop()
		self._prime_cal_measured_s = 0.0
		self._prime_cal_elapsed_var.set("Elapsed: 0.0 s")
		self._prime_cal_set_buttons_idle()

	def _prime_cal_save(self):
		"""Write the measured value to App.prime_time_var so Automated
		mode's Prime time entry picks it up. Rounded to the nearest
		integer second per the spec."""
		if not self._prime_cal_measured_s:
			return
		rounded = int(round(self._prime_cal_measured_s))
		self.app.prime_time_var.set(str(rounded))
		try:
			config_store.save_last_used(
				self.app.automated_frame.get_values())
		except OSError as exc:
			logger.warning("Failed to persist prime_time: %s", exc)
		messagebox.showinfo(
			"Prime time saved",
			f"Saved {rounded} s as Prime time.",
			parent=self,
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
		# Diagnostic: log the direction state on every Y jog so a
		# regression of the post-run Y-inversion bug is observable in
		# --debug output. The motor's actual rotation direction comes
		# from sign(step_cm) + motor.reverse, NOT from motor.forwards
		# — but logging both makes it easy to spot if anyone ever
		# re-introduces a dependency.
		logger.debug(
			"_jog axis=%s sign=%+d step_cm=%+.4f  "
			"carriage_motor.forwards=%s carriage_motor.reverse=%s",
			axis, sign, step_cm,
			self.app.carriage_motor.forwards,
			self.app.carriage_motor.reverse,
		)
		if axis == "x":
			motor = self.app.table_motor
			current_cm = motor.get_angle() * motor.cm_per_deg
			target_cm = current_cm + step_cm
			if target_cm < self._X_MIN:
				self.app.set_status(
					f"X-axis at soft limit: {self._X_MIN:.1f} cm")
				return
			if target_cm > self._X_MAX:
				self.app.set_status(
					f"X-axis at soft limit: {self._X_MAX:.1f} cm")
				return
		else:
			# Y soft limit compares the magnitude against the 15 cm of
			# physical travel — see the class-level comment on
			# ``_Y_TRAVEL_MAX``. Both signed conventions for motor.angle
			# are tolerated because both represent the same physical
			# position; only the magnitude matters for "can we still
			# move this far without overshooting the lead-screw."
			motor = self.app.carriage_motor
			current_cm = motor.get_angle() * motor.cm_per_deg
			target_cm = current_cm + step_cm
			if abs(target_cm) > self._Y_TRAVEL_MAX:
				self.app.set_status(
					f"Y-axis at soft limit: "
					f"{self._Y_TRAVEL_MAX:.1f} cm")
				return

		# Manual jogs are transit moves — operator is positioning the
		# needle, not dispensing fluid mid-pump. Variable speed mode
		# speeds them up; Slow mode keeps them at the fractionation
		# cadence (variable_speed_enabled gates the choice).
		motor.move_dist_relative(step_cm, is_transit=True)
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

	def refresh_jog_tooltips(self):
		"""Set the Y+/Y− tooltips. The Manual jog buttons drive the motors
		in fixed physical directions regardless of plate orientation —
		origin (0, 0) is always the upper-left mechanical limit; +X
		always moves east, +Y always moves south. Kept as a method (not
		a one-shot at construction) so the rest of the App can call it
		uniformly; orientation is no longer consulted.
		"""
		y_plus = (
			"Jog one step in the +Y direction (carriage moves south, "
			"away from the upper-left mechanical limit, toward the plate)."
		)
		y_minus = (
			"Jog one step in the −Y direction (carriage moves north, "
			"toward the upper-left mechanical limit; refused at the limit)."
		)
		self._y_plus_tooltip.text = y_plus
		self._y_minus_tooltip.text = y_minus

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
		# Prime Time Calibration: refresh its button states via the
		# same idle/active discriminator. Don't touch Stop/Save here;
		# the cal helper recomputes everything based on whether a
		# measurement is loaded.
		self._prime_cal_set_buttons_idle()


class CleaningFrame(tk.Frame):
	"""Cleaning mode: move the needle to the waste bin and run the Purge
	pump to flush the lines. The waste-bin coords are the SAME ones the
	operator entered in Automated mode (shared via App-level StringVars),
	so the waste container's physical position is configured once."""

	def __init__(self, master, app):
		super().__init__(master)
		self.app = app

		# Two-column grid with equal weights so the Waste bin and the
		# Purge Time Calibration Tool split width evenly on row 1,
		# and so the Move/Purge buttons below them stay aligned with
		# their respective panels.
		# ``uniform="cleaning_cols"`` forces the two columns to share
		# the same display width so the Move-to-Waste / Purge button
		# pair on row 2 — which uses ``sticky="nsew"`` — renders at
		# identical pixel size and stays equal as the window resizes.
		self.grid_columnconfigure(0, weight=1, uniform="cleaning_cols")
		self.grid_columnconfigure(1, weight=1, uniform="cleaning_cols")

		# Run-active banner: gridded only while an Automated run is
		# in flight. Spans both columns and sits at the very top so
		# the operator's eye lands on the warning before the locked
		# controls below.
		self.run_active_banner = tk.Label(
			self, anchor="w", justify="left",
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
		# Same dynamic-wraplength treatment as ManualFrame's banner:
		# track the panel width so the single-line sentence isn't
		# capped by a static wraplength that's too small for the text.
		bind_dynamic_wraplength(self.run_active_banner, self)

		# Waste-bin coords -- bound to the same App-level StringVars
		# as Automated mode's Waste bin entries, so an edit in either
		# mode propagates automatically. Row 1 col 0, paired with the
		# Purge Time Calibration Tool in col 1; both sticky=nsew so
		# the row's height tracks the taller panel.
		bin_frame = tk.LabelFrame(self, text="Waste bin", padx=8, pady=4)
		bin_frame.grid(row=1, column=0, sticky="nsew",
			padx=(2, 2), pady=(2, 4))
		bin_frame.grid_columnconfigure(0, weight=1)
		self.waste_table_te = TextEntry(
			bin_frame, "Waste bin position (x-axis, center):",
			textvariable=app.waste_bin_table_var,
		)
		self.waste_table_te.grid(row=0, column=0, sticky="we")
		Tooltip(
			self.waste_table_te.entry,
			"X coordinate of the bin's CENTER. Mirrors Tools → Cleaning "
			"Parameters; edits propagate in both directions.",
		)
		self.waste_carriage_te = TextEntry(
			bin_frame, "Waste bin position (y-axis, center):",
			textvariable=app.waste_bin_carriage_var,
		)
		self.waste_carriage_te.grid(row=1, column=0, sticky="we")
		Tooltip(
			self.waste_carriage_te.entry,
			"Y coordinate of the bin's CENTER. Mirrors Tools → Cleaning "
			"Parameters; edits propagate in both directions.",
		)

		# Compact bin-size row: "Bin size: X [__] cm  Y [__] cm". Bound
		# to the SAME App-level StringVars as Tools → Cleaning
		# Parameters' X-extent / Y-extent fields so edits propagate
		# automatically; AutomatedFrame's existing trace_add on these
		# vars fires _refresh_table_view so the XY-table rectangle
		# repaints live as the operator types. These are secondary
		# controls (not primary cleaning actions), so the row is one
		# line of compact labelled entries — no sub-LabelFrame.
		size_row = tk.Frame(bin_frame)
		size_row.grid(row=2, column=0, sticky="we", pady=(4, 0))
		tk.Label(size_row, text="Bin size:", anchor="w").grid(
			row=0, column=0, padx=(0, 6))
		tk.Label(size_row, text="X").grid(row=0, column=1, padx=(0, 2))
		self.waste_x_extent_te = TextEntry(
			size_row, "", textvariable=app.waste_bin_x_extent_var,
		)
		self.waste_x_extent_te.label.grid_remove()
		self.waste_x_extent_te.entry.configure(width=6)
		self.waste_x_extent_te.grid(row=0, column=2, sticky="w")
		tk.Label(size_row, text="cm").grid(row=0, column=3, padx=(2, 10))
		tk.Label(size_row, text="Y").grid(row=0, column=4, padx=(0, 2))
		self.waste_y_extent_te = TextEntry(
			size_row, "", textvariable=app.waste_bin_y_extent_var,
		)
		self.waste_y_extent_te.label.grid_remove()
		self.waste_y_extent_te.entry.configure(width=6)
		self.waste_y_extent_te.grid(row=0, column=5, sticky="w")
		tk.Label(size_row, text="cm").grid(row=0, column=6, padx=(2, 0))
		Tooltip(
			self.waste_x_extent_te.entry,
			"Full width of the bin rectangle along X. The rectangle "
			"spans ± extent/2 around the center. Mirrors Tools → "
			"Cleaning Parameters; edits propagate in both directions.",
		)
		Tooltip(
			self.waste_y_extent_te.entry,
			"Full height of the bin rectangle along Y. The rectangle "
			"spans ± extent/2 around the center. Mirrors Tools → "
			"Cleaning Parameters; edits propagate in both directions.",
		)
		# Live inline validation on every keystroke or programmatic
		# .set(): runs the same validators the Tools dialog uses,
		# surfaces the inline error indicator on the offending entry,
		# clears it once the input parses inside [center − extent/2,
		# center + extent/2] inside the physical table.
		for _te in (self.waste_table_te, self.waste_carriage_te,
				self.waste_x_extent_te, self.waste_y_extent_te):
			_te.var.trace_add(
				"write",
				lambda *_a, _self=self: _self._refresh_waste_bin_errors(),
			)
		# Run once at construction so any stale invalid value from
		# config.json shows its indicator right away.
		self.after_idle(self._refresh_waste_bin_errors)

		# Purge Time Calibration sub-panel. Measures how long wash
		# takes to fully replace one tubing-volume so the operator
		# can save the result as Automated mode's Purge time
		# parameter. Row 1 col 1, paired with the Waste bin panel in
		# col 0; same sticky=nsew so they share a row height.
		cal = tk.LabelFrame(self, text="Purge Time Calibration Tool",
			padx=8, pady=6)
		cal.grid(row=1, column=1, sticky="nsew",
			padx=(2, 2), pady=(2, 4))
		cal.grid_columnconfigure(0, weight=1)

		purge_cal_desc = tk.Label(cal, anchor="w", justify="left",
			wraplength=280, text=(
				"Measure how long it takes wash solution to flow through "
				"your tubing setup. The result can be saved as the Purge "
				"time parameter used by Automated mode.\n"
				"  1. Place the inlet line in your wash solution container.\n"
				"  2. Click Start. The pump runs and a timer begins.\n"
				"  3. Watch the outlet. Click Stop the moment wash first "
				"appears at the outlet — this represents one full tubing volume."
			))
		purge_cal_desc.grid(row=0, column=0, sticky="we", pady=(0, 6))
		bind_dynamic_wraplength(purge_cal_desc, cal)

		# Move to Waste Bin + Purge side-by-side on row 2. The Move
		# button stays vertically aligned with the Waste bin panel
		# above it (col 0); the Purge button lands beneath the
		# Calibration Tool (col 1) so the operator's eye reads
		# Calibrate → run a Purge in one downward sweep.
		self.move_btn = primary_button(
			self, text="Move to Waste Bin", command=self.move_clicked,
		)
		self.move_btn.grid(row=2, column=0, sticky="nsew", padx=2, pady=2)
		Tooltip(
			self.move_btn,
			"Drive the needle to the Waste bin coordinates above so "
			"you can flush wash through the tubing into the bin.",
		)
		# Purge button. The (Space) hint was removed for visual
		# cleanliness; the Space-bar shortcut still toggles Purge in
		# Cleaning mode via the App-level key binding.
		self.purge_btn = ttk.Button(
			self, text="Purge: OFF",
			command=lambda: app._handle_pump_click("purge", parent=self),
			style="PumpOff.TButton", cursor="hand2",
		)
		self.purge_btn.grid(row=2, column=1, sticky="nsew", padx=2, pady=2)
		Tooltip(
			self.purge_btn,
			"Toggle the peristaltic pump for a free-form cleaning purge. "
			"Watch the tubing and click again to stop when the line "
			"reads clean. Space-bar shortcut active in this mode.",
		)

		# Live elapsed-time readout for the manual Purge. Driven by
		# the pump-state callback in ``refresh_pump_buttons`` — works
		# whether Purge was toggled by the button or the Space key.
		# Sits below the Move/Purge row in its own row, full width,
		# so the operator's eye drops naturally from the Purge button
		# to its timing readout.
		self.purge_time_lbl = tk.Label(
			self, text="Purge time: 0.0 s",
			fg="gray40", anchor="center",
		)
		self.purge_time_lbl.grid(row=3, column=0, columnspan=2,
			sticky="we", pady=(2, 4))

		# Purge-timer bookkeeping. ``_purge_was_on`` tracks the last
		# observed relay-on state so refresh_pump_buttons fires the
		# start / freeze transitions exactly once per ON→OFF cycle.
		self._purge_timer_start_mono = None
		self._purge_timer_after = None
		self._purge_was_on = False

		# ---- System Clean header (prominent pink button + icons) ----
		# Row 4 at the bottom: a centered three-widget trio — mop on
		# the left, the pink "System Clean" button in the middle,
		# bucket on the right. The trio is wrapped in a header frame
		# whose own row is sticky="" so the whole group sits centered
		# horizontally, growing left/right uniformly on resize.
		sysclean_header = tk.Frame(self)
		sysclean_header.grid(row=4, column=0, columnspan=2,
			sticky="", padx=2, pady=(8, 4))
		self.sysclean_mop_canvas = make_mop_canvas(sysclean_header, size=60)
		self.sysclean_mop_canvas.grid(row=0, column=0, padx=(0, 10))
		# tk.Button (not ttk) so we can directly drive bg / activebg
		# / font / padx / pady — the role here is one-off prominent
		# header, not the standard role-styled buttons.
		self.sysclean_btn = tk.Button(
			sysclean_header, text="System Clean",
			command=self.system_clean_clicked,
			bg="#26A69A", activebackground="#00897B",
			fg="#FFFFFF", activeforeground="#FFFFFF",
			font=(FONTS["family"], FONTS["size"] + 4, "bold"),
			padx=20, pady=12, bd=1, cursor="hand2",
			relief="solid",
		)
		self.sysclean_btn.grid(row=0, column=1)
		Tooltip(
			self.sysclean_btn,
			"Four-phase decontamination: pump bleach, soak, then "
			"double water rinse. Use at session start or during a "
			"paused run for a stringent line clean.",
		)
		self.sysclean_bucket_canvas = make_bucket_canvas(
			sysclean_header, size=60)
		self.sysclean_bucket_canvas.grid(row=0, column=2, padx=(10, 0))

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

	def _refresh_waste_bin_errors(self):
		"""Validate the four shared waste-bin StringVars and surface an
		inline red error indicator next to whichever entry is currently
		invalid. Runs on every keystroke in either the Cleaning Mode
		panel or the Tools → Cleaning Parameters dialog (since both
		surfaces edit the same vars), keeping the two views in sync
		without manual callbacks.

		Validation chain:
		  1. Each axis position / extent parses through its individual
			 validator (``validation.table_pos`` / ``.carriage_pos`` /
			 ``.waste_bin_extent`` with ``allow_empty=True``).
		  2. If both position + extent on an axis parse, the rectangle
			 edges (``center ± extent/2``) must lie inside the physical
			 table — otherwise the offending extent entry surfaces an
			 overhang error.
		"""
		from well_plate import TABLE_WIDTH_MM, TABLE_HEIGHT_MM
		table_x_max = TABLE_WIDTH_MM / 10.0
		table_y_max = TABLE_HEIGHT_MM / 10.0

		# Per-axis position validation.
		x_ok, x_val = validation.table_pos(
			self.waste_table_te.get(), allow_empty=True)
		y_ok, y_val = validation.carriage_pos(
			self.waste_carriage_te.get(), allow_empty=True)
		if x_ok:
			self.waste_table_te.clear_error()
		else:
			self.waste_table_te.show_error(x_val)
		if y_ok:
			self.waste_carriage_te.clear_error()
		else:
			self.waste_carriage_te.show_error(y_val)

		# Per-axis extent validation.
		ex_ok, ex_val = validation.waste_bin_extent(
			self.waste_x_extent_te.get(), allow_empty=True)
		ey_ok, ey_val = validation.waste_bin_extent(
			self.waste_y_extent_te.get(), allow_empty=True)

		# Overhang check: center ± extent/2 must lie inside the table.
		# Only fires when both the center coord and the extent on the
		# same axis parsed cleanly AND the extent is non-zero.
		def _overhang(axis_label, center, extent, axis_max):
			if center is None or extent is None or extent <= 0:
				return None
			lo = center - extent / 2.0
			hi = center + extent / 2.0
			if lo < -1e-6 or hi > axis_max + 1e-6:
				return (
					f"{axis_label} rectangle ({lo:.2f} → {hi:.2f} cm) "
					f"overhangs [0.00, {axis_max:.2f}] cm."
				)
			return None

		x_overhang = _overhang(
			"Waste bin X", x_val if x_ok else None,
			ex_val if ex_ok else None, table_x_max)
		y_overhang = _overhang(
			"Waste bin Y", y_val if y_ok else None,
			ey_val if ey_ok else None, table_y_max)

		if not ex_ok:
			self.waste_x_extent_te.show_error(ex_val)
		elif x_overhang is not None:
			self.waste_x_extent_te.show_error(x_overhang)
		else:
			self.waste_x_extent_te.clear_error()
		if not ey_ok:
			self.waste_y_extent_te.show_error(ey_val)
		elif y_overhang is not None:
			self.waste_y_extent_te.show_error(y_overhang)
		else:
			self.waste_y_extent_te.clear_error()

	def refresh_pump_buttons(self, claimant, relay_on, in_run):
		_update_pump_button(self.purge_btn, "purge", claimant, relay_on, in_run)
		self._refresh_sysclean_gate()
		self._sync_purge_timer(claimant, relay_on)

	def _sync_purge_timer(self, claimant, relay_on):
		"""Start / freeze the manual-Purge elapsed-time readout based
		on the pump-state callback. Fires on every PumpController
		transition; uses ``_purge_was_on`` to detect ON→OFF / OFF→ON
		edges so the start anchor and tick are managed exactly once.

		The label tracks ALL ``"purge"``-claim relay-ons regardless of
		whether they were triggered from Cleaning Mode or Manual
		Mode — both routes flow through the same PumpController, and
		the operator wants a single readout for "how long the
		peristaltic pump has been running". Fractionate-claim
		activity is ignored.
		"""
		purge_now_on = (claimant == "purge" and bool(relay_on))
		if purge_now_on and not self._purge_was_on:
			# OFF → ON: reset and start ticking.
			self._purge_timer_start_mono = monotonic()
			self.purge_time_lbl.config(
				text="Purge time: 0.0 s", fg="#1e7d20")
			self._schedule_purge_tick()
		elif self._purge_was_on and not purge_now_on:
			# ON → OFF: cancel the tick and freeze the label at the
			# final elapsed value (computed from the captured anchor
			# rather than the just-cancelled tick so we don't truncate
			# a few hundred milliseconds).
			self._cancel_purge_tick()
			if self._purge_timer_start_mono is not None:
				final_s = monotonic() - self._purge_timer_start_mono
				self.purge_time_lbl.config(
					text=f"Purge time: {final_s:.1f} s",
					fg="gray40",
				)
			self._purge_timer_start_mono = None
		self._purge_was_on = purge_now_on

	def _schedule_purge_tick(self):
		if self._purge_timer_after is not None:
			return
		self._purge_timer_after = self.after(100, self._purge_tick)

	def _cancel_purge_tick(self):
		if self._purge_timer_after is not None:
			try:
				self.after_cancel(self._purge_timer_after)
			except Exception:
				pass
			self._purge_timer_after = None

	def _purge_tick(self):
		"""100 ms tick that updates the live elapsed-time label.
		Guarded by ``_purge_timer_start_mono`` so a late tick after
		``_cancel_purge_tick`` is a no-op."""
		self._purge_timer_after = None
		if self._purge_timer_start_mono is None:
			return
		elapsed = monotonic() - self._purge_timer_start_mono
		self.purge_time_lbl.config(
			text=f"Purge time: {elapsed:.1f} s")
		self._purge_timer_after = self.after(100, self._purge_tick)

	def _refresh_sysclean_gate(self):
		"""Enable System Clean when no automated run is active OR the
		run is operator-paused. Disable during active (non-paused)
		dispensing. Idempotent."""
		s = self.app.state
		paused = bool(s.is_paused)
		active_dispense = (s.phase != "idle") and not paused
		self.sysclean_btn["state"] = (
			tk.DISABLED if active_dispense else tk.NORMAL)

	def set_run_active_lock(self, active):
		"""Disable every Cleaning control that could interfere with an
		active Automated run, and grid the active-run banner. Waste-bin
		coordinate entries are made read-only because their Tk variable
		is shared with Automated mode's Waste bin entries -- a mid-run
		edit here would silently redirect the live run's waste target.

		The System Clean button is NOT disabled by this lock — it has
		its own gating (``_refresh_sysclean_gate``) that keeps it
		clickable during a paused run.
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
		# System Clean stays clickable during a paused run.
		self._refresh_sysclean_gate()

	def system_clean_clicked(self):
		"""Launch the 5-phase System Clean routine. Re-checks the gate
		(button could have been clicked just before a non-paused run
		state landed) and delegates to App._start_system_clean."""
		s = self.app.state
		paused = bool(s.is_paused)
		if s.phase != "idle" and not paused:
			messagebox.showinfo(
				"Run in progress",
				"System Clean cannot be started while the automated run "
				"is actively dispensing. Pause the run first.",
				parent=self,
			)
			return
		self.app._start_system_clean(launched_during_pause=paused)

	def move_clicked(self):
		"""Move the needle to the closest entry point inside the waste
		bin rectangle. Falls back to the bin anchor when extents are
		zero (legacy point-target behaviour). Both anchor coords are
		validated; either being out-of-range surfaces inline + halts
		the move. Empty fields are allowed (only the populated axis
		moves), and the routing only fires when BOTH anchor values
		are present."""
		t_ok, t_val = validation.table_pos(self.waste_table_te.get(), allow_empty=True)
		c_ok, c_val = validation.carriage_pos(self.waste_carriage_te.get(), allow_empty=True)
		(self.waste_table_te.clear_error if t_ok else lambda: self.waste_table_te.show_error(t_val))()
		(self.waste_carriage_te.clear_error if c_ok else lambda: self.waste_carriage_te.show_error(c_val))()
		if not (t_ok and c_ok):
			return
		if t_val is None and c_val is None:
			return
		if t_val is not None and c_val is not None:
			# Both axes present — route through the shortest-path
			# helper so the needle enters the bin at the closest point
			# to its current XY (legacy point target if extents are 0).
			entry_x, entry_y = self.app._waste_entry_for_current_position()
			self.app.state.last_waste_entry_x = entry_x
			self.app.state.last_waste_entry_y = entry_y
			self.app.move_to_positions(table_dist=entry_x,
				carriage_dist=entry_y, is_transit=True)
			return
		# Single-axis move: keep the original direct-target semantics
		# since shortest-path is ill-defined with a missing coordinate.
		self.app.move_to_positions(table_dist=t_val, carriage_dist=c_val,
			is_transit=True)


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

		# Plate orientation: "portrait" (default for fresh installs;
		# plate rows on X-axis, columns on Y-axis) or "landscape"
		# (columns on X, rows on Y). Top-level preference persisted in
		# config.json. Origin (0, 0) is the upper-left mechanical limit
		# and the motor reverse flags are fixed regardless of
		# orientation — orientation only affects which plate axis maps
		# to which motor axis when the operator calibrates the
		# Starting Well Position, the snake's physical motor mapping
		# (see ``_snake_step``), and the plate-progress visualization
		# layout.
		self.plate_orientation = config_store.load_plate_orientation()

		# Stepper motor speed mode. ``"slow"`` (default) drives every
		# move at the fractionation cadence so droplets don't fling
		# from the syringe during transit; ``"variable"`` keeps
		# well-to-well dispense moves slow but speeds up transit
		# moves (waste bin approach, return to origin, plate swaps,
		# manual jogs) by ``transit_speed_factor``. Both prefs are
		# top-level in config.json; consulted by every motor call via
		# ``StepperMotor._step_delay_for`` and the per-call
		# ``is_transit`` flag.
		self.motor_speed_mode = config_store.load_motor_speed_mode()
		self.transit_speed_factor = config_store.load_transit_speed_factor()
		self._apply_motor_speed_to_motors()

		# Optional supplementary notifications (ntfy push + local
		# beep). The on-screen dialog at every intervention point
		# remains the source of truth; these calls are async, time-
		# limited, and never raise into the caller. Config lives in
		# its own file so the ntfy topic doesn't leak through
		# shared profiles.
		self.notification_config = config_store.load_notification_config()
		self.notifications = notifications.NotificationManager(
			config_provider=lambda: self.notification_config,
		)

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
		# Waste-bin RECTANGLE extents (cm). The anchor lives in the two
		# vars above; these two extend the bin south and east. Empty /
		# missing values are treated as zero — preserving legacy
		# point-target behaviour. Shared at App level for the same
		# cross-mode reason as the anchor vars.
		self.waste_bin_x_extent_var = tk.StringVar()
		self.waste_bin_y_extent_var = tk.StringVar()
		# Inter-sample purge time. Owned at App level so the Cleaning-mode
		# Purge Time Calibration panel can write a measured value here and
		# Automated mode's Purge time entry picks it up immediately.
		self.purge_time_var = tk.StringVar(value="30.0")
		# Pre-fractionation prime time. Same shared-StringVar pattern as
		# purge_time_var: Manual mode's Prime Time Calibration tool
		# writes here and the Run Parameters Prime time entry mirrors
		# it live.
		self.prime_time_var = tk.StringVar(value="60")
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
		# Fractionation pump rate (mL/hr) and drip-wait time (s). Promoted to
		# App level so the new Tools → Pump Parameters dialog can bind
		# to the same variables that get_values / set_values + the
		# state machine read. No inline default — operator must
		# configure before the first run (run-start guard fires).
		self.pump_rate_var = tk.StringVar(value="")
		self.drip_wait_time_var = tk.StringVar(value="1.0")

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
		# Whatever modal was grabbing input when the threshold dialog
		# opened, captured so the warning's close path can restore it.
		self._waste_threshold_prior_grab = None
		# When True, the active inter-sample purge phase has been halted by
		# a waste-bin threshold trip; the tick callback uses this to stop
		# pumping mid-phase. Reset by waste-reset.
		self._purge_halted_for_waste = False
		# When True, the real-time waste tracker still ticks (relay-on
		# / -off transitions are still detected) but its incremental
		# adds to ``waste_volume_ml`` are suppressed. Used during the
		# pre-fractionation prime when the target is the plate's start
		# well -- pump output lands in the first well as fraction
		# dilution, not in the waste bin.
		self._suppress_waste_tracking = False

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
		# Show the Tools-menu hint banner if any required pump /
		# cleaning parameter is still unset after last_used has
		# populated. Runs unconditionally so a fresh install with no
		# config.json (last_used == {}) still gets the banner.
		self.automated_frame._refresh_config_banner()

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
		tools.add_command(label="Preferences…", command=self._show_preferences_dialog)
		tools.add_separator()
		tools.add_command(label="Fractionation Parameters…",
			command=self._show_pump_parameters_dialog)
		tools.add_command(label="Cleaning Parameters…",
			command=self._show_cleaning_parameters_dialog)
		tools.add_separator()
		tools.add_command(label="Open last run folder", command=self._open_last_run)
		menubar.add_cascade(label="Settings", menu=tools)

		help_menu = tk.Menu(menubar, tearoff=False)
		help_menu.add_command(label="About", command=self._show_about_dialog)
		menubar.add_cascade(label="Help", menu=help_menu)

		self.config(menu=menubar)

	def _show_preferences_dialog(self):
		"""Modal preferences dialog. OK persists each setting to
		config.json and applies immediately; Cancel discards.

		Layout is a two-column grid of ``tk.LabelFrame`` sections —
		**Plate**, **Inter-sample Purge**, **Run Behavior** in
		column 0; **Motor Movement** and **Notifications** in
		column 1 — so the window is wider than tall and related
		settings cluster visually. The bottom row carries the
		OK / Cancel buttons spanning both columns."""
		dlg = tk.Toplevel(self)
		dlg.title("Preferences")
		dlg.transient(self)
		dlg.resizable(True, True)
		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)
		body.grid_columnconfigure(0, weight=1, uniform="prefcols")
		body.grid_columnconfigure(1, weight=1, uniform="prefcols")

		# Section LabelFrames use a slightly bolder title than the
		# inner widget labels so the panel headings read at a glance.
		section_font = (FONTS["family"], FONTS["size"], "bold")
		def _section(text, row, column, *, rowspan=1):
			lf = tk.LabelFrame(body, text=text, padx=10, pady=6,
				font=section_font)
			lf.grid(row=row, column=column, rowspan=rowspan,
				sticky="nsew", padx=4, pady=4)
			lf.grid_columnconfigure(0, weight=1)
			return lf

		def _hint(parent, text, wraplength=300):
			"""Italic muted help text, gridded into the next row of
			the surrounding LabelFrame."""
			lbl = tk.Label(parent, text=text, justify="left", anchor="w",
				wraplength=wraplength, fg=PALETTE["fg_muted"],
				font=(FONTS["family"], FONTS["size"], "italic"))
			return lbl

		# ---- Col 0: Plate ---------------------------------------------
		plate_lf = _section("Plate", 0, 0)
		tk.Label(plate_lf, text="Plate orientation:", anchor="w",
			).grid(row=0, column=0, sticky="we", pady=(0, 2))
		orientation_var = tk.StringVar(value=self.plate_orientation)
		tk.Radiobutton(plate_lf, variable=orientation_var, value="portrait",
			text="Portrait (plate rows on the X-axis)",
		).grid(row=1, column=0, sticky="w", padx=(16, 0))
		tk.Radiobutton(plate_lf, variable=orientation_var, value="landscape",
			text="Landscape (plate columns on the X-axis)",
		).grid(row=2, column=0, sticky="w", padx=(16, 0))
		_hint(plate_lf,
			"Portrait is recommended for this XY table's sizing. "
			"The origin (0, 0) is always the upper-left mechanical "
			"limit and Manual jog buttons always drive the motors in "
			"the same physical direction; only the Starting Well "
			"Position differs between orientations. Recalibrate it "
			"after switching using the Position Calibration tool.",
			wraplength=320,
		).grid(row=3, column=0, sticky="we", padx=(16, 0), pady=(4, 0))

		# ---- Col 0: Inter-sample Purge --------------------------------
		purge_lf = _section("Inter-sample Purge", 1, 0)
		tk.Label(purge_lf, text="Protocol:", anchor="w",
			).grid(row=0, column=0, sticky="we", pady=(0, 2))
		protocol_var = tk.StringVar(value=self.purge_protocol)
		tk.Radiobutton(purge_lf, variable=protocol_var, value="basic",
			text="Water only (water → sample)",
		).grid(row=1, column=0, sticky="w", padx=(16, 0))
		tk.Radiobutton(purge_lf, variable=protocol_var, value="decontamination",
			text="Decontamination (water → bleach → water → sample)",
		).grid(row=2, column=0, sticky="w", padx=(16, 0))
		skip_var = tk.BooleanVar(value=self.skip_intersample_purge_var.get())
		tk.Checkbutton(purge_lf, variable=skip_var,
			text="Skip inter-sample purge entirely",
		).grid(row=3, column=0, sticky="w", pady=(8, 0))

		# ---- Col 0: Run Behavior --------------------------------------
		behav_lf = _section("Run Behavior", 2, 0)
		return_var = tk.BooleanVar(value=self.return_to_origin_on_exit)
		tk.Checkbutton(behav_lf, variable=return_var,
			text="Return needle to origin when closing the application",
		).grid(row=0, column=0, sticky="w")

		# ---- Col 1: Motor Movement ------------------------------------
		motor_lf = _section("Motor Movement", 0, 1)
		tk.Label(motor_lf, text="Motor speed mode:", anchor="w",
			).grid(row=0, column=0, sticky="we", pady=(0, 2))
		speed_var = tk.StringVar(value=self.motor_speed_mode)
		tk.Radiobutton(motor_lf, variable=speed_var, value="slow",
			text="Slow speed (all moves at fractionation speed)",
		).grid(row=1, column=0, sticky="w", padx=(16, 0))
		tk.Radiobutton(motor_lf, variable=speed_var, value="variable",
			text="Variable speed (transit moves faster than fractionation)",
		).grid(row=2, column=0, sticky="w", padx=(16, 0))
		_hint(motor_lf,
			"Slow speed drives all moves at the fractionation speed "
			"to prevent droplets from being flung from the syringe. "
			"Variable speed keeps well-to-well fractionation slow but "
			"speeds up transit moves (to/from the waste bin, return "
			"to origin, plate swaps).",
			wraplength=320,
		).grid(row=3, column=0, sticky="we", padx=(16, 0), pady=(4, 4))

		# Transit speed factor — disabled in Slow mode but kept
		# visible so the operator can see the configured value.
		factor_row = tk.Frame(motor_lf)
		factor_row.grid(row=4, column=0, sticky="we", padx=(16, 0),
			pady=(0, 2))
		tk.Label(factor_row, text="Transit speed factor (×):",
			).pack(side=tk.LEFT)
		factor_var = tk.StringVar(value=f"{self.transit_speed_factor:.1f}")
		factor_entry = ttk.Entry(factor_row, textvariable=factor_var, width=8)
		factor_entry.pack(side=tk.LEFT, padx=(8, 0))
		factor_err_lbl = tk.Label(motor_lf, text="", fg="red", anchor="w",
			wraplength=320)
		factor_err_lbl.grid(row=5, column=0, sticky="we", padx=(16, 0))
		_hint(motor_lf,
			"Default 2.0. Increase cautiously and verify the "
			"carriage still reaches target positions without "
			"stalling or missed steps.",
			wraplength=320,
		).grid(row=6, column=0, sticky="we", padx=(16, 0), pady=(2, 0))

		def _sync_factor_state(*_):
			if speed_var.get() == "variable":
				factor_entry.state(["!disabled"])
			else:
				factor_entry.state(["disabled"])
		speed_var.trace_add("write", _sync_factor_state)
		_sync_factor_state()

		# ---- Col 1: Notifications -------------------------------------
		# Spans rows 1 + 2 so its taller content balances Plate +
		# Inter-sample Purge + Run Behavior stacked in column 0.
		# Audible-alert checkbox removed: the Pi has no native audio and
		# ntfy push notifications cover the "operator out of earshot"
		# case. The underlying _audible / _bell helpers in
		# notifications.py are retained but unreachable so they can be
		# revived by a future build that includes a speaker.
		notif_lf = _section("Notifications", 1, 1, rowspan=2)
		ncfg = dict(self.notification_config or {})
		ntfy_var = tk.BooleanVar(value=bool(ncfg.get("ntfy_enabled")))
		tk.Checkbutton(notif_lf, variable=ntfy_var,
			text="ntfy push notifications",
		).grid(row=0, column=0, sticky="w")

		ntfy_fields = tk.Frame(notif_lf)
		ntfy_fields.grid(row=1, column=0, sticky="we", padx=(20, 0),
			pady=(2, 0))
		ntfy_fields.grid_columnconfigure(1, weight=1)
		tk.Label(ntfy_fields, text="Server:", anchor="w").grid(
			row=0, column=0, sticky="w", padx=(0, 6), pady=2)
		server_var = tk.StringVar(value=ncfg.get("ntfy_server") or "ntfy.sh")
		ntfy_server_entry = ttk.Entry(ntfy_fields, textvariable=server_var,
			width=20)
		ntfy_server_entry.grid(row=0, column=1, sticky="we", pady=2)
		tk.Label(ntfy_fields, text="Topic:", anchor="w").grid(
			row=1, column=0, sticky="w", padx=(0, 6), pady=2)
		topic_var = tk.StringVar(value=ncfg.get("ntfy_topic") or "")
		ntfy_topic_entry = ttk.Entry(ntfy_fields, textvariable=topic_var,
			width=20)
		ntfy_topic_entry.grid(row=1, column=1, sticky="we", pady=2)
		_hint(notif_lf,
			"Install the ntfy app on your phone and subscribe to "
			"this topic. Choose a unique, hard-to-guess string — "
			"anyone who knows it can see your notifications.",
			wraplength=320,
		).grid(row=2, column=0, sticky="we", padx=(20, 0), pady=(4, 4))

		# Test button + inline result label.
		test_row = tk.Frame(notif_lf)
		test_row.grid(row=3, column=0, sticky="we", pady=(4, 0))
		test_btn = ttk.Button(test_row, text="Send Test Notification")
		test_btn.pack(side=tk.LEFT)
		test_result_lbl = tk.Label(test_row, text="", anchor="w",
			fg=PALETTE["fg_muted"])
		test_result_lbl.pack(side=tk.LEFT, padx=(8, 0))

		def _send_test():
			# Read the LIVE entry / checkbox values so the operator
			# doesn't have to OK + reopen prefs just to test a
			# tweak. Build an ephemeral config the manager can read.
			ephemeral = {
				"ntfy_enabled": bool(ntfy_var.get()),
				"ntfy_server": server_var.get().strip(),
				"ntfy_topic": topic_var.get().strip(),
			}
			prior = self.notification_config
			self.notification_config = ephemeral
			try:
				ok, detail = self.notifications.send_test()
			except Exception as exc:
				ok, detail = False, str(exc)
			finally:
				self.notification_config = prior
			if ok:
				test_result_lbl.config(text=f"✓ {detail}", fg="#1e7d20")
			else:
				test_result_lbl.config(text=f"✗ {detail}", fg="#b22222")
		test_btn.configure(command=_send_test)

		def _sync_ntfy_fields(*_):
			st = "normal" if ntfy_var.get() else "disabled"
			ntfy_server_entry.configure(state=st)
			ntfy_topic_entry.configure(state=st)
		ntfy_var.trace_add("write", _sync_ntfy_fields)
		_sync_ntfy_fields()

		# ---- Bottom button row (spans both columns) ------------------
		btn_row = tk.Frame(body)
		btn_row.grid(row=3, column=0, columnspan=2, sticky="we",
			padx=4, pady=(8, 0))

		def _ok():
			new_return = bool(return_var.get())
			new_skip = bool(skip_var.get())
			new_protocol = protocol_var.get()
			new_orientation = orientation_var.get()
			if new_orientation not in ("portrait", "landscape"):
				new_orientation = self.plate_orientation
			new_speed_mode = speed_var.get()
			if new_speed_mode not in ("slow", "variable"):
				new_speed_mode = self.motor_speed_mode
			# Validate the transit speed factor only when Variable
			# mode is the *new* selection; in Slow mode the field is
			# kept as-is for round-tripping but doesn't gate OK.
			factor_err_lbl.config(text="")
			new_factor = self.transit_speed_factor
			if new_speed_mode == "variable":
				ok, parsed = validation.transit_speed_factor(
					factor_var.get())
				if not ok:
					factor_err_lbl.config(text=parsed)
					return
				new_factor = parsed
			else:
				# Slow mode: accept whatever the entry holds, but if
				# it parses cleanly, keep it as the persisted value
				# (so flipping to Variable later doesn't lose the
				# operator's number).
				ok, parsed = validation.transit_speed_factor(
					factor_var.get())
				if ok:
					new_factor = parsed
			# Orientation switch carries a migration prompt: the well-
			# to-XY mapping changes (which plate axis maps to which
			# motor axis), so the operator must re-derive the Starting
			# Well Position before the next run. The origin (0, 0) and
			# physical motor directions are NOT affected by the switch.
			if new_orientation != self.plate_orientation:
				go = messagebox.askyesno(
					"Switch plate orientation?",
					"Switching plate orientation changes which plate "
					"axis maps to which motor axis, so saved Starting "
					"Well and Waste Bin positions will reference the "
					"old mapping until re-derived. Recalibrate them "
					"using the Position Calibration tool before your "
					"next run.\n\n"
					"The origin (0, 0) and the physical direction of "
					"each Manual jog button are NOT affected.\n\n"
					"Continue with the switch?",
					parent=dlg,
				)
				if not go:
					# Revert the radio so the dialog still reflects the
					# committed value if the operator re-opens it.
					orientation_var.set(self.plate_orientation)
					return
			self.return_to_origin_on_exit = new_return
			# Push into the live BooleanVar so the state-machine read
			# (state.skip_intersample_purge at Begin) sees the new value
			# without an app restart.
			self.skip_intersample_purge_var.set(new_skip)
			self.purge_protocol = (
				new_protocol if new_protocol in ("basic", "decontamination")
				else "basic"
			)
			orientation_changed = (new_orientation != self.plate_orientation)
			self.plate_orientation = new_orientation
			if orientation_changed:
				# Motor reverse flags are NOT touched by an orientation
				# switch — origin and physical +X/+Y directions are
				# fixed (upper-left mechanical limit / east / south)
				# regardless of orientation. Push the new orientation
				# into the live plate canvas so the idle visualisation
				# reflects the switch immediately (a subsequent run
				# picks it up via begin_run too).
				af = getattr(self, "automated_frame", None)
				progress = getattr(af, "progress", None)
				if progress is not None and hasattr(progress, "set_orientation"):
					progress.set_orientation(new_orientation)
				# Refresh both canvases so the A1 anchor and the
				# table-view plate footprint match the new
				# orientation immediately.
				if af is not None and hasattr(af, "_refresh_plate_preview"):
					af._refresh_plate_preview()
				if af is not None and hasattr(af, "_refresh_table_view"):
					af._refresh_table_view()
				logger.info("Plate orientation switched to %s", new_orientation)
			# Motor speed: apply if either the mode or the factor
			# changed. The motor methods consult the active values
			# per call, so a mid-session change takes effect on the
			# very next move.
			speed_changed = (
				new_speed_mode != self.motor_speed_mode
				or abs(new_factor - self.transit_speed_factor) > 1e-6
			)
			self.motor_speed_mode = new_speed_mode
			self.transit_speed_factor = new_factor
			if speed_changed:
				self._apply_motor_speed_to_motors()
				logger.info(
					"Motor speed prefs updated: mode=%s factor=%.2f",
					new_speed_mode, new_factor,
				)
			# Commit the notification settings into App state so the
			# next intervention picks them up immediately, then
			# persist to disk.
			self.notification_config = {
				"ntfy_enabled": bool(ntfy_var.get()),
				"ntfy_server": server_var.get().strip(),
				"ntfy_topic": topic_var.get().strip(),
			}
			try:
				config_store.save_return_to_origin_on_exit(new_return)
				config_store.save_skip_intersample_purge(new_skip)
				config_store.save_purge_protocol(self.purge_protocol)
				config_store.save_plate_orientation(self.plate_orientation)
				config_store.save_motor_speed_mode(self.motor_speed_mode)
				config_store.save_transit_speed_factor(self.transit_speed_factor)
				config_store.save_notification_config(self.notification_config)
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

		# Default geometry: wider than tall — about 2× the old width
		# so the two columns of LabelFrames don't crowd each other.
		# Set a minsize too so the user can resize back to a sensible
		# baseline after dragging it smaller.
		dlg.update_idletasks()
		default_w = max(820, dlg.winfo_reqwidth())
		default_h = max(420, dlg.winfo_reqheight())
		x = self.winfo_rootx() + (self.winfo_width() - default_w) // 2
		y = self.winfo_rooty() + (self.winfo_height() - default_h) // 3
		dlg.geometry(f"{default_w}x{default_h}+{max(0, x)}+{max(0, y)}")
		dlg.minsize(720, 380)
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
			     "Elango et al. 2026 (in preparation, HardwareX).",
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

	# -- Pump / Cleaning parameter dialogs ------------------------------

	def _modal_param_dialog(self, *, title, sections, cross_field_check=None):
		"""Shared modal-dialog template for Pump Parameters / Cleaning
		Parameters.

		``sections`` is a list of ``(label_text, [(field_label, var,
		validator, tooltip), ...])`` tuples. Each section becomes a
		LabelFrame. Each field renders as a TextEntry bound to the
		given App-level ``var`` (so edits are live but the snapshot
		under ``initial`` lets Cancel revert).

		Live validation: every field's var is wired through ``trace_add``
		so the inline error indicator updates on every keystroke, in
		both the dialog and any other surface bound to the same var.

		``cross_field_check`` (optional) is a callable taking the list
		of ``(te, var, validator, label)`` entries; it should walk the
		entries, call ``te.show_error(msg)`` for any field that fails a
		cross-field invariant, and return ``True`` if all checks
		passed. It runs after the per-field pass on every var write and
		during Save.

		Save re-runs validation; on failure the offending entry
		surfaces the inline error and the dialog stays open. On success
		the dialog closes and the new values stay on the App-level
		StringVars.
		"""
		dlg = tk.Toplevel(self)
		dlg.title(title)
		dlg.transient(self)
		dlg.resizable(False, False)
		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)

		# Snapshot every var's current value so Cancel can revert.
		initial = {}
		entries = []
		for sect_title, fields in sections:
			lf = tk.LabelFrame(body, text=sect_title, padx=8, pady=6)
			lf.pack(fill=tk.X, expand=True, pady=(0, 6))
			lf.grid_columnconfigure(0, weight=1)
			for row, (field_label, var, validator, tooltip) in enumerate(fields):
				initial[id(var)] = var.get()
				te = TextEntry(lf, field_label, textvariable=var)
				te.grid(row=row, column=0, sticky="we")
				if tooltip:
					Tooltip(te.entry, tooltip)
				entries.append((te, var, validator, field_label))

		def _validate_all(*, surface_errors):
			"""Run per-field + cross-field validation. ``surface_errors``
			controls whether failures call show_error/clear_error or
			just return the pass/fail verdict. Returns True if every
			check passes."""
			ok_all = True
			for te, _v, validator, _label in entries:
				if validator is None:
					if surface_errors:
						te.clear_error()
					continue
				ok, val = validator(te.get())
				if ok:
					if surface_errors:
						te.clear_error()
				else:
					if surface_errors:
						te.show_error(val)
					ok_all = False
			if cross_field_check is not None:
				if not cross_field_check(entries):
					ok_all = False
			return ok_all

		# Live revalidation on every var write. Bound after the
		# entries list is built so cross_field_check can inspect
		# sibling fields.
		def _on_var_write(*_a):
			_validate_all(surface_errors=True)
		for _te, v, _validator, _label in entries:
			v.trace_add("write", _on_var_write)
		# Initial pass so any stale invalid value from config.json or
		# from a prior in-Cleaning-mode edit shows its indicator the
		# moment the dialog opens.
		dlg.after_idle(_on_var_write)

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X, pady=(4, 0))
		def _cancel(_e=None):
			# Revert every var to its pre-open snapshot.
			for _te, v, _val, _lbl in entries:
				v.set(initial[id(v)])
			dlg.destroy()
		def _save(_e=None):
			if not _validate_all(surface_errors=True):
				return
			dlg.destroy()
		ttk.Button(btn_row, text="Cancel", command=_cancel).pack(
			side=tk.LEFT, padx=4)
		ttk.Button(btn_row, text="Save", command=_save,
			style="Primary.TButton").pack(side=tk.RIGHT, padx=4)
		dlg.bind("<Escape>", _cancel)
		dlg.bind("<Return>", _save)
		dlg.protocol("WM_DELETE_WINDOW", _cancel)
		dlg.update_idletasks()
		x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
		y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
		dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
		dlg.grab_set()
		return dlg

	def _show_pump_parameters_dialog(self):
		"""Tools → Pump Parameters. Houses the fields formerly in the
		Automated mode "Fractionation Pump Parameters" LabelFrame: syringe
		pump rate, drip-wait time, and prime time. All three stay bound
		to the App-level StringVars so the Manual mode Prime Time
		Calibration tool can still write directly to ``prime_time_var``.
		On Save the dialog also persists last_used so the next launch
		picks up the new values.
		"""
		dlg = self._modal_param_dialog(
			title="Fractionation Parameters",
			sections=[
				("Pump", [
					("Pump rate (mL/hr — see your fractionation pump spec):",
						self.pump_rate_var, validation.pump_rate,
						"Volumetric flow rate of the fractionation pump driving "
						"fractionation. Set from the pump spec."),
					("Drip wait time (s):", self.drip_wait_time_var,
						validation.drip_wait_time,
						"Wait time between pump-off and moving to the "
						"next well. Longer waits improve volume "
						"consistency; shorter waits run faster."),
					("Prime time (s):", self.prime_time_var,
						validation.prime_time,
						"Time to walk the sample fractionation solution "
						"from the tube to ~5 cm below the syringe "
						"dispenser. The Manual mode Prime Time "
						"Calibration tool can measure this for you."),
				]),
			],
		)
		# After Save: persist last_used + refresh anything that watches
		# these values. Wait for the dialog to close, then act if the
		# values changed.
		self._after_param_dialog(dlg, refresh_table_view=False)

	def _waste_bin_geometry_error(self):
		"""Return the first validation error string for the four
		waste-bin StringVars, or None if everything parses and the
		rectangle (center ± extent/2) fits inside the physical table.

		Centralised so File → Save profile…, the Tools dialog Save,
		and the Cleaning Mode panel all agree on what "invalid" means.
		"""
		from well_plate import TABLE_WIDTH_MM, TABLE_HEIGHT_MM
		table_x_max = TABLE_WIDTH_MM / 10.0
		table_y_max = TABLE_HEIGHT_MM / 10.0
		x_ok, x_val = validation.table_pos(
			self.waste_bin_table_var.get(), allow_empty=True)
		y_ok, y_val = validation.carriage_pos(
			self.waste_bin_carriage_var.get(), allow_empty=True)
		ex_ok, ex_val = validation.waste_bin_extent(
			self.waste_bin_x_extent_var.get(), allow_empty=True)
		ey_ok, ey_val = validation.waste_bin_extent(
			self.waste_bin_y_extent_var.get(), allow_empty=True)
		if not x_ok:
			return f"Waste bin X: {x_val}"
		if not y_ok:
			return f"Waste bin Y: {y_val}"
		if not ex_ok:
			return f"Waste bin size (x-axis): {ex_val}"
		if not ey_ok:
			return f"Waste bin size (y-axis): {ey_val}"
		if x_val is not None and ex_val is not None and ex_val > 0:
			lo, hi = x_val - ex_val / 2.0, x_val + ex_val / 2.0
			if lo < -1e-6 or hi > table_x_max + 1e-6:
				return (f"Waste bin X rectangle ({lo:.2f} → {hi:.2f} cm) "
					f"overhangs [0.00, {table_x_max:.2f}] cm.")
		if y_val is not None and ey_val is not None and ey_val > 0:
			lo, hi = y_val - ey_val / 2.0, y_val + ey_val / 2.0
			if lo < -1e-6 or hi > table_y_max + 1e-6:
				return (f"Waste bin Y rectangle ({lo:.2f} → {hi:.2f} cm) "
					f"overhangs [0.00, {table_y_max:.2f}] cm.")
		return None

	def _show_cleaning_parameters_dialog(self):
		"""Tools → Cleaning Parameters. Houses everything purge- /
		bin-related: per-phase purge time, peristaltic pump rate, max
		waste-bin volume, and the four waste-bin geometry fields
		(center X/Y + extent X/Y). On Save the table view repaints so
		the new bin rectangle renders immediately. Bin geometry
		entries are also duplicated in the Cleaning Mode Waste Bin
		panel; both surfaces edit the same App-level StringVars."""
		# Cross-field overhang check shared with CleaningFrame: the
		# rectangle (center ± extent/2) must fit inside the physical
		# table. Identifies the bin entries by var identity so it
		# doesn't depend on the field order in ``sections``.
		from well_plate import TABLE_WIDTH_MM, TABLE_HEIGHT_MM
		table_x_max = TABLE_WIDTH_MM / 10.0
		table_y_max = TABLE_HEIGHT_MM / 10.0
		bin_vars = (
			self.waste_bin_table_var, self.waste_bin_carriage_var,
			self.waste_bin_x_extent_var, self.waste_bin_y_extent_var,
		)
		def _overhang_check(entries):
			by_var = {id(v): te for te, v, _val, _lbl in entries
				if v in bin_vars}
			cx_te = by_var.get(id(self.waste_bin_table_var))
			cy_te = by_var.get(id(self.waste_bin_carriage_var))
			ex_te = by_var.get(id(self.waste_bin_x_extent_var))
			ey_te = by_var.get(id(self.waste_bin_y_extent_var))
			def _parse(te, validator):
				if te is None:
					return None
				ok, val = validator(te.get(), allow_empty=True)
				return val if ok else None
			cx = _parse(cx_te, validation.table_pos)
			cy = _parse(cy_te, validation.carriage_pos)
			ex = _parse(ex_te, validation.waste_bin_extent)
			ey = _parse(ey_te, validation.waste_bin_extent)
			ok = True
			if ex_te is not None and cx is not None and ex is not None and ex > 0:
				lo, hi = cx - ex / 2.0, cx + ex / 2.0
				if lo < -1e-6 or hi > table_x_max + 1e-6:
					ex_te.show_error(
						f"Waste bin X rectangle ({lo:.2f} → {hi:.2f} cm) "
						f"overhangs [0.00, {table_x_max:.2f}] cm."
					)
					ok = False
			if ey_te is not None and cy is not None and ey is not None and ey > 0:
				lo, hi = cy - ey / 2.0, cy + ey / 2.0
				if lo < -1e-6 or hi > table_y_max + 1e-6:
					ey_te.show_error(
						f"Waste bin Y rectangle ({lo:.2f} → {hi:.2f} cm) "
						f"overhangs [0.00, {table_y_max:.2f}] cm."
					)
					ok = False
			return ok
		dlg = self._modal_param_dialog(
			title="Cleaning Parameters",
			cross_field_check=_overhang_check,
			sections=[
				("Purge & Pump", [
					("Purge time (s):", self.purge_time_var,
						validation.purge_time,
						"Per-phase duration of the inter-sample purge. "
						"Use Cleaning mode's Purge Time Calibration tool "
						"to measure the right value for your tubing."),
					("Peristaltic pump rate (mL/min):",
						self.peristaltic_rate_var,
						validation.peristaltic_rate,
						"Flow rate of the peristaltic pump used for "
						"purges. Drives the waste-bin volume estimate."),
					("Max waste bin volume (mL):",
						self.max_waste_volume_var,
						validation.max_waste_volume,
						"Capacity of your waste container. autoSIP warns "
						"at 80% and halts at 100% to prevent overflow."),
				]),
				("Waste Bin Geometry", [
					("Waste bin position (x-axis; cm):",
						self.waste_bin_table_var, validation.table_pos,
						"X (table-axis) coordinate of the bin's CENTER. "
						"Calibrate via Manual mode by jogging the needle "
						"to the visual center of the bin."),
					("Waste bin position (y-axis; cm):",
						self.waste_bin_carriage_var, validation.carriage_pos,
						"Y (carriage-axis) coordinate of the bin's CENTER. "
						"Calibrate via Manual mode by jogging the needle "
						"to the visual center of the bin."),
					("Waste bin size (x-axis; cm):",
						self.waste_bin_x_extent_var,
						validation.waste_bin_extent,
						"Full width of the bin rectangle along X. The "
						"rectangle spans ± extent/2 around the center. "
						"0 = legacy point target."),
					("Waste bin size (y-axis; cm):",
						self.waste_bin_y_extent_var,
						validation.waste_bin_extent,
						"Full height of the bin rectangle along Y. The "
						"rectangle spans ± extent/2 around the center. "
						"0 = legacy point target."),
				]),
			],
		)
		self._after_param_dialog(dlg, refresh_table_view=True)

	def _after_param_dialog(self, dlg, *, refresh_table_view):
		"""When the dialog finishes (Save or Cancel), reconcile state.

		Persistence: the AutomatedFrame's existing focus-out save handler
		can't fire because the inline widgets don't exist. So we call
		``_save_last_used`` directly after the modal closes.

		Table view refresh: bin-geometry edits in the Cleaning dialog
		need an immediate repaint so the operator sees the new
		rectangle without waiting for the next mode switch.
		"""
		def _on_destroy(_e=None):
			af = getattr(self, "automated_frame", None)
			if af is not None and hasattr(af, "_save_last_used"):
				try:
					af._save_last_used()
				except Exception as exc:
					logger.debug("save_last_used after dialog failed: %s", exc)
			if refresh_table_view and af is not None:
				try:
					af._refresh_table_view()
				except Exception as exc:
					logger.debug(
						"refresh_table_view after dialog failed: %s", exc)
			if af is not None and hasattr(af, "_refresh_config_banner"):
				af._refresh_config_banner()
		dlg.bind("<Destroy>", _on_destroy, add="+")

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
		# Refuse to write a profile while the waste-bin geometry
		# fields are invalid (negative extent, unparseable position,
		# or a rectangle that overhangs the table). Mirrors the
		# inline error indicators already shown in Tools → Cleaning
		# Parameters and Cleaning Mode's Waste Bin panel.
		bin_err = self._waste_bin_geometry_error()
		if bin_err is not None:
			messagebox.showerror(
				"Cannot save profile",
				f"Fix the waste-bin geometry before saving:\n\n{bin_err}",
				parent=self,
			)
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
		self.automated_frame._refresh_config_banner()
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
				self.table_motor.move_dist_absolute(0.0, is_transit=True)
				self.carriage_motor.move_dist_absolute(0.0, is_transit=True)
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
		if name == "Manual":
			# Defensive: clear the snake's last-direction memory
			# before the operator starts jogging. See
			# ``_reset_motor_direction_state`` for the rationale.
			self._reset_motor_direction_state()
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
		      Resume can drive back to it and pop an Origin Calibration
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
			# Pop the Origin Calibration dialog: walks the operator
			# through the re-park against the mechanical limit and
			# owns the Resume action that drives the needle back to
			# the captured pause position. The dialog handles its
			# own re-entry (clicking Return to Origin again while
			# the dialog is open dismisses the prior instance).
			self.set_status(
				"Returned to origin. Origin Calibration dialog open."
			)
			self._update_run_control_buttons()
			self._show_origin_calibration_dialog()
			return
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
		self.move_to_positions(table_dist=t_val, carriage_dist=c_val,
			is_transit=True)
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

		# System Clean: enabled at idle OR while operator-paused;
		# disabled during active dispensing. Pause flips here without
		# moving through ``_set_phase`` so we re-evaluate the gate at
		# every run-control update.
		cf = getattr(self, "cleaning_frame", None)
		if cf is not None and hasattr(cf, "_refresh_sysclean_gate"):
			cf._refresh_sysclean_gate()

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

	def _reset_motor_direction_state(self):
		"""Reset both stepper motors' ``forwards`` backlash-tracking
		flags to their default (True). The motor's actual rotation
		direction is determined per-call by the SIGN of the delta
		passed to ``move_dist_relative`` / ``move_dist_absolute``
		combined with the fixed ``reverse`` flag; ``forwards`` only
		controls backlash compensation timing. Resetting here is a
		defensive belt-and-suspenders measure against a reported
		Y-axis inversion seen on real hardware after an automated
		run completes — even though simulation can't reproduce the
		inversion, dropping the snake's last-direction memory before
		the next Manual jog removes one source of inter-mode state
		coupling. Called from ``end_run`` finalize and from
		``set_mode`` on entry to Manual mode.
		"""
		self.table_motor.forwards = True
		self.carriage_motor.forwards = True

	def _waste_entry_for_current_position(self):
		"""Compute the actual ``(target_table_cm, target_carriage_cm)``
		entry point for the NEXT move-to-waste, given the current
		motor XY and the bin rectangle.

		If both extents are zero (legacy default), return the bin
		center directly — preserves the previous point-target
		behaviour exactly. Otherwise route through
		``shortest_point_in_waste_bin``: clamp the current motor
		position to the bin's interior (rectangle spanning
		[center − extent/2, center + extent/2], then shrunk by the
		``WASTE_BIN_INTERIOR_MARGIN_MM`` rim margin on each side).

		Returns (target_x_cm, target_y_cm) in motor cm. Y is the
		state-machine positive-south-distance convention; the
		``move_to_positions`` boundary negates it before reaching the
		carriage motor.
		"""
		from well_plate import shortest_point_in_waste_bin
		s = self.state
		center_x = float(s.waste_bin_table)
		center_y = float(s.waste_bin_carriage)
		ext_x = float(s.waste_bin_x_extent)
		ext_y = float(s.waste_bin_y_extent)
		if ext_x <= 0.0 and ext_y <= 0.0:
			# Legacy point-target — preserve the center exactly.
			return center_x, center_y
		# Current motor XY, converted to the state-machine positive-
		# south-distance convention so it shares a frame with the bin.
		cur_x_cm = self.table_motor.get_angle() * self.table_motor.cm_per_deg
		cur_y_cm = abs(self.carriage_motor.get_angle() * self.carriage_motor.cm_per_deg)
		# Helper takes mm; convert + back.
		tx_mm, ty_mm = shortest_point_in_waste_bin(
			cur_x_cm * 10.0, cur_y_cm * 10.0,
			center_x * 10.0, center_y * 10.0,
			ext_x * 10.0, ext_y * 10.0,
		)
		return tx_mm / 10.0, ty_mm / 10.0

	def iter_table_views(self):
		"""Yield every constructed ``TableView`` instance so the
		refresh + polling paths can update them together. Automated
		mode's canvas is always first; Manual mode's canvas joins
		once that frame finishes its own ``__init__``. Defensive
		``getattr`` lets this be called during the windowed init
		before all frames exist.
		"""
		for attr in ("automated_frame", "manual_frame"):
			frame = getattr(self, attr, None)
			tv = getattr(frame, "table_view", None) if frame is not None else None
			if tv is not None:
				yield tv

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

	def move_to_positions(self, table_dist=None, carriage_dist=None, *,
			is_transit=False):
		"""Move table and/or carriage to absolute positions (cm).

		``is_transit=True`` requests the fast Variable-speed cadence
		for moves that don't carry pressurized fluid (waste-bin
		approach, return to origin, plate swaps, jogs). Defaults to
		False so well-to-well dispense moves stay on the slow
		fractionation cadence.

		Carriage-axis convention bridge: every state-machine value
		(Starting Well Position, Waste Bin Position, ``s.carriage_*``)
		is a POSITIVE magnitude representing south distance from
		origin. The carriage motor's ``angle`` accumulator uses the
		opposite convention: NEGATIVE for south positions (matching
		Manual jog where Y− click drives the value more negative).
		Negate ``carriage_dist`` here so the motor's signed counter
		stays consistently in [−15, 0] regardless of whether the
		operator got there via Manual jog or an automated absolute
		move. This is what lets ``abs(motor.angle)`` in the crosshair
		poll always correspond to the same physical south distance —
		without it, the post-automated motor.angle ends up positive
		and the next Manual Y− click decreases its magnitude, sending
		the crosshair UP instead of DOWN.
		"""
		if table_dist is not None:
			self.table_motor.move_dist_absolute(table_dist,
				is_transit=is_transit)
		if carriage_dist is not None:
			self.carriage_motor.move_dist_absolute(-carriage_dist,
				is_transit=is_transit)

	def _apply_motor_speed_to_motors(self):
		"""Push the active speed configuration into both stepper
		motors. Called at App init and whenever the operator changes
		Motor speed mode / Transit speed factor in Tools →
		Preferences. Idempotent.

		The fractionation step delay is the existing
		``StepperMotor.DEFAULT_STEP_DELAY_S`` (100 µs). The transit
		step delay is ``fractionation_delay / transit_speed_factor`` —
		so factor=2.0 halves the per-step sleep, making transit
		moves ~2× faster than the slow cadence.
		"""
		fractionation_delay = StepperMotor.DEFAULT_STEP_DELAY_S
		# Defensive clamp so a malformed factor can't drive the motors
		# at absurd rates even if validation was bypassed.
		factor = max(1.0, min(5.0, float(self.transit_speed_factor)))
		transit_delay = fractionation_delay / factor
		variable_enabled = (self.motor_speed_mode == "variable")
		for motor in (self.table_motor, self.carriage_motor):
			motor.configure_speeds(
				fractionation_step_delay=fractionation_delay,
				transit_step_delay=transit_delay,
				variable_speed_enabled=variable_enabled,
			)
		logger.debug(
			"motor speed applied: mode=%s factor=%.2f "
			"(transit_delay=%.6fs / fractionation_delay=%.6fs)",
			self.motor_speed_mode, factor, transit_delay, fractionation_delay,
		)

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
				"  • The Razel R-200 fractionation pump is plugged into the relay outlet.\n"
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
		Automated-mode discards + needle priming are accounted for via
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
		# _suppress_waste_tracking is set during the priming workflow
		# when the target is the plate's start well (pump output lands
		# in the first plate well, not the waste bin). The tick still
		# fires so we don't lose the relay-on/off transitions, but the
		# increment is skipped.
		if not self._suppress_waste_tracking:
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
		if (delta_s > 0 and rate_ml_per_s > 0
				and not self._suppress_waste_tracking):
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
		"""Read the fractionation pump rate (Fractionation Pump Parameters →
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
		# threshold trigger. 100% trigger also sets the 80% warned flag
		# (100% subsumes 80%) so a single tick that crosses both
		# thresholds fires only the 100% dialog -- not the 80% via the
		# nested _add_waste path from _waste_tracker_stop's final delta.
		if severity == "80%":
			self.waste_warned_80 = True
		else:
			self._waste_full = True
			self.waste_warned_80 = True
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
		# Supplementary urgent push / beep. The threshold dialog
		# remains the source of truth; this is the operator-away
		# escape hatch.
		if severity == "80%":
			self.notifications.notify(
				title="autoSIP: waste 80%",
				message="Waste bin at 80%. Pump paused. Empty or Resume.",
				urgent=True,
			)
		else:
			self.notifications.notify(
				title="autoSIP: waste 100%",
				message=(
					"Waste bin at 100%. Pump hard-stopped. Empty "
					"the bin and Reset to resume."
				),
				urgent=True,
			)
		self._show_waste_threshold_dialog(severity, max_v)

	def _show_waste_threshold_dialog(self, severity, max_v):
		"""Modal threshold dialog. Severity drives behavior:

		  ``"80%"`` — advisory pause. Reset + Resume. Resume always
		    enabled. X dismissal = Resume (carry on with current
		    counter).

		  ``"100%"`` — blocking hard-stop. Reset + Resume. Resume
		    disabled until counter drops below 80% (via Reset). X
		    dismissal blocked -- the operator MUST Reset + Resume so
		    the bin is verified empty before further pumping.

		Both variants refresh the dialog's body label after Reset so
		the operator sees the updated waste readout. The dialog calls
		``grab_set`` so the warning takes priority over any modal
		that's already open (e.g. the inter-sample purge phase modal);
		the prior grab is captured and restored on close so the
		previously-modal widget regains focus afterward.
		"""
		if self._waste_threshold_dlg is not None:
			try:
				self._waste_threshold_dlg.destroy()
			except Exception:
				pass
		is_hardstop = (severity == "100%")
		# Capture whatever modal is currently grabbing input (e.g. the
		# inter-sample purge phase modal). The Tcl ``grab current``
		# command needs an explicit window argument to return a plain
		# path string -- the no-arg form returns a tuple of all
		# active grabs which doesn't round-trip through nametowidget.
		# The warning will grab on top of the prior modal; the close
		# handlers restore the prior grab so the stacked modal
		# regains focus when the warning closes.
		prior_grab = None
		try:
			path = str(self.tk.call("grab", "current", str(self)))
			if path:
				prior_grab = self.nametowidget(path)
		except (tk.TclError, KeyError):
			prior_grab = None
		dlg = tk.Toplevel(self)
		dlg.transient(self)
		dlg.resizable(False, False)
		if not is_hardstop:
			dlg.title("⚠ Waste Bin at 80%")
			intro_extra = "Pump has been paused automatically."
			instructions = (
				"Empty the waste container and click Reset if you want a "
				"fresh counter, or click Resume to continue with the "
				"current estimate."
			)
		else:
			dlg.title("⚠ Waste Bin at 100% — Hard Stop")
			intro_extra = (
				"Pump halted by the 100% failsafe. The bin must be "
				"emptied and the counter reset before pumping resumes."
			)
			instructions = (
				"Empty the waste container and click Reset. After "
				"reset, click Resume to continue the paused operation."
			)

		body = tk.Frame(dlg, padx=18, pady=14)
		body.pack(fill=tk.BOTH, expand=True)
		status_lbl = tk.Label(body, anchor="w", justify="left",
			wraplength=440)
		status_lbl.pack(anchor="w", pady=(0, 12))

		def _refresh_body():
			pct = self.waste_volume_ml / max_v if max_v else 0.0
			status_lbl.config(text=(
				f"Waste estimate: {self.waste_volume_ml:.0f} / "
				f"{max_v:.0f} mL ({pct:.0%})\n"
				f"{intro_extra}\n\n"
				f"{instructions}"
			))
		_refresh_body()

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X)
		btn_row.grid_columnconfigure(0, weight=1)
		btn_row.grid_columnconfigure(1, weight=1)

		def _on_reset():
			# Dialog Reset doesn't re-prompt -- already in Reset
			# context. Refresh the visible counter so the operator
			# can see the action took effect.
			self._perform_waste_reset(confirm=False)
			_refresh_body()
		def _on_resume():
			self._waste_threshold_resume(dlg)

		# Primary style highlights the operator's expected next action.
		# 80% advisory: Resume is the recommended path (carry on with
		# the current estimate); Reset is the secondary "if you want
		# to empty mid-task" option.
		# 100% hard-stop: Reset is mandatory before Resume can fire,
		# so Reset gets the highlight and Resume reads as subdued
		# AND is functionally disabled until Reset.
		reset_btn = ttk.Button(btn_row, text="Reset", command=_on_reset,
			style=("Primary.TButton" if is_hardstop else "TButton"))
		reset_btn.grid(row=0, column=0, sticky="w", padx=4)
		resume_btn = ttk.Button(btn_row, text="Resume", command=_on_resume,
			style=("TButton" if is_hardstop else "Primary.TButton"))
		resume_btn.grid(row=0, column=1, sticky="e", padx=4)

		if is_hardstop:
			# Hard stop: Resume disabled until Reset brings the
			# counter below 80%. _perform_waste_reset enables it via
			# self._waste_threshold_resume_btn.
			max_now = self._live_max_waste_volume()
			if self.waste_volume_ml >= 0.80 * max_now:
				resume_btn.state(["disabled"])
			# X dismissal is a no-op so the dialog cannot be closed
			# without resolving the hard stop.
			dlg.protocol("WM_DELETE_WINDOW", lambda: None)
		else:
			# Advisory: X = Resume (acknowledge and carry on).
			dlg.protocol("WM_DELETE_WINDOW", _on_resume)

		dlg.update_idletasks()
		self._center_over_main(dlg)
		# Take the modal grab so this warning sits on top of any
		# already-open modal (e.g. inter-sample purge phase modal).
		# The prior grab is restored on close.
		self._waste_threshold_dlg = dlg
		self._waste_threshold_resume_btn = resume_btn if is_hardstop else None
		self._waste_threshold_prior_grab = prior_grab
		try:
			dlg.grab_set()
		except tk.TclError:
			pass

	def _restore_prior_grab(self):
		"""Restore the modal grab that was active before the threshold
		dialog took it. Called from Reset (when it closes the dialog),
		Resume, and End-Operation paths. No-op when nothing was
		previously grabbing input."""
		prior = getattr(self, "_waste_threshold_prior_grab", None)
		self._waste_threshold_prior_grab = None
		if prior is None:
			return
		try:
			prior.grab_set()
		except tk.TclError:
			# Prior widget may have been destroyed in the meantime
			# (e.g. operator cancelled the underlying modal during
			# the threshold dialog's lifetime).
			pass

	def _waste_threshold_resume(self, dlg):
		"""Close the warning and continue. Behavior depends on context:
		  - Automated run mid-cycle (state.state in pump/wait/move/
		    discard + is_paused): route through toggle_pause so the
		    state-machine after() chain re-arms and the timed cycle
		    resumes.
		  - All other contexts (inter-sample purge modal, Manual /
		    Cleaning Purge button, anything not state-machine-driven):
		    just close the warning. Operator manually restarts pumping
		    via Space in the purge modal or the Manual/Cleaning Purge
		    button. The previous auto-restart created inconsistent
		    state — pump on but the dialog still in "complete" mode
		    awaiting a new extension — which made the next Space press
		    appear to do nothing.

		Clears ``_purge_halted_for_waste`` so any subsequent extension
		tick can proceed (the flag was the previous bug — left True
		after Resume, it caused new pump cycles to halt themselves
		on their first tick).
		"""
		# Clear the halt flag BEFORE destroying the dialog so any
		# tick that fires immediately after sees the cleared state.
		self._purge_halted_for_waste = False
		# Restore the prior grab BEFORE destroying so the previously
		# modal widget (inter-sample purge phase modal, etc.) regains
		# input focus the moment this dialog disappears.
		self._restore_prior_grab()
		dlg.destroy()
		self._waste_threshold_dlg = None
		self._waste_threshold_resume_btn = None
		s = self.state
		if s.state in ("pump", "wait", "move", "discard") and s.is_paused:
			self.toggle_pause()
			return
		# Toggle contexts (inter-sample purge modal, Manual / Cleaning
		# Purge button): pump stays OFF. The operator triggers their
		# own next pump-on via the modal's Space or the Purge button.

	def _waste_threshold_end_operation(self, dlg):
		"""Close the dialog and abort the operation. Pump stays off.
		Automated runs land in the auto-paused state (End Run is the
		operator's exit); Cleaning/Manual purges just dismiss.

		(Dead code -- the End Operation button was removed in a later
		pass. Kept as a compatibility shim in case any code path still
		references it.)
		"""
		self._restore_prior_grab()
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

		# Mid-pause recalibration owns its own Resume path through the
		# Origin Calibration dialog. If the operator clicks the main-UI
		# Resume while a recalibration is pending, redirect them to
		# the dialog instead of bypassing the re-park checklist + the
		# return-to-captured-position move.
		s = self.state
		if s.is_paused and s.origin_returned_during_pause:
			self._show_origin_calibration_dialog()
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

		# --- Resuming branch (non-recalibration pauses) ---
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

	def _recalibration_resume(self):
		"""Drive the needle from origin back to the captured pause
		position, clear the recalibration flag, and trigger the
		normal Resume path. Called by the Origin Calibration dialog's
		Resume button after the operator confirms the re-park.
		"""
		s = self.state
		self.set_status("Returning to last visited well…")
		self.move_to_positions(
			table_dist=s.paused_table_cm,
			carriage_dist=s.paused_carriage_cm,
			is_transit=True,
		)
		# Clear the flag so toggle_pause's recalibration guard doesn't
		# re-open the dialog when we hand off to the normal Resume.
		s.origin_returned_during_pause = False
		self.toggle_pause()

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
			waste_rows, over_capacity):
		"""Compact Begin Fractionation confirmation. Prompts for the
		Sample ID (the parameter most worth a final glance), shows a
		caller-supplied 2-column waste-bin projection table, and
		offers Cancel / Begin Fractionation. All other run parameters
		are visible in the main window behind the dialog and are not
		duplicated.

		``waste_rows`` is a list of ``(label, value, [fg])`` tuples
		ready for ``_build_kv_table``. ``over_capacity`` is True when
		the projection exceeds the configured bin max; an amber
		warning row is appended in that case.

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
		rows = list(waste_rows)
		if over_capacity:
			rows.append(
				("⚠ Projected to exceed bin capacity during this run.",
				 "", "#b25e09"),
			)
		self._build_kv_table(body, rows).pack(fill=tk.X, pady=(0, 12))

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

	def _show_origin_calibration_dialog(self):
		"""Open the post-Return-to-Origin recalibration dialog.

		Fires after ``return_to_origin`` finishes its move to (0, 0)
		when the run is mid-pause AND the operator clicked Return to
		Origin during this pause. Walks the operator through the
		manual re-park against the mechanical limit and provides the
		Resume button that triggers the return-move to the captured
		pause position.

		Non-blocking: the caller returns to the Tk event loop while
		the dialog stays open. Resume / Cancel destroy the dialog and
		do their own follow-up work. Re-opening (operator clicks
		Return to Origin again, or main-UI Resume) destroys the
		previous instance first so there's only ever one calibration
		dialog on screen.
		"""
		s = self.state
		# Dismiss any prior calibration dialog -- re-entry from a
		# second Return to Origin click or a main-UI Resume should
		# refresh state rather than stack a duplicate window.
		prior = getattr(self, "_origin_calibration_dlg", None)
		if prior is not None:
			try:
				if prior.winfo_exists():
					prior.destroy()
			except tk.TclError:
				pass
			self._origin_calibration_dlg = None

		# Origin is always the upper-left mechanical limit regardless
		# of plate orientation.
		corner = "upper-left"
		current_well_id = f"{chr(ord('A') + s.y)}{s.x + 1}"

		dlg = tk.Toplevel(self)
		dlg.title("Origin Calibration")
		dlg.transient(self)
		dlg.resizable(False, False)
		self._origin_calibration_dlg = dlg

		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)

		tk.Label(
			body, justify="left", anchor="w", wraplength=460,
			text=(
				"The dispenser has returned to origin and the position "
				"has been tared. Use this opportunity to true-up "
				"against stepper drift:"
			),
		).pack(anchor="w", pady=(0, 8))

		# Checklist
		var_pushed = tk.BooleanVar(value=False)
		var_seated = tk.BooleanVar(value=False)

		def _refresh_resume_state():
			if var_pushed.get() and var_seated.get():
				resume_btn.state(["!disabled"])
			else:
				resume_btn.state(["disabled"])

		cb1 = tk.Checkbutton(
			body, variable=var_pushed,
			text=(
				f"Manually pushed the carriage against the {corner} "
				"mechanical limit"
			),
			anchor="w", justify="left", wraplength=440,
			command=_refresh_resume_state,
		)
		cb1.pack(anchor="w", pady=(0, 2))
		cb2 = tk.Checkbutton(
			body, variable=var_seated,
			text="Verified the carriage is firmly seated against the limit",
			anchor="w", justify="left", wraplength=440,
			command=_refresh_resume_state,
		)
		cb2.pack(anchor="w", pady=(0, 8))

		tk.Label(body, anchor="w", justify="left", fg="#444",
			text="── Return Information ─────────────",
		).pack(anchor="w", pady=(2, 4))
		info_frame = tk.Frame(body)
		info_frame.pack(anchor="w", pady=(0, 8))
		tk.Label(info_frame, anchor="w", justify="left",
			text=f"Last visited well:  {current_well_id}",
		).pack(anchor="w")
		tk.Label(info_frame, anchor="w", justify="left",
			text=f"Returning to:       X = {s.paused_table_cm:.2f} cm",
		).pack(anchor="w")
		tk.Label(info_frame, anchor="w", justify="left",
			text=f"                    Y = {s.paused_carriage_cm:.2f} cm",
		).pack(anchor="w")

		tk.Label(body, anchor="w", justify="left", wraplength=460,
			text=(
				"Click Resume to return to the last visited well and "
				"continue fractionation."
			),
		).pack(anchor="w", pady=(0, 12))

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X)

		def _close_dialog():
			if dlg.winfo_exists():
				try:
					dlg.grab_release()
				except tk.TclError:
					pass
				dlg.destroy()
			self._origin_calibration_dlg = None

		def _cancel(_event=None):
			_close_dialog()
			self.set_status(
				"Origin Calibration cancelled. Run remains paused."
			)
			self._update_run_control_buttons()

		def _resume(_event=None):
			if str(resume_btn.cget("state")) == "disabled":
				return
			_close_dialog()
			self._recalibration_resume()

		def _skip():
			# Operator override: pre-check both boxes so Resume enables.
			# Useful when the operator already trusts the rig (lab veteran)
			# and doesn't need the prompt.
			var_pushed.set(True)
			var_seated.set(True)
			_refresh_resume_state()

		cancel_btn = ttk.Button(btn_row, text="Cancel", command=_cancel)
		cancel_btn.pack(side=tk.LEFT, padx=4)
		skip_btn = ttk.Button(btn_row, text="Skip Checklist (Expert)",
			command=_skip)
		skip_btn.pack(side=tk.LEFT, padx=4)
		resume_btn = ttk.Button(btn_row, text="Resume", command=_resume,
			style="Primary.TButton")
		resume_btn.pack(side=tk.RIGHT, padx=4)
		resume_btn.state(["disabled"])

		dlg.bind("<Escape>", _cancel)
		dlg.bind("<Return>", _resume)
		dlg.protocol("WM_DELETE_WINDOW", _cancel)

		dlg.update_idletasks()
		self._center_over_main(dlg)
		try:
			dlg.grab_set()
		except tk.TclError:
			pass

	def _next_well_after_resume(self):
		"""Pure mirror of ``_snake_step``'s advancing logic so we can
		name the next well without firing any motors. Used only by the
		resume breadcrumb. Column-snake in both orientations: inner
		sweep on ``s.y`` (rows), outer step on ``s.x`` (cols).
		"""
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
		if s.COLS:
			x = min(x, s.COLS - 1)
		return x, y

	# -- Automated fractionation flow ------------------------------------

	def start_run(self, rows, cols, well_size, pump_rate, volume,
			project, sample_id_at_start, plate_id_at_start,
			number_of_fractions, discard_fractions,
			waste_bin_table, waste_bin_carriage,
			table_start, carriage_start, drip_wait_time,
			purge_time, prime_time_s, skip_intersample_purge,
			peristaltic_rate_ml_per_min, max_waste_volume_ml,
			waste_bin_x_extent=0.0, waste_bin_y_extent=0.0):
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

		# Waste-bin projection: protocol-aware estimate of how much the
		# bin gains over the whole run, accounting for D × V × N
		# discards plus inter-sample purges. The bin is the one
		# parameter without a visible main-window readout for its
		# FORWARD projection, so this table is the operator's chance
		# to spot an overflow before clicking Begin.
		waste_now = self.waste_volume_ml
		waste_max = max_waste_volume_ml
		# Sample count: bulk mode walks every loaded sample; otherwise
		# the run handles one sample at a time (operator drives
		# Continue to Next Sample for additional tubes, each of which
		# re-runs Begin, so the per-Begin projection is for one tube).
		n_samples = (
			len(self.bulk_samples) if self.bulk_mode_active else 1
		)
		discard_volume = discard_fractions * volume * n_samples
		# Per-transition purge volume — protocol-dependent. Each
		# peristaltic phase pumps for purge_time at peristaltic_rate;
		# the priming phase pumps for purge_time at the fractionation pump
		# rate. Basic: 2 peri (wash + clear) + 1 syringe (prime);
		# decon: 4 peri (wash + bleach + rinse + clear) + 1 syringe.
		if skip_intersample_purge or n_samples <= 1:
			purge_per_transition = 0.0
		elif self.purge_protocol == "decontamination":
			purge_per_transition = (
				4 * purge_time * peristaltic_rate_ml_per_min / 60.0
				+ purge_time * pump_rate / 3600.0
			)
		else:  # "basic" / default
			purge_per_transition = (
				2 * purge_time * peristaltic_rate_ml_per_min / 60.0
				+ purge_time * pump_rate / 3600.0
			)
		total_purge = max(0, n_samples - 1) * purge_per_transition
		estimated_added = discard_volume + total_purge
		projected_end = waste_now + estimated_added

		# Per-row contextual labels so the table is self-documenting
		# even when D=0 or skip is on.
		if n_samples == 1:
			purges_label = "Purges (none — single sample)"
		elif skip_intersample_purge:
			purges_label = "Purges (skipped)"
		else:
			purges_label = (
				f"Purges ({n_samples - 1} × {self.purge_protocol})"
			)
		waste_rows = [
			("At run start", f"{waste_now:.1f} mL"),
			(
				f"Discards ({discard_fractions} × {volume:.2f} mL × "
				f"{n_samples} sample{'s' if n_samples != 1 else ''})",
				f"{discard_volume:.1f} mL",
			),
			(purges_label, f"{total_purge:.1f} mL"),
			("Total estimated added", f"{estimated_added:.1f} mL"),
			("Projected end-of-run", f"{projected_end:.1f} mL"),
			("Capacity", f"{waste_max:.0f} mL"),
		]
		over_capacity = projected_end > waste_max and waste_max > 0
		logger.debug(
			"Begin waste projection: D=%d V=%.3f N=%d purge_time=%.1f "
			"peri=%.1f syringe=%.1f skip=%s protocol=%s → "
			"discards=%.1f purges=%.1f total=%.1f projected=%.1f",
			discard_fractions, volume, n_samples, purge_time,
			peristaltic_rate_ml_per_min, pump_rate,
			skip_intersample_purge, self.purge_protocol,
			discard_volume, total_purge, estimated_added, projected_end,
		)

		if not self._show_begin_fractionation_dialog(
				sample_id=sample_id_at_start, plate_id=plate_id_at_start,
				waste_rows=waste_rows, over_capacity=over_capacity):
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
		s.prime_time_s = float(prime_time_s)

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
		s.waste_bin_x_extent = max(0.0, float(waste_bin_x_extent))
		s.waste_bin_y_extent = max(0.0, float(waste_bin_y_extent))
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

		# Priming workflow: walk fractionation solution from the
		# sample tube to the syringe dispenser. Step 1 runs the pump
		# in parallel with a needle move to the first-dispense
		# position (waste bin if D > 0, else plate A1). Step 2 lets
		# the operator manually walk until a droplet forms. After the
		# operator clicks Begin Run, _begin_first_phase fires the
		# normal discard / collect logic -- without the move, since
		# priming already parked the needle.
		self._priming_workflow(on_done=self._begin_first_phase)

	def _priming_workflow(self, on_done):
		"""Pre-fractionation priming. Single modal dialog that walks
		fractionation solution from the sample tube up the inlet line
		to the syringe dispenser. Runs once at run start before the
		state machine's first dispense.

		The dialog has two internal states (mirroring the inter-sample
		purge dialog):

		  * ``"priming"`` — the needle moves to its first-dispense
		    target (waste bin if D > 0, else plate A1), then the pump
		    auto-cycles for ``state.prime_time_s`` with a live
		    countdown. Begin Run disabled; Space ignored.

		  * ``"complete"`` — auto-cycle done. Operator Space-toggles
		    extension cycles until a droplet forms at the needle,
		    then clicks Begin Run.

		On Begin Run, ``on_done()`` fires -- the caller's hook into
		the state machine's first dispense phase.
		On Cancel from either state, the run aborts cleanly and the
		application returns to idle.
		"""
		s = self.state
		if s.discards_at_series_start > 0:
			# Shortest-path routing into the bin rectangle. Falls back
			# to the anchor when extents are zero (legacy point target).
			target_x, target_y = self._waste_entry_for_current_position()
			target_label = "waste bin"
			target_is_waste = True
		else:
			target_x, target_y = s.table_start_cm, s.carriage_start_cm
			target_label = "start well (A1)"
			target_is_waste = False
		# Prime-to-start-well output lands in the first plate well as
		# fraction dilution, not in the waste bin. Suppress the real-
		# time waste tracker for the duration of the prime workflow
		# in that case.
		self._suppress_waste_tracking = not target_is_waste
		self._priming_dialog(target_x, target_y, target_label, on_done)

	def _priming_dialog(self, target_x, target_y, target_label, on_done):
		"""Single-dialog priming. See ``_priming_workflow`` for the
		state-machine description.

		Implementation note on the move/countdown ordering: the needle
		move runs *first* (synchronously via ``move_to_positions``,
		which blocks the GUI) before the auto-pump countdown starts.
		The previous design ran them in parallel and waited on a
		"both done" condition, which dead-locked the transition to
		the manual-prime phase. Serial is robust; the move only adds
		a few seconds of latency at run start.
		"""
		s = self.state
		prime_s = max(0.0, float(s.prime_time_s))

		# Destination note — same conditional wording as the inter-
		# sample purge Step 3 dialog. Mirrors the move/logging choice
		# made in _priming_workflow (waste bin when D > 0, start
		# well A1 when D == 0). The D == 0 wording is intentionally
		# reassuring so the operator doesn't worry that sample
		# material is being wasted.
		if s.discards_at_series_start > 0:
			dest_note = (
				"Priming output will be dispensed into the waste bin "
				"— discard fractions are configured, so this material "
				"is discarded along with them."
			)
		else:
			dest_note = (
				"Priming output will be dispensed into well A1 (the "
				"start well) — no discards configured, so this is "
				"collected as sample material. Walk only as much as "
				"needed to form an even droplet at the needle."
			)

		ctx = {"cancelled": False, "state": "priming",
			"tick_after": None, "stop_after": None,
			"auto_start_iso": None, "auto_start_mono": None,
			"is_pumping": False, "ext_count": 0, "ext_total_s": 0.0,
			"ext_start_mono": None, "ext_start_iso": None}

		dlg = tk.Toplevel(self)
		dlg.title("Prime Fractionation Line")
		dlg.transient(self)
		dlg.resizable(False, False)
		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)

		header_lbl = tk.Label(body, justify="left", anchor="w",
			wraplength=460,
			text=f"Needle parked above the {target_label}.")
		header_lbl.pack(anchor="w", pady=(0, 4))

		dest_note_lbl = tk.Label(body, justify="left", anchor="w",
			wraplength=460, fg="#444",
			font=("TkDefaultFont", 9, "italic"),
			text=dest_note)
		dest_note_lbl.pack(anchor="w", pady=(0, 8))

		body_lbl = tk.Label(body, justify="left", anchor="w",
			wraplength=460,
			text="Moving needle into position...")
		body_lbl.pack(anchor="w", pady=(0, 6))

		countdown_lbl = tk.Label(body, anchor="w", text="")
		countdown_lbl.pack(anchor="w")
		pump_lbl = tk.Label(body, text="Pump: OFF",
			font=FONTS["bold"], anchor="w")
		pump_lbl.pack(anchor="w")
		manual_lbl = tk.Label(body, text="", anchor="w")
		manual_lbl.pack(anchor="w", pady=(0, 12))

		btn_row = tk.Frame(body)
		btn_row.pack(fill=tk.X)
		btn_row.grid_columnconfigure(0, weight=1)
		btn_row.grid_columnconfigure(1, weight=1)

		def _cancel_pending_timers():
			for key in ("tick_after", "stop_after"):
				if ctx[key] is not None:
					try:
						self.after_cancel(ctx[key])
					except Exception:
						pass
					ctx[key] = None

		def _cancel():
			ctx["cancelled"] = True
			_cancel_pending_timers()
			# If we cancel mid-extension, the in-flight cycle is left
			# unlogged; matches step2's prior behavior of only logging
			# fully-bracketed Space-on / Space-off pairs.
			self.pump_controller.set_relay(False)
			ctx["is_pumping"] = False
			if dlg.winfo_exists():
				dlg.destroy()
			self._abort_run_from_priming()

		def _begin_run():
			if ctx["state"] != "complete" or ctx["is_pumping"] \
					or ctx["cancelled"]:
				return
			if dlg.winfo_exists():
				dlg.destroy()
			# Suppression flag served its purpose for the prime
			# steps; clear it so subsequent dispense waste tracks
			# normally.
			self._suppress_waste_tracking = False
			on_done()

		cancel_btn = ttk.Button(btn_row, text="Cancel",
			command=_cancel, style="Danger.TButton")
		cancel_btn.grid(row=0, column=0, sticky="w", padx=4)
		begin_btn = ttk.Button(btn_row, text="Begin Run",
			command=_begin_run, style="Primary.TButton")
		begin_btn.grid(row=0, column=1, sticky="e", padx=4)
		begin_btn.state(["disabled"])

		dlg.protocol("WM_DELETE_WINDOW", _cancel)
		dlg.bind("<Escape>", lambda _e: _cancel())

		# ---- Spacebar handler (gated on state == "complete") ----------
		def _log_ext_cycle():
			elapsed = monotonic() - ctx["ext_start_mono"]
			end_iso = datetime.now().isoformat(timespec="milliseconds")
			if self.run_logger is not None:
				try:
					self.run_logger.prime_manual_ext(
						extension_idx=ctx["ext_count"],
						target_x_cm=target_x, target_y_cm=target_y,
						start_iso=ctx["ext_start_iso"],
						end_iso=end_iso, duration_s=elapsed,
					)
				except Exception as exc:
					logger.warning(
						"Failed to log prime_manual_ext: %s", exc)
			ctx["ext_total_s"] += elapsed

		def _ext_tick():
			ctx["tick_after"] = None
			if ctx["cancelled"] or not ctx["is_pumping"]:
				return
			cycle_s = monotonic() - ctx["ext_start_mono"]
			manual_lbl.config(text=(
				f"Manual prime: {ctx['ext_count']} cycle"
				f"{'s' if ctx['ext_count'] != 1 else ''}, "
				f"{ctx['ext_total_s'] + cycle_s:.1f} s total"
			))
			ctx["tick_after"] = self.after(100, _ext_tick)

		def _on_space(_e=None):
			# Gate: only act in the "complete" state. During the
			# initial move and auto-countdown, Space is a no-op.
			if ctx["cancelled"] or ctx["state"] != "complete":
				return "break"
			# Don't toggle if focus is in a text-entry widget (none
			# in this dialog today, but keeps the binding safe if a
			# future change adds an Entry).
			focused = dlg.focus_get()
			if isinstance(focused, (tk.Entry, tk.Text)):
				return None
			if ctx["is_pumping"]:
				if ctx["tick_after"] is not None:
					try:
						self.after_cancel(ctx["tick_after"])
					except Exception:
						pass
					ctx["tick_after"] = None
				self.pump_controller.set_relay(False)
				ctx["is_pumping"] = False
				_log_ext_cycle()
				pump_lbl.config(text="Pump: OFF")
				manual_lbl.config(text=(
					f"Manual prime: {ctx['ext_count']} cycle"
					f"{'s' if ctx['ext_count'] != 1 else ''}, "
					f"{ctx['ext_total_s']:.1f} s total"
				))
				begin_btn.state(["!disabled"])
			else:
				ctx["ext_count"] += 1
				ctx["ext_start_mono"] = monotonic()
				ctx["ext_start_iso"] = datetime.now().isoformat(
					timespec="milliseconds")
				ctx["is_pumping"] = True
				self.pump_controller.set_relay(True)
				pump_lbl.config(text="Pump: ON")
				begin_btn.state(["disabled"])
				_ext_tick()
			return "break"

		dlg.bind("<space>", _on_space)
		dlg.bind("<Return>", lambda _e: _begin_run())
		# Override Tk's class-level Space-activates-button so the
		# focused Begin Run button can't fire its own command when
		# the operator wants to toggle an extension cycle.
		begin_btn.bind("<space>", _on_space)
		cancel_btn.bind("<space>", _on_space)

		# ---- Transition to "complete" state ---------------------------
		def _enter_complete_state():
			ctx["state"] = "complete"
			body_lbl.config(text=(
				"Automatic prime complete. Press Space to walk the "
				"solution further until a droplet forms at the needle. "
				"Press Space again to stop. Click Begin Run when ready."
			))
			countdown_lbl.config(text="")
			pump_lbl.config(text="Pump: OFF")
			manual_lbl.config(
				text="Manual prime: 0 cycles, 0.0 s total")
			begin_btn.state(["!disabled"])
			begin_btn.focus_set()
			# Supplementary push / beep: operator needs to walk the
			# solution to droplet formation and click Begin Run.
			self.notifications.notify(
				title="autoSIP: prime step",
				message=(
					"Automatic prime complete. Walk the sample to "
					"droplet formation, then continue."
				),
			)

		def _auto_done():
			ctx["stop_after"] = None
			if ctx["cancelled"]:
				return
			self.pump_controller.set_relay(False)
			elapsed = monotonic() - ctx["auto_start_mono"]
			end_iso = datetime.now().isoformat(timespec="milliseconds")
			if self.run_logger is not None:
				try:
					self.run_logger.prime_auto(
						target_x_cm=target_x, target_y_cm=target_y,
						start_iso=ctx["auto_start_iso"],
						end_iso=end_iso, duration_s=elapsed,
					)
				except Exception as exc:
					logger.warning("Failed to log prime_auto: %s", exc)
			_enter_complete_state()

		def _auto_tick():
			ctx["tick_after"] = None
			if ctx["cancelled"] or ctx["state"] != "priming":
				return
			remaining = max(0.0,
				prime_s - (monotonic() - ctx["auto_start_mono"]))
			countdown_lbl.config(
				text=f"Prime time: {remaining:.0f} / {prime_s:.0f} s remaining")
			if remaining > 0:
				ctx["tick_after"] = self.after(100, _auto_tick)

		def _start_auto_cycle():
			"""Begin the auto-pump countdown. Pre-condition: needle
			move is complete and dialog is in ``"priming"`` state."""
			if ctx["cancelled"]:
				return
			body_lbl.config(
				text="Walking sample solution toward the dispenser.")
			countdown_lbl.config(
				text=f"Prime time: {prime_s:.0f} / {prime_s:.0f} s remaining")
			self.pump_controller.claim_for("fractionate")
			self.pump_controller.set_relay(True)
			pump_lbl.config(text="Pump: ON")
			ctx["auto_start_iso"] = datetime.now().isoformat(
				timespec="milliseconds")
			ctx["auto_start_mono"] = monotonic()
			ctx["stop_after"] = self.after(
				int(prime_s * 1000), _auto_done)
			_auto_tick()

		def _do_move_then_prime():
			"""Move the needle to the prime target, then start the
			auto-pump countdown. Move is synchronous (blocks the GUI);
			countdown happens entirely after the move returns."""
			if ctx["cancelled"]:
				return
			self.move_to_positions(table_dist=target_x,
				carriage_dist=target_y, is_transit=True)
			# Re-check cancellation in case the user closed the dialog
			# while the move was blocking the GUI mainloop.
			if ctx["cancelled"] or not dlg.winfo_exists():
				return
			_start_auto_cycle()

		dlg.update_idletasks()
		self._center_over_main(dlg)
		# Defer the move by one tick so the dialog renders first;
		# move_to_positions will then block the GUI for a few seconds.
		self.after(50, _do_move_then_prime)

	def _abort_run_from_priming(self):
		"""Operator clicked Cancel in the priming workflow before the
		state machine started dispensing. Tear down the run-in-progress
		state set up by ``start_run`` and return to idle. Pump claim is
		released; run_logger is closed without finalization (the prime
		rows already written stay on disk).
		"""
		self._suppress_waste_tracking = False
		self.pump_controller.set_relay(False)
		if self.pump_controller.claimant is not None:
			self.pump_controller.release()
		if self.run_logger is not None:
			try:
				self.run_logger.close_without_summary()
			except Exception as exc:
				logger.warning("Failed to close run_logger on abort: %s", exc)
			self.run_logger = None
		s = self.state
		s.state = "idle"
		self._set_phase("idle")
		s.is_paused = False
		s.x = 0
		s.y = 0
		s.discards_done = 0
		s.wells_collected = 0
		s.series_index = 0
		self.automated_frame.progress.reset()
		self._update_run_control_buttons()
		self.set_status("Run aborted from priming. System idle.")

	def _begin_first_phase(self):
		"""Fire the run's first dispense phase. Called by the priming
		workflow's Begin Run, by which time the needle is parked at
		the first-dispense target.

		The trailing ``_update_run_control_buttons`` mirrors the same
		call at the end of ``_commit_new_series`` (the sample 2+
		entry point). ``start_run`` ALSO calls
		``_update_run_control_buttons`` before this method fires, but
		that call lands while ``s.state`` is still ``"idle"`` (the
		priming dialog returns before any phase transition) — so
		without this trailing call, the run-control row stays in its
		idle defaults until sample 2 begins, leaving Pause / End Run
		disabled and Return-to-Origin / Return-to-Start-Well enabled
		during all of sample 1's fractionation.
		"""
		s = self.state
		if s.discards_at_series_start > 0:
			self._set_phase("discard")
			self.set_status(
				f"Discard phase at waste bin "
				f"({s.waste_bin_table:.2f} cm, {s.waste_bin_carriage:.2f} cm)..."
			)
			self.automated_frame.progress.set_discard_status(
				0, s.discards_at_series_start)
			self.pump_liquid()
		else:
			self._set_phase("collect")
			self.set_status("Fractionation in progress...")
			self.pump_liquid()
		self._update_run_control_buttons()

	def pump_liquid(self):
		"""Pump-on phase. Behavior depends on s.phase (discard vs collect)."""
		s = self.state
		s.state = "pump"
		self.pump_controller.set_relay(True)
		if s.phase == "discard":
			idx = s.discards_done + 1
			if self.run_logger is not None:
				# Record the ACTUAL bin entry point (shortest-path
				# target), not the bin anchor, so log.csv reflects
				# where the fluid physically went.
				self.run_logger.discard_dispense_start(
					s.series_index, idx,
					s.last_waste_entry_x, s.last_waste_entry_y)
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
					# First series: move to plate A1 (absolute). Transit
					# move — the discard phase is done; we're traversing
					# to the dispense start position with no fluid in
					# flight.
					self.set_status("Moving to plate A1...")
					self.move_to_positions(
						table_dist=s.table_start_cm,
						carriage_dist=s.carriage_start_cm,
						is_transit=True,
					)
				else:
					# Subsequent series: ABSOLUTE move from the waste
					# bin to the next snake well. ``_snake_step`` here
					# would issue a RELATIVE move of one well-size on
					# the assumption that the carriage is parked at the
					# previous well, but the discards just finished at
					# the waste bin — so a relative step would offset
					# from the waste bin and silently dispense into
					# empty table area for the rest of the sample (the
					# software would still believe it's tracking the
					# plate). ``_snake_step_absolute`` advances the
					# snake state and drives the motors absolutely to
					# the well's coordinates.
					if not self._snake_step_absolute():
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

		# Snake-step to next well. Defer to ``_snake_step`` so the same
		# orientation-aware logic drives the inner sweep / outer step
		# direction AND the off-plate check used by ``_commit_new_series``
		# (which calls ``_snake_step`` for the first well of every
		# subsequent sample). Keeping the two code paths in sync via a
		# single implementation is what makes the inter-sample handoff
		# land on the right next well in both orientations — the previous
		# inline step was hard-coded to portrait's column-snake regardless
		# of ``self.plate_orientation``, so in landscape ``move()`` and
		# ``_snake_step`` disagreed about which axis to advance and the
		# next-sample resume jumped to the wrong well.
		on_plate = self._snake_step()

		if s.is_paused:
			return

		if not on_plate:
			# Plate fully traversed -- should be unreachable since the
			# auto-pause above fires first when wells_collected reaches
			# its target (and validation ensures N <= rows*cols).
			# Defensive: fall through to auto-pause-total-reached so the
			# run still finalizes cleanly.
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

		# Supplementary push / beep: phone-side awareness when the
		# operator has stepped away. The on-screen status above is
		# the source of truth.
		self.notifications.notify(
			title="autoSIP: sample complete",
			message=(
				f"Sample {s.current_sample_id} finished. "
				"Inter-sample purge required before the next sample."
			),
		)

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
		# Supplementary push / beep: operator needs to swap the plate.
		self.notifications.notify(
			title="autoSIP: plate full",
			message=f"Plate {s.current_plate_id} is full. Plate swap required.",
		)

	def end_run(self):
		"""Handle the End Run button click.

		Asks the operator to choose Save (finalize with timestamped files)
		or Discard (skip finalization; leave system.start.state.json +
		log.csv on disk untouched). Either way the run transitions to
		idle: motors released, pump claim cleared, visuals reset,
		FractionatorState run counters zeroed so a fresh Begin
		Fractionation starts from a clean slate.

		The confirmation dialog has three buttons: Save and End writes
		end_*.json + summary*.md; Don't Save leaves system.start.state.json
		+ the raw log.csv on disk without finalization; Cancel returns
		out of end_run without changing run state.
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
		# Snapshot the collected-wells total BEFORE the per-run
		# counters reset further down. Used by the run-complete
		# notification message.
		collected_total = len(s.well_records)

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
		# end/summary; the run dir keeps system.start.state.json +
		# log.csv as-is.
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
		# Bring back the empty-plate preview if Plate Parameters are
		# still valid — the preview's plate-label caption overrides the
		# "Plate: {id}" header set above, which is the right behavior
		# for the pre-run state.
		af._refresh_plate_preview()
		af._refresh_table_view()
		# Drop the snake's last-direction memory so subsequent Manual
		# jogs start from a clean backlash-tracking baseline. See
		# ``_reset_motor_direction_state``.
		self._reset_motor_direction_state()
		self._update_run_control_buttons()
		if save:
			self.set_status(f"Run ended ({final_status}). Logs saved.")
		else:
			self.set_status(
				f"Run discarded. Partial log files at {discarded_run_dir} "
				"may be deleted manually."
			)
		# Supplementary push / beep: operator-away notice that the
		# run is finished and the bench is ready for plate unloading.
		# Fires on both Save and Discard; phrased to fit either.
		self.notifications.notify(
			title="autoSIP: run complete",
			message=(
				f"Run finished ({final_status}). "
				f"{collected_total} fraction"
				f"{'s' if collected_total != 1 else ''} collected."
			),
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
				next_discards=new_d_val,
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
			# Transit to the bin's closest interior entry point (with
			# legacy point-target fallback if extents are zero) —
			# fluid hasn't started flowing yet for this series.
			entry_x, entry_y = self._waste_entry_for_current_position()
			self.move_to_positions(
				table_dist=entry_x,
				carriage_dist=entry_y,
				is_transit=True,
			)
			# Record the actual entry point on the state so the
			# per-discard log rows can carry it.
			s.last_waste_entry_x = entry_x
			s.last_waste_entry_y = entry_y
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

	def _start_intersample_purge(self, new_sample_id, next_series_index,
			next_discards, on_done):
		"""Run the inter-sample purge workflow.

		Phase count depends on ``self.purge_protocol``:

		  "basic" (3 phases):
		     1. Connect inlet to water, flush  (peristaltic pump)
		     2. Disconnect from water (in air), clear  (peristaltic pump)
		     3. Connect to new sample, prime syringe  (fractionation pump)

		  "decontamination" (5 phases):
		     1. Sterile water flush                   (peristaltic)
		     2. Bleach flush                          (peristaltic)
		     3. Sterile water rinse                   (peristaltic)
		     4. Air clear                             (peristaltic)
		     5. Connect to new sample, prime syringe  (fractionation pump)

		Each phase opens with the pump OFF. Pressing Space toggles the
		pump on/off; the operator decides when enough fluid has flowed.
		Continue is disabled while the pump is currently ON. There is
		no fixed duration -- the operator may toggle as many times as
		needed; each on→off cycle writes its own log.csv row.

		Cancel turns the pump off if currently on, then aborts the
		workflow and returns the run to the auto-pause state.
		"""
		s = self.state

		# Move to the bin's closest interior entry point. Synchronous
		# via move_to_positions — transit cadence since no fluid is
		# flowing yet. Cache the entry XY on the state so the purge
		# log rows below carry the actual point used.
		self.set_status("Moving to waste bin for inter-sample purge…")
		entry_x, entry_y = self._waste_entry_for_current_position()
		self.move_to_positions(
			table_dist=entry_x,
			carriage_dist=entry_y,
			is_transit=True,
		)
		s.last_waste_entry_x = entry_x
		s.last_waste_entry_y = entry_y
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
			"cycle_extension": 0, "cycle_start_mono": None,
			"cycle_start_iso": None, "auto_done_at": None}

		def _log_current_cycle():
			"""Write the in-flight cycle row to log.csv if one is open.
			Idempotent: clears ctx['cycle_phase'] so a repeat call is
			a no-op. Caller is responsible for stopping the relay and
			cancelling any pending tick BEFORE invoking this helper;
			it only handles the bookkeeping.
			"""
			phase = ctx["cycle_phase"]
			if phase is None:
				return
			if self.run_logger is None:
				ctx["cycle_phase"] = None
				return
			elapsed = monotonic() - ctx["cycle_start_mono"]
			end_iso = datetime.now().isoformat(timespec="milliseconds")
			# Destination coords: the wash/bleach/rinse/clear phases
			# all dispense into the waste bin; the prime phase
			# dispenses into the waste bin or the current well
			# depending on next_discards. ctx["prime_dest_x_cm"] /
			# ["prime_dest_y_cm"] are set at _prime_phase entry to
			# reflect that choice. For non-prime phases the values
			# are absent — fall back to the waste-bin coords.
			if phase == "prime" and "prime_dest_x_cm" in ctx:
				dest_x_cm = ctx["prime_dest_x_cm"]
				dest_y_cm = ctx["prime_dest_y_cm"]
			else:
				# Non-prime purge phases dispense at the cached bin
				# entry point (shortest-path target from the initial
				# purge move). Falls back to the anchor when extents
				# are zero — last_waste_entry_* gets seeded with the
				# anchor in that case.
				dest_x_cm = s.last_waste_entry_x
				dest_y_cm = s.last_waste_entry_y
			try:
				self.run_logger.purge_committed(
					phase=phase, series_index=next_series_index,
					waste_x_cm=dest_x_cm,
					waste_y_cm=dest_y_cm,
					start_iso=ctx["cycle_start_iso"],
					end_iso=end_iso, duration_s=elapsed,
					extension=ctx["cycle_extension"],
					sub_phase=ctx["cycle_sub"],
				)
			except Exception as exc:
				logger.warning(
					"Failed to log purge cycle row: %s", exc)
			ctx["cycle_phase"] = None
			# Per-cycle waste volume is charged by the real-time tracker
			# (started by the PumpController state callback on relay ON,
			# stopped on relay OFF). No end-of-cycle _add_waste call
			# here -- that would double-count.

		def _cancel_and_close():
			"""Abort the purge workflow. Turns the pump off (commits the
			partial cycle to the log) and closes the modal. Re-claims
			"fractionate" so the run's auto-pause state is consistent
			with how it was before the purge started.
			"""
			logger.debug("purge dialog: cancel fired")
			if ctx["tick_after"] is not None:
				try:
					self.after_cancel(ctx["tick_after"])
				except Exception:
					pass
				ctx["tick_after"] = None
			if ctx["is_pumping"]:
				self.pump_controller.set_relay(False)
				ctx["is_pumping"] = False
				_log_current_cycle()
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
					logger.debug("purge dialog: skip checklist fired")
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
			# Intentionally NOT grabbing: the status-bar Reset (and the
			# waste-warning dialog when one is open) must remain
			# clickable so the operator can always recover from a
			# waste-bin trip mid-purge. Most Manual / Cleaning controls
			# are already disabled while a run is active (see
			# _apply_run_active_lock), so the lack of modal grab is
			# safe.
			return (dlg, body_lbl, pump_lbl, cycle_lbl, total_lbl,
				action_btn, set_pump_gate)

		def _run_auto_phase(title, body_text, phase, sub_phase, pump_kind,
				on_advance, checklist, skip_context,
				action_label="Continue", note_text=None,
				cycle_seconds=None):
			"""Auto-cycle pump phase. The operator completes the
			checklist (or clicks Skip), clicks the action button, and
			the pump auto-cycles for ``state.purge_time`` seconds with
			a live countdown. After the auto-cycle, the modal enters
			the "purge complete" state: Space toggles operator-driven
			extension cycles, Continue advances to the next phase.

			``cycle_seconds`` (optional) overrides the per-cycle
			duration. Defaults to ``state.purge_time`` for the wash /
			bleach / rinse / clear phases; the priming phase passes
			``state.prime_time_s`` so its mechanic matches the
			pre-fractionation prime (it walks the NEXT sample's
			fluid up to the needle, not a wash).

			Each automatic cycle writes one log.csv row with
			``extension=0`` (well_id ``purge_{phase}_{N}``); each
			extension press-on → press-off pair writes its own row
			with ``extension=M`` (well_id ``..._extM``).

			``pump_kind`` selects which physical pump should be wired
			into the relay -- ``"peristaltic"`` for water / bleach /
			clear, ``"syringe"`` for priming. State-machine driven
			relay-claim swap (no confirmation prompt) so the status-
			bar pump indicator tracks the operator-visible pump.
			"""
			if ctx["cancelled"]:
				return
			_claim("purge" if pump_kind == "peristaltic" else "fractionate")

			# ``complete`` flips True the moment _enter_complete_state
			# runs so the Space-bar handler can gate extensions on it
			# without inspecting Tk button state.
			phase_state = {"ext": 0, "total_ext_s": 0.0, "complete": False}

			(dlg, body_lbl, pump_lbl, cycle_lbl, total_lbl,
				action_btn, set_pump_gate) = _build_modal(
					title, body_text, action_label,
					lambda: _start_auto_cycle(),
					checklist=checklist, skip_context=skip_context,
					note_text=note_text,
				)

			def _begin_cycle(extension):
				"""Common setup for both auto-cycle (extension=0) and
				operator-driven extension cycles (extension >= 1).
				Stamps ctx with cycle metadata so _log_current_cycle()
				and Cancel-mid-pump can recover the right phase + suffix.
				"""
				ctx["cycle_phase"] = phase
				ctx["cycle_sub"] = sub_phase or ""
				ctx["cycle_extension"] = extension
				ctx["cycle_start_mono"] = monotonic()
				ctx["cycle_start_iso"] = datetime.now().isoformat(
					timespec="milliseconds")
				ctx["is_pumping"] = True
				self.pump_controller.set_relay(True)
				pump_lbl.config(text="Pump: ON")
				set_pump_gate(True)  # disables Continue while pumping

			def _phase_cycle_seconds():
				"""Resolve the auto-cycle duration for this phase. The
				priming phase passes an explicit override so it uses
				prime_time; all other phases fall back to purge_time."""
				if cycle_seconds is not None:
					return max(0.1, float(cycle_seconds))
				return max(0.1, float(s.purge_time))

			def _start_auto_cycle():
				"""Operator clicked the action button. Hide the setup
				prompt, kick off the auto-countdown using the cycle
				duration resolved by ``_phase_cycle_seconds``."""
				cur_pt = _phase_cycle_seconds()
				logger.debug(
					"purge dialog: primary action (%s) fired; phase=%s cycle=%.1f",
					action_label, phase, cur_pt)
				body_lbl.config(text="Pumping...")
				_begin_cycle(extension=0)
				ctx["auto_done_at"] = monotonic() + cur_pt
				_auto_tick(cur_pt)

			def _auto_tick(total_s):
				if ctx["cancelled"] or not ctx["is_pumping"]:
					ctx["tick_after"] = None
					return
				if self._purge_halted_for_waste:
					# Waste-bin threshold halted the cycle mid-pump.
					# Stop the pump, log the partial, and transition
					# the modal to the SAME complete state the natural
					# countdown-end would produce -- the waste warning
					# dialog conveys the waste situation, and the
					# operator's next moves (Space to extend, Continue
					# to advance) are the same either way.
					_stop_pump_now()
					_enter_complete_state()
					return
				remaining = ctx["auto_done_at"] - monotonic()
				if remaining <= 0:
					cycle_lbl.config(
						text=f"Pumping... 0.0 / {total_s:.0f} s remaining")
					ctx["tick_after"] = None
					_finish_auto_cycle()
					return
				cycle_lbl.config(
					text=f"Pumping... {remaining:.1f} / {total_s:.0f} s remaining")
				ctx["tick_after"] = self.after(100, lambda: _auto_tick(total_s))

			def _finish_auto_cycle():
				"""Clean auto-cycle end. Stop the pump, log the row,
				transition to the complete-state UI."""
				_stop_pump_now()
				_enter_complete_state()

			def _stop_pump_now():
				"""Common pump-off path. Cancels any pending tick,
				flips the relay, logs the in-flight cycle."""
				if ctx["tick_after"] is not None:
					try:
						self.after_cancel(ctx["tick_after"])
					except Exception:
						pass
					ctx["tick_after"] = None
				self.pump_controller.set_relay(False)
				ctx["is_pumping"] = False
				_log_current_cycle()

			def _enter_complete_state():
				# Always the SAME complete-state UI regardless of whether
				# the cycle ended via natural countdown or a waste-bin
				# threshold halt. The waste warning dialog (when present)
				# conveys the waste-bin situation; the purge dialog
				# stays in its normal post-cycle state.
				phase_state["complete"] = True
				cur_pt = _phase_cycle_seconds()
				pump_lbl.config(text="Pump: OFF")
				cycle_lbl.config(
					text=f"Auto-cycle complete ({cur_pt:.0f} s).")
				if phase_state["ext"]:
					total_lbl.config(text=(
						f"Extensions: {phase_state['ext']} cycle"
						f"{'s' if phase_state['ext'] != 1 else ''}, "
						f"{phase_state['total_ext_s']:.1f} s total."
					))
				else:
					total_lbl.config(text="")
				body_lbl.config(text=(
					"Inspect the tubing. Press Space to extend pumping if "
					"you need more time; click Continue to advance to the "
					"next phase."
				))
				action_btn.config(text="Continue", command=_on_continue)
				set_pump_gate(False)
				action_btn.focus_set()
				# Supplementary push / beep only for the prime phase
				# of the inter-sample purge — that's the manual-
				# intervention point per the spec. Wash / bleach /
				# rinse / clear phases complete passively to a
				# Continue button and don't need a notification.
				if phase == "prime":
					self.notifications.notify(
						title="autoSIP: prime step",
						message=(
							"Automatic prime complete. Walk the "
							"sample to droplet formation, then "
							"continue."
						),
					)

			def _start_extension():
				phase_state["ext"] += 1
				_begin_cycle(extension=phase_state["ext"])
				_extension_tick()

			def _extension_tick():
				if ctx["cancelled"] or not ctx["is_pumping"]:
					ctx["tick_after"] = None
					return
				if self._purge_halted_for_waste:
					# Charge the partial extension duration before
					# _stop_pump_now clears the cycle metadata so the
					# Total extended counter stays accurate. Same
					# complete-state transition as a clean Space-toggle
					# stop -- no special halted-by-waste UI.
					if ctx["cycle_start_mono"] is not None:
						phase_state["total_ext_s"] += (
							monotonic() - ctx["cycle_start_mono"])
					_stop_pump_now()
					_enter_complete_state()
					return
				cycle_s = monotonic() - ctx["cycle_start_mono"]
				cycle_lbl.config(
					text=f"Extending — this cycle: {cycle_s:.1f} s")
				total_lbl.config(text=(
					f"Total extended: "
					f"{phase_state['total_ext_s'] + cycle_s:.1f} s "
					f"({phase_state['ext']} extension"
					f"{'s' if phase_state['ext'] != 1 else ''})"
				))
				ctx["tick_after"] = self.after(100, _extension_tick)

			def _stop_extension():
				cycle_s = monotonic() - ctx["cycle_start_mono"]
				_stop_pump_now()
				phase_state["total_ext_s"] += cycle_s
				_enter_complete_state()

			def _on_space(_e=None):
				logger.debug(
					"purge dialog: space fired; complete=%s is_pumping=%s",
					phase_state["complete"], ctx["is_pumping"])
				if ctx["cancelled"]:
					return "break"
				# Space is a no-op until the auto-cycle has completed.
				# After completion, Space toggles extension cycles.
				if not phase_state["complete"]:
					return "break"
				if ctx["is_pumping"]:
					_stop_extension()
				else:
					_start_extension()
				return "break"

			def _on_continue(_e=None):
				logger.debug(
					"purge dialog: continue fired; is_pumping=%s",
					ctx["is_pumping"])
				if ctx["is_pumping"] or ctx["cancelled"]:
					return
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
			# Override the focused-button Space activation so the
			# Continue button can't fire its own command when the
			# operator wants to toggle an extension cycle.
			action_btn.bind("<space>", _on_space)

		# -- Phase definitions -------------------------------------------
		protocol = self.purge_protocol
		decon = (protocol == "decontamination")
		total = 5 if decon else 3

		def _wash_phase(step_no):
			cur_pt = float(s.purge_time)
			_run_auto_phase(
				title=f"Inter-sample Purge — Step {step_no} of {total}",
				body_text=(
					f"Click Start Purge to run the peristaltic pump for "
					f"{cur_pt:.0f} s, drawing sterile water through the "
					"tubing."
				),
				phase="wash", sub_phase="", pump_kind="peristaltic",
				on_advance=_phase_next_after_wash,
				checklist=[
					"Disengaged the syringe from the collector tube",
					"Discarded the used syringe",
					"Attached the collector tube to the wash line",
					"Disconnected inlet line from previous sample tube",
					"Placed inlet line in water container",
				],
				skip_context=f"purge_phase_{step_no}_{next_series_index}",
				action_label="Start Purge",
			)

		def _phase_next_after_wash():
			if decon:
				_bleach_phase()
			else:
				_clear_phase(step_no=2)

		def _bleach_phase():
			cur_pt = float(s.purge_time)
			_run_auto_phase(
				title=f"Inter-sample Purge — Step 2 of {total}",
				body_text=(
					f"Click Purge to run the peristaltic pump for "
					f"{cur_pt:.0f} s, pumping 0.5% sodium hypochlorite "
					"(bleach) solution through the tubing to decontaminate."
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
				action_label="Purge",
			)

		def _rinse_phase():
			cur_pt = float(s.purge_time)
			_run_auto_phase(
				title=f"Inter-sample Purge — Step 3 of {total}",
				body_text=(
					f"Click Purge to run the peristaltic pump for "
					f"{cur_pt:.0f} s, rinsing the tubing with sterile "
					"water to remove residual bleach."
				),
				phase="wash", sub_phase="rinse", pump_kind="peristaltic",
				on_advance=lambda: _clear_phase(step_no=4),
				checklist=[
					"Removed inlet line from bleach solution",
					"Placed inlet line in sterile water",
				],
				skip_context=f"purge_phase_3_{next_series_index}",
				action_label="Purge",
			)

		def _clear_phase(step_no):
			cur_pt = float(s.purge_time)
			_run_auto_phase(
				title=f"Inter-sample Purge — Step {step_no} of {total}",
				body_text=(
					f"Click Purge to run the peristaltic pump for "
					f"{cur_pt:.0f} s, pushing air through the tubing to "
					"clear residual liquid."
				),
				phase="clear", sub_phase="", pump_kind="peristaltic",
				on_advance=lambda: _prime_phase(step_no=step_no + 1),
				checklist=[
					"Disconnected the inlet line from the water container",
					"Line is in air, nothing dripping",
				],
				skip_context=f"purge_phase_{step_no}_{next_series_index}",
				action_label="Purge",
			)

		def _prime_phase(step_no):
			# This phase walks the NEXT sample's fractionation
			# solution up to the needle — same mechanic as the
			# pre-fractionation prime. Cycle duration is prime_time
			# (not purge_time), and the framing emphasizes that
			# sample solution is moving, not a wash.
			prime_s = float(s.prime_time_s)
			# Destination depends on the NEXT sample's discard count
			# (parsed from the edited entry box when the operator
			# clicked Continue to Next Sample):
			#   - D > 0: discards are configured, so prime output is
			#     discarded along with them. Needle stays at the waste
			#     bin (already parked there from the wash/bleach/rinse
			#     phases).
			#   - D == 0: no discards, so prime output is collected
			#     sample material. The needle must return from the
			#     waste bin to the current well so the output lands
			#     in the well, not in waste — and so the operator's
			#     visual position matches what the dialog promises.
			if next_discards > 0:
				# The needle is already at the bin entry point from
				# the initial purge move; keep dispensing there.
				dest_x_cm = s.last_waste_entry_x
				dest_y_cm = s.last_waste_entry_y
				dest_note = (
					"Priming output will be dispensed into the waste "
					"bin — discard fractions are configured for the "
					"next sample, so this material is discarded along "
					"with them."
				)
			else:
				current_well_id = f"{chr(ord('A') + s.y)}{s.x + 1}"
				if self.plate_orientation == "portrait":
					# Portrait: rows east (+X), cols NORTH (-Y).
					dest_x_cm = s.table_start_cm + s.y * s.well_size
					dest_y_cm = s.carriage_start_cm - s.x * s.well_size
				else:
					# Landscape: cols east (+X), rows south (+Y).
					dest_x_cm = s.table_start_cm + s.x * s.well_size
					dest_y_cm = s.carriage_start_cm + s.y * s.well_size
				# Physically return the needle from the waste bin to
				# the current well BEFORE the dialog opens. Transit
				# cadence (no fluid flowing) — same pattern as the
				# waste-bin move at the top of _start_intersample_purge.
				self.set_status(
					f"Moving to well {current_well_id} for priming…")
				self.move_to_positions(
					table_dist=dest_x_cm,
					carriage_dist=dest_y_cm,
					is_transit=True,
				)
				self.set_status("Inter-sample purge: awaiting user.")
				dest_note = (
					f"Priming output will be dispensed into well "
					f"{current_well_id} — no discards configured, so "
					"this is collected as sample material. Walk only "
					"as much as needed to form an even droplet at the "
					"needle."
				)
			# Record the destination for _log_current_cycle so the
			# prime-phase log rows carry the actual destination cm
			# (well coords when D == 0, waste-bin coords when D > 0).
			ctx["prime_dest_x_cm"] = dest_x_cm
			ctx["prime_dest_y_cm"] = dest_y_cm
			_run_auto_phase(
				title=f"Inter-sample Purge — Step {step_no} of {total}",
				body_text=(
					"Priming the next sample. Walk the sample "
					"fractionation solution up to the syringe dispenser "
					"until droplets form evenly, readying the line for "
					"the next fractionation series.\n\n"
					f"Click Prime sample to run the fractionation pump for "
					f"{prime_s:.0f} s, then press Space to extend "
					"pumping until droplets form evenly at the needle."
				),
				phase="prime", sub_phase="", pump_kind="syringe",
				action_label="Prime sample",
				on_advance=_finish,
				checklist=[
					"Attached a new syringe to the collector tube",
					f"Connected the next sample tube ({new_sample_id}) "
					"to the fractionation line",
				],
				skip_context=f"purge_phase_{step_no}_{next_series_index}",
				cycle_seconds=prime_s,
				note_text=dest_note,
			)

		def _finish():
			# Workflow complete -- the fractionation pump is the active
			# claimant (we claimed "fractionate" entering the priming
			# phase). The run continues into the new sample's discard
			# phase via the on_done callback.
			if not ctx["cancelled"]:
				on_done()

		_wash_phase(step_no=1)

	def _sysclean_get_logger(self):
		"""Return ``(logger, owned_by_sysclean)`` for a System Clean
		session.

		If a fractionation run_logger is currently active (System Clean
		was launched during a paused run), reuse it — sysclean rows
		land in the run's ``log.csv`` and the caller does NOT close it.

		Otherwise spin up a fresh ``RunLogger`` pointed at
		``logs/system_clean/{timestamp}/`` so each standalone session
		gets its own directory. ``owned_by_sysclean=True`` signals the
		caller to ``close_without_summary()`` at the end.

		Returns ``(None, False)`` on disk-error fallback; the routine
		runs without logging in that case.
		"""
		if self.run_logger is not None:
			return self.run_logger, False
		try:
			timestamp = datetime.now().isoformat(timespec="milliseconds")
			# Default base_dir is repo_root/logs; with project=system_clean,
			# RunLogger.start() builds logs/system_clean/{ts}_session/.
			rl = run_logger.RunLogger(
				get_current_run_id=lambda: {
					"project": "system_clean",
					"sample_id": "",
					"plate_id": "",
				},
			)
			rl.start({
				"timestamp_start": timestamp,
				"project": "system_clean",
				"sample_id_at_start": "session",
			})
			return rl, True
		except OSError as exc:
			logger.warning("Failed to set up sysclean logger: %s", exc)
			return None, False

	def _start_system_clean(self, launched_during_pause=False):
		"""Four-phase decontamination routine launched from Cleaning
		Mode. More stringent than the inter-sample purge: bleach is
		left static in the line to soak between fill and rinse.
		System Clean intentionally does NOT prime with sample
		solution — it's usually run before any sample is loaded.
		Priming is the inter-sample purge's final phase or the
		pre-fractionation prime workflow's job.

		Phases:
		  1. Bleach fill (peristaltic, auto countdown + Space extension)
		  2. Bleach soak (timed wait, pump off, mm:ss countdown, Skip)
		  3. Water rinse 1 (peristaltic, auto + extension)
		  4. Water rinse 2 (peristaltic, auto + extension)

		When ``launched_during_pause`` is True, an italic note shows in
		Phase 1 reminding the operator the automated run is still
		paused, and the routine restores the "fractionate" claim on
		finish so the paused run can resume cleanly. From idle, the
		routine releases any pump claim it acquired at the end.

		Logging: System Clean rows go through ``sysclean_committed``
		on whatever logger ``_sysclean_get_logger`` returns — the
		active run_logger when launched-during-pause, or a fresh
		``logs/system_clean/{timestamp}/`` standalone logger from idle.
		"""
		s = self.state

		# Soak time is collected per invocation in the Phase 1 dialog
		# (default 5 min, range 0-30, captured at Start Bleach Fill).
		# Stored as a mutable dict so Phase 1's submit handler can
		# update the value that Phase 2's countdown reads. Each
		# invocation starts at the 5-minute default — does NOT carry
		# over between System Clean runs.
		soak_state = {"seconds": 5.0 * 60.0, "minutes": 5.0}

		# Live read purge time (seconds) — used by all four pumping
		# phases. Phases 1, 3, 4 share the inter-sample purge cadence.
		try:
			purge_seconds = max(0.1, float(self.purge_time_var.get()))
		except (TypeError, ValueError):
			purge_seconds = max(0.1, float(s.purge_time or 30.0))

		# Shortest-path routing into the bin (point-target fallback if
		# extents are zero). Cached on state so the sysclean log rows
		# below carry the actual entry point.
		waste_x, waste_y = self._waste_entry_for_current_position()
		s.last_waste_entry_x = waste_x
		s.last_waste_entry_y = waste_y

		self.set_status("Moving to waste bin for System Clean…")
		self.update_idletasks()
		self.move_to_positions(table_dist=waste_x, carriage_dist=waste_y,
			is_transit=True)

		sysclean_logger, owned_by_sysclean = self._sysclean_get_logger()

		def _claim(name):
			"""Force the relay claim to ``name`` (idempotent)."""
			pc = self.pump_controller
			if pc.claimant == name:
				return
			if pc.claimant is not None:
				pc.release()
			pc.claim_for(name)

		def _restore_pre_run_claim():
			"""End-of-routine claim restore. If launched during a paused
			run, re-claim ``"fractionate"`` so the run resumes cleanly.
			From idle, release any claim we still hold."""
			if launched_during_pause:
				_claim("fractionate")
			elif self.pump_controller.claimant is not None:
				self.pump_controller.release()

		def _close_sysclean_logger():
			if owned_by_sysclean and sysclean_logger is not None:
				try:
					sysclean_logger.close_without_summary()
				except Exception as exc:
					logger.warning("Failed to close sysclean logger: %s", exc)

		ctx = {"cancelled": False, "modal": None,
			"is_pumping": False, "tick_after": None,
			"cycle_phase": None, "cycle_extension": 0,
			"cycle_start_mono": None, "cycle_start_iso": None,
			"auto_done_at": None}

		def _log_current_cycle():
			"""Write the in-flight cycle row. Idempotent: clears
			ctx['cycle_phase'] so a repeat call is a no-op."""
			phase = ctx["cycle_phase"]
			if phase is None:
				return
			if sysclean_logger is None:
				ctx["cycle_phase"] = None
				return
			elapsed = monotonic() - ctx["cycle_start_mono"]
			end_iso = datetime.now().isoformat(timespec="milliseconds")
			try:
				# ``waste_x`` / ``waste_y`` already hold the
				# shortest-path entry point (cached on state above);
				# pass them through so the log carries the actual
				# dispense location, not the bin anchor.
				sysclean_logger.sysclean_committed(
					phase=phase,
					start_iso=ctx["cycle_start_iso"],
					end_iso=end_iso, duration_s=elapsed,
					extension=ctx["cycle_extension"],
					waste_x_cm=waste_x, waste_y_cm=waste_y,
				)
			except Exception as exc:
				logger.warning("Failed to log sysclean cycle: %s", exc)
			ctx["cycle_phase"] = None

		def _cancel_and_close():
			"""Abort: stop pump, log partial cycle, close modal,
			restore claim, close standalone logger."""
			logger.debug("sysclean dialog: cancel fired")
			if ctx["tick_after"] is not None:
				try:
					self.after_cancel(ctx["tick_after"])
				except Exception:
					pass
				ctx["tick_after"] = None
			if ctx["is_pumping"]:
				self.pump_controller.set_relay(False)
				ctx["is_pumping"] = False
				_log_current_cycle()
			ctx["cancelled"] = True
			if ctx["modal"] is not None:
				try:
					ctx["modal"].destroy()
				except Exception:
					pass
				ctx["modal"] = None
			_restore_pre_run_claim()
			_close_sysclean_logger()
			self.set_status(
				"System Clean cancelled. Run remains paused; resume "
				"from Automated tab."
				if launched_during_pause
				else "System Clean cancelled. System idle."
			)

		def _build_modal(title, body_text, action_label, action_cmd, *,
				checklist=None, note_text=None,
				after_checklist=None):
			"""Build a sysclean phase modal. Layout: optional checklist
			→ optional after-checklist extras → optional italic note →
			body text → pump status block → [Cancel] [action_btn] row.
			Action button disabled while any checklist item is
			unchecked or pump is ON.

			``after_checklist`` (optional) is a factory called with the
			body Frame as its argument; it may add widgets (e.g. the
			Phase 1 Bleach soak time entry) and return a validator
			callable. The validator runs from the action button's
			handler and should return True on success, False on
			validation failure (in which case the action button's main
			command is short-circuited)."""
			dlg = tk.Toplevel(self)
			dlg.title(title)
			dlg.transient(self)
			dlg.resizable(False, False)
			dlg.protocol("WM_DELETE_WINDOW", _cancel_and_close)
			dlg.bind("<Escape>", lambda _e: _cancel_and_close())
			body = tk.Frame(dlg, padx=14, pady=12)
			body.pack(fill=tk.BOTH, expand=True)

			check_vars = []
			if checklist:
				cl = tk.Frame(body)
				cl.pack(anchor="w", fill=tk.X, pady=(0, 8))
				for i, item in enumerate(checklist):
					v = tk.IntVar(value=0)
					check_vars.append(v)
					ttk.Checkbutton(cl, text=item, variable=v).grid(
						row=i, column=0, sticky="w", pady=1)

			# Hook for phase-specific extras (Phase 1's soak-time
			# entry). Runs between the checklist and the italic note.
			extras_validator = None
			if after_checklist is not None:
				extras_validator = after_checklist(body)

			if note_text:
				tk.Label(body, text=note_text, justify="left", anchor="w",
					wraplength=460, fg=PALETTE["fg_muted"],
					font=(FONTS["family"], FONTS["size"], "italic"),
				).pack(anchor="w", pady=(0, 8))

			body_lbl = tk.Label(body, text=body_text, justify="left",
				wraplength=460, anchor="w")
			body_lbl.pack(anchor="w", pady=(0, 10))

			pump_block = tk.Frame(body)
			pump_block.pack(anchor="w", fill=tk.X, pady=(0, 10))
			pump_lbl = tk.Label(pump_block, text="Pump: OFF",
				font=FONTS["bold"], anchor="w")
			pump_lbl.pack(anchor="w")
			cycle_lbl = tk.Label(pump_block, text="This cycle: 0.0 s",
				anchor="w")
			cycle_lbl.pack(anchor="w")
			total_lbl = tk.Label(pump_block, text="", anchor="w")
			total_lbl.pack(anchor="w")

			btn_row = tk.Frame(body)
			btn_row.pack(fill=tk.X)
			btn_row.grid_columnconfigure(0, weight=1)
			btn_row.grid_columnconfigure(1, weight=1)
			cancel_btn = ttk.Button(btn_row, text="Cancel",
				command=_cancel_and_close, style="Danger.TButton")
			cancel_btn.grid(row=0, column=0, sticky="w", padx=4)
			action_btn = ttk.Button(btn_row, text=action_label,
				command=action_cmd, style="Primary.TButton")
			action_btn.grid(row=0, column=1, sticky="e", padx=4)

			gate = {"pump_on": False}

			def _recompute():
				if gate["pump_on"]:
					action_btn.state(["disabled"])
					return
				if checklist:
					if all(v.get() == 1 for v in check_vars):
						action_btn.state(["!disabled"])
					else:
						action_btn.state(["disabled"])
				else:
					action_btn.state(["!disabled"])

			def set_pump_gate(on):
				gate["pump_on"] = bool(on)
				_recompute()

			if checklist:
				for v in check_vars:
					v.trace_add("write", lambda *_: _recompute())
			_recompute()

			ctx["modal"] = dlg
			dlg.update_idletasks()
			self._center_over_main(dlg)
			return (dlg, body_lbl, pump_lbl, cycle_lbl, total_lbl,
				action_btn, set_pump_gate, extras_validator)

		def _run_auto_phase(*, title, body_text, phase, pump_kind,
				on_advance, checklist=None, action_label="Continue",
				note_text=None, after_checklist=None):
			"""Auto-cycle pump phase with operator-Space extension. Same
			shape as the inter-sample purge's _run_auto_phase but
			parameterized for sysclean's logging path.

			``after_checklist`` (optional) lets a phase add extra
			widgets between the checklist and the body text (e.g.
			Phase 1's Bleach soak time entry). The factory's returned
			validator is invoked from the action-button handler before
			the auto cycle starts; if it returns False the action is
			aborted (e.g. invalid soak input)."""
			if ctx["cancelled"]:
				return
			_claim("purge" if pump_kind == "peristaltic" else "fractionate")

			phase_state = {"ext": 0, "total_ext_s": 0.0, "complete": False}

			(dlg, body_lbl, pump_lbl, cycle_lbl, total_lbl,
				action_btn, set_pump_gate,
				extras_validator) = _build_modal(
					title, body_text, action_label,
					lambda: _start_auto_cycle(),
					checklist=checklist, note_text=note_text,
					after_checklist=after_checklist,
				)

			def _begin_cycle(extension):
				ctx["cycle_phase"] = phase
				ctx["cycle_extension"] = extension
				ctx["cycle_start_mono"] = monotonic()
				ctx["cycle_start_iso"] = datetime.now().isoformat(
					timespec="milliseconds")
				ctx["is_pumping"] = True
				self.pump_controller.set_relay(True)
				pump_lbl.config(text="Pump: ON")
				set_pump_gate(True)

			def _start_auto_cycle():
				logger.debug(
					"sysclean dialog: primary action (%s) fired; phase=%s purge_time=%.1f",
					action_label, phase, purge_seconds)
				# Phase-specific gating (Phase 1: Bleach soak time
				# entry must parse cleanly into the valid range).
				# Failure leaves the dialog open with an inline error.
				if extras_validator is not None and not extras_validator():
					return
				body_lbl.config(text="Pumping…")
				_begin_cycle(extension=0)
				ctx["auto_done_at"] = monotonic() + purge_seconds
				_auto_tick()

			def _stop_pump_now():
				if ctx["tick_after"] is not None:
					try:
						self.after_cancel(ctx["tick_after"])
					except Exception:
						pass
					ctx["tick_after"] = None
				self.pump_controller.set_relay(False)
				ctx["is_pumping"] = False
				_log_current_cycle()

			def _enter_complete_state():
				phase_state["complete"] = True
				pump_lbl.config(text="Pump: OFF")
				cycle_lbl.config(
					text=f"Auto-cycle complete ({purge_seconds:.0f} s).")
				if phase_state["ext"]:
					total_lbl.config(text=(
						f"Extensions: {phase_state['ext']} cycle"
						f"{'s' if phase_state['ext'] != 1 else ''}, "
						f"{phase_state['total_ext_s']:.1f} s total."
					))
				else:
					total_lbl.config(text="")
				body_lbl.config(text=(
					"Inspect the tubing. Press Space to extend pumping "
					"if you need more time; click Continue to advance "
					"to the next phase."
				))
				action_btn.config(text="Continue", command=_on_continue)
				set_pump_gate(False)
				action_btn.focus_set()

			def _finish_auto_cycle():
				_stop_pump_now()
				_enter_complete_state()

			def _auto_tick():
				if ctx["cancelled"] or not ctx["is_pumping"]:
					ctx["tick_after"] = None
					return
				remaining = ctx["auto_done_at"] - monotonic()
				if remaining <= 0:
					cycle_lbl.config(
						text=f"Pumping… 0.0 / {purge_seconds:.0f} s remaining")
					ctx["tick_after"] = None
					_finish_auto_cycle()
					return
				cycle_lbl.config(
					text=f"Pumping… {remaining:.1f} / {purge_seconds:.0f} s remaining")
				ctx["tick_after"] = self.after(100, _auto_tick)

			def _start_extension():
				phase_state["ext"] += 1
				_begin_cycle(extension=phase_state["ext"])
				_extension_tick()

			def _extension_tick():
				if ctx["cancelled"] or not ctx["is_pumping"]:
					ctx["tick_after"] = None
					return
				cycle_s = monotonic() - ctx["cycle_start_mono"]
				cycle_lbl.config(
					text=f"Extending — this cycle: {cycle_s:.1f} s")
				total_lbl.config(text=(
					f"Total extended: "
					f"{phase_state['total_ext_s'] + cycle_s:.1f} s "
					f"({phase_state['ext']} extension"
					f"{'s' if phase_state['ext'] != 1 else ''})"
				))
				ctx["tick_after"] = self.after(100, _extension_tick)

			def _stop_extension():
				cycle_s = monotonic() - ctx["cycle_start_mono"]
				_stop_pump_now()
				phase_state["total_ext_s"] += cycle_s
				_enter_complete_state()

			def _on_space(_e=None):
				if ctx["cancelled"] or not phase_state["complete"]:
					return "break"
				if ctx["is_pumping"]:
					_stop_extension()
				else:
					_start_extension()
				return "break"

			def _on_continue(_e=None):
				if ctx["is_pumping"] or ctx["cancelled"]:
					return
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
			# Override the focused-button Space activation so the
			# action button can't fire its own command when the
			# operator wants to toggle an extension cycle.
			action_btn.bind("<space>", _on_space)

		def _run_soak_phase(*, title, on_advance):
			"""Phase 2: timed wait with the pump OFF. mm:ss countdown
			with a Skip soak button to advance early. Reads the
			captured Phase 1 soak duration from ``soak_state["seconds"]``.
			Writes a single ``sysclean_soak`` log row with ``duration_s``
			set to the actual elapsed soak seconds (whether natural
			countdown end or operator Skip)."""
			if ctx["cancelled"]:
				return
			# Defensive: pump should already be off after Phase 1's
			# Continue, but make absolutely sure during the soak.
			if self.pump_controller.relay_on:
				self.pump_controller.set_relay(False)
			ctx["is_pumping"] = False

			# Snapshot the captured Phase 1 duration. Done here (not
			# at start_system_clean entry) so the operator's entry
			# in Phase 1 is what gets used.
			soak_seconds = soak_state["seconds"]

			dlg = tk.Toplevel(self)
			dlg.title(title)
			dlg.transient(self)
			dlg.resizable(False, False)
			dlg.protocol("WM_DELETE_WINDOW", _cancel_and_close)
			dlg.bind("<Escape>", lambda _e: _cancel_and_close())
			body = tk.Frame(dlg, padx=14, pady=12)
			body.pack(fill=tk.BOTH, expand=True)

			tk.Label(body, justify="left", anchor="w", wraplength=460,
				text=(
					"Bleach is soaking in the line. Pump is off.\n\n"
					"Click Skip soak to advance immediately."
				),
			).pack(anchor="w", pady=(0, 8))

			pump_lbl = tk.Label(body, text="Pump: OFF",
				font=FONTS["bold"], anchor="w")
			pump_lbl.pack(anchor="w")

			# Initial countdown render: mm:ss / mm:ss remaining.
			total_mins = int(soak_seconds // 60)
			total_secs = int(soak_seconds - total_mins * 60)
			total_str = f"{total_mins:d}:{total_secs:02d}"
			countdown_lbl = tk.Label(body, anchor="w",
				text=f"Soak time: {total_str} / {total_str} remaining")
			countdown_lbl.pack(anchor="w", pady=(0, 12))

			btn_row = tk.Frame(body)
			btn_row.pack(fill=tk.X)
			btn_row.grid_columnconfigure(0, weight=1)
			btn_row.grid_columnconfigure(1, weight=1)

			ctx["modal"] = dlg
			tick_state = {
				"start_mono": monotonic(),
				"start_iso": datetime.now().isoformat(timespec="milliseconds"),
				"done_at": monotonic() + soak_seconds,
				"tick_after": None,
				"advanced": False,
			}

			def _finalize(advance):
				if tick_state["advanced"]:
					return
				tick_state["advanced"] = True
				if tick_state["tick_after"] is not None:
					try:
						self.after_cancel(tick_state["tick_after"])
					except Exception:
						pass
					tick_state["tick_after"] = None
				elapsed = monotonic() - tick_state["start_mono"]
				end_iso = datetime.now().isoformat(timespec="milliseconds")
				if sysclean_logger is not None:
					try:
						sysclean_logger.sysclean_committed(
							phase="soak",
							start_iso=tick_state["start_iso"],
							end_iso=end_iso, duration_s=elapsed,
							waste_x_cm=waste_x, waste_y_cm=waste_y,
						)
					except Exception as exc:
						logger.warning(
							"Failed to log sysclean soak: %s", exc)
				if dlg.winfo_exists():
					dlg.destroy()
				ctx["modal"] = None
				if not ctx["cancelled"] and advance:
					on_advance()

			def _skip():
				logger.debug("sysclean dialog: skip soak fired")
				_finalize(advance=True)

			def _tick():
				tick_state["tick_after"] = None
				if ctx["cancelled"] or tick_state["advanced"]:
					return
				remaining = max(0.0,
					tick_state["done_at"] - monotonic())
				mins = int(remaining // 60)
				secs = int(remaining - mins * 60)
				countdown_lbl.config(
					text=f"Soak time: {mins:d}:{secs:02d} / {total_str} remaining")
				if remaining <= 0:
					_finalize(advance=True)
					return
				# 250 ms tick: snappy enough for sub-second responsiveness
				# without burning cycles for a multi-minute wait.
				tick_state["tick_after"] = self.after(250, _tick)

			ttk.Button(btn_row, text="Cancel",
				command=_cancel_and_close, style="Danger.TButton"
				).grid(row=0, column=0, sticky="w", padx=4)
			skip_btn = ttk.Button(btn_row, text="Skip soak",
				command=_skip, style="Primary.TButton")
			skip_btn.grid(row=0, column=1, sticky="e", padx=4)

			dlg.update_idletasks()
			self._center_over_main(dlg)
			# Edge case: zero-duration soak skips straight to advance
			# (validation allows 0; some operators may want a no-op
			# soak phase for a bleach-rinse-only protocol).
			if soak_seconds <= 0:
				_finalize(advance=True)
			else:
				_tick()

		# ---- Phase definitions ----------------------------------------
		paused_note = (
			"Running System Clean during a paused run. The fractionation "
			"run remains paused — resume it from the Automated tab when "
			"finished."
		) if launched_during_pause else None

		def _bleach_fill():
			def _soak_entry_factory(body_frame):
				"""Build the per-invocation Bleach soak time entry
				between the checklist and the body text. Returns a
				validator that the action button calls before
				advancing — on success, ``soak_state`` is updated
				with the captured minutes/seconds for Phase 2's
				countdown to read."""
				# Soak entry on one row: label + entry + 'min' unit.
				field = tk.Frame(body_frame)
				field.pack(anchor="w", pady=(0, 2))
				tk.Label(field, text="Bleach soak time:").pack(
					side=tk.LEFT)
				soak_var = tk.StringVar(value="5")
				soak_entry = ttk.Entry(field,
					textvariable=soak_var, width=6)
				soak_entry.pack(side=tk.LEFT, padx=(8, 4))
				tk.Label(field, text="min").pack(side=tk.LEFT)
				# Inline error placeholder — hidden until validation
				# fails on Start Bleach Fill.
				err_lbl = tk.Label(body_frame, text="",
					fg="red", anchor="w", wraplength=420)
				err_lbl.pack(anchor="w")
				# Italic muted hint below the entry.
				tk.Label(body_frame,
					text=(
						"Default 5 min for nucleic-acid "
						"decontamination."
					),
					fg=PALETTE["fg_muted"],
					font=(FONTS["family"], FONTS["size"], "italic"),
				).pack(anchor="w", pady=(0, 8))

				def _validate():
					ok, val = validation.soak_time(soak_var.get())
					if not ok:
						err_lbl.config(text=val)
						return False
					err_lbl.config(text="")
					soak_state["minutes"] = val
					soak_state["seconds"] = val * 60.0
					return True

				return _validate

			_run_auto_phase(
				title="System Clean — Step 1 of 4",
				body_text=(
					f"Click Start Bleach Fill to run the peristaltic pump "
					f"for {purge_seconds:.0f} s, drawing 0.5% bleach "
					"through the line."
				),
				phase="bleach", pump_kind="peristaltic",
				on_advance=_soak,
				checklist=[
					"Connected inlet line to 0.5% bleach solution",
					"Outlet routed to waste bin",
				],
				action_label="Start Bleach Fill",
				note_text=paused_note,
				after_checklist=_soak_entry_factory,
			)

		def _soak():
			_run_soak_phase(
				title="System Clean — Step 2 of 4",
				on_advance=_rinse1,
			)

		def _rinse1():
			_run_auto_phase(
				title="System Clean — Step 3 of 4",
				body_text=(
					f"Click Start Rinse to run the peristaltic pump for "
					f"{purge_seconds:.0f} s, flushing bleach with sterile "
					"water."
				),
				phase="rinse1", pump_kind="peristaltic",
				on_advance=_rinse2,
				checklist=[
					"Replaced bleach source with sterile water",
				],
				action_label="Start Rinse",
			)

		def _rinse2():
			# Final phase. On Continue, _finish_sysclean closes the
			# routine and restores the pre-routine pump claim. System
			# Clean intentionally does NOT prime with sample solution —
			# it's typically run before any sample is loaded; priming
			# is the inter-sample purge's or pre-fractionation prime's
			# responsibility.
			_run_auto_phase(
				title="System Clean — Step 4 of 4",
				body_text=(
					f"Click Rinse Again to run the peristaltic pump for "
					f"another {purge_seconds:.0f} s of sterile water "
					"rinse."
				),
				phase="rinse2", pump_kind="peristaltic",
				on_advance=_finish_sysclean,
				checklist=[
					"Water source is still connected",
				],
				action_label="Rinse Again",
			)

		def _finish_sysclean():
			_restore_pre_run_claim()
			_close_sysclean_logger()
			self.set_status(
				"System Clean complete. Run remains paused; resume "
				"from Automated tab."
				if launched_during_pause
				else "System Clean complete. System idle."
			)

		_bleach_fill()

	def continue_to_next_plate(self):
		"""Two-stage plate-swap workflow:

		  Dialog 1 — cleanup: operator removes / seals / stores the
		    previous plate and wipes the syringe needle. Needle stays
		    parked wherever it landed at plate-full (typically the
		    last well).
		  Auto-move: needle drives to plate-start coordinates via
		    ``move_to_positions``. Idempotent if Cancel-then-retry
		    runs the move a second time (already-at-target → no-op).
		  Dialog 2 — placement: operator places the new plate so it
		    aligns with the needle over well A1; enters / accepts the
		    new Plate ID.

		The ``plate_swap`` log row is written when Dialog 2's Continue
		is clicked, so the breadcrumb timestamp marks the moment the
		new plate becomes the active one rather than the cleanup step.
		"""
		s = self.state
		if s.state != "plate_full":
			return
		# Dialog 1: cleanup checklist. Returns True on Next; False on Cancel.
		if not self._show_plate_swap_cleanup_dialog(s.current_plate_id):
			return
		# Auto-move to plate-start coords. Synchronous via
		# move_to_positions; idempotent when the needle is already
		# there. Transit cadence — operator just removed the prior
		# plate, no dispense is in flight.
		self.set_status("Moving needle to start coords for next plate...")
		self.update_idletasks()
		self.move_to_positions(
			table_dist=s.table_start_cm,
			carriage_dist=s.carriage_start_cm,
			is_transit=True,
		)
		# Dialog 2: placement + Plate ID entry. Returns the validated
		# new Plate ID on Continue; None on Cancel.
		suggested = validation.auto_increment_plate_id(s.current_plate_id)
		new_plate_id = self._show_plate_swap_placement_dialog(suggested)
		if new_plate_id is None:
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

	def _show_plate_swap_cleanup_dialog(self, old_plate_id):
		"""Stage 1 of the plate-swap workflow. Operator-only checklist:
		remove, seal, store the previous plate + wipe the syringe
		needle. Returns True on Next, False on Cancel.

		Skip Checklist (Expert) writes a
		``checklist_skipped_plate_swap_dialog1_{N}`` audit row.
		"""
		swap_number = self.state.plate_swaps_done + 1
		dlg = tk.Toplevel(self)
		dlg.title("Continue to Next Plate — Step 1 of 2")
		dlg.transient(self)
		dlg.grab_set()
		dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # X disabled
		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)

		items = [
			f"Removed plate {old_plate_id} from stage",
			f"Sealed plate {old_plate_id} for storage",
			f"Stored plate {old_plate_id}",
			"Cleaned syringe needle (wiped with KimWipe or equivalent)",
		]
		check_vars = []
		for i, text in enumerate(items):
			v = tk.IntVar(value=0)
			check_vars.append(v)
			ttk.Checkbutton(body, text=text, variable=v).grid(
				row=i, column=0, columnspan=3, sticky="w", pady=2)

		bypass = {"skipped": False}
		result = {"go": False}

		btn_row = tk.Frame(body)
		btn_row.grid(row=len(items), column=0, columnspan=3, sticky="we",
			pady=(10, 0))
		btn_row.grid_columnconfigure(0, weight=1)
		btn_row.grid_columnconfigure(1, weight=1)
		btn_row.grid_columnconfigure(2, weight=1)

		def _cancel():
			dlg.destroy()
		def _skip():
			bypass["skipped"] = True
			next_btn.state(["!disabled"])
			if self.run_logger is not None:
				try:
					self.run_logger.checklist_skipped(
						f"plate_swap_dialog1_{swap_number}")
				except Exception as exc:
					logger.warning(
						"Failed to log skipped Dialog 1 checklist: %s", exc)
		def _next():
			result["go"] = True
			dlg.destroy()

		ttk.Button(btn_row, text="Cancel", command=_cancel,
			style="Danger.TButton").grid(row=0, column=0, sticky="w", padx=4)
		ttk.Button(btn_row, text="Skip Checklist (Expert)",
			command=_skip).grid(row=0, column=1, padx=4)
		next_btn = ttk.Button(btn_row, text="Next", command=_next,
			style="Primary.TButton")
		next_btn.grid(row=0, column=2, sticky="e", padx=4)
		next_btn.state(["disabled"])

		def _evaluate(*_):
			if bypass["skipped"]:
				return
			if all(v.get() == 1 for v in check_vars):
				next_btn.state(["!disabled"])
			else:
				next_btn.state(["disabled"])
		for v in check_vars:
			v.trace_add("write", _evaluate)
		_evaluate()

		dlg.update_idletasks()
		self._center_over_main(dlg)
		self.wait_window(dlg)
		return result["go"]

	def _show_plate_swap_placement_dialog(self, suggested_new_id):
		"""Stage 2 of the plate-swap workflow. Operator places the new
		plate (needle is already parked at plate-start coords from the
		auto-move) and enters / accepts the new Plate ID. Returns the
		validated Plate ID on Continue, None on Cancel.

		Skip Checklist (Expert) writes a
		``checklist_skipped_plate_swap_dialog2_{N}`` audit row. Skip
		bypasses the placement checkboxes but the Plate ID still has
		to validate before Continue enables.
		"""
		swap_number = self.state.plate_swaps_done + 1
		dlg = tk.Toplevel(self)
		dlg.title("Continue to Next Plate — Step 2 of 2")
		dlg.transient(self)
		dlg.grab_set()
		dlg.protocol("WM_DELETE_WINDOW", lambda: None)
		body = tk.Frame(dlg, padx=14, pady=12)
		body.pack(fill=tk.BOTH, expand=True)

		tk.Label(body, justify="left", anchor="w", wraplength=460, text=(
			"Needle has returned to start coords. Place the new plate "
			"on the stage, aligned with the needle over what will be "
			"its well A1."
		)).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

		placed_var = tk.IntVar(value=0)
		secure_var = tk.IntVar(value=0)
		ttk.Checkbutton(body, variable=placed_var,
			text="Placed new plate on stage",
		).grid(row=1, column=0, columnspan=2, sticky="w", pady=2)
		ttk.Checkbutton(body, variable=secure_var,
			text="Plate is securely positioned",
		).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)

		# Plate-ID entry: validates independently of the checklist
		# (skipping the checklist doesn't bypass Plate ID validation).
		id_row = tk.Frame(body)
		id_row.grid(row=3, column=0, columnspan=2, sticky="we", pady=(8, 0))
		tk.Label(id_row, text="New Plate ID:").pack(side=tk.LEFT)
		plate_te = TextEntry(id_row, "")
		plate_te.label.grid_remove()
		plate_te.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
		plate_te.set(suggested_new_id)
		tk.Label(body, anchor="w",
			text=f"   (suggested: {suggested_new_id})", fg="#666",
		).grid(row=4, column=0, columnspan=2, sticky="w", padx=(20, 0))

		bypass = {"skipped": False}
		result = {"plate_id": None}

		btn_row = tk.Frame(body)
		btn_row.grid(row=5, column=0, columnspan=2, sticky="we", pady=(10, 0))
		btn_row.grid_columnconfigure(0, weight=1)
		btn_row.grid_columnconfigure(1, weight=1)
		btn_row.grid_columnconfigure(2, weight=1)

		def _cancel():
			dlg.destroy()
		def _skip():
			bypass["skipped"] = True
			if self.run_logger is not None:
				try:
					self.run_logger.checklist_skipped(
						f"plate_swap_dialog2_{swap_number}")
				except Exception as exc:
					logger.warning(
						"Failed to log skipped Dialog 2 checklist: %s", exc)
			_evaluate()
		def _continue():
			ok, val = validation.plate_id(plate_te.get())
			if not ok:
				plate_te.show_error(val)
				return
			plate_te.clear_error()
			result["plate_id"] = val
			dlg.destroy()

		ttk.Button(btn_row, text="Cancel", command=_cancel,
			style="Danger.TButton").grid(row=0, column=0, sticky="w", padx=4)
		ttk.Button(btn_row, text="Skip Checklist (Expert)",
			command=_skip).grid(row=0, column=1, padx=4)
		cont_btn = ttk.Button(btn_row, text="Continue", command=_continue,
			style="Primary.TButton")
		cont_btn.grid(row=0, column=2, sticky="e", padx=4)
		cont_btn.state(["disabled"])

		def _evaluate(*_):
			id_ok, _ = validation.plate_id(plate_te.get())
			boxes_ok = bypass["skipped"] or (
				placed_var.get() == 1 and secure_var.get() == 1)
			if id_ok and boxes_ok:
				cont_btn.state(["!disabled"])
			else:
				cont_btn.state(["disabled"])
		for v in (placed_var, secure_var):
			v.trace_add("write", _evaluate)
		plate_te.var.trace_add("write", _evaluate)
		_evaluate()

		dlg.update_idletasks()
		self._center_over_main(dlg)
		self.wait_window(dlg)
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

		# Reset snake position. The auto-move in Dialog 1's Next
		# already drove the needle to plate-start coords, so the
		# move_to_positions call here is idempotent (no-op when
		# already at the target) but kept defensively to guarantee
		# the physical needle is where the state machine expects.
		# Transit cadence — same reasoning as Dialog 1's auto-move.
		self.move_to_positions(
			table_dist=s.table_start_cm,
			carriage_dist=s.carriage_start_cm,
			is_transit=True,
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

	def _snake_step_absolute(self):
		"""Variant of ``_snake_step`` for the cross-sample handoff
		where the needle is at the WASTE BIN (post-discard), not at
		the previous collected well.

		``_snake_step`` issues a RELATIVE motor move of one well-size
		on the assumption that the carriage is currently parked at
		the previous well. When called after the inter-sample
		discards have just dispensed at the waste bin, that relative
		move offsets from the waste-bin position by one well-size
		and lands somewhere off the plate — the state machine then
		runs the rest of the sample at a phantom position while the
		software believes it's tracking the plate. This helper
		advances the snake state the same way ``_snake_step`` does
		AND issues an ABSOLUTE move to the new well's motor
		coordinates so the carriage ends up exactly where the
		software expects.

		Returns True if the new position is on the plate, False if
		the snake walked off — same contract as ``_snake_step``.
		"""
		s = self.state
		# Mirror ``_snake_step``'s state-advance logic exactly
		# (column-wise iteration in BOTH orientations: inner sweep on
		# s.y, outer step on s.x; off-plate at s.x >= s.COLS), but
		# without firing any per-axis relative motor move.
		if s.carriage_forwards:
			s.y = s.y + 1
			if s.y >= s.ROWS:
				s.y = s.ROWS - 1
				s.x = s.x + 1
				s.carriage_forwards = False
		else:
			s.y = s.y - 1
			if s.y < 0:
				s.y = 0
				s.x = s.x + 1
				s.carriage_forwards = True
		if s.x >= s.COLS:
			return False

		# Absolute target. Same orientation-aware mapping as
		# ``well_id_to_cm`` (and ``_prime_phase``'s current-well
		# lookup) so this move agrees with every other state-machine
		# absolute-position computation. Portrait cols extend NORTH
		# (−Y) from A1 per the LEFT-anchored convention.
		if self.plate_orientation == "portrait":
			target_x_cm = s.table_start_cm + s.y * s.well_size
			target_y_cm = s.carriage_start_cm - s.x * s.well_size
		else:
			target_x_cm = s.table_start_cm + s.x * s.well_size
			target_y_cm = s.carriage_start_cm + s.y * s.well_size

		well_id = f"{chr(ord('A') + s.y)}{s.x + 1}"
		self.set_status(
			f"Moving to first collected well of series "
			f"{s.series_index}: {well_id}…"
		)
		self.move_to_positions(
			table_dist=target_x_cm,
			carriage_dist=target_y_cm,
			is_transit=True,
		)
		if self.run_logger is not None:
			try:
				self.run_logger.sample_start_move(
					s.series_index, s.x, s.y,
					target_x_cm, target_y_cm,
				)
			except Exception as exc:
				logger.warning(
					"Failed to log sample_start_move: %s", exc)
		return True

	def _snake_step(self):
		"""Advance s.x/s.y one snake-step AND fire the corresponding motor
		moves. Returns True if we stayed on the plate, False if we walked
		off — the caller decides what to do then.

		Snake pattern is COLUMN-WISE in both orientations: within a
		column the inner sweep advances ``s.y`` across rows A→H (and
		alternates direction column-to-column); when the column
		finishes, ``s.x`` increments to the next column. Off-plate when
		``s.x >= s.COLS``. The same logical sequence (A1, B1, C1, C2,
		B2, A2, A3, ...) is produced regardless of orientation.

		Only the PHYSICAL motor mapping differs between orientations,
		since rows and columns live on different physical axes:

		  ``"portrait"`` — rows (s.y, A-H) on the X-axis (table_motor);
		    columns (s.x, 1-12) on the Y-axis (carriage_motor). Inner
		    sweep moves TABLE; column wrap moves CARRIAGE.

		  ``"landscape"`` — rows (s.y, A-H) on the Y-axis (carriage_motor);
		    columns (s.x, 1-12) on the X-axis (table_motor). Inner
		    sweep moves CARRIAGE; column wrap moves TABLE.

		``carriage_forwards`` is the inner-sweep direction flag
		(alternating). Motor reverse flags are fixed (origin = upper-
		left mechanical limit, +X east, +Y south) regardless of
		orientation; the sign of the relative moves here stays uniform.
		"""
		s = self.state
		# Pick the physical motors that drive "row" (inner sweep) and
		# "column" (outer step) for this orientation. Logical iteration
		# is column-wise in both orientations.
		#
		# CARRIAGE-MOTOR SIGN CONVENTION (must match ``move_to_positions``
		# and Manual jog): south distance = NEGATIVE motor.angle. So
		# every carriage-axis delta passed to ``move_dist_relative`` is
		# negated relative to the state-machine "south = +ws" frame.
		# ``row_sign`` and ``col_sign`` apply that flip only when the
		# motor driving that axis is the carriage motor; table-axis
		# deltas stay in the +X = east convention. This keeps the
		# motor's signed counter in ``[−15, 0]`` for the whole run so
		# ``abs(motor.angle)`` in the crosshair poll always corresponds
		# to the same physical south distance — regardless of whether
		# the operator reached the current position via Manual jog or
		# an automated absolute move.
		if self.plate_orientation == "portrait":
			row_motor = self.table_motor
			col_motor = self.carriage_motor
			# Row inner sweep on table (east-positive): no flip.
			row_sign = +1
			# Col outer step on carriage going NORTH (toward origin):
			# motor.angle INCREASES from negative toward 0 → +ws.
			col_sign = +1
		else:
			row_motor = self.carriage_motor
			col_motor = self.table_motor
			# Row inner sweep on carriage going SOUTH: motor.angle
			# DECREASES (more negative) → −ws.
			row_sign = -1
			# Col outer step on table (east-positive): no flip.
			col_sign = +1

		if s.carriage_forwards:
			s.y = s.y + 1
			if s.y < s.ROWS:
				row_motor.move_dist_relative(row_sign * s.well_size)
			else:
				s.y = s.ROWS - 1
				col_motor.move_dist_relative(col_sign * s.well_size)
				s.x = s.x + 1
				s.carriage_forwards = False
		else:
			s.y = s.y - 1
			if s.y >= 0:
				row_motor.move_dist_relative(-row_sign * s.well_size)
			else:
				s.y = 0
				col_motor.move_dist_relative(col_sign * s.well_size)
				s.x = s.x + 1
				s.carriage_forwards = True
		return s.x < s.COLS

	def carriage_return(self):
		"""Return the needle to the starting position. Transit move —
		dispense isn't in flight when this fires (Return to Origin from
		idle / end of run)."""
		self.table_motor.move_dist_absolute(0.0, is_transit=True)
		self.carriage_motor.move_dist_absolute(0.0, is_transit=True)


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
