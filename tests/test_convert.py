"""문서 변환 작업 테스트 — 한컴오피스 없이 가짜 변환 함수로 재시도·타임아웃·검증을 본다."""

from __future__ import annotations

import pytest

from docflow.adapters import hancom
from docflow.adapters.hancom import HancomConverter, parse_tasklist
from docflow.convert import SIGNATURES, ConversionJob, check_output
from tests import conversion_fakes as fake


@pytest.fixture(autouse=True)
def no_real_hwp(monkeypatch):
    """실제 한글 프로세스를 조회·종료하지 않는다."""
    monkeypatch.setattr(hancom, "hwp_processes", lambda: [])


@pytest.fixture
def job(tmp_path):
    src = tmp_path / "문서.hwpx"
    src.write_bytes(SIGNATURES["HWPX"] + b" source")
    return ConversionJob(source=src, formats=("HWP", "PDF"), out_dir=tmp_path / "out")


def converter(worker, **kw) -> HancomConverter:
    return HancomConverter(worker=worker, timeout=kw.pop("timeout", 20), **kw)


# ------------------------------------------------------------------ 작업 모델
def test_산출물_경로는_출력_폴더와_원본_이름을_따른다(job, tmp_path):
    assert job.target("PDF") == tmp_path / "out" / "문서.pdf"
    named = ConversionJob(source=job.source, stem="5-05_교통비_20261012")
    assert named.target("HWP") == job.source.parent / "5-05_교통비_20261012.hwp"


def test_모르는_형식은_거부한다(job):
    with pytest.raises(ValueError, match="DOCX"):
        ConversionJob(source=job.source, formats=("DOCX",))


@pytest.mark.parametrize(
    ("data", "fmt", "ok"),
    [
        (SIGNATURES["PDF"] + b"1.7", "PDF", True),
        (SIGNATURES["HWP"] + b"\x00", "HWP", True),
        (b"PK\x03\x04...", "HWPX", True),
        (b"", "PDF", False),
        (b"<html>", "PDF", False),
        (SIGNATURES["PDF"], "HWP", False),
    ],
)
def test_산출물은_시작_바이트까지_확인한다(tmp_path, data, fmt, ok):
    path = tmp_path / "x"
    path.write_bytes(data)
    assert (check_output(path, fmt) is None) is ok


def test_산출물이_없으면_사유를_준다(tmp_path):
    assert "없다" in check_output(tmp_path / "none.pdf", "PDF")


# ------------------------------------------------------------------ 변환기
def test_변환에_성공하면_출력_폴더로_옮긴다(job):
    result = converter(fake.ok).convert(job)
    assert result.ok and result.attempts == 1
    assert set(result.outputs) == {"HWP", "PDF"}
    assert result.outputs["PDF"] == job.target("PDF")
    assert check_output(result.outputs["HWP"], "HWP") is None


def test_실패한_형식만_한_번_다시_시도한다(job, tmp_path, monkeypatch):
    log = tmp_path / "formats.log"
    monkeypatch.setenv("FAKE_LOG", str(log))  # 자식 프로세스가 물려받는다
    result = converter(fake.fail_pdf_once).convert(job)
    assert result.ok and result.attempts == 2
    assert log.read_text(encoding="utf-8").split() == ["HWP,PDF", "PDF"]
    assert result.outputs["HWP"].exists() and result.outputs["PDF"].exists()


def test_재시도에도_실패하면_사유를_남긴다(job):
    result = converter(fake.bad_output).convert(job)
    assert not result.ok and result.attempts == 2
    assert result.outputs == {}
    assert "PDF 파일이 아니다" in result.errors["PDF"]


def test_재시도_횟수를_바꿀_수_있다(job):
    assert converter(fake.bad_output, retries=0).convert(job).attempts == 1


def test_시간이_넘으면_끊고_다시_시도한다(job):
    result = converter(fake.hang_then_ok, timeout=3).convert(job)
    assert result.ok and result.attempts == 2


def test_계속_멈추면_시간_초과로_실패한다(job):
    result = converter(fake.hang, timeout=1).convert(job)
    assert not result.ok and result.attempts == 2
    assert "1초 안에 끝나지 않았다" in result.errors["HWP"]
    assert result.elapsed < 20  # 무한 대기하지 않는다


def test_변환_프로세스가_죽어도_멈추지_않는다(job):
    result = converter(fake.crash).convert(job)
    assert not result.ok
    assert "비정상 종료" in result.errors["PDF"]


def test_한글을_시작하지_못하면_그_사유를_준다(job):
    result = converter(fake.cannot_start, retries=0).convert(job)
    assert result.errors == {
        "HWP": "한글을 시작하지 못했다: 라이선스",
        "PDF": "한글을 시작하지 못했다: 라이선스",
    }


def test_원본이_없으면_시도하지_않는다(tmp_path):
    job = ConversionJob(source=tmp_path / "없음.hwpx")
    result = converter(fake.ok).convert(job)
    assert result.attempts == 0 and set(result.errors) == {"HWP", "PDF"}


def test_실패해도_출력_폴더에_찌꺼기를_남기지_않는다(job):
    converter(fake.bad_output).convert(job)
    assert not (job.out_dir.exists() and any(job.out_dir.iterdir()))


# ------------------------------------------------------------------ 남은 한글 정리
TASKLIST = (
    '"Hwp.exe","23988","Console","1","8,560 K","Running","PC\\user","0:00:02","N/A"\r\n'
    '"Hwp.exe","1200","Console","1","68,004 K","Running","PC\\user","0:00:09",'
    '"보고서.hwp - 한글"\r\n'
).encode("cp949")


def test_tasklist에서_PID와_창_제목을_읽는다():
    assert parse_tasklist(TASKLIST) == [(23988, ""), (1200, "보고서.hwp - 한글")]


def test_일치하는_프로세스가_없다는_안내는_무시한다():
    raw = "정보: 실행 중인 작업 중 지정된 조건에 일치하는 작업이 없습니다.\r\n".encode("cp949")
    assert parse_tasklist(raw) == []


@pytest.fixture
def killed(monkeypatch):
    monkeypatch.setattr(hancom, "hwp_processes", lambda: parse_tasklist(TASKLIST))
    found: list[int] = []
    monkeypatch.setattr(hancom, "_kill", lambda pids: found.extend(pids))
    return found


def test_창이_없는_한글만_정리한다(killed):
    """사람이 연 한글(문서 제목 창)은 건드리지 않는다."""
    assert hancom.cleanup_hwp("orphans") == [23988]
    assert killed == [23988]


def test_전용_서버는_모두_정리할_수_있다(killed):
    assert hancom.cleanup_hwp("all") == [23988, 1200]


def test_정리를_끌_수_있다(killed):
    assert hancom.cleanup_hwp("none") == [] and killed == []


def test_지정한_PID_안에서만_정리한다(killed):
    assert hancom.cleanup_hwp("orphans", only={1200}) == []
