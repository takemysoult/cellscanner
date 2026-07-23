"""Сгенерировать иконку приложения (``cellscanner.ico``, файл коммитится).

Синяя плитка со штрихкодом: узнаётся с первого взгляда и не превращается в кашу
на 16 пикселях, в отличие от мелких деталей. Запускается один раз:

    .venv\\Scripts\\python packaging\\make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

PRIMARY = (26, 115, 232)      # #1a73e8 — тот же акцент, что и в интерфейсе
WHITE = (255, 255, 255, 255)
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Ширины штрихов в долях от ширины поля: чередование толстых и тонких читается
# как штрихкод даже когда картинка ужата до 16 px.
BARS = [3, 1, 2, 1, 1, 3, 1, 2]


def _render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    d.rounded_rectangle([pad, pad, size - pad - 1, size - pad - 1],
                        radius=max(2, size // 5), fill=PRIMARY)

    # Поле под штрихи: с отступами от краёв плитки.
    left, right = size * 0.22, size * 0.78
    top, bottom = size * 0.24, size * 0.76
    unit = (right - left) / sum(BARS + [1] * (len(BARS) - 1))  # штрихи + промежутки

    x = left
    for i, w in enumerate(BARS):
        bar_w = w * unit
        if i % 2 == 0:  # чётные — сами штрихи, нечётные уходят в пробел
            d.rectangle([x, top, x + bar_w, bottom], fill=WHITE)
        x += bar_w + unit
    return img


def main() -> None:
    out = Path(__file__).with_name("cellscanner.ico")
    _render(256).save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"записано: {out}")


if __name__ == "__main__":
    main()
