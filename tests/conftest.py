"""테스트 공통 설정. 개인정보 암호 키는 고정 값을 쓴다 (키 파일을 만들지 않도록)."""

import base64
import os

os.environ.setdefault("DOCFLOW_DATA_KEY", base64.b64encode(b"t" * 32).decode("ascii"))
