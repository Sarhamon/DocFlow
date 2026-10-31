"""본문 초안 (M10, F2-b): 채운 입력값으로 LLM 이 서술형 칸을 쓴다. LLM 은 가짜로 바꿔 끼운다."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.llm import LlmError, LlmResult
from docflow.pipeline import parse_input
from docflow.schema.model import (
    FieldSpec,
    FormSchema,
    RepeatColumn,
    RepeatSpec,
    SlotSpec,
    TableSpec,
)
from docflow.service import draft as dr
from docflow.service.catalog import sync_templates
from docflow.service.db import connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import hwpx_factory as hf
from tests import service_fixtures as fx
from tests.test_prefill import FakeLlm
from tests.test_schema_extract import tbl

TODAY = date(2026, 10, 27)


def 기안() -> FormSchema:
    """본문 누름틀, 사업명·목적·금액·계좌 칸, 다과 내역, 참석 학생 명단."""
    return FormSchema(
        title="회의비 집행 기안",
        tables=[TableSpec(index=0, rows=12, cols=4)],
        fields=[FieldSpec(name="content_body", type="", placeholder="1. 관련: \n2. 위와 관련하여"),
                FieldSpec(name="docnumber", type="")],
        slots=[SlotSpec(table=0, row=0, col=1, label="사업명"),
               SlotSpec(table=0, row=1, col=1, label="추진 목적"),
               SlotSpec(table=0, row=2, col=1, label="회의 일자", input_type="date"),
               SlotSpec(table=0, row=3, col=1, label="입금 계좌번호")],
        repeats=[
            RepeatSpec(table=0, header_rows=[4], start_row=5, end_row=6,
                       columns=[RepeatColumn(col=0, label="품목"),
                                RepeatColumn(col=1, label="금액(원)", input_type="amount")]),
            RepeatSpec(table=0, header_rows=[7], start_row=8, end_row=11,
                       columns=[RepeatColumn(col=0, label="성명"),
                                RepeatColumn(col=1, label="계좌번호"),
                                RepeatColumn(col=2, label="교통비(원)", input_type="amount")]),
        ],
    )  # fmt: skip


RAW = {
    "fields": {"docnumber": "산학협력단-2026001"},
    "cells": {"t0.r0.c1": "캡스톤 디자인", "t0.r2.c1": "2026. 10. 30.",
              "t0.r3.c1": "110-123-456789"},
    "rows": {"t0.r5-6": [{"0": "다과", "1": "50,000"}, {"0": "음료", "1": "12,000"}],
             "t0.r8-11": [{"0": "김철수", "1": "123-45", "2": "5,000"},
                          {"0": "이영희", "1": "678-90", "2": "5,000"},
                          {"2": "5,000"}]},
}  # fmt: skip


def data_of(raw: dict = RAW):
    return parse_input(기안(), raw).data


# ---------------------------------------------------------------- 대상 칸·입력값
def test_서술형_글_칸만_대상이다():
    got = [(t.kind, t.key, t.label) for t in dr.targets(기안())]
    assert got == [("field", "content_body", "본문"), ("cell", "t0.r1.c1", "추진 목적")]
    assert dr.targets(기안())[0].example.startswith("1. 관련:")


def test_입력값은_채운_칸과_내역이고_명단은_인원·합계만():
    facts = {f.key: (f.label, f.value) for f in dr.facts(기안(), data_of(), "content_body")}
    assert facts == {
        "docnumber": ("문서번호", "산학협력단-2026001"),
        "t0.r0.c1": ("사업명", "캡스톤 디자인"),
        "t0.r2.c1": ("회의 일자", "2026. 10. 30."),
        "t0.r5-6[0]": ("표 1 내역 1행", "품목: 다과 / 금액(원): 50,000"),
        "t0.r5-6[1]": ("표 1 내역 2행", "품목: 음료 / 금액(원): 12,000"),
        "t0.r5-6.c1.sum": ("표 1 내역 금액(원) 합계", "62,000원"),
        "t0.r8-11.count": ("표 1 명단 인원", "2명"),  # 금액만 미리 적힌 줄은 사람이 아니다
        "t0.r8-11.c2.sum": ("표 1 명단 교통비(원) 합계", "10,000원"),
    }
    text = str(facts)
    assert "김철수" not in text and "110-123" not in text and "123-45" not in text


def test_연락처_칸은_입력값에서_뺀다():
    """실측: 전화·팩스를 넘기자 본문 끝에 `문의 : 031-441-0000 | 웹사이트 : …` 를 붙였다."""
    s = 기안()
    s.fields += [
        FieldSpec(name="telephone", type="", placeholder="031-441-0000"),
        FieldSpec(name="email", type="", placeholder="000000@yeonsung.ac.kr"),
    ]
    s.slots.append(SlotSpec(table=0, row=4, col=1, label="전화"))
    raw = {**RAW, "fields": {**RAW["fields"], "telephone": "031-441-1234", "email": "a@b.kr"},
           "cells": {**RAW["cells"], "t0.r4.c1": "031-441-1234"}}  # fmt: skip
    keys = [f.key for f in dr.facts(s, parse_input(s, raw).data, "content_body")]
    assert "telephone" not in keys and "email" not in keys and "t0.r4.c1" not in keys
    assert "t0.r0.c1" in keys  # 사업명은 그대로


def test_초안을_쓸_칸은_입력값에서_뺀다():
    raw = {**RAW, "cells": {**RAW["cells"], "t0.r1.c1": "기존 목적"}}
    keys = [f.key for f in dr.facts(기안(), data_of(raw), "t0.r1.c1")]
    assert "t0.r1.c1" not in keys
    assert "t0.r1.c1" in [f.key for f in dr.facts(기안(), data_of(raw), "content_body")]


def test_요청에_입력값·서식_예시·요청을_넣고_재생성은_온도를_올린다():
    s = 기안()
    t = dr.targets(s)[0]
    req = dr.build_request(s, t, dr.facts(s, data_of(), t.key), "", "짧게", TODAY)
    assert "- 사업명: 캡스톤 디자인" in req.prompt
    assert "서식 예시 (형식만 참고):\n1. 관련:" in req.prompt
    assert req.prompt.endswith("요청:\n짧게")
    assert req.temperature == 0
    assert req.schema["required"] == ["text"]
    again = dr.build_request(s, t, [], "지금 글", "", TODAY, retry=True)
    assert again.temperature > 0 and "지금 적힌 글 (고쳐 쓴다):\n지금 글" in again.prompt


# ---------------------------------------------------------------- 인용 검사
def test_입력값을_그대로_인용하면_경고가_없다():
    text = ("1. 관련: 캡스톤 디자인\n2. 2026. 10. 30. 회의를 열고자 함. "
            "다과 50,000원, 음료 12,000원, "
            "학생 2명 교통비 합계 10,000원을 집행하고자 함.")  # fmt: skip
    out = dr.draft(기안(), RAW, "content_body", FakeLlm({"text": text}), today=TODAY)
    assert out.text == text and out.issues == []
    assert [f.key for f in out.cited] == [
        "t0.r0.c1", "t0.r2.c1", "t0.r5-6[0]", "t0.r5-6[1]", "t0.r8-11.count", "t0.r8-11.c2.sum"
    ]  # fmt: skip


def test_입력값에_없는_금액·인원·날짜는_DRAFT_UNCITED():
    text = "2026. 11. 3. 회의. 학생 5명, 총 70,000원 집행. 1. 목적 2. 내용"
    out = dr.draft(기안(), RAW, "content_body", FakeLlm({"text": text}), today=TODAY)
    [issue] = out.issues
    assert (issue.code, issue.severity) == (dr.DRAFT_UNCITED, "warning")
    assert issue.where == "content_body"
    assert "70,000원, 5명, 2026. 11. 03." in issue.message  # 목록 번호(1. 2.)는 보지 않는다


def test_초안을_못_만드는_경우():
    with pytest.raises(ValueError, match="초안을 만드는 칸이 아닙니다"):
        dr.draft(기안(), RAW, "t0.r0.c1", FakeLlm({"text": "x"}))
    with pytest.raises(ValueError, match="먼저 채우세요"):
        dr.draft(기안(), {}, "content_body", FakeLlm({"text": "x"}))
    with pytest.raises(ValueError, match="1,000자"):
        dr.draft(기안(), RAW, "content_body", FakeLlm({}), instruction="가" * 1001)
    with pytest.raises(LlmError, match="빈 초안"):
        dr.draft(기안(), RAW, "content_body", FakeLlm({"text": "  "}))


# ---------------------------------------------------------------- 채택 뒤 점검 (ED_05)
def test_채택한_칸만_입력값과_어긋나면_경고한다():
    body = "다과 50,000원을 집행하고자 함."
    raw = {**RAW, "fields": {**RAW["fields"], "content_body": body}}
    assert dr.check(기안(), raw, data_of(raw), TODAY) == []  # 채택하지 않은 칸은 보지 않는다
    raw["_drafted"] = ["content_body"]
    assert dr.check(기안(), raw, data_of(raw), TODAY) == []
    # 입력값을 고치면 본문과 어긋난다
    raw["rows"] = {**RAW["rows"], "t0.r5-6": [{"0": "다과", "1": "40,000"}]}
    [issue] = dr.check(기안(), raw, data_of(raw), TODAY)
    assert issue.where == "content_body" and "50,000원" in issue.message


# ---------------------------------------------------------------- API
PLAN = "test/2026_AID/2. 계획서/[AID 서식 2-01호] 사업 계획서.hwpx"


@pytest.fixture
def client(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    path = settings.templates / PLAN
    path.parent.mkdir(parents=True, exist_ok=True)
    hf.build(path, hf.section(tbl(3, 2, (0, 0, "사업명"), (0, 1, "○○○ 사업"),
                                  (1, 0, "추진 목적"), (1, 1, ""),
                                  (2, 0, "총 사업비"), (2, 1, "000원"))))  # fmt: skip
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    llm = FakeLlm({"text": "캡스톤 사업의 총 사업비 300,000원을 집행하고자 함."})
    app = create_app(settings, converter=None, llm=llm)
    fx.make_user(app.state.sessions)
    with TestClient(app) as c:
        fx.login(c)
        c.llm = llm
        yield c


def test_초안_칸을_알려_주고_초안을_돌려준다(client):
    [form] = client.get("/api/forms", params={"q": "사업 계획서"}).json()["items"]
    doc = client.post("/api/documents", json={"form_id": form["id"]}).json()["id"]
    targets = client.get(f"/api/documents/{doc}/form").json()["draft_targets"]
    assert targets == [{"where": "t0.r1.c1", "label": "추진 목적"}]
    raw = {"cells": {"t0.r0.c1": "캡스톤", "t0.r2.c1": "200,000원"}}
    body = {"target": "t0.r1.c1", "input": raw, "instruction": "한 문장"}
    r = client.post(f"/api/documents/{doc}/draft", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["label"] == "추진 목적"
    assert [i["code"] for i in out["issues"]] == [dr.DRAFT_UNCITED]  # 300,000원은 입력값에 없다
    assert [c["key"] for c in out["cited"]] == ["t0.r0.c1"]
    assert client.llm.requests[0].prompt.endswith("요청:\n한 문장")
    assert client.get(f"/api/documents/{doc}").json()["input"] == {}  # 저장하지 않는다

    # 채택해 저장하면 검증이 계속 본다 (ED_05)
    saved = {**raw, "cells": {**raw["cells"], "t0.r1.c1": out["text"]}, "_drafted": ["t0.r1.c1"]}
    assert client.put(f"/api/documents/{doc}/input", json=saved).status_code == 200
    issues = client.post(f"/api/documents/{doc}/validate").json()["issues"]
    assert [(i["code"], i["where"]) for i in issues] == [(dr.DRAFT_UNCITED, "t0.r1.c1")]
    fixed = {**saved, "cells": {**saved["cells"], "t0.r2.c1": "300,000원"}}
    checked = client.post(f"/api/documents/{doc}/check", json=fixed).json()["issues"]
    assert checked == []


def test_초안_칸이_아니면_400(client):
    [form] = client.get("/api/forms", params={"q": "사업 계획서"}).json()["items"]
    doc = client.post("/api/documents", json={"form_id": form["id"]}).json()["id"]
    body = {"target": "t0.r0.c1", "input": {"cells": {"t0.r2.c1": "1원"}}}
    r = client.post(f"/api/documents/{doc}/draft", json=body)
    assert r.status_code == 400


def test_빈칸_표시_금액은_늘_경고한다():
    """실측: 예산을 모르자 0,000,000원을 썼다. 문서번호의 000 때문에 놓쳤었다."""
    raw = {**RAW, "fields": {"docnumber": "산학협력단-000000"}}
    text = "예산: 0,000,000원 (추후 확정 예정)"
    out = dr.draft(기안(), raw, "content_body", FakeLlm({"text": text}), today=TODAY)
    assert "0원" in out.issues[0].message


def test_고쳐_쓰기_전의_글도_인용_근거다():
    """실측: 기안문은 금액·인원을 본문에만 적는다. 다듬은 글의 320,000원·32명을 경고했었다."""
    body = "- 참석자: 학생 32명\n- 예산: 다과비 320,000원"
    raw = {"fields": {"content_body": body}}
    text = "학생 32명이 참석하며 다과비 320,000원을 집행하고자 함."
    out = dr.draft(기안(), raw, "content_body", FakeLlm({"text": text}), instruction="다듬어",
                   today=TODAY)  # fmt: skip
    assert out.issues == [] and out.basis == f"{body}\n다듬어"
    # 채택 뒤 점검도 같은 근거로 본다. 근거가 없는 예전 모양(칸 목록)이면 경고한다
    adopted = {"fields": {"content_body": text}, "_drafted": {"content_body": out.basis}}
    assert dr.check(기안(), adopted, data_of(adopted), TODAY) == []
    old = {**adopted, "_drafted": ["content_body"]}
    assert len(dr.check(기안(), old, data_of(old), TODAY)) == 1


# ---------------------------------------------------------------- 글로 채우기 + 초안
class SeqLlm:
    """정해 둔 답을 차례로 돌려준다. 답이 예외면 던진다."""

    def __init__(self, *answers: dict | Exception) -> None:
        self.answers = list(answers)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LlmResult(answer, "fake", 0.5)


PROMPT = "캡스톤 디자인 회의를 2026. 10. 30. 에 연다. 다과비 50,000원. 문서번호 산학협력단-2026001"
EXTRACTED = {
    "fields": {"docnumber": "산학협력단-2026001"},
    "cells": {"t0.r0.c1": "캡스톤 디자인", "t0.r2.c1": "2026. 10. 30."},
    "rows": {},
}


def test_글로_채우면_서술형_칸은_묻지_않고_초안이_쓴다():
    """실측: 옮겨 적기 요청이 본문을 빈 칸으로 두거나 글 전체를 그대로 넣었다."""
    body = "캡스톤 디자인 회의를 열고 다과비 50,000원을 집행하고자 함."
    llm = SeqLlm(EXTRACTED, {"text": body}, {"text": "회의 운영 70,000원"})
    out = dr.prefill_with_drafts(기안(), PROMPT, llm, today=TODAY)
    props = llm.requests[0].schema["properties"]
    assert "content_body" not in props["fields"]["properties"]
    assert "t0.r1.c1" not in props["cells"]["properties"]
    # 초안은 채운 값을 입력값으로, 사용자의 글을 요청으로 받는다
    assert "- 사업명: 캡스톤 디자인" in llm.requests[1].prompt
    assert llm.requests[1].prompt.endswith(f"요청:\n{PROMPT}")
    assert out.input["fields"]["content_body"] == body
    assert out.input["cells"]["t0.r1.c1"] == "회의 운영 70,000원"
    assert out.drafted == {"content_body": PROMPT, "t0.r1.c1": PROMPT}
    assert [f.target.key for f in out.filled][-2:] == ["content_body", "t0.r1.c1"]
    # 50,000원은 글에 있어 괜찮고, 70,000원은 어디에도 없다
    assert [(i.code, i.location.id) for i in out.issues] == [(dr.DRAFT_UNCITED, "t0.r1.c1")]
    assert out.elapsed == 1.5


def test_초안_하나가_실패하면_그_칸만_비운다():
    llm = SeqLlm(EXTRACTED, LlmError("시간 초과"), {"text": "회의 목적"})
    out = dr.prefill_with_drafts(기안(), PROMPT, llm, today=TODAY)
    assert "content_body" not in out.input["fields"]
    assert out.input["cells"]["t0.r1.c1"] == "회의 목적"
    assert out.drafted == {"t0.r1.c1": PROMPT}


def test_서술형_칸만_있으면_글로_바로_초안을_쓴다():
    s = FormSchema(title="기안", fields=[FieldSpec(name="content_body", type="")])
    llm = SeqLlm({"text": "회의를 열고자 함."})
    out = dr.prefill_with_drafts(s, "회의를 연다", llm, today=TODAY)
    assert len(llm.requests) == 1 and llm.requests[0].prompt.endswith("요청:\n회의를 연다")
    assert out.input["fields"] == {"content_body": "회의를 열고자 함."}


def test_글로_채우기_API_는_초안_칸과_근거를_돌려준다(client):
    [form] = client.get("/api/forms", params={"q": "사업 계획서"}).json()["items"]
    doc = client.post("/api/documents", json={"form_id": form["id"]}).json()["id"]
    prompt = "캡스톤 사업, 총 사업비 300,000원"
    r = client.post(f"/api/documents/{doc}/prefill", json={"prompt": prompt})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["drafted"] == {"t0.r1.c1": prompt}
    assert out["input"]["cells"]["t0.r1.c1"].startswith("캡스톤 사업의")
    assert out["issues"] == []  # 300,000원은 글에 있다


def test_행마다_적는_사유_칸은_초안을_쓰지_않고_옮겨_적는다():
    """실측: 정산서 집행항목마다의 `증감 사유` 11칸에 지어낸 긴 글을 썼다."""
    s = FormSchema(
        title="정산서",
        tables=[TableSpec(index=0, rows=3, cols=4)],
        slots=[SlotSpec(table=0, row=0, col=1, label="추진 목적"),
               SlotSpec(table=0, row=1, col=3, label="증감 사유"),
               SlotSpec(table=0, row=2, col=3, label="증감 사유")],
    )  # fmt: skip
    extracted = {"fields": {}, "cells": {"t0.r1.c3": "강사 변경"}, "rows": {}}
    llm = SeqLlm(extracted, {"text": "교육과정 개편을 위함."})
    out = dr.prefill_with_drafts(s, "강사료는 강사 변경으로 줄었다", llm, today=TODAY)
    asked = llm.requests[0].schema["properties"]["cells"]["properties"]
    assert set(asked) == {"t0.r1.c3", "t0.r2.c3"}
    assert len(llm.requests) == 2  # 초안은 추진 목적 하나
    assert out.input["cells"] == {"t0.r1.c3": "강사 변경", "t0.r0.c1": "교육과정 개편을 위함."}
    assert out.drafted == {"t0.r0.c1": "강사료는 강사 변경으로 줄었다"}
    # 편집 화면의 [초안 쓰기] 는 그대로 쓸 수 있다
    assert [t.key for t in dr.targets(s)] == ["t0.r0.c1", "t0.r1.c3", "t0.r2.c3"]


def test_적지_않은_붙임은_경고한다():
    """실측: 글에 없는 `붙임 : 개선 과제 운영 계획서 1부.` 를 지어냈다."""
    text = "캡스톤 디자인 회의를 열고자 함.\n\n붙임 : 회의 계획서 1부.  끝."
    out = dr.draft(기안(), RAW, "content_body", FakeLlm({"text": text}), today=TODAY)
    assert "붙임" in out.issues[0].message
    asked = dr.draft(기안(), RAW, "content_body", FakeLlm({"text": text}),
                     instruction="회의 계획서를 첨부함", today=TODAY)  # fmt: skip
    assert asked.issues == []


def test_붙인_문서를_초안에_넘기고_붙임의_근거로_본다():
    text = "캡스톤 디자인 회의를 열고자 함.\n\n붙임  사업 계획서 1부.  끝."
    llm = FakeLlm({"text": text})
    extra = dr.attachment_facts(["사업 계획서"])
    out = dr.draft(기안(), RAW, "content_body", llm, today=TODAY, extra=extra)
    assert "- 붙임 문서: 붙임 1. 사업 계획서 1부" in llm.requests[0].prompt
    assert out.issues == []
    assert dr.attachment_facts([]) == []
