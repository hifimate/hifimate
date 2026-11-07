#!/bin/sh
# HiFiMate one-line installer:
#   curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh
#   curl -fsSL .../install.sh | sudo sh -s -- --name 客厅 --output-device hw:CARD=DAC,DEV=0
#
# Run as root (through sudo). Installs pipx when missing (the distribution's
# package), installs the hifimate command for the whole system
# (/usr/local/bin/hifimate, package in /opt/pipx), then runs
# `hifimate install`, which creates the dedicated user `hifimate` and sets
# everything up (see README). Everything after `--` goes to `hifimate install`
# (see INSTALL.md); passwords go in environment variables or files, never on
# the command line.
#
# Without root, HIFIMATE_USER_MODE=1 installs for the current user only
# (development, or no root available).
set -eu

# A release replaces this with its own version, so the script installs the
# release it was published with. HIFIMATE_VERSION overrides it; empty means latest.
VERSION=${HIFIMATE_VERSION-@VERSION@}
case "$VERSION" in @*@) VERSION= ;; esac
SPEC=${HIFIMATE_SPEC:-hifimate${VERSION:+==$VERSION}}

say() { printf 'hifimate: %s\n' "$*" >&2; }
[ "$(uname -s)" = Linux ] || { say 'HiFiMate runs on Linux only'; exit 2; }

if [ "$(id -u)" -eq 0 ]; then
    SUDO=
    # The command for the whole system, like pipx --global, spelled out for older pipx.
    export PIPX_HOME=${PIPX_HOME:-/opt/pipx} PIPX_BIN_DIR=${PIPX_BIN_DIR:-/usr/local/bin} \
        PIPX_MAN_DIR=${PIPX_MAN_DIR:-/usr/local/share/man}
elif [ "${HIFIMATE_USER_MODE:-}" = 1 ]; then
    SUDO=sudo
else
    say 'run it with sudo:  curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh'
    say '(it creates a dedicated user "hifimate"; HIFIMATE_USER_MODE=1 installs for the current user instead)'
    exit 2
fi

if ! command -v pipx >/dev/null 2>&1; then
    . /etc/os-release 2>/dev/null || true
    say "installing pipx (${PRETTY_NAME:-this system}'s package)"
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update -qq && $SUDO apt-get install -y python3 pipx
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y python3 pipx
    elif command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -S --needed --noconfirm python python-pipx
    elif command -v zypper >/dev/null 2>&1; then
        $SUDO zypper --non-interactive install python3 python3-pipx
    else
        say 'no supported package manager (apt, dnf, pacman, zypper): install python3 and pipx, then run this again'
        exit 2
    fi
fi

say "installing $SPEC with pipx"
pipx install --force "$SPEC" >&2
bin=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || printf '%s' "${PIPX_BIN_DIR:-$HOME/.local/bin}")
if [ -z "$SUDO" ]; then :; else
    case ":$PATH:" in *":$bin:"*) ;; *) pipx ensurepath >/dev/null 2>&1 || true
        say "$bin was added to PATH for new shells; until then run $bin/hifimate" ;; esac
fi

# Piped into sh, stdin is this script: give the installer the terminal back
# so it can ask (name, passwords). Without a terminal it runs unattended.
if [ -t 0 ]; then
    exec "$bin/hifimate" install "$@"
elif (: </dev/tty) 2>/dev/null; then
    exec "$bin/hifimate" install "$@" </dev/tty
else
    exec "$bin/hifimate" install --non-interactive "$@" </dev/null
fi
