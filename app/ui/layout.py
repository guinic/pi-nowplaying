"""
ui.layout - Box layout containers.

VBox / HBox automatically position their children along one axis and align them
on the cross axis, eliminating hard-coded (x, y) from views. `Spacer` children
absorb leftover space so a layout can be self-centering / self-justifying.

A container only re-flows when `layout()` is called (after its size changes or
its child set changes), not every frame -- positions are retained.
"""

import pygame
from .core import Widget, Container


class Spacer(Widget):
    """Invisible flexible gap for VBox/HBox layouts. `weight` sets its share of
    leftover space relative to sibling spacers."""

    __slots__ = ("weight", "_flex")

    def __init__(self, weight=1):
        super().__init__(0, 0, name="Spacer")
        self.weight = max(0.0, float(weight))
        self._flex = 0
        self.visible = False          # never painted; layout-only

    def walk(self):
        return iter(())               # not part of the paint tree


def _flex_distribute(content_len, fixed_total, spacing_total, spacers):
    """Distribute leftover space among Spacer children by weight."""
    leftover = max(0, content_len - fixed_total - spacing_total)
    total_w = sum(s.weight for s in spacers) or 1
    for s in spacers:
        s._flex = int(leftover * s.weight / total_w)


class VBox(Container):
    """Vertical stack. Children are placed top->bottom with `spacing` between
    them; `align` controls horizontal placement within the content box. Spacer
    children absorb leftover vertical space (flex)."""

    def layout(self):
        box = self.content_box()
        kids = [c for c in self.children if c.visible or isinstance(c, Spacer)]
        spacers = [c for c in kids if isinstance(c, Spacer)]
        fixed = sum(c.rect.h for c in kids if not isinstance(c, Spacer))
        spacing_total = self.spacing * max(0, len(kids) - 1)
        if spacers:
            _flex_distribute(box.h, fixed, spacing_total, spacers)
        y = box.y
        for c in kids:
            if isinstance(c, Spacer):
                c.resize(0, c._flex)
                y += c._flex + self.spacing
                continue
            if self.align == "center":
                x = box.x + (box.w - c.rect.w) // 2
            elif self.align == "end":
                x = box.right - c.rect.w
            else:
                x = box.x
            c.set_pos(x, y)
            y += c.rect.h + self.spacing
            if isinstance(c, Container):
                c.layout()


class HBox(Container):
    """Horizontal row. Children left->right with `spacing`; `align` controls
    vertical placement. Spacer children absorb leftover horizontal space."""

    def layout(self):
        box = self.content_box()
        kids = [c for c in self.children if c.visible or isinstance(c, Spacer)]
        spacers = [c for c in kids if isinstance(c, Spacer)]
        fixed = sum(c.rect.w for c in kids if not isinstance(c, Spacer))
        spacing_total = self.spacing * max(0, len(kids) - 1)
        if spacers:
            _flex_distribute(box.w, fixed, spacing_total, spacers)
        x = box.x
        for c in kids:
            if isinstance(c, Spacer):
                c.resize(c._flex, 0)
                x += c._flex + self.spacing
                continue
            if self.align == "center":
                y = box.y + (box.h - c.rect.h) // 2
            elif self.align == "end":
                y = box.bottom - c.rect.h
            else:
                y = box.y
            c.set_pos(x, y)
            x += c.rect.w + self.spacing
            if isinstance(c, Container):
                c.layout()


class ZStack(Container):
    """Overlap container: every child fills the content box (or keeps its own
    size, aligned center). Useful for layering a cover image under text or
    stacking an overlay (volume fader) over the now-playing view. Paint order =
    insertion order, so later children are on top."""

    def layout(self):
        box = self.content_box()
        for c in self.children:
            if c.rect.w == 0 and c.rect.h == 0:
                c.resize(box.w, box.h)
            cx = box.x + (box.w - c.rect.w) // 2
            cy = box.y + (box.h - c.rect.h) // 2
            c.set_pos(cx, cy)
            if isinstance(c, Container):
                c.layout()
