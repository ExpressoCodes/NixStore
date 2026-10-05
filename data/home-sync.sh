#!/usr/bin/env bash
# lib/home-sync.sh — shared home-config deployment engine for NixPresso.
#
# Sourced by install.sh (SEED mode) and update.sh (MERGE mode). Deploys the
# dotfiles `home/` tree into $HOME by COPY (never symlink), so the clone is
# disposable, while never clobbering user customizations.
#
# Design contract (council-reviewed):
#   * Every destructive step (take-repo overwrite, upstream-delete, dir heal)
#     is crash-safe: write-ahead journal + fsync BEFORE the irreversible mutate,
#     then an idempotent reconciliation pass on the next entry.
#   * Baseline for track / track-conservative is ALWAYS seeded from SHIPPED-NEW
#     ($DOTFILES/home/<rel>) — never the live file, never the clone HEAD.
#   * Restore invariant = option (b): a conflict resolution rewrites ONLY the
#     live file; the baseline stays at N (shipped-new), so a restored file is
#     treated as a user modification and is 3-way merged on the next update.
#   * set -euo pipefail safe: every `git` call is wrapped so a nonzero exit can
#     never abort the caller.
#
# This file defines functions only; it performs no work when sourced.

# ── Configuration (all overridable, e.g. for sandbox tests) ──────────────────
: "${DOTFILES:?home-sync.sh: DOTFILES must be set before sourcing}"
HS_HOME="${HS_HOME:-$HOME}"
HS_DOTHOME="${HS_DOTHOME:-$DOTFILES/home}"
HS_STATE_DIR="${HS_STATE_DIR:-${HOME_STATE_DIR:-$HS_HOME/.local/share/dotfiles-home-state}}"
HS_MANIFEST="${HS_MANIFEST:-$HS_DOTHOME/.dotfiles-manifest}"
HS_STATE_ROOT="${HS_STATE_ROOT:-$HS_HOME/.local/state/nixpresso}"
HS_CONFLICT_ROOT="${HS_CONFLICT_ROOT:-$HS_STATE_ROOT/conflicts}"
HS_HEAL_DIR="${HS_HEAL_DIR:-$HS_STATE_ROOT/heal}"
# Top-level roots that hs_selfheal / hs_check consider for directory-granular heal.
HS_CONFIG_ROOT="${HS_CONFIG_ROOT:-$HS_HOME/.config}"
HS_LOCALSHARE_ROOT="${HS_LOCALSHARE_ROOT:-$HS_HOME/.local/share}"
HS_LOCALBIN_ROOT="${HS_LOCALBIN_ROOT:-$HS_HOME/.local/bin}"
# Optional privilege prefix for write primitives (e.g. "sudo" when install.sh
# seeds another user's home). Empty = run directly (the common, self-user path).
HS_SUDO="${HS_SUDO:-}"
_hs_do() { if [ -n "$HS_SUDO" ]; then $HS_SUDO "$@"; else "$@"; fi; }

# ── Logging (reuse caller's helpers if present) ──────────────────────────────
if ! declare -F bold >/dev/null 2>&1; then bold() { printf '\033[1m%s\033[0m\n' "$*" >&2; }; fi
if ! declare -F info >/dev/null 2>&1; then info() { printf '  %s\n' "$*" >&2; }; fi
if ! declare -F ok   >/dev/null 2>&1; then ok()   { printf '  \033[32m*\033[0m %s\n' "$*" >&2; }; fi
if ! declare -F skip >/dev/null 2>&1; then skip() { printf '  \033[33m-\033[0m %s\n' "$*" >&2; }; fi
_hs_warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
_hs_err()  { printf '  \033[31mx\033[0m %s\n' "$*" >&2; }

# ── Small utilities ──────────────────────────────────────────────────────────

# fsync a file (best effort; `sync -d` syncs just that FD on GNU coreutils).
_hs_fsync() { sync -d "$1" 2>/dev/null || sync; }
_hs_fsync_dir() { sync -d "$1" 2>/dev/null || sync; }

_hs_sha256() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

# Device id of the filesystem that holds $1 (or its parent dir if $1 is absent).
_hs_dev() {
    local p="$1"
    [ -e "$p" ] || p="$(dirname "$p")"
    stat -c '%d' "$p" 2>/dev/null || echo "?"
}

# Flatten an absolute path into a single journal-safe filename.
_hs_flat() { printf '%s' "$1" | sed 's|^/||; s|/|%|g'; }

_hs_is_text() { grep -qI '' "$1" 2>/dev/null; }

# home-relative path of an arbitrary path WITHOUT resolving symlinks.
_hs_relhome() { realpath -s --relative-to="$HS_HOME" "$1" 2>/dev/null; }

# Resolved target of the first symlinked ancestor of $1, or "(real)".
_hs_symlink_target() {
    local p="$1"
    while [ "$p" != "/" ] && [ "$p" != "." ] && [ -n "$p" ]; do
        if [ -L "$p" ]; then readlink -f "$p" 2>/dev/null || echo "(dangling)"; return; fi
        p="$(dirname "$p")"
    done
    printf '(real)'
}

# Does $1 have any symlinked ancestor (up to HS_HOME)?
_hs_has_symlink_ancestor() {
    local p="$1"
    while [ "$p" != "$HS_HOME" ] && [ "$p" != "/" ] && [ -n "$p" ] && [ "$p" != "." ]; do
        [ -L "$p" ] && return 0
        p="$(dirname "$p")"
    done
    [ -L "$HS_HOME" ] && return 0
    return 1
}

# git toplevel of the clone that owns $1 (empty if none). Wrapped: never aborts.
_hs_cloneroot() {
    local out
    if out=$(git -C "$1" rev-parse --show-toplevel 2>/dev/null); then
        printf '%s' "$out"
    fi
}

# "dirty" / "clean" / "n/a" for the clone that owns $1 (diagnostic only).
# Resolve through any symlink and use the containing directory, so a file that
# is (or links into) a git working tree is detected.
_hs_clone_state() {
    local probe root
    probe="$(readlink -f "$1" 2>/dev/null || true)"
    [ -n "$probe" ] && [ -e "$probe" ] && probe="$(dirname "$probe")" || probe="$1"
    root="$(_hs_cloneroot "$probe")"
    [ -z "$root" ] && { printf 'n/a'; return; }
    local out
    # Pure diagnostic: --no-optional-locks keeps git from touching .git/index.lock
    # (and fsmonitor disabled so no helper is spawned). Wrapped so a nonzero git
    # exit can never abort the caller; degrade to n/a.
    if out=$(git -C "$root" --no-optional-locks -c core.fsmonitor=false status --porcelain 2>/dev/null); then
        [ -n "$out" ] && printf 'dirty' || printf 'clean'
    else
        printf 'n/a'
    fi
}

# ── Manifest classification ──────────────────────────────────────────────────
_HS_GLOBS=()
_HS_CLASSES=()
_HS_MANIFEST_LOADED=0

_hs_load_manifest() {
    [ "$_HS_MANIFEST_LOADED" = 1 ] && return 0
    _HS_GLOBS=(); _HS_CLASSES=()
    if [ -f "$HS_MANIFEST" ]; then
        local line glob class
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line%%#*}"
            # trim
            line="${line#"${line%%[![:space:]]*}"}"
            line="${line%"${line##*[![:space:]]}"}"
            [ -z "$line" ] && continue
            read -r glob class <<<"$line"
            [ -z "$glob" ] && continue
            [ -z "$class" ] && class="track-conservative"
            _HS_GLOBS+=("$glob")
            _HS_CLASSES+=("$class")
        done < "$HS_MANIFEST"
    fi
    _HS_MANIFEST_LOADED=1
}

# Convert a manifest glob to an anchored regex.
_hs_glob_to_regex() {
    local glob="$1" re="" i c n
    n=${#glob}
    for (( i=0; i<n; i++ )); do
        c="${glob:i:1}"
        case "$c" in
            '*')
                if [ "${glob:i+1:1}" = '*' ]; then re+='.*'; (( i++ )); else re+='[^/]*'; fi ;;
            '?') re+='[^/]' ;;
            '.'|'('|')'|'['|']'|'{'|'}'|'+'|'^'|'$'|'|'|'\\') re+="\\$c" ;;
            *) re+="$c" ;;
        esac
    done
    printf '^%s$' "$re"
}

# hs_classify <rel-to-home> -> class (default track-conservative).
hs_classify() {
    _hs_load_manifest
    local rel="$1" i re
    for (( i=0; i<${#_HS_GLOBS[@]}; i++ )); do
        re="$(_hs_glob_to_regex "${_HS_GLOBS[$i]}")"
        if [[ "$rel" =~ $re ]]; then printf '%s' "${_HS_CLASSES[$i]}"; return 0; fi
    done
    printf 'track-conservative'
}

# Convenience: baseline store path for a home-relative path.
_hs_baseline_path() { printf '%s/%s' "$HS_STATE_DIR" "$1"; }
# Convenience: shipped-new path for a home-relative path.
_hs_shipped_path()  { printf '%s/%s' "$HS_DOTHOME" "$1"; }

# ── Atomic install (model: init-monitors.sh) ─────────────────────────────────
# hs_atomic_install <srcfile> <dest> <mode>
#   tmp on SAME fs as dest, cp, chmod, fsync tmp, mv -f, fsync destdir.
hs_atomic_install() {
    local src="$1" dest="$2" mode="$3"
    local destdir; destdir="$(dirname "$dest")"
    _hs_do mkdir -p "$destdir"
    # Enforce same-fs temp (never $TMPDIR / scratchpad): mktemp inside destdir.
    local tmp
    tmp="$(_hs_do mktemp "$destdir/.tmp.XXXXXX")" || { _hs_err "mktemp failed in $destdir"; return 1; }
    if [ "$(_hs_dev "$tmp")" != "$(_hs_dev "$dest")" ]; then
        _hs_do rm -f "$tmp"; _hs_err "temp not on same fs as $dest"; return 1
    fi
    if ! _hs_do cp "$src" "$tmp"; then _hs_do rm -f "$tmp"; _hs_err "cp failed: $src"; return 1; fi
    _hs_do chmod "$mode" "$tmp" 2>/dev/null || true
    _hs_fsync "$tmp"
    if ! _hs_do mv -f "$tmp" "$dest"; then _hs_do rm -f "$tmp"; _hs_err "mv failed: $dest"; return 1; fi
    _hs_fsync_dir "$destdir"
    return 0
}

# Mode an installed file should carry, from its home-relative path.
_hs_mode_for() {
    case "$1" in
        .local/bin/*) printf '755' ;;
        .config/hypr/scripts/*) printf '755' ;;
        .config/quickshell/*.sh) printf '755' ;;
        *.sh) printf '755' ;;
        *) printf '644' ;;
    esac
}

# ── Writer quiesce / resume (CALLED only in a real run, never in --check) ────
hs_quiesce_writers() {
    # Mark that writers are DOWN so a real-run safety-net trap knows it must
    # resume them if the script aborts before hs_resume_writers runs. Global
    # on purpose (shared with update.sh's cleanup handler).
    _hs_quiesced=1
    pkill -x quickshell 2>/dev/null || true
    local waited=0
    while pgrep -x quickshell >/dev/null 2>&1; do
        sleep 0.1
        waited=$(( waited + 1 ))
        [ "$waited" -gt 100 ] && { _hs_warn "quickshell still running after 10s; continuing"; break; }
    done
    ok "writers quiesced (quickshell stopped)"
}

hs_resume_writers() {
    local qr="$HS_LOCALBIN_ROOT/qs-restart"
    if [ -x "$qr" ]; then
        "$qr" >/dev/null 2>&1 || _hs_warn "qs-restart failed"
    else
        _hs_warn "qs-restart not found; skipping writer relaunch"
    fi
    # hyprctl reload LAST, after writers are back.
    if command -v hyprctl >/dev/null 2>&1; then
        hyprctl reload >/dev/null 2>&1 || _hs_warn "hyprctl reload failed"
    fi
    # Writers are back: clear the quiesce flag so the safety-net trap becomes a
    # harmless no-op (idempotent — resuming when already resumed does nothing).
    _hs_quiesced=0
    ok "writers resumed"
}

# ── Baseline seeding helper (shipped-new semantics) ──────────────────────────
# Seed/refresh the baseline for one home-relative file according to its class.
#   track / track-conservative : baseline := shipped-new
#   seed-once                   : no baseline
#   generated                   : no baseline + purge any stale baseline
_hs_seed_baseline() {
    local rel="$1" class="$2"
    local shipped baseline
    shipped="$(_hs_shipped_path "$rel")"
    baseline="$(_hs_baseline_path "$rel")"
    case "$class" in
        track|track-conservative)
            [ -f "$shipped" ] || return 0
            hs_atomic_install "$shipped" "$baseline" 644
            ;;
        generated)
            if [ -e "$baseline" ]; then rm -f "$baseline"; fi
            ;;
        seed-once|ignore) : ;;
    esac
    return 0
}

# Seed baselines for every shipped file that lives under a given dest dir D.
_hs_heal_seed_baselines() {
    local D="$1"
    local drel; drel="$(_hs_relhome "$D")"
    local shippeddir="$HS_DOTHOME/$drel"
    [ -d "$shippeddir" ] || return 0
    local f rel class
    while IFS= read -r -d '' f; do
        rel="$drel/$(realpath --relative-to="$shippeddir" "$f")"
        class="$(hs_classify "$rel")"
        _hs_seed_baseline "$rel" "$class"
    done < <(find "$shippeddir" -type f -print0)
}

# Fix permissions on a freshly materialized dest dir.
_hs_heal_fix_perms() {
    local D="$1"
    local drel; drel="$(_hs_relhome "$D")"
    local f rel
    while IFS= read -r -d '' f; do
        rel="$drel/$(realpath --relative-to="$D" "$f")"
        chmod "$(_hs_mode_for "$rel")" "$f" 2>/dev/null || true
    done < <(find "$D" -type f -print0)
}

# ── First-heal take-repo decision (track / track-conservative only) ──────────
# A file healed out of a symlink into a (possibly STALE) clone is, by default,
# materialized as-is and kept-live. That silently pins genuinely unmodified but
# stale files to the old clone HEAD, so an upstream feature never lands. The
# decision below detects the UNMODIFIED-STALE case and takes the shipped-new
# (repo) version instead — non-destructively (the replaced original is backed
# up into the resolver store as a recoverable `heal-update` entry).
#
# PROOF of "unmodified": the originating clone's working tree for that path must
# be CLEAN vs HEAD and the live content L must be byte-exact with HEAD (H). Any
# doubt (H unavailable/untracked, dirty tree, N missing, L!=H) → keep-live.
# seed-once / generated NEVER participate — they go through _hs_seed_baseline.

# _hs_heal_probe <L-file> <clone_src> <N-file> [<H-dest>]
#   Pure, READ-ONLY, crash-proof. Echoes a decision token:
#     take-update  L==H, clean, N exists, L!=N  → should take repo N
#     take-noop    L==H, clean, N exists, L==N  → already equal (no write)
#     keep         anything else (the safe fallback)
#   Every git call is wrapped (never aborts the caller). With --no-optional-locks
#   git never mutates the clone, so this is safe in --check.
#   If <H-dest> is given and the decision is take-*, HEAD content is copied there
#   (for an optional `.base` backup blob); otherwise it is discarded.
_hs_heal_probe() {
    local L="$1" clone_src="$2" N="$3" hdest="${4:-}"
    [ -f "$L" ] && [ -f "$N" ] && [ -n "$clone_src" ] || { printf 'keep'; return 0; }
    local probe cloneroot rel_in_clone
    probe="$(readlink -f "$clone_src" 2>/dev/null || printf '%s' "$clone_src")"
    cloneroot="$(_hs_cloneroot "$(dirname "$probe")")"
    [ -n "$cloneroot" ] && [ -d "$cloneroot" ] || { printf 'keep'; return 0; }
    rel_in_clone="$(realpath --relative-to="$cloneroot" "$probe" 2>/dev/null)"
    [ -n "$rel_in_clone" ] || { printf 'keep'; return 0; }
    local htmp gex dex
    htmp="$(mktemp "${TMPDIR:-/tmp}/.hsprobe.XXXXXX" 2>/dev/null)" || { printf 'keep'; return 0; }
    set +e
    git -C "$cloneroot" --no-optional-locks -c core.fsmonitor=false \
        show "HEAD:$rel_in_clone" > "$htmp" 2>/dev/null
    gex=$?
    git -C "$cloneroot" --no-optional-locks -c core.fsmonitor=false \
        diff --quiet HEAD -- "$rel_in_clone" 2>/dev/null
    dex=$?
    set -e
    local out='keep'
    if [ "$gex" -eq 0 ] && [ "$dex" -eq 0 ] && cmp -s "$L" "$htmp"; then
        if cmp -s "$L" "$N"; then out='take-noop'; else out='take-update'; fi
        if [ "$out" != keep ] && [ -n "$hdest" ]; then cp "$htmp" "$hdest" 2>/dev/null || true; fi
    fi
    rm -f "$htmp" 2>/dev/null || true
    printf '%s' "$out"
}

# _hs_heal_take_repo_decide <rel> <class> <live> <clone_src>
#   Applied right after a file is materialized out of a symlinked clone, BEFORE
#   the first hs_sync_tree. Performs the first-heal decision for track /
#   track-conservative; all other classes defer to _hs_seed_baseline unchanged.
#   Postcondition (both branches): baseline == shipped-new N (engine contract).
_hs_heal_take_repo_decide() {
    local rel="$1" class="$2" live="$3" clone_src="$4"
    case "$class" in
        track|track-conservative) : ;;
        *) _hs_seed_baseline "$rel" "$class"; return 0 ;;
    esac
    local shipped; shipped="$(_hs_shipped_path "$rel")"
    local htmp=""
    htmp="$(mktemp "${TMPDIR:-/tmp}/.hsbase.XXXXXX" 2>/dev/null)" || htmp=""
    local decision
    decision="$(_hs_heal_probe "$live" "$clone_src" "$shipped" "$htmp")"
    case "$decision" in
        take-update)
            # UNMODIFIED-STALE and differs from repo → take repo, backup L first.
            if ! hs_heal_update "$live" "$shipped" "$rel" "$class" "$htmp"; then
                _hs_seed_baseline "$rel" "$class"   # backup/install failed → keep live
            fi
            ;;
        *)
            # take-noop (already equal) or keep (unprovable) → keep live.
            _hs_seed_baseline "$rel" "$class"
            ;;
    esac
    [ -n "$htmp" ] && rm -f "$htmp" 2>/dev/null || true
    return 0
}

# Per-file first-heal decision over a freshly materialized dir D (clone T).
_hs_heal_decide_dir() {
    local D="$1" T="$2"
    local drel; drel="$(_hs_relhome "$D")"
    local f within rel class
    while IFS= read -r -d '' f; do
        within="$(realpath --relative-to="$D" "$f")"
        rel="$drel/$within"; rel="${rel#./}"
        class="$(hs_classify "$rel")"
        _hs_heal_take_repo_decide "$rel" "$class" "$f" "$T/$within"
    done < <(find "$D" -type f -print0)
}

# ── heal-journal (crash-safe directory heal) ─────────────────────────────────
# Minimal hand-rolled JSON (jq may be absent). Values are path-safe strings.
_hs_json_escape() { printf '%s' "$1" | sed 's|\\|\\\\|g; s|"|\\"|g'; }

_hs_heal_journal_write() {
    # <jrnl> <status> <D> <T> <payload>
    local jrnl="$1" status="$2" D="$3" T="$4" payload="$5"
    mkdir -p "$(dirname "$jrnl")"
    local tmp; tmp="$(mktemp "$(dirname "$jrnl")/.hj.XXXXXX")"
    {
        printf '{\n'
        printf '  "status": "%s",\n' "$(_hs_json_escape "$status")"
        printf '  "D": "%s",\n'      "$(_hs_json_escape "$D")"
        printf '  "T": "%s",\n'      "$(_hs_json_escape "$T")"
        printf '  "payload": "%s"\n' "$(_hs_json_escape "$payload")"
        printf '}\n'
    } > "$tmp"
    _hs_fsync "$tmp"
    mv -f "$tmp" "$jrnl"
    _hs_fsync_dir "$(dirname "$jrnl")"
}

_hs_json_get() {
    # <file> <key> : extract a top-level "key": "value" string value.
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\\(.*\\)\".*/\\1/p" "$1" 2>/dev/null | head -1
}

# ── hs_selfheal — directory-granular, idempotent, crash-safe ─────────────────
hs_selfheal() {
    local name src_d D
    # .config/* and .local/share/* : whole-dir heal for shipped top-level dirs.
    if [ -d "$HS_DOTHOME/.config" ]; then
        for src_d in "$HS_DOTHOME/.config"/*/; do
            [ -d "$src_d" ] || continue
            name="$(basename "$src_d")"
            _hs_heal_dir "$HS_CONFIG_ROOT/$name"
        done
    fi
    if [ -d "$HS_DOTHOME/.local/share" ]; then
        for src_d in "$HS_DOTHOME/.local/share"/*/; do
            [ -d "$src_d" ] || continue
            name="$(basename "$src_d")"
            _hs_heal_dir "$HS_LOCALSHARE_ROOT/$name"
        done
    fi
    # .local/bin/* : per-file heal.
    if [ -d "$HS_DOTHOME/.local/bin" ]; then
        local f
        for f in "$HS_DOTHOME/.local/bin"/*; do
            [ -e "$f" ] || continue
            _hs_heal_file "$HS_LOCALBIN_ROOT/$(basename "$f")"
        done
    fi
}

_hs_heal_dir() {
    local D="$1"
    local jrnl="$HS_HEAL_DIR/$(_hs_flat "$D").json"

    # Partial-heal reconciliation on entry.
    if [ -f "$jrnl" ]; then
        local status payload
        status="$(_hs_json_get "$jrnl" status)"
        payload="$(_hs_json_get "$jrnl" payload)"
        if [ "$status" = complete ] && [ -d "$D" ] && [ ! -L "$D" ]; then
            _hs_heal_seed_baselines "$D"     # idempotent: ensure baselines present
            return 0
        fi
        if { [ ! -e "$D" ] || [ -L "$D" ]; } && [ -n "$payload" ] && [ -d "$payload" ]; then
            # Interrupted between "rm link" and "mv dir": finish the commit.
            [ -L "$D" ] && rm -f "$D"
            mv -T "$payload" "$D"
            _hs_fsync_dir "$(dirname "$D")"
            _hs_heal_fix_perms "$D"
            _hs_heal_seed_baselines "$D"
            _hs_heal_journal_write "$jrnl" complete "$D" "$(_hs_json_get "$jrnl" T)" ""
            ok "heal finished (resumed): ${D#"$HS_HOME"/}"
            return 0
        fi
    fi

    if [ -L "$D" ]; then
        _hs_heal_symlink_dir "$D" "$jrnl"
    elif [ -d "$D" ]; then
        _hs_heal_nested "$D"
        _hs_heal_seed_baselines "$D"
    elif [ ! -e "$D" ]; then
        # Nothing live yet: install shipped-new fresh, baseline := shipped-new.
        _hs_install_shipped_dir "$D"
    fi
}

# Copy the shipped-new tree for D into place (D must not exist) + baselines.
_hs_install_shipped_dir() {
    local D="$1"
    local drel; drel="$(_hs_relhome "$D")"
    local shippeddir="$HS_DOTHOME/$drel"
    [ -d "$shippeddir" ] || return 0
    local f rel
    while IFS= read -r -d '' f; do
        rel="$drel/$(realpath --relative-to="$shippeddir" "$f")"
        hs_atomic_install "$f" "$HS_HOME/$rel" "$(_hs_mode_for "$rel")"
        _hs_seed_baseline "$rel" "$(hs_classify "$rel")"
    done < <(find "$shippeddir" -type f -print0)
    ok "installed shipped-new: ${D#"$HS_HOME"/}"
}

_hs_heal_symlink_dir() {
    local D="$1" jrnl="$2"
    local T; T="$(readlink -f "$D" 2>/dev/null || true)"

    if [ -z "$T" ] || [ ! -e "$T" ]; then
        # Dangling link → remove the link only, install shipped-new.
        rm -f "$D"
        _hs_install_shipped_dir "$D"
        _hs_heal_journal_write "$jrnl" complete "$D" "${T:-}" ""
        return 0
    fi

    # Enumerate + classify ALL files under T BEFORE materializing.
    local drel; drel="$(_hs_relhome "$D")"
    local payload_parent; payload_parent="$(dirname "$D")"
    local healtmp
    healtmp="$(mktemp -d "$payload_parent/.heal.XXXXXX")" || { _hs_err "heal mktemp failed"; return 1; }
    if [ "$(_hs_dev "$healtmp")" != "$(_hs_dev "$D")" ]; then
        rm -rf "$healtmp"; _hs_err "heal temp not on same fs as $D"; return 1
    fi
    local payload="$healtmp/payload"
    if ! cp -rT "$T" "$payload"; then rm -rf "$healtmp"; _hs_err "cp -rT failed: $T"; return 1; fi

    # Hash-verify every copied file vs its source.
    local f rel within srchash dsthash
    while IFS= read -r -d '' f; do
        within="$(realpath --relative-to="$payload" "$f")"
        srchash="$(_hs_sha256 "$T/$within")"
        dsthash="$(_hs_sha256 "$f")"
        if [ -z "$srchash" ] || [ "$srchash" != "$dsthash" ]; then
            rm -rf "$healtmp"; _hs_err "heal hash mismatch: $within"; return 1
        fi
    done < <(find "$payload" -type f -print0)

    # WRITE-AHEAD journal (fsync) BEFORE mutating D (rm link; mv dir not atomic).
    _hs_heal_journal_write "$jrnl" intended "$D" "$T" "$payload"

    # Commit all-or-nothing: rm the LINK only, then mv payload into place.
    rm -f "$D"
    if ! mv -T "$payload" "$D"; then
        _hs_err "heal mv failed; journal left for reconciliation: $D"; return 1
    fi
    _hs_fsync_dir "$payload_parent"
    rmdir "$healtmp" 2>/dev/null || true

    _hs_heal_fix_perms "$D"
    # First-heal decision per file (take-repo for unmodified-stale, else keep
    # live); always leaves baseline := shipped-new. Replaces the plain baseline
    # seed so the ongoing 3-way sync below is unchanged.
    _hs_heal_decide_dir "$D" "$T"
    _hs_heal_journal_write "$jrnl" complete "$D" "$T" ""
    ok "healed dir (copy from $T): ${D#"$HS_HOME"/}"
    # drel referenced to keep shellcheck happy about intent.
    : "$drel"
}

# Defensively materialize any symlinked subtrees nested inside a REAL dir D.
_hs_heal_nested() {
    local D="$1" sub
    while IFS= read -r -d '' sub; do
        [ -L "$sub" ] || continue
        _hs_heal_dir "$sub"
    done < <(find "$D" -mindepth 1 -maxdepth 1 -type l -print0 2>/dev/null)
}

# Per-file heal for ~/.local/bin/* (and other single-file managed paths).
_hs_heal_file() {
    local dest="$1"
    local rel; rel="$(_hs_relhome "$dest")"
    local class; class="$(hs_classify "$rel")"
    local shipped; shipped="$(_hs_shipped_path "$rel")"
    if [ -L "$dest" ]; then
        local T; T="$(readlink -f "$dest" 2>/dev/null || true)"
        rm -f "$dest"
        if [ -n "$T" ] && [ -f "$T" ]; then
            hs_atomic_install "$T" "$dest" "$(_hs_mode_for "$rel")"
            # First-heal decision using the originating clone (T).
            _hs_heal_take_repo_decide "$rel" "$class" "$dest" "$T"
        elif [ -f "$shipped" ]; then
            hs_atomic_install "$shipped" "$dest" "$(_hs_mode_for "$rel")"
            _hs_seed_baseline "$rel" "$class"
        else
            _hs_seed_baseline "$rel" "$class"
        fi
        ok "healed file: ${dest#"$HS_HOME"/}"
    elif [ ! -e "$dest" ] && [ -f "$shipped" ]; then
        hs_atomic_install "$shipped" "$dest" "$(_hs_mode_for "$rel")"
        _hs_seed_baseline "$rel" "$class"
    else
        # Already real: ensure baseline exists.
        local baseline; baseline="$(_hs_baseline_path "$rel")"
        [ -f "$baseline" ] || _hs_seed_baseline "$rel" "$class"
    fi
}

# ── Mechanic-1: conflict overwrite (track only), crash-safe ──────────────────
_HS_RUNDIR=""
_hs_rundir() {
    if [ -z "$_HS_RUNDIR" ]; then
        local ts; ts="$(date +%Y%m%d-%H%M%S)"
        _HS_RUNDIR="$HS_CONFLICT_ROOT/${ts}-$$"
        mkdir -p "$_HS_RUNDIR"
    fi
    printf '%s' "$_HS_RUNDIR"
}

# Append a journal.jsonl record for a conflict entry (hand-rolled JSON).
_hs_conflict_journal() {
    # <rundir> <dest> <rel> <class> <ub> <bb> <nb> <su> <sb> <sn> <status>
    local rundir="$1" dest="$2" rel="$3" class="$4" ub="$5" bb="$6" nb="$7"
    local su="$8" sb="$9" sn="${10}" status="${11}"
    local jl="$rundir/journal.jsonl"
    {
        printf '{"dest":"%s",'   "$(_hs_json_escape "$dest")"
        printf '"rel":"%s",'     "$(_hs_json_escape "$rel")"
        printf '"class":"%s",'   "$(_hs_json_escape "$class")"
        printf '"user_blob":"%s",' "$(_hs_json_escape "$ub")"
        printf '"base_blob":"%s",' "$(_hs_json_escape "$bb")"
        printf '"repo_blob":"%s",' "$(_hs_json_escape "$nb")"
        printf '"sha_user":"%s",' "$(_hs_json_escape "$su")"
        printf '"sha_base":"%s",' "$(_hs_json_escape "$sb")"
        printf '"sha_repo":"%s",' "$(_hs_json_escape "$sn")"
        printf '"action":"overwrite-repo",'
        printf '"status":"%s"}\n' "$(_hs_json_escape "$status")"
    } >> "$jl"
    _hs_fsync "$jl"
    _hs_fsync_dir "$rundir"
}

# Append a journal.jsonl record for a heal-update entry (first-heal take-repo).
# Same store + schema as a conflict, but action="heal-update" so the resolver
# can clearly distinguish it from a real `overwrite-repo` merge conflict.
_hs_heal_update_journal() {
    # <rundir> <dest> <rel> <class> <ub> <bb> <nb> <su> <sb> <sn> <status>
    local rundir="$1" dest="$2" rel="$3" class="$4" ub="$5" bb="$6" nb="$7"
    local su="$8" sb="$9" sn="${10}" status="${11}"
    local jl="$rundir/journal.jsonl"
    {
        printf '{"dest":"%s",'   "$(_hs_json_escape "$dest")"
        printf '"rel":"%s",'     "$(_hs_json_escape "$rel")"
        printf '"class":"%s",'   "$(_hs_json_escape "$class")"
        printf '"user_blob":"%s",' "$(_hs_json_escape "$ub")"
        printf '"base_blob":"%s",' "$(_hs_json_escape "$bb")"
        printf '"repo_blob":"%s",' "$(_hs_json_escape "$nb")"
        printf '"sha_user":"%s",' "$(_hs_json_escape "$su")"
        printf '"sha_base":"%s",' "$(_hs_json_escape "$sb")"
        printf '"sha_repo":"%s",' "$(_hs_json_escape "$sn")"
        printf '"action":"heal-update",'
        printf '"status":"%s"}\n' "$(_hs_json_escape "$status")"
    } >> "$jl"
    _hs_fsync "$jl"
    _hs_fsync_dir "$rundir"
}

# hs_heal_update <live-L> <repo-N> <rel> <class> [<H-file>]
#   Non-destructive first-heal take-repo: back up the replaced original L into
#   the resolver store (action=heal-update) with the SAME crash-safe ordering as
#   the conflict mechanic — write+fsync+hash-verify the L backup BEFORE the
#   atomic install of N — then install live := N and advance baseline := N.
#   Returns 0 on applied, 1 on error (caller then falls back to keep-live).
hs_heal_update() {
    local live="$1" repo="$2" rel="$3" class="$4" hfile="${5:-}"
    local rundir; rundir="$(_hs_rundir)"
    local blobdir="$rundir/$(dirname "$rel")"
    mkdir -p "$blobdir"
    local base="$rundir/$rel"
    local ub="$base.user" bb="$base.base" nb="$base.repo"

    # (i) snapshot the original L (.user) and the repo N (.repo); H (.base) opt.
    cp "$live" "$ub" || { _hs_err "heal-update: cannot snapshot original $rel"; return 1; }
    cp "$repo" "$nb" || { _hs_err "heal-update: cannot snapshot repo $rel"; return 1; }
    local have_base=0
    if [ -n "$hfile" ] && [ -f "$hfile" ]; then cp "$hfile" "$bb" 2>/dev/null && have_base=1; fi
    # (ii) fsync blobs + dir.
    _hs_fsync "$ub"; _hs_fsync "$nb"; [ "$have_base" = 1 ] && _hs_fsync "$bb"; _hs_fsync_dir "$blobdir"
    # (iii) re-read + hash-verify the L backup (and N) vs source BEFORE overwrite.
    local su sn sb=""
    su="$(_hs_sha256 "$ub")"; sn="$(_hs_sha256 "$nb")"
    if [ "$su" != "$(_hs_sha256 "$live")" ] || [ "$sn" != "$(_hs_sha256 "$repo")" ]; then
        _hs_err "heal-update: backup verify failed for $rel — leaving live intact"
        return 1
    fi
    [ "$have_base" = 1 ] && sb="$(_hs_sha256 "$bb")" || bb=""
    # (iv) journal the recoverable backup (durable) BEFORE the overwrite.
    _hs_heal_update_journal "$rundir" "$live" "$rel" "$class" "$ub" "$bb" "$nb" "$su" "$sb" "$sn" applied
    # (v) overwrite live with repo N (atomic), then advance baseline := N.
    if ! hs_atomic_install "$nb" "$live" "$(_hs_mode_for "$rel")"; then
        _hs_err "heal-update: overwrite failed for $rel"; return 1
    fi
    hs_atomic_install "$nb" "$(_hs_baseline_path "$rel")" 644
    ok "updated $rel to repo version (previous backed up)"
    return 0
}

# hs_conflict_overwrite <dest-live-U> <baseline-B> <repo-N> <rel> <class>
# EXACT ordering per spec. Returns 0 on applied, 1 on aborted/errored.
hs_conflict_overwrite() {
    local live="$1" baseline="$2" repo="$3" rel="$4" class="$5"
    local rundir; rundir="$(_hs_rundir)"
    local blobdir="$rundir/$(dirname "$rel")"
    mkdir -p "$blobdir"
    local base="$rundir/$rel"
    local ub="$base.user" bb="$base.base" nb="$base.repo"

    # (i) write blobs
    cp "$live" "$ub"      || { _hs_err "conflict: cannot snapshot user $rel"; return 1; }
    cp "$baseline" "$bb"  || { _hs_err "conflict: cannot snapshot base $rel"; return 1; }
    cp "$repo" "$nb"      || { _hs_err "conflict: cannot snapshot repo $rel"; return 1; }
    # (ii) fsync blobs + dir
    _hs_fsync "$ub"; _hs_fsync "$bb"; _hs_fsync "$nb"; _hs_fsync_dir "$blobdir"
    # (iii) re-read + hash-verify each blob vs source — mismatch aborts THIS file.
    local su sb sn
    su="$(_hs_sha256 "$ub")"; sb="$(_hs_sha256 "$bb")"; sn="$(_hs_sha256 "$nb")"
    if [ "$su" != "$(_hs_sha256 "$live")" ] || \
       [ "$sb" != "$(_hs_sha256 "$baseline")" ] || \
       [ "$sn" != "$(_hs_sha256 "$repo")" ]; then
        _hs_err "conflict: blob verify failed for $rel — leaving live intact"
        _hs_conflict_journal "$rundir" "$live" "$rel" "$class" "$ub" "$bb" "$nb" "$su" "$sb" "$sn" "errored"
        return 1
    fi
    # (iv) WRITE-AHEAD journal (status=intended) + fsync BEFORE overwrite.
    _hs_conflict_journal "$rundir" "$live" "$rel" "$class" "$ub" "$bb" "$nb" "$su" "$sb" "$sn" "intended"
    # (v) overwrite live with repo (atomic).
    if ! hs_atomic_install "$nb" "$live" "$(_hs_mode_for "$rel")"; then
        _hs_err "conflict: overwrite failed for $rel"; return 1
    fi
    # (vi) advance baseline to N AFTER (v) confirmed, mark applied.
    hs_atomic_install "$nb" "$baseline" 644
    _hs_conflict_journal "$rundir" "$live" "$rel" "$class" "$ub" "$bb" "$nb" "$su" "$sb" "$sn" "applied"
    _hs_warn "conflict (took repo, user backed up): ~/$rel"
    return 0
}

# Restart reconciliation for conflict journals: idempotent.
_hs_conflict_reconcile() {
    [ -d "$HS_CONFLICT_ROOT" ] || return 0
    local jl dest rel nb baseline live_sha n_sha
    for jl in "$HS_CONFLICT_ROOT"/*/journal.jsonl; do
        [ -f "$jl" ] || continue
        # Only act on entries whose last status is not "applied".
        # (Hand-rolled: read each line; this is a best-effort recovery.)
        while IFS= read -r line; do
            case "$line" in *'"status":"applied"'*) continue ;; esac
            case "$line" in *'"status":"errored"'*) continue ;; esac
            dest="$(printf '%s' "$line" | sed -n 's/.*"dest":"\([^"]*\)".*/\1/p')"
            rel="$(printf '%s' "$line"  | sed -n 's/.*"rel":"\([^"]*\)".*/\1/p')"
            nb="$(printf '%s' "$line"   | sed -n 's/.*"repo_blob":"\([^"]*\)".*/\1/p')"
            [ -n "$dest" ] && [ -f "$nb" ] || continue
            baseline="$(_hs_baseline_path "$rel")"
            n_sha="$(_hs_sha256 "$nb")"
            if [ -f "$dest" ] && [ "$(_hs_sha256 "$dest")" = "$n_sha" ]; then
                # live==N: ensure baseline advanced.
                if [ ! -f "$baseline" ] || [ "$(_hs_sha256 "$baseline")" != "$n_sha" ]; then
                    hs_atomic_install "$nb" "$baseline" 644
                fi
            else
                # live==U (or other): re-apply repo, then advance baseline.
                hs_atomic_install "$nb" "$dest" "$(_hs_mode_for "$rel")"
                hs_atomic_install "$nb" "$baseline" 644
            fi
        done < "$jl"
    done
}

# ── hs_sync_tree — per-file 3-way MERGE (update.sh) ──────────────────────────
# hs_sync_tree <src_base> <dst_base> <mode>
hs_sync_tree() {
    local src_base="$1" dst_base="$2" mode="$3"
    [ -d "$src_base" ] || return 0
    _hs_conflict_reconcile
    local src
    while IFS= read -r -d '' src; do
        _hs_sync_one "$src" "$src_base" "$dst_base" "$mode"
    done < <(find "$src_base" -type f -print0)
}

_hs_sync_one() {
    local src="$1" src_base="$2" dst_base="$3" mode="$4"
    local within rel dst baseline class
    within="$(realpath --relative-to="$src_base" "$src")"
    # home-relative rel: dst_base is under HOME.
    rel="$(_hs_relhome "$dst_base")/$within"
    rel="${rel#./}"
    dst="$dst_base/$within"
    baseline="$(_hs_baseline_path "$rel")"
    class="$(hs_classify "$rel")"

    # Skip stray backup files defensively.
    [[ "$src" =~ \.bak\.[0-9]{8}-[0-9]{6}$ ]] && return 0

    case "$class" in
        generated)
            # Owned by generator; purge any stale baseline.
            if [ -e "$baseline" ]; then rm -f "$baseline"; fi
            skip "generated (skip): ~/$rel"
            return 0 ;;
        ignore)
            skip "ignored: ~/$rel"; return 0 ;;
        seed-once)
            if [ ! -e "$dst" ]; then
                hs_atomic_install "$src" "$dst" "$(_hs_mode_for "$rel")"
                ok "seeded (seed-once): ~/$rel"
            else
                skip "seed-once (present, skip): ~/$rel"
            fi
            return 0 ;;
    esac

    # track / track-conservative below.
    mkdir -p "$(dirname "$dst")"

    # Case 1: destination absent — install new, baseline := shipped-new.
    if [ ! -f "$dst" ]; then
        hs_atomic_install "$src" "$dst" "$(_hs_mode_for "$rel")"
        hs_atomic_install "$src" "$baseline" 644
        ok "new: ~/$rel"
        return 0
    fi

    # Case 2: already equal to upstream.
    if cmp -s "$src" "$dst"; then
        [ -f "$baseline" ] || hs_atomic_install "$src" "$baseline" 644
        skip "unchanged: ~/$rel"
        return 0
    fi

    # Ensure a baseline exists (shipped-new) for the 3-way.
    if [ ! -f "$baseline" ]; then
        hs_atomic_install "$src" "$baseline" 644
    fi

    # keep-user when upstream == baseline (user may have diverged — leave live).
    if cmp -s "$src" "$baseline"; then
        skip "unchanged upstream (user-modified): ~/$rel"
        return 0
    fi

    # Both sides changed → merge.
    _hs_merge3 "$src" "$dst" "$baseline" "$rel" "$class"
}

# 3-way merge of upstream(src) / baseline / live(dst).
_hs_merge3() {
    local src="$1" dst="$2" baseline="$3" rel="$4" class="$5"

    # Binary: cannot text-merge.
    if ! _hs_is_text "$src" || ! _hs_is_text "$dst"; then
        if [ "$class" = track ]; then
            hs_conflict_overwrite "$dst" "$baseline" "$src" "$rel" "$class"
        else
            skip "binary differs (keep user): ~/$rel"
        fi
        return 0
    fi

    # JSON merge via jq when available.
    if [[ "$src" == *.json ]] && command -v jq >/dev/null 2>&1; then
        local merged
        merged=$(jq -n \
            --argjson base     "$(cat "$baseline")" \
            --argjson upstream "$(cat "$src")" \
            --argjson current  "$(cat "$dst")" \
            '
              ($upstream | to_entries) as $up_entries |
              ($base | to_entries) as $base_entries |
              ($base_entries | map(.key) | map(select(. as $k | ($up_entries | map(.key) | contains([$k]) | not)))) as $removed_keys |
              reduce $up_entries[] as $e (
                $current;
                if ($base | has($e.key)) and (($base[$e.key]) == ($current[$e.key]))
                then . + {($e.key): $e.value}
                else . end
              ) |
              del(.[$removed_keys[]])
            ' 2>/dev/null) || merged=""
        if [ -n "$merged" ] && printf '%s' "$merged" | jq . >/dev/null 2>&1; then
            if [ "$merged" = "$(cat "$dst")" ]; then
                hs_atomic_install "$src" "$baseline" 644
                skip "unchanged (json merge identical): ~/$rel"
            else
                local tmpj; tmpj="$(mktemp "$(dirname "$dst")/.tmpjson.XXXXXX")"
                printf '%s\n' "$merged" > "$tmpj"
                hs_atomic_install "$tmpj" "$dst" "$(_hs_mode_for "$rel")"
                rm -f "$tmpj"
                hs_atomic_install "$src" "$baseline" 644
                ok "merged (json): ~/$rel"
            fi
            return 0
        fi
        # fall through to diff3 on failure
    fi

    # 3-way text merge with diff3.
    if ! command -v diff3 >/dev/null 2>&1; then
        # No merge tool: conservative keep-user; track takes repo via conflict path.
        if [ "$class" = track ]; then
            hs_conflict_overwrite "$dst" "$baseline" "$src" "$rel" "$class"
        else
            skip "no diff3 (keep user): ~/$rel"
        fi
        return 0
    fi

    local merged diff3_exit
    set +e
    merged=$(diff3 -m "$dst" "$baseline" "$src" 2>/dev/null)
    diff3_exit=$?
    set -e

    case "$diff3_exit" in
        0)
            if [ "$merged" = "$(cat "$dst")" ]; then
                hs_atomic_install "$src" "$baseline" 644
                skip "unchanged (merge identical): ~/$rel"
            else
                local tmpm; tmpm="$(mktemp "$(dirname "$dst")/.tmpmrg.XXXXXX")"
                printf '%s\n' "$merged" > "$tmpm"
                hs_atomic_install "$tmpm" "$dst" "$(_hs_mode_for "$rel")"
                rm -f "$tmpm"
                hs_atomic_install "$src" "$baseline" 644
                ok "merged: ~/$rel"
            fi
            ;;
        1)
            # Conflict.
            if [ "$class" = track ]; then
                hs_conflict_overwrite "$dst" "$baseline" "$src" "$rel" "$class"
            else
                skip "conflict (track-conservative → keep user): ~/$rel"
            fi
            ;;
        *)
            if [ "$class" = track ]; then
                hs_conflict_overwrite "$dst" "$baseline" "$src" "$rel" "$class"
            else
                skip "merge error (keep user): ~/$rel"
            fi
            ;;
    esac
}

# ── hs_delete_pass — Mechanic-2, fail CLOSED ─────────────────────────────────
hs_delete_pass() {
    [ -d "$HS_STATE_DIR" ] || return 0
    local baseline rel dst class shipped
    while IFS= read -r -d '' baseline; do
        rel="$(realpath --relative-to="$HS_STATE_DIR" "$baseline")"
        class="$(hs_classify "$rel")"
        # Only `track` is ever a delete candidate.
        [ "$class" = track ] || continue
        shipped="$(_hs_shipped_path "$rel")"
        # Upstream still ships it → not a deletion.
        [ -e "$shipped" ] && continue
        dst="$HS_HOME/$rel"
        # Candidate gates — ALL required else KEEP.
        [ -f "$baseline" ] && [ -s "$baseline" ] && [ -r "$baseline" ] || { _hs_warn "delete: baseline unusable, keeping ~/$rel"; continue; }
        if [ -L "$dst" ] || [ -d "$dst" ] || [ ! -f "$dst" ]; then
            skip "delete: live not a regular file, keeping ~/$rel"; continue
        fi
        if ! cmp -s "$dst" "$baseline"; then
            _hs_warn "delete: live differs from baseline — KEEPING ~/$rel (user-modified)"; continue
        fi
        # Equal → delete live THEN baseline. Any error aborts this path.
        if rm -f "$dst"; then
            rm -f "$baseline" || _hs_warn "delete: removed live but baseline remains for ~/$rel"
            ok "removed (upstream-deleted): ~/$rel"
        else
            _hs_err "delete: could not remove ~/$rel — leaving baseline"
        fi
    done < <(find "$HS_STATE_DIR" -type f -print0)
}

# ── hs_seed_tree — SEED mode (install.sh), COPY, no merge ────────────────────
# hs_seed_tree <src_base> <dst_base>
hs_seed_tree() {
    local src_base="$1" dst_base="$2"
    [ -d "$src_base" ] || return 0
    local src within rel dst class
    while IFS= read -r -d '' src; do
        within="$(realpath --relative-to="$src_base" "$src")"
        rel="$(_hs_relhome "$dst_base")/$within"; rel="${rel#./}"
        dst="$dst_base/$within"
        class="$(hs_classify "$rel")"
        case "$class" in
            seed-once)
                if [ ! -e "$dst" ]; then
                    hs_atomic_install "$src" "$dst" "$(_hs_mode_for "$rel")"
                    ok "seeded (seed-once): ~/$rel"
                else
                    skip "seed-once (present, skip): ~/$rel"
                fi
                ;;
            generated)
                # Placeholder now; the generator (init-monitors.sh) regenerates later.
                if [ ! -e "$dst" ]; then
                    hs_atomic_install "$src" "$dst" "$(_hs_mode_for "$rel")"
                    ok "seeded (generated placeholder): ~/$rel"
                fi
                # No baseline for generated; purge any stale one.
                local baseline; baseline="$(_hs_baseline_path "$rel")"
                if [ -e "$baseline" ]; then _hs_do rm -f "$baseline"; fi
                ;;
            ignore)
                skip "ignored: ~/$rel" ;;
            *)
                # track / track-conservative: copy real file, baseline := shipped-new.
                hs_atomic_install "$src" "$dst" "$(_hs_mode_for "$rel")"
                hs_atomic_install "$src" "$(_hs_baseline_path "$rel")" 644
                ok "seeded: ~/$rel"
                ;;
        esac
    done < <(find "$src_base" -type f -print0)
}

# ── hs_check — dry-run, ZERO writes ──────────────────────────────────────────
# Prints one line per managed path. NO heal/cp/baseline/journal/quiesce/git mutate.
hs_check() {
    _hs_load_manifest
    printf '  %-40s %-18s %-8s %-7s %-18s %-26s %s\n' \
        REL CLASS CLONE SYMLINK BASELINE ACTION CONFLICT >&2
    local src within rel dst class target clone action conflict symcol base_src
    while IFS= read -r -d '' src; do
        within="$(realpath --relative-to="$HS_DOTHOME" "$src")"
        rel="$within"
        dst="$HS_HOME/$rel"
        class="$(hs_classify "$rel")"
        target="$(_hs_symlink_target "$dst")"
        symcol="(real)"; [ "$target" != "(real)" ] && symcol="link"
        clone="$(_hs_clone_state "$dst")"
        base_src="shipped-new@heal"
        conflict="no"

        case "$class" in
            generated)
                action="generated-skip"; base_src="-" ;;
            ignore)
                action="keep-live(skip)"; base_src="-" ;;
            seed-once)
                if [ -e "$dst" ]; then action="seed-once-skip"; else action="install-new"; fi
                base_src="-" ;;
            *)
                # track / track-conservative.
                if _hs_has_symlink_ancestor "$dst"; then
                    # Dir would be healed first. Compute the SAME first-heal
                    # decision read-only (wrapped git show/diff --quiet, never
                    # aborts; degrades to keep-live). NOT a conflict either way.
                    local resolved
                    resolved="$(readlink -f "$dst" 2>/dev/null || true)"
                    if [ -n "$resolved" ] && [ -f "$resolved" ]; then
                        case "$(_hs_heal_probe "$resolved" "$resolved" "$src")" in
                            take-update) action="heal-dir then take-repo(update)" ;;
                            take-noop)   action="heal-dir then take-repo(noop)" ;;
                            *)           action="heal-dir then keep-live(skip)" ;;
                        esac
                    else
                        action="heal-dir then keep-live(skip)"
                    fi
                elif [ ! -e "$dst" ]; then
                    action="install-new"
                else
                    # Modeled baseline := shipped-new == upstream(src). With src==baseline
                    # the 3-way degenerates to keep-user, so no write and no conflict.
                    action="keep-live(skip)"
                fi
                ;;
        esac

        printf '  %-40s %-18s %-8s %-7s %-18s %-26s %s\n' \
            "$rel" "$class" "$clone" "$symcol" "$base_src" "$action" "$conflict" >&2
        printf '      target: %s\n' "$target" >&2
    done < <(find "$HS_DOTHOME" -type f ! -name '.dotfiles-manifest' -print0 | sort -z)
}
