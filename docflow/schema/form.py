# -*- coding: utf-8 -*-
"""서식 스키마 — 서식 하나를 기계가 읽을 수 있는 형태로 기술한다.

`tools/extract_schema.py` 가 HWPX 템플릿에서 자동 추출하고,
검증 체크리스트(`checklist`)는 이후 단계에서 채운다.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

#: 서식에 미리 박혀 있는 자리표시자 패턴. 실제 채워야 할 지점을 찾는 신호다.
#: 예) ○○○○○과, 재학생 00명, 20○○년, 0,000,000원, 2000000000, □현장견학
PLACEHOLDER_PATTERNS = [
    re.compile(r"[○◯〇●]{2,}"),   # ○○○○○과, 20○○년
    re.compile(r"[OＯ]{3,}"),      # OOO
    re.compile(r"0{2,}"),          # 2000.00.00, 0,000,000원, 재학생 00명, 학번 2000000000
    re.compile(r"[□▢☐]"),          # □현장견학 (선택 항목)
    re.compile(r"[ㅇㆍ]{2,}"),       # ㅇㅇㅇ (한글 이응을 자리표시자로 쓴 경우)
    re.compile(r"홍길[동서남북]"),    # 예시 인명. 명단 예시 행을 헤더로 오인하지 않기 위함
]


def is_placeholder(text: str) -> bool:
    """셀 텍스트가 채워 넣어야 할 자리표시자를 담고 있는지."""
    return any(p.search(text) for p in PLACEHOLDER_PATTERNS)


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


#: "(서식 5-05호) [앵커(신산업)] 교통비 지급 내역서"
#: "(서식 1-01~04호) [앵커(신산업)] 일반기안문(샘플)"
_NAME_RE = re.compile(
    r"^\(서식\s*(?P<form_id>[\d\-~호\s]+?)호?\)\s*"
    r"(?:\[(?P<program>[^\]]+)\]\s*)?"
    r"(?P<title>.+)$"
)


def parse_form_name(stem: str) -> tuple[str, str, str]:
    """파일명에서 (서식번호, 사업명, 서식이름) 을 뽑는다."""
    m = _NAME_RE.match(stem.strip())
    if not m:
        return "", "", stem
    form_id = m.group("form_id").replace("호", "").strip()
    return form_id, (m.group("program") or "").strip(), m.group("title").strip()
