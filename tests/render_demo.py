"""
render_demo.py - Composite ONE now-playing frame through the engine + the
production draw pipeline and save it to docs/preview.png.

Because the demo view renders via ``npdraw`` (the exact code the live app runs),
this image is pixel-identical to the real screen -- the only requirement is that
the Inter fonts are installed (they are on the Pi). On a dev box without Inter it
falls back to DejaVu, so run this ON THE PI for the authoritative preview.

It feeds the SAME synthetic cover + metadata prod injects on SIGUSR2
("Midnight City / M83"), so the preview matches a real captured frame.

Headless: SDL_VIDEODRIVER=dummy.
"""

import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "app"))
sys.path.insert(0, os.path.join(_HERE, "..", "app", "views"))

import pygame
pygame.init()

import npdraw
from nowplaying_view import build


def main():
    screen = pygame.display.set_mode((npdraw.W, npdraw.H))

    wm, view = build(screen,
                     on_transport=lambda a: print("transport:", a),
                     on_mode_toggle=lambda: print("mode toggle"),
                     on_volume=lambda v: print(f"volume: {v:.2f}"))

    # Exact production preview: synthetic cover -> real visual pipeline, plus the
    # injected now-playing metadata (1:13 / 4:04, AirPlay 44.1 kHz / 16-bit).
    visuals = npdraw.build_visuals(npdraw.preview_cover_bytes())
    snap = dict(npdraw.preview_info())
    snap["visuals"] = visuals
    snap["mode"] = "airplay"
    snap["connected"] = True
    view.update(snap)

    wm.layout()
    rects = wm.render()

    out = os.path.join(_HERE, "..", "docs", "preview.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pygame.image.save(screen, out)
    print(f"saved {os.path.normpath(out)}  (updated {len(rects)} rect(s))")


if __name__ == "__main__":
    main()
