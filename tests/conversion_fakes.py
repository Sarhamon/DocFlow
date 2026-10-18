"""HancomConverter 시험용 가짜 변환 함수. 자식 프로세스에서 import 되므로 모듈 최상위에 둔다.

신호는 원본 옆(작업 폴더)의 파일로 남긴다. 시도마다 새 프로세스라 그 밖의 상태는 공유되지 않는다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from docflow.adapters.hancom import DONE, FAILED
from docflow.convert import SIGNATURES


def _write_ok(fmt: str, dst: str) -> None:
    Path(dst).write_bytes(SIGNATURES[fmt] + b" fake")


def _count(src: str) -> int:
    """이번이 몇 번째 시도인지 (1부터). 작업 폴더에 기록한다."""
    marker = Path(src).with_name("attempts")
    n = int(marker.read_text()) + 1 if marker.exists() else 1
    marker.write_text(str(n))
    return n


def ok(src, targets, conn):
    _count(src)
    for fmt, dst in targets:
        _write_ok(fmt, dst)
        conn.send((fmt, None))
    conn.send((DONE, None))


def hang(src, targets, conn):
    time.sleep(60)


def hang_then_ok(src, targets, conn):
    if _count(src) == 1:
        time.sleep(60)
    ok(src, targets, conn)


def fail_pdf_once(src, targets, conn):
    """첫 시도에서 PDF 만 실패한다. 시도마다 받은 형식을 환경변수 FAKE_LOG 파일에 남긴다."""
    n = _count(src)
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
        log.write(",".join(f for f, _ in targets) + "\n")
    for fmt, dst in targets:
        if fmt == "PDF" and n == 1:
            conn.send((fmt, "저장 실패"))
        else:
            _write_ok(fmt, dst)
            conn.send((fmt, None))
    conn.send((DONE, None))


def crash(src, targets, conn):
    os._exit(3)


def bad_output(src, targets, conn):
    for fmt, dst in targets:
        Path(dst).write_bytes(b"not a document")
        conn.send((fmt, None))
    conn.send((DONE, None))


def cannot_start(src, targets, conn):
    conn.send((FAILED, "한글을 시작하지 못했다: 라이선스"))
    conn.send((DONE, None))
