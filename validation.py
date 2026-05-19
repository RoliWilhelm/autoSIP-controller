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

# Pump parameters
PUMP_RATE_MIN, PUMP_RATE_MAX = 0.1, 600.0  # cc/hr
VOLUME_MIN, VOLUME_MAX = 0.001, 5.0        # cc per well

# Drip wait between dispense and move-to-next, seconds. Lower bound of 0 lets
# the operator disable the wait entirely; upper bound is a sanity cap.
DRIP_WAIT_MIN, DRIP_WAIT_MAX = 0.0, 60.0

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


def _parse_float(text, label, lo, hi, *, allow_empty=False):
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
		return False, f"{label} must be between {lo} and {hi} (got {value})"
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
	"""Validate pump rate (cc/hr): float in ``[PUMP_RATE_MIN, PUMP_RATE_MAX]``."""
	return _parse_float(text, "Pump rate", PUMP_RATE_MIN, PUMP_RATE_MAX)


def volume(text):
	"""Validate per-well volume (cc): float in ``[VOLUME_MIN, VOLUME_MAX]``."""
	return _parse_float(text, "Volume per well", VOLUME_MIN, VOLUME_MAX)


def drip_wait_time(text):
	"""Validate the post-pump drip wait (s): float in ``[DRIP_WAIT_MIN, DRIP_WAIT_MAX]``."""
	return _parse_float(text, "Drip wait time", DRIP_WAIT_MIN, DRIP_WAIT_MAX)


def table_pos(text, *, allow_empty=False):
	"""Validate absolute table position (cm): float in ``[TABLE_POS_MIN, TABLE_POS_MAX]``.

	``allow_empty=True`` treats a blank string as ``(True, None)`` so the
	Move button can skip axes the user didn't fill in.
	"""
	return _parse_float(
		text, "Table position", TABLE_POS_MIN, TABLE_POS_MAX,
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
