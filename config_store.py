"""On-disk config + profile store for the autoSIP GUI.

Two layers, both flat JSON under ``~/.autosip/``:

  - ``config.json``  -- a ``last_used`` block, the values that were in the
    Automated frame's entry widgets when they last lost focus or when the
    last run was started. Restored into the entries at next launch.

  - ``profiles/{name}.json``  -- named bundles of the same fields. Users
    can save / load / delete via the File menu. Two starter profiles ship
    in the repo's ``profiles/`` directory and are copied into the user's
    config on first launch if not already present.

Field names match the schema below; values are stored as strings (matching
what the entry widgets return) so loading is a simple ``entry.set(value)``
with no parsing.
"""

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger("autosip")

# Fields persisted to ``last_used`` and to each profile. Listed here so the
# GUI and this module agree on the keys.
FIELDS = (
	"project", "sample_id", "plate_id",
	"number_of_fractions", "discard_fractions",
	"prime_time",
	"rows", "cols", "well_size", "pump_rate", "drip_wait_time",
	"purge_time",
	"peristaltic_rate", "max_waste_volume",
	"volume_per_well",
	"table_start", "carriage_start",
	"waste_bin_table", "waste_bin_carriage",
	"waste_bin_x_extent", "waste_bin_y_extent",
	"labware_file",
)

# Current waste-bin anchor convention. v1 = upper-left corner of the
# bin rectangle (pre-2026-06 builds). v2 = center of the bin rectangle.
# Stored next to ``last_used`` and inside each profile as
# ``waste_anchor_version`` so the loader knows whether to migrate.
WASTE_ANCHOR_VERSION = 2

_CONFIG_DIR = Path.home() / ".autosip"


# ---- waste-bin anchor migration -------------------------------------

def _migrate_waste_anchor(payload):
	"""Silently bring a loaded ``last_used`` / profile dict up to the
	current waste-bin anchor convention.

	v1 stored the bin's upper-left corner; v2 stores its center. When
	the payload predates v2 and both extents are non-zero, shift the
	stored ``waste_bin_table`` / ``waste_bin_carriage`` by +extent/2 so
	the previously-corner-anchored value lands on the new center.
	Payloads with zero extents are left untouched (no safe shift) but
	still receive the version stamp so subsequent loads short-circuit.
	"""
	try:
		version = int(payload.get("waste_anchor_version", 1) or 1)
	except (TypeError, ValueError):
		version = 1
	if version >= WASTE_ANCHOR_VERSION:
		return

	def _coerce(key):
		raw = payload.get(key, "")
		if raw in (None, ""):
			return None
		try:
			return float(raw)
		except (TypeError, ValueError):
			return None

	wx = _coerce("waste_bin_table")
	wy = _coerce("waste_bin_carriage")
	ext_x = _coerce("waste_bin_x_extent") or 0.0
	ext_y = _coerce("waste_bin_y_extent") or 0.0

	if wx is not None and ext_x > 0:
		payload["waste_bin_table"] = f"{wx + ext_x / 2.0:.2f}"
	if wy is not None and ext_y > 0:
		payload["waste_bin_carriage"] = f"{wy + ext_y / 2.0:.2f}"
	payload["waste_anchor_version"] = WASTE_ANCHOR_VERSION


def get_config_dir():
	return _CONFIG_DIR


def get_profiles_dir():
	return _CONFIG_DIR / "profiles"


def get_config_path():
	return _CONFIG_DIR / "config.json"


# -- last_used ---------------------------------------------------------

def load_last_used():
	"""Return the ``last_used`` dict from config.json, or ``{}`` if absent.

	Applies a one-time migration for ``volume_per_well``: if the persisted
	value falls outside the current ``validation.VOLUME_MIN``/``MAX`` bounds
	(e.g., a config saved before the 0.1-2.0 mL clamp was introduced),
	reset it to ``0.22`` mL and rewrite the file so subsequent launches
	don't re-run the migration.
	"""
	path = get_config_path()
	if not path.exists():
		return {}
	try:
		with open(path) as f:
			data = json.load(f)
		last = dict(data.get("last_used") or {})
	except (OSError, json.JSONDecodeError) as exc:
		logger.warning("Failed to read %s: %s", path, exc)
		return {}

	# Waste-anchor (corner → center) migration. Carry the version key
	# inside ``last`` so ``_migrate_waste_anchor`` can read it; persist
	# the migrated payload back to disk so future launches no-op.
	if "waste_anchor_version" not in last:
		# Inherit the version sentinel that lives next to ``last_used``
		# in older builds, if present; default to v1 so legacy configs
		# get migrated.
		last["waste_anchor_version"] = data.get("waste_anchor_version", 1)
	try:
		_pre_version = int(last.get("waste_anchor_version", 1) or 1)
	except (TypeError, ValueError):
		_pre_version = 1
	_migrate_waste_anchor(last)
	if _pre_version < WASTE_ANCHOR_VERSION:
		try:
			save_last_used(last)
		except OSError:
			pass

	# Volume-bound migration. Local import keeps validation/config_store
	# free of a circular dependency at module load time.
	try:
		import validation
		raw = last.get("volume_per_well")
		if raw is not None and raw != "":
			try:
				v = float(raw)
			except (TypeError, ValueError):
				v = None
			if v is not None and (v < validation.VOLUME_MIN or v > validation.VOLUME_MAX):
				logger.info(
					"Volume per well last_used value %s outside new bounds "
					"[%g, %g] mL; reset to 0.22 mL.",
					raw, validation.VOLUME_MIN, validation.VOLUME_MAX,
				)
				last["volume_per_well"] = "0.22"
				try:
					save_last_used(last)
				except OSError as exc:
					logger.warning("Could not persist volume migration: %s", exc)
	except ImportError:
		pass

	return last


def save_last_used(values):
	"""Write the ``last_used`` block to config.json, preserving other keys.

	Filters ``values`` to the known FIELDS so a misbehaving caller can't
	pollute the config with arbitrary keys. Stamps the current
	``waste_anchor_version`` next to ``last_used`` so the corner→center
	migration runs at most once per machine.
	"""
	path = get_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)

	# Preserve any other top-level keys an older/newer version may have written.
	existing = {}
	if path.exists():
		try:
			with open(path) as f:
				existing = json.load(f)
		except (OSError, json.JSONDecodeError):
			existing = {}
	if not isinstance(existing, dict):
		existing = {}

	existing["last_used"] = {k: values.get(k, "") for k in FIELDS}
	existing["waste_anchor_version"] = WASTE_ANCHOR_VERSION
	with open(path, "w") as f:
		json.dump(existing, f, indent=2)


# -- profiles ----------------------------------------------------------

def list_profiles():
	"""Return a sorted list of profile names (sans .json extension)."""
	d = get_profiles_dir()
	if not d.is_dir():
		return []
	return sorted(p.stem for p in d.glob("*.json"))


def _profile_path(name):
	# Strip any extension / leading directory the caller passed; profiles
	# live at top level of get_profiles_dir() and the .json suffix is added.
	stem = Path(name).stem
	return get_profiles_dir() / f"{stem}.json"


def load_profile(name):
	"""Return the profile's values as a dict (only FIELDS keys).

	Applies the waste-anchor (corner → center) migration if the
	profile predates v2. Persists the migrated payload back to the
	profile file so subsequent loads are no-ops.
	"""
	path = _profile_path(name)
	with open(path) as f:
		data = json.load(f)
	if not isinstance(data, dict):
		raise ValueError(f"Profile {name!r} is not a JSON object")

	# Migration uses the on-disk dict directly so the version stamp
	# lands inside the profile file. Persist unconditionally whenever
	# the on-disk version was below current — covers both the
	# coords-shifted path and the extent-0 "stamp only" path.
	try:
		on_disk_version = int(data.get("waste_anchor_version", 1) or 1)
	except (TypeError, ValueError):
		on_disk_version = 1
	_migrate_waste_anchor(data)
	if on_disk_version < WASTE_ANCHOR_VERSION:
		try:
			path.parent.mkdir(parents=True, exist_ok=True)
			with open(path, "w") as f:
				json.dump(data, f, indent=2)
		except OSError:
			pass

	return {k: data.get(k, "") for k in FIELDS}


def save_profile(name, values):
	"""Write ``values`` (filtered to FIELDS) to ``profiles/{name}.json``.

	Stamps the current ``waste_anchor_version`` next to the FIELDS so
	the corner→center migration knows the file is current.
	"""
	path = _profile_path(name)
	path.parent.mkdir(parents=True, exist_ok=True)
	payload = {k: values.get(k, "") for k in FIELDS}
	payload["waste_anchor_version"] = WASTE_ANCHOR_VERSION
	with open(path, "w") as f:
		json.dump(payload, f, indent=2)


def delete_profile(name):
	"""Delete ``profiles/{name}.json`` if it exists."""
	path = _profile_path(name)
	path.unlink(missing_ok=True)


# -- last_pump_used ----------------------------------------------------

# Stored at the top level of config.json (outside the ``last_used`` block
# of FIELDS) because it is App-level UI state, not a profile field.

def load_last_pump_used():
	"""Return the persisted Manual-mode default pump for the space-bar
	shortcut. ``"fractionate"`` or ``"purge"`` -- any other value (or a
	missing/corrupt file) falls back to the safe default ``"fractionate"``.
	"""
	path = get_config_path()
	if not path.exists():
		return "fractionate"
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError):
		return "fractionate"
	val = data.get("last_pump_used") if isinstance(data, dict) else None
	return val if val in ("fractionate", "purge") else "fractionate"


def save_last_pump_used(name):
	"""Persist the Manual-mode default pump to config.json.

	Preserves all other top-level keys. Silently ignores values that are
	not ``"fractionate"`` or ``"purge"``.
	"""
	if name not in ("fractionate", "purge"):
		return
	path = get_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	existing = {}
	if path.exists():
		try:
			with open(path) as f:
				existing = json.load(f)
		except (OSError, json.JSONDecodeError):
			existing = {}
	if not isinstance(existing, dict):
		existing = {}
	existing["last_pump_used"] = name
	with open(path, "w") as f:
		json.dump(existing, f, indent=2)


# -- return_to_origin_on_exit -----------------------------------------

# Top-level boolean preference (alongside ``last_pump_used``). Defaults
# True so a fresh install gets the safe behavior of parking the needle
# at the origin before the window closes.

def load_return_to_origin_on_exit():
	"""Return the persisted close-handler preference. True if the
	value is missing, malformed, or the config file doesn't exist."""
	path = get_config_path()
	if not path.exists():
		return True
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError):
		return True
	if not isinstance(data, dict):
		return True
	val = data.get("return_to_origin_on_exit", True)
	return bool(val)


def save_return_to_origin_on_exit(enabled):
	"""Persist the close-handler preference to config.json. Preserves
	all other top-level keys."""
	path = get_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	existing = {}
	if path.exists():
		try:
			with open(path) as f:
				existing = json.load(f)
		except (OSError, json.JSONDecodeError):
			existing = {}
	if not isinstance(existing, dict):
		existing = {}
	existing["return_to_origin_on_exit"] = bool(enabled)
	with open(path, "w") as f:
		json.dump(existing, f, indent=2)


# -- skip_intersample_purge -------------------------------------------

# Top-level boolean preference. Was previously stored under ``last_used``
# but moved here because it's a behavioral preference (like
# ``return_to_origin_on_exit``), not a per-run parameter.

def load_skip_intersample_purge():
	"""Return the persisted Skip-purge preference. False if missing,
	malformed, or the config file doesn't exist (safe default: a
	multi-sample run does a full inter-sample flush)."""
	path = get_config_path()
	if not path.exists():
		return False
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError):
		return False
	if not isinstance(data, dict):
		return False
	val = data.get("skip_intersample_purge", False)
	return bool(val)


def save_skip_intersample_purge(enabled):
	"""Persist the Skip-purge preference to config.json. Preserves all
	other top-level keys."""
	path = get_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	existing = {}
	if path.exists():
		try:
			with open(path) as f:
				existing = json.load(f)
		except (OSError, json.JSONDecodeError):
			existing = {}
	if not isinstance(existing, dict):
		existing = {}
	existing["skip_intersample_purge"] = bool(enabled)
	with open(path, "w") as f:
		json.dump(existing, f, indent=2)


# -- purge_protocol ----------------------------------------------------

# Top-level string preference selecting the inter-sample purge
# workflow. ``"basic"`` is the default three-phase water flush + air
# clear + syringe priming. ``"decontamination"`` expands to a
# five-phase water → bleach → water → air → prime sequence.

def load_purge_protocol():
	"""Return the persisted protocol id (``"basic"`` or
	``"decontamination"``). ``"basic"`` when missing, malformed, or the
	config file doesn't exist."""
	path = get_config_path()
	if not path.exists():
		return "basic"
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError):
		return "basic"
	if not isinstance(data, dict):
		return "basic"
	val = data.get("purge_protocol", "basic")
	return val if val in ("basic", "decontamination") else "basic"


def save_purge_protocol(protocol):
	"""Persist the protocol id to config.json. Preserves other top-level
	keys. Silently ignores unknown values."""
	if protocol not in ("basic", "decontamination"):
		return
	path = get_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	existing = {}
	if path.exists():
		try:
			with open(path) as f:
				existing = json.load(f)
		except (OSError, json.JSONDecodeError):
			existing = {}
	if not isinstance(existing, dict):
		existing = {}
	existing["purge_protocol"] = protocol
	with open(path, "w") as f:
		json.dump(existing, f, indent=2)


# -- notification_config (separate file) -------------------------------

# Notifications live in their own file (NOT config.json) because the
# ntfy topic is sensitive: anyone who knows it can read the operator's
# push stream. Kept out of git via .gitignore, and intentionally not
# included in saved profiles so a shared profile doesn't leak the
# topic.

# ``audible_enabled`` was removed from the operator-facing Preferences
# (Pi lacks native audio; ntfy push covers the away-from-machine case).
# Older notification_config.json files may still carry the key — it is
# tolerated by the JSON loader and silently ignored on read. The audible
# helper code in notifications.py is retained for a future build with a
# speaker.
_NOTIFICATION_DEFAULTS = {
	"ntfy_enabled": False,
	"ntfy_server": "ntfy.sh",
	"ntfy_topic": "",
}


def get_notification_config_path():
	return _CONFIG_DIR / "notification_config.json"


def load_notification_config():
	"""Return the notification config dict, filling unset keys from
	defaults. Returns a fresh dict on every call so callers can mutate
	freely without poisoning the defaults."""
	out = dict(_NOTIFICATION_DEFAULTS)
	path = get_notification_config_path()
	if not path.exists():
		return out
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError):
		return out
	if not isinstance(data, dict):
		return out
	# ntfy enable flag coerced to bool so a malformed value can't
	# disable the on-screen dialog or anything else upstream.
	# ``audible_enabled`` is intentionally NOT loaded — see the
	# defaults block above.
	out["ntfy_enabled"] = bool(data.get("ntfy_enabled", False))
	server = data.get("ntfy_server")
	if isinstance(server, str) and server.strip():
		out["ntfy_server"] = server.strip()
	topic = data.get("ntfy_topic")
	if isinstance(topic, str):
		# Empty string is a valid "no topic" state — preserve it.
		out["ntfy_topic"] = topic.strip()
	return out


def save_notification_config(values):
	"""Persist the notification config to its dedicated file. Only
	known keys are written so an upstream caller can't smuggle
	arbitrary state into the file."""
	if not isinstance(values, dict):
		return
	payload = {
		"ntfy_enabled": bool(values.get("ntfy_enabled", False)),
		"ntfy_server": str(values.get("ntfy_server") or "ntfy.sh"),
		"ntfy_topic": str(values.get("ntfy_topic") or ""),
	}
	path = get_notification_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "w") as f:
		json.dump(payload, f, indent=2)


# -- motor_speed_mode / transit_speed_factor ---------------------------

# Top-level preferences governing stepper motor speed. ``"slow"`` (the
# default) drives every move at the fractionation step delay so
# droplets don't fling from the syringe during transit. ``"variable"``
# keeps fractionation (well-to-well) moves slow but speeds up transit
# moves (to/from waste bin, return to origin, plate swaps) by the
# configured factor.

def load_motor_speed_mode():
	"""Return ``"slow"`` or ``"variable"``. Defaults to ``"slow"`` so
	a fresh launch — and any existing config from before this feature —
	preserves the original behavior with no surprises."""
	path = get_config_path()
	if not path.exists():
		return "slow"
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError):
		return "slow"
	if not isinstance(data, dict):
		return "slow"
	val = data.get("motor_speed_mode")
	return val if val in ("slow", "variable") else "slow"


def save_motor_speed_mode(mode):
	"""Persist the motor speed mode to config.json. Preserves other
	top-level keys. Silently ignores unknown values."""
	if mode not in ("slow", "variable"):
		return
	path = get_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	existing = {}
	if path.exists():
		try:
			with open(path) as f:
				existing = json.load(f)
		except (OSError, json.JSONDecodeError):
			existing = {}
	if not isinstance(existing, dict):
		existing = {}
	existing["motor_speed_mode"] = mode
	with open(path, "w") as f:
		json.dump(existing, f, indent=2)


def load_transit_speed_factor():
	"""Return the persisted transit speed factor. Defaults to ``2.0``.
	Clamped to the validation bounds on read so a malformed config
	can't drive the motors at absurd rates."""
	path = get_config_path()
	if not path.exists():
		return 2.0
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError):
		return 2.0
	if not isinstance(data, dict):
		return 2.0
	raw = data.get("transit_speed_factor")
	try:
		val = float(raw)
	except (TypeError, ValueError):
		return 2.0
	# Defensive clamp; mirrors validation.TRANSIT_SPEED_FACTOR_{MIN,MAX}
	# without depending on it at import time.
	return max(1.0, min(5.0, val))


def save_transit_speed_factor(factor):
	"""Persist the transit speed factor to config.json. Preserves
	other top-level keys."""
	try:
		val = float(factor)
	except (TypeError, ValueError):
		return
	val = max(1.0, min(5.0, val))
	path = get_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	existing = {}
	if path.exists():
		try:
			with open(path) as f:
				existing = json.load(f)
		except (OSError, json.JSONDecodeError):
			existing = {}
	if not isinstance(existing, dict):
		existing = {}
	existing["transit_speed_factor"] = val
	with open(path, "w") as f:
		json.dump(existing, f, indent=2)


# -- plate_orientation -------------------------------------------------

# Top-level string preference selecting how the plate sits on the XY
# table. ``"portrait"`` (default for fresh installs): plate rows along
# the X-axis (table), columns along the Y-axis (carriage).
# ``"landscape"``: columns on X, rows on Y. Origin (0, 0) is the
# upper-left mechanical limit and motor reverse flags are fixed
# regardless of orientation — switching only changes which plate axis
# maps to which motor axis when the operator calibrates the Starting
# Well Position.

def load_plate_orientation():
	"""Return the persisted plate orientation. ``"portrait"`` for fresh
	installs (no config.json on disk). For pre-existing configs that
	predate the orientation feature, defaults to ``"landscape"`` to
	preserve the previously-working setup — those operators must
	deliberately switch to portrait via Tools → Preferences."""
	path = get_config_path()
	if not path.exists():
		return "portrait"
	try:
		with open(path) as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError):
		return "portrait"
	if not isinstance(data, dict):
		return "portrait"
	val = data.get("plate_orientation")
	if val in ("portrait", "landscape"):
		return val
	# Config exists but no key — existing user from pre-orientation
	# build. Keep their working setup (landscape).
	return "landscape"


def save_plate_orientation(orientation):
	"""Persist the plate orientation to config.json. Preserves other
	top-level keys. Silently ignores unknown values."""
	if orientation not in ("portrait", "landscape"):
		return
	path = get_config_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	existing = {}
	if path.exists():
		try:
			with open(path) as f:
				existing = json.load(f)
		except (OSError, json.JSONDecodeError):
			existing = {}
	if not isinstance(existing, dict):
		existing = {}
	existing["plate_orientation"] = orientation
	with open(path, "w") as f:
		json.dump(existing, f, indent=2)


# -- starter profiles --------------------------------------------------

def seed_starter_profiles(source_dir):
	"""Copy any ``*.json`` from ``source_dir`` into the user's profiles dir
	IF not already present. Idempotent -- safe to call on every launch.
	"""
	src = Path(source_dir)
	if not src.is_dir():
		return
	dest = get_profiles_dir()
	dest.mkdir(parents=True, exist_ok=True)
	for src_file in src.glob("*.json"):
		dest_file = dest / src_file.name
		if not dest_file.exists():
			try:
				shutil.copy(src_file, dest_file)
				logger.info("Seeded starter profile %s", dest_file)
			except OSError as exc:
				logger.warning("Could not seed %s: %s", dest_file, exc)
