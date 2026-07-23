"""Построение текста запроса: экранирование и подстановка штрихкода."""
from __future__ import annotations

import pytest

from app.query import (DEFAULT_FALLBACK_QUERY, DEFAULT_QUERY, PLACEHOLDER,
                       build_lookup_query, normalize_barcode, quote_1c)


def test_quote_1c_wraps_in_quotes():
    assert quote_1c("2000463720017") == '"2000463720017"'


def test_quote_1c_doubles_inner_quote():
    # Внутренняя кавычка удваивается — литерал остаётся одним литералом.
    assert quote_1c('13"x5"') == '"13""x5"""'


def test_quote_1c_handles_empty():
    assert quote_1c("") == '""'


@pytest.mark.parametrize("template", [DEFAULT_QUERY, DEFAULT_FALLBACK_QUERY])
def test_default_templates_have_placeholder(template):
    assert PLACEHOLDER in template


def test_build_lookup_query_substitutes():
    q = build_lookup_query(DEFAULT_QUERY, "2000463720017")
    assert PLACEHOLDER not in q
    assert '"2000463720017"' in q


def test_build_lookup_query_is_injection_safe():
    """Кавычка в штрихкоде не должна закрывать литерал и дописывать условие."""
    q = build_lookup_query('ГДЕ ШК.Штрихкод = {ШТРИХКОД}', '" ИЛИ ИСТИНА ИЛИ "')
    assert q == 'ГДЕ ШК.Штрихкод = """ ИЛИ ИСТИНА ИЛИ """'


def test_build_lookup_query_rejects_template_without_placeholder():
    with pytest.raises(ValueError, match="ШТРИХКОД"):
        build_lookup_query("ВЫБРАТЬ 1", "123")


@pytest.mark.parametrize("raw,expected", [
    ("  2000463720017\r\n", "2000463720017"),
    ("2000 4637 20017", "2000463720017"),
    ("\t!000382266449/1\n", "!000382266449/1"),
    ("", ""),
    (None, ""),
])
def test_normalize_barcode(raw, expected):
    assert normalize_barcode(raw) == expected
