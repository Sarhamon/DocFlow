"""서비스 DB 모델 (SQLAlchemy). 지금은 SQLite, 클라우드로 옮길 때(M12) RDS 로 바꾼다.

* 서식(forms)과 서식 버전(form_versions): 템플릿이 바뀌면 새 버전을 쌓는다. 버전마다 템플릿
  파일과 스키마를 따로 보관해서, 이미 쓴 문서는 작성 당시 버전으로 계속 만들 수 있다.
* 문서(documents): 서식 버전 하나에 묶인다. 입력값은 JSON 그대로 둔다(임시저장).
* 산출물(artifacts): 문서를 만들 때마다 판(revision)이 하나씩 올라가고 형식별 파일이 쌓인다.
* 이력(document_events): 만들기·검증·복제 때의 입력값, 검증 결과, 적용 규칙, 서식 버전.
* 계정(users)·세션(user_sessions)·로그인 이력(login_history): 관리자가 발급하는 계정(M7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Form(Base):
    """서식 하나. 같은 템플릿 경로(key)는 같은 서식이다."""

    __tablename__ = "forms"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: templates/ 기준 템플릿 경로 (posix). 서식을 다시 등록해도 같은 서식으로 찾는다
    key: Mapped[str] = mapped_column(String(500), unique=True)
    #: 서식 번호 (`AID 5-05`)
    form_no: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(300))
    program: Mapped[str] = mapped_column(String(50), default="", index=True)
    category_no: Mapped[int | None] = mapped_column(default=None)
    category: Mapped[str] = mapped_column(String(100), default="", index=True)
    archived: Mapped[bool] = mapped_column(default=False)
    #: 사용자에게 보이는가 (관리자 공개/비공개, M9)
    public: Mapped[bool] = mapped_column(default=True)

    versions: Mapped[list[FormVersion]] = relationship(
        back_populates="form", order_by="FormVersion.version", cascade="all, delete-orphan"
    )

    @property
    def latest(self) -> FormVersion | None:
        return self.versions[-1] if self.versions else None


class FormVersion(Base):
    __tablename__ = "form_versions"
    __table_args__ = (UniqueConstraint("form_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("forms.id"))
    version: Mapped[int]
    #: 파일 저장소 안의 템플릿 위치
    template_key: Mapped[str] = mapped_column(String(500))
    template_sha256: Mapped[str] = mapped_column(String(64))
    schema_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=now)

    form: Mapped[Form] = relationship(back_populates="versions")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 작성자 계정. 로그인(M7) 전에는 비어 있다
    owner_id: Mapped[int | None] = mapped_column(default=None, index=True)
    form_version_id: Mapped[int] = mapped_column(ForeignKey("form_versions.id"))
    title: Mapped[str] = mapped_column(String(300))
    #: draft 작성 중 / completed 산출물을 만들었다
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    #: 입력값 JSON (pipeline.parse_input 형식)
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    #: 마지막으로 만든 판. 0 이면 아직 만들지 않았다
    revision: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    form_version: Mapped[FormVersion] = relationship()
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Artifact.id"
    )
    events: Mapped[list[DocumentEvent]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentEvent.id"
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifact_doc_rev", "document_id", "revision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    revision: Mapped[int]
    #: HWPX / HWP / PDF
    format: Mapped[str] = mapped_column(String(10))
    file_key: Mapped[str] = mapped_column(String(500))
    size: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(default=now)

    document: Mapped[Document] = relationship(back_populates="artifacts")


class DocumentEvent(Base):
    """문서 이력 한 건 (검증 결과 기록 MY_07, 수정 기록 MY_08)."""

    __tablename__ = "document_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    #: created / validated / generated / copied
    kind: Mapped[str] = mapped_column(String(20))
    at: Mapped[datetime] = mapped_column(default=now)
    form_version_id: Mapped[int] = mapped_column(ForeignKey("form_versions.id"))
    #: 그때의 입력값 JSON
    input_json: Mapped[str | None] = mapped_column(Text, default=None)
    #: 검증 결과 (Issue 목록 JSON)
    issues_json: Mapped[str | None] = mapped_column(Text, default=None)
    #: 이 서식에 적용된 규칙 코드 JSON (감사: 어떤 규칙으로 통과했나)
    rules_json: Mapped[str | None] = mapped_column(Text, default=None)
    #: 만든 판 번호, 복제 원본 등
    note: Mapped[str] = mapped_column(String(300), default="")

    document: Mapped[Document] = relationship(back_populates="events")


class User(Base):
    """관리자가 발급한 계정 (IAM 방식). 지워도 행은 남긴다(status=deleted, 문서 보존)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login_id: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(50))
    department: Mapped[str] = mapped_column(String(100), default="")
    phone: Mapped[str | None] = mapped_column(String(30), default=None)
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    #: user / admin / root
    role: Mapped[str] = mapped_column(String(10), default="user")
    #: active / inactive / deleted
    status: Mapped[str] = mapped_column(String(10), default="active", index=True)
    #: 초기·초기화된 비밀번호. 바꾸기 전에는 다른 기능을 쓰지 못한다
    must_change_password: Mapped[bool] = mapped_column(default=True)
    failed_login_count: Mapped[int] = mapped_column(default=0)
    #: 비어 있지 않으면 잠긴 계정. 관리자(관리자 계정은 루트)만 푼다
    locked_at: Mapped[datetime | None] = mapped_column(default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now)


class UserSession(Base):
    """로그인 상태. id 는 쿠키 토큰의 해시라 토큰 원문은 저장하지 않는다."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    extended_at: Mapped[datetime | None] = mapped_column(default=None)
    expires_at: Mapped[datetime]
    #: 입력 중 만료로 5분 자동 연장을 썼다. 연장 버튼을 누르면 다시 쓸 수 있다
    grace_used: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    #: logout / duplicate_login / password_reset / account_disabled
    revoke_reason: Mapped[str | None] = mapped_column(String(20), default=None)
    ip: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)

    user: Mapped[User] = relationship()


class LoginHistory(Base):
    """로그인 시도 한 번 (성공·실패 모두)."""

    __tablename__ = "login_history"
    __table_args__ = (Index("ix_login_history_user_id_at", "user_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 없는 아이디로 시도하면 비어 있다
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    login_id: Mapped[str] = mapped_column(String(50))
    success: Mapped[bool]
    #: wrong_password / locked / inactive / unknown_id
    fail_reason: Mapped[str | None] = mapped_column(String(30), default=None)
    ip: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)
    at: Mapped[datetime] = mapped_column(default=now)


def connect(url: str) -> sessionmaker:
    """DB 에 연결하고 표를 만든다. 세션 팩토리를 돌려준다."""
    sqlite = url.startswith("sqlite")
    if sqlite and (path := make_url(url).database) and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False} if sqlite else {})
    if sqlite:
        event.listen(engine, "connect", _sqlite_pragmas)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

