"""변환 계획 테스트 — 한컴오피스 없이 어떤 파일을 변환할지만 본다."""

from __future__ import annotations

import os

from docflow.config import Settings
from tools.convert_templates import plan


def _touch(path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    os.utime(path, (mtime, mtime))


def test_원본보다_새_템플릿이_있으면_건너뛴다(tmp_path):
    settings = Settings.from_root(tmp_path)
    fresh = settings.forms / "ysu_forms" / "1. 기안문" / "a.hwp"
    stale = settings.forms / "ysu_forms" / "1. 기안문" / "b.hwp"
    new = settings.forms / "ysu_forms" / "c.hwp"
    for src in (fresh, stale, new):
        _touch(src, 1000)
    _touch(settings.mirror(fresh, kind="template"), 2000)
    _touch(settings.mirror(stale, kind="template"), 500)  # 원본이 더 새롭다

    jobs = plan([fresh, stale, new], settings, force=False)
    assert [src.name for src, _dst in jobs] == ["b.hwp", "c.hwp"]
    assert jobs[0][1] == settings.templates / "ysu_forms" / "1. 기안문" / "b.hwpx"


def test_force면_모두_변환한다(tmp_path):
    settings = Settings.from_root(tmp_path)
    src = settings.forms / "a.hwp"
    _touch(src, 1000)
    _touch(settings.mirror(src, kind="template"), 2000)
    assert len(plan([src], settings, force=True)) == 1
