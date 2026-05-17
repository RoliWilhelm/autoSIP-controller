"""Hardware abstraction for autoSIP.

Exposes two backend interfaces and a factory:

- ``StepperBackend``: duck-typed object with ``onestep(*, direction, style)``
  and ``release()``. Real instances come from ``adafruit_motorkit.MotorKit``
  (``kit.stepper1`` / ``kit.stepper2``).
- ``RelayBackend``: duck-typed object with ``on()`` and ``off()``. The real
  instance is a ``gpiozero.LED`` driving the IoT relay GPIO pin.
- ``get_backends(...)``: returns a ``Backends`` bundle. Tries to construct
  the real Adafruit/gpiozero objects; on ``ImportError`` or ``RuntimeError``
  (e.g. no HAT present on a developer laptop), returns mock backends that
  log every effectful call and otherwise no-op.

The module also exposes ``FORWARD``, ``BACKWARD``, and ``MICROSTEP``
constants. With real hardware these are rebound to the matching values from
``adafruit_motor.stepper`` so that backend calls receive the exact values
the Adafruit library expects. With mocks they remain opaque string
sentinels that backends accept and echo back into log lines. Callers must
access these by attribute (``hardware.FORWARD``) rather than capturing them
with ``from hardware import FORWARD`` so the post-init rebinding is seen.
"""

from dataclasses import dataclass
import logging

logger = logging.getLogger("autosip")

# Direction / style sentinels. Rebound inside get_backends() when the real
# adafruit_motor.stepper module is importable so backend.onestep(...) sees
# the values the Adafruit driver expects.
FORWARD = "FORWARD"
BACKWARD = "BACKWARD"
MICROSTEP = "MICROSTEP"


@dataclass
class Backends:
	"""Container for the live hardware backends used by the app."""
	stepper1: object
	stepper2: object
	relay: object
	simulated: bool


class _MockStepper:
	"""No-op stepper that counts steps and logs at release()."""

	def __init__(self, name):
		self.name = name
		self.step_count = 0

	def onestep(self, *, direction, style):
		# Per-step logging would flood the log (3200 steps/rev). Just count;
		# the descriptive log line is emitted by StepperMotor.move_relative.
		self.step_count += 1
		self._last_direction = direction
		self._last_style = style

	def release(self):
		logger.debug(
			"hardware[mock]: %s release() after %d microsteps (last dir=%s style=%s)",
			self.name, self.step_count, getattr(self, "_last_direction", None),
			getattr(self, "_last_style", None),
		)
		self.step_count = 0


class _MockRelay:
	"""No-op relay that logs every state transition at DEBUG.

	The user-facing INFO log line comes from ``PumpController`` higher up
	(``[pump:fractionate] relay ON``), so the raw backend log is demoted to
	avoid duplicate noise in normal runs.
	"""

	def __init__(self, pin):
		self.pin = pin
		self.state = False

	def on(self):
		self.state = True
		logger.debug("hardware[mock]: relay(pin=%s) ON", self.pin)

	def off(self):
		self.state = False
		logger.debug("hardware[mock]: relay(pin=%s) OFF", self.pin)


def _build_mock_backends(relay_pin):
	return Backends(
		stepper1=_MockStepper("stepper1"),
		stepper2=_MockStepper("stepper2"),
		relay=_MockRelay(relay_pin),
		simulated=True,
	)


def get_backends(*, relay_pin="5", force_simulate=False):
	"""Return a ``Backends`` bundle, falling back to simulation if needed.

	If ``force_simulate`` is True, always return mock backends regardless of
	whether the Adafruit/gpiozero modules can be imported. Otherwise attempt
	the real hardware path; on ``ImportError`` (libraries missing) or
	``RuntimeError`` (libraries present but no HAT/GPIO accessible) fall back
	to mocks and log a warning.
	"""
	global FORWARD, BACKWARD, MICROSTEP

	if force_simulate:
		logger.info("hardware: simulation forced; using mock backends")
		return _build_mock_backends(relay_pin)

	try:
		from adafruit_motorkit import MotorKit
		from adafruit_motor import stepper as _stepper
		from gpiozero import LED
	except (ImportError, RuntimeError) as exc:
		logger.warning(
			"hardware: import failed (%s); falling back to mock backends", exc,
		)
		return _build_mock_backends(relay_pin)

	try:
		kit = MotorKit()
		relay = LED(relay_pin)
	except (ImportError, RuntimeError) as exc:
		logger.warning(
			"hardware: init failed (%s); falling back to mock backends", exc,
		)
		return _build_mock_backends(relay_pin)

	FORWARD = _stepper.FORWARD
	BACKWARD = _stepper.BACKWARD
	MICROSTEP = _stepper.MICROSTEP

	logger.info("hardware: real Adafruit MotorKit + gpiozero relay initialized")
	return Backends(
		stepper1=kit.stepper1,
		stepper2=kit.stepper2,
		relay=relay,
		simulated=False,
	)
