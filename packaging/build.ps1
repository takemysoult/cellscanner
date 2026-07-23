# Сборка: CellScanner.exe (PyInstaller) -> установщик (Inno Setup) -> копия на рабочий стол.
# Запуск из любого места:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# venv Python 3.10 x64 + PySide2: Qt5 работает начиная с Win7, Qt6 требует Win10 1809+.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Не найден venv: $py. Смотри README, раздел «Установка»." }

# Среда выполнения VC++ (~25 МБ) в репозиторий не кладётся — качаем, если нет.
$redist = Join-Path $root "packaging\redist\vc_redist.x64.exe"
if (-not (Test-Path $redist)) {
    Write-Host "==> Скачиваю vc_redist.x64.exe"
    New-Item -ItemType Directory -Force (Split-Path $redist) | Out-Null
    & curl.exe -L --max-time 300 -o $redist "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    if ((-not (Test-Path $redist)) -or ((Get-Item $redist).Length -lt 10000000)) {
        throw "Не удалось скачать vc_redist.x64.exe"
    }
}

$icon = Join-Path $root "packaging\cellscanner.ico"
if (-not (Test-Path $icon)) {
    Write-Host "==> Генерирую иконку"
    & $py "packaging\make_icon.py"
    if ($LASTEXITCODE -ne 0) { throw "make_icon.py упал ($LASTEXITCODE)" }
}

# Собираем в НОВЫЙ каталог на каждый прогон. Прежний PyInstaller чистит сам, но
# на этой машине антивирус успевает взять в работу свежие DLL и держит
# dist\CellScanner\_internal — удаление падает с «файл занят другим процессом».
# Свежий каталог обходит это без гонки; старые подчищаем ниже, но не настаиваем.
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$distRel = "dist\build-$stamp"
Write-Host "==> PyInstaller (onedir CellScanner.exe) -> $distRel"
& $py -m PyInstaller --noconfirm --distpath $distRel "packaging\cellscanner.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller упал ($LASTEXITCODE)" }

$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { throw "ISCC.exe не найден: $iscc" }
Write-Host "==> Inno Setup (установщик)"
& $iscc "/DDistDir=$distRel" "packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC упал ($LASTEXITCODE)" }

# Старые каталоги сборок: удаляем те, что отпустил антивирус. Неудача не важна —
# следующий прогон всё равно соберёт в свой каталог.
Get-ChildItem (Join-Path $root "dist") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne "build-$stamp" -and
                   ($_.Name -like "build-*" -or $_.Name -eq "CellScanner") } |
    ForEach-Object { try { Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop }
                     catch { Write-Host "    (занят, оставляю: $($_.Name))" } }

$setup = Get-ChildItem (Join-Path $root "dist\CellScanner-Setup-*.exe") |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
Copy-Item $setup.FullName ([Environment]::GetFolderPath("Desktop")) -Force
Write-Host "==> Готово: $($setup.Name) собран и скопирован на рабочий стол"
