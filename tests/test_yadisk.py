"""Транспорт через Яндекс.Диск — на подставном HTTP, без обращения к сети."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import yadisk
from app.config import Settings
from app.snapshot import Snapshot, SnapshotBuilder, SnapshotError, pull, push
from app.yadisk import YaDisk, YaDiskError, normalize_token, redact

TOKEN = "y0_SEKRETNYJ_TOKEN_DOSTATOCHNO_DLINNYJ"


class FakeResponse:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("не JSON")
        return self._payload

    def iter_content(self, chunk_size=0):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRequests:
    """Подставной requests: хранит «облако» в словаре и пишет журнал вызовов."""

    class RequestException(Exception):
        pass

    def __init__(self):
        self.cloud: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.fail_on: str | None = None

    def _maybe_fail(self, what):
        if self.fail_on == what:
            raise self.RequestException("сеть отвалилась")

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        params = params or {}
        self.calls.append(f"GET {url}")
        if url.endswith("/disk/"):
            return FakeResponse(200, {"used_space": 1 << 30, "total_space": 10 << 30})
        if url.endswith("/resources/upload"):
            return FakeResponse(200, {"href": "https://up.example/" + params["path"]})
        if url.endswith("/resources/download"):
            path = params["path"]
            if path not in self.cloud:
                return FakeResponse(404, {"message": "не найдено"})
            return FakeResponse(200, {"href": "https://down.example/" + path})
        if url.endswith("/resources"):
            path = params["path"]
            if path.endswith("/"):          # папка: проверка доступа
                return FakeResponse(200, {"type": "dir", "path": path})
            if path not in self.cloud:
                return FakeResponse(404, {"message": "не найдено"})
            return FakeResponse(200, {"modified": "2026-07-23T10:00:00+00:00"})
        if url.startswith("https://down.example/"):
            self._maybe_fail("download")
            return FakeResponse(200, content=self.cloud[url.split("/", 3)[3]])
        return FakeResponse(404, {})

    def put(self, url, data=None, headers=None, params=None, timeout=None):
        self.calls.append(f"PUT {url}")
        if url.startswith("https://up.example/"):
            self._maybe_fail("upload")
            self.cloud[url.split("/", 3)[3]] = data.read()
            return FakeResponse(201)
        return FakeResponse(201)          # создание папки

    def post(self, url, headers=None, params=None, timeout=None):
        self.calls.append(f"POST {url}")
        if url.endswith("/resources/move"):
            self.cloud[params["path"]] = self.cloud.pop(params["from"])
            return FakeResponse(201)
        return FakeResponse(201)


@pytest.fixture
def fake(monkeypatch):
    f = FakeRequests()
    monkeypatch.setattr(yadisk, "_requests", lambda: f)
    return f


@pytest.fixture
def snapshot_file(tmp_path):
    b = SnapshotBuilder()
    code = b.add_barcode("2000463720017", "31349756", "Volvo 31349756")
    b.add_barcode_cell(code, "1-3-3-5-2", "Оригинал", True)
    return b.write(tmp_path / "export" / "cells.db")


# --- клиент ---------------------------------------------------------------
def test_check_confirms_folder_access(fake):
    assert "доступ" in YaDisk(TOKEN).check()


def test_check_probes_folder_not_whole_disk(fake):
    """С правами «только папка приложения» запрос всего диска даёт 403.

    Именно на этом проверка однажды сообщила «нет доступа» про рабочий токен.
    """
    YaDisk(TOKEN).check("app:/cells.db")
    folder_calls = [c for c in fake.calls if c.endswith("/resources")]
    assert folder_calls, "проверка обязана спрашивать папку через /resources"


def test_missing_token_is_explained(fake):
    with pytest.raises(YaDiskError, match="Токен не введён"):
        YaDisk("").check()


def test_non_ascii_token_is_rejected_cleanly(fake):
    """Кириллица в токене роняла бы http.client UnicodeEncodeError (заголовки latin-1)."""
    with pytest.raises(YaDiskError, match="нелатинские"):
        YaDisk("ЗАВЕДОМОНЕВЕРНЫЙТОКЕНДЛИННЫЙОЧЕНЬ").check()


def test_ctrl_v_paste_is_recognised(fake):
    """Ctrl+V в консоли Windows вписывает символ 0x16 вместо вставки.

    Именно так в настройки однажды сохранился токен длиной в один символ, а
    Яндекс ответил невнятной «ошибкой 400». Теперь причина называется прямо.
    """
    with pytest.raises(YaDiskError, match="вставка не сработала"):
        YaDisk("\x16").check()


def test_short_token_explains_paste_problem(fake):
    with pytest.raises(YaDiskError, match="слишком короткий"):
        YaDisk("y0_abc").check()


def test_token_without_prefix_is_flagged(fake):
    with pytest.raises(YaDiskError, match="y0_"):
        YaDisk("abcdefghijklmnopqrstuvwxyz0123").check()


def test_bad_token_does_not_leak_raw_exception(fake, snapshot_file):
    """Ни одна операция не должна выпустить наружу не-YaDiskError."""
    for call in (lambda: YaDisk("кириллица").check(),
                 lambda: YaDisk("\x16").upload(snapshot_file, "app:/cells.db"),
                 lambda: YaDisk("").modified_at("app:/cells.db")):
        with pytest.raises(YaDiskError):
            call()


# --- очистка вставленного значения ---------------------------------------
GOOD = "y0_AgAAAAAfakefakefakeFAKEfakefakeFAKE"


@pytest.mark.parametrize("pasted", [
    GOOD,
    f"  {GOOD}  ",
    f"{GOOD}&token_type=bearer&expires_in=31536000",
    f"#access_token={GOOD}&token_type=bearer",
    f"https://oauth.yandex.ru/verification_codes#access_token={GOOD}"
    "&token_type=bearer&expires_in=31536000&cid=2m5jy33aq4nb1u8c1fa56d7cmm",
])
def test_normalize_token_extracts_from_pasted_url(pasted):
    """Из адресной строки токен вытаскивается сам — это самая частая вставка."""
    assert normalize_token(pasted) == GOOD


def test_normalize_token_strips_control_characters():
    assert normalize_token("\x16") == ""


def test_settings_store_clean_token(app_data):
    """В settings.json оседает токен, а не вставленный адрес целиком."""
    s = Settings(yadisk_token=f"https://oauth.yandex.ru/verification_codes"
                              f"#access_token={GOOD}&token_type=bearer")
    assert s.yadisk_token == GOOD
    s.save()
    assert Settings.load().yadisk_token == GOOD


def test_client_accepts_pasted_url(fake):
    """Полный адрес в поле токена должен просто работать."""
    assert "доступ" in YaDisk(
        f"https://oauth.yandex.ru/verification_codes#access_token={TOKEN}"
        "&token_type=bearer").check()


def test_upload_then_download_roundtrip(fake, snapshot_file, tmp_path):
    client = YaDisk(TOKEN)
    client.upload(snapshot_file, "app:/cells.db")
    out = tmp_path / "downloaded.db"
    client.download("app:/cells.db", out)
    assert out.read_bytes() == snapshot_file.read_bytes()


def test_upload_goes_through_temp_name(fake, snapshot_file):
    """Заливка идёт в .part и переименовывается — получатель не увидит обрубок."""
    YaDisk(TOKEN).upload(snapshot_file, "app:/cells.db")
    assert any("cells.db.part" in c for c in fake.calls)
    assert any("/resources/move" in c for c in fake.calls)
    assert "app:/cells.db" in fake.cloud
    assert "app:/cells.db.part" not in fake.cloud


def test_download_of_missing_file_is_explained(fake, tmp_path):
    with pytest.raises(YaDiskError, match="404|не найден"):
        YaDisk(TOKEN).download("app:/нет.db", tmp_path / "x.db")


def test_modified_at_returns_empty_when_absent(fake):
    assert YaDisk(TOKEN).modified_at("app:/нет.db") == ""


def test_broken_download_leaves_no_partial(fake, snapshot_file, tmp_path):
    YaDisk(TOKEN).upload(snapshot_file, "app:/cells.db")
    fake.fail_on = "download"
    out = tmp_path / "out.db"
    with pytest.raises(YaDiskError):
        YaDisk(TOKEN).download("app:/cells.db", out)
    assert not out.exists()
    assert not out.with_name(out.name + ".part").exists()


# --- секрет не должен утекать --------------------------------------------
def test_redact_hides_token():
    assert TOKEN not in redact(f"ошибка с OAuth {TOKEN} внутри", TOKEN)


def test_network_error_text_has_no_token(fake, snapshot_file):
    fake.fail_on = "upload"
    with pytest.raises(YaDiskError) as e:
        YaDisk(TOKEN).upload(snapshot_file, "app:/cells.db")
    assert TOKEN not in str(e.value)


def test_token_is_encrypted_in_settings_file(app_data):
    Settings(yadisk_token=TOKEN).save()
    raw = Settings.load(), (app_data / "settings.json").read_text(encoding="utf-8")
    assert TOKEN not in raw[1], "токен не должен лежать в файле открытым текстом"
    assert raw[0].yadisk_token == TOKEN, "но должен расшифровываться обратно"


# --- push/pull поверх транспорта -----------------------------------------
def test_push_skips_reupload_of_same_file(fake, snapshot_file, app_data):
    """Досыл ходит раз в час — он не должен каждый раз лить те же 5 МБ."""
    s = Settings(transport="yadisk", yadisk_token=TOKEN, yadisk_path="app:/cells.db")
    push(s, snapshot_file)
    fake.calls.clear()
    where = push(s, snapshot_file)
    assert "актуален" in where
    assert not any("up.example" in c for c in fake.calls), "повторная заливка"


def test_push_uploads_again_after_new_export(fake, snapshot_file, app_data):
    s = Settings(transport="yadisk", yadisk_token=TOKEN, yadisk_path="app:/cells.db")
    push(s, snapshot_file)

    b = SnapshotBuilder()
    b.add_barcode("111", "ART", "Новый товар")
    import time
    time.sleep(0.01)
    b.write(snapshot_file)                      # выгрузка обновилась
    fake.calls.clear()
    push(s, snapshot_file)
    assert any("up.example" in c for c in fake.calls), "новый снимок обязан уехать"


def test_push_reuploads_if_file_vanished_from_disk(fake, snapshot_file, app_data):
    """Файл удалили на Диске вручную — досыл обязан залить заново."""
    s = Settings(transport="yadisk", yadisk_token=TOKEN, yadisk_path="app:/cells.db")
    push(s, snapshot_file)
    fake.cloud.clear()
    fake.calls.clear()
    push(s, snapshot_file)
    assert any("up.example" in c for c in fake.calls)


def test_push_and_pull_via_yadisk(fake, snapshot_file, tmp_path, app_data):
    s = Settings(transport="yadisk", yadisk_token=TOKEN, yadisk_path="app:/cells.db")
    assert "Яндекс.Диск" in push(s, snapshot_file)

    target = tmp_path / "local" / "cells.db"
    assert pull(s, target) is True
    snap = Snapshot(target)
    snap.open()
    try:
        assert snap.lookup("2000463720017", True, []).placements[0].cell == "1-3-3-5-2"
    finally:
        snap.close()


def test_pull_skips_download_when_unchanged(fake, snapshot_file, tmp_path, app_data):
    """Второй раз качать те же 5 МБ незачем — сверяем отметку времени."""
    s = Settings(transport="yadisk", yadisk_token=TOKEN, yadisk_path="app:/cells.db")
    push(s, snapshot_file)
    target = tmp_path / "local" / "cells.db"
    pull(s, target)
    fake.calls.clear()
    assert pull(s, target) is False
    assert not any("down.example" in c for c in fake.calls)


def test_pull_without_uploaded_file_explains(fake, tmp_path, app_data):
    s = Settings(transport="yadisk", yadisk_token=TOKEN, yadisk_path="app:/cells.db")
    with pytest.raises(SnapshotError, match="нет файла"):
        pull(s, tmp_path / "local" / "cells.db")


def test_corrupt_download_does_not_destroy_working_db(fake, snapshot_file, tmp_path,
                                                      app_data):
    """Битый файл в облаке не должен затирать вчерашнюю рабочую базу."""
    s = Settings(transport="yadisk", yadisk_token=TOKEN, yadisk_path="app:/cells.db")
    push(s, snapshot_file)
    target = tmp_path / "local" / "cells.db"
    pull(s, target)
    good = target.read_bytes()

    fake.cloud["app:/cells.db"] = "это не база данных".encode("utf-8")
    target.with_name(target.name + ".stamp").unlink()      # заставить скачать заново
    with pytest.raises(SnapshotError):
        pull(s, target)
    assert target.read_bytes() == good, "рабочая база обязана уцелеть"


def test_describe_transport_mentions_yadisk():
    s = Settings(transport="yadisk", yadisk_path="app:/cells.db")
    assert "Яндекс.Диск" in s.describe_transport()
