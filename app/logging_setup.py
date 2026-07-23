"""Журнал приложения: ротация файлов в ``%LOCALAPPDATA%\\CellScanner\\logs``.

Журнал нужен для разбора «почему не напечаталось»: в него пишутся подключения к
1С, результаты поиска и ошибки печати. Пароль в журнал не попадает — текст ошибок
1С предварительно чистится (см. :func:`app.onec.error_text`).
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import paths

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    paths.ensure_dirs()
    root = logging.getLogger()
    if root.handlers:  # повторный вызов не должен дублировать записи
        return
    root.setLevel(level)

    handler = RotatingFileHandler(paths.logs_dir() / "scanner.log",
                                  maxBytes=1_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)

    # Консоль Windows по умолчанию в cp866/cp1251 — русские сообщения в ней
    # превращаются в кашу. Переводим поток в UTF-8, если он это умеет.
    import sys
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # поток не поддерживает (pythonw.exe)
        pass
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(console)
