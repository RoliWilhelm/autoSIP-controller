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
	style.configure("TEntry",
		fieldbackground="#ffffff", foreground=PALETTE["fg_text"], font=body)
	style.configure("TButton",
		font=body, padding=4,
		background=PALETTE["button_bg"], foreground=PALETTE["fg_text"])
	style.map("TButton",
		background=[("active", PALETTE["button_active"]),
			("disabled", "#cccccc")],
		foreground=[("disabled", "#888888")])
	style.configure("Primary.TButton",
		font=bold, padding=8,
		background=PALETTE["accent"], foreground=PALETTE["accent_fg"])
	style.map("Primary.TButton",
		background=[("active", PALETTE["accent_hover"]),
			("pressed", PALETTE["accent_active"]),
			("disabled", "#a0a0a0")],
		foreground=[("disabled", "#ffffff")])

	# --- Minimum window size ---
	# Compute from the line height so the floor scales with HiDPI font
	# sizing. Floor + ceiling keep the window usable across resolutions.
	f = tkfont.Font(root=root, family=family, size=_BODY_SIZE)
	line_h = f.metrics("linespace") or 16
	min_w = max(760, line_h * 32)
	min_h = max(720, line_h * 38)
	root.minsize(min_w, min_h)


def primary_button(parent, **kwargs):
	"""Build a ``tk.Button`` styled as a primary action (accent bg, white
	fg, bold font, increased padding). All ``kwargs`` are forwarded to
	the ``tk.Button`` constructor; the style options below take precedence
	if the caller passes conflicting values for them.

	Stays as ``tk.Button`` (rather than ``ttk.Button``) so callers that
	mutate ``btn["bg"]`` / ``btn["text"]`` at runtime (e.g. the
	Fractionate / Purge pump buttons that flip green on relay-on) keep
	working.
	"""
	style_opts = dict(
		bg=PALETTE["accent"],
		fg=PALETTE["accent_fg"],
		activebackground=PALETTE["accent_hover"],
		activeforeground=PALETTE["accent_fg"],
		disabledforeground="#dde6f5",
		relief="flat",
		borderwidth=0,
		highlightthickness=0,
		font=FONTS["bold"],
		padx=10, pady=6,
		cursor="hand2",
	)
	style_opts.update(kwargs)
	return tk.Button(parent, **style_opts)


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
