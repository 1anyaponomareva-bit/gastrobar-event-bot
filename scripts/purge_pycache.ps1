$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Get-ChildItem -Path $root -Directory -Filter "__pycache__" -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\\.venv\\' } |
    ForEach-Object {
        Write-Host "Removing $($_.FullName)"
        Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
Write-Host "Bytecode cache cleared."
