"""Рисование стикера: один код — три выхода (принтер, PDF, превью на экране).

Этикетка 40 x 58 мм (те самые 400 x 580 в десятых долях мм), книжная ориентация.
Композиция сверху вниз: склад мелко -> ЯЧЕЙКА максимально крупно -> линия ->
артикул -> наименование мелко. Всё, кроме ячейки и артикула, отключается в
настройках.

Размер шрифта не задан числом, а подбирается под ширину этикетки: «2-3-6-5-3» и
«2-3-3-11-1-1» — разной длины, но занять должны одинаково всю ширину. Если в одну
строку получается мелко, номер ячейки разрывается по дефису пополам — двумя
строками шрифт выходит крупнее.

Вывод на устройство — в :mod:`app.printing`; здесь только геометрия и краска.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide2.QtCore import QRectF, Qt
from PySide2.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPixmap

from .config import Settings

FONT_FAMILY = "Arial"          # есть на любой Windows, хорошо читается на термопечати


@dataclass
class LabelData:
    """Что печатаем на одной этикетке."""
    cell: str
    article: str
    name: str = ""
    warehouse: str = ""


@dataclass
class LabelStyle:
    show_name: bool = True
    show_warehouse: bool = True

    @classmethod
    def from_settings(cls, s: Settings) -> "LabelStyle":
        return cls(show_name=s.show_name, show_warehouse=s.show_warehouse)


# --- подбор шрифта --------------------------------------------------------
def _font(px: float, bold: bool) -> QFont:
    f = QFont(FONT_FAMILY)
    f.setPixelSize(max(1, int(px)))
    f.setBold(bold)
    return f


def _advance(fm: QFontMetricsF, text: str) -> float:
    # horizontalAdvance появился в Qt 5.11; на старых сборках остаётся width().
    fn = getattr(fm, "horizontalAdvance", None) or fm.width
    return fn(text)


def _fit_px(lines: list[str], box_w: float, box_h: float, bold: bool) -> float:
    """Максимальный размер шрифта (в пикселях устройства), при котором строки
    целиком помещаются в прямоугольник box_w x box_h."""
    lo, hi, best = 2, max(4, int(box_h) + 2), 2
    while lo <= hi:
        mid = (lo + hi) // 2
        fm = QFontMetricsF(_font(mid, bold))
        widest = max((_advance(fm, ln) for ln in lines), default=0.0)
        if widest <= box_w and fm.height() * len(lines) <= box_h:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def _split_middle(text: str) -> list[str] | None:
    """Разбить номер ячейки по дефису, ближайшему к середине строки."""
    positions = [i for i, ch in enumerate(text) if ch == "-"]
    if not positions:
        return None
    middle = len(text) / 2
    at = min(positions, key=lambda i: abs(i - middle))
    left, right = text[:at], text[at + 1:]
    if not left or not right:
        return None
    return [left + "-", right]


def _layout_cell(text: str, box_w: float, box_h: float) -> tuple[list[str], float]:
    """Выбрать между одной и двумя строками — берём вариант с крупным шрифтом."""
    best_lines, best_px = [text], _fit_px([text], box_w, box_h, True)
    two = _split_middle(text)
    if two:
        px = _fit_px(two, box_w, box_h, True)
        if px > best_px:
            best_lines, best_px = two, px
    return best_lines, best_px


def _draw_lines(painter: QPainter, rect: QRectF, lines: list[str], px: float,
                bold: bool) -> None:
    painter.setFont(_font(px, bold))
    fm = QFontMetricsF(painter.font())
    total = fm.height() * len(lines)
    y = rect.top() + (rect.height() - total) / 2  # блок строк по центру полосы
    for line in lines:
        painter.drawText(QRectF(rect.left(), y, rect.width(), fm.height()),
                         int(Qt.AlignHCenter | Qt.AlignVCenter), line)
        y += fm.height()


def _elide(text: str, box_w: float, px: float) -> str:
    fm = QFontMetricsF(_font(px, False))
    return fm.elidedText(text, Qt.ElideRight, box_w)


# --- собственно этикетка --------------------------------------------------
def draw_label(painter: QPainter, width: float, height: float,
               data: LabelData, style: LabelStyle) -> None:
    """Нарисовать этикетку в прямоугольник width x height пикселей устройства.

    Единый код для принтера, PDF и превью — что видно на экране, то и напечатается.
    """
    painter.save()
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    painter.setPen(QColor(0, 0, 0))

    margin = width * 0.05
    cw = width - 2 * margin
    x = margin

    show_wh = style.show_warehouse and bool(data.warehouse)
    show_nm = style.show_name and bool(data.name)

    # Доли высоты. Освободившееся от отключённых блоков место отдаём ячейке —
    # ради неё всё и затевалось.
    wh_h = height * 0.09 if show_wh else 0.0
    nm_h = height * 0.13 if show_nm else 0.0
    art_h = height * 0.22
    gap = height * 0.02
    top = height * 0.04
    used = top + wh_h + gap + art_h + gap + nm_h + gap + height * 0.04
    cell_h = max(height * 0.20, height - used)

    y = top
    if show_wh:
        px = _fit_px([data.warehouse], cw, wh_h, False)
        _draw_lines(painter, QRectF(x, y, cw, wh_h),
                    [_elide(data.warehouse, cw, px)], px, False)
        y += wh_h + gap

    lines, px = _layout_cell(data.cell or "—", cw, cell_h)
    _draw_lines(painter, QRectF(x, y, cw, cell_h), lines, px, True)
    y += cell_h + gap

    # Разделитель: взгляд не путает номер ячейки с артикулом.
    pen = painter.pen()
    pen.setWidthF(max(1.0, height * 0.004))
    painter.setPen(pen)
    painter.drawLine(QRectF(x, y, cw, 0).topLeft(), QRectF(x, y, cw, 0).topRight())
    y += gap

    art_px = _fit_px([data.article or "—"], cw, art_h, True)
    _draw_lines(painter, QRectF(x, y, cw, art_h), [data.article or "—"], art_px, True)
    y += art_h + gap

    if show_nm:
        px = _fit_px([data.name], cw, nm_h * 0.9, False)
        px = min(px, art_px * 0.5)  # наименование заведомо мельче артикула
        _draw_lines(painter, QRectF(x, y, cw, nm_h),
                    [_elide(data.name, cw, px)], px, False)

    painter.restore()


def render_pixmap(data: LabelData, style: LabelStyle, settings: Settings,
                  target_px_height: int = 520) -> QPixmap:
    """Превью этикетки для экрана (пропорции — как у настоящей)."""
    ratio = settings.label_w_mm / settings.label_h_mm if settings.label_h_mm else 1.0
    h = max(80, target_px_height)
    w = max(60, int(round(h * ratio)))
    pm = QPixmap(w, h)
    pm.fill(Qt.white)
    painter = QPainter(pm)
    try:
        draw_label(painter, w, h, data, style)
    finally:
        painter.end()
    return pm
