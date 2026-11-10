"""DuckDNS 이름을 지금 이 컨테이너의 퍼블릭 IP 로 맞춘다 (M12).

    python tools/duckdns.py

Fargate 태스크는 다시 시작할 때마다 퍼블릭 IP 가 바뀌고, 쓸 수 있는 자원에 ALB·Elastic IP 가 없다.
그래서 컨테이너가 시작할 때 DuckDNS 에 한 번 등록해 `<이름>.duckdns.org` 로 들어오게 한다.
`ip=` 를 비워 보내면 DuckDNS 가 요청을 보낸 IP(태스크의 퍼블릭 IP)를 쓴다. 태스크가 사는 동안
IP 는 바뀌지 않으므로 한 번이면 된다.

    DOCFLOW_DUCKDNS_DOMAIN  이름 (`docflow-ysu` 또는 `docflow-ysu.duckdns.org`)
    DOCFLOW_DUCKDNS_TOKEN   DuckDNS 계정 토큰 (비밀. 작업 정의 환경변수라 정의를 공유하지 않는다)

둘 다 없으면 아무것도 하지 않는다(로컬). 등록에 실패해도 0 으로 끝낸다 —
앱은 떠야 IP 로라도 들어온다.
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.log import setup_logging  # noqa: E402

log = logging.getLogger("duckdns")

URL = "https://www.duckdns.org/update"


def update(domain: str, token: str, *, timeout: float = 10) -> bool:
    """DuckDNS 에 등록한다. 성공하면 True (응답 본문이 `OK`)."""
    name = domain.removesuffix(".duckdns.org")
    query = urllib.parse.urlencode({"domains": name, "token": token, "ip": ""})
    with urllib.request.urlopen(f"{URL}?{query}", timeout=timeout) as resp:
        return resp.read().decode().strip() == "OK"


def main() -> int:
    setup_logging()
    domain = os.environ.get("DOCFLOW_DUCKDNS_DOMAIN", "").strip()
    token = os.environ.get("DOCFLOW_DUCKDNS_TOKEN", "").strip()
    if not domain and not token:
        return 0
    if not domain or not token:
        log.warning("DOCFLOW_DUCKDNS_DOMAIN 과 DOCFLOW_DUCKDNS_TOKEN 이 둘 다 있어야 등록한다")
        return 0
    try:
        ok = update(domain, token)
    except OSError as exc:  # 연결 실패·시간 초과 (URLError 포함)
        log.warning("DuckDNS 등록 실패: %s", exc)
        return 0
    if ok:
        log.info("DuckDNS 등록: %s.duckdns.org", domain.removesuffix(".duckdns.org"))
    else:
        log.warning("DuckDNS 가 등록을 거절했다(KO). 이름·토큰을 확인한다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
