#!/usr/bin/env python3
"""Polishr: rate, fix and package a website design. Standard library only."""
import base64
import binascii
import importlib.util
import io
import ipaddress
import json
import os
import re
import socket
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
MAX_BODY = 12 * 1024 * 1024      # request body cap
MAX_FETCH = 2 * 1024 * 1024      # fetched page cap
FETCH_TIMEOUT = 15               # seconds
MODEL_HTML_CHARS = 6000          # how much HTML reaches the model
SEVERITY_WEIGHT = {"high": 2.0, "medium": 1.0, "low": 0.5}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SCORE_KEYS = ["visual_hierarchy", "typography", "colour_contrast", "spacing_layout",
              "cta_trust", "accessibility", "mobile"]
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def api_key():
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def model_name():
    return os.environ.get("GEMMA_MODEL", "gemma-4-26b-a4b-it").strip() or "gemma-4-26b-a4b-it"


# ---------------------------------------------------------------- rules
def load_rules():
    rules = []
    for path in sorted((BASE / "rules").glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"polishr_rules_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for fn in getattr(mod, "RULES", []):
                rules.append((f"{path.stem}.{fn.__name__}", fn))
        except Exception as exc:  # a broken community pack must not stop the app
            print(f"[polishr] could not load {path.name}: {exc}", file=sys.stderr)
    return rules


RULES = load_rules()


class Page:
    """What rules get to look at."""

    def __init__(self, html):
        self.html = html
        self.title = ""
        self.h1_count = 0
        self.viewport = False
        self.meta_description = ""
        self.lang = ""
        self.imgs_total = 0
        self.imgs_missing_alt = 0
        self.ctas = 0
        self.links = []
        self.style_blocks = []
        self.inline_styles = []


class _Parser(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.p = page
        self.in_title = False
        self.in_style = False
        self._style_buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        p = self.p
        if tag == "html":
            p.lang = a.get("lang", "").strip()
        elif tag == "title":
            self.in_title = True
        elif tag == "h1":
            p.h1_count += 1
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "viewport":
                p.viewport = True
            elif name == "description":
                p.meta_description = a.get("content", "")
        elif tag == "img":
            p.imgs_total += 1
            if "alt" not in a:
                p.imgs_missing_alt += 1
        elif tag == "button":
            p.ctas += 1
        elif tag == "a" and "href" in a:
            p.ctas += 1
            p.links.append(a["href"])
        elif tag == "input" and a.get("type", "").lower() in ("submit", "button"):
            p.ctas += 1
        elif tag == "style":
            self.in_style = True
            self._style_buf = []
        if a.get("style"):
            p.inline_styles.append(a["style"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "style" and self.in_style:
            self.in_style = False
            self.p.style_blocks.append("".join(self._style_buf))

    def handle_data(self, data):
        if self.in_title:
            self.p.title += data
        elif self.in_style:
            self._style_buf.append(data)

    def close(self):
        if self.in_style:  # unterminated <style>
            self.p.style_blocks.append("".join(self._style_buf))
            self.in_style = False
        super().close()


def parse_page(html):
    page = Page(html)
    parser = _Parser(page)
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # malformed HTML: keep whatever was collected
        print(f"[polishr] parse warning: {exc}", file=sys.stderr)
    return page


def run_rules(html):
    page = parse_page(html)
    issues = []
    for name, fn in RULES:
        try:
            for r in fn(page) or []:
                sev = str(r.get("severity", "low")).lower()
                issues.append({
                    "severity": sev if sev in SEVERITY_WEIGHT else "low",
                    "category": str(r.get("category", "general")),
                    "finding": str(r.get("finding", "")),
                    "fix": str(r.get("fix", "")),
                    "source": "measured",
                })
        except Exception as exc:
            print(f"[polishr] rule {name} failed: {exc}", file=sys.stderr)
    return issues


def score(issues):
    total = sum(SEVERITY_WEIGHT.get(i["severity"], 0.5) for i in issues)
    return round(max(0.0, 10.0 - total), 1)


# ---------------------------------------------------------------- demo auto-fix
def basic_fix(html):
    """Demo-mode fix: add lang, viewport and empty alt. Regex based, deliberately simple."""
    if not re.search(r"<html\b", html, re.I):
        html = ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n</head>\n<body>\n"
                + html + "\n</body>\n</html>")
    m = re.search(r"<html\b([^>]*)>", html, re.I)
    if m and not re.search(r"\blang\s*=\s*[\"']?[\w-]+", m.group(1), re.I):
        html = html[:m.start(1)] + ' lang="en"' + html[m.start(1):]
    if not re.search(r"<meta\b[^>]*name\s*=\s*[\"']?viewport", html, re.I):
        tag = '<meta name="viewport" content="width=device-width, initial-scale=1">'
        h = re.search(r"<head\b[^>]*>", html, re.I)
        if h:
            html = html[:h.end()] + "\n" + tag + html[h.end():]
        else:
            h = re.search(r"<html\b[^>]*>", html, re.I)
            html = html[:h.end()] + "\n<head>\n" + tag + "\n</head>" + html[h.end():]

    def add_alt(m):
        t = m.group(0)
        if re.search(r"\balt\s*=", t, re.I):
            return t
        return re.sub(r"\s*/?>$", lambda e: ' alt=""' + e.group(0).lstrip(), t, count=1)

    return re.sub(r"<img\b[^>]*>", add_alt, html, flags=re.I)


# ---------------------------------------------------------------- fetching
def _check_url(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Only http and https URLs can be fetched.")
    if os.environ.get("ALLOW_PRIVATE_FETCH") == "1":  # local testing only
        return
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("Could not resolve that host name.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("Refusing to fetch a private or local address.")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url):
    """Return (html, warning). Raises on failure."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    _check_url(url)
    opener = urllib.request.build_opener(_CheckedRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "Polishr/1.0 (+design checker)",
                                               "Accept": "text/html,*/*;q=0.5"})
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT) as r:
            ctype = r.headers.get("Content-Type", "")
            if ctype and "html" not in ctype.lower() and "text" not in ctype.lower():
                raise ValueError(f"The URL returned {ctype.split(';')[0]}, not HTML.")
            data = r.read(MAX_FETCH + 1)
            charset = r.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise ValueError(f"The site answered HTTP {exc.code}.")
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach the site ({exc.reason}).")
    except (TimeoutError, socket.timeout):
        raise ValueError(f"The site did not answer within {FETCH_TIMEOUT} seconds.")
    warning = None
    if len(data) > MAX_FETCH:
        data = data[:MAX_FETCH]
        warning = "The page was larger than 2 MB, so only the first 2 MB was used."
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    return text, warning


# ---------------------------------------------------------------- Gemma
class GemmaError(Exception):
    pass


def build_prompt(html, measured, has_image):
    truncated = len(html) > MODEL_HTML_CHARS
    snippet = html[:MODEL_HTML_CHARS]
    measured_json = json.dumps([{k: i[k] for k in ("severity", "category", "finding", "fix")} for i in measured],
                               ensure_ascii=False)
    schema = {
        "summary": "2-3 sentences on the overall design quality",
        "scores": {k: "integer 0-10" for k in SCORE_KEYS},
        "issues": [{"severity": "high|medium|low", "category": "string", "finding": "string", "fix": "string"}],
        "top_3_fixes": ["string", "string", "string"],
        "fixed_html": "complete standalone HTML page",
    }
    parts = [
        "You are a senior web designer reviewing a website for a small business.",
        "Return ONLY a JSON object, with no markdown fences and no commentary, matching this shape:",
        json.dumps(schema),
        "Rules for fixed_html: a complete standalone page, same content as the original, inline CSS only, "
        "no external assets, no scripts, and it must fix every measured issue listed below.",
        "Measured issues (facts counted by code, do not contradict them): " + measured_json,
    ]
    if has_image:
        parts.append("A screenshot of the page is attached as the first part. Use it for visual judgement.")
    if html:
        parts.append("HTML of the page" + (f" (truncated to the first {MODEL_HTML_CHARS} characters)" if truncated else "")
                     + ":\n" + snippet)
    else:
        parts.append("No HTML was supplied. Rebuild the page in fixed_html from what the screenshot shows.")
    return "\n\n".join(parts)


def call_gemma(prompt, image_b64, mime):
    key, model = api_key(), model_name()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + urllib.parse.quote(model, safe="-._") + ":generateContent?key=" + urllib.parse.quote(key, safe=""))
    parts = []
    if image_b64:
        parts.append({"inline_data": {"mime_type": mime, "data": image_b64}})
    parts.append({"text": prompt})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        msg = f"Gemini API returned HTTP {exc.code}."
        try:
            msg = json.loads(exc.read().decode("utf-8", "replace"))["error"]["message"]
        except Exception:
            pass
        raise GemmaError(msg)
    except urllib.error.URLError as exc:
        raise GemmaError(f"Could not reach the Gemini API ({exc.reason}).")
    except (TimeoutError, socket.timeout):
        raise GemmaError("The Gemini API did not answer in time.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise GemmaError("The Gemini API returned a reply that is not JSON.")
    try:
        parts_out = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts_out if isinstance(p, dict))
    except (KeyError, IndexError, TypeError):
        reason = (data.get("promptFeedback") or {}).get("blockReason") if isinstance(data, dict) else None
        raise GemmaError("The model returned no answer" + (f" (blocked: {reason})." if reason else "."))


def parse_model_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise GemmaError("The model reply did not contain a JSON object.")
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        raise GemmaError("The model reply was not valid JSON (it may have been cut off).")
    if not isinstance(obj, dict):
        raise GemmaError("The model reply was not a JSON object.")
    return obj


def normalise_model(obj):
    scores = {}
    raw_scores = obj.get("scores") if isinstance(obj.get("scores"), dict) else {}
    for k in SCORE_KEYS:
        try:
            scores[k] = max(0, min(10, int(round(float(raw_scores.get(k))))))
        except (TypeError, ValueError):
            scores[k] = None
    issues = []
    for i in obj.get("issues") if isinstance(obj.get("issues"), list) else []:
        if not isinstance(i, dict):
            continue
        sev = str(i.get("severity", "low")).lower()
        issues.append({"severity": sev if sev in SEVERITY_WEIGHT else "low",
                       "category": str(i.get("category", "general")),
                       "finding": str(i.get("finding", "")),
                       "fix": str(i.get("fix", "")),
                       "source": "gemma"})
    fixes = obj.get("top_3_fixes")
    fixes = [str(f) for f in fixes[:3]] if isinstance(fixes, list) else []
    fixed = obj.get("fixed_html")
    return {"summary": str(obj.get("summary", "")), "scores": scores, "issues": issues,
            "top_3_fixes": fixes, "fixed_html": fixed if isinstance(fixed, str) else ""}


def merge_issues(measured, model_issues):
    seen = {re.sub(r"\W+", " ", i["finding"].lower()).strip() for i in measured}
    merged = list(measured)
    for i in model_issues:
        key = re.sub(r"\W+", " ", i["finding"].lower()).strip()
        if key and key not in seen:
            seen.add(key)
            merged.append(i)
    merged.sort(key=lambda i: SEVERITY_ORDER.get(i["severity"], 2))
    return merged


# ---------------------------------------------------------------- analyze
def analyze(payload):
    url = str(payload.get("url") or "").strip()
    html = str(payload.get("html") or "")
    image_b64 = re.sub(r"\s+", "", str(payload.get("image_b64") or ""))
    mime = str(payload.get("mime_type") or "").strip().lower()
    if not (url or html.strip() or image_b64):
        return 400, {"error": "Give a URL, a screenshot or some HTML."}

    if image_b64:
        if image_b64.startswith("data:"):
            image_b64 = image_b64.partition(",")[2]
        mime = mime or "image/png"
        if mime not in IMAGE_MIMES:
            return 400, {"error": "Screenshot must be PNG, JPEG, WebP or GIF."}
        try:
            base64.b64decode(image_b64, validate=True)
        except (binascii.Error, ValueError):
            return 400, {"error": "The screenshot is not valid base64."}

    warnings, fetch_error = [], None
    if url:
        try:
            html, warn = fetch_url(url)
            if warn:
                warnings.append(warn)
        except ValueError as exc:
            fetch_error = str(exc)
            if html.strip():
                warnings.append("The URL could not be fetched, so the pasted HTML was used instead.")
        except Exception as exc:
            fetch_error = f"Fetch failed: {type(exc).__name__}"
    if not html.strip() and not image_b64:
        return 400, {"error": "Nothing to analyse. " + (fetch_error or ""), "fetch_error": fetch_error}

    demo = not api_key()
    measured = run_rules(html) if html.strip() else []
    score_before = score(measured) if html.strip() else None
    result = {"demo_mode": demo, "fetch_error": fetch_error, "original_html": html,
              "score_before": score_before, "warnings": warnings,
              "html_truncated": len(html) > MODEL_HTML_CHARS}

    if demo:
        if not html.strip():
            return 400, {"error": "Demo mode needs a URL or HTML. Screenshots are only analysed when a Gemini API key is set.",
                         "fetch_error": fetch_error}
        if image_b64:
            warnings.append("Demo mode ignores the screenshot.")
        fixed = basic_fix(html)
        remaining = run_rules(fixed)
        ordered = sorted(measured, key=lambda i: SEVERITY_ORDER[i["severity"]])
        result.update({
            "model": None,
            "summary": "DEMO MODE: no Gemma call was made. These are measured checks plus a simple automatic fix "
                       "(adds lang, viewport and empty alt). Set GEMINI_API_KEY for the full review.",
            "scores": None, "issues": ordered,
            "top_3_fixes": [i["fix"] for i in ordered[:3]],
            "fixed_html": fixed, "score_after": score(remaining), "remaining_after_fix": remaining,
        })
        return 200, result

    prompt = build_prompt(html, measured, bool(image_b64))
    try:
        norm = normalise_model(parse_model_json(call_gemma(prompt, image_b64, mime or "image/png")))
    except GemmaError as exc:
        return 502, {"error": str(exc), "fetch_error": fetch_error}

    fixed = norm["fixed_html"]
    if "<html" in fixed.lower():
        remaining = run_rules(fixed)
        score_after = score(remaining)
    else:
        warnings.append("The model did not return a usable fixed_html, so there is no fix to preview.")
        fixed, remaining, score_after = "", [], None
    result.update({
        "model": model_name(), "summary": norm["summary"], "scores": norm["scores"],
        "issues": merge_issues(measured, norm["issues"]), "top_3_fixes": norm["top_3_fixes"],
        "fixed_html": fixed, "score_after": score_after, "remaining_after_fix": remaining,
    })
    return 200, result


# ---------------------------------------------------------------- bundle
BUNDLE_DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
COPY index.html .
EXPOSE 8080
CMD ["python3", "-m", "http.server", "8080"]
"""

BUNDLE_APP_YAML = """name: fixed-site
region: blr
services:
  - name: fixed-site
    dockerfile_path: Dockerfile
    github:
      repo: YOUR_GITHUB_USERNAME/YOUR_REPO
      branch: main
      deploy_on_push: true
    instance_size_slug: apps-s-1vcpu-1gb
    instance_count: 1
    http_port: 8080
    health_check:
      http_path: /
"""

BUNDLE_README = """# fixed-site

Static page produced by Polishr. It has not been deployed for you.

1. Review index.html in a browser first. A model may have changed wording or layout.
2. Put these files in a Git repository and replace YOUR_GITHUB_USERNAME/YOUR_REPO in .do/app.yaml.
3. Create the app on DigitalOcean App Platform from that repo, or run it locally:
   docker build -t fixed-site . && docker run -p 8080:8080 fixed-site
"""


def make_bundle(fixed_html):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", fixed_html)
        z.writestr("Dockerfile", BUNDLE_DOCKERFILE)
        z.writestr(".do/app.yaml", BUNDLE_APP_YAML)
        z.writestr("README.md", BUNDLE_README)
    return buf.getvalue()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Polishr/1.0"

    def _send(self, status, body, ctype, extra=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "Content-Length is required."})
            return None
        if length > MAX_BODY:
            self._json(413, {"error": "Request is larger than 12 MB."})
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "Body must be JSON."})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "Body must be a JSON object."})
            return None
        return data

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                self._send(200, (BASE / "index.html").read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._json(500, {"error": "index.html is missing."})
        elif path == "/health":
            self._json(200, {"ok": True, "model": model_name(), "demo_mode": not api_key(),
                             "rules": [name for name, _ in RULES]})
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path not in ("/api/analyze", "/api/bundle"):
                self._json(404, {"error": "Not found."})
                return
            data = self._read_json()
            if data is None:
                return
            if path == "/api/analyze":
                status, body = analyze(data)
                self._json(status, body)
            else:
                fixed = data.get("fixed_html")
                if not isinstance(fixed, str) or "<html" not in fixed.lower():
                    self._json(400, {"error": "fixed_html must be a complete HTML page."})
                    return
                self._send(200, make_bundle(fixed), "application/zip",
                           {"Content-Disposition": 'attachment; filename="fixed-site.zip"'})
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Unexpected server error."})


def main():
    port = int(os.environ.get("PORT", "8080"))
    mode = "DEMO MODE (no API key)" if not api_key() else f"live, model {model_name()}"
    print(f"Polishr listening on 0.0.0.0:{port} - {mode} - {len(RULES)} rules", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
