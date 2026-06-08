# Review of Operation Instructions draft (Section 6)

Code-verified against `main.py`, `validation.py`, `well_plate.py`, and
`run_logger.py` as of commit `5b2fce4` plus subsequent unpushed edits.

This file groups feedback into two parts:

- **Part A — Inaccuracies** flagged in the existing draft, with the
  code reality and a suggested rewording.
- **Part B — Missing elements** for features that exist in the current
  software but are not described in the draft.

The draft is structurally solid; the corrections are mostly about
behavior added or changed since the original was written.

---

## Part A — Inaccuracies

### A1. Section numbering

The draft opens with `1. Operation instructions` then jumps to
`6.1 General Setup`. The `1.` looks like a leftover from an earlier
manuscript outline. The standalone `docs/operation_instructions.md`
in the repo uses `# 6 Operation Instructions` as the H1. Suggest
removing the `1.` line and starting at H2 `## 6.1` (the H1 lives
in the manuscript outer chrome).

### A2. §6.1 — pump-model names

> "a fractionation pump for fractionation runs and a peristaltic pump for
> purging and line cleaning"

Specific models are part of the broader manuscript's hardware
section, but the existing in-repo docs name them: **Razel R-200**
fractionation pump and **Adafruit 3910** peristaltic pump. Worth naming
in §6.1 too for consistency with §2 (Bill of Materials).

### A3. §6.2.1 Run Parameters — mid-run editability note

> "Note: these parameters are all editable mid-run, but will prompts
> for confirmation. The new value applies to subsequent logged
> fractions, but files already written will keep the original
> parameters logged."

This is the most consequential inaccuracy in the draft. The actual
behavior, parameter-by-parameter:

| Parameter | Mid-run editable? | Confirmation? |
| --- | --- | --- |
| Project name | Yes | Yes — focus-out triggers `"Project name changed mid-run"` confirm; No reverts. |
| Sample ID | Yes, silently mirrored to state on every keystroke. Intended for tube swaps. | No (the new editable-Sample-ID dialog appears only on Continue to Next Sample when the value matches the previous series). |
| Plate ID | Yes, silently mirrored. Auto-incremented at plate swap. | No. |
| Number of fractions | Editable in the entry box, but the value is snapshotted at run start; mid-run edits **do not affect the in-progress run**. | n/a |
| Discard fractions | Same — but re-read on Continue to Next Sample, so an edit affects the *next* series. | n/a |
| Volume per well | Snapshotted at run start; mid-run edits do not affect the in-progress run. | n/a |

Suggested replacement note:

> Note: Project name, Sample ID, and Plate ID are intended to be
> updated mid-run (Sample ID for tube swaps, Plate ID at plate
> swaps). Project changes prompt for confirmation; Sample ID and
> Plate ID flow silently into subsequent log rows. Number of
> fractions, Discard fractions, and Volume per well are
> snapshotted at run start — mid-run edits to these entries take
> effect only when the next series starts (e.g. Discard fractions
> is re-read on Continue to Next Sample).

### A4. §6.2.1 Run control buttons — Continue to Next Sample

> "starting a new fractionation series with a wash step. If a user
> has not updated the Sample ID since the previous series, the
> software will prompt you to update and confirm before proceeding."

The Sample-ID confirm is correct (it is now an inline editable
dialog, not a yes/no — worth saying so). The "wash step" gloss
undersells the actual flow. The current behavior is a three-phase
inter-sample purge workflow (wash → air-clear → connect new sample),
gated by a "Skip inter-sample purge" checkbox in the Pump section.
See §B1 for the prose to add.

### A5. §6.2.1 Run control buttons — Continue to Next Plate

> "Opens the plate-swap dialog to guide user to reset the starting
> position and other plate parameters."

The dialog does **not** ask the operator to reset starting position
or other plate parameters — those carry over (the new plate is
assumed to sit in the same physical position as the old one). The
dialog walks the operator through five numbered steps:

1. Remove the current plate.
2. Move Needle to Home (button inside the dialog).
3. Place a new plate on the stage.
4. Enter the new Plate ID (pre-filled with the auto-incremented
   suggestion).
5. Click Continue.

Suggest:

> Opens the plate-swap dialog, which walks the operator through
> removing the current plate, returning the needle to home,
> placing a new plate, entering the new Plate ID (pre-filled with
> the auto-incremented suggestion), and clicking Continue to
> resume.

### A6. §6.2.1 Well-plate display — visual states

The draft lists three visual states (gray = unvisited, ▼ = dispensing,
filled = completed). The widget actually renders **five** states:

- `UNVISITED` (light gray, no glyph)
- `DISPENSING` (teal-green `#66c2a5` with `▼` glyph; pulses)
- `WAIT` (orange `#fc8d62` with `⋯` glyph) — the drip-wait
  interval after pump-off and before the next move
- `COMPLETED` (per-sample color from the Okabe–Ito palette, with
  the fraction-index number as the glyph)
- `SKIPPED` (red `#d62728` with `✗` glyph) — emergency-stopped
  fractions inherit this state

The draft can keep its three-state simplification if WAIT is
intentionally hidden from the user-facing description, but the
SKIPPED state should at least get a mention in the run-interrupt
context.

### A7. §6.2.1 Well-plate display — tooltip contents

> "Hovering over any completed well opens a tooltip showing the well
> ID, sample ID, fraction index, and dispense duration."

Actual tooltip lines (`well_plate.py:676-691`):

```
{well_id} — {status}
Sample {sample_id} — fraction {N} of this sample
({color_name})
Planned dispense: {pump_time:.2f} s
```

So the tooltip shows: well ID + status, sample ID + fraction index,
the human-readable Okabe–Ito color name, and the **planned**
per-well dispense duration (a constant derived from
`volume / pump_rate`, not a measured value). The draft says
"dispense duration" which is misleadingly precise — actual
per-well timing is not currently tracked.

### A8. §6.2.1 System status indicators — Terminate Run availability

> "The Terminate Run button anchors the far right of the status bar
> and remains accessible at all times."

The Terminate Run button is **visible only in Automated mode**
(`StatusBarFrame.set_terminate_visible(visible)` is called with
`name == "Automated"` in `App.set_mode`). In Manual or Cleaning,
the octagon disappears. Suggest:

> The Terminate Run button anchors the far right of the status bar
> while Automated mode is active; it is hidden in Manual and
> Cleaning modes, where there is no run to terminate.

### A9. §6.2.2 Manual mode Pump Controls — Fractionate vs. Purge waste tracking

> "Fractionate: toggles the relay on or off.
> Purge: toggles the relay on or off and logs an estimate of the
> volume transferred to the waste bin."

Correct that Purge tracks waste-bin volume; **intentionally**
incomplete on Fractionate. The Fractionate button in Manual mode
does NOT track waste because the dispense destination could be the
plate, the waste container, or a test container — autoSIP cannot
know which. Suggest:

> Fractionate: toggles the relay on or off. Manual-mode Fractionate
> dispenses are intentionally not counted against the waste-bin
> estimate, since the needle position is operator-controlled and
> the dispense destination is ambiguous.
> Purge: toggles the relay on or off; the on-duration is
> multiplied by the configured peristaltic-pump rate and added to
> the waste-bin estimate (visible in the status bar).

### A10. §6.2.2 Manual mode — first-activation confirmation

> "The first time either pump is activated in a Manual mode session,
> a confirmation dialog reminds the operator..."

The confirmation latch was recently changed from **per Manual-mode
visit** to **per pump per session**. Once Fractionate is confirmed
on the first activation in *any* mode, it stays confirmed across
mode switches, runs, pauses, etc. The flags reset only on app
launch and on Terminate Run.

Suggested replacement:

> The first time each pump is activated in a session (regardless of
> mode), a confirmation dialog reminds the operator to verify which
> physical pump is wired to the relay outlet. Subsequent activations
> of the same pump skip the dialog. The flags reset on app restart
> and on Terminate Run (since hardware may have been swapped during
> an emergency stop).

### A11. §6.2.2 Manual mode — space-bar shortcut behavior

> "Pressing the space bar toggles whichever pump was used most
> recently. The space-bar shortcut is active in Manual and Cleaning
> modes."

In Cleaning mode, the space bar **always toggles Purge**
(`App._on_space`'s Cleaning branch hard-codes "purge", since Purge
is the only pump button in that mode). Only Manual mode uses the
"most-recently-used" tracking. Suggest:

> Pressing the space bar in Manual mode toggles whichever pump was
> used most recently (indicated by a small `(Space)` hint next to
> the bound button). In Cleaning mode, the space bar always toggles
> Purge — the only pump button in that mode. In Automated mode,
> the shortcut is inactive. In all modes, the shortcut is suppressed
> while a text-entry widget has keyboard focus, so typing in
> Sample ID etc. still inserts a space character.

### A12. §6.2.3 Cleaning mode — old labels

> "Waste bin: table position (cm) and Waste bin: carriage position
> (cm) — the same two values that appear in Automated mode's Plate
> Parameters → Waste bin section."

The labels were renamed: the entries now read
`Waste bin position (x-axis):` and `Waste bin position (y-axis):`
in both Automated and Cleaning modes. Same for `Starting well
position (x-axis)/(y-axis)` (renamed from `Starting point …`).
Update the draft to the new label text.

### A13. §6.1 — cross-reference placeholder

> "section X.X.X" in the Plate Parameters Note (referring to the
> Manual-mode calibration walkthrough)

Needs the real section number filled in. The existing in-repo doc
puts the walkthrough at §6.3.1.

### A14. Table Z — pre-existing inaccuracies preserved

Table Z reads accurately for the run-control comparison except:

- "Continue to Next Sample" → "Effect" column says "Starts a new
  series: increments series_index, runs the discard phase, then
  collects at the next available well." Missing the inter-sample
  purge workflow when Skip is unchecked. The "What it writes" cell
  could mention `purge_wash` / `purge_clear` rows in addition to
  the `resume` breadcrumb.

---

## Part B — Missing elements

The features below exist in the current software but are not
described in the draft.

### B1. Inter-sample purge workflow (§6.2.1 Run Parameters + §6.2.1 Run controls)

Continue to Next Sample now runs a three-phase tubing purge between
samples to prevent carryover. Two pump-section parameters configure
it:

- **Purge time (s)** — float in `[1.0, 600.0]`, default `30.0`.
  Duration of each of two pump phases between samples.
- **Skip inter-sample purge** (checkbox) — bypasses the purge
  workflow entirely; Continue to Next Sample goes straight to the
  new sample's discard + collection.

When the operator clicks Continue to Next Sample, the needle moves
to the waste bin and the application opens a three-step modal
sequence:

1. **Wash.** Disconnect the inlet line from the previous sample tube
   and place it in the wash-solution container. Click Start Purge
   to run the pump for `Purge time` seconds.
2. **Clear.** Remove the inlet line from the wash container, leaving
   it in air. Click Continue to run the pump for another
   `Purge time` seconds, pushing air through to clear residual wash.
3. **Connect.** Connect the inlet line to the new sample tube.
   Click Begin Fractionation to proceed.

Each modal has a Cancel button that aborts the workflow and returns
the run to the auto-paused state (the operator can re-click
Continue to Next Sample to restart from Step 1). The wash and clear
phases write `purge_wash` and `purge_clear` rows to `log.csv`; the
volumes (duration × peristaltic rate) are added to the waste-bin
estimate.

Use Cleaning mode's **Purge Time Calibration** sub-panel (§B5) to
measure the right purge_time value for your tubing geometry.

### B2. Waste-bin overflow protection (§6.2.1 Pump Parameters + §6.2 system status)

Two additional Pump Parameters:

- **Peristaltic pump rate (mL/min)** — float in `[1.0, 200.0]`,
  default `100.0`. Used by the waste-bin estimator to convert
  purge-phase pump-on time into a volume contribution.
- **Max waste bin volume (mL)** — float in `[10.0, 5000.0]`,
  default `250.0`. Capacity of the waste container.

The right side of the status bar shows an **Erlenmeyer flask icon**
(green → amber → orange → red as it fills, pulsing at ≥95%), a
numeric readout `{volume} / {max} mL ({pct}%)`, and a **Reset**
button.

The estimate is computed from pump-on time × rate:

- Each Automated-mode discard cycle adds `volume_per_well` mL.
- Each inter-sample purge phase adds
  `phase_duration_s × peristaltic_rate / 60` mL.
- Manual Purge, Cleaning Purge, and Purge Time Calibration Start→Stop
  add `on_duration_s × peristaltic_rate / 60` mL.
- Manual Fractionate is **not** counted (its destination is ambiguous).

Thresholds:

- **80%** — one-shot warning dialog; `waste_warning` row to `log.csv`;
  re-arms after Reset.
- **100%** — auto-shutoff: pump halts, every run-control except End Run
  is disabled, in-flight purge phases halt, a recovery modal opens,
  and a `waste_shutoff` row is written.

The **Reset** button (status bar) opens a confirmation dialog, zeroes
the counter on confirm, re-arms the 80% warning, clears the auto-shutoff
lockdown if active, and writes a `waste_reset` row. Reset should follow
a physical empty, not precede one. The counter also resets to 0 on every
app launch (since the bin is typically emptied between sessions).

### B3. Begin Fractionation confirmation contents

The draft mentions "validate inputs, confirm the run summary, and start"
but doesn't list what's in the summary. The actual dialog includes:

- Project, Sample ID, Plate ID.
- Total fractions, discard count, plate-collection count.
- Volume per fraction, Pump rate, Drip wait.
- Inter-sample purge configuration (or "SKIPPED").
- Waste-bin projection: current volume, per-sample discard contribution,
  per-transition purge contribution, projected end-of-first-sample
  volume; a warning line if the projection exceeds bin capacity.
- Estimated total runtime + a caveat about user-controlled pauses.
- Reminders to verify the waste container is positioned at the
  configured coordinates and the plate has A1 at the configured
  start coordinates.

Worth a short paragraph in the Begin Fractionation description.

### B4. Manual mode — Position Calibration sub-panel (§6.2.2)

A LabelFrame below Jog Controls and Pump Controls supports the
"jog-then-save" calibration workflow that today requires reading
the position off the readout and re-typing in Automated mode:

- Live `Current position: X = …, Y = …` line, updated on every jog
  and Home.
- **Save as Starting Well Position** — captures the current
  coordinates and writes them to Automated mode's
  *Starting well position (x-axis)* and *(y-axis)* fields.
- **Save as Waste Bin Position** — same but for Waste bin (also
  appears immediately in Cleaning mode's mirrored fields).

Defensive bounds check before save; status-bar confirmation on success.
This is calibration tooling and is not written to `log.csv`.

### B5. Cleaning mode — Purge Time Calibration sub-panel (§6.2.3)

A LabelFrame below the main Cleaning controls measures how long wash
takes to replace one tubing volume so the operator can set the
*Purge time* parameter from their actual hardware:

1. Place the inlet line in your wash solution container.
2. Click **Start**. The pump powers on (with the standard confirmation
   on first activation) and an `Elapsed` timer ticks every 100 ms.
3. Watch the outlet. Click **Stop** the moment wash first appears at
   the outlet — that's one full tubing volume.
4. Click **Save as Purge Time** to write the measured value to
   Automated mode's Purge time entry. The Save button is enabled only
   when the measured value falls within `[1.0, 600.0]` s.

Reset clears between attempts. Calibration measurements are not
written to `log.csv`.

### B6. Manual mode — arrow-key jog shortcuts (§6.2.2)

In Manual mode, the four arrow keys jog the needle:

- ↑ = Y+ (carriage forward)
- ↓ = Y− (carriage back)
- ← = X−
- → = X+

The current step-size selector determines the per-press distance.
Same gating as the space-bar shortcut: only fires in Manual mode and
only when a text-entry widget does not have focus.

### B7. About dialog (Help menu)

The Help menu's About item now opens a custom Toplevel (not a plain
messagebox) containing:

- Version string from `__version__`.
- A short product description.
- A **clickable GitHub link** (`https://github.com/RoliWilhelm/autoSIP-controller`).
- A **Citation reminder** sub-frame with the exact text:
  *"If you use autoSIP, please cite Elango et al. 2026 (in preparation,
  HardwareX)."*

Worth a one-line mention so reviewers find it.

### B8. Keyboard shortcut summary

The current ops doc has a "Keyboard shortcuts" table at the end of the
Manual mode reference. With the recent additions, that table should
read:

| Key | Effect |
| --- | --- |
| Space | Toggle the most-recently-used pump (Manual mode) or Purge (Cleaning mode). Suppressed inside text-entry widgets. |
| ↑ ↓ ← → | Jog the needle by the selected Step size (Manual mode only). Suppressed inside text-entry widgets. |
| Escape | Cancel the active dialog. (Note: there is no global keyboard shortcut for Terminate Run; the only way to trigger it is the octagonal status-bar button.) |
| Enter | Confirm the active dialog (where applicable). |

### B9. Logging output and Safety controls

The draft introduction mentions "run-logging" but the snippet does
not include a §6.4 or §6.5. The current in-repo doc has:

- **§6.4 Logging Output** — directory layout, file contents,
  `log.csv` columns and the full status-value set (`completed`,
  `discarded`, `resume`, `plate_swap`, `purge_wash`, `purge_clear`,
  `waste_warning`, `waste_shutoff`, `waste_reset`, `emergency_stopped`),
  Estimated waste summary section, file-naming with timestamp suffixes
  so multiple End Runs in a session don't overwrite each other.
- **§6.5 Safety Controls** — Terminate Run + Waste-bin overflow
  protection (B2 above).

If these sections are intended to live elsewhere in the manuscript,
fine — but flagging in case they were omitted from the draft snippet
by accident.

---

## Other minor notes

- The draft refers to "the strip" in *"Begin Fractionation is the
  primary action button at the center of the strip"*. The "strip"
  isn't defined elsewhere in the text. Suggest naming it explicitly:
  *"Begin Fractionation is the primary action button at the center
  of the band beneath the Pump section, flanked by an ultracentrifuge-
  tube icon on the left and a bimodal-distribution icon on the right
  (signifying the input and the SIP-experiment readout)."*

- The draft mentions "Figure X", "Figure P", "Table Z" as
  placeholders. These need real figure / table numbers when laid out
  in the manuscript.

- Software version `1.0.0` is consistent with `main.py:__version__`.
  Confirm before final typesetting since the value is hard-coded
  there.

- The draft writes `cc` nowhere — good. All unit references match the
  current `mL` / `mL/hr` convention.

- The starting bullet ("This section walks through setup, the three
  operating modes, common use cases, and run-logging.") promises a
  "common use cases" section that isn't in the snippet. The in-repo
  doc has §6.3 covering five use cases (calibration, multi-sample,
  multi-plate, button comparison, cleaning).

---

## Summary of code-verified high-confidence corrections

The 14 points in Part A are all directly verifiable against current
code; cite line ranges in `main.py` if reviewers ask for evidence.
The 9 points in Part B are also verifiable but reflect newer features
the draft predates rather than outright errors. Prioritize Part A in
this revision pass; Part B can grow into new prose for the next.
