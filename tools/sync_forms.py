"""templates/ 의 서식을 서비스 DB 에 등록한다 (새 서식은 버전 1, 바뀐 서식은 다음 버전).

    python tools/sync_forms.py

관리자 페이지의 서식 등록(M9)이 생기기 전까지 서식을 올리는 방법이다. 여러 번 돌려도 된다.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.config import get_settings  # noqa: E402
from docflow.log import setup_logging  # noqa: E402
from docflow.service.catalog import sync_templates  # noqa: E402
from docflow.service.db import connect  # noqa: E402
from docflow.service.storage import LocalFileStore  # noqa: E402

log = logging.getLogger("sync_forms")


def main() -> int:
    setup_logging()
    settings = get_settings()
    settings.data.mkdir(parents=True, exist_ok=True)
    sessions = connect(settings.database_url)
    with sessions() as session:
        report = sync_templates(session, LocalFileStore(settings.data / "files"), settings)
    log.info(
        "새 서식 %d / 개정 %d / 그대로 %d / 숨김 %d / 실패 %d",
        len(report.added), len(report.updated), report.unchanged, len(report.hidden),
        len(report.failed),
    )  # fmt: skip
    for key in report.updated:
        log.info("  개정: %s", key)
    for key in report.hidden:
        log.info("  숨김(서식별로 나눔): %s", key)
    for key, why in report.failed:
        log.error("  실패: %s: %s", key, why)
    return 0 if not report.failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
