"""Generate a reproducible macOS .icns icon."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

TOP = (99, 102, 241)
BOTTOM = (56, 189, 248)
FRAME = (255, 255, 255)


def build(size):
    gradient = Image.new('RGB', (size, size))
    pixels = gradient.load()
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * ratio) for i in range(3))
        for x in range(size):
            pixels[x, y] = color
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=size * .22, fill=255)
    icon = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    icon.paste(gradient, (0, 0), mask)
    draw, unit = ImageDraw.Draw(icon), size / 100
    draw.rounded_rectangle([18*unit, 26*unit, 82*unit, 74*unit], radius=7*unit, fill=FRAME)
    draw.rounded_rectangle([31*unit, 34*unit, 69*unit, 66*unit], radius=4*unit, fill=(30, 41, 59))
    for index in range(4):
        top = (31 + index * 11.5) * unit
        for left in (21.5, 72.5):
            draw.rounded_rectangle([left*unit, top, (left+6)*unit, top+7*unit], radius=1.8*unit,
                                   fill=(226, 232, 240))
    draw.polygon([(43*unit, 40*unit), (43*unit, 60*unit), (59*unit, 50*unit)], fill=FRAME)
    return icon


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: make_icon.py <output.icns>')
    output = Path(sys.argv[1]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        iconset = Path(temporary) / 'MixCutStudio.iconset'
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            build(points).save(iconset / f'icon_{points}x{points}.png')
            build(points * 2).save(iconset / f'icon_{points}x{points}@2x.png')
        subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(output)], check=True)
    print(f'wrote {output} ({output.stat().st_size} bytes)')


if __name__ == '__main__':
    main()
