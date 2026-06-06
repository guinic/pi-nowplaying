"""
views.nowplaying_view - The now-playing screen, on the retained-mode ui engine,
rendered through the *production* draw pipeline so the demo is pixel-identical to
the live app.

How it stays faithful to prod
-----------------------------
The visuals are NOT re-implemented here. ``NowPlayingScene`` is a single cached
engine Widget whose ``_render`` delegates to ``npdraw.compose_now_playing`` --
the exact sequence of blits the live render loop performs, on one surface. So the
pixels come straight from production's code; this module only owns the *engine
wiring* around them:

  * Retained-mode cache: the scene re-renders its pixels only when data actually
    changes (``set_info`` / ``set_visuals`` mark it dirty); a static now-playing
    frame costs one blit per damaged region, not a full redraw.
  * Event routing: invisible ``TouchRegion`` overlays sit over the transport
    buttons, the mode-toggle chip and the volume fader. They paint nothing (the
    visuals are the scene's), they only translate touches into callbacks -- so
    the engine's descending event contract is demonstrated end to end. Dragging
    the fader mutates the scene's volume and marks it dirty, and npdraw redraws
    the fader at the new level: touch -> state -> retained re-render.

Bind ``on_volume`` to ``btvolume.BtVolume.set`` (Bluetooth AVRCP) or AirPlay
``SetVolume`` -- never the muted ALSA card (CHANTIER 1.4).
"""

import pygame

# The engine.
from ui import Widget, Container, WindowManager
from ui.core import event_pos, is_down, is_up, is_move

# The production render pipeline (single source of truth for every pixel).
import npdraw
from npconfig import (W, H, PAD, CONT_H, BTN_R, BTN_Y, BTN_X, BTN_KEYS,
                      MODE_TOGGLE, VOL_FADER, VOL_HIT_X, VOL_HIT_PAD)


# ===========================================================================
# The full now-playing scene as one cached widget (composited via npdraw)
# ===========================================================================
class NowPlayingScene(Widget):
    """Full-screen cached widget. Its cache holds an entire production frame,
    composed by ``npdraw.compose_now_playing``. Re-renders only when the track
    data, visuals, mode, volume or transient flags change."""

    __slots__ = ("fonts", "scene", "opts")

    def __init__(self, fonts, size=(W, H), name="scene"):
        super().__init__(size[0], size[1], name=name)
        self.fonts = fonts
        # The data bundle compose_now_playing consumes.
        self.scene = {
            'cover': None, 'shadow': None, 'bg': None,
            'accent': (88, 166, 255),
            'info': npdraw.preview_info(),
            'mode': 'airplay', 'connected': True,
        }
        # Transient render flags (press feedback, fader-active readout, night).
        self.opts = {'vactive': False, 'toggle_pressed': False, 'night': False,
                     'eq_animate': False}

    # ---- data push (each marks the cache dirty only on real change) ------
    def set_visuals(self, bundle):
        if bundle:
            self.scene.update({k: bundle[k] for k in
                               ('cover', 'shadow', 'bg', 'accent') if k in bundle})
            self.is_dirty = True

    def set_info(self, **kw):
        info = self.scene['info']
        changed = any(info.get(k) != v for k, v in kw.items())
        if changed:
            info.update(kw)
            self.is_dirty = True

    def set_field(self, key, value):
        if self.scene.get(key) != value:
            self.scene[key] = value
            self.is_dirty = True

    def set_opt(self, key, value):
        if self.opts.get(key) != value:
            self.opts[key] = value
            self.is_dirty = True

    # ---- opaque cache (byte-exact with prod's framebuffer) ---------------
    def _ensure_cache(self):
        """Allocate the scene cache as an *opaque* Surface, not the engine's
        default SRCALPHA. The scene paints every pixel (the bg blit covers the
        whole frame), so it never needs per-pixel alpha -- and crucially,
        pygame's straight-alpha blit is not associative across an intermediate
        SRCALPHA layer: the alpha-blended button glows would round by a few LSB
        when composited onto SRCALPHA and then blitted to the opaque screen.
        Compositing straight onto an opaque cache matches prod's framebuffer
        exactly (verified: 0 differing pixels)."""
        w = max(1, self.rect.w)
        h = max(1, self.rect.h)
        if self._cache is None or self._cache.get_size() != (w, h) \
                or self._cache.get_flags() & pygame.SRCALPHA:
            self._cache = pygame.Surface((w, h))   # opaque: no SRCALPHA
            self.is_dirty = True

    def _render(self, surf):
        npdraw.compose_now_playing(surf, self.scene, self.fonts, **self.opts)


# ===========================================================================
# Invisible touch overlays (events only -- never paint)
# ===========================================================================
class TouchRegion(Widget):
    """A hit rectangle that consumes a press/release inside it and fires a tap
    callback, but paints nothing (the scene under it owns every pixel). Implements
    the same press-tracking contract as the engine Button: a release that slides
    off cancels."""

    __slots__ = ("on_tap", "_pressed")

    def __init__(self, w, h, on_tap=None, name=None):
        super().__init__(w, h, name=name)
        self.on_tap = on_tap
        self._pressed = False

    # paints nothing: keep the cache transparent so a blit is a no-op
    def _render(self, surf):
        pass

    def handle_event(self, ev):
        pos = event_pos(ev)
        if pos is None:
            return False
        if is_down(ev) and self.contains(*pos):
            self._pressed = True
            return True
        if is_up(ev) and self._pressed:
            inside = self.contains(*pos)
            self._pressed = False
            if inside and self.on_tap is not None:
                self.on_tap()
            return True
        return False


class FaderRegion(Widget):
    """Generous vertical drag zone over the right-edge fader. Maps the pointer y
    to a 0..100 volume (prod's ``y_to_vol`` mapping) and reports it live via
    ``on_volume``; paints nothing."""

    __slots__ = ("on_volume", "_dragging")

    def __init__(self, on_volume=None, name="fader_hit"):
        # Cover the whole generous hit band: from the toggle's bottom to the
        # fader bottom + slop, across the right strip (VOL_HIT_X..W).
        top = max(VOL_FADER.top - VOL_HIT_PAD, 108)
        bottom = VOL_FADER.bottom + VOL_HIT_PAD
        super().__init__(W - VOL_HIT_X, bottom - top, name=name)
        self.set_pos(VOL_HIT_X, top)
        self.on_volume = on_volume
        self._dragging = False

    def _render(self, surf):
        pass

    def _vol_from_y(self, py):
        frac = (VOL_FADER.bottom - py) / VOL_FADER.h
        return int(round(max(0.0, min(1.0, frac)) * 100))

    def handle_event(self, ev):
        pos = event_pos(ev)
        if pos is None:
            return False
        if is_down(ev) and self.contains(*pos):
            self._dragging = True
            if self.on_volume:
                self.on_volume(self._vol_from_y(pos[1]))
            return True
        if is_move(ev) and self._dragging:
            if self.on_volume:
                self.on_volume(self._vol_from_y(pos[1]))
            return True
        if is_up(ev) and self._dragging:
            self._dragging = False
            if self.on_volume:
                self.on_volume(self._vol_from_y(pos[1]))
            return True
        return False


# ===========================================================================
# The view: scene + overlays, with a clean data-push API
# ===========================================================================
class NowPlayingView:
    """Builds the scene graph (``root``) and exposes ``update`` / ``relayout``.

    on_transport(action)   action in {'prev', 'play', 'next'}
    on_mode_toggle()       user tapped the source chip
    on_volume(frac)        frac in [0, 1] from the fader
    """

    def __init__(self, size=(W, H), fonts=None, on_transport=None,
                 on_mode_toggle=None, on_volume=None):
        self.size = size
        self.on_transport = on_transport
        self.on_mode_toggle = on_mode_toggle
        self.on_volume = on_volume
        self.fonts = fonts or npdraw.make_fonts()

        self.scene = NowPlayingScene(self.fonts, size=size)

        # --- invisible interactive overlays (events only) ----------------
        # Transport taps: square hit boxes centred on each button.
        hit = 2 * BTN_R + 16
        self.btns = []
        for key, cx in zip(BTN_KEYS, BTN_X):
            b = TouchRegion(hit, hit, on_tap=(lambda k=key: self._transport(k)),
                            name=f"hit_{key}")
            b.set_pos(cx - hit // 2, BTN_Y - hit // 2)
            self.btns.append(b)

        # Mode-toggle chip.
        self.toggle = TouchRegion(MODE_TOGGLE.w, MODE_TOGGLE.h,
                                  on_tap=self._toggle, name="hit_toggle")
        self.toggle.set_pos(MODE_TOGGLE.x, MODE_TOGGLE.y)

        # Volume fader drag band.
        self.fader = FaderRegion(on_volume=self._volume)

        self.root = Container(*size, name="root")
        # Scene first (painted), overlays last (on top for event routing).
        self.root.add(self.scene, *self.btns, self.toggle, self.fader)

    # ---- interaction -> state -> retained re-render ---------------------
    def _transport(self, key):
        if self.on_transport:
            self.on_transport(key)

    def _toggle(self):
        if self.on_mode_toggle:
            self.on_mode_toggle()

    def _volume(self, pct):
        # Mutate the scene's volume and let npdraw redraw the fader (active +
        # % readout) on the next composite.
        self.scene.set_info(volume=int(pct))
        self.scene.set_opt('vactive', True)
        if self.on_volume:
            self.on_volume(max(0, min(100, int(pct))) / 100.0)

    # ---- geometry --------------------------------------------------------
    def relayout(self):
        self.scene.set_pos(0, 0)
        self.scene.resize(*self.size)

    # ---- data push -------------------------------------------------------
    def set_visuals(self, bundle):
        """Feed an ``npdraw.build_visuals`` bundle (cover/shadow/bg/accent)."""
        self.scene.set_visuals(bundle)

    def update(self, snap):
        """Push a now-playing snapshot. Recognised keys: title, artist, album,
        playing, volume, prog_start, prog_cur, prog_end, prog_at, hw_active,
        hw_rate, hw_bits, bt_codec, client_ip, dacp_port, active_remote, plus
        mode / connected (scene-level) and a visuals bundle under 'visuals'."""
        if 'visuals' in snap and snap['visuals']:
            self.scene.set_visuals(snap['visuals'])
        for top in ('mode', 'connected'):
            if top in snap:
                self.scene.set_field(top, snap[top])
        info_keys = ('title', 'artist', 'album', 'playing', 'volume',
                     'prog_start', 'prog_cur', 'prog_end', 'prog_at',
                     'hw_active', 'hw_rate', 'hw_bits', 'bt_codec',
                     'client_ip', 'dacp_port', 'active_remote')
        info_upd = {k: snap[k] for k in info_keys if k in snap}
        if info_upd:
            self.scene.set_info(**info_upd)


def build(screen, on_transport=None, on_mode_toggle=None, on_volume=None,
          fonts=None):
    """Convenience: create a NowPlayingView + a WindowManager bound to it.
    Returns (wm, view)."""
    wm = WindowManager(screen)
    view = NowPlayingView(screen.get_size(), fonts=fonts,
                          on_transport=on_transport,
                          on_mode_toggle=on_mode_toggle, on_volume=on_volume)
    wm.set_root(view.root)
    wm.layout()
    return wm, view
