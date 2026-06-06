"""
ui.widgets - Concrete content + interactive widgets.

All of these re-render their pixel cache only when their data changes
(`set_text`, `set_value`, press state...), so a static now-playing screen
costs one blit per widget per damaged frame -- no per-frame redraw.

  SolidRect    filled / rounded rectangle primitive
  Label        cached anti-aliased text (auto-sizing or fixed+aligned)
  Icon         a Surface scaled to the widget rect (album art, glyphs)
  Button       Icon/Label + rounded background, press states, on_press callback
  ProgressBar  horizontal track + fill, value in [0, 1]
  VolumeFader  draggable vertical fader, emits value in [0, 1] via on_change
"""

import pygame
from .core import Widget, event_pos, is_down, is_up, is_move


class SolidRect(Widget):
    """Generic filled (optionally rounded) rectangle."""

    __slots__ = ("color", "radius")

    def __init__(self, w, h, color, radius=0, name=None):
        super().__init__(w, h, name=name)
        self.color = color
        self.radius = int(radius)

    def set_color(self, color):
        if tuple(color) != tuple(self.color):
            self.color = color
            self.is_dirty = True

    def _render(self, surf):
        pygame.draw.rect(surf, self.color, surf.get_rect(),
                         border_radius=self.radius)


class Label(Widget):
    """Cached text. By default the widget rect auto-fits the rendered text; pass
    a fixed `w`/`h` plus `halign`/`valign` to place text within a fixed box."""

    __slots__ = ("font", "text", "color", "halign", "valign", "_fit", "_text_surf")

    def __init__(self, font, text="", color=(255, 255, 255),
                 w=None, h=None, halign="left", valign="middle", name=None):
        self.font = font
        self.text = text
        self.color = color
        self.halign = halign
        self.valign = valign
        self._fit = (w is None or h is None)
        self._text_surf = None
        super().__init__(w or 0, h or 0, name=name)
        self._refit()

    def set_text(self, text):
        text = "" if text is None else str(text)
        if text != self.text:
            self.text = text
            self._refit()
            self.is_dirty = True

    def set_color(self, color):
        if tuple(color) != tuple(self.color):
            self.color = color
            self._text_surf = None
            self.is_dirty = True

    def _refit(self):
        if self.font is None:
            return
        self._text_surf = self.font.render(self.text, True, self.color)
        if self._fit:
            self.resize(*self._text_surf.get_size())

    def _render(self, surf):
        if self.font is None:
            return
        if self._text_surf is None:
            self._text_surf = self.font.render(self.text, True, self.color)
        tw, th = self._text_surf.get_size()
        if self.halign == "center":
            x = (surf.get_width() - tw) // 2
        elif self.halign == "right":
            x = surf.get_width() - tw
        else:
            x = 0
        if self.valign == "middle":
            y = (surf.get_height() - th) // 2
        elif self.valign == "bottom":
            y = surf.get_height() - th
        else:
            y = 0
        surf.blit(self._text_surf, (x, y))


class Icon(Widget):
    """A Surface (album art, glyph) scaled to the widget rect with a cached
    smoothscale. Reassigning the source via `set_surface` re-scales once."""

    __slots__ = ("_src", "radius", "_scaled")

    def __init__(self, surface=None, w=0, h=0, radius=0, name=None):
        super().__init__(w, h, name=name)
        self._src = surface
        self.radius = int(radius)
        self._scaled = None

    def set_surface(self, surface):
        if surface is not self._src:
            self._src = surface
            self._scaled = None
            self.is_dirty = True

    def resize(self, w, h):
        prev = (self.rect.w, self.rect.h)
        super().resize(w, h)
        if (self.rect.w, self.rect.h) != prev:
            self._scaled = None

    def _render(self, surf):
        if self._src is None:
            return
        if self._scaled is None or self._scaled.get_size() != surf.get_size():
            self._scaled = pygame.transform.smoothscale(self._src, surf.get_size())
        if self.radius > 0:
            mask = pygame.Surface(surf.get_size(), pygame.SRCALPHA)
            pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(),
                             border_radius=self.radius)
            tmp = self._scaled.copy()
            tmp.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            surf.blit(tmp, (0, 0))
        else:
            surf.blit(self._scaled, (0, 0))


class Button(Widget):
    """Tappable button: rounded background + centred Label and/or Icon, with a
    pressed visual state and an `on_press` callback fired on release-inside.

    Event handling implements the descending touch contract: it consumes a
    DOWN that lands inside it (collidepoint), tracks the press, and fires
    `on_press` only if the matching UP also lands inside -- so a finger that
    slides off cancels, like a native button.
    """

    __slots__ = ("label", "icon", "bg", "bg_pressed", "radius", "on_press",
                 "_pressed", "_pad")

    def __init__(self, w, h, on_press=None, label=None, icon=None,
                 bg=(40, 40, 48), bg_pressed=(70, 70, 84), radius=12,
                 pad=10, name=None):
        super().__init__(w, h, name=name)
        self.label = label            # a Label or None
        self.icon = icon              # an Icon/Surface or None
        self.bg = bg
        self.bg_pressed = bg_pressed
        self.radius = int(radius)
        self.on_press = on_press
        self._pressed = False
        self._pad = int(pad)

    def set_label_text(self, text):
        if self.label is not None:
            self.label.set_text(text)
            self.is_dirty = True

    def set_icon(self, surface):
        if surface is not self.icon:
            self.icon = surface
            self.is_dirty = True

    def _render(self, surf):
        col = self.bg_pressed if self._pressed else self.bg
        if col is not None:
            pygame.draw.rect(surf, col, surf.get_rect(), border_radius=self.radius)
        # Centre the icon (above) and/or label within the button box.
        cx, cy = surf.get_width() // 2, surf.get_height() // 2
        if self.icon is not None:
            ico = self.icon
            iw = min(ico.get_width(), surf.get_width() - 2 * self._pad)
            ih = min(ico.get_height(), surf.get_height() - 2 * self._pad)
            scaled = pygame.transform.smoothscale(ico, (iw, ih)) if (iw, ih) != ico.get_size() else ico
            surf.blit(scaled, (cx - iw // 2, cy - ih // 2))
        if self.label is not None and self.label.text:
            ts = self.label._text_surf or self.label.font.render(
                self.label.text, True, self.label.color)
            surf.blit(ts, (cx - ts.get_width() // 2, cy - ts.get_height() // 2))

    def handle_event(self, ev):
        pos = event_pos(ev)
        if pos is None:
            return False
        if is_down(ev) and self.contains(*pos):
            if not self._pressed:
                self._pressed = True
                self.is_dirty = True
            return True               # consume: this press is ours
        if is_up(ev) and self._pressed:
            inside = self.contains(*pos)
            self._pressed = False
            self.is_dirty = True
            if inside and self.on_press is not None:
                self.on_press(self)
            return True
        return False


class ProgressBar(Widget):
    """Horizontal track + fill. `value` in [0, 1]. Re-renders only when value
    moves by >= one device pixel of fill (the engine still blits one cached
    surface per damaged frame)."""

    __slots__ = ("value", "track", "fill", "radius", "_fill_px")

    def __init__(self, w, h, value=0.0, track=(60, 60, 70), fill=(255, 255, 255),
                 radius=None, name=None):
        super().__init__(w, h, name=name)
        self.value = max(0.0, min(1.0, float(value)))
        self.track = track
        self.fill = fill
        self.radius = (h // 2) if radius is None else int(radius)
        self._fill_px = -1

    def set_value(self, v):
        v = max(0.0, min(1.0, float(v)))
        px = int(v * max(1, self.rect.w))
        if px != self._fill_px:
            self.value = v
            self._fill_px = px
            self.is_dirty = True

    def set_fill_color(self, color):
        if tuple(color) != tuple(self.fill):
            self.fill = color
            self.is_dirty = True

    def _render(self, surf):
        r = surf.get_rect()
        pygame.draw.rect(surf, self.track, r, border_radius=self.radius)
        fw = int(self.value * r.w)
        if fw > 0:
            pygame.draw.rect(surf, self.fill, pygame.Rect(0, 0, fw, r.h),
                             border_radius=self.radius)


class VolumeFader(Widget):
    """Vertical draggable fader. The top of the track is max (1.0), the bottom
    is 0.0. Dragging (or a tap) updates `value` and invokes `on_change(value)`;
    the caller is responsible for throttling actual volume writes.

    This widget is the UI half of CHANTIER 1.4: bind `on_change` to a
    BtVolume / AirPlay setter instead of poking a muted ALSA card.
    """

    __slots__ = ("value", "track", "fill", "knob", "on_change", "_dragging",
                 "_pad")

    def __init__(self, w, h, value=0.5, on_change=None,
                 track=(50, 50, 60), fill=(255, 255, 255), knob=(255, 255, 255),
                 pad=6, name=None):
        super().__init__(w, h, name=name)
        self.value = max(0.0, min(1.0, float(value)))
        self.track = track
        self.fill = fill
        self.knob = knob
        self.on_change = on_change
        self._dragging = False
        self._pad = int(pad)

    def set_value(self, v, notify=False):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self.value) >= 0.001:
            self.value = v
            self.is_dirty = True
            if notify and self.on_change is not None:
                self.on_change(self.value)

    def _value_from_y(self, y):
        top = self.rect.y + self._pad
        usable = max(1, self.rect.h - 2 * self._pad)
        frac = 1.0 - (y - top) / usable
        return max(0.0, min(1.0, frac))

    def handle_event(self, ev):
        pos = event_pos(ev)
        if pos is None:
            return False
        if is_down(ev) and self.contains(*pos):
            self._dragging = True
            self.set_value(self._value_from_y(pos[1]), notify=True)
            return True
        if is_move(ev) and self._dragging:
            self.set_value(self._value_from_y(pos[1]), notify=True)
            return True
        if is_up(ev) and self._dragging:
            self._dragging = False
            self.set_value(self._value_from_y(pos[1]), notify=True)
            return True
        return False

    def _render(self, surf):
        w, h = surf.get_size()
        cx = w // 2
        track_w = max(4, w // 3)
        track_rect = pygame.Rect(cx - track_w // 2, self._pad,
                                 track_w, h - 2 * self._pad)
        pygame.draw.rect(surf, self.track, track_rect, border_radius=track_w // 2)
        # filled portion from the bottom up
        usable = h - 2 * self._pad
        fill_h = int(self.value * usable)
        fill_rect = pygame.Rect(track_rect.x, track_rect.bottom - fill_h,
                                track_w, fill_h)
        if fill_h > 0:
            pygame.draw.rect(surf, self.fill, fill_rect, border_radius=track_w // 2)
        # knob
        knob_r = max(track_w, 12)
        ky = int((1.0 - self.value) * usable) + self._pad
        pygame.draw.circle(surf, self.knob, (cx, ky), knob_r // 2)
