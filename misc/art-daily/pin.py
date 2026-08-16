#!/usr/bin/env python3
"""
Resolve every artwork in artworks.json to a concrete image and pin the
result back into the file.

The page works without this: it resolves images live at runtime. Pinning
just makes the common case fast (no API round trip) and tells you which
entries are broken so you can fix them deliberately instead of finding out
from a blank square eleven months from now.

Usage:
    python3 pin.py              # resolve everything, write back
    python3 pin.py --check      # report only, change nothing
    python3 pin.py --stale      # only entries with nothing pinned yet

Standard library only. Run it from inside misc/art-daily/.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "artworks.json")

UA = "jacobmonzel.com art-daily pinner (+https://www.jacobmonzel.com)"
TIMEOUT = 30
PAUSE = 0.25          # be polite to both APIs


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def head_ok(url):
    """True if the URL serves an image. Follows redirects."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    req.get_method = lambda: "HEAD"
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            ctype = r.headers.get("Content-Type", "")
            return r.status == 200 and ctype.startswith("image/")
    except Exception:
        return False


def resolve_aic(art):
    """Return (image_id, date_display) from the Art Institute API."""
    url = ("https://api.artic.edu/api/v1/artworks/%d"
           "?fields=image_id,title,artist_display,date_display" % art["aic_id"])
    data = get_json(url).get("data") or {}
    return data.get("image_id"), data.get("date_display")


def resolve_wikimedia(art):
    """Return the Commons filename backing the article's lead image."""
    params = {
        "action": "query",
        "format": "json",
        "prop": "pageimages",
        "piprop": "original|name",
        "redirects": "1",
        "titles": art["wikipedia"],
    }
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    pages = (get_json(url).get("query") or {}).get("pages") or {}
    for page in pages.values():
        if "missing" in page:
            return None
        name = page.get("pageimage")
        if name:
            return name
    return None


def commons_url(filename, width=1200):
    return ("https://commons.wikimedia.org/wiki/Special:FilePath/"
            + urllib.parse.quote(filename) + "?width=%d" % width)


def aic_url(image_id, width=843):
    return ("https://www.artic.edu/iiif/2/%s/full/%d,/0/default.jpg"
            % (image_id, width))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report only, do not write")
    ap.add_argument("--stale", action="store_true",
                    help="only entries with nothing pinned yet")
    ap.add_argument("--report", metavar="FILE",
                    help="append a Markdown summary to FILE "
                         "(used for the GitHub Actions job summary)")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any entry failed to resolve")
    args = ap.parse_args()

    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)

    works = manifest["artworks"]
    failures = []
    resolved = 0
    skipped = 0

    for art in works:
        pinned = art.get("image_id") or art.get("commons") or art.get("local")
        if args.stale and pinned:
            skipped += 1
            continue

        day = art["day"]
        label = "%s — %s" % (art["title"], art["artist"])

        try:
            if art["source"] == "aic":
                image_id, date_display = resolve_aic(art)
                if not image_id:
                    failures.append((day, label, "no image_id in AIC record"))
                    continue
                if not head_ok(aic_url(image_id)):
                    failures.append((day, label, "AIC image did not load"))
                    continue
                if not args.check:
                    art["image_id"] = image_id
                    if date_display:
                        art["date"] = date_display
                resolved += 1

            else:
                filename = resolve_wikimedia(art)
                if not filename:
                    failures.append(
                        (day, label, "no lead image on '%s'" % art["wikipedia"]))
                    continue
                if not head_ok(commons_url(filename)):
                    failures.append((day, label, "Commons image did not load"))
                    continue
                if not args.check:
                    art["commons"] = filename
                resolved += 1

        except Exception as exc:
            failures.append((day, label, "%s: %s" % (type(exc).__name__, exc)))

        sys.stdout.write("\r  resolved %d, failed %d " % (resolved, len(failures)))
        sys.stdout.flush()
        time.sleep(PAUSE)

    print("\n")
    print("resolved: %d" % resolved)
    if args.stale:
        print("skipped (already pinned): %d" % skipped)
    print("failed:   %d" % len(failures))

    if failures:
        print("\nEntries needing attention:\n")
        for day, label, why in failures:
            print("  day %-3d  %-55s  %s" % (day, label[:55], why))
        print("\nFix these by editing artworks.json: correct the 'wikipedia' "
              "title, set 'commons' to an exact Commons filename, or point "
              "'local' at a file you host yourself.")

    if not args.check:
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print("\nWrote %s" % MANIFEST)
    else:
        print("\n--check: nothing written.")

    if args.report:
        with open(args.report, "a", encoding="utf-8") as f:
            f.write("## Art Daily image resolution\n\n")
            f.write("- Resolved: **%d**\n" % resolved)
            if args.stale:
                f.write("- Skipped (already pinned): **%d**\n" % skipped)
            f.write("- Failed: **%d**\n\n" % len(failures))
            if failures:
                f.write("### Entries needing attention\n\n")
                f.write("| Day | Artwork | Problem |\n|---|---|---|\n")
                for day, label, why in failures:
                    safe = label.replace("|", "\\|")
                    f.write("| %d | %s | %s |\n" % (day, safe, why))
                f.write("\nFix by editing `artworks.json`: correct the "
                        "`wikipedia` title, set `commons` to an exact Commons "
                        "filename, or point `local` at a self-hosted file.\n")
            else:
                f.write("Everything resolved cleanly.\n")

    # Failures are reported, not fatal: one dead artwork should not stop the
    # other 365 from being pinned. Use --strict to flip that.
    return 1 if (failures and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
