#!/usr/bin/env bash
# NixStore system update script — bundled in the nixstore package.
#
# Self-update: on first invocation (without --skip-self-update) the script
# updates only the nixstore flake input, rebuilds, and re-execs itself from the
# newly installed path. If nixstore was updated the new update.sh runs the rest;
# if unchanged it continues in-place.
#
# Environment variables:
#   NIXSTORE_DOTFILES        — path to the dotfiles repo (skips home-config sync if unset)
#   NIXSTORE_FLAKE           — path to the system flake (default: /etc/nixos)
#   NIXSTORE_NONINTERACTIVE=1 — skip interactive prompts (used by the TUI)
#   SUDO_ASKPASS             — askpass helper for sudo -A (set by the TUI)
set -euo pipefail

FLAKE="${NIXSTORE_FLAKE:-/etc/nixos}"
VARS_FILE="$FLAKE/.dotfiles-vars"
HOME_STATE_DIR="$HOME/.local/share/dotfiles-home-state"

# ── Helpers ───────────────────────────────────────────────────────────────────
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
skip()  { printf '  \033[33m–\033[0m %s\n' "$*"; }

_sudo() {
    if [[ -n "${SUDO_ASKPASS:-}" ]]; then sudo -A "$@"; else sudo "$@"; fi
}

# Load vars file early (needed for hostname + dotfiles path)
# shellcheck source=/dev/null
[ -f "$VARS_FILE" ] && source "$VARS_FILE" || true
HOSTNAME_VAR="${DOTFILES_HOSTNAME:-$(hostname 2>/dev/null || echo nixos)}"

# Resolve dotfiles dir: env var > vars file > unset (skip sync sections)
DOTFILES="${NIXSTORE_DOTFILES:-${DOTFILES_REPO:-}}"

# Directory this script lives in (bundled engine + manifest sit alongside it).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source the bundled home-config deployment engine (lib/home-sync.sh, relocated
# here as $SCRIPT_DIR/home-sync.sh). Requires DOTFILES to be set (the engine
# aborts otherwise), so only call this when a dotfiles repo is resolved.
#
# Manifest resolution: the engine classifies paths from HS_MANIFEST. We prefer
# the engine-matched BUNDLED copy ($SCRIPT_DIR/dotfiles-manifest) so the
# classification always matches the shipped engine logic, and fall back to the
# active repo's home/.dotfiles-manifest only if the bundled one is missing.
_source_home_engine() {
    if [ -f "$SCRIPT_DIR/dotfiles-manifest" ]; then
        HS_MANIFEST="$SCRIPT_DIR/dotfiles-manifest"
    elif [ -f "$DOTFILES/home/.dotfiles-manifest" ]; then
        HS_MANIFEST="$DOTFILES/home/.dotfiles-manifest"
    fi
    # shellcheck source=/dev/null
    . "$SCRIPT_DIR/home-sync.sh"
}

# ── Dry-run (--check): ZERO writes — no heal, cp, baseline, journal, quiesce,
# kill, git mutation, self-update, sudo, or rebuild. Report what a real home
# sync WOULD do, then exit. Intercepted BEFORE self-update on purpose. ─────────
if [[ "${1:-}" == "--check" ]]; then
    if [ -z "$DOTFILES" ] || [ ! -d "$DOTFILES/home" ]; then
        info "No dotfiles repo resolved (set NIXSTORE_DOTFILES or DOTFILES_REPO) — nothing to check."
        exit 0
    fi
    bold "→ Dry-run (--check): reporting planned home-config actions, no writes ..."
    echo ""
    _source_home_engine
    hs_check
    echo ""
    bold "→ (--check) done — nothing was modified."
    exit 0
fi

# ── Self-update nixstore ──────────────────────────────────────────────────────
# Skip when already re-execed from the new binary.
if [[ "${1:-}" != "--skip-self-update" ]]; then
    bold "→ Checking for NixStore updates ..."
    if _sudo nix flake update nixstore --flake "$FLAKE" 2>&1 | sed 's/^/  /'; then
        _sudo nixos-rebuild switch --flake "$FLAKE#$HOSTNAME_VAR" 2>&1 | tail -10 | sed 's/^/  /' || true
        ok "NixStore self-update done"
        # Re-exec from the newly installed update.sh if it differs from this script
        _nixstore_bin=$(command -v nixstore 2>/dev/null || true)
        if [ -n "$_nixstore_bin" ]; then
            _new_script="$(dirname "$(dirname "$(readlink -f "$_nixstore_bin")")")/share/nixstore/update.sh"
            if [ -f "$_new_script" ] && [ "$_new_script" != "${NIXSTORE_UPDATE_SCRIPT:-$0}" ]; then
                bold "→ Re-running with updated update.sh ..."
                exec bash "$_new_script" --skip-self-update "$@"
            fi
        fi
    else
        info "nixstore update check failed — continuing with current version"
    fi
    echo ""
fi

# ── Pull latest dotfiles ───────────────────────────────────────────────────────
if [ -n "$DOTFILES" ] && [ -d "$DOTFILES/.git" ]; then
    bold "→ Pulling latest dotfiles ..."
    git -C "$DOTFILES" pull --ff-only && echo "" || info "git pull failed — continuing with local dotfiles"
fi

# ── ~/.config + ~/.local (deployed by COPY via the shared self-heal engine) ──
# This REPLACES the former inline copy-sync. The bundled engine
# ($SCRIPT_DIR/home-sync.sh) performs, in the council-reviewed order:
#   quiesce writers → self-heal (symlinked clone → real copies) → 3-way MERGE
#   → fail-closed delete pass → regenerate generated files → resume writers
#   → hyprctl reload (last). Every destructive step is crash-safe (write-ahead
#   journal + fsync), baselines are seeded from shipped-new, and a real-run
#   EXIT/INT/TERM trap always brings writers back if the run aborts mid-sync.
if [ -n "$DOTFILES" ] && [ -d "$DOTFILES/home" ]; then
    bold "→ Syncing home config (~/.config, ~/.local) ..."

    _source_home_engine

    # Real-run safety net: under `set -euo pipefail` an unexpected failure
    # between hs_quiesce_writers and hs_resume_writers would leave quickshell
    # DOWN with no reload. This trap ALWAYS restores writers (and does the final
    # hyprctl reload, via hs_resume_writers) on any exit — success, error, or
    # interrupt — if quiesce ran but resume didn't. Idempotent (guarded by
    # _hs_quiesced, cleared by hs_resume_writers). It also folds in the
    # sudo-keepalive cleanup installed later, preserving the original exit code.
    _hs_quiesced=0
    _hs_cleanup() {
        local ec=$?
        trap - EXIT INT TERM        # disarm to avoid re-entry from our own exit
        if [ "${_hs_quiesced:-0}" = "1" ]; then
            _hs_warn "update exited with writers quiesced — restoring them ..."
            hs_resume_writers || true   # includes the final hyprctl reload
        fi
        [ -n "${SUDO_KEEPALIVE_PID:-}" ] && kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
        exit "$ec"                  # never mask the original exit code
    }
    trap _hs_cleanup EXIT INT TERM

    hs_quiesce_writers

    bold "→ Self-healing any symlinked install into real copies ..."
    hs_selfheal

    bold "→ Merging upstream home config (3-way) ..."
    hs_sync_tree "$DOTFILES/home/.config"      "$HOME/.config"      644
    hs_sync_tree "$DOTFILES/home/.local/share" "$HOME/.local/share" 644
    hs_sync_tree "$DOTFILES/home/.local/bin"   "$HOME/.local/bin"   755
    hs_delete_pass

    # Generated files are owned by their generator — regenerate now (writers
    # still quiesced; init-monitors.sh does its own atomic write + hash guard).
    if command -v hyprctl &>/dev/null && hyprctl monitors &>/dev/null 2>&1; then
        _init_monitors="$HOME/.config/hypr/scripts/init-monitors.sh"
        if [ -f "$_init_monitors" ]; then
            bold "→ Regenerating monitors.lua ..."
            bash "$_init_monitors" && ok "monitors.lua updated" || info "init-monitors.sh failed — skipping"
        fi
    fi

    # Bring writers back (qs-restart), then hyprctl reload LAST.
    hs_resume_writers
    echo ""

    DCONF_SCRIPT="$DOTFILES/home/apply-dconf.sh"
    if [ -f "$DCONF_SCRIPT" ] && command -v dconf &>/dev/null; then
        bold "→ Applying dconf settings ..."
        bash "$DCONF_SCRIPT" && ok "dconf settings applied" || info "dconf: failed"
        echo ""
    fi
fi

# ── NixOS config ──────────────────────────────────────────────────────────────
if [ ! -d /etc/nixos ]; then
    info "/etc/nixos not found — skipping NixOS update."
    exit 0
fi

# Acquire sudo
bold "→ Requesting sudo ..."
if [[ "${NIXSTORE_NONINTERACTIVE:-0}" = "1" ]]; then
    if [[ -n "${SUDO_ASKPASS:-}" ]]; then
        _sudo true || { info "sudo: authentication failed."; exit 1; }
    else
        sudo -n true || { info "sudo: credentials not cached."; exit 1; }
    fi
else
    sudo -v
fi
( while true; do sudo -n true; sleep 50; done ) </dev/null &>/dev/null &
SUDO_KEEPALIVE_PID=$!
# If the home-sync engine armed _hs_cleanup, it already kills this keepalive on
# exit (and restores writers) — re-trapping here would clobber it. Only install
# the plain keepalive cleanup when the engine trap is NOT in place.
if ! declare -F _hs_cleanup >/dev/null 2>&1; then
    trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null' EXIT
fi

# Sync /etc/nixos files from dotfiles
if [ -n "$DOTFILES" ] && [ -d "$DOTFILES/nixos" ]; then
    NIXOS_BASELINE_DIR="$FLAKE/.dotfiles-nixos-baseline"
    _sudo mkdir -p "$NIXOS_BASELINE_DIR"

    pci_to_nix() {
        local raw="${1%%.*}" bus="${1%%.*}" slot
        bus="${raw%%:*}"; slot="${raw##*:}"
        printf "PCI:%d:%d:0" "$((16#$bus))" "$((16#$slot))"
    }

    GPU_VARIANT="${DOTFILES_GPU_VARIANT:-intel}"
    TIMEZONE="${DOTFILES_TIMEZONE:-UTC}"
    KEYMAP="${DOTFILES_KEYMAP:-us}"
    LOCALE="${DOTFILES_LOCALE:-en_US.UTF-8}"
    BOOT_MODE="${DOTFILES_BOOT_MODE:-efi}"
    GRUB_DEVICE="${DOTFILES_GRUB_DEVICE:-}"

    for src in "$DOTFILES/nixos"/*; do
        [ -f "$src" ] || continue
        fname="$(basename "$src")"
        dest="$FLAKE/$fname"
        [ "$fname" = "flake.lock" ] && continue
        if [ "$fname" = "packages.json" ] && [ -f "$dest" ]; then
            skip "packages.json (managed by NixStore — skipping)"
            continue
        fi
        if [ "$fname" = "flake.nix" ] && [ -f "$dest" ]; then
            skip "flake.nix (managed by NixStore — skipping)"
            continue
        fi
        if [ "$fname" = "modules.json" ] && [ -f "$dest" ]; then
            skip "modules.json (managed by NixStore — skipping)"
            continue
        fi
        _efi_bool="false"; [ "$BOOT_MODE" = "efi" ] && _efi_bool="true"
        new=$(sed \
            -e "s/yourhostname/$HOSTNAME_VAR/g" \
            -e "s/yourusername/${DOTFILES_USERNAME:-$USER}/g" \
            -e "s|yourtimezone|$TIMEZONE|g" \
            -e "s/yourkbdlayout/$KEYMAP/g" \
            -e "s|yourlocale|$LOCALE|g" \
            -e "s/YOUREFIMODE/$_efi_bool/g" \
            -e "s|YOURGRUBDEVICE|${GRUB_DEVICE:-}|g" \
            "$src")
        baseline_file="$NIXOS_BASELINE_DIR/$fname"
        current=$(_sudo cat "$dest" 2>/dev/null || true)
        if [ -z "$current" ]; then
            echo "$new" | _sudo tee "$dest" > /dev/null
            echo "$new" | _sudo tee "$baseline_file" > /dev/null
            ok "new: $fname"
        elif [ "$new" = "$current" ]; then
            _sudo test -f "$baseline_file" || echo "$new" | _sudo tee "$baseline_file" > /dev/null
            skip "unchanged: $fname"
        else
            echo "$new" | _sudo tee "$dest" > /dev/null
            echo "$new" | _sudo tee "$baseline_file" > /dev/null
            ok "updated: $fname"
        fi
    done

    # GPU hardware-acceleration.nix
    gpu_src="$DOTFILES/nixos/gpu/$GPU_VARIANT.nix"
    if [ -f "$gpu_src" ]; then
        case "$GPU_VARIANT" in
            intel-nvidia)
                _intel=$(lspci 2>/dev/null | grep -i 'Intel.*VGA\|VGA.*Intel\|Intel.*Graphics' | awk '{print $1}' | head -1)
                _nv=$(lspci 2>/dev/null | grep -i 'NVIDIA.*VGA\|VGA.*NVIDIA' | awk '{print $1}' | head -1)
                _new=$(sed -e "s/INTEL_BUS_ID/$(pci_to_nix "$_intel")/g" -e "s/NVIDIA_BUS_ID/$(pci_to_nix "$_nv")/g" "$gpu_src")
                ;;
            amd-nvidia)
                _amd=$(lspci 2>/dev/null | grep -i 'AMD.*VGA\|VGA.*AMD\|Radeon' | awk '{print $1}' | head -1)
                _nv=$(lspci 2>/dev/null | grep -i 'NVIDIA.*VGA\|VGA.*NVIDIA' | awk '{print $1}' | head -1)
                _new=$(sed -e "s/AMD_BUS_ID/$(pci_to_nix "$_amd")/g" -e "s/NVIDIA_BUS_ID/$(pci_to_nix "$_nv")/g" "$gpu_src")
                ;;
            *) _new=$(cat "$gpu_src") ;;
        esac
        echo "$_new" | _sudo tee "$FLAKE/hardware-acceleration.nix" > /dev/null
        ok "hardware-acceleration.nix updated"
    fi
    echo ""
fi

bold "→ Updating flake inputs ..."
_sudo sh -c "cd '$FLAKE' && nix flake update" && ok "flake inputs updated" || info "flake update failed — continuing"

echo ""
bold "→ Running nixos-rebuild switch ..."
_sudo nixos-rebuild switch --flake "$FLAKE#$HOSTNAME_VAR"

if command -v flatpak &>/dev/null; then
    echo ""
    bold "→ Updating Flatpaks ..."
    flatpak update -y --user 2>&1 || info "flatpak update failed"
fi

if command -v hyprctl &>/dev/null && hyprctl monitors &>/dev/null 2>&1; then
    echo ""
    init_monitors="$HOME/.config/hypr/scripts/init-monitors.sh"
    if [ -f "$init_monitors" ]; then
        bold "→ Detecting monitors ..."
        bash "$init_monitors" && ok "monitors updated" || true
    fi
    bold "→ Reloading Hyprland ..."
    sleep 2
    hyprctl reload && ok "Hyprland reloaded" || info "hyprctl reload failed — reload manually"
fi

# ── Home-config conflict report ──────────────────────────────────────────────
# One consolidated report of any `track` conflicts / heal-updates recorded by
# the home-sync engine. The resolver is deployed as a dotfile (class `track`)
# to ~/.local/bin/nixpresso-resolve-conflicts by the sync above, so by this
# point it is present. Reference it by its deployed path; fall back to PATH.
if [ -n "$DOTFILES" ] && [ -d "$DOTFILES/home" ]; then
    _resolver="$HOME/.local/bin/nixpresso-resolve-conflicts"
    if [ -x "$_resolver" ]; then
        echo ""
        "$_resolver" || true
    elif command -v nixpresso-resolve-conflicts &>/dev/null; then
        echo ""
        nixpresso-resolve-conflicts || true
    fi
fi

echo ""
bold "Done!"
