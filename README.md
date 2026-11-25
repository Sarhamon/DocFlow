# DocFlow — 서식 기반 문서 자동 생성 및 검증 시스템

대학 재정지원사업 서식을 기반으로 **문서를 자동 생성하고, 서식·규정 준수 여부를 검증**하는 시스템입니다.
장기적으로는 결재 라인 자동화(반려/통과 판정)까지 확장합니다. 캡스톤 디자인(졸업작품) 프로젝트입니다.

- **사이트**: <http://docflow-ysu.duckdns.org> (HTTPS 가 없어 `http://` 까지 적습니다)
  · 체험: <http://docflow-ysu.duckdns.org/demo> (체험 계정으로 자동 로그인)
- **진행**: M0~M11 완료, M12(AWS 이전) 진행 중 — 2026-11-10 배포, 운영 작업 정의 `docflow:17`(2026-11-21)

| 문서 | 내용 |
|---|---|
| [`docs/design-notes.md`](docs/design-notes.md) | 설계 기록 — 서식 분석, 아키텍처 결정 근거, 검증 수치와 함정 |
| [`docs/functional-spec.md`](docs/functional-spec.md) | 기능명세서 (원본 [`specsheet.xlsx`](docs/specsheet.xlsx)) |
| [`docs/dev-roadmap.md`](docs/dev-roadmap.md) | 개발 계획 |
| [`docs/index.html`](docs/index.html) | 진행 현황 리포트 |
| [`docs/deploy-aws.md`](docs/deploy-aws.md) | AWS 배포·운영 (켜고 끄기 포함) |
| [`docs/demo.md`](docs/demo.md) | 시연 안내 |
| [`docs/tablestats.xlsx`](docs/tablestats.xlsx) | DB 테이블 명세서 |

## 1. 설계 핵심

자세한 근거는 [`design-notes.md`](docs/design-notes.md) 1~2장에 있습니다.

- **서식은 표가 전부다.** 서식 183개 중 46%가 누름틀이 없어, 표 셀 채우기가 주 메커니즘이고 누름틀은 보조입니다.
  채울 곳은 서식에 박힌 자리표시자(`○○○`, `2000.00.00`, `0,000,000원`)가 알려 주고, 명단·내역은 반복 행 영역 하나로 묶습니다.
- **HWP 처리는 하이브리드.** 템플릿을 HWPX 로 한 번 바꿔 두고 값은 zip+XML 로 넣습니다(한컴 없이 동작).
  한컴 COM 은 로컬의 원본 변환·`.hwp`/`.pdf` 산출에만 쓰고 `docflow/adapters/hancom.py` 에 가둡니다.
  한컴이 없는 서버는 HWPX 만 만들고, 올린 `.hwp` 는 hwp2hwpx(Java)로 받습니다.
- **검증은 규칙 코드, 문안은 LLM.** 필수 누락·날짜·금액 합계·문서 간 금액 대조는 재현성이 필요해 규칙 코드가 맡고,
  프롬프트로 칸 채우기·본문 초안·정성 판정은 LLM 이 맡습니다. LLM 결과도 사용자 확인과 규칙 검증을 거칩니다.
- **규칙은 자리표시자에서 유도한다.** 슬롯 4,953개를 손으로 정의하지 않고, 칸이 스스로 날짜·금액·합계 칸임을
  알려 주는 신호로 서식별 규칙을 만듭니다(`docflow/schema/rules.py`).
- **개인정보는 밖으로 내보내지 않는다.** LLM 은 로컬(Ollama + EXAONE 3.5 7.8B)에서 돌고, 계좌·주민등록번호는
  LLM 에 넘기지 않습니다. 주민등록번호가 들 수 있는 값·파일은 AES-256-GCM 으로 암호화해 저장합니다.
- **로그인은 IAM 방식.** 관리자가 계정을 발급하고, 가입·비밀번호 찾기는 없으며 첫 로그인 때 비밀번호를 바꿉니다.

## 2. 파이프라인

```
forms/<원본폴더>/**/*.hwp        원본 서식
    | tools/convert_templates.py        [한컴 COM]
templates/<원본폴더>/**/*.hwpx   편집 가능한 XML 템플릿 (원본 경로 미러링)
    | tools/extract_schema.py           [순수 파이썬]
schemas/<원본폴더>/**/*.json     필드 / 표 / 슬롯 / 반복영역 + 검증 체크리스트
    | tools/sync_forms.py               서식·버전 등록 (DB + data/)
    | 프롬프트 -> LLM 이 칸·본문 초안 채움 -> 사용자 확인      [Ollama]  (M10)
    | 입력값 -> validate -> generate      [순수 파이썬]
문서.hwpx
    | docflow/convert.py  변환 요청/결과
    | docflow/adapters/hancom.py        [한컴 COM · 자식 프로세스]
문서.hwp, 문서.pdf                 내려받기: 서식번호_문서명_작성일

올린 .hwp (서식 등록·명단 가져오기)
    | 한컴 COM, 한컴이 없는 서버는 docflow/adapters/hwp2hwpx.py   [Java · 자식 프로세스]
.hwpx                              그 뒤는 위와 같다 (스키마 추출·등록 / 명단 읽기)
```

## 3. 디렉터리 구조

```
DocFlow/
├─ docflow/
│  ├─ hwpx/document.py     # HWPX 읽기·쓰기(누름틀 + 표 셀 + 반복영역 행), 플랫폼 무관
│  ├─ hwpx/formula.py      # 표 수식 해석·재계산
│  ├─ schema/model.py      # 서식 스키마 모델 (v2)
│  ├─ schema/rules.py      # 자리표시자·합계·날짜·열 이름(성명·계좌…) 판정 (순수 함수)
│  ├─ schema/extract.py    # HWPX -> 스키마 추출기
│  ├─ validate.py          # 규칙 기반 검증 엔진 (칸 단위·문서 단위)
│  ├─ generate.py          # 입력값 -> HWPX (결재란 비우기·반복 행 정리·변형 선택·남은 자리표시자 경고)
│  ├─ pipeline.py          # 입력 JSON 해석 -> 검증 -> 생성 -> 변환 (CLI·웹이 함께 쓴다)
│  ├─ convert.py           # 형식 변환 요청/결과 인터페이스 (변환기와 무관)
│  ├─ service/             # DB 모델·서식 목록(버전)·문서 서비스·파일 저장소·로그인(auth)
│  │                       #   rules(규칙 설정)·audit(감사 로그)·operations(운영 현황)·crosscheck(문서 간 대조)
│  │                       #   roster(입금계좌 확인서 명단)·crypto(개인정보 암호화)·system(LLM 설정)
│  │                       #   prefill(프롬프트 일괄 입력)·draft(본문 초안)·review(정성 판정)
│  │                       #   profile(계정 정보로 칸 채우기)·attachments(붙임 문서)
│  ├─ llm.py               # LLM 요청/결과 인터페이스 (실행 환경과 무관)
│  ├─ web/app.py           # FastAPI (/api) — 사용자 API·계정 관리
│  ├─ web/admin_api.py     # 관리자: 서식 등록·개정·입력폼·카테고리·검증 규칙·시스템 (M9)
│  ├─ web/admin_ops.py     # 관리자: 문서 현황·오류 통계·로그인 이력·감사 로그·대조 규칙 (M11)
│  ├─ web/pages.py         # 화면 (Jinja2 쪽 틀 + static/ 스크립트)
│  ├─ config.py · log.py   # 경로·환경 설정, 로깅
│  ├─ adapters/hancom.py   # 한컴 COM 어댑터 (로컬 전용, 격리 대상)
│  ├─ adapters/hwp2hwpx.py # 올린 HWP -> HWPX (한컴 없는 서버용, Java 자식 프로세스)
│  └─ adapters/ollama.py   # Ollama 클라이언트 (JSON 스키마 제약 출력)
├─ tools/
│  ├─ convert_templates.py # 원본 .hwp -> .hwpx 일괄 변환
│  ├─ hwp2hwpx/            # hwp2hwpx 실행용 Main.java 와 패치 (Dockerfile 이 이미지에 넣는다)
│  ├─ extract_schema.py    # HWPX -> 서식 스키마 JSON 추출
│  ├─ make_document.py     # 서식 하나를 입력 JSON 으로 채워 HWPX·HWP·PDF 로 (M5)
│  ├─ sync_forms.py        # templates/ 의 서식을 서비스 DB 에 등록 (M6)
│  ├─ manage_users.py      # 루트 관리자 만들기·잠금 해제·비밀번호 초기화 (M7), 체험 계정
│  ├─ encrypt_data.py      # 예전에 평문으로 저장한 입력값·파일을 암호화
│  └─ start-server.ps1     # AWS 자원을 켜고 LLM 을 예열 (register-start-task.ps1 로 예약)
├─ tests/                  # pytest. 기본은 내부 자료 없이, 실제 서식 회귀는 `-m local`
├─ docs/                   # 설계 기록·기능명세·로드맵·배포·시연 (위 표)
└─ requirements.txt
```

## 4. 개발 로드맵

자세한 계획은 [`docs/dev-roadmap.md`](docs/dev-roadmap.md)에 있습니다.
진행도는 마일스톤 규모(S=1, M=2, L=3) 가중 **93%** (2026-11-10, 30점 중 28점, 진행 중인 M12 는 세지 않음).

| M | 단계 | 끝났다는 증거 | 상태 |
|---|---|---|:-:|
| M0 | 기반 (테스트·린트·설정·로깅) | 테스트·린트가 한 명령으로 돈다 | ✅ |
| M1 | HWPX 코어 — 누름틀·표 셀·반복영역 쓰기 | 서식 193개 쓰기 → 저장 → 재읽기 일치 | ✅ |
| M2 | 서식 파이프라인 — 변환·스키마 추출 | 변환 193 / 추출 183, 실패 0 | ✅ |
| M3 | 검증 엔진 | 규칙 4종, 올바른 입력에서 오탐 0 | ✅ |
| M4 | 변환 어댑터 — HWP·PDF | 산출물이 한글에서 열림, 비정상 종료 후에도 다음 변환 진행 | ✅ |
| M5 | 수직 슬라이스 (서식 3종 CLI 관통) | 입력 → 검증 → 주입 → HWP·PDF | ✅ |
| M6 | 서비스·저장 (FastAPI) | API로 문서 생성·저장·재조회·다운로드 | ✅ |
| M7 | 인증 (IAM) | 발급 계정으로만 로그인, 역할 분기 | ✅ |
| M8 | 사용자 화면 | 브라우저로 문서 1건 완성 | ✅ |
| M9 | 관리자 페이지 | 새 서식을 올려 사용자 화면에 노출 | ✅ |
| M10 | LLM·입력 자동화 | 프롬프트로 서식 채움, 입금계좌 확인서로 명단 채움, 초안·정성 판정 | ✅ |
| M11 | 문서 간 검증·운영 현황 | 계획서–정산서 금액 대조, 통계·로그 | ✅ |
| M12 | AWS 이전 | 한컴 변환 없이(HWPX 만) AWS 에서 동일 동작 | 🔄 |

## 5. 개발 환경·실행

- Windows 11 / Python 3.12 / **한컴오피스 2022 (한글 12.0)** — COM ProgID `HWPFrame.HwpObject`
- 코어 코드는 한컴오피스 없이도 동작합니다. COM은 템플릿 변환과 최종 산출에만 필요합니다.
- 컨테이너(운영 서버)는 Java 17 + hwp2hwpx 로 올린 HWP 를 받습니다. 로컬 개발에는 Java 가 필요 없습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # 운영만이면 requirements.txt

# forms/ 를 로컬에 배치한 뒤
.\.venv\Scripts\python.exe -u tools\convert_templates.py "forms/<서식 폴더>"
.\.venv\Scripts\python.exe -u tools\extract_schema.py

# 문서 하나 만들기 (입력 틀 -> 채워서 -> 검증·생성·변환)
.\.venv\Scripts\python.exe tools\make_document.py "AID 서식 5-05호" --skeleton > 입력.json
.\.venv\Scripts\python.exe tools\make_document.py "AID 서식 5-05호" 입력.json

# 웹 API (서식을 먼저 등록한다. 데이터는 data/ 에 쌓인다)
.\.venv\Scripts\python.exe tools\sync_forms.py
.\.venv\Scripts\uvicorn.exe --factory docflow.web.app:create_app   # API 목록: http://127.0.0.1:8000/docs

# 테스트 (원본 서식 없이도 돌아갑니다). 실제 서식·한컴 회귀는 -m local
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\pytest.exe -m local
```

컨테이너는 `docker build -t docflow .` 로 만듭니다. 코드만 담고 DB·파일·암호 키는 환경변수로 받습니다(Dockerfile 머리말).
학교 계정처럼 S3 역할을 만들 수 없으면 `DOCFLOW_FILE_STORE=db` 로 파일을 DB 에 둡니다.
실제 배포(Fargate + RDS + Caddy, DuckDNS)는 [`docs/deploy-aws.md`](docs/deploy-aws.md)에 있습니다.

## 6. 검증 결과 (요약)

앵커(신산업)·AID·혁신 3개 사업의 서식 183개 전체가 대상입니다(2026-10-12 기준, 상세는 [`design-notes.md`](docs/design-notes.md) 4장).

```
템플릿 변환      .hwp 193개 -> .hwpx, 실패 0
스키마 추출      서식 183개, 실패 0 — 누름틀 547  표 2,501  슬롯 4,953  수식 305  반복영역 280
쓰기 라운드트립  서식 193개 쓰기 -> 저장 -> 재읽기 일치
검증             올바른 입력에서 183개 서식 모두 오탐 0, 일부러 넣은 오류는 그 자리·그 코드로 검출
문서 생성        서식 183개의 변형 230건 전부 생성, 오류 0
테스트           884개 (기본 861 + 로컬 23)
```

## 7. 원본 서식 취급

학교에서 받은 원본 서식은 **내부 자료라 저장소에 포함하지 않습니다.** 서식 전체를 막으면 공개 가능한 다른 서식까지
걸리므로 내부용 폴더 이름 하나(`ysu_forms/`)만 `.gitignore` 로 뺍니다. 변환·추출 결과가 원본 경로를 그대로
미러링하기 때문에 가능합니다.

| 경로 | 내용 | 재생성 방법 |
|---|---|---|
| `forms/ysu_forms/…` | 원본 서식 (HWP/XLSX/PDF) | 배포 불가. 로컬에 직접 배치 |
| `templates/ysu_forms/…` | 원본을 변환한 HWPX 템플릿 | `tools/convert_templates.py` |
| `schemas/ysu_forms/…` | 서식 구조 스키마 JSON + 목록 | `tools/extract_schema.py` |
| `out/` | 생성된 문서 산출물 | 실행 결과 |
| `data/` | 서비스 DB, 서식 버전별 템플릿, 문서 산출물 | `tools/sync_forms.py` 후 웹 API 사용 결과 |

템플릿·스키마·서식 목록(`index.json`)은 원본의 문구와 서식 이름을 담으므로 원본과 같이 취급해 각 원본 폴더 안에 둡니다.
내부용이 아닌 서식을 `forms/` 아래 다른 이름으로 넣으면 그 파생물은 정상적으로 커밋됩니다.
