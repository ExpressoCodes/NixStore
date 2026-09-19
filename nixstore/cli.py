"""Command line entry point: `nixstore` opens the TUI, subcommands work without it."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys

from . import __version__, core
from .core import Config, Package

BOLD, DIM, RED, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = RED = GREEN = YELLOW = RESET = ""


def err(msg: str) -> None:
    print(f"{RED}error:{RESET} {msg}", file=sys.stderr)


def load_index(cfg: Config) -> list[Package]:
    if not core.index_path(cfg).exists():
        print(f"{DIM}Building package index (first run after a nixpkgs update, ~30s)…{RESET}", file=sys.stderr)
    return asyncio.run(core.load_index(cfg))


def print_packages(packages: list[Package], installed: set[str]) -> None:
    width = shutil.get_terminal_size((100, 20)).columns
    name_w = min(max((len(p.attr) for p in packages), default=0), 40)
    for p in packages:
        mark = f"{GREEN}✓{RESET}" if p.attr in installed else " "
        line = f"{p.attr:<{name_w}}  {p.version:<14}  {p.description}"
        print(f"{mark} {line[: width - 3]}")


def apply(cfg: Config, new_installed: set[str], summary: str) -> int:
    print(f"\n{BOLD}::{RESET} Updating {cfg.packages_file} and rebuilding (sudo)…\n")
    with core.Transaction(cfg, new_installed) as tx:
        code = subprocess.run(tx.argv(), check=False).returncode
    if code == 0:
        print(f"\n{GREEN}{BOLD}✓{RESET} {summary}")
        core.notify("Packages updated", summary)
    else:
        err(f"rebuild failed; {cfg.packages_file} was restored, nothing changed.")
        core.notify("Rebuild failed", summary, "critical")
    return code


def cmd_search(cfg: Config, args: argparse.Namespace) -> int:
    results = core.search(load_index(cfg), " ".join(args.query), limit=args.limit)
    if not results:
        err("no matching packages.")
        return 1
    print_packages(results, core.read_installed(cfg))
    return 0


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    installed = core.read_installed(cfg)
    path = core.index_path(cfg)
    by_attr = {p.attr: p for p in core.read_index(path)} if path.exists() else {}
    print_packages([by_attr.get(a) or Package(a) for a in sorted(installed)], set())
    return 0


def cmd_install(cfg: Config, args: argparse.Namespace) -> int:
    packages = load_index(cfg)
    by_attr = {p.attr: p for p in packages}
    installed = core.read_installed(cfg)
    system = core.read_system_packages(cfg)
    wanted, failed = [], False
    for name in args.packages:
        if name not in by_attr:
            hints = ", ".join(p.attr for p in core.search(packages, name, limit=5))
            err(f"no package '{name}'." + (f" Did you mean: {hints}" if hints else ""))
            failed = True
        elif name in installed or name in system:
            print(f"{YELLOW}!!{RESET} {name} is already installed.")
        else:
            p = by_attr[name]
            print(f"{GREEN}+{RESET} {BOLD}{p.attr}{RESET} {DIM}{p.version}{RESET}  {p.description}")
            wanted.append(name)
    if failed:
        return 1
    if not wanted:
        return 0
    return apply(cfg, installed | set(wanted), "Installed " + ", ".join(wanted))


def cmd_remove(cfg: Config, args: argparse.Namespace) -> int:
    installed = core.read_installed(cfg)
    system = core.read_system_packages(cfg)
    for name in args.packages:
        if name not in installed:
            where = f" (it is set in {system[name]})" if name in system else ""
            err(f"'{name}' is not in {cfg.packages_file}{where}.")
            return 1
        print(f"{RED}-{RESET} {BOLD}{name}{RESET}")
    return apply(cfg, installed - set(args.packages), "Removed " + ", ".join(args.packages))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nixstore",
        description="Search, install and remove NixOS packages. Without a command, opens the TUI.",
    )
    parser.add_argument("--flake", help="system flake directory (default: $NIXSTORE_FLAKE or /etc/nixos)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("tui", help="open the TUI (default), optionally with a search query")
    p.add_argument("query", nargs="*")
    p = sub.add_parser("search", help="search nixpkgs")
    p.add_argument("query", nargs="+")
    p.add_argument("-n", "--limit", type=int, default=20, help="max results (default: 20)")
    p = sub.add_parser("install", help="install packages by attribute name")
    p.add_argument("packages", nargs="+", metavar="package")
    p = sub.add_parser("remove", help="remove packages installed with nixstore")
    p.add_argument("packages", nargs="+", metavar="package")
    sub.add_parser("list", help="list packages installed with nixstore")

    args = parser.parse_args(argv)
    cfg = Config.from_env(args.flake)

    if args.command in (None, "tui"):
        from . import tui  # textual is only needed here

        tui.run(cfg, " ".join(getattr(args, "query", [])))
        return 0
    commands = {"search": cmd_search, "list": cmd_list, "install": cmd_install, "remove": cmd_remove}
    try:
        return commands[args.command](cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
