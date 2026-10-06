"""HWPX 문서 읽기/쓰기 (순수 파이썬).

HWPX는 zip + XML 이므로 한컴오피스 없이 다룰 수 있다.

누름틀(CLICK_HERE 등)은 아래 형태로 저장된다::

    <hp:ctrl><hp:fieldBegin id="123" name="docnumber" .../></hp:ctrl>
    <hp:t>○○○○처-000000</hp:t>
    <hp:ctrl><hp:fieldEnd beginIDRef="123" .../></hp:ctrl>

fieldBegin.id 와 fieldEnd.beginIDRef 로 짝을 짓고, 그 사이의 <hp:t> 가 값이다.

표 셀은 ``tbl > tr > tc > subList > p > run > t`` 로 저장되고, 좌표는 ``cellAddr`` 에
있다. **병합된 칸은 왼쪽 위 셀(앵커) 하나만 XML 에 존재**하고 가려진 칸은 아예 없다.
빈 셀은 ``<run />`` 처럼 ``<t>`` 가 없다. 쓰기는 앵커에만 할 수 있다.
"""

from __future__ import annotations

import copy
import re
import zipfile
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from xml.etree import ElementTree as ET

from docflow.hwpx.formula import FormulaError, evaluate, format_result

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
NS = {"hp": HP}
T = f"{{{HP}}}t"
FIELD_BEGIN = f"{{{HP}}}fieldBegin"
FIELD_END = f"{{{HP}}}fieldEnd"
TBL = f"{{{HP}}}tbl"
TR = f"{{{HP}}}tr"
TC = f"{{{HP}}}tc"
CELL_ADDR = f"{{{HP}}}cellAddr"
CELL_SPAN = f"{{{HP}}}cellSpan"
PARA = f"{{{HP}}}p"
RUN = f"{{{HP}}}run"
CTRL = f"{{{HP}}}ctrl"
SUBLIST = f"{{{HP}}}subList"
SZ = f"{{{HP}}}sz"
CELL_SZ = f"{{{HP}}}cellSz"
LINESEG_ARRAY = f"{{{HP}}}linesegarray"

_SECTION_RE = re.compile(r"^Contents/section\d+\.xml$")
#: XML 1.0 에 쓸 수 없는 문자. 들어가면 문서가 깨지므로 값을 쓰기 전에 걸러낸다.
_XML_ILLEGAL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _clean(text: str) -> str:
    """XML 에 넣을 수 없는 제어문자를 지운다 (탭·줄바꿈은 둔다)."""
    return _XML_ILLEGAL_RE.sub("", text)


class HwpxError(Exception):
    """HWPX 조작 오류의 기반."""


class CellNotFound(HwpxError):
    """표에 그 좌표의 칸이 없다."""


class CoveredCell(HwpxError):
    """병합으로 가려진 칸이다. 앵커(왼쪽 위 셀)에 써야 한다."""


class CapacityError(HwpxError):
    """쓰려는 행이 표가 가진 행보다 많다."""


class FieldInCell(HwpxError):
    """누름틀이 든 칸이다. 필드 구조가 깨지므로 fill() 로 써야 한다."""


class RowSpanConflict(HwpxError):
    """행을 복제할 수 없는 모양이다 (행 병합이 걸쳐 있거나 칸이 비는 행)."""


@dataclass
class Field:
    """문서 안의 누름틀 하나."""

    name: str
    type: str
    value: str
    section: str
    #: 같은 이름이 여러 번 나올 때의 등장 순번 (0-base)
    occurrence: int = 0


@dataclass
class Cell:
    """표의 셀 하나. 병합된 셀은 좌상단 좌표 + span 으로 표현된다."""

    row: int
    col: int
    row_span: int
    col_span: int
    text: str


@dataclass
class FormulaCell:
    """표 칸 안의 수식(FORMULA 누름틀) 하나."""

    table: int
    row: int
    col: int
    formula: str
    result_format: str
    #: 칸에 지금 표시된 결과
    text: str


@dataclass
class Recalc:
    """recalculate_formulas() 의 결과."""

    #: 값이 바뀐 수식 (text 는 새 결과)
    updated: list[FormulaCell]
    #: 계산하지 못해 그대로 둔 수식과 그 이유
    skipped: list[tuple[FormulaCell, str]]


@dataclass
class Table:
    """서식 안의 표 하나."""

    index: int
    section: str
    rows: int
    cols: int
    #: 표 안에 표가 들어있는 경우의 중첩 깊이 (0 = 최상위)
    depth: int
    cells: list[Cell]

    def grid(self) -> list[list[str]]:
        """병합을 펼친 rows x cols 텍스트 격자. 검토·디버깅용."""
        out = [["" for _ in range(self.cols)] for _ in range(self.rows)]
        for c in self.cells:
            for r in range(c.row, min(c.row + c.row_span, self.rows)):
                for k in range(c.col, min(c.col + c.col_span, self.cols)):
                    out[r][k] = c.text if (r, k) == (c.row, c.col) else ""
        return out


@dataclass
class HwpxDocument:
    path: Path
    _entries: dict[str, bytes] = dc_field(default_factory=dict, repr=False)
    _order: list[str] = dc_field(default_factory=list, repr=False)
    _compress: dict[str, int] = dc_field(default_factory=dict, repr=False)
    #: 구역(section) XML 을 한 번만 파싱해 두고, 쓰기는 이 트리에 한다.
    _trees: dict[str, ET.Element] = dc_field(default_factory=dict, repr=False)
    _dirty: set[str] = dc_field(default_factory=set, repr=False)

    # ---------------------------------------------------------------- 로딩
    @classmethod
    def open(cls, path: str | Path) -> HwpxDocument:
        path = Path(path)
        doc = cls(path=path)
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                doc._order.append(info.filename)
                doc._compress[info.filename] = info.compress_type
                doc._entries[info.filename] = zf.read(info.filename)
        return doc

    @property
    def section_names(self) -> list[str]:
        return [n for n in self._order if _SECTION_RE.match(n)]

    def _root(self, section: str) -> ET.Element:
        if section not in self._trees:
            self._trees[section] = ET.fromstring(self._entries[section].decode("utf-8"))
        return self._trees[section]

    # ---------------------------------------------------------------- 읽기
    def fields(self) -> list[Field]:
        """문서 전체의 누름틀을 등장 순서대로 반환한다."""
        found: list[Field] = []
        seen: dict[str, int] = {}
        for section in self.section_names:
            for name, ftype, texts in _walk_fields(self._root(section)):
                occurrence = seen.get(name, 0)
                seen[name] = occurrence + 1
                found.append(
                    Field(
                        name=name,
                        type=ftype,
                        value="".join(t.text or "" for t in texts),
                        section=section,
                        occurrence=occurrence,
                    )
                )
        return found

    def field_names(self) -> list[str]:
        """중복 제거한 필드 이름 목록 (등장 순서 유지)."""
        names: list[str] = []
        for f in self.fields():
            if f.name not in names:
                names.append(f.name)
        return names

    def _table_elements(self) -> list[tuple[str, ET.Element, int]]:
        """(구역, 표 엘리먼트, 깊이) 를 tables() 와 같은 순서로 돌려준다."""
        found: list[tuple[str, ET.Element, int]] = []
        for section in self.section_names:
            for el, depth in _walk_tables(self._root(section)):
                found.append((section, el, depth))
        return found

    def tables(self) -> list[Table]:
        """문서 전체의 표를 등장 순서대로 반환한다 (중첩 표 포함)."""
        return [
            Table(
                index=i,
                section=section,
                rows=int(el.get("rowCnt", 0)),
                cols=int(el.get("colCnt", 0)),
                depth=depth,
                cells=[_read_cell(tc) for tr in el.findall(TR) for tc in tr.findall(TC)],
            )
            for i, (section, el, depth) in enumerate(self._table_elements())
        ]

    # ---------------------------------------------------------------- 쓰기
    def fill(self, values: dict[str, str]) -> int:
        """이름이 일치하는 모든 누름틀에 값을 채운다. 채운 개수를 반환."""
        filled = 0
        for section in self.section_names:
            for name, _ftype, texts in _walk_fields(self._root(section)):
                if name not in values or not texts:
                    continue
                texts[0].text = _clean(str(values[name]))
                for extra in texts[1:]:
                    extra.text = ""
                filled += 1
                self._dirty.add(section)
        return filled

    def replace_text(self, replacements: dict[str, str], *, field: str | None = None) -> int:
        """텍스트 노드 안의 문구를 제자리에서 바꾼다. 바꾼 횟수를 반환한다.

        본문 누름틀(content_body)은 여러 문단에 걸쳐 있어서 fill() 로 덮어쓰면 문단 구조가
        한 줄로 뭉개진다. 서식에 박힌 자리표시자("20○○년", "0,000,000원" 등)만 이 메서드로
        바꾸면 문단·문자 모양이 그대로 남는다. field 를 주면 그 이름의 누름틀 안만 바꾼다.

        한 노드 안에 통째로 들어 있는 문구만 찾는다. 문구가 여러 노드에 걸쳐 있으면 바꾸지 못한다.
        """
        done = 0
        for section in self.section_names:
            root = self._root(section)
            if field is None:
                nodes = [el for el in root.iter(T)]
            else:
                nodes = [
                    t
                    for begin, texts in _walk_field_elements(root)
                    if begin.get("name") == field
                    for t in texts
                ]
            for t in nodes:
                text = t.text or ""
                for old, new in replacements.items():
                    if old and old in text:
                        text = text.replace(old, _clean(new))
                        done += 1
                if text != (t.text or ""):
                    t.text = text
                    self._dirty.add(section)
        return done

    def get_cell(self, table: int, row: int, col: int) -> str:
        """앵커 셀의 텍스트."""
        _section, tc = self._anchor(table, row, col)
        return _cell_text(tc)

    def set_cell(self, table: int, row: int, col: int, text: str) -> None:
        """표 셀 하나의 내용을 text 로 바꾼다. 줄바꿈(\\n)은 문단으로 나뉜다.

        병합 셀은 왼쪽 위 칸(앵커)으로만 쓸 수 있다. 서식의 문자 모양·문단 모양은 유지한다.
        """
        section, tc = self._anchor(table, row, col)
        _write_cell(tc, str(text))
        self._dirty.add(section)

    def fill_rows(
        self,
        table: int,
        first_row: int,
        rows: list[list[str | None]],
        col: int = 0,
        *,
        last_row: int | None = None,
        extend: bool = False,
    ) -> int:
        """반복영역에 여러 행을 쓴다. rows[i][j] 는 (first_row+i, col+j) 칸의 값이다.

        last_row 는 반복영역의 마지막 행(포함)이다. 합계 행처럼 영역 밖의 행을 덮어쓰지
        않도록 호출하는 쪽이 알려준다. 없으면 표의 마지막 행까지로 본다.

        행이 모자라면 기본은 **아무것도 쓰지 않고** CapacityError 를 낸다.
        extend=True 면 last_row 행을 복제해 필요한 만큼 늘린다(그 아래 행은 밀려난다).
        어느 경우든 실패하면 문서는 호출 전 그대로다(늘린 행도, 이미 쓴 칸도 되돌린다).
        쓴 칸 수를 반환한다.

        None 은 건너뛴다(병합으로 가려진 칸, 미리 채워진 칸 등).
        """
        elements = self._table_elements()
        if not 0 <= table < len(elements):
            raise CellNotFound(f"표 {table} 이(가) 없다 (표 {len(elements)}개)")
        section = elements[table][0]
        total_rows = int(elements[table][1].get("rowCnt", 0))
        region_end = total_rows - 1 if last_row is None else last_row
        if not 0 <= first_row <= region_end < total_rows:
            raise CellNotFound(
                f"표 {table}: 반복영역 {first_row}~{region_end}행이 표 범위(0~{total_rows - 1})"
                " 밖이다"
            )
        need = len(rows) - (region_end - first_row + 1)
        if need > 0 and not extend:
            raise CapacityError(
                f"표 {table}: {first_row}행부터 {len(rows)}행을 쓰려면 "
                f"{first_row + len(rows) - 1}행까지 필요한데 영역은 {region_end}행까지다"
            )

        snapshot = copy.deepcopy(self._root(section))
        was_dirty = section in self._dirty
        try:
            for k in range(max(need, 0)):
                self.insert_row(table, region_end + k)
            targets = [
                (first_row + i, col + j, v)
                for i, line in enumerate(rows)
                for j, v in enumerate(line)
                if v is not None
            ]
            # 쓰기 전에 좌표를 전부 확인한다 (일부만 쓰이는 것을 막는다).
            for r, c, _v in targets:
                self._anchor(table, r, c)
            for r, c, v in targets:
                self.set_cell(table, r, c, v)
        except Exception:
            self._trees[section] = snapshot  # 늘린 행과 이미 쓴 칸을 모두 되돌린다
            if not was_dirty:
                self._dirty.discard(section)
            raise
        return len(targets)

    def insert_row(self, table: int, after: int, *, clear: bool = True) -> int:
        """after 행을 복제해 그 바로 아래에 새 행을 넣는다. 새 행의 번호를 반환한다.

        아래 행의 번호와 표의 행 수는 함께 밀린다. 복제할 행은 모든 칸이 행 병합 없이
        그 행에서 시작해야 한다(아니면 RowSpanConflict). clear=True 면 새 행의 글자를
        비운다. 예시 값이나 연번이 그대로 복제돼 조용히 틀리는 것을 막기 위해서다.
        """
        elements = self._table_elements()
        if not 0 <= table < len(elements):
            raise CellNotFound(f"표 {table} 이(가) 없다 (표 {len(elements)}개)")
        section, tbl, _depth = elements[table]
        row_cnt = int(tbl.get("rowCnt", 0))
        col_cnt = int(tbl.get("colCnt", 0))
        trs = tbl.findall(TR)
        if len(trs) != row_cnt:
            raise HwpxError(f"표 {table}: tr {len(trs)}개와 rowCnt {row_cnt} 이(가) 다르다")
        if not 0 <= after < row_cnt:
            raise CellNotFound(f"표 {table} 에 {after}행이 없다")

        source = trs[after]
        cells = [_read_cell(tc) for tc in source.findall(TC)]
        if any(c.row_span != 1 for c in cells) or sum(c.col_span for c in cells) != col_cnt:
            raise RowSpanConflict(
                f"표 {table} {after}행은 복제할 수 없다 (행 병합이 있거나 위 행의 병합이 걸쳐 있다)"
            )

        if any(el.tag == TBL for el in source.iter()):
            raise RowSpanConflict(f"표 {table} {after}행에는 중첩 표가 있어 복제할 수 없다")

        clone = copy.deepcopy(source)  # 여기서 실패해도 문서는 그대로다
        for tc in clone.findall(TC):
            tc.find(CELL_ADDR).set("rowAddr", str(after + 1))
            if clear:
                _write_cell(tc, "")

        tbl.insert(list(tbl).index(source) + 1, clone)
        for tr in trs[after + 1 :]:
            for tc in tr.findall(TC):
                addr = tc.find(CELL_ADDR)
                addr.set("rowAddr", str(int(addr.get("rowAddr", 0)) + 1))
        tbl.set("rowCnt", str(row_cnt + 1))

        size = tbl.find(SZ)
        heights = [
            int(sz.get("height", 0))
            for tc in clone.findall(TC)
            if (sz := tc.find(CELL_SZ)) is not None  # 자식 없는 엘리먼트는 거짓이다
        ]
        if size is not None and heights:
            size.set("height", str(int(size.get("height", 0)) + max(heights)))
        self._dirty.add(section)
        return after + 1

    def _anchor(self, table: int, row: int, col: int) -> tuple[str, ET.Element]:
        elements = self._table_elements()
        if not 0 <= table < len(elements):
            raise CellNotFound(f"표 {table} 이(가) 없다 (표 {len(elements)}개)")
        section, tbl, _depth = elements[table]
        for tr in tbl.findall(TR):
            for tc in tr.findall(TC):
                cell = _read_cell(tc)
                if (cell.row, cell.col) == (row, col):
                    return section, tc
                if (
                    cell.row <= row < cell.row + cell.row_span
                    and cell.col <= col < cell.col + cell.col_span
                ):
                    raise CoveredCell(
                        f"표 {table} ({row},{col}) 은 병합으로 가려진 칸이다. "
                        f"앵커는 ({cell.row},{cell.col})"
                    )
        raise CellNotFound(f"표 {table} 에 ({row},{col}) 칸이 없다")

    # ---------------------------------------------------------------- 수식
    def formula_cells(self) -> list[FormulaCell]:
        """표 칸 안의 수식을 표·행·열 순서로 돌려준다."""
        found: list[FormulaCell] = []
        for ti, (_section, tbl, _depth) in enumerate(self._table_elements()):
            for tr in tbl.findall(TR):
                for tc in tr.findall(TC):
                    cell = _read_cell(tc)
                    for begin, texts in _walk_field_elements(tc):
                        if begin.get("type") != "FORMULA":
                            continue
                        params = _params(begin)
                        found.append(
                            FormulaCell(
                                table=ti,
                                row=cell.row,
                                col=cell.col,
                                formula=params.get("Formula", ""),
                                result_format=params.get("ResultFormat", ""),
                                text="".join(t.text or "" for t in texts),
                            )
                        )
        return sorted(found, key=lambda f: (f.table, f.row, f.col))

    def set_formula_result(self, table: int, row: int, col: int, text: str) -> None:
        """수식 칸에 표시되는 결과와 저장된 마지막 결과(LastResult)를 text 로 바꾼다.

        한글은 열거나 변환할 때 수식을 다시 계산하지 않는다. 그래서 값을 채운 뒤에는
        합계 칸이 예전 값으로 남으므로 이 메서드(또는 recalculate_formulas)로 갱신해야 한다.
        """
        section, tc = self._anchor(table, row, col)
        formulas = [
            (begin, texts)
            for begin, texts in _walk_field_elements(tc)
            if begin.get("type") == "FORMULA"
        ]
        if not formulas:
            raise HwpxError(f"표 {table} ({row},{col}) 에 수식이 없다")
        if len(formulas) > 1:
            raise HwpxError(f"표 {table} ({row},{col}) 에 수식이 {len(formulas)}개 있다")
        begin, texts = formulas[0]
        if not texts:
            raise HwpxError(f"표 {table} ({row},{col}) 수식에 결과를 쓸 자리가 없다")
        text = _clean(text)
        texts[0].text = text
        for extra in texts[1:]:
            extra.text = ""
        for param in begin.iter():
            name = param.get("name")
            if name == "LastResult":
                param.text = text
            elif name == "Command" and ";;" in (param.text or ""):
                param.text = param.text.rsplit(";;", 1)[0] + ";;" + text
        for p in tc.iter(PARA):
            if any(el is begin for el in p.iter()):
                for seg in p.findall(LINESEG_ARRAY):
                    p.remove(seg)
        self._dirty.add(section)

    def recalculate_formulas(self, table: int | None = None) -> Recalc:
        """수식을 다시 계산해 결과를 갱신한다 (table 이 없으면 문서 전체).

        다른 수식의 결과를 참조하는 수식(곱셈 결과의 합계 등)이 있어서 결과가 더는 바뀌지
        않을 때까지 반복한다. 계산하지 못하는 수식은 추측하지 않고 그대로 두고 skipped 에 담는다.
        """
        tables = self.tables()
        grids = {t.index: t.grid() for t in tables}
        updated: dict[tuple[int, int, int], FormulaCell] = {}
        skipped: dict[tuple[int, int, int], tuple[FormulaCell, str]] = {}
        cells = [f for f in self.formula_cells() if table is None or f.table == table]
        for f in cells:  # 칸에 "90.0%" 처럼 글자가 더 붙어 있어도 수식 결과만 본다
            grids[f.table][f.row][f.col] = f.text
        for _ in range(len(cells) + 1):
            changed = False
            for f in cells:
                key = (f.table, f.row, f.col)
                grid = grids[f.table]
                try:
                    value = evaluate(f.formula, grid, f.row, f.col)
                    text = format_result(value, f.result_format)
                except FormulaError as e:
                    skipped[key] = (f, str(e))
                    continue
                skipped.pop(key, None)
                if grid[f.row][f.col] == text:
                    continue
                self.set_formula_result(f.table, f.row, f.col, text)
                grid[f.row][f.col] = text
                updated[key] = FormulaCell(**{**f.__dict__, "text": text})
                changed = True
            if not changed:
                break
        return Recalc(updated=list(updated.values()), skipped=list(skipped.values()))

    def save(self, path: str | Path) -> Path:
        """HWPX 로 저장한다. mimetype 은 규격대로 첫 항목·무압축을 유지한다."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for section in self._dirty:
            self._entries[section] = _serialize(self._trees[section])
        self._dirty.clear()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in self._order:
                compress = self._compress.get(name, zipfile.ZIP_DEFLATED)
                if name == "mimetype":
                    compress = zipfile.ZIP_STORED
                zf.writestr(zipfile.ZipInfo(name), self._entries[name], compress_type=compress)
        return path


def _walk_field_elements(root: ET.Element):
    """(fieldBegin 엘리먼트, [텍스트 노드]) 를 문서 순서대로 yield 한다.

    fieldBegin.id ↔ fieldEnd.beginIDRef 로 짝을 지으며, 중첩된 경우
    가장 안쪽 필드가 텍스트를 가져간다.
    """
    open_stack: list[tuple[ET.Element, list[ET.Element]]] = []
    for el in root.iter():
        if el.tag == FIELD_BEGIN:
            open_stack.append((el, []))
        elif el.tag == FIELD_END:
            ref = el.get("beginIDRef", "")
            for i in range(len(open_stack) - 1, -1, -1):
                if open_stack[i][0].get("id", "") == ref:
                    begin, texts = open_stack.pop(i)
                    yield begin, texts
                    break
        elif el.tag == T and open_stack:
            open_stack[-1][1].append(el)


def _walk_fields(root: ET.Element):
    """(name, type, [텍스트 노드]) 를 문서 순서대로 yield 한다."""
    for begin, texts in _walk_field_elements(root):
        yield begin.get("name", ""), begin.get("type", ""), texts


def _params(begin: ET.Element) -> dict[str, str]:
    """fieldBegin 아래 parameters 의 이름 -> 값."""
    return {p.get("name", ""): (p.text or "") for p in begin.iter() if p.tag.endswith("Param")}


def _walk_tables(parent: ET.Element, depth: int = 0):
    """(표 엘리먼트, 중첩 깊이) 를 문서 순서대로 yield 한다."""
    for el in parent:
        if el.tag == TBL:
            yield el, depth
            for tr in el.findall(TR):
                for tc in tr.findall(TC):
                    yield from _walk_tables(tc, depth + 1)
        else:
            yield from _walk_tables(el, depth)


def _read_cell(tc: ET.Element) -> Cell:
    addr = tc.find(CELL_ADDR)
    span = tc.find(CELL_SPAN)
    return Cell(
        row=int(addr.get("rowAddr", 0)) if addr is not None else 0,
        col=int(addr.get("colAddr", 0)) if addr is not None else 0,
        row_span=int(span.get("rowSpan", 1)) if span is not None else 1,
        col_span=int(span.get("colSpan", 1)) if span is not None else 1,
        text=_cell_text(tc),
    )


def _cell_text(tc: ET.Element) -> str:
    """셀의 텍스트. 중첩된 표의 내용은 제외한다 (그 표가 따로 추출되므로)."""
    lines: list[str] = []

    def walk(node: ET.Element, buf: list[str]) -> None:
        for el in node:
            if el.tag == TBL:
                continue
            if el.tag == PARA:
                inner: list[str] = []
                walk(el, inner)
                lines.append("".join(inner))
            elif el.tag == T:
                buf.append(el.text or "")
            else:
                walk(el, buf)

    walk(tc, [])
    return "\n".join(x for x in lines if x).strip()


# ------------------------------------------------------------------ 셀 쓰기
def _write_cell(tc: ET.Element, text: str) -> None:
    """셀 내용을 text 로 바꾼다. 첫 문단의 문단·문자 모양을 모든 줄에 이어 쓴다."""
    sub = tc.find(SUBLIST)
    if sub is None:
        raise HwpxError("subList 가 없는 셀이다")
    if any(el.tag == FIELD_BEGIN for el in sub.iter()):
        raise FieldInCell("누름틀이 든 칸이다. fill() 로 써야 한다")

    lines = _clean(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paras = sub.findall(PARA)
    if not paras:
        raise HwpxError("문단이 없는 셀이다")

    # 중첩 표가 든 문단은 지우지 않는다. 그 외 문단이 줄 수의 기준이 된다.
    keep = [p for p in paras if any(el.tag == TBL for el in p.iter())]
    plain = [p for p in paras if p not in keep]
    if not plain:
        plain = [_blank_paragraph(paras[0])]
        sub.append(plain[0])

    template = copy.deepcopy(plain[0])
    for extra in plain[len(lines) :]:
        sub.remove(extra)
    plain = plain[: len(lines)]
    while len(plain) < len(lines):
        clone = copy.deepcopy(template)
        # colPr 같은 제어 run 은 첫 문단에만 있어야 한다.
        for run in clone.findall(RUN):
            if run.find(CTRL) is not None:
                clone.remove(run)
        # 새 문단은 마지막 일반 문단 바로 뒤에 둔다.
        sub.insert(list(sub).index(plain[-1]) + 1, clone)
        plain.append(clone)

    for p, line in zip(plain, lines, strict=True):
        _set_paragraph_text(p, line)


def _blank_paragraph(model: ET.Element) -> ET.Element:
    """model 의 문단·문자 모양만 가져온 빈 문단. 표·제어문자·글자는 복사하지 않는다."""
    p = copy.deepcopy(model)
    first_char = (p.find(RUN).get("charPrIDRef") if p.find(RUN) is not None else None) or "0"
    for run in p.findall(RUN):
        p.remove(run)
    for seg in p.findall(LINESEG_ARRAY):
        p.remove(seg)
    p.append(ET.Element(RUN, {"charPrIDRef": first_char}))
    return p


def _set_paragraph_text(p: ET.Element, line: str) -> None:
    runs = p.findall(RUN)
    # 글자를 받을 run: 원래 텍스트가 있던 run > 제어문자(ctrl)가 없는 run > 새 run
    target = next((r for r in runs if r.find(T) is not None), None)
    if target is None:
        target = next((r for r in runs if r.find(CTRL) is None), None)
    if target is None:
        target = ET.Element(RUN, {"charPrIDRef": runs[0].get("charPrIDRef", "0")} if runs else {})
        anchor = p.find(LINESEG_ARRAY)
        if anchor is None:
            p.append(target)
        else:
            p.insert(list(p).index(anchor), target)
    for run in runs:
        for t in run.findall(T):
            run.remove(t)
    if line:
        ET.SubElement(target, T).text = line
    # 줄 배치 캐시는 글자가 바뀌면 틀리므로 지운다. 한글이 열 때 다시 계산한다.
    for seg in p.findall(LINESEG_ARRAY):
        p.remove(seg)


def _serialize(root: ET.Element) -> bytes:
    for prefix, uri in _ns_map(root).items():
        ET.register_namespace(prefix, uri)
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
        root, encoding="utf-8", xml_declaration=False
    )


def _ns_map(root: ET.Element) -> dict[str, str]:
    """루트 태그에서 쓰이는 네임스페이스 접두어를 되살린다."""
    return {
        "hp": HP,
        "hs": "http://www.hancom.co.kr/hwpml/2011/section",
        "hc": "http://www.hancom.co.kr/hwpml/2011/core",
        "hh": "http://www.hancom.co.kr/hwpml/2011/head",
        "hhs": "http://www.hancom.co.kr/hwpml/2011/history",
        "hm": "http://www.hancom.co.kr/hwpml/2011/master-page",
        "hpf": "http://www.hancom.co.kr/schema/2011/hpf",
        "hv": "http://www.hancom.co.kr/hwpml/2011/version",
        "ha": "http://www.hancom.co.kr/hwpml/2011/app",
        "opf": "http://www.idpf.org/2007/opf/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "ooxmlchart": "http://www.hancom.co.kr/hwpml/2016/ooxmlchart",
        "hwpunitchar": "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar",
        "epub": "http://www.idpf.org/2007/ops",
        "config": "http://www.hancom.co.kr/hwpml/2011/config",
    }
