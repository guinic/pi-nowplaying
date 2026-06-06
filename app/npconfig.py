"""Layout, palette, thresholds and user configuration for the now-playing
display. Pure data + a couple of geometry helpers — no app logic, no threads.
Imported wholesale (`from npconfig import *`) by nowplaying.py."""
import pygame

# ---------------------------------------------------------------------------
# User configuration
# ---------------------------------------------------------------------------
WEATHER_LAT  = 50.63          # Liege
WEATHER_LON  = 5.57
NIGHT_START  = 22             # hour to begin night dimming
NIGHT_END    = 7              # hour to end night dimming
NIGHT_DIM    = 110            # 0..255 black scrim alpha at night
SETMODE      = '/opt/nowplaying/setmode.sh'   # AirPlay <-> Bluetooth switch

# ---------------------------------------------------------------------------
# Layout & palette
# ---------------------------------------------------------------------------
W, H   = 800, 480
BAR_H  = 96
CONT_H = H - BAR_H
WHITE  = (240, 242, 250)
GRAY   = (176, 182, 200)
DIMTXT = (120, 126, 148)
BG_TOP = (16, 18, 30)
BG_BOT = (26, 29, 48)
SCRIM  = (0, 0, 0)
PAD    = 30

BTN_R   = 34
BTN_Y   = CONT_H + BAR_H // 2
BTN_X   = [W // 2 - 132, W // 2, W // 2 + 132]   # inside the centered control pill
BTN_KEYS = ['prev', 'play', 'next']
# Centered rounded "pill" that holds the 3 transport buttons.
PILLBAR_W, PILLBAR_H = 392, 92
PILLBAR = pygame.Rect(W // 2 - PILLBAR_W // 2, BTN_Y - PILLBAR_H // 2,
                      PILLBAR_W, PILLBAR_H)
# Vertical volume fader on the right edge of the now-playing view.
VOL_FADER = pygame.Rect(W - 26, 118, 9, CONT_H - 118 - 40)
VOL_HIT_X = W - 70         # left edge of the (generous) fader touch zone
VOL_HIT_PAD = 26           # vertical slop above/below the track for easy grabs


def vol_hit(px, py):
    # Keep the top of the touch zone below the mode-toggle button (bottom ~102).
    top = max(VOL_FADER.top - VOL_HIT_PAD, 108)
    return px >= VOL_HIT_X and top <= py <= (VOL_FADER.bottom + VOL_HIT_PAD)


def y_to_vol(py):
    frac = (VOL_FADER.bottom - py) / VOL_FADER.h
    return int(round(max(0.0, min(1.0, frac)) * 100))


PRESS_MS   = 220
PRGR_STALE = 3.0
IDLE_SECS  = 900
TRANS_MS   = 500           # crossfade duration
VOL_SHOW_MS = 1600
EQ_ANIM_MS = 6000          # EQ bars animate this long after a track change, then settle
# Marquee scrolling for long title/artist/album rows.
MARQUEE_SPEED = 42         # px / second scroll speed
MARQUEE_GAP   = 70         # px gap between the two looping copies
MARQUEE_PAUSE = 1100       # ms to hold at the start so the beginning is readable
ROW_GAP       = 12         # px vertical gap between the text rows
# Standby (veille): after this many seconds with no touch, no playback and no
# AirPlay/Bluetooth connection, the panel backlight is switched OFF. Any touch --
# or a new AirPlay/BT connection or playback -- instantly wakes it. 0 disables.
STANDBY_SECS = 7200

# Idle-screen mode selector pills (AirPlay / Bluetooth)
PILL_W, PILL_H, PILL_GAP = 196, 52, 24
PILL_Y = 300
PILL_AIRPLAY = pygame.Rect(W // 2 - PILL_W - PILL_GAP // 2, PILL_Y, PILL_W, PILL_H)
PILL_BT      = pygame.Rect(W // 2 + PILL_GAP // 2,          PILL_Y, PILL_W, PILL_H)
# Compact "switch source" chip shown in the now-playing view (top-right).
MODE_TOGGLE = pygame.Rect(W - PAD - 92, 62, 92, 40)
