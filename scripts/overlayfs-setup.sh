#!/usr/bin/env bash
#
# overlayfs-setup.sh - Corruption-proof read-only root (CHANTIER 3)
# ===========================================================================
# Goal: survive brutal power cuts without ever corrupting the SD card. We make
# the root filesystem READ-ONLY and stack a tmpfs (RAM) overlay on top, so every
# write lands in RAM and is simply discarded at the next boot. A yanked power
# cord can no longer interrupt a write to the card.
#
# WHAT KEEPS WORKING (the "exceptions" mounted in RAM):
#   * /run, /tmp        - already tmpfs under systemd (the BT/AirPlay arbiter's
#                         /run/nowplaying-mode and its .lock live here -> fine,
#                         they are meant to be ephemeral and are regenerated).
#   * /var/log, /var/tmp- given their own size-capped tmpfs so logs never try to
#                         write to the (now read-only) card and never exhaust RAM.
#   * the whole overlay - any other write (apt, app caches, pygame temp) goes to
#                         the RAM upper layer and vanishes on reboot.
#   * journald          - switched to volatile (RAM) storage: no SD wear, no
#                         write attempts to a read-only /var/log/journal.
#
# PERSISTENCE: nothing on / survives a reboot once protected. If you must keep
# something (e.g. a log you read from Windows), write it under /run (RAM, but
# also gone on reboot) or add a dedicated writable partition -- see the NOTE at
# the bottom. The display app itself keeps no durable on-card state, so it is a
# clean fit.
#
# CROSS-CUTTING (CI/CD, CHANTIER 4): a read-only root means `rsync`/`systemctl`
# deploys fail until you remount rw. Use the installed helper:
#     sudo fsprotect off && sudo reboot     # writable for maintenance/deploy
#     sudo fsprotect on  && sudo reboot      # back to protected
# The GitHub Actions workflow runs `fsprotect off`, deploys, then `fsprotect on`.
#
#   Target: Raspberry Pi OS / moOde (Debian 13 trixie). Run ON the Pi w/ sudo.
#   Reversible. A reboot is required for the overlay to take effect.
# ===========================================================================
set -euo pipefail

log(){ printf '\033[1;36m[overlay]\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m[overlay] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run with sudo"

TS="$(date +%Y%m%d-%H%M%S)"
log "backing up /etc/fstab -> /etc/fstab.bak.$TS"
cp -a /etc/fstab "/etc/fstab.bak.$TS"

# ---------------------------------------------------------------------------
# 1) Volatile dirs -> size-capped tmpfs (so a RO root never blocks logging)
# ---------------------------------------------------------------------------
add_fstab() {  # $1=mountpoint  $2=options
  local mp="$1" opt="$2"
  if ! grep -qE "^[^#]*[[:space:]]$mp[[:space:]]" /etc/fstab; then
    log "fstab += tmpfs $mp"
    printf 'tmpfs\t%s\ttmpfs\t%s\t0 0\n' "$mp" "$opt" >> /etc/fstab
  else
    log "fstab already has $mp (left as-is)"
  fi
}
add_fstab /var/log "nosuid,nodev,noatime,mode=0755,size=32m"
add_fstab /var/tmp "nosuid,nodev,noatime,mode=1777,size=16m"

# journald -> RAM (no SD wear, no writes to a read-only /var/log/journal)
log "journald: Storage=volatile, capped"
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/volatile.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=24M
EOF

# Ensure the app's RAM state dir exists every boot (belt-and-suspenders; the
# arbiter already writes /run/nowplaying-mode on tmpfs).
cat > /etc/tmpfiles.d/nowplaying.conf <<'EOF'
d /run/nowplaying 0755 root root -
EOF

# ---------------------------------------------------------------------------
# 2) Maintenance toggle: fsprotect on|off|status
# ---------------------------------------------------------------------------
log "installing /usr/local/bin/fsprotect helper"
cat > /usr/local/bin/fsprotect <<'EOF'
#!/usr/bin/env bash
# fsprotect on|off|status  -- toggle the read-only overlay root. Reboot to apply.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
have_rc(){ command -v raspi-config >/dev/null && raspi-config nonint get_overlay_now >/dev/null 2>&1; }
case "${1:-status}" in
  on)
    if command -v raspi-config >/dev/null; then
      raspi-config nonint enable_overlayfs
      raspi-config nonint enable_bootro 2>/dev/null || true
    else
      sed -i 's/^overlayroot=.*/overlayroot="tmpfs:swap=0,recurse=0"/' /etc/overlayroot.conf 2>/dev/null \
        || echo 'overlayroot="tmpfs:swap=0,recurse=0"' >> /etc/overlayroot.conf
    fi
    echo "overlay ENABLED - reboot to apply (root will be read-only)";;
  off)
    if command -v raspi-config >/dev/null; then
      raspi-config nonint disable_overlayfs
      raspi-config nonint disable_bootro 2>/dev/null || true
    else
      sed -i 's/^overlayroot=.*/overlayroot=""/' /etc/overlayroot.conf 2>/dev/null || true
    fi
    echo "overlay DISABLED - reboot to apply (root writable for maintenance)";;
  status)
    if command -v raspi-config >/dev/null && raspi-config nonint get_overlay_now 2>/dev/null | grep -q 0; then
      echo "overlay: ON (root read-only)"
    elif findmnt -no FSTYPE / | grep -q overlay; then
      echo "overlay: ON (root read-only)"
    else
      echo "overlay: OFF (root writable)"
    fi;;
  *) echo "usage: fsprotect on|off|status" >&2; exit 1;;
esac
EOF
chmod 755 /usr/local/bin/fsprotect

# ---------------------------------------------------------------------------
# 3) Enable the overlay (supported path first, package fallback)
# ---------------------------------------------------------------------------
if command -v raspi-config >/dev/null 2>&1; then
  log "enabling overlay via raspi-config (initramfs overlay + boot read-only)"
  raspi-config nonint enable_overlayfs
  raspi-config nonint enable_bootro 2>/dev/null || \
    log "  (boot RO toggle unavailable; will set /boot ro in fstab)"
else
  log "raspi-config absent -> using 'overlayroot' package"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y overlayroot
  if grep -q '^overlayroot=' /etc/overlayroot.conf; then
    sed -i 's/^overlayroot=.*/overlayroot="tmpfs:swap=0,recurse=0"/' /etc/overlayroot.conf
  else
    echo 'overlayroot="tmpfs:swap=0,recurse=0"' >> /etc/overlayroot.conf
  fi
fi

# Make the boot partition read-only too (defensive; harmless if already ro).
BOOTMP="$(findmnt -no TARGET /boot/firmware 2>/dev/null || echo /boot)"
if grep -qE "[[:space:]]$BOOTMP[[:space:]]" /etc/fstab; then
  if ! grep -E "[[:space:]]$BOOTMP[[:space:]]" /etc/fstab | grep -qw ro; then
    log "marking $BOOTMP read-only in fstab"
    sed -i -E "s#([[:space:]]$BOOTMP[[:space:]]+[^[:space:]]+[[:space:]]+)defaults#\\1ro,defaults#" /etc/fstab || true
  fi
fi

systemctl daemon-reload || true

cat <<EOF

\033[1;32mConfigured.\033[0m Review /etc/fstab, then:   sudo reboot

After reboot, verify it took:
  findmnt /                 # FSTYPE should be 'overlay'
  fsprotect status          # -> overlay: ON (root read-only)
  touch /root/x 2>&1        # write 'succeeds' but is RAM-only (gone next boot)

NOTE - persistent writable data (optional):
  A read-only root drops everything on reboot. If you need durable storage,
  add a small ext4 partition (e.g. /dev/mmcblk0p3) mounted rw at /data and put
  ONLY the files that must survive there (the root stays protected). The
  display app needs none, so the default setup is complete as-is.

ROLLBACK:
  sudo fsprotect off && sudo reboot
  # then optionally: restore /etc/fstab.bak.$TS and remove the tmpfs lines
EOF
