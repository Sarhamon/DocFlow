"""파일 저장소 — 서식 버전별 템플릿과 문서 산출물. 로컬 폴더, S3, 또는 DB (M12).

키는 `/` 로 나눈 상대 경로다(`forms/12/v1.hwpx`, `documents/3/r2/문서.pdf`).
어느 저장소를 쓸지는 open_store 가 환경변수로 고른다.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Protocol

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import sessionmaker

from docflow.config import Settings
from docflow.service.db import StoredFile, connect


class FileStore(Protocol):
    def put(self, key: str, src: Path) -> int:
        """src 를 key 에 복사하고 크기를 돌려준다."""
        ...

    def local_path(self, key: str) -> Path:
        """읽을 수 있는 로컬 경로. 원격 저장소면 내려받은 임시 파일이다."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete_prefix(self, prefix: str) -> None:
        """prefix 아래를 모두 지운다."""
        ...


def open_store(settings: Settings) -> FileStore:
    """DOCFLOW_FILE_STORE=db 면 DB(stored_files 표), DOCFLOW_S3_BUCKET 이 있으면 S3
    (DOCFLOW_S3_PREFIX 아래), 없으면 data/files 폴더.

    S3 인증·지역은 boto3 표준을 따른다 (Fargate 작업 역할, AWS_DEFAULT_REGION 등).
    버킷에 닿지 않으면 바로 멈춘다: 그대로 띄우면 health 는 통과하고 파일마다 '없다'로 보인다.
    """
    if os.environ.get("DOCFLOW_FILE_STORE") == "db":
        return DbFileStore(connect(settings.database_url), cache=settings.data / "db-cache")
    bucket = os.environ.get("DOCFLOW_S3_BUCKET")
    if not bucket:
        return LocalFileStore(settings.data / "files")
    prefix = os.environ.get("DOCFLOW_S3_PREFIX", "")
    store = S3FileStore(bucket, prefix=prefix, cache=settings.data / "s3-cache")
    try:
        store.client.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        raise RuntimeError(
            f"S3 버킷 {bucket!r} 에 접근할 수 없다 ({code}). "
            "DOCFLOW_S3_BUCKET 이름과 작업 역할(IAM) 권한을 확인한다"
        ) from e
    return store


def _parts(key: str) -> tuple[str, ...]:
    parts = PurePosixPath(key).parts
    if not parts or key.startswith("/") or "//" in key or any(p in ("..", "") for p in parts):
        raise ValueError(f"잘못된 저장소 키: {key!r}")
    return parts


class LocalFileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*_parts(key))

    def put(self, key: str, src: Path) -> int:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return dst.stat().st_size

    def local_path(self, key: str) -> Path:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix.rstrip("/"))
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()


class S3FileStore:
    """S3 버킷의 prefix 아래. 읽을 때는 cache 폴더에 내려받는다.

    내려받은 파일은 객체의 ETag(내용) 별로 두어, 같은 키에 새 내용을 올리면 다시 받는다.
    """

    def __init__(self, bucket: str, *, prefix: str = "", cache: Path, client=None) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.cache = Path(cache)
        self.client = client or boto3.client("s3")

    def _key(self, key: str) -> str:
        return self.prefix + "/".join(_parts(key))

    def put(self, key: str, src: Path) -> int:
        self.client.upload_file(str(src), self.bucket, self._key(key))
        return Path(src).stat().st_size

    def local_path(self, key: str) -> Path:
        head = self._head(key)
        if head is None:
            raise FileNotFoundError(key)
        path = self.cache / head["ETag"].strip('"') / _parts(key)[-1]
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            self.client.download_file(self.bucket, self._key(key), str(part))
            os.replace(part, path)
        return path

    def exists(self, key: str) -> bool:
        return self._head(key) is not None

    def delete_prefix(self, prefix: str) -> None:
        base = self._key(prefix.rstrip("/"))
        self.client.delete_object(Bucket=self.bucket, Key=base)  # prefix 가 파일 하나일 때
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=base + "/"
        )
        for page in pages:
            if objects := [{"Key": o["Key"]} for o in page.get("Contents", [])]:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})

    def download_prefix(self, prefix: str, dest: Path) -> int:
        """prefix 아래 객체를 dest 폴더에 같은 경로로 내려받고 개수를 돌려준다.

        Fargate 에는 templates/ 를 붙일 곳이 없어 서식 적재가 S3 에서 받아 쓴다
        (sync_forms --from-s3).
        """
        base = self._key(prefix.rstrip("/")) + "/"
        count = 0
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=base
        ):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/"):  # 콘솔에서 만든 빈 폴더 표시
                    continue
                path = Path(dest).joinpath(*_parts(obj["Key"][len(base) :]))
                path.parent.mkdir(parents=True, exist_ok=True)
                self.client.download_file(self.bucket, obj["Key"], str(path))
                count += 1
        return count

    def _head(self, key: str) -> dict | None:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise


class DbFileStore:
    """DB 의 stored_files 표. 읽을 때는 cache 폴더에 내용(SHA-256)별로 풀어 둔다.

    S3 를 쓸 IAM 역할을 만들 수 없는 곳에서 쓴다(학교 공용 계정 — 역할 생성이 막혀 있다).
    파일이 작아(템플릿 183개 13MB) 한 행에 통째로 둔다.
    """

    def __init__(self, sessions: sessionmaker, *, cache: Path) -> None:
        self.sessions = sessions
        self.cache = Path(cache)

    def put(self, key: str, src: Path) -> int:
        name = "/".join(_parts(key))
        data = Path(src).read_bytes()
        with self.sessions() as s:
            row = s.get(StoredFile, name) or StoredFile(key=name)
            row.data, row.size, row.sha256 = data, len(data), hashlib.sha256(data).hexdigest()
            s.add(row)
            s.commit()
        return len(data)

    def local_path(self, key: str) -> Path:
        name = "/".join(_parts(key))
        with self.sessions() as s:
            digest = s.scalar(select(StoredFile.sha256).where(StoredFile.key == name))
            if digest is None:
                raise FileNotFoundError(key)
            path = self.cache / digest / _parts(key)[-1]
            if not path.is_file():
                data = s.scalar(select(StoredFile.data).where(StoredFile.key == name))
                path.parent.mkdir(parents=True, exist_ok=True)
                part = path.with_name(path.name + ".part")
                part.write_bytes(data)
                os.replace(part, path)
        return path

    def exists(self, key: str) -> bool:
        name = "/".join(_parts(key))
        with self.sessions() as s:
            return s.scalar(select(StoredFile.key).where(StoredFile.key == name)) is not None

    def delete_prefix(self, prefix: str) -> None:
        base = "/".join(_parts(prefix.rstrip("/")))
        # LIKE 의 특수 문자(`%`·`_`)는 글자 그대로 찾는다.
        # `documents/3` 이 `documents/30` 을 지우지 않게 `/` 까지 붙인다
        below = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
        with self.sessions() as s:
            s.execute(
                delete(StoredFile).where(
                    or_(StoredFile.key == base, StoredFile.key.like(below, escape="\\"))
                )
            )
            s.commit()
