"""Пароль 1С шифруется при хранении через Windows DPAPI (в рамках учётной записи).

``CryptProtectData``/``CryptUnprotectData`` привязывают шифротекст к текущему
пользователю Windows: расшифровать сможет только он и только на этой машине, а
ключей мы у себя не храним. Полученный BLOB кладём в settings.json как base64.

Если DPAPI недоступен (не Windows / нет pywin32), функции бросают RuntimeError —
приложение в этом случае просто работает без сохранённого пароля.
"""
from __future__ import annotations

import base64

# Пишется в поле описания DPAPI-блоба; чисто информационное.
_DESCRIPTION = "cell-scanner secret"


def _win32crypt():
    try:
        import win32crypt
    except ImportError as e:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "DPAPI требует pywin32 (Windows). Установите: pip install pywin32"
        ) from e
    return win32crypt


def encrypt_to_b64(plaintext: str) -> str:
    """Зашифровать строку и вернуть base64 для записи в JSON."""
    if not plaintext:
        return ""
    blob = _win32crypt().CryptProtectData(
        plaintext.encode("utf-8"), _DESCRIPTION, None, None, None, 0)
    return base64.b64encode(bytes(blob)).decode("ascii")


def decrypt_from_b64(value: str) -> str:
    """Расшифровать base64-строку, записанную ``encrypt_to_b64``."""
    if not value:
        return ""
    blob = base64.b64decode(value.encode("ascii"))
    _desc, data = _win32crypt().CryptUnprotectData(blob, None, None, None, 0)
    return bytes(data).decode("utf-8")
