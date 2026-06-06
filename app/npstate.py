"""Shared playback/UI state for the now-playing display.

A single `State` instance (`state`) is updated by the metadata/Bluetooth/DACP/
weather worker threads and read by the render loop. All access goes through a
lock; `snapshot()` returns a plain dict so the render loop never holds the lock
while drawing.
"""
import threading, time, queue


class State:
    def __init__(self):
        self._lock = threading.Lock()
        self.artist = self.title = self.album = ''
        self.playing = False
        self.client_ip = self.active_remote = ''
        self.dacp_port = 0
        self.prog_start = self.prog_cur = self.prog_end = 0
        self.prog_at = 0.0
        self.last_active = 0.0
        self.volume = -1
        self.vol_dragging = False       # finger currently on the volume fader
        self.wx_temp = None
        self.wx_code = None
        self.wx_feels = None           # apparent ("feels like") temperature
        self.wx_tmin = None            # forecast daily min for today
        self.wx_tmax = None            # forecast daily max for today
        self.wx_sunrise = None         # "HH:MM" local sunrise for today
        self.wx_sunset = None          # "HH:MM" local sunset for today
        self.wx_hourly = []            # next hours: [{'h':int,'temp':float,'code':int}]
        self.wx_at = 0.0                # monotonic time of last successful fetch
        self.mode = 'airplay'          # current receiver mode (airplay|bluetooth)
        self.bt_player = ''            # BlueZ MediaPlayer1 path of the BT source
        self.bt_dev = ''               # BlueZ device path of the BT source
        self.bt_codec = ''             # negotiated A2DP codec name (SBC/AAC/aptX/...)
        self.hw_active = False         # ALSA playback device currently open (streaming)
        self.hw_rate = 0               # live sample rate in Hz from hw_params (e.g. 44100)
        self.hw_bits = 0               # live bit depth from hw_params (16/24/32)
        self.hw_channels = 0           # live channel count from hw_params
        self.visual_q = queue.Queue(maxsize=1)

    def set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def touch_active(self):
        with self._lock:
            self.last_active = time.monotonic()

    def clear_track(self):
        # Wipe track/progress so the player view collapses back to the idle
        # screen. Used on mode changes and when the BT source goes away.
        with self._lock:
            self.artist = self.title = self.album = ''
            self.playing = False
            self.prog_start = self.prog_cur = self.prog_end = 0
            self.prog_at = 0.0

    def snapshot(self):
        with self._lock:
            return dict(
                artist=self.artist, title=self.title, album=self.album,
                playing=self.playing, client_ip=self.client_ip,
                active_remote=self.active_remote, dacp_port=self.dacp_port,
                prog_start=self.prog_start, prog_cur=self.prog_cur,
                prog_end=self.prog_end, prog_at=self.prog_at,
                last_active=self.last_active, volume=self.volume,
                vol_dragging=self.vol_dragging,
                wx_temp=self.wx_temp, wx_code=self.wx_code, wx_feels=self.wx_feels,
                wx_tmin=self.wx_tmin, wx_tmax=self.wx_tmax,
                wx_sunrise=self.wx_sunrise, wx_sunset=self.wx_sunset,
                wx_hourly=self.wx_hourly, wx_at=self.wx_at,
                mode=self.mode, bt_player=self.bt_player, bt_dev=self.bt_dev,
                bt_codec=self.bt_codec, hw_active=self.hw_active,
                hw_rate=self.hw_rate, hw_bits=self.hw_bits,
                hw_channels=self.hw_channels)


state = State()
