"""NixStore TUI: sidebar-nav with System Update, Nix Packages, and Flatpaks panels."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from . import core
from .core import Config, FlatpakPackage, Package, SystemUpdateStatus


# ── shared widgets ─────────────────────────────────────────────────────────────

class SearchInput(Input):
    """Search/filter box that forwards arrow keys to the sibling table."""

    BINDINGS = [
        Binding("enter", "submit", "Mark / unmark"),
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("pagedown", "move(10)", show=False),
        Binding("pageup", "move(-10)", show=False),
    ]

    def action_move(self, delta: int) -> None:
        table = self.parent.query_one(DataTable)
        if table.row_count:
            table.move_cursor(row=max(0, min(table.row_count - 1, table.cursor_row + delta)))


class PackageTable(DataTable):
    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self.add_column(" ", key="status", width=1)
        self.add_column("Package", key="attr")
        self.add_column("Version", key="version")
        self.add_column("Description", key="description")

    def highlighted_attr(self) -> str | None:
        if not self.row_count:
            return None
        return self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value


# ── nix apply modal ────────────────────────────────────────────────────────────

class ApplyScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, cfg: Config, changes: dict[str, str], installed: set[str]) -> None:
        super().__init__()
        self.cfg = cfg
        self.changes = changes
        adds = {a for a, c in changes.items() if c == "install"}
        removes = {a for a, c in changes.items() if c == "remove"}
        self.new_installed = (installed | adds) - removes
        self.running = False
        self.succeeded = False

    def compose(self) -> ComposeResult:
        summary = Text()
        for attr, change in sorted(self.changes.items()):
            summary.append(" + " if change == "install" else " − ",
                           "bold green" if change == "install" else "bold red")
            summary.append(attr + "\n")
        with Vertical(id="dialog"):
            yield Label("Apply Nix changes", id="dialog-title")
            yield Static(summary, id="changes")
            yield Input(password=True, placeholder="sudo password", id="password")
            yield RichLog(id="log", wrap=True, markup=False)
            yield Label("Enter: rebuild · Esc: cancel", id="status")

    def on_mount(self) -> None:
        self.query_one("#log").display = False
        self.query_one("#password").focus()

    def set_status(self, text: str, style: str = "") -> None:
        self.query_one("#status", Label).update(Text(text, style=style))

    def action_close(self) -> None:
        if not self.running:
            self.dismiss(self.succeeded)

    @on(Input.Submitted, "#password")
    def submitted(self, event: Input.Submitted) -> None:
        if not self.running and not self.succeeded:
            password = event.value
            event.input.value = ""
            self.run_rebuild(password)

    @work(exclusive=True)
    async def run_rebuild(self, password: str) -> None:
        self.running = True
        pw_input = self.query_one("#password", Input)
        log = self.query_one("#log", RichLog)
        try:
            self.set_status("Checking password…")
            check = await asyncio.create_subprocess_exec(
                "sudo", "-S", "-k", "-v", "-p", "",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await check.communicate((password + "\n").encode())
            if check.returncode != 0:
                self.set_status("Wrong password — try again · Esc: cancel", "bold red")
                pw_input.focus()
                return

            pw_input.display = False
            log.display = True
            self.set_status("Rebuilding… (this can take a while)", "bold yellow")

            with core.Transaction(self.cfg, self.new_installed) as tx:
                proc = await asyncio.create_subprocess_exec(
                    *tx.argv("-S", "-k", "-p", ""),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                proc.stdin.write((password + "\n").encode())
                await proc.stdin.drain()
                proc.stdin.close()
                del password
                async for raw in proc.stdout:
                    log.write(Text.from_ansi(raw.decode(errors="replace").rstrip("\n")))
                await proc.wait()

            names = ", ".join(sorted(self.changes))
            if proc.returncode == 0:
                self.succeeded = True
                self.set_status("✓ Done — Esc: back · Ctrl+Q: quit", "bold green")
                core.notify("Packages updated", names)
            else:
                self.set_status(
                    f"✗ Rebuild failed — {self.cfg.packages_file.name} was restored · Esc: back",
                    "bold red",
                )
                core.notify("Rebuild failed", names, "critical")
        finally:
            self.running = False


# ── flatpak apply modal ────────────────────────────────────────────────────────

class FlatpakApplyScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, changes: dict[str, str]) -> None:
        super().__init__()
        self.changes = changes
        self.running = False
        self.succeeded = False

    def compose(self) -> ComposeResult:
        summary = Text()
        for app_id, change in sorted(self.changes.items()):
            summary.append(" + " if change == "install" else " − ",
                           "bold green" if change == "install" else "bold red")
            summary.append(app_id + "\n")
        with Vertical(id="dialog"):
            yield Label("Apply Flatpak changes", id="dialog-title")
            yield Static(summary, id="changes")
            yield RichLog(id="log", wrap=True, markup=False)
            yield Label("Working…", id="status")

    def on_mount(self) -> None:
        self.run_apply()

    def set_status(self, text: str, style: str = "") -> None:
        self.query_one("#status", Label).update(Text(text, style=style))

    def action_close(self) -> None:
        if not self.running:
            self.dismiss(self.succeeded)

    @work(exclusive=True)
    async def run_apply(self) -> None:
        self.running = True
        log = self.query_one("#log", RichLog)

        def write_log(line: str) -> None:
            log.write(Text.from_ansi(line))

        try:
            ok = True
            for app_id, change in sorted(self.changes.items()):
                if change == "install":
                    self.set_status(f"Installing {app_id}…", "bold yellow")
                    ok = await core.install_flatpak(app_id, write_log) and ok
                else:
                    self.set_status(f"Removing {app_id}…", "bold yellow")
                    ok = await core.remove_flatpak(app_id, write_log) and ok

            names = ", ".join(sorted(self.changes))
            if ok:
                self.succeeded = True
                self.set_status("✓ Done — Esc: back", "bold green")
                core.notify("Flatpaks updated", names)
            else:
                self.set_status("✗ Some operations failed — Esc: back", "bold red")
                core.notify("Flatpak update failed", names, "critical")
        finally:
            self.running = False


# ── system update panel ────────────────────────────────────────────────────────

class UpdateApplyScreen(ModalScreen[bool]):
    """Modal that streams the system update, asking for sudo password when needed."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, cfg: Config, status: SystemUpdateStatus) -> None:
        super().__init__()
        self.cfg = cfg
        self.status = status
        self.running = False
        self.succeeded = False
        self._pw_future: asyncio.Future | None = None

    def compose(self) -> ComposeResult:
        info = Text()
        if self.status.commits_behind:
            info.append(f"{self.status.commits_behind} update(s) to apply:\n\n", "bold")
            for c in self.status.commits[:8]:
                info.append(f"  {c}\n", "dim")
            info.append("\n")
        info.append("Will run: ", "dim")
        if self.status.is_git_repo:
            info.append("git pull  →  ", "dim")
        info.append("update.sh (dotfiles sync + nixos-rebuild switch)", "dim")
        with Vertical(id="dialog"):
            yield Label("Apply System Update", id="dialog-title")
            yield Static(info, id="su-modal-status")
            yield RichLog(id="su-modal-log", wrap=True, markup=False)
            yield Input(password=True, placeholder="sudo password", id="su-modal-password")
            yield Label("", id="su-modal-footer")

    def on_mount(self) -> None:
        self.query_one("#su-modal-password").display = False
        self._set_footer("Starting…", "dim")
        self.apply_updates()

    def _set_footer(self, text: str, style: str = "") -> None:
        self.query_one("#su-modal-footer", Label).update(Text(text, style=style))

    def action_close(self) -> None:
        if not self.running:
            self.dismiss(self.succeeded)

    async def _request_password(self, prompt: str) -> str:
        """Show the password Input, wait for the user to submit it."""
        self._pw_future = asyncio.get_running_loop().create_future()
        pw = self.query_one("#su-modal-password", Input)
        pw.display = True
        self._set_footer("Enter sudo password and press Enter.")
        pw.focus()
        try:
            return await self._pw_future
        finally:
            self._pw_future = None

    @on(Input.Submitted, "#su-modal-password")
    def password_submitted(self, event: Input.Submitted) -> None:
        password = event.value
        event.input.value = ""
        event.input.display = False
        self._set_footer("Updating… (this can take a while)", "bold yellow")
        if self._pw_future is not None and not self._pw_future.done():
            self._pw_future.set_result(password)

    @work(exclusive=True)
    async def apply_updates(self) -> None:
        self.running = True
        log = self.query_one("#su-modal-log", RichLog)
        self._set_footer("Updating… (this can take a while)", "bold yellow")
        try:
            async def write_log(line: str) -> None:
                log.write(Text.from_ansi(line))

            ok = await core.run_system_update(
                self.cfg.flake, write_log, self._request_password
            )
            if ok:
                self.succeeded = True
                self._set_footer("✓ Done — Esc to close.", "bold green")
                core.notify("System updated", "nixos-rebuild succeeded")
            else:
                self._set_footer("✗ Update failed — see log above · Esc to close.", "bold red")
                core.notify("System update failed", "", "critical")
        finally:
            self.running = False
            self.set_focus(None)


class SystemUpdatePanel(Vertical):
    """Auto-checks on mount; Ctrl+S opens the apply modal when updates are found."""

    can_focus = True

    def __init__(self, cfg: Config, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cfg = cfg
        self._running = False
        self._last_status: SystemUpdateStatus | None = None

    def compose(self) -> ComposeResult:
        yield Label("System Update", id="su-title")
        yield Static("Checking for updates…", id="su-status")
        yield Label("", id="su-footer")

    def on_mount(self) -> None:
        self.check_updates()

    def _on_show(self) -> None:
        self.focus()

    def _set_footer(self, text: str, style: str = "") -> None:
        self.query_one("#su-footer", Label).update(Text(text, style=style))

    def action_recheck(self) -> None:
        if not self._running:
            self.check_updates()

    def action_apply_update(self) -> None:
        if self._last_status is not None and not self._running:
            def done(succeeded: bool | None) -> None:
                if succeeded:
                    self.check_updates()
                else:
                    self.focus()
            self.app.push_screen(UpdateApplyScreen(self.cfg, self._last_status), done)

    @work(exclusive=True, group="su-check")
    async def check_updates(self) -> None:
        self._running = True
        self._last_status = None
        status_widget = self.query_one("#su-status", Static)
        status_widget.update(Text("Checking for updates… (fetching flake inputs, may take a moment)", "dim"))
        self._set_footer("")
        try:
            status = await asyncio.to_thread(core.check_system_updates, self.cfg.flake)
            self._last_status = status
            if not status.is_git_repo:
                status_widget.update(Text("Dotfiles repo not configured — run install.sh first.", "dim"))
                self._set_footer("r to re-check")
                return

            if not status.has_updates:
                status_widget.update(Text("✓ Up to date.", "green"))
                self._set_footer("r to re-check")
                return

            summary = Text()
            if status.commits_behind:
                summary.append(f"{status.commits_behind} new dotfiles commit(s):\n\n", "bold")
                for c in status.commits[:8]:
                    summary.append(f"  {c}\n", "dim")
                if status.flake_inputs_updated:
                    summary.append("\n")
            if status.flake_inputs_updated:
                summary.append(f"{len(status.flake_inputs_updated)} package input(s) updated:\n\n", "bold")
                for inp in status.flake_inputs_updated:
                    summary.append(f"  {inp}\n", "dim")
            status_widget.update(summary)
            self._set_footer("u to apply · r to re-check")
        finally:
            self._running = False
            self.focus()


# ── nix packages panel ─────────────────────────────────────────────────────────

class NixPackagesPanel(Vertical):
    """Search nixpkgs, queue and apply installs/removals."""

    def __init__(self, cfg: Config, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cfg = cfg
        self.packages: list[Package] = []
        self.by_attr: dict[str, Package] = {}
        self.installed = core.read_installed(cfg)
        self.system = core.read_system_packages(cfg)
        self.pending: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent(id="nix-tabs"):
            with TabPane("Search nixpkgs", id="nsearch"):
                yield SearchInput(placeholder="Search packages…", id="nix-search-input")
                yield PackageTable(id="nix-search-table")
            with TabPane("Installed (0)", id="ninstalled"):
                yield SearchInput(placeholder="Filter installed…", id="nix-installed-input")
                yield PackageTable(id="nix-installed-table")
        yield Static(id="nix-details")
        yield Static(id="nix-pending")

    def on_mount(self) -> None:
        self.refresh_installed_tab()
        self.refresh_pending()
        self.load_packages()

    def on_show(self) -> None:
        try:
            self.active_input().focus()
        except Exception:  # noqa: BLE001
            pass

    # --- data -----------------------------------------------------------------

    @work(exclusive=True, group="nix-index")
    async def load_packages(self) -> None:
        table = self.query_one("#nix-search-table", PackageTable)
        table.loading = True
        self._show_msg("Loading package index… (first run after a nixpkgs update takes ~30s)")
        try:
            self.packages = await core.load_index(self.cfg)
        except Exception as exc:  # noqa: BLE001
            self._show_msg(f"Could not load package index: {exc}", "bold red")
            return
        finally:
            table.loading = False
        self.by_attr = {p.attr: p for p in self.packages}
        self.refresh_installed_tab()
        self.run_search(self.query_one("#nix-search-input", Input).value)

    # --- rendering ------------------------------------------------------------

    def _status_cell(self, attr: str) -> Text:
        ch = self.pending.get(attr)
        if ch == "install":
            return Text("+", "bold green")
        if ch == "remove":
            return Text("−", "bold red")
        if attr in self.installed:
            return Text("✓", "green")
        if attr in self.system:
            return Text("•", "cyan")
        return Text(" ")

    def _fill(self, table: PackageTable, packages: list[Package]) -> None:
        keep = table.highlighted_attr()
        table.clear()
        name_w = max((len(p.attr) for p in packages), default=0)
        ver_w = max((len(p.version) for p in packages), default=0)
        desc_w = max(20, (table.size.width or self.size.width) - name_w - ver_w - 10)
        for p in packages:
            style = "bold" if p.attr in self.installed or p.attr in self.pending else ""
            desc = p.description[:desc_w - 1] + "…" if len(p.description) > desc_w else p.description
            table.add_row(self._status_cell(p.attr), Text(p.attr, style), p.version, desc, key=p.attr)
        attrs = [p.attr for p in packages]
        table.move_cursor(row=attrs.index(keep) if keep in attrs else 0)
        self._show_details(table)

    @work(exclusive=True, group="nix-search")
    async def run_search(self, query: str) -> None:
        await asyncio.sleep(0.12)
        if not self.packages:
            return
        table = self.query_one("#nix-search-table", PackageTable)
        self._fill(table, core.search(self.packages, query))
        if not query.strip():
            self._show_msg(f"Type to search {len(self.packages):,} packages.")
        elif not table.row_count:
            self._show_msg(f'No package matches “{query.strip()}”.')

    def refresh_installed_tab(self) -> None:
        words = self.query_one("#nix-installed-input", Input).value.strip().lower().split()
        attrs = sorted(self.installed | set(self.pending))
        packages = [self.by_attr.get(a) or Package(a) for a in attrs]
        if words:
            packages = [p for p in packages if all(w in p.lower for w in words)]
        self._fill(self.query_one("#nix-installed-table", PackageTable), packages)
        self.query_one("#nix-tabs", TabbedContent).get_tab("ninstalled").label = (
            f"Installed ({len(self.installed)})"
        )

    def _refresh_search_status(self) -> None:
        table = self.query_one("#nix-search-table", PackageTable)
        for row_key in list(table.rows):
            table.update_cell(row_key, "status", self._status_cell(row_key.value))

    def refresh_pending(self) -> None:
        adds = sorted(a for a, c in self.pending.items() if c == "install")
        removes = sorted(a for a, c in self.pending.items() if c == "remove")
        if not self.pending:
            text = Text("No pending changes. Enter marks a package to install or remove.", "dim")
        else:
            text = Text()
            if adds:
                text.append(f"+{len(adds)} ", "bold green").append(", ".join(adds) + "   ")
            if removes:
                text.append(f"−{len(removes)} ", "bold red").append(", ".join(removes) + "   ")
            text.append("Ctrl+S to apply", "bold")
        self.query_one("#nix-pending", Static).update(text)

    def _show_msg(self, msg: str, style: str = "dim") -> None:
        self.query_one("#nix-details", Static).update(Text(msg, style))

    def _show_details(self, table: PackageTable) -> None:
        attr = table.highlighted_attr()
        if attr is None:
            return
        p = self.by_attr.get(attr) or Package(attr)
        ch = self.pending.get(attr)
        if ch == "install":
            hint = ("will be installed · Enter: undo", "green")
        elif ch == "remove":
            hint = ("will be removed · Enter: undo", "red")
        elif attr in self.installed:
            hint = ("installed · Enter: mark for removal", "green")
        elif attr in self.system:
            hint = (f"installed via {self.system[attr]} · edit that file to remove it", "cyan")
        else:
            hint = ("not installed · Enter: mark for install", "dim")
        text = Text()
        text.append(p.attr, "bold").append("   ").append(p.version, "dim").append("\n")
        text.append(hint[0] + "\n", hint[1])
        text.append(p.description or "")
        self.query_one("#nix-details", Static).update(text)

    def active_table(self) -> PackageTable:
        tab = self.query_one("#nix-tabs", TabbedContent).active  # "nsearch" or "ninstalled"
        suffix = "search" if tab == "nsearch" else "installed"
        return self.query_one(f"#nix-{suffix}-table", PackageTable)

    def active_input(self) -> Input:
        tab = self.query_one("#nix-tabs", TabbedContent).active
        suffix = "search" if tab == "nsearch" else "installed"
        return self.query_one(f"#nix-{suffix}-input", Input)

    def has_pending(self) -> bool:
        return bool(self.pending)

    def clear_active_input(self) -> bool:
        box = self.active_input()
        if box.value:
            box.value = ""
            return True
        return False

    # --- events ---------------------------------------------------------------

    @on(Input.Changed, "#nix-search-input")
    def search_changed(self, event: Input.Changed) -> None:
        self.run_search(event.value)

    @on(Input.Changed, "#nix-installed-input")
    def installed_changed(self, _: Input.Changed) -> None:
        self.refresh_installed_tab()

    @on(Input.Submitted)
    def input_submitted(self, _: Input.Submitted) -> None:
        self._action_toggle()

    @on(DataTable.RowHighlighted)
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self.active_table():
            self._show_details(event.data_table)

    @on(DataTable.RowSelected)
    def row_clicked(self, _: DataTable.RowSelected) -> None:
        self._action_toggle()

    @on(TabbedContent.TabActivated)
    def tab_activated(self, event: TabbedContent.TabActivated) -> None:
        event.pane.query_one(Input).focus()
        self._show_details(self.active_table())

    # --- actions --------------------------------------------------------------

    def _action_toggle(self) -> None:
        attr = self.active_table().highlighted_attr()
        if attr is None:
            return
        if attr in self.pending:
            del self.pending[attr]
        elif attr in self.installed:
            self.pending[attr] = "remove"
        elif attr in self.system:
            self.notify(f"{attr} is set in {self.system[attr]}; edit that file to remove it.",
                        severity="warning")
            return
        else:
            self.pending[attr] = "install"
        self._refresh_search_status()
        self.refresh_installed_tab()
        self.refresh_pending()
        self._show_details(self.active_table())

    def action_apply(self) -> None:
        if not self.pending:
            self.notify("Nothing to apply — mark packages with Enter first.")
            return

        def done(succeeded: bool | None) -> None:
            if succeeded:
                self.installed = core.read_installed(self.cfg)
                self.pending.clear()
                self._refresh_search_status()
                self.refresh_installed_tab()
                self.refresh_pending()
            self.active_input().focus()

        self.app.push_screen(ApplyScreen(self.cfg, dict(self.pending), self.installed), done)

    def action_next_tab(self) -> None:
        tabs = self.query_one("#nix-tabs", TabbedContent)
        tabs.active = "ninstalled" if tabs.active == "nsearch" else "nsearch"

    def on_resize(self) -> None:
        if self.packages:
            self.run_search(self.query_one("#nix-search-input", Input).value)
            self.refresh_installed_tab()


# ── flatpak packages panel ─────────────────────────────────────────────────────

class FlatpakPackagesPanel(Vertical):
    """Search and manage Flatpak packages from Flathub."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.installed: list[FlatpakPackage] = []
        self.installed_ids: set[str] = set()
        self.pending: dict[str, str] = {}
        self._flatpak_available = True

    def compose(self) -> ComposeResult:
        with TabbedContent(id="fp-tabs"):
            with TabPane("Search Flathub", id="fsearch"):
                yield SearchInput(placeholder="Search Flatpak apps…", id="fp-search-input")
                yield PackageTable(id="fp-search-table")
            with TabPane("Installed (0)", id="finstalled"):
                yield SearchInput(placeholder="Filter installed…", id="fp-installed-input")
                yield PackageTable(id="fp-installed-table")
        yield Static(id="fp-details")
        yield Static(id="fp-pending")

    def on_mount(self) -> None:
        self.refresh_pending()
        self._init_flatpak()

    def on_show(self) -> None:
        try:
            self.active_input().focus()
        except Exception:  # noqa: BLE001
            pass

    @work(exclusive=True, group="fp-init")
    async def _init_flatpak(self) -> None:
        import shutil
        if not shutil.which("flatpak"):
            self._flatpak_available = False
            self._show_msg(
                "flatpak is not installed. Add it to your NixOS configuration to use this panel.",
                "bold yellow",
            )
            return
        self._show_msg("Initialising Flatpak (adding Flathub remote if needed)…")
        try:
            await core.ensure_flathub()
            pkgs = await core.list_flatpaks()
        except FileNotFoundError:
            self._flatpak_available = False
            self._show_msg("flatpak not found. Install it via NixOS to use this panel.", "bold yellow")
            return
        self.installed = pkgs
        self.installed_ids = {p.app_id for p in pkgs}
        self.refresh_installed_tab()
        self._show_msg("Type to search Flatpak apps.")

    # --- rendering ------------------------------------------------------------

    def _status_cell(self, app_id: str) -> Text:
        ch = self.pending.get(app_id)
        if ch == "install":
            return Text("+", "bold green")
        if ch == "remove":
            return Text("−", "bold red")
        if app_id in self.installed_ids:
            return Text("✓", "green")
        return Text(" ")

    def _fill_table(self, table: PackageTable, packages: list[FlatpakPackage]) -> None:
        keep = table.highlighted_attr()
        table.clear()
        name_w = max((len(p.name) for p in packages), default=0)
        ver_w = max((len(p.version) for p in packages), default=0)
        desc_w = max(20, (table.size.width or self.size.width) - name_w - ver_w - 10)
        for p in packages:
            style = "bold" if p.app_id in self.installed_ids or p.app_id in self.pending else ""
            desc = p.description[:desc_w - 1] + "…" if len(p.description) > desc_w else p.description
            table.add_row(self._status_cell(p.app_id), Text(p.name, style), p.version, desc, key=p.app_id)
        ids = [p.app_id for p in packages]
        table.move_cursor(row=ids.index(keep) if keep in ids else 0)
        self._show_details(table)

    @work(exclusive=True, group="fp-search")
    async def run_search(self, query: str) -> None:
        await asyncio.sleep(0.15)
        if not self._flatpak_available:
            return
        if not query.strip():
            self._show_msg("Type to search Flatpak apps.")
            return
        table = self.query_one("#fp-search-table", PackageTable)
        table.loading = True
        try:
            results = await core.search_flatpaks(query)
        except FileNotFoundError:
            self._show_msg("flatpak not found.", "bold yellow")
            return
        finally:
            table.loading = False
        self._fill_table(table, results)
        if not results:
            self._show_msg(f'No Flatpak matches “{query.strip()}”.')

    def refresh_installed_tab(self) -> None:
        words = self.query_one("#fp-installed-input", Input).value.strip().lower().split()
        packages = [p for p in self.installed if not words or all(w in p.lower for w in words)]
        # include pending installs not yet in installed list
        pending_installs = [
            FlatpakPackage(app_id=a) for a in self.pending
            if self.pending[a] == "install" and a not in self.installed_ids
        ]
        self._fill_table(self.query_one("#fp-installed-table", PackageTable), packages + pending_installs)
        self.query_one("#fp-tabs", TabbedContent).get_tab("finstalled").label = (
            f"Installed ({len(self.installed)})"
        )

    def _refresh_search_status(self) -> None:
        table = self.query_one("#fp-search-table", PackageTable)
        for row_key in list(table.rows):
            table.update_cell(row_key, "status", self._status_cell(row_key.value))

    def refresh_pending(self) -> None:
        adds = sorted(a for a, c in self.pending.items() if c == "install")
        removes = sorted(a for a, c in self.pending.items() if c == "remove")
        if not self.pending:
            text = Text("No pending changes. Enter marks an app to install or remove.", "dim")
        else:
            text = Text()
            if adds:
                text.append(f"+{len(adds)} ", "bold green").append(", ".join(adds) + "   ")
            if removes:
                text.append(f"−{len(removes)} ", "bold red").append(", ".join(removes) + "   ")
            text.append("Ctrl+S to apply", "bold")
        self.query_one("#fp-pending", Static).update(text)

    def _show_msg(self, msg: str, style: str = "dim") -> None:
        self.query_one("#fp-details", Static).update(Text(msg, style))

    def _show_details(self, table: PackageTable) -> None:
        app_id = table.highlighted_attr()
        if app_id is None:
            return
        ch = self.pending.get(app_id)
        if ch == "install":
            hint = ("will be installed · Enter: undo", "green")
        elif ch == "remove":
            hint = ("will be removed · Enter: undo", "red")
        elif app_id in self.installed_ids:
            hint = ("installed · Enter: mark for removal", "green")
        else:
            hint = ("not installed · Enter: mark for install", "dim")
        text = Text()
        text.append(app_id, "bold").append("\n")
        text.append(hint[0], hint[1])
        self.query_one("#fp-details", Static).update(text)

    def active_table(self) -> PackageTable:
        tab = self.query_one("#fp-tabs", TabbedContent).active  # "fsearch" or "finstalled"
        suffix = "search" if tab == "fsearch" else "installed"
        return self.query_one(f"#fp-{suffix}-table", PackageTable)

    def active_input(self) -> Input:
        tab = self.query_one("#fp-tabs", TabbedContent).active
        suffix = "search" if tab == "fsearch" else "installed"
        return self.query_one(f"#fp-{suffix}-input", Input)

    def has_pending(self) -> bool:
        return bool(self.pending)

    def clear_active_input(self) -> bool:
        box = self.active_input()
        if box.value:
            box.value = ""
            return True
        return False

    # --- events ---------------------------------------------------------------

    @on(Input.Changed, "#fp-search-input")
    def search_changed(self, event: Input.Changed) -> None:
        self.run_search(event.value)

    @on(Input.Changed, "#fp-installed-input")
    def installed_changed(self, _: Input.Changed) -> None:
        self.refresh_installed_tab()

    @on(Input.Submitted)
    def input_submitted(self, _: Input.Submitted) -> None:
        self._action_toggle()

    @on(DataTable.RowHighlighted)
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self.active_table():
            self._show_details(event.data_table)

    @on(DataTable.RowSelected)
    def row_clicked(self, _: DataTable.RowSelected) -> None:
        self._action_toggle()

    @on(TabbedContent.TabActivated)
    def tab_activated(self, event: TabbedContent.TabActivated) -> None:
        event.pane.query_one(Input).focus()
        self._show_details(self.active_table())

    # --- actions --------------------------------------------------------------

    def _action_toggle(self) -> None:
        app_id = self.active_table().highlighted_attr()
        if app_id is None:
            return
        if app_id in self.pending:
            del self.pending[app_id]
        elif app_id in self.installed_ids:
            self.pending[app_id] = "remove"
        else:
            self.pending[app_id] = "install"
        self._refresh_search_status()
        self.refresh_installed_tab()
        self.refresh_pending()
        self._show_details(self.active_table())

    def action_apply(self) -> None:
        if not self._flatpak_available:
            self.notify("Flatpak is not installed on this system.", severity="warning")
            return
        if not self.pending:
            self.notify("Nothing to apply — mark apps with Enter first.")
            return

        def done(succeeded: bool | None) -> None:
            if succeeded:
                self.pending.clear()
                self._init_flatpak()
            self.refresh_pending()
            self.active_input().focus()

        self.app.push_screen(FlatpakApplyScreen(dict(self.pending)), done)

    def action_next_tab(self) -> None:
        tabs = self.query_one("#fp-tabs", TabbedContent)
        tabs.active = "finstalled" if tabs.active == "fsearch" else "fsearch"


# ── sidebar ────────────────────────────────────────────────────────────────────

_NAV_PANELS = ["panel-nix", "panel-flatpak", "panel-system"]
_NAV_LABELS = ["  Nix Packages", "  Flatpaks", "⟳  System Update"]


class Sidebar(Vertical):
    def compose(self) -> ComposeResult:
        yield Label("NIXSTORE", id="brand")
        yield ListView(
            *[ListItem(Label(label)) for label in _NAV_LABELS],
            id="nav",
        )

    @on(ListView.Selected)
    def nav_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None:
            return
        panel_id = _NAV_PANELS[idx]
        app = self.app
        app.query_one(ContentSwitcher).current = panel_id
        app.quit_armed = False  # type: ignore[attr-defined]
        try:
            panel = app.query_one(f"#{panel_id}")
            if hasattr(panel, "active_input"):
                panel.active_input().focus()
            elif panel.can_focus:
                panel.focus()
        except Exception:  # noqa: BLE001
            pass


# ── main app ───────────────────────────────────────────────────────────────────

class NixStore(App):
    TITLE = "NixStore"

    CSS = """
    /* ── layout ── */
    Header { height: 1; }
    #main-row { height: 1fr; }

    Sidebar {
        width: 22;
        height: 1fr;
        background: $boost;
        border-right: tall $primary-darken-2;
        padding: 1 0;
    }
    #brand {
        text-align: center;
        text-style: bold;
        color: $accent;
        padding: 0 1 1 1;
    }
    #nav { background: $boost; height: auto; }
    #nav > ListItem { padding: 0 1; }

    ContentSwitcher { width: 1fr; height: 1fr; }
    ContentSwitcher > * { width: 1fr; height: 1fr; }

    /* ── system update panel ── */
    #panel-system { padding: 1 2; }
    SystemUpdatePanel { height: 1fr; }
    #su-title { text-style: bold; margin-bottom: 1; }
    #su-status { height: auto; margin-bottom: 1; }
    #su-footer { color: $text-muted; }

    /* ── update apply modal ── */
    UpdateApplyScreen { align: center middle; }
    #su-modal-status { height: auto; margin-bottom: 1; }
    #su-modal-password { margin-bottom: 1; }
    #su-modal-log { height: 1fr; border: round $primary-darken-2; }
    #su-modal-footer { margin-top: 1; }

    /* ── shared package panel ── */
    NixPackagesPanel, FlatpakPackagesPanel { padding: 0 1; }
    NixPackagesPanel TabbedContent,
    FlatpakPackagesPanel TabbedContent { height: 1fr; }
    NixPackagesPanel TabbedContent ContentSwitcher,
    FlatpakPackagesPanel TabbedContent ContentSwitcher { height: 1fr; }
    NixPackagesPanel TabPane,
    FlatpakPackagesPanel TabPane { height: 1fr; padding: 0; }
    PackageTable { height: 1fr; }
    #nix-details, #fp-details { height: 5; border: round $primary-darken-2; padding: 0 1; }
    #nix-pending, #fp-pending { height: 1; padding: 0 1; background: $boost; }

    /* ── modals ── */
    ApplyScreen, FlatpakApplyScreen { align: center middle; }
    #dialog {
        width: 90%; height: 85%; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #changes { height: auto; max-height: 10; margin-bottom: 1; }
    #log { height: 1fr; border: round $primary-darken-2; }
    #status { margin-top: 1; }
    """

    BINDINGS = [
        Binding("tab", "next_tab", "Switch tab", priority=True),
        Binding("ctrl+s", "apply", "Apply changes", show=False),
        Binding("escape", "back", "Clear / quit"),
        Binding("r", "recheck_updates", "Re-check", show=False),
        Binding("u", "apply_system_update", "Apply update", show=False),
    ]

    def __init__(self, cfg: Config, query: str = "") -> None:
        super().__init__()
        self.cfg = cfg
        self.sub_title = str(cfg.packages_file)
        self.initial_query = query
        self.quit_armed = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main-row"):
            yield Sidebar()
            with ContentSwitcher(initial="panel-nix"):
                yield SystemUpdatePanel(cfg=self.cfg, id="panel-system")
                yield NixPackagesPanel(id="panel-nix", cfg=self.cfg)
                yield FlatpakPackagesPanel(id="panel-flatpak")
        yield Footer()

    def on_mount(self) -> None:
        # pre-fill search query if launched with one
        if self.initial_query:
            nix = self.query_one(NixPackagesPanel)
            nix.query_one("#nix-search-input", Input).value = self.initial_query
        self.query_one(NixPackagesPanel).query_one("#nix-search-input").focus()
        # select the Nix Packages item in the sidebar (index 0)
        self.query_one("#nav", ListView).index = 0

    def _active_panel(self) -> NixPackagesPanel | FlatpakPackagesPanel | None:
        current = self.query_one(ContentSwitcher).current
        if current == "panel-nix":
            return self.query_one(NixPackagesPanel)
        if current == "panel-flatpak":
            return self.query_one(FlatpakPackagesPanel)
        return None

    def action_apply(self) -> None:
        panel = self._active_panel()
        if panel is not None:
            panel.action_apply()

    def action_apply_system_update(self) -> None:
        if self.query_one(ContentSwitcher).current == "panel-system":
            self.query_one(SystemUpdatePanel).action_apply_update()

    def action_recheck_updates(self) -> None:
        if self.query_one(ContentSwitcher).current == "panel-system":
            self.query_one(SystemUpdatePanel).action_recheck()

    def action_next_tab(self) -> None:
        panel = self._active_panel()
        if panel is not None:
            panel.action_next_tab()

    def action_back(self) -> None:
        panel = self._active_panel()
        if panel is None:
            if self.query_one(ContentSwitcher).current != "panel-system":
                self.exit()
            return
        if panel.clear_active_input():
            self.quit_armed = False
            return
        if panel.has_pending() and not self.quit_armed:
            self.quit_armed = True
            self.notify("You have unapplied changes. Esc again to quit, Ctrl+S to apply.")
            return
        self.exit()



def run(cfg: Config, query: str = "") -> None:
    NixStore(cfg, query).run()
