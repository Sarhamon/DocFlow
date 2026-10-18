"""실제 한컴오피스로 변환해 보는 로컬 회귀 테스트 (M4 완료 기준).

한컴오피스와 실제 서식이 필요해서 `pytest -m local` 로만 돈다.

1. 값을 채운 서식을 HWP·PDF 로 바꾸고, 그 HWP 를 한글이 다시 연다.
2. 자동화가 비정상 종료해 창 없는 한글이 남아도 다음 변환이 멈추지 않고, 남은 한글은 정리된다.
3. 경로가 260자를 넘어도 변환된다 (한글은 그런 경로에 저장하지 못한다).
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from docflow.adapters import hancom
from docflow.adapters.hancom import Hancom, HancomConverter, hwp_processes
from docflow.config import get_settings
from docflow.convert import ConversionJob
from docflow.hwpx.document import HwpxDocument
from docflow.schema.extract import extract

pytestmark = pytest.mark.local


@pytest.fixture(scope="module", autouse=True)
def need_hancom():
    if not hancom.available():
        pytest.skip("한컴오피스가 없다")


@pytest.fixture
def filled(tmp_path):
    """교통비 지급 내역서에 값을 채우고 명단을 늘린 HWPX."""
    templates = list(get_settings().templates.rglob("*AID 서식 5-05호*.hwpx"))
    if not templates:
        pytest.skip("templates/ 가 없다")
    doc = HwpxDocument.open(templates[0])
    schema = extract(doc)
    for slot in schema.input_slots():
        doc.set_cell(slot.table, slot.row, slot.col, "값")
    rep = schema.repeats[0]
    first = rep.columns[0].col
    row = [None] * (rep.columns[-1].col - first + 1)
    for c in rep.columns:
        row[c.col - first] = "5,000" if c.input_type == "amount" else "가"
    doc.fill_rows(rep.table, rep.start_row, [row] * (rep.capacity + 2), first,
                  last_row=rep.end_row, extend=True)  # fmt: skip
    doc.recalculate_formulas()
    return doc.save(tmp_path / "교통비.hwpx")


def _page_count(path) -> int:
    with Hancom() as h:
        assert h.hwp.Open(str(path), "", "forceopen:true")
        try:
            return h.hwp.PageCount
        finally:
            h.hwp.Clear(1)


def _alive(pid: int) -> bool:
    return pid in {p for p, _ in hwp_processes()}


def test_값을_채운_서식을_HWP_PDF로_바꾸고_한글이_다시_연다(filled, tmp_path):
    result = HancomConverter().convert(ConversionJob(filled, ("HWP", "PDF"), tmp_path / "out"))
    assert result.ok, result.errors
    assert result.attempts == 1
    assert _page_count(result.outputs["HWP"]) >= 1
    assert not [p for p, title in hwp_processes() if not title]  # 남긴 한글이 없다


ORPHAN = """
import os, time, win32com.client
win32com.client.Dispatch("HWPFrame.HwpObject")
time.sleep(2)
os._exit(1)  # Quit 없이 죽는다 -> 창 없는 한글이 남는다
"""


def test_비정상_종료로_남은_한글이_있어도_다음_변환이_멈추지_않는다(filled, tmp_path):
    before = {p for p, _ in hwp_processes()}
    subprocess.run([sys.executable, "-c", ORPHAN], check=False, timeout=60)
    time.sleep(1)
    orphans = [p for p, title in hwp_processes() if p not in before and not title]
    assert orphans, "고아 한글을 만들지 못했다"

    started = time.monotonic()
    result = HancomConverter(timeout=60).convert(ConversionJob(filled, ("PDF",), tmp_path))
    assert result.ok, result.errors
    assert time.monotonic() - started < 60
    assert not any(_alive(p) for p in orphans)


def test_경로가_260자를_넘어도_변환된다(filled, tmp_path):
    deep = tmp_path.joinpath(*[f"{i}_" + "긴폴더이름" * 10 for i in range(4)])
    try:
        deep.mkdir(parents=True)
        src = deep / "교통비.hwpx"
        src.write_bytes(filled.read_bytes())
    except OSError:
        pytest.skip("이 PC 는 긴 경로를 쓸 수 없다 (LongPathsEnabled 꺼짐)")
    job = ConversionJob(src, ("HWP",))
    assert len(str(job.target("HWP"))) > hancom.MAX_PATH
    result = HancomConverter().convert(job)
    assert result.ok, result.errors
