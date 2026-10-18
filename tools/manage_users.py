"""계정 관리 CLI — 서버에서 직접 돌린다. 루트 관리자를 만들고 푸는 유일한 길이다.

    python tools/manage_users.py create-root <아이디> <이름>     # 루트 관리자 (하나만)
    python tools/manage_users.py unlock <아이디>                 # 잠금 해제 (루트 포함)
    python tools/manage_users.py reset-password <아이디>         # 초기 비밀번호 다시 발급
    python tools/manage_users.py list                            # 계정 목록

초기 비밀번호는 화면에 한 번만 찍는다. 받은 사람이 처음 로그인할 때 바꾼다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from docflow.config import get_settings  # noqa: E402
from docflow.service.auth import AuthError, AuthService  # noqa: E402
from docflow.service.catalog import NotFound  # noqa: E402
from docflow.service.db import User, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DocFlow 계정 관리")
    sub = parser.add_subparsers(dest="command", required=True)
    root = sub.add_parser("create-root", help="루트 관리자 만들기")
    root.add_argument("login_id")
    root.add_argument("name")
    for name, help_ in (("unlock", "잠금 해제"), ("reset-password", "비밀번호 초기화")):
        sub.add_parser(name, help=help_).add_argument("login_id")
    sub.add_parser("list", help="계정 목록")
    args = parser.parse_args(argv)

    settings = get_settings()
    settings.data.mkdir(parents=True, exist_ok=True)
    with connect(settings.database_url)() as session:
        svc = AuthService(session)
        try:
            if args.command == "create-root":
                user, password = svc.create_user(None, args.login_id, args.name, role="root")
                print(f"루트 관리자 {user.login_id} 를 만들었다. 초기 비밀번호: {password}")
            elif args.command == "list":
                for u in session.scalars(select(User).order_by(User.id)):
                    lock = " 잠김" if u.locked_at else ""
                    print(f"{u.id:>4} {u.login_id:<20} {u.role:<6} {u.status:<9}{lock} {u.name}")
            else:
                user = session.scalar(select(User).where(User.login_id == args.login_id))
                if user is None:
                    raise NotFound(f"없는 아이디다: {args.login_id}")
                if args.command == "unlock":
                    svc.unlock(None, user.id)
                    print(f"{user.login_id} 의 잠금을 풀었다")
                else:
                    _, password = svc.reset_password(None, user.id)
                    print(f"{user.login_id} 의 비밀번호를 초기화했다. 초기 비밀번호: {password}")
        except (AuthError, NotFound, ValueError) as exc:
            print(f"실패: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
