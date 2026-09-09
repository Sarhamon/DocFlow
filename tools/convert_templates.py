# -*- coding: utf-8 -*-
"""forms/ 의 .hwp 서식을 templates/ 아래 .hwpx 로 일괄 변환한다 (로컬 전용).

    python tools/convert_templates.py "forms/ysu_forms/[2026 앵커(신산업)] 서식모음"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.adapters.hancom import Hancom  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "templates"


def main(source_dir: str) -> int:
    src_root = (ROOT / source_dir).resolve()
    sources = sorted(src_root.rglob("*.hwp"))
    if not sources:
        print(f"변환할 .hwp 가 없습니다: {src_root}")
        return 1

    print(f"{len(sources)}개 변환 시작 -> {OUT_ROOT}")
    failed: list[tuple[Path, str]] = []
    with Hancom() as hancom:
        for i, src in enumerate(sources, 1):
            dst = OUT_ROOT / src.relative_to(src_root).with_suffix(".hwpx")
            try:
                hancom.convert(src, dst, fmt="HWPX")
                print(f"  [{i}/{len(sources)}] OK   {dst.relative_to(OUT_ROOT)}")
            except Exception as exc:  # 한 건 실패가 배치를 멈추지 않도록
                failed.append((src, str(exc)))
                print(f"  [{i}/{len(sources)}] FAIL {src.name}: {exc}")

    print(f"완료: 성공 {len(sources) - len(failed)} / 실패 {len(failed)}")
    for src, err in failed:
        print(f"  - {src.name}: {err}")
    return 0 if not failed else 2


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    raise SystemExit(main(sys.argv[1]))
