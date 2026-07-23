"""Самодиагностика: совет должен зависеть от того, что реально найдено в реестре."""
from __future__ import annotations

import pytest

from app import diagnostics

X64_DLL = r"C:\Program Files\1cv8\8.3.27.1989\bin\comcntr.dll"
X86_DLL = r"C:\Program Files (x86)\1cv8\8.3.25.1374\bin\comcntr.dll"


@pytest.fixture
def state(monkeypatch):
    """Подменить чтение реестра парой (64-битная ветка, 32-битная ветка)."""
    def apply(x64, x86):
        monkeypatch.setattr(diagnostics, "connector_state", lambda progid="x": (x64, x86))
    return apply


def test_advice_ok_when_registered_for_64bit(state):
    state(X64_DLL, X86_DLL)
    assert "верно" in diagnostics.advice()


def test_advice_names_bitness_mismatch(state):
    """Коннектор только 32-битный — самый частый «мгновенный» отказ."""
    state(None, X86_DLL)
    text = diagnostics.advice()
    assert "32-битный" in text
    assert "regsvr32" in text


def test_advice_when_not_registered_at_all(state):
    state(None, None)
    text = diagnostics.advice()
    assert "не зарегистрирован" in text.lower()
    assert "regsvr32" in text


def test_report_covers_every_decisive_fact():
    """Отчёт уходит в журнал на чужой машине — в нём должно быть всё для разбора."""
    text = diagnostics.report()
    for required in ("Разрядность приложения", "pywin32", "64-битная ветка",
                     "32-битная ветка", "Платформы 1С"):
        assert required in text


def test_process_bits_is_64_in_this_build():
    # Разрядность обязана совпадать с платформой 1С; 32-битная сборка не подключится.
    assert diagnostics.process_bits() == 64
