"""서식 스키마 v1 (옛 모델) — 서식 하나를 기계가 읽을 수 있는 형태로 기술한다.

추출기는 이제 v2 모델(`schema/model.py`)을 만든다. 이 모듈은 옛 모델을 받는
`validate.py` 를 위해 남겨 두었고, M3 에서 검증 엔진이 v2 로 옮겨가면 지운다.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# 판정 함수는 rules.py 로 옮겼다. 옛 모델을 쓰는 validate.py(M3 에서 다시 짠다)를 위해
# 다시 내보낸다.
from docflow.schema.rules import (  # noqa: F401
    DATE_PLACEHOLDER_RE,
    PLACEHOLDER_PATTERNS,
    TOTAL_LABELS,
    is_date_placeholder,
    is_placeholder,
    is_total_label,
    parse_form_name,
)


class FieldSpec(BaseModel):
    """누름틀 하나. 기안문 계열에만 풍부하게 존재한다."""

    name: str
    type: str
    occurrences: int = 1
    placeholder: str = ""


class SlotSpec(BaseModel):
    """표 안에서 값을 채워야 하는 지점."""

    id: str = Field(description="t{표번호}.r{행}.c{열}")
    table: int
    row: int
    col: int
    label: str = Field(default="", description="같은 행 왼쪽 / 같은 열 위쪽에서 찾은 항목명")
    placeholder: str = Field(default="", description="템플릿에 박혀 있던 원래 텍스트")
    kind: str = Field(default="placeholder", description="placeholder | empty")


class TableSpec(BaseModel):
    index: int
    rows: int
    cols: int
    depth: int = 0
    header: list[str] = Field(default_factory=list, description="첫 행이 헤더로 보일 때의 열 이름")
    grid: list[list[str]] = Field(default_factory=list)


class RepeatSpec(BaseModel):
    """명단·내역처럼 같은 모양의 행이 반복되는 영역.

    빈 셀을 하나씩 슬롯으로 잡으면 출석부 한 장에 350개가 나온다.
    실제로 필요한 정보는 "어떤 열에 몇 행을 채우는가" 이므로 영역으로 묶는다.
    """

    id: str = Field(description="t{표번호}.r{시작행}-{끝행}")
    table: int
    start_row: int
    end_row: int
    header: list[str] = Field(default_factory=list, description="반복 행의 열 이름")

    @property
    def capacity(self) -> int:
        return self.end_row - self.start_row + 1


class FormSchema(BaseModel):
    form_id: str
    title: str
    program: str = ""
    category: str = ""
    template: str = ""
    fields: list[FieldSpec] = Field(default_factory=list)
    tables: list[TableSpec] = Field(default_factory=list)
    slots: list[SlotSpec] = Field(default_factory=list)
    repeats: list[RepeatSpec] = Field(default_factory=list)
    checklist: list[dict] = Field(
        default_factory=list,
        description="검증 규칙. 5·6단계에서 채운다.",
    )
