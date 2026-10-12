"""한컴오피스 COM 어댑터 — **로컬(Windows) 전용**.

클라우드로 옮길 때(M12) 이 모듈만 떼어내 별도 Windows 워커로 분리한다.
코어 로직(docflow.hwpx, docflow.schema, docflow.validate)은 이 모듈에 의존하지 않는다.

* Hancom          한글 인스턴스 하나로 파일을 변환한다 (서식 일괄 변환 도구가 쓴다).
* HancomConverter 문서 변환 작업(docflow.convert)을 자식 프로세스에서 돌린다. 타임아웃·재시도·
                  남은 한글 프로세스 정리를 맡는다 (서비스가 쓴다).

알려진 함정: COM 으로 띄운 한글이 Quit() 뒤에도 남는 경우가 있고, 남은 인스턴스에 다음
Dispatch 가 붙으면 Open() 에서 무한 대기한다. 그래서 변환을 자식 프로세스에서 돌려 시간이
넘으면 통째로 끊고, 창 없이 남은 한글(자동화가 띄운 것)을 정리한다.
"""

from __future__ import annotations

import csv
import io
import logging
import multiprocessing
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from docflow.convert import FORMATS, ConversionJob, ConversionResult, check_output

log = logging.getLogger(__name__)

PROG_ID = "HWPFrame.HwpObject"
#: 한글은 이 길이 이상의 경로에 저장하지 못하고 SaveAs 가 이유 없이 False 를 돌려준다.
#: (2026-10 확인: 258자는 성공, 264자는 실패)
MAX_PATH = 260

#: 남은 한글 정리 정책. orphans: 창이 없는 한글(자동화가 띄운 것)만 / all: 전용 변환 서버용 /
#: none: 정리하지 않는다
CleanupPolicy = Literal["orphans", "all", "none"]


class HancomUnavailable(RuntimeError):
    """Windows 가 아니거나 한컴오피스가 없을 때."""


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return False
    return True


class Hancom:
    """한글 인스턴스 하나를 열어 여러 파일을 변환한다.

    사용::

        with Hancom() as h:
            h.convert(src, dst, fmt="HWPX")
    """

    def __init__(self) -> None:
        if not available():
            raise HancomUnavailable("Windows + 한컴오피스 + pywin32 가 필요합니다.")
        import win32com.client as win32

        before = _hwp_pids()
        self.hwp = win32.Dispatch(PROG_ID)
        #: 이 인스턴스가 띄운 한글 프로세스 (close 시 확실히 정리하기 위함)
        self._spawned = _hwp_pids() - before
        # 자동화 중 뜨는 메시지 상자를 기본값으로 자동 응답
        self.hwp.SetMessageBoxMode(0x00020000)

    def convert(self, src: str | Path, dst: str | Path, fmt: str = "HWPX") -> Path:
        """src 를 열어 fmt(HWP/HWPX/PDF) 로 dst 에 저장한다."""
        src, dst = Path(src).resolve(), Path(dst).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self.hwp.Open(str(src), "", "forceopen:true"):
            raise RuntimeError(f"열기 실패: {src}")
        try:
            if not self.hwp.SaveAs(str(dst), fmt, ""):
                if len(str(dst)) >= MAX_PATH:
                    raise RuntimeError(
                        f"저장 실패: 경로가 {len(str(dst))}자라 한글이 저장하지 못한다 "
                        f"({MAX_PATH}자 미만이어야 한다): {dst}"
                    )
                raise RuntimeError(f"저장 실패: {dst}")
        finally:
            self.hwp.Clear(1)  # 1 = 저장하지 않고 닫기
        return dst

    def close(self) -> None:
        """한글을 종료한다.

        Quit() 만으로는 프로세스가 남는 경우가 있고, 남은 인스턴스에 다음 Dispatch 가
        붙으면 Open() 에서 무한 대기한다. 그래서 직접 띄운 프로세스는 확인 후 정리한다.
        """
        try:
            self.hwp.Quit()
        except Exception:
            pass
        self.hwp = None
        time.sleep(1)
        _kill(self._spawned & _hwp_pids())

    def __enter__(self) -> Hancom:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------- 한글 프로세스
def parse_tasklist(raw: bytes) -> list[tuple[int, str]]:
    """`tasklist /v /fo csv /nh` 출력에서 (PID, 창 제목). 창이 없으면 제목은 빈 문자열이다.

    tasklist 는 한글 윈도우에서 cp949 로 출력한다. 창이 없는 프로세스의 제목은 "N/A" 다.
    """
    try:
        text = raw.decode("cp949")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    found = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or not row[1].isdigit():
            continue  # "정보: 일치하는 작업이 없습니다" 같은 안내 줄
        title = row[-1] if len(row) >= 9 else ""
        found.append((int(row[1]), "" if title == "N/A" else title))
    return found


def hwp_processes() -> list[tuple[int, str]]:
    """실행 중인 Hwp.exe 의 (PID, 창 제목)."""
    if sys.platform != "win32":
        return []
    result = subprocess.run(
        ["tasklist", "/v", "/fi", "imagename eq Hwp.exe", "/fo", "csv", "/nh"],
        capture_output=True,
        check=False,
    )
    return parse_tasklist(result.stdout)


def _hwp_pids() -> set[int]:
    return {pid for pid, _title in hwp_processes()}


def _kill(pids) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)


def cleanup_hwp(policy: CleanupPolicy = "orphans", only: set[int] | None = None) -> list[int]:
    """남은 한글 프로세스를 정리하고 종료한 PID 를 돌려준다.

    orphans 는 창이 없는 한글만 끈다. 자동화가 띄운 한글은 창이 없고, 사람이 연 한글은
    문서 제목이 붙은 창이 있으므로 사용자의 작업은 건드리지 않는다.
    only 를 주면 그 PID 중에서만 고른다.
    """
    if policy == "none":
        return []
    targets = [
        pid
        for pid, title in hwp_processes()
        if (only is None or pid in only) and (policy == "all" or not title)
    ]
    _kill(targets)
    if targets:
        log.warning("남은 한글 프로세스 %d개를 정리했다: %s", len(targets), targets)
    return targets


# ---------------------------------------------------------------- 변환 작업
#: 자식 프로세스에서 도는 변환 함수: (원본, [(형식, 대상)], 결과를 보낼 연결)
Worker = Callable[[str, list[tuple[str, str]], object], None]

DONE = "__done__"
FAILED = "__failed__"


def hancom_worker(src: str, targets: list[tuple[str, str]], conn) -> None:
    """자식 프로세스: 한글 하나를 띄워 형식별로 변환하고 결과를 형식마다 보낸다."""
    try:
        with Hancom() as h:
            for fmt, dst in targets:
                try:
                    h.convert(src, dst, fmt=fmt)
                    conn.send((fmt, None))
                except Exception as exc:  # 한 형식이 실패해도 다음 형식은 시도한다
                    conn.send((fmt, str(exc)))
    except Exception as exc:
        conn.send((FAILED, f"한글을 시작하지 못했다: {exc}"))
    finally:
        conn.send((DONE, None))
        conn.close()


class HancomConverter:
    """문서 변환 작업을 한컴오피스로 처리한다 (docflow.convert.Converter).

    * 변환은 자식 프로세스에서 돈다. timeout 초가 넘으면 자식과 그것이 띄운 한글을 끊는다.
    * 실패한 형식만 retries 번 다시 시도한다. 시도 전에 남은 한글을 정리한다(cleanup 정책).
    * 짧은 임시 폴더에서 변환한 뒤 옮긴다. 경로 길이(260자)·특수문자 문제를 피한다.
    * 산출물은 형식별 시작 바이트까지 확인한다.
    """

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        retries: int = 1,
        cleanup: CleanupPolicy = "orphans",
        worker: Worker = hancom_worker,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.cleanup = cleanup
        self.worker = worker

    def convert(self, job: ConversionJob) -> ConversionResult:
        started = time.perf_counter()
        result = ConversionResult(job=job)
        source = Path(job.source)
        if not source.is_file():
            result.errors = {fmt: f"원본이 없다: {source}" for fmt in job.formats}
            return result

        pending = list(job.formats)
        with tempfile.TemporaryDirectory(prefix="dfc") as tmp:
            work = Path(tmp)
            src = work / f"in{source.suffix}"
            shutil.copyfile(source, src)
            while pending and result.attempts <= self.retries:
                result.attempts += 1
                cleanup_hwp(self.cleanup)
                targets = {f: work / f"out{result.attempts}{FORMATS[f]}" for f in pending}
                status = self._run(src, targets)
                for fmt in list(pending):
                    error = status.get(fmt) or check_output(targets[fmt], fmt)
                    if error:
                        result.errors[fmt] = error
                        continue
                    dst = job.target(fmt)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(targets[fmt], dst)
                    result.outputs[fmt] = dst
                    result.errors.pop(fmt, None)
                    pending.remove(fmt)
                if pending:
                    log.warning("변환 실패 (%d회째) %s: %s", result.attempts, source.name,
                                {f: result.errors[f] for f in pending})
        result.elapsed = time.perf_counter() - started
        return result

    def _run(self, src: Path, targets: dict[str, Path]) -> dict[str, str | None]:
        """자식 프로세스에서 한 번 변환한다. 형식 -> 실패 사유(None 이면 성공)."""
        ctx = multiprocessing.get_context("spawn")
        receive, send = ctx.Pipe(duplex=False)
        before = _hwp_pids()
        proc = ctx.Process(
            target=self.worker,
            args=(str(src), [(f, str(p)) for f, p in targets.items()], send),
            daemon=True,
        )
        proc.start()
        send.close()  # 자식만 쓰게 한다. 자식이 죽으면 recv 가 EOFError 를 낸다

        status: dict[str, str | None] = {}
        reason = None
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receive.poll(remaining):
                reason = f"{self.timeout:g}초 안에 끝나지 않았다"
                break
            try:
                key, value = receive.recv()
            except EOFError:
                proc.join(5)
                reason = f"변환 프로세스가 비정상 종료했다 (종료 코드 {proc.exitcode})"
                break
            if key == DONE:
                break
            if key == FAILED:
                reason = value
                break
            status[key] = value
        receive.close()

        if reason is None:
            proc.join(10)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        if reason is not None:
            # 이 시도가 띄운 한글 중 창이 없는 것(자동화 인스턴스)을 끈다
            cleanup_hwp("orphans", only=_hwp_pids() - before)
        for fmt in targets:
            status.setdefault(fmt, reason or "결과를 받지 못했다")
        return status
