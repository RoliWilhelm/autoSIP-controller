# autoSIP Run Logging — Reference

This document describes exactly what autoSIP records to disk during a
fractionation run, organised by file and by the moment the file is
written. The intent is to give a reader (manuscript audience, reviewer,
downstream analysis) an unambiguous picture of which artefacts persist
incrementally during the run and which are produced only at End Run.

It is derived directly from `run_logger.py` and the call sites in
`main.py`. Where this document and earlier prose disagree, the code is
authoritative.


## 1. Directory layout

Every run gets its own folder under the repository's `logs/` tree:

```
logs/
  {project}/
    {timestamp_start}_{sample_id_at_start}/
      system.start.state.json
      log.csv
      end_{end_timestamp}.json
      summary_{end_timestamp}.md
      summary_{plate_id}_{end_timestamp}.md      (one per plate touched)
```

Reader-relevant behaviours:

- **`{project}` is taken from the operator's Project field at run start**
  and is reused across runs of the same project. If left blank it
  defaults to the literal string `default`.
- **`{timestamp_start}` is the ISO 8601 wall-clock time at the moment
  Begin Fractionation succeeds**, with millisecond precision and with
  `:` characters replaced by `-` so the path is filesystem-safe.
  Example: `2026-05-29T08-14-22.413`.
- **`{sample_id_at_start}` is fixed at run start.** It is the operator's
  Sample ID field as committed when Begin Fractionation succeeded. If
  the operator later swaps to a new sample (multi-sample run), the
  *folder name does not change* — only later log rows carry the new
  Sample ID. This is intentional: the folder name uniquely identifies
  the run; per-row provenance lives in `log.csv` itself.
- **`{end_timestamp}` is recomputed at every End Run** (with
  second precision and `:` replaced by `-`, e.g.
  `2026-05-29T11-02-58`). Because each finalisation suffixes its files
  with this timestamp, the operator can End Run, save, then continue,
  then End Run again with Save, and **no file is ever overwritten** — a
  fresh `end_{ts2}.json` and `summary_{ts2}.md` are created alongside
  the earlier ones.

If a run-directory write ever fails (`OSError`), the application logs a
warning and continues running without further log writes; the run does
not abort.


## 2. Written DURING the run (incrementally, append-only)

These two files persist incrementally as the run progresses. If the
process crashes mid-run, both survive on disk with whatever was written
up to the failure point — no data is held in memory waiting for a final
commit.

### 2.1 `system.start.state.json` — written once at run start

Created by `RunLogger.start(metadata)` the moment the operator
successfully clicks Begin Fractionation. Pretty-printed JSON with two
spaces of indentation. Fields:

| Field                       | Meaning                                                                                            |
| --------------------------- | -------------------------------------------------------------------------------------------------- |
| `timestamp_start`           | ISO 8601, ms precision; equals the folder name's timestamp portion.                                |
| `software_version`          | `main.__version__` at run start (currently `1.0.0`).                                               |
| `project`                   | Operator's Project field, validated identifier (filesystem-safe).                                  |
| `sample_id_at_start`        | Operator's Sample ID at run start. Frozen here; mid-run changes show up in `log.csv` rows only.    |
| `labware_file`              | Absolute path of the Opentrons labware JSON loaded for this run, or `null` if none.                |
| `labware_definition`        | Inline copy of the loaded labware JSON (object), or `null`.                                        |
| `parameters.rows`           | Plate row count (e.g. 8 for SBS).                                                                  |
| `parameters.cols`           | Plate column count (e.g. 12 for SBS).                                                              |
| `parameters.well_size_cm`   | Centre-to-centre well spacing in cm.                                                               |
| `parameters.pump_rate`      | Fractionation pump rate value (mL/hr — unit recorded separately).                                        |
| `parameters.pump_rate_units`| Literal `"mL/hr"`.                                                                                 |
| `parameters.drip_wait_time_s` | Post-dispense drip wait (seconds).                                                              |
| `parameters.purge_time_s`   | Inter-sample purge cycle duration (seconds).                                                       |
| `parameters.skip_intersample_purge` | Boolean — whether the operator preference at run start skips the inter-sample purge.         |
| `parameters.peristaltic_rate_ml_per_min` | Configured peristaltic pump rate, used for waste-volume estimates.                       |
| `parameters.max_waste_volume_ml` | Configured maximum waste-bin capacity.                                                        |
| `parameters.volume_per_well_ml` | Per-well dispense volume.                                                                       |
| `parameters.table_start_cm` | A1 X-coordinate from Plate Parameters (rounded to 2 dp).                                           |
| `parameters.carriage_start_cm` | A1 Y-coordinate (rounded to 2 dp).                                                              |
| `parameters.number_of_fractions` | N — total fractions to collect across the run's first sample.                                 |
| `parameters.discard_fractions` | D — number of initial fractions to discard into the waste bin.                                  |
| `parameters.waste_bin_table_cm` | Waste-bin X-coordinate (rounded).                                                              |
| `parameters.waste_bin_carriage_cm` | Waste-bin Y-coordinate (rounded).                                                           |
| `parameters.plate_id_at_start` | Plate ID at run start.                                                                          |
| `estimated_total_time_s`    | Projected runtime in seconds, derived from N, drip wait, pump time, and a per-move estimate.       |
| `bulk_submission`           | *Optional.* Present only when a Bulk Sample Submission CSV was loaded. Contains `source_path`, `total_samples`, `this_sample_index`, `spreadsheet_sample_id`, `actual_sample_id`, `notes`, and a `samples[]` array describing every sample in the spreadsheet. |

### 2.2 `log.csv` — appended row-by-row throughout the run

A standard CSV with a single header row, written the first time a row is
emitted. Every subsequent event appends a row and `flush()`es so a
post-crash file is complete up to the last event. The file is closed on
End Run (Save or Don't Save).

The header columns, in order:

| # | Column                  | Meaning                                                                                  |
| - | ----------------------- | ---------------------------------------------------------------------------------------- |
| 1 | `project`               | The Project value at the moment of write (so mid-run typo fixes propagate forward).      |
| 2 | `sample_id`             | The Sample ID at the moment of write — captures multi-sample changeovers per row.        |
| 3 | `plate_id`              | The Plate ID at the moment of write — captures plate swaps per row.                      |
| 4 | `well_id`               | Identifier of the event (see well-id conventions below).                                 |
| 5 | `plate_x`               | Numeric column-index of a plate well, OR the waste-bin / target X-coordinate for non-well events. |
| 6 | `plate_y`               | Numeric row-index of a plate well, OR the waste-bin / target Y-coordinate for non-well events.    |
| 7 | `dispense_start_iso`    | Wall-clock ISO 8601 timestamp (ms precision) when the dispense / cycle started.          |
| 8 | `dispense_end_iso`      | Wall-clock ISO 8601 timestamp (ms precision) when the dispense / cycle ended.            |
| 9 | `dispense_duration_s`   | Elapsed seconds, three decimal places. Measured with `time.monotonic()` (not differenced from the ISO strings) so it is immune to mid-run wall-clock adjustments. |
| 10| `status`                | Categorical event tag (see glossary below).                                              |

Numeric coordinate cells (`plate_x`, `plate_y`) are formatted to two
decimal places when populated.

#### `well_id` naming conventions

| Pattern                                | Emitted by                                              |
| -------------------------------------- | ------------------------------------------------------- |
| `A1`, `B7`, `H12`, …                    | Standard SBS well — used for `completed`, `emergency_stopped`, and the `resume` breadcrumb (where it names the *next* well to be dispensed). |
| `discard_{series}_{cycle}`             | Each discard cycle. `series` is the 1-based sample series index; `cycle` is the 1-based discard cycle within that series. |
| `purge_{phase}_{series}`               | Inter-sample purge automatic cycle for `wash` / `clear` / `bleach` / `prime`. `series` is the index of the NEW sample series. |
| `purge_{phase}_{series}_{sub}`         | The decontamination-protocol rinse uses `sub="rinse"` to disambiguate the post-bleach water flush from the pre-bleach wash. |
| `purge_{phase}_{series}[_{sub}]_ext{N}` | A Space-bar extension of a purge cycle, where `N` ≥ 1 is the 1-based extension counter within that phase. |
| `sysclean_{phase}`                     | Each System Clean phase: `bleach`, `soak`, `rinse1`, `rinse2`. |
| `sysclean_{phase}_ext{N}`              | A Space-bar extension of a System Clean phase. |
| `prime_auto`                           | The pre-fractionation automatic prime cycle (fires once at run start before the first dispense). |
| `prime_manual_ext{N}`                  | A Space-bar extension cycle of the pre-fractionation manual prime, with `N` ≥ 1. |
| `plate_swap_{N}`                       | Plate change, `N` is the 1-based count of swaps in this run. |
| `autopause_{N}` / `warning_{N}`        | 80%-of-waste-bin event, `N` is the 1-based count. |
| `hardstop_{N}` / `shutoff_{N}`         | 100%-of-waste-bin event. |
| `reset_{N}`                            | Operator Reset of the waste-bin counter. |
| `checklist_skipped_{context_id}`       | A pre-flight checklist that the operator bypassed via the Skip (Expert) button. `context_id` identifies which checklist (e.g. `plate_swap_2`, `purge_phase_1_4`). |

#### Complete glossary of `status` values

Every value that may appear in column 10. Every status that the code
can emit is listed; the glossary was cross-checked against every
`_write_row(...)` call site in `run_logger.py`.

| `status` value          | Plain-language meaning                                                                                  |
| ----------------------- | ------------------------------------------------------------------------------------------------------- |
| `completed`             | A plate well was dispensed successfully.                                                                |
| `discarded`             | A discard cycle dispensed into the waste bin successfully.                                              |
| `emergency_stopped`     | A plate well *or* a discard cycle was halted mid-dispense by Terminate Run. The row carries the elapsed duration up to the halt. |
| `resume`                | Breadcrumb written when the operator clicks Resume after a Pause during the collect phase. The `well_id` names the next well that will be dispensed; `dispense_duration_s` is `0.000`. |
| `plate_swap`            | Breadcrumb written when the operator completes Continue to Next Plate. `well_id` is `plate_swap_{N}`. |
| `purge_wash`            | One pump cycle of the inter-sample purge's water-wash phase (basic protocol) or pre-bleach water phase (decontamination protocol). |
| `purge_clear`           | One pump cycle of the inter-sample purge's air-clear phase. |
| `purge_bleach`          | One pump cycle of the inter-sample purge's bleach phase (decontamination protocol only). |
| `purge_prime`           | One pump cycle of the inter-sample purge's syringe-priming phase (uses `prime_time`, not `purge_time`). |
| `sysclean_bleach`       | One pump cycle of Phase 1 (Bleach Fill) of the on-demand System Clean routine. |
| `sysclean_soak`         | The Phase 2 timed soak. `dispense_duration_s` records the actual elapsed soak seconds; the pump is OFF for the whole row. |
| `sysclean_rinse1`       | One pump cycle of Phase 3 (Water rinse 1). |
| `sysclean_rinse2`       | One pump cycle of Phase 4 (Water rinse 2). |
| `prime_auto`            | The pre-fractionation automatic prime cycle (fires once at the start of every run). |
| `prime_manual_ext`      | One Space-toggle extension cycle of the pre-fractionation manual prime step. |
| `waste_autopause`       | Waste-bin filled to 80%; pump auto-paused. Counter in `well_id` is the 1-based count. |
| `waste_hardstop`        | Waste-bin filled to 100%; failsafe hard stop. |
| `waste_reset`           | Operator emptied the bin and clicked Reset, returning the running estimate to 0 mL. |
| `waste_warning`         | Legacy alias of `waste_autopause`. Kept so older code paths still produce a row; treated identically to `waste_autopause` by downstream readers. |
| `waste_shutoff`         | Legacy alias of `waste_hardstop`. |
| `checklist_skipped`     | The operator bypassed a pre-flight checklist via the Skip (Expert) button. `well_id` carries the checklist identifier. |

#### Per-sample fraction-index convention

The plate-well rows themselves do not carry an explicit fraction index;
they record the well coordinates. Downstream summary code (see §3.3)
reconstructs the per-sample fraction index by counting `completed` rows
per sample in order and offsetting by the run's configured
`discard_fractions` — i.e. the first `completed` row for a sample is
reported as fraction `D+1`.


## 3. Written only at End Run (Save and End)

These files are written **exclusively** by `RunLogger.end(...)`, which
is invoked only when the operator clicks **End Run → Save and End**. On
**End Run → Don't Save**, `RunLogger.close_without_summary()` runs
instead: it closes the CSV cleanly but writes **no** end / summary
files. On End Run → Cancel, none of the End Run handlers run at all.

Each End-Run save produces three filename families, all suffixed with
the End-Run-click timestamp so repeated End Runs in one session cannot
overwrite each other:

```
end_{end_timestamp}.json
summary_{end_timestamp}.md
summary_{plate_id}_{end_timestamp}.md     (one file per plate the run touched)
```

`{end_timestamp}` has second precision and `:` is replaced by `-`.

### 3.1 `end_{end_timestamp}.json`

Pretty-printed JSON. The schema:

| Field                            | Meaning                                                                                          |
| -------------------------------- | ------------------------------------------------------------------------------------------------ |
| `timestamp_end`                  | ISO 8601, ms precision, of the End-Run click.                                                    |
| `final_status`                   | `"completed"` (the run reached its total before End Run), `"manual_abort"` (operator ended mid-run), or `"emergency_stopped"` (Terminate Run path). |
| `project_at_end`                 | Project field as it stood at End Run (may differ from `metadata.project` if the operator edited it). |
| `final_sample_id`                | Sample ID at End Run.                                                                            |
| `wells_completed`                | Count of `completed` rows in `log.csv`.                                                          |
| `wells_planned`                  | Rows × Cols of the labware at run start.                                                         |
| `actual_total_time_s`            | Wall-clock duration from `timestamp_start` to `timestamp_end`, three decimal places.             |
| `plates_used`                    | Chronological list of Plate IDs the run touched (single-element when no plate swap occurred).    |
| `waste_volume_ml_at_run_start`*  | Running waste-bin estimate at Begin Fractionation.                                               |
| `waste_volume_ml_at_run_end`*    | Running waste-bin estimate at End Run.                                                           |
| `waste_added_this_run_ml`*       | `end − start`. Note: when a mid-run Reset fires this understates the true added volume; see `waste_resets_during_run`. |
| `waste_warnings_fired`*          | Count of 80% threshold events (`waste_autopause` rows).                                          |
| `waste_shutoffs_fired`*          | Count of 100% threshold events (`waste_hardstop` rows).                                          |
| `waste_resets_during_run`*       | Count of operator Reset clicks during the run.                                                   |

\* The starred fields appear only when a `waste_context` was supplied
to `RunLogger.end(...)` — i.e. for runs that went through the standard
end_run path. Direct test invocations of `RunLogger.end` may omit
them.

### 3.2 `summary_{end_timestamp}.md` — run-level human summary

Markdown, one file per End Run. Sections (some are conditional):

- **Title line + software version** — uses `metadata.timestamp_start`
  and `metadata.software_version`.
- **Final Sample ID, Final status, Started, Ended, Total runtime**
  (formatted `HH:MM:SS`).
- **`## Parameters`** — plate geometry (rows × cols), labware file,
  well size, pump rate (with units), volume per well, A1 starting
  position, and the original `estimated_total_time_s` for comparison
  against the realised runtime.
- **`## Result`** — `wells_completed / wells_planned`.
- **`## Discarded fractions`** *(only when `discard_fractions > 0`)* —
  count of discards and the waste-bin coordinates they were directed to.
- **`## Plates used`** *(when one or more Plate IDs were logged)* — per
  plate, the samples on it and the fraction-index ranges, derived by
  walking `log.csv`'s `completed` rows in order.
- **`## Estimated waste`** *(when a waste context was supplied)* —
  starting volume, total added (with sub-breakdown into discard volume
  and purge volume), end volume, warnings count, shutoffs count, and
  resets-during-run count. The volume figures are estimates
  (`pump_rate × pump-on-time`) and the section warns the operator they
  may diverge from physical reality if the configured rate doesn't
  match the actual pump.
- **`## Bulk submission`** *(only when a Bulk Sample Submission CSV was
  loaded)* — `source_path` of the spreadsheet, `total_samples`, and the
  `sample_sequence` actually run, with a trailing `b` appended to any
  Sample ID the operator edited via the transition dialog.
- **`## Inter-sample purges`** *(only when at least one `purge_*` row
  was written)* — per inter-sample transition, the previous / next
  Sample ID and a summary line listing each phase's cumulative
  pumped seconds and the number of operator-triggered Space-bar
  extensions.
- **`## Sample provenance`** — derived from `log.csv`'s `completed`
  rows, one bullet per sample listing the colour name from the
  WellPlateProgress palette, the well ranges (e.g. `A1–A12, B12–B1`),
  and the well count.
- **`## Plate`** *(only when a plate snapshot was supplied)* — fenced
  ASCII-art rendering of the final plate state.

### 3.3 `summary_{plate_id}_{end_timestamp}.md` — per-plate summary

One file per Plate ID in `plates_used`. Intended to be printed and
physically attached to the plate as it goes to downstream processing.
Sections:

- **Header**: Run project, run start timestamp, software version.
- **Plate ID, Final status, Run started, Run ended.**
- **`## Parameters`** — plate geometry, labware file, well size, pump
  rate (with units), volume per well.
- **`## Samples on this plate`** — one bullet per sample that landed on
  this specific plate, listing the WellPlateProgress colour name, the
  well ranges and the fraction-index range. Falls back to `(no wells)`
  if `log.csv` has no `completed` rows for this plate.


## 4. End Run × Save behaviour, in one place

To make the during-run vs End-Run split unambiguous:

| Action                                     | `system.start.state.json` | `log.csv`                      | `end_*.json` | `summary_*.md` | `summary_{plate_id}_*.md` |
| ------------------------------------------ | ------------------------- | ------------------------------ | ------------ | -------------- | ------------------------- |
| Begin Fractionation succeeds               | created         | created on first row           | —            | —              | —                         |
| Every dispense / purge / waste / breadcrumb | —              | one row appended per event     | —            | —              | —                         |
| **End Run → Save and End**                 | already on disk | closed                         | **written**  | **written**    | **written (one per plate)** |
| End Run → Don't Save                       | already on disk | closed (no end / summary files)| —            | —              | —                         |
| End Run → Cancel                           | already on disk | open, run continues            | —            | —              | —                         |
| Unexpected crash mid-run                   | already on disk | survives with rows up to crash | —            | —              | —                         |

The recommended way to read a finalised run is to consult
`end_{ts}.json` for the headline outcome, `summary_{ts}.md` for a
human narrative, the per-plate summaries for downstream-attached
documentation, and `log.csv` for the per-event ground truth that all of
the above are derived from.
