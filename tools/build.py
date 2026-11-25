#!/usr/bin/env python3
"""Rebuild almahadtravels.com as a self-contained static site.

  python3 tools/build.py crawl   # find and download every page (1 request a second)
  python3 tools/build.py         # crawl (from cache) and write site/

Pages
  The homepage comes from source/home.html (the original HTML of the page
  saved in this folder). Every other page is found by following links from
  the homepage and from sitemap.xml, staying on almahadtravels.com and keeping
  out of what robots.txt disallows. Pages are cached in .cache/pages/, so a
  rebuild only fetches what is new; delete .cache/pages/ to refresh them.

Output
  site/<path>/index.html for each page, and site/assets/<host>/<path> for every
  stylesheet, script, image and font they load. Links between pages point at the
  local copies; a link to a page that does not exist on the live site either
  (404) keeps its live address. Pages reached through a redirect get a small
  redirect page at the old address.

Left out: analytics (Clarity, Meta Pixel, Google tag, Hotjar, GTM), the Tawk.to
live chat and reCAPTCHA, which are all tied to Al Mahad's own accounts. Small
no-op stand-ins keep the page code that calls them from erroring. Forms post to
Al Mahad's Django backend, so they do not send from the local copy.

Serve site/ at the root of a host (links are root-relative: /assets/..., /about-us/).
"""
import collections
import hashlib
import html as htmllib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source", "home.html")
OUT = os.path.join(ROOT, "site")
CACHE = os.path.join(ROOT, ".cache")
PAGE_CACHE = os.path.join(CACHE, "pages")
LIVE = "https://almahadtravels.com"
SITE_HOSTS = {"almahadtravels.com", "www.almahadtravels.com"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SITE_DELAY = 1.0          # seconds between requests to almahadtravels.com
MAX_PAGES = 2000          # safety stop for the crawl

# robots.txt (25 Nov 2026) + the admin area
DISALLOW = [
    "/admin", "/calendar", "/easter-umrah-packages", "/ramadan-umrah-packages",
    "/umrah-packages-january", "/umrah-packages-february", "/umrah-packages-march",
    "/umrah-packages-april", "/umrah-packages-may", "/umrah-packages-june",
    "/umrah-packages-july", "/umrah-packages-august", "/umrah-packages-september",
    "/umrah-packages-october", "/umrah-packages-november", "/umrah-packages-december",
]

# Hosts whose files are copied locally (assets only, never pages).
ASSET_HOSTS = {
    "almahad.s3.eu-west-2.amazonaws.com",
    "almahad.s3.amazonaws.com",
    "cdnjs.cloudflare.com",
    "cdn.jsdelivr.net",
    "maxcdn.bootstrapcdn.com",
    "stackpath.bootstrapcdn.com",
    "code.jquery.com",
    "unpkg.com",
    "ajax.googleapis.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "i.ytimg.com",
    "img.youtube.com",
    "i.pravatar.cc",
}

EXT_BY_TYPE = {
    "text/css": ".css", "application/javascript": ".js", "text/javascript": ".js",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/svg+xml": ".svg", "font/woff2": ".woff2",
    "font/woff": ".woff", "font/ttf": ".ttf", "application/pdf": ".pdf",
    "video/mp4": ".mp4", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
}

downloaded = {}   # asset url -> path relative to OUT (None if it failed)
missing = []
_last_request = {}


def throttle(host):
    gap = SITE_DELAY if host in SITE_HOSTS else 0.1
    wait = _last_request.get(host, 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.time()


# ------------------------------------------------------------- assets ---

def fetch(url):
    """Return (bytes, content_type), using .cache/ so each URL is fetched once."""
    key = hashlib.sha1(url.encode()).hexdigest()
    body_p = os.path.join(CACHE, key)
    meta_p = body_p + ".json"
    if os.path.exists(body_p) and os.path.exists(meta_p):
        with open(meta_p) as f:
            ctype = json.load(f)["type"]
        with open(body_p, "rb") as f:
            return f.read(), ctype
    throttle(urllib.parse.urlsplit(url).netloc)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": LIVE + "/"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    os.makedirs(CACHE, exist_ok=True)
    with open(body_p, "wb") as f:
        f.write(data)
    with open(meta_p, "w") as f:
        json.dump({"url": url, "type": ctype}, f)
    return data, ctype


def local_path(url, ctype):
    """assets/<host>/<path>, with a readable, safe file name."""
    u = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(u.path).lstrip("/") or "index"
    path = re.sub(r"[^A-Za-z0-9._/-]+", "-", path).strip("-")
    base, ext = os.path.splitext(path)
    if u.query:
        base += "-" + hashlib.sha1(u.query.encode()).hexdigest()[:8]
    if not ext:
        ext = EXT_BY_TYPE.get(ctype) or mimetypes.guess_extension(ctype or "") or ""
    return "assets/" + u.netloc + "/" + base + ext


def localise(url):
    """Download url (and, for CSS, what it references). Return path relative to OUT."""
    url = htmllib.unescape(url)
    if url in downloaded:
        return downloaded[url]
    try:
        data, ctype = fetch(url)
    except Exception as e:  # keep the live URL if a file cannot be fetched
        missing.append(f"{url}  ({e})")
        downloaded[url] = None
        return None
    rel = local_path(url, ctype)
    downloaded[url] = rel
    dest = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if rel.endswith(".css"):
        data = rewrite_css(data.decode("utf-8", "replace"), url, rel).encode("utf-8")
    with open(dest, "wb") as f:
        f.write(data)
    return rel


def is_asset_url(absu):
    u = urllib.parse.urlsplit(absu)
    if u.netloc in ASSET_HOSTS:
        return True
    # files served by the site itself (Django /static/ and /media/)
    return u.netloc in SITE_HOSTS and u.path.startswith(("/static/", "/media/"))


def rewrite_css(css, css_url, css_rel):
    here = os.path.dirname(css_rel)

    def repl(m):
        # one pass covers url(...) (also inside @import url(...)) and @import "..."
        if m.group(2) is not None:
            quote, raw, is_import = m.group(1), m.group(2), True
        else:
            quote, raw, is_import = m.group(3), m.group(4), False
        raw = raw.strip()
        if raw.startswith(("data:", "#")) or not raw:
            return m.group(0)
        absu = urllib.parse.urljoin(css_url, raw)
        if not is_asset_url(absu):
            return m.group(0)
        frag = ""
        if "#" in absu:  # e.g. font.svg#fontawesome
            absu, frag = absu.split("#", 1)
            frag = "#" + frag
        rel = localise(absu)
        if not rel:
            return m.group(0)
        new = os.path.relpath(rel, here) + frag
        return f"@import {quote}{new}{quote}" if is_import else f"url({quote}{new}{quote})"

    return re.sub(r"@import\s+(['\"])([^'\"]+)\1|url\(\s*(['\"]?)([^'\")]+)\3\s*\)", repl, css)


# -------------------------------------------------------------- pages ---

def get_page(url):
    """{status, final, type} plus body bytes; cached in .cache/pages/."""
    if url == LIVE + "/":
        with open(SRC, "rb") as f:
            return {"status": 200, "final": url, "type": "text/html"}, f.read()
    key = hashlib.sha1(url.encode()).hexdigest()
    body_p = os.path.join(PAGE_CACHE, key)
    meta_p = body_p + ".json"
    if os.path.exists(meta_p):
        with open(meta_p) as f:
            meta = json.load(f)
        body = b""
        if os.path.exists(body_p):
            with open(body_p, "rb") as f:
                body = f.read()
        return meta, body
    throttle(urllib.parse.urlsplit(url).netloc)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            meta = {"url": url, "status": r.status, "final": r.geturl(),
                    "type": (r.headers.get("Content-Type") or "").split(";")[0].strip()}
    except urllib.error.HTTPError as e:
        body = b""
        meta = {"url": url, "status": e.code, "final": url, "type": ""}
    except Exception as e:  # network trouble: not cached, so the next run retries
        print(f"  ! {url}: {e}")
        return {"url": url, "status": 0, "final": url, "type": ""}, b""
    os.makedirs(PAGE_CACHE, exist_ok=True)
    with open(body_p, "wb") as f:
        f.write(body)
    with open(meta_p, "w") as f:
        json.dump(meta, f)
    return meta, body


def page_url(href, base):
    """Canonical live URL of an on-site page link, or None (off-site, query, disallowed...)."""
    raw = htmllib.unescape(href).strip()
    if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "whatsapp:", "sms:")):
        return None
    u = urllib.parse.urlsplit(urllib.parse.urljoin(base, raw))
    if u.scheme not in ("http", "https") or u.netloc not in SITE_HOSTS:
        return None
    path = u.path or "/"
    if any(path.startswith(d) for d in DISALLOW) or path.startswith(("/static/", "/media/")):
        return None
    if u.query:  # only list paging (?page=N) is mirrored; other queries stay live
        m = re.fullmatch(r"page=(\d+)", u.query)
        if not m:
            return None
        return LIVE + path + ("" if m.group(1) == "1" else "?page=" + m.group(1))
    return LIVE + path


def strip_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


A_HREF = re.compile(r"""(<a\b[^>]*?\bhref\s*=\s*)(["'])(.*?)\2""", re.S | re.I)


def page_links(body, base):
    text = strip_comments(body.decode("utf-8", "replace"))
    for m in A_HREF.finditer(text):
        p = page_url(m.group(3), base)
        if p:
            yield p


def sitemap_urls():
    meta, body = get_page(LIVE + "/sitemap.xml")
    if meta["status"] != 200:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body.decode("utf-8", "replace"))
    return [p for p in (page_url(l, LIVE + "/") for l in locs) if p]


def is_html(meta):
    return meta["type"] in ("text/html", "application/xhtml+xml")


def crawl():
    """Breadth-first over every reachable page. Returns {url: meta}."""
    pages = {}
    queue = collections.deque([LIVE + "/"] + sitemap_urls())
    queued = set(queue)
    fetched = 0
    while queue and len(pages) < MAX_PAGES:
        url = queue.popleft()
        meta, body = get_page(url)
        pages[url] = meta
        fetched += 1
        if fetched % 25 == 0:
            print(f"  {fetched} pages, {len(queue)} queued")
        if meta["status"] == 200 and is_html(meta):
            final_host = urllib.parse.urlsplit(meta["final"]).netloc
            if final_host in SITE_HOSTS:
                for link in page_links(body, meta["final"]):
                    if link not in queued:
                        queued.add(link)
                        queue.append(link)
    if queue:
        print(f"  stopped at MAX_PAGES={MAX_PAGES}; {len(queue)} still queued")
    with open(os.path.join(CACHE, "crawl.json"), "w") as f:
        json.dump(pages, f, indent=1)
    ok = [u for u, m in pages.items() if m["status"] == 200]
    print(f"crawl: {len(pages)} URLs, {len(ok)} OK, "
          f"{len([u for u in ok if pages[u]['final'] != u])} redirected, "
          f"{len(pages) - len(ok)} failed")
    return pages


# --------------------------------------------------------------- HTML ---

TRACKING_NOTE = ("<!-- Analytics (Clarity, Meta Pixel, Google tag, Hotjar, GTM), the Tawk.to "
                 "live chat and reCAPTCHA are Al Mahad's own accounts: left out of this "
                 "local rebuild. -->")
STUBS = ("<script>/* local rebuild: no-op stand-ins for the analytics that were left out */"
         "window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
         "function fbq(){}window.hj=window.hj||function(){};window.clarity=window.clarity||function(){};"
         "window.Tawk_API=window.Tawk_API||{maximize:function(){},toggle:function(){},"
         "showWidget:function(){},hideWidget:function(){},popup:function(){}};"
         "function gtag_report_conversion(url){if(typeof url!=='undefined'){window.location=url;}"
         "return false;}"
         # reCAPTCHA's key only works on almahadtravels.com; forms get an empty token instead
         "window.grecaptcha=window.grecaptcha||{ready:function(f){f();},"
         "execute:function(){return Promise.resolve('');}};</script>")

TRACKER_SIGNS = ("googletagmanager.com", "clarity.ms", "connect.facebook.net", "facebook.com/tr",
                 "static.hotjar.com", "embed.tawk.to", "gtag('config'", 'gtag("config"',
                 "google.com/recaptcha/api.js")


def strip_tracking(html):
    html = re.sub(r"<!-- SEO START -->.*?<!-- SEO END -->", TRACKING_NOTE, html, flags=re.S)

    # any tracker snippet outside that block (other pages put them elsewhere)
    def drop(m):
        return "" if any(s in m.group(0) for s in TRACKER_SIGNS) else m.group(0)
    html = re.sub(r"<script\b[^>]*>.*?</script>", drop, html, flags=re.S | re.I)
    html = re.sub(r"<noscript\b[^>]*>.*?</noscript>", drop, html, flags=re.S | re.I)
    # gtag_report_conversion's own definition goes with the Google tag
    html = re.sub(r"<script>\s*function gtag_report_conversion\(url\).*?</script>", "", html, flags=re.S)
    html = re.sub(r"(<head\b[^>]*>)", r"\1" + STUBS, html, count=1, flags=re.I)
    return html


def local_href(final_url):
    """Root-relative local address of a mirrored page (?page=N becomes .../page/N/)."""
    u = urllib.parse.urlsplit(final_url)
    path = u.path or "/"
    m = re.fullmatch(r"page=(\d+)", u.query)
    if m:
        return path.rstrip("/") + f"/page/{m.group(1)}/"
    if path.endswith("/") or os.path.splitext(path)[1]:
        return path
    return path + "/"


def page_file(final_url):
    path = urllib.parse.unquote(local_href(final_url))
    if path.endswith("/"):
        path += "index.html"
    return os.path.join(OUT, path.lstrip("/"))


ASSET_START = re.compile(r"https?://(?:%s)/" % "|".join(re.escape(h) for h in ASSET_HOSTS))
# Where a URL ends depends on what opens it: file names here contain brackets
# and apostrophes, e.g. src=".../hotel+images_Snood+Ajyad+(Sunood).webp".
URL_STOP = {'"': '"<> \t\n', "'": "'<> \t\n", "(": ")\"'<> \t\n"}
BARE_STOP = ")\"'<> \t\n"


def replace_asset_urls(seg, fn):
    """Call fn(url) for every third-party asset URL in seg and splice in its result."""
    out, pos = [], 0
    for m in ASSET_START.finditer(seg):
        start = m.start()
        if start < pos:
            continue
        stop = URL_STOP.get(seg[start - 1] if start else "", BARE_STOP)
        end = start
        while end < len(seg) and seg[end] not in stop:
            end += 1
        url = seg[start:end]
        cut = url.find("&quot;")  # style="background:url(&quot;...&quot;)"
        if cut != -1:
            url, end = url[:cut], start + cut
        out.append(seg[pos:start])
        out.append(fn(url))
        pos = end
    out.append(seg[pos:])
    return "".join(out)
SITE_FILE = re.compile(r"""(\b(?:src|href|data-src|poster|content)\s*=\s*["']|url\(\s*["']?)"""
                       r"""((?:https?://(?:www\.)?almahadtravels\.com)?/(?:static|media)/[^"'\s)<>]+)""")


def rewrite_segment(seg, base, pages):
    """Rewrite one non-comment piece of a page."""
    # 1. third-party asset URLs (attributes, inline <style>, style="", inline JS strings)
    def asset(url):
        if url.endswith("/"):  # a URL prefix built up in inline JS
            return url
        rel = localise(url)
        return "/" + rel if rel else url
    seg = replace_asset_urls(seg, asset)

    # 2. the site's own /static/ and /media/ files
    def site_file(m):
        rel = localise(urllib.parse.urljoin(LIVE + "/", htmllib.unescape(m.group(2))))
        return m.group(1) + ("/" + rel if rel else m.group(2))
    seg = SITE_FILE.sub(site_file, seg)

    # 3. links between pages
    def link(m):
        href = m.group(3)
        raw = htmllib.unescape(href).strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "whatsapp:", "sms:",
                                      "/assets/")):  # /assets/: a linked image, already local
            return m.group(0)
        absu = urllib.parse.urljoin(base, raw)
        u = urllib.parse.urlsplit(absu)
        if u.netloc not in SITE_HOSTS:
            return m.group(0)
        frag = "#" + u.fragment if u.fragment else ""
        target = page_url(raw, base)
        meta = pages.get(target) if target else None
        if meta and meta["status"] == 200:
            final = meta["final"]
            new = (local_href(final) if urllib.parse.urlsplit(final).netloc in SITE_HOSTS
                   else final) + frag
        else:  # not mirrored (missing on the live site too, has a query, disallowed): keep live
            new = urllib.parse.urlunsplit(("https", "almahadtravels.com", u.path, u.query, u.fragment))
        return m.group(1) + m.group(2) + new.replace("&", "&amp;") + m.group(2)
    return A_HREF.sub(link, seg)


def build_page(body, base, pages):
    html = strip_tracking(body.decode("utf-8", "replace"))
    parts = re.split(r"(<!--.*?-->)", html, flags=re.S)  # leave commented-out code alone
    result = "".join(p if p.startswith("<!--") else rewrite_segment(p, base, pages) for p in parts)
    # Captured CSRF tokens are session-specific and must not be published.
    def clear_csrf(match):
        tag = match.group(0)
        if not re.search(r"\bname\s*=\s*([\"'])csrfmiddlewaretoken\1", tag, re.I):
            return tag
        return re.sub(r"(\bvalue\s*=\s*)([\"'])(.*?)\2",
                      lambda m: m.group(1) + m.group(2) + m.group(2), tag, flags=re.I | re.S)
    return re.sub(r"<input\b[^>]*>", clear_csrf, result, flags=re.I)


REDIRECT_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Redirecting</title>
<meta http-equiv="refresh" content="0; url={0}"><link rel="canonical" href="{0}">
<script>location.replace({1} + location.hash);</script></head>
<body><a href="{0}">Continue</a></body></html>
"""


def build():
    pages = crawl()
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    written, redirects, files = 0, 0, 0
    for url, meta in pages.items():
        if meta["status"] != 200:
            continue
        final = meta["final"]
        if urllib.parse.urlsplit(final).netloc not in SITE_HOSTS:
            continue
        dest = page_file(final)
        if not os.path.exists(dest):
            _, body = get_page(url)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if is_html(meta):
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(build_page(body, final, pages))
                written += 1
            else:  # a PDF or image linked as a page
                with open(dest, "wb") as f:
                    f.write(body)
                files += 1
    # old addresses that redirect: a small page that forwards to the new one
    for url, meta in pages.items():
        if meta["status"] != 200 or meta["final"] == url:
            continue
        old = page_file(url)
        if os.path.exists(old):
            continue
        final = meta["final"]
        target = local_href(final) if urllib.parse.urlsplit(final).netloc in SITE_HOSTS else final
        os.makedirs(os.path.dirname(old), exist_ok=True)
        with open(old, "w", encoding="utf-8") as f:
            f.write(REDIRECT_PAGE.format(htmllib.escape(target), json.dumps(target)))
        redirects += 1

    # Favicon is .ico: keep it working at /favicon.ico too.
    ico = downloaded.get("https://almahad.s3.eu-west-2.amazonaws.com/images/almahadico.ico")
    if ico:
        shutil.copy(os.path.join(OUT, ico), os.path.join(OUT, "favicon.ico"))

    size = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(OUT) for fn in fns)
    print(f"site/: {written} pages, {redirects} redirect pages, {files} linked files, "
          f"{len([1 for p in downloaded.values() if p])} assets; {size / 1e6:.1f} MB")
    failed = [(u, m["status"]) for u, m in pages.items() if m["status"] != 200]
    if failed:
        print("Pages that fail on the live site too (links keep the live address):")
        for u, s in failed:
            print(f"  {s}  {u}")
    if missing:
        print("Assets not downloaded (left pointing at the live URL):")
        for m in missing:
            print("  " + m)


if __name__ == "__main__":
    if sys.argv[1:] == ["crawl"]:
        crawl()
    else:
        build()
