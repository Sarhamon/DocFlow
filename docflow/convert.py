"""문서 형식 변환 작업 — 변환기와 상관없이 같은 모양의 요청/결과.

지금 변환기는 같은 PC 의 한컴오피스(`adapters.hancom.HancomConverter`)다. 클라우드로 옮길 때
(M12) Windows 변환 워커나 다른 엔진으로 바꿔도 호출하는 쪽은 이 모듈만 본다. 그래서 이 모듈은
한컴·Windows 에 의존하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

#: 형식 -> 확장자
FORMATS = {"HWP": ".hwp", "PDF": ".pdf", "HWPX": ".hwpx"}

#: 산출물 파일의 첫 바이트. HWP 5.0 은 OLE 복합 문서, HWPX 는 zip 이다.
SIGNATURES = {
    "HWP": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    "PDF": b"%PDF-",
    "HWPX": b"PK\x03\x04",
}


@dataclass(frozen=True)
class ConversionJob:
    """변환 요청 하나: source 를 formats 로 바꿔 out_dir 에 stem.확장자 로 둔다."""

    source: Path
    formats: tuple[str, ...] = ("HWP", "PDF")
    #: 없으면 source 와 같은 폴더
    out_dir: Path | None = None
    #: 없으면 source 의 파일 이름
    stem: str | None = None

    def __post_init__(self) -> None:
        unknown = [f for f in self.formats if f not in FORMATS]
        if unknown or not self.formats:
            raise ValueError(f"알 수 없는 형식: {unknown or '(없음)'} (가능: {list(FORMATS)})")

    def target(self, fmt: str) -> Path:
        folder = Path(self.out_dir) if self.out_dir is not None else Path(self.source).parent
        return folder / f"{self.stem or Path(self.source).stem}{FORMATS[fmt]}"


@dataclass
class ConversionResult:
    job: ConversionJob
    #: 성공한 형식 -> 산출물 경로
    outputs: dict[str, Path] = field(default_factory=dict)
    #: 실패한 형식 -> 마지막 시도의 사유
    errors: dict[str, str] = field(default_factory=dict)
    #: 변환을 시도한 횟수 (재시도 포함)
    attempts: int = 0
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors and set(self.outputs) == set(self.job.formats)


class Converter(Protocol):
    def convert(self, job: ConversionJob) -> ConversionResult: ...


def check_output(path: Path, fmt: str) -> str | None:
    """산출물이 그 형식의 정상 파일로 보이면 None, 아니면 사유."""
    path = Path(path)
    if not path.exists():
        return "산출물이 없다"
    if path.stat().st_size == 0:
        return "산출물이 비어 있다"
    with path.open("rb") as f:
        head = f.read(len(SIGNATURES[fmt]))
    if head != SIGNATURES[fmt]:
        return f"{fmt} 파일이 아니다 (시작 바이트 {head[:8]!r})"
    return None
