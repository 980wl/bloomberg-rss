#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bloomberg RSS generator (GitHub Actions pilot).
Fetches the official Bloomberg RSS (latest 20 headlines), translates new
English titles to Chinese via the free Google Translate endpoint
(translate.googleapis.com, client=gtx, no key), merges with the existing
feed file in this repo (dedup by link, newest first, cap 600 items,
no time limit), and writes the feed file back.
Output file: BLOOMBERG_RSS_OUTPUT env var, default bloomberg.xml.
If Google Translate fails (e.g. 429 rate limit on the runner IP), the run
still succeeds: new items keep English-only titles and a warning is logged.
Exit code is 0 on success (RSS fetched and file written), 1 only if the RSS
itself could not be fetched.
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, format_datetime
from xml.sax.saxutils import escape as xml_escape

RSS_URL = "https://www.bloomberg.com/feeds/news.rss"
_OUT = os.environ.get("BLOOMBERG_RSS_OUTPUT", "bloomberg.xml")
FEED_FILE = (_OUT if os.path.isabs(_OUT) else
             os.path.join(os.path.dirname(os.path.abspath(__file__)), _OUT))
MAX_ITEMS = 600
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}


def log(*a):
    print("[rss]", *a, flush=True)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def google_translate(titles):
    """Translate a list of English titles to Chinese. Returns a list of the
    same length, or None on any failure (caller falls back to English)."""
    if not titles:
        return []
    try:
        joined = "\n".join(titles)
        url = ("https://translate.googleapis.com/translate_a/single"
               "?client=gtx&sl=en&tl=zh-CN&dt=t&q="
               + urllib.parse.quote(joined, safe=""))
        raw = fetch(url, timeout=20)
        resp = json.loads(raw.decode("utf-8"))
        segs = resp[0]
        all_text = "".join(seg[0] for seg in segs)
        parts = all_text.split("\n")
        if len(parts) != len(titles):
            log("translate: split count mismatch (%d != %d)" % (len(parts), len(titles)))
            return None
        return [p.strip() for p in parts]
    except Exception as e:
        log("translate: FAILED (%s)" % e)
        return None


def gemini_translate(titles):
    """Translate via the local Gemini helper (Muse VM only).
    Returns a list of the same length, or None if unavailable.
    The helper lives outside the repo (custom.gemini connector); on the
    GitHub Actions runner it does not exist, so this returns None there."""
    if not titles:
        return []
    helper = os.environ.get("GEMINI_TRANSLATE_HELPER", "")
    if not helper:
        helper = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "gemini_translate.py")
    if not os.path.isfile(helper):
        return None
    try:
        proc = subprocess.run(
            [sys.executable, helper],
            input=json.dumps({"titles": titles}).encode("utf-8"),
            capture_output=True, timeout=120)
    except Exception as e:
        log("gemini: FAILED (%s)" % e)
        return None
    if proc.returncode != 0:
        err = (proc.stdout.decode("utf-8", "replace").strip()
               or proc.stderr.decode("utf-8", "replace").strip())[:300]
        log("gemini: helper failed (%s)" % err)
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        log("gemini: bad helper output")
        return None
    trans = data.get("translations")
    if not isinstance(trans, list) or len(trans) != len(titles):
        log("gemini: count mismatch (%s)"
            % (data.get("error") or "bad response"))
        return None
    return [t.strip() for t in trans]


def parse_pubdate(s):
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def read_existing():
    """Return {link: item-dict} from the current feed file, or {}."""
    items = {}
    if not os.path.isfile(FEED_FILE):
        return items
    try:
        tree = ET.parse(FEED_FILE)
        for it in tree.getroot().find("channel").findall("item"):
            link = (it.findtext("link") or "").strip()
            if not link:
                continue
            items[link] = {
                "title": (it.findtext("title") or "").strip(),
                "zh": (it.findtext("description") or "").strip(),
                "link": link,
                "pub": (it.findtext("pubDate") or "").strip(),
                "pub_dt": parse_pubdate(it.findtext("pubDate") or ""),
            }
    except Exception as e:
        log("read existing %s FAILED (%s); starting fresh" % (os.path.basename(FEED_FILE), e))
    return items


def build_xml(items):
    now = format_datetime(datetime.now(timezone.utc))
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">')
    out.append("<channel>")
    out.append("<title>Bloomberg \u6700\u65b0\u5934\u6761</title>")
    out.append("<link>https://www.bloomberg.com/latest</link>")
    out.append("<description>Bloomberg latest headlines with English titles and Chinese translations.</description>")
    out.append("<language>zh-CN</language>")
    out.append("<lastBuildDate>%s</lastBuildDate>" % now)
    for it in items:
        zh = it["zh"] or it["title"]
        out.append("<item>")
        out.append("<title>%s</title>" % xml_escape(it["title"]))
        out.append("<description>%s</description>" % xml_escape(zh))
        out.append("<link>%s</link>" % xml_escape(it["link"]))
        out.append('<guid isPermaLink="true">%s</guid>' % xml_escape(it["link"]))
        out.append("<pubDate>%s</pubDate>" % xml_escape(it["pub"]))
        out.append("<content:encoded><![CDATA[<p><b>%s</b></p><p>%s</p>]]></content:encoded>"
                   % (xml_escape(zh), xml_escape(it["title"])))
        out.append("</item>")
    out.append("</channel>")
    out.append("</rss>")
    return "\n".join(out) + "\n"


def main():
    existing = read_existing()
    log("existing items: %d" % len(existing))
    try:
        raw = fetch(RSS_URL, timeout=30)
    except Exception as e:
        log("RSS fetch FAILED (%s)" % e)
        return 1
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        log("RSS parse FAILED (%s)" % e)
        return 1
    fresh = []
    for it in root.find("channel").findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        fresh.append({"title": title, "link": link, "pub": pub,
                      "pub_dt": parse_pubdate(pub)})
    log("RSS fetched: %d items" % len(fresh))
    # Translate only titles that are new (not already in the feed).
    need = [f for f in fresh if f["link"] not in existing]
    log("new items: %d" % len(need))
    zh_map = {}
    if need:
        zh = google_translate([f["title"] for f in need])
        if zh is not None:
            for f, t in zip(need, zh):
                if t:
                    zh_map[f["link"]] = t
            log("translated: %d/%d" % (len(zh_map), len(need)))
        else:
            zh_gem = gemini_translate([f["title"] for f in need])
            if zh_gem is not None:
                for f, t in zip(need, zh_gem):
                    if t:
                        zh_map[f["link"]] = t
                log("translated via gemini: %d/%d" % (len(zh_map), len(need)))
            else:
                log("translation unavailable; new items will be English-only")
    # Merge: new items (with translated titles) + existing items.
    merged = dict(existing)
    for f in fresh:
        merged[f["link"]] = {
            "title": f["title"],
            "zh": zh_map.get(f["link"], existing.get(f["link"], {}).get("zh", "")),
            "link": f["link"],
            "pub": f["pub"],
            "pub_dt": f["pub_dt"],
        }
    items = sorted(merged.values(),
                   key=lambda x: x["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)[:MAX_ITEMS]
    with open(FEED_FILE, "w", encoding="utf-8") as fh:
        fh.write(build_xml(items))
    log("wrote %s: %d items, newest %s, oldest %s"
        % (FEED_FILE, len(items),
           items[0]["pub"] if items else "-",
           items[-1]["pub"] if items else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
