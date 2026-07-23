"""Главный сценарий склада: пикнул — этикетка вылезла. Без единого клика.

Проверяется весь путь окна: ввод из сканера -> поиск -> печать, с подменённым
принтером (реальная бумага в тестах не нужна).
"""
from __future__ import annotations

import pytest

from app import printing
from app.config import Settings
from app.label import LabelData
from app.onec import LookupResult, Placement


@pytest.fixture
def printed(monkeypatch):
    """Перехватить печать: вместо принтера — список напечатанного."""
    jobs: list[LabelData] = []

    def fake_print(data, settings, style=None):
        jobs.append(data)
        return "Xprinter XP-365B"

    monkeypatch.setattr(printing, "print_label", fake_print)
    monkeypatch.setattr(printing, "printer_available", lambda s: True)
    monkeypatch.setattr(printing, "resolve_printer", lambda name: object())
    return jobs


@pytest.fixture
def window(qapp, app_data, monkeypatch):
    """Окно без рабочего потока: источник данных подменяем прямо в тесте."""
    from app.gui import main_window as mw

    monkeypatch.setattr(mw.MainWindow, "_start_worker", lambda self: None)
    win = mw.MainWindow(Settings(source="local", auto_print=True))
    yield win
    win.close()


def found(*placements: Placement) -> LookupResult:
    return LookupResult(barcode="2000463720017", found=True, article="31349756",
                        name="Volvo 31349756", placements=list(placements))


MAIN = Placement(cell="1-3-3-5-2", warehouse="Оригинал", is_main=True)
SECOND = Placement(cell="2-3-7-4-2", warehouse="Квант", is_main=False)


def test_single_cell_prints_without_any_click(window, printed):
    """Товар в одной ячейке — этикетка уходит на принтер сама."""
    window._on_result(found(MAIN))
    assert len(printed) == 1
    assert printed[0].cell == "1-3-3-5-2"
    assert printed[0].article == "31349756"


def test_status_confirms_printing(window, printed):
    window._on_result(found(MAIN))
    assert "Напечатано" in window.status.text()


def test_several_cells_still_prints_main_by_default(window, printed):
    """По умолчанию печатается основная ячейка, а не ожидание выбора."""
    window._on_result(found(MAIN, SECOND))
    assert len(printed) == 1
    assert printed[0].cell == MAIN.cell
    assert "есть ещё" in window.status.text()


def test_waiting_mode_prints_nothing(qapp, app_data, printed, monkeypatch):
    """Если галку «печатать основную» снять — приложение ждёт выбора."""
    from app.gui import main_window as mw

    monkeypatch.setattr(mw.MainWindow, "_start_worker", lambda self: None)
    win = mw.MainWindow(Settings(source="local", auto_print=True,
                                 auto_print_when_many=False))
    try:
        win._on_result(found(MAIN, SECOND))
        assert printed == []
        assert "выберите" in win.status.text()
    finally:
        win.close()


def test_nothing_found_prints_nothing(window, printed):
    window._on_result(LookupResult(barcode="0000000000000", found=False))
    assert printed == []
    assert "НЕ НАЙДЕНО" in window.headline.text()


def test_item_without_placement_prints_nothing(window, printed):
    """Товар есть, ячейка не задана — печатать нечего, но это видно на экране."""
    window._on_result(LookupResult(barcode="111", found=True, article="99999",
                                   name="Без ячейки"))
    assert printed == []
    assert "НЕТ РАЗМЕЩЕНИЯ" in window.headline.text()


def test_copies_setting_is_passed_to_printer(qapp, app_data, monkeypatch):
    """Две копии — один вызов печати, копии делает сам драйвер."""
    from app.gui import main_window as mw

    seen = {}

    def fake_print(data, settings, style=None):
        seen["copies"] = settings.copies
        return "Xprinter"

    monkeypatch.setattr(printing, "print_label", fake_print)
    monkeypatch.setattr(printing, "printer_available", lambda s: True)
    monkeypatch.setattr(mw.MainWindow, "_start_worker", lambda self: None)
    win = mw.MainWindow(Settings(source="local", auto_print=True, copies=2))
    try:
        win._on_result(found(MAIN))
        assert seen["copies"] == 2
    finally:
        win.close()


def test_printer_failure_does_not_crash_scanning(window, monkeypatch):
    """Принтер выключили посреди смены — приложение сообщает, но продолжает работать."""
    def boom(data, settings, style=None):
        raise RuntimeError("Принтер не найден")

    monkeypatch.setattr(printing, "print_label", boom)
    window._on_result(found(MAIN))
    assert "не удалась" in window.status.text().lower()
    assert window.scan_input.isEnabled(), "поле сканера обязано остаться рабочим"
