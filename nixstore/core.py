"""Package index, packages.json handling and the rebuild step, shared by the TUI and CLI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Self

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


@dataclass(frozen=True)
class Config:
    flake: Path
    packages_file: Path
    cache_dir: Path

    @classmethod
    def from_env(cls, flake: str | None = None) -> Config:
        flake_dir = Path(flake or os.environ.get("NIXSTORE_FLAKE", "/etc/nixos"))
        packages_file = os.environ.get("NIXSTORE_PACKAGES_FILE") if flake is None else None
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        return cls(
            flake=flake_dir,
            packages_file=Path(packages_file) if packages_file else flake_dir / "packages.json",
            cache_dir=cache_home / "nixstore",
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


VARS_FILE = Path("/etc/nixos/.dotfiles-vars")


def read_dotfiles_vars() -> dict[str, str]:
    if not VARS_FILE.exists():
        return {}
    result: dict[str, str] = {}
    for line in VARS_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


# --- system update helpers ----------------------------------------------------

import shutil


@dataclass(frozen=True)
class SystemUpdateStatus:
    is_git_repo: bool
    commits_behind: int
    commits: list[str]  # one-line summaries of unpulled commits

    @property
    def git_up_to_date(self) -> bool:
        return not self.is_git_repo or self.commits_behind == 0


def check_system_updates(flake: Path) -> SystemUpdateStatus:
    """Check DOTFILES_REPO (from VARS_FILE) for pending commits; fall back to flake."""
    vars_ = read_dotfiles_vars()
    repo_str = vars_.get("DOTFILES_REPO")
    repo = Path(repo_str) if repo_str else flake
    if not (repo / ".git").exists():
        return SystemUpdateStatus(is_git_repo=False, commits_behind=0, commits=[])
    git = shutil.which("git") or "git"

    def run(*args: str) -> str:
        r = subprocess.run([git, "-C", str(repo), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    run("fetch", "--quiet", "origin")
    local = run("rev-parse", "HEAD")
    remote = (run("rev-parse", "origin/HEAD")
              or run("rev-parse", "origin/main")
              or run("rev-parse", "origin/master"))
    if not local or not remote or local == remote:
        return SystemUpdateStatus(is_git_repo=True, commits_behind=0, commits=[])
    count = int(run("rev-list", "--count", f"HEAD..{remote}") or "0")
    commits = [c for c in run("log", "--oneline", f"HEAD..{remote}").splitlines() if c]
    return SystemUpdateStatus(is_git_repo=True, commits_behind=count, commits=commits)


async def run_system_update(flake: Path, password: str, log_cb) -> bool:  # noqa: ANN001
    """git-pull dotfiles (self-updating update.sh) then run it non-interactively."""
    vars_ = read_dotfiles_vars()
    dotfiles_str = vars_.get("DOTFILES_REPO")
    if not dotfiles_str:
        await log_cb("DOTFILES_REPO not set in /etc/nixos/.dotfiles-vars — run install.sh first.")
        return False
    dotfiles = Path(dotfiles_str)
    update_sh = dotfiles / "update.sh"
    if not update_sh.exists():
        await log_cb(f"update.sh not found at {update_sh}")
        return False

    # Authenticate sudo (creates / refreshes credential cache).
    auth = await asyncio.create_subprocess_exec(
        "sudo", "-Skp", "", "true",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    auth.stdin.write((password + "\n").encode())
    await auth.stdin.drain()
    auth.stdin.close()
    del password
    await auth.wait()
    if auth.returncode != 0:
        await log_cb("sudo: incorrect password.")
        return False

    # Keep sudo session alive during the long operation.
    async def _keepalive() -> None:
        while True:
            await asyncio.sleep(50)
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

    ka = asyncio.create_task(_keepalive())

    async def _stream(cmd: list[str], env: dict | None = None) -> int:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for raw in proc.stdout:
            await log_cb(raw.decode(errors="replace").rstrip("\n"))
        await proc.wait()
        return proc.returncode

    try:
        # Pull dotfiles — this self-updates update.sh before we run it.
        await log_cb(f"==> git pull {dotfiles}")
        rc = await _stream(["git", "-C", str(dotfiles), "pull", "--ff-only"])
        if rc != 0:
            await log_cb("git pull failed — aborting.")
            return False

        # Run the freshly-pulled update.sh non-interactively.
        # --no-pull skips its own git pull (we already did it above).
        await log_cb("==> Running update.sh")
        env = {**os.environ, "NIXSTORE_NONINTERACTIVE": "1"}
        rc = await _stream(["bash", str(update_sh), "--no-pull"], env=env)
        return rc == 0
    finally:
        ka.cancel()


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
    for line in output.splitlines():
        parts = line.split("\t")
        app_id = parts[0].strip() if parts else ""
        if not app_id or app_id in ("Application ID", "Name"):
            continue
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
