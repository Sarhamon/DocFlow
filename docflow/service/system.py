"""시스템 설정 (M9, AD_27·AD_28) — LLM 서버 설정과 변환기·LLM 상태.

LLM 은 M10 에서 쓴다. 여기서는 주소·모델을 정하고 서버가 응답하는지만 본다. 기본값은 같은 PC 의
Ollama 와 EXAONE 3.5 7.8B 다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from docflow.service.db import SystemSetting

#: 설정 키 -> 기본값
DEFAULTS = {
    "llm_url": "http://127.0.0.1:11434",
    "llm_model": "exaone3.5:7.8b",
}


def get_settings(session: Session) -> dict[str, str]:
    saved = {row.key: row.value for row in session.query(SystemSetting)}
    return {key: saved.get(key, default) for key, default in DEFAULTS.items()}


def save_settings(session: Session, values: dict[str, str]) -> dict[str, str]:
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"없는 설정이다: {key}")
        value = value.strip()
        if key == "llm_url" and urlparse(value).scheme not in ("http", "https"):
            raise ValueError("LLM 서버 주소는 http:// 나 https:// 로 시작한다")
        if key == "llm_model" and not value:
            raise ValueError("모델 이름이 비었다")
        row = session.get(SystemSetting, key)
        if row is None:
            session.add(SystemSetting(key=key, value=value))
        else:
            row.value = value
    session.commit()
    return get_settings(session)


def llm_status(url: str, model: str, timeout: float = 2.0) -> dict[str, Any]:
    """Ollama 서버가 응답하는지, 모델을 받아 두었는지 (`GET /api/tags`)."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as res:
            tags = json.load(res)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "reachable": False, "models": [], "detail": f"연결 실패: {reason}"}
    models = [m.get("name", "") for m in tags.get("models", [])]
    found = model in models
    detail = "모델 준비됨" if found else f"서버는 응답하지만 {model} 모델이 없다 (ollama pull)"
    return {"ok": found, "reachable": True, "models": models, "detail": detail}
