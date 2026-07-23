"""Связь с 1С через внешнее COM-соединение (Windows) и поиск ячейки по штрихкоду.

Требуется платформа 1С той же разрядности, что и Python (x64), и pywin32.

КРИТИЧНО (проверено на боевой базе в соседнем проекте zzap app):
  * COM апартаментно-потоковый: поток, работающий с COM, сам вызывает
    CoInitialize/CoUninitialize. Весь COM здесь живёт на одном рабочем потоке —
    GUI его не касается.
  * Перед CoUninitialize все COM-объекты обнуляются и делается gc.collect(),
    иначе их финализация после деинициализации COM роняет процесс (segfault).

В отличие от пакетной выгрузки, здесь соединение ДОЛГОЖИВУЩЕЕ: подключение к 1С
занимает секунды, а сканирований за смену сотни — переподключаться на каждый
штрихкод недопустимо. При обрыве связи запрос повторяется один раз с
переподключением.
"""
from __future__ import annotations

import gc
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .config import Settings
from .query import COLUMNS, PING_QUERY, build_lookup_query

log = logging.getLogger(__name__)


@dataclass
class Placement:
    """Одна строка размещения: где товар должен лежать."""
    cell: str
    warehouse: str = ""
    is_main: bool = False


@dataclass
class LookupResult:
    """Итог поиска по одному отсканированному коду."""
    barcode: str
    found: bool = False              # код найден в 1С (даже если ячейки нет)
    article: str = ""
    name: str = ""
    placements: list[Placement] = field(default_factory=list)
    by_article: bool = False         # найдено запасным запросом (по артикулу)

    @property
    def has_cell(self) -> bool:
        return bool(self.placements)


def _s(value) -> str:
    return "" if value is None else str(value).strip()


def _truthy(value) -> bool:
    """1С возвращает булево как Python bool; на всякий случай терпим и строки."""
    if isinstance(value, bool):
        return value
    return _s(value).lower() in ("true", "да", "1")


def build_placements(raw: Iterable[tuple], warehouses: Sequence[str]) -> list[Placement]:
    """Тройки (ячейка, склад, признак основной) -> отфильтрованный список размещений.

    Общая точка для обоих источников — прямого COM-запроса и локального снимка,
    чтобы правила отбора не разъехались между ними:

      * строки без ячейки отбрасываются (у товара нет размещения);
      * при заданном списке складов остальные отсеиваются;
      * дедуп по паре склад+ячейка — регистр размещения разбит ещё и по
        помещениям, поэтому одна и та же ячейка приходит несколько раз;
      * сортировка: основная ячейка первой, дальше по складу и номеру.
    """
    allowed = {w.strip().lower() for w in warehouses if w and w.strip()}
    seen: set[tuple[str, str]] = set()
    out: list[Placement] = []
    for cell_raw, warehouse_raw, main_raw in raw:
        cell, warehouse = _s(cell_raw), _s(warehouse_raw)
        if not cell:
            continue
        if allowed and warehouse.lower() not in allowed:
            continue
        key = (cell.lower(), warehouse.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(Placement(cell=cell, warehouse=warehouse, is_main=_truthy(main_raw)))
    out.sort(key=lambda p: (not p.is_main, p.warehouse.lower(), p.cell))
    return out


class OneCClient:
    """Долгоживущее COM-соединение с 1С + поиск ячейки.

    Всеми методами пользуется ОДИН поток (рабочий поток GUI). ``start()`` делает
    CoInitialize для этого потока, ``close()`` — безопасный teardown.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._connector = None
        self._conn = None
        self._pythoncom = None
        self._co_init = False

    # --- жизненный цикл --------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _ensure_com(self) -> None:
        if self._co_init:
            return
        try:
            import pythoncom
            import win32com.client.dynamic  # noqa: F401 - проверяем наличие
        except ImportError as e:  # pragma: no cover - зависит от окружения
            raise RuntimeError(
                "Нужен pywin32 (Windows). Установите: pip install pywin32") from e
        self._pythoncom = pythoncom
        pythoncom.CoInitialize()
        self._co_init = True

    def connect(self) -> None:
        """Установить соединение (idempotent). Бросает исключение при неудаче."""
        if self._conn is not None:
            return
        self._ensure_com()
        import win32com.client.dynamic

        cs = self.settings.conn_string()
        log.info("COM: подключаюсь к 1С (%s) через %s",
                 self.settings.describe_base(), self.settings.progid)
        try:
            # dynamic.Dispatch — позднее связывание через IDispatch (надёжно для 1С).
            self._connector = win32com.client.dynamic.Dispatch(self.settings.progid)
            self._conn = self._connector.Connect(cs)
        except BaseException:
            self._drop()
            raise
        log.info("COM: соединение установлено")

    def _drop(self) -> None:
        """Отпустить COM-объекты соединения, не снимая CoInitialize."""
        self._conn = None
        self._connector = None
        gc.collect()

    def close(self) -> None:
        """Полный teardown: объекты -> gc -> CoUninitialize (в этом порядке)."""
        self._drop()
        if self._co_init and self._pythoncom is not None:
            self._pythoncom.CoUninitialize()
            self._co_init = False
        log.info("COM: соединение закрыто")

    # --- запросы ----------------------------------------------------------
    def _query(self, text: str, columns: int) -> list[tuple]:
        if self._conn is None:
            raise RuntimeError("Нет активного соединения с 1С.")
        q = sel = None
        try:
            q = self._conn.NewObject("Query")
            q.Text = text
            sel = q.Execute().Select()
            rows: list[tuple] = []
            while sel.Next():
                rows.append(tuple(sel.Get(i) for i in range(columns)))
            return rows
        finally:
            sel = q = None  # освобождаем объекты запроса до следующего вызова

    def _query_resilient(self, text: str, columns: int) -> list[tuple]:
        """Выполнить запрос, один раз переподключившись при обрыве связи."""
        self.connect()
        try:
            return self._query(text, columns)
        except Exception as first:  # noqa: BLE001 - разрыв соединения выглядит по-разному
            log.warning("Запрос не прошёл (%s) — переподключаюсь и повторяю",
                        error_text(first))
            self._drop()
            self.connect()
            return self._query(text, columns)

    def query(self, text: str, columns: int) -> list[tuple]:
        """Выполнить произвольный запрос (используется утилитой выгрузки)."""
        return self._query_resilient(text, columns)

    def ping(self) -> None:
        """Проверить связь (бросает исключение, если 1С недоступна)."""
        self._query_resilient(PING_QUERY, 1)

    def lookup(self, barcode: str) -> LookupResult:
        """Найти ячейку и артикул по отсканированному коду.

        Сначала по регистру штрихкодов; если ничего не найдено и разрешён
        запасной поиск — по артикулу номенклатуры.
        """
        s = self.settings
        rows = self._query_resilient(build_lookup_query(s.query, barcode), COLUMNS)
        by_article = False
        if not rows and s.use_fallback and s.fallback_query.strip():
            rows = self._query_resilient(
                build_lookup_query(s.fallback_query, barcode), COLUMNS)
            by_article = bool(rows)

        if not rows:
            return LookupResult(barcode=barcode, found=False)

        result = LookupResult(barcode=barcode, found=True,
                              article=_s(rows[0][1]), name=_s(rows[0][2]),
                              by_article=by_article)
        result.placements = self._placements(rows)
        return result

    def lookup_article(self, article: str) -> LookupResult:
        """Поиск строго по артикулу — для ручного ввода с клавиатуры.

        Использует тот же запрос, что и запасной поиск, но вызывается явно:
        оператор набирает артикул с коробки, когда штрихкода нет или он не читается.
        """
        s = self.settings
        if not s.fallback_query.strip():
            raise RuntimeError("Не задан запрос поиска по артикулу (см. «Настройки»).")
        rows = self._query_resilient(
            build_lookup_query(s.fallback_query, article), COLUMNS)
        if not rows:
            return LookupResult(barcode=article, found=False, by_article=True)
        return LookupResult(barcode=article, found=True, article=_s(rows[0][1]),
                            name=_s(rows[0][2]), by_article=True,
                            placements=self._placements(rows))

    def _placements(self, rows: list[tuple]) -> list[Placement]:
        """Строки запроса (Ячейка, Артикул, Наименование, Склад, Основная) -> размещения."""
        return build_placements(((r[0], r[3], r[4]) for r in rows),
                                self.settings.warehouses)


# --- разбор ошибок 1С -----------------------------------------------------
# Учётные данные в строке соединения: некоторые ошибки COM возвращают её целиком
# (вместе с Pwd="...") — вычищаем до записи в журнал и показа пользователю.
# Ветка со значением в кавычках обязана съедать УДВОЕННЫЕ кавычки, потому что
# именно так conn_string() экранирует внутреннюю кавычку (Pwd="a""b"); шаблон
# "[^"]*" остановился бы на первой внутренней кавычке и выпустил остаток пароля.
_SECRET_RE = re.compile(r'(?i)\b(Pwd|Password|Usr|User)\s*=\s*("(?:[^"]|"")*"|[^;"\s]+)')


def error_text(exc: BaseException) -> str:
    """Развернуть исключение (включая pythoncom.com_error) в одну строку.

    ``com_error.args`` = (hresult, msg, excinfo, argerr); описание ошибки от 1С
    лежит в excinfo. Пароль 1С утечь не должен — токены Pwd=/Usr= затираются.
    """
    parts: list[str] = []
    for a in getattr(exc, "args", ()) or ():
        if isinstance(a, (tuple, list)):
            parts.extend(str(s) for s in a if isinstance(s, str))
        elif isinstance(a, str):
            parts.append(a)
        elif isinstance(a, int):
            parts.append(f"0x{a & 0xFFFFFFFF:08X}")
    parts.append(str(exc))
    return _SECRET_RE.sub(lambda m: f'{m.group(1)}="***"', " | ".join(p for p in parts if p))


def describe_error(exc: BaseException) -> str:
    """Ошибка 1С/COM -> понятное действие для пользователя."""
    low = error_text(exc).lower()

    def has(*subs: str) -> bool:
        return any(s in low for s in subs)

    if has("внешнее соединение", "external connection"):
        return ("У пользователя 1С нет права «Внешнее соединение» (COM). "
                "Выдайте это право или укажите другого пользователя.")
    # Два разных кода означают одно и то же для пользователя: коннектор недоступен
    # этому процессу. 0x800401F3 — ProgID вообще неизвестен системе (коннектор не
    # регистрировали), 0x80040154 — ProgID есть, а класс не зарегистрирован
    # (обычно 1С другой разрядности). Русская локаль даёт «Недопустимая строка
    # класса» и «Класс не зарегистрирован» — ловим оба падежа.
    if has("не зарегистрирован", "class not registered", "0x80040154", "80040154",
           "0x80040153", "invalid class string", "0x800401f3", "800401f3",
           "недопустимая строка класса"):
        return ("COM-коннектор 1С недоступен приложению: он не зарегистрирован "
                "или зарегистрирован для другой разрядности. Нажмите "
                "«Подробности» — там точная причина и команда для исправления.")
    if has("идентификац", "пароль", "password", "имя пользователя",
           "пользователь не найден", "не найден пользователь"):
        return "Неверный логин или пароль пользователя 1С."
    if has("сервер", "server", "tcp", "не обнаружен", "недоступ", "timeout",
           "соединение с сервером", "rpc"):
        return "Сервер 1С недоступен. Проверьте адрес сервера, сеть и порты."
    if has("pywin32"):
        return ("Не загрузился компонент pywin32 — приложение собрано некорректно. "
                "Нажмите «Подробности».")
    if has("не найден", "поле не найдено", "синтаксическая ошибка", "syntax"):
        return ("Запрос не выполняется на этой базе — проверьте текст запроса "
                "в настройках.")
    return "Не удалось обратиться к 1С. Нажмите «Подробности» — там полный текст ошибки."
