"""관리자 서식 관리 API (M9, AD_01~08). 서식은 코드로 만들고 HWP 변환은 가짜 변환기로 한다.

완료 기준: 관리자가 새 서식을 올려 확인하고 공개하면 사용자 목록에 나타나고 문서를 만들 수 있다.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.convert import SIGNATURES, ConversionJob, ConversionResult
from docflow.service.catalog import sync_templates
from docflow.service.db import connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import hwpx_factory as hf
from tests import service_fixtures as fx

NAME = "[AID 서식 6-01호] 출장 신청서.hwpx"


class HwpToHwpx:
    """HWP 를 HWPX 로 바꾸는 가짜 변환기: 미리 만든 HWPX 를 복사한다."""

    def __init__(self, hwpx: Path) -> None:
        self.hwpx = hwpx

    def convert(self, job: ConversionJob) -> ConversionResult:
        result = ConversionResult(job=job, attempts=1)
        shutil.copy(self.hwpx, job.target("HWPX"))
        result.outputs["HWPX"] = job.target("HWPX")
        return result


def form_bytes(tmp_path: Path, title: str = "○○○ 출장") -> bytes:
    xml = hf.section(fx.meeting_form().replace("○○○ 회의", title))
    return hf.build(tmp_path / "upload.hwpx", xml).read_bytes()


def bundle_bytes(tmp_path: Path) -> bytes:
    table = hf.para_runs(f'<hp:run charPrIDRef="0">{fx.meeting_form()}</hp:run>')
    xml = hf.body(hf.para("[ AID 서식 4-01호 ]"), hf.para("입금계좌 확인서"), table,
                  hf.para("[ AID 서식 4-02호 ]"), hf.para("강의시간 배당표"), table)  # fmt: skip
    return hf.build(tmp_path / "bundle.hwpx", xml).read_bytes()


def make_app(tmp_path, converter=None):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    app = create_app(settings, converter=converter)
    fx.make_user(app.state.sessions)
    fx.make_user(app.state.sessions, "boss", role="admin")
    return app


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    with TestClient(app) as c:
        fx.login(c, "boss")
        yield c


@pytest.fixture
def user(app):
    with TestClient(app) as c:
        fx.login(c)
        yield c


def upload(c, data: bytes, name: str = NAME, **fields):
    form = {"program": "AID", "category_no": "6", "category": "출장비", **fields}
    return c.post("/api/admin/forms", files={"file": (name, data)}, data=form)


# ------------------------------------------------------------ 권한
def test_사용자는_서식_관리를_쓸_수_없다(user, tmp_path):
    assert user.get("/api/admin/forms").status_code == 403
    assert upload(user, form_bytes(tmp_path)).status_code == 403


# ------------------------------------------------------------ 등록 → 확인 → 공개
def test_올린_서식은_비공개로_등록되고_공개하면_사용자가_문서를_만든다(admin, user, tmp_path):
    r = upload(admin, form_bytes(tmp_path))
    assert r.status_code == 201, r.text
    [form] = r.json()["forms"]
    assert (form["form_no"], form["title"], form["program"], form["category"], form["public"]) == (
        "6-01", "출장 신청서", "AID", "출장비", False)  # fmt: skip
    fid = form["id"]

    # 사용자에게는 아직 없다. 관리자는 미리 본다
    assert user.get("/api/forms", params={"q": "출장"}).json()["total"] == 0
    assert user.get(f"/api/forms/{fid}/layout").status_code == 404
    assert user.post("/api/documents", json={"form_id": fid}).status_code == 404
    assert admin.post("/api/documents", json={"form_id": fid}).status_code == 201  # 점검용
    preview = admin.get(f"/api/forms/{fid}/layout").json()
    assert preview["schema"]["slots"][0]["placeholder"] == "○○○ 출장"
    private = admin.get("/api/admin/forms", params={"visibility": "private"}).json()
    assert [f["id"] for f in private["items"]] == [fid]

    assert admin.patch(f"/api/admin/forms/{fid}", json={"public": True}).json()["public"]
    [found] = user.get("/api/forms", params={"q": "출장"}).json()["items"]
    assert found["id"] == fid
    doc = user.post("/api/documents", json={"form_id": fid})
    assert doc.status_code == 201, doc.text


def test_같은_이름을_다시_올리면_새_버전이다(admin, tmp_path):
    [first] = upload(admin, form_bytes(tmp_path)).json()["forms"]
    again = upload(admin, form_bytes(tmp_path, "○○○ 해외출장")).json()
    assert again["report"]["updated"] == [f"uploads/AID/{Path(NAME).stem}.hwpx"]
    assert [f["id"] for f in again["forms"]] == [first["id"]]
    assert again["forms"][0]["version"] == 2


def test_묶음_파일은_서식_번호마다_따로_등록한다(admin, tmp_path):
    r = upload(admin, bundle_bytes(tmp_path), "[AID 서식 4-01~02호] 인건비 서식.hwpx")
    forms = r.json()["forms"]
    assert [(f["form_no"], f["title"], f["public"]) for f in forms] == [
        ("4-01", "입금계좌 확인서", False), ("4-02", "강의시간 배당표", False)]  # fmt: skip
    detail = admin.get(f"/api/admin/forms/{forms[0]['id']}").json()
    assert [s["form_no"] for s in detail["siblings"]] == ["4-02"]


# ------------------------------------------------------------ 수정·개정
def test_이름과_분류를_고치면_개정해도_유지된다(admin, tmp_path):
    [form] = upload(admin, form_bytes(tmp_path)).json()["forms"]
    fid = form["id"]
    body = {"title": "국내 출장 신청서", "category": "여비"}
    fixed = admin.patch(f"/api/admin/forms/{fid}", json=body)
    assert (fixed.json()["title"], fixed.json()["category"]) == ("국내 출장 신청서", "여비")
    assert admin.patch(f"/api/admin/forms/{fid}", json={"title": " "}).status_code == 400

    new = form_bytes(tmp_path, "○○○ 해외출장")
    r = admin.post(f"/api/admin/forms/{fid}/versions", files={"file": ("새 판.hwpx", new)})
    assert r.status_code == 201, r.text
    [revised] = r.json()["forms"]
    assert (revised["id"], revised["version"], revised["title"], revised["category"]) == (
        fid, 2, "국내 출장 신청서", "여비")  # fmt: skip


def test_개정해도_기존_문서는_작성_당시_버전을_쓴다(admin, user, tmp_path):
    [form] = upload(admin, form_bytes(tmp_path)).json()["forms"]
    fid = form["id"]
    admin.patch(f"/api/admin/forms/{fid}", json={"public": True})
    doc = user.post("/api/documents", json={"form_id": fid}).json()
    admin.post(f"/api/admin/forms/{fid}/versions",
               files={"file": ("x.hwpx", form_bytes(tmp_path, "○○○ 해외출장"))})  # fmt: skip
    versions = admin.get(f"/api/admin/forms/{fid}").json()["versions"]
    assert [(v["version"], v["documents"]) for v in versions] == [(1, 1), (2, 0)]
    assert user.get(f"/api/documents/{doc['id']}").json()["form"]["version"] == 1


def test_templates_서식도_관리자_목록에_있고_고칠_수_있다(admin):
    items = admin.get("/api/admin/forms", params={"q": "회의"}).json()["items"]
    assert [(f["title"], f["public"]) for f in items] == [("회의계획서", True)]
    hidden = admin.patch(f"/api/admin/forms/{items[0]['id']}", json={"public": False}).json()
    assert hidden["public"] is False


# ------------------------------------------------------------ 파일 형식
def test_HWP_는_HWPX_로_바꿔_등록한다(tmp_path):
    hwpx = hf.build(tmp_path / "converted.hwpx", hf.section(fx.meeting_form()))
    app = make_app(tmp_path / "app", converter=HwpToHwpx(hwpx))
    with TestClient(app) as c:
        fx.login(c, "boss")
        hwp = SIGNATURES["HWP"] + b" fake hwp"
        r = upload(c, hwp, "[AID 서식 6-02호] 출장 보고서.hwp")
        assert r.status_code == 201, r.text
        assert r.json()["forms"][0]["title"] == "출장 보고서"


def test_한글이_없으면_HWP_는_받지_않는다(admin):
    r = upload(admin, SIGNATURES["HWP"] + b" fake hwp", "[AID 서식 6-02호] 출장 보고서.hwp")
    assert r.status_code == 503 and "HWPX" in r.json()["detail"]


@pytest.mark.parametrize(
    ("name", "data"),
    [("서식.pdf", b"%PDF-1.7"), ("서식.hwpx", b"not a zip"), ("서식.hwp", b"PK\x03\x04 zip")],
)
def test_서식_파일이_아니면_400(admin, name, data):
    assert upload(admin, data, name).status_code == 400


# ------------------------------------------------------------ 입력폼 관리 (AD_09~12, AD_14)
def published(admin, tmp_path) -> int:
    [form] = upload(admin, form_bytes(tmp_path)).json()["forms"]
    admin.patch(f"/api/admin/forms/{form['id']}", json={"public": True})
    return form["id"]


def test_입력폼_항목은_추출한_값과_설정을_나란히_준다(admin, tmp_path):
    fid = published(admin, tmp_path)
    items = admin.get(f"/api/admin/forms/{fid}/items").json()["items"]
    got = [(i["kind"], i["key"], i["extracted"]["label"], i["extracted"]["input_type"],
            i["extracted"]["required"]) for i in items]  # fmt: skip
    assert got == [("cell", "t0.r0.c1", "회의명", "text", True),
                   ("cell", "t0.r1.c1", "일시", "date", True)]  # fmt: skip
    assert items[0]["setting"] == {"hidden": False, "required": None, "input_type": None,
                                   "label": None, "position": None}  # fmt: skip


def test_뺀_칸은_입력·검증하지_않고_서식_글자를_남긴다(admin, user, tmp_path):
    fid = published(admin, tmp_path)
    r = admin.put(f"/api/admin/forms/{fid}/items",
                  json=[{"kind": "cell", "key": "t0.r1.c1", "hidden": True},
                        {"kind": "cell", "key": "t0.r0.c1", "label": "출장 목적"}])  # fmt: skip
    assert r.status_code == 200, r.text
    doc = user.post("/api/documents", json={"form_id": fid}).json()["id"]
    form = user.get(f"/api/documents/{doc}/form").json()
    slots = {s["id"]: (s["role"], s["label"]) for s in form["schema"]["slots"]}
    assert slots == {"t0.r0.c1": ("input", "출장 목적"), "t0.r1.c1": ("fixed", "일시")}
    assert "t0.r1.c1" not in form["skeleton"]["cells"]

    user.put(f"/api/documents/{doc}/input", json={"cells": {"t0.r0.c1": "학회 참석"}})
    assert user.post(f"/api/documents/{doc}/validate").json() == {"ok": True, "issues": []}
    out = user.post(f"/api/documents/{doc}/generate", json={"formats": []}).json()
    assert out["document"]["status"] == "completed"
    assert out["leftovers"] == []  # 남긴 서식 글자(2000.00.00)는 잔여물로 알리지 않는다


def test_필수를_풀면_비워도_통과하고_형식을_바꾸면_점검이_바뀐다(admin, user, tmp_path):
    fid = published(admin, tmp_path)
    admin.put(f"/api/admin/forms/{fid}/items",
              json=[{"kind": "cell", "key": "t0.r0.c1", "required": False},
                    {"kind": "cell", "key": "t0.r1.c1", "input_type": "text"}])  # fmt: skip
    doc = user.post("/api/documents", json={"form_id": fid}).json()["id"]
    issues = user.post(f"/api/documents/{doc}/check",
                       json={"cells": {"t0.r1.c1": "다음 주 화요일"}}).json()["issues"]  # fmt: skip
    assert issues == []


def test_서식에_없는_항목이나_형식은_400(admin, tmp_path):
    fid = published(admin, tmp_path)
    bad = [[{"kind": "cell", "key": "t9.r9.c9", "hidden": True}],
           [{"kind": "cell", "key": "t0.r0.c1", "input_type": "money"}]]  # fmt: skip
    for body in bad:
        assert admin.put(f"/api/admin/forms/{fid}/items", json=body).status_code == 400


def test_입력폼_설정은_개정해도_유지된다(admin, tmp_path):
    fid = published(admin, tmp_path)
    hide = [{"kind": "cell", "key": "t0.r1.c1", "hidden": True}]
    admin.put(f"/api/admin/forms/{fid}/items", json=hide)
    new = form_bytes(tmp_path, "○○○ 해외출장")
    admin.post(f"/api/admin/forms/{fid}/versions", files={"file": ("x.hwpx", new)})
    slots = admin.get(f"/api/forms/{fid}/layout").json()["schema"]["slots"]
    assert [(s["id"], s["role"]) for s in slots] == [("t0.r0.c1", "input"), ("t0.r1.c1", "fixed")]


def test_누름틀은_이름·필수·순서를_바꾼다():
    from docflow.schema.model import FieldSpec, FormSchema
    from docflow.service.catalog import apply_items
    from docflow.service.db import FormItem
    from docflow.validate import field_required

    schema = FormSchema(title="기안문", fields=[
        FieldSpec(name="docnumber", type="CLICK_HERE", placeholder="○○처-0000"),
        FieldSpec(name="content_title", type="CLICK_HERE", placeholder="○○○"),
        FieldSpec(name="apb1_sign", type="CLICK_HERE", role="approval"),
    ])  # fmt: skip
    items = [FormItem(kind="field", key="content_title", label="건명", position=0),
             FormItem(kind="field", key="docnumber", required=False, position=1),
             FormItem(kind="field", key="apb1_sign", hidden=True)]  # fmt: skip
    out = apply_items(schema, items)
    assert [(f.name, f.label, field_required(f), f.role) for f in out.fields] == [
        ("content_title", "건명", True, "input"),
        ("docnumber", "", False, "input"),
        ("apb1_sign", "", False, "approval"),  # 결재란은 손대지 않는다
    ]
    assert schema.fields[0].name == "docnumber"  # 원본은 그대로


# ------------------------------------------------------------ 검증 규칙 관리 (AD_15~17)
def meeting_doc(user) -> int:
    [form] = user.get("/api/forms", params={"q": "회의계획서"}).json()["items"]
    return user.post("/api/documents", json={"form_id": form["id"]}).json()["id"]


def codes(user, doc: int, cells: dict) -> list[str]:
    issues = user.post(f"/api/documents/{doc}/check", json={"cells": cells}).json()["issues"]
    return [i["code"] for i in issues]


def test_규칙_목록은_적용_서식_수와_켜짐을_준다(admin):
    got = {r["code"]: (r["name"], r["enabled"], r["applies"], r["forms"])
           for r in admin.get("/api/admin/rules").json()}  # fmt: skip
    assert got["REQUIRED_EMPTY"] == ("필수 누락", True, 2, 2)
    assert got["DATE_INVALID"] == ("날짜 형식", True, 1, 2)


def test_규칙을_끄면_점검과_만들기에서_빠지고_켜면_돌아온다(admin, user):
    doc = meeting_doc(user)
    bad = {"t0.r0.c1": "정기회의", "t0.r1.c1": "2026.13.45"}
    assert codes(user, doc, bad) == ["DATE_INVALID"]
    assert admin.put("/api/admin/rules/DATE_INVALID", json={"enabled": False}).status_code == 200
    assert codes(user, doc, bad) == []
    user.put(f"/api/documents/{doc}/input", json={"cells": bad})
    out = user.post(f"/api/documents/{doc}/generate", json={"formats": []})
    assert out.status_code == 200, out.text
    rules = {r["code"]: r["enabled"] for r in admin.get("/api/admin/rules").json()}
    assert rules["DATE_INVALID"] is False

    admin.put("/api/admin/rules/DATE_INVALID", json={"enabled": True})
    assert codes(user, meeting_doc(user), bad) == ["DATE_INVALID"]


def test_예외_서식에서만_규칙을_끈다(admin, user):
    meeting = user.get("/api/forms", params={"q": "회의계획서"}).json()["items"][0]["id"]
    r = admin.put("/api/admin/rules/REQUIRED_EMPTY/exceptions", json={"form_ids": [meeting]})
    assert [f["id"] for f in r.json()] == [meeting]
    assert codes(user, meeting_doc(user), {}) == []
    traffic = user.get("/api/forms", params={"q": "교통비"}).json()["items"][0]["id"]
    other = user.post("/api/documents", json={"form_id": traffic}).json()["id"]
    assert "REQUIRED_EMPTY" in codes(user, other, {})

    [rule] = [x for x in admin.get("/api/admin/rules").json() if x["code"] == "REQUIRED_EMPTY"]
    assert rule["enabled"] and [f["id"] for f in rule["exceptions"]] == [meeting]
    admin.put("/api/admin/rules/REQUIRED_EMPTY/exceptions", json={"form_ids": []})
    assert "REQUIRED_EMPTY" in codes(user, meeting_doc(user), {})


def test_없는_규칙이나_서식은_400(admin):
    assert admin.put("/api/admin/rules/NOPE", json={"enabled": False}).status_code == 400
    r = admin.put("/api/admin/rules/REQUIRED_EMPTY/exceptions", json={"form_ids": [999]})
    assert r.status_code == 400


# ------------------------------------------------------------ 카테고리 (AD_13)
def test_사업_안에서_카테고리_이름을_한꺼번에_바꾼다(admin, user):
    cats = admin.get("/api/admin/categories").json()
    assert {"program": "AID", "category_no": 7, "category": "회의비", "forms": 1} in cats
    body = {"program": "AID", "category": "회의비", "new": "회의·행사비"}
    assert admin.put("/api/admin/categories", json=body).json()["forms"] == 1
    [form] = user.get("/api/forms", params={"q": "회의계획서"}).json()["items"]
    assert form["category"] == "회의·행사비"
    assert admin.put("/api/admin/categories", json=body).status_code == 404
    blank = {**body, "category": "회의·행사비", "new": " "}
    assert admin.put("/api/admin/categories", json=blank).status_code == 400


# ------------------------------------------------------------ 시스템 (AD_27·AD_28)
def test_시스템_설정과_상태(admin):
    saved = admin.put("/api/admin/system", json={"llm_url": "http://127.0.0.1:9", "llm_model": "m"})
    assert saved.json() == {"llm_url": "http://127.0.0.1:9", "llm_model": "m"}
    out = admin.get("/api/admin/system").json()
    assert out["converter"] == {"available": False, "name": None}
    assert (out["llm"]["llm_model"], out["llm"]["status"]["reachable"]) == ("m", False)
    for bad in ({"llm_url": "ftp://x"}, {"llm_model": " "}, {"nope": "x"}):
        assert admin.put("/api/admin/system", json=bad).status_code == 400


def test_LLM_상태는_모델이_받아져_있는지_본다(monkeypatch):
    import io
    import json

    from docflow.service import system

    def fake(url, timeout):
        assert url == "http://llm:11434/api/tags"
        return io.BytesIO(json.dumps({"models": [{"name": "exaone3.5:7.8b"}]}).encode())

    monkeypatch.setattr(system.urllib.request, "urlopen", fake)
    assert system.llm_status("http://llm:11434/", "exaone3.5:7.8b")["ok"] is True
    missing = system.llm_status("http://llm:11434", "exaone3.5:2.4b")
    assert (missing["ok"], missing["reachable"]) == (False, True)
