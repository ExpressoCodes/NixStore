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


# --- module subcommands -------------------------------------------------------

_STATUS_COLOR = {
    "enabled": GREEN,
    "disabled": DIM,
    "unregistered": YELLOW,
    "missing": RED,
}


def cmd_module_list(cfg: Config, args: argparse.Namespace) -> int:
    rows = core.get_module_view(cfg.modules_file, cfg.flake / "flake.lock")
    if not rows:
        print(f"{DIM}No modules found.{RESET}")
        return 0
    width = shutil.get_terminal_size((120, 20)).columns
    name_w = min(max((len(r.get("name", "")) for r in rows), default=0), 30)
    status_w = 12
    source_w = 8
    type_w = 14
    header = (
        f"{'Name':<{name_w}}  {'Status':<{status_w}}  {'Source':<{source_w}}"
        f"  {'Type':<{type_w}}  URL/Option"
    )
    print(f"{BOLD}{header}{RESET}")
    print("-" * min(len(header) + 20, width))
    for r in rows:
        status = r.get("status", "")
        color = _STATUS_COLOR.get(status, "")
        url_or_opt = r.get("url") or r.get("option") or r.get("input") or ""
        line = (
            f"{r.get('name', ''):<{name_w}}  "
            f"{color}{status:<{status_w}}{RESET}  "
            f"{r.get('source', ''):<{source_w}}  "
            f"{r.get('type', ''):<{type_w}}  "
            f"{url_or_opt}"
        )
        print(line[: width])
    return 0


def cmd_module_enable(cfg: Config, args: argparse.Namespace) -> int:
    try:
        core.enable_module(args.name, cfg.modules_file)
        print(f"{GREEN}✓{RESET} Module '{args.name}' enabled and system rebuilt.")
        return 0
    except ValueError as exc:
        err(str(exc))
        return 1
    except subprocess.CalledProcessError:
        err("nixos-rebuild failed. The module was enabled in modules.json but the rebuild did not succeed.")
        return 1


def cmd_module_disable(cfg: Config, args: argparse.Namespace) -> int:
    try:
        core.disable_module(args.name, cfg.modules_file)
        print(f"{GREEN}✓{RESET} Module '{args.name}' disabled and system rebuilt.")
        return 0
    except ValueError as exc:
        err(str(exc))
        return 1
    except subprocess.CalledProcessError:
        err("nixos-rebuild failed. The module was disabled in modules.json but the rebuild did not succeed.")
        return 1


def cmd_module_add(cfg: Config, args: argparse.Namespace) -> int:
    def _print_line(line: str) -> None:
        print(line)

    try:
        name = core.add_flake_module(
            url=args.url,
            modules_path=cfg.modules_file,
            flake_file_path=cfg.flake_file,
            flake_dir=cfg.flake,
            name=args.name,
            progress_callback=_print_line,
        )
        print(f"\n{GREEN}✓{RESET} Registered '{name}' (enabled=false). Use 'nixstore module enable {name}' to activate.")
        return 0
    except ValueError as exc:
        err(str(exc))
        return 1
    except subprocess.CalledProcessError as exc:
        err(f"nix flake update failed (exit {exc.returncode}).")
        return 1


def cmd_module_remove(cfg: Config, args: argparse.Namespace) -> int:
    try:
        core.remove_module(args.name, cfg.modules_file, cfg.flake_file)
        print(f"{GREEN}✓{RESET} Module '{args.name}' removed from registry.")
        print(f"{YELLOW}!{RESET} Run 'sudo nixos-rebuild switch --flake {cfg.flake}' to apply.")
        return 0
    except PermissionError as exc:
        err(str(exc))
        return 1
    except ValueError as exc:
        err(str(exc))
        return 1


def cmd_module_register(cfg: Config, args: argparse.Namespace) -> int:
    try:
        entry = core.register_input(args.name, cfg.modules_file, cfg.flake)
        print(f"{GREEN}✓{RESET} Registered '{args.name}': {entry}")
        return 0
    except ValueError as exc:
        err(str(exc))
        return 1


def cmd_module(cfg: Config, args: argparse.Namespace) -> int:
    module_commands = {
        "list": cmd_module_list,
        "enable": cmd_module_enable,
        "disable": cmd_module_disable,
        "add": cmd_module_add,
        "remove": cmd_module_remove,
        "register": cmd_module_register,
    }
    if args.module_command is None:
        err("a module subcommand is required. Try 'nixstore module --help'.")
        return 1
    return module_commands[args.module_command](cfg, args)


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

    # module subcommand group
    mod = sub.add_parser("module", help="manage NixOS modules from flake inputs")
    mod_sub = mod.add_subparsers(dest="module_command", metavar="module_command")

    mod_sub.add_parser("list", help="show all modules (registered + unregistered from flake.lock)")

    p = mod_sub.add_parser("enable", help="enable a module and rebuild")
    p.add_argument("name")

    p = mod_sub.add_parser("disable", help="disable a module and rebuild")
    p.add_argument("name")

    p = mod_sub.add_parser("add", help="add a flake input URL and register it as a module")
    p.add_argument("url")
    p.add_argument("--name", dest="name", default=None, help="override the derived input name")

    p = mod_sub.add_parser("remove", help="remove a module from the registry (rebuild separately)")
    p.add_argument("name")

    p = mod_sub.add_parser("register", help="register an already-locked flake input as a module")
    p.add_argument("name")

    args = parser.parse_args(argv)
    cfg = Config.from_env(args.flake)

    if args.command in (None, "tui"):
        from . import tui  # textual is only needed here

        tui.run(cfg, " ".join(getattr(args, "query", [])))
        return 0
    commands = {
        "search": cmd_search,
        "list": cmd_list,
        "install": cmd_install,
        "remove": cmd_remove,
        "module": cmd_module,
    }
    try:
        return commands[args.command](cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
