"""테스트 공통 설정. 개인정보 암호 키는 고정 값을 쓴다 (키 파일을 만들지 않도록)."""

import base64
import os

os.environ.setdefault("DOCFLOW_DATA_KEY", base64.b64encode(b"t" * 32).decode("ascii"))
# 테스트는 tmp 의 SQLite 를 쓴다. 셸에 운영 DB 주소가 있어도 테스트가 그 DB 에 쓰지 않게 지운다.
os.environ.pop("DOCFLOW_DATABASE_URL", None)
