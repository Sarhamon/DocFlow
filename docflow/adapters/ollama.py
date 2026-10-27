"""Ollama 클라이언트 (M10) — `POST /api/chat` 에 JSON 스키마(`format`)를 주어 제약 출력을 받는다.

주소·모델은 시스템 설정(`service.system`)에서 온다. 서버는 AWS GPU 인스턴스에 두고, 개발 중에는
SSH 터널로 `http://127.0.0.1:11434` 에 붙인다. 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from docflow.llm import LlmError, LlmRequest, LlmResult

#: 7.8B 를 T4 에서 돌리면 긴 서식은 1분 가까이 걸린다
DEFAULT_TIMEOUT = 180.0
#: 문맥 길이(토큰). Ollama 기본값 4096 은 칸이 많은 서식의 프롬프트가 넘치고, 넘치면 오류 없이
#: 앞을 잘라 버린다. EXAONE 3.5 는 32K 까지 받고, T4(16GB)에 7.8B 4bit + 8K 는 넉넉히 들어간다.
DEFAULT_CONTEXT = 8192


class OllamaClient:
    def __init__(
        self,
        url: str,
        model: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        context: int = DEFAULT_CONTEXT,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.context = context
        self._open = opener  # 테스트가 바꾼다

    def complete(self, request: LlmRequest) -> LlmResult:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "stream": False,
            "format": request.schema or "json",
            "options": {"temperature": request.temperature, "num_ctx": self.context},
        }
        req = urllib.request.Request(
            f"{self.url}/api/chat",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        start = time.perf_counter()
        try:
            with self._open(req, timeout=self.timeout) as res:
                reply = json.load(res)
        except urllib.error.HTTPError as exc:
            raise LlmError(f"LLM 서버 오류 {exc.code}: {_detail(exc)}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LlmError(f"LLM 서버에 연결하지 못했다: {getattr(exc, 'reason', exc)}") from exc
        except ValueError as exc:
            raise LlmError("LLM 서버의 응답이 JSON 이 아니다") from exc
        content = (reply.get("message") or {}).get("content", "")
        try:
            data = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise LlmError("LLM 의 답을 JSON 으로 읽지 못했다") from exc
        if not isinstance(data, dict):
            raise LlmError("LLM 의 답이 JSON 객체가 아니다")
        return LlmResult(data, reply.get("model", self.model), time.perf_counter() - start)


def _detail(exc: urllib.error.HTTPError) -> str:
    try:
        return json.load(exc).get("error", "") or exc.reason
    except (ValueError, OSError, AttributeError):
        return str(exc.reason)
