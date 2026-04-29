# Windows PowerShell launcher
Set-Location $PSScriptRoot
if (Test-Path .\vpn-proxy.exe) {
    .\vpn-proxy.exe @args
} else {
    go run . @args
}
