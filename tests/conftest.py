"""Общие приспособления для тестов.

Qt-объекты (QFontMetricsF в подборе шрифта, QPixmap в превью) требуют живого
QApplication, поэтому он создаётся один раз на прогон в offscreen-режиме — окна
на экране не появляются.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    from PySide2.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def app_data(tmp_path, monkeypatch):
    """Изолировать %LOCALAPPDATA%-каталог приложения на время теста."""
    monkeypatch.setenv("CELLSCANNER_APP_DATA", str(tmp_path))
    return tmp_path
