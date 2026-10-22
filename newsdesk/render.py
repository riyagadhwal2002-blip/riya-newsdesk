"""Renders the tweet-style post card (1080x1350 JPEG, Instagram 4:5 portrait)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .factcheck import strip_emoji

W, H = 1080, 1350
PAD_X = 84
FOOTER_H = 128

THEMES = {
    "light": {"bg": "#FFFFFF", "text": "#0F1419", "muted": "#536471", "rule": "#E6EAEC"},
    "dark": {"bg": "#000000", "text": "#E7E9EA", "muted": "#8B9095", "rule": "#2F3336"},
}


@dataclass
class Fonts:
    root: Path

    def get(self, name: str, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(self.root / name), size)


def _contrast_text(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#111111" if lum > 0.6 else "#FFFFFF"


def _wrap(text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words, line = para.split(), ""
        for w in words:
            trial = f"{line} {w}".strip()
            if font.getlength(trial) <= max_w:
                line = trial
                continue
            if line:
                lines.append(line)
            while font.getlength(w) > max_w:  # very long token: hard-break
                cut = len(w)
                while cut > 1 and font.getlength(w[:cut]) > max_w:
                    cut -= 1
                lines.append(w[:cut])
                w = w[cut:]
            line = w
        lines.append(line)
    return lines


def _avatar(path: Path, size: int, accent: str, initial: str, fonts: Fonts) -> Image.Image:
    scale = 4
    big = size * scale
    if path and Path(path).exists():
        img = ImageOps.fit(Image.open(path).convert("RGB"), (big, big), Image.LANCZOS)
    else:
        img = Image.new("RGB", (big, big), accent)
        d = ImageDraw.Draw(img)
        f = fonts.get("Inter-Bold.otf", int(big * 0.46))
        d.text((big / 2, big / 2), initial.upper(), font=f, fill="#FFFFFF", anchor="mm")
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    out = Image.new("RGBA", (big, big))
    out.paste(img, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


def render_card(post: dict, sources: list[str], brand: dict, out_path: Path, fonts_dir: Path,
                when: datetime | None = None) -> Path:
    theme = THEMES.get(brand.get("theme", "light"), THEMES["light"])
    accent = brand.get("accent", "#E1306C")
    fonts = Fonts(Path(fonts_dir))
    tz = ZoneInfo(brand.get("timezone", "Asia/Kolkata"))
    when = (when or datetime.now(tz)).astimezone(tz)

    news = strip_emoji(post["news"])
    take = strip_emoji(post["take"])
    name = brand.get("display_name", "")
    handle = "@" + brand.get("handle", "").lstrip("@")
    src_line = "Source: " + ", ".join(dict.fromkeys(sources[:2])) if sources else ""
    meta = f"{when.strftime('%-I:%M %p')} · {when.strftime('%-d %b %Y')}" + (f" · {src_line}" if src_line else "")

    img = Image.new("RGB", (W, H), theme["bg"])
    d = ImageDraw.Draw(img)
    text_w = W - 2 * PAD_X
    avatar_size = 124
    header_h = avatar_size
    gap_header, gap_para, gap_meta = 44, 34, 40

    # Pick the biggest body size that fits the space above the footer.
    usable_h = H - FOOTER_H - 120
    for size in range(58, 33, -2):
        f_news = fonts.get("Inter-Regular.otf", size)
        f_take = fonts.get("Inter-SemiBold.otf", size)
        lh = int(size * 1.32)
        news_lines = _wrap(news, f_news, text_w)
        take_lines = _wrap(take, f_take, text_w)
        f_meta = fonts.get("Inter-Regular.otf", 29)
        meta_lines = _wrap(meta, f_meta, text_w)
        block_h = (header_h + gap_header + lh * len(news_lines) + gap_para + lh * len(take_lines) + gap_meta
                   + 38 * len(meta_lines))
        if block_h <= usable_h:
            break

    top = max(70, (H - FOOTER_H - block_h) // 2)

    # Header: avatar + name + handle
    av = _avatar(brand.get("_avatar_abs"), avatar_size, accent, name[:1] or "R", fonts)
    img.paste(av, (PAD_X, top), av)
    nx = PAD_X + avatar_size + 28
    d.text((nx, top + 20), name, font=fonts.get("Inter-Bold.otf", 42), fill=theme["text"])
    d.text((nx, top + 72), handle, font=fonts.get("Inter-Regular.otf", 34), fill=theme["muted"])

    y = top + header_h + gap_header
    for line in news_lines:
        d.text((PAD_X, y), line, font=f_news, fill=theme["text"])
        y += lh
    y += gap_para
    for line in take_lines:
        d.text((PAD_X, y), line, font=f_take, fill=theme["text"])
        y += lh
    y += gap_meta - 12
    d.line((PAD_X, y, W - PAD_X, y), fill=theme["rule"], width=2)
    y += 22
    for line in meta_lines:
        d.text((PAD_X, y), line, font=f_meta, fill=theme["muted"])
        y += 38

    # Footer: follow for more (text colour picked for contrast with the accent)
    d.rectangle((0, H - FOOTER_H, W, H), fill=accent)
    foot_fg = _contrast_text(accent)
    f_foot = fonts.get("Inter-Bold.otf", 40)
    d.text((PAD_X, H - FOOTER_H / 2), f"Follow {handle} for more", font=f_foot, fill=foot_fg, anchor="lm")
    d.text((W - PAD_X, H - FOOTER_H / 2), "→", font=fonts.get("Inter-Bold.otf", 52), fill=foot_fg, anchor="rm")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=92, optimize=True, progressive=False)
    return out_path


def render_story(card_path: Path, out_path: Path, brand: dict) -> Path:
    """Full-screen 9:16 story: the same post card centred on a soft background."""
    card = Image.open(card_path).convert("RGB")
    h = brand.get("accent", "#FFC72C").lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    tint = tuple(int(c + (255 - c) * 0.75) for c in (r, g, b))   # soft version of this post's colour
    bg = tint if brand.get("theme", "light") == "light" else "#111111"
    story = Image.new("RGB", (1080, 1920), bg)
    story.paste(card, (0, (1920 - card.height) // 2))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    story.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path
