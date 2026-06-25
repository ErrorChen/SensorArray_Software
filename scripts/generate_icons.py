from __future__ import annotations

import sys
from pathlib import Path

ICON_SIZES = (16, 24, 32, 48, 64, 128, 192, 256, 512, 1024)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        generated = generate_icons(repo_root)
    except MissingIconError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except MissingPillowError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for path in generated:
        print(_describe_output(path))
    return 0


def generate_icons(repo_root: Path) -> list[Path]:
    image_module = _load_pillow()
    Image = image_module
    source = repo_root / "icon.png"
    if not source.exists():
        raise MissingIconError(f"Missing icon source: {source}")

    icons_dir = repo_root / "desktop" / "assets" / "icons"
    public_dir = repo_root / "desktop" / "public"
    icons_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as source_image:
        square = _center_crop_square(source_image.convert("RGBA"), Image)
        generated: list[Path] = []
        resized: dict[int, object] = {}
        for size in ICON_SIZES:
            icon_image = square.resize((size, size), Image.Resampling.LANCZOS)
            resized[size] = icon_image
            path = icons_dir / f"sensorarray-icon-{size}.png"
            icon_image.save(path, "PNG")
            generated.append(path)

        default_png = icons_dir / "sensorarray-icon.png"
        resized[1024].save(default_png, "PNG")
        generated.append(default_png)

        ico_path = icons_dir / "sensorarray-icon.ico"
        resized[256].save(ico_path, format="ICO", sizes=[(size, size) for size in ICO_SIZES])
        generated.append(ico_path)

        favicon_path = public_dir / "favicon.ico"
        resized[256].save(favicon_path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
        generated.append(favicon_path)

        for size in (192, 512):
            path = public_dir / f"icon-{size}.png"
            resized[size].save(path, "PNG")
            generated.append(path)
    return generated


class MissingIconError(RuntimeError):
    pass


class MissingPillowError(RuntimeError):
    pass


def _load_pillow():
    try:
        from PIL import Image
    except ImportError as exc:
        raise MissingPillowError("Pillow is required. Install with: python -m pip install Pillow") from exc
    return Image


def _center_crop_square(image, image_module):
    width, height = image.size
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    cropped = image.crop((left, top, left + edge, top + edge))
    if cropped.size == (edge, edge):
        return cropped
    return cropped.resize((edge, edge), image_module.Resampling.LANCZOS)


def _describe_output(path: Path) -> str:
    size_bytes = path.stat().st_size
    try:
        from PIL import Image

        with Image.open(path) as image:
            dimensions = f"{image.size[0]}x{image.size[1]}"
    except Exception:
        dimensions = "unknown"
    return f"{path} {dimensions} {size_bytes} bytes"


if __name__ == "__main__":
    raise SystemExit(main())
