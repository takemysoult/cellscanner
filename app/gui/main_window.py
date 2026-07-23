"""Главное окно: поле сканера, результат из 1С и мгновенная печать стикера.

Сценарий на складе: оператор пикает штрихкод -> приложение спрашивает 1С ->
этикетка сразу уходит на принтер. Ни одного клика в обычном ходе работы, поэтому:

  * поле ввода всегда в фокусе — сканер-клавиатура печатает прямо в него, а
    завершающий Enter запускает поиск;
  * принтер закреплён за приложением (имя в настройках), диалог печати не
    показывается;
  * ошибки озвучиваются звуком и крупной цветной плашкой — оператор смотрит на
    товар, а не в экран.
"""
from __future__ import annotations

import logging
import time

from PySide2.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide2.QtGui import QDesktopServices, QFont, QKeySequence
from PySide2.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPushButton, QShortcut, QVBoxLayout,
                               QWidget)

from .. import diagnostics, paths, printing
from ..config import Settings
from ..label import LabelData, LabelStyle, render_pixmap
from ..onec import LookupResult, Placement
from ..query import normalize_barcode
from .settings_dialog import SettingsDialog
from .worker import LookupWorker

log = logging.getLogger(__name__)

OK, WARN, ERR, MUTED = "#1b7f3b", "#b25e00", "#b3261e", "#5f6368"

# Тот же код, пришедший повторно за это время, считаем дребезгом сканера.
DOUBLE_SCAN_S = 1.2

STYLE = """
QWidget { background: #f5f6f8; color: #1b1c1e; font-family: 'Segoe UI'; }
QFrame#card { background: #ffffff; border: 1px solid #dfe1e5; border-radius: 10px; }
QLineEdit#scan {
    font-size: 26pt; font-weight: 600; padding: 10px 14px;
    border: 2px solid #c7c9cd; border-radius: 10px; background: #ffffff;
}
QLineEdit#scan:focus { border-color: #1a73e8; }
QLineEdit#article {
    font-size: 15pt; padding: 7px 14px;
    border: 1px solid #c7c9cd; border-radius: 8px; background: #ffffff;
}
QLineEdit#article:focus { border-color: #1a73e8; }
QPushButton {
    background: #ffffff; border: 1px solid #c7c9cd; border-radius: 8px;
    padding: 10px 18px; font-size: 12pt;
}
QPushButton:hover { background: #eef1f6; }
QPushButton#primary {
    background: #1a73e8; border-color: #1a73e8; color: #ffffff; font-weight: 600;
}
QPushButton#primary:hover { background: #1765cc; }
QListWidget {
    background: #ffffff; border: 1px solid #dfe1e5; border-radius: 8px;
    font-size: 14pt;
}
QListWidget::item { padding: 8px 10px; }
QListWidget::item:selected { background: #e8f0fe; color: #1b1c1e; }
"""


class MainWindow(QMainWindow):
    lookupRequested = Signal(str)
    articleRequested = Signal(str)
    settingsApplied = Signal(object)
    reconnectRequested = Signal()
    refreshRequested = Signal()
    shutdownRequested = Signal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self._result: LookupResult | None = None
        self._busy = False
        self._last_barcode = ""
        self._last_scan_at = 0.0
        self._last_detail = ""
        self._manual = False   # последний поиск — ручной ввод артикула

        self.setWindowTitle("Ячейка по штрихкоду")
        self.setMinimumSize(980, 660)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._start_worker()
        self._refresh_printer_label()
        QTimer.singleShot(0, self._focus_scan)

    # --- построение интерфейса -------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(12)
        self.setCentralWidget(root)

        # Шапка: состояние связи с 1С + принтер + настройки
        header = QHBoxLayout()
        self.conn_label = QLabel("● Подключаюсь к 1С…")
        self.conn_label.setStyleSheet(f"font-size: 11pt; color: {MUTED};")
        self.printer_label = QLabel()
        self.printer_label.setStyleSheet(f"font-size: 11pt; color: {MUTED};")
        self.btn_reload = QPushButton()      # надпись зависит от источника данных
        self.btn_reload.clicked.connect(self._on_reload)
        btn_settings = QPushButton("Настройки")
        btn_settings.clicked.connect(self.open_settings)
        header.addWidget(self.conn_label)
        header.addStretch(1)
        header.addWidget(self.printer_label)
        header.addWidget(self.btn_reload)
        header.addWidget(btn_settings)
        outer.addLayout(header)

        # Поле сканера
        from PySide2.QtWidgets import QLineEdit
        self.scan_input = QLineEdit()
        self.scan_input.setObjectName("scan")
        self.scan_input.setPlaceholderText("Отсканируйте штрихкод…")
        self.scan_input.returnPressed.connect(self._on_scan)
        outer.addWidget(self.scan_input)

        # Ручной ввод артикула: когда штрихкода нет или он не читается.
        # Поле мельче — основной путь всё-таки сканер.
        self.article_input = QLineEdit()
        self.article_input.setObjectName("article")
        self.article_input.setPlaceholderText(
            "…или введите артикул вручную и нажмите Enter")
        self.article_input.returnPressed.connect(self._on_article)
        outer.addWidget(self.article_input)

        # Тело: слева результат, справа превью стикера
        body = QHBoxLayout()
        body.setSpacing(14)
        outer.addLayout(body, 1)

        left = QFrame()
        left.setObjectName("card")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(18, 16, 18, 16)
        lv.setSpacing(8)

        self.headline = QLabel("Готов к работе")
        self.headline.setStyleSheet(f"font-size: 20pt; font-weight: 700; color: {MUTED};")
        self.headline.setWordWrap(True)

        self.cell_label = QLabel("")
        f = QFont("Segoe UI", 44)
        f.setBold(True)
        self.cell_label.setFont(f)

        self.article_label = QLabel("")
        self.article_label.setStyleSheet("font-size: 17pt; font-weight: 600;")
        self.name_label = QLabel("")
        self.name_label.setStyleSheet(f"font-size: 12pt; color: {MUTED};")
        self.name_label.setWordWrap(True)

        self.places_hint = QLabel("Товар размещён в нескольких ячейках — выберите нужную:")
        self.places_hint.setStyleSheet(f"font-size: 11pt; color: {WARN};")
        self.places = QListWidget()
        self.places.currentRowChanged.connect(self._on_place_changed)
        self.places_hint.hide()
        self.places.hide()

        lv.addWidget(self.headline)
        lv.addWidget(self.cell_label)
        lv.addWidget(self.article_label)
        lv.addWidget(self.name_label)
        lv.addWidget(self.places_hint)
        lv.addWidget(self.places, 1)
        lv.addStretch(0)
        body.addWidget(left, 1)

        right = QFrame()
        right.setObjectName("card")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(16, 16, 16, 16)
        rv.setSpacing(8)
        cap = QLabel(f"Стикер {self.settings.label_w_mm:g}×{self.settings.label_h_mm:g} мм")
        cap.setStyleSheet(f"font-size: 11pt; color: {MUTED};")
        cap.setAlignment(Qt.AlignHCenter)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet(
            "background: #ffffff; border: 1px solid #9aa0a6; border-radius: 2px;")
        rv.addWidget(cap)
        rv.addWidget(self.preview, 0, Qt.AlignHCenter | Qt.AlignTop)
        rv.addStretch(1)
        self.caption = cap
        body.addWidget(right, 0)

        # Подвал: ручные действия и строка состояния
        footer = QHBoxLayout()
        self.btn_print = QPushButton("Печать  (F2)")
        self.btn_print.setObjectName("primary")
        self.btn_print.clicked.connect(lambda: self._print(auto=False))
        self.btn_pdf = QPushButton("Открыть PDF  (F3)")
        self.btn_pdf.clicked.connect(self._open_pdf)
        self.btn_print.setEnabled(False)
        self.btn_pdf.setEnabled(False)
        # Появляется только при ошибке: на складском ПК журнал открывать некому,
        # причина должна быть доступна прямо из окна.
        self.btn_details = QPushButton("Подробности")
        self.btn_details.clicked.connect(self._show_details)
        self.btn_details.hide()
        self.status = QLabel("")
        self.status.setStyleSheet("font-size: 12pt;")
        footer.addWidget(self.btn_print)
        footer.addWidget(self.btn_pdf)
        footer.addWidget(self.btn_details)
        footer.addSpacing(12)
        footer.addWidget(self.status, 1)
        outer.addLayout(footer)

        QShortcut(QKeySequence("F2"), self, activated=lambda: self._print(auto=False))
        QShortcut(QKeySequence("F3"), self, activated=self._open_pdf)
        QShortcut(QKeySequence("F5"), self, activated=self._on_reload)
        QShortcut(QKeySequence("Escape"), self, activated=self._focus_scanner_field)

    # --- рабочий поток ----------------------------------------------------
    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = LookupWorker(self.settings)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.startUp)
        self.lookupRequested.connect(self._worker.lookup)
        self.articleRequested.connect(self._worker.lookupArticle)
        self.settingsApplied.connect(self._worker.applySettings)
        self.reconnectRequested.connect(self._worker.reconnect)
        self.refreshRequested.connect(self._worker.refreshSnapshot)
        self.shutdownRequested.connect(self._worker.shutDown)

        self._worker.ready.connect(self._on_ready)
        self._worker.connectionFailed.connect(self._on_connection_failed)
        self._worker.resultReady.connect(self._on_result)
        self._worker.lookupFailed.connect(self._on_lookup_failed)
        self._worker.busyChanged.connect(self._on_busy)
        self._worker.snapshotRefreshed.connect(self._on_snapshot_refreshed)
        self._thread.start()

        # Общую папку проверяем по таймеру: выгрузка ночная, но если её сделали
        # днём вручную, склад получит свежие ячейки без перезапуска приложения.
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self.refreshRequested)
        self._apply_source_ui()

    def closeEvent(self, event) -> None:
        # COM закрывается тем же потоком, который его открыл.
        self.shutdownRequested.emit()
        # Поток мог не запуститься (сбой при старте) — закрытие окна не должно
        # из-за этого падать: пользователю нужно просто выйти.
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.quit()
            thread.wait(5000)
        super().closeEvent(event)

    # --- сканирование -----------------------------------------------------
    def _focus_scan(self) -> None:
        """Вернуть курсор туда, откуда пришёл последний поиск.

        После ручного ввода артикула фокус остаётся в его поле: обычно за одним
        набранным артикулом следует другой. Esc всегда возвращает к сканеру.
        """
        target = self.article_input if self._manual else self.scan_input
        target.setFocus()
        target.selectAll()

    def _focus_scanner_field(self) -> None:
        self._manual = False
        self.scan_input.setFocus()
        self.scan_input.selectAll()

    def _on_scan(self) -> None:
        barcode = normalize_barcode(self.scan_input.text())
        self.scan_input.clear()
        if not barcode:
            return
        # Сканеры иногда срабатывают дважды за один пик. При мгновенной печати это
        # лишняя этикетка, поэтому повтор того же кода подряд гасим; осознанный
        # повтор всегда доступен по F2.
        now = time.monotonic()
        if barcode == self._last_barcode and now - self._last_scan_at < DOUBLE_SCAN_S:
            self._set_status(
                f"Повтор {barcode} пропущен (двойной пик). Ещё одна этикетка — F2",
                WARN)
            return
        self._last_barcode, self._last_scan_at = barcode, now
        self._manual = False
        self.btn_details.hide()      # подробности прошлой ошибки к новому коду не относятся
        self._set_status(f"Ищу {barcode}…", MUTED)
        self.headline.setText("Поиск в 1С…")
        self.headline.setStyleSheet(f"font-size: 20pt; font-weight: 700; color: {MUTED};")
        self.lookupRequested.emit(barcode)

    def _on_article(self) -> None:
        """Ручной ввод артикула: ищем строго по артикулу, дребезг не гасим.

        Повтор здесь — осознанное действие человека (нужна ещё одна этикетка),
        в отличие от двойного срабатывания сканера.
        """
        article = normalize_barcode(self.article_input.text())
        if not article:
            return
        self.article_input.selectAll()
        self._last_barcode = ""          # ручной ввод не мешает защите от дребезга
        self.btn_details.hide()
        self._set_status(f"Ищу артикул {article}…", MUTED)
        self.headline.setText("Поиск по артикулу…")
        self.headline.setStyleSheet(f"font-size: 20pt; font-weight: 700; color: {MUTED};")
        self._manual = True
        self.articleRequested.emit(article)

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        # Поле остаётся активным: сканер может пикнуть следующий товар, запросы
        # выполнятся по очереди.

    # --- результат --------------------------------------------------------
    def _on_result(self, res: LookupResult) -> None:
        self._result = res
        self.places.blockSignals(True)
        self.places.clear()
        self.places.blockSignals(False)

        if not res.found:
            where = ("в базе (проверьте, свежая ли выгрузка)"
                     if self.settings.uses_local_db else "в 1С")
            what = "Артикула" if res.by_article else "Штрихкода"
            self._show_failure("НЕ НАЙДЕНО",
                               f"{what} {res.barcode} нет {where}", ERR)
            return
        if not res.placements:
            self.article_label.setText(res.article or "—")
            self.name_label.setText(res.name)
            self._show_failure("НЕТ РАЗМЕЩЕНИЯ",
                               "Товар найден, но ячейка для него не задана", WARN,
                               keep_item=True)
            return

        title = "НАЙДЕНО ПО АРТИКУЛУ" if res.by_article else "НАЙДЕНО"
        self.headline.setText(title)
        self.headline.setStyleSheet(f"font-size: 20pt; font-weight: 700; color: {OK};")
        self.article_label.setText(res.article or "—")
        self.name_label.setText(res.name)

        many = len(res.placements) > 1
        # Пометка «основная» полезна, только если она что-то различает: в базе
        # признак стоит почти у всех строк, и звёздочка на каждой — просто шум.
        mark_main = any(p.is_main for p in res.placements) and \
            not all(p.is_main for p in res.placements)
        for i, p in enumerate(res.placements, 1):
            star = " ★ основная" if (mark_main and p.is_main) else ""
            wh = f"   {p.warehouse}" if p.warehouse else ""
            self.places.addItem(QListWidgetItem(f"{i}.  {p.cell}{wh}{star}"))
        self.places.setVisible(many)
        self.places_hint.setVisible(many)
        self.places.setCurrentRow(0)      # основная идёт первой (сортировка в onec)

        self.btn_print.setEnabled(True)
        self.btn_pdf.setEnabled(True)

        if self.settings.auto_print and (not many or self.settings.auto_print_when_many):
            self._print(auto=True, alternatives=len(res.placements) - 1)
        elif many:
            self._set_status("Несколько ячеек — выберите строку и нажмите «Печать» (F2)",
                             WARN)
            QApplication.beep()
        else:
            p = res.placements[0]
            self._set_status(f"Ячейка {p.cell} — нажмите «Печать» (F2)", OK)
        self._focus_scan()

    def _show_failure(self, title: str, message: str, color: str,
                      keep_item: bool = False) -> None:
        self.headline.setText(title)
        self.headline.setStyleSheet(f"font-size: 20pt; font-weight: 700; color: {color};")
        self.cell_label.setText("")
        if not keep_item:
            self.article_label.setText("")
            self.name_label.setText("")
        self.places.hide()
        self.places_hint.hide()
        self.preview.clear()
        self.btn_print.setEnabled(False)
        self.btn_pdf.setEnabled(False)
        self._set_status(message, color)
        QApplication.beep()
        self._focus_scan()

    def _on_lookup_failed(self, message: str, detail: str) -> None:
        log.warning("Поиск не удался: %s | %s", message, detail)
        self._remember_detail(detail)
        self._show_failure("ОШИБКА 1С", message, ERR)

    def _remember_detail(self, detail: str) -> None:
        self._last_detail = detail
        self.btn_details.setVisible(bool(detail))

    def _show_details(self) -> None:
        """Полный текст ошибки 1С + состояние окружения этой машины."""
        box = QMessageBox(self)
        box.setWindowTitle("Подробности")
        box.setIcon(QMessageBox.Information)
        box.setText("Что именно не сработало")
        box.setInformativeText(self._last_detail or "Текст ошибки недоступен.")
        box.setDetailedText(diagnostics.report(self.settings.progid))
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)  # чтобы можно было скопировать
        box.exec_()

    def _on_place_changed(self, row: int) -> None:
        self._update_preview()

    # --- текущая этикетка -------------------------------------------------
    def _current_placement(self) -> Placement | None:
        if not self._result or not self._result.placements:
            return None
        row = max(0, self.places.currentRow())
        if row >= len(self._result.placements):
            row = 0
        return self._result.placements[row]

    def _current_label(self) -> LabelData | None:
        p = self._current_placement()
        if p is None or self._result is None:
            return None
        return LabelData(cell=p.cell, article=self._result.article,
                         name=self._result.name, warehouse=p.warehouse)

    def _update_preview(self) -> None:
        data = self._current_label()
        if data is None:
            self.preview.clear()
            self.cell_label.setText("")
            return
        self.cell_label.setText(data.cell)
        pm = render_pixmap(data, LabelStyle.from_settings(self.settings),
                           self.settings, target_px_height=380)
        self.preview.setPixmap(pm)
        self.preview.setFixedSize(pm.size())  # рамка обнимает стикер, а не пустоту

    # --- печать -----------------------------------------------------------
    def _print(self, auto: bool, alternatives: int = 0) -> None:
        self._update_preview()
        data = self._current_label()
        if data is None:
            self._set_status("Нечего печатать", WARN)
            return
        try:
            name = printing.print_label(data, self.settings)
        except Exception as e:  # noqa: BLE001 - принтер могли выключить/отсоединить
            log.warning("Печать не удалась: %s", e)
            self._set_status(f"Печать не удалась: {e}", ERR)
            QApplication.beep()
            if not auto:
                QMessageBox.warning(self, "Печать", str(e))
            return
        suffix = "" if self.settings.copies == 1 else f" ×{self.settings.copies}"
        if alternatives:
            # Напечатали первую из нескольких — оператор должен об этом знать,
            # чтобы при необходимости выбрать другую строку и повторить (F2).
            self._set_status(
                f"Напечатано{suffix}: {data.cell}  ({name}) — есть ещё "
                f"{alternatives} ячейк(и): выберите и F2", WARN)
            QApplication.beep()
        else:
            self._set_status(f"Напечатано{suffix}: {data.cell}  ({name})", OK)
        self._focus_scan()

    def _open_pdf(self) -> None:
        self._update_preview()
        data = self._current_label()
        if data is None:
            self._set_status("Нечего сохранять", WARN)
            return
        paths.ensure_dirs()
        stem = printing.safe_file_stem(data.cell, data.article)
        path = paths.pdf_dir() / f"{stem}.pdf"
        try:
            printing.save_pdf(data, self.settings, path)
        except Exception as e:  # noqa: BLE001
            log.warning("PDF не сохранён: %s", e)
            self._set_status(f"PDF не сохранён: {e}", ERR)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self._set_status(f"Открыт PDF: {path.name}", OK)
        self._focus_scan()

    # --- источник данных и настройки --------------------------------------
    def _apply_source_ui(self) -> None:
        """Подогнать шапку и таймер под выбранный источник."""
        local = self.settings.uses_local_db
        self.btn_reload.setText("Обновить базу" if local else "Переподключить")
        self.conn_label.setText("● Открываю базу…" if local else "● Подключаюсь к 1С…")
        self.conn_label.setStyleSheet(f"font-size: 11pt; color: {MUTED};")
        self._auto_timer.stop()
        minutes = self.settings.snapshot_auto_minutes
        has_source = (self.settings.yadisk_token if self.settings.uses_yadisk
                      else self.settings.snapshot_share)
        if local and has_source and minutes > 0:
            self._auto_timer.start(minutes * 60_000)

    def _on_ready(self, description: str) -> None:
        prefix = "База" if self.settings.uses_local_db else "1С"
        self.conn_label.setText(f"● {prefix}: {description}")
        self.conn_label.setStyleSheet(f"font-size: 11pt; color: {OK};")
        self._set_status("Готово. Сканируйте штрихкод.", MUTED)
        self._focus_scan()

    def _on_connection_failed(self, message: str, detail: str) -> None:
        log.warning("Источник данных недоступен: %s | %s", message, detail)
        if not self.settings.uses_local_db:
            log.warning("Диагностика окружения:\n%s",
                        diagnostics.report(self.settings.progid))
        self.conn_label.setText("● База недоступна" if self.settings.uses_local_db
                                else "● 1С: нет связи")
        self.conn_label.setStyleSheet(f"font-size: 11pt; color: {ERR};")
        self._remember_detail(detail or message)
        self._set_status(message, ERR)

    def _on_snapshot_refreshed(self, updated: bool, message: str) -> None:
        self._set_status(message, OK if updated else MUTED)
        self._focus_scan()

    def _on_reload(self) -> None:
        if self.settings.uses_local_db:
            self._set_status("Проверяю общую папку…", MUTED)
            self.refreshRequested.emit()
        else:
            self.conn_label.setText("● Подключаюсь к 1С…")
            self.conn_label.setStyleSheet(f"font-size: 11pt; color: {MUTED};")
            self.reconnectRequested.emit()

    def _refresh_printer_label(self) -> None:
        name = self.settings.printer_name or printing.default_printer_name()
        if printing.printer_available(self.settings):
            self.printer_label.setText(f"🖨 {name}")
            self.printer_label.setStyleSheet(f"font-size: 11pt; color: {MUTED};")
        else:
            self.printer_label.setText("🖨 принтер не найден")
            self.printer_label.setStyleSheet(f"font-size: 11pt; color: {ERR};")

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec_():
            self.settings = dlg.result_settings()
            self.settings.save()
            self.caption.setText(
                f"Стикер {self.settings.label_w_mm:g}×{self.settings.label_h_mm:g} мм")
            self._refresh_printer_label()
            self._update_preview()
            self._apply_source_ui()
            self.settingsApplied.emit(self.settings)
        self._focus_scan()

    def _set_status(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"font-size: 12pt; color: {color};")
