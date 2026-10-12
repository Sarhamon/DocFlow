"""templates/ 의 HWPX 서식에서 서식별 스키마(v2) JSON 을 추출한다.

    python tools/extract_schema.py            # templates/ 전체
    python tools/extract_schema.py --prune    # 이번에 만들지 않은 옛 JSON 을 지운다

산출: schemas/<templates 와 같은 경로>.json, schemas/<원본폴더>/index.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.config import Settings, get_settings  # noqa: E402
from docflow.hwpx.document import HwpxDocument  # noqa: E402
from docflow.log import setup_logging  # noqa: E402
from docflow.schema.extract import extract, template_path  # noqa: E402
from docflow.schema.model import FormSchema  # noqa: E402
from docflow.schema.rules import parse_location  # noqa: E402

log = logging.getLogger("extract_schema")

#: 서식이 아니라 서식 묶음을 설명하는 문서라 스키마를 뽑지 않는다.
SKIP_WORDS = ("서식 목차", "서식목차", "서식 신구대조표")


def is_form(template: Path) -> bool:
    return not any(w in template.stem for w in SKIP_WORDS)


def extract_file(template: Path, settings: Settings) -> FormSchema:
    rel = template.relative_to(settings.templates)
    return extract(
        HwpxDocument.open(template),
        location=parse_location(rel.parent),
        template=template_path(template, settings.root),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HWPX 서식에서 스키마 JSON 을 추출한다")
    parser.add_argument(
        "--prune", action="store_true", help="schemas/ 에서 이번에 만들지 않은 JSON 을 지운다"
    )
    args = parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    templates = sorted(t for t in settings.templates.rglob("*.hwpx") if is_form(t))
    if not templates:
        log.error("템플릿이 없습니다: %s (먼저 convert_templates 실행)", settings.templates)
        return 1

    index: dict[str, list[dict]] = {}
    written: set[Path] = set()
    totals: Counter[str] = Counter()
    failed: list[tuple[Path, str]] = []
    no_target: list[str] = []
    for template in templates:
        rel = template.relative_to(settings.templates)
        try:
            schema = extract_file(template, settings)
        except Exception as exc:  # 한 건 실패가 배치를 멈추지 않도록
            failed.append((rel, str(exc)))
            log.error("실패 %s: %s", rel, exc)
            continue
        # 템플릿 경로를 그대로 미러링한다. 원본 폴더명이 유지되어야 .gitignore 한 줄로 가려진다.
        out = (settings.schemas / rel).with_suffix(".json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(schema.model_dump_json(indent=2), encoding="utf-8")
        written.add(out)

        counts = {
            "fields": len(schema.fields),
            "tables": len(schema.tables),
            "slots": len(schema.slots),
            "formulas": len(schema.formulas),
            "repeats": len(schema.repeats),
            "variants": len(schema.variants),
        }
        totals.update(counts)
        if not schema.slots and not schema.repeats:
            no_target.append(str(rel))
        index.setdefault(rel.parts[0], []).append(
            {
                "form_id": schema.form_id,
                "title": schema.title,
                "program": schema.program,
                "category_no": schema.category_no,
                "category": schema.category,
                "archived": schema.archived,
                "schema": out.relative_to(settings.schemas).as_posix(),
                **counts,
            }
        )
        log.info(
            "%-10s 필드 %3d 표 %3d 슬롯 %4d 반복 %2d  %s",
            schema.form_id, counts["fields"], counts["tables"], counts["slots"],
            counts["repeats"], schema.title,
        )

    # 목록도 서식 이름을 담으므로 원본과 같은 폴더 안에 둔다 (통째로 ignore 되도록)
    for group, entries in index.items():
        out = settings.schemas / group / "index.json"
        out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        written.add(out)

    stale = sorted(set(settings.schemas.rglob("*.json")) - written)
    if stale and args.prune:
        for path in stale:
            path.unlink()
        log.info("이번에 만들지 않은 JSON %d개를 지웠습니다", len(stale))
    elif stale:
        log.warning("이번에 만들지 않은 JSON %d개가 남아 있습니다 (--prune 으로 정리)", len(stale))

    done = sum(len(v) for v in index.values())
    log.info(
        "서식 %d개 추출, 실패 %d -> %s | 누름틀 %d 표 %d 슬롯 %d 수식 %d 반복영역 %d 변형 %d",
        done, len(failed), settings.schemas, totals["fields"], totals["tables"],
        totals["slots"], totals["formulas"], totals["repeats"], totals["variants"],
    )
    for rel in no_target:
        log.warning("채울 지점이 없는 서식: %s", rel)
    return 0 if not failed and not no_target else 2


if __name__ == "__main__":
    raise SystemExit(main())
