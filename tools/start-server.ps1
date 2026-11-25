# 아침에 서버를 켠다: RDS·EC2 시작 → ECS 확인 → LLM 예열 → 사이트 확인. (docs/deploy-aws.md 6장)
# MFA 세션(--profile mfa)이 살아 있어야 한다. 기록은 %TEMP%\docflow-start.log
$ErrorActionPreference = 'Stop'
$log = Join-Path $env:TEMP 'docflow-start.log'
Start-Transcript -Path $log -Append | Out-Null
try {
    $p = @('--profile', 'mfa')
    $ec2 = 'i-09c4bdcd27aacab5a'

    aws sts get-caller-identity @p --output text | Out-Null
    if ($LASTEXITCODE) { throw 'MFA 세션이 만료됐다. 갱신한 뒤 다시 실행한다.' }

    $rds = aws rds describe-db-instances @p --db-instance-identifier docflow-db --query 'DBInstances[0].DBInstanceStatus' --output text
    if ($rds -eq 'stopped') { aws rds start-db-instance @p --db-instance-identifier docflow-db | Out-Null }
    aws ec2 start-instances @p --instance-ids $ec2 | Out-Null
    "RDS: $rds, EC2 시작"

    aws ec2 wait instance-running @p --instance-ids $ec2
    aws rds wait db-instance-available @p --db-instance-identifier docflow-db  # 최대 30분
    if ($LASTEXITCODE) { throw 'RDS 가 30분 안에 available 이 되지 않았다.' }
    'RDS available, EC2 running'

    $desired = aws ecs describe-services @p --cluster docflow --services docflow --query 'services[0].desiredCount' --output text
    if ($desired -eq '0') { aws ecs update-service @p --cluster docflow --service docflow --desired-count 1 | Out-Null; 'ECS 개수 0 → 1' }

    # 예열: 모델을 GPU 에 올리고 keep_alive -1 로 내리지 않게 한다. Ollama 가 뜰 때까지 10초 간격으로 다시 시도.
    $ip = aws ec2 describe-instances @p --instance-ids $ec2 --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
    $remote = 'for i in $(seq 30); do curl -sf http://localhost:11434/api/generate -d ''{"model":"exaone3.5:7.8b","keep_alive":-1}'' && exit 0; sleep 10; done; exit 1'
    for ($i = 0; $i -lt 12; $i++) {  # 부팅 직후엔 SSH 가 아직 안 받는다
        ssh -i C:/.ssh/plus-ec2.pem -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "ubuntu@$ip" $remote
        if (-not $LASTEXITCODE) { break }
        Start-Sleep 10
    }
    if ($LASTEXITCODE) { throw "LLM 예열 실패 (EC2 $ip)" }
    "`nLLM 예열 완료 ($ip)"

    # ECS 가 태스크를 새로 띄우고 DuckDNS 를 고칠 때까지 (최대 10분)
    for ($i = 0; $i -lt 60; $i++) {
        try { if ((Invoke-WebRequest http://docflow-ysu.duckdns.org -TimeoutSec 10).StatusCode -eq 200) { '사이트 200 — 완료'; break } } catch {}
        Start-Sleep 10
    }
    if ($i -eq 60) { throw '사이트가 10분 안에 응답하지 않았다. ECS 이벤트와 /ecs/docflow 로그를 본다.' }
}
catch { "실패: $_"; exit 1 }
finally { Stop-Transcript | Out-Null }
