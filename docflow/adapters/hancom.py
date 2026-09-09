# -*- coding: utf-8 -*-
"""한컴오피스 COM 어댑터 — **로컬(Windows) 전용**.

Phase 2 에서 AWS 로 옮길 때 이 모듈만 떼어내거나 별도 Windows 워커로 분리한다.
코어 로직(docflow.hwpx, docflow.validate)은 이 모듈에 의존하지 않는다.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROG_ID = "HWPFrame.HwpObject"


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
        for pid in self._spawned & _hwp_pids():
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                check=False,
            )

    def __enter__(self) -> "Hancom":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _hwp_pids() -> set[int]:
    """현재 실행 중인 Hwp.exe 의 PID 집합."""
    if sys.platform != "win32":
        return set()
    result = subprocess.run(
        ["tasklist", "/fi", "imagename eq Hwp.exe", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = set()
    for line in result.stdout.splitlines():
        parts = line.split('","')
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids
