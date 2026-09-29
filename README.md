# Experience AI scraper

Downloads every resource from https://experience-ai.org across all 25 locales
and all offered formats (PDF, DOCX, PPTX), plus each page's text as Markdown.

## Setup

```bash
pip install -r requirements.txt
```

## 1. Add your session cookie (needed for lesson-level resources)

Lesson plans, slides and worksheets are only listed for logged-in users.
Unit-level files (overviews, assessments, glossaries) are public.

1. In your normal browser, log in at https://experience-ai.org/en ("Log in").
   If it asks the follow-up questions about how you'll use the resources, finish them.
2. Open DevTools (F12) → **Application** (Chrome/Edge) or **Storage** (Firefox)
   → **Cookies** → `https://experience-ai.org`.
3. Copy the **value** of `_experience_ai_session` into a file named `cookie.txt`
   next to `scrape.py` (just the value, one line).

Treat `cookie.txt` like a password. It gives access to your account until you log out.

Check it works:

```bash
python scrape.py check-auth
```

It should print `logged in; N download links on lesson 12`.

## 2. Run

English first (the main target). This crawls, resolves and downloads English only:

```bash
python scrape.py all --locales en
```

Then the rest. English is skipped because it's already cached and downloaded:

```bash
python scrape.py all
```

`all` runs the stages in order, and each stage finishes for every selected locale before the next one
starts. So no files are downloaded until the crawl of all selected locales is done. That's why
it's worth doing English on its own first. `en-US` and `en-IN` are separate English variants;
add them with `--locales en,en-US,en-IN` if you want them.

You can also run the stages one at a time:

| Stage      | What it does                                                               |
|------------|----------------------------------------------------------------------------|
| `crawl`    | Fetches every page of every locale (cached in `cache/`), builds `data/catalog.json` |
| `resolve`  | Maps each (locale, resource) to its Google Docs/Drive file                 |
| `verify`   | Spot-checks the resolved URLs against the site's own download links        |
| `download` | Downloads each unique file once and writes `data/manifest.csv`             |

Useful options:

- `--locales en,de,pl`: only these locales (default is all 25). Applies to every stage.
- `--delay 1.5`: slow down if you see many retries (default is 0.8 s between requests)
- `--refresh`: ignore the page cache
- `--no-link`: don't hardlink English fallback files into other locales' folders

Every stage is **resumable**. If it stops, run the same command again and it
continues where it left off.

The crawl log line should say `0 gated pages`. If it doesn't, your cookie isn't working.

## Output

```
output/<locale>/<unit>/_unit/                 unit-level files + page.md
output/<locale>/<unit>/lesson-<id>-<title>/   lesson files + page.md (+ videos.txt)
output/<locale>/standalone-lessons/...        lessons not inside a unit
output/<locale>/_pages/...                    home, themes, partners pages (page.md)
data/manifest.csv                             one row per file: locale, page, title,
                                              format, Google ID, status, local path, source URL
```

When a locale has no translation of a resource, the site serves the English
file. That file is downloaded once and hardlinked into the locale's folder, so
every locale folder is complete without using extra disk. In the manifest,
`status` is `downloaded`, `linked` (a fallback/duplicate), `exists` (downloaded
in an earlier run), or `failed (...)`.

## Troubleshooting

- **`WARNING: ... is still gated while authenticated`**: the cookie expired or the
  post-login questions weren't finished. Log in again, update `cookie.txt`, and re-run.
- **`retry N for ...`**: the site (Cloudflare) is throttling. The script backs off
  automatically. If it happens constantly, use `--delay 1.5`.
- **Fresh start**: delete `cache/`, `data/` and `output/`.

The materials are published by the Raspberry Pi Foundation, mostly under
CC BY-NC-SA 4.0. Keep attribution and non-commercial terms if you share them.
