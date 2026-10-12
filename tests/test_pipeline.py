"""문서 만들기 테스트 — 입력 JSON 해석, 입력 틀, 검증 → 생성 → 변환 흐름.

변환은 가짜 변환기로 본다. 실제 한컴 변환은 test_generate_local 이 확인한다.
"""

from __future__ import annotations

import pytest

from docflow.convert import SIGNATURES, ConversionJob, ConversionResult
from docflow.hwpx.document import HwpxDocument
from docflow.pipeline import InputError, make_document, parse_input, skeleton
from docflow.schema.extract import extract
from docflow.schema.model import FormSchema, TableSpec, Variant
from docflow.validate import REQUIRED_EMPTY
from tests import hwpx_factory as hf
from tests.test_schema_extract import 교통비


@pytest.fixture
def traffic(tmp_path):
    path = hf.build(tmp_path / "교통비.hwpx", hf.section(교통비()))
    return extract(HwpxDocument.open(path), stem="교통비"), path


class FakeConverter:
    """산출물 파일을 형식별 시작 바이트로 만든다."""

    def __init__(self) -> None:
        self.jobs: list[ConversionJob] = []

    def convert(self, job: ConversionJob) -> ConversionResult:
        self.jobs.append(job)
        result = ConversionResult(job=job, attempts=1)
        for fmt in job.formats:
            target = job.target(fmt)
            target.write_bytes(SIGNATURES[fmt] + b" fake")
            result.outputs[fmt] = target
        return result


# ------------------------------------------------------------ 입력 해석
def test_반복영역_열은_이름이나_번호로_받는다(traffic):
    schema, _ = traffic
    parsed = parse_input(
        schema, {"rows": {"t0.r3-5": [{"성명": "김철수", "은행명": "국민", "3": "7,000"}]}}
    )
    assert parsed.data.rows == {"t0.r3-5": [{1: "김철수", 2: "국민", 3: "7,000"}]}


def test_열_이름이_여러_열에_걸리면_거부한다(traffic):
    schema, _ = traffic
    with pytest.raises(InputError, match="여러 열"):
        parse_input(schema, {"rows": {"t0.r3-5": [{"입금계좌": "x"}]}})


def test_없는_열_반복영역_항목은_거부한다(traffic):
    schema, _ = traffic
    with pytest.raises(InputError, match="없는 열"):
        parse_input(schema, {"rows": {"t0.r3-5": [{"주소": "x"}]}})
    with pytest.raises(InputError, match="없는 반복영역"):
        parse_input(schema, {"rows": {"t9.r0-1": []}})
    with pytest.raises(InputError, match="모르는 항목"):
        parse_input(schema, {"cell": {}})


def test_밑줄_항목은_설명이라_무시한다(traffic):
    schema, _ = traffic
    parsed = parse_input(schema, {"_form": "교통비", "cells": {"_t0": "설명", "t0.r0.c1": 3}})
    assert parsed.data.cells == {"t0.r0.c1": "3"}  # 값은 글로 읽는다


def test_변형은_이름이나_번호로_고른다():
    schema = FormSchema(
        title="기안문",
        tables=[TableSpec(index=i, rows=1, cols=1) for i in range(2)],
        variants=[Variant(index=0, label="1-01호", first_table=0, last_table=0),
                  Variant(index=1, label="1-02호", first_table=1, last_table=1)],
    )  # fmt: skip
    assert parse_input(schema, {"variant": "1-02호"}).data.variant == 1
    assert parse_input(schema, {"variant": 1}).data.variant == 1
    assert parse_input(schema, {"variant": "0"}).data.variant == 0
    with pytest.raises(InputError, match="없는 변형"):
        parse_input(schema, {"variant": "1-03호"})


# ------------------------------------------------------------ 입력 틀
def test_입력_틀은_자리표시자를_값으로_넣지_않는다(traffic):
    schema, _ = traffic
    sk = skeleton(schema)
    assert sk["cells"] == {"t0.r0.c1": ""}
    assert "○○○○○과" in sk["_cells"]["t0.r0.c1"]
    assert sk["rows"]["t0.r3-5"] == [
        {"연번": "1", "성명": "", "입금계좌 은행명": "", "입금계좌 입금액(원)": "5,000"}
    ]


def test_입력_틀을_그대로_내면_필수_칸이_걸린다(traffic, tmp_path):
    schema, template = traffic
    parsed = parse_input(schema, skeleton(schema))
    result = make_document(schema, parsed, template, tmp_path, "문서")
    assert [(i.code, i.where) for i in result.errors] == [(REQUIRED_EMPTY, "t0.r0.c1")]
    assert result.generated is None
    assert not (tmp_path / "문서.hwpx").exists()


# ------------------------------------------------------------ 만들기
def test_검증을_통과하면_만들고_변환한다(traffic, tmp_path):
    schema, template = traffic
    parsed = parse_input(
        schema, {"cells": {"t0.r0.c1": "컴퓨터과"}, "rows": {"t0.r3-5": [{"성명": "김철수"}]}}
    )
    converter = FakeConverter()
    result = make_document(
        schema, parsed, template, tmp_path / "out", "교통비_1", converter=converter
    )
    assert result.ok
    assert result.generated.path == tmp_path / "out" / "교통비_1.hwpx"
    [job] = converter.jobs
    assert (job.source, job.formats, job.stem) == (
        result.generated.path,
        ("HWP", "PDF"),
        "교통비_1",
    )
    assert set(result.conversion.outputs) == {"HWP", "PDF"}


def test_변환기가_없으면_HWPX까지만_만든다(traffic, tmp_path):
    schema, template = traffic
    parsed = parse_input(schema, {"cells": {"t0.r0.c1": "과"}})
    result = make_document(schema, parsed, template, tmp_path, "문서")
    assert result.ok and result.conversion is None
    assert result.generated.path.exists()


def test_force면_검증_오류가_있어도_만든다(traffic, tmp_path):
    schema, template = traffic
    result = make_document(schema, parse_input(schema, {}), template, tmp_path, "문서", force=True)
    assert result.errors and result.generated is not None
