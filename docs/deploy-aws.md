# AWS 배포 안내 (M12)

사이트: **<http://docflow-ysu.duckdns.org>** (2026-11-10 배포, 2026-11-11 다시 배포 `docflow:6`). 서울 리전(`ap-northeast-2`), 학교 공용 계정 `093356173512`.
HTTPS 가 없어 주소는 `http://` 까지 적어 준다 — 브라우저가 https 로 먼저 시도하면 열리지 않는다.
**자원은 매일 저녁 7시에 자동으로 꺼진다**(관리 측 비용 정책). 다음 날 쓰려면 RDS 를 켠다(6장).
계정이 공용이라 남의 자원(Elastic IP·RDS·EC2 다수)이 섞여 있다. 우리 자원은 이름에 `docflow` 가 붙거나 `Project=DocFlow` 태그가 있다.

## 1. 구성

```
브라우저 ──HTTP:80──▶ Fargate 태스크 (퍼블릭 IP, 서비스 docflow)
                      ├─ caddy    : 80 → 127.0.0.1:8000 (프록시)
                      └─ docflow  : 앱 (uvicorn :8000). 시작할 때 DuckDNS 에 자기 IP 를 등록
                           │ 5432                         │ 11434 (글로 채우기·본문 초안)
                           ▼                              ▼
                      RDS docflow-db              EC2 plus-DocFlow (Ollama, 평소 꺼 둠)
                      (PostgreSQL 16, 비공개)      사설 IP 172.31.18.178
                      템플릿·산출물 파일도 여기
```

| 자원 | 이름·설정 | 비고 |
|---|---|---|
| ECR | `docflow` | 앱 이미지(태그 `<커밋>-<월일시분>`), Caddy 사본(`caddy-2.8.4`) |
| ECS | 클러스터·서비스 `docflow`, 작업 정의 `docflow:N` | Fargate 0.5 vCPU / 1 GB, 1개. 실행 역할 `ecsTaskExecutionRole`(공용) |
| RDS | `docflow-db` | PostgreSQL 16, db.t4g.micro, 20 GB, 외부 접속 불가, 자동 백업 1일 |
| EC2 | `plus-DocFlow` (g4dn.xlarge) | Ollama + `exaone3.5:7.8b`. `OLLAMA_HOST=0.0.0.0` (systemd override) |
| 보안 그룹 | `docflow-app-sg` 80 전체 / `docflow-db-sg` 5432 ← 앱·EC2 / `plus-docflow` 11434 ← 앱 | 8000(Caddy 를 거치지 않는 주소)은 닫았다(2026-11-11) |
| 로그 | CloudWatch `/ecs/docflow` (14일) | 스트림 `web/…`(앱), `caddy/…` |
| 주소 | DuckDNS `docflow-ysu` | 태스크가 뜰 때 자동 등록. **DuckDNS 화면에서 IP 를 고치지 않는다** |

비용은 RDS·Fargate 합쳐 월 약 $40. EC2(LLM)는 켜 둔 시간만큼 시간당 약 $0.53.

### 계획과 달라진 점 (학교 계정 제약)

- **파일을 S3 가 아니라 RDS 에 둔다** (`DOCFLOW_FILE_STORE=db`, 표 `stored_files`). 앱이 S3 를 쓰려면 IAM 역할이
  필요한데 정책 `StudentGuardrails` 가 역할 생성을 막는다. 템플릿 183개가 13 MB 라 DB 에 두어도 된다.
- **ALB·Elastic IP 가 없다** (ALB 는 쓸 수 있는 자원이 아니고, Elastic IP 는 계정 한도가 찼다). 그래서 태스크의
  퍼블릭 IP 를 DuckDNS 이름에 등록하고(`tools/duckdns.py`), 80 포트는 같은 태스크의 Caddy 가 받는다.
- **비밀값이 작업 정의에 평문으로 있다** (Secrets Manager·SSM 을 쓸 수 없음). 작업 정의를 공유하지 않는다.
- **CLI 는 MFA 세션으로만** 된다 (정책 `StudentSelfManagement`). 아래 2장.

## 2. 준비

- **비밀값**: `secrets/aws.env` (git 에 올리지 않음) — DB 비밀번호, 암호 키(`DATA_KEY_NEW`), 보안 그룹 id, 현재 이미지 태그,
  DuckDNS 토큰, 루트 비밀번호. 작업 정의 원본은 `secrets/taskdef.json`. **암호 키를 잃으면 문서를 되살릴 수 없다** — 따로 백업.
- **MFA 세션** (12시간): OTP 앱의 6자리로
  ```powershell
  $c = aws sts get-session-token --serial-number arn:aws:iam::093356173512:mfa/Capstone2 --duration-seconds 43200 --token-code <코드> --query Credentials --output json | ConvertFrom-Json; aws configure set aws_access_key_id $c.AccessKeyId --profile mfa; aws configure set aws_secret_access_key $c.SecretAccessKey --profile mfa; aws configure set aws_session_token $c.SessionToken --profile mfa; aws configure set region ap-northeast-2 --profile mfa
  ```
  이후 명령은 모두 `--profile mfa`. Git Bash 에서는 `/ecs/docflow` 같은 인자가 경로로 바뀌므로 `MSYS_NO_PATHCONV=1` 을 둔다.
- Docker Desktop 을 켠다 (이미지 빌드).

## 3. 다시 배포하기

```bash
R=093356173512.dkr.ecr.ap-northeast-2.amazonaws.com
TAG=$(git rev-parse --short HEAD)-$(date +%m%d%H%M)
aws ecr get-login-password --profile mfa | docker login --username AWS --password-stdin $R
docker build --platform linux/amd64 -t $R/docflow:$TAG . && docker push $R/docflow:$TAG
```

1. `secrets/taskdef.json` 의 `docflow` 컨테이너 `image` 를 새 태그로 바꾸고 `secrets/aws.env` 의 `IMAGE_TAG` 도 고친다.
2. `aws ecs register-task-definition --profile mfa --cli-input-json file://taskdef.json` (secrets 폴더에서) → 새 번호 N
3. `aws ecs update-service --profile mfa --cluster docflow --service docflow --task-definition docflow:N`
4. `aws ecs wait services-stable --profile mfa --cluster docflow --services docflow` — 태스크가 하나라 **1~2분 끊긴다**.
5. **스키마 형식(`SCHEMA_VERSION`)이 바뀐 배포면** 새 코드가 뜬 **뒤에** 서식을 다시 등록한다(4장). 예전 코드는 새 스키마를 못 읽는다.

새 태스크는 IP 가 바뀌고, 앱이 뜰 때 DuckDNS 를 새 IP 로 고친다. DNS 가 퍼지는 데 1분쯤 걸린다.

## 4. RDS 작업 (서식 등록·입력폼 설정·LLM 주소)

RDS 는 밖에서 닿지 않는다. LLM EC2 를 켜고 그 EC2 를 거쳐 터널을 연다 (EC2 퍼블릭 IP 는 켤 때마다 바뀐다).

```bash
ssh -i C:/.ssh/plus-ec2.pem -N -L 15432:docflow-db.cd2e804cyc02.ap-northeast-2.rds.amazonaws.com:5432 ubuntu@<EC2 퍼블릭 IP>
```

다른 창에서 (`secrets/aws.env` 를 읽은 뒤):

```bash
export DOCFLOW_DATABASE_URL="postgresql+psycopg://docflow:$DB_PASSWORD@127.0.0.1:15432/docflow" DOCFLOW_FILE_STORE=db DOCFLOW_DATA_KEY="$DATA_KEY_NEW"
python tools/sync_forms.py      # 서식 등록·개정 (234개, 약 1분). Windows 콘솔에서 글자가 깨지면 PYTHONIOENCODING=utf-8
python tools/demo_setup.py      # 시연 서식 입력폼 설정 (처음 한 번)
```

LLM 주소는 관리자 화면 > 시스템 설정에서 `http://172.31.18.178:11434` (EC2 사설 IP, 껐다 켜도 같다).

## 5. 루트 계정 잠금 해제·비밀번호 초기화

루트는 서버 CLI 로만 푼다. EC2 없이 **일회성 태스크**로 돌린다. 반드시 **`docflow:1`** (DuckDNS 설정이 없는 정의)을 쓴다 —
DuckDNS 설정이 든 정의로 띄우면 그 태스크가 사이트 주소를 자기 IP 로 바꿔 버린다.

```bash
aws ecs run-task --profile mfa --cluster docflow --task-definition docflow:1 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-017fcbea5e7810a81],securityGroups=[$APP_SG],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"docflow","command":["python","tools/manage_users.py","unlock","root"]}]}'
```

`reset-password root` 로 바꾸면 새 초기 비밀번호가 로그(`/ecs/docflow`)에 찍힌다. 다른 계정은 관리자 화면에서 푼다.
콘솔에서는 ECS → 클러스터 `docflow` → 새 태스크 실행 → `docflow:1`, 퍼블릭 IP 켜기, `docflow-app-sg`, 컨테이너 명령 재정의.

## 6. 켜고 끄기

### 매일 저녁 7시 자동 중지

학교 계정은 매일 19시에 자원을 끈다. RDS 가 꺼지면 앱이 시작할 때 DB 에 닿지 못해 죽고, ECS 서비스는 개수 1 을
지키려고 2~3분마다 태스크를 새로 띄우다 헬스체크에서 죽기를 되풀이한다(사이트는 응답 없음). 다음 날 쓰려면:

```bash
aws rds start-db-instance --profile mfa --db-instance-identifier docflow-db
aws rds wait db-instance-available --profile mfa --db-instance-identifier docflow-db   # 5~10분
```

RDS 가 뜨면 ECS 가 다음 태스크를 정상으로 띄우고 DuckDNS 를 새 IP 로 고친다(따로 할 일 없음, 2~3분).
`aws ecs describe-services --profile mfa --cluster docflow --services docflow` 의 `runningCount` 가 1 이면 된다.

작업을 마치고 직접 끌 때는 ECS 개수도 0 으로 내려 재시작 반복을 막는다. 그 경우 켤 때 RDS 를 켠 **뒤에** 1 로 올린다.

```bash
# 끄기
aws ecs update-service --profile mfa --cluster docflow --service docflow --desired-count 0
aws ec2 stop-instances --profile mfa --instance-ids i-09c4bdcd27aacab5a
aws rds stop-db-instance --profile mfa --db-instance-identifier docflow-db
# 켜기 (RDS 가 available 이 된 뒤)
aws ecs update-service --profile mfa --cluster docflow --service docflow --desired-count 1
```

RDS 는 멈춘 지 7일이 지나면 AWS 가 저절로 다시 켠다.

### LLM

평소 꺼 둔다. 글로 채우기·본문 초안을 쓸 때만 콘솔(EC2 → `plus-DocFlow` → 시작/중지) 또는
`aws ec2 start-instances|stop-instances --profile mfa --instance-ids i-09c4bdcd27aacab5a`. 켜고 1~2분 뒤부터 쓸 수 있고,
첫 호출은 모델을 올리느라 30초쯤 걸린다. 꺼져 있으면 글로 채우기만 실패하고 나머지는 그대로 된다.

## 7. HTTPS (아직 안 함)

Caddy 명령의 `--from :80` 을 `--from docflow-ysu.duckdns.org` 로 바꾸고 443 을 열면 Let's Encrypt 인증서를 받아 HTTPS 가 된다
(앱 환경변수 `DOCFLOW_SECURE_COOKIE=1` 도). 다만 인증서를 둘 저장소가 없어 **태스크가 뜰 때마다 새로 받는다** — 같은 이름에
주 5회 한도가 있어 배포가 잦은 동안은 미룬다. 새 태스크의 IP 가 DNS 에 퍼지기 전에 발급을 시도해 실패할 수 있으므로
DuckDNS DNS 인증 플러그인을 넣은 Caddy 이미지(`xcaddy build --with github.com/caddy-dns/duckdns`)로 하는 것을 권한다.

## 8. 정리 (프로젝트가 끝나면)

비용이 계속 나가므로 끝나면 지운다. 지우기 전에 남길 데이터(문서)가 있는지 확인하고, 필요하면 RDS 최종 스냅샷을 남긴다.

1. `aws ecs update-service … --desired-count 0` → `aws ecs delete-service --cluster docflow --service docflow` → `aws ecs delete-cluster --cluster docflow`
2. 작업 정의 `docflow:*` 등록 해제 (`deregister-task-definition`)
3. `aws rds delete-db-instance --db-instance-identifier docflow-db --final-db-snapshot-identifier docflow-final` (스냅샷이 필요 없으면 `--skip-final-snapshot`)
4. `aws ecr delete-repository --repository-name docflow --force`, `aws logs delete-log-group --log-group-name /ecs/docflow`
5. 보안 그룹 `docflow-app-sg`·`docflow-db-sg` 삭제, `plus-docflow` 의 11434 규칙 삭제
6. EC2 `plus-DocFlow` 는 중지 상태 확인 (지울지는 따로 정한다), DuckDNS 이름 삭제
