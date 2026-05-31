#!/usr/bin/env bash
# Verify a fresh Raspberry Pi OS image is ready to talk to the LumiCube
# via /dev/ttyAMA0 with this Python implementation.
#
# Run on the Pi:   bash scripts/check_pi_uart.sh
# Exits non-zero if any FAIL is reported. WARN entries are informational
# and won't fail the check on their own.

set -u

PASS=0
WARN=0
FAIL=0

ok()   { printf "  [ OK ]  %s\n" "$*"; PASS=$((PASS+1)); }
warn() { printf "  [WARN]  %s\n" "$*"; WARN=$((WARN+1)); }
bad()  { printf "  [FAIL]  %s\n" "$*"; FAIL=$((FAIL+1)); }
hdr()  { printf "\n== %s ==\n" "$*"; }

# ----------------------------------------------------------------------
hdr "boot config (UART enabled, BT off PL011, no serial console)"

CFG=""
for cand in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$cand" ]]; then CFG="$cand"; break; fi
done
if [[ -z "$CFG" ]]; then
    bad "no boot config.txt found (looked in /boot/firmware/ and /boot/)"
else
    ok "boot config: $CFG"
    if grep -Eq '^\s*enable_uart\s*=\s*1' "$CFG"; then
        ok "enable_uart=1 present"
    else
        bad "enable_uart=1 missing in $CFG — add it"
    fi
    if grep -Eq '^\s*dtoverlay\s*=\s*disable-bt' "$CFG"; then
        ok "dtoverlay=disable-bt present (PL011 UART on /dev/ttyAMA0)"
    elif grep -Eq '^\s*dtoverlay\s*=\s*miniuart-bt' "$CFG"; then
        warn "dtoverlay=miniuart-bt present — BT moves to mini-UART; ttyAMA0 should still be PL011"
    else
        bad "no dtoverlay=disable-bt (or miniuart-bt) — /dev/ttyAMA0 will be the slow mini-UART"
    fi
fi

CMDLINE=""
for cand in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [[ -f "$cand" ]]; then CMDLINE="$cand"; break; fi
done
if [[ -z "$CMDLINE" ]]; then
    warn "cmdline.txt not found"
else
    ok "kernel cmdline: $CMDLINE"
    if grep -Eq 'console=serial0|console=ttyAMA0' "$CMDLINE"; then
        bad "$CMDLINE has a serial console clause — kernel will hold /dev/ttyAMA0; remove it"
    else
        ok "no serial console on ttyAMA0 in cmdline.txt"
    fi
fi

# ----------------------------------------------------------------------
hdr "serial-getty"

if systemctl is-enabled serial-getty@ttyAMA0.service >/dev/null 2>&1; then
    bad "serial-getty@ttyAMA0.service is enabled — disable & mask it"
else
    ok "serial-getty@ttyAMA0.service not enabled"
fi
if systemctl is-active serial-getty@ttyAMA0.service >/dev/null 2>&1; then
    bad "serial-getty@ttyAMA0.service is running — stop & mask it"
else
    ok "serial-getty@ttyAMA0.service not running"
fi

# ----------------------------------------------------------------------
hdr "device node + permissions"

if [[ -e /dev/ttyAMA0 ]]; then
    ok "/dev/ttyAMA0 exists ($(stat -c '%U:%G %a' /dev/ttyAMA0 2>/dev/null || echo 'stat failed'))"
else
    bad "/dev/ttyAMA0 does not exist"
fi
if id -nG "$USER" 2>/dev/null | grep -qw dialout; then
    ok "user '$USER' in dialout group"
else
    warn "user '$USER' not in dialout group — run: sudo usermod -aG dialout $USER && relogin"
fi

# ----------------------------------------------------------------------
hdr "foundry-daemon must be absent"
# The stock lumicube image installs the daemon as an AppImage under
# ~/AbstractFoundry/Daemon/Software/Daemon-<ver>-aarch64.AppImage, launched
# by ~/AbstractFoundry/Daemon/launch.sh. We check both that location and
# the older /opt/foundry-daemon convention, plus any systemd footprint
# (system OR user-mode).

if command -v foundry-daemon >/dev/null 2>&1; then
    bad "foundry-daemon binary on PATH"
else
    ok "no foundry-daemon binary on PATH"
fi
if systemctl list-unit-files 2>/dev/null | grep -Eq '^foundry-daemon\.service|^foundry\.service'; then
    bad "foundry-daemon system unit present"
else
    ok "no foundry-daemon system unit"
fi
if XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)} \
   systemctl --user list-unit-files 2>/dev/null \
   | grep -Eq '^foundry-daemon\.service|^foundry\.service'; then
    bad "foundry-daemon user unit present (systemctl --user)"
else
    ok "no foundry-daemon user unit"
fi
if pgrep -f -i 'foundry|AbstractFoundry|Daemon-[0-9].*AppImage' >/dev/null 2>&1; then
    bad "daemon-like process running: $(pgrep -af -i 'foundry|AbstractFoundry|Daemon-[0-9].*AppImage' | head -3)"
else
    ok "no foundry-daemon / AppImage process running"
fi
if [[ -d /opt/foundry-daemon ]]; then
    warn "/opt/foundry-daemon directory still present"
else
    ok "no /opt/foundry-daemon leftover"
fi
# Check every real user's home for AbstractFoundry/ (not just $HOME).
FOUND_AF=0
while IFS=: read -r _user _x _uid _gid _gecos _home _shell; do
    [[ "$_uid" -lt 1000 ]] && continue
    [[ -z "$_home" ]] && continue
    [[ -d "$_home/AbstractFoundry" ]] || continue
    bad "$_home/AbstractFoundry/ present (stock-image daemon install)"
    FOUND_AF=1
done < /etc/passwd
[[ "$FOUND_AF" -eq 0 ]] && ok "no ~/AbstractFoundry/ install in any user home"

# ----------------------------------------------------------------------
hdr "python"

if command -v python3 >/dev/null 2>&1; then
    PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    PYMAJ="$(python3 -c 'import sys; print(sys.version_info[0])')"
    PYMIN="$(python3 -c 'import sys; print(sys.version_info[1])')"
    if [[ "$PYMAJ" -gt 3 ]] || { [[ "$PYMAJ" -eq 3 ]] && [[ "$PYMIN" -ge 11 ]]; }; then
        ok "python3 = $PYV (>= 3.11)"
    else
        bad "python3 = $PYV (need >= 3.11 per pyproject.toml)"
    fi
else
    bad "python3 not found"
fi
if command -v uv >/dev/null 2>&1; then
    ok "uv available ($(uv --version 2>&1 | head -1))"
else
    warn "uv not installed (optional; pip works too)"
fi

# ----------------------------------------------------------------------
hdr "summary"
printf "  pass=%d  warn=%d  fail=%d\n" "$PASS" "$WARN" "$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1