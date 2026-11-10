#!/usr/bin/env python3
"""
TidyBot - a little robot that lives on your Ubuntu desktop and keeps the
desktop icons arranged.

It naps in a corner. When it notices an icon out of line, it wakes up with a
start, flies over, picks the icon up and slots it into the column on the
right-hand side. Files are never moved to other folders - only their icon
position on the desktop changes.

Usage:
    tidybot.py              start the robot
    tidybot.py --demo       add 4 sample files at random spots and watch
    tidybot.py --dry-run    animate only, never touch anything
    tidybot.py --debug      print what it sees (grid, icons, targets)
    tidybot.py --pause      pause / wake a running robot (it naps)
    tidybot.py --stop       stop a running robot

Settings: ~/.config/tidybot/settings.json (created on first run)
"""
import os

# On Wayland, run through XWayland: it is the only way a GTK3 app can make an
# always-on-top, click-through overlay positioned at screen (0,0).
if os.environ.get("WAYLAND_DISPLAY") and "GDK_BACKEND" not in os.environ:
    os.environ["GDK_BACKEND"] = "x11"

import argparse
import json
import math
import random
import re
import signal
import sys
import time

import cairo

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Gtk, Gdk, GLib, Gio, GdkPixbuf, Pango, PangoCairo
    HAVE_GTK = True
except Exception:  # lets the drawing code be imported for previews
    HAVE_GTK = False

APP = "tidybot"
HOME = os.path.expanduser("~")
CONFIG_PATH = os.path.join(HOME, ".config", APP, "settings.json")
DATA_DIR = os.path.join(HOME, ".local", "share", APP)
HELD_PATH = os.path.join(DATA_DIR, "held.json")
PID_PATH = os.path.join(HOME, ".cache", APP + ".pid")

# Where the desktop-icons extension keeps each icon's spot. Current Ubuntu uses
# the nautilus key; some older versions used the other one. Read in this order,
# write both.
POS_ATTRS = ["metadata::nautilus-icon-position", "metadata::desktop-icon-position"]
HOLD_PREFIX = ".tidybot-hold-"
DING_SCHEMA = "org.gnome.shell.extensions.ding"
# fallback cell sizes for the desktop-icons extension, by its icon-size setting
DING_CELL = {"tiny": (72, 74), "small": (96, 90),
             "standard": (120, 106), "large": (130, 138)}

# ---------------------------------------------------------------- palette ---
BODY_TOP = (0.98, 0.99, 1.00)
BODY_BOT = (0.76, 0.82, 0.92)
OUTLINE = (0.16, 0.22, 0.35)
LIMB = (0.40, 0.47, 0.61)
VISOR_A = (0.09, 0.12, 0.22)
VISOR_B = (0.03, 0.05, 0.10)
EYE = (0.32, 0.92, 1.00)
ACCENT = (1.00, 0.42, 0.45)
ACCENT2 = (1.00, 0.78, 0.30)
CHEEK = (1.00, 0.45, 0.65)
CONFETTI = [EYE, ACCENT, ACCENT2, (0.55, 0.95, 0.55), (0.75, 0.6, 1.0)]

DEFAULT_CONFIG = {
    "skin": "classic",        # see the right-click menu for all 10
    "side": "right",          # "right" or "left": which edge to line icons up on
    "settle_seconds": 0.6,    # wait until icons stop changing this long
    "new_file_grace": 5,      # leave a just-created file alone this long
    "scan_interval": 0.4,     # how often it glances at the desktop
    "scale": 1.0,             # robot size
    "speed": 520,             # flight speed, px/s
    "ignore": [],             # names the robot never touches
    "coord_scale": 1.0,       # only change if the robot aims at the wrong spots
    "dry_run": False,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
            cfg.update({k: v for k, v in user.items() if k in cfg})
        except Exception as e:
            print(f"TidyBot: could not read {CONFIG_PATH}: {e}", file=sys.stderr)
    else:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    return cfg


def save_settings(**changes):
    """Write menu choices (skin, size) to the settings file."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except Exception:
        data = dict(DEFAULT_CONFIG)
    data.update(changes)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def desktop_dir():
    if HAVE_GTK:
        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        if d:
            return d
    return os.path.join(HOME, "Desktop")


def natural_key(s):
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


# --------------------------------------------------- desktop-icon metadata ---
NOFOLLOW = Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS if HAVE_GTK else 0


def get_pos(gfile):
    """Icon position as the desktop saved it (first key that has a value)."""
    try:
        info = gfile.query_info(",".join(POS_ATTRS), NOFOLLOW, None)
        for attr in POS_ATTRS:
            v = info.get_attribute_as_string(attr)
            if v:
                x, y = v.split(",")[:2]
                return float(x), float(y)
    except Exception:
        pass
    return None


def set_pos(path, x, y):
    gf = Gio.File.new_for_path(path)
    val = f"{int(round(x))},{int(round(y))}"
    for attr in POS_ATTRS:
        gf.set_attribute_string(attr, val, NOFOLLOW, None)


def gio_move(src, dst):
    # Gio's move carries the file's metadata (its icon position) along.
    Gio.File.new_for_path(src).move(
        Gio.File.new_for_path(dst),
        Gio.FileCopyFlags.NOFOLLOW_SYMLINKS | Gio.FileCopyFlags.NO_FALLBACK_FOR_MOVE,
        None, None)


def _held_load():
    try:
        with open(HELD_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _held_save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HELD_PATH, "w") as f:
        json.dump(d, f, ensure_ascii=False)


def hold(path):
    """Hide the item for a moment (a dot-name), so its icon leaves the desktop."""
    d, n = os.path.split(path)
    held, k = os.path.join(d, HOLD_PREFIX + n), 2
    while os.path.lexists(held):
        held = os.path.join(d, f"{HOLD_PREFIX}{k}-{n}")
        k += 1
    rec = _held_load()
    rec[held] = path
    _held_save(rec)
    gio_move(path, held)
    return held


def release(held, path):
    """Bring the item back under its own name (or 'name (2)' if taken)."""
    final = path
    if os.path.lexists(final):
        base, ext = os.path.splitext(os.path.basename(path))
        n = 2
        while os.path.lexists(final):
            final = os.path.join(os.path.dirname(path), f"{base} ({n}){ext}")
            n += 1
    gio_move(held, final)
    rec = _held_load()
    rec.pop(held, None)
    _held_save(rec)
    return final


def restore_held(desktop):
    """Put back anything left hidden if the robot was stopped mid-carry."""
    rec = _held_load()
    if rec.pop("__home__", None):
        try:
            ding_settings().set_boolean("show-home", True)
            Gio.Settings.sync()
            print("TidyBot: restored the Home icon")
        except Exception as e:
            print(f"TidyBot: could not show the Home icon again: {e}", file=sys.stderr)
    for held, orig in list(rec.items()):
        if os.path.lexists(held):
            try:
                release(held, orig)
                print(f"TidyBot: restored {orig}")
            except Exception as e:
                print(f"TidyBot: could not restore {held}: {e}", file=sys.stderr)
        rec.pop(held, None)
    try:
        for n in os.listdir(desktop):
            if n.startswith(HOLD_PREFIX):
                orig = re.sub(r"^\d+-", "", n[len(HOLD_PREFIX):])
                release(os.path.join(desktop, n), os.path.join(desktop, orig))
                print(f"TidyBot: restored {orig}")
    except Exception:
        pass
    _held_save({})


def ding_settings():
    """Read the desktop-icons extension settings if available."""
    if not HAVE_GTK:
        return None
    dirs = [None,
            "/usr/share/gnome-shell/extensions/ding@rastersoft.com/schemas",
            os.path.join(HOME, ".local/share/gnome-shell/extensions/ding@rastersoft.com/schemas")]
    for d in dirs:
        try:
            if d is None:
                src = Gio.SettingsSchemaSource.get_default()
            elif os.path.isdir(d):
                src = Gio.SettingsSchemaSource.new_from_directory(
                    d, Gio.SettingsSchemaSource.get_default(), False)
            else:
                continue
            sch = src.lookup(DING_SCHEMA, True)
            if sch:
                return Gio.Settings.new_full(sch, None, None)
        except Exception:
            pass
    return None


# ------------------------------------------------------------------- grid ---
class Grid:
    """The desktop's icon grid, in the desktop's own coordinates.
    A cell is (col, row); its position is the top-left of that cell."""

    def __init__(self, ox, oy, cw, ch, cols, rows):
        self.ox, self.oy, self.cw, self.ch = ox, oy, cw, ch
        self.cols, self.rows = max(1, cols), max(1, rows)

    def cell_at(self, x, y):
        return (int(round((x - self.ox) / self.cw)), int(round((y - self.oy) / self.ch)))

    def topleft(self, c):
        return self.ox + c[0] * self.cw, self.oy + c[1] * self.ch

    def icon_center(self, c):
        x, y = self.topleft(c)
        return x + self.cw / 2, y + self.ch * 0.36

    def inside(self, c):
        return 0 <= c[0] < self.cols and 0 <= c[1] < self.rows

    def slots(self, side="right"):
        cols = range(self.cols - 1, -1, -1) if side == "right" else range(self.cols)
        for c in cols:
            for r in range(self.rows):
                yield (c, r)


def _spacing(vals, guess):
    """Cell size from known positions (they all sit on grid lines)."""
    vals = sorted(set(round(v) for v in vals))
    diffs = [b - a for a, b in zip(vals, vals[1:]) if b - a > guess * 0.55]
    if not diffs:
        return None
    d = min(diffs)
    return d / max(1, round(d / guess))


def calibrate(positions, area, icon_size):
    ax, ay, aw, ah = area
    w0, h0 = DING_CELL.get(icon_size, DING_CELL["standard"])
    cw0 = aw / max(1, int(aw // w0))
    ch0 = ah / max(1, int(ah // h0))
    cw = _spacing([p[0] for p in positions], cw0) or cw0
    ch = _spacing([p[1] for p in positions], ch0) or ch0
    if not (0.6 * cw0 < cw < 1.6 * cw0):
        cw = cw0
    if not (0.6 * ch0 < ch < 1.6 * ch0):
        ch = ch0
    ref_x = positions[0][0] if positions else ax
    ref_y = positions[0][1] if positions else ay
    ox = ref_x - math.floor((ref_x - ax + 3) / cw) * cw
    oy = ref_y - math.floor((ref_y - ay + 3) / ch) * ch
    cols = int((ax + aw - ox + 3) // cw)
    rows = int((ay + ah - oy + 3) // ch)
    return Grid(ox, oy, cw, ch, cols, rows)


class Desk:
    """Reads the desktop: which icons exist, where they are, where they should be."""

    def __init__(self, cfg, desktop, area):
        self.cfg, self.desktop, self.area = cfg, desktop, area
        self.ours = {}   # path -> when the robot last put it down
        self._trash, self._trash_at = None, -1e9
        self.ding = ding_settings()
        self.icon_size = "standard"
        if self.ding is not None:
            try:
                self.icon_size = self.ding.get_string("icon-size")
            except Exception:
                pass

    def keep_arranged_on(self):
        try:
            sch = self.ding.get_property("settings-schema")
            return sch.has_key("keep-arranged") and self.ding.get_boolean("keep-arranged")
        except Exception:
            return False

    def home_shown(self):
        try:
            return self.ding is not None and self.ding.get_boolean("show-home")
        except Exception:
            return False

    def items(self):
        out = []
        try:
            names = os.listdir(self.desktop)
        except OSError:
            names = []
        for n in names:
            if n.startswith(".") or n in self.cfg["ignore"]:
                continue
            p = os.path.join(self.desktop, n)
            out.append({"path": p, "name": n,
                        "dir": os.path.isdir(p) and not os.path.islink(p),
                        "pos": get_pos(Gio.File.new_for_path(p))})
        # the Home icon is not a file on the Desktop, but it can be moved too
        if self.home_shown() and "Home" not in self.cfg["ignore"]:
            out.append({"path": HOME, "name": "Home", "dir": True, "special": "home",
                        "pos": get_pos(Gio.File.new_for_path(HOME))})
        return out

    def specials(self):
        """Icons the robot can't move (Trash): their cells are kept free.
        Asking for the Trash goes through a system service, so it is cached."""
        now = time.time()
        if now - self._trash_at > 30:
            self._trash_at = now
            self._trash = get_pos(Gio.File.new_for_uri("trash:///"))
        return [self._trash] if self._trash else []

    def hide_home(self):
        rec = _held_load()
        rec["__home__"] = True
        _held_save(rec)
        self.ding.set_boolean("show-home", False)
        Gio.Settings.sync()

    def show_home(self):
        self.ding.set_boolean("show-home", True)
        Gio.Settings.sync()
        rec = _held_load()
        rec.pop("__home__", None)
        _held_save(rec)

    def snapshot(self):
        return tuple(sorted((i["name"], i["pos"]) for i in self.items()))

    def is_fresh(self, it, now):
        """A file that just arrived (download, new save) gets a moment's peace.
        Files the robot itself just moved don't count as new."""
        if it.get("special"):
            return False
        if now - self.ours.get(it["path"], 0) < self.cfg["new_file_grace"] + 5:
            return False
        try:
            return now - os.lstat(it["path"]).st_ctime < self.cfg["new_file_grace"]
        except OSError:
            return True

    def plan(self):
        items = self.items()
        if not items:
            return None
        now = time.time()
        special = self.specials()
        known = [i["pos"] for i in items if i["pos"]] + special
        grid = calibrate(known, self.area, self.icon_size)
        fixed = {grid.cell_at(*p) for p in special}
        # the whole first column on the chosen side counts as "arranged";
        # if it is full, only as many spots as needed in the next column(s)
        slots, first_col = [], None
        for c in grid.slots(self.cfg["side"]):
            if c in fixed:
                continue
            if first_col is None:
                first_col = c[0]
            if c[0] != first_col and len(slots) >= len(items):
                break
            slots.append(c)
        slot_set = set(slots)
        where, occ, settled = {}, {}, {}
        for it in items:
            if it["pos"]:
                c = grid.cell_at(*it["pos"])
                where[it["path"]] = c
                occ.setdefault(c, it["path"])
                if c in slot_set and c not in settled:
                    settled[c] = it["path"]          # already lined up: stays put
        for c in fixed:
            occ[c] = "<special>"
        done = set(settled.values())
        strays = [it for it in items if it["path"] not in done and not self.is_fresh(it, now)]
        strays.sort(key=lambda i: (i["pos"] or (1e9, 1e9))[::-1])  # top to bottom
        free = [c for c in slots if c not in settled]
        targets = {p: c for c, p in settled.items()}
        for it, c in zip(strays, free):
            targets[it["path"]] = c
        order = [it for it in strays if it["path"] in targets]
        todo = [it["path"] for it in order]
        return {"grid": grid, "order": order, "targets": targets, "where": where,
                "occ": occ, "todo": todo, "fixed": fixed, "slots": slot_set}


# -------------------------------------------------------------- icon art ---
def load_item_icon(path, size=56):
    if not HAVE_GTK:
        return None
    ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
    try:
        if ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"} and \
                os.path.isfile(path) and os.path.getsize(path) < 30e6:
            return ("thumb", GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True))
    except Exception:
        pass
    try:
        info = Gio.File.new_for_path(path).query_info(
            "standard::icon", Gio.FileQueryInfoFlags.NONE, None)
        ii = Gtk.IconTheme.get_default().lookup_by_gicon(
            info.get_icon(), size, Gtk.IconLookupFlags.FORCE_SIZE)
        if ii:
            return ("icon", ii.load_icon())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- helpers ---
def clamp(v, a, b):
    return a if v < a else b if v > b else v


def lerp(a, b, t):
    return a + (b - a) * t


def ease_io(t):
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def ease_out_back(t):
    c1 = 1.70158
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1])


def rrect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def glow(cr, x, y, r, rgb, a):
    if r <= 0 or a <= 0:
        return
    g = cairo.RadialGradient(x, y, 0, x, y, r)
    g.add_color_stop_rgba(0, rgb[0], rgb[1], rgb[2], a)
    g.add_color_stop_rgba(1, rgb[0], rgb[1], rgb[2], 0)
    cr.set_source(g)
    cr.arc(x, y, r, 0, 2 * math.pi)
    cr.fill()


def vgrad(y0, y1, top, bot):
    g = cairo.LinearGradient(0, y0, 0, y1)
    g.add_color_stop_rgb(0, *top)
    g.add_color_stop_rgb(1, *bot)
    return g


def star(cr, x, y, r):
    cr.move_to(x, y - r)
    cr.curve_to(x, y, x, y, x + r, y)
    cr.curve_to(x, y, x, y, x, y + r)
    cr.curve_to(x, y, x, y, x - r, y)
    cr.curve_to(x, y, x, y, x, y - r)
    cr.close_path()


def text_layout(cr, text, font="Sans Bold 10"):
    if HAVE_GTK:
        lay = PangoCairo.create_layout(cr)
        lay.set_font_description(Pango.FontDescription.from_string(font))
        lay.set_text(text, -1)
        w, h = lay.get_pixel_size()
        return lay, w, h
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(12)
    return None, cr.text_extents(text).x_advance, 15


def show_text(cr, lay, text, x, y):
    if lay is not None:
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, lay)
    else:
        cr.move_to(x, y + 12)
        cr.show_text(text)


def draw_folder_art(cr):
    """Hand-drawn folder, used when no icon theme is available."""
    rrect(cr, -28, -24, 24, 12, 4)
    cr.set_source_rgb(0.86, 0.33, 0.14)
    cr.fill()
    rrect(cr, -28, -18, 56, 40, 6)
    cr.set_source(vgrad(-18, 22, (1.0, 0.52, 0.27), (0.93, 0.36, 0.15)))
    cr.fill()


def draw_item(cr, it):
    """A carried icon plus its name, centred on the icon."""
    a = it.alpha
    if it.icon is None:
        cr.push_group()
        draw_folder_art(cr)
        cr.pop_group_to_source()
        cr.paint_with_alpha(a)
        ih = 48
    else:
        kind, pb = it.icon
        w, h = pb.get_width(), pb.get_height()
        ih = h
        if kind == "thumb":
            rrect(cr, -w / 2 - 4, -h / 2 - 4, w + 8, h + 8, 5)
            cr.set_source_rgba(1, 1, 1, a)
            cr.fill()
        Gdk.cairo_set_source_pixbuf(cr, pb, -w / 2, -h / 2)
        cr.paint_with_alpha(a)
    if it.label:
        text = it.label if len(it.label) <= 18 else it.label[:16] + "..."
        lay, w, h = text_layout(cr, text, "Sans 9")
        y = ih / 2 + 4
        rrect(cr, -w / 2 - 7, y, w + 14, h + 4, (h + 4) / 2)
        cr.set_source_rgba(0.08, 0.10, 0.16, 0.72 * a)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, a)
        show_text(cr, lay, text, -w / 2, y + 2)


# ------------------------------------------------------------------ skins ---
# Each skin is a different kind of robot: its own head, face, eyes, top piece,
# way of flying, side parts and colors. Palette keys: top/bot (body gradient),
# outline, limb, visor_a/visor_b (face), eye, accent, accent2, cheek, flame.
def _pal(top, bot, outline, limb, visor_a, visor_b, eye, accent, accent2, cheek, flame):
    return dict(top=top, bot=bot, outline=outline, limb=limb, visor_a=visor_a,
                visor_b=visor_b, eye=eye, accent=accent, accent2=accent2,
                cheek=cheek, flame=flame)


SKINS = {
    "classic": dict(name="Classic Bot", head="rounded", face="visor", eyes="pill",
                    top="ball", drive="jet", sides="disc", extras=[],
                    pal=_pal((0.98, 0.99, 1.0), (0.76, 0.82, 0.92), (0.16, 0.22, 0.35),
                             (0.40, 0.47, 0.61), (0.09, 0.12, 0.22), (0.03, 0.05, 0.10),
                             (0.32, 0.92, 1.0), (1.0, 0.42, 0.45), (1.0, 0.78, 0.30),
                             (1.0, 0.45, 0.65), (0.55, 0.90, 1.0))),
    "tin": dict(name="Retro Tin Toy", head="box", face="screen", eyes="dial",
                top="coil", drive="jet", sides="bolt", extras=["rivets", "dial"],
                pal=_pal((0.88, 0.89, 0.92), (0.55, 0.58, 0.64), (0.18, 0.18, 0.22),
                         (0.45, 0.47, 0.52), (0.10, 0.20, 0.12), (0.03, 0.08, 0.04),
                         (0.55, 1.0, 0.45), (0.92, 0.20, 0.18), (1.0, 0.80, 0.25),
                         (1.0, 0.40, 0.30), (1.0, 0.62, 0.22))),
    "astro": dict(name="Astro Pod", head="helmet", face="disc", eyes="round",
                  top="none", drive="twin", sides="fins", extras=[],
                  pal=_pal((1.0, 1.0, 1.0), (0.84, 0.87, 0.93), (0.20, 0.24, 0.33),
                           (0.95, 0.52, 0.18), (0.12, 0.14, 0.22), (0.04, 0.05, 0.10),
                           (0.42, 0.86, 1.0), (1.0, 0.55, 0.15), (1.0, 0.80, 0.30),
                           (1.0, 0.50, 0.60), (1.0, 0.66, 0.26))),
    "drone": dict(name="Cyclops Drone", head="sphere", face="lens", eyes="cyclops",
                  top="propeller", drive="hover", sides="none", extras=[],
                  pal=_pal((0.56, 0.60, 0.67), (0.20, 0.22, 0.27), (0.06, 0.07, 0.10),
                           (0.30, 0.32, 0.38), (0.10, 0.10, 0.12), (0.02, 0.02, 0.03),
                           (1.0, 0.27, 0.22), (1.0, 0.30, 0.25), (1.0, 0.70, 0.20),
                           None, (1.0, 0.35, 0.25))),
    "ufo": dict(name="Saucer Alien", head="dome", face="none", eyes="almond",
                top="double", drive="saucer", sides="none", extras=[],
                pal=_pal((0.80, 1.0, 0.78), (0.38, 0.76, 0.44), (0.08, 0.28, 0.14),
                         (0.30, 0.55, 0.35), (0.05, 0.12, 0.08), (0.02, 0.05, 0.03),
                         (0.70, 1.0, 0.35), (0.72, 0.42, 1.0), (1.0, 0.90, 0.30),
                         (0.55, 0.95, 0.55), (0.70, 1.0, 0.40))),
    "ninja": dict(name="Shadow Ninja", head="rounded", face="slit", eyes="bar",
                  top="headband", drive="jet", sides="none", extras=[],
                  pal=_pal((0.32, 0.33, 0.38), (0.10, 0.10, 0.13), (0.0, 0.0, 0.0),
                           (0.22, 0.22, 0.26), (0.02, 0.02, 0.03), (0.0, 0.0, 0.0),
                           (1.0, 0.18, 0.28), (0.88, 0.10, 0.18), (1.0, 0.35, 0.30),
                           None, (1.0, 0.30, 0.32))),
    "neko": dict(name="Neko Cat-Bot", head="cat", face="visor", eyes="round",
                 top="none", drive="jet", sides="none", extras=["whiskers", "heart"],
                 pal=_pal((1.0, 0.96, 0.98), (0.98, 0.74, 0.84), (0.45, 0.22, 0.35),
                          (0.85, 0.55, 0.68), (0.20, 0.12, 0.22), (0.10, 0.05, 0.12),
                          (0.60, 0.90, 1.0), (1.0, 0.40, 0.60), (1.0, 0.55, 0.75),
                          (1.0, 0.50, 0.70), (1.0, 0.62, 0.82))),
    "steam": dict(name="Brass Steamer", head="tall", face="goggles", eyes="round",
                  top="tophat", drive="smoke", sides="gear", extras=["rivets"],
                  pal=_pal((1.0, 0.86, 0.52), (0.72, 0.47, 0.18), (0.35, 0.20, 0.08),
                           (0.55, 0.35, 0.15), (0.15, 0.10, 0.05), (0.05, 0.03, 0.02),
                           (1.0, 0.80, 0.38), (0.75, 0.20, 0.15), (1.0, 0.62, 0.22),
                           (1.0, 0.50, 0.30), (1.0, 0.60, 0.20))),
    "neon": dict(name="Synthwave Neon", head="rounded", face="screen", eyes="pixel",
                 top="ball", drive="jet", sides="disc", extras=["neon"],
                 pal=_pal((0.30, 0.17, 0.48), (0.08, 0.04, 0.18), (1.0, 0.25, 0.85),
                          (0.38, 0.22, 0.58), (0.04, 0.02, 0.10), (0.02, 0.0, 0.05),
                          (0.30, 1.0, 0.95), (1.0, 0.25, 0.85), (1.0, 0.85, 0.30),
                          (1.0, 0.30, 0.80), (1.0, 0.32, 0.88))),
    "bee": dict(name="Buzz Bee", head="rounded", face="visor", eyes="round",
                top="double", drive="wings", sides="none", extras=["stripes"],
                pal=_pal((1.0, 0.90, 0.36), (0.95, 0.62, 0.08), (0.25, 0.18, 0.05),
                         (0.20, 0.18, 0.15), (0.12, 0.10, 0.08), (0.04, 0.03, 0.02),
                         (1.0, 0.97, 0.80), (0.15, 0.12, 0.10), (1.0, 0.85, 0.30),
                         (1.0, 0.55, 0.40), (1.0, 0.90, 0.50))),
}
SKIN_ORDER = ["classic", "tin", "astro", "drone", "ufo", "ninja", "neko", "steam", "neon", "bee"]
FLAME = (0.55, 0.90, 1.0)


def apply_skin(key):
    """Switch the drawing palette to a skin and return the skin."""
    global BODY_TOP, BODY_BOT, OUTLINE, LIMB, VISOR_A, VISOR_B, EYE, ACCENT, ACCENT2
    global CHEEK, FLAME, CONFETTI
    sk = SKINS.get(key) or SKINS["classic"]
    p = sk["pal"]
    BODY_TOP, BODY_BOT, OUTLINE, LIMB = p["top"], p["bot"], p["outline"], p["limb"]
    VISOR_A, VISOR_B, EYE = p["visor_a"], p["visor_b"], p["eye"]
    ACCENT, ACCENT2, CHEEK, FLAME = p["accent"], p["accent2"], p["cheek"], p["flame"]
    CONFETTI = [EYE, ACCENT, ACCENT2, (0.55, 0.95, 0.55), (0.75, 0.6, 1.0)]
    return sk


# face layouts: clip path, eye centres, eye size
def _face_path(cr, face):
    if face == "visor":
        rrect(cr, -29, -20, 58, 36, 15)
    elif face == "slit":
        rrect(cr, -34, -12, 68, 21, 10)
    elif face == "screen":
        rrect(cr, -30, -21, 60, 37, 5)
    elif face == "disc":
        cr.new_sub_path()
        cr.arc(0, -6, 21, 0, 2 * math.pi)
    elif face == "lens":
        cr.new_sub_path()
        cr.arc(0, -2, 16.5, 0, 2 * math.pi)
    elif face == "goggles":
        for sx in (-14, 14):
            cr.new_sub_path()
            cr.arc(sx, -4, 9.5, 0, 2 * math.pi)
    else:  # "none": whole head
        cr.new_sub_path()
        cr.arc(0, -2, 33, 0, 2 * math.pi)


FACE_EYES = {
    "visor": ([(-11, -2), (11, -2)], 1.0),
    "slit": ([(-13, -1.5), (13, -1.5)], 1.0),
    "screen": ([(-11, -3), (11, -3)], 1.0),
    "disc": ([(-8, -6), (8, -6)], 0.8),
    "lens": ([(0, -2)], 1.0),
    "goggles": ([(-14, -4), (14, -4)], 0.85),
    "none": ([(-12, 0), (12, 0)], 1.0),
}


def _head_path(cr, head):
    if head in ("rounded", "cat"):
        rrect(cr, -38, -30, 76, 58, 22)
    elif head == "box":
        rrect(cr, -36, -30, 72, 58, 7)
    elif head == "tall":
        rrect(cr, -32, -36, 64, 64, 10)
    elif head == "sphere":
        cr.new_sub_path()
        cr.arc(0, -1, 35, 0, 2 * math.pi)
    elif head == "dome":
        cr.new_sub_path()
        cr.arc(0, -2, 33, 0, 2 * math.pi)


def heart(cr, x, y, s):
    cr.move_to(x, y + s * 0.95)
    cr.curve_to(x - s * 1.7, y - s * 0.2, x - s * 0.6, y - s * 1.5, x, y - s * 0.4)
    cr.curve_to(x + s * 0.6, y - s * 1.5, x + s * 1.7, y - s * 0.2, x, y + s * 0.95)
    cr.close_path()


def gear(cr, x, y, r, teeth, rot):
    cr.new_sub_path()
    for i in range(teeth * 2):
        a = rot + i * math.pi / teeth
        rr = r if i % 2 == 0 else r * 0.74
        a1, a2 = a - math.pi / teeth * 0.45, a + math.pi / teeth * 0.45
        if i == 0:
            cr.move_to(x + math.cos(a1) * rr, y + math.sin(a1) * rr)
        else:
            cr.line_to(x + math.cos(a1) * rr, y + math.sin(a1) * rr)
        cr.line_to(x + math.cos(a2) * rr, y + math.sin(a2) * rr)
    cr.close_path()


# -------------------------------------------------------------- particles ---
class Particle:
    __slots__ = ("x", "y", "vx", "vy", "life", "max", "size", "rgb", "kind", "g")

    def __init__(self, x, y, vx, vy, life, size, rgb, kind="dot", g=0.0):
        self.x, self.y, self.vx, self.vy = x, y, vx, vy
        self.life = self.max = life
        self.size, self.rgb, self.kind, self.g = size, rgb, kind, g


class Floater:
    def __init__(self, text, x, y, t0, dur=1.3):
        self.text, self.x, self.y, self.t0, self.dur = text, x, y, t0, dur


class Item:
    def __init__(self, icon, label):
        self.icon, self.label = icon, label
        self.scale, self.alpha, self.dy, self.rot = 0.5, 0.0, 0.0, 0.0
        self.free, self.sx, self.sy = False, 0.0, 0.0   # free = flying on its own


class Slot:
    """Glowing landing spot drawn where the icon is about to go."""

    def __init__(self, x, y, w, h, t0):
        self.x, self.y, self.w, self.h, self.t0 = x, y, w, h, t0
        self.bump, self.leave_t = 0.0, None


# -------------------------------------------------------------------- bot ---
class Bot:
    def __init__(self, cfg, desk, area):
        self.cfg, self.desk, self.area = cfg, desk, area
        self.cs = float(cfg.get("coord_scale", 1.0))
        self.skin = apply_skin(cfg.get("skin", "classic"))
        self.puff_acc = 0.0
        self.scale = float(cfg.get("scale", 1.0))
        ax, ay, aw, ah = area
        if cfg["side"] == "right":
            self.home = (ax + aw - 330 * self.scale, ay + ah - 250 * self.scale)
        else:
            self.home = (ax + 330 * self.scale, ay + ah - 250 * self.scale)
        self.x, self.y = self.home[0], ay - 150  # enters from the top
        self.vx = self.vy = 0.0
        self.t = 0.0
        self.tilt = self.spin = 0.0
        self.squash, self.sq_v = 1.0, 0.0
        self.bob = 0.0
        self.look, self.look_target = [0.0, 0.0], [0.0, 0.0]
        self.blink, self.blink_t, self.next_blink = 0.0, 0.0, 2.0
        self.arm = self.arm_target = 0.0
        self.claw = self.claw_target = 0.0
        self.thrust = 0.4
        self.mood = "normal"
        self.hop = 0.0            # extra vertical offset (startle hop)
        self.tilt_extra = 0.0     # head shake
        self.eye_wide = 0.0       # 1 = eyes wide open in surprise
        self.alert_t = -99.0      # when the "!" badge popped
        self.peek_until = -99.0   # one-eye peek while dozing
        self.item = None
        self.bubble = None
        self.bubble_w = 260
        self.target = None
        self.particles, self.floats = [], []
        self.emit_acc = 0.0
        self.paused = False
        self.stuck = False
        self.fails = 0
        self.skip_until = {}
        self.dragging = False
        self.gave_up_snap = None
        self.planned_snap, self.planned_at = None, -99.0
        self.last_scan = -99.0
        self.last_snap, self.snap_since = None, 0.0
        self.task, self.task_name = None, None
        self.start(self.entrance(), "entrance")

    def ov(self, x, y):
        """Desktop coordinates -> overlay coordinates."""
        return x * self.cs, y * self.cs

    # ----- task plumbing (each behaviour is a generator fed dt per frame)
    def start(self, gen, name):
        self.task, self.task_name = gen, name
        try:
            next(gen)
        except StopIteration:
            self.task = None

    def wait(self, sec):
        el = 0.0
        while el < sec:
            el += (yield)

    def animate(self, dur, fn):
        el = 0.0
        while el < dur:
            el += (yield)
            fn(min(1.0, el / dur))

    def fly_to(self, tx, ty, speed=None, check=None):
        """Fly along a curved path. If `check` is given it is polled every
        0.2 s; when it returns something, the flight stops there and returns
        that value (used to react when the user moves an icon mid-flight)."""
        speed = speed or self.cfg.get("speed", 520)
        ty = max(ty, self.top_limit())               # never aim above the screen
        sx, sy = self.x, self.y
        d = math.hypot(tx - sx, ty - sy)
        if d < 2:
            return None
        dur = max(0.4, d / speed)
        lift = min(140, d * 0.3)
        top = min(self.top_limit(), sy, ty)        # never arc above the screen
        c1 = (sx + (tx - sx) * 0.25, max(top, sy + (ty - sy) * 0.25 - lift))
        c2 = (sx + (tx - sx) * 0.75, max(top, sy + (ty - sy) * 0.75 - lift))
        self.look_target = [clamp((tx - sx) / d * 1.4, -1, 1), clamp((ty - sy) / d, -1, 1) * 0.6]
        el, next_check = 0.0, 0.2
        while el < dur:
            dt = (yield)
            el += dt
            nx, ny = bezier((sx, sy), c1, c2, (tx, ty), ease_io(min(1.0, el / dur)))
            if dt > 0:
                self.vx, self.vy = (nx - self.x) / dt, (ny - self.y) / dt
            self.x, self.y = nx, ny
            if check is not None:
                next_check -= dt
                if next_check <= 0:
                    next_check = 0.2
                    r = check()
                    if r is not None:
                        self.vx = self.vy = 0.0
                        return r
        self.vx = self.vy = 0.0
        self.look_target = [0.0, 0.0]
        return None

    def wait_check(self, sec, check):
        el, next_check = 0.0, 0.2
        while el < sec:
            dt = (yield)
            el += dt
            next_check -= dt
            if next_check <= 0:
                next_check = 0.2
                r = check()
                if r is not None:
                    return r
        return None

    def say(self, text, dur=1.6):
        self.bubble = [text, self.t, self.t + dur]

    def burst(self, x, y, colors, n=16, speed=170):
        for _ in range(n):
            a = random.uniform(0, 2 * math.pi)
            v = random.uniform(speed * 0.4, speed)
            self.particles.append(Particle(
                x, y, math.cos(a) * v, math.sin(a) * v - 60,
                random.uniform(0.5, 0.9), random.uniform(3, 6),
                random.choice(colors), random.choice(["star", "dot"]), g=260))

    # ----- behaviours
    def entrance(self):
        self.say("Hi! I'll keep your icons in line.", 2.4)
        yield from self.fly_to(*self.home, speed=380)
        self.mood = "happy"
        yield from self.wait(1.2)
        self.mood = "normal"
        if self.desk.keep_arranged_on():
            self.say("Desktop 'Keep Arranged' is on - I'll let it do the job.", 4)
            self.stuck = True

    def zzz(self):
        self.particles.append(Particle(
            self.x + 30 * self.scale, self.y - 40 * self.scale,
            random.uniform(10, 25), -35, 1.6, random.uniform(11, 16),
            (1, 1, 1), "z"))

    def idle(self):
        """Doze at home until something on the desktop is out of line."""
        yield from self.fly_to(*self.home, speed=300)
        self.mood = "sleep"
        while True:
            yield from self.wait(1.2)
            if self.t > self.peek_until:
                self.zzz()

    def sleep(self):
        """Paused: same nap, but nothing wakes it."""
        yield from self.fly_to(*self.home, speed=300)
        self.mood = "sleep"
        while self.paused:
            yield from self.wait(1.1)
            self.zzz()
        self.mood = "normal"

    def wake_up(self, look_at):
        """Startled awake: '!' pops, eyes snap wide, a hop and a double-take."""
        self.mood = "normal"
        self.blink_t = 0.0
        self.alert_t = self.t
        self.eye_wide = 1.0
        self.squash = 0.62
        self.burst(self.x, self.y - 40 * self.scale, [ACCENT2, (1, 1, 1)], 8, 120)

        def hop(u):
            self.hop = -math.sin(math.pi * u) * 30 * self.scale
        yield from self.animate(0.36, hop)
        self.hop = 0.0
        self.squash = 0.85
        # double-take: glance the wrong way, then snap to the stray icon
        dx, dy = look_at[0] - self.x, look_at[1] - self.y
        d = max(1.0, math.hypot(dx, dy))
        self.look_target = [-clamp(dx / d * 1.4, -1, 1), 0.0]
        yield from self.wait(0.16)
        self.look_target = [clamp(dx / d * 1.4, -1, 1), clamp(dy / d, -1, 1) * 0.6]
        self.eye_wide = 1.0
        self.say(random.choice(["Huh?! Something's out of place!",
                                "Hey! That's not where it goes!",
                                "A stray icon! On it!"]), 1.6)

        def shake(u):
            self.tilt_extra = math.sin(u * math.pi * 4) * 0.16 * (1 - u)
        yield from self.animate(0.36, shake)
        self.tilt_extra = 0.0
        yield from self.wait(0.15)

    def celebrate(self, small=False):
        self.mood = "happy"
        if not small:
            self.burst(self.x, self.y, CONFETTI, 26, 230)
        self.squash = 0.82

        def f(u):
            self.spin = 2 * math.pi * ease_io(u)
        yield from self.animate(0.8 if small else 0.7, f)
        self.spin = 0.0
        yield from self.wait(0.6)
        self.mood = "normal"

    def arrange(self, plan):
        first = plan["todo"][0]
        if first in plan["where"]:
            look_at = self.ov(*plan["grid"].icon_center(plan["where"][first]))
        else:
            look_at = (self.x - 200, self.y)
        yield from self.wake_up(look_at)
        moved = 0
        for _round in range(6):   # keep going if the user scatters more meanwhile
            grid, fixed, slots = plan["grid"], plan["fixed"], plan["slots"]
            for it in plan["order"]:
                if self.paused or self.stuck:
                    break
                if not os.path.lexists(it["path"]):
                    continue
                r = yield from self.carry(it["path"], grid, fixed, slots)
                moved += 1 if r == "moved" else 0
            if self.paused or self.stuck:
                break
            plan = self.desk.plan()
            if not plan or not plan["todo"]:
                break
            self.say(random.choice(["Another one!", "Oh, more?", "And that one too!"]), 1.2)
        if moved and not (self.paused or self.stuck):
            self.say("All lined up!", 1.4)
            yield from self.celebrate()
        if not moved:
            # nothing worked: don't wake again until the desktop actually changes
            self.gave_up_snap = self.desk.snapshot()
        yield from self.fly_to(*self.home, speed=380)
        if not self.stuck:
            self.say(random.choice(["*yawn* Back to my nap...", "Wake me if it gets messy.",
                                    "Zzz time."]), 1.8)
            yield from self.wait(0.9)

    def top_limit(self):
        """Highest the robot's centre may go and still be fully on screen."""
        return self.area[1] + 72 * self.scale

    def hover_for(self, ix, iy):
        """Where to hover to grab/drop an icon at (ix, iy). Normally right
        above it; near the top edge, beside it (the icon gets pulled/tossed)."""
        s = self.scale
        ax, ay, aw, ah = self.area
        y = iy - 92 * s
        if y >= self.top_limit():
            return ix, y, False
        side = -1 if ix > ax + aw / 2 else 1
        return ix + side * 112 * s, max(self.top_limit(), iy - 20 * s), True

    def live_cells(self, grid, exclude=None):
        """Cells occupied right now by every visible icon (read fresh)."""
        return {grid.cell_at(*it["pos"]) for it in self.desk.items()
                if it["pos"] and it["path"] != exclude}

    def pick_slot(self, grid, path, fixed):
        """First free spot in the column, top to bottom."""
        taken = self.live_cells(grid, exclude=path) | set(fixed)
        for c in grid.slots(self.cfg["side"]):
            if c not in taken:
                return c
        return None

    def carry(self, path, grid, fixed, slots):
        """Fetch one icon and put it in the column. Follows the icon if the
        user moves it on the way. Returns 'moved', 'done' or 'fail'."""
        s = self.scale
        hang = 92 * s
        is_home = path == HOME
        name = "Home" if is_home else os.path.basename(path)
        gf = Gio.File.new_for_path(path)

        def cell_now():
            p = get_pos(gf)
            return grid.cell_at(*p) if p else None

        def arranged(c):
            return c is not None and c in slots

        cur = cell_now()
        if arranged(cur):
            return "done"
        seen = [cur]

        def icon_moved():
            c = cell_now()
            if c != seen[0]:
                seen[0] = c
                return "moved"
            return None

        # --- fly to the icon, re-aiming whenever it is dragged somewhere else
        while cur is not None:
            ix, iy = self.ov(*grid.icon_center(cur))
            hx, hy, beside = self.hover_for(ix, iy)
            r = None
            if not beside:
                r = yield from self.fly_to(hx, hy - 45, check=icon_moved)
            if r is None:
                r = yield from self.fly_to(hx, hy, speed=None if beside else 200,
                                           check=icon_moved)
            if r is None:
                self.mood = "scan"
                r = yield from self.wait_check(0.35, icon_moved)
                self.mood = "normal"
            if r is None:
                break
            cur = seen[0]
            if not is_home and not os.path.lexists(path):
                self.say("Huh, it vanished!", 1.2)
                return "fail"
            if arranged(cur):
                self.mood = "happy"
                self.say("Oh, you fixed it yourself!", 1.4)
                yield from self.wait(0.7)
                self.mood = "normal"
                return "done"
            self.eye_wide = 1.0
            self.squash = 0.8
            self.say(random.choice(["Huh? It moved!", "Hey, come back!",
                                    "Wait, where'd it go?"]), 1.2)
        beside = False
        if cur is None:
            ix, iy = self.x, self.y + hang
        else:
            beside = self.hover_for(ix, iy)[2]
        self.arm_target, self.claw_target = 1.0, 1.0
        yield from self.wait(0.22)

        cell = self.pick_slot(grid, path, fixed)
        held = None
        try:
            if cell is None:
                raise RuntimeError("no free spot in the column")
            if not self.cfg["dry_run"]:
                # write the new position first; if the desktop can't store it,
                # nothing has been hidden yet
                set_pos(path, *grid.topleft(cell))
                if is_home:
                    self.desk.hide_home()               # Home icon leaves its spot
                    held = HOME
                else:
                    held = hold(path)                   # icon leaves its old spot
        except Exception as e:
            if held:
                try:
                    if is_home:
                        self.desk.show_home()
                    else:
                        release(held, path)
                except Exception as e2:
                    print(f"TidyBot: could not put back {held}: {e2}", file=sys.stderr)
            print(f"TidyBot: skipping {name}: {e}", file=sys.stderr)
            self.skip_until[path] = self.t + 90
            self.arm_target = self.claw_target = 0.0
            self.mood = "confused"
            self.say("Hmm, I can't lift that one.", 1.8)
            yield from self.wait(1.2)
            self.mood = "normal"
            return "fail"

        item = Item(load_item_icon(path if is_home else (held or path)), name)
        self.item = item
        self.claw_target = 0.1
        self.squash = 0.8
        self.burst(ix, iy, [EYE, (1, 1, 1)], 10, 130)

        if beside:
            item.free, item.sx, item.sy, item.scale, item.alpha = True, ix, iy, 1.0, 1.0

            def pull(u):
                e = ease_io(u)
                cx, cy = self.local_to_screen(0, 92)
                item.sx = lerp(ix, cx, e)
                item.sy = lerp(iy, cy, e) - math.sin(math.pi * u) * 40 * s
            yield from self.animate(0.4, pull)
            item.free = False
        else:
            def pop(u):
                item.scale, item.alpha = lerp(0.6, 1.0, ease_out_back(u)), u
            yield from self.animate(0.22, pop)
        self.say(random.choice(["Got it!", "Gotcha!", "Yoink!", name[:20]]), 1.0)
        yield from self.fly_to(self.x, max(self.top_limit(), self.y - 55), speed=260)

        # --- carry it over; if someone drops an icon on our spot, pick another
        box = [cell]

        def spot_taken():
            if box[0] in self.live_cells(grid, exclude=path):
                nc = self.pick_slot(grid, path, fixed)
                if nc is not None and nc != box[0]:
                    box[0] = nc
                    return "retarget"
            return None

        tgt = None
        while True:
            cell = box[0]
            tx, ty = self.ov(*grid.icon_center(cell))
            sx, sy = self.ov(*grid.topleft(cell))
            if tgt is None or (tgt.x, tgt.y) != (sx, sy):
                if tgt is not None:
                    tgt.leave_t = self.t
                self.target = tgt = Slot(sx, sy, grid.cw * self.cs, grid.ch * self.cs, self.t)
            hx, hy, beside = self.hover_for(tx, ty)
            r = None
            if not beside:
                r = yield from self.fly_to(hx, hy - 45, check=spot_taken)
            if r is None:
                r = yield from self.fly_to(hx, hy, speed=None if beside else 160,
                                           check=spot_taken)
            if r is None:
                r = spot_taken()
            if r is None:
                break
            self.say("Spot's taken - next one!", 1.2)
        self.claw_target, self.arm_target = 1.0, 0.8

        if beside:   # toss it into the spot with a little arc
            sx0, sy0 = self.local_to_screen(0, 92)
            item.free, item.sx, item.sy = True, sx0, sy0
            self.claw_target = 1.0

            def toss(u):
                e = ease_io(u)
                item.sx = lerp(sx0, tx, e)
                item.sy = lerp(sy0, ty, e) - math.sin(math.pi * u) * 55 * s
                item.rot = 0.5 * math.sin(math.pi * u)
            self.say(random.choice(["Catch!", "Swish!", "In you go!"]), 1.0)
            yield from self.animate(0.45, toss)

        final = path
        try:
            if held and is_home:
                set_pos(HOME, *grid.topleft(cell))
                self.desk.show_home()                   # Home reappears in its new spot
            elif held:
                set_pos(held, *grid.topleft(cell))
                final = release(held, path)             # icon appears in its new spot
                set_pos(final, *grid.topleft(cell))
            self.desk.ours[final] = time.time()
        except Exception as e:
            print(f"TidyBot: could not put back {held}: {e}", file=sys.stderr)

        def drop(u):
            item.dy = 6 * u
            item.scale = lerp(1.0, 0.92, u)
            item.alpha = 1 - u
        yield from self.animate(0.28, drop)
        self.item = None
        tgt.bump = 1.0
        self.burst(tx, ty, [ACCENT2, (1, 1, 1), EYE], 14)

        self.mood = "happy"
        self.arm_target = self.claw_target = 0.0
        yield from self.fly_to(hx, max(self.top_limit(), hy - 60), speed=260)
        tgt.leave_t = self.t

        if held and get_pos(Gio.File.new_for_path(final)) is None:
            self.fails += 1
            if self.fails >= 2:
                self.stuck = True
                self.mood = "confused"
                self.say("My moves aren't sticking - check the terminal.", 4)
                print("TidyBot: the desktop isn't keeping icon positions I set. "
                      "Run 'tidybot --debug' and share the output.", file=sys.stderr)
                return "fail"
        self.mood = "normal"
        return "moved"

    # ----- things you can do to it (left click / right-click menu)
    def is_resting(self):
        return self.task_name in ("idle", "sleep") and not self.dragging

    def hit_rect(self):
        """Clickable area around the robot's body, snapped to 8 px so it
        doesn't change every frame."""
        s = self.scale
        x0 = int(self.x - 52 * s) // 8 * 8
        y0 = int(self.y - 70 * s) // 8 * 8
        return (x0, y0, int(104 * s) // 8 * 8 + 16, int(132 * s) // 8 * 8 + 16)

    # ----- drag and throw
    def begin_drag(self, px, py):
        if self.item is not None:          # never drop a file mid-carry
            self.squash = 0.8
            self.say("I'm carrying something!", 1.2)
            return False
        self.dragging = True
        self.drag_off = (px - self.x, py - self.y)
        self.drag_target = (self.x, self.y)
        self.ptr_hist = [(time.monotonic(), px, py)]
        if self.target:
            self.target.leave_t = self.t
        self.mood = "normal"
        self.eye_wide, self.squash = 1.0, 0.75
        self.arm_target, self.claw_target = 0.3, 0.8
        self.say(random.choice(["Hey! Where are we going?", "Whoa!", "Wheee!",
                                "Put me down!"]), 1.4)
        self.start(self.held(), "dragged")
        return True

    def drag_to(self, px, py):
        if not self.dragging:
            return
        now = time.monotonic()
        self.drag_target = (px - self.drag_off[0], py - self.drag_off[1])
        self.ptr_hist.append((now, px, py))
        self.ptr_hist = [h for h in self.ptr_hist if now - h[0] < 0.12]

    def end_drag(self):
        if not self.dragging:
            return
        self.dragging = False
        now = time.monotonic()
        recent = [h for h in self.ptr_hist if now - h[0] < 0.1]
        vx = vy = 0.0
        if len(recent) >= 2:
            (t0, x0, y0), (t1, x1, y1) = recent[0], recent[-1]
            span = max(0.016, t1 - t0)
            vx, vy = (x1 - x0) / span, (y1 - y0) / span
        self.start(self.thrown(clamp(vx, -2600, 2600), clamp(vy, -2600, 2600)), "thrown")

    def held(self):
        """Follows the pointer on a springy tether: lags, overshoots and
        swings like a pendulum hanging from your cursor."""
        k = 170.0
        damp = 2 * math.sqrt(k) * 0.5
        swing = 0.0
        while self.dragging:
            dt = (yield)
            tx, ty = self.drag_target
            ax = (tx - self.x) * k - self.vx * damp
            ay = (ty - self.y) * k - self.vy * damp
            self.vx += ax * dt
            self.vy += ay * dt
            self.x += self.vx * dt
            self.y += self.vy * dt
            swing += (clamp(-ax * 0.00022, -0.85, 0.85) - swing) * (1 - math.exp(-dt * 10))
            self.tilt_extra = swing
            self.look_target = [clamp(self.vx / 600, -1, 1), clamp(self.vy / 600, -1, 1)]

    def thrown(self, vx, vy):
        s = self.scale
        ax, ay, aw, ah = self.area
        self.tilt_extra = 0.0
        speed = math.hypot(vx, vy)
        if speed < 260:   # gently put down: it stays there
            self.vx = self.vy = 0.0
            self.mood = "happy"
            self.say(random.choice(["New spot? I like it!", "Thanks for the lift!",
                                    "Cozy here."]), 1.6)
            self.home = self.clamp_home(self.x, self.y)
            self.arm_target = self.claw_target = 0.0
            yield from self.wait(1.0)
            self.mood = "normal"
            return
        self.say(random.choice(["Whoaaa!", "Aaaaah!", "Wheeeee!"]), 1.2)
        spin_v = clamp(vx * 0.012, -16, 16) or 6.0
        left, right = ax + 60 * s, ax + aw - 60 * s
        top, bottom = self.top_limit(), ay + ah - 135 * s
        el = 0.0
        while True:
            dt = (yield)
            el += dt
            vx *= math.exp(-dt * 1.5)
            vy = vy * math.exp(-dt * 1.5) + 420 * dt        # a little gravity
            self.x += vx * dt
            self.y += vy * dt
            bounced = False
            if self.x < left:
                self.x, vx, bounced = left, abs(vx) * 0.62, True
            elif self.x > right:
                self.x, vx, bounced = right, -abs(vx) * 0.62, True
            if self.y < top:
                self.y, vy, bounced = top, abs(vy) * 0.62, True
            elif self.y > bottom:
                self.y, vy, bounced = bottom, -abs(vy) * 0.55, True
                vx *= 0.85
            if bounced:
                self.squash = 0.7
                self.burst(self.x, self.y, [(1, 1, 1), EYE], 6, 110)
                spin_v = -spin_v * 0.8
            self.vx, self.vy = vx, vy
            self.spin += spin_v * dt
            spin_v *= math.exp(-dt * 1.1)
            if el > 0.4 and math.hypot(vx, vy) < 90 and self.y >= bottom - 2 or el > 6:
                break
        # recover: straighten up, dizzy for a moment, shake it off
        self.vx = self.vy = 0.0
        s0 = self.spin
        s1 = round(s0 / (2 * math.pi)) * 2 * math.pi

        def upright(u):
            self.spin = lerp(s0, s1, ease_io(u))
        yield from self.animate(0.45, upright)
        self.spin = 0.0
        self.mood = "dizzy"
        self.say(random.choice(["Whoa... so dizzy...", "Everything's spinning...",
                                "Again! ...no wait."]), 1.8)
        yield from self.wait(1.7)

        def shake(u):
            self.tilt_extra = math.sin(u * math.pi * 5) * 0.18 * (1 - u)
        self.mood = "normal"
        yield from self.animate(0.5, shake)
        self.tilt_extra = 0.0
        self.home = self.clamp_home(self.x, self.y - 40 * s)
        self.arm_target = self.claw_target = 0.0

    def clamp_home(self, x, y):
        s = self.scale
        ax, ay, aw, ah = self.area
        return (clamp(x, ax + 70 * s, ax + aw - 70 * s),
                clamp(y, self.top_limit() + 10, ay + ah - 140 * s))

    def poke(self):
        if not self.is_resting():
            self.squash = 0.85
            return
        self.start(self.poked(), "poked")

    def poked(self):
        was_asleep = self.mood == "sleep"
        self.mood = "normal"
        self.eye_wide = 1.0
        self.squash = 0.7
        self.burst(self.x, self.y - 30 * self.scale, [EYE, (1, 1, 1)], 6, 90)
        if was_asleep:
            line = random.choice(["Huh? I'm awake!", "Five more minutes...", "Mm? All tidy here."])
        else:
            line = random.choice(["Hey, that tickles!", "Beep boop!", "I'm on duty!", "Hehe!"])
        self.say(line, 1.4)

        def wiggle(u):
            self.tilt_extra = math.sin(u * math.pi * 3) * 0.2 * (1 - u)
        yield from self.animate(0.45, wiggle)
        self.tilt_extra = 0.0
        self.mood = "happy"
        yield from self.wait(0.9)
        self.mood = "normal"

    def set_skin(self, key):
        self.skin = apply_skin(key)
        self.cfg["skin"] = key
        save_settings(skin=key)
        self.squash = 0.7
        self.burst(self.x, self.y, CONFETTI, 22, 200)
        if self.is_resting():
            self.start(self.show_off(), "showoff")

    def show_off(self):
        self.mood = "happy"
        self.say(random.choice(["New look!", "How do I look?", "Ta-da!"]) + "  "
                 + self.skin["name"], 1.6)
        yield from self.celebrate(small=True)
        yield from self.wait(0.6)

    def set_scale(self, v):
        self.scale = v
        self.cfg["scale"] = v
        save_settings(scale=v)
        ax, ay, aw, ah = self.area
        if self.cfg["side"] == "right":
            self.home = (ax + aw - 330 * v, ay + ah - 250 * v)
        else:
            self.home = (ax + 330 * v, ay + ah - 250 * v)
        self.squash = 0.75
        if self.is_resting():
            self.start(self.idle(), "idle")

    def tidy_now(self):
        if not self.is_resting() or self.paused:
            return
        plan = self.desk.plan()
        if plan and plan["todo"]:
            self.start(self.arrange(plan), "arrange")
        else:
            self.start(self.all_clean(), "showoff")

    def all_clean(self):
        self.mood = "happy"
        self.say("Everything's already tidy!", 1.8)
        yield from self.celebrate(small=True)
        yield from self.wait(0.8)

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.say("Nap time. Zzz", 1.6)
            if self.task_name not in ("arrange", "dragged", "thrown"):
                self.start(self.sleep(), "sleep")
        else:
            self.mood = "normal"
            self.say("I'm back!", 1.4)
            self.squash = 0.8

    # ----- per-frame update
    def update(self, dt):
        self.t += dt
        if self.task is not None:
            try:
                self.task.send(dt)
            except StopIteration:
                self.task = None
        if self.task is None:
            if self.paused:
                self.start(self.sleep(), "sleep")
            else:
                self.start(self.idle(), "idle")

        if (self.task_name == "idle" and not self.paused and not self.stuck
                and self.t - self.last_scan > self.cfg["scan_interval"]):
            self.last_scan = self.t
            snap = self.desk.snapshot()
            if snap != self.last_snap:
                if self.last_snap is not None:
                    self.peek_until = self.t + 1.8   # something moved: one eye opens
                self.last_snap, self.snap_since = snap, self.t
            elif (self.t - self.snap_since >= self.cfg["settle_seconds"]
                  and snap != self.gave_up_snap
                  and (snap != self.planned_snap or self.t - self.planned_at > 5)):
                # only re-plan when something changed (or now and then, for
                # brand-new files whose short grace period has run out)
                self.planned_snap, self.planned_at = snap, self.t
                plan = self.desk.plan()
                if plan:
                    plan["todo"] = [p for p in plan["todo"]
                                    if self.skip_until.get(p, 0) <= self.t]
                    plan["order"] = [i for i in plan["order"] if i["path"] in plan["todo"]]
                if plan and plan["todo"]:
                    self.start(self.arrange(plan), "arrange")

        k = lambda rate: 1 - math.exp(-dt * rate)
        self.arm += (self.arm_target - self.arm) * k(10)
        self.claw += (self.claw_target - self.claw) * k(14)
        for i in (0, 1):
            self.look[i] += (self.look_target[i] - self.look[i]) * k(6)
        self.tilt += (clamp(self.vx * 0.0009, -0.35, 0.35) - self.tilt) * k(6)
        self.eye_wide *= math.exp(-dt * 1.6)
        speed = math.hypot(self.vx, self.vy)
        tgt = 0.08 if self.mood == "sleep" else 0.3 + 0.7 * min(1.0, speed / 500)
        self.thrust += (tgt - self.thrust) * k(8)
        self.sq_v += (1 - self.squash) * 220 * dt
        self.sq_v *= math.exp(-dt * 11)
        self.squash += self.sq_v * dt
        self.bob = math.sin(self.t * 2.4) * (1.5 if self.mood == "sleep" else 4.0)

        if self.blink_t > 0:
            self.blink_t -= dt
            self.blink = math.sin(math.pi * clamp(1 - self.blink_t / 0.16, 0, 1))
        else:
            self.blink = 0.0
            if self.t > self.next_blink:
                self.blink_t = 0.16
                self.next_blink = self.t + (0.25 if random.random() < 0.2
                                            else random.uniform(2.2, 5.5))

        s = self.scale
        drive = self.skin["drive"]
        if drive == "smoke" and self.mood != "sleep":
            self.puff_acc += dt * (1.5 + 14 * self.thrust)
            while self.puff_acc > 1:
                self.puff_acc -= 1
                lx, ly = (0, 58) if random.random() < 0.7 else (random.uniform(-6, 6), -66)
                px, py = self.local_to_screen(lx, ly)
                self.particles.append(Particle(
                    px, py, random.uniform(-12, 12) - self.vx * 0.1,
                    (30 if ly > 0 else -28) + random.uniform(-8, 8),
                    random.uniform(0.7, 1.2), random.uniform(4, 7) * s,
                    (0.86, 0.86, 0.9), "puff"))
        if speed > 120 and self.mood != "sleep" and drive in ("jet", "twin"):
            self.emit_acc += dt * min(60, speed / 8)
            ang = self.tilt + self.spin
            while self.emit_acc > 1:
                self.emit_acc -= 1
                px = self.x - math.sin(ang) * 60 * s
                py = self.y + self.bob + math.cos(ang) * 60 * s
                self.particles.append(Particle(
                    px + random.uniform(-4, 4), py, -self.vx * 0.12 + random.uniform(-20, 20),
                    40 + random.uniform(0, 40), random.uniform(0.3, 0.55),
                    random.uniform(1.8, 3.4), random.choice([EYE, (0.6, 0.75, 1.0)])))

        alive = []
        for p in self.particles:
            p.life -= dt
            if p.life > 0:
                p.vy += p.g * dt
                p.x += p.vx * dt
                p.y += p.vy * dt
                alive.append(p)
        self.particles = alive[-400:]
        self.floats = [f for f in self.floats if self.t - f.t0 < f.dur]
        if self.bubble and self.t > self.bubble[2]:
            self.bubble = None
        if self.target:
            self.target.bump *= math.exp(-dt * 7)
            if self.target.leave_t and self.t - self.target.leave_t > 0.45:
                self.target = None

    def local_to_screen(self, lx, ly):
        s = self.scale
        ang = self.tilt + self.spin + self.tilt_extra
        lx, ly = lx * s * (2 - self.squash), ly * s * self.squash
        return (self.x + lx * math.cos(ang) - ly * math.sin(ang),
                self.y + self.bob + self.hop + lx * math.sin(ang) + ly * math.cos(ang))

    def frame_rate(self):
        """60 fps while anything is happening, 20 fps while quietly napping."""
        quiet = (self.task_name in ("idle", "sleep") and self.mood == "sleep"
                 and self.bubble is None and self.item is None
                 and self.t > self.peek_until
                 and all(p.kind == "z" for p in self.particles))
        return 20 if quiet else 60

    # ----- regions that need redrawing this frame
    def rects(self):
        s = self.scale
        x, y = self.x, self.y + self.bob + self.hop
        out = [(x - 150 * s, y - 170 * s, 300 * s, 340 * s)]
        if self.bubble:
            bw = self.bubble_w + 40
            out.append((x - bw / 2, y - 62 * s - 80, bw, 90))
        if self.target:
            t = self.target
            out.append((t.x - 40, t.y - 40, t.w + 80, t.h + 80))
        if self.item and self.item.free:
            out.append((self.item.sx - 80 * s, self.item.sy - 70 * s, 160 * s, 150 * s))
        if self.particles:
            xs = [p.x for p in self.particles]
            ys = [p.y for p in self.particles]
            out.append((min(xs) - 20, min(ys) - 20, max(xs) - min(xs) + 40, max(ys) - min(ys) + 40))
        for f in self.floats:
            out.append((f.x - 120, f.y - 70, 240, 90))
        return out

    # ----- drawing
    def paint(self, cr):
        if self.target:
            self.paint_slot(cr, self.target)
        if self.item and self.item.free:
            it = self.item
            cr.save()
            cr.translate(it.sx, it.sy)
            cr.rotate(it.rot)
            cr.scale(self.scale * it.scale, self.scale * it.scale)
            draw_item(cr, it)
            cr.restore()
        self.paint_robot(cr)
        if self.mood == "dizzy":
            s = self.scale
            cx, cy = self.x, self.y + self.bob + self.hop - 58 * s
            for i in range(3):
                a = self.t * 4 + i * 2 * math.pi / 3
                star(cr, cx + math.cos(a) * 30 * s, cy + math.sin(a) * 8 * s, 5.5 * s)
                cr.set_source_rgba(*ACCENT2, 0.95)
                cr.fill()
        self.paint_particles(cr)
        if self.bubble:
            self.paint_bubble(cr)

    def paint_slot(self, cr, sl):
        u = clamp((self.t - sl.t0) / 0.35, 0, 1)
        a = ease_io(u)
        if sl.leave_t:
            a *= 1 - clamp((self.t - sl.leave_t) / 0.45, 0, 1)
        pad = 8 - 4 * sl.bump
        cr.save()
        glow(cr, sl.x + sl.w / 2, sl.y + sl.h / 2, max(sl.w, sl.h) * 0.75, EYE, 0.22 * a)
        rrect(cr, sl.x + pad, sl.y + pad, sl.w - 2 * pad, sl.h - 2 * pad, 14)
        cr.set_source_rgba(*EYE, 0.10 * a)
        cr.fill_preserve()
        cr.set_dash([7, 6], (self.t * 30) % 13)
        cr.set_line_width(2)
        cr.set_source_rgba(*EYE, 0.85 * a)
        cr.stroke()
        cr.restore()

    def paint_robot(self, cr):
        sk = self.skin
        s, t = self.scale, self.t
        x, y = self.x, self.y + self.bob + self.hop
        cr.save()
        cr.translate(x, self.y + self.bob + 128 * s)
        cr.scale(1, 0.28)
        glow(cr, 0, 0, 48 * s * (1 - self.bob / 40 + self.hop / 120), (0.05, 0.08, 0.15), 0.2)
        cr.restore()

        self.paint_alert(cr, x, y)

        cr.save()
        cr.translate(x, y)
        cr.rotate(self.tilt + self.spin + self.tilt_extra)
        cr.scale(s * (2 - self.squash), s * self.squash)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        self.draw_drive(cr, sk, t, back=True)
        if self.item and not self.item.free:
            it = self.item
            cr.save()
            cr.translate(0, 92 + it.dy)
            cr.rotate(math.sin(t * 3.1) * 0.08 - self.tilt * 0.6 + it.rot)
            cr.scale(max(0.01, it.scale), max(0.01, it.scale))
            draw_item(cr, it)
            cr.restore()
        self.draw_torso(cr, sk, t)
        self.draw_drive(cr, sk, t, back=False)
        self.draw_arms(cr, t)
        self.draw_sides(cr, sk, t)
        self.draw_head(cr, sk, t)
        cr.restore()

    # ----- robot parts
    def flame(self, cr, ox, oy, th, k):
        if th <= 0.02:
            return
        L = (14 + 28 * th + random.uniform(-3, 3) * th) * k
        w = (7 + 3 * th) * k
        glow(cr, ox, oy + 4, (20 + 16 * th) * k, FLAME, 0.32 * th)
        cr.move_to(ox - w, oy)
        cr.curve_to(ox - w * 0.8, oy + L * 0.5, ox - 2, oy + L * 0.9, ox, oy + L)
        cr.curve_to(ox + 2, oy + L * 0.9, ox + w * 0.8, oy + L * 0.5, ox + w, oy)
        cr.close_path()
        g = cairo.LinearGradient(0, oy, 0, oy + L)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.95)
        g.add_color_stop_rgba(0.35, *FLAME, 0.85)
        g.add_color_stop_rgba(1, *FLAME, 0.0)
        cr.set_source(g)
        cr.fill()

    def draw_drive(self, cr, sk, t, back):
        d, th = sk["drive"], self.thrust
        if back:
            if d == "jet":
                self.flame(cr, 0, 52, th, 1.0)
            elif d == "twin":
                for sx in (-12, 12):
                    self.flame(cr, sx, 55, th, 0.72)
            elif d == "hover":
                glow(cr, 0, 60, 22 + 12 * th, EYE, 0.18 + 0.2 * th)
            elif d == "smoke":
                glow(cr, 0, 56, 10 + 6 * th, (1.0, 0.55, 0.15), 0.35 * th)
            elif d == "saucer" and (self.item is not None or th > 0.45):
                g = cairo.LinearGradient(0, 52, 0, 125)
                g.add_color_stop_rgba(0, *EYE, 0.35)
                g.add_color_stop_rgba(1, *EYE, 0.0)
                cr.move_to(-14, 52)
                cr.line_to(14, 52)
                cr.line_to(36, 125)
                cr.line_to(-36, 125)
                cr.close_path()
                cr.set_source(g)
                cr.fill()
            elif d == "wings":
                flap = 0.25 + 0.75 * abs(math.sin(t * (8 + 30 * th)))
                for side in (-1, 1):
                    cr.save()
                    cr.translate(side * 16, 20)
                    cr.rotate(side * -0.85)
                    cr.scale(1, flap)
                    for k, (rx, ry, off) in enumerate(((27, 12, 24), (18, 8, 14))):
                        cr.save()
                        cr.translate(side * off, 6 + k * 10)
                        cr.scale(rx, ry)
                        cr.arc(0, 0, 1, 0, 2 * math.pi)
                        cr.restore()
                        cr.set_source_rgba(0.92, 0.97, 1.0, 0.55)
                        cr.fill_preserve()
                        cr.set_source_rgba(1, 1, 1, 0.85)
                        cr.set_line_width(1.2)
                        cr.stroke()
                    cr.restore()
            return
        if d == "jet":
            rrect(cr, -9, 47, 18, 7, 3)
            cr.set_source_rgb(*[c * 0.6 for c in LIMB])
            cr.fill()
        elif d == "twin":
            for sx in (-12, 12):
                rrect(cr, sx - 5.5, 34, 11, 22, 4)
                cr.set_source(vgrad(34, 56, ACCENT, [c * 0.7 for c in ACCENT]))
                cr.fill_preserve()
                cr.set_source_rgba(*OUTLINE, 0.4)
                cr.set_line_width(1.2)
                cr.stroke()
        elif d == "smoke":
            rrect(cr, -6, 46, 12, 9, 2)
            cr.set_source_rgb(*[c * 0.7 for c in LIMB])
            cr.fill()
        elif d == "hover":
            cr.save()
            cr.translate(0, 52)
            cr.scale(1, 0.3)
            cr.arc(0, 0, 18, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgba(*EYE, 0.75)
            cr.set_line_width(2)
            cr.stroke()
        elif d == "saucer":
            cr.save()
            cr.translate(0, 47)
            cr.scale(1, 0.27)
            cr.arc(0, 0, 50, 0, 2 * math.pi)
            cr.restore()
            cr.set_source(vgrad(34, 60, (0.88, 0.91, 0.97), (0.42, 0.47, 0.57)))
            cr.fill_preserve()
            cr.set_source_rgba(*OUTLINE, 0.45)
            cr.set_line_width(1.3)
            cr.stroke()
            for i in range(9):
                a = i / 9 * 2 * math.pi + t * 1.6
                if math.sin(a) < -0.1:
                    continue
                lx, ly = math.cos(a) * 42, 47 + math.sin(a) * 10
                col = (ACCENT, ACCENT2, EYE)[i % 3]
                al = 0.6 + 0.4 * math.sin(t * 6 + i)
                glow(cr, lx, ly, 6, col, 0.5 * al)
                cr.arc(lx, ly, 2.2, 0, 2 * math.pi)
                cr.set_source_rgba(*col, al)
                cr.fill()

    def draw_torso(self, cr, sk, t):
        r = 5 if sk["head"] in ("box", "tall") else 11
        rrect(cr, -20, 24, 40, 26, r)
        cr.set_source(vgrad(24, 50, BODY_TOP, BODY_BOT))
        cr.fill()
        if "stripes" in sk["extras"]:
            cr.save()
            rrect(cr, -20, 24, 40, 26, r)
            cr.clip()
            for yy in (30, 40):
                cr.rectangle(-22, yy, 44, 5)
            cr.set_source_rgb(*ACCENT)
            cr.fill()
            cr.restore()
        rrect(cr, -20, 24, 40, 26, r)
        cr.set_source_rgba(*OUTLINE, 0.35)
        cr.set_line_width(1.5)
        cr.stroke()
        if "neon" in sk["extras"]:
            self.neon_stroke(cr, lambda: rrect(cr, -20, 24, 40, 26, r))
        pulse = 0.5 + 0.5 * math.sin(t * 3)
        if "heart" in sk["extras"]:
            glow(cr, 0, 37, 11, ACCENT, 0.3 + 0.3 * pulse)
            heart(cr, 0, 37, 4.2 + 0.6 * pulse)
            cr.set_source_rgb(*ACCENT)
            cr.fill()
        elif "dial" in sk["extras"]:
            cr.arc(0, 37, 6, 0, 2 * math.pi)
            cr.set_source_rgb(0.96, 0.95, 0.9)
            cr.fill_preserve()
            cr.set_source_rgba(*OUTLINE, 0.6)
            cr.set_line_width(1.2)
            cr.stroke()
            a = -math.pi / 2 + math.sin(t * 1.7) * 1.1 + self.thrust
            cr.move_to(0, 37)
            cr.line_to(math.cos(a) * 4.8, 37 + math.sin(a) * 4.8)
            cr.set_source_rgb(*ACCENT)
            cr.set_line_width(1.4)
            cr.stroke()
        elif "stripes" not in sk["extras"]:
            glow(cr, 0, 37, 10, ACCENT2, 0.3 + 0.35 * pulse)
            cr.arc(0, 37, 3.2, 0, 2 * math.pi)
            cr.set_source_rgb(*ACCENT2)
            cr.fill()

    def draw_arms(self, cr, t):
        for side in (-1, 1):
            a = self.arm
            sx, sy = side * 19, 33
            ex, ey = side * 31, lerp(42, 54, a)
            hx = lerp(side * 27, side * 22, a)
            hy = lerp(55, 66, a) + math.sin(t * 2.2 + side) * 2 * (1 - a)
            dangle = clamp(-self.vx * 0.012, -9, 9) * (1 - 0.5 * a)
            hx += dangle
            hy -= abs(dangle) * 0.3
            cr.move_to(sx, sy)
            cr.line_to(ex, ey)
            cr.line_to(hx, hy)
            cr.set_source_rgb(*LIMB)
            cr.set_line_width(5.5)
            cr.stroke()
            ang = math.atan2(hy - ey, hx - ex)
            for f in (-1, 1):
                fa = ang + f * (0.2 + 0.6 * self.claw)
                cr.move_to(hx, hy)
                cr.line_to(hx + math.cos(fa) * 8, hy + math.sin(fa) * 8)
            cr.set_line_width(3)
            cr.stroke()
            cr.arc(hx, hy, 4.6, 0, 2 * math.pi)
            cr.set_source_rgb(*BODY_TOP)
            cr.fill_preserve()
            cr.set_source_rgba(*OUTLINE, 0.5)
            cr.set_line_width(1.2)
            cr.stroke()

    def draw_sides(self, cr, sk, t):
        kind = sk["sides"]
        hw = 32 if sk["head"] == "tall" else 36 if sk["head"] == "box" else 38
        for side in (-1, 1):
            if kind == "disc":
                cr.arc(side * hw, -2, 7.5, 0, 2 * math.pi)
                cr.set_source_rgb(*[c * 0.8 for c in BODY_BOT])
                cr.fill()
                glow(cr, side * (hw + 1.5), -2, 8, EYE, 0.5)
                cr.arc(side * (hw + 1.5), -2, 3, 0, 2 * math.pi)
                cr.set_source_rgb(*EYE)
                cr.fill()
            elif kind == "bolt":
                cr.arc(side * (hw + 1), -2, 6.5, 0, 2 * math.pi)
                cr.set_source_rgb(0.62, 0.64, 0.70)
                cr.fill_preserve()
                cr.set_source_rgba(*OUTLINE, 0.5)
                cr.set_line_width(1.2)
                cr.stroke()
                for a in (0.6, 2.17):
                    cr.move_to(side * (hw + 1) + math.cos(a) * 4, -2 + math.sin(a) * 4)
                    cr.line_to(side * (hw + 1) - math.cos(a) * 4, -2 - math.sin(a) * 4)
                cr.set_line_width(1.4)
                cr.stroke()
            elif kind == "gear":
                gear(cr, side * (hw + 2), 0, 11, 8, side * t * 1.5)
                cr.set_source(vgrad(-11, 11, ACCENT2, [c * 0.6 for c in ACCENT2]))
                cr.fill_preserve()
                cr.set_source_rgba(*OUTLINE, 0.6)
                cr.set_line_width(1)
                cr.stroke()
                cr.arc(side * (hw + 2), 0, 3, 0, 2 * math.pi)
                cr.set_source_rgb(*OUTLINE)
                cr.fill()
            elif kind == "fins":
                cr.move_to(side * 30, -14)
                cr.line_to(side * 47, -28)
                cr.line_to(side * 41, 8)
                cr.close_path()
                cr.set_source(vgrad(-28, 8, ACCENT, [c * 0.75 for c in ACCENT]))
                cr.fill()

    def neon_stroke(self, cr, path_fn):
        path_fn()
        cr.set_source_rgba(*ACCENT, 0.28)
        cr.set_line_width(6)
        cr.stroke()
        path_fn()
        cr.set_source_rgba(*ACCENT, 0.95)
        cr.set_line_width(2)
        cr.stroke()

    def draw_head(self, cr, sk, t):
        head = sk["head"]
        flash = clamp(1 - (t - self.alert_t) / 1.2, 0, 1) * (0.5 + 0.5 * math.sin(t * 30))
        if head == "cat":
            for side in (-1, 1):
                cr.move_to(side * 36, -12)
                cr.line_to(side * 30, -47)
                cr.line_to(side * 9, -28)
                cr.close_path()
                cr.set_source(vgrad(-47, -12, BODY_TOP, BODY_BOT))
                cr.fill_preserve()
                cr.set_source_rgba(*OUTLINE, 0.35)
                cr.set_line_width(1.5)
                cr.stroke()
                cr.move_to(side * 31, -19)
                cr.line_to(side * 28.5, -39)
                cr.line_to(side * 16, -28)
                cr.close_path()
                cr.set_source_rgba(*CHEEK, 0.8)
                cr.fill()
        if head == "helmet":
            rrect(cr, -30, 12, 60, 15, 7)
            cr.set_source(vgrad(12, 27, BODY_TOP, BODY_BOT))
            cr.fill_preserve()
            cr.set_source_rgba(*OUTLINE, 0.35)
            cr.set_line_width(1.5)
            cr.stroke()
            self.draw_face(cr, sk, t)
            cr.arc(0, -8, 34, 0, 2 * math.pi)
            cr.set_source_rgba(*EYE, 0.10)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.75)
            cr.set_line_width(2.2)
            cr.stroke()
            cr.arc(0, -8, 27, math.radians(200), math.radians(245))
            cr.set_source_rgba(1, 1, 1, 0.6)
            cr.set_line_width(4)
            cr.stroke()
            cr.arc(0, 26, 4, 0, 2 * math.pi)  # little collar light
            glow(cr, 0, 22, 8, ACCENT, 0.4 + 0.4 * flash)
            return

        _head_path(cr, head)
        cr.set_source(vgrad(-36, 30, BODY_TOP, BODY_BOT))
        cr.fill()
        if "stripes" in sk["extras"]:
            cr.save()
            _head_path(cr, head)
            cr.clip()
            cr.rectangle(-40, -31, 80, 8)
            cr.set_source_rgb(*ACCENT)
            cr.fill()
            cr.restore()
        _head_path(cr, head)
        cr.set_source_rgba(*OUTLINE, 0.35)
        cr.set_line_width(1.5)
        cr.stroke()
        if head in ("sphere", "dome"):
            cr.save()
            cr.translate(-13, -22)
            cr.scale(1.6, 0.8)
            cr.arc(0, 0, 6, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgba(1, 1, 1, 0.5)
            cr.fill()
        else:
            top = -33 if head == "tall" else -27
            rrect(cr, -28, top, 32, 7, 3.5)
            cr.set_source_rgba(1, 1, 1, 0.55)
            cr.fill()
        if "rivets" in sk["extras"]:
            hw = 26 if head == "tall" else 30
            for rx in (-hw, hw):
                for ry in ((-30 if head == "tall" else -24), 22):
                    cr.arc(rx, ry, 1.8, 0, 2 * math.pi)
                    cr.set_source_rgba(*OUTLINE, 0.45)
                    cr.fill()
        if "neon" in sk["extras"]:
            self.neon_stroke(cr, lambda: _head_path(cr, head))

        self.draw_top(cr, sk, t, flash)
        self.draw_face(cr, sk, t)

        if "whiskers" in sk["extras"]:
            for side in (-1, 1):
                for k, dy in enumerate((-4, 3, 10)):
                    cr.move_to(side * 31, 5 + k * 2.5)
                    cr.line_to(side * 47, dy)
            cr.set_source_rgba(*OUTLINE, 0.55)
            cr.set_line_width(1.4)
            cr.stroke()
        if sk["top"] == "headband":
            rrect(cr, -38.5, -26, 77, 8, 3)
            cr.set_source(vgrad(-26, -18, ACCENT, [c * 0.7 for c in ACCENT]))
            cr.fill()
            dirx = -1 if self.vx > 40 else 1
            for k in range(2):
                ph = t * 9 + k * 1.3
                cr.move_to(dirx * 36, -22 + k * 3)
                cr.curve_to(dirx * 50, -27 + math.sin(ph) * 4 + k * 4,
                            dirx * 58, -16 + math.sin(ph + 1) * 5 + k * 4,
                            dirx * (70 - k * 8), -20 + math.sin(ph + 2) * 6 + k * 6)
                cr.set_source_rgb(*ACCENT)
                cr.set_line_width(4 - k)
                cr.stroke()

    def draw_top(self, cr, sk, t, flash):
        top = sk["top"]
        ty = -36 if sk["head"] == "tall" else -35 if sk["head"] == "sphere" else -30
        p2 = 0.5 + 0.5 * math.sin(t * 5)
        if self.mood == "sleep":
            p2 *= 0.3
        wob = math.sin(t * 4) * 3 + clamp(-self.vx * 0.006, -6, 6)
        if top == "ball":
            cr.move_to(0, ty)
            cr.curve_to(0, ty - 8, wob * 0.5, ty - 11, wob, ty - 17)
            cr.set_source_rgb(*LIMB)
            cr.set_line_width(2.5)
            cr.stroke()
            glow(cr, wob, ty - 19, 13 + 14 * flash, ACCENT, 0.35 + 0.35 * p2 + 0.4 * flash)
            cr.arc(wob, ty - 19, 4.6, 0, 2 * math.pi)
            cr.set_source_rgb(*ACCENT)
            cr.fill()
            cr.arc(wob - 1.4, ty - 20.5, 1.4, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.8)
            cr.fill()
        elif top == "double":
            for side in (-1, 1):
                bx, by = side * 23 + wob, ty - 19
                cr.move_to(side * 12, ty + 2)
                cr.curve_to(side * 14, ty - 8, side * 18 + wob * 0.6, ty - 13, bx, by)
                cr.set_source_rgb(*[c * 0.8 for c in OUTLINE] if sk["drive"] == "wings" else LIMB)
                cr.set_line_width(2.4)
                cr.stroke()
                glow(cr, bx, by, 10 + 10 * flash, ACCENT2, 0.25 + 0.3 * p2 + 0.4 * flash)
                cr.arc(bx, by, 4.2, 0, 2 * math.pi)
                cr.set_source_rgb(*ACCENT)
                cr.fill()
        elif top == "coil":
            cr.move_to(0, ty)
            for i in range(1, 7):
                cr.line_to((4 if i % 2 else -4) + wob * i / 7, ty - i * 2.6)
            cr.line_to(wob, ty - 19)
            cr.set_source_rgb(*LIMB)
            cr.set_line_width(2)
            cr.stroke()
            glow(cr, wob, ty - 21, 12 + 14 * flash, ACCENT, 0.35 + 0.35 * p2 + 0.4 * flash)
            cr.arc(wob, ty - 21, 4.4, 0, 2 * math.pi)
            cr.set_source_rgb(*ACCENT)
            cr.fill()
        elif top == "propeller":
            cr.move_to(0, ty)
            cr.line_to(0, ty - 9)
            cr.set_source_rgb(*LIMB)
            cr.set_line_width(3)
            cr.stroke()
            spin = t * (3 if self.mood == "sleep" else 22 + 20 * self.thrust)
            cr.save()
            cr.translate(0, ty - 10)
            cr.scale(1, 0.16)
            cr.arc(0, 0, 32, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgba(*LIMB, 0.18)
            cr.fill()
            cr.save()
            cr.translate(0, ty - 10)
            cr.scale(max(0.06, abs(math.cos(spin))), 1)
            rrect(cr, -32, -2.5, 64, 5, 2.5)
            cr.restore()
            cr.set_source_rgb(*[c * 0.85 for c in BODY_TOP])
            cr.fill()
            cr.arc(0, ty - 10, 3.4, 0, 2 * math.pi)
            cr.set_source_rgb(*ACCENT)
            cr.fill()
            glow(cr, 0, ty - 10, 8 + 10 * flash, ACCENT, 0.3 * p2 + 0.4 * flash)
        elif top == "tophat":
            hat = (0.13, 0.10, 0.10)
            rrect(cr, -25, ty - 3, 50, 6, 3)
            cr.set_source_rgb(*hat)
            cr.fill()
            rrect(cr, -16, ty - 28, 32, 26, 3)
            cr.set_source(vgrad(ty - 28, ty, (0.24, 0.20, 0.20), hat))
            cr.fill()
            rrect(cr, -16, ty - 9, 32, 5, 1)
            cr.set_source_rgb(*ACCENT)
            cr.fill()
            cr.arc(10, ty - 6.5, 2.2, 0, 2 * math.pi)  # brass buckle glint
            cr.set_source_rgb(*ACCENT2)
            cr.fill()
            glow(cr, 0, ty - 15, 14 * flash, ACCENT2, 0.5 * flash)

    def draw_face(self, cr, sk, t):
        face, style = sk["face"], sk["eyes"]
        centers, sz = FACE_EYES[face]
        # face plate
        if face == "lens":
            cr.arc(0, -2, 22, 0, 2 * math.pi)
            cr.set_source(vgrad(-24, 20, LIMB, OUTLINE))
            cr.fill()
            for i in range(12):
                a = i * math.pi / 6
                cr.move_to(math.cos(a) * 18.5, -2 + math.sin(a) * 18.5)
                cr.line_to(math.cos(a) * 21, -2 + math.sin(a) * 21)
            cr.set_source_rgba(1, 1, 1, 0.25)
            cr.set_line_width(1.2)
            cr.stroke()
        elif face == "goggles":
            cr.move_to(-32, -4)
            cr.line_to(32, -4)
            cr.set_source_rgb(*OUTLINE)
            cr.set_line_width(4)
            cr.stroke()
            for sx in (-14, 14):
                cr.arc(sx, -4, 12.5, 0, 2 * math.pi)
                cr.set_source(vgrad(-17, 9, ACCENT2, [c * 0.55 for c in ACCENT2]))
                cr.fill_preserve()
                cr.set_source_rgba(*OUTLINE, 0.7)
                cr.set_line_width(1.2)
                cr.stroke()
        if face != "none":
            _face_path(cr, face)
            cr.set_source(vgrad(-21, 17, VISOR_A, VISOR_B))
            cr.fill()
            if face == "screen":
                _face_path(cr, face)
                cr.set_source_rgb(*[c * 0.75 for c in LIMB])
                cr.set_line_width(3)
                cr.stroke()

        cr.save()
        _face_path(cr, face)
        cr.clip()
        if face == "screen":
            for yy in range(-20, 16, 3):
                cr.rectangle(-30, yy, 60, 1)
            cr.set_source_rgba(1, 1, 1, 0.05)
            cr.fill()
        if CHEEK is not None and face in ("visor", "screen", "disc", "none"):
            cy = 9 if face != "disc" else 2
            cx = 20 if face != "disc" else 13
            for side in (-1, 1):
                glow(cr, side * cx, cy, 7, CHEEK, 0.35)
        self.draw_eyes(cr, style, centers, sz, t)
        if face in ("visor", "slit", "screen"):
            g = cairo.LinearGradient(0, -20, 0, -6)
            g.add_color_stop_rgba(0, 1, 1, 1, 0.16)
            g.add_color_stop_rgba(1, 1, 1, 1, 0)
            cr.set_source(g)
            rrect(cr, -26, -19, 52, 12, 6)
            cr.fill()
        elif face in ("lens", "goggles", "disc"):
            for cx, cy in centers:
                cr.arc(cx - 4, cy - 5, 2.4, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.55)
                cr.fill()
        cr.restore()

    def draw_eyes(self, cr, style, centers, sz, t):
        lx, ly = self.look
        ox, oy = lx * 5 * sz, ly * 3 * sz
        dark = style == "almond"
        col = VISOR_B if dark else EYE
        mood = self.mood
        if mood == "dizzy":
            for cx, cy in centers:
                r = (8 if len(centers) == 1 else 6) * sz
                cr.save()
                cr.translate(cx, cy)
                cr.rotate(t * 9 * (1 if cx <= 0 else -1))
                cr.move_to(0, 0)
                for i in range(1, 40):
                    a, rr = i * 0.42, r * i / 39
                    cr.line_to(math.cos(a) * rr, math.sin(a) * rr)
                cr.restore()
                cr.set_source_rgb(*col)
                cr.set_line_width(1.9)
                cr.stroke()
            return
        if mood == "happy":
            for cx, cy in centers:
                r = (9 if len(centers) == 1 else 6) * sz
                if not dark:
                    glow(cr, cx + ox, cy + oy + 2, 15 * sz, EYE, 0.35)
                cr.arc(cx + ox, cy + oy + 5 * sz, r, math.pi, 2 * math.pi)
                cr.set_source_rgb(*col)
                cr.set_line_width(4 if dark else 3.4)
                cr.stroke()
            return
        if mood == "sleep":
            peek = (clamp((self.peek_until - t) / 0.3, 0, 1)
                    * clamp((t - (self.peek_until - 1.8)) / 0.3, 0, 1))
            for i, (cx, cy) in enumerate(centers):
                if i == len(centers) - 1 and peek > 0.05:
                    self.draw_eye(cr, style, cx - 2 * peek, cy + 1, sz, 0.55 * peek, 1.0)
                    continue
                w = (7 if len(centers) == 1 else 5) * sz
                cr.move_to(cx - w, cy + 2)
                cr.line_to(cx + w, cy + 2)
                cr.set_source_rgba(*col, 0.75)
                cr.set_line_width(2.6)
                cr.stroke()
            return
        if mood == "scan":
            for cx, cy in centers:
                if not dark:
                    glow(cr, cx + ox, cy + oy, 14 * sz, EYE, 0.35)
                rrect(cr, cx + ox - 7 * sz, cy + oy - 2, 14 * sz, 4, 2)
                cr.set_source_rgb(*col)
                cr.fill()
            sx = math.sin(t * 7) * 24
            g = cairo.LinearGradient(sx - 8, 0, sx + 8, 0)
            g.add_color_stop_rgba(0, *EYE, 0)
            g.add_color_stop_rgba(0.5, *EYE, 0.55)
            g.add_color_stop_rgba(1, *EYE, 0)
            cr.set_source(g)
            cr.rectangle(sx - 8, -36, 16, 60)
            cr.fill()
            return
        wide = self.eye_wide
        for i, (cx, cy) in enumerate(centers):
            h = (1 - self.blink) * (1 + 0.35 * wide)
            if mood == "confused" and i == len(centers) - 1:
                h *= 0.45
            self.draw_eye(cr, style, cx + ox, cy + oy, sz, h, 1 + 0.3 * wide)

    def draw_eye(self, cr, style, cx, cy, sz, hm, wm):
        hm = max(0.12, hm)
        if style != "almond":
            glow(cr, cx, cy, (15 + 6 * self.eye_wide) * sz, EYE, 0.38 + 0.2 * self.eye_wide)
        cr.set_source_rgb(*EYE)
        if style == "pill":
            h, w = 15 * sz * hm, 10 * sz * wm
            rrect(cr, cx - w / 2, cy - h / 2, w, h, min(w / 2, h / 2))
            cr.fill()
            if h > 6:
                cr.arc(cx - 1.6, cy - h / 4, 1.6, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.85)
                cr.fill()
        elif style == "round":
            cr.save()
            cr.translate(cx, cy)
            cr.scale(wm, hm)
            cr.arc(0, 0, 6.8 * sz, 0, 2 * math.pi)
            cr.restore()
            cr.fill()
            if hm > 0.5:
                cr.arc(cx - 2, cy - 2.5 * hm, 1.9 * sz, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.9)
                cr.fill()
        elif style == "bar":
            side = -1 if cx < 0 else 1
            cr.save()
            cr.translate(cx, cy)
            cr.rotate(side * 0.18)
            w, h = 17 * sz * wm, max(2.0, 5.5 * hm)
            rrect(cr, -w / 2, -h / 2, w, h, h / 2)
            cr.restore()
            cr.fill()
        elif style == "cyclops":
            r = 10.5 * sz * wm
            cr.save()
            cr.translate(cx, cy)
            cr.scale(1, hm)
            cr.arc(0, 0, r, 0, 2 * math.pi)
            cr.restore()
            cr.fill()
            if hm > 0.4:
                cr.arc(cx + self.look[0] * 2, cy + self.look[1] * 2, 4.2 * sz, 0, 2 * math.pi)
                cr.set_source_rgb(*VISOR_B)
                cr.fill()
                cr.arc(cx + 3.5, cy - 3.5, 2.2, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.8)
                cr.fill()
        elif style == "pixel":
            rows = (-1, 0, 1) if hm > 0.55 else (0,)
            for r in rows:
                for c in (-1, 0, 1):
                    if r != 0 and c != 0 and hm < 1.2:
                        continue  # a "plus" shape; wide-eyed fills the square
                    cr.rectangle(cx + c * 4.3 * wm - 1.7, cy + r * 4.3 - 1.7, 3.4, 3.4)
            cr.fill()
        elif style == "dial":
            cr.save()
            cr.translate(cx, cy)
            cr.scale(wm, hm)
            cr.arc(0, 0, 7 * sz, 0, 2 * math.pi)
            cr.restore()
            cr.fill()
            if hm > 0.5:
                cr.arc(cx, cy, 3.6 * sz, 0, 2 * math.pi)
                cr.set_source_rgba(*VISOR_B, 0.8)
                cr.set_line_width(1.4)
                cr.stroke()
                cr.arc(cx, cy, 1.2, 0, 2 * math.pi)
                cr.set_source_rgb(*VISOR_B)
                cr.fill()
        elif style == "almond":
            side = -1 if cx < 0 else 1
            cr.save()
            cr.translate(cx, cy)
            cr.rotate(side * 0.45)
            cr.scale(8.5 * sz * wm, 13 * sz * hm)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgb(*VISOR_B)
            cr.fill_preserve()
            cr.set_source_rgba(*EYE, 0.55)
            cr.set_line_width(1.4)
            cr.stroke()
            if hm > 0.4:
                cr.arc(cx - side * 2.5, cy - 4 * hm, 2.3, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.85)
                cr.fill()

    def paint_alert(self, cr, x, y):
        """The '!' badge that pops above the head when it notices a mess."""
        el = self.t - self.alert_t
        if el < 0 or el > 1.7:
            return
        s = self.scale
        pop = ease_out_back(clamp(el / 0.25, 0, 1))
        a = clamp((1.7 - el) / 0.3, 0, 1)
        wig = math.sin(el * 22) * 0.18 * clamp(1 - el / 0.8, 0, 1)
        cr.save()
        cr.translate(x + 34 * s, y - 72 * s)
        cr.rotate(wig)
        cr.scale(max(0.01, pop * s), max(0.01, pop * s))
        glow(cr, 0, 0, 26, ACCENT2, 0.45 * a)
        cr.arc(0, 0, 13, 0, 2 * math.pi)
        badge = ACCENT if sum(ACCENT) > 0.9 else (1.0, 0.36, 0.36)
        cr.set_source_rgba(*badge, a)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.9 * a)
        cr.set_line_width(2.5)
        cr.stroke()
        rrect(cr, -2.4, -8, 4.8, 10, 2.4)
        cr.arc(0, 6.2, 2.6, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, a)
        cr.fill()
        cr.restore()

    def paint_particles(self, cr):
        for p in self.particles:
            a = clamp(p.life / p.max, 0, 1)
            if p.kind == "star":
                star(cr, p.x, p.y, p.size * (0.6 + 0.4 * a))
                cr.set_source_rgba(*p.rgb, a)
                cr.fill()
            elif p.kind == "puff":
                cr.arc(p.x, p.y, p.size * (1.8 - a), 0, 2 * math.pi)
                cr.set_source_rgba(*p.rgb, a * 0.42)
                cr.fill()
            elif p.kind == "z":
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(p.size)
                cr.move_to(p.x, p.y)
                cr.set_source_rgba(0.85, 0.95, 1.0, a * 0.9)
                cr.show_text("z")
            else:
                cr.arc(p.x, p.y, p.size * a, 0, 2 * math.pi)
                cr.set_source_rgba(*p.rgb, a * 0.8)
                cr.fill()

    def paint_bubble(self, cr):
        text, t0, until = self.bubble
        s = self.scale
        u = clamp((self.t - t0) / 0.25, 0, 1)
        sc = max(0.01, ease_out_back(u))
        a = clamp((until - self.t) / 0.2, 0, 1)
        x, y = self.x, self.y + self.bob + self.hop - 64 * s
        if self.t - self.alert_t < 1.7:
            y -= 22 * s   # leave room for the "!" badge
        cr.save()
        cr.translate(x, y)
        cr.scale(sc, sc)
        lay, w, h = text_layout(cr, text, "Sans Bold 10")
        self.bubble_w = w + 24
        bw, bh = w + 24, h + 14
        # keep the bubble on screen near the top edge and the sides
        y_min = self.area[1] + 6 + (bh + 12) * sc
        if y < y_min:
            cr.translate(0, y_min - y)
        ax, aw = self.area[0], self.area[2]
        if x - bw / 2 * sc < ax + 6:
            cr.translate(ax + 6 - (x - bw / 2 * sc), 0)
        elif x + bw / 2 * sc > ax + aw - 6:
            cr.translate(ax + aw - 6 - (x + bw / 2 * sc), 0)
        bx, by = -bw / 2, -bh - 12
        rrect(cr, bx + 1, by + 3, bw, bh, 12)
        cr.set_source_rgba(0, 0, 0, 0.18 * a)
        cr.fill()
        rrect(cr, bx, by, bw, bh, 12)
        cr.move_to(-7, by + bh - 1)
        cr.line_to(0, by + bh + 9)
        cr.line_to(7, by + bh - 1)
        cr.close_path()
        cr.set_source_rgba(1, 1, 1, 0.97 * a)
        cr.fill()
        cr.set_source_rgba(0.12, 0.16, 0.26, a)
        show_text(cr, lay, text, -w / 2, by + 7)
        cr.restore()


# ---------------------------------------------------------------- overlay ---
class Perf:
    """--perf: prints frame timing every 5 seconds."""

    def __init__(self):
        self.reset(time.monotonic())

    def reset(self, now):
        self.t0, self.gaps, self.upd, self.draw, self.scan = now, [], [], [], []

    def report(self, now, bot):
        if now - self.t0 < 5 or not self.gaps:
            return
        hitches = sum(1 for x, aim in self.gaps if x > aim * 1.5 + 8)
        g = sorted(x for x, _ in self.gaps)
        avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
        print(f"[perf] {len(g) / (now - self.t0):5.1f} fps | frame gap avg {avg(g):5.1f}"
              f" p95 {g[int(len(g) * 0.95)]:5.1f} max {g[-1]:6.1f} ms | hitches {hitches}"
              f" | update {avg(self.upd):4.2f} ms (desktop reads {avg(self.scan):4.2f})"
              f" | draw {avg(self.draw):4.2f} ms | {bot.task_name}/{bot.mood}", flush=True)
        self.reset(now)


class Overlay:
    def __init__(self, bot, workarea, perf=False):
        self.bot = bot
        self.perf = Perf() if perf else None
        if self.perf:   # time desktop reads separately
            orig = bot.desk.items

            def timed_items(*a, **k):
                t = time.perf_counter()
                try:
                    return orig(*a, **k)
                finally:
                    self.perf.scan.append((time.perf_counter() - t) * 1000)
            bot.desk.items = timed_items
        self.last = None
        self.prev = []
        # Ubuntu's desktop icons live in the "always below other windows"
        # layer (not the lower "desktop" layer). The robot joins that same
        # layer and stays on top of the icons inside it, so it receives its
        # own clicks - while every normal app window still covers it.
        # The window covers the work area (not the whole monitor) so GNOME
        # never mistakes it for a full-screen app.
        self.ox, self.oy, self.ow, self.oh = workarea
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_below(True)
        win.set_focus_on_map(False)
        win.set_title("TidyBot")
        scr = win.get_screen()
        vis = scr.get_rgba_visual()
        if vis is None or not scr.is_composited():
            print("TidyBot: no compositing available - the robot needs a "
                  "compositing desktop such as GNOME.", file=sys.stderr)
        else:
            win.set_visual(vis)
        win.set_app_paintable(True)
        win.set_accept_focus(False)
        win.move(self.ox, self.oy)
        win.set_default_size(self.ow, self.oh)
        win.resize(self.ow, self.oh)
        area = Gtk.DrawingArea()
        area.connect("draw", self.on_draw)
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        area.connect("button-press-event", self.on_click)
        area.connect("motion-notify-event", self.on_motion)
        area.connect("button-release-event", self.on_release)
        self.press, self.drag_started = None, False
        self.hit = None
        self.menu = None
        self.raised_at, self.want_raise = 0.0, True
        self.aimed_fps = 60
        win.add(area)
        win.show_all()
        win.stick()                                     # every workspace
        win.move(self.ox, self.oy)
        win.input_shape_combine_region(cairo.Region())  # clicks pass through
        area.add_tick_callback(self.on_tick)
        self.win, self.area = win, area

    def on_tick(self, widget, clock):
        now = clock.get_frame_time() / 1e6
        if self.last is not None and now - self.last < 1.0 / self.bot.frame_rate() - 0.004:
            return True          # skip this frame: nothing worth drawing yet
        dt = 0.016 if self.last is None else clamp(now - self.last, 0.0, 0.1)
        if self.perf and self.last is not None:
            self.perf.gaps.append(((now - self.last) * 1000, 1000.0 / self.aimed_fps))
        self.last = now
        self.aimed_fps = self.bot.frame_rate()
        t_upd = time.perf_counter()
        self.bot.update(dt)
        if self.perf:
            self.perf.upd.append((time.perf_counter() - t_upd) * 1000)
            self.perf.report(time.monotonic(), self.bot)
        # Make sure we sit above the desktop-icons layer (still below every
        # app window). Re-stacking makes Ubuntu's desktop-icons app react, so
        # do it rarely: at start, right after you interact with the robot,
        # and otherwise at most every 30 seconds.
        if now - self.raised_at > 30.0 or self.want_raise:
            self.raised_at, self.want_raise = now, False
            gw = self.win.get_window()
            if gw is not None:
                gw.raise_()
                if self.win.get_position() != (self.ox, self.oy):
                    self.win.move(self.ox, self.oy)   # undo any window-manager nudging
        hit = self.bot.hit_rect()
        if hit != self.hit:   # only the robot itself catches clicks
            self.hit = hit
            self.win.input_shape_combine_region(cairo.Region(cairo.RectangleInt(
                hit[0] - self.ox, hit[1] - self.oy, hit[2], hit[3])))
        new = self.bot.rects()
        for r in self.prev + new:
            widget.queue_draw_area(int(r[0] - self.ox) - 2, int(r[1] - self.oy) - 2,
                                   int(r[2]) + 4, int(r[3]) + 4)
        self.prev = new
        return True

    def on_click(self, widget, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return True
        if ev.button == 3:
            self.show_menu(ev)
        elif ev.button == 1:
            self.press, self.drag_started = (ev.x_root, ev.y_root), False
        return True

    def on_motion(self, widget, ev):
        if self.press is None:
            return False
        if not self.drag_started:
            if math.hypot(ev.x_root - self.press[0], ev.y_root - self.press[1]) < 6:
                return True
            self.drag_started = True
            if not self.bot.begin_drag(*self.press):
                self.press = None
                return True
        self.bot.drag_to(ev.x_root, ev.y_root)
        return True

    def on_release(self, widget, ev):
        if ev.button != 1 or self.press is None:
            return False
        if self.drag_started:
            self.bot.end_drag()
        else:
            self.bot.poke()      # a click without moving = poke
        self.press = None
        self.want_raise = True
        return True

    def show_menu(self, ev):
        bot = self.bot
        menu = Gtk.Menu()

        def action(label, cb):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda *_: cb())
            menu.append(item)

        def radio_submenu(title, choices, current, cb):
            parent = Gtk.MenuItem(label=title)
            sub, group = Gtk.Menu(), None
            for value, label in choices:
                item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
                group = item
                item.set_active(value == current)
                item.connect("toggled", lambda w, v=value: w.get_active() and cb(v))
                sub.append(item)
            parent.set_submenu(sub)
            menu.append(parent)

        radio_submenu("Skin", [(k, SKINS[k]["name"]) for k in SKIN_ORDER],
                      bot.cfg.get("skin", "classic"), bot.set_skin)
        radio_submenu("Size", [(0.75, "Small"), (1.0, "Normal"), (1.3, "Large"), (1.6, "Huge")],
                      bot.scale, bot.set_scale)
        menu.append(Gtk.SeparatorMenuItem())
        action("Tidy up now", bot.tidy_now)
        action("Wake up" if bot.paused else "Take a nap", bot.toggle_pause)
        menu.append(Gtk.SeparatorMenuItem())
        action("Quit TidyBot", Gtk.main_quit)
        menu.connect("deactivate", lambda *_: setattr(self, "want_raise", True))
        menu.show_all()
        menu.popup_at_pointer(ev)
        self.menu = menu   # keep a reference while it is open

    def on_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.translate(-self.ox, -self.oy)   # the robot thinks in screen coordinates
        t = time.perf_counter()
        self.bot.paint(cr)
        if self.perf:
            self.perf.draw.append((time.perf_counter() - t) * 1000)
        return True


# ------------------------------------------------------------------- main ---
def read_pid():
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"tidybot" in f.read():
                return pid
    except Exception:
        pass
    return None


def make_demo_files(desk):
    """Create 4 sample files and drop them at random spots on the desktop."""
    plan = desk.plan() or {}
    grid = plan.get("grid") or calibrate([], desk.area, desk.icon_size)
    occ = set(plan.get("occ", {}).keys())
    free = [c for c in grid.slots() if c not in occ]
    random.shuffle(free)
    made = []
    for i, name in enumerate(["tidybot-demo-apple.txt", "tidybot-demo-banana.txt",
                              "tidybot-demo-cherry.txt", "tidybot-demo-date.txt"]):
        final = os.path.join(desk.desktop, name)
        tmp = os.path.join(desk.desktop, HOLD_PREFIX + name)
        with open(tmp, "w") as f:
            f.write("TidyBot demo file - safe to delete.\n")
        if i < len(free):
            set_pos(tmp, *grid.topleft(free[i]))
        gio_move(tmp, final)
        made.append(final)
    return made


def debug_report(desk):
    print("Desktop folder:", desk.desktop)
    print("Work area (x, y, w, h):", desk.area)
    print("Desktop-icons settings found:", desk.ding is not None,
          "| icon size:", desk.icon_size, "| keep-arranged:", desk.keep_arranged_on())
    print("Home icon shown:", desk.home_shown(), "| Home position:",
          get_pos(Gio.File.new_for_path(HOME)), "| Trash position:", desk.specials())
    print("Session:", os.environ.get("XDG_SESSION_TYPE"), "| GDK backend:",
          os.environ.get("GDK_BACKEND"), "| screen:",
          Gdk.Screen.get_default().get_width(), "x", Gdk.Screen.get_default().get_height())
    print("Stored metadata per icon:")
    for it in desk.items():
        try:
            info = Gio.File.new_for_path(it["path"]).query_info("metadata::*", NOFOLLOW, None)
            meta = {a: info.get_attribute_as_string(a) for a in info.list_attributes("metadata")}
        except Exception as e:
            meta = f"error: {e}"
        print(f"  {it['name'][:34]:34}  {meta}")
    try:
        test = os.path.join(desk.desktop, ".tidybot-write-test")
        open(test, "w").close()
        set_pos(test, 10, 10)
        ok = get_pos(Gio.File.new_for_path(test)) == (10.0, 10.0)
        os.remove(test)
        print("Can write icon positions:", ok)
    except Exception as e:
        print("Can write icon positions: NO -", e)
    plan = desk.plan()
    if not plan:
        print("No plan (empty desktop, or a file is newer than the grace period).")
        return
    g = plan["grid"]
    print(f"Grid: origin=({g.ox:.0f},{g.oy:.0f}) cell={g.cw:.1f}x{g.ch:.1f} "
          f"cols={g.cols} rows={g.rows}")
    for it in plan["order"]:
        p = it["path"]
        print(f"  {it['name'][:34]:34}  pos={it['pos']}  cell={plan['where'].get(p)}"
              f"  -> {plan['targets'][p]}{'' if p in plan['todo'] else '  (ok)'}")


def main():
    ap = argparse.ArgumentParser(description="A robot that keeps your desktop icons arranged.")
    ap.add_argument("--demo", action="store_true", help="add 4 sample files at random spots")
    ap.add_argument("--dry-run", action="store_true", help="animate only, never touch anything")
    ap.add_argument("--debug", action="store_true", help="print grid and icon positions, then exit")
    ap.add_argument("--stop", action="store_true", help="stop a running TidyBot")
    ap.add_argument("--pause", action="store_true", help="pause or wake a running TidyBot")
    ap.add_argument("--scale", type=float, help="robot size, e.g. 1.3")
    ap.add_argument("--perf", action="store_true", help="print frame timing every 5 seconds")
    args = ap.parse_args()

    if args.stop or args.pause:
        pid = read_pid()
        if not pid:
            print("TidyBot isn't running.")
            return
        os.kill(pid, signal.SIGTERM if args.stop else signal.SIGUSR1)
        print("Stopped." if args.stop else "Toggled pause.")
        return
    if not HAVE_GTK:
        sys.exit("TidyBot needs GTK 3 for Python:\n"
                 "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0")

    cfg = load_config()
    if args.dry_run:
        cfg["dry_run"] = True
    if args.scale:
        cfg["scale"] = args.scale
    desktop = desktop_dir()

    display = Gdk.Display.get_default()
    mon = display.get_primary_monitor() or display.get_monitor(0)
    wa = mon.get_workarea()
    cs = float(cfg.get("coord_scale", 1.0)) or 1.0
    area = (wa.x / cs, wa.y / cs, wa.width / cs, wa.height / cs)  # desktop coords
    desk = Desk(cfg, desktop, area)

    if args.debug:
        debug_report(desk)
        return
    if read_pid():
        print("TidyBot is already running (use --stop or --pause).")
        return

    restore_held(desktop)
    if args.demo:
        for p in make_demo_files(desk):
            print("demo file:", p)

    os.makedirs(os.path.dirname(PID_PATH), exist_ok=True)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    bot = Bot(cfg, desk, (wa.x, wa.y, wa.width, wa.height))
    Overlay(bot, (wa.x, wa.y, wa.width, wa.height), perf=args.perf)

    def quit_(*_):
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE

    def pause_(*_):
        bot.toggle_pause()
        return GLib.SOURCE_CONTINUE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, quit_)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, quit_)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, pause_)
    try:
        Gtk.main()
    finally:
        restore_held(desktop)   # never leave an icon hidden
        try:
            os.remove(PID_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
