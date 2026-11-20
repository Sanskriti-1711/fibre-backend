"""Evaluate the tape reader on clean, field-degraded and edge-bounded images.

Local only: builds the images in `field_eval/` and prints what the reader does
with each. Run with the venv python, PYTHONPATH stripped and tesseract on PATH:

    env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe field_eval.py
"""

import io
import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from supervision import tape_measure

OUT = 'field_eval'


def save(image: Image.Image, name: str) -> str:
    path = f'{OUT}/{name}.jpg'
    image.convert('RGB').save(path, quality=82)
    return path


def degradations(tape: Image.Image):
    """The same photo as a phone in the field would give it back."""
    yield 'clean', tape
    yield 'soft', tape.filter(ImageFilter.GaussianBlur(1.6))
    yield 'dim', ImageEnhance.Brightness(tape.filter(ImageFilter.GaussianBlur(0.8))).enhance(0.5)
    yield 'angled', tape.rotate(7, resample=Image.BICUBIC, expand=True, fillcolor=90)
    yield 'tilted12', tape.rotate(12, resample=Image.BICUBIC, expand=True, fillcolor=90)

    glare = tape.convert('L').copy()
    draw = ImageDraw.Draw(glare)
    draw.ellipse(
        (int(glare.width * 0.55), -int(glare.height * 0.4),
         int(glare.width * 1.1), int(glare.height * 0.6)),
        fill=245,
    )
    yield 'glare', glare

    noisy = tape.convert('L').point(lambda value: max(0, min(255, int(value * 0.8) + random.randint(-22, 22))))
    yield 'noisy', noisy


def trench_scene(tape: Image.Image) -> tuple[Image.Image, dict]:
    """A blade lying across a trench: known edges at 1/3 and 2/3 of the frame."""
    blade = tape.crop(
        (0, int(tape.height * 0.2), tape.width, int(tape.height * 0.8))
    ).resize((900, 110))
    canvas = Image.new('L', (900, 420), 60)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 900, 420), fill=150)          # ground either side
    draw.rectangle((300, 0, 600, 420), fill=45)         # the open trench
    canvas.paste(blade, (0, 155))
    return canvas, {'top': 155, 'bottom': 265}


def evaluate(path: str, element: str, dimension: str) -> None:
    data = open(path, 'rb').read()
    read = tape_measure.read_tape_bytes(data)
    if not read:
        print(f'  {path}: no tape read')
        return
    values = [(row['value'], round(row['x'], 2)) for row in read['readings']]
    row, note = tape_measure.measure(element, dimension, read['readings'], read['unit'])
    print(f'  {path}: {values} bounds={read.get("bounds")}')
    print(f'     measure={row["value"] if row else None} {row["unit"] if row else ""} | {note[:90]}')


def main():
    import os

    os.makedirs(OUT, exist_ok=True)
    tape = Image.open('tape_test4.png').convert('L')

    print('=== degraded field variants ===')
    for name, image in degradations(tape):
        evaluate(save(image, name), 'trench', 'length')

    print('\n=== blade across a trench (true edges at 0.33 and 0.67) ===')
    scene, band = trench_scene(tape)
    path = save(scene, 'trench_scene')
    bounds = tape_measure._object_bounds(scene, band)
    print('  detected bounds:', bounds)
    evaluate(path, 'trench', 'width')


if __name__ == '__main__':
    main()
