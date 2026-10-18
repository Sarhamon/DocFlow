"""forms/ 의 .hwp 서식을 templates/ 아래 .hwpx 로 일괄 변환한다 (로컬 전용, 한컴 COM).

    python tools/convert_templates.py                 # forms/ 전체
    python tools/convert_templates.py forms/ysu_forms/2026_AID
    python tools/convert_templates.py --force         # 이미 변환된 것도 다시

원본보다 새 .hwpx 가 이미 있으면 건너뛴다. 한 건이 실패해도 나머지는 계속한다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.adapters.hancom import Hancom, cleanup_hwp  # noqa: E402
from docflow.config import Settings, get_settings  # noqa: E402
from docflow.log import setup_logging  # noqa: E402

log = logging.getLogger("convert_templates")


def plan(sources: list[Path], settings: Settings, *, force: bool) -> list[tuple[Path, Path]]:
    """변환할 (원본, 대상) 목록. force 가 아니면 원본보다 새 대상이 있는 것은 뺀다."""
    jobs = []
    for src in sources:
        # forms/ 기준 경로를 그대로 미러링한다. 원본 폴더명이 유지되어야 .gitignore 로 가려진다.
        dst = settings.mirror(src, kind="template")
        if force or not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            jobs.append((src, dst))
    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", nargs="?", help="forms/ 아래 폴더 (기본: forms/ 전체)")
    parser.add_argument("--force", action="store_true", help="이미 변환된 것도 다시 변환")
    args = parser.parse_args(argv)

    setup_logging()
    settings = get_settings()
    src_root = (settings.root / args.source).resolve() if args.source else settings.forms
    sources = sorted(src_root.rglob("*.hwp"))
    if not sources:
        log.error("변환할 .hwp 가 없습니다: %s", src_root)
        return 1

    jobs = plan(sources, settings, force=args.force)
    log.info("원본 %d개 중 %d개 변환 (나머지는 최신) -> %s",
             len(sources), len(jobs), settings.templates)
    if not jobs:
        return 0

    failed: list[tuple[Path, str]] = []
    cleanup_hwp("orphans")  # 지난 실행이 남긴 창 없는 한글에 붙으면 Open() 에서 멈춘다
    with Hancom() as hancom:
        for i, (src, dst) in enumerate(jobs, 1):
            rel = dst.relative_to(settings.templates)
            try:
                hancom.convert(src, dst, fmt="HWPX")
                log.info("[%d/%d] OK   %s", i, len(jobs), rel)
            except Exception as exc:  # 한 건 실패가 배치를 멈추지 않도록
                failed.append((src, str(exc)))
                log.error("[%d/%d] FAIL %s: %s", i, len(jobs), src.name, exc)

    log.info("완료: 성공 %d / 실패 %d", len(jobs) - len(failed), len(failed))
    for src, err in failed:
        log.error("  - %s: %s", src.name, err)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
