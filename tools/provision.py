"""Первичная настройка машины: подключение к 1С, транспорт и токен Яндекс.Диска.

Заполняет ``%LOCALAPPDATA%\\CellScanner\\settings.json``, чтобы дальше
``export_cells.py`` работал по расписанию без ручного ввода, а приложение знало,
откуда брать выгрузку.

Параметры подключения к 1С берутся из соседнего приложения ZZap Sync, где эта же
база уже настроена и проверена (пароль там лежит зашифрованным DPAPI и
расшифровывается только под той же учётной записью Windows).

Токен Яндекс.Диска запрашивается интерактивно и на экране не отображается; в
settings.json он тоже попадает только зашифрованным. Указывать его аргументом
командной строки не стоит — он осел бы в истории команд.

Машина выгрузки (есть платформа 1С):
    .venv\\Scripts\\python tools\\provision.py --yadisk --source com

Складской ПК (только сканер и принтер):
    .venv\\Scripts\\python tools\\provision.py --yadisk --source local

Общая папка вместо Яндекс.Диска:
    .venv\\Scripts\\python tools\\provision.py --share "\\\\СКЛАД-ПК\\CellScanner\\cells.db"
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app import paths  # noqa: E402
from app.config import Settings  # noqa: E402

ZZAP_DB = Path(r"C:\Users\Admin\AppData\Local\ZZapSync\zzapsync.db")


def from_zzap() -> dict | None:
    """Забрать проверенное подключение из ZZap Sync (или None, если его нет)."""
    if not ZZAP_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{ZZAP_DB}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT srvr, ref, progid, usr, password_enc FROM connection_1c "
            "WHERE is_default = 1").fetchone()
        conn.close()
    except sqlite3.Error as e:
        print(f"ZZap Sync: база не читается ({e})")
        return None
    if row is None:
        return None
    srvr, ref, progid, usr, enc = row
    password = ""
    if enc:
        try:
            import win32crypt
            password = bytes(win32crypt.CryptUnprotectData(
                bytes(enc), None, None, None, 0)[1]).decode("utf-8")
        except Exception as e:  # noqa: BLE001 - чужой профиль/машина
            print(f"ZZap Sync: пароль не расшифрован ({e}) — введите его в приложении")
    return {"srvr": srvr, "ref": ref, "progid": progid, "usr": usr,
            "password": password}


def ask_token() -> str:
    """Спросить токен Яндекс.Диска и убедиться, что вставка удалась.

    Через аргумент командной строки токен не принимаем: он остался бы в истории
    команд и в журнале планировщика. Ввод не скрываем: в консоли Windows при
    скрытом вводе Ctrl+V вставляет не текст, а управляющий символ 0x16 — молча
    сохранённый мусор потом выглядит как непонятная ошибка 400 от Яндекса.
    Показанный на экране токен — меньшее зло, чем несколько часов поисков.
    """
    from app.yadisk import describe_token_problem, normalize_token

    print()
    print("Вставьте токен Яндекс.Диска и нажмите Enter.")
    print("Это часть адреса после access_token= и до знака &, начинается на y0_")
    print("Вставлять в консоли: правой кнопкой мыши или Ctrl+Shift+V (Ctrl+V не работает).")
    print("Можно вставить и весь адрес целиком — токен из него будет выделен.")

    for attempt in range(3):
        raw = input("Токен: ")
        token = normalize_token(raw)
        problem = describe_token_problem(token)
        if not problem:
            print(f"Принят токен длиной {len(token)} символов "
                  f"({token[:6]}…{token[-4:]}).")
            return token
        print(f"  {problem}")
        if attempt < 2:
            print("  Попробуйте ещё раз (или Ctrl+C для выхода).")
    print("Токен не принят — оставляю прежний.")
    return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Первичная настройка машины")
    p.add_argument("--share", default="",
                   help=r"путь к общему файлу, напр. \\СКЛАД-ПК\CellScanner\cells.db")
    p.add_argument("--yadisk", action="store_true",
                   help="транспорт — Яндекс.Диск; токен будет запрошен интерактивно")
    p.add_argument("--yadisk-path", default="",
                   help="путь файла на Диске (по умолчанию app:/cells.db)")
    p.add_argument("--source", choices=("com", "local"), default="com",
                   help="com — машина выгрузки (есть платформа 1С); "
                        "local — складской ПК (читает готовый файл)")
    p.add_argument("--srvr", default="")
    p.add_argument("--ref", default="")
    p.add_argument("--usr", default="")
    p.add_argument("--progid", default="")
    args = p.parse_args(argv)

    if not args.share and not args.yadisk:
        p.error("укажите транспорт: --yadisk или --share <путь>")

    paths.ensure_dirs()
    s = Settings.load()
    s.source = args.source
    s.kind = "server"
    if args.yadisk:
        s.transport = "yadisk"
        if args.yadisk_path:
            s.yadisk_path = args.yadisk_path
        token = ask_token()
        if token:
            s.yadisk_token = token
    if args.share:
        s.transport = "file"
        s.snapshot_share = args.share

    # Подключение к 1С нужно только машине выгрузки; складскому ПК оно ни к чему.
    zzap = from_zzap() if args.source == "com" else None
    if zzap and not args.srvr:
        s.srvr, s.ref, s.usr = zzap["srvr"], zzap["ref"], zzap["usr"]
        s.progid = zzap["progid"] or s.progid
        if zzap["password"]:
            s.password = zzap["password"]
        print(f"Подключение взято из ZZap Sync: {s.describe_base()}")
    if args.srvr:
        s.srvr = args.srvr
    if args.ref:
        s.ref = args.ref
    if args.usr:
        s.usr = args.usr
    if args.progid:
        s.progid = args.progid

    s.save()

    check = Settings.load()
    print(f"Файл настроек   : {paths.settings_path()}")
    print(f"Роль машины     : "
          f"{'выгрузка из 1С' if check.source == 'com' else 'склад (читает файл)'}")
    if check.source == "com":
        print(f"База 1С         : {check.describe_base()}")
        print(f"Пользователь    : {check.usr or '<не задан>'}")
        print(f"Пароль сохранён : {bool(check.password)}")
    print(f"Транспорт       : {check.describe_transport()}")
    if check.uses_yadisk:
        print(f"Токен сохранён  : {bool(check.yadisk_token)}")
        # Сразу проверяем токен: лучше узнать о проблеме здесь, чем ночью в задаче.
        from app.yadisk import YaDisk, YaDiskError
        try:
            print(f"Яндекс.Диск     : "
                  f"{YaDisk(check.yadisk_token).check(check.yadisk_path)}")
        except YaDiskError as e:
            print(f"Яндекс.Диск     : ОШИБКА — {e}")
            return 1

    ready, problem = check.ready_to_work()
    print(f"Готово к работе : {ready}{'' if ready else ' — ' + problem}")
    if check.source == "com" and not check.is_configured():
        return 1
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
