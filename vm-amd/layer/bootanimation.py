"""The product's boot animation with a held frame that loops until boot completes.

The stock animation (logo, then a fade to black) lasts ~5 s, which covers a real Googlebook's boot.
In a VM the boot takes minutes and the screen sat black after the fade. The stock parts still play;
then one frame with the wordmark and a line of text stays up until Android is ready.
"""
import io
import zipfile

from PIL import Image, ImageDraw, ImageFont

TEXT_COLOR = (154, 160, 166)
TEXT_HEIGHT = 0.70  # fraction of the frame height: between the wordmark and the "Designed for" line
FONT_SIZE = 0.025  # fraction of the frame height


def _frame_trim(stock: zipfile.ZipFile, frame: str) -> str:
    """trim.txt lines follow the part's frames in name order."""
    part = frame.rsplit('/', 1)[0]
    frames = sorted(n for n in stock.namelist() if n.startswith(part + '/') and n.endswith('.png'))
    return stock.read(f'{part}/trim.txt').decode().splitlines()[frames.index(frame)]


def _draw_text(data: bytes, text: str) -> bytes:
    im = Image.open(io.BytesIO(data)).convert('RGB')
    font = ImageFont.load_default(size=max(8, int(im.height * FONT_SIZE)))
    draw = ImageDraw.Draw(im)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((im.width - (right - left)) / 2, im.height * TEXT_HEIGHT), text, font=font, fill=TEXT_COLOR)
    out = io.BytesIO()
    im.save(out, 'PNG', optimize=True)
    return out.getvalue()


def build_bootanimation(stock_zip: bytes, hold_frame: str, text: str) -> bytes:
    stock = zipfile.ZipFile(io.BytesIO(stock_zip))
    desc = stock.read('desc.txt').decode().rstrip('\n') + '\np 0 0 part1\n'
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as z:
        for info in stock.infolist():
            if info.filename != 'desc.txt':
                z.writestr(info.filename, stock.read(info), zipfile.ZIP_STORED)
        z.writestr('desc.txt', desc, zipfile.ZIP_STORED)
        z.writestr('part1/hold.png', _draw_text(stock.read(hold_frame), text), zipfile.ZIP_STORED)
        z.writestr('part1/trim.txt', _frame_trim(stock, hold_frame) + '\n', zipfile.ZIP_STORED)
    return out.getvalue()
