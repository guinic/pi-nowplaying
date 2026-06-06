"""
views.nowplaying_view - A now-playing screen assembled with the ui engine.

This is the reference port that demonstrates CHANTIER 1 end to end:
  * Widgets cache their own pixels (Label/Icon/ProgressBar/Button).
  * VBox / HBox place every child -- there is not a single hard-coded child
    coordinate here; only intrinsic widget sizes + container padding/spacing.
  * Touch events propagate from the root down to the transport Buttons and the
    VolumeFader via collidepoint.
  * The fader's on_change is wired to a volume callback (bind it to
    btvolume.BtVolume.set for Bluetooth, or AirPlay SetVolume) -- never the
    muted ALSA card.

`NowPlayingView.update(snapshot)` pushes new data; nothing re-renders unless a
value actually changed.
"""

import pygame
from ui import (WindowManager, Container, VBox, HBox, Spacer,
                Label, Icon, Button, ProgressBar, VolumeFader)


def _font(size, bold=False):
    pygame.font.init()
    f = pygame.font.SysFont("dejavusans", size, bold=bold)
    return f


def _fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def glyph(kind, size=46, color=(240, 240, 245)):
    """Crisp, font-independent transport icon as an SRCALPHA Surface."""
    s = pygame.Surface((size, size), pygame.SRCALPHA)
    w = size
    if kind == "play":
        pygame.draw.polygon(s, color, [(w * 0.32, w * 0.22),
                                       (w * 0.32, w * 0.78), (w * 0.78, w * 0.5)])
    elif kind == "pause":
        bw = int(w * 0.15)
        pygame.draw.rect(s, color, (int(w * 0.31), int(w * 0.24), bw, int(w * 0.52)), border_radius=2)
        pygame.draw.rect(s, color, (int(w * 0.54), int(w * 0.24), bw, int(w * 0.52)), border_radius=2)
    elif kind == "prev":
        pygame.draw.polygon(s, color, [(w * 0.32, w * 0.5),
                                       (w * 0.64, w * 0.26), (w * 0.64, w * 0.74)])
        pygame.draw.rect(s, color, (int(w * 0.26), int(w * 0.26), int(w * 0.06), int(w * 0.48)))
    elif kind == "next":
        pygame.draw.polygon(s, color, [(w * 0.68, w * 0.5),
                                       (w * 0.36, w * 0.26), (w * 0.36, w * 0.74)])
        pygame.draw.rect(s, color, (int(w * 0.68), int(w * 0.26), int(w * 0.06), int(w * 0.48)))
    return s


class NowPlayingView:
    """Builds the scene graph and exposes `root` (give it to a WindowManager)
    plus `update()` / `relayout()`.

    on_transport(action)  action in {'prev','play_pause','next'}
    on_volume(frac)       frac in [0,1] from the fader
    """

    def __init__(self, size=(800, 480), fonts=None, on_transport=None,
                 on_volume=None):
        self.size = size
        self.on_transport = on_transport
        self.on_volume = on_volume
        f = fonts or {}
        self.f_title = f.get("title") or _font(40, bold=True)
        self.f_artist = f.get("artist") or _font(28)
        self.f_time = f.get("time") or _font(20)
        self.f_btn = f.get("btn") or _font(34, bold=True)

        W, H = size
        PAD = 24

        # --- content widgets ---------------------------------------------
        self.cover = Icon(w=240, h=240, radius=16, name="cover")
        self.title = Label(self.f_title, "", (255, 255, 255), name="title")
        self.artist = Label(self.f_artist, "", (170, 170, 180), name="artist")
        self.t_cur = Label(self.f_time, "0:00", (170, 170, 180), name="t_cur")
        self.t_end = Label(self.f_time, "0:00", (170, 170, 180),
                           halign="right", name="t_end")
        self.progress = ProgressBar(W - 2 * PAD, 8, name="progress")

        self._ico_play = glyph("play", 52)
        self._ico_pause = glyph("pause", 52)
        self.btn_prev = Button(96, 96, on_press=lambda b: self._fire("prev"),
                               icon=glyph("prev", 46), name="prev")
        self.btn_play = Button(112, 112, on_press=lambda b: self._fire("play_pause"),
                               icon=self._ico_play, name="play")
        self.btn_next = Button(96, 96, on_press=lambda b: self._fire("next"),
                               icon=glyph("next", 46), name="next")

        self.fader = VolumeFader(56, 300, value=0.5,
                                 on_change=self._on_fader, name="fader")

        # --- layout tree (VBox / HBox, no hard child coords) -------------
        info = VBox(W - 240 - 20 - 2 * PAD, 240, spacing=6, align="start",
                    name="info")
        info.add(self.title, self.artist, Spacer())

        top = HBox(W - 2 * PAD, 240, spacing=20, align="start", name="top")
        top.add(self.cover, info)

        timerow = HBox(W - 2 * PAD, 24, spacing=8, name="timerow")
        self.t_cur.resize(80, 24)
        self.t_end.resize(80, 24)
        timerow.add(self.t_cur, Spacer(), self.t_end)

        transport = HBox(W - 2 * PAD, 112, spacing=28, align="center",
                         name="transport")
        transport.add(Spacer(), self.btn_prev, self.btn_play, self.btn_next,
                      Spacer())

        self.main = VBox(W, H, padding=PAD, spacing=16, name="main")
        self.main.add(top, self.progress, timerow, Spacer(), transport)

        # Root: absolute container holding the main column + the fader overlay.
        self.root = Container(W, H, name="root")
        self.root.add(self.main, self.fader)

        self.relayout()

    # ---- callbacks -------------------------------------------------------
    def _fire(self, action):
        if self.on_transport:
            self.on_transport(action)

    def _on_fader(self, frac):
        if self.on_volume:
            self.on_volume(frac)

    # ---- geometry --------------------------------------------------------
    def relayout(self):
        W, H = self.size
        self.main.set_pos(0, 0)
        self.main.resize(W, H)
        self.main.layout()
        # fader pinned to the right edge, vertically centred
        self.fader.set_pos(W - self.fader.rect.w - 12, (H - self.fader.rect.h) // 2)

    # ---- data push -------------------------------------------------------
    def update(self, snap):
        """`snap` is a dict: title, artist, playing(bool), pos(sec), dur(sec),
        volume(0..1), cover(Surface)."""
        if "title" in snap:
            self.title.set_text(snap["title"])
        if "artist" in snap:
            self.artist.set_text(snap["artist"])
        if "cover" in snap and snap["cover"] is not None:
            self.cover.set_surface(snap["cover"])
        if "playing" in snap:
            self.btn_play.set_icon(self._ico_pause if snap["playing"] else self._ico_play)
        if "pos" in snap and "dur" in snap:
            dur = max(1, snap["dur"])
            self.progress.set_value(snap["pos"] / dur)
            self.t_cur.set_text(_fmt_time(snap["pos"]))
            self.t_end.set_text(_fmt_time(snap["dur"]))
        if "volume" in snap and not self.fader._dragging:
            self.fader.set_value(snap["volume"])
        # title/artist auto-resize on set_text; reflow so the row stays aligned
        self.relayout()


def build(screen, on_transport=None, on_volume=None):
    """Convenience: create a NowPlayingView + a WindowManager bound to it."""
    wm = WindowManager(screen)
    view = NowPlayingView(screen.get_size(), on_transport=on_transport,
                          on_volume=on_volume)
    wm.set_root(view.root)
    wm.layout()
    return wm, view
