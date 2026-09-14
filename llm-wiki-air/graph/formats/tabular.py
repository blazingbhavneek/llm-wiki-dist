"""Tables: grid, model-selected structure, records, pages, and safe queries."""

from __future__ import annotations

import csv as _csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

Cell = tuple[int, int]


@dataclass
class Grid:
    cells: dict[Cell, str]
    col_letters: dict[int, str] = field(default_factory=dict)

    @property
    def rows(self) -> list[int]:
        return sorted({r for r, _ in self.cells})

    @property
    def cols(self) -> list[int]:
        return sorted({c for _, c in self.cells})

    def get(self, row: int, col: int) -> str:
        return self.cells.get((row, col), "")

    def letter(self, col: int) -> str:
        return self.col_letters.get(col) or _letter(col)


def _letter(col: int) -> str:
    result = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        result = chr(65 + rem) + result
    return result


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[tuple[str, int, int]]] = []
        self._row: list[tuple[str, int, int]] | None = None
        self._cell: list[str] | None = None
        self._span = (1, 1)
        self._in_th = False
        self.headers: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._span = (int(attrs.get("rowspan", 1) or 1), int(attrs.get("colspan", 1) or 1))
            self._in_th = tag == "th"

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = "".join(self._cell).strip()
            if self._in_th:
                self.headers.append(text)
            else:
                self._row.append((text, *self._span))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def grid_from_html(table_html: str) -> Grid:
    parser = _TableParser()
    parser.feed(table_html)
    letters = {i: name for i, name in enumerate(parser.headers[1:], 1)}
    cells: dict[Cell, str] = {}
    covered: set[Cell] = set()
    for raw in parser.rows:
        row_number = int(re.sub(r"\D", "", raw[0][0]) or 0)
        col = 1
        for text, rowspan, colspan in raw[1:]:
            while (row_number, col) in covered:
                col += 1
            if text:
                cells[(row_number, col)] = text
            for dr in range(rowspan):
                for dc in range(colspan):
                    if dr or dc:
                        covered.add((row_number + dr, col + dc))
            col += colspan
    return Grid(cells=cells, col_letters=letters)


def grid_from_gfm(lines: Sequence[str]) -> Grid:
    cells: dict[Cell, str] = {}
    row = 0
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", p) for p in parts):
            continue
        row += 1
        for col, text in enumerate(parts, 1):
            if text:
                cells[(row, col)] = text.replace("\\|", "|")
    return Grid(cells=cells)


@dataclass
class Region:
    id: int
    r1: int
    c1: int
    r2: int
    c2: int
    cell_count: int

    def preview(self, grid: Grid, *, rows: int, cols: int) -> str:
        picked = list(range(self.r1, min(self.r2, self.r1 + rows - 1) + 1))
        if self.r2 - self.r1 + 1 > rows:
            picked += [self.r2 - 1, self.r2]
        return "\n".join(
            f"行{r}: " + " | ".join(
                f"{grid.letter(c)}:{grid.get(r, c)[:24]}"
                for c in range(self.c1, min(self.c2, self.c1 + cols - 1) + 1)
                if grid.get(r, c)
            )
            for r in picked
        )


def find_regions(grid: Grid) -> list[Region]:
    rows, cols = grid.rows, grid.cols
    if not rows:
        return []
    occupied = set(rows)
    bands: list[tuple[int, int]] = []
    start: int | None = rows[0]
    for row in range(rows[0], rows[-1] + 1):
        if row not in occupied:
            if start is not None:
                bands.append((start, row - 1))
            start = None
        elif start is None:
            start = row
    if start is not None:
        bands.append((start, rows[-1]))
    regions: list[Region] = []
    for r1, r2 in bands:
        used = sorted({c for r, c in grid.cells if r1 <= r <= r2})
        c_start = used[0]
        for i, col in enumerate(used):
            next_col = used[i + 1] if i + 1 < len(used) else None
            if next_col is None or next_col > col + 1:
                count = sum(1 for r, c in grid.cells if r1 <= r <= r2 and c_start <= c <= col)
                regions.append(Region(len(regions) + 1, r1, c_start, r2, col, count))
                if next_col is not None:
                    c_start = next_col
    big = [r for r in regions if r.cell_count > 3]
    for small in [r for r in regions if r.cell_count <= 3]:
        host = next((b for b in big if b.r1 - 2 <= small.r2 < b.r1 or (b.r1 <= small.r1 <= b.r2 and abs(b.c1 - small.c2) <= 2)), None)
        if host:
            host.r1, host.c1 = min(host.r1, small.r1), min(host.c1, small.c1)
            host.r2, host.c2 = max(host.r2, small.r2), max(host.c2, small.c2)
            host.cell_count += small.cell_count
    result = [r for r in regions if r.cell_count > 3] or regions
    for i, region in enumerate(result, 1):
        region.id = i
    return result


class TableSpec(BaseModel):
    region: int = 0
    title: str = ""
    orientation: Literal["rows", "columns"] = "rows"
    header_rows: list[int] = Field(default_factory=list)
    label_cols: list[int] = Field(default_factory=list)
    data_rows: list[int] = Field(default_factory=list)
    data_cols: list[int] = Field(default_factory=list)
    notes: str = ""


class SheetStructure(BaseModel):
    summary: str = ""
    tables: list[TableSpec] = Field(default_factory=list)
    ignore: list[int] = Field(default_factory=list)


def validate_structure(structure: SheetStructure, regions: Sequence[Region]) -> str | None:
    by_id = {r.id: r for r in regions}
    if not structure.tables:
        return "no tables identified; every sheet with data has at least one"
    for spec in structure.tables:
        region = by_id.get(spec.region)
        if region is None:
            return f"table '{spec.title}' names unknown region {spec.region}"
        if len(spec.data_rows) != 2 or len(spec.data_cols) != 2:
            return f"table '{spec.title}': data_rows and data_cols must be [first, last]"
        r1, r2 = spec.data_rows
        c1, c2 = spec.data_cols
        if not (region.r1 <= r1 <= r2 <= region.r2 and region.c1 <= c1 <= c2 <= region.c2):
            return f"table '{spec.title}': data range leaves region {region.id}"
        if spec.orientation == "rows" and any(not (region.r1 <= h < r1) for h in spec.header_rows):
            return f"table '{spec.title}': header_rows must lie above the data rows"
        if spec.orientation == "columns" and any(not (region.c1 <= h < c1) for h in spec.label_cols):
            return f"table '{spec.title}': label_cols must lie left of the data columns"
    return None


def _numeric(text: str) -> float | None:
    raw = text.strip()
    if not re.fullmatch(r"^[-+]?[¥$€]?\s*\d[\d,]*(\.\d+)?\s*%?$", raw):
        return None
    try:
        return float(raw.replace(",", "").replace("¥", "").replace("$", "").replace("€", "").rstrip("%").strip())
    except ValueError:
        return None


def heuristic_structure(regions: Sequence[Region], grid: Grid, *, header_row: bool = True) -> SheetStructure:
    return SheetStructure(
        summary="（自動判定）",
        tables=[
            TableSpec(
                region=region.id,
                title=f"領域 {region.id}",
                header_rows=[region.r1] if header_row else [],
                label_cols=[region.c1],
                data_rows=[min(region.r1 + 1, region.r2) if header_row else region.r1, region.r2],
                data_cols=[region.c1, region.c2],
            )
            for region in regions
        ],
    )


def structure_prompt(sheet: str, regions: Sequence[Region], grid: Grid, *, rows: int, cols: int, error: str | None, language: str) -> str:
    blocks = "\n\n".join(f"### 領域 {r.id}（行{r.r1}-{r.r2}, 列{grid.letter(r.c1)}-{grid.letter(r.c2)}, {r.cell_count}セル）\n{r.preview(grid, rows=rows, cols=cols)}" for r in regions)
    fix = f"\n\n前回の回答は却下されました: {error}\n修正して返してください。" if error else ""
    return (
        f"シート「{sheet}」の非空セルの塊です。各領域について、表ならtitle、orientation、header_rows、label_cols、"
        f"data_rows=[最初,最後]、data_cols=[最初,最後]を決め、注記・凡例はignoreにしてください。\n\n{blocks}{fix}\n\n"
        f"出力言語: {language}。JSON のみ。"
    )


async def decide_structure(sheet: str, grid: Grid, regions: Sequence[Region], *, model: Any, config: Any, attempts: int = 2) -> SheetStructure:
    from langchain_core.messages import HumanMessage

    error = None
    for _ in range(attempts):
        try:
            structure = await model.structured(SheetStructure, [HumanMessage(content=structure_prompt(sheet, regions, grid, rows=config.tabular_preview_rows, cols=config.tabular_preview_cols, error=error, language=config.output_language))])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            continue
        error = validate_structure(structure, regions)
        if error is None:
            return structure
    return heuristic_structure(regions, grid, header_row=getattr(config, "source_kind", "csv") != "xlsx")


@dataclass
class Record:
    key: str
    label: str
    values: dict[str, str]


def headers_for(grid: Grid, spec: TableSpec) -> dict[int, str]:
    names: dict[int, str] = {}
    last = ""
    if spec.orientation == "rows":
        c1, c2 = spec.data_cols
        for col in range(c1, c2 + 1):
            parts = [grid.get(row, col) for row in spec.header_rows if grid.get(row, col)]
            name = " / ".join(parts) or last or grid.letter(col)
            if parts:
                last = name
            names[col] = name
    else:
        r1, r2 = spec.data_rows
        for row in range(r1, r2 + 1):
            parts = [grid.get(row, col) for col in spec.label_cols if grid.get(row, col)]
            name = " / ".join(parts) or last or f"行{row}"
            if parts:
                last = name
            names[row] = name
    return names


def records_for(grid: Grid, spec: TableSpec) -> tuple[list[str], list[Record]]:
    names = headers_for(grid, spec)
    records: list[Record] = []
    if spec.orientation == "rows":
        for row in range(spec.data_rows[0], spec.data_rows[1] + 1):
            values = {names[col]: grid.get(row, col) for col in names if grid.get(row, col)}
            if values:
                label = " ".join(grid.get(row, col) for col in spec.label_cols if grid.get(row, col))
                records.append(Record(f"行 {row}", label, values))
    else:
        for col in range(spec.data_cols[0], spec.data_cols[1] + 1):
            values = {names[row]: grid.get(row, col) for row in names if grid.get(row, col)}
            if values:
                label = " ".join(grid.get(row, col) for row in spec.header_rows if grid.get(row, col))
                records.append(Record(f"列 {_letter(col)}", label, values))
    return list(dict.fromkeys(name for record in records for name in record.values)), records


def stats_for(columns: Sequence[str], records: Sequence[Record]) -> list[dict[str, Any]]:
    output = []
    for column in columns:
        raw = [record.values.get(column, "") for record in records if record.values.get(column, "")]
        nums = [value for value in (_numeric(item) for item in raw) if value is not None]
        entry: dict[str, Any] = {"column": column, "filled": len(raw)}
        if nums and len(nums) >= len(raw) * 0.6:
            entry.update(kind="numeric", min=min(nums), max=max(nums), mean=sum(nums) / len(nums), sum=sum(nums))
            entry["max_key"] = next(record.key for record in records if _numeric(record.values.get(column, "")) == max(nums))
            entry["min_key"] = next(record.key for record in records if _numeric(record.values.get(column, "")) == min(nums))
        else:
            distinct = list(dict.fromkeys(raw))
            entry.update(kind="text", distinct=len(distinct), top=distinct[:20])
        output.append(entry)
    return output


SPEC_MARK = "<!-- table-spec: {} -->"
SPEC_RE = re.compile(r"^<!-- table-spec: (\{.*\}) -->\s*$", re.MULTILINE)


def spec_comment(sheet: str, spec: TableSpec) -> str:
    return SPEC_MARK.format(json.dumps({"sheet": sheet, **spec.model_dump()}, ensure_ascii=False))


def specs_in_page(body: str) -> list[dict[str, Any]]:
    return [json.loads(match.group(1)) for match in SPEC_RE.finditer(body)]


def grids_in_page(body: str) -> list[Grid]:
    html = [grid_from_html(match.group(0)) for match in re.finditer(r"<table>.*?</table>", body, re.DOTALL)]
    if html:
        return html
    tables: list[list[str]] = []
    current: list[str] = []
    for line in body.splitlines() + [""]:
        if line.strip().startswith("|"):
            current.append(line)
        elif current:
            if len(current) >= 2:
                tables.append(current)
            current = []
    return [grid_from_gfm(table) for table in tables]


def records_from_page(body: str) -> list[tuple[dict[str, Any], list[str], list[Record]]]:
    grids = grids_in_page(body)
    if not grids:
        table = [line for line in body.splitlines() if line.strip().startswith("|")]
        grids = [grid_from_gfm(table)] if table else []
    output = []
    for index, spec_dict in enumerate(specs_in_page(body)):
        spec = TableSpec.model_validate({k: v for k, v in spec_dict.items() if k != "sheet"})
        # Rendered pages may contain a statistics table before the original
        # GFM source; the last table is the source table in that format.
        grid = grids[index] if "<table>" in body and index < len(grids) else (grids[-1] if grids else Grid(cells={}))
        columns, records = records_for(grid, spec)
        output.append((spec_dict, columns, records))
    return output


def query_records(columns: Sequence[str], records: Sequence[Record], sql: str, *, limit: int = 200) -> str:
    conn = sqlite3.connect(":memory:")
    quoted = ", ".join(f'"{name.replace(chr(34), chr(34) * 2)}"' for name in ["key", "label", *columns])
    conn.execute(f"CREATE TABLE t ({quoted})")
    rows = []
    for record in records:
        values = []
        for column in columns:
            value = record.values.get(column, "")
            values.append(_numeric(value) if _numeric(value) is not None else value)
        rows.append([record.key, record.label, *values])
    conn.executemany(f"INSERT INTO t VALUES ({', '.join('?' * (len(columns) + 2))})", rows)

    def authorizer(action, arg1, *_args):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if arg1 == "t" else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)
    statement = sql.strip().rstrip(";")
    if ";" in statement:
        return "error: one statement only"
    if not re.search(r"\blimit\b", statement, re.IGNORECASE):
        statement += f" LIMIT {limit}"
    try:
        cursor = conn.execute(statement)
        output = io.StringIO()
        writer = _csv.writer(output)
        writer.writerow([description[0] for description in cursor.description or []])
        writer.writerows(cursor.fetchmany(limit))
        return output.getvalue()[:8000]
    except sqlite3.Error as exc:
        return f"error: {exc}"
    finally:
        conn.close()


def render_table_page(sheet: str, structure: SheetStructure, tables: list[tuple[TableSpec, list[str], list[Record], list[dict]]], table_html: str, grid: Grid) -> str:
    output = [f"# {sheet}", "", structure.summary, ""]
    for spec, columns, records, stats in tables:
        output += [f"## {spec.title}", "", spec_comment(sheet, spec), "", f"- レコード: {'行' if spec.orientation == 'rows' else '列'}（{len(records)} 件）", f"- データ範囲: 行 {spec.data_rows[0]}-{spec.data_rows[1]}, 列 {grid.letter(spec.data_cols[0])}-{grid.letter(spec.data_cols[1])}", "", "### 列の定義", ""]
        output += [f"- **{column}**" for column in columns] + ["", "### 統計", "", "| 列 | 種類 | 値 |", "|---|---|---|"]
        for stat in stats:
            summary = (f"min {stat['min']:g}（{stat['min_key']}） / max {stat['max']:g}（{stat['max_key']}） / mean {stat['mean']:.4g} / sum {stat['sum']:g}" if stat["kind"] == "numeric" else f"{stat['distinct']} 種類: {', '.join(map(str, stat['top'][:8]))}")
            output.append(f"| {stat['column']} | {stat['kind']} | {summary} |")
        output.append("")
    return "\n".join(output + ["## 元データ（原本）", "", table_html, ""])


def analysis_prompt(sheet: str, spec: TableSpec, columns: Sequence[str], records: Sequence[Record], stats: Sequence[dict], *, language: str, feedback: Sequence[str] = ()) -> str:
    table = ["| key | label | " + " | ".join(columns) + " |", "|" + "---|" * (len(columns) + 2)]
    table += [f"| {record.key} | {record.label} | " + " | ".join(record.values.get(column, "") for column in columns) + " |" for record in records]
    feedback_text = ("\n\n前回の指摘:\n" + "\n".join(f"- {item}" for item in feedback)) if feedback else ""
    return f"シート「{sheet}」の表「{spec.title}」です。統計:\n{json.dumps(stats, ensure_ascii=False)}\n\nレコード:\n" + "\n".join(table) + f"\n\nこの表から読み取れることをMarkdownで書いてください。数値や行に言及するときは必ず `行 12` / `列 D` 形式で引用してください。{feedback_text}\n出力言語: {language}。"


CITE_RE = re.compile(r"(行|列)\s*([A-Z]+|\d+)")


def check_citations(markdown: str, records: Sequence[Record]) -> list[str]:
    keys = {record.key.replace(" ", "") for record in records}
    bad = sorted({f"{kind}{number}" for kind, number in CITE_RE.findall(markdown) if f"{kind}{number}" not in keys})
    return [f"存在しないキーを引用しています: {', '.join(bad[:10])}"] if bad else []


async def write_tables(*, sheets: list[tuple[str, str, tuple[int, int]]], run_dir, model, config, on_progress=None, stop_check=None) -> list[dict[str, Any]]:
    from langchain_core.messages import HumanMessage
    from graph.wiki.storage import write_json_atomic, write_text_atomic

    docs = Path(run_dir) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    number = 0
    files: list[dict[str, Any]] = []
    for sheet, table_text, source_range in sheets:
        if stop_check and stop_check():
            raise RuntimeError("tabular cancelled")
        grid = grid_from_html(table_text) if table_text.lstrip().startswith("<table") else grid_from_gfm(table_text.splitlines())
        regions = find_regions(grid)
        structure = SheetStructure(summary="（大きすぎるため原本のみ）", tables=[]) if not regions or "_Sparse cell view" in table_text else await decide_structure(sheet, grid, regions, model=model, config=config)
        tables = []
        for spec in structure.tables:
            columns, records = records_for(grid, spec)
            if records:
                tables.append((spec, columns, records, stats_for(columns, records)))
        number += 1
        name = f"{number:03d}-{_slug(sheet)}.md"
        write_text_atomic(docs / name, render_table_page(sheet, structure, tables, table_text, grid))
        files.append({"filename": name, "title": sheet, "kind": "table", "source_ranges": [list(source_range)], "summary": structure.summary})
        for spec, columns, records, stats in tables:
            slices = [records] if config.tabular_slice_records <= 0 else [records[i:i + config.tabular_slice_records] for i in range(0, len(records), config.tabular_slice_records)]
            targets = [("分析", records if len(records) <= 60 else records[:30] + records[-10:])]
            if len(slices) > 1:
                targets += [(str(i + 1), chunk) for i, chunk in enumerate(slices)]
            for suffix, subset in targets:
                feedback: list[str] = []
                text = ""
                for _ in range(3):
                    text = await model.text([HumanMessage(content=analysis_prompt(sheet, spec, columns, subset, stats, language=config.output_language, feedback=feedback))])
                    feedback = check_citations(text, subset)
                    if not feedback:
                        break
                if feedback:
                    text = "> 引用チェックに失敗したため、統計のみを掲載します。\n\n" + json.dumps(stats, ensure_ascii=False, indent=1)
                number += 1
                filename = f"{number:03d}-{_slug(sheet)}-{_slug(spec.title)}-{suffix}.md"
                write_text_atomic(docs / filename, f"# {spec.title} — {suffix}\n\n元の表: [{sheet}]({name})（{spec.title}）\n\n{text}\n")
                files.append({"filename": filename, "title": f"{spec.title} — {suffix}", "kind": "analysis", "source_ranges": [list(source_range)], "summary": text.strip().splitlines()[0][:120] if text.strip() else ""})
        if on_progress:
            on_progress({"stage": "tabular", "sheet": sheet, "tables": len(tables)})
    planning = Path(run_dir) / "_planning"
    planning.mkdir(exist_ok=True)
    write_json_atomic(planning / "manifest.json", {"planning": {"ingest_mode": "wiki", "strategy": "tabular"}, "files": files})
    write_json_atomic(planning / "coverage.json", {"files": [{"title": item["title"], "filename": re.sub(r"^\d+-", "", item["filename"]), "summary": item["summary"], "header": "表", "source_start": item["source_ranges"][0][0], "source_end": item["source_ranges"][0][1]} for item in files]})
    write_json_atomic(planning / "metadata.json", {"files": [{"name": re.sub(r"^\d+-", "", item["filename"]), "header": "表"} for item in files]})
    return files


def _slug(text: str) -> str:
    from graph.wiki.ids import slugify

    return slugify(text, fallback="sheet")
