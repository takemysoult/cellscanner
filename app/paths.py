"""Расположение пользовательских данных приложения.

Настройки, журнал и временные PDF-стикеры лежат в ``%LOCALAPPDATA%\\CellScanner``.
Переменная окружения ``CELLSCANNER_APP_DATA`` переопределяет весь каталог целиком
(используется тестами для изоляции состояния).
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "CellScanner"


def app_data_dir() -> Path:
    override = os.environ.get("CELLSCANNER_APP_DATA")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def pdf_dir() -> Path:
    """Каталог для стикеров, открываемых в браузере (кнопка «PDF»)."""
    return app_data_dir() / "pdf"


def export_staging_path() -> Path:
    """Куда утилита выгрузки складывает снимок ДО доставки получателю.

    Выгрузка и доставка разделены: складской ПК может быть выключен, и тогда
    файл просто ждёт здесь, а доставку повторит следующая попытка. Иначе
    недоступность получателя означала бы потерю всей ночной выгрузки.
    """
    return app_data_dir() / "export" / "cells.db"


def ensure_dirs() -> None:
    app_data_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    pdf_dir().mkdir(parents=True, exist_ok=True)
    export_staging_path().parent.mkdir(parents=True, exist_ok=True)
