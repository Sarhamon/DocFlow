import logging
from pathlib import Path

import pytest

from docflow.config import Settings, get_settings
from docflow.log import setup_logging


def make(tmp_path: Path) -> Settings:
    return Settings.from_root(tmp_path)


def test_경로는_루트_아래로_잡힌다(tmp_path):
    s = make(tmp_path)
    assert s.forms == tmp_path.resolve() / "forms"
    assert s.templates == tmp_path.resolve() / "templates"
    assert s.schemas == tmp_path.resolve() / "schemas"
    assert s.out == tmp_path.resolve() / "out"


def test_원본경로가_템플릿과_스키마로_미러링된다(tmp_path):
    s = make(tmp_path)
    src = s.forms / "ysu_forms" / "앵커" / "서식1.hwp"
    assert s.mirror(src, kind="template") == s.templates / "ysu_forms" / "앵커" / "서식1.hwpx"
    assert s.mirror(src, kind="schema") == s.schemas / "ysu_forms" / "앵커" / "서식1.json"


def test_미러링은_원본폴더_이름을_유지한다(tmp_path):
    """.gitignore 의 `ysu_forms/` 한 줄이 파생물까지 가리려면 폴더명이 같아야 한다."""
    s = make(tmp_path)
    dst = s.mirror(s.forms / "ysu_forms" / "a.hwp", kind="schema")
    assert "ysu_forms" in dst.parts


def test_forms_밖의_경로는_거부한다(tmp_path):
    s = make(tmp_path)
    with pytest.raises(ValueError):
        s.mirror(tmp_path / "other" / "a.hwp", kind="template")


def test_알수없는_종류는_거부한다(tmp_path):
    s = make(tmp_path)
    with pytest.raises(ValueError):
        s.mirror(s.forms / "a.hwp", kind="pdf")


def test_환경변수가_저장소_루트보다_우선한다(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCFLOW_ROOT", str(tmp_path))
    monkeypatch.setenv("DOCFLOW_OUT", str(tmp_path / "산출"))
    s = get_settings()
    assert s.root == tmp_path.resolve()
    assert s.out == (tmp_path / "산출").resolve()


def test_환경변수가_없으면_저장소_루트를_쓴다(monkeypatch):
    monkeypatch.delenv("DOCFLOW_ROOT", raising=False)
    monkeypatch.delenv("DOCFLOW_OUT", raising=False)
    s = get_settings()
    assert (s.root / "docflow").is_dir()


def test_로그레벨을_문자열로_지정한다():
    setup_logging("debug")
    assert logging.getLogger().level == logging.DEBUG
    setup_logging("WARNING")
    assert logging.getLogger().level == logging.WARNING


def test_잘못된_로그레벨은_거부한다():
    with pytest.raises(ValueError):
        setup_logging("시끄럽게")
