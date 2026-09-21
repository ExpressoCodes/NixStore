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

# ── ~/.config + ~/.local/share (copied from dotfiles/home/) ──────────────────
if [ -n "$DOTFILES" ] && [ -d "$DOTFILES/home" ]; then
    bold "→ Syncing home config (~/.config, ~/.local/share) ..."

    _is_text_file() { grep -qI '' "$1" 2>/dev/null; }

    _backup_file() {
        local file="$1" stamp
        stamp=$(date +%Y%m%d-%H%M%S)
        cp "$file" "${file}.bak.${stamp}"
        echo "${file}.bak.${stamp}"
    }

    _sync_one_home_file() {
        local src="$1" src_base="$2" dst_base="$3"
        local rel dst baseline
        rel="${src#"$src_base"/}"
        dst="$dst_base/$rel"
        baseline="$HOME_STATE_DIR/${dst_base##"$HOME"/}/$rel"
        [[ "$src" =~ \.bak\.[0-9]{8}-[0-9]{6}$ ]] && return
        mkdir -p "$(dirname "$dst")" "$(dirname "$baseline")"
        if [ ! -f "$dst" ]; then
            cp "$src" "$dst"; cp "$src" "$baseline"
            ok "new: ~/${dst#"$HOME"/}"; return
        fi
        if cmp -s "$src" "$dst"; then
            [ -f "$baseline" ] || cp "$src" "$baseline"
            return
        fi
        if [[ "$src" == *.sh ]]; then
            local bak; bak=$(_backup_file "$dst")
            cp "$src" "$dst"; cp "$src" "$baseline"
            ok "updated (script): ~/${dst#"$HOME"/}"; return
        fi
        if [ ! -f "$baseline" ]; then
            if ! _is_text_file "$src"; then
                cp "$src" "$dst"; cp "$src" "$baseline"
                ok "updated (binary): ~/${dst#"$HOME"/}"; return
            fi
            if [[ "${NIXSTORE_NONINTERACTIVE:-0}" = "1" ]]; then
                cp "$src" "$dst"; cp "$src" "$baseline"
                ok "updated: ~/${dst#"$HOME"/}"; return
            fi
            bold "  ~/${dst#"$HOME"/} differs — update? [U/s]: "
            read -r ans; ans="${ans:-u}"
            if [[ "$ans" =~ ^[Uu] ]]; then
                cp "$src" "$dst"; cp "$src" "$baseline"
                ok "updated: ~/${dst#"$HOME"/}"
            else
                cp "$src" "$baseline"; skip "kept local: ~/${dst#"$HOME"/}"
            fi
            return
        fi
        cmp -s "$src" "$baseline" && return
        if ! _is_text_file "$src" || ! command -v diff3 &>/dev/null; then
            local bak; bak=$(_backup_file "$dst")
            cp "$src" "$dst"; cp "$src" "$baseline"
            ok "updated: ~/${dst#"$HOME"/}"; return
        fi
        local merged diff3_exit
        set +e; merged=$(diff3 -m "$dst" "$baseline" "$src" 2>/dev/null); diff3_exit=$?; set -e
        if [ "$diff3_exit" -eq 0 ]; then
            if [ "$merged" = "$(cat "$dst")" ]; then cp "$src" "$baseline"
            else printf '%s\n' "$merged" > "$dst"; cp "$src" "$baseline"; ok "merged: ~/${dst#"$HOME"/}"; fi
        else
            local bak; bak=$(_backup_file "$dst")
            cp "$src" "$dst"; cp "$src" "$baseline"
            printf '  \033[33m!\033[0m conflict in ~/%s — backup: %s\n' "${dst#"$HOME"/}" "$(basename "$bak")"
        fi
    }

    _sync_home_files() {
        local src_base="$1" dst_base="$2"
        [ -d "$src_base" ] || return 0
        while IFS= read -r -d '' src; do
            _sync_one_home_file "$src" "$src_base" "$dst_base"
        done < <(find "$src_base" -type f -print0)
    }

    _sync_home_files "$DOTFILES/home/.config"      "$HOME/.config"
    _sync_home_files "$DOTFILES/home/.local/share" "$HOME/.local/share"
    _sync_home_files "$DOTFILES/home/.local/bin"   "$HOME/.local/bin"
    [ -d "$HOME/.local/bin" ] && chmod +x "$HOME/.local/bin"/* 2>/dev/null || true
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
( while true; do sudo -n true; sleep 50; done ) &
SUDO_KEEPALIVE_PID=$!
trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null' EXIT

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

echo ""
bold "Done!"
