"""Optional Textual UI; imported only by ``quackpack tui``."""

from __future__ import annotations

import asyncio

from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static

from .engine import EngineError, QueryResult, run_query
from .history import ERROR, OK
from .params import extract_params
from .snapshots import Snapshot, SnapshotError, load_snapshot, save_snapshot
from .store import Catalog, CatalogError
from .targets import TargetError
from .templating import TemplateError, expand_query
from .tui import build_query_list, resolve_run


class ParamPrompt(ModalScreen[dict[str, str] | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__()
        self.names = names

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Missing parameters (auto-typed as int / float / text)")
            for index, name in enumerate(self.names):
                yield Label(Text(f":{name}"))
                yield Input(id=f"param-{index}")
            yield Button("Run", id="confirm", variant="primary")
            yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss({
            name: self.query_one(f"#param-{index}", Input).value
            for index, name in enumerate(self.names)
        })

    @on(Input.Submitted)
    def submit(self) -> None:
        self.confirm()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close"), ("?", "close", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(
                "Browse with arrows; Enter on the query list or Ctrl+R runs.\n"
                "/ focuses the live substring filter (name, SQL, description, tags).\n"
                "The tag field matches one exact tag; clear it to show all tags.\n"
                "Ctrl+S focuses SQL; Tab moves between fields and results.\n"
                "Set DB and/or File paths and engine before running.\n"
                "Or use Target for a URI / @connection (leave DB/File blank).\n"
                "Remote targets use the existing read-only DuckDB scanners.\n"
                "Blank targets use an in-memory database. SQL runs as written,\n"
                "including writes: choose your target carefully.\n"
                "Missing parameters are prompted in-app; Escape cancels.\n"
                "Successful runs update history and the cached result.\n"
                "Scroll results with arrows / PageUp / PageDown.\n"
                "Q quits; Escape closes this help."
            )
            yield Button("Close", id="close-help")

    @on(Button.Pressed)
    def action_close(self) -> None:
        self.dismiss(None)


class QuackpackApp(App[None]):
    TITLE = "quackpack"
    BINDINGS = [
        ("/", "filter", "Filter"),
        ("ctrl+r", "run_query", "Run"),
        ("ctrl+s", "show_sql", "SQL"),
        ("?", "help", "Help"),
        ("q", "quit", "Quit"),
    ]
    CSS = """
    #body { height: 1fr; }
    #library { width: 40%; min-width: 24; }
    #preview { width: 1fr; }
    #queries { height: 1fr; }
    #sql-pane { height: 1fr; border: solid $primary; }
    #results { height: 1fr; }
    #targets { height: 3; }
    #targets Input { width: 1fr; }
    #targets Select { width: 18; }
    #status { height: auto; max-height: 4; }
    #result-title { height: auto; }
    ModalScreen { align: center middle; background: $background 70%; }
    #dialog { width: 70; max-width: 95%; height: auto; max-height: 85%;
              padding: 1 2; background: $surface; border: thick $primary; }
    #dialog Button { margin-top: 1; }
    """

    def __init__(
        self, catalog: Catalog, *, db: str = "", file: str = "",
        engine: str = "auto", tag: str = "", target: str = "",
    ) -> None:
        super().__init__()
        self.catalog = catalog
        self.initial_db = db
        self.initial_file = file
        self.initial_engine = engine
        self.initial_tag = tag
        self.initial_target = target
        self.selected_name: str | None = None
        self._query_running = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="library"):
                yield Input(placeholder="Search queries (/)", id="filter")
                yield Input(self.initial_tag, placeholder="Exact tag (blank = all)", id="tag")
                yield DataTable(id="queries", cursor_type="row")
            with Vertical(id="preview"):
                with VerticalScroll(id="sql-pane", can_focus=True):
                    yield Static(id="metadata", markup=False)
                    yield Static(id="sql")
                yield Label("No cached result", id="result-title")
                yield DataTable(id="results")
        yield Input(
            self.initial_target, password=True, id="target",
            placeholder="Target URI / @connection (masked; leave DB/File blank)",
        )
        with Horizontal(id="targets"):
            yield Input(self.initial_db, placeholder="DB path (blank = memory)", id="db")
            yield Input(self.initial_file, placeholder="File path (CSV / Parquet / SQLite)", id="file")
            yield Select(
                [(name, name) for name in ("auto", "duckdb", "sqlite")],
                value=self.initial_engine, allow_blank=False, id="engine",
            )
            yield Button("Run", id="run", variant="primary")
        yield Static("Select a query. Blank targets = in-memory.", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#queries", DataTable).add_columns("Name", "Tags", "Last run")
        self.refresh_queries()
        self.query_one("#queries", DataTable).focus()

    @on(Input.Changed, "#filter")
    @on(Input.Changed, "#tag")
    def filter_changed(self) -> None:
        self.refresh_queries()

    def refresh_queries(self) -> None:
        table = self.query_one("#queries", DataTable)
        if not table.columns:
            return
        items = build_query_list(
            self.catalog,
            self.query_one("#filter", Input).value,
            self.query_one("#tag", Input).value,
        )
        names = [item.name for item in items]
        selected = self.selected_name
        table.clear()
        for item in items:
            table.add_row(Text(item.name), Text(", ".join(item.tags)), item.last_run, key=item.name)
        if items:
            index = names.index(selected) if selected in names else 0
            table.move_cursor(row=index)
            self.show_query(names[index])
        else:
            self.selected_name = None
            self.query_one("#metadata", Static).update("No matching queries.")
            self.query_one("#sql", Static).update("")
            self.show_result(None, "No cached result")

    @on(DataTable.RowHighlighted, "#queries")
    def highlight_query(self, event: DataTable.RowHighlighted) -> None:
        name = event.row_key.value
        # Ignore queued highlight events from rows removed by live filtering.
        if name is not None and name in self.query_one("#queries", DataTable).rows:
            if name != self.selected_name:
                self.show_query(name)

    def show_query(self, name: str) -> None:
        self.selected_name = name
        query = self.catalog.get(name)
        try:
            sql = expand_query(self.catalog, name)
        except TemplateError as exc:
            sql = query.sql
            self.query_one("#status", Static).update(f"Template error: {exc}")
        params = ", ".join(f":{param}" for param in extract_params(sql)) or "none"
        self.query_one("#metadata", Static).update(
            f"{name}\n{query.desc}\nTags: {', '.join(query.tags) or 'none'}\nParams: {params}"
        )
        self.query_one("#sql", Static).update(Syntax(sql, "sql", word_wrap=True))
        try:
            snapshot = load_snapshot(name)
        except SnapshotError as exc:
            self.show_result(None, f"Cache error: {exc}")
        else:
            self.show_result(
                snapshot.as_result() if snapshot else None,
                f"Cached {snapshot.taken} ({snapshot.rowcount} rows)" if snapshot else "No cached result",
            )

    def show_result(self, result: QueryResult | None, title: str) -> None:
        self.query_one("#result-title", Label).update(Text(title))
        table = self.query_one("#results", DataTable)
        table.clear(columns=True)
        if result is not None and result.columns:
            table.add_columns(*(Text(column) for column in result.columns))
            table.add_rows(
                tuple(Text("NULL" if value is None else str(value)) for value in row)
                for row in result.rows
            )

    @on(DataTable.RowSelected, "#queries")
    @on(Button.Pressed, "#run")
    def run_selected(self) -> None:
        self.action_run_query()

    @work()
    async def action_run_query(self) -> None:
        if self._query_running or self.selected_name is None:
            return
        self._query_running = True
        run_button = self.query_one("#run", Button)
        run_button.disabled = True
        name = self.selected_name
        status = self.query_one("#status", Static)
        try:
            sql = expand_query(self.catalog, name)
            fields = {
                "db": self.query_one("#db", Input).value,
                "file": self.query_one("#file", Input).value,
                "target": self.query_one("#target", Input).value,
                "engine": str(self.query_one("#engine", Select).value),
            }
            request = resolve_run(sql, **fields)
            if request.missing:
                values = await self.push_screen_wait(ParamPrompt(request.missing))
                if values is None:
                    status.update("Run cancelled.")
                    return
                request = resolve_run(sql, values=values, **fields)
            status.update(f"Running {name}...")
            try:
                result = await asyncio.to_thread(
                    run_query, sql, db=request.db, file=request.file,
                    engine=request.engine, params=request.params, target=request.target,
                )
            except EngineError:
                self.record_history(name, ERROR)
                self.refresh_queries()
                raise
            self.record_history(name, OK)
            warnings = []
            try:
                save_snapshot(Snapshot.from_result(
                    name, result, params=request.params, engine=request.engine,
                ))
            except OSError as exc:
                warnings.append(f"Could not save snapshot: {exc}")
            self.refresh_queries()
            if self.selected_name == name:
                self.show_result(result, f"{name}: {result.rowcount} rows")
            status.update(f"{name}: {result.rowcount} rows." + (" " + " ".join(warnings) if warnings else ""))
        except (EngineError, TemplateError, TargetError, ValueError) as exc:
            status.update(f"Error: {exc}")
            self.notify(str(exc), title="Run failed", severity="error")
        finally:
            self._query_running = False
            run_button.disabled = False

    def record_history(self, name: str, outcome: str) -> None:
        try:
            self.catalog.record_run(name, outcome)
        except (CatalogError, OSError) as exc:
            self.notify(f"Could not save run history: {exc}", severity="warning")

    def action_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_show_sql(self) -> None:
        self.query_one("#sql-pane", VerticalScroll).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
