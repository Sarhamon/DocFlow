"""파일 저장소 (M12). 로컬 폴더와 S3 가 같은 약속을 지켜야 한다. S3 는 moto 로 흉내 낸다."""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from docflow.config import Settings
from docflow.service.storage import LocalFileStore, S3FileStore, open_store

BUCKET = "docflow-test"


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path, monkeypatch):
    if request.param == "local":
        yield LocalFileStore(tmp_path / "files")
        return
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-2")
    with mock_aws():
        boto3.client("s3").create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"}
        )
        yield S3FileStore(BUCKET, prefix="app", cache=tmp_path / "cache")


def src(tmp_path, data: bytes = b"hwpx") -> Path:
    path = tmp_path / "src.bin"
    path.write_bytes(data)
    return path


def test_넣은_파일을_그대로_읽는다(store, tmp_path):
    assert store.put("forms/12/v1.hwpx", src(tmp_path, b"abc")) == 3
    assert store.exists("forms/12/v1.hwpx")
    assert store.local_path("forms/12/v1.hwpx").read_bytes() == b"abc"


def test_한글_이름도_된다(store, tmp_path):
    store.put("documents/3/r2/교통비 지급 내역서.hwpx", src(tmp_path))
    assert store.local_path("documents/3/r2/교통비 지급 내역서.hwpx").read_bytes() == b"hwpx"


def test_없는_파일(store):
    assert not store.exists("forms/1/v1.hwpx")
    with pytest.raises(FileNotFoundError):
        store.local_path("forms/1/v1.hwpx")


def test_다시_넣으면_새_내용을_읽는다(store, tmp_path):
    store.put("forms/1/v1.hwpx", src(tmp_path, b"old"))
    store.local_path("forms/1/v1.hwpx")
    store.put("forms/1/v1.hwpx", src(tmp_path, b"new"))
    assert store.local_path("forms/1/v1.hwpx").read_bytes() == b"new"


def test_접두어_아래만_지운다(store, tmp_path):
    for key in ("documents/3/r1/a.hwpx", "documents/3/r2/a.hwpx", "documents/30/r1/a.hwpx"):
        store.put(key, src(tmp_path))
    store.local_path("documents/3/r1/a.hwpx")
    store.delete_prefix("documents/3")
    assert not store.exists("documents/3/r1/a.hwpx")
    assert not store.exists("documents/3/r2/a.hwpx")
    with pytest.raises(FileNotFoundError):
        store.local_path("documents/3/r1/a.hwpx")
    assert store.exists("documents/30/r1/a.hwpx")  # 3 으로 시작하는 다른 문서는 남는다


@pytest.mark.parametrize("key", ["", "/etc/passwd", "a/../b", "a//b"])
def test_잘못된_키는_거부한다(store, tmp_path, key):
    with pytest.raises(ValueError):
        store.put(key, src(tmp_path))


def test_버킷을_주지_않으면_로컬_폴더다(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCFLOW_S3_BUCKET", raising=False)
    store = open_store(Settings.from_root(tmp_path))
    assert isinstance(store, LocalFileStore)
    assert store.root == tmp_path.resolve() / "data" / "files"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-2")
    with mock_aws():
        boto3.client("s3").create_bucket(
            Bucket="prod-bucket", CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"}
        )
        yield


def test_버킷을_주면_S3다(tmp_path, monkeypatch, aws):
    monkeypatch.setenv("DOCFLOW_S3_BUCKET", "prod-bucket")
    monkeypatch.setenv("DOCFLOW_S3_PREFIX", "docflow")
    store = open_store(Settings.from_root(tmp_path))
    assert isinstance(store, S3FileStore)
    assert (store.bucket, store.prefix) == ("prod-bucket", "docflow/")


def test_없는_버킷이면_바로_멈춘다(tmp_path, monkeypatch, aws):
    """버킷 이름·권한이 틀리면 시작할 때 알린다 (파일마다 '없다'로 보이지 않게)."""
    monkeypatch.setenv("DOCFLOW_S3_BUCKET", "nope")
    with pytest.raises(RuntimeError, match="'nope'"):
        open_store(Settings.from_root(tmp_path))
