#!/usr/bin/env python3
"""
Extra listing sources that answer a GitHub runner (checked by the Probe
workflow): Craigslist by-owner, Whitetail Properties, and two auction houses.
Each fetcher returns (rows, complete) like zillow.fetch_county - `complete`
False means the source could not be read fully and its listings' absence
proves nothing.

Row shape (listings):
  id, source, address, town, price, acres, lat, lon, url, photo, byOwner
Row shape (auctions):
  name, url, when, acres, source
"""

import hashlib
import html as H
import json
import re
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PAUSE = 2.5

# our 12 counties as Whitetail spells them in URLs
WT_COUNTIES = {"knox", "blount", "loudon", "anderson", "union", "grainger",
               "jefferson", "sevier", "roane", "monroe", "campbell", "morgan"}

ACRES_RE = re.compile(r"([\d]{1,4}(?:\.\d+)?)\s*(?:\+/-|±|&#177;)?\s*(?:acres?|ac\b)", re.I)
LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
OG_IMG = re.compile(r'<meta property="og:image" content="([^"]+)"')


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, timeout=45, tries=2):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "text/html,*/*",
                "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001
            if attempt == tries - 1:
                log(f"  ! {url[:70]}: {getattr(e, 'code', e)}")
                return None
            time.sleep(4)


FINANCE_RE = re.compile(r"owner[\s-]*financ|seller[\s-]*financ|\$\s?[\d,]+\s*(?:a|per|/)\s*mo(?:nth)?\b|no credit|rent[\s-]*to[\s-]*own|land contract", re.I)


def owner_financing(text):
    return bool(FINANCE_RE.search(text or ""))


def acres_in(text):
    m = ACRES_RE.search(text or "")
    return float(m.group(1)) if m else None


def money(text):
    m = re.search(r"\$\s?([\d,]{4,9})", text or "")
    return int(m.group(1).replace(",", "")) if m else None


# ------------------------------------------------------------- craigslist ---

CL_URL = ("https://knoxville.craigslist.org/search/rea?purveyor=owner&query=acres"
          "&max_price={max_price}")


def craigslist(max_price):
    """By-owner real estate posts mentioning acres, Knoxville area. The search
    page's JSON-LD carries a coordinate per result; the rows carry price/link."""
    html = get(CL_URL.format(max_price=max_price))
    if not html:
        return [], False
    coords = []
    for b in LD_RE.findall(html):
        try:
            d = json.loads(b)
        except ValueError:
            continue
        for it in d.get("itemListElement") or []:
            item = it.get("item") or {}
            coords.append((item.get("name"), item.get("latitude"), item.get("longitude"),
                           (item.get("address") or {}).get("addressLocality")))
    rows_html = re.findall(r'<li class="cl-static-search-result"[^>]*>(.*?)</li>', html, re.S)
    out = []
    for i, r in enumerate(rows_html):
        href = re.search(r'href="([^"]+)"', r)
        title = re.search(r'class="title">([^<]+)', r)
        price = money((re.search(r'class="price">([^<]+)', r) or [None, ""])[1])
        if not href or not title or not price:
            continue
        name, lat, lon, town = coords[i] if i < len(coords) else (None, None, None, None)
        acres = acres_in(H.unescape(title.group(1)))
        if lat is None or lon is None or not acres:
            continue
        url = href.group(1)
        out.append({
            "id": "cl" + hashlib.md5(url.encode()).hexdigest()[:10],
            "source": "Craigslist", "byOwner": True,
            "address": H.unescape(title.group(1))[:80], "town": town or "",
            "price": price, "acres": round(acres, 2),
            "lat": round(float(lat), 6), "lon": round(float(lon), 6),
            "url": url, "photo": None,
            "ownerFinance": owner_financing(H.unescape(title.group(1))),
        })
    log(f"  craigslist  {len(out):>4} by-owner posts with acres and a price")
    return out, True


# -------------------------------------------------------------- whitetail ---

WT_LIST = "https://www.whitetailproperties.com/hunting-land/tennessee?page={page}"
WT_LINK = re.compile(r'href="(/hunting-land/tennessee/([a-z-]+)/[a-z0-9-]{12,})"')


def whitetail(max_price, known_urls=(), max_pages=12):
    """Whitetail's Tennessee listings, filtered to our counties by the URL,
    with price / acres / coordinates read off each listing page. Pages we
    already know are not re-fetched - pass their URLs in known_urls."""
    links, complete = [], True
    seen = set()
    for page in range(1, max_pages + 1):
        html = get(WT_LIST.format(page=page))
        if not html:
            complete = False
            break
        fresh = 0
        for path, county in WT_LINK.findall(html):
            if path in seen:
                continue
            seen.add(path)
            fresh += 1
            if county in WT_COUNTIES:
                links.append((path, county))
        if fresh == 0:
            break
        time.sleep(PAUSE)
    out = []
    for path, county in links:
        url = "https://www.whitetailproperties.com" + path
        if url in known_urls:
            out.append({"url": url, "known": True})
            continue
        html = get(url)
        if not html:
            complete = False
            continue
        lat = re.search(r'"latitude"\s*:\s*(-?\d+\.\d+)', html)
        lon = re.search(r'"longitude"\s*:\s*(-?\d+\.\d+)', html)
        title = re.search(r"<title>([^<]+)</title>", html)
        price = money(html)
        acres = acres_in(H.unescape(title.group(1)) if title else "") or acres_in(html)
        img = OG_IMG.search(html)
        if not (lat and lon and price and acres):
            continue
        if price > max_price:
            continue
        out.append({
            "id": "wt" + hashlib.md5(path.encode()).hexdigest()[:10],
            "source": "Whitetail", "byOwner": False,
            "address": H.unescape(title.group(1)).split("|")[0].strip()[:80] if title else path,
            "town": "", "price": price, "acres": round(acres, 2),
            "lat": round(float(lat.group(1)), 6), "lon": round(float(lon.group(1)), 6),
            "url": url, "photo": img.group(1) if img else None,
        })
        time.sleep(PAUSE)
    log(f"  whitetail   {len(out):>4} listings in our counties"
        f"{'' if complete else '  (INCOMPLETE)'}")
    return out, complete


# --------------------------------------------------------------- auctions ---

def auctions():
    """Upcoming land auctions with an acreage in the title."""
    out = []
    html = get("https://www.powellauction.com/auctions/")
    if html:
        for b in LD_RE.findall(html):
            try:
                d = json.loads(b)
            except ValueError:
                continue
            if d.get("@type") != "Event":
                continue
            name = H.unescape(str(d.get("name") or ""))
            ac = acres_in(name)
            if ac:
                out.append({"name": name, "url": d.get("url"), "when": (d.get("startDate") or "")[:10],
                            "acres": ac, "source": "Powell Auction"})
    time.sleep(PAUSE)
    html = get("https://www.ayersauctionrealty.com/search/auctions/all-sales/ending-soon/"
               "all-categories/all-locations")
    if html:
        for path, rest in re.findall(r'href="(/auction/[^"]+/details)"[^>]*>(.*?)</a>', html, re.S):
            title = H.unescape(re.sub(r"<[^>]+>", " ", rest)).strip()
            if title.lower() == "view auction":
                continue
            ac = acres_in(title)
            if ac:
                # the end date sits near the link in the card
                near = html[max(0, html.find(path) - 800): html.find(path) + 800]
                when = re.search(r"(?:ENDS|Ends)\s*(\d{2}/\d{2}/\d{2})", near)
                out.append({"name": title[:120], "url": "https://www.ayersauctionrealty.com" + path,
                            "when": when.group(1) if when else "", "acres": ac,
                            "source": "Ayers Auction"})
    # one entry per url
    uniq = {a["url"]: a for a in out if a.get("url")}
    out = sorted(uniq.values(), key=lambda a: a["when"] or "9999")
    log(f"  auctions    {len(out):>4} upcoming with an acreage")
    return out


if __name__ == "__main__":
    rows, ok = craigslist(250_000)
    for r in rows[:5]:
        print(f"  CL  {r['acres']:>7} ac  ${r['price']:>8,}  {r['town']:<14} {r['address'][:50]}")
    rows, ok = whitetail(250_000)
    for r in rows[:5]:
        print(f"  WT  {r['acres']:>7} ac  ${r['price']:>8,}  {r['address'][:50]}")
    for a in auctions()[:6]:
        print(f"  AU  {a['when']:<10} {a['acres']:>6} ac  {a['name'][:60]}  [{a['source']}]")
