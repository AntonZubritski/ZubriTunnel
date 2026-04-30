# Создаёт ярлык "ZubriTunnel" на рабочем столе с иконкой icon.ico
# Запуск: правой кнопкой → Run with PowerShell
# Или из PowerShell:
#   powershell -ExecutionPolicy Bypass -File install_shortcut.ps1

Add-Type -AssemblyName PresentationFramework

$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = "$desktop\ZubriTunnel.lnk"

# Prefer the standalone ZubriTunnel.exe (PyInstaller bundle, no Python needed).
# Fall back to gui.bat / pythonw gui.py only in dev mode (git clone without build).
$zubri_exe = Join-Path $PSScriptRoot 'ZubriTunnel.exe'
if (Test-Path $zubri_exe) {
    $target = $zubri_exe
    $arguments = ""
} else {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { $python = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if ($python) {
        $pyDir = Split-Path $python
        $pythonw = Join-Path $pyDir "pythonw.exe"
        $target = if (Test-Path $pythonw) { $pythonw } else { $python }
        $arguments = "`"$PSScriptRoot\gui.py`""
    } else {
        Write-Warning "Python не найден в PATH. Ярлык будет указывать на gui.bat."
        $target = "$PSScriptRoot\gui.bat"
        $arguments = ""
    }
}

$shortcut = $ws.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.IconLocation = "$PSScriptRoot\icon.ico"
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = "ZubriTunnel GUI"
$shortcut.Save()

Write-Host ""
Write-Host "Ярлык создан: $shortcutPath" -ForegroundColor Green
Write-Host "Цель:        $target $arguments"
Write-Host "Иконка:      $PSScriptRoot\icon.ico"
Write-Host ""

# Графическое сообщение с выбором "запустить сейчас"
$msg = "Ярлык 'ZubriTunnel' создан на рабочем столе.`n`nДважды кликни по нему чтобы открыть GUI.`n`nЗапустить прямо сейчас?"
$result = [System.Windows.MessageBox]::Show(
    $msg,
    "ZubriTunnel — установка завершена",
    [System.Windows.MessageBoxButton]::YesNo,
    [System.Windows.MessageBoxImage]::Information
)

if ($result -eq 'Yes') {
    if ($arguments) {
        Start-Process -FilePath $target -ArgumentList $arguments -WorkingDirectory $PSScriptRoot
    } else {
        Start-Process -FilePath $target -WorkingDirectory $PSScriptRoot
    }
    Write-Host "Запущено." -ForegroundColor Green
} else {
    # открыть папку рабочего стола с выделенным ярлыком
    Start-Process -FilePath "explorer.exe" -ArgumentList "/select,`"$shortcutPath`""
}
