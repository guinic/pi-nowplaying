#!/usr/bin/env python3
"""Ambient Now-Playing display for Pi DSI 800x480 touchscreen.

Features
--------
* AirPlay (shairport-sync) metadata: title / artist / album / cover art.
* Ambient adaptive background: cover art blurred + darkened, full screen.
* Accent colour derived from the cover (artist text, progress, button halo).
* Crossfade transitions between tracks (background + cover + accent).
* Full-screen touch gestures:
    - tap on artwork      -> play / pause
    - swipe left / right  -> next / previous
    - swipe up / down     -> volume up / down
  plus the always-visible bottom transport bar.
* Volume overlay synced with AirPlay volume (pvol) + DACP volume control.
* Live progress bar (interpolated locally between updates).
* Enriched idle screen: large clock, date, and current weather (Open-Meteo).
* Automatic night dimming (software scrim).
* "Lossless 44.1 kHz" quality badge.

Transport commands are sent back to the iPhone via DACP. There is NO local
mpc fallback, so the buttons never touch the moOde playlist.
"""
import os, sys, time, threading, base64 as b64, io, queue, datetime
import logging, signal, math, json, colorsys, subprocess, urllib.request, socket
import re, locale, glob
import xml.etree.ElementTree as ET

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('nowplaying')

# French day/month names on the idle clock (the rest of the UI is French too).
# moOde may not have the locale generated; fall back silently if so.
for _loc in ('fr_FR.UTF-8', 'fr_FR.utf8', 'fr_FR', 'C.UTF-8'):
    try:
        locale.setlocale(locale.LC_TIME, _loc); break
    except locale.Error:
        continue

os.environ['SDL_AUDIODRIVER'] = 'dummy'
os.environ['DISPLAY'] = ''

import pygame

# Layout, palette, thresholds, geometry helpers and user config live in
# npconfig.py; the shared State class + `state` singleton in npstate.py. Both
# sit alongside this file in /opt/nowplaying (the script dir is on sys.path).
from npconfig import *
from npstate import State, state

# ---------------------------------------------------------------------------
# Clean exit on SIGTERM
# ---------------------------------------------------------------------------
def _sigterm(signum, frame):
    log.info("SIGTERM - exiting")
    os._exit(0)

signal.signal(signal.SIGTERM, _sigterm)

# ===========================================================================
# shairport-sync metadata codes
# ===========================================================================
CORE = '636f7265'; SSNC = '73736e63'
MINM = '6d696e6d'; ASAR = '61736172'; ASAL = '6173616c'
PICT = '50494354'; PBEG = '70626567'; PEND = '70656e64'
PRSM = '7072736d'; PAUS = '70617573'; PFLS = '70666c73'
DCON = '64636f6e'; CLIP = '636c6970'; ACRE = '61637265'
DAPO = '6461706f'; PRGR = '70726772'; PVOL = '70766f6c'

SAMPLE_RATE = 44100
PIPE = '/tmp/shairport-sync-metadata'

# ===========================================================================
# DACP remote control
# ===========================================================================
_DACP_CMDS = {
    'play_pause': 'playpause', 'next': 'nextitem', 'prev': 'previtem',
    'volume_up': 'volumeup',   'volume_down': 'volumedown',
}

def send_dacp(action):
    info = state.snapshot()
    ip, port, token = info['client_ip'], info['dacp_port'], info['active_remote']
    cmd = _DACP_CMDS.get(action, action)
    if not (ip and port and token):
        log.warning(f"DACP {action} skipped - no AirPlay source")
        return
    try:
        # Raw HTTP/1.1 keep-alive (NO `Connection: close`): iOS's DACP server
        # rejects close-style requests on /ctrl-int with 400.
        st, _ = _dacp_request(ip, port, f"/ctrl-int/1/{cmd}", token, timeout=3)
        log.info(f"DACP {action} -> {ip}:{port} HTTP {st}")
    except Exception as e:
        log.warning(f"DACP {action} failed: {e}")

def send_volume(direction, steps=3):
    act = 'volume_up' if direction > 0 else 'volume_down'
    for _ in range(max(1, min(8, steps))):
        send_dacp(act)
        time.sleep(0.05)

# ===========================================================================
# Absolute volume (shairport SYSTEM-bus MPRIS for AirPlay, ALSA mixer for BT)
# ---------------------------------------------------------------------------
# MPRIS SetVolume is an *absolute* set the user can preview live on the fader
# (vs the old blind, after-the-fact relative DACP step). NOTE: MPRIS
# PlaybackStatus is NOT usable for pause -- shairport keeps it permanently
# "Playing" for an AirPlay-1 session (pause is a stream flush, not a state
# change). Authoritative play/pause comes from the phone via DACP
# playstatusupdate (see dacp_state_worker below).
# ===========================================================================
SPS_MPRIS_NAME   = 'org.mpris.MediaPlayer2.ShairportSync'
SPS_MPRIS_PATH   = '/org/mpris/MediaPlayer2'
SPS_PLAYER_IFACE = 'org.mpris.MediaPlayer2.Player'

def set_airplay_volume(frac):
    """Absolute AirPlay volume via MPRIS SetVolume (frac 0.0..1.0). shairport
    relays the new level back to the phone, so both stay in sync."""
    frac = max(0.0, min(1.0, float(frac)))
    try:
        import dbus
        obj = dbus.SystemBus().get_object(SPS_MPRIS_NAME, SPS_MPRIS_PATH)
        dbus.Interface(obj, SPS_PLAYER_IFACE).SetVolume(dbus.Double(frac))
        state.set(volume=round(frac * 100))
    except Exception as e:
        log.warning(f"set_airplay_volume {frac:.2f}: {e}")

def set_bt_volume(pct):
    """Absolute Bluetooth volume: drive the hardware PCM mixer to an exact %."""
    pct = max(0, min(100, int(round(pct))))
    try:
        subprocess.run(['amixer', '-c', 'Headphones', 'sset', 'PCM', f'{pct}%'],
                       capture_output=True, timeout=3)
        state.set(volume=pct)
    except Exception as e:
        log.warning(f"set_bt_volume: {e}")

def set_volume_abs(pct, mode):
    if mode == 'bluetooth':
        set_bt_volume(pct)
    else:
        set_airplay_volume(pct / 100.0)

# ===========================================================================
# DACP play-state (authoritative pause for AirPlay)
# ---------------------------------------------------------------------------
# The phone is a DACP server; `playstatusupdate` is a long-poll that returns the
# real player state the instant it changes. We already hold the phone's ip/port
# + Active-Remote token (from the metadata pipe), so we query it directly. The
# reply is a DMAP/TLV blob: container `cmst` -> children incl. `cmsr` (status
# revision, used to long-poll the *next* change) and `caps` (1 byte player
# state: 2=stopped, 3=paused, 4=playing). This is the ONLY reliable pause signal.
# ===========================================================================
def _dmap_kids(buf):
    """Parse one level of DMAP TLV (4-byte ascii tag + 4-byte BE length + data)."""
    out = {}; i = 0; n = len(buf)
    while i + 8 <= n:
        tag = buf[i:i+4]
        ln = int.from_bytes(buf[i+4:i+8], 'big')
        out[tag] = buf[i+8:i+8+ln]
        i += 8 + ln
    return out

def _dacp_request(ip, port, path, token, timeout):
    """Issue a DACP GET over a raw HTTP/1.1 KEEP-ALIVE socket and return
    (status, body_bytes). Critical: NO `Connection: close` header — iOS's DACP
    server answers a `playstatusupdate` long-poll only on a persistent
    connection; `Connection: close` (and HTTP/1.0) make it reply 400. We read
    the headers, honour Content-Length, then close our end ourselves."""
    is6 = ':' in ip
    hh = f"[{ip}]:{port}" if is6 else f"{ip}:{port}"
    req = (f"GET {path} HTTP/1.1\r\nHost: {hh}\r\n"
           f"Active-Remote: {token}\r\n\r\n").encode("ascii")
    fam = socket.AF_INET6 if is6 else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port, 0, 0) if is6 else (ip, port))
        s.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        sep = buf.find(b"\r\n\r\n")
        head = buf[:sep] if sep >= 0 else buf
        body = buf[sep+4:] if sep >= 0 else b""
        status = 0
        try:
            status = int(head.split(b"\r\n", 1)[0].split(b" ")[1])
        except Exception:
            pass
        clen = 0
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                try: clen = int(line.split(b":", 1)[1].strip())
                except Exception: pass
                break
        while len(body) < clen:
            chunk = s.recv(4096)
            if not chunk:
                break
            body += chunk
        return status, body
    finally:
        s.close()

def dacp_state_worker():
    """Best-effort: long-poll the phone's DACP `playstatusupdate` to reflect a
    phone-initiated pause quickly. iOS frequently refuses the query (HTTP 400 on
    /ctrl-int), so this is opportunistic — when it answers we honour `caps`, and
    on repeated refusal we back off quietly (a phone pause then still reflects via
    the pipe `pend` at session teardown). Display-initiated pause is handled
    instantly by the optimistic toggle, independent of this worker."""
    rev = 1; fails = 0; warned = False
    while True:
        info = state.snapshot()
        if info['mode'] != 'airplay' or not (info['client_ip']
                and info['dacp_port'] and info['active_remote']):
            rev = 1; fails = 0; warned = False; time.sleep(1); continue
        ip, port, token = info['client_ip'], info['dacp_port'], info['active_remote']
        path = f"/ctrl-int/1/playstatusupdate?revision-number={rev}"
        try:
            # Long-poll: holds open until the state changes, else ~30 s.
            st, data = _dacp_request(ip, port, path, token, timeout=32)
            if st != 200:
                fails += 1
                if not warned:
                    log.info(f"DACP query unavailable (HTTP {st}); phone pause will "
                             f"reflect via pipe teardown"); warned = True
                time.sleep(min(60, 2 ** fails)); continue
            fails = 0; warned = False
            kids = _dmap_kids(_dmap_kids(data).get(b'cmst', b''))
            cmsr = kids.get(b'cmsr')
            if cmsr and len(cmsr) >= 4:
                rev = int.from_bytes(cmsr[:4], 'big')
            caps = kids.get(b'caps')
            if caps:
                playing = (caps[0] == 4)        # 2=stopped 3=paused 4=playing
                state.set(playing=playing)
                if playing:
                    state.touch_active()
            time.sleep(0.2)                      # guard against a hot loop on instant returns
        except (socket.timeout, TimeoutError):
            fails = 0; continue                 # no change within the long-poll window
        except Exception as e:
            fails += 1
            if not warned:
                log.warning(f"DACP query error: {e}"); warned = True
            rev = 1; time.sleep(min(60, 2 ** fails))

# ===========================================================================
# Bluetooth AVRCP: metadata (MediaPlayer1) + transport control
# ===========================================================================
BT_PLAYER_IFACE = 'org.bluez.MediaPlayer1'
BT_POLL = 1.0   # seconds between AVRCP metadata polls

def _bt_find_player(bus):
    """Return (player_path, device_path) of the first connected AVRCP player,
    or ('', '')."""
    import dbus
    om = dbus.Interface(bus.get_object('org.bluez', '/'),
                        'org.freedesktop.DBus.ObjectManager')
    for path, ifaces in om.GetManagedObjects().items():
        if BT_PLAYER_IFACE in ifaces:
            p = str(path)
            return p, p.rsplit('/', 1)[0]
    return '', ''

def bt_metadata_worker():
    """When in Bluetooth mode, mirror the phone's AVRCP track info into `state`
    so the normal now-playing view renders. No-op in AirPlay mode (the BT
    adapter is powered off then, so there is no player anyway)."""
    bus = None
    player = ''; dev = ''
    had_player = False
    while True:
        time.sleep(BT_POLL)
        if state.snapshot()['mode'] != 'bluetooth':
            player = ''; dev = ''; had_player = False
            continue
        try:
            import dbus
            if bus is None:
                bus = dbus.SystemBus()
            # Only do the (relatively heavy) full ObjectManager scan when we
            # don't already know the player; otherwise read the cached path.
            if not player:
                player, dev = _bt_find_player(bus)
                if not player:
                    if had_player:
                        state.set(bt_player='', bt_dev=''); state.clear_track()
                        had_player = False
                    continue
            props = dbus.Interface(bus.get_object('org.bluez', player),
                                   'org.freedesktop.DBus.Properties')
            allp = props.GetAll(BT_PLAYER_IFACE)   # one round-trip, not three
            status = str(allp.get('Status', ''))
            try:
                position = int(allp.get('Position', 0))
            except Exception:
                position = 0
            track = allp.get('Track', {}) or {}
            title = str(track.get('Title', '') or '')
            artist = str(track.get('Artist', '') or '')
            album = str(track.get('Album', '') or '')
            try:
                dur = int(track.get('Duration', 0))
            except Exception:
                dur = 0
            sr = SAMPLE_RATE
            state.set(title=title, artist=artist, album=album,
                      playing=(status == 'playing'),
                      prog_start=0, prog_cur=int(position * sr / 1000),
                      prog_end=int(dur * sr / 1000), prog_at=time.monotonic(),
                      bt_player=player, bt_dev=dev)
            if title or artist:
                state.touch_active()
            had_player = True
        except Exception as e:
            # The player likely vanished (track skipped, phone disconnected).
            # Drop the cached path so the next tick rescans, and reset the bus.
            log.warning(f"BT metadata: {e}")
            player = ''; dev = ''; bus = None
            if had_player:
                state.set(bt_player='', bt_dev=''); state.clear_track()
                had_player = False

def send_avrcp(action):
    """Transport control for the Bluetooth source via BlueZ MediaPlayer1."""
    player = state.snapshot()['bt_player']
    if not player:
        log.warning(f"AVRCP {action} skipped - no BT source")
        return
    try:
        import dbus
        bus = dbus.SystemBus()
        obj = bus.get_object('org.bluez', player)
        mp = dbus.Interface(obj, BT_PLAYER_IFACE)
        if action == 'play_pause':
            props = dbus.Interface(obj, 'org.freedesktop.DBus.Properties')
            status = str(props.Get(BT_PLAYER_IFACE, 'Status'))
            (mp.Pause if status == 'playing' else mp.Play)()
        elif action == 'next':
            mp.Next()
        elif action == 'prev':
            mp.Previous()
        log.info(f"AVRCP {action} OK")
    except Exception as e:
        log.warning(f"AVRCP {action} failed: {e}")

def _read_pcm_pct():
    try:
        r = subprocess.run(['amixer', '-c', 'Headphones', 'sget', 'PCM'],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r'\[(\d+)%\]', r.stdout)
        return int(m.group(1)) if m else -1
    except Exception:
        return -1

def bt_volume(direction, steps=1):
    """Bluetooth mode volume: drive the hardware PCM mixer (the BT path doesn't
    otherwise touch it). Updates state.volume so the overlay reflects the change."""
    sign = '+' if direction > 0 else '-'
    pct_step = 4 * max(1, min(8, steps))
    try:
        subprocess.run(['amixer', '-c', 'Headphones', '--', 'sset', 'PCM',
                        f'{pct_step}%{sign}'], capture_output=True, timeout=3)
    except Exception as e:
        log.warning(f"bt_volume: {e}")
    pct = _read_pcm_pct()
    if pct >= 0:
        state.set(volume=pct)

# ===========================================================================
# Receiver mode via sudo helper script
# ===========================================================================
def read_mode():
    try:
        r = subprocess.run(['sudo', SETMODE, 'status'],
                           capture_output=True, text=True, timeout=8)
        m = r.stdout.strip()
        return m if m in ('airplay', 'bluetooth') else 'airplay'
    except Exception as e:
        log.warning(f"read_mode: {e}")
        return 'airplay'

def apply_mode(mode):
    log.info(f"Switching receiver mode -> {mode}")
    try:
        subprocess.Popen(['sudo', SETMODE, mode],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning(f"apply_mode {mode}: {e}")

# ===========================================================================
# Metadata reader thread
# ===========================================================================
def decode_text(d):
    return d.decode('utf-8', errors='replace').strip()

def handle_item(t, c, d):
    if t == CORE:
        if   c == MINM: state.set(title=decode_text(d)); state.touch_active()
        elif c == ASAR: state.set(artist=decode_text(d)); state.touch_active()
        elif c == ASAL: state.set(album=decode_text(d))
    elif t == SSNC:
        if c == PICT and d:
            threading.Thread(target=_build_visuals, args=(d,), daemon=True).start()
        elif c in (PBEG, PRSM):
            state.set(playing=True); state.touch_active()
        elif c == PEND:
            state.set(playing=False)
        elif c in (PAUS, PFLS):
            state.set(playing=False)
        elif c == DCON:
            state.set(playing=False, artist='', title='', album='',
                      client_ip='', active_remote='', dacp_port=0,
                      prog_start=0, prog_cur=0, prog_end=0)
        elif c == CLIP:
            ip = decode_text(d); state.set(client_ip=ip)
            log.info(f"DACP client IP = {ip}")
        elif c == ACRE:
            state.set(active_remote=decode_text(d))
            log.info("DACP Active-Remote received")
        elif c == DAPO:
            txt = decode_text(d)
            try: state.set(dacp_port=int(txt)); log.info(f"DACP port = {txt}")
            except ValueError: log.warning(f"DACP port unparseable: {txt!r}")
        elif c == PRGR:
            try:
                a, b, e = (int(x) for x in decode_text(d).split('/')[:3])
                state.set(prog_start=a, prog_cur=b, prog_end=e,
                          prog_at=time.monotonic())
                state.touch_active()
            except Exception:
                pass
        elif c == PVOL:
            try:
                apvol = float(decode_text(d).split(',')[0])
                pct = 0 if apvol <= -144 else round((apvol + 30) / 30 * 100)
                state.set(volume=max(0, min(100, pct)))
            except Exception:
                pass

def metadata_worker():
    while True:
        if not os.path.exists(PIPE):
            time.sleep(3); continue
        try:
            log.info("Opening metadata FIFO")
            buf = ''
            with open(PIPE, 'r', encoding='utf-8', errors='replace') as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        log.info("FIFO EOF, reopening"); break
                    buf += chunk
                    while '</item>' in buf:
                        end = buf.index('</item>') + 7
                        frag = buf[:end]; buf = buf[end:]
                        start = frag.find('<item>')
                        if start < 0: continue
                        try:
                            item = ET.fromstring(frag[start:])
                            t = item.findtext('type', ''); c = item.findtext('code', '')
                            de = item.find('data')
                            d = b64.b64decode(de.text.strip()) \
                                if de is not None and de.text else b''
                        except Exception:
                            continue
                        handle_item(t, c, d)
        except Exception as e:
            log.warning(f"FIFO error: {e}")
        time.sleep(2)

# ===========================================================================
# Visual builder
# ===========================================================================
COVER_MAX = 240

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

def _build_visuals(raw):
    try:
        from PIL import Image as PILImage, ImageFilter, ImageEnhance, ImageDraw
        img = PILImage.open(io.BytesIO(raw)).convert('RGB')
        accent = _vibrant_accent(img)

        # Rounded album art + a soft blurred drop shadow, both built once here
        # (per track) rather than per frame.
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

        # Compatibility fix for Pillow >= 10.0
        try:
            resample_filter = PILImage.Resampling.BILINEAR
        except AttributeError:
            resample_filter = PILImage.BILINEAR

        small = img.resize((100, 60)).filter(ImageFilter.GaussianBlur(8))
        big = small.resize((W, H), resample_filter)
        big = ImageEnhance.Brightness(big).enhance(0.45)
        big = ImageEnhance.Color(big).enhance(1.15)
        # Updated fromstring to frombytes
        bg_surf = pygame.image.frombytes(big.tobytes(), (W, H), 'RGB')

        try: state.visual_q.get_nowait()
        except queue.Empty: pass
        state.visual_q.put({'cover': cover_surf, 'shadow': shadow_surf,
                            'bg': bg_surf, 'accent': accent})
    except Exception as e:
        log.warning(f"Visual build: {e}")

# ---------------------------------------------------------------------------
# Dev preview harness: SIGUSR1 dumps the current frame to /tmp/np_frame.png;
# SIGUSR2 toggles a synthetic now-playing view (fake track + generated cover fed
# through the real visual pipeline) so the player view can be reviewed without a
# live AirPlay/BT source. Both are flag-only in the signal handler; the render
# loop does the actual work. No effect on normal operation.
# ---------------------------------------------------------------------------
_WANT_SHOT = False
_WANT_PREVIEW_TOGGLE = False
_PREVIEW = False

def _inject_preview():
    """Feed a generated cover + fake metadata so the now-playing view renders."""
    try:
        from PIL import Image as PILImage, ImageDraw
        base = PILImage.new('RGB', (2, 2))
        base.putpixel((0, 0), (236, 94, 142)); base.putpixel((1, 0), (92, 124, 246))
        base.putpixel((0, 1), (250, 186, 92)); base.putpixel((1, 1), (54, 200, 178))
        try: rs = PILImage.Resampling.BILINEAR
        except AttributeError: rs = PILImage.BILINEAR
        img = base.resize((600, 600), rs)
        d = ImageDraw.Draw(img, 'RGBA')
        d.ellipse([150, 150, 450, 450], outline=(255, 255, 255, 90), width=10)
        d.ellipse([240, 240, 360, 360], fill=(20, 20, 28, 140))
        bio = io.BytesIO(); img.save(bio, format='PNG')
        _build_visuals(bio.getvalue())
    except Exception as e:
        log.warning(f"Preview build: {e}")
    now = time.monotonic()
    state.set(title='Midnight City', artist='M83',
              album="Hurry Up, We're Dreaming", playing=True, volume=42,
              client_ip='preview', dacp_port=3391, active_remote='1',
              prog_start=0, prog_cur=int(72 * SAMPLE_RATE),
              prog_end=int(244 * SAMPLE_RATE), prog_at=now, last_active=now,
              hw_active=True, hw_rate=44100, hw_bits=16, hw_channels=2)

# ===========================================================================
# Live transmission quality thread
# ---------------------------------------------------------------------------
# Reads the REAL post-decode PCM parameters straight from the ALSA driver
# (/proc/asound/.../hw_params). Reports "closed" when nothing is streaming and
# the concrete rate / bit-depth / channels while audio is flowing -- works for
# both AirPlay and Bluetooth, since both decode down to the same ALSA device.
# When in Bluetooth mode it additionally reads the negotiated A2DP codec from
# BlueZ (hw_params only sees PCM, never the codec). The badge renders from this.
# ===========================================================================
_FMT_BITS = {'S16_LE': 16, 'S16_BE': 16, 'U16_LE': 16,
             'S24_LE': 24, 'S24_BE': 24, 'S24_3LE': 24, 'S24_3BE': 24,
             'S32_LE': 32, 'S32_BE': 32, 'F32_LE': 32}
# A2DP codec id (org.bluez.MediaTransport1 "Codec" byte) -> human name.
_A2DP_CODEC = {0x00: 'SBC', 0x01: 'MP3', 0x02: 'AAC', 0x04: 'ATRAC',
               0xff: 'aptX'}

def _read_hw_params():
    """Return (active, rate, bits, channels) by scanning every playback
    sub-stream. The first one that is open (not "closed") wins."""
    for f in glob.glob('/proc/asound/card*/pcm*p/sub*/hw_params'):
        try:
            with open(f) as fh:
                txt = fh.read()
        except OSError:
            continue
        if not txt or txt.strip() == 'closed':
            continue
        rate = bits = chans = 0
        for line in txt.splitlines():
            if line.startswith('rate:'):
                m = re.search(r'(\d+)', line)
                if m:
                    rate = int(m.group(1))
            elif line.startswith('format:'):
                bits = _FMT_BITS.get(line.split(':', 1)[1].strip(), 0)
            elif line.startswith('channels:'):
                m = re.search(r'(\d+)', line)
                if m:
                    chans = int(m.group(1))
        if rate:
            return True, rate, bits, chans
    return False, 0, 0, 0

def _read_bt_codec():
    """Negotiated A2DP codec name, or '' if no streaming transport exists.
    The MediaTransport1 object only appears while audio is actually flowing."""
    try:
        import dbus
        bus = dbus.SystemBus()
        om = dbus.Interface(bus.get_object('org.bluez', '/'),
                            'org.freedesktop.DBus.ObjectManager')
        for path, ifaces in om.GetManagedObjects().items():
            tr = ifaces.get('org.bluez.MediaTransport1')
            if tr is not None:
                cid = int(tr.get('Codec', -1))
                return _A2DP_CODEC.get(cid, f'codec {cid:#x}' if cid >= 0 else '')
    except Exception:
        pass
    return ''

def hw_quality_worker():
    last = None
    bt_codec = ''
    while True:
        time.sleep(1.2)
        if _PREVIEW:            # screenshot preview injects synthetic hw values
            continue
        try:
            active, rate, bits, chans = _read_hw_params()
            mode = state.snapshot()['mode']
            if mode == 'bluetooth' and active:
                # only re-scan dbus when we don't have a codec yet (cheap path)
                if not bt_codec:
                    bt_codec = _read_bt_codec()
            else:
                bt_codec = ''
            sig = (active, rate, bits, chans, bt_codec)
            if sig != last:
                last = sig
                state.set(hw_active=active, hw_rate=rate, hw_bits=bits,
                          hw_channels=chans, bt_codec=bt_codec)
        except Exception as e:
            log.warning(f"hw quality: {e}")

# ===========================================================================
# Weather thread (Open-Meteo, no API key)
# ===========================================================================
def weather_worker():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}"
           f"&longitude={WEATHER_LON}"
           f"&current=temperature_2m,weather_code,apparent_temperature"
           f"&hourly=temperature_2m,weather_code"
           f"&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
           f"&forecast_days=2&timezone=auto")
    while True:
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                data = json.loads(r.read().decode())
            cur = data.get('current', {})
            daily = data.get('daily', {})
            def _first(k):
                v = daily.get(k)
                return v[0] if isinstance(v, list) and v else None
            def _hhmm(k):
                v = _first(k)
                return v[11:16] if isinstance(v, str) and len(v) >= 16 else None
            # Next few hours: pick the hourly slots strictly after the current
            # hour so the strip shows what's coming, not the hour we're in.
            hourly = data.get('hourly', {})
            htime = hourly.get('time') or []
            htemp = hourly.get('temperature_2m') or []
            hcode = hourly.get('weather_code') or []
            now_h = datetime.datetime.now().replace(minute=0, second=0,
                                                    microsecond=0)
            slots = []
            for i, ts in enumerate(htime):
                try:
                    t = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M')
                except Exception:
                    continue
                if t <= now_h:
                    continue
                if i < len(htemp) and i < len(hcode):
                    slots.append({'h': t.hour, 'temp': htemp[i],
                                  'code': hcode[i]})
                if len(slots) >= 6:
                    break
            state.set(wx_temp=cur.get('temperature_2m'),
                      wx_code=cur.get('weather_code'),
                      wx_feels=cur.get('apparent_temperature'),
                      wx_tmax=_first('temperature_2m_max'),
                      wx_tmin=_first('temperature_2m_min'),
                      wx_sunrise=_hhmm('sunrise'),
                      wx_sunset=_hhmm('sunset'),
                      wx_hourly=slots,
                      wx_at=time.monotonic())
            log.info(f"Weather: {cur.get('temperature_2m')}C code={cur.get('weather_code')} "
                     f"max={_first('temperature_2m_max')} min={_first('temperature_2m_min')}")
            time.sleep(900)
        except Exception as e:
            log.warning(f"Weather: {e}")
            time.sleep(60)

WX_TEXT = {0: 'Ciel clair', 1: 'Plutôt clair', 2: 'Nuageux', 3: 'Couvert',
           45: 'Brouillard', 48: 'Brouillard', 51: 'Bruine', 53: 'Bruine',
           55: 'Bruine', 61: 'Pluie', 63: 'Pluie', 65: 'Forte pluie',
           71: 'Neige', 73: 'Neige', 75: 'Forte neige', 80: 'Averses',
           81: 'Averses', 82: 'Fortes averses', 95: 'Orage', 96: 'Orage',
           99: 'Orage'}

def wx_kind(code):
    if code is None: return None
    if code == 0: return 'sun'
    if code in (1, 2): return 'partly'
    if code == 3: return 'cloud'
    if code in (45, 48): return 'fog'
    if code in (71, 73, 75): return 'snow'
    if code in (95, 96, 99): return 'storm'
    return 'rain'

def zone_for(px, py):
    # Transport taps register on the 3 buttons inside the centered pill only.
    for key, cx in zip(BTN_KEYS, BTN_X):
        if math.hypot(px - cx, py - BTN_Y) <= BTN_R + 16:
            return key
    return None

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

# ===========================================================================
# Vector icons
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

def wx_icon(s, cx, cy, z, kind, accent, night=False):
    sun = (255, 210, 90); cloud = (210, 216, 230); rain = (120, 170, 255)
    if kind in ('sun', 'partly'):
        ox = cx - z*0.3 if kind == 'partly' else cx
        if night:
            # Clear/partly at night -> moon instead of sun.
            _moon(s, ox, cy - z*0.1, z*(0.85 if kind == 'partly' else 1.0))
        else:
            pygame.draw.circle(s, sun, (int(ox), int(cy - z*0.1)), int(z*0.5))
            for a in range(0, 360, 45):
                rad = math.radians(a)
                x1 = ox + math.cos(rad) * z*0.7; y1 = cy - z*0.1 + math.sin(rad) * z*0.7
                x2 = ox + math.cos(rad) * z*0.95; y2 = cy - z*0.1 + math.sin(rad) * z*0.95
                pygame.draw.line(s, sun, (x1, y1), (x2, y2), 2)
    if kind in ('partly', 'cloud', 'fog', 'rain', 'snow', 'storm'):
        cy2 = cy + (z*0.2 if kind != 'cloud' else 0)
        pygame.draw.circle(s, cloud, (int(cx - z*0.35), int(cy2)), int(z*0.4))
        pygame.draw.circle(s, cloud, (int(cx + z*0.35), int(cy2)), int(z*0.4))
        pygame.draw.circle(s, cloud, (int(cx), int(cy2 - z*0.25)), int(z*0.5))
        pygame.draw.rect(s, cloud, pygame.Rect(cx - z*0.7, cy2, z*1.4, z*0.45),
                         border_radius=int(z*0.2))
    if kind in ('rain', 'storm'):
        for dx in (-0.4, 0, 0.4):
            pygame.draw.line(s, rain, (cx + dx*z, cy + z*0.7),
                             (cx + dx*z - z*0.1, cy + z*1.05), 3)
    if kind == 'storm':
        _aa_poly(s, (255, 220, 80), [(cx, cy + z*0.6), (cx - z*0.2, cy + z*1.0),
                 (cx, cy + z*1.0), (cx - z*0.1, cy + z*1.3)])
    if kind == 'snow':
        for dx in (-0.4, 0, 0.4):
            pygame.draw.circle(s, WHITE, (int(cx + dx*z), int(cy + z*0.85)), 3)

# ===========================================================================
# Display init / fonts
# ===========================================================================
def try_disable_blanking():
    for path in ['/sys/class/drm/card0-DSI-1/dpms', '/sys/class/drm/card0-DSI-2/dpms',
                 '/sys/class/graphics/fb0/blank']:
        try:
            with open(path, 'w') as f:
                f.write('On' if 'dpms' in path else '0')
        except Exception:
            pass

# ---- Backlight control for standby (veille) -------------------------------
# The DSI panel exposes /sys/class/backlight/<dev>/brightness, which is writable
# by the 'video' group (our service user is in it) -- so no sudo is needed.
# Writing 0 turns the backlight off; max_brightness turns it back on.
_BL_DIR = None
_BL_MAX = None
def _bl_dir():
    global _BL_DIR
    if _BL_DIR is None:
        _BL_DIR = ''
        try:
            base = '/sys/class/backlight'
            names = sorted(os.listdir(base))
            if names:
                _BL_DIR = os.path.join(base, names[0])
        except Exception:
            pass
    return _BL_DIR

def _bl_max():
    global _BL_MAX
    if _BL_MAX is None:
        _BL_MAX = 255
        d = _bl_dir()
        if d:
            try:
                with open(os.path.join(d, 'max_brightness')) as f:
                    _BL_MAX = int(f.read().strip()) or 255
            except Exception:
                pass
    return _BL_MAX

def set_backlight(on):
    """Turn the panel backlight on/off. Best-effort: silently no-ops if the
    sysfs node is missing or not writable (never crashes the render loop)."""
    d = _bl_dir()
    if not d:
        return
    try:
        with open(os.path.join(d, 'brightness'), 'w') as f:
            f.write(str(_bl_max() if on else 0))
    except Exception:
        pass

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
        if os.path.exists(path):
            try: return pygame.font.Font(path, size)
            except Exception: pass
    return pygame.font.Font(None, size + 10)

_text_cache = {}
def text_surf(font, text, color, max_w):
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

def make_gradient(w, h, top, bot):
    s = pygame.Surface((w, h))
    for y in range(h):
        t = y / max(1, h - 1)
        pygame.draw.line(s, lerp(top, bot, t), (0, y), (w, y))
    return s

# Sky keyframes (hour, top-colour, bottom-colour) for the idle background. The
# gradient drifts from deep night -> warm dawn -> daylight blue -> dusk amber ->
# evening, interpolated by the time of day so the clock screen feels alive.
_SKY = [
    (0.0,  (10, 12, 24),  (20, 22, 42)),
    (5.5,  (22, 22, 46),  (44, 40, 70)),
    (7.5,  (54, 46, 86),  (110, 84, 98)),
    (12.0, (22, 34, 66),  (42, 64, 108)),
    (17.0, (26, 36, 70),  (48, 66, 110)),
    (19.5, (58, 42, 84),  (132, 76, 84)),
    (21.5, (28, 24, 52),  (46, 38, 72)),
    (24.0, (10, 12, 24),  (20, 22, 42)),
]

def _sky_colors(h):
    for i in range(len(_SKY) - 1):
        h0, t0, b0 = _SKY[i]
        h1, t1, b1 = _SKY[i + 1]
        if h0 <= h <= h1:
            f = (h - h0) / (h1 - h0) if h1 > h0 else 0.0
            return lerp(t0, t1, f), lerp(b0, b1, f)
    return _SKY[0][1], _SKY[0][2]

# Idle accent keyframes (hour -> colour) tracking the sky: cool indigo at night,
# warm coral at dawn, sky blue by day, amber at dusk. Used for the clock glow,
# day-arc, pills and hourly highlight so the whole idle screen shares one mood.
_ACC = [
    (0.0,  (96, 120, 215)),
    (6.0,  (240, 142, 120)),
    (8.5,  (96, 165, 240)),
    (17.0, (96, 170, 235)),
    (19.5, (245, 150, 92)),
    (22.0, (122, 122, 212)),
    (24.0, (96, 120, 215)),
]

def idle_accent(h):
    for i in range(len(_ACC) - 1):
        h0, c0 = _ACC[i]
        h1, c1 = _ACC[i + 1]
        if h0 <= h <= h1:
            f = (h - h0) / (h1 - h0) if h1 > h0 else 0.0
            return lerp(c0, c1, f)
    return _ACC[0][1]

def soft_glow(surf, scale=7):
    # Cheap blur: shrink then grow with smooth scaling. Good enough for a halo
    # behind large glyphs without a real Gaussian pass.
    w, h = surf.get_size()
    small = pygame.transform.smoothscale(
        surf, (max(1, w // scale), max(1, h // scale)))
    return pygame.transform.smoothscale(small, (w, h))

def _hm_to_min(s):
    try:
        h, m = s.split(':')
        return int(h) * 60 + int(m)
    except Exception:
        return None

def _moon(s, cx, cy, z, col=(226, 230, 244)):
    # Crescent: a disc with an offset disc carved out (alpha 0 overwrites on the
    # SRCALPHA surface, so no background colour is needed to mask it).
    r = max(3, int(z * 0.62))
    surf = pygame.Surface((r * 2 + 2, r * 2 + 2), pygame.SRCALPHA)
    c = (r + 1, r + 1)
    pygame.draw.circle(surf, col, c, r)
    pygame.draw.circle(surf, (0, 0, 0, 0),
                       (c[0] + int(r * 0.55), c[1] - int(r * 0.28)),
                       int(r * 0.95))
    s.blit(surf, (int(cx) - c[0], int(cy) - c[1]))

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

_idle_grad_cache = {}

def idle_gradient(hour, minute):
    # Bucket by half-hour so the gradient is rebuilt at most ~48x/day, not per
    # frame (each rebuild paints H scanlines).
    key = hour * 2 + (1 if minute >= 30 else 0)
    g = _idle_grad_cache.get(key)
    if g is None:
        top, bot = _sky_colors(hour + minute / 60.0)
        g = make_gradient(W, H, top, bot)
        if len(_idle_grad_cache) > 8:
            _idle_grad_cache.clear()
        _idle_grad_cache[key] = g
    return g

def init_display():
    try: pygame.display.quit()
    except Exception: pass
    forced = os.environ.get('SDL_VIDEODRIVER', '')
    for drv in ([forced] if forced else ['kmsdrm', 'fbdev']):
        os.environ['SDL_VIDEODRIVER'] = drv
        if drv == 'kmsdrm':
            os.environ.setdefault('SDL_VIDEO_KMSDRM_DEVICE', '/dev/dri/card0')
        elif drv == 'fbdev':
            os.environ.setdefault('SDL_FBDEV', '/dev/fb0')
        try:
            pygame.display.init()
            log.info(f"SDL driver: {pygame.display.get_driver()}")
            # The HDMI connector is force-enabled (for the passive HDMI->jack DAC
            # audio), so KMSDRM exposes TWO displays: HDMI (1920x1080, index 0) and
            # the DSI touch panel (800x480). SDL defaults to index 0 = HDMI, which
            # would push the UI out the VGA adapter and leave the DSI on the console.
            # Pin to whichever display matches the panel resolution (the DSI).
            disp_index = 0
            try:
                sizes = [tuple(s) for s in pygame.display.get_desktop_sizes()]
                for i, sz in enumerate(sizes):
                    if sz == (W, H):
                        disp_index = i; break
                log.info(f"displays={sizes} -> DSI index {disp_index}")
            except Exception as e:
                log.warning(f"display enum failed: {e}")
            screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN | pygame.NOFRAME,
                                             24, display=disp_index)
            pygame.mouse.set_visible(False)
            return screen
        except Exception as e:
            log.warning(f"{drv} failed: {e}")
            try: pygame.display.quit()
            except Exception: pass
    return None

# ===========================================================================
# Drawing helpers
# ===========================================================================
def fmt_time(sec):
    sec = max(0, int(sec)); return f"{sec // 60}:{sec % 60:02d}"

def blit_alpha(screen, surf, pos, alpha):
    if alpha >= 255:
        screen.blit(surf, pos)
    elif alpha > 0:
        surf.set_alpha(int(alpha)); screen.blit(surf, pos); surf.set_alpha(255)

# Reusable scroll strips, keyed by (viewport_w, height), so the marquee does not
# allocate a Surface on every frame (only fills + blits into the cached one).
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

# Static, full-surface overlays built once and reused (they never change), so
# the render loop stops re-allocating + re-filling a Surface on every frame.
_BAR_SURF = None
def bar_surf():
    global _BAR_SURF
    if _BAR_SURF is None:
        s = pygame.Surface((W, BAR_H), pygame.SRCALPHA)
        s.fill((10, 12, 22, 165))
        _BAR_SURF = s
    return _BAR_SURF

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
# scrolling-title marquee (30 fps full-scene repaint). Cache by (size, accent)
# so the blur runs ONCE per track and is just blitted thereafter. A colour
# transition (accent lerp) briefly misses the cache -> same cost as before, no
# worse. Bounded dicts so memory never grows.
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
    dur = (info['prog_end'] - info['prog_start']) / SAMPLE_RATE
    if dur <= 0: return
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
    if et: screen.blit(et, (bx, by - 22))
    td = text_surf(fonts['time'], fmt_time(dur), GRAY, W)
    if td: screen.blit(td, (bx + bw - td.get_width(), by - 22))

def _fmt_khz(rate):
    if not rate:
        return ''
    k = rate / 1000.0
    s = f'{k:.1f}'.rstrip('0').rstrip('.')
    return f'{s} kHz'

def draw_badge(screen, fonts, accent, mode, info):
    # Live transmission tag driven by the real ALSA hw_params (state.hw_*) plus,
    # in BT mode, the negotiated A2DP codec. The leading dot is lit (accent) when
    # the device is actually open/streaming and dim when idle. No more hardcoded
    # rates/labels -- it reports exactly what the DAC is being fed right now.
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
    # `pressed` fills it with the accent for a brief tap confirmation (the
    # actual source switch takes 1-2 s, so the user needs immediate feedback).
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
    # Centered rounded "pill" holding the 3 transport buttons (replaces the old
    # full-width bottom bar). Translucent dark fill; an accent hairline border when
    # a source is connected, dim grey otherwise.
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

def draw_bt_icon(s, cx, cy, z, c):
    top, bot = cy - z, cy + z
    midx = cx + z * 0.55
    pts = [(cx, top), (midx, cy - z * 0.5), (cx - z * 0.55, cy + z * 0.5),
           (cx - z * 0.55, cy - z * 0.5), (midx, cy + z * 0.5), (cx, bot)]
    pygame.draw.lines(s, c, False, [(cx, top), (midx, cy - z*0.5),
        (cx - z*0.55, cy + z*0.5)], 3)
    pygame.draw.lines(s, c, False, [(cx - z*0.55, cy - z*0.5),
        (midx, cy + z*0.5), (cx, bot)], 3)
    pygame.draw.line(s, c, (cx, top), (cx, bot), 3)

def draw_pill(screen, font, rect, label, icon_fn, selected, accent):
    surf = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
    if selected:
        pygame.draw.rect(surf, (*accent, 235), (0, 0, rect.w, rect.h),
                         border_radius=rect.h // 2)
        fg = (16, 18, 28)
    else:
        pygame.draw.rect(surf, (255, 255, 255, 22), (0, 0, rect.w, rect.h),
                         border_radius=rect.h // 2)
        pygame.draw.rect(surf, (180, 186, 205, 160), (0, 0, rect.w, rect.h),
                         width=2, border_radius=rect.h // 2)
        fg = (210, 214, 230)
    screen.blit(surf, rect.topleft)
    icy = rect.centery
    txt = font.render(label, True, fg)
    icon_r = 12
    gap = 18
    content_w = icon_r + gap + txt.get_width()
    start = rect.centerx - content_w // 2
    icx = start + icon_r // 2
    icon_fn(screen, icx, icy, icon_r, fg)
    screen.blit(txt, (icx + icon_r // 2 + gap, icy - txt.get_height() // 2))

def _airplay_icon(s, cx, cy, z, c):
    _aa_poly(s, c, [(cx, cy + z * 0.2), (cx - z * 0.7, cy + z), (cx + z * 0.7, cy + z)])
    pygame.draw.arc(s, c, pygame.Rect(cx - z, cy - z, 2 * z, 2 * z),
                    math.radians(35), math.radians(145), 3)

def draw_idle(screen, fonts, now, accent, info, connected, mode):
    # --- Large, light digital clock (numbers, no hands) with a soft accent halo
    # behind the digits for depth. The halo colour tracks the time of day. ---
    cs = fonts['clock'].render(now.strftime('%H:%M'), True, WHITE)
    cr = cs.get_rect(centerx=W // 2, top=10)
    glow = fonts['clock'].render(now.strftime('%H:%M'), True, accent)
    glow = soft_glow(glow, 7); glow.set_alpha(95)
    screen.blit(glow, glow.get_rect(center=cr.center))
    screen.blit(cs, cr)

    # --- "Day arc": progress through the 24 h day. Ticks mark sunrise & sunset
    # and the travelling marker is a little sun (daytime) or moon (night), so the
    # bar reads as the sun's journey across the day rather than a plain meter. ---
    nowmin = now.hour * 60 + now.minute
    frac = (nowmin * 60 + now.second) / 86400.0
    bx0, bx1 = 150, W - 150
    by = cr.bottom + 16
    bw = bx1 - bx0
    srm = _hm_to_min(info.get('wx_sunrise'))
    ssm = _hm_to_min(info.get('wx_sunset'))
    is_day = (srm is not None and ssm is not None and srm <= nowmin < ssm)
    pygame.draw.rect(screen, (50, 54, 78), (bx0, by, bw, 4), border_radius=2)
    fillw = int(bw * frac)
    if fillw > 0:
        pygame.draw.rect(screen, accent, (bx0, by, fillw, 4), border_radius=2)
    # sunrise / sunset ticks
    for m, col in ((srm, (250, 190, 110)), (ssm, (150, 150, 220))):
        if m is not None:
            tx = bx0 + int(bw * (m / 1440.0))
            pygame.draw.line(screen, col, (tx, by - 4), (tx, by + 8), 2)
    # travelling sun / moon marker
    mx = bx0 + fillw
    pygame.draw.circle(screen, (16, 18, 30), (mx, by + 2), 11)
    if is_day:
        pygame.draw.circle(screen, (255, 214, 96), (mx, by + 2), 6)
        for a in range(0, 360, 45):
            rad = math.radians(a)
            pygame.draw.line(screen, (255, 214, 96),
                             (mx + math.cos(rad) * 8, by + 2 + math.sin(rad) * 8),
                             (mx + math.cos(rad) * 10.5, by + 2 + math.sin(rad) * 10.5), 2)
    else:
        _moon(screen, mx, by + 2, 11, (220, 224, 240))

    # --- Wide info band: weekday + date on the left, weather on the right.
    # Using the full width (instead of stacking everything centred) keeps the
    # band short so nothing spills past the clock above. ---
    card = pygame.Rect(60, by + 18, W - 120, 98)
    surf = pygame.Surface((card.w, card.h), pygame.SRCALPHA)
    pygame.draw.rect(surf, (255, 255, 255, 14), surf.get_rect(), border_radius=22)
    pygame.draw.rect(surf, (255, 255, 255, 30), surf.get_rect(),
                     width=1, border_radius=22)
    screen.blit(surf, card.topleft)
    midx = card.centerx
    pygame.draw.line(screen, (72, 76, 104),
                     (midx, card.y + 22), (midx, card.bottom - 22), 1)

    # Left column: weekday banner + date, and (if known) sun up/down times.
    wd = now.strftime('%A'); wd = wd[:1].upper() + wd[1:]
    ws = fonts['day'].render(wd, True, WHITE)
    ds = fonts['date'].render(now.strftime('%d %B'), True, GRAY)
    sr, ss = info.get('wx_sunrise'), info.get('wx_sunset')
    sr_s = fonts['mm'].render(sr, True, GRAY) if (sr and ss) else None
    ss_s = fonts['mm'].render(ss, True, GRAY) if (sr and ss) else None
    lx = card.x + 34
    block_h = ws.get_height() + 2 + ds.get_height()
    if sr_s is not None:
        block_h += 8 + sr_s.get_height()
    ltop = card.centery - block_h // 2
    screen.blit(ws, (lx, ltop))
    dy = ltop + ws.get_height() + 2
    screen.blit(ds, (lx, dy))
    if sr_s is not None:
        sy = dy + ds.get_height() + 8
        cy = sy + sr_s.get_height() // 2
        # up-chevron (sunrise) + time
        pygame.draw.polygon(screen, accent,
                            [(lx, cy + 3), (lx + 8, cy + 3), (lx + 4, cy - 4)])
        screen.blit(sr_s, (lx + 16, sy))
        # down-chevron (sunset) + time, spaced after the first
        dx2 = lx + 16 + sr_s.get_width() + 22
        pygame.draw.polygon(screen, (232, 172, 64),
                            [(dx2, cy - 4), (dx2 + 8, cy - 4), (dx2 + 4, cy + 3)])
        screen.blit(ss_s, (dx2 + 16, sy))

    # Right column: real weather icon + current temp, condition word and the
    # day's min/max.
    kind = wx_kind(info['wx_code'])
    have_wx = kind is not None and info['wx_temp'] is not None
    wx_at = info.get('wx_at') or 0
    stale = (not wx_at) or (time.monotonic() - wx_at > 1800)
    if have_wx:
        ct = DIMTXT if stale else WHITE
        cg = DIMTXT if stale else GRAY
        cur_night = (srm is not None and ssm is not None
                     and not (srm <= nowmin < ssm))
        icx = midx + 40
        wx_icon(screen, icx, card.centery, 26, kind, accent, night=cur_night)
        tx = icx + 44
        tt = fonts['wxtemp'].render(f"{round(info['wx_temp'])}°", True, ct)
        screen.blit(tt, (tx, card.centery - tt.get_height() // 2 - 2))
        # Compact stacked detail column: condition, day min/max, feels-like.
        sx = tx + tt.get_width() + 16
        lines = []
        desc = WX_TEXT.get(info['wx_code'], '')
        if desc:
            lines.append(fonts['wxdesc'].render(desc, True, ct))
        tmin, tmax = info.get('wx_tmin'), info.get('wx_tmax')
        if tmin is not None and tmax is not None:
            lines.append(fonts['mm'].render(
                f"max {round(tmax)}°  min {round(tmin)}°", True, cg))
        feels = info.get('wx_feels')
        if feels is not None:
            lines.append(fonts['mm'].render(
                f"ressenti {round(feels)}°", True, cg))
        if lines:
            th = sum(l.get_height() for l in lines) + 4 * (len(lines) - 1)
            yy = card.centery - th // 2
            for l in lines:
                screen.blit(l, (sx, yy)); yy += l.get_height() + 4
    else:
        msg = fonts['wxdesc'].render('Météo indisponible', True, DIMTXT)
        mr = msg.get_rect(); mr.center = ((midx + card.right) // 2, card.centery)
        screen.blit(msg, mr)
    if stale:
        pygame.draw.circle(screen, (232, 172, 64),
                           (card.right - 18, card.y + 18), 5)

    # --- Source pills ---
    draw_pill(screen, fonts['pill'], PILL_AIRPLAY, 'AirPlay', _airplay_icon,
              mode == 'airplay', accent)
    draw_pill(screen, fonts['pill'], PILL_BT, 'Bluetooth', draw_bt_icon,
              mode == 'bluetooth', accent)
    if mode == 'bluetooth':
        hint = fonts['hint'].render('Découvrable – appairez depuis votre téléphone',
                                    True, DIMTXT)
        screen.blit(hint, hint.get_rect(centerx=W // 2, top=PILL_Y + PILL_H + 6))


def draw_hourly(screen, fonts, info, accent):
    # Bottom-band mini-forecast: the next few hours as icon + temperature
    # columns. Fills the space the (inert, no-track) transport pill used to
    # occupy on the idle screen.
    slots = (info.get('wx_hourly') or [])[:6]
    band = pygame.Rect(PAD, CONT_H + 8, W - 2 * PAD, BAR_H - 16)
    n = len(slots)
    surf = pygame.Surface((band.w, band.h), pygame.SRCALPHA)
    pygame.draw.rect(surf, (255, 255, 255, 12), surf.get_rect(), border_radius=20)
    pygame.draw.rect(surf, (255, 255, 255, 26), surf.get_rect(),
                     width=1, border_radius=20)
    if n > 1:
        cwf = band.w / n
        for i in range(1, n):
            x = int(cwf * i)
            pygame.draw.line(surf, (255, 255, 255, 18),
                             (x, 16), (x, band.h - 16), 1)
    screen.blit(surf, band.topleft)
    if n == 0:
        msg = fonts['wxdesc'].render('Prévisions indisponibles', True, DIMTXT)
        screen.blit(msg, msg.get_rect(center=band.center))
        return
    srm = _hm_to_min(info.get('wx_sunrise'))
    ssm = _hm_to_min(info.get('wx_sunset'))
    cw = band.w / n
    for i, s in enumerate(slots):
        cx = int(band.x + cw * (i + 0.5))
        first = (i == 0)
        hl = fonts['badge'].render(f"{s['h']:02d}h", True,
                                   accent if first else GRAY)
        screen.blit(hl, hl.get_rect(centerx=cx, top=band.y + 9))
        kind = wx_kind(s['code'])
        if kind:
            smin = s['h'] * 60
            night = (srm is not None and ssm is not None
                     and not (srm <= smin < ssm))
            wx_icon(screen, cx, band.y + 41, 13, kind, accent, night=night)
        if s['temp'] is not None:
            tl = fonts['mm'].render(f"{round(s['temp'])}°", True,
                                    WHITE if first else GRAY)
            screen.blit(tl, tl.get_rect(centerx=cx, bottom=band.bottom - 9))

# ===========================================================================
# Gesture recognition
# ===========================================================================
def classify_gesture(dx, dy, dist, dt, x0, y0):
    if dist < 28 and dt < 600:
        key = zone_for(x0, y0)
        if key:
            return ({'prev': 'prev', 'play': 'play_pause', 'next': 'next'}[key], key)
        return ('play_pause', 'play')
    if abs(dx) > abs(dy) and abs(dx) > 60:
        return ('next', 'next') if dx < 0 else ('prev', 'prev')
    if abs(dy) > 55:
        return ('vol_up' if dy < 0 else 'vol_down', None)
    return (None, None)

# ===========================================================================
# Main render loop
# ===========================================================================
def render_loop():
    global _WANT_SHOT, _WANT_PREVIEW_TOGGLE, _PREVIEW
    pygame.init(); pygame.font.init()
    screen = None; fonts = {}; gradient = None
    presses = {}; last_blank = 0
    cur = {'cover': None, 'bg': None, 'accent': (88, 166, 255)}
    prev = None; trans_t0 = 0
    press_start = None
    vol_drag = None; vol_preview = -1
    vol_until = 0; vol_shown = 0
    show_np = False; playing_eff = False; last_sig = None
    track_t0 = pygame.time.get_ticks(); last_track = None; marquee_active = False
    last_active_ms = pygame.time.get_ticks(); standby = False; prev_connected = False
    mode = read_mode(); last_show_np = False
    log.info(f"Initial receiver mode: {mode}")
    state.set(mode=mode)
    apply_mode(mode)
    
    while True:
        if screen is None:
            try_disable_blanking()
            screen = init_display()
            if screen is None:
                log.error("No display - retry 5s"); time.sleep(5); continue
            fonts = {
                'clock': load_font(120, weight='Light', display=True),
                'day': load_font(30, weight='SemiBold'),
                'mm': load_font(19, weight='Medium'),
                'date': load_font(24, weight='Medium'),
                'idle': load_font(26, weight='Regular'),
                'title': load_font(44, weight='Bold', display=True),
                'artist': load_font(30, weight='Medium'),
                'album': load_font(23, weight='Regular'),
                'time': load_font(18, weight='Medium'),
                'badge': load_font(15, weight='SemiBold'),
                'vol': load_font(24, weight='SemiBold'),
                'wxtemp': load_font(44, weight='SemiBold', display=True),
                'wxdesc': load_font(20, weight='Regular'),
                'pill': load_font(22, weight='SemiBold'),
                'hint': load_font(16, weight='Regular'),
            }
            gradient = make_gradient(W, H, BG_TOP, BG_BOT)
            last_sig = None   # force a full repaint onto the fresh framebuffer
            set_backlight(True); standby = False
            last_active_ms = pygame.time.get_ticks()
            log.info("Display initialised")

        try:
            now_ms = pygame.time.get_ticks()
            # Event-driven pacing: block until a touch OR a time-based deadline,
            # instead of busy-spinning. Touch is handled the instant it arrives;
            # the timeout only sets how often we wake for time-based redraws.
            #   first paint / animating -> short; playing -> slow progress tick;
            #   idle -> ~1 s (just to catch the minute rolling over).
            if standby:
                wait_ms = 1000            # panel dark: idle slowly (touch still wakes instantly)
            elif last_sig is None:
                wait_ms = 1
            elif prev is not None or now_ms < vol_until or vol_drag is not None:
                wait_ms = 33              # transition / volume -> 30 fps (brief, interactive)
            elif marquee_active:
                wait_ms = 50              # scrolling text -> 20 fps (smooth enough, ~40% fewer frames)
            elif show_np and playing_eff and (now_ms - track_t0) < EQ_ANIM_MS:
                wait_ms = 120              # fresh track: animate EQ bars smoothly
            elif show_np and playing_eff:
                wait_ms = 1000             # settled: ~1 fps, just tick the progress bar
            else:
                wait_ms = 1000
            ev0 = pygame.event.wait(wait_ms)
            events = [] if ev0.type == pygame.NOEVENT else [ev0]
            events += pygame.event.get()
            input_redraw = False
            for ev in events:
                if ev.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN,
                               pygame.MOUSEBUTTONUP, pygame.FINGERUP):
                    input_redraw = True
                if ev.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
                    px = int(ev.x * W) if ev.type == pygame.FINGERDOWN else ev.pos[0]
                    py = int(ev.y * H) if ev.type == pygame.FINGERDOWN else ev.pos[1]
                    # Volume fader grab (now-playing view only): start a live drag
                    # and jump to the touched level immediately.
                    if last_show_np and vol_hit(px, py):
                        v = y_to_vol(py)
                        vol_drag = {'last': now_ms, 'sent': v}
                        vol_preview = v; press_start = None
                        state.set(vol_dragging=True)
                        threading.Thread(target=set_volume_abs, args=(v, mode),
                                         daemon=True).start()
                        input_redraw = True
                    else:
                        press_start = (px, py, now_ms)
                elif ev.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION):
                    if vol_drag is not None:
                        px = int(ev.x * W) if ev.type == pygame.FINGERMOTION else ev.pos[0]
                        py = int(ev.y * H) if ev.type == pygame.FINGERMOTION else ev.pos[1]
                        v = y_to_vol(py); vol_preview = v; input_redraw = True
                        # Absolute set, throttled to ~11/s so D-Bus/amixer keeps up.
                        if v != vol_drag['sent'] and (now_ms - vol_drag['last']) >= 90:
                            vol_drag['last'] = now_ms; vol_drag['sent'] = v
                            threading.Thread(target=set_volume_abs, args=(v, mode),
                                             daemon=True).start()
                elif ev.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP):
                    if vol_drag is not None:
                        px = int(ev.x * W) if ev.type == pygame.FINGERUP else ev.pos[0]
                        py = int(ev.y * H) if ev.type == pygame.FINGERUP else ev.pos[1]
                        v = y_to_vol(py); vol_preview = v
                        threading.Thread(target=set_volume_abs, args=(v, mode),
                                         daemon=True).start()
                        vol_drag = None; state.set(vol_dragging=False)
                        vol_until = now_ms + VOL_SHOW_MS; vol_shown = v
                        input_redraw = True
                        continue
                    if press_start:
                        px = int(ev.x * W) if ev.type == pygame.FINGERUP else ev.pos[0]
                        py = int(ev.y * H) if ev.type == pygame.FINGERUP else ev.pos[1]
                        x0, y0, t0 = press_start; press_start = None
                        dx, dy = px - x0, py - y0
                        dist = math.hypot(dx, dy); dt = now_ms - t0
                        # Mode toggle in the now-playing view: switch source even
                        # while a track is playing (big pills only show when idle).
                        if last_show_np and dist < 28 and MODE_TOGGLE.collidepoint(x0, y0):
                            new_mode = 'bluetooth' if mode == 'airplay' else 'airplay'
                            mode = new_mode
                            presses['mode'] = now_ms   # tap-confirmation flash
                            state.set(mode=new_mode); state.clear_track()
                            threading.Thread(target=apply_mode, args=(new_mode,), daemon=True).start()
                            continue
                        if last_show_np is False and dist < 28:
                            if PILL_AIRPLAY.collidepoint(x0, y0) and mode != 'airplay':
                                mode = 'airplay'
                                state.set(mode='airplay'); state.clear_track()
                                threading.Thread(target=apply_mode, args=('airplay',), daemon=True).start()
                                continue
                            if PILL_BT.collidepoint(x0, y0) and mode != 'bluetooth':
                                mode = 'bluetooth'
                                state.set(mode='bluetooth'); state.clear_track()
                                threading.Thread(target=apply_mode, args=('bluetooth',), daemon=True).start()
                                continue
                        action, key = classify_gesture(dx, dy, dist, dt, x0, y0)
                        if action:
                            if key and (now_ms - presses.get(key, -9999)) < PRESS_MS:
                                pass
                            else:
                                if key: presses[key] = now_ms
                                if action == 'vol_up':
                                    tgt = bt_volume if mode == 'bluetooth' else send_volume
                                    threading.Thread(target=tgt, args=(1, 3), daemon=True).start()
                                    vol_until = now_ms + VOL_SHOW_MS
                                elif action == 'vol_down':
                                    tgt = bt_volume if mode == 'bluetooth' else send_volume
                                    threading.Thread(target=tgt, args=(-1, 3), daemon=True).start()
                                    vol_until = now_ms + VOL_SHOW_MS
                                elif mode == 'bluetooth':
                                    threading.Thread(target=send_avrcp, args=(action,), daemon=True).start()
                                else:
                                    if action == 'play_pause':
                                        # Optimistic feedback: we are the one issuing
                                        # the pause/play, so flip our state NOW — the
                                        # button + timeline update instantly instead of
                                        # waiting on an (unreliable) DACP query.
                                        np = not state.snapshot()['playing']
                                        state.set(playing=np)
                                        if np: state.touch_active()
                                    threading.Thread(target=send_dacp, args=(action,), daemon=True).start()
                elif ev.type == pygame.QUIT:
                    log.warning("QUIT ignored")

            if _WANT_PREVIEW_TOGGLE:
                _WANT_PREVIEW_TOGGLE = False
                _PREVIEW = not _PREVIEW
                if _PREVIEW:
                    _inject_preview()
                    set_backlight(True); standby = False
                    last_active_ms = now_ms
                else:
                    state.clear_track()
                    state.set(client_ip='', dacp_port=0, active_remote='')
                last_sig = None

            try:
                bundle = state.visual_q.get_nowait()
                prev = cur; cur = bundle; trans_t0 = now_ms
            except queue.Empty:
                pass

            if now_ms - last_blank > 30000:
                try_disable_blanking(); last_blank = now_ms

            info = state.snapshot()
            now = datetime.datetime.now()
            mono = time.monotonic()
            # Reset the marquee scroll whenever the track changes so a new title
            # starts paused at the left edge.
            track_key = (info['title'], info['artist'], info['album'])
            if track_key != last_track:
                last_track = track_key
                track_t0 = now_ms
            if mode == 'bluetooth':
                connected = bool(info['bt_player'])
            else:
                connected = bool(info['client_ip'] and info['dacp_port'] and info['active_remote'])

            if info['volume'] >= 0 and info['volume'] != vol_shown:
                vol_until = now_ms + VOL_SHOW_MS
                vol_shown = info['volume']

            # Play/pause is authoritative from explicit shairport events
            # (pbeg/prsm -> playing, pfls/paus/pend -> stopped). We must NOT gate
            # it on how recently a `prgr` (progress) item arrived: shairport emits
            # progress only every few seconds (sometimes once per track), and the
            # bar is meant to interpolate locally between updates. The old
            # PRGR_STALE freshness gate made the bar advance ~2 s then freeze and
            # the play/pause icon stick on the wrong glyph.
            playing_eff = info['playing']
            have_track = bool(info['title'] or info['artist'])
            recent = (mono - info['last_active']) < IDLE_SECS
            show_np = have_track and (playing_eff or recent)

            tp = 1.0 if not prev else min(1.0, (now_ms - trans_t0) / TRANS_MS)
            if tp >= 1.0: prev = None
            accent = cur['accent'] if not prev else lerp(prev['accent'], cur['accent'], tp)

            minute = now.strftime('%H:%M')
            press_active = any((now_ms - t) < PRESS_MS for t in presses.values())

            # ---- standby (veille): sleep the backlight when truly idle --------
            # "Active" = a touch just happened, music is actively playing, a NEW
            # source just connected (rising edge -- so an idle phone that merely
            # keeps an AirPlay session open does NOT keep the panel lit forever),
            # or the volume overlay is up. We deliberately do NOT count the
            # lingering now-playing view (show_np stays true up to IDLE_SECS after
            # playback) nor a steady `connected`, so the panel sleeps once paused.
            connect_edge = connected and not prev_connected
            prev_connected = connected
            if STANDBY_SECS > 0:
                active = (input_redraw or playing_eff or connect_edge
                          or now_ms < vol_until or press_active)
                if active:
                    last_active_ms = now_ms
                want_standby = (now_ms - last_active_ms) > STANDBY_SECS * 1000
                if want_standby and not standby:
                    standby = True; set_backlight(False); marquee_active = False
                    log.info("Display standby (idle)")
                elif not want_standby and standby:
                    standby = False; set_backlight(True)
                    last_sig = None   # force a full repaint on wake
                    log.info("Display wake")

            # ---- decide whether anything visible actually changed ----
            sig = (show_np, info['title'], info['artist'], info['album'],
                   playing_eff, vol_shown, mode, connected, accent,
                   info['wx_code'],
                   None if info['wx_temp'] is None else round(info['wx_temp']),
                   info['hw_active'], info['hw_rate'], info['hw_bits'],
                   info['bt_codec'],
                   None if show_np else minute)
            need = (input_redraw or prev is not None or now_ms < vol_until
                    or press_active or sig != last_sig or vol_drag is not None
                    or _WANT_SHOT
                    or (show_np and playing_eff)
                    or (show_np and marquee_active))
            last_sig = sig

            if need and not standby:
                if show_np and cur['bg'] is not None:
                    if prev and prev['bg'] is not None:
                        screen.blit(prev['bg'], (0, 0))
                        blit_alpha(screen, cur['bg'], (0, 0), tp * 255)
                    else:
                        screen.blit(cur['bg'], (0, 0))
                elif show_np:
                    screen.blit(gradient, (0, 0))
                else:
                    screen.blit(idle_gradient(now.hour, now.minute), (0, 0))

                if show_np:
                    screen.blit(vignette(), (0, 0))

                if show_np:
                    cover = cur['cover']
                    if prev and prev['cover'] is not None and tp < 1.0:
                        draw_cover(screen, prev['cover'], prev.get('shadow'), 255)
                        tx = draw_cover(screen, cover, cur.get('shadow'), tp * 255, accent) if cover else PAD
                    else:
                        tx = draw_cover(screen, cover, cur.get('shadow'), 255, accent) if cover else PAD
                    tw = W - tx - PAD
                    ty = max(PAD, (CONT_H - 170) // 2)
                    age = now_ms - track_t0
                    anim = False
                    # Title first (largest), then artist (accent), then album --
                    # each scrolls as a marquee when it overflows the text column.
                    for fk, val, color in [('title', info['title'], WHITE),
                                           ('artist', info['artist'], accent),
                                           ('album', info['album'], GRAY)]:
                        fs = full_text_surf(fonts[fk], val, color)
                        if fs:
                            anim = draw_text_row(screen, fs, tx, ty, tw, age) or anim
                            ty += fs.get_height() + ROW_GAP
                    marquee_active = anim
                    # Equalizer "now playing" mark under the album line. To spare
                    # the Pi 3A+ CPU (which it needs for audio interpolation), the
                    # bars only ANIMATE for a few seconds after a track change,
                    # then settle into a static glyph -- so we stop forcing 8 fps
                    # repaints for the rest of the song.
                    if playing_eff:
                        eq_anim = (now_ms - track_t0) < EQ_ANIM_MS
                        for i in range(4):
                            if eq_anim:
                                ph = 7 + int(11 * (0.5 + 0.5 * math.sin(
                                    now_ms / 170.0 + i * 1.15)))
                            else:
                                ph = (10, 18, 13, 7)[i]   # static resting heights
                            pygame.draw.rect(screen, accent,
                                             (tx + i * 9, ty + 4 + (18 - ph), 5, ph),
                                             border_radius=2)
                    draw_progress(screen, fonts, info, accent, playing_eff)
                    draw_badge(screen, fonts, accent, mode, info)
                    toggle_pressed = (now_ms - presses.get('mode', -9999)) < PRESS_MS
                    draw_mode_toggle(screen, fonts, mode, accent, toggle_pressed)
                    draw_controls(screen, info, presses, accent, playing_eff,
                                  connected)
                else:
                    marquee_active = False
                    iaccent = idle_accent(now.hour + now.minute / 60.0)
                    draw_idle(screen, fonts, now, iaccent, info, connected, mode)
                    draw_hourly(screen, fonts, info, iaccent)

                # Persistent right-edge volume fader (now-playing view only): the
                # swipe target is always visible; the % readout shows while the
                # user is dragging or just after a change.
                if show_np:
                    vdisp = vol_preview if vol_drag is not None else info['volume']
                    vactive = (vol_drag is not None) or (now_ms < vol_until)
                    draw_volume_fader(screen, fonts, accent, vdisp, vactive)

                hr = now.hour
                if hr >= NIGHT_START or hr < NIGHT_END:
                    screen.blit(night_scrim(), (0, 0))

                pygame.display.update()
                last_show_np = show_np

                if _WANT_SHOT:
                    _WANT_SHOT = False
                    try:
                        shot = pygame.Surface((W, H)); shot.blit(screen, (0, 0))
                        pygame.image.save(shot, '/tmp/np_frame.png')
                        log.info("Screenshot -> /tmp/np_frame.png")
                    except Exception as e:
                        log.warning(f"Screenshot failed: {e}")

        except Exception as e:
            log.error(f"Render error: {e} - reinit")
            try: pygame.display.quit()
            except Exception: pass
            screen = None; time.sleep(2); continue

# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == '__main__':
    log.info("Starting nowplaying")
    # Restore the backlight on shutdown so a stop/restart while in standby never
    # leaves the panel dark.
    def _restore_bl(signum, frame):
        set_backlight(True)
        os._exit(0)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try: signal.signal(_sig, _restore_bl)
        except Exception: pass
    # Dev harness: SIGUSR1 = screenshot, SIGUSR2 = toggle now-playing preview.
    def _on_usr1(s, f):
        global _WANT_SHOT; _WANT_SHOT = True
    def _on_usr2(s, f):
        global _WANT_PREVIEW_TOGGLE; _WANT_PREVIEW_TOGGLE = True
    try: signal.signal(signal.SIGUSR1, _on_usr1)
    except Exception: pass
    try: signal.signal(signal.SIGUSR2, _on_usr2)
    except Exception: pass
    threading.Thread(target=metadata_worker, name='metadata', daemon=True).start()
    threading.Thread(target=bt_metadata_worker, name='btmeta', daemon=True).start()
    threading.Thread(target=dacp_state_worker, name='dacpstate', daemon=True).start()
    threading.Thread(target=weather_worker, name='weather', daemon=True).start()
    threading.Thread(target=hw_quality_worker, name='hwquality', daemon=True).start()
    while True:
        try:
            render_loop()
        except Exception as e:
            log.error(f"render_loop crashed: {e} - restart 3s")
            time.sleep(3)