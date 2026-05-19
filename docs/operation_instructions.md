# 6 Operation Instructions

autoSIP (version 0.2.0) is a Python/Tkinter graphical controller for the
Raspberry-Pi-based isopycnic gradient fractionator described in earlier
sections. This section walks through setup, the three operating modes,
five common workflows, the run-logging output, and the safety-stop
mechanism.

## 6.1 General Setup

Clone the repository and install the Python dependencies into a virtual
environment:

```bash
git clone https://github.com/RoliWilhelm/autoSIP-controller.git
cd autoSIP-controller
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Before each session, **park the dispensing carriage at the upper-left
mechanical limit** of the lead screws — gently slide both axes against
their stops by hand while the autoSIP is powered off. This physical
position becomes the software's coordinate origin `(0, 0)` once the
application launches.

The autoSIP supports two pumps connected to a single Digital Loggers
IoT relay on Raspberry Pi GPIO 5:

- The **Razel R-200 syringe pump** is used for fractionation runs.
- The **Adafruit 3910 peristaltic pump** is used for purging and line
  cleaning.

Only one pump is wired into the relay outlet at a time. The operator
manually swaps the pump connection between fractionation and purging
operations. The software exposes two semantic pump labels —
**Fractionate** and **Purge** — that both drive the same relay; a
confirmation dialog before each activation reminds the operator which
physical pump should be wired in for the chosen operation. While one
semantic claim is active, the other's button is disabled across the UI.

Launch the application:

```bash
python main.py
```

The window opens in **Automated** mode by default. Three mode tabs at
the top of the window (**Automated**, **Manual**, **Cleaning**) highlight
the active mode in the accent color; click any tab to switch directly.
If a fractionation run is paused mid-cycle, the application asks for
confirmation before switching modes.

![Figure: autoSIP main window in Automated mode](figures/main_window.png)

## 6.2 Software Interface

### 6.2.1 Automated Mode

Automated mode is the main fractionation workflow. The window is laid
out top-to-bottom in four input sections, followed by the Begin
Fractionation button and the well-plate progress display. A row of
five run-control buttons sits at the top-right of the frame.

![Figure: Automated mode panel](figures/automated_mode.png)

**Run Parameters.** Identifying information and the per-well volume:

- **Project name** — required identifier (1–64 characters, drawn from
  letters, digits, `.`, `_`, `-`). Becomes the top-level folder under
  `logs/`. Editing the Project mid-run prompts for confirmation: the
  new value applies to subsequent log rows, but files already written
  keep their original Project name.
- **Sample ID** — required identifier with the same character class.
  Identifies the source tube. *Commonly edited mid-run* — update Sample
  ID before clicking Continue to Next Sample after a tube swap.
- **Plate ID** — required identifier. Identifies the physical plate
  currently on the stage. The plate-swap dialog auto-increments the
  trailing integer of this value (e.g., `Plate-1` → `Plate-2`); first-
  launch default is `Plate-1`.
- **Number of fractions** — total fractions to dispense per sample,
  *including discards*. Bounded `[1, 384]`; the application enforces an
  additional `≤ rows × cols` check at run start once labware is loaded.
- **Discard fractions** — number of initial fractions sent to the waste
  bin before plate collection begins. Bounded `[0, N − 1]`. Set to `0`
  to skip the discard phase entirely.
- **Volume per well (cc)** — float in `[0.001, 5.0]`. The pump runs for
  `volume / pump_rate` seconds per well.

**Plate Parameters.** Labware geometry and positions:

- **Load well plate file** — at the top of the section. Browse for an
  Opentrons-format JSON file; the application reads `ordering`,
  `dimensions`, and per-well coordinates, then populates the rows,
  columns, well-width, and starting-point fields below.
- **Number of rows** — 1–16.
- **Number of columns** — 1–24.
- **Well width (cm)** — center-to-center well spacing, `[0.1, 5.0]`.
- **Starting point (x-axis)** — X position of well A1 in cm,
  `[0.0, 20.0]`.
- **Starting point (y-axis)** — Y position of well A1 in cm,
  `[0.0, 15.0]`.
- **Waste bin: table position** — X position of the waste container in
  cm, `[0.0, 20.0]`. Required when Discard fractions > 0. The
  application warns at run start if this position appears to fall
  inside the plate footprint.
- **Waste bin: carriage position** — Y position of the waste container
  in cm, `[0.0, 15.0]`.

**Pump.** Flow control:

- **Pump rate (cc/hr)** — float in `[0.1, 600.0]`. Match the value to
  the syringe pump's gear-set or the peristaltic pump's calibration.
- **Drip wait time (s)** — float in `[0.0, 60.0]`, default `1.0`. The
  dwell time *after* the pump shuts off and *before* the carriage moves
  to the next well, so a dispensed drop has time to detach cleanly.
  Longer waits improve volume consistency; shorter waits speed up the
  run.

**Run controls** (top-right of the Automated frame):

- **Return to Start Coords** — moves the needle to the plate-start
  coordinates (well A1 of the loaded labware).
- **Pause** — pauses an in-progress run; pump off, motors hold position.
  The button label flips to **Resume** while paused.
- **Continue to Next Sample** — enabled after the auto-pause at "Total
  reached". Starts a new sample series: an optional discard phase, then
  collection at the next available plate well.
- **Continue to Next Plate** — enabled after the auto-pause at "Plate
  full". Opens the plate-swap dialog.
- **End Run** — finalizes the run with a save/discard confirmation.

**Begin Fractionation** is the primary action button at the center of
the strip, flanked by a centrifuge-tube icon on the left and the
bimodal density-distribution icon on the right. Click to validate
inputs, confirm the run summary, and start the state machine.

**Progress display.** A to-scale rendering of the loaded labware shows
each well's state during the run:

- A gray, unmarked well = not yet visited.
- A numbered, colored well = collected fraction. The number is the
  fraction index within the current sample, **including discards**.
  With Discard fractions = 2, the first plate well of a sample is
  numbered **3**.
- Each sample uses a distinct color from the eight-entry Okabe–Ito
  colorblind-safe palette (orange, sky blue, bluish green, yellow,
  blue, vermillion, reddish purple, black). Runs with more than eight
  samples cycle back to the start of the palette.

Hovering over a filled well surfaces a tooltip with the Sample ID and
the fraction's sequence within that sample.

### 6.2.2 Manual Mode

Manual mode supports free needle positioning, independent pump
operation, and calibration of the plate-start and waste-bin
coordinates. It does NOT carry the fractionation-sequence inputs;
those live in Automated mode.

![Figure: Manual mode panel](figures/manual_mode.png)

**Jog Controls.** Directional movement:

- Four directional buttons: **▲ Y+**, **◀ X−**, **X+ ▶**, **▼ Y−**.
- **Step** selector — three radio buttons: `0.1 mm`, `1 mm`, `10 mm`
  (default `1 mm`). The step size translates directly into the cm
  units used in Automated mode (a 10 mm jog covers the same physical
  distance as typing `1.0` cm into a Starting point field).
- **Home** — moves both motors to origin `(0, 0)` and re-zeros the
  software's tracked angle counters. The Position readout then
  reads exactly `Position: X = 0.000 cm, Y = 0.000 cm`. Stepper
  motors can lose steps over a long session; periodically re-park
  the carriage against the upper-left mechanical limit by hand and
  click Home to recalibrate.
- **Position readout** — `Position: X = {x:.3f} cm, Y = {y:.3f} cm`,
  updated after every jog and Home action.

Soft travel limits are enforced on every jog: the X axis is bounded
`[0, 20]` cm and the Y axis `[-15, 0]` cm. With this Y range, pressing
**▼ Y−** from origin moves the needle into the plate-side travel
range; pressing **▲ Y+** from origin is refused with a status-bar
message. The Y readout therefore shows a **negative** value as the
needle moves below the upper-left origin.

**Pump Controls.** Two pump buttons:

- **Fractionate** — toggles the relay; the button label reads
  `Fractionate: OFF` or `Fractionate: ON`.
- **Purge** — toggles the relay; reads `Purge: OFF` or `Purge: ON`.

The first time either pump is turned on per Manual-mode visit, a
confirmation dialog reminds the operator to verify which pump is wired
to the relay outlet. Both buttons control the same physical relay; the
button for the non-claimant pump is disabled while the other has the
claim.

**Space-bar shortcut.** Pressing the space bar toggles whichever pump
was used most recently. A small `(Space)` hint label sits next to the
bound button so the active target is visible at a glance. The shortcut
is active **only in Manual mode** and **only when no text-entry widget
has keyboard focus** (so space still types a literal space character
inside name fields). The most-recently-used pump persists across
application restarts in `~/.autosip/config.json`.

### 6.2.3 Cleaning Mode

Cleaning mode strips Automated mode down to two inputs and two actions
for flushing the fluid path between sample types.

![Figure: Cleaning mode panel](figures/cleaning_mode.png)

- **Waste bin: table position (cm)** and **Waste bin: carriage position
  (cm)** — the same two values that appear in Automated mode's Plate
  Parameters → Waste bin section. Edits in either mode propagate
  automatically via shared App-level variables.
- **Move to Waste Bin** — jogs the needle to the waste-bin coordinates.
- **Purge** — toggles the relay (same semantics as Manual mode's Purge
  button; same confirmation dialog on first activation).

A typical cleaning cycle: switch to Cleaning mode, click **Move to
Waste Bin**, click **Purge**, run the pump until the fluid path is
clear, then click **Purge** again to stop.

## 6.3 Common Workflows

The following five workflows cover the operations most autoSIP users
will perform. Work through §6.3.1 first if the rig has not yet been
calibrated; every other workflow assumes the plate-start and
waste-bin coordinates are known.

### 6.3.1 Calibrating plate-start and waste-bin coordinates

This procedure produces the two pairs of coordinates — *Starting
point* and *Waste bin* — that autoSIP needs to drive the carriage to
well A1 and to the waste container.

![Figure: Manual mode jog controls and Position readout](figures/calibration.png)

1. **Park the carriage at the upper-left mechanical limit.** With the
   autoSIP powered off, gently slide the carriage by hand until it
   rests against the upper-left lead-screw stops on both axes. This
   physical position becomes origin `(0, 0)` once you launch the
   software.

2. **Power on and launch autoSIP.**

   ```bash
   python main.py
   ```

   The window opens in Automated mode by default. Click the **Manual**
   tab in the header to switch.

3. **Verify the Position readout shows `X = 0.000 cm, Y = 0.000 cm`.**
   If it does not (for example because a previous session left the
   software in some other state), click **Home** in the Jog Controls
   section. Home moves both motors to origin and re-zeros the
   software's tracked angle counters.

4. **Place the labware on the stage** in its intended position.
   Tighten any clamps so the plate cannot shift during a run.

5. **Jog the needle to well A1.** Use the **▲ Y+** / **◀ X−** /
   **X+ ▶** / **▼ Y−** buttons. Start with the **10 mm** step to move
   quickly, then switch to **1 mm** as you get close, and to
   **0.1 mm** for final centering. The Position readout updates after
   every jog.

6. **Record the X and Y values from the Position readout.** As the
   needle moves down and to the right from origin, the X value
   increases (positive) and the Y value decreases (negative, because
   Manual mode's Y axis is in motor-frame and the valid travel range
   from origin is `[-15, 0]` cm). Note the magnitudes — these are
   the absolute distances from origin to well A1.

7. **Enter the absolute (positive) values in Automated mode.** Switch
   to **Automated** and enter the magnitudes from step 6 in Plate
   Parameters → `Starting point (x-axis)` and `Starting point
   (y-axis)`. Automated mode's Y validator accepts values in
   `[0.0, 15.0]` cm.

8. **Repeat the jog process for the waste container.** Return to
   Manual mode, jog the needle until it sits above the waste
   container's opening, and record the magnitudes. Enter them in
   Automated mode under `Waste bin: table position` and
   `Waste bin: carriage position`. (Cleaning mode shares these two
   fields, so editing in either mode updates both.)

9. **Save the calibration.** File → *Save current as profile…* writes
   the field values to `~/.autosip/profiles/{name}.json` so you can
   reload them later without re-jogging. Most-recently-used values
   also persist automatically across launches via
   `~/.autosip/config.json`.

**Drift over a long session.** autoSIP does not automatically detect
lost stepper steps. Periodically re-park the carriage against the
upper-left mechanical limit by hand and click **Home** in Manual mode
to re-zero the counters.

### 6.3.2 Fractionating multiple samples on a single plate

When several ultracentrifuge tubes' worth of fractions all fit on one
96-well plate, run them sequentially in a single session. The total
of (collected wells + discards) × number of samples must be ≤ plate
capacity for this workflow; if it exceeds capacity, follow §6.3.3.

For this example: five samples, 18 collected wells per sample with
two discard fractions each, total 100 dispense cycles = 90 collected
+ 10 discarded; fits within 96 plate wells.

![Figure: Begin Fractionation confirmation summary](figures/begin_confirm.png)

1. **Set Run Parameters** for the first sample:

   - **Project name** — e.g., `MyStudy_2026_Q2`. Persists across all
     samples in this run.
   - **Sample ID** — e.g., `Tube-A12`. Update between samples.
   - **Plate ID** — e.g., `Plate-1`.
   - **Number of fractions** — `20` (= 2 discards + 18 plate wells).
   - **Discard fractions** — `2`.
   - **Volume per well** — your per-fraction volume in cc, e.g., `0.22`.

2. **Verify Plate Parameters and Pump parameters** are correct from
   your calibration (§6.3.1) and your pump's gear-set or calibration
   table.

3. **Click Begin Fractionation.** A summary dialog lists every
   parameter and the estimated run time. Confirm to start.

4. **The discard phase runs first.** The needle moves to the waste bin
   and dispenses the two discard fractions there. The progress
   display header reads `Discard phase: N of 2 fractions dispensed
   to waste`.

5. **Collection begins at well A1.** The first plate well is labeled
   **3** (= 2 discards + 1) in the sample's color from the Okabe–Ito
   palette. The application snakes through the plate column-by-column,
   filling wells until the per-sample target is reached.

6. **autoSIP auto-pauses at "Total reached".** The status bar reads
   `Total of {N} fractions reached. Click End Run or Continue to Next
   Sample.` The run-control buttons update: **Continue to Next
   Sample** and **End Run** become enabled.

7. **Physically swap the source tube** on the ultracentrifuge or
   fraction collector to the next sample.

8. **Update Sample ID** in the Run Parameters section, e.g.
   `Tube-A12` → `Tube-A13`. Do not change Project, Plate ID, or any
   other field — they apply to the whole run.

9. **Click Continue to Next Sample.** If you forgot to change Sample
   ID, the application prompts: *"Sample ID is still 'Tube-A12'. Did
   you mean to update it for the new sample? Continue anyway?"*

10. The new sample's discard phase runs at the waste bin, then
    collection resumes at the next available plate well in a
    **different color** (Okabe–Ito index 2 instead of 1).

11. Repeat steps 6–10 for each additional sample.

12. **When the last sample finishes, click End Run.** The save/discard
    confirmation appears: *"Save the run logs for project '…' /
    sample '…'?"* Click **Yes** to write `end_*.json`,
    `summary_*.md`, and `summary_Plate-1_*.md` to the run directory.

After End Run, the progress canvas clears, all run counters reset to
zero, and the run-control buttons return to their idle state. Click
Begin Fractionation again to start a fresh run with new inputs — no
application restart is required.

### 6.3.3 Running a sample whose fractions span multiple plates

When the total work exceeds a single plate's capacity, autoSIP
automatically pauses at the last well of the current plate and
prompts for a plate swap. The same sample's fractions continue on
the new plate in the same color, with the fraction counter
continuing from where it left off.

For this example: eight samples, 18 collected wells each with two
discards — 144 total collected wells, requiring two 96-well plates.

![Figure: Plate Full dialog with swap steps](figures/plate_full_dialog.png)

1. Set Run Parameters and click Begin Fractionation as in §6.3.2.

2. **Samples 1–5 complete on Plate-1** (5 samples × 18 wells = 90
   wells filled; 6 wells remain on the plate). After each sample's
   auto-pause, update Sample ID and click **Continue to Next
   Sample**.

3. **Click Continue to Next Sample for sample 6.** Because only 6
   plate wells remain but the new sample needs 18, the application
   shows an informational notice: *"This sample requires 18 wells.
   Only 6 remain on plate Plate-1. autoSIP will prompt for a plate
   swap when the current plate fills."* Click OK to acknowledge.
   The discard phase runs, then collection begins at the next
   available well of Plate-1.

4. **autoSIP auto-pauses at "Plate full"** when sample 6's 6th
   collected well lands at the last well of Plate-1. The
   **Continue to Next Plate** button enables; **Continue to Next
   Sample** stays disabled (sample 6 is not complete).

5. **Click Continue to Next Plate.** A modal dialog opens with five
   numbered steps:

   1. Remove the current plate (`Plate-1`) from the stage and store
      it for downstream processing.
   2. Return the dispensing needle to home position — click the
      **Move Needle to Home** button. The button changes to
      `✓ Needle at home` once the carriage reaches origin.
   3. Place a new plate on the stage.
   4. Enter the new Plate ID — pre-filled as `Plate-2` (the
      application auto-increments the trailing integer; you can
      override).
   5. Click **Continue** to resume fractionation.

   The dialog cannot be dismissed via the window's close box — you
   must either click **Continue** or **Cancel Run**.

6. **After Continue, autoSIP moves to well A1 of Plate-2** and
   resumes sample 6's collection. The fraction counter continues
   from where it left off: if sample 6 was at fraction 8 on Plate-1's
   last well, sample 6 starts at fraction 9 on A1 of Plate-2 in the
   same color.

7. Sample 6's remaining wells finish on Plate-2; samples 7 and 8
   collect on Plate-2 via Continue to Next Sample as in §6.3.2.

8. **Click End Run when the last sample finishes.** Confirm "Yes,
   save."

The output directory contains:

```
end_{end_timestamp}.json
summary_{end_timestamp}.md
summary_Plate-1_{end_timestamp}.md
summary_Plate-2_{end_timestamp}.md
```

The per-plate summary files are filtered slices of the full run
summary — print them out and attach each one to its physical plate
as the plates move to downstream processing. The `log.csv` for this
run includes a `plate_swap` breadcrumb row at each swap (with
`well_id` values `plate_swap_1`, `plate_swap_2`, …) and a `resume`
breadcrumb row at each Continue to Next Sample.

### 6.3.4 Pause and end controls — when to use which

autoSIP has five interruption controls. They look similar in the GUI
but differ in when they are available, what they do to the run, and
what files they produce.

| Button                      | Available when                                                                 | Effect                                                                                                          | Resumable?                                                       | What it writes to `log.csv` / disk                                                                |
| --------------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| **Pause** / **Resume**      | During a run (pump, wait, or move phase)                                       | Cancels the in-flight `after()` task; pump relay off; motors hold position. Claim stays held.                   | Yes — click the same button (label becomes **Resume**)           | One `resume` breadcrumb row on Resume, if currently in the collection phase                       |
| **Continue to Next Sample** | After auto-pause at "Total reached"                                            | Starts a new series: increments series_index, runs the discard phase, then collects at the next available well. | Continues the run                                                | `resume` breadcrumb row                                                                           |
| **Continue to Next Plate**  | After auto-pause at "Plate full"                                               | Opens the plate-swap dialog. After Continue, moves the carriage to A1 of the new plate and resumes.             | Continues the run after the dialog                               | `plate_swap` breadcrumb row                                                                       |
| **End Run**                 | Any active run state (running, paused, total reached, plate full)              | Save-or-discard prompt; pump off; motors released; visuals reset; FractionatorState counters zeroed.            | No — the run terminates                                          | On "Yes, save": `end_{ts}.json`, `summary_{ts}.md`, `summary_{plate_id}_{ts}.md`. On "No": none.  |
| **Terminate Run**           | Visible in Automated mode (bottom-right of the status bar, red octagon button) | Hard-halt: pump off, motors released, run-control buttons disabled. Confirmation dialog required.               | After clicking **Return to Start Coords** the controls re-enable | In-flight entry stamped `emergency_stopped` in `log.csv`; `end.json` + `summary.md` written       |

When to use each:

- **Quick interruption** (operator break, momentary distraction):
  **Pause**. Cleanest interrupt. Click again to resume; the cycle
  picks up at exactly the same point.
- **End-of-tube — swap source tubes**: **Continue to Next Sample**.
  autoSIP fires this auto-pause on its own when the per-sample
  fraction target is reached; update Sample ID and click Continue.
- **End-of-plate** (autoSIP triggers this automatically):
  **Continue to Next Plate**, then follow the five-step swap dialog.
- **Intentional finish** at the end of a session: **End Run**, then
  "Yes, save" to keep the end/summary files (or "No, discard" if the
  run was a test).
- **Safety emergency** (smell, collision, fluid leak):
  **Terminate Run**. Stops the pump and motors immediately on
  confirming the dialog. After the rig is verified safe, click
  **Return to Start Coords** to re-enable controls.

Pause is reversible and silent; Continue advances the run; End Run
finishes cleanly; Terminate Run is the heavy hammer and writes
`emergency_stopped` to the log so the interruption is unmistakable in
the audit trail.

### 6.3.5 Cleaning between sample types

Use Cleaning mode to flush the fluid path between sample types — for
example, after running an undeuterated control and before running a
deuterated one, to clear residual buffer or DNA from the tubing.

1. **Disconnect the Razel R-200 syringe pump from the relay outlet
   and connect the Adafruit 3910 peristaltic pump.** Only one pump
   is wired to the relay at a time; the operator does the swap
   manually.

2. **Switch the autoSIP GUI to the Cleaning tab.**

3. **Confirm the waste-bin coordinates** in the Cleaning panel. These
   are the same two values shown in Automated mode's Plate Parameters
   → Waste bin section — editing either set updates the other. If you
   have already calibrated the waste-bin position via §6.3.1, no
   further input is needed here.

4. **Click Move to Waste Bin.** The needle moves to the waste-bin
   coordinates.

5. **Click Purge.** A confirmation dialog reminds you that the
   peristaltic pump should be plugged in. Confirm to power on the
   relay.

6. **Wait for the line to clear.** Watch the tubing through any
   transparent sections until clean cleaning fluid is flowing.

7. **Click Purge again to stop.**

8. **Disconnect the peristaltic pump and reconnect the syringe pump
   before starting the next Automated run.** The software cannot
   detect which pump is wired in — that is the purpose of the
   Fractionate/Purge confirmation dialogs.

## 6.4 Logging Output

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

The `{timestamp_start}` and `{end_timestamp}` are ISO 8601 datetimes
with colons replaced by hyphens (e.g., `2026-05-19T14-22-01`) so the
filenames are portable across operating systems and multiple End Runs
in the same session never overwrite each other.

**metadata.json** — captured at run start. Contains the project name,
Sample ID at start, Plate ID at start, the parameters block (rows,
cols, well width, pump rate, drip wait time, volume per well,
waste-bin coordinates, plate-start coordinates, number of fractions,
discard fractions), the labware file path, the inline labware JSON
contents, and the estimated total run time.

**log.csv** — one row per fraction, plus breadcrumb rows at sample
and plate boundaries. Columns:

```
project, sample_id, plate_id,
well_id, plate_x, plate_y,
dispense_start_iso, dispense_end_iso,
dispense_duration_s, status
```

Status values:

- `completed` — plate well dispense finished cleanly.
- `discarded` — discard cycle finished cleanly.
- `resume` — breadcrumb at the start of a new series (Continue to
  Next Sample) or after Pause + Resume during the collection phase.
- `plate_swap` — breadcrumb at the start of a new plate (Continue to
  Next Plate); the `well_id` for these rows is `plate_swap_1`,
  `plate_swap_2`, etc.
- `emergency_stopped` — the run was terminated while this well or
  discard cycle was mid-dispense.

**end_{end_timestamp}.json** — written only if you choose "Yes, save"
at End Run. Contains the final status (`completed`, `manual_abort`,
or `emergency_stopped`), wells completed, wells planned, actual total
time, and the list of plates used.

**summary_{end_timestamp}.md** — human-readable run summary.

**summary_{plate_id}_{end_timestamp}.md** — one file per plate the
run touched. Filtered slice of the run summary, suitable for printing
and attaching to the physical plate as it goes to downstream
processing.

`metadata.json` and `log.csv` are written **continuously during the
run**; the three `_{end_timestamp}` files are written **only when End
Run is confirmed with "Yes, save"**. If you choose "No, discard," the
metadata.json and log.csv remain on disk but no end/summary files are
produced — useful when the run was a test or calibration you do not
want to archive.

## 6.5 Safety Controls

The **Terminate Run** button is a red octagonal button in the
bottom-right corner of the status bar, visible only in Automated
mode. It serves as the application's emergency-stop control.

![Figure: Terminate Run button in the status bar](figures/terminate_run.png)

Clicking Terminate Run opens a confirmation dialog
(*"Are you sure that you wish to stop the whole run?"*). On
confirmation, the application:

- Cancels any pending pump or move callback.
- Turns the relay off and releases the pump claim.
- Releases both stepper motors so they do not overheat.
- Sets the in-flight log entry to `emergency_stopped` if a dispense
  was in progress.
- Writes `end.json` + `summary.md` with `final_status =
  "emergency_stopped"`.
- Offers to save a plate-state snapshot to a `.txt` file before
  clearing the progress view.
- Disables every run-control button until the operator clicks
  **Return to Start Coords**, which re-enables the controls and
  clears the terminated state.

Use Terminate Run for safety emergencies (smell, collision, fluid
leak) where an immediate hardware stop matters more than a clean
log. For planned interruptions (operator break, tube swap,
troubleshooting), use **Pause** instead — Pause is reversible and
does not write any `emergency_stopped` rows.

There is no global keyboard shortcut for Terminate Run; the only
keyboard shortcut in the application is **Space**, which toggles the
most-recently-used pump in Manual mode.
