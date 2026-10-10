"""Rebuild the original SVG and PNG icons with Pillow 12.3.0.

Run from any directory with: python tools/generate_brand.py
The shared geometry below defines both the SVG source and PNG exports.
No downloaded artwork, fonts, timestamps, or image metadata are used.
"""

from pathlib import Path

from PIL import Image, ImageDraw

BRAND = Path(__file__).resolve().parents[1] / "custom_components/tuya_ble_lock/brand"
COLOR = "#078DA8"
BODY = (16, 110, 240, 244)
BODY_RADIUS = 28
SHACKLE_WIDTH = 24
GLYPH = [(101, 146), (155, 200), (127, 224), (127, 130), (155, 155), (101, 209)]
GLYPH_WIDTH = 10


def render_png(size: int, destination: Path) -> None:
    """Draw at four times the target resolution for smooth transparent edges."""
    factor = size * 4 / 256
    image = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def scaled(points: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(round(value * factor) for value in points)

    width = round(SHACKLE_WIDTH * factor)
    # Pillow strokes fall inside the arc box; SVG strokes straddle their path.
    draw.arc(scaled((58, 4, 198, 144)), 180, 360, fill=COLOR, width=width)
    for x in (70, 186):
        draw.line([scaled((x, 74)), scaled((x, 124))], fill=COLOR, width=width)
    draw.rounded_rectangle(scaled(BODY), radius=round(BODY_RADIUS * factor), fill=COLOR)

    points = [scaled(point) for point in GLYPH]
    draw.line(points, fill="white", width=round(GLYPH_WIDTH * factor), joint="curve")
    radius = GLYPH_WIDTH * factor / 2
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="white")
    image.resize((size, size), Image.Resampling.LANCZOS).save(
        destination, format="PNG", optimize=True, compress_level=9
    )


def main() -> None:
    """Write the vector source and both Home Assistant icon sizes."""
    BRAND.mkdir(parents=True, exist_ok=True)
    points = " ".join(f"{x},{y}" for x, y in GLYPH)
    x, y, right, bottom = BODY
    source = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <title>Tuya BLE Lock</title>
  <desc>An original teal padlock with a white Bluetooth symbol.</desc>
  <path d="M70 124V74a58 58 0 0 1 116 0v50" fill="none" stroke="{COLOR}" stroke-width="{SHACKLE_WIDTH}"/>
  <rect x="{x}" y="{y}" width="{right - x}" height="{bottom - y}" rx="{BODY_RADIUS}" fill="{COLOR}"/>
  <polyline points="{points}" fill="none" stroke="#fff" stroke-width="{GLYPH_WIDTH}" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
'''
    (BRAND / "icon.svg").write_text(source, encoding="utf-8")
    render_png(256, BRAND / "icon.png")
    render_png(512, BRAND / "icon@2x.png")


if __name__ == "__main__":
    main()
