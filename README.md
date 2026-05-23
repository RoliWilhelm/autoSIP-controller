# autoSIP Controller Software

A Python/Tkinter graphical interface controlling a low-cost, 3D-printable,
Raspberry Pi-based isopycnic gradient fractionating robot for DNA/RNA stable
isotope probing (SIP) experiments. Accompanies Laud et al. 2024 (in preparation,
HardwareX).

## Status
Under active development — manuscript in preparation.

## Hardware requirements
- Raspberry Pi 2B+ running Raspberry Pi OS.
- Adafruit DC & Stepper Motor HAT controlling both NEMA-17 stepper motors via
  `adafruit_motorkit`.
- Two NEMA-17 stepper motors (200 base steps/rev, microstepped to 3200 effective
  steps/rev) driving lead screws with a 40 mm pitch (40 mm linear travel per
  revolution).
- Digital Loggers IoT relay on GPIO 5 (driven through `gpiozero.LED`) switching
  one of: a Razel R-200 syringe pump or an Adafruit 3910 peristaltic pump. Only
  one pump is connected at a time.
- Display: a ~7" Raspberry Pi touchscreen, or a developer laptop over VNC.

## Quick start

```bash
git clone <url> && cd autoSIP-controller-software
```

Create and activate a virtual environment:

```bash
python -m venv .venv && source .venv/bin/activate
```

On Windows, activate with:

```cmd
.venv\Scripts\activate
```

Install dependencies (Pi-only hardware deps; on a non-Pi system, simulation mode
will be enabled automatically once it lands in the next commit):

```bash
pip install -r requirements.txt
```

Run the application:

```bash
python main.py
```

## User Manual

This manual covers day-to-day operation of autoSIP. It assumes you have a
working installation (see *Quick start* above) and a calibrated rig
mounted with an ultracentrifuge tube above the carriage and a 96-well
plate (or other Opentrons-format labware) on the stage. For step-by-step
walkthroughs of common workflows, see the
[Operation Instructions — Common Workflows](docs/operation_instructions.md#63-common-workflows)
(Section 6.3 of the manuscript supplement).

![Figure: autoSIP main window, Automated mode](docs/figures/main_window.png)

### Coordinate system and the origin

Two stepper motors drive the dispensing needle: one for the X-axis
("table") and one for the Y-axis ("carriage"). At power-on, the motors
are wherever they were left in their last session. The software treats
that physical position as `(0, 0)` cm — the **origin**. The origin is a
software reference, not a guaranteed physical location.

The convention is to park the carriage at the **upper-left mechanical
limit** of the lead screws (gently against the stop) before launching
the software. Doing this consistently makes origin = the upper-left
limit, and gives every coordinate you later enter a stable physical
meaning.

Two consequences follow from that convention:

- **Plate-start coordinates** (Automated mode's Plate Parameters →
  *Starting well position (x-axis)* / *Starting well position (y-axis)*) are the X, Y
  position of well **A1** of the labware on the stage, measured in cm
  from the origin. You determine these once for a given stage layout
  by jogging the needle in Manual mode (see
  [Operation Instructions §6.3.1](docs/operation_instructions.md#631-calibrating-plate-start-and-waste-bin-coordinates)).

- **Waste bin position** (Plate Parameters → *Waste bin position
  (x-axis)* / *Waste bin position (y-axis)*) is the X, Y position of
  the waste container. These same two values are mirrored in
  Cleaning mode — editing them in either mode updates the other.

Three buttons sound similar but do different things:

- **Return to Origin** (Automated mode, run-controls row) and **Home**
  (Manual mode, Jog Controls) are equivalent: both move the motors
  to origin `(0, 0)` and re-zero the software's tracked angle
  counters. Return to Origin in Automated mode is also the mid-run
  recalibration entry point — clicking it while paused captures the
  current motor position so Resume can drive back there and pop a
  Confirm Calibration dialog.
- **Return to Start Well** (Automated mode, run-controls row) moves
  the needle to the plate-start coordinates — well **A1** of the plate
  — *not* to origin. Enabled only while idle; disabled mid-run.
- **Home** (Manual mode, Jog Controls) moves both motors to origin
  `(0, 0)` and re-zeros the software's tracked angle counters. The
  Position readout will then read exactly `X = 0.000 cm,
  Y = 0.000 cm`.

The re-zeroing matters: stepper motors can miss steps over a long
session, and the software counters drift away from the true physical
position. Periodically re-park the carriage manually against the
upper-left limit and click **Home** to recalibrate.

### The three operating modes

The top of the window has three mode tabs — **Automated**, **Manual**,
and **Cleaning** — and the active mode is highlighted in the accent
color. Click any tab to switch directly; if a fractionation is paused
mid-run, you will be asked to confirm the switch.

- **Automated** — the main fractionation workflow. Enter run, plate,
  and pump parameters, click **Begin Fractionation**, and autoSIP
  drives the run to completion.
- **Manual** — free needle positioning and independent pump
  operation. Used for calibration, troubleshooting, and ad-hoc tasks.
- **Cleaning** — a stripped-down mode for purging tubing. The needle
  moves to the waste bin and a single Purge button runs the pump.

### Automated mode controls

![Figure: Automated mode panel](docs/figures/automated_mode.png)

Automated mode is laid out top-to-bottom in four sections, then the
run-launch button and the progress canvas.

**Run Parameters** — what identifies this run and how much liquid to
move:

- **Project name** — required identifier (1–64 characters; letters,
  digits, `.`, `_`, `-`). Becomes the top-level folder under `logs/`.
  Mid-run edits prompt for confirmation: the new value applies to
  subsequent log rows, but files already written keep their original
  Project name.
- **Sample ID** — required identifier with the same character class.
  Identifies the source tube. **Commonly edited mid-run** — when you
  swap source tubes between samples, update Sample ID before clicking
  Continue to Next Sample. Tooltip in the GUI reads: "Identifies the
  source tube. Change during a pause when swapping tubes."
- **Plate ID** — required identifier. Identifies the physical plate
  currently on the stage. **Auto-incremented at each plate swap**
  (e.g., `Plate-1` → `Plate-2`). Default at first launch is `Plate-1`.
- **Number of fractions** — total fractions to dispense per sample,
  *including discards* (1 to {ROWS_MAX × COLS_MAX = 384}; capped at
  `rows × cols` for the loaded plate at run start).
- **Discard fractions** — number of initial fractions to send to the
  waste bin before plate collection begins (0 to N−1). Useful for
  bleeding off low-density buffer above the band of interest. Set
  to `0` to skip the discard phase entirely.
- **Volume per well (mL)** — float in `[0.1, 2.0]`, e.g. `0.22`.
  The pump runs for `volume / pump_rate` seconds per well.

**Bulk Sample Submission** — preload metadata for a multi-sample
session from a spreadsheet, so you don't have to retype Sample ID /
Plate ID / fraction counts between tubes:

- **Generate Template** — writes a starter CSV (`sample_id`,
  `plate_id`, `number_of_fractions`, `discard_fractions`,
  `volume_per_well_ml`, `notes`) with header comments and a couple of
  example rows. Only `sample_id` is required per row; blank optional
  cells inherit the current Run Parameters values at import time.
- **Import Submission** — parses the CSV, validates each row, and
  loads the samples. On success, Run Parameters auto-populate from
  the first row and lock (except **Project name**, which stays
  editable so you can adjust the log folder before clicking Begin
  Fractionation). On any validation error the import is rejected
  whole (the panel never half-activates).
- **Exit Bulk Mode** — appears when a submission is loaded; clears
  the loaded samples after a confirmation prompt and re-enables the
  Run Parameters entries.

When bulk mode is active and a sample finishes (auto-pause at "Total
reached"), a transition dialog opens showing the next sample's
spreadsheet values. You can edit the Sample ID inline before clicking
**Continue** — edits are remembered in `summary.md` with a `b`
suffix. End Run and Terminate Run both implicitly exit bulk mode.

**Plate Parameters** — what the labware looks like and where it sits:

- **Load labware specs** — at the top of the section. Browse for
  an Opentrons-format JSON file; autoSIP reads `ordering`,
  `dimensions`, and per-well `x`/`y` and uses them to populate the
  rows, columns, well-width, and starting-point fields below.
- **Number of rows** — 1–16.
- **Number of columns** — 1–24.
- **Well width (cm)** — center-to-center well spacing, `[0.1, 5.0]`.
- **Starting well position (x-axis)** — X position of well A1, `[0.0, 20.0]` cm.
- **Starting well position (y-axis)** — Y position of well A1, `[0.0, 15.0]` cm.
- **Waste bin position (x-axis)** — X position of the waste container
  in the same coordinate frame, `[0.0, 20.0]` cm. Required when
  Discard fractions > 0. autoSIP warns if this position appears to
  fall inside the plate footprint.
- **Waste bin position (y-axis)** — Y position of the waste
  container, `[0.0, 15.0]` cm.

**Pump** — flow rate and per-well drip wait:

- **Pump rate (mL/hr)** — float in `[0.1, 600.0]`. Match the value to
  your syringe pump's gear-set or peristaltic-pump calibration.
- **Drip wait time (s)** — float in `[0.0, 60.0]`. The dwell time
  *after* the pump shuts off, *before* the carriage moves to the next
  well, so the dispensed drop has time to detach cleanly. Default
  `1.0`. Longer waits improve volume consistency; shorter waits
  speed up the run.
- **Purge time (s)** — float in `[1.0, 600.0]`, default `30.0`. The
  duration of each of two pump phases run between samples: one
  flushing wash solution through the tubing, one pushing air through
  to clear the wash. Use Cleaning mode's *Purge Time Calibration*
  panel to measure the right value for your tubing.
- **Skip inter-sample purge** (checkbox) — when checked, Continue to
  Next Sample goes straight to the new sample's discard phase with
  no tubing flush. Leave unchecked for multi-sample runs to prevent
  carryover.
- **Peristaltic pump rate (mL/min)** — float in `[1.0, 200.0]`,
  default `100.0`. Used by the waste-bin estimator to convert
  purge-phase pump-on time into a volume contribution.
- **Max waste bin volume (mL)** — float in `[10.0, 5000.0]`, default
  `250.0`. autoSIP's waste-bin estimator warns when the running
  estimate reaches 80 % of this capacity and **halts all pump
  activity** at 100 %. The estimate is based on configured pump rates
  × pump-on time, not a real measurement; the *Reset* button in the
  status bar is the ground-truth mechanism after a physical empty.
  The counter resets to 0 on every app launch (so closing and
  reopening the app produces a fresh counter — empty the bin first
  if you want the new counter to reflect reality).

**Run controls** (top-right of the Automated frame):

- **Return to Origin** — moves the motors to `(0, 0)` and tares the
  software counters. Equivalent to Manual mode's Home button. Also
  works mid-pause: clicking it captures the current position so the
  matching Resume can drive the needle back and pop a Confirm
  Calibration dialog. Used to recover from stepper-motor drift
  without aborting the run.
- **Return to Start Well** — moves the needle to the plate-start
  coordinates (well A1) entered in Plate Parameters. Enabled only
  while idle; disabled mid-run.
- **Pause** — pauses an in-progress run; pump off, motors hold
  position. The button label flips to **Resume** while paused.
- **Continue to Next Sample** — enabled after the auto-pause at "Total
  reached". Starts a new sample series: optional discard phase, then
  collection at the next available plate well.
- **Continue to Next Plate** — enabled after the auto-pause at
  "Plate full". Opens the plate-swap dialog.
- **End Run** — finalizes the run. Prompts to save the run logs;
  on Yes, writes the end/summary files. On No, the run terminates
  without finalization (the metadata.json and log.csv written
  during the run remain on disk).

**Begin Fractionation** — the large primary action button at the
center of the strip, flanked by a centrifuge-tube icon on the left
and the SIP-readout bimodal-distribution icon on the right. Click to
validate inputs, confirm the run summary, and start the state
machine.

**Progress display** — a to-scale rendering of the well plate
beneath the run controls.

- **Gray, unmarked well** = not yet visited.
- **Numbered, colored well** = collected fraction. The number is the
  **fraction index within the current sample**, counting discards.
  For example, with Discard fractions = 2, the first plate well of
  a sample is numbered **3**.
- Each sample uses a distinct color from the eight-entry Okabe–Ito
  colorblind-safe palette (orange, sky blue, bluish green, yellow,
  blue, vermillion, reddish purple, black). A run with more than
  eight samples cycles back to the start of the palette.

Hovering over a filled well shows a tooltip with the Sample ID and
the fraction's position within that sample.

### Manual mode controls

![Figure: Manual mode panel](docs/figures/manual_mode.png)

**Jog Controls** — directional movement:

- Four directional buttons: **▲ Y+**, **◀ X−**, **X+ ▶**, **Y− ▼**.
  The Y axis is oriented so that pressing **▼ Y−** moves the needle
  *down* (toward higher row indices, A → H), matching a plate origin
  at the upper-left corner.
- **Step** selector — `0.1 mm`, `1 mm`, or `10 mm`. The step size
  translates directly to the cm units used in Automated mode (a
  10 mm jog covers the same distance as typing `1.0` cm into a
  Starting well position field).
- **Home** — moves both motors to origin `(0, 0)` and re-zeros the
  software's tracked angle counters.
- **Position readout** — `Position: X = 0.000 cm, Y = 0.000 cm`,
  updated after every jog and Home.

Soft travel limits are enforced on every jog: X axis `[0, 20]` cm,
Y axis `[-15, 0]` cm (the Y range is negative so that pressing
**▼ Y−** moves from origin into the plate's travel range; **▲ Y+**
from origin is refused).

**Pump Controls** — the two pump buttons:

- **Fractionate** — toggles the relay; the button below the label
  reads `Fractionate: OFF` or `Fractionate: ON`.
- **Purge** — toggles the relay; reads `Purge: OFF` or `Purge: ON`.

Both buttons control the same physical relay (GPIO 5). Only one of
the two semantic claims can be active at a time — while one is on,
the other's button is disabled. The first time you turn either pump
on per Manual-mode visit, autoSIP shows a confirmation dialog that
reminds you to verify which pump is wired to the relay outlet.

**Space-bar shortcut** — pressing the space bar toggles whichever
pump was used most recently. A small `(Space)` hint label sits next
to the bound button so you can see at a glance which one space will
fire. The shortcut is active **only in Manual mode** and **only when
no text-entry widget has keyboard focus** (so space still types a
literal space inside a name field). The most-recently-used pump
persists across application restarts.

### Cleaning mode

![Figure: Cleaning mode panel](docs/figures/cleaning_mode.png)

Cleaning mode strips Automated mode down to two inputs and two
actions:

- **Waste bin position (x-axis)** and **Waste bin position
  (y-axis)** — the same two values that appear in Automated
  mode's Plate Parameters → Waste bin section. Edits propagate
  in both directions automatically.
- **Move to Waste Bin** — jogs the needle to the waste-bin
  coordinates.
- **Purge** — toggles the relay (same semantics as Manual mode's
  Purge button; same confirmation dialog).

A typical cleaning cycle is: switch to Cleaning mode, click
**Move to Waste Bin**, click **Purge**, run the pump until the
fluid path is clear, click **Purge** again to stop.

### Fractionate and Purge: how the two pump labels work

autoSIP exposes two semantic pump buttons — **Fractionate** and
**Purge** — but the hardware has **only one relay** (Digital Loggers
IoT relay on GPIO 5). The operator physically plugs **one** pump
into the relay outlet at a time:

- The **Razel R-200 syringe pump** for fractionation runs.
- The **Adafruit 3910 peristaltic pump** for purging / line cleaning.

The two labels are an intentional UX safety cue. Before the relay
turns on, a confirmation dialog spells out which pump should be
plugged in for the chosen operation and asks you to confirm. This
forces you to think about the physical state of the rig before
sending current through the relay.

While one pump (Fractionate or Purge) holds the claim, the other's
button is disabled across all three modes — a software interlock
on top of the physical "you can only plug one cable in" reality.
The state machine claims Fractionate at the start of an Automated
run and holds it across the entire run (even during the drip wait
when the relay is briefly off).

### Terminate Run (emergency stop)

The red octagonal **Terminate Run** button lives at the
bottom-right of the status bar, visible in Automated mode. Click it
to hard-halt a run: pump off, motors released, all run-control
buttons disabled. A confirmation dialog ("Are you sure that you
wish to stop the whole run?") guards against accidental clicks.

After a Terminate Run:

- If autoSIP was mid-dispense or mid-drip-wait, the in-flight entry
  is committed to `log.csv` with status `emergency_stopped`.
- A "save plate snapshot" file dialog opens, offering to write the
  current plate state to a `.txt` file before clearing the view.
- The status bar reads `Run terminated — click Return to Start
  Coords to re-enable controls.`
- Clicking **Return to Origin** re-enables Manual jog and
  Begin Fractionation.

Use Terminate Run for safety emergencies (smell, collision, fluid
leak) when an immediate stop matters more than a clean log. For
planned interruptions (operator break, tube swap, troubleshooting),
prefer **Pause** instead — Pause is reversible and does not write
any `emergency_stopped` rows.

### Logging output

Each run writes a directory under `logs/` in the working directory:

```
logs/
└── {project}/
    └── {timestamp_start}_{sample_id_at_start}/
        ├── metadata.json
        ├── log.csv
        ├── end_{end_timestamp}.json
        ├── summary_{end_timestamp}.md
        └── summary_{plate_id}_{end_timestamp}.md
```

- **`metadata.json`** — captured at run start: project, sample ID,
  plate ID, parameters block (rows, cols, well width, pump rate,
  drip wait time, volume per well, waste-bin coords, plate-start
  coords, number of fractions, discard fractions), labware file
  path, inline labware JSON contents, estimated total run time.
- **`log.csv`** — one row per fraction, plus breadcrumb rows at
  sample and plate boundaries. Columns: `project, sample_id,
  plate_id, well_id, plate_x, plate_y, dispense_start_iso,
  dispense_end_iso, dispense_duration_s, status`. Status values
  are:
  - `completed` — plate well dispense finished cleanly.
  - `discarded` — discard cycle finished cleanly.
  - `resume` — breadcrumb at the start of a new series
    (Continue to Next Sample) or after Pause + Resume during
    collection.
  - `plate_swap` — breadcrumb at the start of a new plate
    (Continue to Next Plate).
  - `emergency_stopped` — the run was terminated while this
    well or discard cycle was mid-dispense.
- **`end_{end_timestamp}.json`** — written only if you choose
  "Yes, save" at End Run. Final status (`completed`,
  `manual_abort`, or `emergency_stopped`), wells completed,
  wells planned, actual total time, list of plates used.
- **`summary_{end_timestamp}.md`** — human-readable run summary.
- **`summary_{plate_id}_{end_timestamp}.md`** — one per plate
  used. Filtered slice of the run summary suitable for attaching
  to the physical plate during downstream processing.

`metadata.json` and `log.csv` are written **continuously during the
run**; the three `_{end_timestamp}` files are written **only when
End Run is confirmed with "Yes, save"**. If you choose "No,
discard," the metadata.json and log.csv remain on disk but no
end/summary files are produced — useful when the run was a test or
calibration you do not want to archive.

The `{end_timestamp}` suffix is an ISO 8601 datetime with colons
replaced by hyphens (e.g., `2026-05-19T14-22-01`), so multiple End
Runs in the same session never overwrite each other.

### Keyboard shortcuts

| Key       | Effect                                                 |
| --------- | ------------------------------------------------------ |
| **Space** | Toggle the most-recently-used pump (Manual mode only). |

The space-bar shortcut is suppressed when keyboard focus is inside a
text-entry widget so you can still type a literal space character in
any name field.

For step-by-step walkthroughs of common workflows, see
[Operation Instructions §6.3](docs/operation_instructions.md#63-common-workflows).

## License
GPL-3.0 — see LICENSE.

## Citation
If you use autoSIP, please cite Laud et al. 2024 (in preparation, HardwareX).
