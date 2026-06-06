"""
test_ui.py - Hardware-free tests for the ui engine + btvolume backend.

Runs two ways:
    python3 test_ui.py        # standalone runner (no pytest on the Pi)
    pytest test_ui.py         # collected as test_* functions

Forces the dummy SDL drivers so it works headless over SSH.
"""

import os
import sys
import types

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# Make the app/ package importable (ui/, btvolume.py live there).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "app"))

import pygame
pygame.init()

from ui import (WindowManager, Container, VBox, HBox, Spacer,
                Label, Button, ProgressBar, VolumeFader, SolidRect, merge_rects,
                set_screen_size)
import btvolume


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _screen(w=800, h=480):
    set_screen_size((w, h))
    return pygame.Surface((w, h))


def _display(w=800, h=480):
    """A real display surface (the compositor calls display.flip/update, which
    require a set video mode). Works under SDL_VIDEODRIVER=dummy."""
    set_screen_size((w, h))
    return pygame.display.set_mode((w, h))


def _md(ev_type, x, y, button=1):
    return pygame.event.Event(ev_type, pos=(x, y), button=button)


# ---------------------------------------------------------------------------
# Widget cache lifecycle
# ---------------------------------------------------------------------------
def test_widget_cache_clears_dirty():
    r = SolidRect(40, 20, (255, 0, 0))
    assert r.is_dirty is True
    r.render_cache()
    assert r.is_dirty is False
    assert r._cache is not None and r._cache.get_size() == (40, 20)


def test_setters_mark_dirty_only_on_change():
    r = SolidRect(40, 20, (255, 0, 0))
    r.render_cache()
    r.set_color((255, 0, 0))            # same colour
    assert r.is_dirty is False
    r.set_color((0, 255, 0))            # changed
    assert r.is_dirty is True


def test_label_autosizes_and_caches():
    font = pygame.font.SysFont("dejavusans", 24)
    lbl = Label(font, "Hello")
    assert lbl.rect.w > 0 and lbl.rect.h > 0
    w0 = lbl.rect.w
    lbl.render_cache()
    assert lbl.is_dirty is False
    lbl.set_text("Hello world, longer")
    assert lbl.is_dirty is True
    assert lbl.rect.w > w0               # grew to fit


def test_resize_invalidates_cache():
    r = SolidRect(40, 20, (255, 0, 0))
    r.render_cache()
    r.resize(80, 20)
    assert r._cache is None and r.is_dirty is True


# ---------------------------------------------------------------------------
# Layout: VBox / HBox / Spacer
# ---------------------------------------------------------------------------
def test_vbox_stacks_with_spacing():
    box = VBox(200, 300, padding=10, spacing=5)
    a = SolidRect(50, 30, (1, 1, 1))
    b = SolidRect(50, 40, (2, 2, 2))
    box.add(a, b)
    box.set_pos(0, 0)
    box.layout()
    assert (a.rect.x, a.rect.y) == (10, 10)          # padding
    assert (b.rect.x, b.rect.y) == (10, 10 + 30 + 5) # below a + spacing


def test_hbox_aligns_center_cross_axis():
    box = HBox(300, 100, align="center")
    a = SolidRect(40, 20, (1, 1, 1))
    box.add(a)
    box.set_pos(0, 0)
    box.layout()
    assert a.rect.y == (100 - 20) // 2               # vertically centred


def test_spacer_flex_distributes_leftover():
    box = HBox(300, 50)
    a = SolidRect(40, 20, (1, 1, 1))
    b = SolidRect(60, 20, (2, 2, 2))
    box.add(a, Spacer(), b)
    box.set_pos(0, 0)
    box.layout()
    # a at x=0; b pushed to the right edge by the flexible spacer
    assert a.rect.x == 0
    assert b.rect.right == 300


# ---------------------------------------------------------------------------
# Event routing + Button
# ---------------------------------------------------------------------------
def test_button_press_release_inside_fires():
    fired = []
    btn = Button(100, 50, on_press=lambda b: fired.append(b))
    btn.set_pos(10, 10)
    root = Container(800, 480)
    root.add(btn)
    assert root.handle_event(_md(pygame.MOUSEBUTTONDOWN, 30, 30)) is True
    assert btn._pressed is True
    assert root.handle_event(_md(pygame.MOUSEBUTTONUP, 30, 30)) is True
    assert fired == [btn]


def test_button_release_outside_cancels():
    fired = []
    btn = Button(100, 50, on_press=lambda b: fired.append(b))
    btn.set_pos(10, 10)
    btn.handle_event(_md(pygame.MOUSEBUTTONDOWN, 30, 30))
    btn.handle_event(_md(pygame.MOUSEBUTTONUP, 500, 500))  # slid off
    assert fired == []
    assert btn._pressed is False


def test_event_routes_to_topmost_child():
    hits = []
    lower = Button(100, 100, on_press=lambda b: hits.append("lower"))
    upper = Button(100, 100, on_press=lambda b: hits.append("upper"))
    lower.set_pos(0, 0)
    upper.set_pos(0, 0)              # exactly overlapping
    root = Container(200, 200)
    root.add(lower, upper)           # upper added last => on top
    root.handle_event(_md(pygame.MOUSEBUTTONDOWN, 50, 50))
    root.handle_event(_md(pygame.MOUSEBUTTONUP, 50, 50))
    assert hits == ["upper"]         # only the top-most consumed it


def test_miss_outside_does_not_consume():
    btn = Button(40, 40)
    btn.set_pos(0, 0)
    root = Container(800, 480)
    root.add(btn)
    assert root.handle_event(_md(pygame.MOUSEBUTTONDOWN, 700, 400)) is False


# ---------------------------------------------------------------------------
# VolumeFader
# ---------------------------------------------------------------------------
def test_fader_maps_drag_to_value():
    seen = []
    fader = VolumeFader(56, 300, value=0.5, on_change=lambda v: seen.append(v), pad=0)
    fader.set_pos(0, 0)
    # tap at the very top -> 1.0, very bottom -> 0.0
    fader.handle_event(_md(pygame.MOUSEBUTTONDOWN, 28, 0))
    assert abs(fader.value - 1.0) < 1e-6
    fader.handle_event(pygame.event.Event(pygame.MOUSEMOTION, pos=(28, 300),
                                          rel=(0, 300), buttons=(1, 0, 0)))
    assert abs(fader.value - 0.0) < 1e-6
    fader.handle_event(_md(pygame.MOUSEBUTTONUP, 28, 150))
    assert abs(fader.value - 0.5) < 0.02
    assert seen and all(0.0 <= v <= 1.0 for v in seen)


# ---------------------------------------------------------------------------
# Compositor
# ---------------------------------------------------------------------------
def test_merge_rects_coalesces_neighbours():
    rects = [pygame.Rect(0, 0, 10, 10), pygame.Rect(5, 5, 10, 10),
             pygame.Rect(500, 400, 20, 20)]
    merged = merge_rects(rects, gap=2)
    assert len(merged) == 2


def test_window_manager_damage_cycle():
    scr = _display()
    wm = WindowManager(scr)
    rect = SolidRect(50, 50, (255, 255, 255))
    rect.set_pos(100, 100)
    root = Container(800, 480)
    root.add(rect)
    wm.set_root(root)
    wm.layout()
    first = wm.render()                       # forced full repaint
    assert first and first[0].size == (800, 480)
    assert wm.render() == []                  # nothing changed -> no damage
    rect.set_color((255, 0, 0))               # dirty one widget
    dmg = wm.render()
    assert len(dmg) == 1 and dmg[0].colliderect(rect.rect)


# ---------------------------------------------------------------------------
# btvolume: mapping + backends
# ---------------------------------------------------------------------------
def test_avrcp_mapping_roundtrip():
    assert btvolume.frac_to_avrcp(0.0) == 0
    assert btvolume.frac_to_avrcp(1.0) == 127
    assert btvolume.frac_to_avrcp(0.5) == 64
    assert abs(btvolume.avrcp_to_frac(127) - 1.0) < 1e-9
    assert btvolume.avrcp_to_frac("bad") is None


class _FakeBackend:
    """Mimics the BlueZ D-Bus backend with two transports."""
    def __init__(self, with_volume=True, active=True):
        self.set_calls = []
        self._objs = {}
        if with_volume:
            self._objs["/t/inactive"] = {
                btvolume.TRANSPORT_IFACE: {"State": "idle", "Volume": 40}}
            self._objs["/t/active"] = {
                btvolume.TRANSPORT_IFACE: {
                    "State": "active" if active else "idle", "Volume": 64}}
        else:
            self._objs["/t/novol"] = {
                btvolume.TRANSPORT_IFACE: {"State": "active"}}

    def managed_objects(self):
        return self._objs

    def get_property(self, path, iface, prop):
        return self._objs.get(path, {}).get(iface, {}).get(prop)

    def set_property(self, path, iface, prop, value):
        self.set_calls.append((path, prop, int(value)))
        self._objs[path][iface][prop] = int(value)
        return True


def test_btvolume_prefers_active_transport():
    be = _FakeBackend(active=True)
    bt = btvolume.BtVolume(backend=be)
    assert bt.find_transport() == "/t/active"
    assert bt.set_percent(100) is True
    assert be.set_calls[-1] == ("/t/active", "Volume", 127)


def test_btvolume_get_reads_active():
    bt = btvolume.BtVolume(backend=_FakeBackend(active=True))
    assert abs(bt.get() - 64 / 127) < 1e-9


def test_btvolume_softvol_fallback():
    # No AVRCP transport at all -> must use the bluealsactl soft-volume path.
    be = _FakeBackend(with_volume=False)
    # strip the transport so find_transport() returns "" (no Volume key)
    cmds = []

    def runner(argv):
        cmds.append(argv)
        out = "/org/bluealsa/hci0/dev_AA/a2dpsnk running" if argv[-1] == "list-pcms" else ""
        return types.SimpleNamespace(stdout=out, returncode=0)

    bt = btvolume.BtVolume(backend=be, runner=runner)
    bt._bluealsactl = lambda: "bluealsactl"      # pretend the CLI exists
    assert bt.set(0.5) is True
    # it discovered the PCM and issued a volume set of 64
    assert any(c[:2] == ["bluealsactl", "volume"] and c[-1] == "64" for c in cmds)


# ---------------------------------------------------------------------------
# standalone runner
# ---------------------------------------------------------------------------
def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"  ok   {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{passed + failed} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
