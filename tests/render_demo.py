"""
render_demo.py - Build the NowPlayingView, push fake data, composite one frame
and save it to docs/preview.png. Proves the engine assembles a real screen.
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
from nowplaying_view import build


def synth_cover(size=320):
    s = pygame.Surface((size, size))
    for y in range(size):
        t = y / size
        col = (int(40 + 120 * t), int(20 + 60 * (1 - t)), int(90 + 120 * t))
        pygame.draw.line(s, col, (0, y), (size, y))
    pygame.draw.circle(s, (255, 255, 255), (size // 2, size // 2), size // 5, 6)
    return s


def main():
    screen = pygame.display.set_mode((800, 480))
    # warm time-of-day gradient background
    bg = pygame.Surface((800, 480))
    for y in range(480):
        t = y / 480
        bg.fill((int(12 + 8 * t), int(10 + 6 * t), int(18 + 14 * t)),
                pygame.Rect(0, y, 800, 1))

    wm, view = build(screen,
                     on_transport=lambda a: print("transport:", a),
                     on_volume=lambda v: print(f"volume: {v:.2f}"))
    wm.set_background(bg)
    view.update({"title": "Re:Stacks", "artist": "Bon Iver",
                 "playing": True, "pos": 73, "dur": 215,
                 "volume": 0.62, "cover": synth_cover()})
    wm.layout()
    rects = wm.render()

    out = os.path.join(_HERE, "..", "docs", "preview.png")
    pygame.image.save(screen, out)
    print(f"saved {os.path.normpath(out)}  (updated {len(rects)} rect(s))")


if __name__ == "__main__":
    main()
