$ErrorActionPreference = 'Stop'
$certDir = Join-Path $PSScriptRoot 'local'
$null = New-Item -ItemType Directory -Path $certDir -Force
Push-Location $certDir
try {
    $files = 'ca.crt', 'ca.key', 'server.crt', 'server.key'
    $existing = @($files | Where-Object { Test-Path -LiteralPath $_ })
    if ($existing.Count -eq $files.Count) { return }
    if ($existing.Count -gt 0) { throw 'Local TLS directory is incomplete. Move it aside before regenerating.' }
    $openssl = (Get-Command openssl -ErrorAction Stop).Source
    function Invoke-OpenSSL {
        & $openssl @args
        if ($LASTEXITCODE -ne 0) { throw 'OpenSSL failed to generate local certificates.' }
    }
    @'
[req]
distinguished_name = dn
[dn]
'@ | Set-Content -Encoding ascii -LiteralPath 'req.cnf'
    Invoke-OpenSSL req -config req.cnf -x509 -newkey rsa:2048 -nodes -sha256 -days 365 -subj '/CN=Tax Product Local CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' -keyout ca.key -out ca.crt
    Invoke-OpenSSL req -config req.cnf -new -newkey rsa:2048 -nodes -subj '/CN=localhost' -keyout server.key -out server.csr
    @'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,IP:127.0.0.1
'@ | Set-Content -Encoding ascii -LiteralPath 'server.ext'
    Invoke-OpenSSL x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 90 -sha256 -extfile server.ext
} finally {
    Pop-Location
}
