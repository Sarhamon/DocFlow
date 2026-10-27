"""자연어 일괄 입력 (M10, F2-c): 글로 LLM 이 칸을 채운다. LLM 은 가짜로 바꿔 끼운다."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import date

import pytest
from fastapi.testclient import TestClient

from docflow.adapters.ollama import OllamaClient
from docflow.config import Settings
from docflow.llm import LlmError, LlmRequest, LlmResult
from docflow.schema.model import (
    FieldSpec,
    FormSchema,
    RepeatColumn,
    RepeatSpec,
    SlotSpec,
    TableSpec,
    Variant,
)
from docflow.service.catalog import sync_templates
from docflow.service.db import connect
from docflow.service.prefill import (
    LLM_UNCITED,
    MAX_PROMPT,
    build_request,
    fix_weekday,
    korean_amount,
    plan,
    prefill,
    read_answer,
    table_view,
)
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import service_fixtures as fx

TODAY = date(2026, 10, 27)


class FakeLlm:
    """정해 둔 답을 돌려주고 받은 요청을 적어 둔다. 답이 예외면 던진다."""

    def __init__(self, answer: dict | Exception) -> None:
        self.answer = answer
        self.requests: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResult:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return LlmResult(self.answer, "fake", 0.5)


def 회의비() -> FormSchema:
    """회의명·일시 칸, 다과 내역(명단 아님), 참석자 명단, 계좌 칸, 변형 2개."""
    return FormSchema(
        title="회의비 집행 계획서",
        tables=[TableSpec(index=0, rows=12, cols=4), TableSpec(index=1, rows=2, cols=2)],
        variants=[Variant(index=0, label="1-01호", first_table=0, last_table=0),
                  Variant(index=1, label="1-02호", first_table=1, last_table=1)],
        fields=[FieldSpec(name="docnumber", type="", placeholder="산학협력단-0000"),
                FieldSpec(name="apb1_sign", type="", role="approval")],
        slots=[SlotSpec(table=0, row=0, col=1, label="회의명", placeholder="○○○ 회의"),
               SlotSpec(table=0, row=1, col=1, label="일시", input_type="date",
                        placeholder="2000.00.00"),
               SlotSpec(table=0, row=2, col=1, label="입금 계좌번호"),
               SlotSpec(table=0, row=3, col=1, label="합계", role="computed"),
               SlotSpec(table=1, row=0, col=1, label="다른 변형 칸")],
        repeats=[
            RepeatSpec(table=0, header_rows=[4], start_row=5, end_row=6,
                       columns=[RepeatColumn(col=0, label="품목"),
                                RepeatColumn(col=1, label="금액(원)", input_type="amount")]),
            RepeatSpec(table=0, header_rows=[7], start_row=8, end_row=11,
                       columns=[RepeatColumn(col=0, label="성명"),
                                RepeatColumn(col=1, label="학번")]),
        ],
    )  # fmt: skip


# ---------------------------------------------------------------- 채울 칸
def test_입력_칸만_넘기고_명단·계좌·다른_변형은_뺀다():
    p = plan(회의비(), 0)
    assert [t.key for t in p.fields] == ["docnumber"]  # 결재란은 빠진다
    assert [t.key for t in p.cells] == ["t0.r0.c1", "t0.r1.c1"]
    assert list(p.rows) == ["t0.r5-6"]  # 참석자 명단은 통째로 빠진다
    assert [t.label for t in p.rows["t0.r5-6"]] == ["품목", "금액(원)"]
    assert p.skipped == ["입금 계좌번호", "명단 (성명, 학번)"]


def test_요청의_스키마는_입력_JSON_모양이고_칸_이름을_설명에_넣는다():
    s = 회의비()
    req = build_request(s, plan(s, 0), "  중간점검 회의  ", TODAY)
    props = req.schema["properties"]
    assert set(props) == {"fields", "cells", "rows"}
    assert props["cells"]["properties"]["t0.r1.c1"]["description"] == "일시 [date] 예: 2000.00.00"
    assert props["rows"]["properties"]["t0.r5-6"]["type"] == "array"
    assert req.schema["additionalProperties"] is False
    # 모든 칸이 필수다. 선택이면 7.8B 가 {} 로 답한다
    assert req.schema["required"] == ["fields", "cells", "rows"]
    assert props["cells"]["required"] == ["t0.r0.c1", "t0.r1.c1"]
    assert props["rows"]["properties"]["t0.r5-6"]["items"]["required"] == ["0", "1"]
    assert req.prompt.startswith("[사용자의 글]\n중간점검 회의\n")  # 글이 맨 앞
    assert "[오늘] 2026. 10. 27." in req.prompt
    assert "성명" not in req.prompt and "계좌" not in req.prompt
    assert req.temperature == 0


def test_결재란_칸은_빼고_같은_이름의_사본은_한_번만_묻는다():
    s = 회의비().model_copy(update={"variants": [], "slots": [
        SlotSpec(table=0, row=0, col=1, label="회의명"),
        SlotSpec(table=0, row=1, col=1, label="결 재"),
        SlotSpec(table=1, row=0, col=1, label="회 의 명"),  # 학과용·부서용 두 벌
    ]})  # fmt: skip
    p = plan(s)
    assert [t.key for t in p.cells] == ["t0.r0.c1"]
    assert [t.key for t in p.copies["t0.r0.c1"]] == ["t1.r0.c1"]
    raw, filled = read_answer(p, {"cells": {"t0.r0.c1": "중간점검"}})
    # 사본에는 쓰지 않는다. 비운 사본은 원본 값을 쓰므로 원본을 고치면 따라간다
    assert raw["cells"] == {"t0.r0.c1": "중간점검"}
    assert [f.location.key for f in filled] == ["t0.r0.c1"]


@pytest.mark.parametrize(
    ("value", "want"),
    [("2026년 10월 30일(월)", "2026년 10월 30일(금)"),
     ("2026. 11. 5.(월요일)", "2026. 11. 5.(목요일)"),
     ("2026.02.30(월)", "2026.02.30(월)"),  # 달력에 없는 날은 그대로 (DATE_INVALID 몫)
     ("10월 30일(월)", "10월 30일(월)")],  # 연도가 없으면 계산하지 않는다
)  # fmt: skip
def test_요일은_코드가_계산한다(value, want):
    assert fix_weekday(value) == want


def test_답에서_계획에_있는_칸의_값만_꺼낸다():
    p = plan(회의비(), 0)
    raw, filled = read_answer(p, {
        "fields": {"docnumber": "  산학협력단-2026001 ", "apb1_sign": "남"},
        "cells": {"t0.r0.c1": "중간점검 회의", "t0.r1.c1": "", "t0.r2.c1": "123-456",
                  "t9.r9.c9": "없는 칸"},
        "rows": {"t0.r5-6": [{"0": "다과", "1": 50000}, {"0": "", "1": ""}, "이상한 행"],
                 "t0.r8-11": [{"0": "김철수"}]},
        "extra": "무시",
    })  # fmt: skip
    assert raw == {
        "fields": {"docnumber": "산학협력단-2026001"},
        "cells": {"t0.r0.c1": "중간점검 회의"},
        "rows": {"t0.r5-6": [{"0": "다과", "1": "50000"}]},
    }
    assert [(f.location.key, f.value) for f in filled] == [
        ("docnumber", "산학협력단-2026001"), ("t0.r0.c1", "중간점검 회의"),
        ("t0.r5-6[0].c0", "다과"), ("t0.r5-6[0].c1", "50000"),
    ]  # fmt: skip


def test_답이_엉뚱한_모양이어도_빈_입력이다():
    answer = {"fields": "?", "cells": [], "rows": {"t0.r5-6": 1}}
    raw, filled = read_answer(plan(회의비(), 0), answer)
    assert raw == {"fields": {}, "cells": {}, "rows": {}} and filled == []


# ---------------------------------------------------------------- 인용 검사
@pytest.mark.parametrize(
    ("text", "want"),
    [("50만", 500_000), ("1억 2천만", 120_000_000), ("3천5백", 3_500), ("천", 1_000),
     ("1.5억", 150_000_000), ("12만 3천", 123_000), ("5,000", None)],
)  # fmt: skip
def test_한글_단위_금액을_읽는다(text, want):
    assert korean_amount(text) == want


def run(answer: dict, text: str, schema: FormSchema | None = None):
    return prefill(schema or 회의비(), text, FakeLlm(answer), variant="1-01호", today=TODAY)


def test_글에_있는_금액·날짜는_경고하지_않는다():
    out = run(
        {"cells": {"t0.r1.c1": "2026. 10. 30."},
         "rows": {"t0.r5-6": [{"0": "다과", "1": "500,000"}, {"0": "음료", "1": "12000"}]}},
        "10월 30일 회의. 다과 50만원, 음료 12,000원",
    )  # fmt: skip
    assert out.issues == []
    assert out.model == "fake"


def test_글에_없는_금액·날짜·인원·낱말은_LLM_UNCITED_경고():
    out = run(
        {"cells": {"t0.r0.c1": "예산 300,000원 회의 5명", "t0.r1.c1": "2026. 11. 3."},
         "rows": {"t0.r5-6": [{"0": "다과", "1": "50,000"}, {"0": "음료", "1": "70,000"}]}},
        "10월 30일 회의 4명. 다과 50,000원",
    )  # fmt: skip
    assert [(i.code, i.severity, i.where) for i in out.issues] == [
        (LLM_UNCITED, "warning", "t0.r0.c1"),
        (LLM_UNCITED, "warning", "t0.r1.c1"),
        (LLM_UNCITED, "warning", "t0.r5-6[1].c0"),
        (LLM_UNCITED, "warning", "t0.r5-6[1].c1"),
    ]
    assert "5, 300,000" in out.issues[0].message  # 인원도 본다
    assert "2026. 11. 03." in out.issues[1].message
    assert "'음료'" in out.issues[2].message  # 글에 없는 낱말
    assert out.issues[3].location.row == 6  # 반복영역 두 번째 행


def test_건물_이름을_보고_지어낸_학과_이름을_잡는다():
    """실측: '공학관 301호' 를 보고 주관학과에 '공학과' 를 넣었다."""
    out = run({"cells": {"t0.r0.c1": "공학과"}}, "공학관 301호에서 중간점검 회의")
    assert [i.where for i in out.issues] == ["t0.r0.c1"]
    ok = run({"cells": {"t0.r0.c1": "캡스톤디자인 중간점검 회의"}},
             "캡스톤디자인 중간점검 회의를 연다")  # fmt: skip
    assert ok.issues == []  # 띄어쓰기·토씨가 달라도 글에 있으면 괜찮다


def test_연도가_다르면_월·일이_같아도_경고():
    out = run({"cells": {"t0.r1.c1": "2025. 10. 30."}}, "10월 30일 회의")
    assert len(out.issues) == 1
    assert not run({"cells": {"t0.r1.c1": "2025. 10. 30."}}, "2025년 10월 30일 회의").issues


def test_빈_글·너무_긴_글·채울_칸이_없는_서식은_거절():
    with pytest.raises(ValueError, match="적으세요"):
        run({}, "   ")
    with pytest.raises(ValueError, match=f"{MAX_PROMPT:,}자"):
        run({}, "가" * (MAX_PROMPT + 1))
    only_roster = FormSchema(
        title="명단", tables=[TableSpec(index=0, rows=3, cols=2)],
        repeats=[RepeatSpec(table=0, header_rows=[0], start_row=1, end_row=2,
                            columns=[RepeatColumn(col=0, label="성명")])],
    )  # fmt: skip
    with pytest.raises(ValueError, match="입금계좌 확인서"):
        prefill(only_roster, "김철수", FakeLlm({}), today=TODAY)


def test_변형을_고르면_그_변형의_칸만_넘긴다():
    llm = FakeLlm({})
    prefill(회의비(), "회의", llm, variant="1-02호", today=TODAY)
    cells = llm.requests[0].schema["properties"]["cells"]["properties"]
    assert list(cells) == ["t1.r0.c1"]


# ---------------------------------------------------------------- Ollama 어댑터
class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_of(payload, sent: list | None = None):
    def open_(req, timeout):
        if sent is not None:
            sent.append((req, timeout))
        if isinstance(payload, Exception):
            raise payload
        return Reply(json.dumps(payload).encode() if not isinstance(payload, bytes) else payload)

    return open_


def test_ollama_에_스키마를_주고_답을_JSON_으로_읽는다():
    sent: list = []
    content = json.dumps({"cells": {"t0.r0.c1": "회의"}}, ensure_ascii=False)
    client = OllamaClient("http://gpu:11434/", "exaone3.5:7.8b", timeout=9,
                          opener=opener_of({"model": "exaone3.5:7.8b",
                                            "message": {"content": content}}, sent))  # fmt: skip
    result = client.complete(LlmRequest("규칙", "글", {"type": "object"}))
    assert result.data == {"cells": {"t0.r0.c1": "회의"}}
    assert result.model == "exaone3.5:7.8b"
    req, timeout = sent[0]
    assert req.full_url == "http://gpu:11434/api/chat" and timeout == 9
    body = json.loads(req.data)
    assert body["format"] == {"type": "object"}
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.0, "num_ctx": 8192}  # 기본 4096 은 잘린다
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    ("payload", "match"),
    [(urllib.error.URLError("connection refused"), "연결하지 못했다"),
     (TimeoutError(), "연결하지 못했다"),
     (b"<html>", "JSON 이 아니다"),
     ({"message": {"content": "그냥 글"}}, "JSON 으로 읽지 못했다"),
     ({"message": {"content": "[1, 2]"}}, "JSON 객체가 아니다")],
)  # fmt: skip
def test_ollama_가_안_되면_LlmError(payload, match):
    client = OllamaClient("http://gpu:11434", "m", opener=opener_of(payload))
    with pytest.raises(LlmError, match=match):
        client.complete(LlmRequest("규칙", "글"))


def test_ollama_HTTP_오류는_서버가_준_사유를_보여_준다():
    err = urllib.error.HTTPError("http://gpu", 404, "Not Found", {},
                                 io.BytesIO(b'{"error": "model \\"m\\" not found"}'))  # fmt: skip
    client = OllamaClient("http://gpu:11434", "m", opener=opener_of(err))
    with pytest.raises(LlmError, match='404: model "m" not found'):
        client.complete(LlmRequest("규칙", "글"))


# ---------------------------------------------------------------- API
def make_client(tmp_path, llm):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    app = create_app(settings, converter=None, llm=llm)
    fx.make_user(app.state.sessions)
    c = TestClient(app)
    fx.login(c)
    return c


def new_doc(client, q: str) -> int:
    [form] = client.get("/api/forms", params={"q": q}).json()["items"]
    return client.post("/api/documents", json={"form_id": form["id"]}).json()["id"]


def test_글로_채운_값을_돌려주고_저장하지는_않는다(tmp_path):
    llm = FakeLlm({"cells": {"t0.r0.c1": "중간점검 회의", "t0.r1.c1": "2026. 11. 3."}})
    client = make_client(tmp_path, llm)
    doc = new_doc(client, "회의계획서")
    r = client.post(f"/api/documents/{doc}/prefill", json={"prompt": "10월 30일 중간점검 회의"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["input"]["cells"] == {"t0.r0.c1": "중간점검 회의", "t0.r1.c1": "2026. 11. 3."}
    assert [(f["where"], f["label"]) for f in out["filled"]] == [("t0.r0.c1", "회의명"),
                                                               ("t0.r1.c1", "일시")]  # fmt: skip
    assert [(i["code"], i["where"]) for i in out["issues"]] == [(LLM_UNCITED, "t0.r1.c1")]
    assert out["model"] == "fake"
    assert client.get(f"/api/documents/{doc}").json()["input"] == {}  # 저장하지 않는다


def test_교통비_명단은_LLM_에_넘기지_않는다(tmp_path):
    llm = FakeLlm({"cells": {"t0.r0.c1": "컴퓨터과"}, "rows": {"t0.r3-5": [{"1": "김철수"}]}})
    client = make_client(tmp_path, llm)
    doc = new_doc(client, "교통비")
    out = client.post(f"/api/documents/{doc}/prefill", json={"prompt": "컴퓨터과"}).json()
    assert out["input"] == {"fields": {}, "cells": {"t0.r0.c1": "컴퓨터과"}, "rows": {}}
    assert out["skipped"] and out["skipped"][0].startswith("명단")
    assert "rows" not in llm.requests[0].schema["properties"]


def test_LLM_을_못_쓰면_503(tmp_path):
    client = make_client(tmp_path, FakeLlm(LlmError("LLM 서버에 연결하지 못했다: refused")))
    doc = new_doc(client, "회의계획서")
    r = client.post(f"/api/documents/{doc}/prefill", json={"prompt": "회의"})
    assert r.status_code == 503
    assert "직접 채우세요" in r.json()["detail"]


def test_LLM_을_끄면_503(tmp_path):
    client = make_client(tmp_path, None)
    doc = new_doc(client, "회의계획서")
    assert client.post(f"/api/documents/{doc}/prefill", json={"prompt": "회의"}).status_code == 503


def test_빈_글은_422(tmp_path):
    client = make_client(tmp_path, FakeLlm({}))
    doc = new_doc(client, "회의계획서")
    assert client.post(f"/api/documents/{doc}/prefill", json={"prompt": ""}).status_code == 422


def test_남의_문서는_채울_수_없다(tmp_path):
    client = make_client(tmp_path, FakeLlm({}))
    doc = new_doc(client, "회의계획서")
    fx.make_user(client.app.state.sessions, "other")
    fx.login(client, "other")
    assert client.post(f"/api/documents/{doc}/prefill", json={"prompt": "회의"}).status_code == 404


def test_서식에_박힌_값은_묻지_않고_베낀_값은_버린다():
    """실측: 주소를 그대로 옮기고, 문서번호에 자리표시자, 칸 이름을 값으로 적었다."""
    s = 회의비().model_copy(update={"variants": [], "repeats": [], "fields": [
        FieldSpec(name="address", type="", placeholder="경기도 안양시 만안구 양화로 37번길 34"),
        FieldSpec(name="docnumber", type="", placeholder="○○○○처-000000"),
        FieldSpec(name="content_body", type="", placeholder="1. 관련: \n2. 위와 관련하여"),
    ], "slots": [SlotSpec(table=0, row=0, col=1, label="연성대학교"),
                 SlotSpec(table=0, row=1, col=1, label="회의일자", placeholder="2000.00.00")]})
    p = plan(s)
    assert [t.key for t in p.fields] == ["docnumber", "content_body"]  # 주소는 묻지 않는다
    raw, _ = read_answer(p, {
        "fields": {"docnumber": "○○○○처-000000", "content_body": "1. 관련: 회의"},
        "cells": {"t0.r0.c1": "연성대학교", "t0.r1.c1": "2000.00.00"},
    })  # fmt: skip
    assert raw["fields"] == {"content_body": "1. 관련: 회의"}
    assert raw["cells"] == {}


def 정산표(table: int = 0, items: tuple[str, ...] = ("① 강사료", "② 회의비")) -> tuple:
    """정산서 모양: 행 제목(집행항목) x 열 제목(예산·집행). 칸 이름은 추출이 행 제목만 붙인다."""
    grid = [["집행항목", "예산금액", "현집행금액"]] + [[i, "", ""] for i in items]
    slots = [SlotSpec(table=table, row=r, col=c, label=item, input_type="amount")
             for r, item in enumerate(items, 1) for c in (1, 2)]  # fmt: skip
    return TableSpec(index=table, rows=len(grid), cols=3, grid=grid), slots


def test_칸_이름에_행_제목과_열_제목을_함께_쓴다():
    """실측: 예산·집행 칸이 모두 '① 강사료' 라 모델이 엉뚱한 칸에 썼다."""
    table, slots = 정산표()
    p = plan(FormSchema(title="정산서", tables=[table], slots=slots))
    assert [t.label for t in p.cells] == [
        "① 강사료 / 예산금액", "① 강사료 / 현집행금액",
        "② 회의비 / 예산금액", "② 회의비 / 현집행금액",
    ]
    assert p.copies == {}  # 같은 표 안에서 이름이 같아도 사본이 아니다


def test_열_제목은_빈_칸을_건너_다른_블록에서_가져오지_않는다():
    """실측: 회의비용 칸이 위쪽 명단 표 머리의 '성명' 을 열 제목으로 집었다."""
    grid = [["", "성명", "직위"], ["", "", ""], ["회의비용", "", ""]]
    s = FormSchema(title="회의", tables=[TableSpec(index=0, rows=3, cols=3, grid=grid)],
                   slots=[SlotSpec(table=0, row=2, col=1, label="회의비용")])  # fmt: skip
    assert [t.label for t in plan(s).cells] == ["회의비용"]


def test_칸_구성이_통째로_같은_표만_사본이다():
    """실측: 강사료 표와 전문가활용비 표의 칸이 행 값 '보조' 로 이름이 같아 묶였다."""
    t0, s0 = 정산표(0)
    t1, s1 = 정산표(1)  # 학과용·부서용처럼 똑같은 표
    t2, s2 = 정산표(2, ("① 강사료", "③ 재료비"))  # 한 줄이 다른 표
    p = plan(FormSchema(title="정산서", tables=[t0, t1, t2], slots=s0 + s1 + s2))
    assert {k: [c.key for c in v] for k, v in p.copies.items()} == {
        "t0.r1.c1": ["t1.r1.c1"], "t0.r1.c2": ["t1.r1.c2"],
        "t0.r2.c1": ["t1.r2.c1"], "t0.r2.c2": ["t1.r2.c2"],
    }  # fmt: skip
    assert {t.table for t in p.cells} == {0, 2}


def test_프롬프트에_표_모양을_넣고_채울_자리를_키로_표시한다():
    t0, s0 = 정산표(0)
    t1, s1 = 정산표(1)
    s = FormSchema(title="정산서", tables=[t0, t1], slots=s0 + s1)
    view = table_view(s, plan(s))
    assert view == ("표 1:\n| 집행항목 | 예산금액 | 현집행금액 |\n"
                    "| ① 강사료 | {cells.t0.r1.c1} | {cells.t0.r1.c2} |\n"
                    "| ② 회의비 | {cells.t0.r2.c1} | {cells.t0.r2.c2} |")  # fmt: skip
    assert "[서식 모양]\n표 1:" in build_request(s, plan(s), "글", TODAY).prompt
    assert table_view(s, plan(s), budget=10) == ""  # 넘치면 넣지 않는다
