"""서식 판정 규칙(순수 함수) 테스트. 예시 문구는 실제 서식에서 나온 것이다."""

from __future__ import annotations

from pathlib import PurePath

import pytest

from docflow.schema.rules import (
    FormLocation,
    field_input_type,
    form_numbers,
    infer_input_type,
    is_approval_field,
    is_approval_placeholder,
    is_approval_title,
    is_date_placeholder,
    is_placeholder,
    is_total_label,
    normalize_category,
    parse_form_name,
    parse_location,
)


# ------------------------------------------------------------------ 자리표시자
@pytest.mark.parametrize(
    "text",
    ["○○○○○과", "재학생 00명", "20○○년", "0,000,000원", "2000000000", "□현장견학", "OOO",
     "홍길동", "홍 길 동"],
)
def test_자리표시자를_알아본다(text):
    assert is_placeholder(text)


@pytest.mark.parametrize("text", ["연번", "성명", "학과/부서명", "1", "국민", ""])
def test_항목명은_자리표시자가_아니다(text):
    assert not is_placeholder(text)


@pytest.mark.parametrize("text", ["2000.00.00", "2000.00.00(금)", "2024.5.2.(목)", "2000-00-00"])
def test_날짜_자리표시자(text):
    assert is_date_placeholder(text)


def test_문서번호가_붙은_칸은_날짜가_아니다():
    assert not is_date_placeholder("○○○○처-000000 2000.00.00")


@pytest.mark.parametrize("text", ["합계", "합 계", "소계", "총계"])
def test_합계_항목명(text):
    assert is_total_label(text)


# ------------------------------------------------------------------ 입력 유형
@pytest.mark.parametrize(
    ("text", "label", "expected"),
    [
        ("2000.00.00(금)", "", "date"),
        ("0,000,000원", "", "amount"),
        ("5,000", "", "amount"),
        ("3000원", "", "amount"),
        ("□현장견학 □현장체험", "", "checkbox"),
        ("재학생 00명", "", "text"),  # 앞에 글자가 붙으면 글자 칸이다
        ("00명", "", "number"),
        ("1", "", "number"),
        ("2000000000", "학번", "text"),  # 긴 숫자는 번호다
        ("○○○", "", "text"),
        ("○○○○○과", "", "text"),
        ("", "입금액(원)", "amount"),
        ("", "신청금액", "amount"),
        ("", "작성일", "date"),
        ("", "성명", "text"),
    ],
)
def test_입력_유형을_정한다(text, label, expected):
    assert infer_input_type(text, label) == expected


def test_텍스트가_있으면_항목명보다_우선한다():
    assert infer_input_type("국민", "입금액(원)") == "text"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("enforcedate", "date"),
        ("apb3_date", "date"),
        ("BILL_AMT", "amount"),
        ("CARD_CNT", "number"),
        ("docnumber", "text"),
    ],
)
def test_누름틀_이름으로_입력_유형을_정한다(name, expected):
    assert field_input_type(name) == expected


# ------------------------------------------------------------------ 결재란
@pytest.mark.parametrize(
    "text",
    ["총장", "처장", "혁신지원사업담당2", "PM 과장", "산학협력단계장", "담당", "결 재", "전결"],
)
def test_결재란_직위(text):
    assert is_approval_title(text)


@pytest.mark.parametrize("text", ["출장", "성명", "담당자", "장 소", "학과/부서명"])
def test_직위가_아닌_항목명(text):
    assert not is_approval_title(text)


@pytest.mark.parametrize("text", ["00/00", "전결 00/00"])
def test_결재_날짜_자리(text):
    assert is_approval_placeholder(text)


def test_결재선_누름틀():
    assert is_approval_field("apb1_sign")
    assert not is_approval_field("docnumber")


# ------------------------------------------------------------------ 파일 이름
@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("(서식 5-05호) [앵커(신산업)] 교통비 지급 내역서",
         ("5-05", "앵커(신산업)", "교통비 지급 내역서")),
        ("(서식 1-01~04호) [앵커(신산업)] 일반기안문(샘플)",
         ("1-01~04", "앵커(신산업)", "일반기안문(샘플)")),
        ("[AID 서식 1-01~10호] 일반기안문(샘플 10종)",
         ("1-01~10", "AID", "일반기안문(샘플 10종)")),
        ("[혁신 서식 1-03호+05호] 일반기안문(선대응 2종)",
         ("1-03+05", "혁신", "일반기안문(선대응 2종)")),
        ("성범죄 경력 조회 동의서(부서용)", ("", "", "성범죄 경력 조회 동의서(부서용)")),
    ],
)
def test_파일_이름에서_서식번호_사업_제목을_읽는다(stem, expected):
    assert parse_form_name(stem) == expected


@pytest.mark.parametrize(
    ("form_id", "expected"),
    [
        ("5-05", ["5-05"]),
        ("1-01~04", ["1-01", "1-02", "1-03", "1-04"]),
        ("1-11~16", ["1-11", "1-12", "1-13", "1-14", "1-15", "1-16"]),
        ("1-03+05", ["1-03", "1-05"]),
        ("1-03 05", ["1-03", "1-05"]),
        ("", []),
    ],
)
def test_서식번호를_개별_번호로_푼다(form_id, expected):
    assert form_numbers(form_id) == expected


# ------------------------------------------------------------------ 경로
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "ysu_forms/[2026 앵커(신산업)] 서식모음/5. [학생지원비] 관련 서식",
            FormLocation("앵커(신산업)", 5, "학생지원비", False),
        ),
        ("ysu_forms/2026_AID/2026_AID/1. 기안문", FormLocation("AID", 1, "기안문", False)),
        (
            "ysu_forms/2026_혁신/2026_혁신/2_계획서및보고서",
            FormLocation("혁신", 2, "계획서 및 보고서", False),
        ),
        (
            "ysu_forms/2026_혁신/2026_혁신/0_2025학년도 이전/1_기안문/선대응의 경우",
            FormLocation("혁신", 1, "기안문", True),
        ),
        (
            "ysu_forms/2026_AID/2026_AID/10. 성범죄 경력 조회 요청 매뉴얼(행정지원처)/기본서류",
            FormLocation("AID", 10, "성범죄 경력 조회 요청 매뉴얼(행정지원처)", False),
        ),
    ],
)
def test_경로에서_사업_카테고리를_읽는다(path, expected):
    assert parse_location(PurePath(path)) == expected


def test_연도_폴더는_카테고리가_아니다():
    assert parse_location(("2026_AID",)).category_no is None


def test_사업마다_다른_카테고리_이름을_맞춘다():
    assert normalize_category("계획서및보고서") == normalize_category("계획서 및 보고서")
