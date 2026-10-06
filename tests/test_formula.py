"""한글 표 수식 계산기 테스트. 문법은 서식 193개의 수식 305개를 조사해 정했다."""

from __future__ import annotations

from decimal import Decimal

import pytest

from docflow.hwpx.formula import FormulaError, evaluate, format_result, parse_number

D = Decimal


# ------------------------------------------------------------------ 숫자
@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("5,000", D(5000)),
        ("5,000원", D(5000)),
        ("-1,200", D(-1200)),
        ("12.5", D("12.5")),
        (" 3 ", D(3)),
        ("", None),
        ("합계", None),
        ("10명", None),
        ("1-2", None),
    ],
)
def test_숫자_파싱(text, want):
    assert parse_number(text) == want


@pytest.mark.parametrize(
    ("value", "fmt", "want"),
    [
        (D(84000), "%g,", "84,000"),
        (D("84000.00"), "%g,", "84,000"),
        (D("12.50"), "%g,", "12.5"),
        (D("1234567.5"), "%g,", "1,234,567.5"),
        (D(0), "%g,", "0"),
        (D("0.0909"), "%.2f", "0.09"),
        (D("1234.56"), "%.1f,", "1,234.6"),
        (D(100), "%.1f,", "100.0"),
        (D(-1500), "%g,", "-1,500"),
        (D("-0"), "%g,", "0"),
        (D("-0.0"), "%.2f", "0.00"),
    ],
)
def test_결과_형식(value, fmt, want):
    assert format_result(value, fmt) == want


def test_알수없는_결과_형식은_거부한다():
    with pytest.raises(FormulaError):
        format_result(D(1), "%s")


# ------------------------------------------------------------------ 계산
def grid(*rows):
    return [list(r) for r in rows]


def test_SUM_ABOVE는_위쪽_숫자를_더한다():
    g = grid(["", "금액"], ["", "5,000"], ["", "7,000"], ["합계", ""])
    assert evaluate("=SUM(ABOVE)", g, 3, 1) == 12000


def test_SUM_ABOVE는_숫자가_아닌_칸에서_멈춘다():
    g = grid(["9,999"], ["제목"], ["1"], ["2"], [""])
    assert evaluate("=SUM(ABOVE)", g, 4, 0) == 3  # '제목' 위의 9,999 는 더하지 않는다


def test_SUM_LEFT():
    g = grid(["이름", "1", "2", ""])
    assert evaluate("=SUM(LEFT)", g, 0, 3) == 3


def test_물음표는_수식이_든_칸의_열과_행이다():
    g = grid(["1", "x"], ["2", "x"], ["3", "x"], ["", "x"])
    assert evaluate("=SUM(?1:?3)", g, 3, 0) == 6  # ? = 현재 열
    g = grid(["2", "5", ""])
    assert evaluate("=PRODUCT(A?:B?)", g, 0, 2) == 10  # ? = 현재 행


def test_범위_함수는_숫자가_아닌_칸을_건너뛴다():
    g = grid(["1"], ["메모"], ["2"], [""])
    assert evaluate("=SUM(A1:A3)", g, 3, 0) == 3


def test_빈_범위의_곱은_0이다():
    """한글은 숫자가 하나도 없는 범위의 PRODUCT 를 0 으로 표시한다."""
    g = grid(["", "", ""])
    assert evaluate("=PRODUCT(A?:B?)", g, 0, 2) == 0


def test_사칙연산과_우선순위():
    g = grid(["2", "3", "4", ""])
    assert evaluate("=a?+b?*c?", g, 0, 3) == 14
    assert evaluate("=(a?+b?)*c?", g, 0, 3) == 20
    assert evaluate("=c?-a?-b?", g, 0, 3) == -1
    assert evaluate("=-a?+c?", g, 0, 3) == 2


def test_함수_이름은_대소문자를_가리지_않는다():
    g = grid(["1", "2", ""])
    assert evaluate("=sum(a?:b?)", g, 0, 2) == 3


def test_함수_인수가_여러_개():
    g = grid(["1", "2", "3", ""])
    assert evaluate("=SUM(A1:B1, C1)", g, 0, 3) == 6


def test_나눗셈과_백분율():
    g = grid(["", "10", "9"])
    assert evaluate("=c1/b1*100", g, 0, 0) == 90
    assert evaluate("=1-(C1/B1)", g, 0, 0) == D("0.1")


def test_빈_칸은_0이다():
    g = grid(["", "5", ""])
    assert evaluate("=a1+b1", g, 0, 2) == 5


def test_0으로_나누면_오류():
    g = grid(["1", "0", ""])
    with pytest.raises(FormulaError, match="0 으로"):
        evaluate("=a?/b?", g, 0, 2)


@pytest.mark.parametrize(
    "bad",
    ["=AVG(A1:A2)", "=SUM(A1:A2", "=1+", "SUM(ABOVE)", "=1 2", "=Z9", "=a?+"],
)
def test_계산할_수_없는_수식은_추측하지_않고_오류(bad):
    g = grid(["1", ""], ["2", ""])
    with pytest.raises(FormulaError):
        evaluate(bad, g, 0, 1)


def test_산술에_쓰는_칸이_숫자가_아니면_오류():
    g = grid(["메모", ""])
    with pytest.raises(FormulaError, match="숫자가 아닌"):
        evaluate("=a1+1", g, 0, 1)
