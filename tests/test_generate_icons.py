from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from scripts.generate_icons import MissingIconError, generate_icons


def test_generate_icons_missing_source_reports_clear_error(tmp_path: Path):
    with pytest.raises(MissingIconError, match="Missing icon source:"):
        generate_icons(tmp_path)


def test_generate_icons_outputs_png_ico_and_public_assets(tmp_path: Path):
    source = tmp_path / "icon.png"
    Image.new("RGBA", (64, 48), (7, 22, 49, 255)).save(source)

    generated = generate_icons(tmp_path)
    expected = [
        tmp_path / "desktop" / "assets" / "icons" / "sensorarray-icon-256.png",
        tmp_path / "desktop" / "assets" / "icons" / "sensorarray-icon.ico",
        tmp_path / "desktop" / "public" / "favicon.ico",
        tmp_path / "desktop" / "public" / "icon-192.png",
        tmp_path / "desktop" / "public" / "icon-512.png",
    ]
    for path in expected:
        assert path in generated
        assert path.exists()
        assert path.stat().st_size > 0

    with Image.open(tmp_path / "desktop" / "assets" / "icons" / "sensorarray-icon-256.png") as image:
        assert image.size == (256, 256)
