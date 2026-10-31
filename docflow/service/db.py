"""서비스 DB 모델 (SQLAlchemy). 지금은 SQLite, 클라우드로 옮길 때(M12) RDS 로 바꾼다.

* 서식(forms)과 서식 버전(form_versions): 템플릿이 바뀌면 새 버전을 쌓는다. 버전마다 템플릿
  파일과 스키마를 따로 보관해서, 이미 쓴 문서는 작성 당시 버전으로 계속 만들 수 있다.
* 문서(documents): 서식 버전 하나에 묶인다. 입력값은 JSON 그대로 둔다(임시저장).
* 산출물(artifacts): 문서를 만들 때마다 판(revision)이 하나씩 올라가고 형식별 파일이 쌓인다.
* 이력(document_events): 만들기·검증·복제 때의 입력값, 검증 결과, 적용 규칙, 서식 버전.
* 계정(users)·세션(user_sessions)·로그인 이력(login_history): 관리자가 발급하는 계정(M7).
* 입력폼 설정(form_items): 관리자가 추출 항목을 빼거나 필수·형식·이름·순서를 고친 것(M9).
* 규칙 설정(rule_settings): 검증 규칙의 전체 on/off 와 서식별 예외(M9).
* 시스템 설정(system_settings): LLM 서버 주소·모델 같은 운영 설정 (키-값, M9).
* 감사 로그(audit_logs): 관리자의 변경과 문서 열람 (M11). 지우지 않는다.
* 문서 간 대조(cross_rules·document_links): 관리자가 정한 서식 쌍의 금액 대조와, 사용자가
  이어 둔 문서 쌍 (M11, VA_11·VA_12).
  서식 단위로 두어 개정해도 유지된다. 있는 표에 칸을 더하지 않으려고 따로 둔다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.types import TypeDecorator

from docflow.service import crypto


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Encrypted(TypeDecorator):
    """개인정보가 든 글 (문서 입력값). 저장할 때 암호화하고 읽을 때 푼다. 예전 평문도 읽는다."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return None if value is None else crypto.current().encrypt_text(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        return None if value is None else crypto.current().decrypt_text(value)


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
    items: Mapped[list[FormItem]] = relationship(cascade="all, delete-orphan")

    @property
    def latest(self) -> FormVersion | None:
        return self.versions[-1] if self.versions else None


class FormItem(Base):
    """입력폼 항목 하나에 대한 관리자 설정 (AD_09~12, AD_14). 비운 값은 추출한 대로 둔다."""

    __tablename__ = "form_items"
    __table_args__ = (UniqueConstraint("form_id", "kind", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("forms.id"), index=True)
    #: field 누름틀(키는 이름) / cell 표 칸(키는 t0.r1.c2) / column 반복영역 열(키는 t0.r3-9:4)
    kind: Mapped[str] = mapped_column(String(10))
    key: Mapped[str] = mapped_column(String(200))
    #: 입력폼에서 뺀다 (서식 글자를 그대로 둔다)
    hidden: Mapped[bool] = mapped_column(default=False)
    required: Mapped[bool | None] = mapped_column(default=None)
    input_type: Mapped[str | None] = mapped_column(String(20), default=None)
    label: Mapped[str | None] = mapped_column(String(200), default=None)
    #: 누름틀 표시 순서. 작은 것부터
    position: Mapped[int | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class RuleSetting(Base):
    """검증 규칙 설정 (AD_16·AD_17). form_id 가 없으면 전체 설정, 있으면 그 서식의 예외다.

    줄이 없으면 켜진 것이다. 예외는 전체로 켠 규칙을 그 서식에서만 끈다.
    """

    __tablename__ = "rule_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), index=True)
    form_id: Mapped[int | None] = mapped_column(ForeignKey("forms.id"), default=None, index=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class SystemSetting(Base):
    """운영 설정 하나 (AD_27). 값은 글자로 둔다."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class AuditLog(Base):
    """관리자 행위 한 건 (AD_26). 계정을 지워도 남도록 아이디를 같이 적는다."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=now, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    actor_login: Mapped[str] = mapped_column(String(50), default="")
    #: user.create, form.publish, document.view … (audit.ACTIONS)
    action: Mapped[str] = mapped_column(String(40), index=True)
    #: 대상 (계정 아이디, 서식 번호·이름, 문서 이름)
    target: Mapped[str] = mapped_column(String(300), default="")
    #: 바꾼 내용이나 열람 사유
    detail: Mapped[str] = mapped_column(Text, default="")


class CrossRule(Base):
    """문서 간 금액 대조 규칙 (VA_12). 기준 서식(계획서)과 대상 서식(정산서)의 금액을 견준다.

    키는 입력폼 항목 키다: 누름틀 이름, 표 칸(t0.r1.c2), 명단 열(t0.r3-9:4 — 행을 모두 더한다).
    여러 키를 주면 더한 값을 쓴다.
    """

    __tablename__ = "cross_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_form_id: Mapped[int] = mapped_column(ForeignKey("forms.id"))
    source_keys: Mapped[str] = mapped_column(Text)
    target_form_id: Mapped[int] = mapped_column(ForeignKey("forms.id"))
    target_keys: Mapped[str] = mapped_column(Text)
    #: eq 같아야 한다 / le 대상이 기준보다 크지 않아야 한다 (정산 ≤ 계획)
    op: Mapped[str] = mapped_column(String(5), default="eq")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=now)

    source_form: Mapped[Form] = relationship(foreign_keys=[source_form_id])
    target_form: Mapped[Form] = relationship(foreign_keys=[target_form_id])


class ChecklistItem(Base):
    """정성 판정 체크리스트 항목 (M10, ER_05). form_id 가 없으면 모든 서식에 묻는다.

    규정 해석이 필요한 물음이다(예: "다과비가 회의 목적과 인원에 비추어 과다하지 않은가").
    LLM 이 문서 입력값만 보고 적합·부적합·판단 불가와 근거 문장을 낸다.
    """

    __tablename__ = "checklist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    form_id: Mapped[int | None] = mapped_column(ForeignKey("forms.id"), default=None, index=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=now)

    form: Mapped[Form | None] = relationship()


class DocumentLink(Base):
    """사용자가 대조 규칙에 이어 둔 문서 쌍 (정산서 ↔ 계획서). 어느 쪽에서 이어도 양쪽에서 본다."""

    __tablename__ = "document_links"
    __table_args__ = (UniqueConstraint("document_id", "rule_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("cross_rules.id"))
    linked_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)


class DocumentAttachment(Base):
    """기안문에 붙인 문서 (사업 계획서 등). 본문의 `붙임 … 1부.` 가 가리키는 문서다.

    기안문은 계획서를 따로 만들어 함께 올린다. 계획서는 다른 서식이라 같은 파일에 없다.
    """

    __tablename__ = "document_attachments"
    __table_args__ = (UniqueConstraint("document_id", "attachment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    #: 붙임 순서 (1부터)
    position: Mapped[int] = mapped_column(default=1)


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
    #: 입력값 JSON (pipeline.parse_input 형식). 주민등록번호가 들 수 있어 암호화해 둔다
    input_json: Mapped[str] = mapped_column(Encrypted, default="{}")
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
    #: 그때의 입력값 JSON (암호화)
    input_json: Mapped[str | None] = mapped_column(Encrypted, default=None)
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

