"""Раскладка стикера: подбор шрифта, перенос номера ячейки, вывод в PDF."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.label import (LabelData, LabelStyle, _fit_px, _layout_cell, _split_middle,
                       render_pixmap)
from app.printing import safe_file_stem, save_pdf


def test_split_middle_picks_hyphen_near_centre():
    assert _split_middle("2-3-3-11-1-1") == ["2-3-3-", "11-1-1"]


def test_split_middle_without_hyphen():
    assert _split_middle("А12") is None


def test_split_middle_ignores_edge_hyphen():
    assert _split_middle("-") is None


def test_fit_px_larger_box_gives_larger_font(qapp):
    small = _fit_px(["2-3-6-5-3"], 100, 40, True)
    large = _fit_px(["2-3-6-5-3"], 400, 160, True)
    assert large > small


def test_fit_px_longer_text_gives_smaller_font(qapp):
    short = _fit_px(["А12"], 300, 200, True)
    long_ = _fit_px(["2-3-3-11-1-1"], 300, 200, True)
    assert long_ < short


def test_layout_cell_uses_two_lines_when_that_is_bigger(qapp):
    """Узкая высокая этикетка: в две строки шрифт крупнее, чем в одну."""
    lines, px = _layout_cell("2-3-3-11-1-1", 260, 300)
    assert len(lines) == 2
    assert px > _fit_px(["2-3-3-11-1-1"], 260, 300, True)


def test_layout_cell_keeps_short_text_on_one_line(qapp):
    lines, _ = _layout_cell("А12", 260, 300)
    assert lines == ["А12"]


def test_render_pixmap_keeps_label_proportions(qapp):
    s = Settings(label_w_mm=40, label_h_mm=58)
    pm = render_pixmap(LabelData("1-1-1", "ART"), LabelStyle(), s, target_px_height=580)
    assert pm.height() == 580
    assert pm.width() == pytest.approx(400, abs=2)   # 40x58 мм -> 400x580


def test_save_pdf_writes_file(qapp, tmp_path):
    out = tmp_path / "label.pdf"
    save_pdf(LabelData("2-3-6-5-3", "86617L2300", "KIA 86617L2300", "Оригинал"),
             Settings(), out)
    assert out.exists() and out.stat().st_size > 1000
    assert out.read_bytes().startswith(b"%PDF")


def test_save_pdf_handles_missing_optional_fields(qapp, tmp_path):
    out = tmp_path / "bare.pdf"
    save_pdf(LabelData(cell="1-1-1", article=""), Settings(), out)
    assert out.exists()


@pytest.mark.parametrize("parts,expected", [
    (("2-3-6-5-3", "86617L2300"), "2-3-6-5-3_86617L2300"),
    (("!000382266449/1", "ART"), "_000382266449_1_ART"),
    (("", ""), "label"),
])
def test_safe_file_stem(parts, expected):
    assert safe_file_stem(*parts) == expected
