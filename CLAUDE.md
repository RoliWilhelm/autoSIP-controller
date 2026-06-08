# autoSIP — Robotic Gradient Fractionator GUI

## What this is
autoSIP is an open-source, Raspberry Pi-controlled benchtop robot that automates
isopycnic gradient fractionation for stable isotope probing (SIP) molecular biology
experiments. Two NEMA-17 stepper motors on lead screws drive an XY carriage with a
dispensing needle, and a relay-switched pump extracts liquid from an ultracentrifuge
tube and dispenses measured fractions into a 96-well plate (or other Opentrons-format
labware). Manuscript in preparation (Elango et al., 2026, for HardwareX).

## Codebase orientation
- `main.py` — entire application: the `StepperMotor` hardware class, the `TextEntry`
  composite widget, and the `App(tk.Tk)` with three modes (Automated, Manual, Cleaning).
- `hardware.py` / `validation.py` / `well_plate.py` / `config_store.py` /
  `run_logger.py` — feature modules used by `main.py`.
- `profiles/` — starter profile JSON files (e.g. `96well_CsCl_default.json`)
  copied into `~/.autosip/profiles/` on first launch.
- `logs/` — per-run output, one subdirectory per fractionation run, layout
  `logs/{project}/{timestamp}_{sample_id_at_start}/` containing
  `system.start.state.json`, `log.csv`, `end.json`, `summary.md`. Visible in a file
  manager (no dotfile location); ignored by git.
- `~/.autosip/` — user preferences (`config.json` for `last_used` field
  values; `profiles/` for saved profile bundles). Per-user, not per-repo.
- `custom_96_well_plate.json` — example labware in Opentrons format. The app reads
  `ordering`, `dimensions`, and per-well `x`/`y`/`z` (mm).

## Hardware target
- Raspberry Pi 2B+ running Raspberry Pi OS.
- Adafruit DC & Stepper Motor HAT (controls both NEMA-17 motors via `adafruit_motorkit`).
- Two NEMA-17 stepper motors: 200 base steps/rev, microstepped to 3200 effective
  steps/rev. Lead screws have 40 mm pitch (40 mm linear travel per revolution).
- Single Digital Loggers IoT relay on GPIO 5 (driven via `gpiozero.LED`). The relay
  outlet physically powers whichever single pump the operator has plugged in:
  the Razel R-200 fractionation pump for **fractionation** runs, or the Adafruit 3910
  peristaltic pump for **purging** / line cleaning. Only one pump is connected at
  a time; the operator swaps the plug between runs. There is no second relay or
  GPIO pin.
- The GUI exposes two semantic buttons over this single relay — **Fractionate**
  and **Purge** — to make the operator think about which pump should be connected
  for the operation they're about to perform. The redundancy is intentional UX,
  not a bug. A `PumpController` in `main.py` enforces a claim-based interlock so
  only one logical pump (Fractionate xor Purge) can be driving the relay at a time.
- Display target: ~7" Pi touchscreen or a laptop over VNC.

## Coding constraints
- Python 3, Tkinter for the GUI. Do not switch to PyQt, Kivy, or a web frontend — the
  manuscript's software description depends on Tkinter and the Pi's reliability does
  too.
- The app MUST be runnable on a developer laptop with no HAT/GPIO present by stubbing
  hardware. Wrap hardware imports in try/except and provide a no-op simulation backend.
- Splitting `main.py` into modules is fine and encouraged once it gets long, but keep
  `python main.py` as the entry point.
- Use only standard library plus currently-imported third-party packages
  (`tkinter`, `adafruit-motorkit`, `gpiozero`). If you need a new dependency, justify
  it and add it to `requirements.txt`.

## Behavioral invariants — do not break these
- Automated mode: load Opentrons JSON → set rows/cols/well size/table+carriage start →
  Begin fractionation runs a snaking path, pumps for `volume / pump_rate` seconds per
  well, then waits the same duration, then moves to the next well.
- Pause cancels any in-flight `after()` task, turns the relay off (claim stays held),
  and resumes from the same state on unpause.
- On window close, motors must be released to prevent overheating.
- `PumpController` claim invariants:
  - At most one claimant (`"fractionate"` or `"purge"`) at a time. The opposite
    pump's buttons across the entire UI are disabled (interlocked) while a claim
    is held.
  - State machine claims `"fractionate"` at run start; the claim persists across
    the per-well pump-on / drip-wait cycles (relay flickers, claim stays); release
    happens at run end (`move()` success path) or on Terminate.
  - User-initiated pump clicks always confirm before turning the relay ON (no
    suppression). State-machine driven flips never prompt.

## Style
- Add docstrings to public methods.
- Use the `autosip` module logger (`logging.getLogger("autosip")`) for diagnostic
  output. No bare `print()` calls in production paths.
- New widgets should be created once and shown/hidden by their parent Frame, not
  destroyed and rebuilt each mode switch.

## Verification
- After every change, run `python main.py --debug` in simulation mode and confirm
  the GUI launches without exceptions and the affected feature behaves as described.
- For hardware-touching changes, hand off to the team for on-Pi verification.
