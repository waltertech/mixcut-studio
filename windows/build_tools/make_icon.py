"""Generate the MixCut Studio application icon as a multi-resolution .ico.

The mark is drawn with primitives only, so the icon set is reproducible without
external assets.  Run with the build virtual environment:

    python make_icon.py <output.ico>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [256, 128, 64, 48, 32, 24, 16]

TOP = (99, 102, 241)      # indigo-500
BOTTOM = (56, 189, 248)   # sky-400
FRAME = (255, 255, 255)
TRIANGLE = (79, 70, 229)  # indigo-600


def _linear_gradient(size, top, bottom):
    image = Image.new('RGB', (size, size))
    pixels = image.load()
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        for x in range(size):
            pixels[x, y] = color
    return image


def _rounded_mask(size, radius):
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _draw_mark(draw, size):
    """A film frame with sprocket holes plus a play triangle."""
    unit = size / 100.0

    def box(x, y, w, h, radius=0, fill=FRAME):
        draw.rounded_rectangle([x * unit, y * unit, (x + w) * unit, (y + h) * unit],
                               radius=radius * unit, fill=fill)

    # Film body with rounded corners.
    box(18, 26, 64, 48, radius=7)
    # Inner screen area (cut back to the gradient by drawing the darker plate).
    box(31, 34, 38, 32, radius=4, fill=(30, 41, 59))

    # Sprocket holes down both edges.
    for index in range(4):
        top = 31 + index * 11.5
        box(21.5, top, 6, 7, radius=1.8, fill=(226, 232, 240))
        box(72.5, top, 6, 7, radius=1.8, fill=(226, 232, 240))

    # Play triangle inside the screen.
    draw.polygon([(43 * unit, 40 * unit), (43 * unit, 60 * unit), (59 * unit, 50 * unit)],
                 fill=FRAME)

    # Two music bars suggesting a mixed soundtrack.
    box(26, 74, 5, 10, radius=2, fill=(224, 231, 255))
    box(34, 70, 5, 14, radius=2, fill=(191, 219, 254))
    box(42, 76, 5, 8, radius=2, fill=(224, 231, 255))


def build(size):
    gradient = _linear_gradient(size, TOP, BOTTOM)
    icon = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    icon.paste(gradient, (0, 0), _rounded_mask(size, round(size * 0.22)))
    _draw_mark(ImageDraw.Draw(icon), size)
    return icon


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: make_icon.py <output.ico>')
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = [build(size) for size in SIZES]
    frames[0].save(output, format='ICO', sizes=[(size, size) for size in SIZES],
                   append_images=frames[1:])
    print(f'wrote {output} ({output.stat().st_size} bytes) sizes={SIZES}')


if __name__ == '__main__':
    main()
