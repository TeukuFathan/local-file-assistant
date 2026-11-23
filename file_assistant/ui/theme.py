"""Local fonts, rounded surfaces and native ttk control styling; no assets needed."""

import math
import tkinter as tk
from tkinter import font as tkfont, ttk


COLORS = {
    "background": "#F4F6FB",
    "surface": "#FFFFFF",
    "text": "#172033",
    "muted": "#667085",
    "border": "#E3E8F0",
    "accent": "#5146D8",
    "accent_hover": "#4338BE",
    "tint": "#EEEDFC",
    "soft": "#F0F3F8",
}


def choose_font(families, default):
    available = {family.casefold(): family for family in families}
    for family in ("Inter", "Inter Variable", "Segoe UI Variable Text", "Segoe UI", "Arial"):
        if family.casefold() in available:
            return available[family.casefold()]
    return default


def _button_image(master, fill, border, backdrop):
    """Small rounded stretchable image, with blended edges for smooth corners."""
    size, radius = 32, 11
    image = tk.PhotoImage(master=master, width=size, height=size)

    def rgb(color):
        return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))

    def blend(first, second, amount):
        return "#" + "".join(f"{round(a * amount + b * (1 - amount)):02x}"
                             for a, b in zip(rgb(first), rgb(second)))

    for y in range(size):
        row = []
        for x in range(size):
            dx = max(radius - x - .5, x + .5 - (size - radius), 0)
            dy = max(radius - y - .5, y + .5 - (size - radius), 0)
            distance = math.hypot(dx, dy) - radius
            coverage = max(0, min(1, .5 - distance))
            inner = max(0, min(1, -.5 - distance))
            color = blend(fill, border, inner)
            row.append(blend(color, backdrop, coverage))
        image.put("{" + " ".join(row) + "}", to=(0, y))
    return image


def apply_theme(window):
    family = choose_font(tkfont.families(window), tkfont.nametofont("TkDefaultFont", root=window).actual("family"))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name, root=window).configure(family=family, size=11)
    window.configure(background=COLORS["background"])
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure("TFrame", background=COLORS["surface"])
    style.configure("TLabel", background=COLORS["surface"], foreground=COLORS["text"])
    style.configure("TEntry", fieldbackground=COLORS["surface"], foreground=COLORS["text"],
                    borderwidth=0, padding=5, insertcolor=COLORS["accent"])
    style.layout("Flat.TEntry", [("Entry.padding", {"sticky": "nswe", "children": [
        ("Entry.textarea", {"sticky": "nswe"}),
    ]})])
    style.configure("TCheckbutton", background=COLORS["surface"], foreground=COLORS["text"],
                    indicatorcolor=COLORS["surface"], padding=4)
    style.map("TCheckbutton", background=[("active", COLORS["surface"])],
              indicatorcolor=[("selected", COLORS["accent"])])
    style.configure("Vertical.TScrollbar", background="#CBD2DF", troughcolor=COLORS["background"],
                    bordercolor=COLORS["background"], lightcolor="#CBD2DF", darkcolor="#CBD2DF",
                    borderwidth=0, arrowsize=9, width=9, gripcount=0)
    style.map("Vertical.TScrollbar", background=[("active", "#A7B2C5"), ("!active", "#CBD2DF")])
    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"expand": True, "sticky": "nswe"}),
    ]})])
    # Retain real ttk buttons: focus, keyboard invocation and disabled-state
    # enforcement remain native rather than being reimplemented on a canvas.
    images = []
    for name, fill, hover, foreground in (
        ("Primary.TButton", COLORS["accent"], COLORS["accent_hover"], "#FFFFFF"),
        ("Soft.TButton", COLORS["tint"], "#E0DDF9", COLORS["accent"]),
        ("Quiet.TButton", COLORS["soft"], "#E5EAF2", COLORS["text"]),
    ):
        normal = _button_image(window, fill, fill, COLORS["surface"])
        active = _button_image(window, hover, hover, COLORS["surface"])
        focused = _button_image(window, fill, COLORS["text"], COLORS["surface"])
        disabled = _button_image(window, "#F0F2F6", "#F0F2F6", COLORS["surface"])
        images.extend((normal, active, focused, disabled))
        element = name + ".rounded"
        style.element_create(element, "image", normal, ("disabled", disabled),
                             ("pressed", active), ("active", active), ("focus", focused),
                             border=(11, 11, 11, 11), sticky="nswe")
        style.layout(name, [(element, {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [
                ("Button.label", {"sticky": "nswe"}),
            ]}),
        ]})])
        style.configure(name, padding=(16, 10), font=(family, 10, "bold"), foreground=foreground)
        style.map(name, foreground=[("disabled", "#98A2B3")])
    window._theme_images = images  # Tk images need Python references for their lifetime.
    return family


class RoundedPanel(tk.Canvas):
    """Rounded backdrop with an ordinary frame for accessible child widgets."""

    def __init__(self, master, *, surface=None, backdrop=None, padding=18, **kwargs):
        self.surface = surface or COLORS["surface"]
        self.padding = padding
        super().__init__(master, background=backdrop or COLORS["background"],
                         highlightthickness=0, borderwidth=0, height=1, **kwargs)
        self.shape = self.create_polygon(0, 0, 0, 0, smooth=True, splinesteps=24,
                                         fill=self.surface, outline=COLORS["border"])
        self.content = tk.Frame(self, background=self.surface)
        self.content_id = self.create_window(padding, padding, anchor="nw", window=self.content)
        self.bind("<Configure>", self._resize)
        self.content.bind("<Configure>", self._fit_content)

    def _fit_content(self, event=None):
        wanted = self.content.winfo_reqheight() + 2 * self.padding
        if int(self.cget("height")) != wanted:
            self.configure(height=wanted)

    def _resize(self, event):
        self.itemconfigure(self.content_id, width=max(1, event.width - 2 * self.padding))
        # A one-pixel initial canvas can clip the frame before it receives its
        # own Configure event. Request its full height from the canvas as well.
        self._fit_content()
        x, y, right, bottom = 1, 1, event.width - 1, event.height - 1
        radius = max(0, min(18, (right - x) / 2, (bottom - y) / 2))
        self.coords(self.shape, x + radius, y, right - radius, y, right, y,
                    right, y + radius, right, bottom - radius, right, bottom,
                    right - radius, bottom, x + radius, bottom, x, bottom,
                    x, bottom - radius, x, y + radius, x, y)
