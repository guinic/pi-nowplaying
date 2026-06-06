"""
btvolume.py - Bluetooth A2DP volume control done right (CHANTIER 1.4).

THE PROBLEM
-----------
The legacy app set Bluetooth volume with:

    amixer -c Headphones sset PCM <pct>%

But on this unit the audio leaves over HDMI/IEC958 to an external DAC; the
on-board "Headphones" PWM jack (card0) is bypassed. So that mixer is a *muted
card* -- moving it changes nothing the listener hears. BT volume was a no-op.

THE FIX
-------
Drive the volume where it actually lives for an A2DP SINK:

  1. AVRCP absolute volume via BlueZ `org.bluez.MediaTransport1.Volume`
     (uint16, 0..127). Writing it makes BlueZ -- the AVRCP target -- send a
     volume-changed notification to the phone (the controller). The phone's
     own volume HUD follows, and the level is applied to the stream we render.
     This is the exact BT analogue of AirPlay's MPRIS `SetVolume`.

  2. Fallback: bluez-alsa software volume via `bluealsactl` (attenuates the
     decoded PCM) for peers that don't negotiate AVRCP absolute volume.

The D-Bus / subprocess access sits behind a small `backend` object so this
module imports and unit-tests with zero hardware (inject a fake backend).
"""

import shutil
import subprocess

VOL_MAX = 127  # AVRCP / A2DP absolute-volume full-scale

BLUEZ = "org.bluez"
TRANSPORT_IFACE = "org.bluez.MediaTransport1"
DEVICE_IFACE = "org.bluez.Device1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"


def frac_to_avrcp(frac):
    """[0.0, 1.0]  ->  integer 0..127."""
    frac = max(0.0, min(1.0, float(frac)))
    return int(round(frac * VOL_MAX))


def avrcp_to_frac(raw):
    """0..127  ->  [0.0, 1.0]."""
    try:
        return max(0.0, min(1.0, int(raw) / VOL_MAX))
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Default D-Bus backend (real hardware). Tests inject a fake with the same API.
# ===========================================================================
class _DBusBackend:
    """Thin adapter over python-dbus. Lazily connects to the system bus; every
    call is best-effort and returns None / [] on failure rather than raising."""

    def __init__(self):
        self._bus = None

    def _bus_obj(self):
        if self._bus is None:
            import dbus  # imported lazily so the module loads on dev machines
            self._bus = dbus.SystemBus()
        return self._bus

    def managed_objects(self):
        """{object_path: {iface: {prop: value}}} for the whole BlueZ tree."""
        try:
            bus = self._bus_obj()
            om = bus.get_object(BLUEZ, "/")
            import dbus
            mgr = dbus.Interface(om, OM_IFACE)
            return mgr.GetManagedObjects()
        except Exception:
            return {}

    def get_property(self, path, iface, prop):
        try:
            bus = self._bus_obj()
            import dbus
            p = dbus.Interface(bus.get_object(BLUEZ, path), PROPS_IFACE)
            return p.Get(iface, prop)
        except Exception:
            return None

    def set_property(self, path, iface, prop, value):
        try:
            bus = self._bus_obj()
            import dbus
            p = dbus.Interface(bus.get_object(BLUEZ, path), PROPS_IFACE)
            p.Set(iface, prop, dbus.UInt16(int(value)))
            return True
        except Exception:
            return False


# ===========================================================================
# Volume controller
# ===========================================================================
class BtVolume:
    """Stateless-ish controller. Re-discovers the active A2DP transport on each
    call (cheap, and robust to reconnects). `set(frac)`/`get()` use AVRCP
    absolute volume; if no transport exposes a Volume, `set()` falls back to
    bluez-alsa soft-volume via `runner`.

    Parameters
    ----------
    backend : object with managed_objects/get_property/set_property
        Defaults to a real python-dbus backend. Inject a fake for tests.
    runner : callable(list[str]) -> CompletedProcess
        Subprocess runner for the soft-volume fallback. Defaults to a
        3s-timeout subprocess.run. Inject a fake for tests.
    log : logging.Logger or None
    """

    def __init__(self, backend=None, runner=None, log=None):
        self.backend = backend if backend is not None else _DBusBackend()
        self.runner = runner if runner is not None else self._default_runner
        self.log = log
        self._last_pcm = None  # cache the bluez-alsa PCM path for the fallback

    # ---- internals -------------------------------------------------------
    @staticmethod
    def _default_runner(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=3)

    def _warn(self, msg):
        if self.log is not None:
            self.log.warning(msg)

    def find_transport(self):
        """Path of the A2DP MediaTransport1 to drive, or '' if none.
        Prefers a transport whose State is 'active'; otherwise the first one
        that exposes a Volume property (i.e. AVRCP absolute volume available)."""
        objs = self.backend.managed_objects() or {}
        active = ""
        first_with_vol = ""
        for path, ifaces in objs.items():
            t = ifaces.get(TRANSPORT_IFACE)
            if t is None:
                continue
            has_vol = "Volume" in t
            if str(t.get("State", "")) == "active" and has_vol:
                active = str(path)
                break
            if has_vol and not first_with_vol:
                first_with_vol = str(path)
        return active or first_with_vol

    # ---- public API ------------------------------------------------------
    def get(self):
        """Current volume as a float in [0, 1], or None if unavailable."""
        path = self.find_transport()
        if not path:
            return None
        raw = self.backend.get_property(path, TRANSPORT_IFACE, "Volume")
        return avrcp_to_frac(raw)

    def set(self, frac):
        """Set absolute volume from a fraction [0, 1]. Returns True on success.
        Tries AVRCP absolute volume first, then bluez-alsa soft-volume."""
        raw = frac_to_avrcp(frac)
        path = self.find_transport()
        if path and self.backend.set_property(path, TRANSPORT_IFACE, "Volume", raw):
            return True
        if self._softvol_set(frac):
            return True
        self._warn(f"BtVolume.set({frac:.2f}): no AVRCP transport and softvol failed")
        return False

    def set_percent(self, pct):
        return self.set(max(0, min(100, int(round(pct)))) / 100.0)

    def get_percent(self):
        f = self.get()
        return None if f is None else int(round(f * 100))

    # ---- bluez-alsa soft-volume fallback ---------------------------------
    def _bluealsactl(self):
        return shutil.which("bluealsactl") or shutil.which("bluealsa-cli")

    def _find_pcm(self):
        """Discover a bluez-alsa A2DP PCM path via `bluealsactl list-pcms`."""
        if self._last_pcm:
            return self._last_pcm
        ctl = self._bluealsactl()
        if not ctl:
            return None
        try:
            r = self.runner([ctl, "list-pcms"])
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if "a2dp" in line and line.startswith("/org/bluealsa"):
                    self._last_pcm = line.split()[0]
                    return self._last_pcm
        except Exception as e:
            self._warn(f"bluealsa list-pcms failed: {e}")
        return None

    def _softvol_set(self, frac):
        """Set bluez-alsa software volume (0..127) on the active A2DP PCM."""
        ctl = self._bluealsactl()
        pcm = self._find_pcm()
        if not ctl or not pcm:
            return False
        raw = frac_to_avrcp(frac)
        try:
            self.runner([ctl, "soft-volume", pcm, "on"])
            r = self.runner([ctl, "volume", pcm, str(raw)])
            return getattr(r, "returncode", 1) == 0
        except Exception as e:
            self._warn(f"bluealsa soft-volume set failed: {e}")
            return False


# ---------------------------------------------------------------------------
# Drop-in helper mirroring the legacy signature, for wiring into nowplaying.py.
# Replace the body of the old `set_bt_volume(pct)` with a call to this.
# ---------------------------------------------------------------------------
_DEFAULT = None

def set_bt_volume(pct, log=None):
    """Absolute Bluetooth volume in percent [0, 100]. Lazily reuses one
    BtVolume so the PCM-path cache survives across calls."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = BtVolume(log=log)
    return _DEFAULT.set_percent(pct)
