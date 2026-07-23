"""Сообщения об ошибках источника данных: человек должен видеть причину, а не следствие.

На складском ПК приложение однажды написало «Файл базы не найден», хотя настоящая
причина была в неудавшемся скачивании с Яндекс.Диска. Файл отсутствовал именно
потому, что не скачался, — и чинить надо было не путь, а доступ.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.gui.worker import LookupWorker
from app.snapshot import SnapshotBuilder, SnapshotError


@pytest.fixture
def messages():
    return []


def make_worker(settings, messages, monkeypatch, pull_result):
    """Рабочий поток с подменённым скачиванием.

    pull_result: исключение (скачать не удалось) либо callable (успех).
    """
    w = LookupWorker(settings)

    def fake_pull():
        if isinstance(pull_result, Exception):
            raise pull_result
        return pull_result()

    monkeypatch.setattr(w, "_pull", fake_pull)
    w.connectionFailed.connect(lambda msg, detail: messages.append(msg))
    w.ready.connect(lambda text: messages.append(f"OK:{text}"))
    return w


def test_download_failure_is_reported_not_missing_file(qapp, app_data, messages,
                                                       monkeypatch):
    """Скачать не вышло и копии нет — на экране причина, а не «файл не найден»."""
    s = Settings(source="local", transport="yadisk", yadisk_token="y0_" + "x" * 40)
    w = make_worker(s, messages, monkeypatch,
                    SnapshotError("Яндекс.Диск отклонил токен (401)."))
    w.openLocal(pull_first=True)

    assert messages, "приложение обязано сообщить о проблеме"
    text = messages[0]
    assert "401" in text, f"нужна причина отказа, а показано: {text!r}"
    assert "не найден" not in text.lower(), "следствие вместо причины"


def test_message_names_the_real_blocker(qapp, app_data, messages, monkeypatch):
    s = Settings(source="local", transport="yadisk", yadisk_token="y0_" + "x" * 40)
    w = make_worker(s, messages, monkeypatch,
                    SnapshotError("Нет связи с Яндекс.Диском: таймаут"))
    w.openLocal(pull_first=True)
    assert "связи" in messages[0]


def test_existing_copy_survives_failed_download(qapp, app_data, messages, monkeypatch):
    """Интернет пропал, но вчерашняя база на месте — склад продолжает работать."""
    s = Settings(source="local", transport="yadisk", yadisk_token="y0_" + "x" * 40)
    b = SnapshotBuilder()
    code = b.add_barcode("2000463720017", "31349756", "Volvo 31349756")
    b.add_barcode_cell(code, "1-3-3-5-2", "Оригинал", True)
    b.write(s.local_db_path())

    w = make_worker(s, messages, monkeypatch, SnapshotError("нет интернета"))
    w.openLocal(pull_first=True)
    try:
        assert messages and messages[0].startswith("OK:"), \
            "с готовой базой приложение обязано работать, несмотря на обрыв связи"
        assert w.lookup and w._snapshot is not None
    finally:
        w.shutDown()


def test_no_source_configured_says_so(qapp, app_data, messages, monkeypatch):
    """Токен не введён — сообщение про настройки, а не про отсутствующий файл."""
    s = Settings(source="local", transport="yadisk", yadisk_token="")
    w = LookupWorker(s)
    w.connectionFailed.connect(lambda msg, detail: messages.append(msg))
    w.openLocal(pull_first=True)
    assert messages
    assert "не найден" in messages[0].lower() or "настрой" in messages[0].lower()
