# Показать все процессы, похожие на Gastrobar bot
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Write-Host "Project: $root"
Write-Host ""
$found = $false
foreach ($name in @("python.exe", "pythonw.exe", "python3.exe", "py.exe")) {
    Get-CimInstance Win32_Process -Filter "Name = '$name'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -like "*$root*" -or
                $_.CommandLine -like "*GASTROBAR*" -or
                $_.CommandLine -like "*gastrobar*main.py*"
            )
        } |
        ForEach-Object {
            $found = $true
            Write-Host "[$name] PID $($_.ProcessId)"
            Write-Host "  $($_.CommandLine)"
            Write-Host ""
        }
}
if (-not $found) {
    Write-Host "No bot-related python processes found."
}
