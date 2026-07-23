"""Вывод стикера на устройство: прямая печать на принтер и сохранение в PDF.

Печать идёт БЕЗ диалогов — оператор пикает штрихкод, этикетка сразу выезжает.
Принтер «привязывается» к приложению: имя хранится в настройках, а при первом
запуске подбирается автоматически (см. :func:`detect_label_printer`) — на рабочем
месте это подключённый проводом Xprinter.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from PySide2.QtCore import QMarginsF, QSizeF
from PySide2.QtGui import QPageSize, QPainter
from PySide2.QtPrintSupport import QPrinter, QPrinterInfo

from .config import Settings
from .label import LabelData, LabelStyle, draw_label

log = logging.getLogger(__name__)

# Признаки принтера этикеток в его имени/драйвере. Xprinter в Windows называется
# по-разному в зависимости от драйвера: «Xprinter XP-365B», «XP-370B», иногда
# «4BARCODE» или «POS-80» — поэтому список широкий.
_LABEL_HINTS = (
    "xprinter", "xp-3", "xp-4", "xp-2", "xp-8", "gprinter", "gp-", "zdesigner",
    "zebra", "tsc ", "ttp-", "godex", "bixolon", "argox", "honeywell", "datamax",
    "intermec", "sato", "citizen", "4barcode", "label", "этикет", "thermal",
    "термо", "printpos", "pos-", "netum", "sunmi", "rongta", "atol",
)

# Виртуальные принтеры: автоподбором их брать нельзя — печатать «сразу» в файл
# бессмысленно, оператор ничего не получит.
_VIRTUAL_HINTS = (
    "onenote", "pdf", "xps", "fax", "microsoft print to", "отправить", "snagit",
    "foxit", "dopdf", "bullzip", "adobe pdf", "любимый",
)


def available_printers() -> list[str]:
    return [p.printerName() for p in QPrinterInfo.availablePrinters()]


def default_printer_name() -> str:
    info = QPrinterInfo.defaultPrinter()
    return "" if info.isNull() else info.printerName()


def _is_virtual(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in _VIRTUAL_HINTS)


def detect_label_printer() -> str:
    """Найти установленный принтер этикеток; "" если не опознан.

    Сначала ищем по характерным именам, затем — принтер Windows по умолчанию,
    если он не виртуальный. Результат приложение сохраняет в настройки, так что
    подбор происходит один раз, а дальше принтер закреплён явно.
    """
    names = available_printers()
    for name in names:
        low = name.lower()
        if _is_virtual(low):
            continue
        if any(h in low for h in _LABEL_HINTS):
            log.info("Автоподбор принтера этикеток: %s", name)
            return name
    fallback = default_printer_name()
    if fallback and not _is_virtual(fallback):
        log.info("Принтер этикеток не опознан, беру принтер по умолчанию: %s", fallback)
        return fallback
    return ""


def resolve_printer(name: str) -> QPrinterInfo | None:
    """Найти принтер по имени; пустое имя = принтер Windows по умолчанию."""
    if name:
        for p in QPrinterInfo.availablePrinters():
            if p.printerName() == name:
                return p
        return None
    info = QPrinterInfo.defaultPrinter()
    return None if info.isNull() else info


def printer_available(settings: Settings) -> bool:
    return resolve_printer(settings.printer_name) is not None


def _configure(printer: QPrinter, settings: Settings) -> None:
    """Задать точный размер этикетки и убрать поля.

    setFullPage(True) означает «рисую по всей бумаге сам» — Qt не отступает на
    неиспользуемые драйвером поля, и композиция не съезжает.
    """
    size = QPageSize(QSizeF(settings.label_w_mm, settings.label_h_mm),
                     QPageSize.Millimeter, "Label", QPageSize.ExactMatch)
    printer.setPageSize(size)
    printer.setFullPage(True)
    # Перегрузка с QMarginsF + QPageLayout.Unit в PySide2 не проброшена, а форма
    # из четырёх чисел объявлена устаревшей; берём приём одного QMarginsF —
    # нулевые поля одинаковы в любых единицах.
    printer.setPageMargins(QMarginsF(0, 0, 0, 0))
    printer.setOrientation(QPrinter.Portrait)


def _paint(printer: QPrinter, data: LabelData, style: LabelStyle, copies: int) -> None:
    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("Не удалось начать печать: устройство недоступно.")
    try:
        for i in range(max(1, copies)):
            if i:
                printer.newPage()
            draw_label(painter, printer.width(), printer.height(), data, style)
    finally:
        painter.end()


def print_label(data: LabelData, settings: Settings,
                style: LabelStyle | None = None) -> str:
    """Напечатать этикетку напрямую, без диалогов. Возвращает имя принтера."""
    info = resolve_printer(settings.printer_name)
    if info is None:
        raise RuntimeError(
            f"Принтер «{settings.printer_name}» не найден. "
            "Проверьте кабель и выберите принтер в настройках."
            if settings.printer_name else
            "Принтер не выбран. Укажите его в настройках.")
    printer = QPrinter(info, QPrinter.HighResolution)
    _configure(printer, settings)
    _paint(printer, data, style or LabelStyle.from_settings(settings), settings.copies)
    log.info("Напечатано: ячейка=%s артикул=%s принтер=%s",
             data.cell, data.article, info.printerName())
    return info.printerName()


def save_pdf(data: LabelData, settings: Settings, path: Path,
             style: LabelStyle | None = None) -> Path:
    """Сохранить этикетку в PDF того же физического размера."""
    path.parent.mkdir(parents=True, exist_ok=True)
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(str(path))
    _configure(printer, settings)
    _paint(printer, data, style or LabelStyle.from_settings(settings), 1)
    log.info("PDF сохранён: %s", path)
    return path


def safe_file_stem(*parts: str) -> str:
    """Имя файла из ячейки/артикула: без символов, недопустимых в путях Windows."""
    raw = "_".join(p for p in parts if p) or "label"
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", raw)[:80]
