"""Выгрузка из 1С в локальный снимок: «штрихкод → ячейка + артикул».

Запускается на машине, где стоит ПОЛНАЯ 64-битная платформа 1С (COM-соединение).
На складском ПК стоит тонкий клиент, в нём нет comcntr.dll — оттуда выгружать
нечем, он только читает готовый файл.

Работа идёт в два шага, и это важно: снимок сначала собирается ЛОКАЛЬНО
(``%LOCALAPPDATA%\\CellScanner\\export\\cells.db``), и только потом копируется
получателю. Получатель — папка на складском ПК, а его на ночь могут выключить;
при слитных шагах недоступность получателя означала бы потерю всей выгрузки.
Здесь же снимок остаётся готовым и ждёт следующей попытки доставки.

Настройки подключения берутся из настроек приложения
(``%LOCALAPPDATA%\\CellScanner\\settings.json``) — то есть один раз настраиваются
в самом приложении на этой машине, а утилита их переиспользует.

Запуск вручную:
    .venv\\Scripts\\python tools\\export_cells.py --out "\\\\СКЛАД-ПК\\CellScanner\\cells.db"

Повторить только доставку (без обращения к 1С) — этим живёт частая задача-досыл:
    .venv\\Scripts\\python tools\\export_cells.py --deliver-only

По расписанию — через tools\\export_cells.bat и «Планировщик заданий» Windows.

Коды возврата:
    0 — снимок сделан и доставлен;
    1 — выгрузка не удалась (1С недоступна, настройки не заданы);
    2 — снимок сделан, но доставить не удалось (получатель выключен/недоступен).
        Это НЕ потеря данных: следующая попытка доставки заберёт готовый файл.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paths  # noqa: E402
from app.config import Settings  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.onec import OneCClient, describe_error, error_text  # noqa: E402
from app.query import (EXPORT_BARCODES_COLUMNS, EXPORT_BARCODES_QUERY,  # noqa: E402
                       EXPORT_PLACEMENTS_COLUMNS, EXPORT_PLACEMENTS_QUERY)
from app.snapshot import SnapshotBuilder, SnapshotError, push  # noqa: E402

log = logging.getLogger("export_cells")


def build_snapshot(client: OneCClient) -> SnapshotBuilder:
    """Прочитать оба среза из 1С и сложить в накопитель."""
    builder = SnapshotBuilder()

    log.info("Читаю штрихкоды и их размещения…")
    started = time.monotonic()
    rows = client.query(EXPORT_BARCODES_QUERY, EXPORT_BARCODES_COLUMNS)
    log.info("  строк: %d за %.1f с", len(rows), time.monotonic() - started)
    for code, article, name, cell, warehouse, is_main in rows:
        code = builder.add_barcode(code, article, name)
        builder.add_barcode_cell(code, cell, warehouse, is_main)

    log.info("Читаю размещения по артикулам…")
    started = time.monotonic()
    rows = client.query(EXPORT_PLACEMENTS_QUERY, EXPORT_PLACEMENTS_COLUMNS)
    log.info("  строк: %d за %.1f с", len(rows), time.monotonic() - started)
    for article, name, cell, warehouse, is_main in rows:
        article = builder.add_article(article, name)
        builder.add_article_cell(article, cell, warehouse, is_main)

    return builder


def deliver(staging: Path, settings: Settings) -> int:
    """Отправить готовый снимок получателю. 0 — доставлено, 2 — не удалось.

    Транспорт (общая папка или Яндекс.Диск) выбран в настройках; здесь он не важен.
    """
    staging = Path(staging)
    if not staging.exists():
        log.error("Доставлять нечего: снимок ещё не собран (%s). "
                  "Запустите выгрузку без --deliver-only.", staging)
        return 1
    try:
        where = push(settings, staging)
        log.info("Доставлено: %s", where)
        return 0
    except (SnapshotError, OSError) as e:
        # Штатная ситуация: получатель выключен, нет интернета, отвалился Диск.
        # Снимок никуда не делся — доставит следующая попытка.
        log.warning("Доставить не удалось (%s): %s", settings.describe_transport(), e)
        log.warning("Снимок сохранён локально: %s — доставка повторится позже.",
                    staging)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Выгрузка из 1С в локальную базу приложения «Ячейка по штрихкоду»")
    parser.add_argument("--out", default="",
                        help="куда доставить файл (по умолчанию — путь из настроек "
                             "приложения, поле «Общая папка»)")
    parser.add_argument("--deliver-only", action="store_true",
                        help="не обращаться к 1С: только повторить доставку "
                             "уже собранного снимка")
    args = parser.parse_args(argv)

    paths.ensure_dirs()
    setup_logging()

    settings = Settings.load()
    if args.out:
        # Явный путь всегда означает доставку файлом, даже если в настройках Диск.
        settings.transport = "file"
        settings.snapshot_share = args.out
    staging = paths.export_staging_path()

    if not settings.uses_yadisk and not settings.snapshot_share:
        log.error("Не задан путь доставки. Укажите --out или заполните источник "
                  "в настройках приложения.")
        return 1
    if settings.uses_yadisk and not settings.yadisk_token:
        log.error("Не задан токен Яндекс.Диска. Откройте приложение на этой машине, "
                  "«Настройки» → «Источник данных».")
        return 1

    if args.deliver_only:
        return deliver(staging, settings)

    if not settings.is_configured():
        log.error("Подключение к 1С не настроено. Откройте приложение на этой машине, "
                  "вкладка «Настройки» → «1С», и проверьте соединение.")
        return 1

    log.info("=== Выгрузка из 1С (%s) -> %s ===",
             settings.describe_base(), settings.describe_transport())
    client = OneCClient(settings)
    try:
        started = time.monotonic()
        builder = build_snapshot(client)
        builder.write(staging, meta={"source_base": settings.describe_base()})
        log.info("Снимок собран за %.1f с: %s", time.monotonic() - started,
                 builder.counts)
    except Exception as e:  # noqa: BLE001 - утилита не должна падать трассировкой
        log.error("Выгрузка не удалась: %s", describe_error(e))
        log.error("Подробности: %s", error_text(e))
        return 1
    finally:
        client.close()

    return deliver(staging, settings)


if __name__ == "__main__":
    sys.exit(main())
