"""Draw logo-j-dark.svg into jmeter-agent.ico, the icon of the Windows desktop shortcut.

The geometry is the SVG's own (a 200 x 200 view box); it is drawn 8x larger and scaled down for smooth edges.
Run it again after changing the logo:  python logo/make_ico.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

SCALE = 8
DARK, WHITE, GREEN = (15, 23, 42, 255), (248, 250, 252, 255), (34, 197, 94, 255)
OFFSET = (8, -4)  # The SVG group's translate.


def point(x, y):
    return ((x + OFFSET[0]) * SCALE, (y + OFFSET[1]) * SCALE)


def quadratic(start, control, end, steps=24):
    return [tuple((1 - t) ** 2 * a + 2 * (1 - t) * t * b + t ** 2 * c for a, b, c in zip(start, control, end))
            for t in (i / steps for i in range(steps + 1))]


def stroke(draw, points, width, color):
    """A polyline with round joins and caps, as stroke-linecap/linejoin="round" draws it."""
    pixels = [point(x, y) for x, y in points]
    radius = width * SCALE / 2
    draw.line(pixels, fill=color, width=round(width * SCALE))
    for x, y in pixels:  # Every vertex, not only the ends: a curve's short segments would otherwise leave notches.
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def logo(size=200) -> Image.Image:
    image = Image.new('RGBA', (size * SCALE, size * SCALE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size * SCALE - 1, size * SCALE - 1), radius=44 * SCALE, fill=DARK)
    stroke(draw, [(96, 48), (152, 48)], 17, WHITE)
    stroke(draw, [(128, 48), (128, 112)] + quadratic((128, 112), (128, 146), (96, 146)), 17, WHITE)
    stroke(draw, [(96, 146), (86, 146), (76, 118), (62, 170), (52, 146), (38, 146)], 15, GREEN)
    return image.resize((256, 256), Image.LANCZOS)


if __name__ == '__main__':
    target = Path(__file__).with_name('jmeter-agent.ico')
    logo().save(target, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(target)
