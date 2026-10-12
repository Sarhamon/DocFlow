"""파일 저장소 — 서식 버전별 템플릿과 문서 산출물. 지금은 로컬 폴더, 클라우드로 옮길 때 S3.

키는 `/` 로 나눈 상대 경로다(`forms/12/v1.hwpx`, `documents/3/r2/문서.pdf`).
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from typing import Protocol


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


class LocalFileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        parts = PurePosixPath(key).parts
        if not parts or key.startswith("/") or any(p in ("..", "") for p in parts):
            raise ValueError(f"잘못된 저장소 키: {key!r}")
        return self.root.joinpath(*parts)

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
