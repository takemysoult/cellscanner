"""Настройки: строка соединения, сохранение/чтение, разбор списка баз 1С."""
from __future__ import annotations

import json

import pytest

from app.config import Settings, parse_connect_string
from app.paths import settings_path


def test_conn_string_server():
    s = Settings(kind="server", srvr="Serv1C", ref="ut2025", usr="Натали", password="p")
    assert s.conn_string() == 'Srvr="Serv1C";Ref="ut2025";Usr="Натали";Pwd="p";'


def test_conn_string_file():
    s = Settings(kind="file", file_path=r"C:\Bases\ut", usr="u", password="")
    assert s.conn_string() == r'File="C:\Bases\ut";Usr="u";Pwd="";'


def test_conn_string_escapes_quotes():
    """Кавычка в пароле удваивается, иначе строка соединения развалится."""
    s = Settings(kind="server", srvr="S", ref="R", usr="u", password='a"b')
    assert 'Pwd="a""b";' in s.conn_string()


@pytest.mark.parametrize("settings", [
    Settings(kind="server", srvr="", ref="ut"),
    Settings(kind="server", srvr="S", ref=""),
    Settings(kind="file", file_path=""),
])
def test_conn_string_requires_base(settings):
    with pytest.raises(ValueError):
        settings.conn_string()
    assert not settings.is_configured()


def test_describe_base_hides_credentials():
    s = Settings(kind="server", srvr="Serv1C", ref="ut2025", usr="u", password="секрет")
    assert s.describe_base() == "ut2025 (Serv1C)"
    assert "секрет" not in s.describe_base()


def test_save_load_roundtrip(app_data):
    s = Settings(kind="server", srvr="Serv1C", ref="ut2025", usr="Натали",
                 password="секрет", warehouses=["Оригинал"], copies=2,
                 label_w_mm=40.0, label_h_mm=58.0, printer_name="Xprinter XP-365B")
    s.save()
    loaded = Settings.load()
    assert loaded.srvr == "Serv1C"
    assert loaded.warehouses == ["Оригинал"]
    assert loaded.copies == 2
    assert loaded.printer_name == "Xprinter XP-365B"
    assert loaded.password == "секрет"      # расшифровался через DPAPI


def test_saved_file_has_no_plaintext_password(app_data):
    Settings(kind="server", srvr="S", ref="R", usr="u", password="ОченьСекретно").save()
    raw = settings_path().read_text(encoding="utf-8")
    assert "ОченьСекретно" not in raw
    assert "password" not in json.loads(raw)   # только password_enc


def test_load_missing_file_gives_defaults(app_data):
    loaded = Settings.load()
    assert loaded.progid == "V83.COMConnector"
    assert loaded.label_w_mm == 40.0 and loaded.label_h_mm == 58.0


def test_load_ignores_unknown_keys(app_data):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps({"srvr": "S", "неизвестное": 1}),
                               encoding="utf-8")
    assert Settings.load().srvr == "S"


def test_load_survives_broken_file(app_data):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text("{это не json", encoding="utf-8")
    assert Settings.load().srvr == ""       # молча берём значения по умолчанию


def test_default_source_is_local_db():
    """По умолчанию приложение ставится на склад, где 1С недоступна напрямую."""
    assert Settings().uses_local_db


def test_ready_to_work_local_needs_share_or_file(app_data):
    ready, problem = Settings(source="local").ready_to_work()
    assert not ready and "файл с выгрузкой" in problem.lower()


def test_ready_to_work_local_ok_with_share(app_data):
    ready, _ = Settings(source="local", snapshot_share=r"\\SRV\share\cells.db").ready_to_work()
    assert ready


def test_ready_to_work_local_ok_with_existing_file(app_data):
    s = Settings(source="local")
    s.local_db_path().parent.mkdir(parents=True, exist_ok=True)
    s.local_db_path().write_bytes(b"")     # файл с прошлой смены — работать можно
    assert s.ready_to_work()[0]


def test_ready_to_work_com_needs_connection(app_data):
    ready, problem = Settings(source="com", srvr="", ref="").ready_to_work()
    assert not ready and "1С" in problem


def test_local_db_path_is_inside_app_data(app_data):
    assert Settings().local_db_path().parent == app_data


def test_parse_connect_string_server():
    parsed = parse_connect_string('Srvr="Serv1C";Ref="ut2025";')
    assert parsed["srvr"] == "Serv1C" and parsed["ref"] == "ut2025"


def test_parse_connect_string_file():
    assert parse_connect_string(r'File="C:\Bases\ut";')["file"] == r"C:\Bases\ut"
