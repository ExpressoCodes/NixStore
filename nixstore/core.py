"""Package index, packages.json handling and the rebuild step, shared by the TUI and CLI."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Self

# Library/package sets that are almost never what you want in systemPackages.
SKIP_RE = re.compile(
    r"^(tests|(python[0-9]*|haskell|perl[0-9]*|ocaml|lua[0-9]*|luajit|r|emacs|node|php[0-9]*"
    r"|coq|idris|agda|elm|chicken|beam|octave|akkoma|rubyGems|texlive|typst|linux)"
    r"[A-Za-z0-9_]*Packages[A-Za-z0-9_]*|vimPlugins|vscode-extensions|emacsPackagesFor"
    r"|tree-sitter-grammars|home-assistant-custom-components|lib)\."
)

# A bare package line in a hand-written list, e.g. "    kitty   # terminal".
NIX_LIST_LINE = re.compile(r"^\s*(?:pkgs\.)?([A-Za-z_][A-Za-z0-9_.+-]*)\s*(?:#.*)?$")

# Runs as root: install the new packages.json, rebuild, and put the old file
# back if the rebuild fails.  $1 new file, $2 packages.json, $3 flake, $4 old file.
APPLY_SCRIPT = """
cp "$1" "$2" && chmod 644 "$2" || exit 1
nixos-rebuild switch --flake "$3" && exit 0
cp "$4" "$2"
exit 1
"""


def _sudo_write(path: Path, content: str, password: str = "") -> None:
    """Write content to a file that may be root-owned, using sudo install if needed."""
    if os.access(path.parent, os.W_OK) and (not path.exists() or os.access(path, os.W_OK)):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, path)
    else:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            cmd = ["sudo", "-S", "-p", "", "install", "-m", "644", tmp_path, str(path)]
            subprocess.run(
                cmd,
                input=(password + "\n").encode() if password else None,
                check=True,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)


@dataclass(frozen=True)
class Config:
    flake: Path
    packages_file: Path
    cache_dir: Path
    modules_file: Path = field(default=None)  # type: ignore[assignment]
    flake_file: Path = field(default=None)  # type: ignore[assignment]

    @classmethod
    def from_env(cls, flake: str | None = None) -> Config:
        flake_dir = Path(flake or os.environ.get("NIXSTORE_FLAKE", "/etc/nixos"))
        packages_file = os.environ.get("NIXSTORE_PACKAGES_FILE") if flake is None else None
        modules_file = os.environ.get("NIXSTORE_MODULES_FILE") if flake is None else None
        flake_file = os.environ.get("NIXSTORE_FLAKE_FILE") if flake is None else None
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        return cls(
            flake=flake_dir,
            packages_file=Path(packages_file) if packages_file else flake_dir / "packages.json",
            cache_dir=cache_home / "nixstore",
            modules_file=Path(modules_file) if modules_file else flake_dir / "modules.json",
            flake_file=Path(flake_file) if flake_file else flake_dir / "flake.nix",
        )


class Package:
    __slots__ = ("attr", "description", "lower", "name", "version")

    def __init__(self, attr: str, version: str = "", description: str = "") -> None:
        self.attr = attr
        self.version = version
        self.description = description
        self.lower = attr.lower()
        self.name = self.lower.rsplit(".", 1)[-1]

    def __repr__(self) -> str:
        return f"Package({self.attr!r})"


# --- package index ------------------------------------------------------------


def nixpkgs_rev(cfg: Config) -> str:
    lock = json.loads((cfg.flake / "flake.lock").read_text())
    node = lock["nodes"]["root"]["inputs"]["nixpkgs"]
    return lock["nodes"][node]["locked"].get("rev", "unknown")


def index_path(cfg: Config) -> Path:
    return cfg.cache_dir / f"{nixpkgs_rev(cfg)}.tsv"


def parse_search_json(data: dict) -> list[str]:
    """`nix search --json` output -> sorted "attr\\tversion\\tdescription" lines."""
    lines = []
    for key, info in data.items():
        attr = key.split(".", 2)[2]  # strip "legacyPackages.<system>."
        if SKIP_RE.match(attr):
            continue
        desc = re.sub(r"[\t\n]", " ", info.get("description") or "")
        lines.append(f"{attr}\t{info.get('version') or ''}\t{desc}\n")
    return sorted(lines)


def read_index(path: Path) -> list[Package]:
    packages = []
    for line in path.read_text().splitlines():
        attr, version, desc = (line.split("\t") + ["", ""])[:3]
        packages.append(Package(attr, version, desc))
    return packages


async def load_index(cfg: Config) -> list[Package]:
    """All packages of the flake's pinned nixpkgs, cached per nixpkgs revision."""
    path = index_path(cfg)
    if not path.exists():
        proc = await asyncio.create_subprocess_exec(
            "nix", "search", "--inputs-from", str(cfg.flake), "nixpkgs", "^", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("nix search failed")
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(parse_search_json(json.loads(out))))
        tmp.replace(path)
        for old in cfg.cache_dir.glob("*.tsv"):
            if old != path:
                old.unlink()
    return read_index(path)


def search(packages: list[Package], query: str, limit: int = 300) -> list[Package]:
    """Exact name, then prefix, then substring; if none, every word must appear."""
    q = query.strip().lower()
    if not q:
        return []
    exact, prefix, contains = [], [], []
    for p in packages:
        if p.name == q:
            exact.append(p)
        elif p.name.startswith(q):
            prefix.append(p)
        elif q in p.lower:
            contains.append(p)
    if not (exact or prefix or contains):
        words = q.split()
        contains = [p for p in packages if all(w in p.lower for w in words)]

    def key(p: Package) -> tuple[int, str]:
        return (len(p.attr), p.attr)

    return (sorted(exact, key=key) + sorted(prefix, key=key) + sorted(contains, key=key))[:limit]


# --- configuration state -------------------------------------------------------


def read_installed(cfg: Config) -> set[str]:
    try:
        return set(json.loads(cfg.packages_file.read_text()))
    except FileNotFoundError:
        return set()


def read_system_packages(cfg: Config) -> dict[str, str]:
    """Packages listed by hand in the flake's .nix files: attr -> file name."""
    found: dict[str, str] = {}
    for nix_file in sorted(cfg.flake.glob("*.nix")):
        for line in nix_file.read_text(errors="replace").splitlines():
            m = NIX_LIST_LINE.match(line)
            if m:
                found.setdefault(m.group(1), nix_file.name)
    return found


def render_packages_json(attrs: set[str]) -> str:
    return json.dumps(sorted(attrs), indent=2) + "\n"


# --- applying changes ----------------------------------------------------------


class Transaction:
    """Temp copies of the new and current packages.json plus the root command to apply them."""

    def __init__(self, cfg: Config, new_installed: set[str]) -> None:
        self.cfg = cfg
        self._dir = tempfile.TemporaryDirectory(prefix="nixstore-")
        tmp = Path(self._dir.name)
        self.new_file = tmp / "new.json"
        self.old_file = tmp / "old.json"
        self.new_file.write_text(render_packages_json(new_installed))
        old = cfg.packages_file.read_text() if cfg.packages_file.exists() else "[]\n"
        self.old_file.write_text(old)

    def argv(self, *sudo_flags: str) -> list[str]:
        return [
            "sudo", *sudo_flags, "sh", "-c", APPLY_SCRIPT, "nixstore",
            str(self.new_file), str(self.cfg.packages_file), str(self.cfg.flake), str(self.old_file),
        ]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._dir.cleanup()


def notify(summary: str, body: str, urgency: str = "normal") -> None:
    try:
        subprocess.run(
            ["notify-send", "-a", "NixStore", "-i", "nixstore", "-u", urgency, summary, body],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


# --- system update helpers ----------------------------------------------------


@dataclass(frozen=True)
class SystemUpdateStatus:
    flake_inputs_updated: list[str]

    @property
    def has_updates(self) -> bool:
        return bool(self.flake_inputs_updated)


def check_system_updates(flake: Path) -> SystemUpdateStatus:
    """Check flake inputs for available updates."""
    return SystemUpdateStatus(flake_inputs_updated=_check_flake_inputs(flake))


def _check_flake_inputs(flake: Path) -> list[str]:
    """Run nix flake update in a temp dir to detect which inputs would change."""
    flake_nix = flake / "flake.nix"
    flake_lock = flake / "flake.lock"
    if not flake_nix.exists() or not flake_lock.exists():
        return []

    try:
        orig_lock = json.loads(flake_lock.read_text())
    except Exception:
        return []

    try:
        with tempfile.TemporaryDirectory(prefix="nixstore-flake-check-") as tmpdir:
            tmppath = Path(tmpdir)
            shutil.copy(flake_nix, tmppath / "flake.nix")
            shutil.copy(flake_lock, tmppath / "flake.lock")

            result = subprocess.run(
                ["nix", "flake", "update", "--flake", str(tmppath)],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode != 0:
                return []

            new_lock = json.loads((tmppath / "flake.lock").read_text())

        orig_nodes = orig_lock.get("nodes", {})
        new_nodes = new_lock.get("nodes", {})
        changed = []
        for name, node in new_nodes.items():
            if name == "root":
                continue
            orig_rev = orig_nodes.get(name, {}).get("locked", {}).get("rev", "")
            new_rev = node.get("locked", {}).get("rev", "")
            if orig_rev and new_rev and orig_rev != new_rev:
                changed.append(name)
        return changed
    except Exception:
        return []


def _bundled_update_script() -> str:
    """Return path to the bundled update.sh — env var set by the Nix wrapper, else repo fallback."""
    env_path = os.environ.get("NIXSTORE_UPDATE_SCRIPT", "")
    if env_path and Path(env_path).is_file():
        return env_path
    # Development fallback: look relative to this file
    candidate = Path(__file__).parent.parent / "data" / "update.sh"
    return str(candidate) if candidate.is_file() else env_path


async def run_system_update(flake: Path, log_cb, password_cb=None) -> bool:  # noqa: ANN001
    """Run the bundled update.sh which self-updates nixstore then does a full system update."""
    password = ""
    if password_cb is not None:
        password = await password_cb("sudo password")

    script_path = _bundled_update_script()
    if not script_path or not Path(script_path).is_file():
        await log_cb("ERROR: update.sh not found. Rebuild nixstore to install it.")
        return False

    # Write a temporary SUDO_ASKPASS helper so the script can authenticate
    askpass_file = None
    pw_file = None
    try:
        if password:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".pw", delete=False, prefix="nixstore-pw-"
            ) as f:
                f.write(password)
                pw_file = f.name
            os.chmod(pw_file, stat.S_IRUSR)

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="nixstore-askpass-"
            ) as f:
                f.write(f"#!/bin/sh\ncat '{pw_file}'\n")
                askpass_file = f.name
            os.chmod(askpass_file, stat.S_IRWXU)

        env = os.environ.copy()
        env["NIXSTORE_NONINTERACTIVE"] = "1"
        env["NIXSTORE_FLAKE"] = str(flake)
        if askpass_file:
            env["SUDO_ASKPASS"] = askpass_file

        proc = await asyncio.create_subprocess_exec(
            "bash", script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            await log_cb(raw.decode(errors="replace").rstrip("\n"))
        await proc.wait()
        return (proc.returncode or 0) == 0
    finally:
        for p in (askpass_file, pw_file):
            if p:
                Path(p).unlink(missing_ok=True)


# --- flatpak ------------------------------------------------------------------


class FlatpakPackage:
    __slots__ = ("app_id", "description", "lower", "name", "version")

    def __init__(self, app_id: str, name: str = "", version: str = "", description: str = "") -> None:
        self.app_id = app_id
        self.name = name or app_id
        self.version = version
        self.description = description
        self.lower = f"{app_id} {name} {description}".lower()

    def __repr__(self) -> str:
        return f"FlatpakPackage({self.app_id!r})"


async def ensure_flathub() -> None:
    """Add the flathub remote (user-level) if not already present."""
    proc = await asyncio.create_subprocess_exec(
        "flatpak", "remote-add", "--if-not-exists", "--user",
        "flathub", "https://dl.flathub.org/repo/flathub.flatpakrepo",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def _parse_flatpak_lines(output: str) -> list[FlatpakPackage]:
    results = []
    seen: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t")
        app_id = parts[0].strip() if parts else ""
        if not app_id or app_id in ("Application ID", "Name"):
            continue
        if app_id in seen:
            continue
        seen.add(app_id)
        results.append(FlatpakPackage(
            app_id=app_id,
            name=parts[1].strip() if len(parts) > 1 else "",
            version=parts[2].strip() if len(parts) > 2 else "",
            description=parts[3].strip() if len(parts) > 3 else "",
        ))
    return results


async def search_flatpaks(query: str) -> list[FlatpakPackage]:
    if not query.strip():
        return []
    proc = await asyncio.create_subprocess_exec(
        "flatpak", "search", "--columns=application,name,version,description", query,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return _parse_flatpak_lines(out.decode(errors="replace"))


async def list_flatpaks() -> list[FlatpakPackage]:
    proc = await asyncio.create_subprocess_exec(
        "flatpak", "list", "--app", "--user",
        "--columns=application,name,version,description",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return _parse_flatpak_lines(out.decode(errors="replace"))


async def install_flatpak(app_id: str, log_cb) -> bool:  # noqa: ANN001
    proc = await asyncio.create_subprocess_exec(
        "flatpak", "install", "-y", "--user", "flathub", app_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    async for raw in proc.stdout:
        log_cb(raw.decode(errors="replace").rstrip("\n"))
    await proc.wait()
    return proc.returncode == 0


async def remove_flatpak(app_id: str, log_cb) -> bool:  # noqa: ANN001
    proc = await asyncio.create_subprocess_exec(
        "flatpak", "remove", "-y", "--user", app_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    async for raw in proc.stdout:
        log_cb(raw.decode(errors="replace").rstrip("\n"))
    await proc.wait()
    return proc.returncode == 0


# --- module management --------------------------------------------------------


def load_modules(path: str | Path) -> dict:
    """Read and JSON-parse modules.json. Returns {} if file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON in {path}: {exc}") from exc


def save_modules(path: str | Path, data: dict) -> None:
    """Atomically write modules registry JSON to path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _sudo_write(path, json.dumps(data, indent=2) + "\n")


def rebuild(flake_dir: str | Path) -> None:
    """Run nixos-rebuild switch for the given flake directory."""
    subprocess.run(
        ["sudo", "nixos-rebuild", "switch", "--flake", str(flake_dir)],
        check=True,
    )


_NON_MODULE_INPUTS = re.compile(r"^nixpkgs")

def discover_inputs_from_lock(lock_path: str | Path) -> list[str]:
    """Return sorted list of direct flake inputs from flake.nix, excluding package sets."""
    lock_path = Path(lock_path)
    data = json.loads(lock_path.read_text())
    nodes = data.get("nodes", {})
    root_inputs = nodes.get("root", {}).get("inputs", {})
    return sorted(k for k in root_inputs if not _NON_MODULE_INPUTS.match(k))


def get_module_view(modules_path: str | Path, lock_path: str | Path) -> list[dict]:
    """Return a combined view of flake inputs and the module registry.

    Rows have at minimum: name, status, source, type.
    Status values: "enabled", "disabled", "unregistered", "missing".
    Sorted: system entries first, then user entries, then unregistered — all alphabetical
    within each group.
    """
    registry: dict = load_modules(modules_path)

    lock_path = Path(lock_path)
    lock_inputs: set[str] = set()
    if lock_path.exists():
        try:
            lock_inputs = set(discover_inputs_from_lock(lock_path))
        except Exception:
            lock_inputs = set()

    rows: list[dict] = []

    # Inputs present in flake.lock
    for inp in lock_inputs:
        if inp in registry:
            row = dict(registry[inp])
            row["name"] = inp
            row["status"] = "enabled" if registry[inp].get("enabled", False) else "disabled"
        else:
            row = {"name": inp, "status": "unregistered", "source": "", "type": "", "input": inp}
        rows.append(row)

    # Registry entries NOT in flake.lock
    for name, entry in registry.items():
        if name not in lock_inputs:
            row = dict(entry)
            row["name"] = name
            if entry.get("type") == "flake-module":
                # Flake-module input missing from lock means it was removed from flake.nix
                # externally (or an add-URL failed partway). Show as "missing" so user knows.
                row["status"] = "missing"
            else:
                # Program-option entries have no flake.lock presence — show enabled/disabled.
                row["status"] = "enabled" if entry.get("enabled", False) else "disabled"
            rows.append(row)

    def _sort_key(r: dict) -> tuple[int, str]:
        status = r.get("status", "")
        source = r.get("source", "")
        if status == "unregistered":
            order = 2
        elif source == "system":
            order = 0
        else:
            order = 1
        return (order, r.get("name", ""))

    rows.sort(key=_sort_key)
    return rows


def enable_module(name: str, modules_path: str | Path) -> None:
    """Enable a module in the registry and rebuild NixOS."""
    modules_path = Path(modules_path)
    registry = load_modules(modules_path)
    if name not in registry:
        raise ValueError(f"Module '{name}' not found in registry ({modules_path})")
    registry[name]["enabled"] = True
    save_modules(modules_path, registry)
    rebuild(modules_path.parent)


def disable_module(name: str, modules_path: str | Path) -> None:
    """Disable a module in the registry and rebuild NixOS."""
    modules_path = Path(modules_path)
    registry = load_modules(modules_path)
    if name not in registry:
        raise ValueError(f"Module '{name}' not found in registry ({modules_path})")
    registry[name]["enabled"] = False
    save_modules(modules_path, registry)
    rebuild(modules_path.parent)


def remove_module(
    name: str,
    modules_path: str | Path,
    flake_file_path: str | Path,
) -> None:
    """Remove a module from the registry and from the inputs block of flake.nix.

    Raises PermissionError for system modules.
    Raises ValueError if the input line is not found in flake.nix.
    Does NOT call rebuild() — caller must rebuild separately.
    """
    modules_path = Path(modules_path)
    flake_file_path = Path(flake_file_path)
    registry = load_modules(modules_path)
    if name not in registry:
        raise ValueError(f"Module '{name}' not found in registry ({modules_path})")
    entry = registry[name]
    if entry.get("source") == "system":
        raise PermissionError(
            "Cannot remove a system module. Edit modules.json manually if you are sure."
        )

    del registry[name]
    save_modules(modules_path, registry)

    # Remove from flake.nix if applicable
    input_name = entry.get("input") if entry.get("type") == "flake-module" else None
    if input_name and flake_file_path.exists():
        text = flake_file_path.read_text()

        # Remove simple `input.url = "...";` line (with optional surrounding blank lines / comments)
        new_text = re.sub(
            rf"^[^\S\n]*{re.escape(input_name)}\.url\s*=\s*\"[^\"]*\";\s*\n",
            "",
            text,
            flags=re.MULTILINE,
        )

        if new_text == text:
            raise ValueError(
                f"Could not find input '{input_name}' in {flake_file_path}. "
                "Remove it manually from flake.nix."
            )

        _sudo_write(flake_file_path, new_text)


def remove_unregistered_input(name: str, flake_file_path: str | Path) -> None:
    """Remove a flake input that is in flake.lock but not in the registry.

    Strips the `<name>.url = "...";` line from flake.nix so the input is
    dropped from flake.lock on the next rebuild. Does NOT call rebuild().
    """
    flake_file_path = Path(flake_file_path)
    text = flake_file_path.read_text()
    new_text = re.sub(
        rf"^[^\S\n]*{re.escape(name)}\.url\s*=\s*\"[^\"]*\";\s*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    if new_text == text:
        raise ValueError(
            f"Could not find '{name}.url = ...' in {flake_file_path}. "
            "Remove it manually from flake.nix."
        )
    _sudo_write(flake_file_path, new_text)


def register_input(
    name: str,
    modules_path: str | Path,
    flake_dir: str | Path,
) -> dict:
    """Probe a flake input for nixosModules.default and register it in modules.json.

    Raises ValueError if already registered or if the input doesn't expose
    nixosModules.default.
    """
    modules_path = Path(modules_path)
    flake_dir = Path(flake_dir)
    registry = load_modules(modules_path)
    if name in registry:
        raise ValueError(f"Input '{name}' is already registered in {modules_path}")

    result = subprocess.run(
        [
            "nix", "eval",
            f".#inputs.{name}.nixosModules",
            "--apply", "builtins.attrNames",
        ],
        cwd=str(flake_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or "default" not in result.stdout:
        raise ValueError(
            f"This input does not expose nixosModules.default. "
            f"Cannot register '{name}' as a module."
        )

    entry: dict = {
        "source": "user",
        "enabled": False,
        "type": "flake-module",
        "input": name,
    }
    registry[name] = entry
    save_modules(modules_path, registry)
    return entry


def add_flake_module(
    url: str,
    modules_path: str | Path,
    flake_file_path: str | Path,
    flake_dir: str | Path,
    name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    password: str = "",
) -> str:
    """Add a flake input by patching flake.nix, update flake.lock, and register it as a module.

    Returns the registered name.
    """
    modules_path = Path(modules_path)
    flake_file_path = Path(flake_file_path)
    flake_dir = Path(flake_dir)

    if name is None:
        name = url.rstrip("/").rsplit("/", 1)[-1]

    # Guard: duplicate in lock or registry
    lock_path = flake_dir / "flake.lock"
    if lock_path.exists():
        if name in discover_inputs_from_lock(lock_path):
            raise ValueError(
                f"Input '{name}' already exists. Use --name to specify a different name."
            )
    registry = load_modules(modules_path)
    if name in registry:
        raise ValueError(
            f"Input '{name}' already exists. Use --name to specify a different name."
        )

    # Patch flake.nix: insert `    {name}.url = "{url}";` before the closing `};`
    # of the inputs block.
    orig_text = flake_file_path.read_text()

    # Find the inputs = { ... }; block.
    inputs_start = orig_text.find("inputs = {")
    if inputs_start == -1:
        raise ValueError(
            f"Could not find 'inputs = {{' in {flake_file_path}. Cannot insert input safely."
        )

    # Find the matching closing `};` — walk forward counting braces.
    brace_depth = 0
    inputs_end = -1
    i = orig_text.index("{", inputs_start)
    while i < len(orig_text):
        if orig_text[i] == "{":
            brace_depth += 1
        elif orig_text[i] == "}":
            brace_depth -= 1
            if brace_depth == 0:
                inputs_end = i
                break
        i += 1

    if inputs_end == -1:
        raise ValueError(
            f"Could not find closing '}}' of inputs block in {flake_file_path}. "
            "Cannot insert input safely."
        )

    insert_line = f"    {name}.url = \"{url}\";\n"
    new_flake_text = orig_text[:inputs_end] + insert_line + orig_text[inputs_end:]

    # Run nix flake update as the current user in a temp dir so nix uses the
    # normal user environment (cache, SSL, daemon access). Only the final file
    # installs need sudo.
    with tempfile.TemporaryDirectory(prefix="nixstore-add-") as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "flake.nix").write_text(new_flake_text)
        lock_src = flake_dir / "flake.lock"
        if lock_src.exists():
            shutil.copy(lock_src, tmp / "flake.lock")

        proc = subprocess.Popen(
            ["nix", "flake", "update", name],
            cwd=str(tmp),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if progress_callback is not None:
                progress_callback(line.rstrip("\n"))
        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, f"nix flake update {name}")

        # Build updated modules registry
        new_registry = dict(registry)
        new_registry[name] = {
            "source": "user", "enabled": False,
            "type": "flake-module", "input": name,
        }
        (tmp / "modules.json").write_text(json.dumps(new_registry, indent=2) + "\n")

        # Single sudo call: install updated flake.nix, flake.lock, and modules.json.
        # Networking is done; sudo only copies already-computed files.
        script = (
            f"install -m 644 '{tmp}/flake.nix' '{flake_file_path}' && "
            f"install -m 644 '{tmp}/flake.lock' '{flake_dir}/flake.lock' && "
            f"install -m 644 '{tmp}/modules.json' '{modules_path}'"
        )
        sudo_proc = subprocess.Popen(
            ["sudo", "-S", "-p", "", "sh", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert sudo_proc.stdin is not None and sudo_proc.stdout is not None
        if password:
            sudo_proc.stdin.write(password + "\n")
        sudo_proc.stdin.close()
        for line in sudo_proc.stdout:
            if progress_callback is not None:
                progress_callback(line.rstrip("\n"))
        sudo_proc.wait()
        if sudo_proc.returncode != 0:
            raise subprocess.CalledProcessError(sudo_proc.returncode, f"sudo install (add {name})")

    return name
