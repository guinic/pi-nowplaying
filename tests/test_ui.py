"""
test_ui.py - Hardware-free tests for the ui engine + btvolume backend.

Runs two ways:
    python3 test_ui.py        # standalone runner (no pytest on the Pi)
    pytest test_ui.py         # collected as test_* functions

Forces the dummy SDL drivers so it works headless over SSH.
"""

import io
import os
import sys
import types

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# Make the app/ package importable (ui/, btvolume.py, npdraw.py live there) and
# app/views (the engine demo view) importable by its bare module name.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "app"))
sys.path.insert(0, os.path.join(_HERE, "..", "app", "views"))

import pygame
pygame.init()

from ui import (WindowManager, Container, VBox, HBox, Spacer,
                Label, Button, ProgressBar, VolumeFader, SolidRect, merge_rects,
                set_screen_size)
import btvolume

# The production render pipeline + the engine demo view that composes through it.
import npdraw
from nowplaying_view import NowPlayingScene, NowPlayingView, build


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
# npdraw: the shared production render pipeline (single source of truth for
# every now-playing pixel). These guard that the demo cannot drift from prod.
# ---------------------------------------------------------------------------
_NP_CACHE = {}


def _np_visuals():
    """Memoised preview visual bundle (cover/shadow/bg/accent) -- build_visuals
    does PIL work, so build it once for the whole suite."""
    if "vis" not in _NP_CACHE:
        _NP_CACHE["vis"] = npdraw.build_visuals(npdraw.preview_cover_bytes())
    return _NP_CACHE["vis"]


def _np_fonts():
    if "fonts" not in _NP_CACHE:
        _NP_CACHE["fonts"] = npdraw.make_fonts()
    return _NP_CACHE["fonts"]


def _np_scene_dict():
    """A fresh scene bundle for compose_now_playing (fresh info copy, shared
    immutable visual surfaces)."""
    vis = _np_visuals()
    return {'cover': vis['cover'], 'shadow': vis['shadow'], 'bg': vis['bg'],
            'accent': vis['accent'], 'info': dict(npdraw.preview_info()),
            'mode': 'airplay', 'connected': True}


def test_npdraw_preview_info_is_frozen_snapshot():
    info = npdraw.preview_info()
    # prog_at falsy => draw_progress skips its time.monotonic live-advance term
    # => the composited frame is fully deterministic (byte-diffable).
    assert info["prog_at"] == 0
    assert info["playing"] is True
    assert info["title"] and info["artist"] and info["album"]
    for k in ("volume", "prog_start", "prog_cur", "prog_end", "hw_rate", "hw_bits"):
        assert k in info


def test_npdraw_preview_cover_is_png():
    cb = npdraw.preview_cover_bytes()
    assert cb[:8] == b"\x89PNG\r\n\x1a\n"            # PNG signature
    surf = pygame.image.load(io.BytesIO(cb))         # pygame can decode it
    assert surf.get_width() > 0 and surf.get_height() > 0


def test_npdraw_build_visuals_bundle():
    vis = _np_visuals()
    assert {"cover", "shadow", "bg", "accent"} <= set(vis)
    assert isinstance(vis["accent"], tuple) and len(vis["accent"]) == 3
    assert all(0 <= c <= 255 for c in vis["accent"])
    assert vis["bg"].get_size() == (npdraw.W, npdraw.H)   # full-screen blur
    assert max(vis["cover"].get_size()) <= npdraw.COVER_MAX


def test_npdraw_make_fonts_has_now_playing_keys():
    fonts = _np_fonts()
    for k in ("title", "artist", "album", "time", "badge", "vol"):
        assert k in fonts
        assert fonts[k].render("x", True, (255, 255, 255)).get_height() > 0


def test_npdraw_compose_is_deterministic():
    fonts = _np_fonts()

    def frame():
        s = pygame.Surface((npdraw.W, npdraw.H))      # opaque, like the framebuffer
        npdraw.compose_now_playing(s, _np_scene_dict(), fonts)
        return pygame.image.tobytes(s, "RGBA")

    assert frame() == frame()                          # no time/animation leak


def test_npdraw_compose_paints_full_frame():
    fonts = _np_fonts()
    s = pygame.Surface((npdraw.W, npdraw.H))
    npdraw.compose_now_playing(s, _np_scene_dict(), fonts)
    assert s.get_at((0, 0))[:3] != (0, 0, 0)          # background blit covers (0,0)


# ---------------------------------------------------------------------------
# Engine demo view: the now-playing scene composed through npdraw, on the
# retained-mode engine. Must stay pixel-identical to a direct prod compose.
# ---------------------------------------------------------------------------
def test_scene_cache_is_opaque():
    # The scene paints every pixel, so its cache must NOT be SRCALPHA: an
    # intermediate alpha layer rounds the alpha-blended button glows (pygame's
    # straight-alpha blit is not associative) and drifts from prod's framebuffer.
    scene = NowPlayingScene(_np_fonts())
    scene.render_cache()
    assert not (scene._cache.get_flags() & pygame.SRCALPHA)


def test_engine_view_matches_direct_compose():
    # The whole point of the rebuild: the engine view path yields the EXACT
    # same pixels as composing straight onto the framebuffer with npdraw.
    fonts = _np_fonts()
    vis = _np_visuals()

    direct = pygame.Surface((npdraw.W, npdraw.H))      # what prod's loop draws onto
    npdraw.compose_now_playing(direct, _np_scene_dict(), fonts)

    screen = _display(npdraw.W, npdraw.H)
    wm, view = build(screen, fonts=fonts)
    snap = dict(npdraw.preview_info())
    snap.update(visuals=vis, mode="airplay", connected=True)
    view.update(snap)
    wm.layout()
    wm.render()

    assert pygame.image.tobytes(screen, "RGBA") == \
           pygame.image.tobytes(direct, "RGBA")


def test_view_overlays_paint_nothing():
    # The invisible touch overlays contribute zero pixels (the scene owns every
    # pixel); their caches are fully transparent.
    view = NowPlayingView(fonts=_np_fonts())
    for ov in (*view.btns, view.toggle, view.fader):
        ov.render_cache()
        assert ov._cache.get_bounding_rect().width == 0   # nothing opaque


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
