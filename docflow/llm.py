"""LLM 호출 — 실행 환경과 상관없이 같은 모양의 요청/결과 (M10).

운영에서는 AWS 의 GPU 인스턴스에 띄운 Ollama(`adapters.ollama.OllamaClient`)를 부른다. 개발 PC 에는
GPU 가 없어 테스트는 가짜 클라이언트로 한다. 관리형 서비스(Bedrock 등)로 바꿔도 호출하는 쪽은
이 모듈만 본다.

출력은 늘 JSON 객체다. 요청에 JSON 스키마를 주면 서버가 그 모양으로만 답하게 한다(제약 출력).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LlmError(RuntimeError):
    """LLM 서버에 닿지 못했거나, 답을 JSON 객체로 읽을 수 없다."""


@dataclass(frozen=True)
class LlmRequest:
    #: 역할·규칙 (system 메시지)
    system: str
    #: 사용자 글과 채울 칸 목록 (user 메시지)
    prompt: str
    #: 답의 JSON 스키마
    schema: dict[str, Any] = field(default_factory=dict)
    #: 같은 입력에 같은 답이 나오도록 0
    temperature: float = 0.0


@dataclass
class LlmResult:
    data: dict[str, Any]
    model: str = ""
    elapsed: float = 0.0


class LlmClient(Protocol):
    def complete(self, request: LlmRequest) -> LlmResult: ...
