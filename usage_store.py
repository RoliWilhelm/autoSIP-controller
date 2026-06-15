"""Persistent storage for stepper-motor usage counters.

Two files, both under ``~/.autosip/``:

  - ``usage.json``         — the live counters (per-axis microsteps +
    direction reversals) plus a ``last_reset_iso`` / ``last_reset_trigger``
    breadcrumb. Flushed on every reset, on application shutdown, and
    periodically (~60 s) during operation. Corrupt / missing file
    initialises to zero — the loader logs a warning rather than
    raising.
  - ``usage_history.csv``  — append-only CSV with one row per reset
    event. Columns:
        ``timestamp_iso, x_steps, y_steps, x_reversals, y_reversals,
        reset_trigger``
    ``reset_trigger`` ∈ ``{"manual", "origin_calibration"}``. This
    is the file the operator uses externally to correlate measured
    physical slippage against cumulative motor activity.

Counters are a property of the physical machine, not the operator's
configuration, so neither file is part of profile save/load.

Multi-instance caveat: if two App instances run simultaneously on the
same machine (unlikely on a dedicated Pi) the last writer wins on
``usage.json``. Documented; not defended.
"""

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("autosip")

_CONFIG_DIR = Path.home() / ".autosip"

_DEFAULT_USAGE = {
	"x_steps": 0,
	"y_steps": 0,
	"x_reversals": 0,
	"y_reversals": 0,
	"last_reset_iso": "",
	"last_reset_trigger": "",
}

_HISTORY_HEADER = [
	"timestamp_iso", "x_steps", "y_steps",
	"x_reversals", "y_reversals", "reset_trigger",
]

# Append-only CSV at ``~/.autosip/slippage_test.csv``. One row per
# completed validation phase; users build longitudinal data by re-running
# the routine on different days and letting rows accumulate. Columns:
#   phase             — "phase1" / "phase2" / "phase3"
#   timestamp_iso     — when the operator clicked Continue after measuring
#   x_steps, y_steps  — absolute (NOT delta) microstep counters at row time
#   x_reversals, y_reversals — same; absolute reversal counts
#   x_offset_mm, y_offset_mm — operator-measured offset from a physical
#                       reference mark, in millimetres (signed)
_SLIPPAGE_HEADER = [
	"phase", "timestamp_iso",
	"x_steps", "y_steps", "x_reversals", "y_reversals",
	"x_offset_mm", "y_offset_mm",
]


def get_usage_path():
	return _CONFIG_DIR / "usage.json"


def get_history_path():
	return _CONFIG_DIR / "usage_history.csv"


def get_slippage_path():
	return _CONFIG_DIR / "slippage_test.csv"


def load_usage():
	"""Return the usage dict from ``usage.json``, or a fresh zeroed
	dict if the file is missing or unparseable. On corruption the
	loader logs a warning and returns the default — never raises.
	"""
	path = get_usage_path()
	if not path.exists():
		return dict(_DEFAULT_USAGE)
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError) as exc:
		logger.warning(
			"Could not read %s (%s); reinitialising counters to zero",
			path, exc,
		)
		return dict(_DEFAULT_USAGE)
	if not isinstance(data, dict):
		logger.warning(
			"%s did not contain a JSON object; reinitialising counters",
			path,
		)
		return dict(_DEFAULT_USAGE)
	out = dict(_DEFAULT_USAGE)
	for k in _DEFAULT_USAGE:
		v = data.get(k, _DEFAULT_USAGE[k])
		# Coerce numeric keys to int defensively. A stray float / string
		# in the file shouldn't take down the App.
		if k in ("x_steps", "y_steps", "x_reversals", "y_reversals"):
			try:
				out[k] = int(v)
			except (TypeError, ValueError):
				out[k] = 0
		else:
			out[k] = v if isinstance(v, str) else ""
	return out


def save_usage(values):
	"""Write the usage dict to ``usage.json``. Creates the parent
	directory if it does not yet exist. Silently swallows write errors
	(returns False) so a transient disk problem doesn't crash the GUI;
	the App treats a failed save as "lose at most one period of data
	on the next crash" rather than a fatal error.
	"""
	if not isinstance(values, dict):
		return False
	path = get_usage_path()
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		payload = {k: values.get(k, _DEFAULT_USAGE[k]) for k in _DEFAULT_USAGE}
		with open(path, "w") as f:
			json.dump(payload, f, indent=2)
		return True
	except OSError as exc:
		logger.warning("Could not write %s: %s", path, exc)
		return False


def append_history_row(*, x_steps, y_steps, x_reversals, y_reversals,
		reset_trigger, timestamp_iso=None):
	"""Append one row to ``usage_history.csv``. Creates the file with
	a header line the first time it's written. ``timestamp_iso``
	defaults to the current wall-clock time in ISO-8601 with
	millisecond precision.

	Returns the timestamp written, so callers that want to surface it
	(e.g. update the in-memory ``last_reset_iso``) can use the same
	string the row carries.
	"""
	if timestamp_iso is None:
		timestamp_iso = datetime.now().isoformat(timespec="milliseconds")
	path = get_history_path()
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		new_file = not path.exists()
		with open(path, "a", newline="") as f:
			writer = csv.writer(f)
			if new_file:
				writer.writerow(_HISTORY_HEADER)
			writer.writerow([
				timestamp_iso,
				int(x_steps), int(y_steps),
				int(x_reversals), int(y_reversals),
				reset_trigger,
			])
	except OSError as exc:
		logger.warning("Could not append to %s: %s", path, exc)
	return timestamp_iso


def append_slippage_row(*, phase, x_steps, y_steps,
		x_reversals, y_reversals, x_offset_mm, y_offset_mm,
		timestamp_iso=None):
	"""Append one row to ``slippage_test.csv``. Creates the file
	with a header line on the first write. ``timestamp_iso`` defaults
	to the current wall-clock time in ISO-8601 with millisecond
	precision. Returns the timestamp written.

	The file is the raw data record for the post-build slippage
	characterisation workflow; the controller does not analyse it.
	"""
	if timestamp_iso is None:
		timestamp_iso = datetime.now().isoformat(timespec="milliseconds")
	path = get_slippage_path()
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		new_file = not path.exists()
		with open(path, "a", newline="") as f:
			writer = csv.writer(f)
			if new_file:
				writer.writerow(_SLIPPAGE_HEADER)
			writer.writerow([
				phase, timestamp_iso,
				int(x_steps), int(y_steps),
				int(x_reversals), int(y_reversals),
				float(x_offset_mm), float(y_offset_mm),
			])
	except OSError as exc:
		logger.warning("Could not append to %s: %s", path, exc)
	return timestamp_iso
