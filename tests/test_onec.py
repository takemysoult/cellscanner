"""Разбор ответа 1С и текстов ошибок — без реального COM.

``OneCClient._placements`` — чистая функция над строками запроса, поэтому её можно
проверять на клиенте без соединения.
"""
from __future__ import annotations

from app.config import Settings
from app.onec import OneCClient, describe_error, error_text


def rows(*items):
    """Строки в порядке колонок запроса: Ячейка, Артикул, Наименование, Склад, Основная."""
    return [(cell, "ART", "Наименование", wh, main) for cell, wh, main in items]


def client(**kw):
    return OneCClient(Settings(**kw))


def test_placements_basic():
    p = client()._placements(rows(("1-3-3-5-2", "Оригинал", True)))
    assert len(p) == 1
    assert p[0].cell == "1-3-3-5-2" and p[0].warehouse == "Оригинал" and p[0].is_main


def test_placements_drop_rows_without_cell():
    """ЛЕВОЕ СОЕДИНЕНИЕ даёт строку с пустой ячейкой, если размещения нет."""
    assert client()._placements(rows(("", "Оригинал", False))) == []


def test_placements_dedupe_same_cell_and_warehouse():
    """Регистр разбит ещё и по помещениям — одна пара склад+ячейка приходит дважды."""
    p = client()._placements(rows(("2-3-2-9-1-1", "Квант", True),
                                  ("2-3-2-9-1-1", "Квант", True)))
    assert len(p) == 1


def test_placements_keep_same_cell_in_other_warehouse():
    p = client()._placements(rows(("2-3-2-9-1-1", "Квант", True),
                                  ("2-3-2-9-1-1", "Потеряшки", True)))
    assert {x.warehouse for x in p} == {"Квант", "Потеряшки"}


def test_placements_main_goes_first():
    p = client()._placements(rows(("2-2-2", "Склад Б", False),
                                  ("1-1-1", "Склад Я", True)))
    assert [x.cell for x in p] == ["1-1-1", "2-2-2"]


def test_placements_sorted_by_warehouse_when_all_main():
    p = client()._placements(rows(("3-3-3", "Потеряшки", True),
                                  ("1-1-1", "Квант", True)))
    assert [x.warehouse for x in p] == ["Квант", "Потеряшки"]


def test_placements_warehouse_filter():
    c = client(warehouses=["Оригинал"])
    p = c._placements(rows(("1-1-1", "Оригинал", True), ("2-2-2", "Потеряшки", True)))
    assert [x.cell for x in p] == ["1-1-1"]


def test_placements_warehouse_filter_is_case_insensitive():
    c = client(warehouses=["оригинал"])
    assert len(c._placements(rows(("1-1-1", "ОРИГИНАЛ", True)))) == 1


def test_placements_empty_filter_keeps_everything():
    c = client(warehouses=[])
    assert len(c._placements(rows(("1-1-1", "A", True), ("2-2-2", "B", True)))) == 2


def test_error_text_redacts_password():
    """Ошибка COM часто содержит всю строку соединения — пароль не должен утечь."""
    exc = Exception('Ошибка: Srvr="S";Ref="R";Usr="Натали";Pwd="секрет";')
    text = error_text(exc)
    assert "секрет" not in text and "Натали" not in text
    assert 'Pwd="***"' in text


def test_error_text_redacts_password_with_inner_quote():
    exc = Exception('Pwd="a""b";Ref="R";')
    assert "a" not in error_text(exc).split("Ref")[0].replace('Pwd="***";', "")


def test_error_text_flattens_com_error_args():
    exc = Exception(-2147352567, "Ошибка.", ("описание от 1С",), None)
    text = error_text(exc)
    assert "описание от 1С" in text and "0x80020009" in text


def test_describe_error_external_connection_right():
    exc = Exception("Недостаточно прав: Внешнее соединение")
    assert "Внешнее соединение" in describe_error(exc)


def test_describe_error_connector_not_registered():
    assert "коннектор" in describe_error(Exception("Class not registered 0x80040154")).lower()


def test_describe_error_invalid_class_string():
    """0x800401F3 — ProgID неизвестен системе: коннектор вообще не регистрировали.

    Именно этот случай складской ПК показывал как безликое «Не удалось
    подключиться»: код в разборе не значился.
    """
    exc = Exception(-2147221005, "Ошибка.", ("Недопустимая строка класса",), None)
    assert "коннектор" in describe_error(exc).lower()


def test_describe_error_class_not_registered_masculine():
    """Русская локаль даёт «Класс не зарегистрирован» — без окончания «-а»."""
    assert "коннектор" in describe_error(Exception("Класс не зарегистрирован")).lower()


def test_describe_error_library_not_registered_feminine():
    assert "коннектор" in describe_error(Exception("Библиотека не зарегистрирована")).lower()


def test_describe_error_pywin32_missing():
    exc = RuntimeError("Нужен pywin32 (Windows). Установите: pip install pywin32")
    assert "pywin32" in describe_error(exc)


def test_describe_error_bad_credentials():
    assert "пароль" in describe_error(Exception("Ошибка идентификации пользователя")).lower()


def test_describe_error_unknown_falls_back():
    assert describe_error(Exception("что-то новое")).startswith("Не удалось")
