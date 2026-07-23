# PyInstaller spec для «Ячейка по штрихкоду» (onedir, без окна консоли).
# Собирать из корня проекта venv-ом Python 3.10 x64 + PySide2:
#   .venv\Scripts\pyinstaller packaging\cellscanner.spec
#
# Разрядность сборки = разрядность 1С на рабочем месте (x64), иначе COM-коннектор
# не подключится.
import os

ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # корень проекта (родитель packaging/)
ENTRY = os.path.join(ROOT, "main.py")
ICON = os.path.join(SPECPATH, "cellscanner.ico")

# Модули, которые импортируются лениво/динамически — статический анализ
# PyInstaller их может не увидеть.
hiddenimports = [
    "win32timezone",              # нужен pywin32/win32com в собранном виде
    "win32com.client.dynamic",    # позднее связывание с COM-коннектором 1С
    "win32crypt",                 # DPAPI: шифрование пароля 1С и токена Диска
    "pythoncom",
    "pywintypes",
    "requests",                   # доставка файла через Яндекс.Диск
]

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Сборка только на PySide2 — Qt5 и Qt6 в одном процессе несовместимы.
    # PIL нужен лишь генератору иконки, в приложение он не входит.
    excludes=["tkinter", "pytest", "PySide6", "PIL", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="CellScanner",
    console=False,                 # оконное приложение, консоль не нужна
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CellScanner")
