# Остановить все python-процессы main.py из папки GASTROBAR_bot

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$names = @("python.exe", "pythonw.exe", "python3.exe")



foreach ($name in $names) {

    Get-CimInstance Win32_Process -Filter "Name = '$name'" -ErrorAction SilentlyContinue |

        Where-Object {

            $_.CommandLine -and

            $_.CommandLine -like "*$root*" -and

            $_.CommandLine -like "*main.py*"

        } |

        ForEach-Object {

            Write-Host "Stopping PID $($_.ProcessId) [$name]: $($_.CommandLine)"

            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue

        }

}



# py launcher: py -3 main.py

Get-CimInstance Win32_Process -Filter "Name = 'py.exe'" -ErrorAction SilentlyContinue |

    Where-Object {

        $_.CommandLine -and

        $_.CommandLine -like "*$root*" -and

        $_.CommandLine -like "*main.py*"

    } |

    ForEach-Object {

        Write-Host "Stopping PID $($_.ProcessId) [py.exe]: $($_.CommandLine)"

        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue

    }



Start-Sleep -Seconds 3

Write-Host "Done."


