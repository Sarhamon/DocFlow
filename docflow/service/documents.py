"""문서 서비스 — 새 문서 만들기, 임시저장, 검증, 산출물 만들기·내려받기, 내 문서함.

문서는 만들 때의 서식 버전에 묶인다. 서식이 개정돼도 작성 중인 문서와 완료 문서는 그 버전의
템플릿·스키마로 계속 검증하고 만든다.

상태는 두 가지다.

* draft     작성 중. 입력값을 고치고 임시저장한다.
* completed 요청한 형식(HWP·PDF)을 모두 만들었다. 고치려면 복제해서 새 문서로 쓴다(MY_06).
  변환이 일부 실패하면 HWPX 는 보존하고 draft 로 둔다. 다시 만들면 다음 판이 된다.

owner_id 가 None 이면 계정 구분 없이 모든 문서를 본다(로그인 M7 전). 값이 있으면 그 계정의
문서만 보이고 남의 문서는 없는 것으로 다룬다.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docflow.convert import ConversionJob, ConversionResult, Converter
from docflow.generate import Leftover
from docflow.pipeline import ParsedInput, make_document, parse_input
from docflow.schema.model import FormSchema
from docflow.service import crosscheck, crypto
from docflow.service.catalog import NotFound, get_form, load_schema
from docflow.service.db import Artifact, Document, DocumentEvent, now
from docflow.service.rules import config_for
from docflow.service.storage import FileStore
from docflow.validate import Issue, ValidationConfig, applicable_rules, validate

STATUSES = ("draft", "completed")
#: 파일 이름에 쓸 수 없는 글자
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

#: 한컴 변환은 한 번에 하나만 한다. 변환 전에 창 없는 한글을 정리하므로, 동시에 돌면 다른
#: 요청이 띄운 한글을 끌 수 있다. 워커로 분리할 때(M12) 작업 대기열이 이 자리를 맡는다.
_CONVERT_LOCK = threading.Lock()


class Conflict(RuntimeError):
    """지금 상태에서는 할 수 없다 (완료 문서 수정 등)."""


class ValidationFailed(RuntimeError):
    """검증 오류가 있어 만들지 않았다."""

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__(f"검증 오류 {sum(i.severity == 'error' for i in issues)}건")
        self.issues = issues


@dataclass
class GenerateOutcome:
    document: Document
    revision: int
    #: 오류가 아닌 검증 결과 (경고)
    issues: list[Issue] = field(default_factory=list)
    leftovers: list[Leftover] = field(default_factory=list)
    #: 변환하지 못한 형식 -> 사유
    conversion_errors: dict[str, str] = field(default_factory=dict)
    #: 계산하지 못한 수식 (좌표, 사유)
    skipped_formulas: list[tuple[str, str]] = field(default_factory=list)


class Serialized:
    """변환을 한 번에 하나만 하도록 감싼다. 문서 생성과 관리자의 서식 업로드(HWP)가 같이 쓴다."""

    def __init__(self, inner: Converter) -> None:
        self.inner = inner

    def convert(self, job: ConversionJob) -> ConversionResult:
        with _CONVERT_LOCK:
            return self.inner.convert(job)


def issue_dict(issue: Issue) -> dict:
    return {
        "code": issue.code,
        "severity": issue.severity,
        "where": issue.where,
        "location": asdict(issue.location),
        "message": issue.message,
        "action": issue.action,
    }


def safe_name(text: str) -> str:
    return _UNSAFE_RE.sub("_", " ".join(text.split())).strip(" ._") or "문서"


class DocumentService:
    def __init__(
        self, session: Session, store: FileStore, converter: Converter | None = None
    ) -> None:
        self.session = session
        self.store = store
        self.converter = Serialized(converter) if converter is not None else None

    # ------------------------------------------------------------ 조회
    def get(self, doc_id: int, owner_id: int | None = None) -> Document:
        doc = self.session.get(Document, doc_id)
        if doc is None or (owner_id is not None and doc.owner_id != owner_id):
            raise NotFound(f"문서 {doc_id} 이(가) 없다")
        return doc

    def schema_of(self, doc: Document) -> FormSchema:
        return load_schema(doc.form_version)

    def rules_of(self, doc: Document) -> ValidationConfig:
        """관리자가 정한 규칙 설정 (전체 on/off + 이 서식의 예외)."""
        return config_for(self.session, doc.form_version.form_id)

    def raw_input(self, doc: Document) -> dict:
        return json.loads(doc.input_json or "{}")

    def list(
        self,
        owner_id: int | None = None,
        *,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[Document], int]:
        """내 문서함 (작성 중 / 완료). 최근에 고친 순서."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"상태는 {STATUSES} 중 하나다")
        query = select(Document)
        if owner_id is not None:
            query = query.where(Document.owner_id == owner_id)
        if status is not None:
            query = query.where(Document.status == status)
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        page, size = max(page, 1), min(max(size, 1), 100)
        query = query.order_by(Document.updated_at.desc(), Document.id.desc())
        items = list(self.session.scalars(query.offset((page - 1) * size).limit(size)))
        return items, total

    def history(self, doc_id: int, owner_id: int | None = None) -> list[DocumentEvent]:
        return list(self.get(doc_id, owner_id).events)

    # ------------------------------------------------------------ 작성
    def create(
        self,
        form_id: int,
        owner_id: int | None = None,
        *,
        title: str | None = None,
        public_only: bool = True,
    ) -> Document:
        """새 문서 (FM_09). 서식의 최신 버전으로 만든다.

        public_only=False 는 관리자가 공개 전 서식을 점검용으로 써 볼 때다.
        """
        form = get_form(self.session, form_id, public_only=public_only)
        doc = Document(
            owner_id=owner_id,
            form_version_id=form.latest.id,
            title=(title or form.title).strip() or form.title,
        )
        self.session.add(doc)
        self.session.flush()
        self._event(doc, "created", note=f"서식 {form.id} 버전 {form.latest.version}")
        self.session.commit()
        return doc

    def save_input(self, doc_id: int, raw: dict, owner_id: int | None = None) -> Document:
        """임시저장 (IN_12). 서식에 맞지 않는 입력(없는 칸·열·변형)은 InputError 로 거부한다."""
        doc = self._draft(doc_id, owner_id)
        parse_input(self.schema_of(doc), raw)
        doc.input_json = json.dumps(raw, ensure_ascii=False)
        doc.updated_at = now()
        self.session.commit()
        return doc

    def rename(self, doc_id: int, title: str, owner_id: int | None = None) -> Document:
        doc = self.get(doc_id, owner_id)
        if not title.strip():
            raise ValueError("문서 이름이 비었다")
        doc.title = title.strip()
        doc.updated_at = now()
        self.session.commit()
        return doc

    def copy(self, doc_id: int, owner_id: int | None = None) -> Document:
        """재수정(복제, MY_06). 같은 서식 버전·입력값으로 새 작성 중 문서를 만든다."""
        src = self.get(doc_id, owner_id)
        doc = Document(
            owner_id=src.owner_id if owner_id is None else owner_id,
            form_version_id=src.form_version_id,
            title=src.title,
            input_json=src.input_json,
        )
        self.session.add(doc)
        self.session.flush()
        self._event(doc, "copied", input_json=src.input_json, note=f"문서 {src.id} 에서 복제")
        self.session.commit()
        return doc

    def delete(self, doc_id: int, owner_id: int | None = None) -> None:
        """작성 중 문서 삭제 (MY_03). 완료 문서는 이력이라 지우지 않는다."""
        doc = self._draft(doc_id, owner_id)
        crosscheck.unlink_all(self.session, doc)
        self.store.delete_prefix(f"documents/{doc.id}")
        self.session.delete(doc)
        self.session.commit()

    # ------------------------------------------------------------ 검증·만들기
    def validate(self, doc_id: int, owner_id: int | None = None) -> list[Issue]:
        """입력 완료 검증 (VA_01). 결과를 이력에 남긴다 (MY_07)."""
        doc = self.get(doc_id, owner_id)
        schema = self.schema_of(doc)
        data = parse_input(schema, self.raw_input(doc)).data
        issues = validate(schema, data, self.rules_of(doc)) + crosscheck.check(
            self.session, doc, schema, data
        )
        self._event(doc, "validated", input_json=doc.input_json, issues=issues)
        self.session.commit()
        return issues

    def check(self, doc_id: int, raw: dict, owner_id: int | None = None) -> list[Issue]:
        """입력 중 실시간 점검 (IN_13~15). 저장하지 않고 이력도 남기지 않는다."""
        doc = self.get(doc_id, owner_id)
        schema = self.schema_of(doc)
        data = parse_input(schema, raw).data
        return validate(schema, data, self.rules_of(doc)) + crosscheck.check(
            self.session, doc, schema, data
        )

    def generate(
        self,
        doc_id: int,
        owner_id: int | None = None,
        *,
        formats: tuple[str, ...] = ("HWP", "PDF"),
        force: bool = False,
    ) -> GenerateOutcome:
        """검증 → HWPX → 변환 → 산출물 저장. 검증 오류가 있으면 ValidationFailed."""
        doc = self._draft(doc_id, owner_id)
        schema = self.schema_of(doc)
        parsed: ParsedInput = parse_input(schema, self.raw_input(doc))
        version = doc.form_version
        template = self.store.local_path(version.template_key)
        name = self._file_stem(doc)
        config = self.rules_of(doc)

        with tempfile.TemporaryDirectory(prefix="dfg") as tmp:
            result = make_document(
                schema, parsed, template, Path(tmp), name,
                converter=self.converter, formats=formats, force=force, config=config,
            )  # fmt: skip
            # 문서 간 대조는 경고라 만들기를 막지 않는다. 결과와 이력에만 더한다
            result.issues += crosscheck.check(self.session, doc, schema, parsed.data)
            if result.generated is None:
                self._event(doc, "validated", input_json=doc.input_json, issues=result.issues)
                self.session.commit()
                raise ValidationFailed(result.issues)

            doc.revision += 1
            outputs = {"HWPX": result.generated.path}
            errors: dict[str, str] = {}
            if result.conversion is not None:
                outputs.update(result.conversion.outputs)
                errors = dict(result.conversion.errors)
            elif self.converter is None and formats:
                errors = {fmt: "변환기가 없다 (한컴오피스가 없는 환경)" for fmt in formats}
            for fmt, path in outputs.items():
                key = f"documents/{doc.id}/r{doc.revision}/{Path(path).name}"
                crypto.current().encrypt_file(Path(path))  # 주민등록번호가 든 파일일 수 있다
                size = self.store.put(key, Path(path))
                doc.artifacts.append(
                    Artifact(revision=doc.revision, format=fmt, file_key=key, size=size)
                )

        stamp = now()
        doc.updated_at = stamp
        if not errors:
            doc.status = "completed"
            doc.completed_at = stamp
        gen = result.generated
        skipped = [(f"t{f.table}.r{f.row}.c{f.col}", why) for f, why in gen.formulas.skipped]
        self._event(
            doc, "generated", input_json=doc.input_json, issues=result.issues,
            rules=sorted(applicable_rules(schema, config)),
            note=f"{doc.revision}판" + (f", 변환 실패 {sorted(errors)}" if errors else ""),
        )  # fmt: skip
        self.session.commit()
        return GenerateOutcome(
            document=doc,
            revision=doc.revision,
            issues=result.issues,
            leftovers=gen.leftovers,
            conversion_errors=errors,
            skipped_formulas=skipped,
        )

    def artifact(
        self, doc_id: int, fmt: str, owner_id: int | None = None, *, revision: int | None = None
    ) -> tuple[bytes, str]:
        """내려받을 파일 내용(복호화한 것)과 파일 이름 (SV_03·SV_04). 판을 주지 않으면 마지막 판."""
        doc = self.get(doc_id, owner_id)
        fmt = fmt.upper()
        revision = doc.revision if revision is None else revision
        for a in doc.artifacts:
            if a.revision == revision and a.format == fmt:
                data = crypto.current().read_file(self.store.local_path(a.file_key))
                return data, Path(a.file_key).name
        raise NotFound(f"문서 {doc_id} 의 {revision}판 {fmt} 파일이 없다")

    # ------------------------------------------------------------ 내부
    def _draft(self, doc_id: int, owner_id: int | None) -> Document:
        doc = self.get(doc_id, owner_id)
        if doc.status != "draft":
            raise Conflict(f"문서 {doc_id} 은(는) 완료됐다. 고치려면 복제한다")
        return doc

    def _file_stem(self, doc: Document) -> str:
        """파일 이름 규칙 `서식번호_문서명_작성일` (SV_04)."""
        form = doc.form_version.form
        number = " ".join(x for x in (form.program, form.form_no) if x)
        return "_".join(
            safe_name(x) for x in (number or "서식", doc.title, f"{datetime.now():%Y%m%d}")
        )

    def _event(
        self,
        doc: Document,
        kind: str,
        *,
        input_json: str | None = None,
        issues: list[Issue] | None = None,
        rules: list[str] | None = None,
        note: str = "",
    ) -> None:
        doc.events.append(
            DocumentEvent(
                kind=kind,
                form_version_id=doc.form_version_id,
                input_json=input_json,
                issues_json=None
                if issues is None
                else json.dumps([issue_dict(i) for i in issues], ensure_ascii=False),
                rules_json=None if rules is None else json.dumps(rules),
                note=note,
            )
        )
