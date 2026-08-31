#!/usr/bin/env python3
"""
Build an S3-ready copy of neopets.lgbt (main site) from the Apache source of truth.

NON-DESTRUCTIVE: reads from SRC, writes only to DST. Never modifies SRC.

Reproduces Apache behavior via S3-native file layout (Layout B: clean URLs as
folder/index.html) so S3 needs no server tricks:
  - Apache served /jico  (from neopets/jico.html) as a clean URL
    -> S3 copy places it at  jico/index.html   (served at /jico/)
  - Apache served /tenko  (dir neopets/tenko/)  -> tenko/index.html
  - /about, /cosplay stay as about/index.html, cosplay/index.html
  - hidden/deep pages keep their existing folder/index.html paths
  - explicit Apache "Redirect 301" rules are emitted as S3 routing rules (separate file)

Link rewriting inside HTML:
  - clean-URL nav links (root-absolute like /jico and page-relative like "qm")
    are rewritten to the trailing-slash clean URL form (/jico/) that S3 serves.
  - asset links (/images, /audio, /fonts, /jquery-1.7.js) are normalized to
    root-absolute so they resolve from any page depth.

Excluded from the copy: .git, .DS_Store, *.psd, *.swp, *.sample, *.rev/.pack/.idx
(git internals), and other non-web junk. jquery-1.7.js is KEPT (audio player needs it).
"""

import os
import re
import shutil
import sys

# Repo-relative by default so this runs identically locally and in CI.
# SRC = the repo root (this script lives at the repo root).
# DST = a build output dir (gitignored). Both overridable via env vars.
_HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("SRC_DIR", _HERE)
DST = os.environ.get("DST_DIR", os.path.join(_HERE, "build"))

# ---------------------------------------------------------------------------
# 1. FILE PLACEMENT MAP: source file (relative to SRC) -> dest path (relative to DST)
#    Only HTML pages that are "clean URLs" get relocated. Everything else
#    (assets) is copied to the same relative path.
# ---------------------------------------------------------------------------

# Character/page .html files that live in neopets/ and were served as /<name>
# (Apache rule 2). Each becomes <name>/index.html so S3 serves it at /<name>/.
NEOPETS_PAGES = [
    "arvain", "caci", "caruso", "heige", "jico", "lasak", "machu", "maku",
    "nekotenko", "qiru", "qm", "ranka", "roku", "roninko", "sinchi", "sosa",
    "sphoryx", "taji", "turu", "veirs", "vim", "yura",
]

# Explicit relocation map for HTML pages: SRC-relative -> DST-relative
HTML_MAP = {
    "index.html": "index.html",                 # site root
    "404.html": "404.html",                      # S3 error document
    "about.html": "about/index.html",            # /about
    "cosplay.html": "cosplay/index.html",        # /cosplay
    "neopets/index.html": "neopets/index.html",  # /neopets
    "neopets/tenko/index.html": "tenko/index.html",          # /tenko (dir)
    "neopets/tenko/tenkoref.html": "tenkoref/index.html",    # /tenkoref (redirect target, also direct)
    "neopets/ilaloref.html": "ilaloref/index.html",          # /ilaloref
    # NOTE: previously-hidden pages (images/mystery/index.html,
    # images/mystery/temporary/index.html, images/characters/tenko/xxx/index.html)
    # were intentionally DELETED from the source by the owner and are omitted.
    # Any unmapped .html is copied at its same relative path by the safety-net
    # branch below, so no explicit entries are needed here for deep pages.
}
for name in NEOPETS_PAGES:
    HTML_MAP[f"neopets/{name}.html"] = f"{name}/index.html"

# ---------------------------------------------------------------------------
# 2. CLEAN-URL LINK MAP: how an internal target token maps to its S3 clean URL.
#    Keys are the "bare" names used in links (with or without leading slash).
# ---------------------------------------------------------------------------
CLEAN_URL = {}
for name in NEOPETS_PAGES:
    CLEAN_URL[name] = f"/{name}/"
CLEAN_URL["about"] = "/about/"
CLEAN_URL["cosplay"] = "/cosplay/"
CLEAN_URL["neopets"] = "/neopets/"
CLEAN_URL["tenko"] = "/tenko/"
CLEAN_URL["tenkoref"] = "/tenkoref/"
CLEAN_URL["ilaloref"] = "/ilaloref/"
# Apache explicit redirects -> final destinations (so internal links skip the hop)
CLEAN_URL["characters"] = "/neopets/"          # Redirect 301 /characters -> /neopets/index.html
CLEAN_URL["fursuits"] = "/cosplay/"            # Redirect 301 /fursuits -> /cosplay.html
CLEAN_URL["ilalo"] = "/ilaloref/"              # index links /ilalo; only ilaloref exists -> point at it
CLEAN_URL["pike"] = "https://www.youtube.com/watch?v=WKFJ0DcMY6A"

# Asset prefixes that must stay root-absolute and untouched
ASSET_PREFIXES = ("images/", "audio/", "fonts/", "jquery-1.7.js")

EXCLUDE_DIRS = {".git", ".github", "build", ".vscode", "node_modules"}
EXCLUDE_EXT = {".ds_store", ".psd", ".swp", ".sample", ".rev", ".pack", ".idx", ".graph"}
EXCLUDE_NAMES = {
    ".gitignore", ".DS_Store", "build_s3_site.py",
    "README.md", "MIGRATION-NOTES-future-upgrade.md",
}


def is_excluded(path):
    base = os.path.basename(path)
    if base in EXCLUDE_NAMES:
        return True
    ext = os.path.splitext(base)[1].lower()
    if ext in EXCLUDE_EXT:
        return True
    return False


def rewrite_html(text):
    """Rewrite internal links in an HTML page to S3 clean-URL (Layout B) form."""

    def repl(m):
        attr, quote, url = m.group("attr"), m.group("q"), m.group("url")
        new = rewrite_url(url)
        return f'{attr}={quote}{new}{quote}'

    # match href="..." and src="..."
    pattern = re.compile(
        r'(?P<attr>href|src)=(?P<q>["\'])(?P<url>[^"\']*)(?P=q)',
        re.IGNORECASE,
    )
    return pattern.sub(repl, text)


def rewrite_url(url):
    # Leave absolute external URLs, anchors, mailto, and asset paths alone.
    low = url.lower()
    if low.startswith(("http://", "https://", "mailto:", "#", "//", "data:")):
        return url
    # Root-absolute asset paths stay as-is.
    stripped = url.lstrip("/")
    if stripped.startswith(ASSET_PREFIXES):
        return url
    if url == "/":
        return "/"

    # Split off any anchor/query
    frag = ""
    for sep in ("#", "?"):
        if sep in url:
            i = url.index(sep)
            frag = url[i:] + frag if sep == "#" else url[i:]
    core = re.split(r"[#?]", url)[0]

    token = core.strip("/")
    # strip a trailing .html if present (Apache cleanup rule)
    if token.lower().endswith(".html"):
        token = token[:-5]

    if token in CLEAN_URL:
        return CLEAN_URL[token] + frag
    # Unknown internal link: leave as-is (surface in verification)
    return url


def main():
    if not os.path.isdir(SRC):
        sys.exit(f"SRC not found: {SRC}")
    if os.path.exists(DST):
        sys.exit(f"DST already exists, refusing to overwrite: {DST}\n"
                 f"Remove it first if you want a clean rebuild.")
    os.makedirs(DST)

    copied, rewritten, skipped = 0, 0, 0
    unknown_links = set()

    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            src_path = os.path.join(root, fn)
            rel = os.path.relpath(src_path, SRC)
            if is_excluded(src_path):
                skipped += 1
                continue

            # Determine destination
            if rel in HTML_MAP:
                dst_rel = HTML_MAP[rel]
            elif rel.lower().endswith(".html"):
                # Any HTML not explicitly mapped: keep same path (safety net)
                dst_rel = rel
            else:
                dst_rel = rel  # assets keep their path

            dst_path = os.path.join(DST, dst_rel)
            os.makedirs(os.path.dirname(dst_path) or DST, exist_ok=True)

            if dst_rel.lower().endswith(".html"):
                with open(src_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                new_text = rewrite_html(text)
                # collect unknown internal links for reporting
                for m in re.finditer(r'(?:href|src)=["\']([^"\']+)["\']', text, re.I):
                    u = m.group(1)
                    low = u.lower()
                    if low.startswith(("http", "mailto:", "#", "//", "data:")):
                        continue
                    s = u.lstrip("/")
                    if s.startswith(ASSET_PREFIXES) or u == "/":
                        continue
                    tok = re.split(r"[#?]", u)[0].strip("/")
                    if tok.lower().endswith(".html"):
                        tok = tok[:-5]
                    if tok and tok not in CLEAN_URL:
                        unknown_links.add(u)
                with open(dst_path, "w", encoding="utf-8") as f:
                    f.write(new_text)
                rewritten += 1
            else:
                shutil.copy2(src_path, dst_path)
                copied += 1

    print(f"HTML pages rewritten+placed: {rewritten}")
    print(f"Asset files copied:          {copied}")
    print(f"Junk files skipped:          {skipped}")
    print(f"DST: {DST}")
    if unknown_links:
        print("\nUNKNOWN internal links left as-is (review these):")
        for u in sorted(unknown_links):
            print(f"  {u}")

    if os.environ.get("OPTIMIZE_IMAGES", "1") != "0":
        optimize_images(DST)


def optimize_images(dst):
    """Lossless image optimization on the build output only. Degrades gracefully
    if a tool isn't installed (skips that type). Never touches SRC."""
    import subprocess

    def have(tool):
        return shutil.which(tool) is not None

    img_root = os.path.join(dst, "images")
    if not os.path.isdir(img_root):
        return
    print("\n=== optimizing images (lossless) ===")

    if have("oxipng"):
        pngs = []
        for r, _, fs in os.walk(img_root):
            pngs += [os.path.join(r, f) for f in fs if f.lower().endswith(".png")]
        if pngs:
            subprocess.run(["oxipng", "-o", "max", "--strip", "safe", "--quiet", *pngs],
                           check=False)
            print(f"  oxipng: optimized {len(pngs)} PNG(s)")
    else:
        print("  (oxipng not found; skipping PNG optimization)")

    if have("jpegtran"):
        n = 0
        for r, _, fs in os.walk(img_root):
            for f in fs:
                if f.lower().endswith((".jpg", ".jpeg")):
                    p = os.path.join(r, f)
                    tmp = p + ".tmp"
                    rc = subprocess.run(["jpegtran", "-copy", "none", "-optimize",
                                         "-progressive", "-outfile", tmp, p], check=False)
                    if rc.returncode == 0 and os.path.exists(tmp):
                        os.replace(tmp, p); n += 1
        print(f"  jpegtran: optimized {n} JPEG(s)")
    else:
        print("  (jpegtran not found; skipping JPEG optimization)")

    if have("gifsicle"):
        n = 0
        for r, _, fs in os.walk(img_root):
            for f in fs:
                if f.lower().endswith(".gif"):
                    p = os.path.join(r, f)
                    tmp = p + ".tmp"
                    rc = subprocess.run(["gifsicle", "-O3", "--careful", "-o", tmp, p],
                                        check=False)
                    if rc.returncode == 0 and os.path.exists(tmp):
                        os.replace(tmp, p); n += 1
                    elif os.path.exists(tmp):
                        os.remove(tmp)
        print(f"  gifsicle: optimized {n} GIF(s)")
    else:
        print("  (gifsicle not found; skipping GIF optimization)")


if __name__ == "__main__":
    main()
