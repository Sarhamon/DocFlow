# DocFlow 웹 앱 (M12). 코드만 담는다 — 서식·DB·파일은 RDS·S3 에, 암호 키는 환경변수로.
#
#   docker build -t docflow .
#   docker run -p 8000:8000 -e DOCFLOW_DATA_KEY=… -e DOCFLOW_DATABASE_URL=… -e DOCFLOW_S3_BUCKET=… docflow
#   (+ -e DOCFLOW_DUCKDNS_DOMAIN=… -e DOCFLOW_DUCKDNS_TOKEN=… 이면 시작할 때 DuckDNS 에 등록)
#
# 한컴 변환이 없어 HWPX 만 만든다. 서식 적재·루트 계정은 같은 이미지로 한 번 돌린다:
#   docker run --rm -e … docflow python tools/manage_users.py create-root <아이디> <이름>
#   docker run --rm -e … docflow python tools/sync_forms.py --from-s3   # S3 의 <prefix>/templates/ 에서
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOCFLOW_DATA=/var/lib/docflow

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY docflow ./docflow
COPY tools ./tools

# /app 은 쓸 수 없다: 암호 키가 환경변수로 오지 않으면 키 파일을 새로 만들지 못하고 멈춘다
# (컨테이너 안에서 만든 키는 재시작하면 사라져 문서를 되살릴 수 없다). 쓰는 곳은 DOCFLOW_DATA 뿐이다
RUN useradd --system --uid 10001 docflow \
    && mkdir -p /var/lib/docflow \
    && chown docflow /var/lib/docflow
USER docflow

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# 시작할 때 DuckDNS 이름을 이 태스크의 퍼블릭 IP 로 맞춘다(DOCFLOW_DUCKDNS_* 가 있을 때만, tools/duckdns.py).
# ALB 없이 태스크가 바로 노출되므로 프록시 헤더(X-Forwarded-For)는 믿지 않는다 — 믿으면 누구나
# 로그인 이력·감사 로그의 IP 를 꾸밀 수 있다
CMD ["sh", "-c", "python tools/duckdns.py; exec uvicorn --factory docflow.web.app:create_app --host 0.0.0.0 --port 8000"]
