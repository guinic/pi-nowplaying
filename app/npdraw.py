"""
npdraw.py - The production now-playing render pipeline, extracted as a pure,
hardware-free module so the engine demo (and the tests) draw frames that are
PIXEL-IDENTICAL to the live app.

Why this exists
---------------
The demo view used to be a generic approximation (DejaVu fonts, flat colours, a
VBox layout) that looked nothing like the real screen. The only way to replicate
production *exactly* is to share its actual drawing code, not re-implement it.
Every function below is a faithful, line-for-line port of the matching function
in ``nowplaying.py``'s render loop; because they are one code path, the demo
cannot drift from production.

Design
------
* Immediate-mode primitives that blit onto a caller-supplied ``Surface`` exactly
  as prod blits onto its KMSDRM framebuffer. ``compose_now_playing`` runs the
  same sequence of blits prod's render loop runs for the now-playing screen, on a
  single surface -- so the result is byte-identical (pygame's straight-alpha
  integer blit is not perfectly associative across intermediate transparent
  layers, so we composite on ONE surface, like prod does).
* Geometry + palette come from ``npconfig`` -- the same module prod imports, so
  there is a single source of truth for every coordinate and colour.
* The only module state is the bounded memo caches for per-track glows /
  text shadows, identical to prod's (same CPU win, no behavioural change).
* Hardware-free: importing this module touches no display, font file or GPU. The
  fonts are loaded lazily by ``load_font``; PIL is imported lazily inside
  ``build_visuals`` / ``vignette`` so the module imports on a bare dev box.
"""

import io
import math
import logging
import colorsys

import pygame

# Geometry, palette and thresholds -- the SAME module the live app imports, so
# every coordinate (PAD, CONT_H, BTN_X, PILLBAR, VOL_FADER, MODE_TOGGLE, ...),
# colour (WHITE, GRAY, BG_TOP, SCRIM, ...) and timing (ROW_GAP, MARQUEE_*,
# EQ_ANIM_MS, NIGHT_DIM, ...) is shared, never duplicated.
from npconfig import *  # noqa: F401,F403

log = logging.getLogger("npdraw")

SAMPLE_RATE = 44100          # matches nowplaying.SAMPLE_RATE (progress math)
COVER_MAX = 240              # matches nowplaying.COVER_MAX (album-art max edge)


# ===========================================================================
# Colour lerp (prod nowplaying.lerp -- integer per-channel, RGB only)
# ===========================================================================
def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_gradient(w, h, top, bot):
    s = pygame.Surface((w, h))
    for y in range(h):
        t = y / max(1, h - 1)
        pygame.draw.line(s, lerp(top, bot, t), (0, y), (w, y))
    return s


# ===========================================================================
# Fonts (Inter, with DejaVu/Liberation fallback)
# ===========================================================================
_INTER_DIR = "/usr/share/fonts/opentype/inter"


def load_font(size, bold=False, weight=None, display=False):
    """Load Inter at a given weight (Thin/Light/Regular/Medium/SemiBold/Bold/
    ExtraBold/Black). `display=True` uses the Inter Display optical variant, which
    is tuned for large sizes (the clock). Falls back to DejaVu/Liberation, then
    the pygame default, so a missing font never crashes the render loop."""
    if weight is None:
        weight = 'Bold' if bold else 'Regular'
    fam = 'InterDisplay' if display else 'Inter'
    suffix = '-Bold' if (bold or weight in ('Bold', 'ExtraBold', 'Black')) else ''
    for path in [f"{_INTER_DIR}/{fam}-{weight}.otf",
                 f"{_INTER_DIR}/Inter-{weight}.otf",
                 f"{_INTER_DIR}/Inter-Regular.otf",
                 f"/usr/share/fonts/truetype/dejavu/DejaVuSans{suffix}.ttf",
                 f"/usr/share/fonts/truetype/liberation/LiberationSans{suffix}-Regular.ttf"]:
        try:
            import os
            if os.path.exists(path):
                return pygame.font.Font(path, size)
        except Exception:
            pass
    return pygame.font.Font(None, size + 10)


def make_fonts():
    """The exact font set prod's render loop builds (same sizes/weights/optical
    variants). Returned as a dict keyed like prod's ``fonts``."""
    return {
        'clock':  load_font(120, weight='Light', display=True),
        'day':    load_font(30, weight='SemiBold'),
        'mm':     load_font(19, weight='Medium'),
        'date':   load_font(24, weight='Medium'),
        'idle':   load_font(26, weight='Regular'),
        'title':  load_font(44, weight='Bold', display=True),
        'artist': load_font(30, weight='Medium'),
        'album':  load_font(23, weight='Regular'),
        'time':   load_font(18, weight='Medium'),
        'badge':  load_font(15, weight='SemiBold'),
        'vol':    load_font(24, weight='SemiBold'),
        'wxtemp': load_font(44, weight='SemiBold', display=True),
        'wxdesc': load_font(20, weight='Regular'),
        'pill':   load_font(22, weight='SemiBold'),
        'hint':   load_font(16, weight='Regular'),
    }


# ===========================================================================
# Vector glyphs (transport icons + source icons)
# ===========================================================================
def _aa_poly(surf, color, pts):
    try:
        import pygame.gfxdraw as gfx
        ip = [(int(x), int(y)) for x, y in pts]
        gfx.aapolygon(surf, ip, color); gfx.filled_polygon(surf, ip, color)
    except Exception:
        pygame.draw.polygon(surf, color, pts)


def draw_play(s, cx, cy, z, c):
    _aa_poly(s, c, [(cx - z*0.46, cy - z*0.62), (cx - z*0.46, cy + z*0.62),
                    (cx + z*0.66, cy)])


def draw_pause(s, cx, cy, z, c):
    bw, gap, bh = z*0.34, z*0.16, z*1.3; rad = max(1, int(bw*0.28))
    pygame.draw.rect(s, c, pygame.Rect(cx - gap - bw, cy - bh/2, bw, bh), border_radius=rad)
    pygame.draw.rect(s, c, pygame.Rect(cx + gap, cy - bh/2, bw, bh), border_radius=rad)


def draw_next(s, cx, cy, z, c):
    _aa_poly(s, c, [(cx - z*0.78, cy - z*0.58), (cx - z*0.78, cy + z*0.58), (cx - z*0.04, cy)])
    _aa_poly(s, c, [(cx - z*0.04, cy - z*0.58), (cx - z*0.04, cy + z*0.58), (cx + z*0.70, cy)])
    pygame.draw.rect(s, c, pygame.Rect(cx + z*0.70, cy - z*0.58, z*0.22, z*1.16),
                     border_radius=max(1, int(z*0.06)))


def draw_prev(s, cx, cy, z, c):
    pygame.draw.rect(s, c, pygame.Rect(cx - z*0.92, cy - z*0.58, z*0.22, z*1.16),
                     border_radius=max(1, int(z*0.06)))
    _aa_poly(s, c, [(cx + z*0.78, cy - z*0.58), (cx + z*0.78, cy + z*0.58), (cx + z*0.04, cy)])
    _aa_poly(s, c, [(cx + z*0.04, cy - z*0.58), (cx + z*0.04, cy + z*0.58), (cx - z*0.70, cy)])


def draw_bt_icon(s, cx, cy, z, c):
    top, bot = cy - z, cy + z
    midx = cx + z * 0.55
    pygame.draw.lines(s, c, False, [(cx, top), (midx, cy - z*0.5),
        (cx - z*0.55, cy + z*0.5)], 3)
    pygame.draw.lines(s, c, False, [(cx - z*0.55, cy - z*0.5),
        (midx, cy + z*0.5), (cx, bot)], 3)
    pygame.draw.line(s, c, (cx, top), (cx, bot), 3)


def _airplay_icon(s, cx, cy, z, c):
    _aa_poly(s, c, [(cx, cy + z * 0.2), (cx - z * 0.7, cy + z), (cx + z * 0.7, cy + z)])
    pygame.draw.arc(s, c, pygame.Rect(cx - z, cy - z, 2 * z, 2 * z),
                    math.radians(35), math.radians(145), 3)


# ===========================================================================
# Blur / text / time helpers
# ===========================================================================
def soft_glow(surf, scale=7):
    # Cheap blur: shrink then grow with smooth scaling. Good enough for a halo
    # behind large glyphs without a real Gaussian pass.
    w, h = surf.get_size()
    small = pygame.transform.smoothscale(
        surf, (max(1, w // scale), max(1, h // scale)))
    return pygame.transform.smoothscale(small, (w, h))


# Strip control chars (NUL/\x01.. + DEL) before they reach SDL_ttf. Some AirPlay
# senders (e.g. TuneBlade) NUL-terminate metadata strings; a raw \x00 makes
# font.render raise "A null character was found in the text". This is the exact
# same guard prod's nowplaying.py applies, so the demo stays a faithful port.
_CTRL_TABLE = {i: None for i in range(0x20)}
_CTRL_TABLE[0x7F] = None


def sanitize_text(s):
    """Drop control chars that would crash SDL_ttf or render as tofu."""
    return s.translate(_CTRL_TABLE) if s else s


_text_cache = {}


def text_surf(font, text, color, max_w):
    text = sanitize_text(text)          # never feed a NUL/control char to SDL_ttf
    key = (id(font), text, color, max_w)
    s = _text_cache.get(key)
    if s is None:
        if not text:
            return None
        s = font.render(text, True, color)
        if s.get_width() > max_w:
            t = text
            while len(t) > 1 and font.size(t + '…')[0] > max_w:
                t = t[:-1]
            s = font.render(t + '…', True, color)
        if len(_text_cache) > 60:
            _text_cache.clear()
        _text_cache[key] = s
    return s


_full_cache = {}


def full_text_surf(font, text, color, shadow=True):
    """Full (un-truncated) text surface with a soft drop shadow baked in, cached.
    The marquee needs the real pixel width to know when to scroll, and the baked
    shadow keeps the text legible over any album-art background (no per-frame
    cost -- it is rendered once and reused)."""
    text = sanitize_text(text)          # never feed a NUL/control char to SDL_ttf
    if not text:
        return None
    key = (id(font), text, color, shadow)
    s = _full_cache.get(key)
    if s is None:
        base = font.render(text, True, color)
        if shadow:
            sh = font.render(text, True, (0, 0, 0))
            w, h = base.get_size()
            pad = 3
            s = pygame.Surface((w + pad * 2, h + pad * 2), pygame.SRCALPHA)
            sh.set_alpha(110)
            s.blit(sh, (pad + 1, pad + 1))
            s.blit(sh, (pad + 2, pad + 2))
            s.blit(base, (pad, pad))
        else:
            s = base
        if len(_full_cache) > 80:
            _full_cache.clear()
        _full_cache[key] = s
    return s


def fmt_time(sec):
    sec = max(0, int(sec)); return f"{sec // 60}:{sec % 60:02d}"


def blit_alpha(screen, surf, pos, alpha):
    if alpha >= 255:
        screen.blit(surf, pos)
    elif alpha > 0:
        surf.set_alpha(int(alpha)); screen.blit(surf, pos); surf.set_alpha(255)


_strip_cache = {}


def _scroll_strip(view_w, h):
    s = _strip_cache.get((view_w, h))
    if s is None:
        s = pygame.Surface((view_w, h), pygame.SRCALPHA)
        _strip_cache[(view_w, h)] = s
    return s


def draw_text_row(screen, full_surf, x, y, view_w, age_ms):
    """Blit one text row at (x, y). If it is wider than view_w, scroll it as a
    seamless loop ([text][gap][text]); age_ms is the time since the current track
    loaded, so each new title starts paused at the left. Returns True while it is
    actively scrolling (the caller speeds the render loop up to 30 fps)."""
    if full_surf is None:
        return False
    fw, h = full_surf.get_size()
    if fw <= view_w:
        screen.blit(full_surf, (x, y))
        return False
    period = fw + MARQUEE_GAP
    off = (max(0, age_ms - MARQUEE_PAUSE) / 1000.0 * MARQUEE_SPEED) % period
    strip = _scroll_strip(view_w, h)
    strip.fill((0, 0, 0, 0))
    strip.blit(full_surf, (int(-off), 0))
    strip.blit(full_surf, (int(period - off), 0))   # trailing copy for the wrap
    screen.blit(strip, (x, y))
    return True


# ===========================================================================
# Static full-surface overlays (built once, reused)
# ===========================================================================
_VIGNETTE = None


def vignette():
    # Soft edge-darkening overlay for the player view -- grounds the floating
    # cover/text against the blurred background and adds depth. Built once at a
    # small size (cheap Python loop) then smooth-scaled to full screen.
    global _VIGNETTE
    if _VIGNETTE is None:
        try:
            from PIL import Image as PILImage
            sw, sh = 160, 96
            v = PILImage.new('RGBA', (sw, sh), (0, 0, 0, 0))
            pv = v.load()
            cxv, cyv = sw / 2.0, sh / 2.0
            maxd = math.hypot(cxv, cyv)
            for y in range(sh):
                for x in range(sw):
                    d = math.hypot(x - cxv, y - cyv) / maxd
                    a = int((min(1.0, max(0.0, (d - 0.5) / 0.5)) ** 1.5) * 165)
                    pv[x, y] = (0, 0, 0, a)
            surf = pygame.image.frombytes(v.tobytes(), (sw, sh), 'RGBA')
            _VIGNETTE = pygame.transform.smoothscale(surf, (W, H))
        except Exception as e:
            log.warning(f"vignette: {e}")
            _VIGNETTE = pygame.Surface((W, H), pygame.SRCALPHA)
    return _VIGNETTE


_NIGHT_SCRIM = None


def night_scrim():
    global _NIGHT_SCRIM
    if _NIGHT_SCRIM is None:
        s = pygame.Surface((W, H), pygame.SRCALPHA)
        s.fill((*SCRIM, NIGHT_DIM))
        _NIGHT_SCRIM = s
    return _NIGHT_SCRIM


# --- Per-track static glows, memoised --------------------------------------
# The cover bloom/ring and the progress scrubber halo depend only on the cover
# size + accent colour, which stay constant for the whole track. soft_glow runs
# two smoothscale passes, so recomputing them every frame cost ~75% CPU during a
# scrolling-title marquee. Cache by (size, accent) so the blur runs ONCE.
_cover_bloom_cache = {}
_cover_ring_cache = {}
_scrub_glow_cache = {}


def _cover_bloom(cw, ch, accent):
    key = (cw, ch, tuple(accent))
    b = _cover_bloom_cache.get(key)
    if b is None:
        sp = 34
        b = pygame.Surface((cw + sp * 2, ch + sp * 2), pygame.SRCALPHA)
        pygame.draw.rect(b, (*accent, 130), (sp, sp, cw, ch), border_radius=30)
        b = soft_glow(b, 9); b.set_alpha(150)
        if len(_cover_bloom_cache) > 12:
            _cover_bloom_cache.clear()
        _cover_bloom_cache[key] = b
    return b


def _cover_ring(cw, ch, accent):
    key = (cw, ch, tuple(accent))
    r = _cover_ring_cache.get(key)
    if r is None:
        r = pygame.Surface((cw + 4, ch + 4), pygame.SRCALPHA)
        pygame.draw.rect(r, (*accent, 95), r.get_rect(), width=2, border_radius=20)
        if len(_cover_ring_cache) > 12:
            _cover_ring_cache.clear()
        _cover_ring_cache[key] = r
    return r


def _scrub_glow(accent):
    key = tuple(accent)
    g = _scrub_glow_cache.get(key)
    if g is None:
        g = pygame.Surface((40, 40), pygame.SRCALPHA)
        pygame.draw.circle(g, (*accent, 150), (20, 20), 13)
        g = soft_glow(g, 5); g.set_alpha(160)
        if len(_scrub_glow_cache) > 24:
            _scrub_glow_cache.clear()
        _scrub_glow_cache[key] = g
    return g


# ===========================================================================
# Now-playing elements (each = one prod draw_* function, verbatim)
# ===========================================================================
def draw_cover(screen, cover, shadow=None, alpha=255, accent=None):
    cw, ch = cover.get_size()
    x, y = PAD, (CONT_H - ch) // 2
    # Ambient accent bloom: a soft colour halo bleeding out from behind the art,
    # as if the cover is lit. Sits under the (darker) drop shadow.
    if accent is not None and alpha >= 255:
        screen.blit(_cover_bloom(cw, ch, accent), (x - 34, y - 34))
    if alpha >= 255 and shadow is not None:
        screen.blit(shadow, (x - 16, y - 16 + 5))   # shp offset, nudged down to ground it
    blit_alpha(screen, cover, (x, y), alpha)
    # Thin accent hairline tracing the art's rounded edge -- frames the cover
    # against the blurred background and ties it to the track's accent colour.
    if accent is not None and alpha >= 255:
        screen.blit(_cover_ring(cw, ch, accent), (x - 2, y - 2))
    return x + cw + PAD


def draw_progress(screen, fonts, info, accent, playing_eff):
    import time
    dur = (info['prog_end'] - info['prog_start']) / SAMPLE_RATE
    if dur <= 0:
        return
    base = (info['prog_cur'] - info['prog_start']) / SAMPLE_RATE
    elapsed = base + (time.monotonic() - info['prog_at'] if playing_eff and info['prog_at'] else 0)
    elapsed = max(0, min(dur, elapsed))
    bx, bw, by, bh = PAD, W - 2 * PAD, CONT_H - 34, 6
    pygame.draw.rect(screen, (70, 74, 96), (bx, by, bw, bh), border_radius=3)
    fill = int(bw * (elapsed / dur))
    if fill > 0:
        pygame.draw.rect(screen, accent, (bx, by, fill, bh), border_radius=3)
        cxp, cyp = bx + fill, by + bh // 2
        # Soft accent halo + white scrubber with an accent ring for a premium,
        # tactile handle.
        screen.blit(_scrub_glow(accent), (cxp - 20, cyp - 20))
        pygame.draw.circle(screen, (*accent, 255), (cxp, cyp), 9)
        pygame.draw.circle(screen, WHITE, (cxp, cyp), 6)
    et = text_surf(fonts['time'], fmt_time(elapsed), GRAY, W)
    if et:
        screen.blit(et, (bx, by - 22))
    td = text_surf(fonts['time'], fmt_time(dur), GRAY, W)
    if td:
        screen.blit(td, (bx + bw - td.get_width(), by - 22))


def _fmt_khz(rate):
    if not rate:
        return ''
    k = rate / 1000.0
    s = f'{k:.1f}'.rstrip('0').rstrip('.')
    return f'{s} kHz'


def draw_badge(screen, fonts, accent, mode, info):
    # Live transmission tag driven by the real ALSA hw_params (state.hw_*) plus,
    # in BT mode, the negotiated A2DP codec. The leading dot is lit (accent) when
    # the device is actually open/streaming and dim when idle.
    active = info['hw_active']
    parts = ['BLUETOOTH' if mode == 'bluetooth' else 'AIRPLAY']
    if active:
        if mode == 'bluetooth' and info['bt_codec']:
            parts.append(info['bt_codec'])
        khz = _fmt_khz(info['hw_rate'])
        if khz:
            parts.append(khz)
        if info['hw_bits'] and mode != 'bluetooth':
            parts.append(f"{info['hw_bits']}-bit")
    else:
        parts.append('—')
    label = '  ·  '.join(parts)
    txt = fonts['badge'].render(label, True, WHITE if active else GRAY)
    dot_r = 4
    dot_gap = 9
    pad_l = 12
    pw = pad_l + dot_r * 2 + dot_gap + txt.get_width() + 13
    ph = txt.get_height() + 10
    px, py = W - PAD - pw, PAD
    pill = pygame.Surface((pw, ph), pygame.SRCALPHA)
    pygame.draw.rect(pill, (*accent, 60) if active else (40, 44, 60, 150),
                     (0, 0, pw, ph), border_radius=ph // 2)
    pygame.draw.rect(pill, (*accent, 200) if active else (96, 102, 124, 170),
                     (0, 0, pw, ph), width=1, border_radius=ph // 2)
    screen.blit(pill, (px, py))
    dcx = px + pad_l + dot_r
    dcy = py + ph // 2
    if active:
        glow = pygame.Surface((dot_r * 6, dot_r * 6), pygame.SRCALPHA)
        pygame.draw.circle(glow, (*accent, 120), (dot_r * 3, dot_r * 3), dot_r * 3)
        screen.blit(soft_glow(glow, 3), (dcx - dot_r * 3, dcy - dot_r * 3))
        pygame.draw.circle(screen, accent, (dcx, dcy), dot_r)
    else:
        pygame.draw.circle(screen, (110, 116, 138), (dcx, dcy), dot_r, width=1)
    screen.blit(txt, (px + pad_l + dot_r * 2 + dot_gap, py + 5))


def draw_mode_toggle(screen, fonts, mode, accent, pressed=False):
    # Small chip showing the OTHER source; tapping it switches to that source.
    target = 'bluetooth' if mode == 'airplay' else 'airplay'
    label = 'BT' if target == 'bluetooth' else 'AirPlay'
    icon_fn = draw_bt_icon if target == 'bluetooth' else _airplay_icon
    r = MODE_TOGGLE
    surf = pygame.Surface((r.w, r.h), pygame.SRCALPHA)
    if pressed:
        pygame.draw.rect(surf, (*accent, 235), (0, 0, r.w, r.h), border_radius=r.h // 2)
        fg = (16, 18, 28); arrow_col = (16, 18, 28)
    else:
        pygame.draw.rect(surf, (255, 255, 255, 26), (0, 0, r.w, r.h), border_radius=r.h // 2)
        pygame.draw.rect(surf, (*accent, 170), (0, 0, r.w, r.h), width=2, border_radius=r.h // 2)
        fg = (224, 228, 240); arrow_col = accent
    screen.blit(surf, r.topleft)
    arrow = fonts['badge'].render('>', True, arrow_col)
    txt = fonts['badge'].render(label, True, fg)
    icon_r = 8
    gap = 6
    content_w = arrow.get_width() + 6 + icon_r + gap + txt.get_width()
    x = r.centerx - content_w // 2
    screen.blit(arrow, (x, r.centery - arrow.get_height() // 2)); x += arrow.get_width() + 6
    icon_fn(screen, x + icon_r // 2, r.centery, icon_r, fg); x += icon_r + gap
    screen.blit(txt, (x, r.centery - txt.get_height() // 2))


def draw_controls(screen, info, presses, accent, playing_eff, connected=None):
    now = pygame.time.get_ticks()
    if connected is None:
        connected = bool(info['client_ip'] and info['dacp_port'] and info['active_remote'])
    # Centered rounded "pill" holding the 3 transport buttons. Translucent dark
    # fill; an accent hairline border when a source is connected, dim grey else.
    rad = PILLBAR.h // 2
    pill = pygame.Surface((PILLBAR.w, PILLBAR.h), pygame.SRCALPHA)
    pygame.draw.rect(pill, (12, 14, 24, 205), pill.get_rect(), border_radius=rad)
    pygame.draw.rect(pill, (*accent, 150) if connected else (78, 84, 110, 150),
                     pill.get_rect(), width=2, border_radius=rad)
    screen.blit(pill, PILLBAR.topleft)
    for key, cx in zip(BTN_KEYS, BTN_X):
        pressed = (now - presses.get(key, -9999)) < PRESS_MS
        if key == 'play':
            # Primary action: a filled accent disc.
            r = BTN_R + (2 if pressed else 0)
            if connected:
                pygame.draw.circle(screen, accent, (cx, BTN_Y), r)
                icon = (14, 16, 26)
            else:
                pygame.draw.circle(screen, (44, 50, 74), (cx, BTN_Y), r)
                icon = (110, 116, 138)
            (draw_pause if playing_eff else draw_play)(screen, cx, BTN_Y, 20, icon)
        else:
            # Secondary actions: light "ghost" icons, with a soft disc on press.
            if pressed:
                pygame.draw.circle(screen, (54, 60, 86), (cx, BTN_Y), BTN_R - 4)
            if connected:
                icon = accent if pressed else (214, 218, 232)
            else:
                icon = (96, 102, 124)
            (draw_prev if key == 'prev' else draw_next)(screen, cx, BTN_Y, 16, icon)


def draw_volume_fader(screen, fonts, accent, vol, active=False):
    """Persistent vertical fader on the right edge: the swipe/drag target is
    always visible and the level updates live. `active` swells the knob and shows
    the % readout (while dragging or just after a change)."""
    if vol < 0:
        vol = 0
    vol = max(0, min(100, vol))
    r = VOL_FADER
    rad = r.w // 2
    track = pygame.Surface((r.w, r.h), pygame.SRCALPHA)
    pygame.draw.rect(track, (255, 255, 255, 46), track.get_rect(), border_radius=rad)
    screen.blit(track, r.topleft)
    fh = int(round(r.h * vol / 100))
    if fh > 0:
        fill = pygame.Surface((r.w, fh), pygame.SRCALPHA)
        pygame.draw.rect(fill, (*accent, 255), fill.get_rect(), border_radius=rad)
        screen.blit(fill, (r.x, r.bottom - fh))
    ky = max(r.top, min(r.bottom, r.bottom - fh))
    kr = 14 if active else 10
    pygame.draw.circle(screen, (14, 16, 26), (r.centerx, ky), kr + 2)
    pygame.draw.circle(screen, accent if active else WHITE, (r.centerx, ky), kr)
    if active:
        lab = fonts['vol'].render(f"{int(round(vol))}%", True, WHITE)
        lr = lab.get_rect(); lr.right = r.x - 14; lr.centery = ky
        bg = pygame.Surface((lr.w + 18, lr.h + 10), pygame.SRCALPHA)
        pygame.draw.rect(bg, (12, 14, 24, 225), bg.get_rect(), border_radius=9)
        screen.blit(bg, (lr.x - 9, lr.y - 5))
        screen.blit(lab, lr.topleft)


def draw_eq(screen, accent, tx, ty, now_ms=0, animate=False):
    """The 4-bar equalizer mark under the album line. Static resting heights
    (10,18,13,7) unless ``animate`` (matches prod's EQ_ANIM_MS window)."""
    for i in range(4):
        if animate:
            ph = 7 + int(11 * (0.5 + 0.5 * math.sin(now_ms / 170.0 + i * 1.15)))
        else:
            ph = (10, 18, 13, 7)[i]   # static resting heights
        pygame.draw.rect(screen, accent,
                         (tx + i * 9, ty + 4 + (18 - ph), 5, ph),
                         border_radius=2)


# ===========================================================================
# Visual builder (album-art derived cover / shadow / blurred bg / accent)
# ===========================================================================
def _vibrant_accent(img):
    try:
        small = img.resize((48, 48))
        best, best_score = (88, 166, 255), -1.0
        for count, (r, g, b) in small.getcolors(48 * 48) or []:
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            score = s * count * (1.0 - abs(v - 0.6))
            if score > best_score:
                best_score, best = score, (r, g, b)
        r, g, b = best
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        v = max(v, 0.65); s = max(s, 0.45)
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        return (int(r * 255), int(g * 255), int(b * 255))
    except Exception:
        return (88, 166, 255)


def build_visuals(raw):
    """Turn raw album-art bytes into the same {cover, shadow, bg, accent} bundle
    prod's ``_build_visuals`` produces (rounded art + blurred drop shadow +
    blurred/darkened full-screen background + vibrant accent). Returns the dict
    (prod pushes it onto a queue; here we just return it). None on failure."""
    try:
        from PIL import Image as PILImage, ImageFilter, ImageEnhance, ImageDraw
        img = PILImage.open(io.BytesIO(raw)).convert('RGB')
        accent = _vibrant_accent(img)

        cov = img.copy()
        cov.thumbnail((COVER_MAX, COVER_MAX))
        cov = cov.convert('RGBA')
        cw, ch = cov.size
        radius = 18
        mask = PILImage.new('L', (cw, ch), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, cw - 1, ch - 1],
                                               radius=radius, fill=255)
        cov.putalpha(mask)
        cover_surf = pygame.image.frombytes(cov.tobytes(), (cw, ch), 'RGBA')
        shp = 16
        shadow = PILImage.new('RGBA', (cw + shp * 2, ch + shp * 2), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [shp, shp, shp + cw - 1, shp + ch - 1], radius=radius, fill=(0, 0, 0, 165))
        shadow = shadow.filter(ImageFilter.GaussianBlur(9))
        shadow_surf = pygame.image.frombytes(shadow.tobytes(), shadow.size, 'RGBA')

        try:
            resample_filter = PILImage.Resampling.BILINEAR
        except AttributeError:
            resample_filter = PILImage.BILINEAR

        small = img.resize((100, 60)).filter(ImageFilter.GaussianBlur(8))
        big = small.resize((W, H), resample_filter)
        big = ImageEnhance.Brightness(big).enhance(0.45)
        big = ImageEnhance.Color(big).enhance(1.15)
        bg_surf = pygame.image.frombytes(big.tobytes(), (W, H), 'RGB')

        return {'cover': cover_surf, 'shadow': shadow_surf,
                'bg': bg_surf, 'accent': accent}
    except Exception as e:
        log.warning(f"Visual build: {e}")
        return None


def preview_cover_bytes():
    """The exact synthetic album art prod injects on SIGUSR2 (a 2x2 colour field
    upsized to 600x600 with a white ring + dark centre). Returns PNG bytes ready
    for ``build_visuals``. Lets the demo reproduce the real preview frame."""
    from PIL import Image as PILImage, ImageDraw
    base = PILImage.new('RGB', (2, 2))
    base.putpixel((0, 0), (236, 94, 142)); base.putpixel((1, 0), (92, 124, 246))
    base.putpixel((0, 1), (250, 186, 92)); base.putpixel((1, 1), (54, 200, 178))
    try:
        rs = PILImage.Resampling.BILINEAR
    except AttributeError:
        rs = PILImage.BILINEAR
    img = base.resize((600, 600), rs)
    d = ImageDraw.Draw(img, 'RGBA')
    d.ellipse([150, 150, 450, 450], outline=(255, 255, 255, 90), width=10)
    d.ellipse([240, 240, 360, 360], fill=(20, 20, 28, 140))
    bio = io.BytesIO(); img.save(bio, format='PNG')
    return bio.getvalue()


def preview_info(elapsed_s=73, dur_s=244):
    """The exact now-playing metadata prod injects on SIGUSR2, as a snapshot-like
    dict. ``prog_at=0`` freezes the progress at ``elapsed_s`` (deterministic for a
    one-frame render); prod uses a live monotonic clock instead."""
    return {
        'title': 'Midnight City', 'artist': 'M83',
        'album': "Hurry Up, We're Dreaming",
        'playing': True, 'volume': 42,
        'client_ip': 'preview', 'dacp_port': 3391, 'active_remote': '1',
        'prog_start': 0, 'prog_cur': int(elapsed_s * SAMPLE_RATE),
        'prog_end': int(dur_s * SAMPLE_RATE), 'prog_at': 0,
        'hw_active': True, 'hw_rate': 44100, 'hw_bits': 16, 'bt_codec': None,
    }


# ===========================================================================
# Full-frame composition (mirrors the render loop's now-playing branch)
# ===========================================================================
def compose_now_playing(surf, scene, fonts, *, now_ms=10000, track_t0=0,
                        eq_animate=False, night=False, presses=None,
                        toggle_pressed=False, vactive=False):
    """Composite the whole now-playing screen onto ``surf`` in prod's exact draw
    order (single source -- no crossfade). ``scene`` carries the visual bundle
    (cover/shadow/bg/accent), the ``info`` snapshot, ``mode`` and ``connected``.

    Mirrors nowplaying.py render loop lines (background -> vignette -> cover ->
    text rows -> EQ -> progress -> badge -> mode toggle -> controls -> fader ->
    night scrim). Defaults render the deterministic, settled frame (no marquee,
    static EQ, no night dim, nothing pressed)."""
    info = scene['info']
    accent = scene['accent']
    mode = scene.get('mode', 'airplay')
    connected = scene.get('connected', True)
    presses = presses or {}
    playing_eff = bool(info.get('playing'))

    # background (blurred album art, else the night/idle gradient fallback)
    if scene.get('bg') is not None:
        surf.blit(scene['bg'], (0, 0))
    else:
        surf.blit(make_gradient(W, H, BG_TOP, BG_BOT), (0, 0))
    surf.blit(vignette(), (0, 0))

    cover = scene.get('cover')
    tx = draw_cover(surf, cover, scene.get('shadow'), 255, accent) if cover else PAD
    tw = W - tx - PAD
    ty = max(PAD, (CONT_H - 170) // 2)
    age = now_ms - track_t0
    for fk, val, color in [('title', info['title'], WHITE),
                           ('artist', info['artist'], accent),
                           ('album', info['album'], GRAY)]:
        fs = full_text_surf(fonts[fk], val, color)
        if fs:
            draw_text_row(surf, fs, tx, ty, tw, age)
            ty += fs.get_height() + ROW_GAP
    if playing_eff:
        animate = eq_animate and (now_ms - track_t0) < EQ_ANIM_MS
        draw_eq(surf, accent, tx, ty, now_ms, animate)
    draw_progress(surf, fonts, info, accent, playing_eff)
    draw_badge(surf, fonts, accent, mode, info)
    draw_mode_toggle(surf, fonts, mode, accent, toggle_pressed)
    draw_controls(surf, info, presses, accent, playing_eff, connected)
    draw_volume_fader(surf, fonts, accent, info['volume'], vactive)
    if night:
        surf.blit(night_scrim(), (0, 0))
    return surf
