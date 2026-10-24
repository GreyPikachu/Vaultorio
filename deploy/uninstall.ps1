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
function Resolve-FactorioRoot($rawPath) {
    if (-not $rawPath) { return $null }
    $p = $rawPath.ToString().Trim().Trim('"').Trim("'")
    if (-not (Test-Path -LiteralPath $p)) { return $null }
    
    $item = Get-Item -LiteralPath $p -ErrorAction SilentlyContinue
    if (-not $item) { return $null }
    
    # 1. Если указан сам файл factorio.exe или factorio_core.exe
    if (-not $item.PSIsContainer) {
        if ($item.Name -match "^factorio(_core)?\.exe$") {
            if ($item.Directory.Name -eq "x64" -and $item.Directory.Parent.Name -eq "bin") {
                return $item.Directory.Parent.Parent.FullName
            }
            return $item.Directory.FullName
        }
    }
    
    # 2. Если указана папка bin\x64
    if ($item.Name -eq "x64" -and $item.Parent.Name -eq "bin") {
        $root = $item.Parent.Parent.FullName
        if ((Test-Path -LiteralPath "$root\bin\x64\factorio.exe") -or (Test-Path -LiteralPath "$root\bin\x64\factorio_core.exe")) {
            return $root
        }
    }
    
    # 3. Если указана папка bin
    if ($item.Name -eq "bin") {
        $root = $item.Parent.FullName
        if ((Test-Path -LiteralPath "$root\bin\x64\factorio.exe") -or (Test-Path -LiteralPath "$root\bin\x64\factorio_core.exe")) {
            return $root
        }
    }
    
    # 4. Если указан корень игры (где лежит bin\x64)
    if ((Test-Path -LiteralPath "$($item.FullName)\bin\x64\factorio.exe") -or (Test-Path -LiteralPath "$($item.FullName)\bin\x64\factorio_core.exe")) {
        return $item.FullName
    }
    
    # 5. Если внутри выбранной папки есть каталог Factorio
    $sub = Join-Path $item.FullName "Factorio"
    if ((Test-Path -LiteralPath "$sub\bin\x64\factorio.exe") -or (Test-Path -LiteralPath "$sub\bin\x64\factorio_core.exe")) {
        return $sub
    }
    
    return $null
}

$gameCandidates = @(
    "C:\Program Files (x86)\Factorio",
    "C:\Program Files\Factorio",
    "C:\Games\Factorio",
    "D:\Games\Factorio",
    "E:\Games\Factorio",
    "C:\Factorio",
    "D:\Factorio",
    "E:\Factorio",
    "$env:USERPROFILE\Downloads\Factorio",
    "$env:USERPROFILE\Desktop\Factorio"
)

# Проверка библиотек Steam
$steamPath = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -Name "SteamPath" -ErrorAction SilentlyContinue).SteamPath
if ($steamPath) {
    $gameCandidates += "$steamPath\steamapps\common\Factorio"
}

# Поиск в подпапках Загрузок и Рабочего стола
$userExtraDirs = Get-ChildItem -Path "$env:USERPROFILE\Downloads", "$env:USERPROFILE\Desktop" -Filter "*Factorio*" -Directory -Recurse -Depth 2 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
if ($userExtraDirs) {
    $gameCandidates += $userExtraDirs
}

$foundGame = $null
foreach ($candidate in $gameCandidates) {
    $res = Resolve-FactorioRoot $candidate
    if ($res) {
        $foundGame = $res
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
