"""로그인·세션·계정 관리 서비스 테스트 (M7). DB 는 임시 SQLite, 시간은 가짜 시계로 옮긴다."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from docflow.service.auth import (
    AccountConflict,
    AccountInactive,
    AccountLocked,
    AuthService,
    Forbidden,
    LoginFailed,
    SessionInvalid,
    generate_password,
    hash_password,
    password_problems,
    verify_password,
)
from docflow.service.db import LoginHistory, User, connect
from tests.service_fixtures import PASSWORD


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 18, 9, 0)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(tmp_path, clock):
    with connect(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")() as s:
        yield AuthService(s, clock)


def issue(svc: AuthService, login_id: str = "writer", role: str = "user") -> User:
    """비밀번호를 PASSWORD 로 바꾼 계정."""
    user, _ = svc.create_user(None, login_id, login_id, role=role)
    user.password_hash = hash_password(PASSWORD)
    user.must_change_password = False
    svc.session.commit()
    return user


# ------------------------------------------------------------ 비밀번호 규칙
@pytest.mark.parametrize(
    ("password", "problems"),
    [
        ("Abcdef1!", []),
        ("Ab1!", ["8자 이상"]),
        ("abcdefg1!", ["대문자 포함"]),
        ("ABCDEFG1!", ["소문자 포함"]),
        ("Abcdefgh!", ["숫자 포함"]),
        ("Abcdefgh1", ["특수문자 포함"]),
        ("한글비밀번호입니다1!", ["대문자 포함", "소문자 포함"]),
    ],
)
def test_비밀번호_규칙(password, problems):
    assert password_problems(password) == problems


def test_만든_초기_비밀번호는_규칙을_지킨다():
    assert all(password_problems(generate_password()) == [] for _ in range(50))


def test_비밀번호는_해시로만_남는다():
    stored = hash_password(PASSWORD)
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)
    assert not verify_password("Test#pass2", stored)
    assert hash_password(PASSWORD) != stored  # 솔트가 매번 다르다


# ------------------------------------------------------------ 로그인 (AU_01·AU_03)
def test_발급한_계정으로만_로그인한다(svc):
    issue(svc)
    assert svc.login("writer", PASSWORD).session.user.login_id == "writer"
    with pytest.raises(LoginFailed):
        svc.login("nobody", PASSWORD)
    history = svc.session.query(LoginHistory).order_by(LoginHistory.id).all()
    assert [(h.success, h.fail_reason) for h in history] == [(True, None), (False, "unknown_id")]


def test_3회_실패하면_잠기고_시간이_지나도_풀리지_않는다(svc, clock):
    user = issue(svc)
    with pytest.raises(LoginFailed) as first:
        svc.login("writer", "Wrong#pass1")
    assert first.value.extra == {"remaining": 2}
    with pytest.raises(LoginFailed):
        svc.login("writer", "Wrong#pass1")
    with pytest.raises(AccountLocked):
        svc.login("writer", "Wrong#pass1")
    assert user.locked_at is not None

    clock.advance(days=30)
    with pytest.raises(AccountLocked):
        svc.login("writer", PASSWORD)  # 맞는 비밀번호도 막는다


def test_성공하면_실패_횟수를_되돌린다(svc):
    user = issue(svc)
    for _ in range(2):
        with pytest.raises(LoginFailed):
            svc.login("writer", "Wrong#pass1")
    svc.login("writer", PASSWORD)
    assert user.failed_login_count == 0
    with pytest.raises(LoginFailed):
        svc.login("writer", "Wrong#pass1")  # 다시 3회 기회


def test_비활성_계정은_로그인하지_못한다(svc):
    root = issue(svc, "root", "root")
    user = issue(svc)
    svc.set_status(root, user.id, "inactive")
    with pytest.raises(AccountInactive):
        svc.login("writer", PASSWORD)


# ------------------------------------------------------------ 세션 (AU_04)
def test_세션은_30분이고_요청만으로는_늘지_않는다(svc, clock):
    issue(svc)
    token = svc.login("writer", PASSWORD).token
    clock.advance(minutes=29)
    svc.authenticate(token)
    clock.advance(minutes=1)
    with pytest.raises(SessionInvalid) as exc:
        svc.authenticate(token)
    assert exc.value.reason == "expired"


def test_연장하면_그때부터_다시_30분(svc, clock):
    issue(svc)
    token = svc.login("writer", PASSWORD).token
    clock.advance(minutes=20)
    row = svc.extend(token)
    assert row.expires_at == clock() + timedelta(minutes=30)
    clock.advance(minutes=29)
    svc.authenticate(token)


def test_입력_중_만료되면_5분_자동_연장은_한_번만(svc, clock):
    issue(svc)
    token = svc.login("writer", PASSWORD).token
    clock.advance(minutes=30, seconds=10)  # 화면 타이머가 조금 늦게 불러도 받는다
    assert svc.grace(token).expires_at == clock() + timedelta(minutes=5)
    with pytest.raises(Forbidden) as again:
        svc.grace(token)
    assert again.value.reason == "grace_used"

    svc.extend(token)  # 연장 버튼을 누르면 다시 쓸 수 있다
    clock.advance(minutes=30, seconds=10)
    svc.grace(token)


def test_만료되고_한참_지나면_자동_연장도_안_된다(svc, clock):
    issue(svc)
    token = svc.login("writer", PASSWORD).token
    clock.advance(minutes=32)
    with pytest.raises(SessionInvalid):
        svc.grace(token)


def test_같은_계정으로_다시_로그인하면_기존_세션은_끊긴다(svc):
    issue(svc)
    old = svc.login("writer", PASSWORD).token
    new = svc.login("writer", PASSWORD).token
    svc.authenticate(new)
    with pytest.raises(SessionInvalid) as exc:
        svc.authenticate(old)
    assert exc.value.reason == "duplicate_login"


def test_로그아웃한_세션은_쓸_수_없다(svc):
    issue(svc)
    token = svc.login("writer", PASSWORD).token
    svc.logout(token)
    with pytest.raises(SessionInvalid) as exc:
        svc.authenticate(token)
    assert exc.value.reason == "logout"


# ------------------------------------------------------------ 비밀번호 변경 (AU_02·AU_05)
def test_초기_비밀번호를_바꾸면_변경_강제가_풀린다(svc):
    user, initial = svc.create_user(None, "writer", "작성자")
    assert user.must_change_password
    with pytest.raises(ValueError, match="특수문자"):
        svc.change_password(user, initial, "Newpass12")
    with pytest.raises(ValueError, match="다른"):
        svc.change_password(user, initial, initial)
    with pytest.raises(LoginFailed):
        svc.change_password(user, "Wrong#pass1", "New#pass12")
    svc.change_password(user, initial, "New#pass12")
    assert not user.must_change_password
    svc.login("writer", "New#pass12")


# ------------------------------------------------------------ 계정 관리 (AD_18~22)
def test_루트는_하나만_CLI로_만든다(svc):
    issue(svc, "root", "root")
    with pytest.raises(AccountConflict):
        svc.create_user(None, "root2", "루트2", role="root")
    admin = issue(svc, "admin", "admin")
    with pytest.raises(Forbidden):
        svc.create_user(admin, "root3", "루트3", role="root")


def test_관리자_계정은_루트만_발급하고_푼다(svc):
    root = issue(svc, "root", "root")
    admin = issue(svc, "admin", "admin")
    with pytest.raises(Forbidden):
        svc.create_user(admin, "admin2", "관리자2", role="admin")
    other, _ = svc.create_user(root, "admin2", "관리자2", role="admin")
    other.locked_at = datetime(2026, 1, 1)
    with pytest.raises(Forbidden):
        svc.unlock(admin, other.id)
    assert svc.unlock(root, other.id).locked_at is None


def test_관리자는_사용자_계정을_발급하고_관리한다(svc):
    admin = issue(svc, "admin", "admin")
    user, password = svc.create_user(admin, "writer", "작성자", department="교무처")
    assert (user.role, user.created_by, user.must_change_password) == ("user", admin.id, True)
    assert password_problems(password) == []
    assert svc.update_user(admin, user.id, phone="010-0000-0000").phone == "010-0000-0000"
    with pytest.raises(AccountConflict):
        svc.create_user(admin, "writer", "중복")
    with pytest.raises(ValueError):
        svc.create_user(admin, "한글아이디", "작성자")


def test_일반_사용자는_계정을_관리하지_못한다(svc):
    user = issue(svc)
    with pytest.raises(Forbidden):
        svc.create_user(user, "other", "남")
    with pytest.raises(Forbidden):
        svc.list_users(user)


def test_비밀번호_초기화는_잠금을_풀고_세션을_끊는다(svc):
    admin = issue(svc, "admin", "admin")
    user = issue(svc)
    token = svc.login("writer", PASSWORD).token
    user.locked_at = datetime(2026, 1, 1)
    _, password = svc.reset_password(admin, user.id)
    assert (user.locked_at, user.must_change_password) == (None, True)
    with pytest.raises(SessionInvalid) as exc:
        svc.authenticate(token)
    assert exc.value.reason == "password_reset"
    svc.login("writer", password)


def test_삭제해도_계정은_남고_되살리지_않는다(svc):
    admin = issue(svc, "admin", "admin")
    user = issue(svc)
    token = svc.login("writer", PASSWORD).token
    svc.set_status(admin, user.id, "deleted")
    with pytest.raises(SessionInvalid):
        svc.authenticate(token)
    assert [u.login_id for u in svc.list_users(admin)] == ["admin"]
    assert [u.login_id for u in svc.list_users(admin, status="deleted")] == ["writer"]
    with pytest.raises(AccountConflict):
        svc.set_status(admin, user.id, "active")
    with pytest.raises(Forbidden):
        svc.set_status(admin, admin.id, "inactive")
