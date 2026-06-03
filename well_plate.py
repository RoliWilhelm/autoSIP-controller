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


def parse_well_id(well_id):
	"""Parse a SBS well ID like ``"E3"`` into ``(col_idx, row_idx)`` —
	both 0-based. ``"A1"`` → ``(0, 0)``; ``"H12"`` → ``(11, 7)``.
	Raises ``ValueError`` on malformed input."""
	if not isinstance(well_id, str) or len(well_id) < 2:
		raise ValueError(f"Malformed well_id {well_id!r}")
	letter = well_id[0].upper()
	if not letter.isalpha():
		raise ValueError(f"Well_id {well_id!r} must start with a row letter")
	try:
		col_idx = int(well_id[1:]) - 1
	except ValueError as exc:
		raise ValueError(f"Well_id {well_id!r} column part is not an integer") from exc
	row_idx = ord(letter) - ord("A")
	if col_idx < 0 or row_idx < 0:
		raise ValueError(f"Well_id {well_id!r} out of range")
	return col_idx, row_idx


def well_id_to_cm(well_id, start_x_cm, start_y_cm, well_width_cm,
		orientation="portrait"):
	"""Return the absolute ``(x_cm, y_cm)`` table+carriage positions
	for a SBS well_id given the calibrated A1 location and well width.

	Origin (0, 0) is always the upper-left mechanical limit and the
	motor reverse flags are fixed regardless of orientation. Plate
	orientation only changes which plate axis maps to which motor
	axis:

	  * ``"landscape"`` — columns on X, rows on Y.
	    ``x = start_x + col_idx × well_width``
	    ``y = start_y + row_idx × well_width``

	  * ``"portrait"`` — rows on X (east-positive), columns on Y
	    (north-NEGATIVE relative to A1, since col 2, 3, ... extend
	    UP from A1 in the LEFT-anchored portrait convention).
	    ``x = start_x + row_idx × well_width``
	    ``y = start_y − col_idx × well_width``

	``start_x_cm`` / ``start_y_cm`` are the operator-calibrated A1
	coordinates for the current orientation; they're the sole input
	that differs between orientations.

	The mechanical state machine uses relative moves in ``_snake_step``
	for within-sample steps, but cross-sample handoffs and the
	pre-fractionation prime call this through ``_snake_step_absolute``
	and ``_prime_phase`` respectively, so any sign error here surfaces
	as a needle that lands off the plate after the first post-purge
	move.
	"""
	col_idx, row_idx = parse_well_id(well_id)
	if orientation == "portrait":
		x_cm = start_x_cm + row_idx * well_width_cm
		y_cm = start_y_cm - col_idx * well_width_cm
	else:
		x_cm = start_x_cm + col_idx * well_width_cm
		y_cm = start_y_cm + row_idx * well_width_cm
	return x_cm, y_cm


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


# XY table physical dimensions in millimetres. The table was sized for
# a 1×2 grid of SBS microplates in portrait — two plates side-by-side
# along X, one plate tall along Y. So:
#   X (table-screw axis)     = 2 × 85.48 = 170.96 mm
#   Y (carriage-screw axis)  = 1 × 127.76 = 127.76 mm
# Aspect ratio 170.96 / 127.76 ≈ 1.34 — slightly wider than tall.
#
# An earlier draft used Y = 255.52 mm, but that turns out to be the
# lead-screw length / structural extent rather than the usable plate
# area. The visualization scales to the usable plate area.
TABLE_WIDTH_MM = 170.96   # X (table-screw axis)
TABLE_HEIGHT_MM = 127.76  # Y (carriage-screw axis)

# SBS standard microplate footprint in millimetres. Same plastic
# perimeter for every SBS-standard plate regardless of well count; the
# well grid sits centered inside it. Used by ``TableView`` so a 12-well
# Corning plate, a 96-well plate, and a 384-well plate all render at
# their real physical footprint (the well grids inside them differ).
SBS_FOOTPRINT_LONG_MM = 127.76
SBS_FOOTPRINT_SHORT_MM = 85.48

# Waste bin "interior margin" — how far inside the bin's outer
# rectangle the needle must be to count as a valid dispense entry
# point. Prevents the dispenser from dripping on the bin rim. Used by
# ``shortest_point_in_waste_bin`` to shrink the rectangle on all
# sides before the point-to-rectangle clamp.
WASTE_BIN_INTERIOR_MARGIN_MM = 5.0


def shortest_point_in_waste_bin(
		current_x_mm, current_y_mm,
		bin_center_x_mm, bin_center_y_mm,
		bin_extent_x_mm, bin_extent_y_mm,
		margin_mm=WASTE_BIN_INTERIOR_MARGIN_MM):
	"""Return ``(target_x_mm, target_y_mm)`` — the closest point inside
	the waste-bin interior to the current XY position. Classic
	point-to-rectangle clamp with a margin inset on all sides so the
	dispenser doesn't drop fluid on the bin rim.

	The bin is parameterized by its CENTER ``(bin_center_x_mm,
	bin_center_y_mm)`` and full extents ``(bin_extent_x_mm,
	bin_extent_y_mm)``; the rectangle occupies
	``[center − extent/2, center + extent/2]`` on each axis. The
	"interior" is that rectangle shrunk by ``margin_mm`` on every side.
	If the interior collapses on an axis (extent < 2 × margin), fall
	back to the bin centre on that axis so the dispenser still targets
	the middle of whatever ledge remains rather than the out-of-bounds
	clamp.

	Backward compatibility: callers should short-circuit when both
	extents are zero (legacy point-target behaviour). This helper
	collapses extent==0 to "bin centre" via the same code path, but
	callers avoid the function call entirely so the legacy code path
	stays identical.
	"""
	def axis_target(current, center, extent):
		half = extent / 2.0
		interior_min = center - half + margin_mm
		interior_max = center + half - margin_mm
		if interior_min > interior_max:
			# Extent too small to admit the margin — fall back to bin
			# centre on this axis.
			return center
		if current < interior_min:
			return interior_min
		if current > interior_max:
			return interior_max
		return current
	return (
		axis_target(current_x_mm, bin_center_x_mm, bin_extent_x_mm),
		axis_target(current_y_mm, bin_center_y_mm, bin_extent_y_mm),
	)


class TableView(tk.Frame):
	"""Whole-XY-table visualization showing the plate, fixed reference
	markers (origin, waste bin), and a live crosshair for the
	dispenser's real-time position.

	Lives next to ``WellPlateProgress`` in Automated mode. Origin
	(0, 0 cm) is the upper-left corner of the table; +X moves east,
	+Y moves south, matching the canvas's native coordinate
	convention so no axis flipping is needed — only a scale from
	cm to pixels.

	Implementation rolls out across five phases (see the spec the
	user provided). This file's current scope is **Phase 1 only** —
	static table outline + plate outline + empty wells — so the
	user can validate before later phases add markers, the
	crosshair, resize handling, and polish.
	"""

	# Padding inside the canvas before the table outline starts —
	# leaves room for axis labels in later phases.
	_INNER_PAD_PX = 10

	# Diagnostic: draw two ghost SBS-plate rectangles tiling the
	# entire table (left half + right half in portrait-SBS). Kept as
	# a togglable diagnostic — flip to ``True`` to re-check the
	# table-dimension constants against the two-SBS-portrait layout
	# they were sized for (the rectangles should EXACTLY cover the
	# table with no gaps and no overhang).
	_DEBUG_GHOST_PLATES = False

	def __init__(self, parent, min_width=360, min_height=240):
		super().__init__(parent)
		self.canvas = tk.Canvas(
			self, bg="#f4f4f4", bd=0, highlightthickness=1,
			highlightbackground="#bbbbbb",
			width=min_width, height=min_height,
		)
		self.canvas.pack(fill=tk.BOTH, expand=True)

		# Plate parameters — set by ``set_plate``; ``None`` until the
		# operator finishes entering valid Plate Parameters.
		self.rows = 0
		self.cols = 0
		self.well_size_cm = 0.0
		self.table_start_cm = 0.0
		self.carriage_start_cm = 0.0
		self.orientation = "landscape"
		self._has_plate = False

		# Waste-bin marker — set by ``set_waste_bin``; ``False`` until
		# the operator enters valid Waste Bin Position fields.
		# Waste-bin rectangle (center + extents). ``waste_x_cm`` /
		# ``waste_y_cm`` are the bin's CENTER in motor cm; extents
		# describe the full width/height of the rectangle so the bin
		# occupies [center − extent/2, center + extent/2] on each axis.
		# Default extents = 0 falls back to a small point marker at the
		# center.
		self.waste_x_cm = 0.0
		self.waste_y_cm = 0.0
		self.waste_x_extent_cm = 0.0
		self.waste_y_extent_cm = 0.0
		self._has_waste = False

		# Live crosshair (Phase 3) — driven by App's 100 ms polling of
		# the motor positions. ``_crosshair_h`` / ``_crosshair_v`` are
		# Tk canvas item IDs for the horizontal and vertical lines so
		# ``update_position`` can move them in place via
		# ``canvas.coords()`` without rebuilding the entire canvas
		# (which would flicker visibly at 10 Hz). They are recreated
		# on every full ``_redraw`` since ``delete("all")`` would have
		# nuked them.
		self._position_x_cm = 0.0
		self._position_y_cm = 0.0
		self._has_position = False
		self._crosshair_h = None
		self._crosshair_v = None

		# Resize redraws — Phase 4 makes this responsive; for Phase 1
		# the bind exists so the first ``<Configure>`` after pack()
		# triggers a draw at the real geometry.
		self.canvas.bind("<Configure>", lambda _e: self._redraw())

	# -- Public API -----------------------------------------------------

	def set_plate(self, rows, cols, well_size_cm, table_start_cm,
			carriage_start_cm, orientation):
		"""Set the plate parameters and re-render."""
		self.rows = int(rows)
		self.cols = int(cols)
		self.well_size_cm = float(well_size_cm)
		self.table_start_cm = float(table_start_cm)
		self.carriage_start_cm = float(carriage_start_cm)
		if orientation in ("portrait", "landscape"):
			self.orientation = orientation
		self._has_plate = True
		self._redraw()

	def clear(self):
		"""Drop the plate from the view (Plate Parameters became
		invalid). Table outline still draws so the operator sees the
		canvas is alive — it's the plate that hides."""
		self._has_plate = False
		self._redraw()

	def set_waste_bin(self, waste_x_cm, waste_y_cm,
			x_extent_cm=0.0, y_extent_cm=0.0):
		"""Show the waste-bin marker.

		``waste_x_cm`` / ``waste_y_cm`` are the CENTER of the bin
		rectangle in motor cm. ``x_extent_cm`` / ``y_extent_cm`` are
		the full width/height; the rectangle spans
		``[center − extent/2, center + extent/2]`` on each axis. When
		both extents are 0 (legacy default) the marker falls back to a
		small labelled point at the center."""
		self.waste_x_cm = float(waste_x_cm)
		self.waste_y_cm = float(waste_y_cm)
		self.waste_x_extent_cm = max(0.0, float(x_extent_cm))
		self.waste_y_extent_cm = max(0.0, float(y_extent_cm))
		self._has_waste = True
		self._redraw()

	def clear_waste_bin(self):
		"""Hide the waste-bin marker (Waste Bin Position became
		invalid). The origin marker and table outline stay visible."""
		self._has_waste = False
		self._redraw()

	def update_position(self, x_cm, y_cm):
		"""Move the live crosshair to ``(x_cm, y_cm)`` in motor cm.
		Called by the App's polling loop at ~10 Hz; uses
		``canvas.coords()`` in-place when possible so the table
		outline / plate / markers don't flicker. If the items have
		been nuked by a prior ``_redraw`` (resize, param change), this
		falls back to creating them fresh.
		"""
		self._position_x_cm = float(x_cm)
		self._position_y_cm = float(y_cm)
		self._has_position = True
		# Fast path: shift the existing crosshair items.
		if (self._crosshair_h is not None
				and self._crosshair_v is not None):
			try:
				geom = self._scale()
				self._place_crosshair(geom)
				return
			except tk.TclError:
				# Items got destroyed under us (e.g., widget tear-down).
				self._crosshair_h = None
				self._crosshair_v = None
		# Slow path: create them. Skip if canvas isn't laid out yet.
		w = self.canvas.winfo_width()
		h = self.canvas.winfo_height()
		if w < 30 or h < 30:
			return
		geom = self._scale()
		self._draw_crosshair(geom)

	def clear_position(self):
		"""Hide the crosshair. Idempotent."""
		self._has_position = False
		for attr in ("_crosshair_h", "_crosshair_v"):
			item = getattr(self, attr)
			if item is not None:
				try:
					self.canvas.delete(item)
				except tk.TclError:
					pass
				setattr(self, attr, None)

	# -- Geometry -------------------------------------------------------

	def _scale(self):
		"""Return ``(px_per_mm, table_x_offset_px, table_y_offset_px,
		table_w_px, table_h_px)`` for the current canvas size. The
		table is centered (letter-boxed) so aspect ratio is preserved
		regardless of how the operator stretches the window.
		"""
		w = max(self.canvas.winfo_width(), 1)
		h = max(self.canvas.winfo_height(), 1)
		usable_w = max(1, w - 2 * self._INNER_PAD_PX)
		usable_h = max(1, h - 2 * self._INNER_PAD_PX)
		px_per_mm = min(
			usable_w / TABLE_WIDTH_MM,
			usable_h / TABLE_HEIGHT_MM,
		)
		table_w_px = TABLE_WIDTH_MM * px_per_mm
		table_h_px = TABLE_HEIGHT_MM * px_per_mm
		x_offset = (w - table_w_px) / 2
		y_offset = (h - table_h_px) / 2
		return px_per_mm, x_offset, y_offset, table_w_px, table_h_px

	def _cm_to_px(self, x_cm, y_cm, geom=None):
		"""Convert a motor (table_cm, carriage_cm) point to canvas
		pixels. Pass the cached ``_scale()`` tuple as ``geom`` to
		avoid recomputing it for many points in one draw."""
		if geom is None:
			geom = self._scale()
		px_per_mm, x_offset, y_offset, _w, _h = geom
		return (
			x_offset + x_cm * 10.0 * px_per_mm,
			y_offset + y_cm * 10.0 * px_per_mm,
		)

	# -- Drawing --------------------------------------------------------

	def _redraw(self):
		"""Repaint the table outline, the SBS labware footprint (if
		Plate Parameters are set), and the empty well grid inside the
		footprint with the operator's orientation-aware A1 corner.

		Coordinate convention:
		  * ``(table_start_cm, carriage_start_cm)`` is the position
		    the operator JOGGED THE NEEDLE TO when calibrating — i.e.,
		    A1's CENTRE in motor cm.
		  * Plate orientation determines which corner of the well grid
		    A1 occupies. This matches ``WellPlateProgress`` (the
		    standalone plate preview) so the two views agree on A1's
		    location.
		      LANDSCAPE — A1 at the TOP-LEFT of the grid. Cols extend
		        right (+X = east); rows extend down (+Y = south).
		        Footprint is 127.76 × 85.48 mm.
		      PORTRAIT  — A1 at the BOTTOM-LEFT of the grid. Rows
		        extend right (+X); cols extend up (−Y = north).
		        Footprint is 85.48 × 127.76 mm.
		  * The drawn plate rectangle is the SBS standard footprint,
		    not the bare well-grid extent. Same footprint for every
		    SBS-standard plate regardless of well count; the well grid
		    sits centred inside it via the ``margin_x/y`` insets.
		  * All drawing uses a single ``px_per_mm`` scale derived from
		    the physical table dimensions so the plate, wells, and
		    markers share the table-outline coordinate system.

		Markers and crosshair sit on top of all this via
		``_draw_markers`` and ``_draw_crosshair``.
		"""
		self.canvas.delete("all")
		w = self.canvas.winfo_width()
		h = self.canvas.winfo_height()
		if w < 30 or h < 30:
			return
		geom = self._scale()
		pxmm, x_off, y_off, t_w, t_h = geom

		# Table outline — solid rectangle covering the scaled extent
		# of the physical table. Fill is a slightly lighter shade so
		# it visibly stands apart from the canvas background.
		self.canvas.create_rectangle(
			x_off, y_off, x_off + t_w, y_off + t_h,
			fill="#fafafa", outline="#555555", width=1,
		)

		# Diagnostic ghost plates: two SBS-portrait footprints tiling
		# the table side-by-side (left half + right half along X).
		# If TABLE_WIDTH_MM and TABLE_HEIGHT_MM are correct, these
		# rectangles cover the table EXACTLY with no gaps or overhang.
		# Dashed outline so they read as a sanity-check overlay rather
		# than as labware. Remove by flipping ``_DEBUG_GHOST_PLATES``
		# to ``False`` once the user has confirmed the layout.
		if self._DEBUG_GHOST_PLATES:
			g_w = SBS_FOOTPRINT_SHORT_MM * pxmm  # 85.48 mm in portrait
			g_h = SBS_FOOTPRINT_LONG_MM * pxmm   # 127.76 mm in portrait
			# Left ghost: (0, 0) to (85.48, 127.76) mm
			self.canvas.create_rectangle(
				x_off, y_off, x_off + g_w, y_off + g_h,
				outline="#cc8844", width=1, dash=(4, 3),
			)
			# Right ghost: (85.48, 0) to (170.96, 127.76) mm
			self.canvas.create_rectangle(
				x_off + g_w, y_off, x_off + 2 * g_w, y_off + g_h,
				outline="#cc8844", width=1, dash=(4, 3),
			)

		if not self._has_plate or self.rows <= 0 or self.cols <= 0:
			self._draw_markers(geom)
			self._crosshair_h = None
			self._crosshair_v = None
			if self._has_position:
				self._draw_crosshair(geom)
			return
		if self.well_size_cm <= 0:
			self._draw_markers(geom)
			self._crosshair_h = None
			self._crosshair_v = None
			if self._has_position:
				self._draw_crosshair(geom)
			return

		# Starting Well Position = A1's CENTRE in motor cm (positive
		# magnitudes east + south of origin).
		a1_x_mm = self.table_start_cm * 10.0
		a1_y_mm = self.carriage_start_cm * 10.0
		ws_mm = self.well_size_cm * 10.0
		half_ws = ws_mm / 2.0

		# Footprint dimensions transpose with orientation; the well
		# grid extents and A1's corner of the grid swap accordingly.
		# See ``_redraw`` docstring for the convention.
		if self.orientation == "landscape":
			footprint_w_mm = SBS_FOOTPRINT_LONG_MM
			footprint_h_mm = SBS_FOOTPRINT_SHORT_MM
			grid_w_mm = self.cols * ws_mm
			grid_h_mm = self.rows * ws_mm
			# A1 at TOP-LEFT of grid → grid's UL corner is one
			# half-well NW of A1's centre.
			grid_left_mm = a1_x_mm - half_ws
			grid_top_mm  = a1_y_mm - half_ws
		else:
			footprint_w_mm = SBS_FOOTPRINT_SHORT_MM
			footprint_h_mm = SBS_FOOTPRINT_LONG_MM
			grid_w_mm = self.rows * ws_mm
			grid_h_mm = self.cols * ws_mm
			# A1 at BOTTOM-LEFT of grid → grid's UL corner is one
			# half-well west of A1 and (cols·ws − half_ws) NORTH of
			# A1's centre.
			grid_left_mm = a1_x_mm - half_ws
			grid_top_mm  = a1_y_mm + half_ws - grid_h_mm

		# SBS footprint centres the well grid via per-orientation
		# margins. Standard plates have positive margins; extreme
		# operator inputs that produce a grid larger than the SBS
		# footprint give negative margins and let the rectangle clip
		# the wells — appropriate feedback that the input is non-SBS.
		margin_x_mm = (footprint_w_mm - grid_w_mm) / 2.0
		margin_y_mm = (footprint_h_mm - grid_h_mm) / 2.0
		fp_left_mm = grid_left_mm - margin_x_mm
		fp_top_mm  = grid_top_mm  - margin_y_mm

		fp_x1 = x_off + fp_left_mm * pxmm
		fp_y1 = y_off + fp_top_mm  * pxmm
		fp_x2 = fp_x1 + footprint_w_mm * pxmm
		fp_y2 = fp_y1 + footprint_h_mm * pxmm
		self.canvas.create_rectangle(
			fp_x1, fp_y1, fp_x2, fp_y2,
			fill="#ffffff", outline="#444444", width=1,
		)

		# Wells — orientation-aware. Each well's centre is anchored to
		# A1's centre (= Starting Well Position) so the crosshair
		# lands directly on the well labelled "A1" after calibration.
		# Landscape: cols extend east (+X), rows extend south (+Y).
		# Portrait:  rows extend east (+X), cols extend north (−Y).
		well_r_px = half_ws * pxmm * 0.85
		for col_idx in range(self.cols):
			for row_idx in range(self.rows):
				if self.orientation == "landscape":
					cx_mm = a1_x_mm + col_idx * ws_mm
					cy_mm = a1_y_mm + row_idx * ws_mm
				else:
					cx_mm = a1_x_mm + row_idx * ws_mm
					cy_mm = a1_y_mm - col_idx * ws_mm
				cx = x_off + cx_mm * pxmm
				cy = y_off + cy_mm * pxmm
				self.canvas.create_oval(
					cx - well_r_px, cy - well_r_px,
					cx + well_r_px, cy + well_r_px,
					fill="", outline="#999999", width=1,
				)

		# Origin + waste-bin markers sit on top of the static layout.
		self._draw_markers(geom)

		# Crosshair sits on top of everything. ``delete("all")`` at the
		# top of this method nuked the prior items, so reset and
		# re-create when a position is set.
		self._crosshair_h = None
		self._crosshair_v = None
		if self._has_position:
			self._draw_crosshair(geom)

	# -- Static markers (Phase 2) ---------------------------------------

	def _draw_markers(self, geom):
		"""Origin marker (always) + waste-bin marker (when set).
		Drawn after the plate/wells so they sit on top of the static
		layout. The crosshair (Phase 3) will sit on top of these.
		"""
		pxmm, x_off, y_off, _t_w, _t_h = geom

		# Origin marker — a small "+" cross at canvas (0, 0) mm.
		# Arm length 6 mm; thicker line so it reads against the table
		# fill. Stays in fixed dark grey so the operator can use it
		# as a stable spatial anchor.
		arm_mm = 6.0
		arm_px = arm_mm * pxmm
		ox = x_off
		oy = y_off
		self.canvas.create_line(
			ox - arm_px, oy, ox + arm_px, oy,
			fill="#333333", width=2,
		)
		self.canvas.create_line(
			ox, oy - arm_px, ox, oy + arm_px,
			fill="#333333", width=2,
		)
		# Caption sits in the canvas's upper-left corner (outside the
		# table outline) so it can't be confused with the plate's
		# starting well — placing it next to the cross used to drop the
		# text inside the white plate footprint in landscape configs.
		self.canvas.create_text(
			4, 4, text="Origin", anchor="nw",
			font=("TkDefaultFont", 8), fill="#333333",
		)

		# Waste-bin marker. Two render modes:
		#   * Both extents > 0  — render the bin as a semi-transparent
		#     amber rectangle from (anchor) to (anchor + extent). Tk
		#     canvas has no true alpha; the stippled fill gives an
		#     approximate ~30% visual weight. Label "Waste Bin"
		#     centred inside if there's room (>= 30 px on the short
		#     side).
		#   * Either extent == 0 — fall back to a small 4 mm circle
		#     at the anchor so the marker is still visible in the
		#     pre-configuration state.
		if not self._has_waste:
			return
		# Center, in canvas pixels.
		cx = x_off + self.waste_x_cm * 10.0 * pxmm
		cy = y_off + self.waste_y_cm * 10.0 * pxmm
		if self.waste_x_extent_cm > 0 and self.waste_y_extent_cm > 0:
			# Center-based rectangle: span ±extent/2 on each axis.
			half_w = self.waste_x_extent_cm * 10.0 * pxmm / 2.0
			half_h = self.waste_y_extent_cm * 10.0 * pxmm / 2.0
			x1, y1 = cx - half_w, cy - half_h
			x2, y2 = cx + half_w, cy + half_h
			self.canvas.create_rectangle(
				x1, y1, x2, y2,
				fill="#f0c060",
				stipple="gray25",
				outline="#8a6a00", width=1,
			)
			# Label only if the rectangle is big enough to fit it.
			if min(x2 - x1, y2 - y1) >= 30:
				self.canvas.create_text(
					cx, cy,
					text="Waste Bin",
					font=("TkDefaultFont", 8, "bold"),
					fill="#3a2a00",
				)
		else:
			# Pre-configuration fallback: 4 mm circle at the center.
			r_px = 2.0 * pxmm
			self.canvas.create_oval(
				cx - r_px, cy - r_px,
				cx + r_px, cy + r_px,
				fill="#f0c060", outline="#8a6a00", width=1,
			)
			# Small caption to the right of the dot.
			self.canvas.create_text(
				cx + r_px + 3, cy,
				text="Waste",
				anchor="w",
				font=("TkDefaultFont", 8),
				fill="#3a2a00",
			)

	# -- Live crosshair (Phase 3) ---------------------------------------

	_CROSSHAIR_ARM_MM = 5.0  # 10 mm extent total
	_CROSSHAIR_COLOUR = "#cc0000"  # bright red — stands out vs the
	# white plate / amber bin / gray table / dark origin marker.

	def _crosshair_coords(self, geom):
		"""Return ``(cx, cy, arm_px)`` in canvas pixels for the
		current crosshair position."""
		pxmm, x_off, y_off, _t_w, _t_h = geom
		cx = x_off + self._position_x_cm * 10.0 * pxmm
		cy = y_off + self._position_y_cm * 10.0 * pxmm
		arm_px = self._CROSSHAIR_ARM_MM * pxmm
		return cx, cy, arm_px

	def _draw_crosshair(self, geom):
		"""Create the crosshair items. Called at the end of
		``_redraw`` (after the full repaint nuked the canvas) and by
		``update_position`` when no existing items can be moved."""
		cx, cy, arm_px = self._crosshair_coords(geom)
		self._crosshair_h = self.canvas.create_line(
			cx - arm_px, cy, cx + arm_px, cy,
			fill=self._CROSSHAIR_COLOUR, width=2,
		)
		self._crosshair_v = self.canvas.create_line(
			cx, cy - arm_px, cx, cy + arm_px,
			fill=self._CROSSHAIR_COLOUR, width=2,
		)

	def _place_crosshair(self, geom):
		"""Move the existing crosshair items via ``canvas.coords()``.
		Raises ``tk.TclError`` if the items have been destroyed —
		``update_position`` handles that by falling back to a fresh
		``_draw_crosshair``."""
		cx, cy, arm_px = self._crosshair_coords(geom)
		self.canvas.coords(
			self._crosshair_h,
			cx - arm_px, cy, cx + arm_px, cy,
		)
		self.canvas.coords(
			self._crosshair_v,
			cx, cy - arm_px, cx, cy + arm_px,
		)


class WellPlateProgress(tk.Frame):
	"""Per-well progress view backed by a Tk Canvas.

	Lifecycle:
	  - Construct once; the canvas is empty until ``begin_run`` is called.
	  - ``begin_run`` sets dimensions + starts the elapsed/remaining clock.
	  - State-transition methods (``well_dispensing`` / ``_waiting`` /
	    ``_completed`` / ``_skipped``) update one well and the header.
	  - ``end_run`` stops the pulsing border and the clock tick.
	"""

	# Margins reserved inside the canvas for the row letters
	# (left) and column numbers (top) in landscape orientation.
	# Portrait swaps these: row letters along the bottom, column
	# numbers along the left, both rotated 90°. The portrait
	# bottom/left margins below are sized to comfortably hold a
	# rotated label at the largest plausible label_font_size.
	_LEFT_MARGIN = 22
	_TOP_MARGIN = 18
	_RIGHT_MARGIN = 6
	_BOTTOM_MARGIN = 6

	# Extra clearance reserved in portrait for the rotated labels,
	# which live along the bottom edge (row letters) and the left
	# edge (column numbers) adjacent to the wells.
	_PORTRAIT_LABEL_GAP = 6
	_PORTRAIT_LABEL_CLEARANCE = 30

	_PULSE_INTERVAL_MS = 500
	_CLOCK_INTERVAL_MS = 1000

	def __init__(self, parent, min_width=420, min_height=260):
		super().__init__(parent)
		logger.debug("WellPlateProgress created (id=%s)", id(self))

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
		# current_lbl carries the detailed phase status (e.g.
		# "Pumping well G2 (col 1, row 8)...") -- promoted from the
		# system status bar's middle area so the plate-area header is
		# self-contained. App.set_status mirrors its text here while a
		# run is active.
		self.current_lbl = tk.Label(
			self, text="", anchor="w", justify="left", width=60,
			font=("TkDefaultFont", 11, "bold"),
		)
		self.current_lbl.grid(row=1, column=0, sticky="w", padx=4, pady=(2, 4))
		# count_lbl + time_lbl still exist as Tk objects so calling
		# code that updates them is a harmless no-op; they're not
		# gridded so they take no vertical space.
		self.count_lbl = tk.Label(self, text="", anchor="w", width=40)
		self.time_lbl = tk.Label(self, text="", anchor="w", width=60)

		self.canvas = tk.Canvas(
			self, bg="white", bd=0, highlightthickness=1,
			highlightbackground="#bbbbbb",
			width=min_width, height=min_height,
		)
		self.canvas.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))

		self.grid_columnconfigure(0, weight=1)
		self.grid_rowconfigure(2, weight=1)

		# Plate state
		self.rows = 0
		self.cols = 0
		# Plate orientation drives the canvas layout (tall vs wide).
		# ``"landscape"`` (legacy default): plate cols across the
		# canvas X-axis, rows down the Y-axis. ``"portrait"`` (current
		# default in App init): plate ROWS across the canvas X-axis,
		# COLS down the Y-axis with col 1 at the bottom. Both modes
		# store status_grid keyed by the orientation-independent
		# (col_idx, row_idx) tuple — the remapping happens at paint
		# time so well_dispensing(x, y) callers don't need to know
		# about orientation. The widget's "A1 corner" is a layout
		# choice; the mechanical origin lives in App and is always the
		# upper-left mechanical limit regardless of orientation.
		self.orientation = "landscape"
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

		# Preview mode: set by ``show_preview`` before any run starts so
		# the widget paints an empty plate the operator can use to
		# position labware on the table. Cleared by ``begin_run`` (the
		# live visualization takes over) and by ``clear_preview`` (when
		# Plate Parameters become invalid).
		self._is_preview = False

		# Pulse animation state
		self._pulse_thick = False
		self._pulse_after = None

		# Elapsed clock. ``_elapsed_accum_s`` holds the running total of
		# active-time accrued through the most recent pause; ``_active_since_mono``
		# is the monotonic timestamp the run last resumed (or None when
		# the clock is paused). Elapsed at any instant =
		#   _elapsed_accum_s + (monotonic() - _active_since_mono if active else 0).
		# This intentionally excludes operator-blocking pauses (inter-sample
		# purge modal phases, plate-swap dialog, manual Pause) and the
		# auto-pause states (total_reached, plate_full, e-stop). The widget
		# exposes pause_elapsed() / resume_elapsed() for App to call; the
		# Remaining estimate that previously sat next to Elapsed was removed
		# because its inputs (operator-controlled modal pauses, purge
		# extensions, plate-swap dwell) are unbounded by design.
		self._elapsed_accum_s = 0.0
		self._active_since_mono = None
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

	def begin_run(self, cols, rows, volume_per_well, pump_time,
			orientation=None):
		"""Reset the plate to a fresh run and start the elapsed-time clock.

		``orientation`` (optional) selects the canvas layout:
		``"portrait"`` (tall, 8 cols × 12 rows; col 1 anchored at the
		bottom of the canvas) or ``"landscape"`` (wide, 12 cols × 8
		rows; col 1 anchored at the left). ``None`` keeps whatever
		orientation the widget already holds. Callers without
		orientation context get the legacy landscape layout (the
		widget's default).
		"""
		self.rows = rows
		self.cols = cols
		if orientation in ("portrait", "landscape"):
			self.orientation = orientation
		self.volume_per_well = volume_per_well
		self.pump_time = pump_time
		self.status_grid = {(x, y): UNVISITED for x in range(cols) for y in range(rows)}
		self.error_reasons = {}
		self.well_records = {}
		self.dispensing_xy = None
		# Live run takes over from any preview state.
		self._is_preview = False
		self._stop_pulse()
		self._elapsed_accum_s = 0.0
		self._active_since_mono = monotonic()
		self._start_clock()
		self._redraw()
		self._update_header_count()
		self.current_lbl["text"] = ""

	def show_preview(self, cols, rows, orientation=None):
		"""Render an empty plate preview before any run starts.

		Drawn whenever the Plate Parameters in Automated mode all
		validate, so the operator can use the canvas as a placement
		guide for orienting the labware on the table. Unlike
		``begin_run`` this does NOT start the elapsed-time clock and
		does NOT count as a run start — the App's state machine stays
		at ``"idle"`` until the operator clicks Begin Fractionation.

		The well grid uses the same drawing logic as a live run; the
		only visible differences are:

		  * A subtle accent ring around well A1 anchors the operator's
		    orientation reference.
		  * The plate-label header reads "Plate preview — orient the
		    plate according to this layout." instead of the run's
		    plate ID.

		Idempotent — calling repeatedly with the same args is a no-op
		paint. Safe to call at any moment while the app is idle; the
		caller is responsible for not invoking it during an active run
		(``App`` gates this via ``state.state == "idle"``).
		"""
		self.rows = rows
		self.cols = cols
		if orientation in ("portrait", "landscape"):
			self.orientation = orientation
		self.status_grid = {(x, y): UNVISITED for x in range(cols)
			for y in range(rows)}
		self.error_reasons = {}
		self.well_records = {}
		self.dispensing_xy = None
		self._is_preview = True
		self._stop_pulse()
		# Preview is static — no elapsed clock, no header counters.
		self.plate_lbl["text"] = (
			"Plate preview — orient the plate according to this layout."
		)
		self.current_lbl["text"] = ""
		self._redraw()

	def clear_preview(self):
		"""Hide the plate preview (called when Plate Parameters fall
		out of validation). Idempotent. Caller is responsible for not
		invoking this during an active run."""
		self._is_preview = False
		self.rows = 0
		self.cols = 0
		self.status_grid = {}
		self.error_reasons = {}
		self.well_records = {}
		self.dispensing_xy = None
		self.plate_lbl["text"] = ""
		self.current_lbl["text"] = ""
		self._redraw()

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
		"""Stop the pulsing border and the elapsed clock tick.

		Wells keep whatever status they have so the final state stays on
		screen until the next ``begin_run``.
		"""
		self.pause_elapsed()
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
		self._elapsed_accum_s = 0.0
		self._active_since_mono = None
		self.plate_lbl["text"] = ""
		self.current_lbl["text"] = ""
		self.count_lbl["text"] = ""
		self.time_lbl["text"] = ""
		self._redraw()

	def refresh_from_state(self):
		"""Force a full canvas repaint from the widget's own
		``status_grid`` + ``well_records`` and refresh the header
		labels. Safe to call any time -- a no-op until ``begin_run``
		has been called (``rows``/``cols`` still zero).

		Called when the AutomatedFrame becomes visible again after a
		mode switch: the state machine kept ``status_grid`` up to date
		while the frame was hidden, but a stray ``<Configure>`` during
		the hidden period may have cleared the canvas item cache. This
		method rebuilds it.
		"""
		logger.debug(
			"WellPlateProgress refresh_from_state: records=%d wells_done=%d dispensing=%s",
			len(self.well_records),
			sum(1 for s in self.status_grid.values() if s != UNVISITED),
			self.dispensing_xy,
		)
		self._redraw()
		self._update_header_count()
		self._update_time_label()
		if self.dispensing_xy is not None:
			self._update_current_label(*self.dispensing_xy)
			self._start_pulse()

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

	def set_orientation(self, orientation):
		"""Switch the canvas's plate orientation and trigger a redraw.
		Called outside ``begin_run`` (e.g. when the operator changes the
		orientation in Tools → Preferences with no run active) so the
		idle canvas reflects the new layout immediately. No-op if the
		orientation is unchanged or unrecognised."""
		if orientation not in ("portrait", "landscape"):
			return
		if orientation == self.orientation:
			return
		self.orientation = orientation
		self._redraw()

	def _grid_dims(self):
		"""Return the (canvas_cols, canvas_rows) tuple — i.e. how many
		well columns and rows the canvas paints. In landscape this is
		(plate_cols, plate_rows); in portrait the plate is rotated 90°
		so the canvas shows (plate_rows, plate_cols)."""
		if self.orientation == "portrait":
			return self.rows, self.cols
		return self.cols, self.rows

	def _logical_to_canvas(self, x, y):
		"""Map a logical well (x=plate-col-index, y=plate-row-index) to
		canvas grid (col, row). Identity in landscape; in portrait the
		plate is rotated so plate rows run across the canvas (rows on
		X-axis) and plate columns run UP the canvas (col 1 at the
		bottom, so canvas row index = (plate_cols - 1) - x)."""
		if self.orientation == "portrait":
			return y, (self.cols - 1) - x
		return x, y

	def _canvas_to_logical(self, cx, cy):
		"""Inverse of ``_logical_to_canvas``. Used by hover hit-testing
		to translate canvas (col, row) back to plate (col_idx, row_idx)."""
		if self.orientation == "portrait":
			# canvas (col, row) → plate (x, y): plate_y = canvas_col,
			# plate_x = (cols - 1) - canvas_row.
			return (self.cols - 1) - cy, cx
		return cx, cy

	def _redraw(self):
		"""Recompute layout (cell sizes, margins, label positions) + redraw.

		Cell size is derived from the LIVE canvas dimensions so the plate
		grows and shrinks with the window. Wells render at a diameter of
		(1 - spacing) * cell_size, clamped to ``[_WELL_PX_MIN,
		_WELL_PX_MAX]`` so they stay legible at extreme aspect ratios.
		The plate is centered within the usable area when one axis runs
		out of room before the other.

		Orientation drives the canvas layout via ``_grid_dims`` (which
		decides the (cols_on_canvas, rows_on_canvas) pair) and
		``_logical_to_canvas`` (which places each well at its rotated
		position). Logical well_id semantics are orientation-independent.
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

		# Per-orientation label margin layout. In landscape, the row
		# letters live on the LEFT edge and the column numbers across
		# the TOP — matches the physical plate held upright. In
		# portrait the plate is tilted: row letters move to the
		# BOTTOM edge and column numbers stay on the LEFT but run
		# bottom-to-top (col 1 at the bottom = A1 corner). Both
		# portrait label sets are rotated 90° so they read along
		# their respective axes when the operator tilts the screen
		# clockwise to match the physical plate.
		portrait_label_band = self._PORTRAIT_LABEL_GAP + self._PORTRAIT_LABEL_CLEARANCE
		if self.orientation == "portrait":
			top_margin = self._BOTTOM_MARGIN
			bottom_margin = self._BOTTOM_MARGIN + portrait_label_band
			left_margin = self._RIGHT_MARGIN + portrait_label_band
			right_margin = self._RIGHT_MARGIN
		else:
			top_margin = self._TOP_MARGIN
			bottom_margin = self._BOTTOM_MARGIN
			left_margin = self._LEFT_MARGIN
			right_margin = self._RIGHT_MARGIN

		usable_w = w - left_margin - right_margin
		usable_h = h - top_margin - bottom_margin

		canvas_cols, canvas_rows = self._grid_dims()

		# Largest cell that fits in the usable area, accounting for the
		# inter-well gap. Width-limited and height-limited candidates --
		# the constrained axis wins.
		spacing = 1.0 + self._CELL_SPACING_FRAC
		cell_from_w = usable_w / (canvas_cols * spacing)
		cell_from_h = usable_h / (canvas_rows * spacing)
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
		plate_w = cell_size * canvas_cols - gap_correction
		plate_h = cell_size * canvas_rows - gap_correction
		x_offset = left_margin + max(0, (usable_w - plate_w) / 2)
		y_offset = top_margin + max(0, (usable_h - plate_h) / 2)

		# Row/column label font scales with cell size, capped so the
		# labels don't crowd small wells (min 8 pt) or shout on big ones
		# (cap a few pts below the in-well sequence font).
		label_font_size = max(8, min(int(cell_size // 4), icon_font_size - 1))

		# Labels — orientation-dependent placement, both drawn in
		# the main canvas adjacent to the wells.
		if self.orientation == "landscape":
			# Row letters down the LEFT inside-margin, column
			# numbers across the TOP inside-margin. No rotation.
			for c in range(canvas_cols):
				cx = x_offset + cell_size * (c + 0.5)
				self.canvas.create_text(
					cx, y_offset - top_margin / 2,
					text=str(c + 1),
					font=("TkDefaultFont", label_font_size),
				)
			for r in range(canvas_rows):
				cy = y_offset + cell_size * (r + 0.5)
				self.canvas.create_text(
					x_offset - left_margin / 2, cy,
					text=chr(ord("A") + r),
					font=("TkDefaultFont", label_font_size),
				)
		else:
			# Portrait: rotated row letters along the BOTTOM,
			# rotated column numbers down the LEFT.
			label_band_mid = self._PORTRAIT_LABEL_CLEARANCE / 2
			label_y = y_offset + plate_h + self._PORTRAIT_LABEL_GAP + label_band_mid
			label_x = x_offset - self._PORTRAIT_LABEL_GAP - label_band_mid
			for c in range(canvas_cols):
				cx = x_offset + cell_size * (c + 0.5)
				self.canvas.create_text(
					cx, label_y,
					text=chr(ord("A") + c),
					font=("TkDefaultFont", label_font_size),
					angle=90,
				)
			for r in range(canvas_rows):
				cy = y_offset + cell_size * (r + 0.5)
				self.canvas.create_text(
					label_x, cy,
					text=str(self.cols - r),
					font=("TkDefaultFont", label_font_size),
					angle=90,
				)

		# Wells. Iterate logical coords; place each at its rotated
		# canvas position.
		for x in range(self.cols):
			for y in range(self.rows):
				cc, cr = self._logical_to_canvas(x, y)
				cx = x_offset + cell_size * (cc + 0.5)
				cy = y_offset + cell_size * (cr + 0.5)
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

		# Preview-mode A1 accent: a thin green ring around well A1 so
		# the operator can see at a glance where A1 lands in the
		# current orientation. Drawn AFTER the wells so it sits on top.
		# In landscape A1 is at the canvas upper-left; in portrait it's
		# at the canvas lower-left.
		if self._is_preview and self.cols > 0 and self.rows > 0:
			a1_cc, a1_cr = self._logical_to_canvas(0, 0)
			a1_cx = x_offset + cell_size * (a1_cc + 0.5)
			a1_cy = y_offset + cell_size * (a1_cr + 0.5)
			accent_r = well_radius + 3
			self.canvas.create_oval(
				a1_cx - accent_r, a1_cy - accent_r,
				a1_cx + accent_r, a1_cy + accent_r,
				outline="#1e7d20", width=2,
			)

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
			f"Dispensing into {_well_id(x, y)} — {self.volume_per_well:g} mL"
		)

	def _update_header_count(self):
		total = self.rows * self.cols
		started = sum(1 for s in self.status_grid.values() if s != UNVISITED)
		pct = (started / total * 100) if total else 0.0
		self.count_lbl["text"] = f"Well {started} of {total} ({pct:.1f}%)"

	def pause_elapsed(self):
		"""Freeze the Elapsed counter at its current value. Called from
		App on every state transition that stops active fractionation:
		manual Pause, auto-pause-at-total-reached, plate-full auto-pause,
		inter-sample purge modal display, plate-swap dialog display,
		terminate, end_run.

		Idempotent -- calling while already paused leaves the accumulator
		unchanged. Safe to call before begin_run too.
		"""
		if self._active_since_mono is None:
			return
		self._elapsed_accum_s += monotonic() - self._active_since_mono
		self._active_since_mono = None
		self._update_time_label()

	def resume_elapsed(self):
		"""Unfreeze the Elapsed counter so it starts accruing again.
		Called from App when the run transitions back into active
		fractionation: Resume button, Continue to Next Sample (after
		the purge workflow completes), Continue to Next Plate (after
		the plate swap completes), Space-bar extension cycle in the
		purge modal.

		Idempotent -- calling while already running is a no-op.
		"""
		if self._active_since_mono is not None:
			return
		self._active_since_mono = monotonic()
		self._update_time_label()

	def _update_time_label(self):
		# Remaining was removed because operator-controlled modal pauses
		# (inter-sample purge phases, plate swap, Space extensions) make
		# any estimate unbounded; a wrong number is worse than no number.
		# Elapsed is the accumulator-driven "active fractionation time"
		# tally; it pauses across the same modal/auto-pause states.
		elapsed = self._elapsed_accum_s
		if self._active_since_mono is not None:
			elapsed += monotonic() - self._active_since_mono
		if elapsed <= 0 and self._active_since_mono is None:
			self.time_lbl["text"] = ""
			return
		self.time_lbl["text"] = f"Elapsed: {_fmt_hms(elapsed)}"

	# -- Tooltips --------------------------------------------------------

	def _well_at_pixel(self, px, py):
		"""Return logical (x=col_idx, y=row_idx) of the well under the
		pixel, or None. Hit-tests in canvas grid coords first, then
		maps back to plate-logical coords via ``_canvas_to_logical``
		so portrait orientation tooltips identify the right plate well."""
		g = self._geom
		if g is None:
			return None
		cs = g["cell_size"]
		canvas_col = int((px - g["x_offset"]) // cs)
		canvas_row = int((py - g["y_offset"]) // cs)
		canvas_cols, canvas_rows = self._grid_dims()
		if not (0 <= canvas_col < canvas_cols and 0 <= canvas_row < canvas_rows):
			return None
		# Hit only inside the circular well, not the cell's corners.
		cx = g["x_offset"] + cs * (canvas_col + 0.5)
		cy = g["y_offset"] + cs * (canvas_row + 0.5)
		if (px - cx) ** 2 + (py - cy) ** 2 > g["well_radius"] ** 2:
			return None
		return self._canvas_to_logical(canvas_col, canvas_row)

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
