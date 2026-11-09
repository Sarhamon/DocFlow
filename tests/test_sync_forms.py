"""서식 적재 도구 (tools/sync_forms.py).

--from-s3 는 Fargate 처럼 templates/ 가 없는 곳에서 쓴다.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from docflow.config import Settings
from docflow.service.catalog import sync_templates
from docflow.service.db import Form, connect
from docflow.service.storage import LocalFileStore
from tests import service_fixtures as fx
from tools import sync_forms

BUCKET = "docflow-test"


@pytest.fixture
def s3(tmp_path, monkeypatch):
    """templates/ 가 없는 실행 환경 + S3 버킷. DB 는 임시 SQLite."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-2")
    monkeypatch.setenv("DOCFLOW_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("DOCFLOW_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DOCFLOW_DATABASE_URL", f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    monkeypatch.setenv("DOCFLOW_S3_BUCKET", BUCKET)
    monkeypatch.setenv("DOCFLOW_S3_PREFIX", "prod")
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"}
        )
        yield client


def upload_templates(client, tmp_path) -> Settings:
    """로컬 templates/ 를 만들어 `aws s3 sync templates/ s3://…/prod/templates/` 처럼 올린다."""
    local = Settings.from_root(tmp_path / "local")
    fx.write_templates(local)
    for path in local.templates.rglob("*.hwpx"):
        rel = path.relative_to(local.templates).as_posix()
        client.upload_file(str(path), BUCKET, f"prod/templates/{rel}")
    client.put_object(Bucket=BUCKET, Key="prod/templates/", Body=b"")  # 콘솔의 폴더 표시
    return local


def registered(url: str) -> list[tuple[str, str]]:
    with connect(url)() as s:
        return sorted((f.key, f.latest.schema_json) for f in s.query(Form))


def test_S3_에서_받아_로컬과_같은_키로_등록한다(s3, tmp_path):
    local = upload_templates(s3, tmp_path)
    assert sync_forms.main(["--from-s3"]) == 0

    # 같은 templates/ 를 로컬에서 sync 한 결과와 서식 키·스키마(템플릿 경로 포함)가 같다
    url = f"sqlite:///{(tmp_path / 'local.sqlite').as_posix()}"
    with connect(url)() as s:
        sync_templates(s, LocalFileStore(tmp_path / "local-files"), local)
    got = registered(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    assert [k for k, _ in got] == sorted([fx.MEETING, fx.TRAFFIC])
    assert got == registered(url)
    assert s3.head_object(Bucket=BUCKET, Key="prod/forms/1/v1.hwpx")

    assert sync_forms.main(["--from-s3"]) == 0  # 다시 돌려도 그대로


def test_S3_에_템플릿이_없으면_실패한다(s3):
    assert sync_forms.main(["--from-s3"]) == 1


def test_버킷_없이_from_s3_는_실패한다(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCFLOW_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCFLOW_S3_BUCKET", raising=False)
    monkeypatch.delenv("DOCFLOW_DATA", raising=False)
    monkeypatch.setenv("DOCFLOW_DATABASE_URL", f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    assert sync_forms.main(["--from-s3"]) == 1


def test_폴더_밖으로_나가는_키는_받지_않는다(s3, tmp_path):
    s3.put_object(Bucket=BUCKET, Key="prod/templates/../evil.hwpx", Body=b"x")
    with pytest.raises(ValueError, match="잘못된 저장소 키"):
        sync_forms.main(["--from-s3"])
    assert not list(tmp_path.rglob("evil.hwpx"))
