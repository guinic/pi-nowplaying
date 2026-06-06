#!/usr/bin/env bash
#
# bluealsa-codecs.sh - Rebuild BlueZ-ALSA with extra A2DP codecs (CHANTIER 2)
# ===========================================================================
# Stock bluez-alsa on Debian ships SBC-only. This unit is an A2DP *sink* fed by
# an iPhone, and iPhones send **AAC** over A2DP -- so enabling AAC is the real,
# audible win here (cleaner highs, no SBC bottleneck). aptX/LDAC only matter if
# an *Android* source connects; they are gated OFF by default because their
# encoder libs are not cleanly packaged on every Debian and carry licensing
# nuance. Turn them on explicitly if you want them.
#
#   Target : Raspberry Pi OS / moOde (Debian 13 "trixie", arm64)
#   Source : https://github.com/arkq/bluez-alsa  (v4 -> daemon = `bluealsad`)
#
# USAGE (run ON the Pi, as a user with sudo -- NOT from CI):
#   chmod +x bluealsa-codecs.sh
#   sudo ./bluealsa-codecs.sh                 # AAC only (recommended)
#   sudo BUILD_LDAC=1 BUILD_APTX=1 ./bluealsa-codecs.sh   # everything
#   sudo BAVER=v4.3.1 PREFIX=/usr/local ./bluealsa-codecs.sh
#
# This is reversible: the original daemon is untouched (we install to
# /usr/local and override the service with a drop-in). See ROLLBACK at the end.
# ===========================================================================
set -euo pipefail

BAVER="${BAVER:-v4.3.1}"          # bluez-alsa release tag to build
PREFIX="${PREFIX:-/usr/local}"    # install prefix (keeps distro pkg intact)
BUILD_AAC="${BUILD_AAC:-1}"
BUILD_LDAC="${BUILD_LDAC:-0}"
BUILD_APTX="${BUILD_APTX:-0}"
SRC="/usr/local/src/bluez-alsa"
JOBS="$(nproc)"

log(){ printf '\033[1;36m[bluealsa]\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m[bluealsa] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"

# ---------------------------------------------------------------------------
# 1) Build toolchain + bluez-alsa build dependencies
# ---------------------------------------------------------------------------
log "apt: base build dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential pkg-config automake libtool git ca-certificates \
  libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev \
  libsbc-dev libspandsp-dev libreadline-dev

CONFIG_FLAGS=( --prefix="$PREFIX" --enable-msbc --enable-cli
               --with-alsaplugindir="/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || echo aarch64-linux-gnu)/alsa-lib" )

# ---------------------------------------------------------------------------
# 2) Codec libraries (only those requested)
# ---------------------------------------------------------------------------
ensure_nonfree() {
  # libfdk-aac-dev lives in Debian 'non-free'. Add it once if missing.
  if ! apt-cache policy libfdk-aac-dev 2>/dev/null | grep -q Candidate:\ [0-9]; then
    log "enabling 'non-free' apt component for libfdk-aac-dev"
    local list=/etc/apt/sources.list.d/nonfree-bluealsa.list
    . /etc/os-release
    echo "deb http://deb.debian.org/debian ${VERSION_CODENAME:-trixie} non-free non-free-firmware" > "$list"
    apt-get update -qq
  fi
}

if [ "$BUILD_AAC" = 1 ]; then
  log "AAC: installing Fraunhofer FDK-AAC dev lib"
  ensure_nonfree
  apt-get install -y libfdk-aac-dev || die "could not install libfdk-aac-dev"
  CONFIG_FLAGS+=( --enable-aac )
fi

if [ "$BUILD_LDAC" = 1 ]; then
  log "LDAC: installing encoder/decoder dev libs"
  if ! apt-get install -y libldacbt-enc-dev libldacbt-abr-dev 2>/dev/null; then
    log "LDAC pkgs absent -> building libldac from source"
    rm -rf /tmp/ldacBT && git clone --recursive --depth 1 \
      https://github.com/EHfive/ldacBT.git /tmp/ldacBT
    cmake -S /tmp/ldacBT -B /tmp/ldacBT/build -DCMAKE_INSTALL_PREFIX="$PREFIX" >/dev/null
    make -C /tmp/ldacBT/build -j"$JOBS" && make -C /tmp/ldacBT/build install
    ldconfig
  fi
  CONFIG_FLAGS+=( --enable-ldac )
fi

if [ "$BUILD_APTX" = 1 ]; then
  log "aptX: installing libopenaptx (FOSS reimplementation)"
  if apt-get install -y libopenaptx-dev 2>/dev/null; then
    CONFIG_FLAGS+=( --enable-aptx --enable-aptx-hd --with-libopenaptx )
  else
    log "WARN: libopenaptx-dev not available on this release -> skipping aptX"
  fi
fi

# ---------------------------------------------------------------------------
# 3) Fetch + build bluez-alsa
# ---------------------------------------------------------------------------
log "fetching bluez-alsa $BAVER"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch --depth 1 origin "$BAVER"
  git -C "$SRC" checkout -q FETCH_HEAD
else
  rm -rf "$SRC"; mkdir -p "$(dirname "$SRC")"
  git clone --depth 1 --branch "$BAVER" https://github.com/arkq/bluez-alsa "$SRC"
fi

cd "$SRC"
log "autogen + configure: ${CONFIG_FLAGS[*]}"
[ -x ./autogen.sh ] && ./autogen.sh
mkdir -p build && cd build
../configure "${CONFIG_FLAGS[@]}"
log "compiling (-j$JOBS)"
make -j"$JOBS"
make install
ldconfig

DAEMON="$PREFIX/bin/bluealsad"
[ -x "$DAEMON" ] || DAEMON="$PREFIX/bin/bluealsa"   # v3 fallback name
[ -x "$DAEMON" ] || die "build produced no bluealsa daemon"
log "installed daemon: $DAEMON"
"$DAEMON" --version || true

# ---------------------------------------------------------------------------
# 4) Point the systemd service at the new daemon (drop-in override)
# ---------------------------------------------------------------------------
SVC=""
for cand in bluealsa.service bt-bluealsa.service bluealsad.service; do
  if systemctl list-unit-files | grep -q "^$cand"; then SVC="$cand"; break; fi
done
[ -n "$SVC" ] || die "no bluealsa systemd service found to override"
log "overriding $SVC"

# Offer BOTH sink and source profiles; codecs compiled-in are auto-advertised.
PROFILES="-p a2dp-sink -p a2dp-source"
mkdir -p "/etc/systemd/system/$SVC.d"
cat > "/etc/systemd/system/$SVC.d/10-codecs.conf" <<EOF
# Generated by bluealsa-codecs.sh -- use the locally built, codec-enabled daemon
[Service]
ExecStart=
ExecStart=$DAEMON $PROFILES
EOF

systemctl daemon-reload
systemctl restart bluetooth 2>/dev/null || true
systemctl restart "$SVC"
sleep 2

# ---------------------------------------------------------------------------
# 5) Verify
# ---------------------------------------------------------------------------
log "service status:"
systemctl is-active "$SVC" || true
log "advertised A2DP SEP codecs (reconnect the phone, then re-run if empty):"
"$PREFIX/bin/bluealsactl" list-pcms 2>/dev/null || \
  log "  (bluealsactl: connect a device to populate PCMs)"

cat <<EOF

\033[1;32mDONE.\033[0m AAC$([ "$BUILD_LDAC" = 1 ] && echo '+LDAC')$([ "$BUILD_APTX" = 1 ] && echo '+aptX') built into $DAEMON.
Reconnect the iPhone and confirm the codec with:  bluealsactl list-pcms

ROLLBACK (revert to the distro SBC-only daemon):
  sudo rm -f /etc/systemd/system/$SVC.d/10-codecs.conf
  sudo systemctl daemon-reload && sudo systemctl restart $SVC
EOF
