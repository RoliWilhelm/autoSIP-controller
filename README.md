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

The convention is to park the carriage at the **upper-left
mechanical-limit corner** of the lead screws (gently against the stop)
before launching the software. This corner is the origin `(0, 0)`
regardless of plate orientation; the Manual jog buttons drive the
motors in fixed physical directions (`+X` moves east, `+Y` moves
south) regardless of orientation too.

Doing this consistently makes `(0, 0)` cm equal to the upper-left
mechanical limit and gives every coordinate you later enter a stable
physical meaning. Switching plate orientation only changes which
plate axis maps to which motor axis (and therefore the Starting Well
Position you calibrate) — recalibrate the Starting Well and Waste
Bin coordinates after a switch.

Two consequences follow from that convention:

- **Plate-start coordinates** (Automated mode's Plate Parameters →
  *Starting well position (x-axis)* / *Starting well position (y-axis)*) are the X, Y
  position of well **A1** of the labware on the stage, measured in cm
  from the origin. You determine these once for a given stage layout
  by jogging the needle in Manual mode (see
  [Operation Instructions §6.3.1](docs/operation_instructions.md#631-calibrating-plate-start-and-waste-bin-coordinates)).

- **Waste bin position** (Tools → Cleaning Parameters… → *Waste bin
  position (x-axis)* / *Waste bin position (y-axis)*) is the X, Y
  position of the **CENTER** of the waste bin rectangle. Manual
  mode's Position Calibration Tool prompts the operator to jog the
  needle over the bin's visual center before saving, so the stored
  coordinate matches what the operator physically aimed at. These
  same two values are mirrored in Cleaning mode — editing them in
  either place updates the other.

- **Waste bin size** (Tools → Cleaning Parameters… → *Waste bin size
  (X × Y, cm)*) — two optional extents giving the bin's full width
  and height. The rectangle spans `± extent/2` around the center on
  each axis. When both extents are 0 (default), every move-to-waste
  targets the center point itself (legacy point-target behaviour).
  When extents are set, every move-to-waste — discards,
  inter-sample purge phases, System Clean, pre-fractionation prime
  when `D > 0`, and the Manual Move-to-Waste button — routes
  through a shortest-path helper that clamps the current needle XY
  to the bin's interior (rectangle shrunk by a 5 mm rim margin on
  each side). This saves motor travel when the needle is far from
  the bin center but already near a different bin edge. `log.csv`
  records the actual entry point used, not the center, so per-event
  coordinates reflect where the fluid physically went. The XY
  table view renders the bin as a semi-transparent amber rectangle
  at scale, centered on the saved coordinate; the marker falls back
  to a small amber dot at the center when extents are 0.

Two button names cover the same action in different places:

- **Return to Origin** (Automated mode, run-controls row, AND
  Manual mode, Jog Controls — the same name in both, redundant by
  design) moves the motors to origin `(0, 0)` and re-zeros the
  software's tracked angle counters. In Automated mode it's also
  the mid-run recalibration entry point — clicking it while paused
  captures the current motor position so Resume can drive back
  there and pop an Origin Calibration dialog.
- **Return to Start Well** (Automated mode only) moves the needle
  to the plate-start coordinates — well **A1** of the plate — *not*
  to origin. Enabled only while idle; disabled mid-run.

The re-zeroing matters: stepper motors can miss steps over a long
session, and the software counters drift away from the true physical
position. Periodically re-park the carriage manually against the
upper-left mechanical limit and click **Return to Origin** to
recalibrate.

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
- **Prime time (s)** — float in `[0.0, 600.0]`, default `60`.
  Duration the syringe pump runs automatically at the start of
  every run to walk the sample solution from the tube up to roughly
  5 cm below the dispenser. Used by the pre-fractionation priming
  workflow (see *Pre-fractionation priming* below). Manual mode's
  *Prime Time Calibration Tool* measures it empirically.

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
suffix. End Run implicitly exits bulk mode.

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

The previous *Waste bin position (x-axis)* and *(y-axis)* rows
moved out of Plate Parameters and now live in **Tools → Cleaning
Parameters…** alongside the rest of the waste-bin geometry; see
"Tools menu" below.

**Tools menu — Pump Parameters…** (Razel R-200 syringe pump settings
used during fractionation):

- **Pump rate (mL/hr)** — float in `[0.1, 600.0]`. Match the value
  to your syringe pump's gear-set.
- **Drip wait time (s)** — float in `[0.0, 60.0]`. The dwell time
  *after* the pump shuts off, *before* the carriage moves to the next
  well, so the dispensed drop has time to detach cleanly. Default
  `1.0`. Longer waits improve volume consistency; shorter waits
  speed up the run.
- **Prime time (s)** — float in `[0.0, 600.0]`, default `60`. How
  long the syringe pump runs to walk fractionation solution from
  the tube to the dispenser tip. Cleaning mode's *Prime Time
  Calibration Tool* measures this empirically.

Save persists the values to `config.json`; Cancel reverts the dialog
to the values it opened with. Mid-run pump-rate changes apply to the
next dispense (the in-flight dispense uses the rate captured when
it started).

**Tools menu — Cleaning Parameters…** (Adafruit 3910 peristaltic
pump and waste-bin geometry, used during inter-sample purges,
manual purges, and Cleaning Purge):

- **Purge time (s)** — float in `[1.0, 600.0]`, default `30.0`. The
  duration of each of two pump phases run between samples: one
  flushing wash solution through the tubing, one pushing air through
  to clear the wash. Use Cleaning mode's *Purge Time Calibration
  Tool* panel to measure the right value for your tubing.
- **Peristaltic pump rate (mL/min)** — float in `[1.0, 200.0]`,
  default `100.0`. Used by the waste-bin estimator to convert
  purge-phase pump-on time into a volume contribution.
- **Max waste bin volume (mL)** — float in `[10.0, 5000.0]`, default
  `250.0`. autoSIP's waste-bin estimator tracks the running
  estimate **live** (the flask icon in the status bar fills and
  changes colour from green → amber → orange → red while any pump
  is on), shows an advisory **80% auto-pause** dialog (where the
  operator may Reset after a physical empty *or* Resume past the
  warning) and a blocking **100% hard stop** dialog (where Resume
  is disabled until Reset is clicked).
- **Waste bin position (x-axis)** — X anchor of the waste container
  in table coordinates, `[0.0, 20.0]` cm. Required when Discard
  fractions > 0.
- **Waste bin position (y-axis)** — Y anchor of the waste container,
  `[0.0, 15.0]` cm.
- **Waste bin size (X × Y, cm)** — width and height of the
  rectangular waste bin. Non-zero values enable shortest-path
  routing; default `0` keeps the legacy point-target behaviour.

Save persists and triggers an immediate repaint of the table view
so bin geometry edits are visible without a mode switch.

If either dialog has required fields left blank, an italic muted
hint banner appears at the top of the Automated panel:
*"Configure pump and cleaning parameters in the Tools menu before
starting a run."* The banner dismisses itself once the relocated
fields are populated. Clicking **Begin Fractionation** with any
required pump/cleaning field still blank surfaces a targeted dialog
that names the missing fields and the Tools entry to open.

The *Skip inter-sample purge* preference lives under **Tools →
Preferences** alongside *Return needle to origin on exit*.

**Run controls** (top-right of the Automated frame):

- **Return to Origin** — moves the motors to `(0, 0)` and tares the
  software counters. Same action as Manual mode's Return to Origin
  button (the two are redundant by design). Also works mid-pause:
  clicking it captures the current position so the matching Resume
  can drive the needle back and pop an Origin Calibration dialog.
  Used to recover from stepper-motor drift without aborting the run.
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
  without finalization (the system.start.state.json and log.csv written
  during the run remain on disk).

**Begin Fractionation** — the large primary action button at the
center of the strip, flanked by a centrifuge-tube icon on the left
and the SIP-readout bimodal-distribution icon on the right. Click to
validate inputs, confirm the run summary, and start the state
machine.

**Pre-fractionation priming.** After the Begin-Fractionation
confirmation, the needle moves to its first-dispense position
(waste bin if Discard fractions > 0, otherwise plate start well
A1) and a *Prime Fractionation Line* modal opens. The syringe pump
runs automatically for the configured **Prime time** seconds with a
live countdown; when the automatic prime completes, the dialog
switches to a *manual walk-to-droplet* phase — press **Space** to
start the pump, watch the line until an even droplet forms at the
needle, press **Space** again to stop, then click **Begin Run**.
Multiple Space-toggle extensions are allowed; each press-on →
press-off pair is logged. For bulk submissions, this priming runs
once before the first sample only — later samples are primed by
the inter-sample purge's final phase, which uses the same
Prime-time-driven mechanic.

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
  Each button drives the motor in a fixed physical direction
  regardless of plate orientation: `+X` moves east, `+Y` moves south
  (away from the upper-left origin and into the plate-side travel
  range).
- **Step** selector — `0.1 mm`, `1 mm`, or `10 mm`. The step size
  translates directly to the cm units used in Automated mode (a
  10 mm jog covers the same distance as typing `1.0` cm into a
  Starting well position field).
- **Return to Origin** — sits above the directional pad. Moves
  both motors to origin `(0, 0)` and re-zeros the software's
  tracked angle counters.
- **Position readout** — `Position: X = 0.00 cm, Y = 0.00 cm`,
  updated after every jog and Return-to-Origin action.

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

**Position Calibration Tool** — captures the carriage's current cm
position into either *Starting well* or *Waste bin* with one click.
The full step-by-step calibration walkthrough lives in
[Operation Instructions §6.3.1](docs/operation_instructions.md).

**Prime Time Calibration Tool** — a stopwatch panel that measures
the *Prime time* parameter empirically for your tubing geometry.
Connect a sample tube, click **Start** to run the syringe pump and
begin the timer, watch the line, and click **Stop** the moment the
solution reaches ~5 cm below the syringe needle. Click **Save as
Prime Time** to write the measured value into Automated mode's
Prime time field. **Reset** clears the measurement between
attempts. The measurement is not logged to `log.csv` — it is a
setup operation, not a fractionation event.

### Cleaning mode

![Figure: Cleaning mode panel](docs/figures/cleaning_mode.png)

Cleaning mode focuses on between-sample / between-session line
maintenance:

- **Waste bin position (x-axis)** and **Waste bin position
  (y-axis)** — the same two values that appear in Automated
  mode's Plate Parameters → Waste bin section. Edits propagate
  in both directions automatically.
- **Move to Waste Bin** — jogs the needle to the waste-bin
  coordinates.
- **Purge** — toggles the relay (same semantics as Manual mode's
  Purge button; same confirmation dialog).
- **System Clean** — a four-phase decontamination routine
  (bleach fill → soak → water rinse 1 → water rinse 2). More
  stringent than the inter-sample purge: the bleach is held
  static in the line for a configurable soak period before
  rinsing. The soak duration is entered at runtime in the
  Phase 1 (Bleach Fill) dialog — default 5 minutes (range 0–30),
  not persisted between invocations. Use at session start, end of
  session, or during a paused automated run. System Clean does not
  prime with sample solution — that step belongs to the
  pre-fractionation prime workflow or the inter-sample purge's
  final phase.

A typical cleaning cycle is: switch to Cleaning mode, click
**Move to Waste Bin**, click **Purge**, run the pump until the
fluid path is clear, click **Purge** again to stop. For a
stringent end-of-session decontamination, click **System
Clean** instead.

### Inter-sample purge workflow

Between samples in a multi-sample run, autoSIP opens a modal
sequence that walks the operator through swapping the syringe,
attaching tubing, cleaning the line, and re-priming. The protocol
is selected in **Tools → Preferences → Inter-sample purge
protocol**:

- **Water only (3 phases)** — wash, air clear, prime sample.
- **Decontamination (5 phases)** — sterile water flush, **0.5%
  v/v sodium hypochlorite (bleach) flush** (1:10 dilution of
  household bleach, *prepared fresh on the day of use*), sterile
  water rinse, air clear, prime sample.

Each phase opens with a checklist of physical actions the
operator must perform — disengage the used syringe, attach the
collector tube to the wash line, swap the inlet between containers,
attach a new syringe, connect the next sample tube. The primary
action button starts the appropriate pump (peristaltic for
wash/bleach/rinse/clear, syringe for the final *Prime sample*
phase). Wash/bleach/rinse/clear phases run for *Purge time*
seconds with a live countdown; the *Prime sample* phase runs the
syringe pump for **Prime time** seconds, then enters a
walk-to-droplet state where pressing **Space** extends pumping
until the operator sees an even droplet. A dynamic line in the
prime dialog names the destination of the priming output (the
waste bin if the next sample has discards, or the current well
otherwise — so the operator knows whether to walk minimally or
freely). The workflow can be bypassed entirely by ticking
*Skip inter-sample purge* in Preferences.

### Preferences

**Tools → Preferences** exposes six persistent behavioral
preferences. Five are stored in `~/.autosip/config.json`; the
sensitive notification topic lives in a separate
`~/.autosip/notification_config.json` that is `.gitignore`d.

- **Return needle to origin when closing the application**
  (default on) — the close handler drives both motors back to
  `(0, 0)` and tares before the window goes away.
- **Skip inter-sample purge** (default off) — bypass the
  inter-sample purge modal between samples.
- **Inter-sample purge protocol** — *Water only* (default) or
  *Decontamination*.
- **Plate orientation** — *Portrait* (default on fresh installs;
  plate rows on the X-axis) or *Landscape* (plate columns on the
  X-axis). The origin `(0, 0)` is always the upper-left mechanical
  limit and Manual jog directions are fixed regardless of
  orientation. Switching only changes which plate axis maps to
  which motor axis; recalibrate Starting Well and Waste Bin
  positions after a switch.
- **Motor speed mode** — *Slow speed* (default; all moves at the
  fractionation cadence) or *Variable speed* (well-to-well
  dispensing stays slow but transit moves — waste-bin approach,
  return to origin, plate swaps, Manual jogs — speed up by a
  configurable factor of 1.0–5.0).
- **Notifications** — optional supplementary alerts that fire *in
  addition to* the on-screen dialog at every manual-intervention
  point (sample auto-pause, plate full, prime-step complete,
  waste 80%/100%, run complete). Single channel: an ntfy.sh push
  to a user-chosen topic. Network failures are logged and
  swallowed — a failed push never blocks a run. Subscribe to your
  chosen topic in the [ntfy phone app](https://ntfy.sh) to receive
  pushes. **Choose a unique, hard-to-guess topic string — anyone
  who knows it can read your notifications.** A **Send Test
  Notification** button fires the push using the live entry values.

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

### Halting a run

There is no dedicated emergency-stop button. Two operator paths
cover halt needs:

- **Pause** — reversible. Pump off, motors hold position, claim
  stays held. Click again to resume from the same cycle phase.
- **End Run** — one-way. Three-button dialog: Cancel stays in the
  run; **Don't Save** leaves only the raw `system.start.state.json` +
  `log.csv` on disk; **Save and End** also writes `end_*.json` +
  `summary_*.md`. Use **Don't Save** for the planned-interruption
  halt path.

The application's one autonomous emergency path is the waste-bin
overflow lockdown: when the running waste-volume estimate reaches
the configured maximum, all pump activity halts and a
`waste_hardstop` row is appended to `log.csv`. Empty the container
and click **Reset** next to the flask icon to clear the lockdown.

### Logging output

Each run writes a directory under `logs/` in the working directory:

```
logs/
└── {project}/
    └── {timestamp_start}_{sample_id_at_start}/
        ├── system.start.state.json
        ├── log.csv
        ├── end_{end_timestamp}.json
        ├── summary_{end_timestamp}.md
        └── summary_{plate_id}_{end_timestamp}.md
```

- **`system.start.state.json`** — captured at run start: project, sample ID,
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
  - `prime_auto` / `prime_manual_ext` — pre-fractionation
    automatic prime cycle and each Space-toggle extension.
  - `purge_wash` / `purge_clear` / `purge_bleach` / `purge_prime`
    — inter-sample purge phase cycles; `_ext{N}` for extensions.
  - `sysclean_bleach` / `sysclean_soak` / `sysclean_rinse1` /
    `sysclean_rinse2` — System Clean phases; soak duration is
    actual elapsed seconds.
  - `waste_autopause` / `waste_hardstop` / `waste_reset` — 80%
    advisory, 100% hard stop, operator Reset.
  - `checklist_skipped` — operator clicked Skip Checklist
    (Expert) on a purge or plate-swap dialog.

  See [docs/logging_reference.md](docs/logging_reference.md) for
  the complete column-by-column and status-by-status reference.
- **`end_{end_timestamp}.json`** — written only if you choose
  "Yes, save" at End Run. Final status (`completed`,
  `manual_abort`, or `emergency_stopped`), wells completed,
  wells planned, actual total time, list of plates used.
- **`summary_{end_timestamp}.md`** — human-readable run summary.
- **`summary_{plate_id}_{end_timestamp}.md`** — one per plate
  used. Filtered slice of the run summary suitable for attaching
  to the physical plate during downstream processing.

`system.start.state.json` and `log.csv` are written **continuously during the
run**; the three `_{end_timestamp}` files are written **only when
End Run is confirmed with "Yes, save"**. If you choose "No,
discard," the system.start.state.json and log.csv remain on disk but no
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
