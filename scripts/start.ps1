$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if ($env:PYTHON) {
    $Python = $env:PYTHON
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Python = "python"
} else {
    $Python = "py"
}

Write-Host "=== Riot API bootstrap ==="
Write-Host "project: $Root"
Write-Host "python:  $Python"

& $Python -m pip install --quiet --no-cache-dir --upgrade pip
& $Python -m pip install --quiet --no-cache-dir -e .

Write-Host "Fetching Camoufox/Firefox (first run is ~2.5 GB)..."
& $Python -m camoufox fetch
& $Python -c "from camoufox.geolocation import download_mmdb; download_mmdb()"

New-Item -ItemType Directory -Force data/input, data/output | Out-Null

$env:PYTHONUNBUFFERED = "1"
if (-not $env:BIND_HOST) { $env:BIND_HOST = "127.0.0.1" }
if (-not $env:PORT) { $env:PORT = "8000" }
$env:HEADLESS = "1"
if (-not $env:MAX_WORKERS) { $env:MAX_WORKERS = "10" }
if (-not $env:MIN_FREE_MB) { $env:MIN_FREE_MB = "660" }
$env:JOBS_DB = if ($env:JOBS_DB) { $env:JOBS_DB } else { Join-Path $Root "data/jobs.sqlite3" }
$env:ENABLE_DOCS = if ($env:ENABLE_DOCS) { $env:ENABLE_DOCS } else { "0" }
$env:ENABLE_SYNC_RUN = if ($env:ENABLE_SYNC_RUN) { $env:ENABLE_SYNC_RUN } else { "0" }
$env:LOG_LEVEL = if ($env:LOG_LEVEL) { $env:LOG_LEVEL } else { "INFO" }

$GeneratedKey = ""
if (-not $env:API_KEY -and -not $env:API_KEYS) {
    $env:API_KEY = (& $Python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
    $GeneratedKey = $env:API_KEY
}

$GeneratedWebhookSecret = ""
if (-not $env:WEBHOOK_SECRET) {
    $env:WEBHOOK_SECRET = (& $Python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
    $GeneratedWebhookSecret = $env:WEBHOOK_SECRET
}

$KeyForPrint = if ($env:API_KEY) { $env:API_KEY } else { "YOUR_API_KEY" }
$LocalUrl = "http://127.0.0.1:$env:PORT"
Write-Host ""
Write-Host "================ READY ================"
Write-Host "local:  $LocalUrl"
if ($GeneratedKey) {
    Write-Host "api key: $GeneratedKey"
} else {
    Write-Host "api key: use API_KEY/API_KEYS from the environment"
}
if ($GeneratedWebhookSecret) {
    Write-Host "webhook secret: $GeneratedWebhookSecret"
}
Write-Host ""
Write-Host "Submit from another terminal:"
Write-Host "  python scripts/submit_accounts.py --url $LocalUrl --api-key $KeyForPrint --input data/input/accounts-test.jsonl --out data/output/job-results.jsonl --webhook-url http://127.0.0.1:$env:PORT/api/v1/webhook --concurrency 10 --rate 10"
Write-Host ""
Write-Host "Results: data/output/webhook-results.jsonl"
Write-Host "Stop: Ctrl-C"
Write-Host "========================================"
Write-Host ""

& $Python server.py
