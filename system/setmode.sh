#!/bin/bash
# setmode.sh -- switch the hifi audio receiver between AirPlay and Bluetooth.
#
#   setmode.sh airplay     -> shairport-sync owns the output (default)
#   setmode.sh bluetooth   -> bluez-alsa A2DP sink owns the output
#   setmode.sh status      -> prints the current mode
#
# AirPlay (shairport-sync) and Bluetooth (bluealsa-aplay) both want the ALSA
# '_audioout' device, so only one can run at a time. Run as root (via sudo).
#
# Robustness: the Pi's onboard BT controller (BCM43455, shared antenna with
# WiFi) can occasionally wedge, after which bluetoothctl AND btmgmt block
# forever. EVERY adapter call here is wrapped in `timeout` so a wedge can never
# hang the switch; bt_start self-heals (rfkill + hciuart reset) and, failing
# that, falls back to AirPlay so the system is never left in a broken state.
# Switches are serialized with flock so a late call can't stomp a newer one.
set -u

STATE="/run/nowplaying-mode"
LOG="/var/log/nowplaying-setmode.log"
LOCK="/run/nowplaying-mode.lock"

log() { echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }

# Bounded probe: returns 0 as soon as the adapter answers on D-Bus.
adapter_up() {
    for _ in $(seq 1 "${1:-20}"); do
        timeout 2 bluetoothctl show >/dev/null 2>&1 && return 0
        sleep 0.3
    done
    return 1
}

# AirPlay drives the hardware PCM mixer (shairport mixer_control_name="PCM" on
# hw:Headphones), so it gets loud. The Bluetooth path does NOT touch that mixer --
# bluealsa applies the phone's volume in software and leaves the hardware control
# wherever AirPlay last parked it (we saw -43.87 dB), so BT was far quieter. Pin
# PCM to a loud 0 dB reference on every BT switch; the iPhone's volume buttons
# then ride on top via bluealsa/AVRCP absolute volume.
bt_volume() {
    amixer -c Headphones -- sset PCM 0dB >/dev/null 2>&1 \
        || amixer -- sset PCM 0dB >/dev/null 2>&1
}

# Mark every paired phone as trusted so bluetoothd auto-accepts its A2DP
# reconnection. We power the adapter off in AirPlay mode, so on the way back to
# Bluetooth the phone re-initiates the link; without trust bluetoothd would
# reject it until the user manually re-pairs ("ca ne fonctionne plus").
bt_trust_paired() {
    local mac
    for mac in $(timeout 6 bluetoothctl devices Paired 2>/dev/null | awk '{print $2}'); do
        timeout 4 bluetoothctl trust "$mac" >/dev/null 2>&1
    done
}

bt_start() {
    # Idempotent: if we're already in bluetooth with the sink running, a repeat
    # tap must NOT restart the daemons -- that would drop an active A2DP
    # connection. Just re-assert discoverable + the loud mixer and return.
    if [ "$(cat "$STATE" 2>/dev/null)" = bluetooth ] && systemctl is-active --quiet bt-aplay; then
        log "already in bluetooth; keeping active connection"
        timeout 8 bluetoothctl <<'EOF' >/dev/null 2>&1
power on
pairable on
discoverable on
EOF
        bt_trust_paired
        bt_volume
        log "bluetooth ready (unchanged)"
        return
    fi

    log "switching to bluetooth"
    systemctl stop shairport-sync 2>/dev/null

    rfkill unblock bluetooth 2>/dev/null
    systemctl start bluetooth 2>/dev/null

    if ! adapter_up 20; then
        # Controller may be wedged -- try a hard reset once.
        log "bluetooth: adapter unresponsive, resetting controller"
        rfkill block bluetooth 2>/dev/null;  sleep 1
        rfkill unblock bluetooth 2>/dev/null; sleep 1
        timeout 15 systemctl restart hciuart 2>/dev/null; sleep 2
        systemctl restart bluetooth 2>/dev/null
        if ! adapter_up 20; then
            log "bluetooth: adapter still dead -> falling back to airplay"
            airplay_start
            return
        fi
    fi

    # bluetoothd is authoritative when running; drive discoverable/pairable
    # through it (DiscoverableTimeout/PairableTimeout are 0 in main.conf, so
    # they persist). Bounded so a wedge can't hang us.
    timeout 8 bluetoothctl <<'EOF' >/dev/null 2>&1
power on
pairable on
discoverable on
EOF

    # Just-Works pairing agent + A2DP sink daemon (persistent units).
    systemctl restart bt-agent
    systemctl restart bt-bluealsa
    for _ in $(seq 1 15); do
        busctl list 2>/dev/null | grep -q org.bluealsa && break
        systemctl is-active --quiet bt-bluealsa || break
        sleep 0.2
    done
    systemctl restart bt-aplay

    bt_trust_paired
    bt_volume
    echo bluetooth > "$STATE"
    log "bluetooth ready"
}

airplay_start() {
    log "switching to airplay"
    systemctl stop bt-aplay bt-bluealsa bt-agent 2>/dev/null
    # Clear any "failed" artifact left by a stopped bt daemon so `systemctl
    # status` stays clean (they're restarted fresh on the next bluetooth switch).
    systemctl reset-failed bt-aplay bt-bluealsa bt-agent 2>/dev/null
    # Power the adapter down THROUGH bluetoothd (which is responsive) while it is
    # still running -- this makes the adapter non-discoverable/non-connectable so
    # "Hifi Bluetooth" disappears from nearby phones. We deliberately avoid
    # `btmgmt power off`: talking straight to the kernel mgmt socket can wedge the
    # BCM43455 controller (both control paths then hang). Bounded so a crashy
    # bluetoothctl can never stall the switch.
    timeout 6 bluetoothctl <<'EOF' >/dev/null 2>&1
discoverable off
pairable off
power off
EOF
    systemctl stop bluetooth 2>/dev/null
    systemctl start shairport-sync
    echo airplay > "$STATE"
    log "airplay ready"
}

case "${1:-status}" in
    bluetooth|airplay)
        exec 9>"$LOCK"
        flock 9
        [ "$1" = bluetooth ] && bt_start || airplay_start
        ;;
    status)
        cat "$STATE" 2>/dev/null || echo airplay
        ;;
    *)
        echo "usage: $0 {airplay|bluetooth|status}" >&2; exit 1
        ;;
esac
