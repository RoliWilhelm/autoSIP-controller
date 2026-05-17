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
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("autosip")

# Run logs live alongside the source so they're visible in a file manager
# without showing hidden files. Resolved at import time; tests can patch
# this module attribute to redirect to a tmpdir.
DEFAULT_LOGS_DIR = Path(__file__).resolve().parent / "logs"

# Backwards-compatible alias for older tests that reached in by the old name.
_DEFAULT_BASE_DIR = DEFAULT_LOGS_DIR


def _now_iso():
	"""Local-time ISO 8601 timestamp to second precision."""
	return datetime.now().isoformat(timespec="seconds")


def _iso_for_dirname(iso):
	"""Replace path-unfriendly characters in an ISO timestamp."""
	return iso.replace(":", "-").replace("/", "-")


def _well_id(x, y):
	return f"{chr(ord('A') + y)}{x + 1}"


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


def _fmt_hms(seconds):
	seconds = max(0, int(round(seconds)))
	h, rem = divmod(seconds, 3600)
	m, s = divmod(rem, 60)
	return f"{h:02d}:{m:02d}:{s:02d}"


class RunLogger:
	"""One instance per fractionation run; do not reuse across runs."""

	# project + sample_id come FIRST so a CSV reader sees provenance before
	# any well-level data. Both values are captured at the moment each row
	# is written (not at run start), via the ``get_current_run_id`` callable.
	CSV_HEADER = [
		"project", "sample_id",
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
		# Callable returning ``{"project": str, "sample_id": str}`` for the
		# current state of the GUI. Called per CSV write so mid-run edits
		# propagate. Defaults to an empty dict for tests / call-sites that
		# don't care about provenance.
		self._get_current_run_id = get_current_run_id or (lambda: {"project": "", "sample_id": ""})
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

	def end(self, final_status, snapshot=None):
		"""Write ``end.json`` and ``summary.md``, then close the CSV.

		``final_status`` is one of {completed, emergency_stopped, manual_abort}.
		"""
		if self.run_dir is None:
			return
		timestamp_end = _now_iso()
		rid = self._get_current_run_id()

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

		try:
			with open(self.run_dir / "end.json", "w") as f:
				json.dump(
					{
						"timestamp_end": timestamp_end,
						"final_status": final_status,
						"project_at_end": rid.get("project", ""),
						"final_sample_id": rid.get("sample_id", ""),
						"wells_completed": wells_completed,
						"wells_planned": wells_planned,
						"actual_total_time_s": round(actual_total_time_s, 3),
					},
					f, indent=2,
				)
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
					wells_completed, wells_planned, actual_total_time_s)
			except (OSError, Exception) as exc:
				logger.warning("Failed to write summary.md: %s", exc)

	# -- Per-well timestamps -------------------------------------------
	#
	# All in-flight tracking is keyed by well_id (string) so plate wells
	# ("A1", "B4", ...) AND discard cycles ("discard_1", "discard_2", ...)
	# share the same code path. For plate wells the public methods take
	# (x, y) and compute the well_id internally; for discard cycles the
	# discard_* methods take an index and waste-bin coords.

	def _track(self, well_id, plate_x, plate_y):
		if self.run_dir is None:
			return
		self._wells[well_id] = {
			"well_id": well_id,
			"plate_x": plate_x,
			"plate_y": plate_y,
			"dispense_start_iso": _now_iso(),
			"dispense_end_iso": None,
		}

	def _mark_end(self, well_id):
		if self.run_dir is None:
			return
		well = self._wells.get(well_id)
		if well is not None:
			well["dispense_end_iso"] = _now_iso()

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
		start = well["dispense_start_iso"]
		end = well["dispense_end_iso"]
		duration = ""
		if start and end:
			try:
				delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
				duration = f"{delta.total_seconds():.3f}"
			except ValueError:
				duration = ""
		rid = self._get_current_run_id()
		self._write_row([
			rid.get("project", ""), rid.get("sample_id", ""),
			well["well_id"], well["plate_x"], well["plate_y"],
			start, end, duration, status,
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
			rid.get("project", ""), rid.get("sample_id", ""),
			_well_id(next_x, next_y), next_x, next_y,
			now, now, "0.000", "resume",
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
			wells_completed, wells_planned, actual_total_time_s):
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
			f"- Volume per well: {params.get('volume_per_well_cc', '?')} cc",
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

		with open(self.run_dir / "summary.md", "w") as f:
			f.write("\n".join(out))

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
