"""Точка входа: «Ячейка по штрихкоду».

Пикнули сканером -> приложение спросило 1С, где должен лежать товар -> стикер
40x58 мм с номером ячейки и артикулом ушёл на принтер.

Запуск:  .venv\\Scripts\\pythonw.exe main.py      (без окна консоли)
         .venv\\Scripts\\python.exe  main.py      (с консолью, для отладки)
"""
from __future__ import annotations

import logging
import sys

from PySide2.QtCore import QTimer
from PySide2.QtWidgets import QApplication, QMessageBox

from app import diagnostics, paths, printing
from app.config import Settings
from app.gui.main_window import MainWindow
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


def bind_printer(settings: Settings) -> Settings:
    """Привязать принтер при первом запуске.

    Дальше имя принтера живёт в настройках, и подбор больше не выполняется —
    иначе появившийся в системе новый принтер мог бы молча перехватить печать.
    """
    if settings.printer_bound:
        return settings
    name = printing.detect_label_printer()
    if name:
        settings.printer_name = name
        log.info("Принтер привязан при первом запуске: %s", name)
    settings.printer_bound = True
    settings.save()
    return settings


def main() -> int:
    paths.ensure_dirs()
    setup_logging()
    log.info("=== Запуск «Ячейка по штрихкоду» ===")

    # Состояние окружения пишем сразу: если 1С не подключится, причина уже будет
    # в журнале — на складском ПК разбираться по факту некому.
    log.info("Диагностика окружения:\n%s", diagnostics.report())

    app = QApplication(sys.argv)
    app.setApplicationName("Ячейка по штрихкоду")

    settings = bind_printer(Settings.load())
    window = MainWindow(settings)
    window.show()

    ready, problem = settings.ready_to_work()
    if not ready:
        # Первый запуск: без источника данных работать нечем — открываем настройки.
        log.info("Источник данных не настроен: %s", problem)
        QTimer.singleShot(300, window.open_settings)
    elif not printing.printer_available(settings):
        QTimer.singleShot(300, lambda: QMessageBox.warning(
            window, "Принтер",
            "Принтер этикеток не найден. Проверьте, что Xprinter включён и "
            "подключён кабелем, либо выберите принтер в настройках.\n\n"
            "Пока принтера нет, стикер можно открыть в PDF (кнопка «Открыть PDF»)."))

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
