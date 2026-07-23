"""Настройки приложения: подключение к 1С, запросы, принтер, вид стикера.

Хранятся в ``%LOCALAPPDATA%\\CellScanner\\settings.json``. Пароль 1С в файл
попадает только зашифрованным (DPAPI, см. :mod:`app.secrets`) — в открытом виде
он живёт лишь в оперативной памяти.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import paths, secrets
from .query import DEFAULT_FALLBACK_QUERY, DEFAULT_QUERY

log = logging.getLogger(__name__)

DEFAULT_PROGID = "V83.COMConnector"

# Размер этикетки по умолчанию: 400 x 580 в десятых долях мм = 40 x 58 мм —
# ходовой рулон для термопринтеров (Xprinter/Godex и т.п.).
DEFAULT_LABEL_W_MM = 40.0
DEFAULT_LABEL_H_MM = 58.0


@dataclass
class Settings:
    # --- откуда берём ячейку ---
    # 'local' — из выгруженного файла (рабочее место со сканером: там тонкий
    #           клиент 1С, COM-соединение оттуда невозможно);
    # 'com'   — напрямую из 1С (машина с полной платформой; ею же делается выгрузка).
    source: str = "local"
    snapshot_share: str = ""             # общий файл, откуда берём свежую выгрузку
    snapshot_auto_minutes: int = 60      # как часто проверять источник; 0 = не проверять

    # --- чем доставляем файл между машинами ---
    # 'file'   — общая папка/локальный путь (snapshot_share);
    # 'yadisk' — Яндекс.Диск: машинам не нужно видеть друг друга по сети.
    transport: str = "file"
    yadisk_token: str = ""               # OAuth-токен; в файл пишется зашифрованным
    yadisk_path: str = "app:/cells.db"   # путь файла на Диске

    # --- подключение к 1С (нужно только при source == 'com' и для выгрузки) ---
    kind: str = "server"                 # 'server' | 'file'
    srvr: str = ""                       # сервер 1С (для серверной базы)
    ref: str = ""                        # имя базы на сервере
    file_path: str = ""                  # путь к файловой базе
    progid: str = DEFAULT_PROGID
    usr: str = ""
    password: str = ""                   # в файл пишется зашифрованным

    # --- запросы ---
    query: str = DEFAULT_QUERY
    fallback_query: str = DEFAULT_FALLBACK_QUERY
    use_fallback: bool = True            # искать по артикулу, если ШК не найден

    # --- отбор размещений ---
    warehouses: list[str] = field(default_factory=list)  # пусто = все склады

    # --- печать ---
    printer_name: str = ""               # пусто = принтер по умолчанию Windows
    printer_bound: bool = False           # принтер уже подобран/выбран хоть раз
    label_w_mm: float = DEFAULT_LABEL_W_MM
    label_h_mm: float = DEFAULT_LABEL_H_MM
    auto_print: bool = True              # печатать сразу после сканирования
    auto_print_when_many: bool = True    # если ячеек несколько — печатать основную
    show_name: bool = True               # наименование мелким шрифтом внизу
    show_warehouse: bool = True          # склад мелким шрифтом сверху
    copies: int = 1

    # --- поведение ---
    reconnect_on_start: bool = True      # подключаться к 1С при запуске

    def __post_init__(self) -> None:
        # Токен чистим при любом способе задания — из интерфейса, из утилиты или
        # напрямую. Иначе в settings.json оседает вставленный адрес целиком: он
        # работает (клиент чистит его на лету), но в журнале и при разборе
        # проблем выглядит непонятно.
        if self.yadisk_token:
            from .yadisk import normalize_token
            self.yadisk_token = normalize_token(self.yadisk_token)

    # ------------------------------------------------------------------
    def conn_string(self) -> str:
        """Строка соединения для ``V83.COMConnector.Connect``.

        Значения экранируются (внутренняя кавычка удваивается), поэтому пароль
        или путь с кавычкой не ломают строку.
        """
        q = lambda v: (v or "").replace('"', '""')  # noqa: E731
        if self.kind == "file":
            if not self.file_path:
                raise ValueError("Для файловой базы укажите путь к каталогу базы.")
            return f'File="{q(self.file_path)}";Usr="{q(self.usr)}";Pwd="{q(self.password)}";'
        if not self.srvr or not self.ref:
            raise ValueError("Для серверной базы укажите сервер и имя базы.")
        return (f'Srvr="{q(self.srvr)}";Ref="{q(self.ref)}";'
                f'Usr="{q(self.usr)}";Pwd="{q(self.password)}";')

    def describe_base(self) -> str:
        """Короткое описание базы 1С для строки состояния (без учётных данных)."""
        if self.kind == "file":
            return self.file_path or "<база не выбрана>"
        if self.srvr and self.ref:
            return f"{self.ref} ({self.srvr})"
        return "<база не выбрана>"

    def is_configured(self) -> bool:
        """Готово ли подключение к 1С (проверяется независимо от выбранного источника)."""
        try:
            self.conn_string()
        except ValueError:
            return False
        return True

    @property
    def uses_local_db(self) -> bool:
        return self.source == "local"

    def local_db_path(self) -> Path:
        """Рабочая копия базы на этой машине.

        Приложение всегда читает локальную копию, а не файл в общей папке: сеть
        может отвалиться посреди смены, да и держать SQLite открытым по SMB —
        плохая идея. Из общей папки файл только копируется, целиком.
        """
        return paths.app_data_dir() / "cells.db"

    @property
    def uses_yadisk(self) -> bool:
        return self.transport == "yadisk"

    def describe_transport(self) -> str:
        """Откуда берётся файл — для строки состояния и журнала."""
        if self.uses_yadisk:
            return f"Яндекс.Диск ({self.yadisk_path})"
        return self.snapshot_share or "<источник не указан>"

    def ready_to_work(self) -> tuple[bool, str]:
        """Можно ли работать прямо сейчас; вторым значением — чего не хватает."""
        if self.uses_local_db:
            if self.local_db_path().exists():
                return True, ""      # база с прошлой смены — работать можно
            if self.uses_yadisk:
                if self.yadisk_token:
                    return True, ""
                return False, ("Не задан токен Яндекс.Диска. Откройте «Настройки» → "
                               "«Источник данных».")
            if self.snapshot_share:
                return True, ""
            return False, ("Не указан файл с выгрузкой. Откройте «Настройки» → "
                           "«Источник данных».")
        if self.is_configured():
            return True, ""
        return False, "База 1С не настроена. Откройте «Настройки» и укажите подключение."

    # ------------------------------------------------------------------
    def save(self, path: Path | None = None) -> None:
        path = path or paths.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        # Секреты в файл попадают только зашифрованными (DPAPI, см. app.secrets).
        for plain, enc_key in (("password", "password_enc"),
                               ("yadisk_token", "yadisk_token_enc")):
            value = data.pop(plain, "")
            try:
                data[enc_key] = secrets.encrypt_to_b64(value)
            except RuntimeError as e:  # DPAPI недоступен — секрет просто не сохраняем
                log.warning("Секрет %s не сохранён (нет DPAPI): %s", plain, e)
                data[enc_key] = ""
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # атомарная запись: настройки не бьются при сбое

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or paths.settings_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("Не читаются настройки (%s) — беру значения по умолчанию", e)
            return cls()
        encrypted = {plain: data.pop(enc_key, "")
                     for plain, enc_key in (("password", "password_enc"),
                                            ("yadisk_token", "yadisk_token_enc"))}
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        obj = cls(**kwargs)
        for plain, blob in encrypted.items():
            if not blob:
                continue
            try:
                setattr(obj, plain, secrets.decrypt_from_b64(blob))
            except Exception as e:  # noqa: BLE001 - чужой профиль/машина
                log.warning("Секрет %s не расшифрован (%s) — введите заново", plain, e)
        return obj


def list_known_bases() -> list[tuple[str, str]]:
    """Базы из списка 1С (``%APPDATA%\\1C\\1CEStart\\ibases.v8i``).

    Возвращает пары (имя, строка Connect) — из них диалог настроек заполняет
    сервер/базу, чтобы их не набирать руками. Файл в UTF-8 с BOM, секции вида
    ``[Имя]`` + ``Connect=Srvr="...";Ref="...";``.
    """
    import os

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    f = Path(appdata) / "1C" / "1CEStart" / "ibases.v8i"
    if not f.exists():
        return []
    out: list[tuple[str, str]] = []
    name = ""
    try:
        for line in f.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                name = line[1:-1]
            elif line.lower().startswith("connect=") and name:
                out.append((name, line.split("=", 1)[1]))
                name = ""
    except OSError as e:
        log.warning("Список баз 1С не прочитан: %s", e)
    return out


def parse_connect_string(connect: str) -> dict[str, str]:
    """Разобрать ``Srvr="x";Ref="y";`` / ``File="...";`` в поля настроек."""
    out: dict[str, str] = {}
    for part in connect.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip().lower()] = value.strip().strip('"')
    return out
