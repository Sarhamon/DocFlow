# 이 PC 에 작업 'DocFlow 서버 켜기'(2026-11-26·27 09:00, tools/start-server.ps1)를 등록한다. 켜 둘 PC 에서 한 번.
# pwsh 7 로 실행한다: pwsh -File tools\register-start-task.ps1   (docs/deploy-aws.md 6장)
$ErrorActionPreference = 'Stop'
foreach ($c in 'pwsh', 'aws', 'ssh') {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { throw "$c 가 없다. 설치한 뒤 다시 실행한다." }
}
$key = 'C:\.ssh\plus-ec2.pem'
if (-not (Test-Path $key)) { throw "$key 가 없다. 원래 PC 에서 복사해 온다." }
icacls $key /inheritance:r /grant:r "${env:USERNAME}:R" | Out-Null  # Windows ssh 는 본인만 읽을 수 있는 키만 쓴다

$script = Join-Path $PSScriptRoot 'start-server.ps1'
$a = New-ScheduledTaskAction -Execute (Get-Command pwsh).Source -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$t = '2026-11-26 09:00', '2026-11-27 09:00' | ForEach-Object { New-ScheduledTaskTrigger -Once -At $_ }
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName 'DocFlow 서버 켜기' -Action $a -Trigger $t -Settings $s -Force | Out-Null
"등록됨. 다음 실행: $((Get-ScheduledTaskInfo -TaskName 'DocFlow 서버 켜기').NextRunTime)"

aws sts get-caller-identity --profile mfa --query Arn --output text
if ($LASTEXITCODE) { 'MFA 세션(--profile mfa)이 아직 없거나 만료됐다. 갱신해 둔다 (docs/deploy-aws.md 2장).' }
