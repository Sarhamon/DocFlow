"""DuckDNS 등록 (tools/duckdns.py). 네트워크는 가짜 urlopen 으로 대신한다."""

from __future__ import annotations

import io
import urllib.error
import urllib.parse

import pytest

from tools import duckdns


class FakeDuckDNS:
    """urlopen 대신. 받은 요청 주소를 urls 에 모으고, reply(본문 또는 예외)로 답한다."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.reply: str | Exception = "OK"

    def __call__(self, url: str, timeout: float) -> io.BytesIO:
        self.urls.append(url)
        if isinstance(self.reply, Exception):
            raise self.reply
        return io.BytesIO(self.reply.encode())


@pytest.fixture
def fake(monkeypatch) -> FakeDuckDNS:
    f = FakeDuckDNS()
    monkeypatch.setattr(duckdns.urllib.request, "urlopen", f)
    monkeypatch.delenv("DOCFLOW_DUCKDNS_DOMAIN", raising=False)
    monkeypatch.delenv("DOCFLOW_DUCKDNS_TOKEN", raising=False)
    return f


def test_요청한_IP_로_등록한다(fake, monkeypatch):
    monkeypatch.setenv("DOCFLOW_DUCKDNS_DOMAIN", "docflow-ysu.duckdns.org")
    monkeypatch.setenv("DOCFLOW_DUCKDNS_TOKEN", "tok")
    assert duckdns.main() == 0
    [url] = fake.urls
    assert url.startswith(duckdns.URL + "?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    # 이름만 보내고, ip 를 비워 DuckDNS 가 요청을 보낸 IP 를 쓰게 한다
    assert query == {"domains": ["docflow-ysu"], "token": ["tok"], "ip": [""]}


def test_설정이_없으면_아무것도_하지_않는다(fake):
    assert duckdns.main() == 0
    assert fake.urls == []


def test_하나만_있으면_등록하지_않는다(fake, monkeypatch):
    monkeypatch.setenv("DOCFLOW_DUCKDNS_DOMAIN", "docflow-ysu")
    assert duckdns.main() == 0
    assert fake.urls == []


@pytest.mark.parametrize("reply", ["KO", urllib.error.URLError("timed out")])
def test_실패해도_앱은_뜬다(fake, monkeypatch, reply):
    monkeypatch.setenv("DOCFLOW_DUCKDNS_DOMAIN", "docflow-ysu")
    monkeypatch.setenv("DOCFLOW_DUCKDNS_TOKEN", "tok")
    fake.reply = reply
    assert duckdns.main() == 0
    assert len(fake.urls) == 1


def test_응답이_OK_인지로_성공을_가린다(fake):
    assert duckdns.update("docflow-ysu", "tok") is True
    fake.reply = "KO"
    assert duckdns.update("docflow-ysu", "tok") is False
