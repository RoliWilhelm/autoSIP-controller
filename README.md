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

## License
GPL-3.0 — see LICENSE.

## Citation
If you use autoSIP, please cite Laud et al. 2024 (in preparation, HardwareX).
