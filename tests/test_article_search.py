"""Ручной ввод артикула: набрал с коробки, нажал Enter — этикетка вылезла.

Нужен, когда штрихкода на товаре нет или он не читается.
"""
from __future__ import annotations

import pytest

from app import printing
from app.config import Settings
from app.label import LabelData
from app.onec import LookupResult, Placement
from app.snapshot import Snapshot, SnapshotBuilder


# --- уровень данных -------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    b = SnapshotBuilder()
    code = b.add_barcode("2000463720017", "31349756", "Volvo 31349756")
    b.add_barcode_cell(code, "1-3-3-5-2", "Оригинал", True)

    art = b.add_article("6203", "NTN / SNR 6203")
    b.add_article_cell(art, "2-3-2-10-3", "Квант", True)
    b.add_article_cell(art, "2-3-7-4-2", "Потеряшки", False)

    snap = Snapshot(b.write(tmp_path / "cells.db"))
    snap.open()
    yield snap
    snap.close()


def test_lookup_article_finds_all_cells(db):
    res = db.lookup_article("6203", [])
    assert res.found and res.by_article
    assert res.article == "6203"
    assert len(res.placements) == 2


def test_lookup_article_is_case_insensitive(tmp_path):
    b = SnapshotBuilder()
    art = b.add_article("AbC123", "Деталь")
    b.add_article_cell(art, "1-1-1", "Склад", True)
    snap = Snapshot(b.write(tmp_path / "c.db"))
    snap.open()
    try:
        assert snap.lookup_article("abc123", []).found
    finally:
        snap.close()


def test_unknown_article_is_marked_as_article_search(db):
    """Флаг by_article нужен окну: сказать «артикула нет», а не «штрихкода нет»."""
    res = db.lookup_article("НЕТ-ТАКОГО", [])
    assert not res.found and res.by_article


def test_lookup_article_respects_warehouse_filter(db):
    res = db.lookup_article("6203", ["Потеряшки"])
    assert [p.warehouse for p in res.placements] == ["Потеряшки"]


def test_barcode_search_untouched_by_article_search(db):
    """Штрихкод по-прежнему ищется как штрихкод, с точным наименованием."""
    res = db.lookup("2000463720017", True, [])
    assert res.found and not res.by_article
    assert res.article == "31349756"


# --- окно -----------------------------------------------------------------
@pytest.fixture
def printed(monkeypatch):
    jobs: list[LabelData] = []
    monkeypatch.setattr(printing, "print_label",
                        lambda data, settings, style=None: jobs.append(data) or "Xprinter")
    monkeypatch.setattr(printing, "printer_available", lambda s: True)
    return jobs


@pytest.fixture
def window(qapp, app_data, monkeypatch):
    from app.gui import main_window as mw

    monkeypatch.setattr(mw.MainWindow, "_start_worker", lambda self: None)
    win = mw.MainWindow(Settings(source="local", auto_print=True))
    yield win
    win.close()


def test_article_field_exists(window):
    assert window.article_input is not None
    assert "артикул" in window.article_input.placeholderText().lower()


def test_entering_article_requests_article_search(window):
    """Enter в поле артикула шлёт именно поиск по артикулу, а не по штрихкоду."""
    asked = []
    window.articleRequested.connect(asked.append)
    window.article_input.setText("6203")
    window._on_article()
    assert asked == ["6203"]


def test_empty_article_does_nothing(window):
    asked = []
    window.articleRequested.connect(asked.append)
    window.article_input.setText("   ")
    window._on_article()
    assert asked == []


def test_article_result_prints_immediately(window, printed):
    window._on_article()  # чтобы пометить поиск как ручной
    window._on_result(LookupResult(
        barcode="6203", found=True, article="6203", name="NTN / SNR 6203",
        by_article=True,
        placements=[Placement(cell="2-3-2-10-3", warehouse="Квант", is_main=True)]))
    assert len(printed) == 1
    assert printed[0].cell == "2-3-2-10-3"


def test_missing_article_says_article_not_barcode(window):
    window._on_result(LookupResult(barcode="НЕТ", found=False, by_article=True))
    assert "Артикула" in window.status.text()


def test_missing_barcode_still_says_barcode(window):
    window._on_result(LookupResult(barcode="000", found=False))
    assert "Штрихкода" in window.status.text()


def test_focus_returns_to_article_field_after_manual_search(window, printed):
    """Набрал один артикул — курсор остался здесь же, для следующего."""
    window.article_input.setText("6203")
    window._on_article()
    window._on_result(LookupResult(
        barcode="6203", found=True, article="6203", name="",
        by_article=True,
        placements=[Placement(cell="1-1-1", warehouse="Склад", is_main=True)]))
    assert window.focusWidget() is window.article_input


def test_escape_returns_to_scanner(window):
    window.article_input.setFocus()
    window._focus_scanner_field()
    assert window.focusWidget() is window.scan_input


def test_manual_entry_is_not_debounced(window, printed):
    """Повтор артикула — осознанное действие: нужна ещё одна этикетка."""
    asked = []
    window.articleRequested.connect(asked.append)
    window.article_input.setText("6203")
    window._on_article()
    window._on_article()
    assert asked == ["6203", "6203"]
