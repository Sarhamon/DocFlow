"""예전에 평문으로 저장한 문서 입력값과 만든 파일을 암호화한다 (여러 번 돌려도 된다).

    python tools/encrypt_data.py            # 바꿀 것을 세기만 한다
    python tools/encrypt_data.py --apply    # 실제로 바꾼다

키는 앱과 같다 (환경변수 DOCFLOW_DATA_KEY 또는 secrets/data.key). 돌리기 전에 data/ 를 백업한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from docflow.config import get_settings  # noqa: E402
from docflow.service import crypto  # noqa: E402
from docflow.service.db import connect  # noqa: E402

#: 입력값이 든 칸 (표, 칸)
COLUMNS = (("documents", "input_json"), ("document_events", "input_json"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제로 암호화한다")
    args = parser.parse_args(argv)

    settings = get_settings()
    cipher = crypto.Cipher(crypto.load_key(settings))
    sessions = connect(settings.database_url)
    with sessions() as s:
        for table, column in COLUMNS:
            rows = s.execute(text(
                f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL "
                f"AND {column} NOT LIKE :prefix"
            ), {"prefix": crypto.TEXT_PREFIX + "%"}).all()  # fmt: skip
            print(f"{table}.{column}: 평문 {len(rows)}건")
            if args.apply:
                for row_id, value in rows:
                    s.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :id"),
                              {"v": cipher.encrypt_text(value), "id": row_id})  # fmt: skip
        if args.apply:
            s.commit()

    files = settings.data / "files" / "documents"
    found = [p for p in files.rglob("*") if p.is_file()] if files.exists() else []
    plain = [p for p in found if not p.read_bytes().startswith(crypto.FILE_MAGIC)]
    print(f"만든 파일: 평문 {len(plain)}개 ({files})")
    if args.apply:
        for path in plain:
            cipher.encrypt_file(path)
    print("암호화했습니다." if args.apply else "바꾸려면 --apply 를 붙여 다시 돌리세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
