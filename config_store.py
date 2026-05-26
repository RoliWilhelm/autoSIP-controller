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
	"rows", "cols", "well_size", "pump_rate", "drip_wait_time",
	"purge_time",
	"peristaltic_rate", "max_waste_volume",
	"volume_per_well",
	"table_start", "carriage_start",
	"waste_bin_table", "waste_bin_carriage",
	"labware_file",
)

_CONFIG_DIR = Path.home() / ".autosip"


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
	pollute the config with arbitrary keys.
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
	"""Return the profile's values as a dict (only FIELDS keys)."""
	path = _profile_path(name)
	with open(path) as f:
		data = json.load(f)
	if not isinstance(data, dict):
		raise ValueError(f"Profile {name!r} is not a JSON object")
	return {k: data.get(k, "") for k in FIELDS}


def save_profile(name, values):
	"""Write ``values`` (filtered to FIELDS) to ``profiles/{name}.json``."""
	path = _profile_path(name)
	path.parent.mkdir(parents=True, exist_ok=True)
	payload = {k: values.get(k, "") for k in FIELDS}
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
