"""경로·환경 설정.

원본 서식이 내부 자료라 저장소에 없으므로, 경로는 코드에 박지 않고 여기서 한 곳에 모은다.
변환·추출 결과는 원본 경로를 그대로 미러링한다(forms -> templates -> schemas).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    root: Path
    forms: Path
    templates: Path
    schemas: Path
    out: Path

    @classmethod
    def from_root(cls, root: Path, out: Path | None = None) -> Settings:
        root = Path(root).resolve()
        return cls(
            root=root,
            forms=root / "forms",
            templates=root / "templates",
            schemas=root / "schemas",
            out=Path(out).resolve() if out else root / "out",
        )

    def mirror(self, src: Path, *, kind: str) -> Path:
        """forms/ 아래 원본 경로를 templates/ 또는 schemas/ 의 같은 경로로 옮긴다.

        kind="template" -> templates/…/이름.hwpx, kind="schema" -> schemas/…/이름.json
        원본 폴더 이름(예: ysu_forms)이 그대로 이어져야 .gitignore 한 줄로 파생물까지 가려진다.
        """
        targets = {
            "template": (self.templates, ".hwpx"),
            "schema": (self.schemas, ".json"),
        }
        if kind not in targets:
            raise ValueError(f"알 수 없는 kind: {kind!r} (template/schema)")
        base, suffix = targets[kind]
        rel = Path(src).resolve().relative_to(self.forms)
        return (base / rel).with_suffix(suffix)


def get_settings() -> Settings:
    """환경변수 DOCFLOW_ROOT / DOCFLOW_OUT 이 있으면 우선하고, 없으면 저장소 루트를 쓴다."""
    root = Path(os.environ.get("DOCFLOW_ROOT") or _REPO_ROOT)
    out = os.environ.get("DOCFLOW_OUT")
    return Settings.from_root(root, Path(out) if out else None)
