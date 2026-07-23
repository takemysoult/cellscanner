"""Самодиагностика окружения: почему 1С не подключается на чужой машине.

Приложение стоит на складском ПК, до которого не дотянуться, поэтому оно само
должно уметь сказать, чего ему не хватает. Отчёт пишется в журнал при запуске и
показывается по кнопке «Подробности» рядом с сообщением об ошибке.

Главная проверяемая вещь — регистрация COM-коннектора 1С. Он регистрируется
отдельно в 64- и 32-битной ветках реестра, и 64-битное приложение видит ТОЛЬКО
64-битную. Если 1С поставили 32-битную (или коннектор не регистрировали вовсе),
``Dispatch("V83.COMConnector")`` падает мгновенно с «Недопустимая строка класса».
"""
from __future__ import annotations

import struct
import winreg
from pathlib import Path

# Каталоги, куда 1С ставится по умолчанию: x64 и x86 соответственно.
_INSTALL_DIRS = (Path(r"C:\Program Files\1cv8"), Path(r"C:\Program Files (x86)\1cv8"))


def process_bits() -> int:
    return struct.calcsize("P") * 8


def _server_path(progid: str, view: int) -> str | None:
    """Путь к comcntr.dll для ProgID в заданной ветке реестра, иначе None.

    ``view`` — winreg.KEY_WOW64_64KEY или KEY_WOW64_32KEY: без явного указания
    64-битный процесс никогда не увидит 32-битную регистрацию, и наоборот.
    """
    access = winreg.KEY_READ | view
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID", 0, access) as k:
            clsid = winreg.QueryValue(k, "")
    except OSError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            rf"CLSID\{clsid}\InprocServer32", 0, access) as k:
            return winreg.QueryValue(k, "")
    except OSError:
        # ProgID есть, а сервер за ним не зарегистрирован — тоже неработоспособно.
        return f"{clsid} (без InprocServer32)"


def connector_state(progid: str = "V83.COMConnector") -> tuple[str | None, str | None]:
    """(путь в 64-битной ветке, путь в 32-битной ветке); None — не зарегистрирован."""
    return (_server_path(progid, winreg.KEY_WOW64_64KEY),
            _server_path(progid, winreg.KEY_WOW64_32KEY))


def installed_platforms() -> list[str]:
    """Найденные на диске версии платформы 1С, с пометкой разрядности каталога."""
    found: list[str] = []
    for base in _INSTALL_DIRS:
        if not base.is_dir():
            continue
        bits = "x86" if "(x86)" in str(base) else "x64"
        for item in sorted(base.iterdir(), reverse=True):
            if item.is_dir() and item.name[:1].isdigit():
                found.append(f"{item.name} ({bits})")
    return found


def pywin32_state() -> str:
    try:
        import pythoncom  # noqa: F401
        import win32com.client.dynamic  # noqa: F401
    except ImportError as e:
        return f"НЕ ЗАГРУЖАЕТСЯ: {e}"
    return "доступен"


def advice(progid: str = "V83.COMConnector") -> str:
    """Что делать — исходя из того, что реально найдено на этой машине."""
    x64, x86 = connector_state(progid)
    bits = process_bits()
    if x64 and bits == 64:
        return "Коннектор зарегистрирован верно."
    if x86 and not x64:
        version = next((p for p in installed_platforms() if "x64" in p), None)
        cmd = (rf'regsvr32 "C:\Program Files\1cv8\{version.split()[0]}\bin\comcntr.dll"'
               if version else
               r'regsvr32 "C:\Program Files\1cv8\<версия>\bin\comcntr.dll"')
        return ("Коннектор 1С зарегистрирован ТОЛЬКО как 32-битный, а приложение "
                "64-битное — соединение невозможно.\n"
                "Установите 64-битную платформу 1С и зарегистрируйте её коннектор "
                f"от имени администратора:\n    {cmd}")
    if not x64 and not x86:
        return ("COM-коннектор 1С не зарегистрирован в системе.\n"
                "Зарегистрируйте его от имени администратора:\n"
                r'    regsvr32 "C:\Program Files\1cv8\<версия>\bin\comcntr.dll"')
    return "Коннектор найден, но соединение не устанавливается — смотрите текст ошибки."


def report(progid: str = "V83.COMConnector") -> str:
    """Полный отчёт для журнала и окна «Подробности»."""
    x64, x86 = connector_state(progid)
    platforms = installed_platforms()
    lines = [
        f"Разрядность приложения: {process_bits()} бит",
        f"pywin32: {pywin32_state()}",
        f"COM-коннектор {progid}:",
        f"    64-битная ветка реестра: {x64 or 'НЕ ЗАРЕГИСТРИРОВАН'}",
        f"    32-битная ветка реестра: {x86 or 'не зарегистрирован'}",
        f"Платформы 1С на диске: {', '.join(platforms) if platforms else 'не найдены'}",
        "",
        advice(progid),
    ]
    return "\n".join(lines)
