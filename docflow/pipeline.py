"""문서 만들기 — 입력값 해석 → 검증 → 생성 → 형식 변환을 한 번에 (M5 수직 슬라이스).

명령줄(`tools/make_document.py`)과 이후 웹 서비스가 같은 함수를 부른다.
변환기는 주입받는다. 없으면 HWPX 까지만 만든다(한컴오피스가 없는 곳에서도 돈다).

입력값은 JSON 으로 받는다. 키는 스키마의 id 를 그대로 쓴다::

    {
      "variant": "1-03호",                      # 변형이 있는 서식만. 번호(2)도 된다
      "fields":  {"docnumber": "산학협력처-2026001", "content_body": "1. 관련\\n..."},
      "cells":   {"t0.r0.c2": "[2026 AID] ..."},
      "rows":    {"t0.r7-16": [{"성명": "김철수", "11": "7,000"}]},   # 열은 이름이나 번호
      "replace": {"20○○년 ○○월 ○○일": "2026년 10월 12일"}          # 입력칸이 아닌 본문 문구
    }

`_` 로 시작하는 키는 설명용이라 무시한다(`skeleton` 이 만든 입력 틀).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docflow.convert import ConversionJob, ConversionResult, Converter
from docflow.generate import GenerateResult, generate
from docflow.schema.model import FormSchema, RepeatColumn, RepeatSpec, SlotSpec
from docflow.schema.rules import is_placeholder
from docflow.validate import DEFAULT_CONFIG, FormInput, Issue, ValidationConfig, validate


class InputError(ValueError):
    """입력 JSON 을 서식에 맞게 읽을 수 없다."""


@dataclass
class ParsedInput:
    data: FormInput
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class MakeResult:
    issues: list[Issue] = field(default_factory=list)
    generated: GenerateResult | None = None
    conversion: ConversionResult | None = None

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        converted = self.conversion is None or self.conversion.ok
        return self.generated is not None and converted


# ---------------------------------------------------------------- 입력 해석
def _visible(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


def _variant(schema: FormSchema, value: Any) -> int | None:
    if value is None:
        return schema.variant
    for v in schema.variants:
        if str(value) in (str(v.index), v.label):
            if schema.variant not in (None, v.index):
                break  # 떼어 낸 서식은 다른 변형으로 바꿀 수 없다
            return v.index
    labels = ", ".join(f"{v.index}={v.label}" for v in schema.choices) or "변형 없음"
    raise InputError(f"없는 변형 {value!r} ({labels})")


def _column(rep: RepeatSpec, key: str) -> int:
    """반복영역 열 키(열 번호 또는 열 이름)를 열 번호로."""
    inputs = [c for c in rep.columns if c.role == "input"]
    if key.isdigit():
        col = int(key)
        if any(c.col == col for c in inputs):
            return col
        raise InputError(f"반복영역 {rep.id}: 입력 열이 아니다 {key!r}")
    exact = [c for c in inputs if c.label == key]
    partial = [c for c in inputs if key in c.label]
    found = exact or partial
    if len(found) != 1:
        names = [c.label for c in inputs]
        why = "없는" if not found else "여러 열에 해당하는"
        raise InputError(f"반복영역 {rep.id}: {why} 열 이름 {key!r} (열: {names})")
    return found[0].col


def parse_input(schema: FormSchema, raw: dict[str, Any]) -> ParsedInput:
    """입력 JSON 을 FormInput 으로 읽는다. 반복영역 열은 이름으로도 받는다."""
    raw = _visible(raw)
    unknown = sorted(set(raw) - {"variant", "fields", "cells", "rows", "replace"})
    if unknown:
        raise InputError(f"모르는 항목: {unknown}")
    repeats = {r.id: r for r in schema.repeats}
    rows: dict[str, list[dict[int, str]]] = {}
    for rid, lines in _visible(raw.get("rows") or {}).items():
        rep = repeats.get(rid)
        if rep is None:
            raise InputError(f"없는 반복영역 {rid!r} (있는 것: {sorted(repeats)})")
        rows[rid] = [
            {_column(rep, str(k)): str(v) for k, v in _visible(line).items()} for line in lines
        ]
    data = FormInput(
        cells={k: str(v) for k, v in _visible(raw.get("cells") or {}).items()},
        fields={k: str(v) for k, v in _visible(raw.get("fields") or {}).items()},
        rows=rows,
        variant=_variant(schema, raw.get("variant")),
    )
    replacements = {str(k): str(v) for k, v in _visible(raw.get("replace") or {}).items()}
    return ParsedInput(data, replacements)


def skeleton(schema: FormSchema, variant: int | None = None) -> dict[str, Any]:
    """입력 틀. 작성자가 채울 자리를 늘어놓는다 (`_` 항목은 설명).

    서식의 자리표시자(○○, 00)는 값으로 넣지 않고 설명에만 보여 준다. 틀을 그대로 내면
    필수 칸이 비어 있다고 검증에 걸려야 하기 때문이다. 주소처럼 실제 값이 박힌 칸은 값으로 둔다.
    """

    def default(text: str) -> str:
        return "" if is_placeholder(text) else text

    if schema.variant is not None:
        variant = schema.variant

    def mine(table: int) -> bool:
        owner = schema.variant_of(table)
        return variant is None or owner is None or owner == variant

    out: dict[str, Any] = {"_form": schema.title}
    if schema.choices:
        out["_variants"] = {str(v.index): v.label for v in schema.choices}
        out["variant"] = schema.variants[variant or 0].label
    out["fields"] = {f.name: default(f.placeholder) for f in schema.fields if f.role == "input"}
    slots = [s for s in schema.slots if s.role == "input" and mine(s.table)]
    out["_cells"] = {
        s.id: f"{s.label} [{s.input_type}{', 필수' if s.required else ''}]"
        + (f" 예: {' '.join(s.placeholder.split())}" if s.placeholder else "")
        for s in slots
    }
    def cell_default(s: SlotSpec) -> str:
        """제출 문장은 문장째 두고(날짜·이름만 고친다), 관리자가 바꾼 칸은 예시를 지운다."""
        if s.origin == "template":
            return s.placeholder
        return "" if s.origin == "example" else default(s.placeholder)

    out["cells"] = {s.id: cell_default(s) for s in slots}
    def column_key(rep: RepeatSpec, col: RepeatColumn) -> str:
        """열 이름이 겹치면(`사업추진일정 0월` 여러 개) 이름으로 못 찾으므로 열 번호를 쓴다."""
        try:
            return col.label if col.label and _column(rep, col.label) == col.col else str(col.col)
        except InputError:
            return str(col.col)

    def prefill(col: RepeatColumn) -> str:
        """미리 적힌 연번·기본 금액은 두고, `2000.00.00(금)` 같은 날짜 자리표시자는 뺀다."""
        value = col.prefilled or ""
        return default(value) if col.input_type == "date" else value

    out["rows"] = {
        r.id: [{column_key(r, c): prefill(c) for c in r.columns if c.role == "input"}]
        for r in schema.repeats
        if mine(r.table)
    }
    out["replace"] = {}
    return out


# ---------------------------------------------------------------- 만들기
def make_document(
    schema: FormSchema,
    parsed: ParsedInput,
    template: Path,
    out_dir: Path,
    stem: str,
    *,
    converter: Converter | None = None,
    formats: tuple[str, ...] = ("HWP", "PDF"),
    force: bool = False,
    config: ValidationConfig = DEFAULT_CONFIG,
) -> MakeResult:
    """검증 → HWPX 생성 → 변환. 검증 오류가 있으면 force 가 아닌 한 만들지 않는다."""
    result = MakeResult(issues=validate(schema, parsed.data, config))
    if result.errors and not force:
        return result
    hwpx = Path(out_dir) / f"{stem}.hwpx"
    result.generated = generate(
        schema, parsed.data, template, hwpx, replacements=parsed.replacements
    )
    if converter is not None and formats:
        job = ConversionJob(source=hwpx, formats=tuple(formats), out_dir=Path(out_dir), stem=stem)
        result.conversion = converter.convert(job)
    return result
