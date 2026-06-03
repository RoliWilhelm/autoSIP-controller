"""Input validators for autoSIP GUI fields.

Each public validator takes the raw string from a Tk Entry and returns either
``(True, parsed_value)`` or ``(False, error_message)``. The error message is
displayed inline next to the field on failure; for Begin Fractionation a
summary of all failures is also shown via ``messagebox.showerror``.

The bounds below are starting points sized for typical SIP isopycnic
fractionation workflows. Some are deliberately tighter than the raw hardware
limits to keep accidental input within sensible ranges.

TODO: revisit bounds once we have real-instrument calibration data.
  * ``PUMP_RATE_MAX = 600`` is the Razel R-200 high-gear-set ceiling. The
    Adafruit 3910 peristaltic pump can push higher; when the pump-device
    selector lands in the header, make this pump-dependent.
  * ``TABLE_POS_MAX`` / ``CARRIAGE_POS_MAX`` match the current chassis's
    lead-screw travel; revise if the mechanics change.
  * ``ROWS_MAX = 16`` / ``COLS_MAX = 24`` cover up to a 384-well plate,
    which is past what we'd run today but leaves headroom.
"""

# Plate geometry
ROWS_MIN, ROWS_MAX = 1, 16
COLS_MIN, COLS_MAX = 1, 24
WELL_SIZE_MIN, WELL_SIZE_MAX = 0.1, 5.0  # cm

# Pump parameters.
# Pump rate units are mL/hr (1 mL = 1 cm^3 = 1 cc; the rate ceiling matches
# the Razel R-200 high-gear-set spec).
PUMP_RATE_MIN, PUMP_RATE_MAX = 0.1, 600.0  # mL/hr

# Per-well volume in mL. Lower bound is ~5x the volume of one aqueous drop
# (~20 µL = 0.02 mL), so 0.1 mL is the smallest dispensable volume that
# reliably forms a contiguous drip rather than a single droplet. Upper bound
# is a generous cap for typical SIP fractions.
VOLUME_MIN, VOLUME_MAX = 0.1, 2.0          # mL per well

# Drip wait between dispense and move-to-next, seconds. Lower bound of 0 lets
# the operator disable the wait entirely; upper bound is a sanity cap.
DRIP_WAIT_MIN, DRIP_WAIT_MAX = 0.0, 60.0

# Inter-sample purge duration, seconds. Two pump phases run for this many
# seconds each between samples: one drawing wash through the tubing, one
# pushing air to clear the wash. Lower bound 1 s prevents an effectively
# disabled purge; upper bound 10 min is a generous ceiling that catches
# unit-confusion typos.
PURGE_TIME_MIN, PURGE_TIME_MAX = 1.0, 600.0
PRIME_TIME_MIN, PRIME_TIME_MAX = 0.0, 600.0

# Bleach soak time (minutes) for the on-demand System Clean routine in
# Cleaning Mode. 0 lets the operator skip the soak entirely (e.g. for a
# water-only flush); 30 min is a generous ceiling for nucleic-acid
# decontamination protocols.
SOAK_TIME_MIN, SOAK_TIME_MAX = 0.0, 30.0

# Transit speed factor for Variable speed motor mode. Multiplier
# applied to the fractionation step rate when a move is flagged as
# transit (to/from waste bin, return to origin, plate swaps, etc.).
# Lower bound 1.0 = no speed-up (effectively same as Slow mode);
# upper bound 5.0 is a generous ceiling — beyond that, missed steps
# become likely on most lead-screw setups.
TRANSIT_SPEED_FACTOR_MIN, TRANSIT_SPEED_FACTOR_MAX = 1.0, 5.0

# Peristaltic pump rate, mL/min. Adafruit 3910 nominal ~100 mL/min; bound
# leaves room for slower flow restrictors below it and faster pumps above.
PERISTALTIC_RATE_MIN, PERISTALTIC_RATE_MAX = 1.0, 200.0

# Max waste-bin volume, mL. The estimated-volume tracker shuts off the
# pump at this threshold to prevent overflow. Lower bound 10 mL is the
# smallest container we can think of using; upper bound 5 L is generous
# for a bench-top jug.
MAX_WASTE_VOLUME_MIN, MAX_WASTE_VOLUME_MAX = 10.0, 5000.0

# Stage positions (lead-screw travel)
TABLE_POS_MIN, TABLE_POS_MAX = 0.0, 20.0       # cm
CARRIAGE_POS_MIN, CARRIAGE_POS_MAX = 0.0, 15.0  # cm

# Fractionation counts. Upper bound on N is the maximum plate capacity we
# allow (16*24 = 384); a stricter "N must fit on the plate" check happens
# at submit time inside AutomatedFrame.begin_clicked once rows/cols are
# known. Discard bound is 0..N-1 enforced cross-field, not here.
N_FRACTIONS_MIN, N_FRACTIONS_MAX = 1, ROWS_MAX * COLS_MAX
DISCARD_MIN, DISCARD_MAX = 0, ROWS_MAX * COLS_MAX - 1

# Run-identification fields. The character class is filesystem-safe and avoids
# spaces / slashes / colons so the strings can be embedded in path components
# (e.g. the per-run directory name) without escaping.
import re
IDENTIFIER_MAX = 64
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._\-]+$")


def _parse_int(text, label, lo, hi):
	text = (text or "").strip()
	if text == "":
		return False, f"{label} is required"
	try:
		value = int(text)
	except ValueError:
		return False, f"{label} must be a whole number (got '{text}')"
	if value < lo or value > hi:
		return False, f"{label} must be between {lo} and {hi} (got {value})"
	return True, value


def _parse_float(text, label, lo, hi, *, allow_empty=False, unit=None):
	"""Parse a float and bounds-check it. ``unit`` (optional) is appended
	to the bounds in the error message, e.g. ``"... between 0.1 and 2.0 mL"``.
	"""
	text = (text or "").strip()
	if text == "":
		if allow_empty:
			return True, None
		return False, f"{label} is required"
	try:
		value = float(text)
	except ValueError:
		return False, f"{label} must be a number (got '{text}')"
	if value < lo or value > hi:
		bounds = f"{lo} and {hi}" if unit is None else f"{lo} and {hi} {unit}"
		return False, f"{label} must be between {bounds} (got {value})"
	return True, value


def rows(text):
	"""Validate a row count: int in ``[ROWS_MIN, ROWS_MAX]``."""
	return _parse_int(text, "Number of rows", ROWS_MIN, ROWS_MAX)


def cols(text):
	"""Validate a column count: int in ``[COLS_MIN, COLS_MAX]``."""
	return _parse_int(text, "Number of columns", COLS_MIN, COLS_MAX)


def well_size(text):
	"""Validate well size (cm): float in ``[WELL_SIZE_MIN, WELL_SIZE_MAX]``."""
	return _parse_float(text, "Well size", WELL_SIZE_MIN, WELL_SIZE_MAX)


def pump_rate(text):
	"""Validate pump rate (mL/hr): float in ``[PUMP_RATE_MIN, PUMP_RATE_MAX]``."""
	return _parse_float(text, "Pump rate", PUMP_RATE_MIN, PUMP_RATE_MAX,
		unit="mL/hr")


def volume(text):
	"""Validate per-well volume (mL): float in ``[VOLUME_MIN, VOLUME_MAX]``."""
	return _parse_float(text, "Volume per well", VOLUME_MIN, VOLUME_MAX,
		unit="mL")


def drip_wait_time(text):
	"""Validate the post-pump drip wait (s): float in ``[DRIP_WAIT_MIN, DRIP_WAIT_MAX]``."""
	return _parse_float(text, "Drip wait time", DRIP_WAIT_MIN, DRIP_WAIT_MAX)


def purge_time(text):
	"""Validate per-phase inter-sample purge time (s): float in
	``[PURGE_TIME_MIN, PURGE_TIME_MAX]``. The same duration is used for
	the wash and air-clear phases."""
	return _parse_float(text, "Purge time", PURGE_TIME_MIN, PURGE_TIME_MAX,
		unit="s")


def prime_time(text):
	"""Validate the pre-fractionation prime time (s): float in
	``[PRIME_TIME_MIN, PRIME_TIME_MAX]``. The auto-prime step at the
	start of a run walks fractionation solution from the sample tube
	toward the syringe dispenser for this many seconds while the
	needle moves to its first-dispense position."""
	return _parse_float(text, "Prime time", PRIME_TIME_MIN, PRIME_TIME_MAX,
		unit="s")


def soak_time(text):
	"""Validate the System Clean bleach soak time (minutes): float in
	``[SOAK_TIME_MIN, SOAK_TIME_MAX]``. Phase 2 of the System Clean
	routine holds bleach static in the line for this many minutes
	before rinsing."""
	return _parse_float(text, "Bleach soak time", SOAK_TIME_MIN, SOAK_TIME_MAX,
		unit="min")


def transit_speed_factor(text):
	"""Validate the Variable speed mode's transit multiplier: float in
	``[TRANSIT_SPEED_FACTOR_MIN, TRANSIT_SPEED_FACTOR_MAX]``."""
	return _parse_float(
		text, "Transit speed factor",
		TRANSIT_SPEED_FACTOR_MIN, TRANSIT_SPEED_FACTOR_MAX,
		unit="×",
	)


def peristaltic_rate(text):
	"""Validate peristaltic pump rate (mL/min): float in
	``[PERISTALTIC_RATE_MIN, PERISTALTIC_RATE_MAX]``."""
	return _parse_float(text, "Peristaltic pump rate",
		PERISTALTIC_RATE_MIN, PERISTALTIC_RATE_MAX, unit="mL/min")


def max_waste_volume(text):
	"""Validate max waste-bin volume (mL): float in
	``[MAX_WASTE_VOLUME_MIN, MAX_WASTE_VOLUME_MAX]``."""
	return _parse_float(text, "Max waste bin volume",
		MAX_WASTE_VOLUME_MIN, MAX_WASTE_VOLUME_MAX, unit="mL")


def table_pos(text, *, allow_empty=False):
	"""Validate absolute table position (cm): float in ``[TABLE_POS_MIN, TABLE_POS_MAX]``.

	``allow_empty=True`` treats a blank string as ``(True, None)`` so the
	Move button can skip axes the user didn't fill in.
	"""
	return _parse_float(
		text, "Table position", TABLE_POS_MIN, TABLE_POS_MAX,
		allow_empty=allow_empty,
	)


# Waste-bin rectangle extents (cm). The bin's UL anchor lives in the
# existing ``waste_bin_table`` / ``waste_bin_carriage`` validators
# (``table_pos`` / ``carriage_pos``); the extents extend the rectangle
# south and east from there. Both extents default to 0 (legacy
# point-target behaviour) and must keep anchor + extent within the
# table's physical bounds — checked at the call site once the anchor
# is known, since this scalar validator can only enforce the bare
# ``≥ 0`` lower bound.
WASTE_BIN_EXTENT_MIN, WASTE_BIN_EXTENT_MAX = 0.0, max(TABLE_POS_MAX, CARRIAGE_POS_MAX)


def waste_bin_extent(text, *, allow_empty=False):
	"""Validate a waste-bin axis extent (cm): float in
	``[WASTE_BIN_EXTENT_MIN, WASTE_BIN_EXTENT_MAX]``. The full
	"anchor + extent inside table" rectangle check happens at the
	call site (``AutomatedFrame._validate_waste_bin_rect``), where
	both the anchor and the physical table dimensions are in scope.
	"""
	return _parse_float(
		text, "Waste bin extent", WASTE_BIN_EXTENT_MIN, WASTE_BIN_EXTENT_MAX,
		allow_empty=allow_empty,
	)


def _parse_identifier(text, label):
	text = (text or "").strip()
	if text == "":
		return False, f"{label} is required"
	if len(text) > IDENTIFIER_MAX:
		return False, f"{label} must be {IDENTIFIER_MAX} characters or fewer (got {len(text)})"
	if not IDENTIFIER_PATTERN.match(text):
		return False, f"{label} may only contain letters, digits, '.', '_', '-' (got '{text}')"
	return True, text


def project(text):
	"""Validate a Project name identifier (filesystem-safe, 1-64 chars)."""
	return _parse_identifier(text, "Project name")


def sample_id(text):
	"""Validate a Sample ID identifier (filesystem-safe, 1-64 chars)."""
	return _parse_identifier(text, "Sample ID")


def plate_id(text):
	"""Validate a Plate ID identifier (filesystem-safe, 1-64 chars).

	Used both as the live plate name during a run AND as the filename
	stem for the per-plate ``summary_{plate_id}.md`` written at run end,
	hence the same filesystem-safe character class.
	"""
	return _parse_identifier(text, "Plate ID")


def auto_increment_plate_id(current):
	"""Suggest the next Plate ID from a current one.

	If the string ends in an integer, increment it ("Plate-1" -> "Plate-2",
	"MyPlate_03" -> "MyPlate_04", preserving zero-padding). Otherwise
	append "_2" to the current string. Falls back to "Plate-1" for an
	empty input so the first-ever swap suggestion is sensible.
	"""
	cur = (current or "").strip()
	if not cur:
		return "Plate-1"
	# Trailing-integer match, with optional separator before it captured so
	# zero-padding can be preserved.
	import re as _re
	m = _re.search(r"^(.*?)(\d+)$", cur)
	if m:
		prefix, num = m.group(1), m.group(2)
		incremented = str(int(num) + 1)
		# Preserve zero-padding (e.g. "03" -> "04", not "4").
		if len(incremented) < len(num):
			incremented = incremented.zfill(len(num))
		return f"{prefix}{incremented}"
	return f"{cur}_2"


def number_of_fractions(text):
	"""Validate total number of fractions: int in ``[N_FRACTIONS_MIN, N_FRACTIONS_MAX]``.

	An additional "must fit on plate" cross-check happens at submit time in
	AutomatedFrame.begin_clicked once rows + cols are known.
	"""
	return _parse_int(text, "Number of fractions", N_FRACTIONS_MIN, N_FRACTIONS_MAX)


def discard_fractions(text):
	"""Validate discard count: int in ``[DISCARD_MIN, DISCARD_MAX]``.

	The "must be < number of fractions" cross-check happens at submit time.
	"""
	return _parse_int(text, "Discard fractions", DISCARD_MIN, DISCARD_MAX)


def carriage_pos(text, *, allow_empty=False):
	"""Validate absolute carriage position (cm): float in ``[CARRIAGE_POS_MIN, CARRIAGE_POS_MAX]``.

	``allow_empty=True`` treats a blank string as ``(True, None)`` so the
	Move button can skip axes the user didn't fill in.
	"""
	return _parse_float(
		text, "Carriage position", CARRIAGE_POS_MIN, CARRIAGE_POS_MAX,
		allow_empty=allow_empty,
	)
