# autoSIP Use Cases

This document walks through five common autoSIP workflows: calibrating
the plate-start and waste-bin coordinates, running multiple samples on a
single plate, running a sample that spans two plates, choosing between
the various pause / continue / end controls, and using Cleaning mode to
flush the fluid path between sample types.

If you are new to autoSIP, work through Use Case 1 first — every other
workflow assumes the plate-start and waste-bin coordinates have been
calibrated for your physical layout. For installation and a description
of every GUI control, see the main [README](../README.md).

---

## Use Case 1: Determining the plate-start and waste-bin coordinates

This is the first thing to do after assembling the hardware (or any time
you reposition the stage). The procedure produces the two pairs of
coordinates — *Starting point* and *Waste bin* — that autoSIP needs to
drive the carriage to well A1 and to the waste container.

![Figure: Manual mode jog controls and Position readout](figures/calibration.png)

1. **Park the carriage at the upper-left mechanical limit.**
   With the autoSIP powered off, gently slide the carriage by hand
   until it rests against the upper-left lead-screw stops on both
   axes. This physical position becomes origin `(0, 0)` once you
   launch the software.

2. **Power on and launch autoSIP.**

   ```bash
   python main.py
   ```

   The window opens in Automated mode by default. Click the **Manual**
   tab in the header to switch.

3. **Verify the Position readout shows `X = 0.000 cm, Y = 0.000 cm`.**
   If it does not (for example because the software was previously
   left in some other state), click **Home** in the Jog Controls
   section. Home moves both motors to origin and re-zeros the
   software's tracked angle counters.

4. **Place the labware on the stage** in its intended position.
   Tighten any clamps so the plate cannot shift during a run.

5. **Jog the needle to well A1.** Use the **▲ Y+** / **◀ X−** /
   **X+ ▶** / **▼ Y−** buttons. Start with the **10 mm** step to
   move quickly, then switch to **1 mm** as you get close to A1,
   and to **0.1 mm** for final centering. The Position readout
   updates after every jog.

6. **Record the X and Y values from the Position readout.** These are
   your plate-start coordinates. Switch back to **Automated** and
   enter them in Plate Parameters:
   - `Starting point (x-axis)` — the recorded X value.
   - `Starting point (y-axis)` — the recorded Y value (note the Y
     value in Manual mode shows as a negative number; enter the
     same numeric value in Automated mode's field).

7. **Repeat the jog process for the waste container** — return to
   Manual mode, jog the needle until it sits above the waste
   container's opening, and record the values. Enter them in
   Automated mode under `Waste bin: table position` and
   `Waste bin: carriage position`. (Cleaning mode shares these
   same two fields, so editing either updates both.)

8. **Save the calibration.** Once both coordinate pairs are entered,
   File → *Save current as profile…* writes the field values to
   `~/.autosip/profiles/{name}.json` so you can reload them later
   without re-jogging. The most-recently-used values also persist
   automatically across launches via `~/.autosip/config.json`.

**Drift over a long session.** Stepper motors occasionally miss steps,
and the software's tracked position can drift away from the true
physical position over many hours. Periodically re-park the carriage
against the upper-left mechanical limit by hand and click **Home** in
Manual mode to re-zero the counters. autoSIP does not automatically
detect lost steps.

---

## Use Case 2: Fractionating multiple samples on a single plate

When several ultracentrifuge tubes' worth of fractions all fit on one
96-well plate, run them sequentially in a single autoSIP session. The
total fractions per sample × number of samples must be ≤ plate
capacity for this workflow; if it exceeds capacity, see
[Use Case 3](#use-case-3-a-sample-whose-fractions-span-two-plates).

For this example: five samples, 18 collected wells per sample (with two
discard fractions each), total 100 wells of activity = 90 collected + 10
discarded; fits comfortably in 96 wells.

![Figure: Begin Fractionation confirmation summary](figures/begin_confirm.png)

1. **Set Run Parameters** for the first sample:
   - **Project name** — e.g., `MyStudy_2026_Q2`. (Persists across
     all samples in this run.)
   - **Sample ID** — e.g., `Tube-A12`. (You will update this between
     samples.)
   - **Plate ID** — e.g., `Plate-1`.
   - **Number of fractions** — `20` (= 2 discards + 18 plate wells).
   - **Discard fractions** — `2`.
   - **Volume per well** — your per-fraction volume in cc, e.g. `0.22`.

2. **Verify Plate Parameters** (rows, columns, well width, starting
   point, waste bin) and **Pump** parameters (pump rate, drip wait
   time) are correct from your calibration.

3. **Click Begin Fractionation.** A summary dialog lists every
   parameter and the estimated run time. Confirm to start.

4. **autoSIP runs the discard phase first.** The needle moves to the
   waste bin and dispenses the two discard fractions there. The
   progress display shows `Discard phase: 1 of 2 fractions dispensed
   to waste` in the header.

5. **Collection begins at well A1.** The first plate well is labeled
   **3** (= 2 discards + 1) in the sample's color from the Okabe–Ito
   palette. autoSIP snakes through the plate column-by-column,
   filling wells until the per-sample target is reached.

6. **autoSIP auto-pauses at "Total reached"** — the status bar reads
   `Total of 20 fractions reached. Click End Run or Continue to Next
   Sample.` The run-control buttons update: **Continue to Next
   Sample** and **End Run** become enabled; **Pause** is disabled
   (since the run is already paused).

7. **Physically swap the source tube** on the ultracentrifuge or
   fraction collector to the next sample.

8. **Update Sample ID** in the Run Parameters section, e.g.
   `Tube-A12` → `Tube-A13`. **Do not** change Project, Plate ID, or
   any other field — they apply to the whole run.

9. **Click Continue to Next Sample.** If you forgot to change Sample
   ID, autoSIP prompts: *"Sample ID is still 'Tube-A12'. Did you
   mean to update it for the new sample? Continue anyway?"*

10. The new sample's discard phase runs at the waste bin, then
    collection resumes at the next available plate well in a
    **different color** (Okabe–Ito index 2 instead of 1).

11. **Repeat steps 6–10** for each additional sample.

12. **When the last sample finishes, click End Run.** The save/discard
    confirmation appears: *"Save the run logs for project '…' /
    sample '…'?"* Click **Yes** to write `end_*.json`, `summary_*.md`,
    and `summary_Plate-1_*.md` to the run directory.

After End Run, the progress canvas clears, all run counters reset to
zero, and the run-control buttons return to their idle state. Click
Begin Fractionation again if you want to start a fresh run with new
inputs — no application restart is required.

---

## Use Case 3: A sample whose fractions span two plates

When the total work exceeds a single plate's capacity, autoSIP
automatically pauses at the last well of the current plate and prompts
for a plate swap. The same sample's fractions continue on the new plate
in the same color, with the fraction counter continuing from where it
left off.

For this example: eight samples, 18 collected wells each (with two
discards each) — 144 total collected wells, which require two 96-well
plates.

![Figure: Plate Full dialog with swap steps](figures/plate_full_dialog.png)

1. **Set Run Parameters and click Begin Fractionation** as in Use
   Case 2.

2. **Samples 1–5 complete on Plate-1** (5 samples × 18 wells = 90
   wells filled; 6 wells remain on the plate). After each sample's
   auto-pause, update Sample ID and click **Continue to Next
   Sample**.

3. **Click Continue to Next Sample for sample 6.** Because only 6
   plate wells remain but the new sample needs 18, autoSIP shows
   an informational notice: *"This sample requires 18 wells. Only 6
   remain on plate Plate-1. autoSIP will prompt for a plate swap
   when the current plate fills."* Click OK to acknowledge. The
   discard phase runs, then collection begins at the next available
   well of Plate-1.

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
   4. Enter the new Plate ID — pre-filled as `Plate-2` (autoSIP
      auto-increments the trailing integer; you can override).
   5. Click **Continue** to resume fractionation.

   The dialog cannot be dismissed via the window's close box — you
   must either click **Continue** or **Cancel Run**.

6. **After Continue, autoSIP moves to well A1 of Plate-2** and
   resumes sample 6's collection. The fraction counter continues
   from where it left off: if sample 6 was at fraction 8 on
   Plate-1's last well, sample 6 starts at fraction 9 on A1 of
   Plate-2, still in sample 6's color.

7. **Sample 6's remaining wells finish on Plate-2**, then samples 7
   and 8 collect on Plate-2 via Continue to Next Sample as in Use
   Case 2.

8. **Click End Run when the last sample finishes.** Confirm "Yes,
   save."

The output directory now contains:

```
end_{end_timestamp}.json
summary_{end_timestamp}.md
summary_Plate-1_{end_timestamp}.md
summary_Plate-2_{end_timestamp}.md
```

The per-plate summary files are filtered slices of the full run
summary — print them out and attach each one to its physical plate as
the plates move to downstream processing.

The `log.csv` for this run includes a `plate_swap` breadcrumb row at
the moment of each swap (well_id `plate_swap_1`, `plate_swap_2`, …)
and `resume` breadcrumb rows at each Continue to Next Sample.

---

## Use Case 4: Pause and end controls — when to use which

autoSIP has five interruption controls. They look similar in the GUI
but differ in when they are available, what they do to the run, and
what files they produce.

### Comparison

| Button                      | Available when                                                                                       | Effect                                                                                                          | Resumable?                                                              | What it writes to `log.csv` / disk                                                                |
| --------------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| **Pause** / **Resume**      | During a run (pump, wait, or move phase)                                                             | Cancels the in-flight `after()` task; pump relay off; motors hold position. Claim stays held.                   | Yes — click the same button (label becomes **Resume**)                  | One `resume` breadcrumb row on Resume, if currently in the collection phase                       |
| **Continue to Next Sample** | After auto-pause at "Total reached"                                                                  | Starts a new series: increments series_index, runs the discard phase, then collects at the next available well. | Continues the run                                                       | `resume` breadcrumb row                                                                           |
| **Continue to Next Plate**  | After auto-pause at "Plate full"                                                                     | Opens the plate-swap dialog. After Continue, moves the carriage to A1 of the new plate and resumes.             | Continues the run after the dialog                                      | `plate_swap` breadcrumb row                                                                       |
| **End Run**                 | Any active run state (running, paused, total reached, plate full)                                    | Save-or-discard prompt; pump off; motors released; visuals reset; FractionatorState counters zeroed.            | No — the run terminates                                                 | On "Yes, save": `end_{ts}.json`, `summary_{ts}.md`, `summary_{plate_id}_{ts}.md`. On "No": none.  |
| **Terminate Run**           | Visible in Automated mode (bottom-right of the status bar)                                           | Hard-halt: pump off, motors released, run-control buttons disabled. Confirmation dialog required.               | After clicking **Return to Start Coords** the controls re-enable        | In-flight entry stamped `emergency_stopped` in `log.csv`; `end.json` + `summary.md` written       |

### When to use each

- **Quick interruption** (operator break, someone walked by) →
  **Pause**. Cleanest interrupt. Click again to resume; the cycle
  picks up at exactly the same point.
- **End-of-tube — you need to swap source tubes** →
  **Continue to Next Sample**. autoSIP fires this auto-pause on its
  own when the per-sample fraction target is reached; you update
  Sample ID and click Continue.
- **End-of-plate — autoSIP triggers this automatically** →
  **Continue to Next Plate**, then follow the five-step swap dialog.
- **Intentional finish at the end of a session** → **End Run**, then
  "Yes, save" to keep the end/summary files (or "No, discard" if the
  run was a test).
- **Safety emergency** (smell, collision, fluid leak) →
  **Terminate Run**. Stops the pump and motors as soon as you can
  confirm the dialog. After the rig is verified safe, click
  **Return to Start Coords** to re-enable controls.

A useful mnemonic: **Pause is reversible and silent**;
**Continue advances the run**; **End Run finishes cleanly**;
**Terminate Run is the heavy hammer** and writes `emergency_stopped`
to the log so the interruption is unmistakable in the audit trail.

---

## Use Case 5: Cleaning between sample types

Use Cleaning mode to flush the fluid path between sample types — for
example, after running an undeuterated control and before running a
deuterated one, to clear residual buffer or DNA from the tubing.

![Figure: Cleaning mode panel](figures/cleaning_mode.png)

1. **Disconnect the Razel R-200 syringe pump from the relay outlet
   and connect the Adafruit 3910 peristaltic pump.** Only one pump
   is wired to the relay at a time; the operator does the swap.

2. **Switch the autoSIP GUI to the Cleaning tab.**

3. **Confirm the waste-bin coordinates** in the Cleaning panel. These
   are the same two values that appear in Automated mode's Plate
   Parameters → Waste bin section — editing either set updates the
   other. If you have already calibrated the waste-bin position via
   Use Case 1, no further input is needed here.

4. **Click Move to Waste Bin.** The needle moves to the waste-bin
   coordinates.

5. **Click Purge.** A confirmation dialog reminds you that the
   peristaltic pump should be plugged in. Confirm to power on the
   relay.

6. **Wait for the line to clear.** Watch the tubing through the
   transparent sections until clean cleaning fluid is flowing.

7. **Click Purge again to stop.**

8. **Disconnect the peristaltic pump and reconnect the syringe pump
   before starting the next Automated run.** autoSIP cannot detect
   which pump is wired in — that is what the Fractionate/Purge
   confirmation dialogs are for.
