"""Optional, non-blocking, fail-safe operator notifications for autoSIP.

The on-screen dialog at every manual-intervention point is the source of
truth. These notifications are SUPPLEMENTARY — phone alerts and an
optional local beep — so the operator can step away from the bench and
be called back when the system needs them. Every send happens in a
daemon thread with a hard timeout; every failure path is logged and
swallowed so a network drop or missing audio device never blocks or
crashes a run.

Two channels:

  * Audible: local-only. Tries ``aplay`` against a short bundled .wav
    if present; otherwise writes ``\\a`` to stderr (terminal bell).
    Repeats 2-3 times when ``urgent`` is True.

  * ntfy push: HTTP POST to ``https://{server}/{topic}`` with the
    message as the body, a ``Title`` header, and ``Priority: high`` on
    urgent. Default server ``ntfy.sh``. Requires the ``requests``
    package; if missing, the channel logs once and degrades to no-op.

The ``NotificationManager`` is constructed by ``App`` with a read-only
view of the current notification configuration (audible enabled, ntfy
enabled, server, topic). The config is reloaded on every notify() call
so a mid-session Preferences change takes effect immediately without
recreating the manager.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger("autosip")

# Hard ceiling on the HTTP request. ntfy.sh is usually < 1 s; 10 s is
# generous for slow networks and still fast enough that a stuck request
# can't pile up state on a busy run.
_NTFY_TIMEOUT_S = 10.0

# Per-thread join cap so notify() returns to the GUI thread promptly
# even if the daemon thread is still sending. The thread continues in
# the background — the join is just for telemetry / logging context.
_THREAD_JOIN_S = 0.1


class NotificationManager:
	"""Dispatch supplementary notifications to enabled channels.

	Construction is cheap — the manager just stashes a callable that
	returns the live notification config. Every ``notify()`` call reads
	the config fresh so a Preferences change applies immediately.

	The ``config_provider`` callable should return a dict shaped like::

	    {
	      "audible_enabled": bool,
	      "ntfy_enabled": bool,
	      "ntfy_server": str,    # e.g. "ntfy.sh"
	      "ntfy_topic": str,     # operator-chosen, optionally blank
	    }

	Missing keys are treated as falsey / empty.
	"""

	def __init__(self, config_provider):
		self._config_provider = config_provider
		# Track whether we've already logged that ``requests`` is
		# missing, so a long run doesn't spam the log on every notify.
		self._requests_missing_warned = False

	# -- public API -----------------------------------------------------

	def notify(self, title, message, urgent=False):
		"""Fire a notification to every enabled channel.

		Non-blocking: each enabled channel runs in its own daemon thread
		with a hard timeout. Returns immediately. Failures are logged
		at WARNING and swallowed — the caller's flow continues as if
		the notification path didn't exist.
		"""
		try:
			cfg = self._read_config()
		except Exception as exc:
			logger.warning("notifications: config read failed: %s", exc)
			return
		if cfg.get("audible_enabled"):
			self._spawn(self._audible, title, message, urgent, cfg)
		if cfg.get("ntfy_enabled") and cfg.get("ntfy_topic"):
			self._spawn(self._ntfy, title, message, urgent, cfg)

	def send_test(self, title="autoSIP test",
			message="Notifications are working."):
		"""Synchronous-feeling test from Preferences' "Send Test
		Notification" button. Performs the same dispatch as ``notify``
		but ALSO returns a (ok, detail) tuple for the in-app result
		label. ``ok`` is True when every enabled channel reported
		success within a short timeout; ``detail`` is a short human
		message ("Test sent" / "ntfy HTTP 401" / etc.).

		Audible always counts as "ok" — it logs but never reports
		failure to the caller (a missing audio device is still a
		successful "best-effort" notify in the spec's sense).
		"""
		try:
			cfg = self._read_config()
		except Exception as exc:
			return False, f"Config read failed: {exc}"
		channels_tried = 0
		# Audible: fire-and-forget. Always returns ok=True if enabled.
		if cfg.get("audible_enabled"):
			channels_tried += 1
			self._spawn(self._audible, title, message, False, cfg)
		# ntfy: synchronous within the test path so we can report the
		# real HTTP outcome. Still wrapped in try/except.
		ntfy_detail = None
		ntfy_ok = True
		if cfg.get("ntfy_enabled") and cfg.get("ntfy_topic"):
			channels_tried += 1
			ntfy_ok, ntfy_detail = self._ntfy_test(title, message, cfg)
		if channels_tried == 0:
			return False, "No notification channels enabled"
		if not ntfy_ok:
			return False, f"Test failed — {ntfy_detail or 'see log'}"
		return True, "Test sent"

	# -- internals ------------------------------------------------------

	def _read_config(self):
		raw = self._config_provider() or {}
		if not isinstance(raw, dict):
			return {}
		return raw

	def _spawn(self, target, title, message, urgent, cfg):
		t = threading.Thread(
			target=self._safe_invoke,
			args=(target, title, message, urgent, cfg),
			daemon=True,
		)
		t.start()
		t.join(_THREAD_JOIN_S)

	def _safe_invoke(self, target, title, message, urgent, cfg):
		"""Wrap a channel send in try/except. Any exception is logged
		at WARNING and absorbed — never re-raised into the daemon's
		caller (which is itself called from the worker thread)."""
		try:
			target(title, message, urgent, cfg)
		except Exception as exc:
			logger.warning(
				"notifications: %s send failed: %s",
				getattr(target, "__name__", repr(target)), exc,
			)

	# -- audible channel ------------------------------------------------

	def _audible(self, title, message, urgent, cfg):
		"""Play a short local beep. Prefer ``aplay`` against a bundled
		``alert.wav`` if both exist; otherwise emit terminal bells.
		Urgent triples the count for a 3-beep stutter."""
		count = 3 if urgent else 1
		wav_path = self._bundled_wav()
		aplay = shutil.which("aplay")
		if wav_path is not None and aplay is not None:
			for _ in range(count):
				try:
					subprocess.run(
						[aplay, "-q", str(wav_path)],
						timeout=3.0,
						stdout=subprocess.DEVNULL,
						stderr=subprocess.DEVNULL,
						check=False,
					)
				except (subprocess.TimeoutExpired, OSError) as exc:
					logger.warning(
						"notifications: aplay failed (%s); "
						"falling back to terminal bell", exc,
					)
					self._bell(count)
					return
			return
		# No bundled wav or no aplay — terminal bell. Best-effort: on a
		# headless service this writes to nowhere visible, but the GUI
		# operator (if any) hears the system bell.
		self._bell(count)

	@staticmethod
	def _bundled_wav():
		"""Return the path to ``alert.wav`` bundled alongside this
		module, or ``None`` if not present. Operators who want a beep
		can drop their own .wav here; absence falls back to the
		terminal bell."""
		p = Path(__file__).resolve().parent / "alert.wav"
		return p if p.exists() else None

	@staticmethod
	def _bell(count):
		try:
			sys.stderr.write("\a" * max(1, int(count)))
			sys.stderr.flush()
		except Exception:
			pass  # Even the bell write is best-effort.

	# -- ntfy channel ---------------------------------------------------

	def _ntfy(self, title, message, urgent, cfg):
		ok, _ = self._ntfy_post(title, message, urgent, cfg)
		# Async path: success / failure logged inside _ntfy_post.
		return ok

	def _ntfy_test(self, title, message, cfg):
		"""Synchronous test variant. Returns (ok, detail)."""
		return self._ntfy_post(title, message, urgent=False, cfg=cfg,
			return_detail=True)

	def _ntfy_post(self, title, message, urgent, cfg, *,
			return_detail=False):
		try:
			import requests  # local import so absence degrades cleanly
		except ImportError:
			if not self._requests_missing_warned:
				logger.warning(
					"notifications: 'requests' package not installed; "
					"ntfy push disabled. Install with: pip install requests"
				)
				self._requests_missing_warned = True
			detail = "requests package not installed"
			return False, detail
		server = (cfg.get("ntfy_server") or "ntfy.sh").strip().strip("/")
		topic = (cfg.get("ntfy_topic") or "").strip().strip("/")
		if not topic:
			return False, "ntfy topic is empty"
		# Defensive scheme handling. ntfy.sh defaults to https; allow
		# the operator to type "http://..." for a self-hosted server.
		if server.startswith("http://") or server.startswith("https://"):
			url = f"{server}/{topic}"
		else:
			url = f"https://{server}/{topic}"
		headers = {"Title": _ascii_header(title)}
		if urgent:
			headers["Priority"] = "high"
		try:
			resp = requests.post(
				url,
				data=message.encode("utf-8"),
				headers=headers,
				timeout=_NTFY_TIMEOUT_S,
			)
		except Exception as exc:
			logger.warning("notifications: ntfy POST failed: %s", exc)
			return False, str(exc)
		if 200 <= resp.status_code < 300:
			logger.debug(
				"notifications: ntfy push OK (%d) to %s",
				resp.status_code, url,
			)
			return True, f"HTTP {resp.status_code}"
		logger.warning(
			"notifications: ntfy returned HTTP %d at %s: %s",
			resp.status_code, url, resp.text[:200],
		)
		return False, f"HTTP {resp.status_code}"


def _ascii_header(value):
	"""Best-effort ASCII-clean a header value. ntfy's Title header is
	HTTP — non-ASCII bytes there can blow up some upstream proxies.
	Operators rarely put emoji in run alerts, but if they do, drop the
	non-encodable bits so the request still goes through."""
	try:
		return value.encode("ascii", "ignore").decode("ascii")
	except Exception:
		return ""
