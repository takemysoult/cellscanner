"""Диалог настроек: подключение к 1С, принтер и этикетка, тексты запросов.

Проверка связи выполняется в отдельном потоке (COM апартаментный — своему потоку
свой CoInitialize), поэтому окно не «висит», пока 1С отвечает.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from PySide2.QtCore import Qt
from PySide2.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QRadioButton,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from .. import printing
from ..config import Settings, list_known_bases, parse_connect_string
from ..label import LabelData
from ..query import DEFAULT_FALLBACK_QUERY, DEFAULT_QUERY
from ..yadisk import normalize_token
from ..snapshot import Snapshot, SnapshotError
from .worker import TestConnectionTask

log = logging.getLogger(__name__)

DEFAULT_PRINTER_ITEM = "— принтер Windows по умолчанию —"


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._test: TestConnectionTask | None = None
        self.setWindowTitle("Настройки")
        self.setMinimumSize(760, 620)

        tabs = QTabWidget()
        tabs.addTab(self._build_source_tab(), "Источник данных")
        tabs.addTab(self._build_connection_tab(), "1С")
        tabs.addTab(self._build_print_tab(), "Печать")
        tabs.addTab(self._build_query_tab(), "Запросы")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        root.addWidget(buttons)
        self._sync_kind()
        self._sync_transport()

    # --- вкладка «Источник данных» ----------------------------------------
    def _build_source_tab(self) -> QWidget:
        s = self._settings
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "Откуда приложение берёт ячейку.\n\n"
            "На рабочем месте со сканером стоит тонкий клиент 1С, а в нём нет "
            "COM-коннектора — напрямую к 1С оттуда не подключиться. Поэтому машина "
            "с полной платформой раз в сутки выгружает данные в файл, а склад "
            "читает только файл.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Две независимые пары переключателей. Без явных QButtonGroup Qt считает
        # исключающими ВСЕ радиокнопки одного родителя — выбор транспорта сбрасывал
        # бы выбор источника.
        self.rb_local = QRadioButton("Локальная база (файл выгрузки) — для склада")
        self.rb_com = QRadioButton("Напрямую из 1С (COM) — где есть полная платформа")
        self.grp_source = QButtonGroup(self)
        self.grp_source.addButton(self.rb_local)
        self.grp_source.addButton(self.rb_com)
        (self.rb_local if s.uses_local_db else self.rb_com).setChecked(True)
        layout.addWidget(self.rb_local)
        layout.addWidget(self.rb_com)

        # --- чем доставляем файл между машинами ---
        layout.addSpacing(8)
        transport_row = QHBoxLayout()
        transport_row.addWidget(QLabel("Файл передаётся через:"))
        self.rb_tr_file = QRadioButton("общую папку")
        self.rb_tr_yadisk = QRadioButton("Яндекс.Диск")
        self.grp_transport = QButtonGroup(self)
        self.grp_transport.addButton(self.rb_tr_file)
        self.grp_transport.addButton(self.rb_tr_yadisk)
        (self.rb_tr_yadisk if s.uses_yadisk else self.rb_tr_file).setChecked(True)
        self.rb_tr_yadisk.toggled.connect(self._sync_transport)
        transport_row.addWidget(self.rb_tr_file)
        transport_row.addWidget(self.rb_tr_yadisk)
        transport_row.addStretch(1)
        layout.addLayout(transport_row)

        form = QFormLayout()
        self.ed_share = QLineEdit(s.snapshot_share)
        self.ed_share.setPlaceholderText(r"C:\CellScanner\cells.db")
        btn_pick = QPushButton("Обзор…")
        btn_pick.clicked.connect(self._on_pick_share)
        row = QHBoxLayout()
        row.addWidget(self.ed_share, 1)
        row.addWidget(btn_pick)
        box = QWidget()
        box.setLayout(row)
        self.row_share = box
        form.addRow("Файл (папка)", box)

        self.ed_token = QLineEdit(s.yadisk_token)
        self.ed_token.setEchoMode(QLineEdit.Password)
        self.ed_token.setPlaceholderText("вставьте OAuth-токен Яндекс.Диска")
        btn_token_help = QPushButton("Как получить?")
        btn_token_help.clicked.connect(self._on_token_help)
        trow = QHBoxLayout()
        trow.addWidget(self.ed_token, 1)
        trow.addWidget(btn_token_help)
        tbox = QWidget()
        tbox.setLayout(trow)
        self.row_token = tbox
        form.addRow("Токен Яндекс.Диска", tbox)

        self.ed_yapath = QLineEdit(s.yadisk_path)
        self.ed_yapath.setPlaceholderText("app:/cells.db")
        form.addRow("Путь на Диске", self.ed_yapath)

        self.btn_check_ya = QPushButton("Проверить Яндекс.Диск")
        self.btn_check_ya.clicked.connect(self._on_check_yadisk)
        self.lbl_ya = QLabel("")
        self.lbl_ya.setWordWrap(True)
        form.addRow("", self.btn_check_ya)
        form.addRow("", self.lbl_ya)

        self.sp_auto = QSpinBox()
        self.sp_auto.setRange(0, 24 * 60)
        self.sp_auto.setSuffix(" мин")
        self.sp_auto.setSpecialValueText("не проверять")
        self.sp_auto.setValue(s.snapshot_auto_minutes)
        form.addRow("Проверять обновление каждые", self.sp_auto)
        layout.addLayout(form)
        self.form_source = form

        self.lbl_local = QLabel()
        self.lbl_local.setWordWrap(True)
        self._describe_local_db()
        layout.addWidget(self.lbl_local)

        hint = QLabel(
            "Выгрузку делает утилита tools\\export_cells.py на машине с платформой 1С — "
            "поставьте её в «Планировщик заданий» на ночь (см. README).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5f6368;")
        layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _describe_local_db(self) -> None:
        """Показать, что за файл сейчас лежит локально и насколько он свежий."""
        path = self._settings.local_db_path()
        snap = Snapshot(path)
        try:
            snap.open()
            meta = snap.meta()
            self.lbl_local.setText(
                f"Локальная копия: {path}\n{snap.age_text()}; "
                f"штрихкодов: {meta.get('barcodes', '?')}, "
                f"размещений: {meta.get('barcode_cells', '?')}")
            self.lbl_local.setStyleSheet("color: #1b7f3b;")
        except SnapshotError as e:
            self.lbl_local.setText(f"Локальной базы пока нет: {e}")
            self.lbl_local.setStyleSheet("color: #b25e00;")
        finally:
            snap.close()

    def _on_pick_share(self) -> None:
        start = self.ed_share.text().strip() or ""
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл выгрузки", start, "База выгрузки (*.db);;Все файлы (*)")
        if chosen:
            self.ed_share.setText(chosen)

    def _sync_transport(self) -> None:
        """Показывать только поля выбранного транспорта — лишнее не путает."""
        yadisk = self.rb_tr_yadisk.isChecked()
        for widget, visible in ((self.row_share, not yadisk),
                                (self.row_token, yadisk),
                                (self.ed_yapath, yadisk),
                                (self.btn_check_ya, yadisk),
                                (self.lbl_ya, yadisk)):
            widget.setVisible(visible)
            label = self.form_source.labelForField(widget)
            if label is not None:
                label.setVisible(visible)

    def _on_token_help(self) -> None:
        QMessageBox.information(
            self, "Токен Яндекс.Диска",
            "Токен выдаёт Яндекс — приложение его только хранит (в зашифрованном "
            "виде, доступном лишь этой учётной записи Windows).\n\n"
            "Как получить:\n"
            "1. Откройте https://oauth.yandex.ru и нажмите «Создать приложение».\n"
            "2. Платформа — «Веб-сервисы», Redirect URI — "
            "https://oauth.yandex.ru/verification_codes\n"
            "3. Доступы: «Яндекс.Диск REST API» → «Доступ к папке приложения» "
            "(этого достаточно, доступ ко всему диску выдавать не нужно).\n"
            "4. Создайте приложение, скопируйте его ClientID и откройте ссылку:\n"
            "   https://oauth.yandex.ru/authorize?response_type=token&client_id=ВАШ_CLIENT_ID\n"
            "5. Подтвердите доступ — Яндекс покажет токен. Вставьте его в это поле.\n\n"
            "Тот же токен нужно ввести на второй машине.")

    def _on_check_yadisk(self) -> None:
        """Проверить токен: показать, что диск отвечает и файл на месте."""
        from ..yadisk import YaDisk, YaDiskError, normalize_token

        # Из поля часто прилетает кусок адресной строки — вычищаем и показываем
        # результат, чтобы человек видел, что именно сохранится.
        token = normalize_token(self.ed_token.text())
        if token != self.ed_token.text().strip():
            self.ed_token.setText(token)
        remote = self.ed_yapath.text().strip() or "app:/cells.db"
        if not token:
            self.lbl_ya.setText("Сначала вставьте токен.")
            self.lbl_ya.setStyleSheet("color: #b25e00;")
            return
        self.btn_check_ya.setEnabled(False)
        self.lbl_ya.setText("Проверяю…")
        self.lbl_ya.setStyleSheet("color: #5f6368;")
        QApplication.processEvents()
        try:
            client = YaDisk(token)
            info = client.check(remote)
            stamp = client.modified_at(remote)
            where = f"файл есть, изменён {stamp}" if stamp else "файла ещё нет"
            self.lbl_ya.setText(f"Яндекс.Диск отвечает: {info}. {remote} — {where}.")
            self.lbl_ya.setStyleSheet("color: #1b7f3b;")
        except YaDiskError as e:
            log.warning("Проверка Яндекс.Диска не удалась: %s", e)
            self.lbl_ya.setText(str(e))
            self.lbl_ya.setStyleSheet("color: #b3261e;")
        finally:
            self.btn_check_ya.setEnabled(True)

    # --- вкладка «1С» -----------------------------------------------------
    def _build_connection_tab(self) -> QWidget:
        s = self._settings
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignRight)

        self.known = QComboBox()
        self.known.addItem("— выбрать из списка баз 1С —", "")
        for name, connect in list_known_bases():
            self.known.addItem(name, connect)
        self.known.currentIndexChanged.connect(self._on_known_base)
        form.addRow("Известные базы", self.known)

        self.rb_server = QRadioButton("Серверная")
        self.rb_file = QRadioButton("Файловая")
        self.grp_kind = QButtonGroup(self)
        self.grp_kind.addButton(self.rb_server)
        self.grp_kind.addButton(self.rb_file)
        (self.rb_file if s.kind == "file" else self.rb_server).setChecked(True)
        self.rb_server.toggled.connect(self._sync_kind)
        kind_row = QHBoxLayout()
        kind_row.addWidget(self.rb_server)
        kind_row.addWidget(self.rb_file)
        kind_row.addStretch(1)
        kind_box = QWidget()
        kind_box.setLayout(kind_row)
        form.addRow("Тип базы", kind_box)

        self.ed_srvr = QLineEdit(s.srvr)
        self.ed_srvr.setPlaceholderText("например, Serv1C")
        self.ed_ref = QLineEdit(s.ref)
        self.ed_ref.setPlaceholderText("например, ut2025")
        self.ed_file = QLineEdit(s.file_path)
        self.ed_file.setPlaceholderText(r"например, C:\Bases\ut")
        self.ed_progid = QLineEdit(s.progid)
        self.ed_usr = QLineEdit(s.usr)
        self.ed_pwd = QLineEdit(s.password)
        self.ed_pwd.setEchoMode(QLineEdit.Password)
        form.addRow("Сервер", self.ed_srvr)
        form.addRow("Имя базы", self.ed_ref)
        form.addRow("Каталог базы", self.ed_file)
        form.addRow("ProgID коннектора", self.ed_progid)
        form.addRow("Пользователь 1С", self.ed_usr)
        form.addRow("Пароль", self.ed_pwd)

        self.cb_reconnect = QCheckBox("Подключаться к 1С при запуске")
        self.cb_reconnect.setChecked(s.reconnect_on_start)
        form.addRow("", self.cb_reconnect)

        self.btn_test = QPushButton("Проверить подключение")
        self.btn_test.clicked.connect(self._on_test)
        self.lbl_test = QLabel("")
        self.lbl_test.setWordWrap(True)
        form.addRow("", self.btn_test)
        form.addRow("", self.lbl_test)

        hint = QLabel("Пароль хранится зашифрованным (Windows DPAPI) и доступен "
                      "только этой учётной записи Windows.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5f6368;")
        form.addRow("", hint)
        return page

    def _on_known_base(self, index: int) -> None:
        connect = self.known.itemData(index) or ""
        if not connect:
            return
        parsed = parse_connect_string(connect)
        if parsed.get("file"):
            self.rb_file.setChecked(True)
            self.ed_file.setText(parsed["file"])
        else:
            self.rb_server.setChecked(True)
            self.ed_srvr.setText(parsed.get("srvr", ""))
            self.ed_ref.setText(parsed.get("ref", ""))
        self._sync_kind()

    def _sync_kind(self) -> None:
        server = self.rb_server.isChecked()
        self.ed_srvr.setEnabled(server)
        self.ed_ref.setEnabled(server)
        self.ed_file.setEnabled(not server)

    def _on_test(self) -> None:
        if self._test is not None and self._test.isRunning():
            return
        self.btn_test.setEnabled(False)
        self.lbl_test.setText("Проверяю…")
        self.lbl_test.setStyleSheet("color: #5f6368;")
        self._test = TestConnectionTask(self.result_settings(), self)
        self._test.finishedWith.connect(self._on_test_done)
        self._test.start()

    def done(self, result: int) -> None:
        # Проверка связи может идти в момент закрытия окна: уничтожить работающий
        # QThread нельзя (Qt роняет процесс), поэтому дожидаемся его.
        if self._test is not None and self._test.isRunning():
            self._test.wait(10000)
        super().done(result)

    def _on_test_done(self, ok: bool, message: str, detail: str) -> None:
        if detail:
            log.info("Проверка подключения: %s", detail)
        self.lbl_test.setText(message)
        self.lbl_test.setStyleSheet(f"color: {'#1b7f3b' if ok else '#b3261e'};")
        self.btn_test.setEnabled(True)

    # --- вкладка «Печать» -------------------------------------------------
    def _build_print_tab(self) -> QWidget:
        s = self._settings
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignRight)

        self.cmb_printer = QComboBox()
        self.cmb_printer.addItem(DEFAULT_PRINTER_ITEM, "")
        for name in printing.available_printers():
            self.cmb_printer.addItem(name, name)
        idx = self.cmb_printer.findData(s.printer_name)
        self.cmb_printer.setCurrentIndex(idx if idx >= 0 else 0)
        if s.printer_name and idx < 0:
            # Принтер сохранён, но сейчас не найден (выключен/отсоединён кабель) —
            # не теряем выбор пользователя молча.
            self.cmb_printer.addItem(f"{s.printer_name} (не найден)", s.printer_name)
            self.cmb_printer.setCurrentIndex(self.cmb_printer.count() - 1)

        btn_detect = QPushButton("Найти принтер этикеток")
        btn_detect.clicked.connect(self._on_detect_printer)
        row = QHBoxLayout()
        row.addWidget(self.cmb_printer, 1)
        row.addWidget(btn_detect)
        box = QWidget()
        box.setLayout(row)
        form.addRow("Принтер", box)

        self.sp_w = QDoubleSpinBox()
        self.sp_w.setRange(10, 210)
        self.sp_w.setDecimals(1)
        self.sp_w.setSuffix(" мм")
        self.sp_w.setValue(s.label_w_mm)
        self.sp_h = QDoubleSpinBox()
        self.sp_h.setRange(10, 297)
        self.sp_h.setDecimals(1)
        self.sp_h.setSuffix(" мм")
        self.sp_h.setValue(s.label_h_mm)
        form.addRow("Ширина этикетки", self.sp_w)
        form.addRow("Высота этикетки", self.sp_h)

        self.sp_copies = QSpinBox()
        self.sp_copies.setRange(1, 10)
        self.sp_copies.setValue(s.copies)
        form.addRow("Копий", self.sp_copies)

        self.cb_auto = QCheckBox("Печатать сразу после сканирования")
        self.cb_auto.setChecked(s.auto_print)
        self.cb_auto_many = QCheckBox(
            "Если ячеек несколько — печатать основную (иначе ждать выбора)")
        self.cb_auto_many.setChecked(s.auto_print_when_many)
        self.cb_wh = QCheckBox("Печатать склад мелким шрифтом сверху")
        self.cb_wh.setChecked(s.show_warehouse)
        self.cb_name = QCheckBox("Печатать наименование мелким шрифтом снизу")
        self.cb_name.setChecked(s.show_name)
        for cb in (self.cb_auto, self.cb_auto_many, self.cb_wh, self.cb_name):
            form.addRow("", cb)

        btn_test_print = QPushButton("Пробная печать")
        btn_test_print.clicked.connect(self._on_test_print)
        form.addRow("", btn_test_print)
        return page

    def _on_detect_printer(self) -> None:
        name = printing.detect_label_printer()
        if not name:
            QMessageBox.information(
                self, "Принтер",
                "Принтер этикеток не найден. Проверьте, что Xprinter включён, "
                "подключён кабелем и установлен в Windows.")
            return
        idx = self.cmb_printer.findData(name)
        if idx < 0:
            self.cmb_printer.addItem(name, name)
            idx = self.cmb_printer.count() - 1
        self.cmb_printer.setCurrentIndex(idx)

    def _on_test_print(self) -> None:
        sample = LabelData(cell="2-3-3-11-1-1", article="86617L2300",
                           name="KIA 86617L2300", warehouse="Оригинал")
        try:
            name = printing.print_label(sample, self.result_settings())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Пробная печать", str(e))
            return
        QMessageBox.information(self, "Пробная печать",
                                f"Этикетка отправлена на «{name}».")

    # --- вкладка «Запросы» ------------------------------------------------
    def _build_query_tab(self) -> QWidget:
        s = self._settings
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel(
            "Запрос по штрихкоду. Плейсхолдер {ШТРИХКОД} подставляется как строковый "
            "литерал 1С.\nПорядок колонок обязателен: Ячейка, Артикул, Наименование, "
            "Склад, ОсновнаяЯчейка."))
        self.ed_query = QPlainTextEdit(s.query)
        self.ed_query.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self.ed_query, 2)

        self.cb_fallback = QCheckBox("Если штрихкод не найден — искать по артикулу")
        self.cb_fallback.setChecked(s.use_fallback)
        layout.addWidget(self.cb_fallback)
        self.ed_fallback = QPlainTextEdit(s.fallback_query)
        self.ed_fallback.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self.ed_fallback, 2)

        row = QHBoxLayout()
        row.addWidget(QLabel("Склады (через запятую, пусто = все):"))
        self.ed_warehouses = QLineEdit(", ".join(s.warehouses))
        self.ed_warehouses.setPlaceholderText("например, Оригинал, Квант (Новые) 3 этаж")
        row.addWidget(self.ed_warehouses, 1)
        layout.addLayout(row)

        btn_reset = QPushButton("Вернуть запросы по умолчанию")
        btn_reset.clicked.connect(self._on_reset_queries)
        layout.addWidget(btn_reset)
        return page

    def _on_reset_queries(self) -> None:
        self.ed_query.setPlainText(DEFAULT_QUERY)
        self.ed_fallback.setPlainText(DEFAULT_FALLBACK_QUERY)

    # --- результат --------------------------------------------------------
    def result_settings(self) -> Settings:
        """Собрать настройки из полей (исходный объект не меняется)."""
        warehouses = [w.strip() for w in self.ed_warehouses.text().split(",") if w.strip()]
        printer = self.cmb_printer.currentData() or ""
        return replace(
            self._settings,
            source="local" if self.rb_local.isChecked() else "com",
            snapshot_share=self.ed_share.text().strip(),
            snapshot_auto_minutes=self.sp_auto.value(),
            transport="yadisk" if self.rb_tr_yadisk.isChecked() else "file",
            yadisk_token=normalize_token(self.ed_token.text()),
            yadisk_path=self.ed_yapath.text().strip() or "app:/cells.db",
            kind="server" if self.rb_server.isChecked() else "file",
            srvr=self.ed_srvr.text().strip(),
            ref=self.ed_ref.text().strip(),
            file_path=self.ed_file.text().strip(),
            progid=self.ed_progid.text().strip() or "V83.COMConnector",
            usr=self.ed_usr.text().strip(),
            password=self.ed_pwd.text(),
            reconnect_on_start=self.cb_reconnect.isChecked(),
            query=self.ed_query.toPlainText(),
            fallback_query=self.ed_fallback.toPlainText(),
            use_fallback=self.cb_fallback.isChecked(),
            warehouses=warehouses,
            printer_name=printer,
            printer_bound=True,
            label_w_mm=self.sp_w.value(),
            label_h_mm=self.sp_h.value(),
            copies=self.sp_copies.value(),
            auto_print=self.cb_auto.isChecked(),
            auto_print_when_many=self.cb_auto_many.isChecked(),
            show_warehouse=self.cb_wh.isChecked(),
            show_name=self.cb_name.isChecked(),
        )
