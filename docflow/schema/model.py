"""서식 스키마 v2 — 서식 하나를 기계가 읽을 수 있는 형태로 기술한다.

옛 모델(`schema/form.py`)이 실제 서식에서 틀리게 나온 지점을 바로잡은 것이다.

* 반복영역이 합계 행을 포함하고 예시 행을 빼먹었다 -> 헤더 행 / 데이터 행 / 합계 행을 따로 둔다.
* 헤더가 두 줄인 서식에서 열 이름이 비었다 -> 열마다 헤더 줄을 합친 이름을 둔다.
* 기안문 1-01~04호처럼 변형 4종이 한 파일에 든다 -> `variants` 로 변형을 구분한다.
* 결재란의 템플릿 잔여값(00/00, ○○○)이 문서에 남는다 -> `role="approval"` 로 표시한다.
* 수식 칸(합계 등)을 입력 대상과 구분하지 못했다 -> `formulas` 로 따로 둔다.

좌표는 모두 **앵커 기준**이다(병합된 칸은 왼쪽 위 셀). `HwpxDocument` 의 쓰기 API 와 같은 기준이다.
이 모듈은 모델만 담는다. 추출 규칙(자리표시자 판정 등)은 다른 모듈이 맡는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

SCHEMA_VERSION = 2

#: 입력칸의 종류. 입력폼의 입력기와 형식 점검(날짜·금액)이 이 값을 따른다.
InputType = Literal["text", "date", "amount", "number", "checkbox"]

#: input    작성자가 채운다
#: approval 결재선이 채운다 (apbN_*). 입력폼에서 빼고, 생성할 때 템플릿 잔여값을 비운다
#: computed 수식이 계산한다 (합계 등). 입력 대상이 아니다
Role = Literal["input", "approval", "computed"]


class FieldSpec(BaseModel):
    """누름틀 하나. 같은 이름이 변형마다 한 번씩 나올 수 있다."""

    name: str
    type: str
    role: Role = "input"
    input_type: InputType = "text"
    occurrences: int = Field(default=1, ge=1, description="문서 안에서 나오는 횟수")
    placeholder: str = Field(default="", description="서식에 박혀 있던 첫 값")


class SlotSpec(BaseModel):
    """표 안에서 값을 채워야 하는 칸 하나 (앵커 좌표)."""

    table: int = Field(ge=0)
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    label: str = Field(default="", description="같은 행 왼쪽 / 같은 열 위쪽에서 찾은 항목명")
    placeholder: str = Field(default="", description="서식에 박혀 있던 원래 텍스트")
    #: placeholder 자리표시자가 있던 칸 / empty 항목명만 있는 빈 칸
    origin: Literal["placeholder", "empty"] = "placeholder"
    input_type: InputType = "text"
    role: Role = "input"
    required: bool = Field(
        default=True, description="자리표시자가 있던 칸은 필수, 항목명만 있던 빈 칸은 선택"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        return f"t{self.table}.r{self.row}.c{self.col}"


class FormulaSpec(BaseModel):
    """표 칸 안의 수식 (합계 등). 값은 우리가 계산해서 쓴다 — 한글은 다시 계산하지 않는다."""

    table: int = Field(ge=0)
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    formula: str
    result_format: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        return f"t{self.table}.r{self.row}.c{self.col}"


class RepeatColumn(BaseModel):
    """반복영역의 열 하나 (앵커 열)."""

    col: int = Field(ge=0)
    label: str = Field(default="", description="헤더 줄들을 합친 열 이름")
    input_type: InputType = "text"
    #: 데이터 행마다 미리 채워져 있는 값 (연번, 기본 금액 등). None 이면 비어 있다.
    prefilled: str | None = None


class RepeatSpec(BaseModel):
    """명단·내역처럼 같은 모양의 행이 반복되는 영역.

    ``start_row..end_row`` 가 데이터 행이다. 서식의 예시 행(홍길동 등)은 **첫 데이터 행**이며
    첫 사람의 값으로 덮어쓴다. 합계 행은 영역 밖이다. ``HwpxDocument.fill_rows`` 에 그대로 넘긴다.
    """

    table: int = Field(ge=0)
    header_rows: list[int] = Field(default_factory=list)
    start_row: int = Field(ge=0, description="첫 데이터 행 (예시 행 포함)")
    end_row: int = Field(ge=0, description="마지막 데이터 행 (합계 행 제외)")
    total_row: int | None = Field(default=None, description="합계 행. 없으면 None")
    has_example: bool = Field(default=False, description="첫 데이터 행이 예시 값을 담고 있는가")
    columns: list[RepeatColumn] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        return f"t{self.table}.r{self.start_row}-{self.end_row}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def capacity(self) -> int:
        """서식이 그대로 수용하는 행 수. 넘으면 행을 복제해 늘린다."""
        return self.end_row - self.start_row + 1

    @model_validator(mode="after")
    def _check_rows(self) -> RepeatSpec:
        if self.end_row < self.start_row:
            raise ValueError("end_row 는 start_row 보다 작을 수 없다")
        if self.total_row is not None and self.total_row <= self.end_row:
            raise ValueError("합계 행은 데이터 행 아래에 있어야 한다")
        if any(r >= self.start_row for r in self.header_rows):
            raise ValueError("헤더 행은 데이터 행 위에 있어야 한다")
        return self


class TableSpec(BaseModel):
    index: int = Field(ge=0)
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)
    depth: int = Field(default=0, ge=0, description="중첩 깊이 (0 = 최상위)")
    grid: list[list[str]] = Field(default_factory=list, description="병합을 펼친 텍스트 격자")


class Variant(BaseModel):
    """한 파일에 든 서식 변형 하나 (예: 기안문 1-01호 / 02호 / 03호 / 04호).

    변형 n 은 표 ``first_table..last_table`` 과, 누름틀마다 n 번째 등장분으로 이루어진다.
    사용자는 변형 하나를 고르고, 고르지 않은 변형의 쪽은 생성 단계에서 제외한다.
    """

    index: int = Field(ge=0)
    label: str
    first_table: int = Field(ge=0)
    last_table: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_tables(self) -> Variant:
        if self.last_table < self.first_table:
            raise ValueError("last_table 은 first_table 보다 작을 수 없다")
        return self


class FormSchema(BaseModel):
    schema_version: int = SCHEMA_VERSION
    form_id: str = ""
    title: str
    program: str = ""
    category: str = ""
    template: str = Field(default="", description="저장소 루트 기준 HWPX 템플릿 경로")
    variants: list[Variant] = Field(default_factory=list, description="비어 있으면 변형 없음")
    fields: list[FieldSpec] = Field(default_factory=list)
    tables: list[TableSpec] = Field(default_factory=list)
    slots: list[SlotSpec] = Field(default_factory=list)
    formulas: list[FormulaSpec] = Field(default_factory=list)
    repeats: list[RepeatSpec] = Field(default_factory=list)
    checklist: list[dict] = Field(
        default_factory=list, description="검증 규칙 (이후 단계에서 채움)"
    )

    @model_validator(mode="after")
    def _check_references(self) -> FormSchema:
        count = len(self.tables)
        groups = (("슬롯", self.slots), ("수식", self.formulas), ("반복영역", self.repeats))
        for kind, items in groups:
            for item in items:
                if item.table >= count:
                    raise ValueError(f"{kind} {item.id} 가 없는 표 {item.table} 을 가리킨다")
        for v in self.variants:
            if v.last_table >= count:
                raise ValueError(f"변형 {v.label} 이(가) 없는 표 {v.last_table} 을 가리킨다")
        return self

    def variant_of(self, table: int) -> int | None:
        """표가 속한 변형의 번호. 변형이 없거나 어느 변형에도 속하지 않으면 None."""
        for v in self.variants:
            if v.first_table <= table <= v.last_table:
                return v.index
        return None

    def input_slots(self) -> list[SlotSpec]:
        """작성자가 채우는 칸."""
        return [s for s in self.slots if s.role == "input"]
