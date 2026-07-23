"""Разделение «собрать снимок» и «доставить его».

Ключевой сценарий: складской ПК на ночь выключают. Выгрузка обязана пережить
недоступность получателя, не потеряв результат, — иначе одна выключенная машина
означает потерянную ночную выгрузку.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from export_cells import deliver  # noqa: E402
from app.config import Settings  # noqa: E402
from app.snapshot import Snapshot, SnapshotBuilder  # noqa: E402


@pytest.fixture
def staging(tmp_path):
    """Готовый снимок, как его оставляет шаг выгрузки."""
    b = SnapshotBuilder()
    code = b.add_barcode("2000463720017", "31349756", "Volvo 31349756")
    b.add_barcode_cell(code, "1-3-3-5-2", "Оригинал", True)
    return b.write(tmp_path / "export" / "cells.db")


def to(destination) -> Settings:
    """Настройки с доставкой файлом в заданный путь."""
    return Settings(transport="file", snapshot_share=str(destination))


def test_delivers_to_empty_destination(staging, tmp_path):
    dst = tmp_path / "warehouse" / "cells.db"
    assert deliver(staging, to(dst)) == 0
    assert dst.exists()


def test_delivered_file_is_usable(staging, tmp_path):
    dst = tmp_path / "warehouse" / "cells.db"
    deliver(staging, to(dst))
    snap = Snapshot(dst)
    snap.open()
    try:
        assert snap.lookup("2000463720017", True, []).placements[0].cell == "1-3-3-5-2"
    finally:
        snap.close()


def test_unreachable_destination_returns_2_and_keeps_snapshot(staging, tmp_path):
    """Складской ПК выключен: код 2 (не 1), снимок остаётся на месте."""
    rc = deliver(staging, to(r"\\НЕТ-ТАКОГО-ПК\CellScanner\cells.db"))
    assert rc == 2
    assert staging.exists(), "снимок обязан пережить неудачную доставку"


def test_retry_after_failure_succeeds(staging, tmp_path):
    """Досыл: получатель появился — тот же снимок доставляется без обращения к 1С."""
    assert deliver(staging, to(r"\\НЕТ-ТАКОГО-ПК\CellScanner\cells.db")) == 2
    dst = tmp_path / "warehouse" / "cells.db"
    assert deliver(staging, to(dst)) == 0
    assert dst.exists()


def test_second_delivery_is_noop(staging, tmp_path):
    dst = tmp_path / "warehouse" / "cells.db"
    deliver(staging, to(dst))
    assert deliver(staging, to(dst)) == 0        # уже актуален, но не ошибка


def test_nothing_to_deliver_is_an_error(tmp_path):
    """--deliver-only до первой выгрузки: honest error, а не тихий успех."""
    assert deliver(tmp_path / "нет" / "cells.db", to(tmp_path / "d.db")) == 1
