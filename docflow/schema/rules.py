"""서식 판정 규칙 — 칸의 텍스트·항목명·파일 경로만 보고 판단하는 순수 함수.

추출(`schema/extract.py`)과 검증(`validate.py`)이 같은 판정을 공유한다.
문서 구조(표·행)를 봐야 하는 판단은 여기 두지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

# ---------------------------------------------------------------- 자리표시자

#: 서식에 미리 박혀 있는 자리표시자 패턴. 실제 채워야 할 지점을 찾는 신호다.
#: 예) ○○○○○과, 재학생 00명, 20○○년, 0,000,000원, 2000000000, □현장견학
PLACEHOLDER_PATTERNS = [
    re.compile(r"[○◯〇●]{2,}"),  # ○○○○○과, 20○○년
    re.compile(r"[OＯ]{3,}"),  # OOO
    re.compile(r"0{2,}"),  # 2000.00.00, 0,000,000원, 재학생 00명, 학번 2000000000
    re.compile(r"[□▢☐]"),  # □현장견학 (선택 항목)
    re.compile(r"[ㅇㆍ]{2,}"),  # ㅇㅇㅇ (한글 이응을 자리표시자로 쓴 경우)
    re.compile(r"홍\s*길\s*[동서남북]"),  # 예시 인명 (`홍 길 동` 처럼 띄어 쓴 것도). 예시 행을
    # 헤더로 오인하지 않기 위함
]


def is_placeholder(text: str) -> bool:
    """셀 텍스트가 채워 넣어야 할 자리표시자를 담고 있는지."""
    return any(p.search(text) for p in PLACEHOLDER_PATTERNS)


#: 합계 행의 항목명. 반복 영역의 끝을 정하고, 금액 합계 검증의 기준점이 된다.
TOTAL_LABELS = {"합계", "계", "총계", "소계"}


def is_total_label(text: str) -> bool:
    """셀 텍스트가 합계 항목명인지."""
    return text.replace(" ", "").strip() in TOTAL_LABELS


def is_total_text(text: str) -> bool:
    """합계 칸. `합계(반드시 부가세 포함)`, `합계 : 10,500원` 처럼 설명이나 값이 붙어도 합계다."""
    return is_total_label(re.sub(r"\(.*?\)|\(.*$|:.*$", "", text))


#: 자리표시자에 쓰인 날짜. 서식에는 `2000.00.00`, `20○○년 ○○월 ○○일` 처럼
#: 숫자 자리를 0 이나 ○ 로 채워 둔다. 실제 입력값 파싱과는 패턴이 다르다.
_PH_DATE = r"(?:[0-9○O]{4}\s*[.\-/년]\s*[0-9○O]{1,2}\s*[.\-/월]\s*[0-9○O]{1,2}\s*[.일]?)"
_PH_WEEKDAY = r"(?:\s*\(\s*[월화수목금토일0○]\s*요?일?\s*\))?"
_PH_TIME = (
    r"(?:\s*[0-9○O]{1,2}\s*[:;]\s*[0-9○O]{2}"
    r"(?:\s*[~\-]\s*[0-9○O]{1,2}\s*[:;]\s*[0-9○O]{2})?)?"
)

#: 칸 전체가 날짜(또는 날짜 기간)인 경우만 통과시킨다.
#: `○○○○처-000000 2000.00.00` 처럼 문서번호가 붙은 칸을 날짜 칸으로 오인하지 않기 위함이다.
DATE_PLACEHOLDER_RE = re.compile(
    rf"^{_PH_DATE}{_PH_WEEKDAY}{_PH_TIME}"
    rf"(?:\s*[~\-]\s*{_PH_DATE}{_PH_WEEKDAY}{_PH_TIME})?\s*$"
)


def is_date_placeholder(text: str) -> bool:
    """자리표시자가 날짜 칸을 가리키는지."""
    return bool(DATE_PLACEHOLDER_RE.match(" ".join(text.split())))


# ---------------------------------------------------------------- 입력 유형

_CHECKBOX_RE = re.compile(r"[□▢☐]")
#: 0,000,000원 / 5,000 / 10,000,000 / 3000원 — 쉼표 자리 구분이 있거나 '원' 이 붙은 숫자
_AMOUNT_RE = re.compile(r"^[0-9○O]{1,3}(?:,[0-9○O]{3})+\s*원?$|^[0-9○O]+\s*원$")
#: 00명, 3, 10개 — 짧은 수량. 학번·전화번호 같은 긴 숫자와 ○○○ 는 글자로 둔다
_NUMBER_RE = re.compile(r"^[0-9]{1,5}\s*(?:명|개|회|건|매|부|권|박스|시간|일|박)?$")
_AMOUNT_LABEL_RE = re.compile(r"(금액|액|비용|단가|예산|원\)|\(원|수령액)$")
_DATE_LABEL_RE = re.compile(r"(일자|날짜|년월일|작성일|신청일|지급일)$")


def infer_input_type(text: str, label: str = "") -> str:
    """칸의 원래 텍스트(자리표시자·예시 값)와 항목명으로 입력 유형을 정한다.

    텍스트가 더 확실한 신호라 먼저 보고, 텍스트가 없을 때만 항목명을 본다.
    결과는 model.InputType 중 하나다.
    """
    t = " ".join(text.split())
    if _CHECKBOX_RE.search(t):
        return "checkbox"
    if t:
        if is_date_placeholder(t):
            return "date"
        if _AMOUNT_RE.match(t):
            return "amount"
        if _NUMBER_RE.match(t):
            return "number"
        return "text"
    name = label.replace(" ", "")
    if _AMOUNT_LABEL_RE.search(name):
        return "amount"
    if _DATE_LABEL_RE.search(name):
        return "date"
    return "text"


# ---------------------------------------------------------------- 결재란

#: 열의 뜻 -> 머리줄에서 찾는 말. **위에서부터** 본다: `교통비 입금계좌 입금액(원)` 은 계좌보다
#: 입금액이 먼저, `교통비 입금계좌 은행명` 은 은행이 먼저 걸려야 한다.
COLUMN_CONCEPTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rrn", ("주민등록번호", "주민번호")),
    ("student_id", ("학번",)),
    ("bank", ("은행",)),
    ("amount", ("입금액", "지급액", "금액")),
    ("account", ("계좌번호", "계좌")),
    ("name", ("성명", "예금주", "이름", "강사명", "전문가명")),
    ("dept", ("학과", "소속")),
    ("grade", ("학년",)),
    ("address", ("주소",)),
    ("phone", ("연락처", "전화", "휴대폰")),
)
def column_concept(label: str) -> str | None:
    """열(항목) 이름의 뜻. 모르면 None (연번·서명·비고 등). 명단 가져오기와 형식 점검이 쓴다."""
    text = "".join(label.split())
    for name, words in COLUMN_CONCEPTS:
        if any(w in text for w in words):
            return name
    return None


#: 결재란 칸의 직위. `혁신지원사업담당2`, `PM 과장`, `산학협력단계장` 처럼 앞에 소속이 붙는다.
_APPROVAL_TITLE_RE = re.compile(
    r"^.{0,15}(?:총장|처장|단장|과장|계장|팀장|실장|원장|부장|센터장|담당\d*)$|^(?:결재|전결|대결|협조)$"
)
#: 결재 날짜 자리 — `00/00`, `전결 00/00`
_APPROVAL_DATE_RE = re.compile(r"^(?:(?:전결|대결)\s*)?[0O]{2}\s*/\s*[0O]{2}$")
#: 결재선 누름틀 — apb1_type, apb8_sign …
_APPROVAL_FIELD_RE = re.compile(r"^apb\d+_")


def is_approval_title(text: str) -> bool:
    """결재란 머리의 직위 이름인지. 약한 신호라 같은 행에 둘 이상일 때만 결재란으로 본다."""
    return bool(_APPROVAL_TITLE_RE.match("".join(text.split())))  # `결⏎재` 처럼 줄을 바꿔 쓴다


def is_approval_head(text: str) -> bool:
    """결재란 머리의 `결재` 칸. 이 칸이 있는 행의 직위는 모두 결재선이다(`담당교수`)."""
    return "".join(text.split()) in {"결재", "전결", "대결", "협조"}


#: 제출 문장 속의 날짜·이름 자리: `20○○년 ○○월 ○○일`, `2000년 00월 00일`, `○ ○ ○`, `O O O`
_STATEMENT_DATE_RE = re.compile(r"(?:20|19)?[0○O]{2}\s*년\s*[0○O]{1,2}\s*월\s*[0○O]{1,2}\s*일")
_STATEMENT_NAME_RE = re.compile(r"(?:[○OＯ]\s*){2,4}(?=\s*(?:\(|$|\n))")
#: 날짜·이름 자리를 빼고 남은 글이 이만큼은 돼야 문장이다
_STATEMENT_MIN = 10


def is_statement(text: str) -> bool:
    """`상기와 같이 … 제출합니다. 20○○년 ○○월 ○○일 사업담당자 : ○ ○ ○ (날인)` 같은 제출 문장.

    문장은 그대로 두고 날짜·이름만 고치는 칸이다. 통째로 자리표시자로 보면 입력 화면이 빈 칸을
    보여 주고, 비워 두면 문장째 사라진다.
    """
    found = _STATEMENT_DATE_RE.search(text) or _STATEMENT_NAME_RE.search(text)
    rest = _STATEMENT_NAME_RE.sub("", _STATEMENT_DATE_RE.sub("", text))
    rest = " ".join(rest.split())
    return bool(found) and len(rest) >= _STATEMENT_MIN and not is_placeholder(rest)


def has_fill_marks(text: str) -> bool:
    """값에 아직 채우지 않은 자리(○, `2000년 00월 00일`, `O O O`)가 남았는지."""
    return "○" in text or bool(_STATEMENT_DATE_RE.search(text) or _STATEMENT_NAME_RE.search(text))


def is_approval_placeholder(text: str) -> bool:
    """결재 날짜 자리(`00/00`)인지."""
    return bool(_APPROVAL_DATE_RE.match(" ".join(text.split())))


def is_approval_field(name: str) -> bool:
    return bool(_APPROVAL_FIELD_RE.match(name))


#: 누름틀 이름으로 정하는 입력 유형. 이름은 전자문서 표준 서식에서 왔다.
_FIELD_TYPE_RULES = [
    (re.compile(r"date$", re.I), "date"),
    (re.compile(r"_AMT$"), "amount"),
    (re.compile(r"_CNT$"), "number"),
]


def field_input_type(name: str, placeholder: str = "") -> str:
    """누름틀의 입력 유형. 이름 규칙이 우선이고, 없으면 자리표시자로 판단한다."""
    for pattern, kind in _FIELD_TYPE_RULES:
        if pattern.search(name):
            return kind
    return infer_input_type(placeholder) if is_placeholder(placeholder) else "text"


# ---------------------------------------------------------------- 파일 이름·경로

#: "(서식 5-05호) [앵커(신산업)] 교통비 지급 내역서"
#: "(서식 1-01~04호) [앵커(신산업)] 일반기안문(샘플)"
_NAME_RE = re.compile(
    r"^\(서식\s*(?P<form_id>[\d\-~호\s]+?)호?\)\s*"
    r"(?:\[(?P<program>[^\]]+)\]\s*)?"
    r"(?P<title>.+)$"
)

#: "[AID 서식 1-01~10호] 일반기안문(샘플 10종)"
#: "[혁신 서식 9-03호] 용역구입비 지출품의서"
_NAME_BRACKET_RE = re.compile(
    r"^\[(?P<program>[^\]]+?)\s*서식\s*(?P<form_id>[\d\-~호+\s]+?)호?\]\s*"
    r"(?P<title>.+)$"
)


def parse_form_name(stem: str) -> tuple[str, str, str]:
    """파일명에서 (서식번호, 사업명, 서식이름) 을 뽑는다."""
    stem = stem.strip()
    m = _NAME_RE.match(stem) or _NAME_BRACKET_RE.match(stem)
    if not m:
        return "", "", stem
    form_id = m.group("form_id").replace("호", "").strip()
    return form_id, (m.group("program") or "").strip(), m.group("title").strip()


def form_numbers(form_id: str) -> list[str]:
    """서식번호 하나가 가리키는 개별 번호들.

    "1-01~04" -> 1-01, 1-02, 1-03, 1-04 / "1-03+05", "1-03 05" -> 1-03, 1-05 / "5-05" -> 5-05
    """
    m = re.match(r"^(\d+)-(\d+)(.*)$", form_id.strip())
    if not m:
        return [form_id] if form_id else []
    major, first, rest = m.group(1), m.group(2), m.group(3).strip()
    width = len(first)
    if rest.startswith("~"):
        last = re.match(r"~\s*(\d+)", rest)
        if last:
            return [f"{major}-{n:0{width}d}" for n in range(int(first), int(last.group(1)) + 1)]
    extra = re.findall(r"\d+", rest)
    return [f"{major}-{first}"] + [f"{major}-{int(n):0{width}d}" for n in extra]


#: 사업 폴더: "2026_AID", "2026_혁신", "[2026 앵커(신산업)] 서식모음"
_PROGRAM_DIR_RE = re.compile(r"^\[?\d{4}[ _](?P<program>.+?)\]?(?:\s*서식모음)?$")
#: 카테고리 폴더: "1. 기안문", "2_계획서및보고서", "1. [기안문] 관련 서식"
#: 번호는 두 자리까지 — "2026_AID" 같은 연도 폴더를 카테고리로 잡지 않는다.
_CATEGORY_DIR_RE = re.compile(
    r"^(?P<no>\d{1,2})\s*[._]\s*(?:\[(?P<bracket>.+?)\]\s*관련\s*서식|(?P<name>.+))$"
)
#: 지난 학년도 서식 폴더: "0_2025학년도 이전"
_ARCHIVE_DIR_RE = re.compile(r"이전$")


@dataclass(frozen=True)
class FormLocation:
    """서식 파일 경로에서 읽어낸 분류."""

    program: str = ""
    category_no: int | None = None
    category: str = ""
    #: 지난 학년도 서식이면 True. 사용자 목록에는 기본으로 내놓지 않는다.
    archived: bool = False


def normalize_category(name: str) -> str:
    """사업마다 다르게 적은 카테고리 이름을 맞춘다 ("계획서및보고서" -> "계획서 및 보고서")."""
    name = re.sub(r"\s*및\s*", " 및 ", name)
    return " ".join(name.split())


def parse_location(parts: tuple[str, ...] | list[str] | PurePath) -> FormLocation:
    """templates/ 기준 경로의 폴더들에서 사업·카테고리·보관 여부를 읽는다.

    사업은 가장 바깥의 사업 폴더, 카테고리는 가장 안쪽의 번호 붙은 폴더(0번은 목차라 제외)다.
    """
    if isinstance(parts, PurePath):
        parts = parts.parts
    program = ""
    category_no: int | None = None
    category = ""
    archived = False
    for part in parts:
        if not program and (m := _PROGRAM_DIR_RE.match(part)):
            program = m.group("program").strip()
        if _ARCHIVE_DIR_RE.search(part):
            archived = True
        m = _CATEGORY_DIR_RE.match(part)
        if m and int(m.group("no")) > 0:
            category_no = int(m.group("no"))
            category = normalize_category(m.group("bracket") or m.group("name"))
    return FormLocation(program, category_no, category, archived)
