"""Рабочий поток: всё медленное и всё COM-ное живёт здесь, интерфейс не блокируется.

Источников два и они взаимоисключающие:

  * ``local`` — локальный файл выгрузки (рабочее место со сканером: там тонкий
    клиент 1С, а в нём нет comcntr.dll — COM-соединение невозможно в принципе);
  * ``com`` — прямое подключение к 1С (машина с полной платформой).

COM апартаментно-потоковый, поэтому ``OneCClient`` создаётся, используется и
закрывается одним и тем же потоком (см. :mod:`app.onec`). GUI общается с ним
только сигналами — очередь Qt сама переносит вызовы между потоками.
"""
from __future__ import annotations

import logging

from PySide2.QtCore import QObject, QThread, Signal, Slot

from ..config import Settings
from ..onec import OneCClient, describe_error, error_text
from ..snapshot import Snapshot, SnapshotError, pull

log = logging.getLogger(__name__)


class LookupWorker(QObject):
    """Живёт в рабочем потоке: держит источник данных и выполняет поиск."""

    ready = Signal(str)                     # описание источника для шапки
    connectionFailed = Signal(str, str)     # понятное сообщение, подробности
    resultReady = Signal(object)            # LookupResult
    lookupFailed = Signal(str, str)
    busyChanged = Signal(bool)
    snapshotRefreshed = Signal(bool, str)   # обновилась ли база, сообщение

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self._client: OneCClient | None = None
        self._snapshot: Snapshot | None = None

    # --- запуск и переключение источника ---------------------------------
    @Slot()
    def startUp(self) -> None:
        if self.settings.uses_local_db:
            self.openLocal(pull_first=True)
        elif self.settings.reconnect_on_start:
            self.reconnect()

    @Slot()
    def reload(self) -> None:
        """Переоткрыть текущий источник (после смены настроек)."""
        self._release()
        self.startUp()

    def _release(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._snapshot is not None:
            self._snapshot.close()
            self._snapshot = None

    # --- локальная база ---------------------------------------------------
    @Slot(bool)
    def openLocal(self, pull_first: bool = False) -> None:
        """Открыть локальную базу, при необходимости скачав свежую из источника."""
        self.busyChanged.emit(True)
        pull_error = ""
        try:
            if pull_first and self._have_source():
                try:
                    self._pull()
                except SnapshotError as e:
                    # Источник недоступен — это не повод не работать: копия с
                    # прошлого раза остаётся годной. Но причину запоминаем: если
                    # копии нет вовсе, показать надо именно её.
                    pull_error = str(e)
                    log.warning("Обновление базы не удалось: %s", e)
            if self._snapshot is not None:
                self._snapshot.close()
            self._snapshot = Snapshot(self.settings.local_db_path())
            self._snapshot.open()
            self.ready.emit(self._snapshot.age_text())
        except SnapshotError as e:
            self._snapshot = None
            # «Файла нет» — следствие неудачной загрузки, а не самостоятельная
            # причина. Показываем то, что человек может исправить.
            message = (f"База ещё не скачана. {pull_error}" if pull_error else str(e))
            log.warning("Локальная база недоступна: %s", message)
            self.connectionFailed.emit(message, pull_error)
        finally:
            self.busyChanged.emit(False)

    def _pull(self) -> bool:
        return pull(self.settings, self.settings.local_db_path())

    def _have_source(self) -> bool:
        s = self.settings
        return bool(s.yadisk_token) if s.uses_yadisk else bool(s.snapshot_share)

    @Slot()
    def refreshSnapshot(self) -> None:
        """Проверить источник и переоткрыть базу, если файл обновился."""
        if not self._have_source():
            self.snapshotRefreshed.emit(
                False, "Источник данных не указан — обновлять неоткуда.")
            return
        self.busyChanged.emit(True)
        try:
            updated = self._pull()
        except SnapshotError as e:
            log.warning("Обновление не удалось: %s", e)
            self.snapshotRefreshed.emit(False, str(e))
            return
        finally:
            self.busyChanged.emit(False)

        if not updated:
            age = self._snapshot.age_text() if self._snapshot else ""
            self.snapshotRefreshed.emit(False, f"База уже свежая — {age}")
            return
        self.openLocal(pull_first=False)
        self.snapshotRefreshed.emit(
            True, f"База обновлена — {self._snapshot.age_text()}"
            if self._snapshot else "База обновлена")

    # --- прямое подключение к 1С -----------------------------------------
    @Slot()
    def reconnect(self) -> None:
        if not self.settings.is_configured():
            self.connectionFailed.emit(
                "База 1С не настроена. Откройте «Настройки» и укажите подключение.", "")
            return
        self.busyChanged.emit(True)
        try:
            if self._client is None:
                self._client = OneCClient(self.settings)
            self._client.settings = self.settings
            self._client._drop()          # сбрасываем прежнее соединение, если было
            self._client.ping()
            self.ready.emit(self.settings.describe_base())
        except Exception as e:  # noqa: BLE001 - любую ошибку показываем человеку
            detail = error_text(e)
            log.warning("Подключение к 1С не удалось: %s", detail)
            self.connectionFailed.emit(describe_error(e), detail)
        finally:
            self.busyChanged.emit(False)

    # --- поиск ------------------------------------------------------------
    @Slot(str)
    def lookup(self, barcode: str) -> None:
        """Поиск по отсканированному штрихкоду (с запасным поиском по артикулу)."""
        self._run_lookup(
            barcode, "ШК",
            lambda: self._snapshot.lookup(barcode, self.settings.use_fallback,
                                          self.settings.warehouses),
            lambda: self._client.lookup(barcode))

    @Slot(str)
    def lookupArticle(self, article: str) -> None:
        """Поиск строго по артикулу — из поля ручного ввода."""
        self._run_lookup(
            article, "артикулу",
            lambda: self._snapshot.lookup_article(article,
                                                  self.settings.warehouses),
            lambda: self._client.lookup_article(article))

    def _run_lookup(self, value: str, what: str, from_snapshot, from_1c) -> None:
        """Общая обвязка обоих поисков: источник, занятость, разбор ошибок."""
        self.busyChanged.emit(True)
        try:
            if self.settings.uses_local_db:
                if self._snapshot is None:
                    raise SnapshotError(
                        "Локальная база не открыта. Нажмите «Обновить базу».")
                result = from_snapshot()
            else:
                if self._client is None:
                    self._client = OneCClient(self.settings)
                result = from_1c()
            self.resultReady.emit(result)
        except SnapshotError as e:
            log.warning("Поиск по %s %s не удался: %s", what, value, e)
            self.lookupFailed.emit(str(e), "")
        except Exception as e:  # noqa: BLE001
            detail = error_text(e)
            log.warning("Поиск по %s %s не удался: %s", what, value, detail)
            self.lookupFailed.emit(describe_error(e), detail)
        finally:
            self.busyChanged.emit(False)

    @Slot(object)
    def applySettings(self, settings: Settings) -> None:
        """Принять новые настройки и переоткрыть источник."""
        self.settings = settings
        self.reload()

    @Slot()
    def shutDown(self) -> None:
        """Закрыть COM в том же потоке, где он был инициализирован."""
        self._release()


class TestConnectionTask(QThread):
    """Разовая проверка подключения к 1С для диалога настроек.

    Отдельный поток со своим CoInitialize: проверяем кандидатские настройки, не
    трогая рабочий источник сканирования.
    """

    finishedWith = Signal(bool, str, str)   # успех, сообщение, подробности

    def __init__(self, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

    def run(self) -> None:  # выполняется в новом потоке
        client = OneCClient(self._settings)
        try:
            client.ping()
            self.finishedWith.emit(True, "Соединение установлено.", "")
        except Exception as e:  # noqa: BLE001
            self.finishedWith.emit(False, describe_error(e), error_text(e))
        finally:
            client.close()
