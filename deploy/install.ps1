<#
.SYNOPSIS
    Vaultorio — Factorio P2P Cloud Sync One-Line Installer for Windows.
.DESCRIPTION
    Автоматический установщик перехватчика Vaultorio для клиентов на Windows.
    Проверяет права Администратора, находит игру Factorio, настраивает окружение Python,
    загружает репозиторий с GitHub, собирает и внедряет перехватчик в игру.
#>

# 1. Принудительный UTF-8 вывод в консоль
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 2. Проверка прав Администратора (автоматический перезапуск с UAC)
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Запрос прав администратора для внедрения перехватчика в каталог Factorio..." -ForegroundColor Yellow
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"irm https://raw.githubusercontent.com/GreyPikachu/Vaultorio/main/deploy/install.ps1 | iex`"" -Verb RunAs
    exit
}

Clear-Host
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "      Vaultorio — Factorio P2P Cloud Sync (Автоустановка)   " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# 3. Поиск установленной игры Factorio
Write-Host "[1/5] Поиск установки Factorio на дисках..." -ForegroundColor White
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

# Проверка библиотек Steam
$steamPath = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -Name "SteamPath" -ErrorAction SilentlyContinue).SteamPath
if ($steamPath) {
    $gameCandidates += "$steamPath\steamapps\common\Factorio"
}

$foundGame = $null
foreach ($candidate in $gameCandidates) {
    if ((Test-Path "$candidate\bin\x64\factorio.exe") -or (Test-Path "$candidate\bin\x64\factorio_core.exe")) {
        $foundGame = $candidate
        break
    }
}

if (-not $foundGame) {
    Write-Host "Игра не найдена в стандартных путях." -ForegroundColor Yellow
    $userInput = Read-Host "Введите полный путь к папке игры Factorio (где лежит bin\x64)"
    if ($userInput -and ((Test-Path "$userInput\bin\x64\factorio.exe") -or (Test-Path "$userInput\bin\x64\factorio_core.exe"))) {
        $foundGame = $userInput
    } else {
        Write-Host "ОШИБКА: Исполняемый файл factorio.exe не найден в указанном пути: $userInput" -ForegroundColor Red
        pause
        exit 1
    }
}

Write-Host "Найдена игра: $foundGame" -ForegroundColor Green
Write-Host ""

# 4. Проверка и установка Python 3
Write-Host "[2/5] Проверка окружения Python 3..." -ForegroundColor White
$pythonExe = $null

# Проверяем в PATH
$pyCmd = Get-Command python.exe, py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pyCmd) {
    $versionStr = (& $pyCmd.Source --version 2>&1)
    if ($versionStr -match "Python 3\.(1[0-9]|[2-9][0-9])") {
        $pythonExe = $pyCmd.Source
        Write-Host "Обнаружен $versionStr ($pythonExe)" -ForegroundColor Green
    }
}

# Проверяем стандартные пути установки
if (-not $pythonExe) {
    $stdPython = Get-ChildItem -Path "$env:ProgramFiles\Python*", "$env:LOCALAPPDATA\Programs\Python\Python*" -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue -Depth 1 | Select-Object -First 1
    if ($stdPython) {
        $pythonExe = $stdPython.FullName
        Write-Host "Обнаружен Python в $pythonExe" -ForegroundColor Green
    }
}

# Если Python не найден — тихая автоматическая установка официального Python 3.12
if (-not $pythonExe) {
    Write-Host "Python 3 не обнаружен. Автоматическая загрузка официального Python 3.12..." -ForegroundColor Yellow
    $pyInstaller = "$env:TEMP\python-3.12.7-amd64.exe"
    
    curl.exe -sSL "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -o $pyInstaller
    if (-not (Test-Path $pyInstaller) -or (Get-Item $pyInstaller).Length -lt 10000000) {
        Write-Host "ОШИБКА: Не удалось скачать установщик Python." -ForegroundColor Red
        pause
        exit 1
    }
    
    Write-Host "Установка Python 3.12 (в тихом режиме, пожалуйста, подождите)..." -ForegroundColor Cyan
    $proc = Start-Process -FilePath $pyInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_tcltk=1 Include_pip=1" -Wait -PassThru
    Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
    
    # Обновление PATH в текущей сессии
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath;$env:ProgramFiles\Python312;$env:ProgramFiles\Python312\Scripts"
    
    $pythonExe = "$env:ProgramFiles\Python312\python.exe"
    if (-not (Test-Path $pythonExe)) {
        $pyCmd = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pyCmd) { $pythonExe = $pyCmd.Source }
    }
    Write-Host "Python 3.12 успешно установлен!" -ForegroundColor Green
}
Write-Host ""

# 5. Загрузка исходников Vaultorio с GitHub
Write-Host "[3/5] Загрузка компонентов Vaultorio с GitHub..." -ForegroundColor White
$installDir = "$env:ProgramData\Vaultorio"
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
}

$zipPath = "$env:TEMP\Vaultorio-main.zip"
curl.exe -sSL "https://github.com/GreyPikachu/Vaultorio/archive/refs/heads/main.zip" -o $zipPath

$extractTemp = "$env:TEMP\Vaultorio-extract"
if (Test-Path $extractTemp) { Remove-Item $extractTemp -Recurse -Force -ErrorAction SilentlyContinue }
Expand-Archive -Path $zipPath -DestinationPath $extractTemp -Force
Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

$sourceDir = Join-Path $extractTemp "Vaultorio-main"
Copy-Item -Path "$sourceDir\*" -Destination $installDir -Recurse -Force
Remove-Item $extractTemp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Файлы Vaultorio размещены в $installDir" -ForegroundColor Green
Write-Host ""

# 6. Настройка виртуального окружения и сборка перехватчика
Write-Host "[4/5] Подготовка перехватчика и зависимостей..." -ForegroundColor White
$venvPython = "$installDir\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    & $pythonExe -m venv "$installDir\.venv"
}

Write-Host "Установка библиотек (httpx, pyinstaller)..." -ForegroundColor Cyan
& $venvPython -m pip install -q -r "$installDir\deploy\client\requirements.txt"
& $venvPython -m pip install -q pyinstaller

Write-Host "Сборка standalone-перехватчика factorio_wrapper.exe..." -ForegroundColor Cyan
& $venvPython "$installDir\build\build_client.py" --target wrapper --clean

$wrapperBin = "$installDir\dist\factorio_wrapper.exe"
if (-not (Test-Path $wrapperBin)) {
    Write-Host "ОШИБКА: Не удалось собрать factorio_wrapper.exe" -ForegroundColor Red
    pause
    exit 1
}
Write-Host "Перехватчик успешно собран: $wrapperBin" -ForegroundColor Green
Write-Host ""

# 7. Внедрение перехватчика в игру
Write-Host "[5/5] Внедрение перехватчика в Factorio..." -ForegroundColor White
& $venvPython -m src.installer.installer --install --game-dir "$foundGame" --wrapper-bin "$wrapperBin" --hub-url "http://45.91.248.78:8088"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "      УСТАНОВКА VAULTORIO УСПЕШНО ЗАВЕРШЕНА!               " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "Теперь вы можете просто запустить Factorio как обычно!" -ForegroundColor White
Write-Host "При первом запуске откроется окно привязки Кода Дружбы." -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Нажмите Enter для выхода..."
Read-Host
