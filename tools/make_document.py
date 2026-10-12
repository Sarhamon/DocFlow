"""서식 하나를 입력값으로 채워 HWPX·HWP·PDF 로 만든다 (M5 수직 슬라이스).

    python tools/make_document.py "AID 서식 5-05호" --skeleton > 교통비.json   # 입력 틀
    python tools/make_document.py "AID 서식 5-05호" 교통비.json                # 검증 → 생성 → 변환
    python tools/make_document.py "AID 서식 5-05호" 교통비.json --hwpx-only    # 한글 없이 HWPX 까지

서식은 templates/ 기준 경로나 파일 이름 일부로 고른다. 검증 오류가 있으면 위치와 사유를
보여주고 만들지 않는다(--force 면 그래도 만든다). 산출물은 out/ 아래에 둔다.

종료 코드: 0 성공 / 1 사용법·입력 오류 / 2 검증 오류 / 3 변환 실패
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.config import Settings, get_settings  # noqa: E402
from docflow.generate import GenerateError  # noqa: E402
from docflow.log import setup_logging  # noqa: E402
from docflow.pipeline import InputError, make_document, parse_input, skeleton  # noqa: E402
from docflow.schema.extract import extract_file, is_form  # noqa: E402

log = logging.getLogger("make_document")


def find_template(query: str, settings: Settings) -> Path:
    """templates/ 의 서식 하나. 경로가 아니면 파일 이름에 query 가 든 서식을 찾는다."""
    for candidate in (Path(query), settings.templates / query, settings.root / query):
        if candidate.is_file():
            return candidate.resolve()
    templates = sorted(settings.templates.rglob("*.hwpx"))
    found = [t for t in templates if query in t.name and is_form(t)]
    if len(found) != 1:
        names = "\n  ".join(str(t.relative_to(settings.templates)) for t in found[:10])
        why = "찾지 못했다" if not found else f"{len(found)}개다. 더 좁혀 주세요:\n  {names}"
        raise InputError(f"서식 {query!r} 을(를) {why}")
    return found[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("form", help="templates/ 기준 경로 또는 파일 이름 일부")
    parser.add_argument("input", nargs="?", help="입력값 JSON 파일")
    parser.add_argument("--skeleton", action="store_true", help="입력 틀 JSON 을 출력한다")
    parser.add_argument("--variant", help="--skeleton 에서 고를 변형 (번호 또는 이름)")
    parser.add_argument("--out", type=Path, help="산출물 폴더 (기본: out/)")
    parser.add_argument("--stem", help="산출물 파일 이름 (기본: 서식 이름_날짜시각)")
    parser.add_argument("--formats", nargs="*", default=["HWP", "PDF"], help="변환 형식")
    parser.add_argument("--hwpx-only", action="store_true", help="변환하지 않는다")
    parser.add_argument("--force", action="store_true", help="검증 오류가 있어도 만든다")
    args = parser.parse_args(argv)

    setup_logging()
    settings = get_settings()
    try:
        template = find_template(args.form, settings)
        schema = extract_file(template, settings)
        if args.skeleton:
            variant = None
            if args.variant is not None:
                variant = parse_input(schema, {"variant": args.variant}).data.variant
            json.dump(skeleton(schema, variant), sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        if not args.input:
            parser.error("입력값 JSON 파일이 필요하다 (입력 틀은 --skeleton)")
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        parsed = parse_input(schema, raw)
    except (InputError, OSError, json.JSONDecodeError) as exc:
        log.error("%s", exc)
        return 1

    converter = None
    if not args.hwpx_only and args.formats:
        from docflow.adapters.hancom import HancomConverter  # 한컴이 필요한 경로에서만 부른다

        converter = HancomConverter()
    stem = args.stem or f"{template.stem}_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir = args.out or settings.out
    try:
        result = make_document(
            schema, parsed, template, out_dir, stem,
            converter=converter, formats=tuple(args.formats), force=args.force,
        )  # fmt: skip
    except GenerateError as exc:
        log.error("문서를 만들 수 없다: %s", exc)
        return 1

    for issue in result.issues:
        log.log(
            logging.ERROR if issue.severity == "error" else logging.WARNING,
            "[%s] %s: %s %s", issue.code, issue.where, issue.message, issue.action,
        )  # fmt: skip
    if result.generated is None:
        log.error("검증 오류 %d건이라 만들지 않았다 (--force 로 무시)", len(result.errors))
        return 2

    gen = result.generated
    log.info(
        "HWPX %s (칸 %d, 누름틀 %d, 수식 %d개 다시 계산)",
        gen.path,
        gen.cells,
        gen.fields,
        len(gen.formulas.updated) if gen.formulas else 0,
    )
    for f, why in gen.formulas.skipped if gen.formulas else []:
        cell = f"t{f.table}.r{f.row}.c{f.col}"
        log.warning(
            "수식 %s %s 를 계산하지 못해 서식의 값 %r 이 남았다: %s", cell, f.formula, f.text, why
        )
    for left in gen.leftovers:
        log.warning("남은 자리표시자 %s %s: %r", left.kind, left.id, left.text[:60])
    if result.conversion is not None:
        for fmt, path in result.conversion.outputs.items():
            log.info("%s %s", fmt, path)
        for fmt, why in result.conversion.errors.items():
            log.error("%s 변환 실패: %s", fmt, why)
        if not result.conversion.ok:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
