#!/usr/bin/env python3
"""
Resolve every artwork in artworks.json to a concrete image and pin the
result back into the file.

The page works without this: it resolves images live at runtime. Pinning
makes the common case fast (no API round trip) and reports entries that
cannot be resolved at all, so they can be fixed deliberately.

Usage:
    python3 pin.py                  # resolve everything, write back
    python3 pin.py --check          # report only, change nothing
    python3 pin.py --stale          # only entries with nothing pinned yet
    python3 pin.py --verify         # also confirm each image actually loads
    python3 pin.py --report FILE    # append a Markdown summary

Standard library only.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "artworks.json")

# Wikimedia's policy wants a descriptive agent with a contact URL. Requests
# without one get throttled or refused, which is much of why the first
# version of this script failed when run from a CI runner.
UA = ("jacobmonzel-art-daily/2.0 "
      "(https://www.jacobmonzel.com; static site image pinner) "
      "python-urllib")

TIMEOUT = 45
AIC_BATCH = 100      # AIC accepts up to 100 ids per request
WIKI_BATCH = 50      # MediaWiki accepts up to 50 titles per request
RETRIES = 4


def fetch_json(url):
    """GET with exponential backoff. 429 and 5xx are retried; other 4xx are not."""
    delay = 1.5
    last = None
    for _ in range(RETRIES):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(last or "request failed")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- AIC

def resolve_aic(works):
    """id -> (image_id, date_display), batched."""
    out = {}
    ids = [w["aic_id"] for w in works]
    for batch in chunks(ids, AIC_BATCH):
        url = ("https://api.artic.edu/api/v1/artworks"
               "?ids=" + ",".join(str(i) for i in batch) +
               "&fields=id,image_id,date_display"
               "&limit=%d" % AIC_BATCH)
        for rec in fetch_json(url).get("data") or []:
            out[rec["id"]] = (rec.get("image_id"), rec.get("date_display"))
        time.sleep(0.5)
    return out


# ------------------------------------------------------------ Wikipedia

def _wiki_query(params):
    params.setdefault("action", "query")
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    return fetch_json("https://en.wikipedia.org/w/api.php?" +
                      urllib.parse.urlencode(params))


def resolve_wikipedia(titles):
    """requested title -> Commons filename, batched.

    pilimit must equal the batch size; without it MediaWiki returns a page
    image for only the first article in the set."""
    out = {}
    for batch in chunks(titles, WIKI_BATCH):
        data = _wiki_query({
            "prop": "pageimages",
            "piprop": "name|original",
            "pilimit": str(len(batch)),
            "pilicense": "free",       # free-licensed only, so it is on Commons
            "redirects": "1",
            "titles": "|".join(batch),
        })
        query = data.get("query") or {}

        # Map what the API resolved back to what we asked for, so redirects
        # and title normalisation do not silently drop entries.
        alias = {}
        for key in ("normalized", "redirects"):
            for m in query.get(key) or []:
                alias[m["to"]] = alias.get(m["from"], m["from"])

        for page in query.get("pages") or []:
            if page.get("missing"):
                continue
            title = page.get("title")
            name = page.get("pageimage")
            if name:
                out[alias.get(title, title)] = name
        time.sleep(0.5)
    return out


def search_for_article(query):
    """Last resort: ask Wikipedia which article this actually is."""
    try:
        data = _wiki_query({"list": "search", "srsearch": query,
                            "srlimit": "1", "srnamespace": "0"})
        hits = (data.get("query") or {}).get("search") or []
        return hits[0]["title"] if hits else None
    except Exception:
        return None



# ------------------------------------------------- AIC -> Commons fallback

def _norm(t):
    keep = []
    for ch in t.lower():
        keep.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return " ".join("".join(keep).split())


def looks_like_same_work(artwork_title, article_title):
    """Only accept an article that is plausibly the same object.

    A loose match here would silently hang the wrong painting on the wall,
    which is worse than an empty frame, so the bar is deliberately high."""
    a, b = _norm(artwork_title), _norm(article_title)
    if len(a) < 6 or len(b) < 6:
        return False
    # strip a trailing disambiguator like "(painting)" or "(Hopper)"
    b_head = b.split(" ")[0:len(a.split(" "))]
    if a == b:
        return True
    # Only the artwork title inside the article title, never the reverse.
    # An article whose name is shorter is a more general topic -- "Harvest"
    # is not "Harvest Talk" -- and that is how you hang the wrong picture.
    if a in b:
        return True
    return " ".join(b_head) == a


def resolve_aic_fallbacks(works):
    """Give AIC works a Commons image as a second source.

    The Art Institute's image host is not always reachable for every visitor,
    and a work that also exists on Commons should not vanish because of it."""
    found, checked = {}, 0
    for w in works:
        checked += 1
        query = '"%s" %s' % (w["title"], w["artist"])
        article = search_for_article(query)
        if not article:
            time.sleep(0.3)
            continue
        if not looks_like_same_work(w["title"], article):
            time.sleep(0.3)
            continue
        got = resolve_wikipedia([article])
        if article in got:
            found[w["day"]] = (article, got[article])
        sys.stdout.write("\r  matched %d of %d checked " % (len(found), checked))
        sys.stdout.flush()
        time.sleep(0.3)
    print()
    return found

# --------------------------------------------------------------- verify

def image_loads(url):
    """Confirm a URL serves an image. Uses a ranged GET rather than HEAD:
    Commons redirects, and Python rebuilds the request as a GET across a
    redirect, so a HEAD never survives to the real host."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": "bytes=0-2047",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status not in (200, 206):
                return False, "HTTP %d" % r.status
            ctype = r.headers.get("Content-Type", "")
            if not ctype.startswith("image/"):
                return False, "content-type %s" % (ctype or "unknown")
            return True, ""
    except urllib.error.HTTPError as e:
        return False, "HTTP %d" % e.code
    except Exception as e:
        return False, type(e).__name__


def commons_url(filename, width=1200):
    return ("https://commons.wikimedia.org/wiki/Special:FilePath/"
            + urllib.parse.quote(filename) + "?width=%d" % width)


def aic_url(image_id, width=843):
    return ("https://www.artic.edu/iiif/2/%s/full/%d,/0/default.jpg"
            % (image_id, width))


# ----------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--stale", action="store_true",
                    help="only entries with nothing pinned yet")
    ap.add_argument("--aic-fallback", action="store_true",
                    help="give AIC works a Commons image as a second source")
    ap.add_argument("--verify", action="store_true",
                    help="also confirm each resolved image actually loads")
    ap.add_argument("--report", metavar="FILE",
                    help="append a Markdown summary to FILE")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any entry failed")
    args = ap.parse_args()

    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    works = manifest["artworks"]

    todo = [w for w in works
            if not (args.stale and (w.get("image_id") or w.get("commons")
                                    or w.get("local")))]
    skipped = len(works) - len(todo)

    aic_works = [w for w in todo if w["source"] == "aic"]
    wiki_works = [w for w in todo if w["source"] == "wikimedia"]

    failures, substituted, resolved = [], [], 0

    # --- AIC ---
    print("Resolving %d Art Institute works in %d request(s)..."
          % (len(aic_works), (len(aic_works) + AIC_BATCH - 1) // AIC_BATCH))
    try:
        aic_map = resolve_aic(aic_works)
    except Exception as e:
        aic_map = {}
        failures.append((0, "Art Institute API", "batch lookup failed: %s" % e))

    for w in aic_works:
        image_id, date_display = aic_map.get(w["aic_id"], (None, None))
        if not image_id:
            failures.append((w["day"], "%s — %s" % (w["title"], w["artist"]),
                             "no image in AIC record (id %d)" % w["aic_id"]))
            continue
        if not args.check:
            w["image_id"] = image_id
            if date_display:
                w["date"] = date_display
        resolved += 1

    # --- Wikipedia ---
    print("Resolving %d Wikimedia works in %d request(s)..."
          % (len(wiki_works), (len(wiki_works) + WIKI_BATCH - 1) // WIKI_BATCH))
    try:
        wiki_map = resolve_wikipedia([w["wikipedia"] for w in wiki_works])
    except Exception as e:
        wiki_map = {}
        failures.append((0, "Wikipedia API", "batch lookup failed: %s" % e))

    unresolved = [w for w in wiki_works if w["wikipedia"] not in wiki_map]
    if unresolved:
        print("Searching for %d article(s) that did not resolve..."
              % len(unresolved))
    for w in unresolved:
        found = search_for_article(w["title"] + " " + w["artist"])
        if not found or found == w["wikipedia"]:
            continue
        retry = resolve_wikipedia([found])
        if found in retry:
            # Key under both the old and corrected titles: the entry's own
            # title is rewritten below, and the final lookup uses whichever
            # one is current (--check leaves it unchanged).
            wiki_map[w["wikipedia"]] = retry[found]
            wiki_map[found] = retry[found]
            substituted.append((w["day"], w["wikipedia"], found))
            if not args.check:
                w["wikipedia"] = found
                w["link"] = ("https://en.wikipedia.org/wiki/"
                             + found.replace(" ", "_"))
        time.sleep(0.4)

    for w in wiki_works:
        name = wiki_map.get(w["wikipedia"])
        if not name:
            failures.append((w["day"], "%s — %s" % (w["title"], w["artist"]),
                             "no free lead image on '%s'" % w["wikipedia"]))
            continue
        if not args.check:
            w["commons"] = name
        resolved += 1

    # --- optional second source for AIC works ---
    if args.aic_fallback:
        pool = [w for w in aic_works if not w.get("commons")]
        print("Looking for Commons fallbacks for %d Art Institute works..." % len(pool))
        found = resolve_aic_fallbacks(pool)
        # If two different works resolved to the same image, the match was
        # ambiguous for both. Drop them rather than guess.
        seen = {}
        for day, hit in found.items():
            seen.setdefault(hit[1], []).append(day)
        for fname, days in seen.items():
            if len(days) > 1:
                for day in days:
                    found.pop(day, None)
                print("  ambiguous match dropped for days %s" % days)
        for w in pool:
            hit = found.get(w["day"])
            if not hit:
                continue
            if not args.check:
                w["commons"] = hit[1]
            substituted.append((w["day"], w["title"], "also on Commons via '%s'" % hit[0]))
        print("Commons fallbacks added: %d of %d" % (len(found), len(pool)))

    # --- optional verification ---
    if args.verify:
        print("Verifying images...")
        checked = 0
        for w in todo:
            if w.get("image_id"):
                url = aic_url(w["image_id"])
            elif w.get("commons"):
                url = commons_url(w["commons"])
            else:
                continue
            ok, why = image_loads(url)
            checked += 1
            if not ok:
                failures.append((w["day"], "%s — %s" % (w["title"], w["artist"]),
                                 "image did not load (%s)" % why))
            if checked % 25 == 0:
                sys.stdout.write("\r  verified %d " % checked)
                sys.stdout.flush()
            time.sleep(0.15)
        print()

    # --- output ---
    print()
    print("resolved: %d" % resolved)
    if args.stale:
        print("skipped (already pinned): %d" % skipped)
    if substituted:
        print("article titles corrected: %d" % len(substituted))
    print("failed:   %d" % len(failures))

    if substituted:
        print("\nCorrected article titles:")
        for day, old, new in substituted:
            print("  day %-3d  %s  ->  %s" % (day, old, new))

    if failures:
        print("\nEntries needing attention:")
        for day, label, why in failures:
            print("  day %-3d  %-50s  %s" % (day, label[:50], why))

    if args.report:
        with open(args.report, "a", encoding="utf-8") as f:
            f.write("## Art Daily image resolution\n\n")
            f.write("- Resolved: **%d**\n" % resolved)
            if args.stale:
                f.write("- Skipped (already pinned): **%d**\n" % skipped)
            f.write("- Corrected titles: **%d**\n" % len(substituted))
            f.write("- Failed: **%d**\n\n" % len(failures))
            if substituted:
                f.write("### Article titles corrected automatically\n\n")
                f.write("| Day | Was | Now |\n|---|---|---|\n")
                for day, old, new in substituted:
                    f.write("| %d | %s | %s |\n" % (day, old, new))
                f.write("\n")
            if failures:
                f.write("### Entries needing attention\n\n")
                f.write("| Day | Artwork | Problem |\n|---|---|---|\n")
                for day, label, why in failures:
                    f.write("| %d | %s | %s |\n"
                            % (day, label.replace("|", "\\|"), why))
                f.write("\nFix by editing `artworks.json`: correct the "
                        "`wikipedia` title, set `commons` to an exact Commons "
                        "filename, or point `local` at a self-hosted file.\n")
            else:
                f.write("Everything resolved cleanly.\n")

    if not args.check:
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print("\nWrote %s" % MANIFEST)
    else:
        print("\n--check: nothing written.")

    return 1 if (failures and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
