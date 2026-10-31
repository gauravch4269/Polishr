"""Core Polishr rules. Each rule takes a parsed page and returns a list of issues.

Issue shape: {"severity": "high|medium|low", "category": str, "finding": str, "fix": str}
"""
import re

HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def issue(severity, category, finding, fix):
    return {"severity": severity, "category": category, "finding": finding, "fix": fix}


# ---------- contrast helpers (WCAG 2.x) ----------
def parse_hex(value):
    """'#abc' or '#aabbcc' -> (r, g, b) or None."""
    m = HEX_RE.fullmatch((value or "").strip())
    if not m:
        return None
    h = m.group(0)[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _channel(c):
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = (_channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg_hex, bg_hex):
    fg, bg = parse_hex(fg_hex), parse_hex(bg_hex)
    if fg is None or bg is None:
        return None
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _declarations(css):
    """Split 'a:b;c:d' into {'a': 'b', 'c': 'd'} using exact property names."""
    out = {}
    for part in css.split(";"):
        if ":" not in part:
            continue
        name, _, value = part.partition(":")
        out[name.strip().lower()] = value.strip().lower()
    return out


def _fg_bg(decls):
    """Return (fg, bg) hex strings from one declaration block, or None if not comparable."""
    fg_val = decls.get("color", "")
    fg = HEX_RE.search(fg_val)
    if not fg:
        return None
    bg_val = decls.get("background-color") or decls.get("background") or ""
    if "gradient" in bg_val or "url(" in bg_val:
        return None  # cannot compare against an image or gradient
    bg = HEX_RE.search(bg_val)
    return fg.group(0), (bg.group(0) if bg else "#ffffff")


def _style_blocks_decls(blocks):
    for css in blocks:
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for m in re.finditer(r"[^{}]+\{([^{}]*)\}", css):
            yield _declarations(m.group(1))


# ---------- rules ----------
def title_exists(page):
    if page.title.strip():
        return []
    return [issue("high", "seo", "The page has no <title> or it is empty.",
                  "Add a short, specific <title> inside <head>.")]


def single_h1(page):
    n = page.h1_count
    if n == 1:
        return []
    if n == 0:
        return [issue("medium", "visual_hierarchy", "The page has no <h1> heading.",
                      "Add exactly one <h1> that states what the page or business offers.")]
    return [issue("medium", "visual_hierarchy", f"The page has {n} <h1> headings; it should have exactly one.",
                  "Keep one <h1> and turn the others into <h2> or <h3>.")]


def viewport_meta(page):
    if page.viewport:
        return []
    return [issue("high", "mobile", "Missing viewport meta tag, so phones render the page zoomed out.",
                  'Add <meta name="viewport" content="width=device-width, initial-scale=1"> in <head>.')]


def meta_description(page):
    if page.meta_description.strip():
        return []
    return [issue("low", "seo", "Missing meta description.",
                  'Add <meta name="description" content="..."> with one or two plain sentences.')]


def html_lang(page):
    if page.lang.strip():
        return []
    return [issue("medium", "accessibility", "The <html> element has no lang attribute.",
                  'Set a language, for example <html lang="en">.')]


def images_alt(page):
    n = page.imgs_missing_alt
    if n == 0:
        return []
    return [issue("medium", "accessibility", f"{n} image(s) have no alt attribute.",
                  'Describe each meaningful image in alt text; use alt="" for purely decorative ones.')]


def has_cta(page):
    if page.ctas > 0:
        return []
    return [issue("medium", "cta_trust", "The page has no button or link, so visitors have nothing to click.",
                  "Add a clear call to action such as a button for contacting, booking or buying.")]


def text_contrast(page):
    blocks = list(_style_blocks_decls(page.style_blocks))
    blocks += [_declarations(s) for s in page.inline_styles]
    seen, bad = set(), []
    for decls in blocks:
        pair = _fg_bg(decls)
        if not pair or pair in seen:
            continue
        seen.add(pair)
        ratio = contrast_ratio(*pair)
        if ratio is not None and ratio < 4.5:
            bad.append((pair[0], pair[1], ratio))
    if not bad:
        return []
    worst = min(r for _, _, r in bad)
    examples = "; ".join(f"{f} on {b} = {r:.2f}:1" for f, b, r in bad[:3])
    return [issue("high" if worst < 3 else "medium", "colour_contrast",
                  f"{len(bad)} text/background colour pair(s) are below the WCAG 4.5:1 minimum ({examples}).",
                  "Darken the text or lighten the background until each pair reaches at least 4.5:1.")]


RULES = [title_exists, single_h1, viewport_meta, meta_description, html_lang,
         images_alt, has_cta, text_contrast]
