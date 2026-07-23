"""Локальный снимок: запись, чтение и обновление из общей папки."""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.snapshot import (SCHEMA_VERSION, Snapshot, SnapshotBuilder, SnapshotError,
                          copy_if_newer)


@pytest.fixture
def db(tmp_path):
    """Небольшой снимок, повторяющий реальные особенности базы."""
    b = SnapshotBuilder()

    code = b.add_barcode("2000463720017", "31349756", "Volvo 31349756")
    b.add_barcode_cell(code, "1-3-3-5-2", "Оригинал", True)

    # Товар со штрихкодом, но без размещения.
    b.add_barcode("2000463720099", "99999", "Без ячейки")

    # Один артикул -> несколько ячеек на разных складах (как «6203»).
    art = b.add_article("6203", "NTN / SNR 6203")
    b.add_article_cell(art, "2-3-2-10-3", "Квант (Новые) 3 этаж", True)
    b.add_article_cell(art, "2-3-2-9-1-1", "Потеряшки", True)
    # Тот же артикул в другом регистре — в таблице это одна строка.
    b.add_article("6203", "6203 Подшипник")

    path = tmp_path / "cells.db"
    b.write(path, meta={"source_base": "ut2025 (Serv1C)"})
    snap = Snapshot(path)
    snap.open()
    yield snap
    snap.close()


def test_meta_records_source_and_counts(db):
    meta = db.meta()
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["source_base"] == "ut2025 (Serv1C)"
    assert meta["barcodes"] == "2"


def test_lookup_by_barcode(db):
    res = db.lookup("2000463720017", True, [])
    assert res.found and not res.by_article
    assert res.article == "31349756"
    assert [p.cell for p in res.placements] == ["1-3-3-5-2"]


def test_lookup_barcode_without_placement(db):
    """Штрихкод есть, ячейки нет — приложение должно сказать «нет размещения»."""
    res = db.lookup("2000463720099", True, [])
    assert res.found and res.article == "99999"
    assert res.placements == []


def test_lookup_unknown_code(db):
    assert not db.lookup("0000000000000", True, []).found


def test_fallback_by_article(db):
    res = db.lookup("6203", True, [])
    assert res.found and res.by_article
    assert len(res.placements) == 2


def test_fallback_is_case_insensitive(tmp_path):
    b = SnapshotBuilder()
    art = b.add_article("AbC123", "Деталь")
    b.add_article_cell(art, "1-1-1", "Склад", True)
    path = b.write(tmp_path / "c.db")
    snap = Snapshot(path)
    snap.open()
    try:
        assert snap.lookup("abc123", True, []).found
        assert snap.lookup("ABC123", True, []).found
    finally:
        snap.close()


def test_fallback_can_be_disabled(db):
    assert not db.lookup("6203", False, []).found


def test_ambiguous_article_name_is_deterministic(db):
    """Под артикулом несколько позиций — подпись берётся стабильно, а не «как повезёт»."""
    assert db.lookup("6203", True, []).name == "6203 Подшипник"


def test_warehouse_filter_applies_to_snapshot(db):
    res = db.lookup("6203", True, ["Потеряшки"])
    assert [p.warehouse for p in res.placements] == ["Потеряшки"]


def test_duplicate_rows_are_collapsed(tmp_path):
    """Регистр размещения разбит по помещениям — одна ячейка приходит дважды."""
    b = SnapshotBuilder()
    code = b.add_barcode("111", "ART", "Имя")
    b.add_barcode_cell(code, "1-1-1", "Склад", True)
    b.add_barcode_cell(code, "1-1-1", "Склад", True)
    path = b.write(tmp_path / "c.db")
    assert sqlite3.connect(path).execute(
        "SELECT COUNT(*) FROM barcode_cell").fetchone()[0] == 1


def test_blank_values_are_skipped(tmp_path):
    b = SnapshotBuilder()
    assert b.add_barcode("   ", "A", "N") == ""
    b.add_barcode_cell("111", "   ", "Склад", True)   # пустая ячейка не пишется
    assert b.counts == {"barcodes": 0, "barcode_cells": 0,
                        "articles": 0, "article_cells": 0}


def test_write_is_atomic(tmp_path):
    """Файл появляется целиком; временных огрызков рядом не остаётся."""
    path = SnapshotBuilder().write(tmp_path / "cells.db")
    assert path.exists()
    assert not (tmp_path / "cells.db.tmp").exists()


def test_open_missing_file_explains_itself(tmp_path):
    with pytest.raises(SnapshotError, match="не найден"):
        Snapshot(tmp_path / "нет.db").open()


def test_open_rejects_foreign_file(tmp_path):
    path = tmp_path / "чужое.db"
    sqlite3.connect(path).execute("CREATE TABLE t (x)")
    with pytest.raises(SnapshotError):
        Snapshot(path).open()


def test_open_rejects_incompatible_schema(tmp_path):
    path = SnapshotBuilder().write(tmp_path / "cells.db")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(SnapshotError, match="несовместимой"):
        Snapshot(path).open()


def test_age_text_mentions_export_date(db):
    assert "выгрузка от" in db.age_text()


# --- обновление из общей папки -------------------------------------------
def test_copy_if_newer_copies_when_absent(tmp_path):
    src = SnapshotBuilder().write(tmp_path / "share" / "cells.db")
    dst = tmp_path / "local" / "cells.db"
    assert copy_if_newer(src, dst) is True
    assert dst.exists()


def test_copy_if_newer_skips_same_file(tmp_path):
    src = SnapshotBuilder().write(tmp_path / "share" / "cells.db")
    dst = tmp_path / "local" / "cells.db"
    copy_if_newer(src, dst)
    assert copy_if_newer(src, dst) is False


def test_copy_if_newer_takes_fresh_export(tmp_path):
    src = SnapshotBuilder().write(tmp_path / "share" / "cells.db")
    dst = tmp_path / "local" / "cells.db"
    copy_if_newer(src, dst)

    b = SnapshotBuilder()
    b.add_barcode("111", "ART", "Новый товар")
    time.sleep(0.01)
    b.write(src)                                  # выгрузка обновилась
    assert copy_if_newer(src, dst) is True
    snap = Snapshot(dst)
    snap.open()
    try:
        assert snap.lookup("111", True, []).found
    finally:
        snap.close()


def test_copy_if_newer_reports_unreachable_share(tmp_path):
    with pytest.raises(SnapshotError, match="недоступен"):
        copy_if_newer(tmp_path / "нет" / "cells.db", tmp_path / "local.db")


def test_copy_survives_repeated_delivery(tmp_path):
    """Доставка идемпотентна: повторный вызов не портит уже доставленный файл."""
    src = SnapshotBuilder().write(tmp_path / "staging" / "cells.db")
    dst = tmp_path / "warehouse" / "cells.db"
    copy_if_newer(src, dst)
    size = dst.stat().st_size
    copy_if_newer(src, dst)
    assert dst.stat().st_size == size
    snap = Snapshot(dst)
    snap.open()          # файл остался читаемым
    snap.close()


def test_copy_leaves_no_partial_file(tmp_path):
    src = SnapshotBuilder().write(tmp_path / "share" / "cells.db")
    dst = tmp_path / "local" / "cells.db"
    copy_if_newer(src, dst)
    assert not (dst.parent / "cells.db.part").exists()
