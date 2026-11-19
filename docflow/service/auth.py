"""로그인·세션·계정 관리 (M7). 계정은 관리자가 발급하고(IAM 방식) 스스로 가입하지 않는다.

역할은 세 가지다.

* user  문서를 쓴다.
* admin 사용자 화면 + 관리자 페이지. 사용자(user) 계정을 발급·관리한다.
* root  AWS IAM 의 루트처럼 하나만 둔다. 서버에서 CLI(tools/manage_users.py)로 만들고,
        관리자 계정의 발급·잠금 해제를 맡는다. 루트가 잠기면 CLI 로만 푼다.

정책 (functional-spec 1.1 로그인 정책)

* 비밀번호: 대문자·소문자·숫자·특수문자를 모두 포함해 8자 이상.
* 3회 연속 실패하면 잠근다. 시간이 지나도 풀리지 않는다.
* 세션은 30분. 연장하면 그때부터 다시 30분. 입력 중 만료되면 5분 자동 연장(한 번).
  요청이 와도 저절로 늘어나지 않는다(연장은 사용자가 고른다).
* 같은 계정이 새로 로그인하면 기존 세션은 끊는다(계정당 유효 세션 1개).

체험 계정(demo_logins, 환경변수 DOCFLOW_DEMO_LOGINS)은 페스티벌 부스에서 여럿이 같이 쓰므로
동시 로그인을 허용하고, 실패해도 잠그지 않고, 비밀번호를 바꾸지 못하게 한다.
패널의 QR(/demo)로 비밀번호 없이 들어온다(demo_login). 이때는 들어올 때마다 그 사람만 쓰는
임시 계정(`demo~1a2b3c4d`)을 만들어 서로의 문서가 섞이지 않게 한다. 임시 계정은 만든 지
DEMO_TTL 이 지나면 다음 /demo 로그인 때 문서와 함께 지운다(expired_demo_users·retire).
`~` 는 발급 아이디 규칙에 없는 글자라 관리자가 만든 계정과 겹치지 않는다.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import re
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from docflow.service.catalog import NotFound
from docflow.service.db import LoginHistory, User, UserSession, now

SESSION_TTL = timedelta(minutes=30)
GRACE_TTL = timedelta(minutes=5)
#: 만료 시각을 조금 넘겨 온 자동 연장 요청도 받는다 (화면 타이머와 서버 시계 차이)
GRACE_WINDOW = timedelta(minutes=1)
MAX_FAILURES = 3
#: 임시 체험 계정을 남겨 두는 시간 (행사 당일엔 다시 들어와도 문서가 남는다)
DEMO_TTL = timedelta(hours=24)
#: 임시 체험 계정 아이디: 체험 계정 아이디 + 이 글자 + 무작위
DEMO_SEP = "~"

ROLES = ("user", "admin", "root")
STATUSES = ("active", "inactive", "deleted")
_LOGIN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,50}$")
_SPECIALS = set(string.punctuation)


# ---------------------------------------------------------------- 오류
class AuthError(Exception):
    """인증·권한 오류. status 는 HTTP 상태, reason 은 화면이 분기할 코드."""

    status = 401
    reason = "unauthenticated"

    def __init__(self, detail: str, reason: str | None = None, **extra) -> None:
        super().__init__(detail)
        if reason is not None:
            self.reason = reason
        self.extra = extra


class LoginFailed(AuthError):
    reason = "wrong_credentials"


class AccountLocked(AuthError):
    status = 423
    reason = "locked"


class AccountInactive(AuthError):
    status = 403
    reason = "inactive"


class SessionInvalid(AuthError):
    """로그인하지 않았거나 세션이 끝났다. reason: unauthenticated / expired / 끊긴 사유."""


class Forbidden(AuthError):
    status = 403
    reason = "forbidden"


class AccountConflict(AuthError):
    status = 409
    reason = "conflict"


# ---------------------------------------------------------------- 비밀번호
def password_problems(password: str) -> list[str]:
    """비밀번호 규칙에 어긋나는 점. 비어 있으면 통과."""
    problems = []
    if len(password) < 8:
        problems.append("8자 이상")
    if len(password) > 128:
        problems.append("128자 이하")
    for ok, what in (
        (any(c.isascii() and c.isupper() for c in password), "대문자"),
        (any(c.isascii() and c.islower() for c in password), "소문자"),
        (any(c.isdigit() for c in password), "숫자"),
        (any(c in _SPECIALS for c in password), "특수문자"),
    ):
        if not ok:
            problems.append(f"{what} 포함")
    return problems


def check_password_rule(password: str) -> None:
    if problems := password_problems(password):
        raise ValueError("비밀번호 규칙: " + ", ".join(problems))


def generate_password() -> str:
    """초기·초기화 비밀번호. 규칙을 지키는 12자."""
    groups = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*"]
    chars = [secrets.choice(g) for g in groups]
    chars += [secrets.choice("".join(groups)) for _ in range(8)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


#: scrypt 강도. 64MB 미만이어야 OpenSSL 기본 메모리 한도에 걸리지 않는다
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(got.hex(), digest)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_demo(login_id: str, demo_logins: frozenset[str]) -> bool:
    """체험 계정이거나 그 임시 계정이다."""
    return login_id.partition(DEMO_SEP)[0] in demo_logins


# ---------------------------------------------------------------- 서비스
@dataclass
class Client:
    """요청한 쪽 정보 (이력·세션에 남긴다)."""

    ip: str | None = None
    user_agent: str | None = None


@dataclass
class LoginResult:
    token: str
    session: UserSession


class AuthService:
    def __init__(
        self,
        session: Session,
        clock: Callable[[], datetime] = now,
        demo_logins: frozenset[str] = frozenset(),
    ) -> None:
        self.session = session
        self.clock = clock
        self.demo_logins = demo_logins

    # ------------------------------------------------------------ 로그인
    def login(self, login_id: str, password: str, client: Client | None = None) -> LoginResult:
        """로그인 (AU_01·AU_03). 성공하면 같은 계정의 다른 세션을 끊는다."""
        client = client or Client()
        user = self.session.scalar(select(User).where(User.login_id == login_id))
        if user is None:
            verify_password(password, _dummy_hash())  # 없는 아이디도 같은 시간이 걸리게
            self._history(None, login_id, "unknown_id", client)
            raise LoginFailed("아이디 또는 비밀번호가 틀렸다")
        if user.status != "active":
            self._history(user, login_id, "inactive", client)
            raise AccountInactive("사용할 수 없는 계정이다. 관리자에게 문의한다")
        if user.locked_at is not None:
            self._history(user, login_id, "locked", client)
            raise AccountLocked(_locked_message(user))
        if not verify_password(password, user.password_hash):
            if is_demo(user.login_id, self.demo_logins):
                self._history(user, login_id, "wrong_password", client)
                raise LoginFailed("아이디 또는 비밀번호가 틀렸다")
            user.failed_login_count += 1
            left = MAX_FAILURES - user.failed_login_count
            if left <= 0:
                user.locked_at = self.clock()
            self._history(user, login_id, "wrong_password", client)
            if left <= 0:
                raise AccountLocked(_locked_message(user))
            raise LoginFailed(f"아이디 또는 비밀번호가 틀렸다 (남은 횟수 {left}회)", remaining=left)

        return self._open_session(user, login_id, client)

    def demo_login(self, client: Client | None = None) -> LoginResult:
        """비밀번호 없이 그 사람만 쓰는 임시 체험 계정으로 들어간다 (페스티벌 패널 QR).

        체험 계정(여럿이면 아이디 순 첫 계정)을 끄면 임시 계정도 만들지 않는다.
        """
        if not self.demo_logins:
            raise Forbidden("체험 계정이 꺼져 있다")
        base = self.session.scalar(select(User).where(User.login_id == min(self.demo_logins)))
        if base is None or base.status != "active":
            raise AccountInactive("체험 계정을 쓸 수 없다")
        stamp = self.clock()
        user = User(
            login_id=f"{base.login_id}{DEMO_SEP}{secrets.token_hex(4)}",
            password_hash="!",  # 비밀번호로는 들어올 수 없다 (verify_password 가 늘 거짓)
            name=base.name,
            department=base.department,
            must_change_password=False,
            created_by=base.id,
            created_at=stamp,
            updated_at=stamp,
        )
        self.session.add(user)
        self.session.flush()  # 세션 행이 계정 id 를 쓴다
        return self._open_session(user, user.login_id, client or Client())

    def expired_demo_users(self) -> list[User]:
        """만든 지 DEMO_TTL 이 지난 임시 체험 계정 (지우지 않은 것)."""
        prefixes = [f"{d}{DEMO_SEP}" for d in self.demo_logins]
        if not prefixes:
            return []
        query = select(User).where(
            User.status != "deleted",
            User.created_at <= self.clock() - DEMO_TTL,
            or_(*(User.login_id.startswith(p, autoescape=True) for p in prefixes)),
        )
        return list(self.session.scalars(query))

    def retire(self, user: User) -> None:
        """임시 체험 계정을 끝낸다: 세션을 끊고 지운 계정으로 둔다 (문서는 부르는 쪽이 지운다)."""
        self._revoke_all(user, "account_disabled")
        user.status = "deleted"
        user.updated_at = self.clock()
        self.session.commit()

    def _open_session(self, user: User, login_id: str, client: Client) -> LoginResult:
        """로그인 성공. 체험 계정이 아니면 같은 계정의 다른 세션을 끊는다."""
        stamp = self.clock()
        user.failed_login_count = 0
        user.last_login_at = stamp
        if not is_demo(user.login_id, self.demo_logins):
            self._revoke_all(user, "duplicate_login")
        token = secrets.token_urlsafe(32)
        row = UserSession(
            id=token_hash(token),
            user_id=user.id,
            created_at=stamp,
            expires_at=stamp + SESSION_TTL,
            ip=client.ip,
            user_agent=(client.user_agent or "")[:300] or None,
        )
        self.session.add(row)
        self._history(user, login_id, None, client)
        return LoginResult(token=token, session=row)

    def authenticate(self, token: str | None) -> UserSession:
        """요청의 세션. 없거나 끝났으면 SessionInvalid (끊긴 사유를 reason 에 담는다)."""
        row = self._session_of(token)
        if row.expires_at <= self.clock():
            raise SessionInvalid("세션이 만료됐다. 다시 로그인한다", "expired")
        return row

    def logout(self, token: str | None) -> None:
        row = self.session.get(UserSession, token_hash(token)) if token else None
        if row is not None and row.revoked_at is None:
            row.revoked_at, row.revoke_reason = self.clock(), "logout"
            self.session.commit()

    def extend(self, token: str | None) -> UserSession:
        """연장 버튼 (AU_04). 지금부터 다시 30분, 자동 연장도 다시 쓸 수 있다."""
        row = self.authenticate(token)
        stamp = self.clock()
        row.expires_at = stamp + SESSION_TTL
        row.extended_at = stamp
        row.grace_used = False
        self.session.commit()
        return row

    def grace(self, token: str | None) -> UserSession:
        """입력 중 만료 (AU_04). 만료 시각에서 5분 늘린다. 연장 버튼을 누르기 전까지 한 번만."""
        row = self._session_of(token)
        stamp = self.clock()
        if row.expires_at + GRACE_WINDOW <= stamp:
            raise SessionInvalid("세션이 만료됐다. 다시 로그인한다", "expired")
        if row.grace_used:
            raise Forbidden("자동 연장은 한 번만 된다. 연장 버튼을 누른다", "grace_used")
        row.expires_at = max(row.expires_at, stamp) + GRACE_TTL
        row.grace_used = True
        self.session.commit()
        return row

    def change_password(self, user: User, current: str, new: str) -> None:
        """비밀번호 변경 (AU_02·AU_05). 지금 세션은 유지한다."""
        if is_demo(user.login_id, self.demo_logins):
            raise Forbidden("체험 계정은 비밀번호를 바꾸지 않는다")
        if not verify_password(current, user.password_hash):
            raise LoginFailed("현재 비밀번호가 틀렸다", "wrong_password")
        check_password_rule(new)
        if current == new:
            raise ValueError("지금과 다른 비밀번호를 쓴다")
        user.password_hash = hash_password(new)
        user.must_change_password = False
        user.updated_at = self.clock()
        self.session.commit()

    # ------------------------------------------------------------ 계정 관리 (AD_18~22)
    def create_user(
        self,
        actor: User | None,
        login_id: str,
        name: str,
        *,
        role: str = "user",
        department: str = "",
        phone: str | None = None,
        email: str | None = None,
    ) -> tuple[User, str]:
        """계정 발급. 초기 비밀번호를 만들어 돌려준다(한 번만 보여 준다).

        actor 가 None 이면 서버 CLI 다(루트를 만들 수 있는 유일한 길).
        """
        if role not in ROLES:
            raise ValueError(f"역할은 {ROLES} 중 하나다")
        if role == "root":
            if actor is not None:
                raise Forbidden("루트 관리자는 서버 CLI 로만 만든다")
            if self.session.scalar(select(func.count()).where(User.role == "root")):
                raise AccountConflict("루트 관리자는 이미 있다. 하나만 둔다")
        elif actor is not None:
            self._require_manage(actor, role)
        if not _LOGIN_ID_RE.match(login_id):
            raise ValueError("아이디는 영문·숫자·._- 3~50자다")
        if self.session.scalar(select(User).where(User.login_id == login_id)) is not None:
            raise AccountConflict(f"이미 있는 아이디다: {login_id}")
        if not name.strip():
            raise ValueError("이름이 비었다")
        password = generate_password()
        user = User(
            login_id=login_id,
            password_hash=hash_password(password),
            name=name.strip(),
            department=department.strip(),
            phone=phone,
            email=email,
            role=role,
            created_by=actor.id if actor else None,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        self.session.add(user)
        self.session.commit()
        return user, password

    def list_users(
        self, actor: User, *, role: str | None = None, status: str | None = None
    ) -> list[User]:
        """발급된 계정 목록 (AD_22). 지운 계정은 status=deleted 로 따로 고른다.

        임시 체험 계정은 발급한 계정이 아니라 넣지 않는다.
        """
        _require_admin(actor)
        query = select(User).where(User.login_id.not_like(f"%{DEMO_SEP}%")).order_by(User.id)
        if role is not None:
            query = query.where(User.role == role)
        query = (
            query.where(User.status == status) if status else query.where(User.status != "deleted")
        )
        return list(self.session.scalars(query))

    def get_user(self, user_id: int) -> User:
        user = self.session.get(User, user_id)
        if user is None:
            raise NotFound(f"계정 {user_id} 이(가) 없다")
        return user

    def update_user(self, actor: User, user_id: int, **fields: str | None) -> User:
        """계정 정보 수정 (AD_19): 이름·소속·연락처·이메일."""
        user = self._managed(actor, user_id)
        for key in ("name", "department", "phone", "email"):
            if key in fields:
                setattr(user, key, fields[key])
        if not (user.name or "").strip():
            raise ValueError("이름이 비었다")
        user.updated_at = self.clock()
        self.session.commit()
        return user

    def reset_password(self, actor: User | None, user_id: int) -> tuple[User, str]:
        """비밀번호 초기화 (AD_20). 새 초기 비밀번호를 돌려주고, 다음 로그인 때 변경을 강제한다.

        잠금도 함께 푼다(비밀번호를 잊어 잠긴 경우가 대부분이다). 세션은 끊는다.
        """
        user = self._managed(actor, user_id)
        password = generate_password()
        user.password_hash = hash_password(password)
        user.must_change_password = True
        self._unlock(user)
        self._revoke_all(user, "password_reset")
        self.session.commit()
        return user, password

    def unlock(self, actor: User | None, user_id: int) -> User:
        """잠금 해제 (AD_20). 관리자 계정은 루트만, 루트는 CLI(actor=None)로만."""
        user = self._managed(actor, user_id)
        self._unlock(user)
        self.session.commit()
        return user

    def set_status(self, actor: User, user_id: int, status: str) -> User:
        """비활성화·삭제·재활성화 (AD_21). 행은 남겨 기존 문서를 보존한다."""
        if status not in STATUSES:
            raise ValueError(f"상태는 {STATUSES} 중 하나다")
        user = self._managed(actor, user_id)
        if user.id == actor.id:
            raise Forbidden("자기 계정의 상태는 바꾸지 않는다")
        if user.status == "deleted" and status != "deleted":
            raise AccountConflict("삭제한 계정은 되살리지 않는다. 새로 발급한다")
        user.status = status
        user.updated_at = self.clock()
        if status != "active":
            self._revoke_all(user, "account_disabled")
        self.session.commit()
        return user

    # ------------------------------------------------------------ 내부
    def _session_of(self, token: str | None) -> UserSession:
        row = self.session.get(UserSession, token_hash(token)) if token else None
        if row is None:
            raise SessionInvalid("로그인이 필요하다")
        if row.revoked_at is not None:
            raise SessionInvalid(_REVOKED.get(row.revoke_reason or "", "로그아웃됐다"),
                                 row.revoke_reason or "logout")  # fmt: skip
        if row.user.status != "active":
            raise SessionInvalid("사용할 수 없는 계정이다", "account_disabled")
        return row

    def _managed(self, actor: User | None, user_id: int) -> User:
        """actor 가 관리할 수 있는 계정. actor=None 은 서버 CLI(모두 관리)."""
        user = self.get_user(user_id)
        if actor is not None:
            self._require_manage(actor, user.role)
        return user

    @staticmethod
    def _require_manage(actor: User, target_role: str) -> None:
        _require_admin(actor)
        if target_role == "root":
            raise Forbidden("루트 관리자는 서버 CLI 로만 관리한다")
        if target_role == "admin" and actor.role != "root":
            raise Forbidden("관리자 계정은 루트 관리자만 관리한다")

    def _unlock(self, user: User) -> None:
        user.locked_at = None
        user.failed_login_count = 0
        user.updated_at = self.clock()

    def _revoke_all(self, user: User, reason: str) -> None:
        stamp = self.clock()
        for row in self.session.scalars(
            select(UserSession).where(
                UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
            )
        ):
            row.revoked_at, row.revoke_reason = stamp, reason

    def _history(self, user: User | None, login_id: str, fail: str | None, client: Client) -> None:
        self.session.add(
            LoginHistory(
                user_id=user.id if user else None,
                login_id=login_id[:50],
                success=fail is None,
                fail_reason=fail,
                ip=client.ip,
                user_agent=(client.user_agent or "")[:300] or None,
                at=self.clock(),
            )
        )
        self.session.commit()


def _require_admin(actor: User) -> None:
    if actor.role not in ("admin", "root"):
        raise Forbidden("관리자만 쓸 수 있다")


def _locked_message(user: User) -> str:
    who = (
        "서버 관리자(CLI)"
        if user.role == "root"
        else ("루트 관리자" if user.role == "admin" else "관리자")
    )
    return f"로그인 {MAX_FAILURES}회 실패로 잠긴 계정이다. {who}에게 해제를 요청한다"


_REVOKED = {
    "duplicate_login": "다른 곳에서 같은 계정으로 로그인해 로그아웃됐다",
    "password_reset": "비밀번호가 초기화돼 로그아웃됐다. 새 비밀번호로 로그인한다",
    "account_disabled": "사용할 수 없는 계정이다",
    "logout": "로그아웃됐다",
}


@functools.cache
def _dummy_hash() -> str:
    """없는 아이디로 로그인할 때 비교에 쓰는 해시 (응답 시간으로 아이디 존재를 알 수 없게)."""
    return hash_password("Dummy#password1")
