#!/usr/bin/env python3
"""Build an EPUB cover and site preview images from a background image.

The background (typically a public-domain painting or photograph) is cropped
to a 2:3 book cover, darkened behind the text, and typeset with Pillow. The
same image is also cropped to the 1200x630 and 640x360 preview sizes used by
the Jekyll listing pages.

Example:
    python make_cover.py painting.jpg docs/assets/essays/my-essay \
        --title "My Essay" --subtitle "A subtitle" --author "ClaudeAI" \
        --credit "Painter, Title (1850), public domain"

Then build the EPUB from the Jekyll post with pandoc:
    pandoc docs/_posts/2026-01-01-my-essay.md -o docs/assets/essays/my-essay/my-essay.epub \
        --epub-cover-image docs/assets/essays/my-essay/cover.jpg --toc --toc-depth 2
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

COVER_SIZE = (1600, 2400)
PREVIEW_SIZES = [(1200, 630), (640, 360)]
FONT_DIRS = [
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/dejavu",
    "/Library/Fonts",
    "C:/Windows/Fonts",
]
SERIF = ["LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf", "Georgia Bold.ttf", "georgiab.ttf"]
SERIF_ITALIC = ["LiberationSerif-Italic.ttf", "DejaVuSerif-Italic.ttf", "Georgia Italic.ttf", "georgiai.ttf"]
SANS = ["LiberationSans-Regular.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf"]


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for directory in FONT_DIRS:
        for name in candidates:
            path = Path(directory) / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        trial = f"{lines[-1]} {word}" if lines else word
        if lines and draw.textlength(trial, font=font) <= max_width:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines


def _fit_lines(draw, text, candidates, start_size, max_width, max_lines):
    size = start_size
    while size > 24:
        font = _font(candidates, size)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= max_lines and all(draw.textlength(l, font=font) <= max_width for l in lines):
            return font, lines
        size -= 4
    font = _font(candidates, size)
    return font, _wrap(draw, text, font, max_width)


def _shade(size: tuple[int, int], top: float, bottom: float) -> Image.Image:
    """Vertical black gradient mask: strong at the top and bottom, clear in the middle."""
    w, h = size
    mask = Image.new("L", (1, h))
    for y in range(h):
        t = y / (h - 1)
        top_alpha = max(0.0, 1 - t / 0.45) * top
        bottom_alpha = max(0.0, (t - 0.72) / 0.28) * bottom
        mask.putpixel((0, y), int(255 * max(top_alpha, bottom_alpha)))
    return mask.resize((w, h))


def _draw_centered(draw, lines, font, y, width, fill, spacing):
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text(((width - (bbox[2] - bbox[0])) / 2 - bbox[0], y), line, font=font, fill=fill)
        y += (bbox[3] - bbox[1]) + spacing
    return y


def build_cover(
    background: Image.Image, title: str, subtitle: str, author: str, credit: str, shade: bool = True
) -> Image.Image:
    cover = ImageOps.fit(background.convert("RGB"), COVER_SIZE, Image.LANCZOS, centering=(0.5, 0.5))
    if shade:
        black = Image.new("RGB", COVER_SIZE, (8, 14, 24))
        cover = Image.composite(black, cover, _shade(COVER_SIZE, top=0.88, bottom=0.8))
    draw = ImageDraw.Draw(cover)
    w, h = COVER_SIZE
    margin = 130
    ivory, gold = (247, 241, 226), (222, 186, 110)

    main, _, rest = title.partition(":")
    title_font, title_lines = _fit_lines(draw, main.strip(), SERIF, 150, w - 2 * margin, 3)
    y = _draw_centered(draw, title_lines, title_font, 170, w, ivory, 26)

    y += 40
    draw.line([(w / 2 - 170, y), (w / 2 + 170, y)], fill=gold, width=5)
    y += 55

    if rest.strip():
        sub_font, sub_lines = _fit_lines(draw, rest.strip(), SERIF_ITALIC, 76, w - 2 * margin, 3)
        y = _draw_centered(draw, sub_lines, sub_font, y, w, gold, 18) + 30
    if subtitle:
        small_font, small_lines = _fit_lines(draw, subtitle, SERIF_ITALIC, 56, w - 2 * margin - 40, 3)
        _draw_centered(draw, small_lines, small_font, y, w, ivory, 16)

    author_font = _font(SANS, 58)
    author_text = author.upper()
    spaced = " ".join(author_text)
    bbox = draw.textbbox((0, 0), spaced, font=author_font)
    draw.text(((w - (bbox[2] - bbox[0])) / 2, h - 260), spaced, font=author_font, fill=ivory)

    if credit:
        credit_font = _font(SANS, 30)
        bbox = draw.textbbox((0, 0), credit, font=credit_font)
        draw.text(((w - (bbox[2] - bbox[0])) / 2, h - 110), credit, font=credit_font, fill=(200, 196, 186))
    return cover


def build_preview(background: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(background.convert("RGB"), size, Image.LANCZOS, centering=(0.5, 0.45))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", help="Background image (public domain or suitably licensed)")
    parser.add_argument("out_dir", help="Essay asset directory, e.g. docs/assets/essays/<slug>")
    parser.add_argument("--title", required=True)
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--author", default="ClaudeAI")
    parser.add_argument("--credit", default="", help="Image credit printed at the foot of the cover")
    parser.add_argument("--no-shade", action="store_true", help="Skip darkening the background behind the text")
    parser.add_argument("--preview-image", help="Separate (landscape) background for the preview images")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    background = Image.open(args.image)
    # A light blur keeps busy paintings from fighting the type.
    softened = background.convert("RGB").filter(ImageFilter.GaussianBlur(radius=1.2))

    build_cover(softened, args.title, args.subtitle, args.author, args.credit, shade=not args.no_shade).save(
        out_dir / "cover.jpg", quality=90, optimize=True
    )
    preview_background = Image.open(args.preview_image) if args.preview_image else background
    for size in PREVIEW_SIZES:
        build_preview(preview_background, size).save(
            out_dir / "images" / f"preview_{size[0]}x{size[1]}.jpg", quality=85, optimize=True
        )
    print(f"Wrote cover and previews to {out_dir}")


if __name__ == "__main__":
    main()
