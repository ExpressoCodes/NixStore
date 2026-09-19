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


# --- dotfiles update helpers --------------------------------------------------

import shutil

VARS_FILE = Path("/etc/nixos/.dotfiles-vars")


def read_dotfiles_vars() -> dict[str, str]:
    """Parse /etc/nixos/.dotfiles-vars written by the dotfiles install.sh."""
    if not VARS_FILE.exists():
        return {}
    result = {}
    for line in VARS_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


@dataclass(frozen=True)
class DotfilesStatus:
    repo: Path
    local_rev: str
    remote_rev: str
    count: int
    commits: list[str]  # one-line summaries, newest first

    @property
    def up_to_date(self) -> bool:
        return self.local_rev == self.remote_rev


def check_dotfiles_updates() -> DotfilesStatus | None:
    """Fetch origin and return status, or None if repo/network unavailable."""
    vars_ = read_dotfiles_vars()
    repo_str = vars_.get("DOTFILES_REPO")
    if not repo_str:
        return None
    repo = Path(repo_str)
    if not (repo / ".git").exists():
        return None
    git = shutil.which("git") or "git"

    def run(*args: str) -> str:
        r = subprocess.run([git, "-C", str(repo), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    run("fetch", "--quiet", "origin")
    local = run("rev-parse", "HEAD")
    remote = (run("rev-parse", "origin/HEAD")
              or run("rev-parse", "origin/main")
              or run("rev-parse", "origin/master"))
    if not local or not remote:
        return None
    count = int(run("rev-list", "--count", f"HEAD..{remote}") or "0")
    commits = [c for c in run("log", "--oneline", f"HEAD..{remote}").splitlines() if c]
    return DotfilesStatus(repo=repo, local_rev=local, remote_rev=remote,
                          count=count, commits=commits)


async def run_dotfiles_update(repo: Path, password: str, log_cb) -> bool:
    """Run update.sh with the given sudo password, streaming output to log_cb."""
    script = repo / "update.sh"
    if not script.exists():
        await log_cb(f"update.sh not found in {repo}")
        return False
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-S", "-k", "-p", "", str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    proc.stdin.write((password + "\n").encode())
    await proc.stdin.drain()
    proc.stdin.close()
    del password
    async for raw in proc.stdout:
        await log_cb(raw.decode(errors="replace").rstrip("\n"))
    await proc.wait()
    return proc.returncode == 0
