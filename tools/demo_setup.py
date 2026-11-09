"""시연 서식 5종(AID 3-05·7-01·3-04·5-11·9-02)의 입력폼 설정을 DB 에 넣는다.

    python tools/demo_setup.py

관리자 화면 "입력폼 설정"에서 하던 것과 같다
(서식에 박힌 예시 칸을 입력 칸으로, 쓰지 않는 칸은 끄기).
설정은 DB 에만 있어 PC 마다 다시 넣어야 하므로 스크립트로 둔다. 여러 번 돌려도 된다(통째로 바꾼다).
먼저 tools/sync_forms.py 로 서식을 등록해 둔다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from docflow.config import get_settings  # noqa: E402
from docflow.service import catalog  # noqa: E402
from docflow.service.db import Form, connect  # noqa: E402
from docflow.service.storage import open_store  # noqa: E402


def cell(key: str, label: str, *, required: bool = True) -> dict:
    """서식 글자(예시)로 잡힌 칸을 입력 칸으로 바꾼다."""
    return {"kind": "cell", "key": key, "label": label, "required": required}


def off(kind: str, key: str) -> dict:
    """입력 칸 사용 끄기 (서식 글자 그대로 둔다)."""
    return {"kind": kind, "key": key, "hidden": True}


def meeting(t: int) -> list[dict]:
    """회의계획서의 학과용(t1)·부서용(t3) 표: 예시 값이 박혀 추출에서 빠진 칸."""
    return [
        cell(f"t{t}.r2.c2", "회의장소"),
        cell(f"t{t}.r3.c2", "회의안건"),
        cell(f"t{t}.r4.c2", "참석 대상(대학)"),
        cell(f"t{t}.r12.c2", "기타사항", required=False),
    ]


#: AID 서식 번호 -> 입력폼 설정
SETTINGS: dict[str, list[dict]] = {
    "3-05": [
        cell("t0.r0.c1", "세세부과제(Activity)명"),
        cell("t0.r1.c1", "학과"),
        cell("t0.r2.c1", "일시"),
        cell("t0.r4.c1", "내용"),
        cell("t0.r5.c1", "상세사유"),
        cell("t0.r6.c1", "붙임(증빙자료)", required=False),
    ],
    "3-04": [cell("t0.r1.c1", "학과")],
    "7-01": meeting(1) + meeting(3),
    "5-11": [cell("t0.r2.c9", "장소")],
    # 8쪽짜리 계획서: 본문 칸이 `• • •` 빈칸·예시 글씨라 추출에서 빠졌다
    "2-03": [
        cell("t0.r0.c2", "영역 번호"),
        cell("t0.r0.c4", "과제 번호"),
        cell("t0.r0.c6", "세부과제 번호"),
        cell("t0.r0.c8", "세세부과제 번호"),
        cell("t1.r0.c0", "표지 제목"),
        cell("t2.r0.c2", "대학중장기발전계획 번호"),
        cell("t2.r4.c2", "안전관리책임자 성명"),
        cell("t3.r0.c0", "본문 제목"),
        cell("t6.r0.c0", "목적 및 필요성"),
        cell("t8.r0.c0", "추진 내용 및 방법"),
        cell("t10.r0.c1", "정량적 실적 및 성과"),
        cell("t10.r1.c1", "정성적 실적 및 성과"),
        off("cell", "t21.r0.c3"),  # 머리글(집행비율)이 입력 칸으로 잡혔다
        cell("t21.r1.c3", "집행비율(D=B/A*100)"),
        # 대응투자 비고의 예시(교비·RISE·대플)
        cell("t19.r1.c4", "강사료 비고", required=False),
        cell("t19.r2.c4", "전문가활용비 비고", required=False),
        cell("t19.r3.c4", "식사비 비고", required=False),
    ]
    # 강사료 지급기준 표: 금액만 입력 칸으로 잡혀 예시 이름(이동일 등)이 남는다
    + [
        cell(f"t16.r{r}.c{c}", f"강사 {r} {name}", required=False)
        for r in range(1, 5)
        for c, name in enumerate(("성명", "강사구분", "지급구분", "지급 대상/기준"))
    ],
    "9-02": [
        off("cell", "t1.r2.c0"),
        off("cell", "t1.r2.c1"),
        off("cell", "t1.r4.c0"),
        off("cell", "t1.r4.c1"),
        off("column", "t0.r3-17:4"),  # 대상자 서명은 손으로
    ],
}


def main() -> int:
    settings = get_settings()
    store = open_store(settings)
    with connect(settings.database_url)() as session:
        for form_no, items in SETTINGS.items():
            form = session.scalar(
                select(Form).where(
                    Form.program == "AID", Form.form_no == form_no, Form.public.is_(True)
                )
            )
            if form is None:
                print(f"없음: AID {form_no} — 먼저 tools/sync_forms.py", file=sys.stderr)
                return 1
            anchors = {(t["index"], c["row"], c["col"]) for t in catalog.layout(form.latest, store)
                       for c in t["cells"]}  # fmt: skip
            saved = catalog.save_items(session, form, items, anchors)
            print(f"AID {form_no} {form.title}: 항목 {len(saved)}개")
        session.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
