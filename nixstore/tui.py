"""NixStore TUI: search nixpkgs, queue installs/removals, apply with one rebuild."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from . import core
from .core import Config, Package


class SearchInput(Input):
    """Search box that drives the table below it, fzf style."""

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


class ApplyScreen(ModalScreen[bool]):
    """Review queued changes, ask for the sudo password, rebuild."""

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
            if change == "install":
                summary.append(" + ", "bold green").append(attr + "\n")
            else:
                summary.append(" − ", "bold red").append(attr + "\n")
        with Vertical(id="dialog"):
            yield Label("Apply changes", id="dialog-title")
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
                    f"✗ Rebuild failed — {self.cfg.packages_file.name} was restored, nothing changed · Esc: back",
                    "bold red",
                )
                core.notify("Rebuild failed", names, "critical")
        finally:
            self.running = False


class UpdateScreen(ModalScreen[bool]):
    """Check for and apply dotfiles upstream updates."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.succeeded = False
        self._repo = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Dotfiles updates", id="dialog-title")
            yield Static(id="update-status")
            yield Input(password=True, placeholder="sudo password (needed for nixos-rebuild)", id="update-password")
            yield RichLog(id="update-log", wrap=True, markup=False)
            yield Label("Checking…", id="update-footer")

    def on_mount(self) -> None:
        self.query_one("#update-log").display = False
        self.query_one("#update-password").display = False
        self.check_updates()

    def set_footer(self, text: str, style: str = "") -> None:
        self.query_one("#update-footer", Label).update(Text(text, style=style))

    def action_close(self) -> None:
        if not self.running:
            self.dismiss(self.succeeded)

    @work(exclusive=True)
    async def check_updates(self) -> None:
        self.running = True
        try:
            status = await asyncio.to_thread(core.check_dotfiles_updates)
            if status is None:
                self.query_one("#update-status", Static).update(
                    Text("Dotfiles repo not found.\nRun install.sh first.", "dim"))
                self.set_footer("Esc: close")
                return
            if status.up_to_date:
                self.query_one("#update-status", Static).update(
                    Text("✓ Already up to date.", "bold green"))
                self.set_footer("Esc: close")
                return
            summary = Text()
            summary.append(f"{status.count} update(s) available:\n\n", "bold")
            for c in status.commits[:8]:
                summary.append(f"  {c}\n", "dim")
            self.query_one("#update-status", Static).update(summary)
            self._repo = status.repo
            self.query_one("#update-password").display = True
            self.query_one("#update-password").focus()
            self.set_footer("Enter sudo password to apply · Esc: cancel")
        finally:
            self.running = False

    @on(Input.Submitted, "#update-password")
    def password_submitted(self, event: Input.Submitted) -> None:
        if not self.running and not self.succeeded:
            password = event.value
            event.input.value = ""
            self.apply_updates(password)

    @work(exclusive=True)
    async def apply_updates(self, password: str) -> None:
        self.running = True
        log = self.query_one("#update-log", RichLog)
        self.query_one("#update-password").display = False
        log.display = True
        self.set_footer("Updating… (this can take a while)", "bold yellow")
        try:
            async def write_log(line: str) -> None:
                log.write(Text.from_ansi(line))
            ok = await core.run_dotfiles_update(self._repo, password, write_log)
            if ok:
                self.succeeded = True
                self.set_footer("✓ Done — Esc: back · Ctrl+Q: quit", "bold green")
                core.notify("Dotfiles updated", "System config is up to date")
            else:
                self.set_footer("✗ Update failed — see log above · Esc: back", "bold red")
                core.notify("Dotfiles update failed", "", "critical")
        finally:
            self.running = False


class NixStore(App):
    TITLE = "NixStore"

    CSS = """
    #tabs { height: 1fr; }
    #tabs ContentSwitcher { height: 1fr; }
    TabPane { height: 1fr; padding: 0 1; }
    PackageTable { height: 1fr; }
    #details { height: 5; border: round $primary-darken-2; padding: 0 1; }
    #pending { height: 1; padding: 0 1; background: $boost; }
    ApplyScreen, UpdateScreen { align: center middle; }
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
        Binding("ctrl+s", "apply", "Apply changes"),
        Binding("ctrl+u", "updates", "Check updates"),
        Binding("escape", "back", "Clear / quit"),
    ]

    def __init__(self, cfg: Config, query: str = "") -> None:
        super().__init__()
        self.cfg = cfg
        self.sub_title = str(cfg.packages_file)
        self.initial_query = query
        self.packages: list[Package] = []
        self.by_attr: dict[str, Package] = {}
        self.installed = core.read_installed(cfg)
        self.system = core.read_system_packages(cfg)
        self.pending: dict[str, str] = {}
        self.quit_armed = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(id="tabs"):
            with TabPane("Search nixpkgs", id="search"):
                yield SearchInput(placeholder="Search packages…", id="search-input")
                yield PackageTable(id="search-table")
            with TabPane("Installed", id="installed"):
                yield SearchInput(placeholder="Filter installed packages…", id="installed-input")
                yield PackageTable(id="installed-table")
        yield Static(id="details")
        yield Static(id="pending")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).value = self.initial_query
        self.query_one("#search-input").focus()
        self.refresh_installed()
        self.refresh_pending()
        self.load_packages()

    # --- data ---------------------------------------------------------------

    @work(exclusive=True, group="index")
    async def load_packages(self) -> None:
        table = self.query_one("#search-table", PackageTable)
        table.loading = True
        self.show_message("Loading package index… (the first run after a nixpkgs update takes ~30s)")
        try:
            self.packages = await core.load_index(self.cfg)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.show_message(f"Could not load the package index: {exc}", "bold red")
            return
        finally:
            table.loading = False
        self.by_attr = {p.attr: p for p in self.packages}
        self.refresh_installed()
        self.run_search(self.query_one("#search-input", Input).value)

    # --- rendering ----------------------------------------------------------

    def status_cell(self, attr: str) -> Text:
        change = self.pending.get(attr)
        if change == "install":
            return Text("+", "bold green")
        if change == "remove":
            return Text("−", "bold red")
        if attr in self.installed:
            return Text("✓", "green")
        if attr in self.system:
            return Text("•", "cyan")
        return Text(" ")

    def fill(self, table: PackageTable, packages: list[Package]) -> None:
        keep = table.highlighted_attr()
        table.clear()
        # Cut descriptions to the space left so the table never scrolls sideways.
        name_w = max((len(p.attr) for p in packages), default=0)
        ver_w = max((len(p.version) for p in packages), default=0)
        desc_w = max(20, (table.size.width or self.size.width) - name_w - ver_w - 10)
        for p in packages:
            name_style = "bold" if p.attr in self.installed or p.attr in self.pending else ""
            desc = p.description if len(p.description) <= desc_w else p.description[: desc_w - 1] + "…"
            table.add_row(self.status_cell(p.attr), Text(p.attr, name_style), p.version, desc, key=p.attr)
        attrs = [p.attr for p in packages]
        table.move_cursor(row=attrs.index(keep) if keep in attrs else 0)
        self.show_details(table)

    @work(exclusive=True, group="search")
    async def run_search(self, query: str) -> None:
        await asyncio.sleep(0.12)  # debounce typing
        if not self.packages:
            return
        table = self.query_one("#search-table", PackageTable)
        self.fill(table, core.search(self.packages, query))
        if not query.strip():
            self.show_message(f"Type to search {len(self.packages):,} packages.")
        elif not table.row_count:
            self.show_message(f"No package matches “{query.strip()}”.")

    def refresh_installed(self) -> None:
        words = self.query_one("#installed-input", Input).value.strip().lower().split()
        attrs = sorted(self.installed | set(self.pending))
        packages = [self.by_attr.get(a) or Package(a) for a in attrs]
        packages = [p for p in packages if all(w in p.lower for w in words)]
        self.fill(self.query_one("#installed-table", PackageTable), packages)
        self.query_one(TabbedContent).get_tab("installed").label = f"Installed ({len(self.installed)})"

    def refresh_search_status(self) -> None:
        table = self.query_one("#search-table", PackageTable)
        for row_key in list(table.rows):
            table.update_cell(row_key, "status", self.status_cell(row_key.value))

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
        self.query_one("#pending", Static).update(text)

    def show_message(self, message: str, style: str = "dim") -> None:
        self.query_one("#details", Static).update(Text(message, style))

    def show_details(self, table: PackageTable) -> None:
        attr = table.highlighted_attr()
        if attr is None:
            return
        p = self.by_attr.get(attr) or Package(attr)
        change = self.pending.get(attr)
        if change == "install":
            hint = ("will be installed · Enter: undo", "green")
        elif change == "remove":
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
        self.query_one("#details", Static).update(text)

    def active_table(self) -> PackageTable:
        tab = self.query_one(TabbedContent).active
        return self.query_one(f"#{tab}-table", PackageTable)

    def active_input(self) -> Input:
        tab = self.query_one(TabbedContent).active
        return self.query_one(f"#{tab}-input", Input)

    # --- events -------------------------------------------------------------

    def on_resize(self) -> None:
        if self.packages:  # re-fit descriptions to the new width
            self.run_search(self.query_one("#search-input", Input).value)
            self.refresh_installed()

    @on(Input.Changed, "#search-input")
    def search_changed(self, event: Input.Changed) -> None:
        self.run_search(event.value)

    @on(Input.Changed, "#installed-input")
    def installed_changed(self, event: Input.Changed) -> None:
        self.refresh_installed()

    @on(Input.Submitted)
    def input_submitted(self) -> None:
        self.action_toggle()

    @on(DataTable.RowHighlighted)
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self.active_table():
            self.show_details(event.data_table)

    @on(DataTable.RowSelected)
    def row_clicked(self, event: DataTable.RowSelected) -> None:
        self.action_toggle()

    @on(TabbedContent.TabActivated)
    def tab_activated(self, event: TabbedContent.TabActivated) -> None:
        event.pane.query_one(Input).focus()
        self.show_details(self.active_table())

    # --- actions ------------------------------------------------------------

    def on_main_screen(self) -> bool:
        return self.screen is self.screen_stack[0]

    def action_toggle(self) -> None:
        if not self.on_main_screen():
            return
        attr = self.active_table().highlighted_attr()
        if attr is None:
            return
        if attr in self.pending:
            del self.pending[attr]
        elif attr in self.installed:
            self.pending[attr] = "remove"
        elif attr in self.system:
            self.notify(f"{attr} is set in {self.system[attr]}; edit that file to remove it.", severity="warning")
            return
        else:
            self.pending[attr] = "install"
        self.quit_armed = False
        self.refresh_search_status()
        self.refresh_installed()
        self.refresh_pending()
        self.show_details(self.active_table())

    def action_next_tab(self) -> None:
        if not self.on_main_screen():
            self.screen.focus_next()
            return
        tabs = self.query_one(TabbedContent)
        tabs.active = "installed" if tabs.active == "search" else "search"

    def action_apply(self) -> None:
        if not self.on_main_screen():
            return
        if not self.pending:
            self.notify("Nothing to apply — mark packages with Enter first.")
            return

        def done(succeeded: bool | None) -> None:
            if succeeded:
                self.installed = core.read_installed(self.cfg)
                self.pending.clear()
                self.refresh_search_status()
                self.refresh_installed()
                self.refresh_pending()
            self.active_input().focus()

        self.push_screen(ApplyScreen(self.cfg, dict(self.pending), self.installed), done)

    def action_updates(self) -> None:
        if not self.on_main_screen():
            return
        def done(succeeded: bool | None) -> None:
            self.active_input().focus()
        self.push_screen(UpdateScreen(), done)

    def action_back(self) -> None:
        box = self.active_input()
        if box.value:
            box.value = ""
        elif self.pending and not self.quit_armed:
            self.quit_armed = True
            self.notify("You have unapplied changes. Esc again to quit without applying, Ctrl+S to apply.")
        else:
            self.exit()


def run(cfg: Config, query: str = "") -> None:
    NixStore(cfg, query).run()
