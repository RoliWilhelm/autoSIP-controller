# 6 Operation Instructions

autoSIP (version 1.0.0) is a Python/Tkinter graphical controller for the
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

Before each session, **park the dispensing carriage at the
orientation-specific mechanical-limit corner** of the lead screws —
gently slide both axes against their stops by hand while the autoSIP
is powered off. In **portrait** orientation (Tools → Preferences
default) this corner is the **bottom-left** mechanical limit; in
**landscape** orientation it is the **upper-left**. This physical
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

autoSIP's window carries three mode tabs at the top — **Automated**,
**Manual**, **Cleaning**. The status bar (bottom of the window)
shows the current pump state and a waste-bin fill indicator labeled
*Waste:* with a flask icon + Reset button. Status messages during
an Automated run appear above the well-plate canvas (next to the
plate they describe); outside a run the status-bar middle area
reads "System idle." Switching modes mid-run is safe — the
fractionation state machine continues to tick in the background
while the operator visits Manual or Cleaning mode, and the plate-
progress canvas re-paints from its current state on return to
Automated.

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
- **Volume per well (mL)** — float in `[0.1, 2.0]`. The pump runs for
  `volume / pump_rate` seconds per well.

**Bulk Sample Submission.** Preload Sample ID, Plate ID, fraction
counts, and volume for an entire multi-sample session from a CSV so
the operator does not have to retype Run Parameters between samples.
Two buttons:

- **Generate Template** — writes a starter CSV with header comments
  and example rows. Only `sample_id` is required per row; blank
  optional cells inherit the current Run Parameters values at the
  moment of import.
- **Import Submission** — parses + validates the CSV; on success,
  Run Parameters auto-populate from row 1 and lock (except Project
  name). A third button — **Exit Bulk Mode** — appears once a
  submission is loaded; clears the loaded samples after confirmation.

Full workflow: §6.3.7.

**Plate Parameters.** Labware geometry and positions:

- **Load labware specs** — at the top of the section. Browse for an
  Opentrons-format JSON file; the application reads `ordering`,
  `dimensions`, and per-well coordinates, then populates the rows,
  columns, well-width, and starting-point fields below.
- **Number of rows** — 1–16.
- **Number of columns** — 1–24.
- **Well width (cm)** — center-to-center well spacing, `[0.1, 5.0]`.
- **Starting well position (x-axis)** — X position of well A1 in cm,
  `[0.0, 20.0]`.
- **Starting well position (y-axis)** — Y position of well A1 in cm,
  `[0.0, 15.0]`.
- **Waste bin position (x-axis)** — X position of the waste container
  in cm, `[0.0, 20.0]`. Required when Discard fractions > 0. The
  application warns at run start if this position appears to fall
  inside the plate footprint.
- **Waste bin position (y-axis)** — Y position of the waste container
  in cm, `[0.0, 15.0]`.

**Fractionation Pump Parameters.** Settings for the Razel R-200
syringe pump used during fractionation (column 0, below Run
Parameters):

- **Pump rate (mL/hr)** — float in `[0.1, 600.0]`. Match the value to
  the syringe pump's gear-set.
- **Drip wait time (s)** — float in `[0.0, 60.0]`, default `1.0`. The
  dwell time *after* the pump shuts off and *before* the carriage moves
  to the next well, so a dispensed drop has time to detach cleanly.
  Longer waits improve volume consistency; shorter waits speed up the
  run.

**Cleaning Parameters.** Settings for the Adafruit 3910 peristaltic
pump used during inter-sample purges, manual purges, and Cleaning
Purge (column 1, below Plate Parameters):

- **Purge time (s)** — float in `[1.0, 600.0]`, default `30.0`. The
  per-phase duration of the inter-sample purge (see §6.3.2). Use
  Cleaning mode's *Purge Time Calibration Tool* panel (§6.2.3) to measure
  the right value for your tubing geometry.
- **Peristaltic pump rate (mL/min)** — float in `[1.0, 200.0]`,
  default `100.0`. Used by the waste-bin estimator (see §6.5) to
  convert purge-phase pump-on time into a volume contribution.
  Calibrate this against your physical hardware for accurate
  estimates.
- **Max waste bin volume (mL)** — float in `[10.0, 5000.0]`, default
  `250.0`. Capacity of your waste container. autoSIP warns at 80 %
  and halts all pump activity at 100 % to prevent overflow. The
  estimate is based on configured pump rates × pump-on time, not a
  real measurement; the *Reset* button in the status bar is the
  ground-truth mechanism after a physical empty.

The *Skip inter-sample purge* behavioral preference moved to **Tools
→ Preferences** (§6.2.4) so it persists across launches alongside
*Return needle to origin on exit*.

**Run controls** (top-right of the Automated frame):

- **Return to Origin** — moves both motors to physical `(0, 0)` and
  tares the software counters. Same action as Manual mode's
  Return to Origin button (the two are redundant by design). Also
  works while a run is paused: the first click in a pause captures
  the current motor position so the matching Resume can drive the
  needle back and pop a Confirm Calibration dialog. This is the
  mid-run recalibration entry point (see §6.3.6).
- **Return to Start Well** — moves the needle to the plate-start
  (well A1) coordinates from Plate Parameters. Enabled only while
  idle; disabled mid-run since interrupting the snake-path would
  lose the operator's place.
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
  distance as typing `1.0` cm into a Starting well position field).
- **Return to Origin** — sits above the directional pad. Moves
  both motors to origin `(0, 0)` and re-zeros the software's
  tracked angle counters. The Position readout then reads exactly
  `Position: X = 0.00 cm, Y = 0.00 cm`. Stepper motors can lose
  steps over a long session; periodically re-park the carriage
  against the orientation-appropriate mechanical-limit corner
  (upper-left in landscape, bottom-left in portrait) by hand and
  click Return to Origin to recalibrate. A fresh app launch also reads
  `(0.00, 0.00)` — the seating wiggle that initializes lead-screw
  backlash is tared immediately after.
- **Position readout** — `Position: X = {x:.2f} cm, Y = {y:.2f} cm`,
  updated after every jog and Return-to-Origin action. All coordinate displays
  across the GUI use two decimal places (0.01 cm = 0.1 mm
  precision); user-typed values in the Automated-mode coordinate
  entries are normalized on focus-out (`12.6` → `12.60`).

Soft travel limits are enforced on every jog: the X axis is bounded
`[0, 20]` cm and the Y axis `[-15, 0]` cm. With this Y range, pressing
**▼ Y−** from origin moves the needle into the plate-side travel
range; pressing **▲ Y+** from origin is refused with a status-bar
message. The Y readout shows a **negative** value as the needle moves
away from origin. Which compass direction the +Y / −Y buttons
correspond to physically depends on plate orientation: in
**landscape** orientation +Y points UP (toward the upper-left
origin); in **portrait** orientation the carriage motor's reverse
flag inverts, so +Y points the opposite way relative to the chosen
origin corner. Hover the Y+ / Y− buttons in Manual mode for the
orientation-specific direction text.

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

- **Waste bin position (x-axis)** and **Waste bin position (y-axis)**
  — the same two values that appear in Automated mode's Plate
  Parameters → Waste bin section. Edits in either mode propagate
  automatically via shared App-level variables.
- **Move to Waste Bin** — jogs the needle to the waste-bin coordinates.
- **Purge** — toggles the relay (same semantics as Manual mode's Purge
  button; same confirmation dialog on first activation).
- **System Clean** — runs an on-demand four-phase decontamination
  routine (bleach fill → soak → water rinse 1 → water rinse 2). More
  stringent than the inter-sample purge: the bleach is held static in
  the line for a configurable soak period before rinsing. Use at
  session start or during a paused automated run; the button is
  disabled only during active (non-paused) dispensing. System Clean
  intentionally stops after the second water rinse — priming with
  sample solution is the job of the pre-fractionation prime workflow
  (§6.3.1) or the inter-sample purge's final phase (§6.3.4).

A typical cleaning cycle: switch to Cleaning mode, click **Move to
Waste Bin**, click **Purge**, run the pump until the fluid path is
clear, then click **Purge** again to stop. For a stringent end-of-
session decontamination, click **System Clean** instead and step
through the four-phase routine.

**Purge Time Calibration Tool** — a sub-panel below the manual
Purge controls measures how long wash takes to fully replace one
tubing volume so the Automated mode *Purge time* parameter (§6.2.1)
reflects your actual hardware:

1. Place the inlet line in your wash solution container.
2. Click **Start**. The pump powers on (after the standard
   confirmation dialog) and an *Elapsed* timer ticks every 100 ms.
3. Watch the outlet. Click **Stop** the moment wash solution first
   appears at the outlet — this represents one full tubing volume.
   The measured value is shown next to *Measured*.
4. Click **Save as Purge Time** to write the measured value to
   Automated mode's Purge time entry. The Save button is enabled
   only when the measured value falls within `[1.0, 600.0]` s.

Use **Reset** to clear the measurement state between attempts. The
calibration measurement is *not* recorded in `log.csv` — it is a
setup operation, not a fractionation event.

### 6.2.4 Preferences

Three persistent behavioral preferences live under **Tools →
Preferences**. All are stored at the top level of
`~/.autosip/config.json` and apply across launches.

- **Return needle to origin when closing the application**
  (default `True`) — when the operator clicks the window's close
  button, the application drives both motors to `(0, 0)` and re-tares
  the position counters before the window goes away. Skipped during
  an active run and while the waste-bin lockdown is active — those
  signal a hardware issue that warrants inspection before any
  further motion.

- **Skip inter-sample purge** (default `False`) — when checked,
  Continue to Next Sample bypasses the three-phase purge workflow
  and goes directly to the new sample's discard + collection.
  Leave unchecked for multi-sample runs to prevent carryover
  between samples; useful for solvent-compatible same-sample-type
  sessions where the purge between tubes is wasted effort.

- **Inter-sample purge protocol** (default *Water only*) — radio
  group selecting the workflow used between samples:

  - *Water only (water → sample)* — three phases: sterile water
    flush, air clear, syringe priming.
  - *Decontamination (water → bleach → water → sample)* — five
    phases: sterile water flush, **0.5% sodium hypochlorite
    (bleach) flush**, sterile water rinse, air clear, syringe
    priming. Use when carryover between sample types must be
    eliminated (e.g. between deuterated and undeuterated runs,
    or between projects). *Prepare the 0.5% bleach solution
    fresh on the day of use — dilute hypochlorite degrades
    within 24 hours.*

OK saves all three preferences and applies the new values
immediately (no app restart needed); Cancel discards.

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

1. **Park the carriage at the origin mechanical-limit corner.** With
   the autoSIP powered off, gently slide the carriage by hand until it
   rests against the lead-screw stops on both axes. In **portrait**
   (Tools → Preferences default) the origin corner is the
   **bottom-left**; in **landscape** it is the **upper-left**. This
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
   Parameters → `Starting well position (x-axis)` and `Starting well
   position (y-axis)`. Automated mode's Y validator accepts values in
   `[0.0, 15.0]` cm.

8. **Repeat the jog process for the waste container.** Return to
   Manual mode, jog the needle until it sits above the waste
   container's opening, and record the magnitudes. Enter them in
   Automated mode under `Waste bin position (x-axis)` and
   `Waste bin position (y-axis)`. (Cleaning mode shares these two
   fields, so editing in either mode updates both.)

9. **Save the calibration.** File → *Save current as profile…* writes
   the field values to `~/.autosip/profiles/{name}.json` so you can
   reload them later without re-jogging. Most-recently-used values
   also persist automatically across launches via
   `~/.autosip/config.json`.

**Drift over a long session.** autoSIP does not automatically detect
lost stepper steps. Periodically re-park the carriage against the
orientation-appropriate mechanical-limit corner (upper-left in
landscape, bottom-left in portrait) by hand and click **Home** in
Manual mode to re-zero the counters.

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
   - **Volume per well** — your per-fraction volume in mL, e.g., `0.22`.

2. **Verify Plate Parameters and Pump parameters** are correct from
   your calibration (§6.3.1) and your pump's gear-set or calibration
   table.

3. **Click Begin Fractionation.** A compact confirmation dialog
   opens. It shows the Sample ID and Plate ID prominently (these
   are the parameters most worth a final glance), prompts the
   operator to verify them, and presents a 4-row waste-bin
   projection table (current volume, estimated added this run,
   projected end-of-run, capacity). A ⚠ row appears if the
   projection exceeds capacity. Other run parameters are visible
   in the main window behind the dialog and are not duplicated.
   Click **Begin Fractionation** to start; **Cancel** returns to
   the input fields without launching.

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

10. **The inter-sample purge workflow runs** (unless *Skip inter-sample
    purge* is checked in **Tools → Preferences**, §6.2.4). The needle
    first moves to the waste bin, then the application opens a
    multi-step modal sequence. The phase count depends on the
    *Inter-sample purge protocol* preference: three steps for *Water
    only* (default) or five steps for *Decontamination*.

    Each step leads with a checklist of the physical actions to
    perform. Below the checklist is a pump-toggle status block —
    `Pump: OFF`/`Pump: ON`, `This cycle: X.X s` (current on-period),
    `Total pumping: X.X s` (cumulative for this phase). Press
    **Space** to toggle the pump on/off; the operator decides when
    enough fluid has flowed through. **Continue** is disabled while
    the pump is currently ON and while the checklist is incomplete.

    The button row reads `[Cancel] [Skip Checklist (Expert)]
    [Continue]`. Skip bypasses the checklist gate without ticking
    the boxes and writes an audit row to `log.csv`
    (`status="checklist_skipped"`,
    `well_id="checklist_skipped_purge_phase_{N}_{series}"`).

    **Water only (3 phases):**

    1. **Wash.** Checklist: *Disconnected inlet line from previous
       sample tube* / *Placed inlet line in water container*. Toggle
       the **peristaltic pump** until the tubing reads clean.
    2. **Clear.** Checklist: *Removed inlet line from water
       container* / *Line is in air, nothing dripping*. Toggle the
       **peristaltic pump** to push air through and clear residual
       liquid.
    3. **Prime.** Checklist: *Connected inlet line to the new sample
       tube* / *Connection is secure*. Toggle the **syringe pump**
       to walk fractionation fluid through the tubing until even
       droplets exit the needle — this displaces the air gap left by
       Phase 2 so the dispense pressure is consistent across wells.

    **Decontamination (5 phases):**

    1. **Sterile water flush** (peristaltic) — initial rinse of the
       previous sample's residues.
    2. **Bleach flush** (peristaltic) — toggle through **0.5% sodium
       hypochlorite (bleach) solution** to decontaminate. *Prepare
       the 0.5% bleach solution fresh on the day of use — dilute
       hypochlorite degrades within 24 hours.*
    3. **Sterile water rinse** (peristaltic) — flush thoroughly to
       remove residual bleach before the next sample contacts the
       lines.
    4. **Air clear** (peristaltic) — push air through to clear
       residual liquid.
    5. **Prime** (syringe pump) — same as the Water-only Phase 3.

    Each modal has a **Cancel** button that aborts the workflow and
    returns the run to the auto-paused state (you can click
    *Continue to Next Sample* again to restart from Step 1). Each
    Space-toggle press-on → press-off pair writes its own row to
    `log.csv`: `purge_wash` / `purge_clear` / `purge_bleach` /
    `purge_prime` with `well_id` of the form
    `purge_{phase}_{series}` for the first cycle and
    `purge_{phase}_{series}_cycle{N}` for subsequent toggles. The
    decontamination rinse phase uses `purge_wash` with `well_id`
    suffix `_rinse` to distinguish it from the initial wash.
    `summary.md` reports total seconds and cycle counts per phase.

    If *Skip inter-sample purge* is enabled in Preferences, this
    workflow is bypassed entirely — the new sample's discard phase
    starts immediately after the pre-flight dialogs.

11. **The new sample's discard phase runs at the waste bin**, then
    collection resumes at the next available plate well in a
    **different color** (Okabe–Ito index 2 instead of 1).

12. Repeat steps 6–11 for each additional sample.

13. **When the last sample finishes, click End Run.** A three-button
    confirmation appears (*"Save the logs for project '…' / sample
    '…'?"*): **Save and End** writes `end_*.json`, `summary_*.md`,
    and `summary_Plate-1_*.md` to the run directory; **Don't Save**
    leaves only the raw `metadata.json` + `log.csv` on disk; **Cancel**
    returns to the run without changing state. Enter activates
    Save and End by default; Escape activates Cancel.

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

5. **Click Continue to Next Plate.** A clickable checklist dialog
   opens with four items:

   1. *Removed previous plate (`Plate-1`) and stored it*.
   2. *Moved needle to home* — ticking this checkbox drives the
      carriage to `(0, 0)` directly (no separate Move button).
   3. *Placed new plate on stage*.
   4. *New plate ID:* `Plate-2` (auto-incremented suggestion; the
      checkbox is auto-ticked once the Entry holds a value that
      passes validation, so a fresh dialog already counts this row
      as checked).

   The **Continue** button stays disabled until every checkbox is
   ticked. Two helpers sit beneath the list: **Select All** ticks
   every box at once (and triggers the home move via the home
   checkbox's trace), and **Skip Checklist (Expert)** enables
   Continue without forcing the operator to tick each box —
   intended for users who have done the procedure enough times
   that the checklist is redundant. Skipping writes a
   `checklist_skipped_plate_swap_{N}` row to `log.csv` so the
   bypass is recorded.

   The dialog cannot be dismissed via the window's close box —
   you must either click **Continue** or **Cancel Run**.

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

autoSIP has six interruption controls. They look similar in the GUI
but differ in when they are available, what they do to the run, and
what files they produce.

| Button                      | Available when                                                                 | Effect                                                                                                          | Resumable?                                                       | What it writes to `log.csv` / disk                                                                |
| --------------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| **Pause** / **Resume**      | During a run (pump, wait, or move phase)                                       | Cancels the in-flight `after()` task; pump relay off; motors hold position. Claim stays held.                   | Yes — click the same button (label becomes **Resume**)           | One `resume` breadcrumb row on Resume, if currently in the collection phase                       |
| **Return to Origin** (mid-pause) | While the run is paused                                                   | Moves motors to `(0, 0)` and tares the counters; captures the paused position on the first click. Used for mid-run recalibration against stepper drift (§6.3.6). | Yes — on Resume, the application moves the needle back to the captured position and shows a Confirm Calibration dialog. | No row in `log.csv`. |
| **Continue to Next Sample** | After auto-pause at "Total reached"                                            | Starts a new series: increments series_index, runs the discard phase, then collects at the next available well. | Continues the run                                                | `resume` breadcrumb row                                                                           |
| **Continue to Next Plate**  | After auto-pause at "Plate full"                                               | Opens the plate-swap dialog. After Continue, moves the carriage to A1 of the new plate and resumes.             | Continues the run after the dialog                               | `plate_swap` breadcrumb row                                                                       |
| **End Run**                 | Any active run state (running, paused, total reached, plate full)              | Three-button prompt (Cancel / Don't Save / Save and End); pump off; motors released; visuals reset; FractionatorState counters zeroed. | Cancel stays in the run; Save and End / Don't Save terminate it. | On **Save and End**: `end_{ts}.json`, `summary_{ts}.md`, `summary_{plate_id}_{ts}.md`. On **Don't Save**: none.  |

When to use each:

- **Quick interruption** (operator break, momentary distraction):
  **Pause**. Cleanest interrupt. Click again to resume; the cycle
  picks up at exactly the same point.
- **Stepper drift suspected mid-run**: **Pause** → **Return to
  Origin** → manually re-park the carriage → **Resume** →
  **Confirm Calibration**. See §6.3.6 for the full walkthrough.
- **End-of-tube — swap source tubes**: **Continue to Next Sample**.
  autoSIP fires this auto-pause on its own when the per-sample
  fraction target is reached; update Sample ID and click Continue.
- **End-of-plate** (autoSIP triggers this automatically):
  **Continue to Next Plate**, then follow the four-item swap
  checklist.
- **Intentional finish** at the end of a session: **End Run**, then
  **Save and End** to keep the end/summary files (or **Don't Save**
  if the run was a test).
- **Safety emergency** (smell, collision, fluid leak):
  **End Run** → **Don't Save** (or **Save and End** if the partial
  run logs are worth keeping) is the manual halt path. The
  application's automatic waste-bin lockdown (§6.5.1) covers the
  one autonomous emergency case.

Pause is reversible and silent; Continue advances the run; End Run
finishes cleanly and writes the appropriate finalization files for
either choice (Save and End writes end/summary; Don't Save leaves
only the raw `metadata.json` + `log.csv`).

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

### 6.3.6 Recalibrating mid-run after stepper drift

Stepper motors occasionally miss steps, so the software's tracked
position can drift away from the true physical position over a long
run. autoSIP supports a mid-run recalibration that re-zeros the
counters against the origin mechanical-limit corner without aborting
the run. The origin corner depends on plate orientation (upper-left
in landscape, bottom-left in portrait).

1. **Notice the drift.** The dispensed drop lands slightly off-well,
   or the snake path looks visibly shifted. Click **Pause**.

2. **Click Return to Origin.** The motors drive to coordinate `(0, 0)`
   and the software's tracked counters are tared. The current motor
   position at the moment of the click is captured so the matching
   Resume can drive the needle back to it. Status bar:
   *"Returned to origin. Manually re-park the carriage against the
   {origin} limit, then click Resume."* — where `{origin}` is
   `upper-left` in landscape or `bottom-left` in portrait.

3. **Manually re-park the carriage** against the
   orientation-appropriate mechanical-limit stops. The software's
   `(0, 0)` now matches the physical origin corner — drift is
   corrected.

4. **Click Resume.** The application drives the needle from the
   freshly-calibrated origin to the position captured in step 2,
   then opens a **Confirm Calibration** modal showing the captured
   coordinates and asking you to verify the needle is correctly
   positioned over the expected well.

5. **Inspect, then click Confirm.** Fractionation resumes from the
   exact state-machine point where it was paused (mid-pump, mid-wait,
   or between wells).

If the calibration looks wrong, click **Cancel** in the confirm
dialog instead. The run stays paused; the needle stays at the
captured position. You can click Return to Origin again to retry,
or End Run to abandon.

Multiple Return-to-Origin clicks during the same pause are safe —
only the FIRST click captures the reference position. Subsequent
clicks simply re-issue the move-to-origin + tare, so an operator
who botches the first manual re-park can retry without losing the
"where the run actually was" reference.

The recalibration flag clears on Resume-confirm, End Run, Continue
to Next Sample, and Continue to Next Plate — those all advance the
run past the paused point, so the captured position is no longer
the right reference.

### 6.3.7 Running a bulk submission

For multi-sample sessions, autoSIP can preload Sample ID, Plate ID,
fraction counts, and per-well volume for every tube from a spreadsheet
so the operator does not have to retype Run Parameters between
samples. This is the recommended workflow whenever more than two
tubes are being processed in a session.

1. **Generate a template.** In Automated mode, open the *Bulk Sample
   Submission* panel above Run Parameters and click **Generate
   Template**. Pick a save location (e.g. on a USB stick or in your
   project folder). The CSV opens with header comments explaining
   the columns and two example rows.

2. **Fill in the spreadsheet.** Edit the CSV in a spreadsheet editor
   (LibreOffice Calc, Excel, Google Sheets). The only required
   column is `sample_id`; blank cells in the optional columns
   (`plate_id`, `number_of_fractions`, `discard_fractions`,
   `volume_per_well_ml`, `notes`) inherit the value currently in the
   GUI's Run Parameters at the moment of import. Comment lines
   starting with `#` and blank lines are ignored. Delete the example
   rows before importing.

   **Plan plate capacity.** A 96-well plate holds 96 fractions
   minus any pre-collected wells. If your samples in aggregate
   would exceed the capacity of one plate, choose where the plate
   swap occurs by setting `plate_id` to a new value (e.g.
   `Plate-2`) on the first row that should land on the new plate.
   The transition dialog at that point will prompt you to swap the
   physical plate before continuing.

3. **Import the submission.** Click **Import Submission** in the
   Bulk Sample Submission panel and pick the CSV. autoSIP validates
   every row before activating the panel — if any row fails (bad
   Sample ID character, non-integer fraction count, discards ≥ N,
   out-of-range volume) the import is rejected as a whole and the
   panel stays inactive. Fix the spreadsheet and re-import.

   On success, Run Parameters auto-populate from row 1 and lock.
   The panel header shows the source filename and a status line
   (e.g. *"Bulk mode — 8 samples loaded, sample 1 of 8"*). The
   **Project name** field remains editable so you can adjust the
   log folder before clicking Begin Fractionation.

4. **Begin Fractionation.** Confirm the run summary — the dialog
   includes a "Bulk mode" line showing total samples and the
   sample-1 ID. The first sample runs exactly like a normal
   single-sample run.

5. **Transition dialog at each Total reached.** When the first
   sample finishes (auto-pause at "Total reached"), a *Continue to
   Next Sample* dialog opens automatically. It shows the next
   sample's Sample ID, Plate ID, and the inter-sample purge
   reminder. You can edit the Sample ID inline if the physical tube
   label differs from the spreadsheet — edits are flagged in the
   final `summary.md` with a `b` suffix on the Sample ID. Click
   **Continue** to apply the next sample's metadata to Run
   Parameters and start the discard phase for that sample.

   If you instead click **End Run** in the transition dialog,
   bulk mode exits and the run finalizes with whatever samples
   were completed.

6. **Final sample.** After the last sample's Total reached, the
   transition dialog reads *"Bulk Run Complete"* and offers only
   End Run. Click it to write the final summary and exit bulk mode.

The `summary.md` for a bulk run includes a `## Bulk submission`
section listing the source spreadsheet path, total samples, and the
as-run Sample ID sequence (with `b` markers for any IDs edited in
the transition dialog). The `metadata.json` at run start records
the full spreadsheet contents so the planned vs. actual sequence is
recoverable later.

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
- `purge_wash` / `purge_clear` — inter-sample purge pump phases.
  `well_id` is `purge_{phase}_{series}` for the initial cycle and
  `purge_{phase}_{series}_ext{N}` for each Space-bar extension; the
  `dispense_duration_s` column carries the measured per-cycle pump
  duration.
- `checklist_skipped` — emitted when the operator clicks **Skip
  Checklist (Expert)** on a plate-swap or inter-sample-purge dialog.
  `well_id` is `checklist_skipped_plate_swap_{N}` or
  `checklist_skipped_purge_phase_{N}_{series}` so the bypass is
  pinpointed in the log.
- `waste_warning` / `waste_shutoff` / `waste_reset` — breadcrumb
  rows for waste-bin events (80 % warning, 100 % auto-shutoff,
  Reset Waste Counter click).
- `emergency_stopped` — the run was terminated while this well or
  discard cycle was mid-dispense. Also used for purge pump cycles
  interrupted by Escape or window close.

**end_{end_timestamp}.json** — written only if you choose **Save and
End** at End Run. Contains the final status (`completed`,
`manual_abort`, or `emergency_stopped`), wells completed, wells
planned, actual total time, and the list of plates used.

**summary_{end_timestamp}.md** — human-readable run summary.

**summary_{plate_id}_{end_timestamp}.md** — one file per plate the
run touched. Filtered slice of the run summary, suitable for printing
and attaching to the physical plate as it goes to downstream
processing.

`metadata.json` and `log.csv` are written **continuously during the
run**; the three `_{end_timestamp}` files are written **only when End
Run is confirmed with Save and End**. If you choose **Don't Save**,
the metadata.json and log.csv remain on disk but no end/summary files
are produced — useful when the run was a test or calibration you do
not want to archive.

## 6.5 Safety Controls

Operator-initiated halts go through **Pause** (reversible, the cycle
resumes from the same phase) or **End Run → Don't Save** (one-way,
no finalization files). Both pump activity and motor motion stop
immediately on either. There is no dedicated emergency-stop button
or keyboard shortcut — the only keyboard shortcut in the
application is **Space**, which toggles the most-recently-used pump
in Manual mode.

The one autonomous emergency path is the waste-bin overflow
lockdown described in §6.5.1: the application halts pump activity
on its own when the running waste-volume estimate reaches the
configured maximum, and a `waste_shutoff` row is appended to
`log.csv`. The operator clears the lockdown via the **Reset**
button next to the flask icon in the status bar.

### 6.5.1 Waste-bin overflow protection

autoSIP maintains an internal estimate of how full the waste container
is and locks down pump activity before it overflows. The right side
of the status bar shows an Erlenmeyer flask icon (green → amber →
orange → red as it fills), a numeric readout (`{volume} / {max} mL
({pct}%)`), and a **Reset** button.

The estimate is computed by multiplying every recorded pump-on
duration by the matching pump rate:

- Each Automated-mode discard cycle adds `volume_per_well` mL
  (configured Volume per well — by construction one full per-well
  dispense lands in the waste bin during the discard phase).
- Each inter-sample purge phase (wash and clear) adds
  `phase_duration_s × peristaltic_rate_ml_per_min / 60` mL.
- Manual-mode Purge button on→off, Cleaning-mode Purge button
  on→off, and Purge Time Calibration Start→Stop each add the same
  peristaltic-rate-based contribution. Manual-mode *Fractionate* is
  **not** tracked (its dispense location is ambiguous).

Because the estimate depends on the configured pump rates matching
the physical hardware, it can drift over time. Treat it as
"approximate" rather than "measured."

**80 % warning.** When the running estimate reaches 80 % of the
configured *Max waste bin volume*, a one-shot warning dialog fires
and a `waste_warning` row is appended to `log.csv`. The warning
fires once per fill cycle (it re-arms after a Reset).

**100 % auto-shutoff.** When the running estimate reaches the
configured maximum:

- All pump activity halts immediately.
- All run-control buttons are disabled except **End Run**.
- If a Cleaning operation or Purge Time Calibration was running, its
  controls are disabled too.
- If an inter-sample purge phase was mid-pump, its modal stays open
  showing a "HALTED" message; re-click the modal's action button
  after Reset to retry the phase from the start.
- A concise blocking modal appears: *"Estimated waste reached
  {max} mL. Pump halted."* followed by the three-step recovery
  list (empty container / click Reset next to flask icon / click
  Resume).
- A `waste_shutoff` row is appended to `log.csv`.

**Reset workflow.** After physically emptying the waste container,
click **Reset** in the status bar. The application asks for
confirmation, then:

- Resets `waste_volume_ml` to 0 and re-arms the 80 % warning.
- Appends a `waste_reset` row to `log.csv` (if a run is active).
- Clears the auto-shutoff lockdown if one was active, re-enabling
  the run-control buttons.

Reset should follow a physical empty, not precede one — the
application has no way to verify the container is actually empty.

The counter resets to 0 on every app launch as well: if you empty
the bin between sessions and start a fresh process, the counter
reflects the empty state automatically. If you empty mid-session,
use Reset; if you close and reopen the app instead, the new process
starts fresh either way.
