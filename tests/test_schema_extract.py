"""HWPX -> 스키마(v2) 추출 테스트.

표 모양은 실제 서식에서 확인한 것을 줄여 tests/hwpx_factory.py 로 만든다.
교통비 지급 내역서(두 줄 헤더 + 예시 행 + 합계), 물품 구입 요구서(예시로 다 채운 명단),
협약체결식 계획서(예시 두 줄), 회의계획서(왼쪽 묶음 이름), 결재란, 기안문·지출품의서(변형).
"""

from __future__ import annotations

import pytest

from docflow.hwpx.document import HwpxDocument
from docflow.schema.extract import extract
from docflow.schema.rules import FormLocation
from tests import hwpx_factory as hf

#: (행, 열, 글자[, 열병합, 행병합]) — 글자 대신 문단 XML 을 넣으려면 "<" 로 시작한다
Spec = tuple


def tbl(rows: int, cols: int, *cells: Spec) -> str:
    by_row: list[list[str]] = [[] for _ in range(rows)]
    for r, c, text, *span in cells:
        colspan, rowspan = (span + [1, 1])[:2]
        content = text if text.startswith("<") else (hf.para(text) if text else "")
        by_row[r].append(hf.cell(r, c, content, colspan=colspan, rowspan=rowspan))
    return hf.table(rows, cols, by_row)


def schema_of(tmp_path, *bodies: str, stem: str = "서식", **kw):
    path = hf.build(tmp_path / "f.hwpx", hf.section("".join(bodies)))
    return extract(HwpxDocument.open(path), stem=stem, **kw)


# ------------------------------------------------------------------ 교통비 지급 내역서
def 교통비() -> str:
    return tbl(
        7, 4,
        (0, 0, "학과/부서명"), (0, 1, "○○○○○과", 3, 1),
        (1, 0, "연번", 1, 2), (1, 1, "성명", 1, 2), (1, 2, "입금계좌", 2, 1),
        (2, 2, "은행명"), (2, 3, "입금액(원)"),
        (3, 0, "1"), (3, 1, "홍길동"), (3, 2, "국민"), (3, 3, "5,000"),
        (4, 0, "2"), (4, 1, ""), (4, 2, ""), (4, 3, "5,000"),
        (5, 0, "3"), (5, 1, ""), (5, 2, ""), (5, 3, "5,000"),
        (6, 0, "합계", 3, 1), (6, 3, hf.formula_para("=SUM(ABOVE)", "15,000")),
    )  # fmt: skip


@pytest.fixture
def 교통비_스키마(tmp_path):
    return schema_of(tmp_path, 교통비())


def test_반복영역은_예시_행부터_합계_행_앞까지다(교통비_스키마):
    [rep] = 교통비_스키마.repeats
    assert (rep.start_row, rep.end_row, rep.total_row) == (3, 5, 6)
    assert rep.has_example
    assert rep.capacity == 3


def test_두_줄_헤더를_합쳐_열_이름을_만든다(교통비_스키마):
    [rep] = 교통비_스키마.repeats
    assert rep.header_rows == [1, 2]
    labels = [c.label for c in rep.columns]
    assert labels == ["연번", "성명", "입금계좌 은행명", "입금계좌 입금액(원)"]


def test_모든_행에_적힌_값과_연번은_미리_채움이다(교통비_스키마):
    cols = {c.col: c for c in 교통비_스키마.repeats[0].columns}
    assert cols[0].prefilled == "1"
    assert cols[3].prefilled == "5,000"
    assert cols[3].input_type == "amount"
    assert cols[1].prefilled is None  # 예시 이름(홍길동)은 미리 채움이 아니다


def test_반복영역_밖의_자리표시자는_슬롯이다(교통비_스키마):
    [slot] = 교통비_스키마.slots
    assert (slot.id, slot.label, slot.origin, slot.required) == (
        "t0.r0.c1", "학과/부서명", "placeholder", True,
    )  # fmt: skip


def test_수식은_슬롯이_아니라_수식으로_뽑는다(교통비_스키마):
    assert [(f.id, f.formula) for f in 교통비_스키마.formulas] == [("t0.r6.c3", "=SUM(ABOVE)")]
    assert all(s.id != "t0.r6.c3" for s in 교통비_스키마.slots)


def test_표_격자를_담는다(교통비_스키마):
    [t] = 교통비_스키마.tables
    assert (t.rows, t.cols) == (7, 4)
    assert t.grid[3] == ["1", "홍길동", "국민", "5,000"]


# ------------------------------------------------------------------ 명단 모양
def test_예시로_다_채운_명단도_연번으로_찾는다(tmp_path):
    """물품 구입 요구서: 빈 행 없이 10행이 모두 예시 값이다."""
    s = schema_of(
        tmp_path,
        tbl(
            5, 3,
            (0, 0, "연번"), (0, 1, "품목명"), (0, 2, "신청금액"),
            (1, 0, "1"), (1, 1, "00장비"), (1, 2, "100,000"),
            (2, 0, "2"), (2, 1, "00기자재"), (2, 2, "20,000"),
            (3, 0, "3"), (3, 1, "00수리"), (3, 2, "400,000"),
            (4, 0, "합계(부가세 포함)", 2, 1), (4, 2, "520,000"),
        ),
    )  # fmt: skip
    [rep] = s.repeats
    assert (rep.start_row, rep.end_row, rep.total_row, rep.has_example) == (1, 3, 4, True)
    cols = {c.col: c for c in rep.columns}
    assert cols[0].prefilled == "1"
    assert cols[1].prefilled is None  # 행마다 다른 예시 품목
    assert cols[2].input_type == "amount"


def test_예시가_두_줄이고_왼쪽에_묶음_이름이_있는_명단(tmp_path):
    """협약체결식 계획서·회의계획서: 예시 두 줄 + 빈 행, 왼쪽에 `참석대상` 이 세로로 병합돼 있다."""
    s = schema_of(
        tmp_path,
        tbl(
            5, 3,
            (0, 0, "참석대상", 1, 5), (0, 1, "구분"), (0, 2, "성명"),
            (1, 1, "대학"), (1, 2, "홍길동"),
            (2, 1, "산업체"), (2, 2, "홍길서"),
            (3, 1, ""), (3, 2, ""),
            (4, 1, ""), (4, 2, ""),
        ),
    )  # fmt: skip
    [rep] = s.repeats
    assert (rep.start_row, rep.end_row, rep.header_rows, rep.has_example) == (1, 4, [0], True)
    assert [(c.col, c.label) for c in rep.columns] == [(1, "구분"), (2, "성명")]
    assert s.slots == []  # 명단 칸은 개별 슬롯으로 잡지 않는다


def test_띄어_쓴_예시_이름도_예시_행으로_본다(tmp_path):
    """성범죄 경력 조회 동의서: 예시 행이 `홍 길 동` 이라 옛 판정은 예시 행을 헤더로 잡았다."""
    s = schema_of(
        tmp_path,
        tbl(
            4, 3,
            (0, 0, "연 번"), (0, 1, "성명"), (0, 2, "동의자 서명"),
            (1, 0, "1"), (1, 1, "홍 길 동"), (1, 2, ""),
            (2, 0, ""), (2, 1, ""), (2, 2, ""),
            (3, 0, ""), (3, 1, ""), (3, 2, ""),
        ),
    )  # fmt: skip
    [rep] = s.repeats
    assert (rep.header_rows, rep.start_row, rep.has_example) == ([0], 1, True)
    assert [c.label for c in rep.columns] == ["연 번", "성명", "동의자 서명"]


def test_모양이_다른_예시_항목을_건너뛰어_헤더를_찾는다(tmp_path):
    """인건비 입금계좌 확인서: 예시 항목 2번이 두 줄에 병합돼 있고, 3~5번만 같은 모양이다.

    옛 판정은 데이터 행(3)을 헤더로 잡았다. 예시 항목 행은 슬롯으로도 잡지 않는다.
    """
    s = schema_of(
        tmp_path,
        tbl(
            7, 3,
            (0, 0, "연번"), (0, 1, "강사명"), (0, 2, "강사료"),
            (1, 0, "1"), (1, 1, "000"), (1, 2, "35,000"),
            (2, 0, "2", 1, 2), (2, 1, "", 1, 2), (2, 2, "100,000"),
            (3, 2, "50,000"),
            (4, 0, "3"), (4, 1, ""), (4, 2, ""),
            (5, 0, "4"), (5, 1, ""), (5, 2, ""),
            (6, 0, "5"), (6, 1, ""), (6, 2, ""),
        ),
    )  # fmt: skip
    [rep] = s.repeats
    assert (rep.header_rows, rep.example_rows, rep.start_row, rep.end_row) == (
        [0], [1, 2, 3], 4, 6,
    )  # fmt: skip
    assert [c.label for c in rep.columns] == ["연번", "강사명", "강사료"]
    assert rep.has_example
    assert s.slots == []


def test_항목명과_값이_번갈아_나오는_표는_반복영역이_아니다(tmp_path):
    s = schema_of(
        tmp_path,
        tbl(
            3, 2,
            (0, 0, "성명"), (0, 1, ""),
            (1, 0, "소속"), (1, 1, "○○○○과"),
            (2, 0, "연락처"), (2, 1, ""),
        ),
    )  # fmt: skip
    assert s.repeats == []
    assert [(x.label, x.origin, x.required) for x in s.slots] == [
        ("성명", "empty", False),
        ("소속", "placeholder", True),
        ("연락처", "empty", False),
    ]


def test_왼쪽이_숫자면_위쪽_열_이름을_항목명으로_쓴다(tmp_path):
    """세부과제 운영 계획서: 비고 칸 왼쪽이 계정 코드(1, 5)라 옛 판정은 항목명을 "1" 로 잡았다."""
    s = schema_of(
        tmp_path,
        tbl(
            2, 3,
            (0, 0, "집행항목"), (0, 1, "정과목"), (0, 2, "비고"),
            (1, 0, "① 강사료"), (1, 1, "1"), (1, 2, ""),
        ),
    )  # fmt: skip
    assert [(x.id, x.label) for x in s.slots] == [("t0.r1.c2", "비고")]


def test_왼쪽이_숫자고_위에도_없으면_항목명을_비운다(tmp_path):
    s = schema_of(tmp_path, tbl(1, 2, (0, 0, "200,000,000"), (0, 1, "0,000,000")))
    assert [(x.id, x.label) for x in s.slots] == [("t0.r0.c0", ""), ("t0.r0.c1", "")]


def test_값이_붙은_합계_행도_합계_행이다(tmp_path):
    """학생모니터링 활동비 지급 내역서: 합계 행이 `합계 : 10,500원` 한 칸이다."""
    s = schema_of(
        tmp_path,
        tbl(
            5, 2,
            (0, 0, "연번"), (0, 1, "성명"),
            (1, 0, "1"), (1, 1, ""),
            (2, 0, "2"), (2, 1, ""),
            (3, 0, "3"), (3, 1, ""),
            (4, 0, "합계 : 10,500원 × 00시간", 2, 1),
        ),
    )  # fmt: skip
    assert s.repeats[0].total_row == 4
    assert [(x.id, x.label) for x in s.slots] == [("t0.r4.c0", "합계")]  # 위의 연번 3 이 아니다


def test_합계_행_아래_빈_줄은_합계_칸이_아니다(tmp_path):
    """연구수당 기여도 평가서: 합계 행 아래 빈 줄의 칸들이 `합 계` 를 항목명으로 물려받았다."""
    s = schema_of(
        tmp_path,
        tbl(
            3, 2,
            (0, 0, "강사료"), (0, 1, ""),
            (1, 0, "합    계"), (1, 1, ""),
            (2, 0, ""), (2, 1, ""),
        ),
    )  # fmt: skip
    assert [(x.id, x.label) for x in s.slots] == [("t0.r0.c1", "강사료"), ("t0.r1.c1", "합 계")]


def test_항목명이_없는_빈_칸은_슬롯이_아니다(tmp_path):
    s = schema_of(tmp_path, tbl(1, 2, (0, 0, ""), (0, 1, "")))
    assert s.slots == []


# ------------------------------------------------------------------ 결재란
def test_결재란_칸은_결재선이_채운다(tmp_path):
    s = schema_of(
        tmp_path,
        tbl(
            2, 3,
            (0, 0, "결 재", 1, 2), (0, 1, "담당"), (0, 2, "처장"),
            (1, 1, ""), (1, 2, "00/00"),
        ),
    )  # fmt: skip
    assert [(x.id, x.role, x.required) for x in s.slots] == [
        ("t0.r1.c1", "approval", False),
        ("t0.r1.c2", "approval", False),
    ]
    assert s.input_slots() == []


def test_직위_하나뿐이면_결재란으로_보지_않는다(tmp_path):
    s = schema_of(tmp_path, tbl(1, 2, (0, 0, "담당"), (0, 1, "")))
    assert [x.role for x in s.slots] == ["input"]


def test_누름틀이_든_칸은_슬롯이_아니다(tmp_path):
    cell = hf.para().replace('<hp:run charPrIDRef="10"/>', hf.field("docnumber", "○○처-0000"))
    s = schema_of(tmp_path, tbl(1, 2, (0, 0, "문서번호"), (0, 1, cell)))
    assert s.slots == []
    assert [f.name for f in s.fields] == ["docnumber"]


# ------------------------------------------------------------------ 누름틀
def test_누름틀을_이름별로_묶고_역할과_유형을_정한다(tmp_path):
    s = schema_of(
        tmp_path,
        hf.field("docnumber", "○○○○처-000000", fid="1"),
        hf.field("enforcedate", "", fid="2"),
        hf.field("apb1_date", "00/00", fid="3"),
        hf.field("docnumber", "", fid="4"),
        hf.formula("=1+1", "2", fid="5"),
    )
    got = {f.name: (f.occurrences, f.role, f.input_type, f.placeholder) for f in s.fields}
    assert got == {
        "docnumber": (2, "input", "text", "○○○○처-000000"),
        "enforcedate": (1, "input", "date", ""),
        "apb1_date": (1, "approval", "date", "00/00"),
    }


# ------------------------------------------------------------------ 변형
def _기안문_변형(i: int) -> str:
    """본문 표 -> content_body -> 머리 표 -> docnumber -> 결재선 누름틀 -> 직인 표."""
    return (
        tbl(1, 1, (0, 0, "본문"))
        + hf.field("content_body", "", fid=f"{i}1")
        + tbl(1, 1, (0, 0, "머리"))
        + hf.field("docnumber", "", fid=f"{i}2")
        + hf.field("apb1_sign", "", fid=f"{i}3")
        + tbl(1, 1, (0, 0, "직인"))
        + hf.field("seal", "", fid=f"{i}4")
    )


def test_같은_서식이_여러_번_들면_변형으로_나눈다(tmp_path):
    s = schema_of(
        tmp_path, *(_기안문_변형(i) for i in range(1, 4)), stem="[AID 서식 1-01~03호] 일반기안문"
    )
    assert [(v.label, v.first_table, v.last_table) for v in s.variants] == [
        ("1-01호", 0, 2),
        ("1-02호", 3, 5),
        ("1-03호", 6, 8),
    ]
    assert s.variant_of(4) == 1


def test_머리_표가_첫_누름틀보다_앞서도_변형_경계가_맞다(tmp_path):
    """지출품의서: 머리 표 두 개가 첫 누름틀(BILL_TYPE_RCD)보다 앞에 있다."""

    def 변형(i: int) -> str:
        return (
            tbl(1, 1, (0, 0, "머리"))
            + tbl(1, 1, (0, 0, "결재"))
            + hf.field("BILL_TYPE_RCD", "", fid=f"{i}1")
            + tbl(1, 1, (0, 0, "본문"))
            + hf.field("publication", "", fid=f"{i}2")
        )

    s = schema_of(tmp_path, 변형(1), 변형(2), stem="(서식 1-05~09호) [앵커(신산업)] 지출품의서")
    # 번호가 5개인데 변형이 2개면 번호를 붙이지 않는다
    assert [(v.label, v.first_table, v.last_table) for v in s.variants] == [
        ("변형 1", 0, 2),
        ("변형 2", 3, 5),
    ]


def test_누름틀이_한_번씩이면_변형이_없다(tmp_path):
    s = schema_of(tmp_path, _기안문_변형(1))
    assert s.variants == []


# ------------------------------------------------------------------ 메타
def test_파일_이름과_경로에서_서식_정보를_채운다(tmp_path):
    s = schema_of(
        tmp_path,
        교통비(),
        stem="[AID 서식 5-05호] 교통비 지급 내역서",
        location=FormLocation("AID", 5, "학생지원비", False),
        template="templates/x.hwpx",
    )
    assert (s.form_id, s.title, s.program, s.category_no, s.category, s.archived) == (
        "5-05", "교통비 지급 내역서", "AID", 5, "학생지원비", False,
    )  # fmt: skip
    assert s.template == "templates/x.hwpx"


def test_경로에_사업이_없으면_파일_이름의_사업을_쓴다(tmp_path):
    s = schema_of(tmp_path, 교통비(), stem="(서식 5-05호) [앵커(신산업)] 교통비 지급 내역서")
    assert s.program == "앵커(신산업)"


# ------------------------------------------------------------------ 수식이 읽는 칸
def test_수식이_읽는_빈_입력칸은_금액이다(tmp_path):
    """정산서: 집행 금액 칸은 자리표시자 없이 비어 있어 항목명만으로는 유형을 모른다."""
    s = schema_of(
        tmp_path,
        tbl(
            4, 3,
            (0, 0, "집행항목"), (0, 1, "예산금액"), (0, 2, "비고"),
            (1, 0, "강사료"), (1, 1, ""), (1, 2, ""),
            (2, 0, "회의비"), (2, 1, ""), (2, 2, ""),
            (3, 0, "합계"), (3, 1, hf.formula_para("=SUM(ABOVE)", "0")), (3, 2, ""),
        ),  # fmt: skip
    )
    types = {x.id: x.input_type for x in s.slots}
    assert types["t0.r1.c1"] == types["t0.r2.c1"] == "amount"
    assert types["t0.r1.c2"] == "text"  # 수식이 읽지 않는 비고 칸은 그대로
