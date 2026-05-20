"""Well-plate progress view for autoSIP's Automated mode.

A ``WellPlateProgress`` widget replaces the original 25 px canvas grid with
a to-scale SBS-format plate (row letters A.. down the left, column numbers
1.. across the top), header text showing the current well / count / elapsed
+ remaining time, and tooltips on hover.

The fractionation state machine in ``main.py`` keeps owning all motion +
relay logic; it just calls the methods below to drive the view:

  begin_run(rows, cols, volume_per_well, pump_time)
  well_dispensing(x, y)
  well_waiting(x, y)
  well_completed(x, y)
  well_skipped(x, y, reason)
  end_run()

Color palette is ColorBrewer Set2-style (color-blind safe). Each state also
encodes a Unicode glyph so the view doesn't rely on color alone (WCAG).
"""

import logging
from time import monotonic
import tkinter as tk

logger = logging.getLogger("autosip")

# TODO: calibrate from real motor data. On the mock, ``move()`` does a
# ~0.2-0.4 s motor move per step plus the carriage_return at end-of-run;
# real-hardware times depend on lead-screw friction and microstep timing.
# 1.5 s is an over-estimate so the Remaining counter trends down to zero.
ESTIMATED_MOVE_TIME_S = 1.5

# Status constants (also used as keys in the per-status color/icon maps)
UNVISITED = "unvisited"
DISPENSING = "dispensing"
WAIT = "wait"
COMPLETED = "completed"
SKIPPED = "skipped"

# ColorBrewer Set2-ish, picked for distinguishability under common color
# vision deficiencies. Avoid the original green+blue+yellow trio.
_COLORS = {
	UNVISITED: "#dcdcdc",   # light gray
	DISPENSING: "#66c2a5",  # Set2 teal-green
	WAIT: "#fc8d62",        # Set2 orange (amber substitute)
	COMPLETED: "#8da0cb",   # Set2 blue-violet
	SKIPPED: "#d62728",     # red
}

# Glyph shown inside each well in addition to its color. Empty string means
# no glyph (unvisited wells stay clean).
_ICONS = {
	UNVISITED: "",
	DISPENSING: "▼",
	WAIT: "⋯",
	COMPLETED: "✓",
	SKIPPED: "✗",
}

# Glyph color -- white reads better on the darker fills, dark on the lighter
# wait/skipped fills.
_ICON_COLOR_LIGHT = "#ffffff"
_ICON_COLOR_DARK = "#222222"
_ICON_COLORS = {
	UNVISITED: _ICON_COLOR_DARK,
	DISPENSING: _ICON_COLOR_LIGHT,
	WAIT: _ICON_COLOR_DARK,
	COMPLETED: _ICON_COLOR_LIGHT,
	SKIPPED: _ICON_COLOR_LIGHT,
}


def _fmt_hms(seconds):
	"""Format ``seconds`` (float) as ``HH:MM:SS``."""
	seconds = max(0, int(round(seconds)))
	h, rem = divmod(seconds, 3600)
	m, s = divmod(rem, 60)
	return f"{h:02d}:{m:02d}:{s:02d}"


def _well_id(x, y):
	"""Return the SBS well ID for column ``x`` (0-based) and row ``y`` (0-based)."""
	return f"{chr(ord('A') + y)}{x + 1}"


# Okabe-Ito color-blind-safe qualitative palette. Each entry is
# ``(hex_color, human_name)``. Index 0 is sample 1, index 1 is sample 2,
# and so on; cycles after 8 samples. Text-on-fill contrast is encoded
# in ``_SAMPLE_TEXT_COLOR`` so we hit WCAG AA on every background.
SAMPLE_PALETTE = [
	("#E69F00", "orange"),
	("#56B4E9", "sky blue"),
	("#009E73", "bluish green"),
	("#F0E442", "yellow"),
	("#0072B2", "blue"),
	("#D55E00", "vermillion"),
	("#CC79A7", "reddish purple"),
	("#000000", "black"),
]
# Backgrounds that are dark enough to need white text. The remaining
# palette entries take dark text on the light fill.
_DARK_BG_HEXES = {"#0072B2", "#D55E00", "#000000"}


def color_for_series(series_index):
	"""Return ``(hex_color, human_name)`` for 1-indexed ``series_index``.

	Cycles through SAMPLE_PALETTE so a run with more than 8 samples reuses
	colors from the top of the palette. (We could disambiguate with patterns
	in a future iteration, but cycling is simpler and the spec allows it.)
	"""
	idx = max(0, (series_index - 1) % len(SAMPLE_PALETTE))
	return SAMPLE_PALETTE[idx]


def text_color_for(bg_hex):
	"""White on dark bgs, dark on light. Keeps WCAG AA contrast."""
	return "#ffffff" if bg_hex in _DARK_BG_HEXES else "#222222"


# Single-character glyphs for the snapshot ASCII grid.
_SNAPSHOT_GLYPHS = {
	UNVISITED: ".",
	DISPENSING: "D",
	WAIT: "W",
	COMPLETED: "*",
	SKIPPED: "X",
}


def format_snapshot_log(snap):
	"""Format a ``snapshot()`` dict as a multi-line log block.

	Includes per-status counts plus an ASCII plate grid (A1 top-left,
	column numbers across the top) so the final state is human-readable
	when scrolling back through the log file.
	"""
	cols, rows = snap["cols"], snap["rows"]
	counts = snap["counts"]
	wells_by_status = snap["wells_by_status"]
	status_lookup = {}
	for status, wells in wells_by_status.items():
		for xy in wells:
			status_lookup[xy] = status

	lines = [
		f"Plate snapshot ({rows} rows x {cols} cols):",
		f"  *={COMPLETED} D={DISPENSING} W={WAIT} X={SKIPPED} .={UNVISITED}",
		f"  Counts: completed={counts[COMPLETED]} "
		f"dispensing={counts[DISPENSING]} wait={counts[WAIT]} "
		f"skipped={counts[SKIPPED]} unvisited={counts[UNVISITED]}",
		"",
	]
	# Column number header (right-aligned in 2-char cells)
	col_header = "       " + " ".join(f"{c+1:2d}" for c in range(cols))
	lines.append(col_header)
	for y in range(rows):
		cells = []
		for x in range(cols):
			status = status_lookup.get((x, y), UNVISITED)
			cells.append(f" {_SNAPSHOT_GLYPHS[status]}")
		lines.append(f"    {chr(ord('A') + y)}  " + " ".join(cells))
	return "\n".join(lines)


class WellPlateProgress(tk.Frame):
	"""Per-well progress view backed by a Tk Canvas.

	Lifecycle:
	  - Construct once; the canvas is empty until ``begin_run`` is called.
	  - ``begin_run`` sets dimensions + starts the elapsed/remaining clock.
	  - State-transition methods (``well_dispensing`` / ``_waiting`` /
	    ``_completed`` / ``_skipped``) update one well and the header.
	  - ``end_run`` stops the pulsing border and the clock tick.
	"""

	# Margins reserved for the row letters (left) and column numbers (top).
	_LEFT_MARGIN = 22
	_TOP_MARGIN = 18
	_RIGHT_MARGIN = 6
	_BOTTOM_MARGIN = 6

	_PULSE_INTERVAL_MS = 500
	_CLOCK_INTERVAL_MS = 1000

	def __init__(self, parent, min_width=420, min_height=260):
		super().__init__(parent)

		# Explicit width= on each label (in character cells) so the labels'
		# requested widths don't fluctuate as text changes -- otherwise the
		# root window oscillates as e.g. "Well 9 of 96" -> "Well 10 of 96"
		# shifts by one character and propagates up the geometry chain.
		# Plate ID header line, updated on each plate swap. Lives above
		# the per-well progress text so the plate-vs-sample distinction is
		# the first thing the eye lands on.
		self.plate_lbl = tk.Label(
			self, text="", anchor="w", width=60,
			font=("TkDefaultFont", 10, "italic"), fg="#555",
		)
		self.plate_lbl.grid(row=0, column=0, sticky="w", padx=4, pady=(2, 0))
		self.current_lbl = tk.Label(
			self, text="", anchor="w", justify="left", width=60,
			font=("TkDefaultFont", 11, "bold"),
		)
		self.current_lbl.grid(row=1, column=0, sticky="w", padx=4, pady=(2, 0))
		self.count_lbl = tk.Label(self, text="", anchor="w", width=40)
		self.count_lbl.grid(row=2, column=0, sticky="w", padx=4)
		self.time_lbl = tk.Label(self, text="", anchor="w", width=60)
		self.time_lbl.grid(row=3, column=0, sticky="w", padx=4, pady=(0, 4))

		self.canvas = tk.Canvas(
			self, bg="white", bd=0, highlightthickness=1,
			highlightbackground="#bbbbbb",
			width=min_width, height=min_height,
		)
		self.canvas.grid(row=4, column=0, sticky="nsew", padx=4, pady=(0, 4))

		self.grid_columnconfigure(0, weight=1)
		self.grid_rowconfigure(4, weight=1)

		# Plate state
		self.rows = 0
		self.cols = 0
		self.volume_per_well = 0.0
		self.pump_time = 0.0
		self.status_grid = {}        # (x, y) -> status string
		self.error_reasons = {}      # (x, y) -> reason for SKIPPED
		# Per-well sample info for COMPLETED wells. Keys: (x, y).
		# Values: {color, sequence, sample_id, color_name}. Populated by
		# well_completed when the caller passes per-sample data.
		self.well_records = {}
		self.dispensing_xy = None    # (x, y) or None

		# Canvas item caches so we can update one well without redrawing all
		self._well_items = {}        # (x, y) -> oval id
		self._well_text_items = {}   # (x, y) -> text id
		self._geom = None            # last computed layout for hit-testing

		# Pulse animation state
		self._pulse_thick = False
		self._pulse_after = None

		# Elapsed/Remaining clock
		self._start_time = None
		self._clock_after = None

		# Tooltip state
		self._tooltip = None
		self._tooltip_xy = None

		# Resize + hover. ``_on_resize`` logs the new canvas dimensions
		# (visible under ``--debug``) and re-renders the plate at the new
		# size; both call paths route through ``_redraw`` so the cell-size
		# math lives in exactly one place.
		self.canvas.bind("<Configure>", self._on_resize)
		self.canvas.bind("<Motion>", self._on_motion)
		self.canvas.bind("<Leave>", lambda e: self._hide_tooltip())

	# -- Public API (called by the state machine) ------------------------

	def begin_run(self, cols, rows, volume_per_well, pump_time):
		"""Reset the plate to a fresh run and start the elapsed-time clock."""
		self.rows = rows
		self.cols = cols
		self.volume_per_well = volume_per_well
		self.pump_time = pump_time
		self.status_grid = {(x, y): UNVISITED for x in range(cols) for y in range(rows)}
		self.error_reasons = {}
		self.well_records = {}
		self.dispensing_xy = None
		self._stop_pulse()
		self._start_time = monotonic()
		self._start_clock()
		self._redraw()
		self._update_header_count()
		self.current_lbl["text"] = ""

	def well_dispensing(self, x, y):
		# A different well dispensing? Stop its pulse before starting ours.
		if self.dispensing_xy is not None and self.dispensing_xy != (x, y):
			self._stop_pulse()
		self._set_status(x, y, DISPENSING)
		self.dispensing_xy = (x, y)
		self._start_pulse()
		self._update_current_label(x, y)
		self._update_header_count()

	def well_waiting(self, x, y):
		self._set_status(x, y, WAIT)
		if self.dispensing_xy == (x, y):
			self._stop_pulse()
			self.dispensing_xy = None
		self._update_header_count()

	def well_completed(self, x, y, color=None, sequence=None,
			sample_id=None, color_name=None):
		"""Mark the well as completed.

		If ``color`` + ``sequence`` are provided, the well is rendered with
		that fill and the sequence number as its glyph -- so a finished
		plate maps each sample to its color and shows the per-sample
		dispense order at a glance. ``sample_id`` + ``color_name`` are
		shown in the tooltip.

		Callers without per-sample info (older code paths, tests) get the
		legacy COMPLETED rendering (blue fill + check glyph).
		"""
		if color is not None and sequence is not None:
			self.well_records[(x, y)] = {
				"color": color,
				"sequence": sequence,
				"sample_id": sample_id or "",
				"color_name": color_name or "",
			}
		self._set_status(x, y, COMPLETED)
		if self.dispensing_xy == (x, y):
			self._stop_pulse()
			self.dispensing_xy = None
		self._update_header_count()

	def well_skipped(self, x, y, reason):
		self._set_status(x, y, SKIPPED)
		self.error_reasons[(x, y)] = reason
		if self.dispensing_xy == (x, y):
			self._stop_pulse()
			self.dispensing_xy = None
		self._update_header_count()

	def end_run(self):
		"""Stop the pulsing border and the elapsed/remaining clock tick.

		Wells keep whatever status they have so the final state stays on
		screen until the next ``begin_run``.
		"""
		self._stop_pulse()
		self._stop_clock()
		self.dispensing_xy = None
		self.current_lbl["text"] = ""

	def set_discard_status(self, index, total):
		"""Show discard-phase progress text in the header.

		Called during Phase 1 of a run with D > 0. The plate canvas stays
		in its "all wells pending" state (no animation) because discard
		cycles don't touch any wells.
		"""
		if total <= 0:
			self.current_lbl["text"] = ""
			return
		self.current_lbl["text"] = (
			f"Discard phase: {index} of {total} fractions dispensed to waste"
		)

	def set_total_reached(self, n_fractions):
		"""Switch the header into the "auto-paused, total reached" state."""
		self._stop_pulse()
		self.dispensing_xy = None
		self.current_lbl["text"] = (
			f"Total of {n_fractions} fractions reached. Click End Run to finalize."
		)

	def set_plate_label(self, plate_id):
		"""Update the "Plate: {plate_id}" header line. Idempotent."""
		self.plate_lbl["text"] = f"Plate: {plate_id}" if plate_id else ""

	def reset_plate(self, plate_id):
		"""Clear visible plate state for a swap WITHOUT touching elapsed-time
		bookkeeping. Wipes status_grid + well_records (the per-plate render
		cache); the App keeps the cross-plate well history in its own
		state.well_records."""
		self._stop_pulse()
		self.dispensing_xy = None
		self.status_grid = {(x, y): UNVISITED for x in range(self.cols) for y in range(self.rows)}
		self.error_reasons = {}
		self.well_records = {}
		self.current_lbl["text"] = ""
		self.set_plate_label(plate_id)
		self._redraw()
		self._update_header_count()

	def reset(self):
		"""Stop animations AND clear the plate back to empty.

		Use after a Terminate when the operator has acknowledged the snapshot
		(or declined it) and wants the view ready for a fresh run. To
		preserve the final colors instead, call ``end_run`` and skip this.
		"""
		self._stop_pulse()
		self._stop_clock()
		self.rows = 0
		self.cols = 0
		self.status_grid = {}
		self.error_reasons = {}
		self.well_records = {}
		self.dispensing_xy = None
		self._start_time = None
		self.plate_lbl["text"] = ""
		self.current_lbl["text"] = ""
		self.count_lbl["text"] = ""
		self.time_lbl["text"] = ""
		self._redraw()

	def snapshot(self):
		"""Return a structured summary of the current plate state.

		Returns a dict suitable for ``format_snapshot_log`` -- counts per
		status plus the list of well IDs in each status.
		"""
		counts = {UNVISITED: 0, DISPENSING: 0, WAIT: 0, COMPLETED: 0, SKIPPED: 0}
		wells_by_status = {k: [] for k in counts}
		for (x, y), status in self.status_grid.items():
			counts[status] += 1
			wells_by_status[status].append((x, y))
		return {
			"rows": self.rows,
			"cols": self.cols,
			"counts": counts,
			"wells_by_status": wells_by_status,
		}

	# -- Drawing ---------------------------------------------------------

	def _set_status(self, x, y, status):
		self.status_grid[(x, y)] = status
		self._redraw_well(x, y)

	# Per-well rendering bounds. Tracks the spec: at least 12 px so wells
	# stay visible at the smallest sensible window size; at most 80 px so
	# extreme aspect ratios don't blow up to absurd circles.
	_WELL_PX_MIN = 12
	_WELL_PX_MAX = 80
	# Fraction of cell width given over to between-well spacing. Matches
	# the spec's "1.1 accounts for spacing" formula.
	_CELL_SPACING_FRAC = 0.10

	def _on_resize(self, event):
		"""``<Configure>`` callback. Logs the new size (for ``--debug``)
		and re-renders the plate; the cell-size math lives in ``_redraw``."""
		logger.debug("canvas resize: %d x %d", event.width, event.height)
		self._redraw()

	def _redraw(self):
		"""Recompute layout (cell sizes, margins, label positions) + redraw.

		Cell size is derived from the LIVE canvas dimensions so the plate
		grows and shrinks with the window. Wells render at a diameter of
		(1 - spacing) * cell_size, clamped to ``[_WELL_PX_MIN,
		_WELL_PX_MAX]`` so they stay legible at extreme aspect ratios.
		The plate is centered within the usable area when one axis runs
		out of room before the other.
		"""
		self.canvas.delete("all")
		self._well_items.clear()
		self._well_text_items.clear()
		if self.rows == 0 or self.cols == 0:
			self._geom = None
			return

		w = max(self.canvas.winfo_width(), 1)
		h = max(self.canvas.winfo_height(), 1)
		if w < 60 or h < 60:
			# Canvas hasn't been laid out yet; let the <Configure> callback
			# retry after geometry settles.
			self._geom = None
			return

		usable_w = w - self._LEFT_MARGIN - self._RIGHT_MARGIN
		usable_h = h - self._TOP_MARGIN - self._BOTTOM_MARGIN

		# Largest cell that fits in the usable area, accounting for the
		# inter-well gap. Width-limited and height-limited candidates --
		# the constrained axis wins.
		spacing = 1.0 + self._CELL_SPACING_FRAC
		cell_from_w = usable_w / (self.cols * spacing)
		cell_from_h = usable_h / (self.rows * spacing)
		cell_size = min(cell_from_w, cell_from_h)
		cell_size = max(self._WELL_PX_MIN, min(self._WELL_PX_MAX, cell_size))

		# Well diameter inside each cell (rest of the cell is the gap).
		well_radius = (cell_size * (1.0 - self._CELL_SPACING_FRAC)) / 2.0
		# Tie the per-well sequence-number font to cell size so the digits
		# scale with the wells.
		icon_font_size = max(8, int(cell_size // 3))

		# Center the plate horizontally/vertically in the available area so
		# the plate doesn't drift left/up when the window is wider than
		# tall. ``plate_w/h`` subtract the trailing-cell gap so the
		# centering accounts for the rightmost/bottommost well actually
		# not needing extra spacing after it.
		gap_correction = cell_size * self._CELL_SPACING_FRAC
		plate_w = cell_size * self.cols - gap_correction
		plate_h = cell_size * self.rows - gap_correction
		x_offset = self._LEFT_MARGIN + max(0, (usable_w - plate_w) / 2)
		y_offset = self._TOP_MARGIN + max(0, (usable_h - plate_h) / 2)

		# Row/column label font scales with cell size, capped so the
		# labels don't crowd small wells (min 8 pt) or shout on big ones
		# (cap a few pts below the in-well sequence font).
		label_font_size = max(8, min(int(cell_size // 4), icon_font_size - 1))

		# Column numbers across the top
		for c in range(self.cols):
			cx = x_offset + cell_size * (c + 0.5)
			self.canvas.create_text(
				cx, y_offset - self._TOP_MARGIN / 2,
				text=str(c + 1), font=("TkDefaultFont", label_font_size),
			)

		# Row letters down the left
		for r in range(self.rows):
			cy = y_offset + cell_size * (r + 0.5)
			self.canvas.create_text(
				x_offset - self._LEFT_MARGIN / 2, cy,
				text=chr(ord("A") + r), font=("TkDefaultFont", label_font_size),
			)

		# Wells
		for x in range(self.cols):
			for y in range(self.rows):
				cx = x_offset + cell_size * (x + 0.5)
				cy = y_offset + cell_size * (y + 0.5)
				status = self.status_grid.get((x, y), UNVISITED)
				border = self._border_for(x, y, status)
				fill, glyph, glyph_color = self._fill_glyph_for(x, y, status)
				oval = self.canvas.create_oval(
					cx - well_radius, cy - well_radius,
					cx + well_radius, cy + well_radius,
					fill=fill, outline="#444", width=border,
				)
				self._well_items[(x, y)] = oval
				text_id = self.canvas.create_text(
					cx, cy, text=glyph, fill=glyph_color,
					font=("TkDefaultFont", icon_font_size, "bold"),
				)
				self._well_text_items[(x, y)] = text_id

		self._geom = {
			"x_offset": x_offset, "y_offset": y_offset,
			"cell_size": cell_size, "well_radius": well_radius,
			"icon_font_size": icon_font_size,
		}

	def _border_for(self, x, y, status):
		if status == DISPENSING and self._pulse_thick:
			return 3
		return 1

	def _fill_glyph_for(self, x, y, status):
		"""Return ``(fill_hex, glyph_text, glyph_color_hex)`` for a well.

		Completed wells with a sample record use the per-sample color +
		sequence number; everything else falls back to the status palette.
		"""
		if status == COMPLETED:
			rec = self.well_records.get((x, y))
			if rec is not None:
				return rec["color"], str(rec["sequence"]), text_color_for(rec["color"])
		return _COLORS[status], _ICONS[status], _ICON_COLORS[status]

	def _redraw_well(self, x, y):
		"""Update one well's fill/border/icon without redrawing the whole plate."""
		oval = self._well_items.get((x, y))
		if oval is None:
			# Plate hasn't been drawn yet (e.g., canvas not laid out). The
			# next _redraw will pick up the new status from status_grid.
			return
		status = self.status_grid.get((x, y), UNVISITED)
		fill, glyph, glyph_color = self._fill_glyph_for(x, y, status)
		self.canvas.itemconfig(
			oval, fill=fill, width=self._border_for(x, y, status),
		)
		text_id = self._well_text_items.get((x, y))
		if text_id is not None:
			self.canvas.itemconfig(text_id, text=glyph, fill=glyph_color)

	# -- Pulse animation -------------------------------------------------

	def _start_pulse(self):
		if self._pulse_after is not None:
			return
		self._pulse_thick = False
		self._pulse_after = self.after(self._PULSE_INTERVAL_MS, self._tick_pulse)

	def _stop_pulse(self):
		if self._pulse_after is not None:
			self.after_cancel(self._pulse_after)
			self._pulse_after = None
		if self._pulse_thick:
			self._pulse_thick = False
			if self.dispensing_xy is not None:
				self._redraw_well(*self.dispensing_xy)

	def _tick_pulse(self):
		self._pulse_thick = not self._pulse_thick
		if self.dispensing_xy is not None:
			self._redraw_well(*self.dispensing_xy)
		self._pulse_after = self.after(self._PULSE_INTERVAL_MS, self._tick_pulse)

	# -- Elapsed/Remaining clock ----------------------------------------

	def _start_clock(self):
		self._stop_clock()
		self._tick_clock()

	def _stop_clock(self):
		if self._clock_after is not None:
			self.after_cancel(self._clock_after)
			self._clock_after = None

	def _tick_clock(self):
		self._update_time_label()
		self._clock_after = self.after(self._CLOCK_INTERVAL_MS, self._tick_clock)

	# -- Header text -----------------------------------------------------

	def _update_current_label(self, x, y):
		self.current_lbl["text"] = (
			f"Dispensing into {_well_id(x, y)} — {self.volume_per_well:g} cc"
		)

	def _update_header_count(self):
		total = self.rows * self.cols
		started = sum(1 for s in self.status_grid.values() if s != UNVISITED)
		pct = (started / total * 100) if total else 0.0
		self.count_lbl["text"] = f"Well {started} of {total} ({pct:.1f}%)"

	def _update_time_label(self):
		if self._start_time is None:
			self.time_lbl["text"] = ""
			return
		elapsed = monotonic() - self._start_time

		total = self.rows * self.cols
		started = sum(1 for s in self.status_grid.values() if s != UNVISITED)
		remaining_wells = max(0, total - started)
		per_well_s = self.pump_time * 2 + ESTIMATED_MOVE_TIME_S
		remaining_s = remaining_wells * per_well_s

		self.time_lbl["text"] = (
			f"Elapsed: {_fmt_hms(elapsed)}   "
			f"Remaining: ~{_fmt_hms(remaining_s)}"
		)

	# -- Tooltips --------------------------------------------------------

	def _well_at_pixel(self, px, py):
		"""Return (x, y) of the well under the pixel, or None."""
		g = self._geom
		if g is None:
			return None
		cs = g["cell_size"]
		col = int((px - g["x_offset"]) // cs)
		row = int((py - g["y_offset"]) // cs)
		if not (0 <= col < self.cols and 0 <= row < self.rows):
			return None
		# Hit only inside the circular well, not the cell's corners.
		cx = g["x_offset"] + cs * (col + 0.5)
		cy = g["y_offset"] + cs * (row + 0.5)
		if (px - cx) ** 2 + (py - cy) ** 2 > g["well_radius"] ** 2:
			return None
		return (col, row)

	def _on_motion(self, event):
		xy = self._well_at_pixel(event.x, event.y)
		if xy == self._tooltip_xy:
			return
		self._hide_tooltip()
		if xy is not None:
			self._show_tooltip(event, xy)
		self._tooltip_xy = xy

	def _show_tooltip(self, event, xy):
		x, y = xy
		status = self.status_grid.get(xy, UNVISITED)
		lines = [f"{_well_id(x, y)} — {status}"]
		if status == COMPLETED and xy in self.well_records:
			rec = self.well_records[xy]
			sample = rec.get("sample_id") or "(unknown)"
			cname = rec.get("color_name") or ""
			lines.append(f"Sample {sample} — fraction {rec['sequence']} of this sample")
			if cname:
				lines.append(f"({cname})")
		if status == SKIPPED:
			reason = self.error_reasons.get(xy, "")
			if reason:
				lines.append(f"Reason: {reason}")
		# Planned per-well dispense duration. Actual per-well timing isn't
		# tracked yet (every well uses the same pump_time); leave a TODO
		# in the surrounding text if/when we start logging actuals.
		lines.append(f"Planned dispense: {self.pump_time:.2f} s")
		text = "\n".join(lines)

		try:
			self._tooltip = tk.Toplevel(self)
			self._tooltip.wm_overrideredirect(True)
			self._tooltip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
			tk.Label(
				self._tooltip, text=text, justify="left",
				bg="#ffffe1", fg="#222", relief="solid", borderwidth=1,
				font=("TkDefaultFont", 9), padx=6, pady=3,
			).pack()
		except tk.TclError:
			# Window destroyed mid-event; safe to ignore.
			self._tooltip = None

	def _hide_tooltip(self):
		if self._tooltip is not None:
			try:
				self._tooltip.destroy()
			except tk.TclError:
				pass
			self._tooltip = None
		self._tooltip_xy = None
