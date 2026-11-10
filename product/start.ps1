param([switch]$Https)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (!(Test-Path -LiteralPath '.env')) {
        'DB_PASSWORD', 'AGENT_TOKEN', 'ALPHA_PASSWORD', 'BETA_PASSWORD' | ForEach-Object {
            "PRODUCT_$_=" + [guid]::NewGuid().ToString('N')
        } | Set-Content -LiteralPath '.env' -Encoding ascii
    }
    foreach ($setting in 'IMPORT_TOKEN', 'S3_PASSWORD') {
        if (!(Select-String -LiteralPath '.env' -Pattern "^PRODUCT_${setting}=" -Quiet)) {
            "PRODUCT_${setting}=" + [guid]::NewGuid().ToString('N') | Add-Content -LiteralPath '.env' -Encoding ascii
        }
    }
    $composeFiles = @('-f', 'compose.yaml')
    if ($Https) {
        & (Join-Path $PSScriptRoot 'tls/generate-local.ps1')
        $composeFiles += @('-f', 'compose.https.yaml')
    }
    docker compose @composeFiles up --build --remove-orphans --wait --wait-timeout 120
    if ($LASTEXITCODE -ne 0) { throw 'Product startup failed. Check the Compose output.' }
} finally {
    Pop-Location
}
