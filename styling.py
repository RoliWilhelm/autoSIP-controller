"""Centralized visual styling for the autoSIP Tk GUI.

Exports:
  - ``apply_style(root)`` -- one-shot setup called from ``App.__init__``
    after ``tk.Tk()`` is created and BEFORE any widget construction.
    Configures the ttk theme, default fonts, the window background, and
    a set of ``option_add`` defaults so the rest of the codebase can
    continue to use plain ``tk.Frame`` / ``tk.Label`` / ``tk.Button``
    widgets and pick up the visual treatment automatically.
  - ``PALETTE`` / ``FONTS`` constants exposing the chosen colors and
    fonts for code that needs to apply them explicitly (e.g. primary
    action buttons).
  - ``primary_button(parent, ...)`` -- factory returning a ``tk.Button``
    pre-styled with the accent color, bold font, and increased padding.
    Used for "Begin Fractionation", "Move to Waste Bin", "Home" etc.
  - ``make_centrifuge_tube_canvas(parent, ...)`` -- builds a small
    ``tk.Canvas`` rendering a 45-deg-rotated ultracentrifuge tube
    silhouette. Used as the icon next to the Begin Fractionation
    button.

Color choices meet WCAG AA contrast (>= 4.5:1) for body text against
both the window and frame backgrounds, and for white text on the
accent color.
"""

import math
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk


# ----- Palette --------------------------------------------------------
# Each pair below has been spot-checked against the WCAG AA luminance
# ratio. The fg text colors are deliberately near-black (#222) rather
# than pure black so they don't compete with the accent blue for
# attention; #222 on #ffffff is ~16:1, #222 on #f5f5f5 is ~14:1, white
# on the accent #3066BE is ~5.8:1 -- all well above the 4.5 threshold.
PALETTE = {
	"bg_window":      "#f5f5f5",
	"bg_frame":       "#ffffff",
	"border_frame":   "#d0d0d0",
	"fg_text":        "#222222",
	"fg_muted":       "#555555",
	"accent":         "#3066BE",
	"accent_hover":   "#1F4D9C",
	"accent_active":  "#13386F",
	"accent_fg":      "#ffffff",
	"button_bg":      "#e8e8e8",
	"button_active":  "#d8d8d8",
	"button_border":  "#888888",
	"danger":         "#C0392B",
	"danger_hover":   "#922B21",
	"danger_active":  "#6B1F18",
	"pump_on":        "#27a72c",
	"pump_on_hover":  "#1e7d20",
	"pump_locked":    "#bdbdbd",
	"pause_running": "#27a72c",
	"pause_paused":  "#6F4E37",
	"mode_inactive": "#e0e0e0",
	"mode_inactive_hover": "#cfcfcf",
}

# ----- Typography -----------------------------------------------------
# DejaVu Sans ships with most Linux distros (including Raspberry Pi
# OS); Helvetica is the macOS fallback. Tk silently substitutes if
# neither is present, but we still resolve to a known-installed family
# at apply_style() time so subsequent option_add calls use a concrete
# name (option_add with a missing family logs a warning on some
# platforms).
_FAMILY_PREFERENCE = ("DejaVu Sans", "Helvetica")
_BODY_SIZE = 11
_HEADING_SIZE = 13

FONTS = {
	# Populated by apply_style() once Tk is initialized; consumers must
	# call apply_style() before reading FONTS.
}


def _choose_family(root):
	"""Return the first preferred family that's actually installed."""
	available = set(tkfont.families(root=root))
	for fam in _FAMILY_PREFERENCE:
		if fam in available:
			return fam
	return "Helvetica"


def apply_style(root):
	"""One-shot UI styling pass. Call exactly once at startup.

	Configures the ttk theme to "clam" (which honors background/
	foreground configuration cross-platform, unlike "aqua" or "vista"),
	sets the default font on every tk widget via ``option_add``, paints
	the root window in the palette's window background, and sets a
	minimum size scaled to the body font so all controls are visible
	at launch without scrolling.

	Must be called BEFORE any widgets are constructed -- ``option_add``
	defaults are consulted at widget creation time, not retroactively.
	"""
	family = _choose_family(root)
	body = (family, _BODY_SIZE)
	bold = (family, _BODY_SIZE, "bold")
	heading = (family, _HEADING_SIZE, "bold")
	mono = ("DejaVu Sans Mono", _BODY_SIZE)
	FONTS["body"] = body
	FONTS["bold"] = bold
	FONTS["heading"] = heading
	FONTS["mono"] = mono
	FONTS["family"] = family
	FONTS["size"] = _BODY_SIZE

	# --- Root chrome ---
	root.configure(bg=PALETTE["bg_window"])

	# --- Default fonts + colors via option_add (affects tk widgets) ---
	root.option_add("*Font", body)
	root.option_add("*Background", PALETTE["bg_window"])
	root.option_add("*Foreground", PALETTE["fg_text"])
	root.option_add("*Frame.Background", PALETTE["bg_frame"])
	root.option_add("*Label.Background", PALETTE["bg_frame"])
	root.option_add("*Label.Foreground", PALETTE["fg_text"])
	root.option_add("*Entry.Background", "#ffffff")
	root.option_add("*Entry.Foreground", PALETTE["fg_text"])
	root.option_add("*Entry.relief", "solid")
	root.option_add("*Entry.borderwidth", 1)
	root.option_add("*Entry.highlightThickness", 0)
	root.option_add("*LabelFrame.Background", PALETTE["bg_frame"])
	root.option_add("*LabelFrame.Foreground", PALETTE["fg_text"])
	root.option_add("*LabelFrame.relief", "solid")
	root.option_add("*LabelFrame.borderwidth", 1)
	root.option_add("*LabelFrame.Font", bold)
	root.option_add("*Button.Background", PALETTE["button_bg"])
	root.option_add("*Button.activeBackground", PALETTE["button_active"])
	root.option_add("*Button.Foreground", PALETTE["fg_text"])
	root.option_add("*Button.activeForeground", PALETTE["fg_text"])
	root.option_add("*Button.relief", "flat")
	root.option_add("*Button.borderwidth", 1)
	root.option_add("*Button.highlightThickness", 0)
	root.option_add("*Button.padX", 6)
	root.option_add("*Button.padY", 3)
	root.option_add("*Radiobutton.Background", PALETTE["bg_frame"])
	root.option_add("*Radiobutton.Foreground", PALETTE["fg_text"])
	root.option_add("*Menu.Background", PALETTE["bg_frame"])
	root.option_add("*Menu.Foreground", PALETTE["fg_text"])
	root.option_add("*Menu.activeBackground", PALETTE["accent"])
	root.option_add("*Menu.activeForeground", PALETTE["accent_fg"])

	# --- ttk theme + styles ---
	style = ttk.Style(root)
	try:
		style.theme_use("clam")
	except tk.TclError:
		# "clam" is shipped with every Tk we expect to encounter, but if
		# the theme is missing for some reason just keep whatever Tk
		# defaulted to. The option_add settings above still give us a
		# coherent look for the plain tk widgets.
		pass

	style.configure("TFrame", background=PALETTE["bg_frame"])
	style.configure("TSeparator", background=PALETTE["border_frame"])
	style.configure("TLabel",
		background=PALETTE["bg_frame"], foreground=PALETTE["fg_text"], font=body)
	style.configure("Heading.TLabel",
		background=PALETTE["bg_frame"], foreground=PALETTE["fg_text"], font=heading)
	style.configure("Muted.TLabel",
		background=PALETTE["bg_frame"], foreground=PALETTE["fg_muted"], font=body)
	style.configure("TLabelframe",
		background=PALETTE["bg_frame"], bordercolor=PALETTE["border_frame"],
		relief="solid", borderwidth=1)
	style.configure("TLabelframe.Label",
		background=PALETTE["bg_frame"], foreground=PALETTE["fg_text"], font=bold)

	# Entries: white field, dark text, thin border. Both ``fieldbackground``
	# (the editable area) and ``background`` (the wider widget frame) get
	# set because some themes paint each.
	style.configure("TEntry",
		fieldbackground="#ffffff", background="#ffffff",
		foreground=PALETTE["fg_text"],
		bordercolor=PALETTE["button_border"], lightcolor=PALETTE["button_border"],
		darkcolor=PALETTE["button_border"],
		borderwidth=1, relief="solid", padding=(4, 2),
		font=body)
	style.map("TEntry",
		fieldbackground=[("disabled", "#f0f0f0")],
		foreground=[("disabled", PALETTE["fg_muted"])])

	# Single uniform button typography. Every role style below INHERITS
	# this font/padding/border. Only color and weight may vary per role.
	style.configure("TButton",
		font=body,
		padding=(8, 4),
		relief="solid",
		borderwidth=1,
		bordercolor=PALETTE["button_border"],
		lightcolor=PALETTE["button_border"],
		darkcolor=PALETTE["button_border"],
		background=PALETTE["button_bg"],
		foreground=PALETTE["fg_text"],
		focuscolor=PALETTE["accent"])
	style.map("TButton",
		background=[("active", PALETTE["button_active"]),
			("disabled", "#dddddd")],
		foreground=[("disabled", "#9a9a9a")])

	# Role styles. Each one only overrides what's different from TButton --
	# font, padding, and border are inherited so all buttons read uniformly.
	def _role(name, *, bg, hover, active=None, pressed=None,
			fg=PALETTE["accent_fg"], font_override=None, disabled_bg=None,
			disabled_fg=None, padding=None):
		opts = {"background": bg, "foreground": fg}
		if font_override is not None:
			opts["font"] = font_override
		if padding is not None:
			opts["padding"] = padding
		style.configure(name, **opts)
		map_opts = {"background": [("active", hover)]}
		if pressed is not None:
			map_opts["background"].append(("pressed", pressed))
		if disabled_bg is not None:
			map_opts["background"].append(("disabled", disabled_bg))
		if disabled_fg is not None:
			map_opts["foreground"] = [("disabled", disabled_fg)]
		style.map(name, **map_opts)

	# Primary: Begin Fractionation, Move to Waste Bin, Home, About/Close.
	_role("Primary.TButton",
		bg=PALETTE["accent"], hover=PALETTE["accent_hover"],
		pressed=PALETTE["accent_active"],
		font_override=bold, padding=(10, 6),
		disabled_bg="#a0a0a0", disabled_fg="#ffffff")

	# Danger: End Run. Same typography as TButton; red bg.
	_role("Danger.TButton",
		bg=PALETTE["danger"], hover=PALETTE["danger_hover"],
		pressed=PALETTE["danger_active"],
		disabled_bg="#dddddd", disabled_fg="#9a9a9a")

	# Pump-button role styles. _update_pump_button switches the button's
	# style name based on (claimant, relay_on, in_run) instead of mutating
	# bg/fg directly (which ttk.Button doesn't support).
	_role("PumpOff.TButton",
		bg=PALETTE["accent"], hover=PALETTE["accent_hover"],
		font_override=bold,
		disabled_bg=PALETTE["accent"], disabled_fg=PALETTE["accent_fg"])
	_role("PumpOn.TButton",
		bg=PALETTE["pump_on"], hover=PALETTE["pump_on_hover"],
		font_override=bold,
		disabled_bg=PALETTE["pump_on"], disabled_fg=PALETTE["accent_fg"])
	_role("PumpLocked.TButton",
		bg=PALETTE["pump_locked"], hover=PALETTE["pump_locked"],
		fg=PALETTE["fg_text"],
		font_override=bold,
		disabled_bg=PALETTE["pump_locked"], disabled_fg=PALETTE["fg_text"])

	# Pause/Resume role styles -- swapped by _update_run_control_buttons.
	# Pause-Idle is the default TButton style; running / paused get colors.
	_role("PauseRunning.TButton",
		bg=PALETTE["pause_running"], hover="#1e7d20",
		disabled_bg=PALETTE["pause_running"], disabled_fg=PALETTE["accent_fg"])
	_role("PausePaused.TButton",
		bg=PALETTE["pause_paused"], hover="#553a29",
		disabled_bg=PALETTE["pause_paused"], disabled_fg=PALETTE["accent_fg"])

	# Mode-tab styles: active mode wears Primary; others use ModeInactive.
	# Both inherit the standard TButton typography.
	_role("ModeActive.TButton",
		bg=PALETTE["accent"], hover=PALETTE["accent_hover"],
		font_override=bold, padding=(10, 8),
		disabled_bg=PALETTE["accent"], disabled_fg=PALETTE["accent_fg"])
	_role("ModeInactive.TButton",
		bg=PALETTE["mode_inactive"], hover=PALETTE["mode_inactive_hover"],
		fg=PALETTE["fg_text"],
		font_override=bold, padding=(10, 8),
		disabled_bg=PALETTE["mode_inactive"], disabled_fg=PALETTE["fg_text"])

	# --- Minimum window size ---
	# Compute from the line height so the floor scales with HiDPI font
	# sizing. Floor + ceiling keep the window usable across resolutions.
	f = tkfont.Font(root=root, family=family, size=_BODY_SIZE)
	line_h = f.metrics("linespace") or 16
	min_w = max(760, line_h * 32)
	min_h = max(720, line_h * 38)
	root.minsize(min_w, min_h)


def primary_button(parent, **kwargs):
	"""Build a ``ttk.Button`` carrying the ``Primary.TButton`` style.

	Used for clearly-primary actions (Begin Fractionation, Move to Waste
	Bin, Home, About/Close). All buttons in the GUI -- primary, danger,
	pump, pause, mode tab, plain -- are ``ttk.Button`` widgets and share
	the same font family/size; only color (and weight, for primaries)
	differs by role.

	Kwargs are forwarded to the constructor; ``style`` defaults to
	``Primary.TButton`` if the caller doesn't override it.
	"""
	kwargs.setdefault("style", "Primary.TButton")
	kwargs.setdefault("cursor", "hand2")
	return ttk.Button(parent, **kwargs)


def make_centrifuge_tube_canvas(parent, size=36, bg=None, tube_color=None):
	"""Render a 45-deg-rotated ultracentrifuge tube silhouette in a small
	``tk.Canvas``. Used as the icon next to the Begin Fractionation
	button; ``bg`` should match the surrounding button color so the
	canvas blends in (defaults to the accent color, ``tube_color`` to
	the accent foreground).

	Geometry is a simplified ASCII-art tube: a small flat cap on top,
	a cylindrical body, and a conical bottom -- all drawn as a single
	closed polygon so the rotation produces a clean silhouette without
	internal seams.
	"""
	if bg is None:
		bg = PALETTE["accent"]
	if tube_color is None:
		tube_color = PALETTE["accent_fg"]

	canvas = tk.Canvas(parent, width=size, height=size, bg=bg,
		highlightthickness=0, bd=0)

	cx, cy = size / 2.0, size / 2.0
	cos_a = math.cos(math.pi / 4.0)
	sin_a = math.sin(math.pi / 4.0)

	def rot(point):
		x, y = point
		dx, dy = x - cx, y - cy
		return (cx + dx * cos_a - dy * sin_a,
			cy + dx * sin_a + dy * cos_a)

	# Unrotated tube outline, centered on (cx, cy). Heights are fractions
	# of the canvas so the same code scales with ``size``.
	body_w = size * 0.34
	cap_w = size * 0.22
	total_h = size * 0.78
	cap_h = size * 0.07
	cone_h = size * 0.14

	top = cy - total_h / 2.0
	cap_top = top
	cap_bot = top + cap_h
	body_top = cap_bot
	body_bot = cy + total_h / 2.0 - cone_h
	cone_tip = cy + total_h / 2.0

	# Counter-clockwise outline. Cap is narrower than body; the step
	# happens between cap_bot and body_top.
	outline = [
		(cx - cap_w / 2, cap_top),
		(cx + cap_w / 2, cap_top),
		(cx + cap_w / 2, cap_bot),
		(cx + body_w / 2, body_top),
		(cx + body_w / 2, body_bot),
		(cx, cone_tip),
		(cx - body_w / 2, body_bot),
		(cx - body_w / 2, body_top),
		(cx - cap_w / 2, cap_bot),
	]
	rotated = [rot(p) for p in outline]
	flat = [v for p in rotated for v in p]
	canvas.create_polygon(flat, fill=tube_color, outline=tube_color,
		width=1, smooth=False)
	return canvas


def make_bimodal_distribution_canvas(parent, width=64, height=40, bg=None,
		primary_color=None, secondary_color=None):
	"""Render two overlapping bimodal-Gaussian curves on a small canvas.

	Thematic: in a SIP experiment each sample's DNA density distribution
	is bimodal -- a primary peak at the unlabeled-DNA density and a
	smaller secondary peak at the heavy-isotope density. The size of the
	secondary peak is the operator's readout of isotope incorporation.
	Drawing two such curves side-by-side gives a quick visual signature
	of "labeled vs unlabeled" right next to the Begin Fractionation
	button.

	Both curves are computed as the sum of two Gaussians, sampled across
	[0, 1] and normalized so the main peak fills the canvas height. They
	differ only in the amplitude of the secondary peak (curve A larger,
	curve B smaller).
	"""
	if bg is None:
		bg = PALETTE["accent"]
	if primary_color is None:
		primary_color = PALETTE["accent_fg"]            # white
	if secondary_color is None:
		secondary_color = "#FFD966"                       # soft yellow

	canvas = tk.Canvas(parent, width=width, height=height, bg=bg,
		highlightthickness=0, bd=0)

	def _gauss(x, mu, sigma, amp):
		return amp * math.exp(-((x - mu) ** 2) / (2.0 * sigma * sigma))

	def _curve(amp1, mu1, sigma1, amp2, mu2, sigma2):
		n = 64
		pad_x, pad_y = 3, 3
		plot_w = width - 2 * pad_x
		plot_h = height - 2 * pad_y
		ys = []
		xs = []
		for i in range(n):
			x = i / (n - 1)
			y = _gauss(x, mu1, sigma1, amp1) + _gauss(x, mu2, sigma2, amp2)
			xs.append(x)
			ys.append(y)
		ymax = max(ys) or 1.0
		pts = []
		for x, y in zip(xs, ys):
			px = pad_x + x * plot_w
			py = pad_y + (1.0 - y / ymax) * plot_h
			pts.extend([px, py])
		return pts

	# Curve A: larger second hump (heavy-isotope-incorporated population)
	curve_a = _curve(1.0, 0.28, 0.085, 0.70, 0.72, 0.090)
	# Curve B: smaller second hump (lightly-labeled or unlabeled sample)
	curve_b = _curve(1.0, 0.28, 0.085, 0.22, 0.72, 0.090)

	canvas.create_line(*curve_a, fill=primary_color, width=2, smooth=True)
	canvas.create_line(*curve_b, fill=secondary_color, width=2, smooth=True)
	return canvas
