"""한글 표 수식(FORMULA 누름틀) 계산기.

한글은 수식을 저장만 하고, 문서를 열거나 변환할 때 다시 계산하지 않는다. 그래서 값을 채운 뒤
합계 칸이 예전 값으로 남는다. 이 모듈이 수식을 직접 계산해 결과를 돌려준다.

지원하는 문법 (서식 193개의 수식 305개를 조사해 정했다)::

    =SUM(ABOVE)            위쪽 칸의 합 (숫자가 아닌 칸을 만나면 거기서 멈춘다)
    =SUM(?3:?6)            범위의 합. ? 는 "수식이 든 칸의 열/행"
    =PRODUCT(F?:G?)        범위의 곱
    =e?+f?+h?+I?           칸 참조와 사칙연산, 괄호
    =C?-sum(d?:e?)         함수 이름은 대소문자를 가리지 않는다
    =c?/b?*100             나눗셈

칸 참조는 열 문자(A=0) + 행 번호(1부터)다. 계산하지 못하는 수식은 추측하지 않고
FormulaError 를 낸다. 틀린 합계를 쓰는 것보다 못 쓰는 편이 낫다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal, DivisionByZero, InvalidOperation

Grid = list[list[str]]


class FormulaError(Exception):
    """계산할 수 없는 수식이다 (지원하지 않는 문법, 0으로 나눔 등)."""


# ------------------------------------------------------------------ 숫자
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def parse_number(text: str) -> Decimal | None:
    """'5,000' '5,000원' '-1,200' 같은 문자열을 숫자로. 숫자가 아니면 None."""
    s = text.replace(",", "").replace(" ", "").replace(" ", "")
    s = s.removesuffix("원")
    if not _NUMBER_RE.match(s):
        return None
    return Decimal(s)


def format_result(value: Decimal, fmt: str) -> str:
    """한글의 ResultFormat(예: '%g,' '%.1f,' '%.2f') 대로 결과를 문자열로 만든다.

    끝의 ',' 는 천 단위 구분이다. %g 는 소수가 없으면 정수로, 있으면 필요한 만큼만 쓴다.
    """
    if value == 0:
        value = Decimal(0)  # -0 이 "-0" 으로 찍히지 않게 한다
    comma = fmt.endswith(",")
    m = re.match(r"^%(?:\.(\d+))?([fg])", fmt)
    if not m:
        raise FormulaError(f"알 수 없는 결과 형식: {fmt!r}")
    digits, kind = m.groups()
    if kind == "f":
        places = int(digits or 6)
        text = f"{value:,.{places}f}" if comma else f"{value:.{places}f}"
    else:
        rounded = value.quantize(Decimal(1)) if value == value.to_integral_value() else value
        text = format(rounded.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        if comma:
            head, _, frac = text.partition(".")
            text = f"{int(head):,}" + (f".{frac}" if frac else "")
    return text


# ------------------------------------------------------------------ 구문 분석
_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<num>\d+(?:\.\d+)?)
      | (?P<ref>[A-Za-z?](?:\d+|\?))(?![A-Za-z(])
      | (?P<name>[A-Za-z]+)
      | (?P<op>[-+*/(),:])
    )""",
    re.VERBOSE,
)


def _tokenize(src: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            if src[pos:].strip() == "":
                break
            raise FormulaError(f"해석할 수 없는 수식: {src!r}")
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    return tokens


class _Calc:
    def __init__(self, tokens: list[tuple[str, str]], grid: Grid, row: int, col: int) -> None:
        self.t = tokens
        self.i = 0
        self.grid = grid
        self.row = row
        self.col = col

    # -- 토큰 도우미
    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None or (value is not None and tok[1] != value):
            raise FormulaError("수식이 끝나지 않았거나 괄호가 맞지 않는다")
        self.i += 1
        return tok

    # -- 문법: expr -> term (+|- term)* ; term -> unary (*|/ unary)* ; unary -> -unary | atom
    def expr(self) -> Decimal:
        value = self.term()
        while (tok := self.peek()) and tok[1] in "+-" and tok[0] == "op":
            self.take()
            rhs = self.term()
            value = value + rhs if tok[1] == "+" else value - rhs
        return value

    def term(self) -> Decimal:
        value = self.unary()
        while (tok := self.peek()) and tok[1] in "*/" and tok[0] == "op":
            self.take()
            rhs = self.unary()
            if tok[1] == "*":
                value *= rhs
            else:
                try:
                    value /= rhs
                except (DivisionByZero, InvalidOperation) as e:
                    raise FormulaError("0 으로 나눌 수 없다") from e
        return value

    def unary(self) -> Decimal:
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] == "-":
            self.take()
            return -self.unary()
        return self.atom()

    def atom(self) -> Decimal:
        tok = self.peek()
        if tok is None:
            raise FormulaError("수식이 비어 있다")
        kind, text = tok
        if kind == "num":
            self.take()
            return Decimal(text)
        if kind == "ref":
            self.take()
            return self.cell_value(*self.resolve(text))
        if kind == "op" and text == "(":
            self.take()
            value = self.expr()
            self.take(")")
            return value
        if kind == "name":
            self.take()
            return self.call(text.upper())
        raise FormulaError(f"예상하지 못한 {text!r}")

    # -- 함수
    def call(self, name: str) -> Decimal:
        funcs: dict[str, Callable[[list[Decimal]], Decimal]] = {
            "SUM": lambda xs: sum(xs, Decimal(0)),
            "PRODUCT": _product,
        }
        if name not in funcs:
            raise FormulaError(f"지원하지 않는 함수: {name}")
        self.take("(")
        values = self.args()
        self.take(")")
        return funcs[name](values)

    def args(self) -> list[Decimal]:
        values: list[Decimal] = []
        while True:
            values.extend(self.arg())
            tok = self.peek()
            if tok and tok[1] == ",":
                self.take()
                continue
            return values

    def arg(self) -> list[Decimal]:
        tok = self.peek()
        if tok and tok[0] == "name" and tok[1].upper() in ("ABOVE", "LEFT"):
            self.take()
            return self.directional(tok[1].upper())
        if tok and tok[0] == "ref":
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
            if nxt and nxt[1] == ":":
                first = self.resolve(self.take()[1])
                self.take(":")
                last = self.resolve(self.take()[1])
                return self.range_values(first, last)
        return [self.expr()]

    # -- 칸
    def resolve(self, ref: str) -> tuple[int, int]:
        letter, number = ref[0], ref[1:]
        col = self.col if letter == "?" else ord(letter.upper()) - ord("A")
        row = self.row if number == "?" else int(number) - 1
        return row, col

    def text_at(self, row: int, col: int) -> str:
        if not (0 <= row < len(self.grid) and 0 <= col < len(self.grid[0])):
            raise FormulaError(f"표 밖의 칸을 참조한다 (행 {row + 1}, 열 {col + 1})")
        return self.grid[row][col]

    def cell_value(self, row: int, col: int) -> Decimal:
        text = self.text_at(row, col).strip()
        if text == "":
            return Decimal(0)
        number = parse_number(text)
        if number is None:
            raise FormulaError(f"숫자가 아닌 칸을 계산에 쓴다: {text!r}")
        return number

    def range_values(self, first: tuple[int, int], last: tuple[int, int]) -> list[Decimal]:
        (r1, c1), (r2, c2) = first, last
        values = []
        for r in range(min(r1, r2), max(r1, r2) + 1):
            for c in range(min(c1, c2), max(c1, c2) + 1):
                number = parse_number(self.text_at(r, c))
                if number is not None:  # 범위 함수는 숫자가 아닌 칸을 건너뛴다
                    values.append(number)
        return values

    def directional(self, which: str) -> list[Decimal]:
        """ABOVE / LEFT: 숫자가 아닌 칸을 만나기 전까지의 숫자들."""
        step = (-1, 0) if which == "ABOVE" else (0, -1)
        r, c = self.row + step[0], self.col + step[1]
        values: list[Decimal] = []
        while 0 <= r < len(self.grid) and 0 <= c < len(self.grid[0]):
            number = parse_number(self.grid[r][c])
            if number is None:
                break
            values.append(number)
            r, c = r + step[0], c + step[1]
        return values


def _product(xs: list[Decimal]) -> Decimal:
    """숫자가 하나도 없으면 0 이다 (한글은 빈 범위의 곱을 0 으로 표시한다)."""
    if not xs:
        return Decimal(0)
    out = Decimal(1)
    for x in xs:
        out *= x
    return out


def evaluate(formula: str, grid: Grid, row: int, col: int) -> Decimal:
    """수식을 계산한다. grid 는 병합을 펼친 텍스트 격자, (row, col) 은 수식이 든 칸(0부터)."""
    src = formula.strip()
    if not src.startswith("="):
        raise FormulaError(f"수식이 '=' 로 시작하지 않는다: {formula!r}")
    calc = _Calc(_tokenize(src[1:]), grid, row, col)
    value = calc.expr()
    if calc.peek() is not None:
        raise FormulaError(f"수식 뒤에 남는 부분이 있다: {formula!r}")
    return value
