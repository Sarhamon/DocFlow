"""로깅 설정. 라이브러리는 getLogger(__name__) 만 쓰고, 설정은 진입점에서 한 번 호출한다."""

from __future__ import annotations

import logging
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str | int | None = None) -> None:
    """루트 로거를 설정한다. level 이 없으면 환경변수 DOCFLOW_LOG_LEVEL, 그것도 없으면 INFO."""
    level = level or os.environ.get("DOCFLOW_LOG_LEVEL") or "INFO"
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
        if not isinstance(level, int):
            raise ValueError("알 수 없는 로그 레벨")
    logging.basicConfig(level=level, format=_FORMAT, force=True)
