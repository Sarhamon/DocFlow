"""templates/ 의 서식을 서비스 DB 에 등록한다 (새 서식은 버전 1, 바뀐 서식은 다음 버전).

    python tools/sync_forms.py
    python tools/sync_forms.py --from-s3   # S3 의 <DOCFLOW_S3_PREFIX>/templates/ 에서 읽는다

관리자 페이지의 서식 등록(M9)이 생기기 전까지 서식을 올리는 방법이다. 여러 번 돌려도 된다.
--from-s3 는 templates/ 를 붙일 수 없는 Fargate 일회성 작업용이다. 먼저 올려 둔다:
    aws s3 sync templates/ s3://<버킷>/<DOCFLOW_S3_PREFIX>/templates/
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.config import get_settings  # noqa: E402
from docflow.log import setup_logging  # noqa: E402
from docflow.service.catalog import sync_templates  # noqa: E402
from docflow.service.db import connect  # noqa: E402
from docflow.service.storage import S3FileStore, open_store  # noqa: E402

log = logging.getLogger("sync_forms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="templates/ 의 서식을 서비스 DB 에 등록한다")
    parser.add_argument(
        "--from-s3", action="store_true", help="S3 의 <DOCFLOW_S3_PREFIX>/templates/ 에서 읽는다"
    )
    args = parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    settings.data.mkdir(parents=True, exist_ok=True)
    store = open_store(settings)
    with tempfile.TemporaryDirectory() as tmp:
        if args.from_s3:
            if not isinstance(store, S3FileStore):
                log.error("--from-s3 는 DOCFLOW_S3_BUCKET 이 있어야 한다")
                return 1
            # 서식 키가 로컬과 같도록(templates/…) 임시 루트 아래 templates/ 에 받는다
            root = Path(tmp)
            count = store.download_prefix("templates", root / "templates")
            log.info("S3 에서 템플릿 %d 개를 받았다", count)
            if not count:
                log.error("S3 에 templates/ 가 비어 있다. 먼저 aws s3 sync 로 올린다")
                return 1
            settings = dataclasses.replace(settings, root=root, templates=root / "templates")
        with connect(settings.database_url)() as session:
            report = sync_templates(session, store, settings)
    log.info(
        "새 서식 %d / 개정 %d / 그대로 %d / 숨김 %d / 실패 %d",
        len(report.added), len(report.updated), report.unchanged, len(report.hidden),
        len(report.failed),
    )  # fmt: skip
    for key in report.updated:
        log.info("  개정: %s", key)
    for key in report.restored:
        log.warning("  파일 복구(저장소에 없었음): %s", key)
    for key in report.hidden:
        log.info("  숨김(서식별로 나눔): %s", key)
    for key, why in report.failed:
        log.error("  실패: %s: %s", key, why)
    return 0 if not report.failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
