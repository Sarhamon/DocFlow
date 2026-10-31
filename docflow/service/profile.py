"""계정 정보로 문서 칸 미리 채우기 — 부서·전화·팩스·전자우편.

글로 채우기(LLM)는 글에 없는 값을 지어내지 않으므로, 작성자마다 같은 값은 계정에서 가져온다.
모두 관리자가 계정에 적은 값(`users`)이다. 팩스는 따로 받지 않고 계정의 연락처를 같이 쓴다.
새 문서의 입력 틀에서 **빈 칸만** 채운다. 서식에 실제 값이 박힌 칸(대표 전화 등)은 그대로 둔다.
"""

from __future__ import annotations

import re
from typing import Any

from docflow.schema.model import FormSchema
from docflow.service.db import User

#: 계정 정보 -> 그 값을 받는 누름틀
_FIELDS = {"phone": "telephone", "fax": "fax", "email": "email"}
#: 계정 정보 -> 그 값을 받는 표 칸의 이름 (띄어쓰기·기호를 뺀 칸 이름 **전체**와 맞춘다).
#: `강사 연락처`, `참여학과` 처럼 다른 사람·다른 곳을 가리키는 칸은 채우지 않는다
_CELLS = {
    "department": re.compile(r"(?:소속|학과|부서)+명?"),
    "phone": re.compile(r"전화(?:번호)?|연락처"),
    "fax": re.compile(r"팩스(?:번호)?|FAX", re.I),
    "email": re.compile(r"전자우편|이메일|E-?mail", re.I),
}


def _bare(label: str) -> str:
    return re.sub(r"[\W_]", "", label)


def defaults(schema: FormSchema, user: User) -> dict[str, dict[str, str]]:
    """계정 정보로 채울 값: {"fields": {누름틀: 값}, "cells": {칸 id: 값}}.

    계정에 없는 정보는 뺀다. 팩스는 연락처와 같은 번호다.
    """
    phone = user.phone or ""
    info = {"department": user.department, "phone": phone, "fax": phone,
            "email": user.email or ""}  # fmt: skip
    info = {k: v.strip() for k, v in info.items() if v and v.strip()}
    names = {f.name for f in schema.fields if f.role == "input"}
    fields = {_FIELDS[k]: v for k, v in info.items() if _FIELDS.get(k) in names}
    cells = {}
    for slot in schema.input_slots():
        label = _bare(slot.label)
        key = next((k for k, pattern in _CELLS.items() if pattern.fullmatch(label)), None)
        if key in info:
            cells[slot.id] = info[key]
    return {"fields": fields, "cells": cells}


def fill_blanks(skeleton: dict[str, Any], values: dict[str, dict[str, str]]) -> dict[str, Any]:
    """입력 틀의 빈 칸에만 값을 넣는다. 틀에 없는 칸(다른 변형)은 넣지 않는다."""
    out = dict(skeleton)
    for part in ("fields", "cells"):
        mine = dict(out.get(part) or {})
        for key, value in values.get(part, {}).items():
            if key in mine and not str(mine[key]).strip():
                mine[key] = value
        out[part] = mine
    return out
