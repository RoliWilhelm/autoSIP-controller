"""Per-run on-disk logger for autoSIP fractionation runs.

A ``RunLogger`` instance owns one run's directory under
``<repo_root>/logs/{project}/{timestamp}_{sample_id_at_start}/`` and writes
four artifacts:

  - ``metadata.json``  -- written at run start
  - ``log.csv``        -- one row per well + pause/resume breadcrumbs
  - ``end.json``       -- run termination snapshot
  - ``summary.md``     -- human-readable lab-notebook summary at run end

The state machine in ``main.py`` calls into this class via thin hooks:

  ``start(metadata)`` -> creates the directory + writes metadata
  ``dispense_start(x, y)`` -> stamps the dispense-start time for this well
  ``dispense_end(x, y)``   -> stamps the dispense-end time
  ``well_completed(x, y)``        -> commits the well's row as completed
  ``well_emergency_stopped(x, y)``-> commits with status emergency_stopped
  ``resume_breadcrumb(x, y)``     -> appends a status="resume" row
  ``end(final_status, snapshot)`` -> writes end.json + summary.md

Methods are safe no-ops if ``start`` hasn't run (e.g. on disk failure the
state machine continues running but logs nothing).

Logs live IN the repo (not under a dotfile) so scientists can open a file
manager, navigate to the autoSIP folder, and see their runs at a glance.
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from time import monotonic

logger = logging.getLogger("autosip")

# Run logs live alongside the source so they're visible in a file manager
# without showing hidden files. Resolved at import time; tests can patch
# this module attribute to redirect to a tmpdir.
DEFAULT_LOGS_DIR = Path(__file__).resolve().parent / "logs"

# Backwards-compatible alias for older tests that reached in by the old name.
_DEFAULT_BASE_DIR = DEFAULT_LOGS_DIR


def _now_iso():
	"""Local-time ISO 8601 timestamp with millisecond precision.

	Three decimal places of seconds keeps the strings compact in CSV
	viewers while still distinguishing back-to-back pump cycles. NB:
	dispense_duration_s is measured separately via ``time.monotonic``;
	this timestamp is for human readability only.
	"""
	return datetime.now().isoformat(timespec="milliseconds")


def _iso_for_dirname(iso):
	"""Replace path-unfriendly characters in an ISO timestamp."""
	return iso.replace(":", "-").replace("/", "-")


def _well_id(x, y):
	return f"{chr(ord('A') + y)}{x + 1}"


def _fmt_coord(value):
	"""Format a coordinate (cm or column-index) as two decimal places
	for log.csv consistency. Empty strings pass through unchanged so
	rows without a coordinate value still write blank columns."""
	if value is None or value == "":
		return ""
	try:
		return f"{float(value):.2f}"
	except (TypeError, ValueError):
		return str(value)


def _well_sort_key(well_id):
	"""Sort key for well IDs in snake-path-friendly order: row letter then
	zero-padded column. Returns (-1, -1) for malformed IDs so they sink to
	the front for visibility."""
	if not well_id or len(well_id) < 2 or not well_id[0].isalpha():
		return (-1, -1)
	try:
		return (ord(well_id[0].upper()), int(well_id[1:]))
	except ValueError:
		return (-1, -1)


def _summarize_well_list(wells):
	"""Collapse a list of well IDs into a comma-joined range string.

	Sorted by (row, col); contiguous runs along EITHER axis become ranges.
	Example: ['A1','A2','A3','B1'] -> 'A1–A3, B1'
	"""
	if not wells:
		return ""
	# Preserve insertion order's logical groupings by sorting on row+col
	keyed = sorted(set(wells), key=_well_sort_key)
	chunks = []
	run = [keyed[0]]
	for w in keyed[1:]:
		prev = run[-1]
		# Same-row contiguous: same letter, col = prev_col + 1
		pr, pc = _well_sort_key(prev)
		cr, cc = _well_sort_key(w)
		if cr == pr and cc == pc + 1:
			run.append(w)
		else:
			chunks.append(run)
			run = [w]
	chunks.append(run)
	parts = []
	for chunk in chunks:
		if len(chunk) == 1:
			parts.append(chunk[0])
		else:
			parts.append(f"{chunk[0]}–{chunk[-1]}")
	return ", ".join(parts)


def _summarize_fraction_list(fracs):
	"""Collapse a sorted list of fraction indices into "3–14" / "3, 5, 6".

	Always sorts ascending; treats consecutive integers as a range.
	"""
	if not fracs:
		return ""
	keyed = sorted(set(int(f) for f in fracs))
	chunks = [[keyed[0]]]
	for f in keyed[1:]:
		if f == chunks[-1][-1] + 1:
			chunks[-1].append(f)
		else:
			chunks.append([f])
	parts = []
	for ch in chunks:
		parts.append(f"{ch[0]}–{ch[-1]}" if len(ch) > 1 else f"{ch[0]}")
	return ", ".join(parts)


def _is_contiguous_full_sample(fracs):
	"""True if ``fracs`` is a single unbroken ascending range. Used by the
	"Plates used" section to choose between "sample X" and "sample X
	(fractions 3–14)" -- if a plate holds the WHOLE sample, the fraction
	range is redundant. Approximation: any single contiguous range counts
	as "full" for display purposes; the per-plate summary is exact."""
	if not fracs:
		return False
	keyed = sorted(int(f) for f in fracs)
	return keyed == list(range(keyed[0], keyed[-1] + 1))


def _fmt_hms(seconds):
	seconds = max(0, int(round(seconds)))
	h, rem = divmod(seconds, 3600)
	m, s = divmod(rem, 60)
	return f"{h:02d}:{m:02d}:{s:02d}"


class RunLogger:
	"""One instance per fractionation run; do not reuse across runs."""

	# project + sample_id + plate_id come FIRST so a CSV reader sees the
	# provenance (project, biological sample, and physical plate) before
	# any well-level data. All three values are captured at the moment
	# each row is written (not at run start), via the
	# ``get_current_run_id`` callable.
	CSV_HEADER = [
		"project", "sample_id", "plate_id",
		"well_id", "plate_x", "plate_y",
		"dispense_start_iso", "dispense_end_iso",
		"dispense_duration_s", "status",
	]

	def __init__(self, base_dir=None, get_current_run_id=None):
		# Resolve DEFAULT_LOGS_DIR through the module name so tests that
		# patch ``run_logger.DEFAULT_LOGS_DIR`` after import are honored.
		if base_dir is not None:
			self.base_dir = Path(base_dir)
		else:
			import run_logger as _self
			self.base_dir = Path(_self.DEFAULT_LOGS_DIR)
		# Callable returning ``{"project", "sample_id", "plate_id"}`` for
		# the current state of the GUI. Called per CSV write so mid-run
		# edits (sample swap, plate swap) flow into subsequent rows.
		# Defaults to empty strings for tests that don't care about
		# provenance.
		self._get_current_run_id = get_current_run_id or (
			lambda: {"project": "", "sample_id": "", "plate_id": ""}
		)
		self.run_dir = None
		self._metadata = None
		self._csv_file = None
		self._csv_writer = None
		# Per-well in-flight state: (x, y) -> {well_id, plate_x, plate_y,
		# dispense_start_iso, dispense_end_iso}
		self._wells = {}
		# (x, y) that have already been written to CSV -- guards against
		# double-commit on terminate during the move() that follows.
		self._committed = set()
		# Per-status counters for end.json's wells_completed and friends.
		# Resume breadcrumbs aren't counted here -- they're transition
		# markers, not dispense outcomes.
		self._status_counts = {}

	# -- Lifecycle ------------------------------------------------------

	def start(self, metadata):
		"""Create the run directory and write ``metadata.json``.

		Layout: ``base_dir/{project}/{timestamp}_{sample_id_at_start}/``
		The Project subdirectory is reused across runs of the same project;
		the inner timestamp+sample_id dir is unique per run.

		Returns the run directory ``Path``. Raises ``OSError`` if creation
		fails -- caller should catch and continue running without logging.
		"""
		timestamp = metadata.get("timestamp_start") or _now_iso()
		project = (metadata.get("project") or "default").strip() or "default"
		sample_id = (metadata.get("sample_id_at_start") or "unknown").strip() or "unknown"
		leaf = f"{_iso_for_dirname(timestamp)}_{sample_id}"
		self.run_dir = self.base_dir / project / leaf
		self.run_dir.mkdir(parents=True, exist_ok=True)
		self._metadata = dict(metadata)
		with open(self.run_dir / "metadata.json", "w") as f:
			json.dump(self._metadata, f, indent=2, default=str)
		return self.run_dir

	def end(self, final_status, snapshot=None, plates_used=None, well_records=None,
			file_suffix=None, waste_context=None, bulk_context=None):
		"""Write ``end.json``, ``summary.md``, plus one ``summary_{id}.md``
		per plate used, then close the CSV.

		``final_status`` is one of {completed, emergency_stopped, manual_abort}.
		``plates_used`` is a chronological list of Plate IDs the run touched
		(may be ``None`` for legacy callers; defaults to a single-plate run).
		``well_records`` is the App's per-well metadata list; if provided,
		it's used to filter the per-plate summary files. (CSV is the
		canonical source; well_records is just a convenience pass-through.)
		``file_suffix`` is appended to the ``end`` / ``summary`` /
		``summary_{plate_id}`` filenames so multiple End Runs across a session
		never overwrite each other. A typical caller passes the End-Run
		click's ISO timestamp with colons replaced by hyphens; legacy
		callers can omit it for the original unsuffixed names.
		``waste_context`` (optional) is a dict carrying waste-bin
		bookkeeping that end.json + the Estimated-waste summary section
		need:
		  * waste_volume_ml_at_run_start
		  * waste_volume_ml_at_run_end
		  * max_waste_volume_ml
		  * waste_warnings_fired (int)
		  * waste_shutoffs_fired (int)
		  * waste_resets_during_run (int)
		"""
		if self.run_dir is None:
			return
		timestamp_end = _now_iso()
		rid = self._get_current_run_id()
		suffix = f"_{file_suffix}" if file_suffix else ""
		plates_used = list(plates_used or [rid.get("plate_id", "")])

		# Derived counters for end.json
		params = (self._metadata or {}).get("parameters", {}) if self._metadata else {}
		rows = int(params.get("rows", 0) or 0)
		cols = int(params.get("cols", 0) or 0)
		wells_planned = rows * cols
		wells_completed = self._status_counts.get("completed", 0)

		ts_start = (self._metadata or {}).get("timestamp_start") if self._metadata else None
		actual_total_time_s = 0.0
		if ts_start:
			try:
				actual_total_time_s = (
					datetime.fromisoformat(timestamp_end)
					- datetime.fromisoformat(ts_start)
				).total_seconds()
			except (ValueError, TypeError):
				actual_total_time_s = 0.0

		end_payload = {
			"timestamp_end": timestamp_end,
			"final_status": final_status,
			"project_at_end": rid.get("project", ""),
			"final_sample_id": rid.get("sample_id", ""),
			"wells_completed": wells_completed,
			"wells_planned": wells_planned,
			"actual_total_time_s": round(actual_total_time_s, 3),
			"plates_used": plates_used,
		}
		if waste_context:
			# Compute waste added during this run (accounting for any
			# mid-run resets that subtracted from the running tally).
			start_v = float(waste_context.get("waste_volume_ml_at_run_start") or 0.0)
			end_v = float(waste_context.get("waste_volume_ml_at_run_end") or 0.0)
			resets = int(waste_context.get("waste_resets_during_run") or 0)
			# Approximation: when a Reset fires mid-run, we conservatively
			# treat the value-at-reset as "added during this run since the
			# previous bookmark"; we don't track per-reset deltas precisely.
			# The simple formula end - start understates if there were
			# resets; the explicit reset count is shown alongside.
			added = end_v - start_v
			end_payload.update({
				"waste_volume_ml_at_run_start": start_v,
				"waste_volume_ml_at_run_end": end_v,
				"waste_added_this_run_ml": round(added, 3),
				"waste_warnings_fired": int(waste_context.get("waste_warnings_fired") or 0),
				"waste_shutoffs_fired": int(waste_context.get("waste_shutoffs_fired") or 0),
				"waste_resets_during_run": resets,
			})
		try:
			with open(self.run_dir / f"end{suffix}.json", "w") as f:
				json.dump(end_payload, f, indent=2)
		except OSError as exc:
			logger.warning("Failed to write end.json: %s", exc)

		# Close CSV before reading it back for the provenance section.
		if self._csv_file is not None:
			try:
				self._csv_file.close()
			except OSError:
				pass
			self._csv_file = None
			self._csv_writer = None

		if self._metadata is not None:
			try:
				self._write_summary(timestamp_end, final_status, snapshot,
					wells_completed, wells_planned, actual_total_time_s,
					plates_used, suffix, waste_context, bulk_context)
			except (OSError, Exception) as exc:
				logger.warning("Failed to write summary.md: %s", exc)
			# Per-plate summaries: filtered slices of the run summary for
			# printing/attaching to the physical plate.
			for plate_id in plates_used:
				if not plate_id:
					continue
				try:
					self._write_plate_summary(timestamp_end, final_status, plate_id, suffix)
				except (OSError, Exception) as exc:
					logger.warning("Failed to write per-plate summary for %s: %s",
						plate_id, exc)

	# -- Per-well timestamps -------------------------------------------
	#
	# All in-flight tracking is keyed by well_id (string) so plate wells
	# ("A1", "B4", ...) AND discard cycles ("discard_1", "discard_2", ...)
	# share the same code path. For plate wells the public methods take
	# (x, y) and compute the well_id internally; for discard cycles the
	# discard_* methods take an index and waste-bin coords.

	def _track(self, well_id, plate_x, plate_y):
		"""Snapshot a dispense-start time. Captures BOTH a wall-clock ISO
		string (for human reading in the log) AND a monotonic timestamp
		(for measuring duration without being affected by wall-clock
		adjustments mid-run)."""
		if self.run_dir is None:
			return
		self._wells[well_id] = {
			"well_id": well_id,
			"plate_x": plate_x,
			"plate_y": plate_y,
			"dispense_start_iso": _now_iso(),
			"dispense_end_iso": None,
			"_mono_start": monotonic(),
			"_mono_end": None,
		}

	def _mark_end(self, well_id):
		if self.run_dir is None:
			return
		well = self._wells.get(well_id)
		if well is not None:
			well["dispense_end_iso"] = _now_iso()
			well["_mono_end"] = monotonic()

	def _commit(self, well_id, status):
		if self.run_dir is None or well_id in self._committed:
			return
		well = self._wells.get(well_id)
		if well is None:
			return
		# If we never reached stop_pump (terminate mid-dispense), stamp end
		# now so the duration still reflects how long the relay was on.
		if well["dispense_end_iso"] is None:
			well["dispense_end_iso"] = _now_iso()
		if well["_mono_end"] is None:
			well["_mono_end"] = monotonic()
		# Duration comes from the monotonic clock, NOT from differencing the
		# ISO strings. The ISO strings are wall-clock (now ms-precision) for
		# human reading; monotonic is immune to mid-run clock adjustments
		# and gives true elapsed seconds at full resolution.
		mono_start = well["_mono_start"]
		mono_end = well["_mono_end"]
		duration = ""
		if mono_start is not None and mono_end is not None:
			duration = f"{round(mono_end - mono_start, 3):.3f}"
		rid = self._get_current_run_id()
		self._write_row([
			rid.get("project", ""), rid.get("sample_id", ""), rid.get("plate_id", ""),
			well["well_id"], _fmt_coord(well["plate_x"]), _fmt_coord(well["plate_y"]),
			well["dispense_start_iso"], well["dispense_end_iso"],
			duration, status,
		])
		self._committed.add(well_id)
		self._status_counts[status] = self._status_counts.get(status, 0) + 1

	# -- Plate-well API (collection phase) ------------------------------

	def dispense_start(self, x, y):
		"""Record the dispense-start time for the plate well at (x, y)."""
		self._track(_well_id(x, y), x, y)

	def dispense_end(self, x, y):
		"""Record the dispense-end time for the plate well at (x, y)."""
		self._mark_end(_well_id(x, y))

	def well_completed(self, x, y):
		"""Commit the plate well at (x, y) as successfully completed."""
		self._commit(_well_id(x, y), "completed")

	def well_emergency_stopped(self, x, y):
		"""Commit the plate well at (x, y) as halted mid-run by terminate."""
		self._commit(_well_id(x, y), "emergency_stopped")

	# -- Discard-cycle API (discard phase) -----------------------------

	@staticmethod
	def _discard_id(series_index, cycle_index):
		"""``discard_<series>_<cycle>`` -- series-aware so discards from
		different multi-tube series don't collide on the same well_id."""
		return f"discard_{series_index}_{cycle_index}"

	def discard_dispense_start(self, series_index, cycle_index,
			waste_x_cm, waste_y_cm):
		"""Record dispense-start for the cycle ``cycle_index`` of series
		``series_index`` (both 1-indexed).

		``waste_x_cm`` / ``waste_y_cm`` are written into the row's plate_x /
		plate_y columns so a CSV reader can see WHERE each discard went,
		even though discards share a single physical position per series.
		"""
		self._track(self._discard_id(series_index, cycle_index),
			waste_x_cm, waste_y_cm)

	def discard_dispense_end(self, series_index, cycle_index):
		self._mark_end(self._discard_id(series_index, cycle_index))

	def discard_committed(self, series_index, cycle_index):
		self._commit(self._discard_id(series_index, cycle_index), "discarded")

	def discard_emergency_stopped(self, series_index, cycle_index):
		self._commit(self._discard_id(series_index, cycle_index), "emergency_stopped")

	def resume_breadcrumb(self, next_x, next_y):
		"""Append a status="resume" row marking a pause-resume transition.

		Emitted from ``App.toggle_pause`` before the after() loop is rearmed,
		with ``next_x``/``next_y`` set to the well that will be dispensed
		next. Captures the CURRENT Project + Sample ID so a tube swap during
		the pause shows up unmistakably: prior wells under the old Sample ID,
		one ``resume`` row at the changeover with the new Sample ID, then
		subsequent ``completed`` rows under the new Sample ID.
		"""
		if self.run_dir is None:
			return
		now = _now_iso()
		rid = self._get_current_run_id()
		self._write_row([
			rid.get("project", ""), rid.get("sample_id", ""), rid.get("plate_id", ""),
			_well_id(next_x, next_y), _fmt_coord(next_x), _fmt_coord(next_y),
			now, now, "0.000", "resume",
		])

	def close_without_summary(self):
		"""Close the CSV file without writing end.json / summary.md.

		Used when the operator chooses Discard on End Run -- the
		metadata.json and log.csv that were written during the run remain
		on disk for forensic review, but no finalization files are produced.
		Idempotent: safe to call after the file is already closed.
		"""
		if self._csv_file is not None:
			try:
				self._csv_file.close()
			except OSError:
				pass
			self._csv_file = None
			self._csv_writer = None

	def reset_for_new_plate(self):
		"""Clear per-well dedup state so well_ids can be reused on a new
		plate. The same SBS well_id ("A1") refers to different physical
		wells on different plates, so the run-level _committed guard --
		which prevents double-commit within a plate -- must reset on swap.
		Status counts and the CSV file stay; only the per-well in-flight
		map and the committed set are wiped."""
		self._wells = {}
		self._committed = set()

	def waste_event(self, kind, count):
		"""Append a waste-bin event row to log.csv.

		``kind`` is one of:
		  ``"waste_autopause"`` — 80% threshold reached; pump auto-paused.
		  ``"waste_hardstop"``  — 100% reached; failsafe hard stop.
		  ``"waste_reset"``     — operator emptied the bin + clicked Reset.
		  ``"waste_warning"`` / ``"waste_shutoff"`` — legacy aliases kept
		    so older code paths still write a row; treated identically
		    to ``waste_autopause`` / ``waste_hardstop`` in the suffix.

		``count`` is a 1-based counter (incremented in App-level state)
		so log.csv readers can distinguish individual events; it shows
		up in the row's well_id as e.g. ``autopause_1``, ``hardstop_1``,
		``reset_3``.
		"""
		if self.run_dir is None:
			return
		suffix_map = {
			"waste_autopause": "autopause",
			"waste_hardstop": "hardstop",
			"waste_reset": "reset",
			"waste_warning": "warning",
			"waste_shutoff": "shutoff",
		}
		if kind not in suffix_map:
			logger.warning("Unknown waste-event kind %r", kind)
			return
		suffix = suffix_map[kind]
		now = _now_iso()
		rid = self._get_current_run_id()
		self._write_row([
			rid.get("project", ""), rid.get("sample_id", ""), rid.get("plate_id", ""),
			f"{suffix}_{count}", _fmt_coord(0), _fmt_coord(0),
			now, now, "0.000", kind,
		])
		self._status_counts[kind] = self._status_counts.get(kind, 0) + 1

	_VALID_PURGE_PHASES = ("wash", "clear", "bleach", "prime")

	def purge_committed(self, phase, series_index,
			waste_x_cm, waste_y_cm, start_iso, end_iso, duration_s,
			cycle=1, sub_phase=""):
		"""Append a ``status="purge_{phase}"`` row marking one operator-
		toggled pump cycle within an inter-sample purge phase.

		``phase`` is one of ``"wash"``, ``"clear"``, ``"bleach"``,
		``"prime"``. ``series_index`` is the 1-based index of the NEW
		sample series. ``cycle`` is the 1-based cycle number within
		this phase (each Space-toggle press-on → press-off pair writes
		its own row, so a phase the operator toggled three times
		produces ``cycle=1, 2, 3``). ``sub_phase`` (default empty) is
		an optional suffix like ``"rinse"`` for the post-bleach water
		flush in the decontamination protocol, inserted between the
		series index and the cycle suffix.

		Resulting ``well_id``:
		  - ``purge_{phase}_{series}``                  (cycle 1, no sub)
		  - ``purge_{phase}_{series}_cycle{N}``         (cycle ≥ 2)
		  - ``purge_{phase}_{series}_{sub}``            (cycle 1, sub)
		  - ``purge_{phase}_{series}_{sub}_cycle{N}``   (cycle ≥ 2, sub)
		"""
		if self.run_dir is None:
			return
		if phase not in self._VALID_PURGE_PHASES:
			logger.warning("Unknown purge phase %r; skipping log row", phase)
			return
		status = f"purge_{phase}"
		well_id = f"{status}_{series_index}"
		if sub_phase:
			well_id += f"_{sub_phase}"
		if cycle > 1:
			well_id += f"_cycle{cycle}"
		rid = self._get_current_run_id()
		self._write_row([
			rid.get("project", ""), rid.get("sample_id", ""), rid.get("plate_id", ""),
			well_id, _fmt_coord(waste_x_cm), _fmt_coord(waste_y_cm),
			start_iso, end_iso, f"{duration_s:.3f}", status,
		])
		self._status_counts[status] = self._status_counts.get(status, 0) + 1

	# (purge_emergency_stopped was used by the old timed-countdown design
	# to log partial cycles when Escape→Terminate fired mid-pump. The new
	# operator-toggle design always commits a finished cycle on pump-off,
	# so partial-cycle e-stop logging is no longer needed. The Terminate
	# Run UI was removed in a later pass; this method is kept as dead
	# code in case a future caller wants a status="emergency_stopped"
	# purge row.)

	def checklist_skipped(self, context_id):
		"""Append a status="checklist_skipped" row marking a pre-flight
		checklist that the operator bypassed via the Skip (Expert)
		button. ``context_id`` is appended to the well_id (e.g.
		``"plate_swap_2"``, ``"purge_phase_1"``) so the row pinpoints
		which checklist was skipped. Useful for audit -- a run with
		many skipped checklists may indicate UI friction or expert
		usage; either way the log carries the trail.
		"""
		if self.run_dir is None:
			return
		now = _now_iso()
		rid = self._get_current_run_id()
		self._write_row([
			rid.get("project", ""), rid.get("sample_id", ""), rid.get("plate_id", ""),
			f"checklist_skipped_{context_id}", _fmt_coord(0), _fmt_coord(0),
			now, now, "0.000", "checklist_skipped",
		])

	def plate_swap_breadcrumb(self, swap_index):
		"""Append a status="plate_swap" row marking a physical plate change.

		Called by App.continue_to_next_plate AFTER the new Plate ID has
		been committed to state.current_plate_id, so the ``get_current_run_id``
		callback returns the NEW plate. ``swap_index`` is the 1-based count
		of swaps in this run, used to disambiguate row ``well_id`` values
		when reading the CSV (``plate_swap_1``, ``plate_swap_2``, ...).
		"""
		if self.run_dir is None:
			return
		now = _now_iso()
		rid = self._get_current_run_id()
		self._write_row([
			rid.get("project", ""), rid.get("sample_id", ""), rid.get("plate_id", ""),
			f"plate_swap_{swap_index}", _fmt_coord(0), _fmt_coord(0),
			now, now, "0.000", "plate_swap",
		])

	# -- CSV I/O --------------------------------------------------------

	def _write_row(self, row):
		try:
			if self._csv_writer is None:
				csv_path = self.run_dir / "log.csv"
				need_header = not csv_path.exists() or csv_path.stat().st_size == 0
				self._csv_file = open(csv_path, "a", newline="")
				self._csv_writer = csv.writer(self._csv_file)
				if need_header:
					self._csv_writer.writerow(self.CSV_HEADER)
			self._csv_writer.writerow(row)
			self._csv_file.flush()
		except OSError as exc:
			logger.warning("Failed to write log.csv row: %s", exc)

	# -- Markdown summary ----------------------------------------------

	def _write_summary(self, timestamp_end, final_status, snapshot,
			wells_completed, wells_planned, actual_total_time_s,
			plates_used=None, suffix="", waste_context=None, bulk_context=None):
		m = self._metadata
		params = m.get("parameters", {})
		rows = int(params.get("rows", 0) or 0)
		cols = int(params.get("cols", 0) or 0)

		ts_start = m.get("timestamp_start", "?")
		project = m.get("project", m.get("project_at_start", "?"))
		rid = self._get_current_run_id()
		final_sample = rid.get("sample_id") or m.get("sample_id_at_start", "?")

		labware = m.get("labware_file") or "(none -- manual entry)"
		out = [
			f"# Run summary — {project} ({ts_start})",
			"",
			f"_software version {m.get('software_version', '?')}_",
			"",
			f"- **Final Sample ID:** {final_sample}",
			f"- **Final status:** {final_status}",
			f"- Started: {ts_start}",
			f"- Ended: {timestamp_end}",
			f"- Total runtime: {_fmt_hms(actual_total_time_s)}",
			"",
			"## Parameters",
			f"- Plate: {rows} × {cols} ({wells_planned} wells)",
			f"- Labware file: `{labware}`",
			f"- Well size: {params.get('well_size_cm', '?')} cm",
			f"- Pump rate: {params.get('pump_rate', '?')} "
			f"{params.get('pump_rate_units', '')}".rstrip(),
			f"- Volume per well: {params.get('volume_per_well_ml', '?')} mL",
			f"- Starting position: "
			f"table = {params.get('table_start_cm', 0):.3f} cm, "
			f"carriage = {params.get('carriage_start_cm', 0):.3f} cm",
			f"- Estimated runtime: {_fmt_hms(m.get('estimated_total_time_s') or 0)}",
			"",
			"## Result",
			f"- Completed wells: {wells_completed} / {wells_planned}",
			"",
		]

		# Discarded fractions: a small section showing how many fractions
		# went to the waste bin and where. Omitted when D == 0.
		discards = int(params.get("discard_fractions", 0) or 0)
		if discards > 0:
			wx = params.get("waste_bin_table_cm", 0)
			wy = params.get("waste_bin_carriage_cm", 0)
			out.extend([
				"## Discarded fractions",
				f"- {discards} fractions discarded to waste at "
				f"({wx} cm, {wy} cm)",
				"",
			])

		# Plates used: chronological list of physical plates the run
		# touched, with the sample ranges each contributed to. Derived
		# from log.csv so it reflects what actually happened.
		if plates_used:
			plate_breakdown = self._compute_plate_breakdown()
			out.append("## Plates used")
			for plate_id in plates_used:
				if not plate_id:
					continue
				samples_on_plate = plate_breakdown.get(plate_id, [])
				if samples_on_plate:
					parts = [
						(f"sample {sid} (fractions {_summarize_fraction_list(fracs)})"
						 if not _is_contiguous_full_sample(fracs)
						 else f"sample {sid}")
						for sid, fracs in samples_on_plate
					]
					out.append(f"- {plate_id}: {', '.join(parts)}")
				else:
					out.append(f"- {plate_id}: (no plate wells)")
			out.append("")

		# Estimated waste: bin-tracking bookkeeping. The volume figures
		# are estimates (pump_rate × pump_on_time) and may diverge from
		# physical reality if the configured rate doesn't match the
		# actual pump. Reset events shown so the operator can reconcile.
		if waste_context:
			start_v = float(waste_context.get("waste_volume_ml_at_run_start") or 0.0)
			end_v = float(waste_context.get("waste_volume_ml_at_run_end") or 0.0)
			max_v = float(waste_context.get("max_waste_volume_ml") or 0.0)
			added = end_v - start_v
			# Per-status volume sums for the breakdown.
			discard_ml, discard_n, purge_ml, purge_n = (
				self._compute_waste_breakdown(
					params.get("volume_per_well_ml"),
					params.get("peristaltic_rate_ml_per_min"),
				)
			)
			out.append("## Estimated waste")
			out.append(f"- At run start: {start_v:.1f} mL")
			out.append(f"- Added during run: {added:.1f} mL")
			out.append(f"  - Discards: {discard_ml:.1f} mL across {discard_n} cycles")
			out.append(f"  - Purges: {purge_ml:.1f} mL across {purge_n} phases")
			cap_str = f" (of {max_v:.0f} mL bin capacity)" if max_v else ""
			out.append(f"- At run end: {end_v:.1f} mL{cap_str}")
			out.append(f"- Warnings fired: {int(waste_context.get('waste_warnings_fired') or 0)}")
			out.append(f"- Auto-shutoffs: {int(waste_context.get('waste_shutoffs_fired') or 0)}")
			out.append(f"- Resets during run: {int(waste_context.get('waste_resets_during_run') or 0)}")
			out.append("")

		# Bulk submission: spreadsheet source + sample sequence with
		# edit markers. Only present when the operator imported a
		# Bulk Sample Submission CSV for this run.
		if bulk_context:
			out.append("## Bulk submission")
			out.append(f"- Source: {bulk_context.get('source_path', '')}")
			out.append(
				f"- Total samples in spreadsheet: "
				f"{bulk_context.get('total_samples', 0)}"
			)
			seq = bulk_context.get("sample_sequence") or []
			if seq:
				out.append(f"- Sample sequence (as run): {', '.join(seq)}")
				out.append("  (\"b\" suffix marks samples whose Sample ID was edited)")
			out.append("")

		# Inter-sample purges: list each transition with its measured
		# wash + clear durations. Omitted entirely when no purge rows
		# were written (Skip checked, or single-sample run).
		purges = self._compute_intersample_purges()
		if purges:
			out.append("## Inter-sample purges")
			phase_order = ("wash", "bleach", "rinse", "clear", "prime")
			for p in purges:
				parts = []
				for phase in phase_order:
					seconds, cycles = p[phase]
					if cycles == 0:
						continue
					cycle_note = (
						f" ({cycles} cycle{'s' if cycles != 1 else ''})"
						if cycles > 1 else ""
					)
					parts.append(f"{seconds:.1f} s {phase}{cycle_note}")
				if not parts:
					continue
				out.append(
					f"- Between {p['prev']} → {p['next']}: " + " + ".join(parts)
				)
			out.append("")

		# Sample provenance section: derived entirely from log.csv so it
		# reflects per-well sample_id values as recorded, not the start/end
		# snapshot. The color-name is derived from each sample's first-
		# appearance order to match WellPlateProgress's per-series palette.
		provenance = self._compute_provenance()
		if provenance:
			from well_plate import color_for_series
			out.append("## Sample provenance")
			for series_idx, (sid, wells) in enumerate(provenance, start=1):
				_, color_name = color_for_series(series_idx)
				summary_wells = _summarize_well_list(wells)
				out.append(
					f"- {sid} ({color_name}) → {summary_wells} "
					f"({len(wells)} wells)"
				)
			out.append("")

		if snapshot and snapshot.get("rows") and snapshot.get("cols"):
			# Import inline so well_plate isn't a hard dependency for users
			# of run_logger that don't have Tk available (e.g. tests).
			from well_plate import format_snapshot_log
			out.extend([
				"## Plate",
				"```",
				format_snapshot_log(snapshot),
				"```",
				"",
			])

		with open(self.run_dir / f"summary{suffix}.md", "w") as f:
			f.write("\n".join(out))

	def _compute_plate_breakdown(self):
		"""Walk log.csv and return ``{plate_id: [(sample_id, [fraction_idx, ...]), ...]}``
		preserving first-appearance order WITHIN each plate.

		Used by the "Plates used" section of summary.md to describe which
		samples (or fraction ranges of which samples) landed on each plate.
		Fraction index is reconstructed by counting completed rows per
		(plate, sample) sequentially -- close enough for human reading,
		and never used as the source of truth (log.csv itself is).
		"""
		csv_path = self.run_dir / "log.csv"
		if not csv_path.exists():
			return {}
		breakdown = {}    # plate_id -> [(sample_id, [int])]
		# Track per-(plate, sample) running fraction counter so we can build
		# a "fractions 3–14" type range. Discards are tube-level so they
		# DON'T count toward the plate's fraction list; only completed
		# rows on the plate do.
		sample_counter = {}  # sample_id -> next fraction index
		try:
			# Per-sample D snapshot: each sample's first completed row's
			# fraction index tells us where to start counting. We compute
			# inline by tracking the FIRST observed index per sample-by-plate
			# group via reading metadata's discard_fractions, BUT the log
			# doesn't directly record fraction_index_in_sample. For the
			# summary we just report the running offset within each
			# (plate, sample) group, which is what the user sees on the
			# plate display.
			with open(csv_path, newline="") as f:
				reader = csv.DictReader(f)
				for row in reader:
					if row.get("status") != "completed":
						continue
					plate_id = row.get("plate_id") or "(unknown)"
					sid = row.get("sample_id") or "(unspecified)"
					if sid not in sample_counter:
						# Bump to discard_fractions + 1 if metadata has it
						d = int((self._metadata or {}).get(
							"parameters", {}).get("discard_fractions", 0) or 0)
						sample_counter[sid] = d + 1
					fraction = sample_counter[sid]
					sample_counter[sid] += 1
					plate = breakdown.setdefault(plate_id, [])
					if plate and plate[-1][0] == sid:
						plate[-1][1].append(fraction)
					else:
						plate.append((sid, [fraction]))
		except OSError as exc:
			logger.warning("Failed to read log.csv for plate breakdown: %s", exc)
			return {}
		return breakdown

	def _write_plate_summary(self, timestamp_end, final_status, plate_id, suffix=""):
		"""Write ``summary_{plate_id}.md`` -- a printer-friendly per-plate
		filter of the run summary, intended to be attached to the physical
		plate as it goes to downstream processing."""
		m = self._metadata or {}
		params = m.get("parameters", {})
		ts_start = m.get("timestamp_start", "?")
		project = m.get("project", "?")
		labware = m.get("labware_file") or "(none -- manual entry)"

		# Build per-sample fraction lists from log.csv for THIS plate only.
		csv_path = self.run_dir / "log.csv"
		samples_on_plate = []     # list of (sample_id, [well_id], [fraction_idx])
		discards_on_plate = 0
		if csv_path.exists():
			try:
				sample_counter = {}
				with open(csv_path, newline="") as f:
					reader = csv.DictReader(f)
					for row in reader:
						row_plate = row.get("plate_id") or ""
						if row_plate != plate_id:
							continue
						status = row.get("status", "")
						sid = row.get("sample_id") or "(unspecified)"
						if status == "completed":
							if sid not in sample_counter:
								d = int(params.get("discard_fractions", 0) or 0)
								sample_counter[sid] = d + 1
							fraction = sample_counter[sid]
							sample_counter[sid] += 1
							if samples_on_plate and samples_on_plate[-1][0] == sid:
								samples_on_plate[-1][1].append(row.get("well_id", ""))
								samples_on_plate[-1][2].append(fraction)
							else:
								samples_on_plate.append((sid, [row.get("well_id", "")], [fraction]))
						elif status == "discarded":
							# discards aren't on the plate, but if the
							# operator routed waste to the plate (weird
							# setup) we still surface the count.
							discards_on_plate += 1
			except OSError as exc:
				logger.warning("Failed to read log.csv for %s summary: %s", plate_id, exc)

		out = [
			f"# Plate summary — {plate_id}",
			"",
			f"_Run: {project} ({ts_start}) — software v{m.get('software_version', '?')}_",
			"",
			f"- **Plate ID:** {plate_id}",
			f"- **Final status:** {final_status}",
			f"- Run started: {ts_start}",
			f"- Run ended: {timestamp_end}",
			"",
			"## Parameters",
			f"- Plate: {params.get('rows', '?')} × {params.get('cols', '?')}",
			f"- Labware file: `{labware}`",
			f"- Well size: {params.get('well_size_cm', '?')} cm",
			f"- Pump rate: {params.get('pump_rate', '?')} "
			f"{params.get('pump_rate_units', '')}".rstrip(),
			f"- Volume per well: {params.get('volume_per_well_ml', '?')} mL",
			"",
		]
		if samples_on_plate:
			out.append("## Samples on this plate")
			from well_plate import color_for_series
			# Compute series index for color name -- use sample's FIRST
			# appearance across the whole run, not just this plate. We
			# don't have that here easily; just use the index within
			# samples_on_plate. Close enough for the per-plate sheet.
			for series_idx, (sid, wells, fracs) in enumerate(samples_on_plate, start=1):
				_, cname = color_for_series(series_idx)
				summary_wells = _summarize_well_list(wells)
				fr_range = _summarize_fraction_list(fracs)
				out.append(
					f"- {sid} ({cname}) → wells {summary_wells} "
					f"(fractions {fr_range}, {len(wells)} wells)"
				)
			out.append("")
		else:
			out.extend(["## Samples on this plate", "(no wells)", ""])

		with open(self.run_dir / f"summary_{plate_id}{suffix}.md", "w") as f:
			f.write("\n".join(out))

	def _compute_waste_breakdown(self, volume_per_well_ml, peristaltic_rate_ml_per_min):
		"""Sum waste contributions per category from log.csv.

		Returns ``(discard_ml, discard_count, purge_ml, purge_count)``.
		Discard volume per row equals ``volume_per_well_ml`` (each
		discard cycle dispenses one configured volume to waste). Purge
		volume per row equals ``dispense_duration_s × rate / 60``.
		"""
		csv_path = self.run_dir / "log.csv"
		if not csv_path.exists():
			return 0.0, 0, 0.0, 0
		try:
			vol_per_well = float(volume_per_well_ml or 0.0)
		except (TypeError, ValueError):
			vol_per_well = 0.0
		try:
			peri_rate = float(peristaltic_rate_ml_per_min or 0.0)
		except (TypeError, ValueError):
			peri_rate = 0.0
		discard_ml = 0.0
		discard_n = 0
		purge_ml = 0.0
		purge_n = 0
		try:
			with open(csv_path) as f:
				for row in csv.DictReader(f):
					st = row.get("status", "")
					if st == "discarded":
						discard_ml += vol_per_well
						discard_n += 1
					elif st in ("purge_wash", "purge_clear"):
						try:
							dur = float(row.get("dispense_duration_s") or 0.0)
						except (TypeError, ValueError):
							dur = 0.0
						purge_ml += dur * (peri_rate / 60.0)
						purge_n += 1
		except OSError as exc:
			logger.warning("Failed to read log.csv for waste breakdown: %s", exc)
			return 0.0, 0, 0.0, 0
		return discard_ml, discard_n, purge_ml, purge_n

	def _compute_intersample_purges(self):
		"""Walk log.csv and return a list of per-transition dicts, one
		per inter-sample transition that wrote purge rows.

		Each dict carries::

		    {
		      "prev": str,                # previous sample_id
		      "next": str,                # next sample_id
		      "wash": (seconds, cycles),  # pre-bleach water flush
		      "rinse": (seconds, cycles), # post-bleach water flush (decon)
		      "bleach": (seconds, cycles),# bleach phase (decon)
		      "clear": (seconds, cycles), # air clear
		      "prime": (seconds, cycles), # syringe priming
		    }

		Missing phases come back as ``(0.0, 0)``. The purge rows
		themselves carry the NEW sample's sample_id (``get_current_run_id``
		is read at write time, after the Sample ID entry has been updated
		for the new sample). To reconstruct the previous sample we walk
		the CSV in order and track the last seen ``completed`` row's
		sample_id.

		``well_id`` parsing:
		  purge_{phase}_{N}                  → phase, series N, cycle 1
		  purge_{phase}_{N}_cycle{M}         → phase, series N, cycle M
		  purge_wash_{N}_rinse(_cycle{M})    → rinse (decontamination),
		                                       cycle M (default 1)
		"""
		csv_path = self.run_dir / "log.csv"
		if not csv_path.exists():
			return []
		# Two patterns: the rinse sub-phase (post-bleach water flush) and
		# the regular per-phase row.
		rinse_re = re.compile(r"^purge_wash_(\d+)_rinse(?:_cycle(\d+))?$")
		phase_re = re.compile(
			r"^purge_(wash|clear|bleach|prime)_(\d+)(?:_cycle(\d+))?$"
		)
		last_completed_sid = ""
		pending = {}

		def _ensure(series_idx, next_sid):
			e = pending.setdefault(series_idx, {
				"prev": last_completed_sid or "(unset)",
				"next": next_sid,
				"wash": [0.0, 0], "rinse": [0.0, 0],
				"bleach": [0.0, 0], "clear": [0.0, 0],
				"prime": [0.0, 0],
			})
			e["next"] = next_sid
			return e

		try:
			with open(csv_path) as f:
				reader = csv.DictReader(f)
				for row in reader:
					status = row.get("status", "")
					if status == "completed":
						last_completed_sid = row.get("sample_id", "") or last_completed_sid
						continue
					if not status.startswith("purge_"):
						continue
					try:
						duration = float(row.get("dispense_duration_s") or 0.0)
					except (TypeError, ValueError):
						duration = 0.0
					next_sid = row.get("sample_id", "") or "(unset)"
					well = row.get("well_id", "")
					m_rinse = rinse_re.match(well)
					if m_rinse:
						idx = int(m_rinse.group(1))
						entry = _ensure(idx, next_sid)
						entry["rinse"][0] += duration
						entry["rinse"][1] += 1
						continue
					m = phase_re.match(well)
					if not m:
						continue
					phase = m.group(1)
					idx = int(m.group(2))
					entry = _ensure(idx, next_sid)
					entry[phase][0] += duration
					entry[phase][1] += 1
		except OSError as exc:
			logger.warning("Failed to read log.csv for purge breakdown: %s", exc)
			return []
		out = []
		for _, e in sorted(pending.items()):
			out.append({
				"prev": e["prev"], "next": e["next"],
				"wash": tuple(e["wash"]),
				"rinse": tuple(e["rinse"]),
				"bleach": tuple(e["bleach"]),
				"clear": tuple(e["clear"]),
				"prime": tuple(e["prime"]),
			})
		return out

	def _compute_provenance(self):
		"""Walk log.csv and return [(sample_id, [well_ids]), …] preserving
		first-appearance order. Skips ``resume`` rows -- they're transition
		markers, not dispense events. Wells with empty sample_id are bucketed
		under "(unspecified)" so they're still visible."""
		csv_path = self.run_dir / "log.csv"
		if not csv_path.exists():
			return []
		try:
			with open(csv_path, newline="") as f:
				reader = csv.DictReader(f)
				per_sample = {}  # sample_id -> [well_id]
				order = []
				for row in reader:
					if row.get("status") == "resume":
						continue
					sid = row.get("sample_id") or "(unspecified)"
					wid = row.get("well_id") or ""
					if sid not in per_sample:
						per_sample[sid] = []
						order.append(sid)
					per_sample[sid].append(wid)
		except OSError as exc:
			logger.warning("Failed to read log.csv for provenance: %s", exc)
			return []
		return [(sid, per_sample[sid]) for sid in order]
