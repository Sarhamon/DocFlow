"""개인정보 암호화 (주민등록번호 등) — 문서 입력값과 만든 파일을 AES-256-GCM 으로 저장한다.

개인정보보호법상 주민등록번호는 암호화해 저장해야 한다. 입금계좌 확인서처럼 주민등록번호가
입력값에도, 만든 HWP·PDF 에도 들어가므로 둘 다 암호화한다.

* 입력값: DB 칸 타입(`db.Encrypted`)이 저장할 때 암호화하고 읽을 때 푼다. `enc:v1:` 로 시작한다.
* 파일: 저장소에 넣기 전에 파일째 암호화하고, 내려받을 때 푼다. `DFENC1` 로 시작한다.
* 예전에 평문으로 저장한 값·파일은 그대로 읽는다. `tools/encrypt_data.py` 로 한 번에 바꾼다.

키는 환경변수 `DOCFLOW_DATA_KEY`(32바이트를 base64 로) 또는 키 파일(기본 `secrets/data.key`)에서
읽는다. 키 파일이 없으면 처음 쓸 때 만든다. **키를 잃으면 암호화한 문서를 되살릴 수 없으므로**
키 파일은 DB(`data/`)와 따로 백업한다. 클라우드로 옮길 때(M12)는 KMS 로 바꾼다.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from docflow.config import Settings, get_settings

log = logging.getLogger(__name__)

TEXT_PREFIX = "enc:v1:"
FILE_MAGIC = b"DFENC1"
KEY_ENV = "DOCFLOW_DATA_KEY"
_NONCE = 12


class Cipher:
    """AES-256-GCM. 같은 값도 매번 다른 무작위 nonce 로 암호화한다."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("암호 키는 32바이트다")
        self._aes = AESGCM(key)

    def _seal(self, data: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        return nonce + self._aes.encrypt(nonce, data, aad)

    def _open(self, blob: bytes, aad: bytes) -> bytes:
        return self._aes.decrypt(blob[:_NONCE], blob[_NONCE:], aad)

    # 글 (DB 칸)
    def encrypt_text(self, text: str) -> str:
        if text.startswith(TEXT_PREFIX):
            return text  # 이미 암호화한 값
        blob = self._seal(text.encode("utf-8"), b"text")
        return TEXT_PREFIX + base64.b64encode(blob).decode("ascii")

    def decrypt_text(self, value: str) -> str:
        if not value.startswith(TEXT_PREFIX):
            return value  # 예전 평문
        blob = base64.b64decode(value[len(TEXT_PREFIX) :])
        return self._open(blob, b"text").decode("utf-8")

    # 파일 (만든 문서)
    def encrypt_bytes(self, data: bytes) -> bytes:
        if data.startswith(FILE_MAGIC):
            return data
        return FILE_MAGIC + self._seal(data, b"file")

    def decrypt_bytes(self, data: bytes) -> bytes:
        if not data.startswith(FILE_MAGIC):
            return data  # 예전 평문 파일
        return self._open(data[len(FILE_MAGIC) :], b"file")

    def encrypt_file(self, path: Path) -> None:
        """파일을 그 자리에서 암호화한다 (저장소에 넣기 전 임시 파일)."""
        path = Path(path)
        path.write_bytes(self.encrypt_bytes(path.read_bytes()))

    def read_file(self, path: Path) -> bytes:
        return self.decrypt_bytes(Path(path).read_bytes())


#: 주민등록번호: 생년월일 6자리(달 01~12, 날 01~31) + 성별 자리(1~8) + 6자리. 하이픈은 있어도
#: 없어도 된다. 하이픈 없는 13자리 계좌번호가 이 모양이면 함께 가려진다(가리는 쪽으로 틀린다).
_RRN_RE = re.compile(
    r"(?<!\d)(\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))\s*-?\s*([1-8])\d{6}(?!\d)"
)


def mask_rrn(text: str) -> str:
    """주민등록번호 뒷자리를 가린다 (`900101-1******`). 관리자 열람 화면에 쓴다."""
    return _RRN_RE.sub(r"\1-\2******", text)


def new_key() -> str:
    """새 키 (base64). 환경변수나 키 파일에 넣는다."""
    return base64.b64encode(os.urandom(32)).decode("ascii")


def load_key(settings: Settings) -> bytes:
    """환경변수가 있으면 그것을, 없으면 키 파일을 읽는다. 키 파일이 없으면 만든다."""
    if value := os.environ.get(KEY_ENV):
        return base64.b64decode(value)
    path = settings.key_file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_key() + "\n", encoding="ascii")
        try:
            path.chmod(0o600)
        except OSError:  # Windows 는 권한 비트를 거의 무시한다
            pass
        log.warning("암호 키를 새로 만들었다: %s — DB 와 따로 백업하세요", path)
    return base64.b64decode(path.read_text(encoding="ascii").strip())


_current: Cipher | None = None


def use(cipher: Cipher) -> None:
    """이 프로세스가 쓸 암호기를 정한다 (앱을 만들 때)."""
    global _current
    _current = cipher


def current() -> Cipher:
    """지금 암호기. 정하지 않았으면 기본 설정(환경변수·키 파일)으로 만든다."""
    global _current
    if _current is None:
        _current = Cipher(load_key(get_settings()))
    return _current
