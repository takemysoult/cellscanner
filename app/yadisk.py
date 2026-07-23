"""Доставка снимка через Яндекс.Диск (REST API).

Зачем: сервер 1С трогать нельзя, а общая папка в рабочей группе требует возни с
учётными данными Windows. Яндекс.Диск снимает и то, и другое: машинам вообще не
нужно видеть друг друга по сети — достаточно интернета. ПК с платформой 1С
загружает файл, складской ПК скачивает.

Десктопный клиент Яндекс.Диска не нужен: работаем напрямую с API по HTTPS.

Авторизация — OAuth-токен, он же «токен приложения». Токен вводится
пользователем в настройках приложения и хранится зашифрованным (DPAPI), как и
пароль 1С. В журнал он не попадает: :func:`redact` вычищает его из текстов ошибок.

Загрузка идёт во временное имя и переименовывается на месте — скачивающая
сторона никогда не увидит наполовину загруженный файл.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
TIMEOUT = 60          # обычный запрос к API
IO_TIMEOUT = 300      # заливка/скачивание пяти мегабайт по медленному каналу

# Путь по умолчанию: папка приложения на Диске. Префикс `app:/` означает
# «личная папка этого приложения», к остальным файлам Диска доступа нет —
# так безопаснее, чем выдавать токен на весь диск.
DEFAULT_REMOTE = "app:/cells.db"


class YaDiskError(RuntimeError):
    """Ошибка обмена с Яндекс.Диском (текст уже очищен от токена)."""


def redact(text: str, token: str) -> str:
    """Убрать токен из текста ошибки перед записью в журнал или показом."""
    if token and token in text:
        text = text.replace(token, "***")
    return text


def normalize_token(raw: str) -> str:
    """Вытащить чистый токен из того, что человек скопировал.

    Яндекс отдаёт токен в адресной строке, поэтому в поле попадает то весь адрес,
    то хвост вида ``y0_xxx&token_type=bearer&expires_in=...``. Разбираем оба
    случая, а заодно убираем управляющие символы: при скрытом вводе в консоли
    Windows нажатие Ctrl+V не вставляет текст, а вписывает символ 0x16, и без
    очистки в настройки сохранялся бы мусор.
    """
    text = (raw or "").strip()
    # Управляющие символы (в т.ч. 0x16 от Ctrl+V) осмысленного значения не несут.
    text = "".join(ch for ch in text if ch.isprintable() or ch.isspace()).strip()
    if "access_token=" in text:
        text = text.split("access_token=", 1)[1]
    for sep in ("&", "#", "?"):
        if sep in text:
            text = text.split(sep, 1)[0]
    return text.strip()


def describe_token_problem(token: str) -> str:
    """Почему этот токен заведомо не сработает; "" — с виду годный.

    Проверяем до обращения к сети: понятная подсказка полезнее, чем «ошибка 400»
    от Яндекса.
    """
    if not token:
        return ("Токен не введён. Похоже, вставка не сработала: в консоли Windows "
                "Ctrl+V не вставляет текст при скрытом вводе — нажмите правую "
                "кнопку мыши либо Ctrl+Shift+V.")
    try:
        token.encode("latin-1")
    except UnicodeEncodeError:
        return ("Токен содержит нелатинские символы. Скопируйте его заново — "
                "токен состоит только из латинских букв, цифр, дефисов и знаков "
                "подчёркивания.")
    if any(ch.isspace() for ch in token):
        return "Токен содержит пробел или перенос строки — скопирован лишний текст."
    if len(token) < 20:
        return (f"Токен слишком короткий ({len(token)} симв.). Похоже, вставка не "
                "сработала: в консоли Windows Ctrl+V не вставляет текст при "
                "скрытом вводе — нажмите правую кнопку мыши либо Ctrl+Shift+V.")
    if not token.startswith("y0_"):
        return ("Токен обычно начинается с «y0_». Проверьте, что скопирована "
                "именно часть после access_token= и до знака &.")
    return ""


def _requests():
    try:
        import requests
    except ImportError as e:  # pragma: no cover - зависит от окружения
        raise YaDiskError(
            "Нужна библиотека requests. Установите: pip install requests") from e
    return requests


def _describe(response, token: str) -> str:
    """Ответ API -> понятное сообщение (у Яндекса оно лежит в JSON-поле message)."""
    try:
        data = response.json()
        message = data.get("message") or data.get("description") or ""
    except ValueError:
        message = ""
    status = response.status_code
    if status == 401:
        return "Яндекс.Диск отклонил токен (401). Проверьте токен в настройках."
    if status == 403:
        return ("Нет доступа (403): у токена нет прав на этот путь. "
                "Проверьте, что приложению разрешён доступ к папке.")
    if status == 404:
        return "Файл на Яндекс.Диске не найден (404). Выгрузка ещё не загружалась?"
    if status == 507:
        return "На Яндекс.Диске закончилось место (507)."
    if status == 429:
        return "Слишком частые обращения к Яндекс.Диску (429). Повторите позже."
    return redact(f"Яндекс.Диск вернул ошибку {status}. {message}".strip(), token)


class YaDisk:
    """Минимальный клиент Яндекс.Диска: загрузить файл, скачать файл, проверить связь."""

    def __init__(self, token: str) -> None:
        # Чистим сразу: в поле часто попадает кусок адресной строки целиком.
        self.token = normalize_token(token)

    def _headers(self) -> dict:
        # Заведомо негодный токен отсеиваем до сети: понятная подсказка полезнее,
        # чем «ошибка 400» от Яндекса. Заодно это защищает от UnicodeEncodeError
        # внутри http.client — заголовки кодируются latin-1.
        problem = describe_token_problem(self.token)
        if problem:
            raise YaDiskError(problem)
        return {"Authorization": f"OAuth {self.token}",
                "Accept": "application/json"}

    def _get(self, url: str, **params):
        requests = _requests()
        try:
            return requests.get(url, headers=self._headers(), params=params or None,
                                timeout=TIMEOUT)
        except requests.RequestException as e:
            raise YaDiskError(redact(f"Нет связи с Яндекс.Диском: {e}",
                                     self.token)) from e

    # --- операции ---------------------------------------------------------
    def _folder_of(self, remote: str) -> str:
        parent = remote.rsplit("/", 1)[0]
        return parent + "/" if parent.endswith(":") else (parent or "app:/")

    def check(self, remote: str = DEFAULT_REMOTE) -> str:
        """Проверить, что токен даёт доступ к нужной папке.

        Проверяем именно папку, а не диск целиком: с правами «доступ к папке
        приложения» (а мы просим только их) запрос общей информации о диске
        возвращает 403, и это выглядело бы как неверный токен, хотя он рабочий.
        """
        folder = self._folder_of(remote)
        r = self._get(API + "/resources", path=folder, limit=0)
        if r.status_code == 200:
            return f"доступ к папке {folder} есть{self._space_hint()}"
        if r.status_code == 404:
            # Папки ещё нет — она создастся при первой выгрузке. Права при этом
            # в порядке: отсутствие прав дало бы 403.
            return f"доступ есть, папка {folder} пока пуста{self._space_hint()}"
        raise YaDiskError(_describe(r, self.token))

    def _space_hint(self) -> str:
        """Сколько свободно — если токену разрешено это спрашивать (не обязательно)."""
        try:
            r = self._get(API + "/")
            if r.status_code != 200:
                return ""
            data = r.json()
            free = (int(data.get("total_space", 0))
                    - int(data.get("used_space", 0))) / 1024 ** 3
            return f", свободно {free:.1f} ГБ"
        except (YaDiskError, ValueError, TypeError):
            return ""

    def upload(self, local: Path, remote: str) -> None:
        """Залить файл, заменив прежний.

        Сначала во временное имя, потом переименование поверх боевого: если связь
        оборвётся на середине, у скачивающей стороны останется прежний рабочий
        файл, а не обрубок.
        """
        requests = _requests()
        local = Path(local)
        if not local.exists():
            raise YaDiskError(f"Нечего загружать: файла нет ({local})")

        tmp_remote = remote + ".part"
        self._ensure_parent(remote)

        r = self._get(API + "/resources/upload", path=tmp_remote, overwrite="true")
        if r.status_code != 200:
            raise YaDiskError(_describe(r, self.token))
        href = r.json().get("href")
        if not href:
            raise YaDiskError("Яндекс.Диск не выдал ссылку для загрузки.")

        try:
            with local.open("rb") as f:
                put = requests.put(href, data=f, timeout=IO_TIMEOUT)
        except (requests.RequestException, OSError) as e:
            raise YaDiskError(redact(f"Загрузка прервана: {e}", self.token)) from e
        if put.status_code not in (201, 202):
            raise YaDiskError(_describe(put, self.token))

        self._move(tmp_remote, remote)
        log.info("Яндекс.Диск: загружено %s (%.1f МБ)", remote,
                 local.stat().st_size / 1024 ** 2)

    def download(self, remote: str, local: Path) -> None:
        """Скачать файл во временный и подменить одним движением."""
        requests = _requests()
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)

        r = self._get(API + "/resources/download", path=remote)
        if r.status_code != 200:
            raise YaDiskError(_describe(r, self.token))
        href = r.json().get("href")
        if not href:
            raise YaDiskError("Яндекс.Диск не выдал ссылку для скачивания.")

        tmp = local.with_name(local.name + ".part")
        try:
            with requests.get(href, stream=True, timeout=IO_TIMEOUT) as resp:
                if resp.status_code != 200:
                    raise YaDiskError(_describe(resp, self.token))
                with tmp.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
        except (requests.RequestException, OSError) as e:
            tmp.unlink(missing_ok=True)
            raise YaDiskError(redact(f"Скачивание прервано: {e}", self.token)) from e
        tmp.replace(local)
        log.info("Яндекс.Диск: скачано %s -> %s", remote, local)

    def modified_at(self, remote: str) -> str:
        """Время изменения файла на Диске (ISO-строка) или "" если файла нет.

        По нему решается, нужно ли вообще качать пять мегабайт.
        """
        r = self._get(API + "/resources", path=remote, fields="modified")
        if r.status_code == 404:
            return ""
        if r.status_code != 200:
            raise YaDiskError(_describe(r, self.token))
        try:
            return str(r.json().get("modified", ""))
        except ValueError:
            return ""

    # --- вспомогательное --------------------------------------------------
    def _ensure_parent(self, remote: str) -> None:
        """Создать папку назначения, если её ещё нет (409 = уже есть, это норма)."""
        parent = remote.rsplit("/", 1)[0]
        if not parent or parent.endswith(":") or parent in ("app:", "disk:"):
            return
        requests = _requests()
        try:
            requests.put(API + "/resources", headers=self._headers(),
                         params={"path": parent}, timeout=TIMEOUT)
        except requests.RequestException:
            pass  # не смертельно: настоящая ошибка вылезет на самой загрузке

    def _move(self, src: str, dst: str) -> None:
        requests = _requests()
        try:
            r = requests.post(API + "/resources/move", headers=self._headers(),
                              params={"from": src, "path": dst, "overwrite": "true"},
                              timeout=TIMEOUT)
        except requests.RequestException as e:
            raise YaDiskError(redact(f"Не удалось заменить файл: {e}",
                                     self.token)) from e
        if r.status_code not in (201, 202):
            raise YaDiskError(_describe(r, self.token))
