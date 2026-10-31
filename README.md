# Polishr

Polishr rates a website's design, fixes it and packages the fix for deployment.
Paste a URL, upload a screenshot or paste HTML. Polishr measures the page with open,
pluggable rules, asks Gemma 4 (through the Gemini API) for a review and a fixed page,
re-runs the same rules on the fix, and shows before and after scores side by side.

Built for Hacktoberfest Hack Day Delhi x Braveon AI & RoundTable DTU (MLH).
Licence: MIT. Python 3.12, standard library only, no build step.

## How it works

1. Fetch the URL if given (15 s timeout, 2 MB cap). Pasted HTML is the fallback if the fetch fails.
2. Measure the HTML in code with the rules in `rules/`.
3. Send the screenshot (if any), the first 6,000 characters of HTML and the measured issues to Gemma in one request.
4. Merge measured issues into the model's issues, so measured facts are never lost.
5. Run the same rules on the model's `fixed_html` to get `score_after` and any remaining issues.
6. Offer a deploy bundle zip (`index.html`, Dockerfile, `.do/app.yaml`, README).

Score = 10 minus severity weights (high 2, medium 1, low 0.5), floored at 0.

## Run it

```
python3 app.py                 # DEMO MODE, no key needed
GEMINI_API_KEY=... python3 app.py   # live Gemma
```

Open http://localhost:8080.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | none | Gemini API key. Without it the app runs in DEMO MODE. |
| `GEMMA_MODEL` | `gemma-4-26b-a4b-it` | Model name sent to the Gemini API. |
| `PORT` | `8080` | Port to listen on. |

Never commit a key. `.do/app.yaml` declares `GEMINI_API_KEY` as an empty SECRET; set the value in the DigitalOcean dashboard.

### Demo mode

With no key, Polishr makes no Gemma call. It runs the measured checks and a simple auto-fix
(adds `lang`, a viewport tag and empty `alt`). The UI shows a DEMO MODE badge. Demo output is
not Gemma output. Screenshots are ignored in demo mode, so demo mode needs a URL or HTML.

## Write a rule pack (the open-source part)

Add a file to `rules/`. Every `rules/*.py` file is loaded at start-up and must expose `RULES = [fn, ...]`.
Each `fn(page)` returns a list of `{severity, category, finding, fix}`, where severity is `high`, `medium` or `low`.

```python
def has_pricing_word(page):
    if "price" in page.html.lower():
        return []
    return [{"severity": "low", "category": "cta_trust",
             "finding": "No pricing mentioned.", "fix": "Add a price or a 'contact for quote' line."}]

RULES = [has_pricing_word]
```

`page` has: `html`, `title`, `h1_count`, `viewport`, `meta_description`, `lang`, `imgs_total`,
`imgs_missing_alt`, `ctas`, `links`, `style_blocks`, `inline_styles`.
`rules/india_smb.py` is the example community pack (flags a missing `tel:` or `wa.me` link).
A rule that raises an error is skipped and logged; it cannot crash the app.

## API

- `GET /` the UI.
- `GET /health` returns `{ok, model, demo_mode, rules}`.
- `POST /api/analyze` takes `{url, image_b64, mime_type, html}`; at least one input is required, otherwise 400.
  Returns the Gemma fields plus `demo_mode`, `model`, `fetch_error`, `score_before`, `score_after`,
  `remaining_after_fix` and `original_html`. A Gemini error returns 502 with the API message.
- `POST /api/bundle` takes `{fixed_html}` and returns a zip, or 400 if the HTML has no `<html`.

Request bodies are capped at 12 MB. Nothing is stored: no database, no saved uploads.

## Deploy

`Dockerfile` runs `python3 app.py` on port 8080. `.do/app.yaml` targets DigitalOcean App Platform
(region `blr`, `apps-s-1vcpu-1gb`, health check `/health`). Check the `github.repo` value matches your repo.
**This repository has not been deployed by the author of this README; do not treat it as live.**

## Limits (please read)

- Only the first 6,000 characters of HTML reach the model. For long pages, the model's fix is built from a partial view.
- The contrast check reads hex colours only, in inline styles and `<style>` blocks. It ignores named colours, `rgb()`, CSS variables, gradients and images, and assumes a white background when none is given.
- Rules look at static HTML. They do not run JavaScript or load external CSS.
- A screenshot cannot show real mobile behaviour such as tap targets or overflow on a small screen.
- Gemma can hallucinate: its `fixed_html` may change wording, drop content or break layout. Always preview the fix before you deploy it. Previews are sandboxed iframes showing inline CSS only.
- The URL fetcher refuses private and local addresses and re-checks redirects, but it is not a hardened proxy (a DNS change between check and fetch is possible). Do not expose it to untrusted users without further protection.
- The model name `gemma-4-26b-a4b-it` and its support for `responseMimeType: application/json` were taken from the project brief and have not been confirmed against the live API.

## Licence

MIT, see `LICENSE`.
