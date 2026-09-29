"""Scraper for https://experience-ai.org — all locales, all resource formats.

Stages (run in order, each is resumable):
  crawl     Fetch every page of every locale into cache/, build data/catalog.json
  resolve   Map each (locale, resource id) to its Google Drive/Docs file ID
  download  Download each unique Google file once, hardlink locale fallbacks
  all       crawl + resolve + download

Authentication: lesson-level resources are only listed for logged-in users.
Put the value of the `_experience_ai_session` cookie in cookie.txt (or the
EAI_COOKIE env var). Without it the crawl still runs but lesson pages are gated.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://experience-ai.org"
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache" / "html"
DATA = ROOT / "data"
OUT = ROOT / "output"
LOCALES = ["en", "en-US", "yo", "zu", "en-IN", "sw", "ar", "ms", "de", "et", "el", "es",
           "es-419", "fr-CA", "ga", "hr-HR", "it", "lv", "lt", "pl", "pt-BR", "pt-PT",
           "ro", "tr", "uk"]
GATED_MARKER = "c-download-materials__login-button"
DOWNLOAD_RE = re.compile(r"/drive_resources/download/(\d+)\.([A-Za-z0-9]+)$")
REDIRECT_RE = re.compile(r"/drive_resources/redirect/(\d+)$")
YOUTUBE_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=)|youtu\.be/)([\w-]{11})")
# Paths inside a locale that are not content pages
SKIP_PATH_RE = re.compile(r"^/[^/]+/(links|auth|drive_resources|users|sign_out|logout)(/|$)")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- HTTP client

class Client:
    """Polite HTTP client: fixed delay per host, exponential backoff on resets/429/5xx."""

    def __init__(self, cookie=None, delay=0.8):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36")
        self.authenticated = bool(cookie)
        if cookie:
            self.s.cookies.set("_experience_ai_session", cookie, domain="experience-ai.org", path="/")
        self.delay = delay
        self.last = {}

    def get(self, url, **kw):
        """Throttling (429, resets, timeouts) gets a long backoff and raises FetchError if it
        never clears. A 5xx is usually a broken page, not throttling: after a few quick
        retries the 5xx response is returned for the caller to record and skip."""
        host = urlparse(url).netloc
        delay = self.delay if host == "experience-ai.org" else 0.3
        server_errors = 0
        for attempt in range(7):
            wait = self.last.get(host, 0) + delay - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=60, **kw)
                self.last[host] = time.time()
                if r.status_code >= 500:
                    server_errors += 1
                    if server_errors >= 3:
                        log(f"  HTTP {r.status_code} persists for {url} — skipping")
                        return r
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                if r.status_code == 429:
                    raise requests.HTTPError("HTTP 429")
                return r
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                self.last[host] = time.time()
                backoff = min(5 * 2 ** attempt, 300)
                log(f"  retry {attempt + 1} for {url} in {backoff}s ({e.__class__.__name__}: {str(e)[:80]})")
                time.sleep(backoff)
        raise FetchError(f"giving up on {url}")


class FetchError(Exception):
    pass


def load_cookie():
    c = os.environ.get("EAI_COOKIE")
    f = ROOT / "cookie.txt"
    if not c and f.exists():
        c = f.read_text(encoding="utf8").strip()
    if c and c.startswith("_experience_ai_session="):
        c = c.split("=", 1)[1]
    return c or None


def jload(p, default):
    return json.loads(p.read_text(encoding="utf8")) if p.exists() else default


def jsave(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf8")
    tmp.replace(p)


# ---------------------------------------------------------------- crawl

def cache_path(path):
    return CACHE / (path.strip("/").replace("/", "__") + ".html")


def fetch_page(client, path, refresh=False):
    """Return (status, html). Cached; a gated page is refetched once we are authenticated."""
    cp, meta_p = cache_path(path), cache_path(path).with_suffix(".json")
    if cp.exists() and not refresh:
        meta = jload(meta_p, {})
        html = cp.read_text(encoding="utf8")
        stale = client.authenticated and not meta.get("auth") and GATED_MARKER in html
        if not stale:
            return meta.get("status", 200), html
    r = client.get(BASE + path, allow_redirects=False)
    if r.status_code >= 500:
        return r.status_code, ""    # not cached, so the next run tries it again
    html = r.text if r.status_code == 200 else ""
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(html, encoding="utf8")
    jsave(meta_p, {"status": r.status_code, "auth": client.authenticated,
                   "location": r.headers.get("location"), "fetched": time.time()})
    return r.status_code, html


def text_of(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def page_kind(path):
    parts = path.strip("/").split("/")[1:]
    if not parts:
        return "home"
    if parts[0] == "units" and len(parts) == 1:
        return "units_index"
    if parts[0] == "units" and len(parts) == 2:
        return "unit"
    if parts[0] == "units" and len(parts) == 4 and parts[2] == "lessons":
        return "lesson"
    if parts[0] == "lessons" and len(parts) == 2:
        return "lesson"
    if parts[0] == "themes":
        return "theme"
    return parts[0]


def to_markdown(root, page_url):
    """Minimal HTML -> Markdown for the page body (headings, paragraphs, lists, links)."""
    lines = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "iframe"]):
        if el.find_parent(["nav", "footer"]) or el.find_parent(id=re.compile("Cookiebot")):
            continue
        if el.name == "iframe":
            src = el.get("src") or el.get("data-src") or el.get("data-cookieblock-src")
            if src and "googletagmanager" not in src:
                lines.append(f"[Embedded video]({src})")
            continue
        if el.name == "p" and el.find_parent("li"):
            continue
        t = text_of(el)
        if not t:
            continue
        link = el.find_parent("a", href=True)
        if link:
            lines.append(f"[{t}]({urljoin(page_url, link['href'])})")
            continue
        for a in el.find_all("a", href=True):
            at = text_of(a)
            if at:
                t = t.replace(at, f"[{at}]({urljoin(page_url, a['href'])})", 1)
        if el.name[0] == "h":
            lines.append("\n" + "#" * int(el.name[1]) + " " + t)
        elif el.name == "li":
            lines.append("- " + t)
        else:
            lines.append("\n" + t)
    md = "\n".join(lines).strip() + "\n"
    return re.sub(r"\n{3,}", "\n\n", md)


def parse_page(path, html):
    soup = BeautifulSoup(html, "html.parser")
    url = BASE + path
    main = soup.find("main") or soup.body or soup
    h1 = main.find("h1")
    info = {
        "path": path,
        "url": url,
        "locale": path.strip("/").split("/")[0],
        "kind": page_kind(path),
        "title": text_of(h1),
        "gated": GATED_MARKER in html,
        "resources": [],
        "videos": sorted(set(YOUTUBE_RE.findall(html))),
        "links": [],
        "markdown": to_markdown(main, url),
    }
    # File cards: one resource (several formats) per card
    seen = set()
    for a in main.find_all("a", href=True):
        href = urljoin(url, a["href"])
        m = DOWNLOAD_RE.search(urlparse(href).path)
        if not m:
            continue
        rid, ext = m.group(1), m.group(2).lower()
        card = a.find_parent(class_="c-file-card")
        title, updated = "", ""
        if card:
            title = text_of(card.find(["h3", "h2", "h4"]))
            p = card.select_one(".c-file-card__data-text p")
            updated = text_of(p)
        else:
            prev = a.find_previous(["h3", "h2", "h4"])
            title = text_of(prev)
        key = (rid, ext)
        if key in seen:
            continue
        seen.add(key)
        # Section heading above the card (e.g. "Lessons", "Teacher resources")
        section = a.find_previous(["h1", "h2"])
        info["resources"].append({"id": rid, "ext": ext, "title": title or f"resource-{rid}",
                                  "updated": updated, "section": text_of(section)})
    for a in main.find_all("a", href=True):
        href = urljoin(url, a["href"])
        info["links"].append(href)
    info["links"] = sorted(set(info["links"]))
    return info


def crawl(client, locales, refresh=False):
    catalog = jload(DATA / "catalog.json", {})
    unknown = set()
    failed = []
    for loc in locales:
        queue = [f"/{loc}", f"/{loc}/units", f"/{loc}/partners"]
        seen = set()
        n_gated = 0
        while queue:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            try:
                status, html = fetch_page(client, path, refresh)
            except FetchError as e:
                log(f"  FAILED {path}: {e}")
                status, html = "unreachable", ""
            if status != 200:
                catalog[path] = {"path": path, "locale": loc, "status": status}
                if status == "unreachable" or status >= 500:
                    failed.append(f"{path} ({status})")
                continue
            info = parse_page(path, html)
            info["status"] = status
            n_gated += info["gated"]
            if client.authenticated and info["gated"]:
                log(f"WARNING: {path} is still gated while authenticated — cookie expired or questionnaire unfinished?")
            catalog[path] = info
            for href in info["links"]:
                pu = urlparse(href)
                if pu.netloc != "experience-ai.org":
                    continue
                p = pu.path.rstrip("/") or "/"
                if not p.startswith(f"/{loc}/") or p.startswith("/assets"):
                    continue
                if DOWNLOAD_RE.search(p) or REDIRECT_RE.search(p) or SKIP_PATH_RE.match(p):
                    continue
                if p not in seen:
                    queue.append(p)
            for href in info["links"]:
                p = urlparse(href).path
                if "experience-ai.org" in href and not p.startswith(f"/{loc}") and not p.startswith("/assets") \
                        and not any(p.startswith(f"/{l}/") or p == f"/{l}" for l in LOCALES):
                    unknown.add(p)
        n_res = sum(len(v.get("resources", [])) for k, v in catalog.items() if v.get("locale") == loc)
        log(f"[{loc}] {len(seen)} pages, {n_res} resource links, {n_gated} gated pages")
        jsave(DATA / "catalog.json", catalog)
    if unknown:
        log("Paths outside locale trees (not crawled):", sorted(unknown)[:30])
    if failed:
        log(f"{len(failed)} page(s) failed (server errors; re-run later to retry): " + ", ".join(failed))
    return catalog


# ---------------------------------------------------------------- resolve

GDOC_RE = re.compile(r"https://docs\.google\.com/(document|presentation|spreadsheets|drawings)/d/([\w-]+)")
GFILE_RE = re.compile(r"https://drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=download&)?id=)([\w-]+)")


def export_url(kind, gid, ext):
    if kind == "presentation":
        return f"https://docs.google.com/presentation/d/{gid}/export/{ext}"
    if kind == "drawings":
        return f"https://docs.google.com/drawings/d/{gid}/export/{ext}"
    return f"https://docs.google.com/{kind}/d/{gid}/export?format={ext}"


def resolve(client, locales):
    """For each (locale, id): follow the Google Drive redirect once to learn the file ID.
    Download URLs for native Google files are then built locally; anything else is
    resolved per format through the site's own download endpoint."""
    catalog = jload(DATA / "catalog.json", {})
    resolved = jload(DATA / "resolved.json", {})
    wanted = {}
    for page in catalog.values():
        if page.get("locale") not in locales:
            continue
        for r in page.get("resources", []):
            wanted.setdefault(f"{page['locale']}/{r['id']}", set()).add(r["ext"])
    todo = [k for k in wanted
            if set(wanted[k]) - {e for e, u in resolved.get(k, {}).get("urls", {}).items() if u}]
    log(f"resolve: {len(wanted)} (locale, resource) pairs, {len(todo)} to do")
    failed = []
    for i, key in enumerate(todo, 1):
        loc, rid = key.split("/")
        entry = resolved.get(key, {"urls": {}})
        try:
            if not entry.get("target"):
                r = client.get(f"{BASE}/{loc}/drive_resources/redirect/{rid}", allow_redirects=False)
                entry["target"] = r.headers.get("location")
            m = GDOC_RE.match(entry["target"] or "")
            for ext in sorted(wanted[key]):
                if entry["urls"].get(ext):
                    continue
                if m:
                    entry["kind"], entry["gid"] = m.group(1), m.group(2)
                    entry["urls"][ext] = export_url(m.group(1), m.group(2), ext)
                else:
                    r = client.get(f"{BASE}/{loc}/drive_resources/download/{rid}.{ext}", allow_redirects=False)
                    loc_url = r.headers.get("location")
                    if not loc_url:     # 5xx or no redirect: leave unresolved so the next run retries
                        failed.append(f"{key}.{ext} (HTTP {r.status_code})")
                        continue
                    fm = GFILE_RE.match(loc_url) or GDOC_RE.match(loc_url)
                    entry["kind"] = "file"
                    entry["gid"] = fm.groups()[-1] if fm else None
                    entry["urls"][ext] = loc_url
        except FetchError as e:
            failed.append(f"{key} ({e})")
        resolved[key] = entry
        if i % 25 == 0 or i == len(todo):
            jsave(DATA / "resolved.json", resolved)
            log(f"  resolved {i}/{len(todo)}")
    jsave(DATA / "resolved.json", resolved)
    if failed:
        log(f"{len(failed)} resource(s) could not be resolved (re-run later to retry): " + ", ".join(failed[:20]))
    return resolved


def verify_resolution(client, samples=6):
    """Spot-check that locally built export URLs match what the site's download endpoint returns."""
    resolved = jload(DATA / "resolved.json", {})
    bad = 0
    for key, e in list(resolved.items())[:: max(1, len(resolved) // samples)][:samples]:
        loc, rid = key.split("/")
        if not e.get("urls"):
            continue
        ext = sorted(e["urls"])[0]
        try:
            r = client.get(f"{BASE}/{loc}/drive_resources/download/{rid}.{ext}", allow_redirects=False)
        except FetchError as err:
            log(f"  verify {key}.{ext}: skipped ({err})")
            continue
        ok = r.headers.get("location") == e["urls"][ext]
        bad += not ok
        log(f"  verify {key}.{ext}: {'ok' if ok else 'MISMATCH ' + str(r.headers.get('location'))}")
    return bad == 0


# ---------------------------------------------------------------- download

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name, maxlen=80):
    name = INVALID.sub("_", name).strip(" .")
    return (name[:maxlen].rstrip(" .") or "untitled")


def page_dir(page, catalog):
    """output/<locale>/<unit>/<lesson>/ — lesson folders nested under their unit."""
    loc = page["locale"]
    parts = page["path"].strip("/").split("/")[1:]
    if page["kind"] == "unit":
        return OUT / loc / safe(parts[1]) / "_unit"
    if page["kind"] == "lesson" and parts[0] == "units":
        return OUT / loc / safe(parts[1]) / f"lesson-{parts[3]}-{safe(page['title'], 60)}"
    if page["kind"] == "lesson":
        return OUT / loc / "standalone-lessons" / f"lesson-{parts[1]}-{safe(page['title'], 60)}"
    return OUT / loc / "_pages" / safe("__".join(parts) or "home")


def download(client, locales, link_fallbacks=True):
    catalog = jload(DATA / "catalog.json", {})
    resolved = jload(DATA / "resolved.json", {})
    blobs = jload(DATA / "blobs.json", {})          # "<gid>.<ext>" -> first local path
    manifest = []
    pages = [p for p in catalog.values() if p.get("status") == 200 and p.get("locale") in locales]
    total = sum(len(p["resources"]) for p in pages)
    done = 0
    for page in sorted(pages, key=lambda p: (p["locale"] != "en", p["path"])):
        d = page_dir(page, catalog)
        d.mkdir(parents=True, exist_ok=True)
        (d / "page.md").write_text(f"<!-- {page['url']} -->\n" + page["markdown"], encoding="utf8")
        if page["videos"]:
            (d / "videos.txt").write_text(
                "\n".join(f"https://www.youtube.com/watch?v={v}" for v in page["videos"]) + "\n", encoding="utf8")
        used = set()
        for r in page["resources"]:
            done += 1
            e = resolved.get(f"{page['locale']}/{r['id']}")
            url = e and e["urls"].get(r["ext"])
            row = {"locale": page["locale"], "page": page["path"], "page_title": page["title"],
                   "section": r["section"], "resource_id": r["id"], "title": r["title"],
                   "updated": r["updated"], "format": r["ext"], "google_id": e and e.get("gid"),
                   "source_url": url, "local_path": None, "status": None}
            if not url:
                row["status"] = "unresolved"
                manifest.append(row)
                continue
            base = safe(r["title"])
            fname = f"{base}.{r['ext']}"
            if fname.lower() in used:
                fname = f"{base} ({r['id']}).{r['ext']}"
            used.add(fname.lower())
            dest = d / fname
            bkey = f"{e.get('gid') or url}.{r['ext']}"
            first = blobs.get(bkey)
            if dest.exists() and dest.stat().st_size > 0:
                row["status"] = "exists"
            elif first and (ROOT / first).exists():
                if link_fallbacks:
                    try:
                        os.link(ROOT / first, dest)
                    except OSError:
                        dest.write_bytes((ROOT / first).read_bytes())
                    row["status"] = "linked"
                else:
                    row["status"] = "duplicate"
                    row["local_path"] = first
            else:
                try:
                    resp = client.get(url, allow_redirects=True)
                except FetchError as err:
                    row["status"] = "failed (unreachable)"
                    log(f"  FAILED {page['locale']} {r['title']}.{r['ext']}: {err}")
                    manifest.append(row)
                    continue
                ctype = resp.headers.get("content-type", "")
                if resp.status_code != 200 or ctype.startswith("text/html"):
                    row["status"] = f"failed ({resp.status_code} {ctype.split(';')[0]})"
                    log(f"  FAILED {page['locale']} {r['title']}.{r['ext']}: {row['status']}")
                    manifest.append(row)
                    continue
                tmp = dest.with_name(dest.name + ".part")
                tmp.write_bytes(resp.content)
                tmp.replace(dest)
                row["status"] = "downloaded"
            rel = str(dest.relative_to(ROOT)).replace("\\", "/")
            row["local_path"] = row["local_path"] or rel
            blobs.setdefault(bkey, rel)
            manifest.append(row)
            if done % 20 == 0:
                jsave(DATA / "blobs.json", blobs)
                log(f"  {done}/{total} resource files")
    jsave(DATA / "blobs.json", blobs)
    # Keep manifest rows for locales not processed in this run
    kept = [m for m in jload(DATA / "manifest.json", []) if m["locale"] not in locales]
    write_manifest(kept + manifest)
    counts = {}
    for m in manifest:
        counts[m["status"].split(" ")[0]] = counts.get(m["status"].split(" ")[0], 0) + 1
    log("download summary:", counts)


def write_manifest(rows):
    import csv
    jsave(DATA / "manifest.json", rows)
    cols = ["locale", "page", "page_title", "section", "resource_id", "title", "updated",
            "format", "google_id", "status", "local_path", "source_url"]
    with open(DATA / "manifest.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- main

def check_auth(client):
    status, html = fetch_page(client, "/en/units/understanding-ai-11-14/lessons/12", refresh=True)
    if status != 200:
        log(f"auth check: HTTP {status}")
        return False
    gated = GATED_MARKER in html
    n = len(set(re.findall(r"/drive_resources/download/\d+\.\w+", html)))
    log(f"auth check: {'GATED (not logged in)' if gated else 'logged in'}; {n} download links on lesson 12")
    return not gated


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["check-auth", "crawl", "resolve", "verify", "download", "all"])
    ap.add_argument("--locales", default="all", help="comma list, or 'all' (default)")
    ap.add_argument("--delay", type=float, default=0.8, help="seconds between requests to experience-ai.org")
    ap.add_argument("--refresh", action="store_true", help="ignore the HTML cache")
    ap.add_argument("--no-link", action="store_true", help="don't hardlink English fallbacks into other locales")
    a = ap.parse_args()
    locales = LOCALES if a.locales == "all" else a.locales.split(",")
    cookie = load_cookie()
    client = Client(cookie, a.delay)
    log(f"authenticated: {bool(cookie)}; locales: {len(locales)}")
    if a.stage == "check-auth":
        sys.exit(0 if check_auth(client) else 1)
    if a.stage in ("crawl", "all"):
        if cookie and not check_auth(client):
            sys.exit("Cookie present but lesson pages are still gated — refresh cookie.txt (and finish the "
                     "post-login questionnaire in the browser if you haven't).")
        crawl(client, locales, a.refresh)
    if a.stage in ("resolve", "all"):
        resolve(client, locales)
    if a.stage in ("verify", "all"):
        verify_resolution(client)
    if a.stage in ("download", "all"):
        download(client, locales, link_fallbacks=not a.no_link)


if __name__ == "__main__":
    main()
