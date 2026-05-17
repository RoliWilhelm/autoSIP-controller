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
	"project", "sample_id",
	"number_of_fractions", "discard_fractions",
	"rows", "cols", "well_size", "pump_rate", "volume_per_well",
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
	"""Return the ``last_used`` dict from config.json, or ``{}`` if absent."""
	path = get_config_path()
	if not path.exists():
		return {}
	try:
		with open(path) as f:
			data = json.load(f)
		return dict(data.get("last_used") or {})
	except (OSError, json.JSONDecodeError) as exc:
		logger.warning("Failed to read %s: %s", path, exc)
		return {}


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
