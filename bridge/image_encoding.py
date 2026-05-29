from PIL import Image, ImageDraw, ImageFont
from itertools import groupby
import struct

PRINT_WIDTH = 384
TRANSLATE = [(1536, 255), (1152, 254), (768, 253), (384, 252), (251, 251)]


def _rle_lengths(lengths):
    for length in lengths:
        if length <= 251:
            yield length
        else:
            remainder = length
            first = True
            while remainder > 251:
                chunk, code = next(t for t in TRANSLATE if remainder >= t[0])
                remainder -= chunk
                if not first:
                    yield 0  # toggle back to same colour for next chunk
                first = False
                yield code
            if remainder > 0:
                yield 0
                yield remainder


def prepare_image(im: Image.Image, dither: bool = True) -> Image.Image:
    """Scale to PRINT_WIDTH and convert to 1-bit. Shared by encoding and preview."""
    w, h = im.size
    if w > PRINT_WIDTH:
        new_h = int(h * PRINT_WIDTH / w)
        im = im.resize((PRINT_WIDTH, new_h), Image.Resampling.LANCZOS)

    if dither:
        return im.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    return im.convert("1", dither=Image.Dither.NONE)


def image_to_rle(im: Image.Image, dither: bool = False) -> tuple[int, bytes]:
    """Convert a PIL image to (pixel_count, rle_bytes).

    The image is scaled to PRINT_WIDTH wide if needed, converted to 1-bit,
    and rotated 180 degrees before encoding.
    """
    im = prepare_image(im, dither=dither)
    im = im.transpose(Image.Transpose.ROTATE_180)

    pixels = list(im.getdata())
    grouped = [(k, sum(1 for _ in g)) for k, g in groupby(pixels)]

    # RLE always starts with a white run; prepend zero-length white if needed
    if grouped and grouped[0][0] == 0:  # 0 = black in mode "1"
        grouped.insert(0, (255, 0))

    lengths = [run_len for _, run_len in grouped]
    rle_bytes = bytes(_rle_lengths(lengths))

    # type byte (0x01) + 4-byte LE length + rle data
    payload = struct.pack("<BL", 0x01, len(rle_bytes)) + rle_bytes
    return len(pixels), payload


def text_to_image(text: str, font_size: int = 24) -> Image.Image:
    """Render text to a white-background image at PRINT_WIDTH."""
    dummy = Image.new("L", (PRINT_WIDTH, 10))
    draw = ImageDraw.Draw(dummy)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    # Measure wrapped text height
    lines = []
    words = text.split()
    line = ""
    for word in words:
        test = f"{line} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] > PRINT_WIDTH - 8 and line:
            lines.append(line)
            line = word
        else:
            line = test
    if line:
        lines.append(line)

    line_height = font_size + 4
    height = max(line_height * len(lines) + 16, 32)

    im = Image.new("L", (PRINT_WIDTH, height), 255)
    draw = ImageDraw.Draw(im)
    y = 8
    for line in lines:
        draw.text((4, y), line, fill=0, font=font)
        y += line_height

    return im


_RLE_DECODE_TRANSLATE = {252: 384, 253: 768, 254: 1152, 255: 1536}
_RLE_TYPE_OFFSET = 47   # byte 0x01 in build_command output
_RLE_LEN_OFFSET = 48    # 4-byte LE RLE data length
_RLE_DATA_OFFSET = 52   # raw RLE bytes start here


def _decode_rle(rle_bytes: bytes) -> list:
    pixels = []
    color = 255  # RLE always starts with a white run
    for b in rle_bytes:
        if b == 0:
            color = 0 if color == 255 else 255
        else:
            pixels.extend([color] * _RLE_DECODE_TRANSLATE.get(b, b))
            color = 0 if color == 255 else 255
    return pixels


def lp_binary_to_pil(binary: bytes) -> Image.Image:
    """Decode an LP thermal binary payload (as received from the LP server) into a PIL image.

    binary is the full build_command(...) output, i.e. the base64-decoded print payload.
    Raises ValueError on malformed input.
    """
    if len(binary) < _RLE_DATA_OFFSET:
        raise ValueError(f"LP binary too short: {len(binary)} bytes")
    if binary[_RLE_TYPE_OFFSET] != 0x01:
        raise ValueError(f"Unexpected RLE type byte: 0x{binary[_RLE_TYPE_OFFSET]:02x}")

    rle_len = struct.unpack_from("<I", binary, _RLE_LEN_OFFSET)[0]
    pixels = _decode_rle(binary[_RLE_DATA_OFFSET: _RLE_DATA_OFFSET + rle_len])

    height = len(pixels) // PRINT_WIDTH
    if height == 0:
        raise ValueError("Decoded image has zero height")
    pixels = pixels[:height * PRINT_WIDTH]

    im = Image.new("L", (PRINT_WIDTH, height))
    im.putdata(pixels)
    return im.transpose(Image.Transpose.ROTATE_180)


def create_blank_image(height: int = 64) -> Image.Image:
    """Return an all-white image at print width, used for unused personality slots."""
    return Image.new("L", (PRINT_WIDTH, height), 255)


def load_image(path: str, max_height: int | None = None) -> Image.Image:
    im = Image.open(path)
    if im.mode == "RGBA":
        bg = Image.new("L", im.size, 255)
        bg.paste(im.convert("L"), mask=im.split()[3])
        im = bg
    else:
        im = im.convert("L")

    # Resize to PRINT_WIDTH wide, maintaining aspect ratio.
    w, h = im.size
    if w != PRINT_WIDTH:
        new_h = int(h * PRINT_WIDTH / w)
        im = im.resize((PRINT_WIDTH, new_h), Image.Resampling.LANCZOS)

    if max_height and im.height > max_height:
        im = im.resize((PRINT_WIDTH, max_height), Image.Resampling.LANCZOS)

    return im
