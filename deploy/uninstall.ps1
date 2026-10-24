<#
.SYNOPSIS
    Vaultorio — Factorio P2P Cloud Sync One-Line Uninstaller for Windows.
.DESCRIPTION
    Автоматическое удаление перехватчика Vaultorio и полный возврат оригинальной игры.
#>

# 1. Принудительный UTF-8 вывод в консоль
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 2. Проверка прав Администратора
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Запрос прав администратора для восстановления игры..." -ForegroundColor Yellow
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"irm https://raw.githubusercontent.com/GreyPikachu/Vaultorio/main/deploy/uninstall.ps1 | iex`"" -Verb RunAs
    exit
}

Clear-Host
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host "      Vaultorio — Удаление перехватчика (Очистка)           " -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host ""

# 3. Поиск установленной игры Factorio
$gameCandidates = @(
    "C:\Program Files (x86)\Factorio",
    "C:\Program Files\Factorio",
    "C:\Games\Factorio",
    "D:\Games\Factorio",
    "E:\Games\Factorio",
    "C:\Factorio",
    "D:\Factorio",
    "E:\Factorio"
)

$steamPath = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -Name "SteamPath" -ErrorAction SilentlyContinue).SteamPath
if ($steamPath) {
    $gameCandidates += "$steamPath\steamapps\common\Factorio"
}

$foundGame = $null
foreach ($candidate in $gameCandidates) {
    if ((Test-Path "$candidate\bin\x64\factorio_core.exe") -or (Test-Path "$candidate\bin\x64\factorio.exe")) {
        $foundGame = $candidate
        break
    }
}

if ($foundGame) {
    $coreBin = "$foundGame\bin\x64\factorio_core.exe"
    $gameBin = "$foundGame\bin\x64\factorio.exe"
    
    if (Test-Path $coreBin) {
        Write-Host "[1/3] Восстановление оригинального factorio.exe..." -ForegroundColor Cyan
        Move-Item -Path $coreBin -Destination $gameBin -Force
        Write-Host "Оригинальный исполняемый файл игры успешно восстановлен!" -ForegroundColor Green
    } else {
        Write-Host "[1/3] factorio_core.exe не обнаружен (игра уже в чистом виде)." -ForegroundColor Yellow
    }
    
    Write-Host "[2/3] Удаление конфигурационных файлов..." -ForegroundColor Cyan
    Remove-Item -Path "$foundGame\factorio_sync_config.json" -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "$env:APPDATA\Factorio\factorio_sync_config.json" -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "Каталог Factorio не найден в стандартных путях." -ForegroundColor Yellow
}

Write-Host "[3/3] Очистка системной папки Vaultorio..." -ForegroundColor Cyan
$installDir = "$env:ProgramData\Vaultorio"
if (Test-Path $installDir) {
    Remove-Item -Path $installDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Папка $installDir удалена." -ForegroundColor Green
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  ОЧИСТКА ЗАВЕРШЕНА! Игра возвращена в исходное состояние. " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Нажмите Enter для выхода..."
Read-Host
