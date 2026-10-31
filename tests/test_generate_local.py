"""실제 서식(templates/)으로 문서를 만들어 보는 로컬 회귀 테스트 (M5 완료 기준).

내부 자료가 필요해서 `pytest -m local` 로만 돈다.

1. 서식 183개의 변형 234개 전부가 올바른 입력으로 만들어진다. 고른 변형의 표만 남고 다른
   변형의 제목이 남지 않으며, 수식은 모두 다시 계산된다.
2. 수직 슬라이스 서식 3종(examples/ysu_forms/m5/*.json)이 입력 → 검증 → 생성까지 통과하고,
   한컴오피스가 있으면 HWP·PDF 까지 만든다. 입력 예시도 내부 서식 이름을 담아 저장소에 없다.
3. 일부러 넣은 오류는 만들기 전에 그 자리에서 잡힌다.
"""

from __future__ import annotations

import json

import pytest

from docflow.config import get_settings
from docflow.generate import generate
from docflow.hwpx.document import HwpxDocument
from docflow.pipeline import make_document, parse_input
from docflow.schema.model import FormSchema
from docflow.validate import DATE_INVALID, REQUIRED_EMPTY, FormInput
from tests.test_validate_local import valid_input
from tools.extract_schema import extract_file, is_form
from tools.make_document import find_template

pytestmark = pytest.mark.local

#: 2026-10 측정: 서식 183개, 변형으로 나누면 234건 (머리 번호로 나눈 4-01~05호 묶음 포함)
EXPECTED_CASES = 234

#: 수직 슬라이스: 입력 파일 -> (서식, 합계 칸 좌표, 기대 합계)
SLICE = {
    "교통비_지급_내역서": ("AID 서식 5-05호", (0, 17, 11), "17,000"),
    "정산서": ("AID 서식 3-01호", (0, 16, 7), "2,300,000"),
    "일반기안문_1-03호": ("AID 서식 1-01~10호", None, None),
}


@pytest.fixture(scope="module")
def forms() -> list[tuple[object, FormSchema]]:
    settings = get_settings()
    files = sorted(f for f in settings.templates.rglob("*.hwpx") if is_form(f))
    if not files:
        pytest.skip("templates/ 가 없다")
    return [(f, extract_file(f, settings)) for f in files]


def _for_variant(schema: FormSchema, base: FormInput, variant: int | None) -> FormInput:
    """valid_input 에서 고른 변형의 칸·반복영역만 남긴다."""

    def mine(key: str) -> bool:
        owner = schema.variant_of(int(key[1:].split(".")[0]))
        return variant is None or owner is None or owner == variant

    return FormInput(
        cells={k: v for k, v in base.cells.items() if mine(k)},
        fields=base.fields,
        rows={k: v for k, v in base.rows.items() if mine(k)},
        variant=variant,
    )


def test_모든_서식의_모든_변형을_만든다(forms, tmp_path):
    errors: list[str] = []
    cases = 0
    for path, schema in forms:
        base = valid_input(schema)
        for variant in [v.index for v in schema.variants] or [None]:
            cases += 1
            name = f"{path.name} 변형 {variant}"
            try:
                result = generate(
                    schema, _for_variant(schema, base, variant), path, tmp_path / "out.hwpx"
                )
            except Exception as exc:
                errors.append(f"{name}: {exc!r}")
                continue
            errors += [f"{name}: 수식 {f.formula} {why}" for f, why in result.formulas.skipped]
            if variant is None:
                continue
            doc = HwpxDocument.open(result.path)
            own = schema.variants[variant]
            shared = sum(schema.variant_of(t.index) is None for t in schema.tables)
            if len(doc.tables()) != own.last_table - own.first_table + 1 + shared:
                errors.append(f"{name}: 표 {len(doc.tables())}개")
            text = " ".join(b.text for b in doc.blocks())
            others = [v.label for v in schema.variants if v is not own and v.label not in own.label]
            errors += [f"{name}: 다른 변형 {label} 이 남았다" for label in others if label in text]
    assert cases == EXPECTED_CASES
    assert not errors, f"{len(errors)}건\n" + "\n".join(errors[:20])


# ------------------------------------------------------------ 수직 슬라이스
def _slice_input(name: str) -> dict:
    path = get_settings().root / "examples" / "ysu_forms" / "m5" / f"{name}.json"
    if not path.exists():
        pytest.skip(f"입력 예시가 없다: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _converter():
    from docflow.adapters.hancom import HancomConverter, available

    return HancomConverter() if available() else None


@pytest.mark.parametrize("name", SLICE)
def test_수직_슬라이스_서식을_끝까지_만든다(name, tmp_path):
    query, total_at, total = SLICE[name]
    settings = get_settings()
    template = find_template(query, settings)
    schema = extract_file(template, settings)
    parsed = parse_input(schema, _slice_input(name))

    converter = _converter()
    result = make_document(schema, parsed, template, tmp_path, name, converter=converter)
    assert result.issues == []
    assert result.ok, result.conversion and result.conversion.errors
    gen = result.generated
    assert gen.formulas.skipped == []
    if total_at is not None:
        assert HwpxDocument.open(gen.path).get_cell(*total_at) == total
    if converter is not None:
        assert set(result.conversion.outputs) == {"HWP", "PDF"}


@pytest.mark.parametrize(
    ("name", "where", "value", "code"),
    [
        ("교통비_지급_내역서", "t0.r2.c5", "2026.02.30", DATE_INVALID),
        ("정산서", "t0.r1.c1", "", REQUIRED_EMPTY),
        # 문서번호는 결재 시스템이 매겨 비어도 된다. 필수인 제목 칸을 비운다
        ("일반기안문_1-03호", "t32.r4.c1", "", REQUIRED_EMPTY),
    ],
)
def test_일부러_넣은_오류는_만들기_전에_잡는다(name, where, value, code, tmp_path):
    query = SLICE[name][0]
    settings = get_settings()
    template = find_template(query, settings)
    schema = extract_file(template, settings)
    raw = _slice_input(name)
    section = "fields" if not where.startswith("t") else "cells"
    raw.setdefault(section, {})[where] = value
    result = make_document(schema, parse_input(schema, raw), template, tmp_path, name)
    assert [(i.code, i.where) for i in result.errors] == [(code, where)]
    assert result.generated is None
