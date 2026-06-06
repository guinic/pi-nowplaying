"""
ui.core - Retained-mode UI engine core for the HiFi now-playing display.

Design goals (Raspberry Pi 3A+, software-rendered 800x480 DSI panel):
  * Retained-mode scene graph instead of an immediate-mode procedural loop.
  * Per-widget pixel cache: a widget re-renders its OWN pixels only when its
    `is_dirty` flag is set; otherwise drawing is a cheap blit of the cache.
  * Dirty-RECTANGLE compositor: each frame we repaint ONLY the screen
    sub-rectangles that actually changed (a widget that became dirty, moved, or
    toggled visibility), then `pygame.display.update(rects)` flips just those.
    An idle clock tick repaints a ~300x140 region once/min, not the whole panel.
  * RAM-tight: each widget's cache surface is sized to the widget rect (never
    full-screen); the background is a single shared surface.

This module is HARDWARE-FREE and import-safe headless (SDL_VIDEODRIVER=dummy),
so it can be unit-smoke-tested without a framebuffer.

Public API (see also ui.layout, ui.widgets):
    easing:   linear, ease_in, ease_out, ease_in_out, ease_out_cubic
              lerp, lerp_color, Animator
    base:     Widget, Container
    input:    event_pos
    compositor: merge_rects, WindowManager
"""

import pygame

# ===========================================================================
# Easing / interpolation
# ===========================================================================
# Pure time-normalised easing functions: input t in [0,1] -> output in [0,1].

def linear(t):
    return t

def ease_in(t):                 # quadratic accelerate
    return t * t

def ease_out(t):                # quadratic decelerate
    return 1.0 - (1.0 - t) * (1.0 - t)

def ease_in_out(t):             # smoothstep, smooth both ends
    return t * t * (3.0 - 2.0 * t)

def ease_out_cubic(t):
    u = 1.0 - t
    return 1.0 - u * u * u


def lerp(a, b, t):
    """Scalar linear interpolation, t clamped to [0,1]."""
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    return a + (b - a) * t


def lerp_color(c0, c1, t):
    """Per-channel colour interpolation. Accepts RGB or RGBA tuples; the result
    width matches the longer input (missing alpha treated as 255)."""
    if t <= 0.0:
        return tuple(c0)
    if t >= 1.0:
        return tuple(c1)
    n = max(len(c0), len(c1))
    out = []
    for i in range(n):
        a = c0[i] if i < len(c0) else 255
        b = c1[i] if i < len(c1) else 255
        out.append(int(a + (b - a) * t + 0.5))
    return tuple(out)


class Animator:
    """A single time-based tween. Drive it with `value(now_ms)`; it eases from
    `start` to `end` over `dur_ms` using one of the easing functions above.

    `start`/`end` may be scalars or colour tuples (auto-detected). `done()` is
    True once the duration elapses. Re-arm with `to(new_end, now_ms, dur)` for a
    fresh transition that begins at the current (possibly mid-flight) value.
    """

    __slots__ = ("start", "end", "dur", "easing", "t0", "_is_color")

    def __init__(self, start, dur_ms=300, easing=ease_in_out):
        self.start = start
        self.end = start
        self.dur = max(1, int(dur_ms))
        self.easing = easing
        self.t0 = 0
        self._is_color = isinstance(start, (tuple, list))

    def to(self, end, now_ms, dur_ms=None, easing=None):
        """Begin a new tween toward `end` starting from the CURRENT value."""
        self.start = self.value(now_ms)
        self.end = end
        if dur_ms is not None:
            self.dur = max(1, int(dur_ms))
        if easing is not None:
            self.easing = easing
        self.t0 = now_ms
        self._is_color = isinstance(end, (tuple, list))
        return self

    def value(self, now_ms):
        t = (now_ms - self.t0) / self.dur
        if t <= 0.0:
            return self.start
        if t >= 1.0:
            return self.end
        e = self.easing(t)
        if self._is_color:
            return lerp_color(self.start, self.end, e)
        return lerp(self.start, self.end, e)

    def done(self, now_ms):
        return (now_ms - self.t0) >= self.dur


# ===========================================================================
# Input: normalise pygame touch + mouse events to pixel coordinates
# ===========================================================================
# KMSDRM on the DSI panel can deliver either MOUSEBUTTON* (x,y in pixels) or
# FINGER* (x,y normalised 0..1) events depending on the SDL touch backend.
# `event_pos` returns absolute pixel coords for either family, or None.

_DOWN = frozenset((pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN))
_UP = frozenset((pygame.MOUSEBUTTONUP, pygame.FINGERUP))
_MOVE = frozenset((pygame.MOUSEMOTION, pygame.FINGERMOTION))

# Screen size used to de-normalise FINGER* events. WindowManager sets it from
# the real display; tests can set it directly. Defaults to the common panel.
_SCREEN_SIZE = (800, 480)


def set_screen_size(size):
    global _SCREEN_SIZE
    _SCREEN_SIZE = (int(size[0]), int(size[1]))


def get_screen_size():
    return _SCREEN_SIZE


def event_pos(ev, size=None):
    """Pixel (x, y) for a pointer event, or None if `ev` carries no position.
    `size` is the (w, h) FINGER* normalised coords map onto; defaults to the
    screen size registered by the WindowManager (see `set_screen_size`)."""
    if size is None:
        size = _SCREEN_SIZE
    t = ev.type
    if t in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION):
        return ev.pos
    if t in (pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION):
        return (int(ev.x * size[0]), int(ev.y * size[1]))
    return None


def is_down(ev):
    return ev.type in _DOWN

def is_up(ev):
    return ev.type in _UP

def is_move(ev):
    return ev.type in _MOVE


# ===========================================================================
# Widget base  --  cache lifecycle
# ===========================================================================
class Widget:
    """Base UI node. Owns a Rect and a pixel cache; re-renders the cache only
    when `is_dirty`. Subclasses override `_render(self, surf)` to paint local
    pixels (origin 0,0, size = rect size) and optionally `update(now_ms)` /
    `handle_event(ev)`.
    """

    __slots__ = ("rect", "_cache", "is_dirty", "visible", "_was_visible",
                 "parent", "alpha", "name", "_moved", "_prev_rect")

    def __init__(self, w=0, h=0, name=None):
        self.rect = pygame.Rect(0, 0, int(w), int(h))
        self._prev_rect = pygame.Rect(self.rect)
        self._cache = None
        self.is_dirty = True          # content (pixels) need re-render
        self.visible = True
        self._was_visible = True
        self.parent = None
        self.alpha = 255
        self.name = name or self.__class__.__name__
        self._moved = False           # position changed since last paint

    # ---- geometry --------------------------------------------------------
    def set_pos(self, x, y):
        x, y = int(x), int(y)
        if (x, y) != (self.rect.x, self.rect.y):
            self.rect.x, self.rect.y = x, y
            self._moved = True          # cache pixels still valid, only position

    def resize(self, w, h):
        w, h = int(w), int(h)
        if (w, h) != (self.rect.w, self.rect.h):
            self.rect.w, self.rect.h = w, h
            self._cache = None
            self.is_dirty = True

    def set_visible(self, on):
        self.visible = bool(on)

    def set_alpha(self, a):
        a = max(0, min(255, int(a)))
        if a != self.alpha:
            self.alpha = a
            self.is_dirty = True        # cheap; forces a re-blit at new alpha

    # ---- dirty / cache ---------------------------------------------------
    def mark_dirty(self):
        self.is_dirty = True

    def _ensure_cache(self):
        w = max(1, self.rect.w)
        h = max(1, self.rect.h)
        if self._cache is None or self._cache.get_size() != (w, h):
            self._cache = pygame.Surface((w, h), pygame.SRCALPHA)
            self.is_dirty = True

    def render_cache(self):
        """Re-render local pixels into the cache iff dirty."""
        self._ensure_cache()
        if self.is_dirty:
            self._cache.fill((0, 0, 0, 0))
            self._render(self._cache)
            self.is_dirty = False

    def _render(self, surf):
        """Override: paint widget pixels into `surf` (origin 0,0)."""
        pass

    def blit_to(self, target):
        """Blit the (cached) pixels onto `target` at the widget's position.
        Honours per-widget alpha. Assumes `target` clip is already set by the
        compositor for dirty-rect clipping."""
        if not self.visible:
            return
        self.render_cache()
        if self.alpha >= 255:
            target.blit(self._cache, self.rect.topleft)
        else:
            tmp = self._cache.copy()
            tmp.set_alpha(self.alpha)
            target.blit(tmp, self.rect.topleft)

    # ---- per-frame + input ----------------------------------------------
    def update(self, now_ms):
        """Override for time-based animation. Return True if pixels changed
        (the manager will mark this widget dirty)."""
        return False

    def handle_event(self, ev):
        """Override for touch/mouse. Return True to CONSUME the event (stops
        propagation to widgets behind this one)."""
        return False

    def contains(self, x, y):
        return self.visible and self.rect.collidepoint(x, y)

    # ---- tree ------------------------------------------------------------
    def walk(self):
        """Yield self then descendants in paint order (parent before children =
        children drawn on top)."""
        yield self


# ===========================================================================
# Container  --  layout + event routing
# ===========================================================================
class Container(Widget):
    """Holds child widgets and routes layout + events. A container does NOT
    composite its children into its own cache (that would defeat per-widget
    dirty tracking); children blit independently and the WindowManager handles
    z-order + damage. Subclasses (VBox/HBox) implement `layout()` positioning.
    """

    __slots__ = ("children", "pad_l", "pad_t", "pad_r", "pad_b", "spacing",
                 "align")

    def __init__(self, w=0, h=0, padding=0, spacing=0, align="start", name=None):
        super().__init__(w, h, name=name)
        self.children = []
        if isinstance(padding, (tuple, list)):
            self.pad_l, self.pad_t, self.pad_r, self.pad_b = (list(padding) + [0, 0, 0, 0])[:4]
        else:
            self.pad_l = self.pad_t = self.pad_r = self.pad_b = int(padding)
        self.spacing = int(spacing)
        self.align = align            # cross-axis: 'start' | 'center' | 'end'

    def add(self, *widgets):
        for wdg in widgets:
            wdg.parent = self
            self.children.append(wdg)
        return self

    def remove(self, wdg):
        if wdg in self.children:
            wdg.parent = None
            self.children.remove(wdg)
        return self

    def clear(self):
        for c in self.children:
            c.parent = None
        self.children = []

    def content_box(self):
        return pygame.Rect(
            self.rect.x + self.pad_l,
            self.rect.y + self.pad_t,
            max(0, self.rect.w - self.pad_l - self.pad_r),
            max(0, self.rect.h - self.pad_t - self.pad_b),
        )

    def layout(self):
        """Base: containers with explicit child positions. Override in VBox/HBox.
        Always recurse so nested containers re-flow."""
        for c in self.children:
            if isinstance(c, Container):
                c.layout()

    def handle_event(self, ev):
        # Descending propagation: top-most child first (last added is visually
        # on top). The first child to CONSUME the event stops the walk.
        for c in reversed(self.children):
            if c.visible and c.handle_event(ev):
                return True
        return False

    # A container is structural: it owns no pixels, so it never allocates a
    # cache nor paints (this saves a full-screen SRCALPHA surface for the root).
    def render_cache(self):
        pass

    def blit_to(self, target):
        pass

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


# ===========================================================================
# Dirty-rectangle compositor + frame loop
# ===========================================================================
def merge_rects(rects, gap=8):
    """Coalesce a list of Rects into fewer non-trivially-overlapping clusters.
    Two rects merge if they intersect (inflated by `gap`), reducing the number
    of separate `display.update` regions and clip passes. O(n^2) but n is tiny.
    """
    clusters = []
    for r in rects:
        r = pygame.Rect(r)
        merged = False
        for i, c in enumerate(clusters):
            if c.inflate(gap, gap).colliderect(r):
                clusters[i] = c.union(r)
                merged = True
                break
        if not merged:
            clusters.append(r)
    # second pass: clusters may now overlap each other
    changed = True
    while changed:
        changed = False
        out = []
        for r in clusters:
            placed = False
            for i, c in enumerate(out):
                if c.colliderect(r):
                    out[i] = c.union(r)
                    placed = True
                    changed = True
                    break
            if not placed:
                out.append(r)
        clusters = out
    return clusters


class WindowManager:
    """Owns the screen, the background surface, and the root container. Each
    frame: dispatch events, run per-widget `update`, then composite only the
    damaged regions and flip them.

    Set `background` to a full-screen Surface; assigning a *different* surface
    forces a full repaint (used for album-art / time-of-day gradient changes).
    """

    def __init__(self, screen):
        self.screen = screen
        self.size = screen.get_size()
        set_screen_size(self.size)    # so leaf widgets can de-normalise touches
        self.root = None
        self.background = None
        self._bg_dirty = True
        self._force_full = True

    # ---- tree / background ----------------------------------------------
    def set_root(self, container):
        self.root = container
        if container is not None:
            container.parent = self
            container.rect.size = self.size
        self._force_full = True

    def set_background(self, surf):
        if surf is not self.background:
            self.background = surf
            self._bg_dirty = True

    def request_full_repaint(self):
        self._force_full = True

    def layout(self):
        if self.root is not None:
            self.root.rect.size = self.size
            self.root.layout()

    def _paintable(self):
        """Widgets in z (paint) order. Containers render nothing themselves but
        stay in the walk for structure."""
        if self.root is None:
            return []
        return list(self.root.walk())

    # ---- input -----------------------------------------------------------
    def dispatch(self, ev):
        """Route one event to the tree, top-most widget first. Returns True if
        consumed."""
        if self.root is None:
            return False
        return self.root.handle_event(ev)

    # ---- per-frame -------------------------------------------------------
    def update(self, now_ms):
        for w in self._paintable():
            if w.visible and w.update(now_ms):
                w.mark_dirty()

    def _full_repaint(self, widgets):
        if self.background is not None:
            self.screen.blit(self.background, (0, 0))
        else:
            self.screen.fill((0, 0, 0))
        for w in widgets:
            if w.visible:
                w.blit_to(self.screen)
            w._moved = False
            w._was_visible = w.visible
            w._prev_rect = pygame.Rect(w.rect)
        self._bg_dirty = False
        self._force_full = False
        pygame.display.flip()
        return [pygame.Rect(0, 0, *self.size)]

    def render(self):
        """Composite the frame. Returns the list of screen rects updated (empty
        if nothing changed)."""
        widgets = self._paintable()

        if self._force_full or self._bg_dirty:
            return self._full_repaint(widgets)

        # 1) Collect damage from content-dirty, moved, or visibility-toggled.
        #    Containers own no pixels, so they never contribute damage.
        damage = []
        for w in widgets:
            if isinstance(w, Container):
                continue
            vis_changed = (w.visible != w._was_visible)
            if w.visible and (w.is_dirty or w._moved):
                if w._moved and w._prev_rect != w.rect:
                    damage.append(pygame.Rect(w._prev_rect))
                damage.append(pygame.Rect(w.rect))
            elif vis_changed:
                # appeared or disappeared -> damage its footprint
                damage.append(pygame.Rect(w._prev_rect if not w.visible else w.rect))

        if not damage:
            return []

        # 2) Merge into a few regions; clip to the screen rect.
        screen_rect = self.screen.get_rect()
        regions = []
        for r in merge_rects(damage):
            r = r.clip(screen_rect)
            if r.w > 0 and r.h > 0:
                regions.append(r)
        if not regions:
            return []

        # 3) Repaint each region: background slice, then every widget that
        #    intersects it, in z-order, all clipped to the region.
        for r in regions:
            self.screen.set_clip(r)
            if self.background is not None:
                self.screen.blit(self.background, (0, 0))
            else:
                self.screen.fill((0, 0, 0))
            for w in widgets:
                if w.visible and w.rect.colliderect(r):
                    w.blit_to(self.screen)
        self.screen.set_clip(None)

        # 4) Commit bookkeeping for next frame.
        for w in widgets:
            w.is_dirty = False
            w._moved = False
            w._was_visible = w.visible
            w._prev_rect = pygame.Rect(w.rect)

        pygame.display.update(regions)
        return regions
