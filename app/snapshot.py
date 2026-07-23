"""Локальный снимок данных 1С: «штрихкод → ячейка + артикул» в одном SQLite-файле.

Зачем: на складском ПК стоит только тонкий клиент 1С, а он не содержит
``comcntr.dll`` — COM-соединение оттуда невозможно в принципе. Поэтому машина с
полной платформой раз в сутки выгружает нужный срез в файл (см.
``tools/export_cells.py``), кладёт его в общую папку, а приложение на складе
читает только файл: ни платформы, ни лицензии, ни разрядности, ни связи с 1С.

Объём смешной — 23 706 штрихкодов и 9 213 размещений, файл на пару мегабайт,
поиск по индексу мгновенный.

Почему таблицы устроены именно так. Связать штрихкод с размещением на стороне
Python не выйдет: у номенклатуры нет пригодного ключа — `Код` пустой у части
позиций и не уникален (10 668 кодов на 12 054 товара), а ссылку через COM в
строку не превратить. Поэтому соединение делает сама 1С в тексте запроса, а
снимок хранит уже готовые пары «штрихкод → ячейка» и «артикул → ячейка».
Дублирование ячеек в двух таблицах на таких объёмах ничего не стоит и полностью
снимает вопрос идентичности товара.

Модуль держит и запись, и чтение: схема описана один раз, выгрузка и приложение
не разъедутся.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .onec import LookupResult, build_placements

log = logging.getLogger(__name__)

# Версия схемы: приложение откажется читать снимок несовместимой выгрузки вместо
# того, чтобы молча искать не в тех колонках.
SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE barcode (
    code    TEXT PRIMARY KEY,
    article TEXT NOT NULL,
    name    TEXT NOT NULL
);
CREATE TABLE barcode_cell (
    code      TEXT NOT NULL,
    cell      TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    is_main   INTEGER NOT NULL
);
CREATE INDEX barcode_cell_code ON barcode_cell(code);

-- Запасной поиск: на коробке часто напечатан артикул, а не штрихкод. Регистр
-- не учитываем — набивают как придётся.
CREATE TABLE article (
    article TEXT PRIMARY KEY COLLATE NOCASE,
    name    TEXT NOT NULL
);
CREATE TABLE article_cell (
    article   TEXT NOT NULL COLLATE NOCASE,
    cell      TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    is_main   INTEGER NOT NULL
);
CREATE INDEX article_cell_article ON article_cell(article COLLATE NOCASE);
"""


class SnapshotBuilder:
    """Накопитель выгрузки: собирает строки в памяти и пишет файл одним куском."""

    def __init__(self) -> None:
        self._barcodes: dict[str, tuple[str, str]] = {}
        self._barcode_cells: set[tuple[str, str, str, int]] = set()
        # Ключ — артикул в нижнем регистре: в таблице он PRIMARY KEY COLLATE
        # NOCASE, и «ABC» с «abc» там одна строка. Дедуп на стороне Python обязан
        # работать по тому же правилу, иначе вставка падает на UNIQUE.
        self._articles: dict[str, tuple[str, str]] = {}
        self._article_cells: set[tuple[str, str, str, int]] = set()

    @staticmethod
    def _clean(value) -> str:
        return "" if value is None else str(value).strip()

    def add_barcode(self, code, article, name) -> str:
        code = self._clean(code)
        if code:
            self._barcodes[code] = (self._clean(article), self._clean(name))
        return code

    def add_barcode_cell(self, code, cell, warehouse, is_main) -> None:
        code, cell = self._clean(code), self._clean(cell)
        if code and cell:
            self._barcode_cells.add((code, cell, self._clean(warehouse),
                                     1 if is_main else 0))

    def add_article(self, article, name) -> str:
        """Запомнить артикул; возвращает каноническое написание, чтобы строки
        размещений ссылались на него единообразно.

        Под одним артикулом в базе может лежать несколько позиций с разными
        наименованиями (например, «6203»). Ячейки у них собираются все, а для
        подписи берётся первое по алфавиту наименование — выбор произвольный, но
        одинаковый от выгрузки к выгрузке, чтобы подпись на стикере не прыгала.
        """
        article = self._clean(article)
        if not article:
            return ""
        key = article.casefold()
        name = self._clean(name)
        known = self._articles.get(key)
        if known is None:
            self._articles[key] = (article, name)
        elif name and (not known[1] or name < known[1]):
            self._articles[key] = (known[0], name)
        return self._articles[key][0]

    def add_article_cell(self, article, cell, warehouse, is_main) -> None:
        article, cell = self._clean(article), self._clean(cell)
        if article and cell:
            self._article_cells.add((article, cell, self._clean(warehouse),
                                     1 if is_main else 0))

    @property
    def counts(self) -> dict[str, int]:
        return {"barcodes": len(self._barcodes),
                "barcode_cells": len(self._barcode_cells),
                "articles": len(self._articles),
                "article_cells": len(self._article_cells)}

    def write(self, path: Path, meta: dict[str, str] | None = None) -> Path:
        """Записать снимок. Файл собирается рядом и подменяется одним движением —
        приложение никогда не увидит наполовину записанную базу."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)

        conn = sqlite3.connect(tmp)
        try:
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT INTO barcode(code, article, name) VALUES (?, ?, ?)",
                ((c, a, n) for c, (a, n) in self._barcodes.items()))
            conn.executemany(
                "INSERT INTO barcode_cell(code, cell, warehouse, is_main) "
                "VALUES (?, ?, ?, ?)", self._barcode_cells)
            conn.executemany("INSERT INTO article(article, name) VALUES (?, ?)",
                             self._articles.values())
            conn.executemany(
                "INSERT INTO article_cell(article, cell, warehouse, is_main) "
                "VALUES (?, ?, ?, ?)", self._article_cells)
            full_meta = {
                "schema_version": SCHEMA_VERSION,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                **{k: str(v) for k, v in self.counts.items()},
                **{k: str(v) for k, v in (meta or {}).items()},
            }
            conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)",
                             full_meta.items())
            conn.commit()
        finally:
            conn.close()
        tmp.replace(path)
        log.info("Снимок записан: %s (%s)", path, self.counts)
        return path


class SnapshotError(RuntimeError):
    """Снимок недоступен или непригоден к чтению."""


class Snapshot:
    """Чтение снимка. Открывается только на чтение — файл могут подменить в любой миг."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def open(self) -> None:
        if self._conn is not None:
            return
        if not self.path.exists():
            raise SnapshotError(
                f"Файл базы не найден: {self.path}\n"
                "Проверьте путь в настройках или нажмите «Обновить базу».")
        try:
            # check_same_thread=False: базу открывает и читает рабочий поток.
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro",
                                         uri=True, check_same_thread=False)
            version = self.meta().get("schema_version")
        except sqlite3.Error as e:
            self._conn = None
            raise SnapshotError(f"Файл базы не читается: {e}") from e
        if version != SCHEMA_VERSION:
            self.close()
            raise SnapshotError(
                f"Файл базы сделан несовместимой выгрузкой (версия схемы {version!r}, "
                f"нужна {SCHEMA_VERSION!r}). Обновите утилиту выгрузки.")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def meta(self) -> dict[str, str]:
        if self._conn is None:
            return {}
        return dict(self._conn.execute("SELECT key, value FROM meta"))

    def exported_at(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.meta().get("exported_at", ""))
        except ValueError:
            return None

    def age_text(self) -> str:
        """Человеческий возраст выгрузки — оператор должен видеть, свежая ли база."""
        when = self.exported_at()
        if when is None:
            return "дата выгрузки неизвестна"
        stamp = when.strftime("%d.%m.%Y %H:%M")
        days = (datetime.now() - when).days
        return f"выгрузка от {stamp}" if days <= 0 else \
            f"выгрузка от {stamp} ({days} дн. назад)"

    # --- поиск ------------------------------------------------------------
    def lookup(self, barcode: str, use_fallback: bool,
               warehouses: list[str]) -> LookupResult:
        """Тот же результат, что и у прямого запроса в 1С (см. OneCClient.lookup)."""
        if self._conn is None:
            raise SnapshotError("База не открыта.")

        row = self._conn.execute(
            "SELECT article, name FROM barcode WHERE code = ?", (barcode,)).fetchone()
        if row is not None:
            cells = self._conn.execute(
                "SELECT cell, warehouse, is_main FROM barcode_cell WHERE code = ?",
                (barcode,)).fetchall()
            return LookupResult(barcode=barcode, found=True, article=row[0],
                                name=row[1],
                                placements=build_placements(cells, warehouses))

        if use_fallback:
            return self.lookup_article(barcode, warehouses)

        return LookupResult(barcode=barcode, found=False)

    def lookup_article(self, article: str, warehouses: list[str]) -> LookupResult:
        """Поиск строго по артикулу — для ручного ввода с клавиатуры.

        Тот же путь, которым пользуется запасной поиск при неизвестном штрихкоде,
        но вызванный явно: оператор набирает артикул с коробки, когда штрихкода
        нет или он не читается.
        """
        if self._conn is None:
            raise SnapshotError("База не открыта.")
        row = self._conn.execute(
            "SELECT article, name FROM article WHERE article = ?",
            (article,)).fetchone()
        if row is None:
            return LookupResult(barcode=article, found=False, by_article=True)
        cells = self._conn.execute(
            "SELECT cell, warehouse, is_main FROM article_cell WHERE article = ?",
            (article,)).fetchall()
        return LookupResult(barcode=article, found=True, article=row[0], name=row[1],
                            by_article=True,
                            placements=build_placements(cells, warehouses))


def validate(path: Path) -> None:
    """Убедиться, что файл — пригодный снимок. Бросает SnapshotError, если нет.

    Нужна перед подменой рабочей базы: недокачанный или битый файл не должен
    затирать вчерашнюю рабочую копию — иначе склад останется вообще без данных.
    """
    snap = Snapshot(path)
    snap.open()
    try:
        if not snap.meta().get("barcodes"):
            raise SnapshotError("В файле нет данных о штрихкодах — выгрузка неполная.")
    finally:
        snap.close()


def adopt(candidate: Path, target: Path) -> None:
    """Поставить проверенный файл на место рабочей базы (или не трогать её)."""
    validate(candidate)
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate.replace(target)


def copy_if_newer(source: Path, target: Path) -> bool:
    """Забрать снимок из папки-источника, если он новее локального. True — обновили.

    Копия делается через временный файл и проверяется до подмены: оборванное
    копирование по сети не оставит на месте рабочей базы обрубок.
    """
    source, target = Path(source), Path(target)
    if not source.exists():
        raise SnapshotError(f"Источник недоступен или в нём нет файла: {source}")
    if target.exists():
        src, dst = source.stat(), target.stat()
        if src.st_mtime <= dst.st_mtime and src.st_size == dst.st_size:
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    shutil.copy2(source, tmp)
    try:
        adopt(tmp, target)
    except SnapshotError:
        tmp.unlink(missing_ok=True)     # рабочую базу оставляем прежней
        raise
    log.info("Локальная база обновлена из %s", source)
    return True


# --- транспорт между машинами --------------------------------------------
def _upload_marker(staging: Path) -> Path:
    return staging.with_name(staging.name + ".uploaded")


def _fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def push(settings, staging: Path) -> str:
    """Отправить готовый снимок получателю. Возвращает описание адресата.

    Работает и через общую папку, и через Яндекс.Диск — вызывающему всё равно.
    Уже доставленный файл повторно не отправляется: задача-досыл ходит раз в час,
    и без этой проверки она бы каждый раз заливала одни и те же пять мегабайт.
    """
    staging = Path(staging)
    if getattr(settings, "uses_yadisk", False):
        from .yadisk import YaDisk, YaDiskError
        client = YaDisk(settings.yadisk_token)
        marker = _upload_marker(staging)
        try:
            if marker.exists() and marker.read_text(encoding="utf-8").strip() == \
                    _fingerprint(staging) and client.modified_at(settings.yadisk_path):
                log.info("На Яндекс.Диске уже этот файл — повторная заливка не нужна")
                return f"Яндекс.Диск ({settings.yadisk_path}), файл уже актуален"
            client.upload(staging, settings.yadisk_path)
        except YaDiskError as e:
            raise SnapshotError(str(e)) from e
        marker.write_text(_fingerprint(staging), encoding="utf-8")
        return f"Яндекс.Диск ({settings.yadisk_path})"

    destination = settings.snapshot_share
    if not destination:
        raise SnapshotError("Не указан путь доставки (см. «Настройки»).")
    if copy_if_newer(staging, Path(destination)):
        return destination
    return f"{destination}, файл уже актуален"


def pull(settings, target: Path) -> bool:
    """Забрать свежий снимок в рабочую копию. True — база обновилась.

    Для Яндекс.Диска сначала сверяется время изменения: качать пять мегабайт
    ради неизменившегося файла незачем.
    """
    target = Path(target)
    if not getattr(settings, "uses_yadisk", False):
        return copy_if_newer(settings.snapshot_share, target)

    from .yadisk import YaDisk, YaDiskError
    client = YaDisk(settings.yadisk_token)
    try:
        remote_stamp = client.modified_at(settings.yadisk_path)
        if not remote_stamp:
            raise SnapshotError(
                f"На Яндекс.Диске нет файла {settings.yadisk_path}. "
                "Выгрузка ещё не загружалась?")
        marker = target.with_name(target.name + ".stamp")
        if target.exists() and marker.exists():
            if marker.read_text(encoding="utf-8").strip() == remote_stamp:
                return False        # на Диске тот же файл, что уже лежит локально

        tmp = target.with_name(target.name + ".part")
        client.download(settings.yadisk_path, tmp)
        try:
            adopt(tmp, target)
        except SnapshotError:
            tmp.unlink(missing_ok=True)     # рабочую базу оставляем прежней
            raise
        marker.write_text(remote_stamp, encoding="utf-8")
        return True
    except YaDiskError as e:
        raise SnapshotError(str(e)) from e
