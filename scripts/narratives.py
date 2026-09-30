#!/usr/bin/env python3
"""
Russian government narrative tracker.

  collect [--backfill-days N]   every 6 hours: add new official items to the archive
  weekly                        once a week: compute theme trends, pick review candidates
  apply-review ISSUE_BODY_FILE  after you tick boxes in the review issue: record decisions

Principles
  * Primary sources only, in their own words. Russian state media (TASS) is kept as
    evidence of what Moscow wants said, never as evidence of what happened.
  * Counting is transparent keyword matching (data/narratives/themes.json), so every
    number can be checked by hand. No AI classification.
  * Nothing is displayed as an exemplar quote until a human approves it.
  * Trends are shares of output, not raw counts, and are smoothed over four weeks;
    a theme is only flagged as rising when the change is both large and well-supported.
"""
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ND = ROOT / "data" / "narratives"
ARCH = ND / "archive"
UA = {"User-Agent": "EasternFlankEstimate/1.0 (non-commercial research; github.com/Gh0stmahn-ai/eastern-flank-estimate)"}

SOURCES = {
    "kremlin_ru": {"label": "Kremlin (Russian)", "audience": "domestic", "tier": "official", "grade": "B",
                   "lists": ["http://kremlin.ru/events/president/news", "http://kremlin.ru/events/president/transcripts"],
                   "base": "http://kremlin.ru", "licence": "CC BY 4.0"},
    "kremlin_en": {"label": "Kremlin (English)", "audience": "foreign", "tier": "official", "grade": "B",
                   "lists": ["http://en.kremlin.ru/events/president/news", "http://en.kremlin.ru/events/president/transcripts"],
                   "base": "http://en.kremlin.ru", "licence": "CC BY 4.0"},
    "mfa_ru":     {"label": "Foreign Ministry (Russian)", "audience": "domestic", "tier": "official", "grade": "B",
                   "lists": ["https://mid.ru/ru/foreign_policy/news/"], "base": "https://mid.ru"},
    "mfa_tg":     {"label": "Foreign Ministry Telegram", "audience": "domestic", "tier": "official", "grade": "B",
                   "channel": "MID_Russia"},
    "tass_en":    {"label": "TASS (English, state media)", "audience": "foreign", "tier": "state media", "grade": "D",
                   "rss": "https://tass.com/rss/v2.xml"},
}

LEAD_CHARS = 800
MAX_FETCH_PER_RUN = 60


def now():
    return dt.datetime.now(dt.timezone.utc)


def get(url, tries=3, timeout=40):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404, 410):
                break                      # permanent: do not retry
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def norm(s):
    return (s or "").lower().replace("ё", "е")


# ---------------------------------------------------------------- matching
def compile_themes():
    cfg = json.loads((ND / "themes.json").read_text())

    def rx(term):
        if "\\b" in term:
            return re.compile(term, re.I)
        return re.compile(r"(?<![\w])" + re.escape(term).replace(r"\ ", r"\s+"), re.I)

    out = []
    for t in cfg["themes"]:
        out.append({
            "id": t["id"], "label": t["label"],
            "en": ([rx(x) for x in t["anchors_en"]], [rx(x) for x in t["frames_en"]]),
            "ru": ([rx(x) for x in t["anchors_ru"]], [rx(x) for x in t["frames_ru"]]),
        })
    return cfg, out


SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def match_themes(text, lang, themes):
    """Return {theme_id: [matched sentence, ...]} using anchor+frame co-occurrence per sentence."""
    hits = {}
    for sent in SENT_SPLIT.split(norm(text)):
        if len(sent) < 12:
            continue
        for t in themes:
            anchors, frames = t[lang]
            if any(a.search(sent) for a in anchors) and any(f.search(sent) for f in frames):
                hits.setdefault(t["id"], []).append(sent.strip()[:300])
    return hits


def exemplar_sentences(text, lang, themes, limit=2):
    """Original-case sentences for review candidates (from the full body)."""
    out = {}
    raw_sents = [s.strip() for s in SENT_SPLIT.split(text or "") if len(s.strip()) >= 30]
    for s in raw_sents:
        n = norm(s)
        for t in themes:
            anchors, frames = t[lang]
            if any(a.search(n) for a in anchors) and any(f.search(n) for f in frames):
                lst = out.setdefault(t["id"], [])
                if len(lst) < limit:
                    lst.append(s[:320])
    return out


# ---------------------------------------------------------------- archive
def month_file(d):
    return ARCH / f"{d[:7]}.jsonl"


def load_seen():
    seen = set()
    if ARCH.exists():
        for f in ARCH.glob("*.jsonl"):
            for line in f.read_text().splitlines():
                try:
                    seen.add(json.loads(line)["id"])
                except Exception:  # noqa: BLE001
                    pass
    return seen


def append(rec):
    ARCH.mkdir(parents=True, exist_ok=True)
    with month_file(rec["date"]).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- sources
def kremlin_items(src_key, pages):
    s = SOURCES[src_key]
    items = []
    for lst in s["lists"]:
        for p in range(1, pages + 1):
            url = lst if p == 1 else f"{lst}/page/{p}"
            page = get(url)
            for path in dict.fromkeys(re.findall(r'href="(/events/president/(?:news|transcripts)/\d+)"', page)):
                items.append(s["base"] + path)
            time.sleep(0.8)
    return list(dict.fromkeys(items))


def kremlin_fetch(url):
    time.sleep(1.2)                   # the Kremlin site throttles bursts
    page = get(url)
    title = clean((re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S) or re.search(r"<title>(.*?)</title>", page, re.S)).group(1))
    m = re.search(r'datetime="([^"]+)"', page)
    date = m.group(1)[:10] if m else now().date().isoformat()
    # marker-based: take everything from the article body to the end-of-article block.
    # (The Russian and English sites nest their markup differently; a "next div" pattern fails on English.)
    i = page.find('class="entry-content')
    body = ""
    if i >= 0:
        j = min([k for k in (page.find("read__cut", i), page.find("read__bottommeta", i), page.find("read__tags", i)) if k > 0] or [len(page)])
        body = clean(page[page.find(">", i) + 1:j])
    return title, date, body


def mfa_items(pages):
    items = []
    for p in range(1, pages + 1):
        url = SOURCES["mfa_ru"]["lists"][0] + ("" if p == 1 else f"?PAGEN_1={p}")
        page = get(url)
        for path in dict.fromkeys(re.findall(r'href="(/ru/foreign_policy/news/\d+/)"', page)):
            items.append("https://mid.ru" + path)
        time.sleep(0.8)
    return list(dict.fromkeys(items))


def mfa_fetch(url):
    page = get(url)
    title = clean((re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S) or re.search(r"<title>(.*?)</title>", page, re.S)).group(1))
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", page)
    date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else now().date().isoformat()
    body_m = re.search(r'<div class="text article-content[^"]*"[^>]*>(.*?)</div>\s*</div>', page, re.S) or \
             re.search(r'<div class="announce__text[^"]*"[^>]*>(.*?)</div>', page, re.S)
    body = clean(body_m.group(1)) if body_m else ""
    return title, date, body


def telegram_items(channel, stop_before_date, seen, max_pages):
    out, before = [], None
    for _ in range(max_pages):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        page = get(url)
        blocks = re.findall(r'data-post="' + channel + r'/(\d+)".*?(?=data-post="|$)', page, re.S)
        ids = [int(x) for x in re.findall(r'data-post="' + channel + r'/(\d+)"', page)]
        if not ids:
            break
        for pid in ids:
            seg = page.split(f'data-post="{channel}/{pid}"', 1)[1]
            seg = seg.split('data-post="', 1)[0]
            dm = re.search(r'<time datetime="([^"]+)"', seg)
            tm = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', seg, re.S)
            if not dm:
                continue
            out.append({"pid": pid, "date": dm.group(1)[:10], "text": clean(tm.group(1)) if tm else ""})
        oldest = min(ids)
        oldest_date = min((o["date"] for o in out if o["pid"] == oldest), default="9999")
        if oldest_date < stop_before_date or f"mfa_tg:{oldest}" in seen:
            break
        before = oldest
        time.sleep(1.0)
    return out


def tass_items():
    xml = get(SOURCES["tass_en"]["rss"])
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def f(tag):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", it, re.S)
            return clean(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))) if m else ""
        link = f("link")
        try:
            date = dt.datetime.strptime(f("pubDate"), "%a, %d %b %Y %H:%M:%S %z").astimezone(dt.timezone.utc).date().isoformat()
        except Exception:  # noqa: BLE001
            date = now().date().isoformat()
        out.append({"link": link, "title": f("title"), "desc": f("description"), "date": date})
    return out


# ---------------------------------------------------------------- collect
from concurrent.futures import ThreadPoolExecutor


def fetch_many(fn, urls, workers=4):
    """Fetch pages with a small, polite worker pool; failures are returned, not raised."""
    def one(u):
        try:
            return u, fn(u), None
        except Exception as e:  # noqa: BLE001
            return u, None, str(e)[:120]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, urls))
def collect(backfill_days=0):
    cfg, themes = compile_themes()
    seen = load_seen()
    cutoff = (now() - dt.timedelta(days=backfill_days or 2)).date().isoformat()
    pages = max(1, backfill_days // 6 + 1) if backfill_days else 1
    added, errors, fetched = 0, [], 0

    def record(src, rid, date, title, lead_text, body_text, link, lang):
        nonlocal added
        hits = match_themes(title + ". " + (body_text or lead_text), lang, themes)
        rec = {"id": rid, "source": src, "audience": SOURCES[src]["audience"], "tier": SOURCES[src]["tier"],
               "date": date, "lang": lang, "title": title[:300], "link": link,
               "themes": sorted(hits.keys()), "dict_v": cfg["version"]}
        if SOURCES[src]["tier"] == "official" and body_text:
            ex = exemplar_sentences(body_text, lang, themes)
            if ex:
                rec["candidates"] = ex
        append(rec)
        added += 1

    # Kremlin, both languages
    for src, lang in (("kremlin_ru", "ru"), ("kremlin_en", "en")):
        try:
            todo = []
            for url in kremlin_items(src, pages):
                rid = f"{src}:{url.rsplit('/', 1)[-1]}:{'t' if '/transcripts/' in url else 'n'}"
                if rid not in seen:
                    todo.append((rid, url))
            todo = todo[: (1500 if backfill_days else MAX_FETCH_PER_RUN)]
            for (rid, url), (_, res, err) in zip(todo, fetch_many(kremlin_fetch, [u for _, u in todo], workers=1)):
                fetched += 1
                if err:
                    errors.append(f"{src} item: {err}")
                    continue
                title, date, body = res
                if date < cutoff:
                    continue
                record(src, rid, date, title, body[:LEAD_CHARS], body, url, lang)
                seen.add(rid)
            print(f"{src}: {len(todo)} fetched", flush=True)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src}: {str(e)[:160]}")

    # Foreign Ministry site
    try:
        todo = []
        for url in mfa_items(pages * 2):
            rid = "mfa_ru:" + url.rstrip("/").rsplit("/", 1)[-1]
            if rid not in seen:
                todo.append((rid, url))
        todo = todo[: (1500 if backfill_days else MAX_FETCH_PER_RUN)]
        for (rid, url), (_, res, err) in zip(todo, fetch_many(mfa_fetch, [u for _, u in todo])):
            if err:
                errors.append(f"mfa_ru item: {err}")
                continue
            title, date, body = res
            if date < cutoff:
                continue
            record("mfa_ru", rid, date, title, body[:LEAD_CHARS], body, url, "ru")
            seen.add(rid)
        print(f"mfa_ru: {len(todo)} fetched", flush=True)
    except Exception as e:  # noqa: BLE001
        errors.append(f"mfa_ru: {str(e)[:160]}")

    # Foreign Ministry Telegram
    try:
        for m in telegram_items("MID_Russia", cutoff, seen, max_pages=60 if backfill_days else 8):
            rid = f"mfa_tg:{m['pid']}"
            if rid in seen or m["date"] < cutoff or not m["text"]:
                continue
            title = m["text"][:140]
            record("mfa_tg", rid, m["date"], title, m["text"][:LEAD_CHARS], m["text"],
                   f"https://t.me/MID_Russia/{m['pid']}", "ru")
            seen.add(rid)
    except Exception as e:  # noqa: BLE001
        errors.append(f"mfa_tg: {str(e)[:160]}")

    # TASS (no archive; the feed covers roughly a day, hence the six-hourly schedule)
    try:
        for it in tass_items():
            rid = "tass_en:" + hashlib.sha1(it["link"].encode()).hexdigest()[:12]
            if rid in seen:
                continue
            # headline only is stored; the description is used for matching, not republished
            hits = match_themes(it["title"] + ". " + it["desc"], "en", themes)
            append({"id": rid, "source": "tass_en", "audience": "foreign", "tier": "state media",
                    "date": it["date"], "lang": "en", "title": it["title"][:300], "link": it["link"],
                    "themes": sorted(hits.keys())})
            seen.add(rid)
            added += 1
    except Exception as e:  # noqa: BLE001
        errors.append(f"tass_en: {str(e)[:160]}")

    status = {"last_collect_utc": now().isoformat(timespec="seconds"), "added": added, "errors": errors[:12], "error_count": len(errors)}
    (ND / "collect_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1))
    print(json.dumps(status, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- recompute
def recompute(force=False):
    """Re-fetch and recount every archived official item under the current dictionary.
    Runs automatically when themes.json changes version, so edits never show up as trends."""
    cfg, themes = compile_themes()
    ver = cfg["version"]
    files = sorted(ARCH.glob("*.jsonl"))
    recs = [json.loads(l) for f in files for l in f.read_text().splitlines() if l.strip()]
    stale = [r for r in recs if force or r.get("dict_v") != ver]
    if not stale:
        print("recompute: archive already on dictionary v%s" % ver); return
    fetchers = {"kremlin_ru": kremlin_fetch, "kremlin_en": kremlin_fetch, "mfa_ru": mfa_fetch}
    done = failed = 0
    for r in stale:
        if r["source"] in fetchers:
            try:
                title, _, body = fetchers[r["source"]](r["link"])
                text = title + ". " + body
                r["candidates"] = exemplar_sentences(body, r["lang"], themes) or {}
                if not r["candidates"]:
                    r.pop("candidates", None)
            except Exception:  # noqa: BLE001
                failed += 1
                continue                   # keep old counts, left on old version to retry next week
        elif r["source"] == "mfa_tg":
            m = re.search(r"/(\d+)$", r["link"])
            text = r["title"]              # message text is re-read below where available
            try:
                page = get(f"https://t.me/MID_Russia/{m.group(1)}?embed=1") if m else ""
                tm = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', page, re.S)
                if tm:
                    text = clean(tm.group(1))
                    r["candidates"] = exemplar_sentences(text, "ru", themes) or {}
                    if not r["candidates"]:
                        r.pop("candidates", None)
            except Exception:  # noqa: BLE001
                failed += 1
                continue
        else:                              # TASS: only headline is stored; recount on it
            text = r["title"]
        r["themes"] = sorted(match_themes(text, r["lang"], themes).keys())
        r["dict_v"] = ver
        done += 1
    by = {}
    for r in recs:
        by.setdefault(r["date"][:7], []).append(r)
    for f in files:
        f.unlink()
    for mth, lst in by.items():
        (ARCH / f"{mth}.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lst))
    print(json.dumps({"recomputed": done, "failed_kept_old": failed, "dictionary": ver}))


# ---------------------------------------------------------------- weekly
def iso_week(d):
    y, w, _ = dt.date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def weekly():
    cfg, _ = compile_themes()
    labels = {t["id"]: t["label"] for t in cfg["themes"]}
    defs = {t["id"]: t["definition"] for t in cfg["themes"]}
    recs = []
    for f in sorted(ARCH.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    ver = cfg["version"]
    stale_n = sum(1 for r in recs if r.get("dict_v") != ver)
    recs = [r for r in recs if r.get("dict_v") == ver]      # never mix counting methods
    today = now().date()
    this_week = iso_week(today.isoformat())
    weeks = sorted({iso_week(r["date"]) for r in recs if r["date"] <= today.isoformat()})
    weeks = [w for w in weeks if w <= this_week][-12:]

    def share(filter_fn, theme, wk_list):
        items = [r for r in recs if iso_week(r["date"]) in wk_list and filter_fn(r)]
        n = len(items)
        k = sum(1 for r in items if theme in r["themes"])
        return k, n

    groups = {
        "all_official": lambda r: r["tier"] == "official",
        "kremlin_ru": lambda r: r["source"] == "kremlin_ru",
        "kremlin_en": lambda r: r["source"] == "kremlin_en",
        "mfa": lambda r: r["source"] in ("mfa_ru", "mfa_tg"),
        "tass_en": lambda r: r["source"] == "tass_en",
    }
    complete = weeks[:-1] if weeks and weeks[-1] == this_week else weeks   # exclude the partial week
    ru_by = {r["id"].split(":", 1)[1]: r for r in recs if r["source"] == "kremlin_ru"}
    en_by = {r["id"].split(":", 1)[1]: r for r in recs if r["source"] == "kremlin_en"}
    pairs = [(ru_by[k], en_by[k]) for k in ru_by.keys() & en_by.keys()]
    last4, prev4 = complete[-4:], complete[-8:-4]

    themes_out = []
    for tid in labels:
        series = []
        for w in complete:
            k, n = share(groups["all_official"], tid, [w])
            series.append({"week": w, "hits": k, "items": n, "share": round(100 * k / n, 2) if n else None})
        k4, n4 = share(groups["all_official"], tid, last4)
        kp, np_ = share(groups["all_official"], tid, prev4)
        s4 = 100 * k4 / n4 if n4 else None
        sp = 100 * kp / np_ if np_ else None
        trend = "insufficient data"
        if s4 is not None and sp is not None and len(prev4) == 4:
            diff = s4 - sp
            if k4 >= 5 and diff >= max(1.5, 0.5 * sp):
                trend = "rising"
            elif kp >= 5 and -diff >= max(1.5, 0.5 * sp):
                trend = "falling"
            else:
                trend = "steady"
        # home vs abroad: the SAME Kremlin items in both languages (matched by shared ID),
        # so gaps in either stream cannot masquerade as a difference in emphasis
        pr = [p for p in pairs if iso_week(p[0]["date"]) in last4]
        nr = ne = len(pr)
        kr = sum(1 for a, b in pr if tid in a["themes"])
        ke = sum(1 for a, b in pr if tid in b["themes"])
        div = None
        if nr >= 20 and ne >= 20 and (kr + ke) >= 3:
            sr, se = kr / nr, ke / ne
            if se == 0 and sr > 0:
                div = {"ratio": None, "note": "Russian only"}
            elif sr == 0 and se > 0:
                div = {"ratio": None, "note": "English only"}
            elif se > 0:
                r_ = sr / se
                if r_ >= 2 or r_ <= 0.5:
                    div = {"ratio": round(r_, 2), "note": "more at home" if r_ >= 2 else "more abroad"}
        km, nm = share(groups["mfa"], tid, last4)
        kt, nt = share(groups["tass_en"], tid, last4)
        themes_out.append({
            "id": tid, "label": labels[tid], "definition": defs[tid],
            "share_4w": round(s4, 2) if s4 is not None else None,
            "share_prev_4w": round(sp, 2) if sp is not None else None,
            "hits_4w": k4, "trend": trend, "series": series,
            "kremlin_ru": {"hits": kr, "items": nr}, "kremlin_en": {"hits": ke, "items": ne},
            "mfa": {"hits": km, "items": nm}, "tass_en": {"hits": kt, "items": nt},
            "divergence": div,
        })

    # review candidates: new official exemplar sentences not yet decided
    approved = json.loads((ND / "approved.json").read_text()) if (ND / "approved.json").exists() else []
    rejected = json.loads((ND / "rejected.json").read_text()) if (ND / "rejected.json").exists() else []
    decided = {a["cid"] for a in approved} | {r["cid"] for r in rejected}
    week_start = (today - dt.timedelta(days=7)).isoformat()
    pending = []
    for r in sorted(recs, key=lambda r: r["date"], reverse=True):
        if r["date"] < week_start or "candidates" not in r:
            continue
        for tid, sents in r["candidates"].items():
            for i, s in enumerate(sents[:1]):
                cid = hashlib.sha1(f"{r['id']}|{tid}|{s}".encode()).hexdigest()[:10]
                if cid in decided:
                    continue
                pending.append({"cid": cid, "theme": tid, "theme_label": labels.get(tid, tid),
                                "source": r["source"], "source_label": SOURCES[r["source"]]["label"],
                                "date": r["date"], "title": r["title"], "link": r["link"], "text": s})
    # keep the queue reviewable: at most 3 per theme, 25 total
    capped, per = [], {}
    for p in pending:
        if per.get(p["theme"], 0) < 3 and len(capped) < 25:
            capped.append(p)
            per[p["theme"]] = per.get(p["theme"], 0) + 1
    (ND / "pending.json").write_text(json.dumps(capped, ensure_ascii=False, indent=1))

    counts = {}
    for r in recs:
        if iso_week(r["date"]) in last4:
            counts[r["source"]] = counts.get(r["source"], 0) + 1
    first = min((r["date"] for r in recs), default=None)
    summary = {
        "generated_utc": now().isoformat(timespec="seconds"),
        "weeks": complete, "window": last4, "items_last4w": counts,
        "archive_from": first, "archive_items": len(recs), "pending_recount": stale_n, "dictionary_version": ver,
        "kremlin_pairs_last4w": sum(1 for p in pairs if iso_week(p[0]["date"]) in last4),
        "sources": {k: {kk: v for kk, v in s.items() if kk in ("label", "audience", "tier", "grade", "licence")} for k, s in SOURCES.items()},
        "themes": themes_out,
        "approved": approved[-60:],
        "pending_count": len(capped),
        "method": cfg["method"],
    }
    status = ND / "collect_status.json"
    if status.exists():
        summary["collect_status"] = json.loads(status.read_text())
    (ND / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))

    # issue body for the review queue
    lines = [f"Weekly review queue, generated {summary['generated_utc']}.",
             "",
             "Tick each statement that is **accurately quoted and genuinely expresses the narrative**. "
             "Ticked items appear in the app as approved exemplars. When you close this issue, anything left "
             "unticked is recorded as rejected and will not be proposed again.",
             "",
             "Check each against its link before ticking. Keyword matching can catch rebuttals, quotations of "
             "other people, and sarcasm.", ""]
    for p in capped:
        lines.append(f"- [ ] `{p['cid']}` **{p['theme_label']}** · {p['source_label']} · {p['date']}  ")
        lines.append(f"  > {p['text']}  ")
        lines.append(f"  [{p['title'][:90]}]({p['link']})")
    if not capped:
        lines.append("_Nothing new to review this week._")
    (ND / "review_issue.md").write_text("\n".join(lines))
    print(json.dumps({"weeks": complete, "pending": len(capped), "archive_items": len(recs)}, indent=1))


# ---------------------------------------------------------------- review
def apply_review(body_file, closing=False):
    body = Path(body_file).read_text()
    pend = json.loads((ND / "pending.json").read_text()) if (ND / "pending.json").exists() else []
    by = {p["cid"]: p for p in pend}
    approved = json.loads((ND / "approved.json").read_text()) if (ND / "approved.json").exists() else []
    rejected = json.loads((ND / "rejected.json").read_text()) if (ND / "rejected.json").exists() else []
    done = {a["cid"] for a in approved} | {r["cid"] for r in rejected}
    stamp = now().isoformat(timespec="seconds")
    ticked = set(re.findall(r"- \[[xX]\] `([0-9a-f]{10})`", body))
    unticked = set(re.findall(r"- \[ \] `([0-9a-f]{10})`", body))
    n_a = n_r = 0
    for cid in ticked:
        if cid in by and cid not in done:
            approved.append({**by[cid], "approved_utc": stamp})
            done.add(cid)
            n_a += 1
    if closing:
        for cid in unticked:
            if cid in by and cid not in done:
                rejected.append({"cid": cid, "rejected_utc": stamp})
                done.add(cid)
                n_r += 1
    (ND / "approved.json").write_text(json.dumps(approved, ensure_ascii=False, indent=1))
    (ND / "rejected.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=1))
    print(json.dumps({"approved_now": n_a, "rejected_now": n_r}))


if __name__ == "__main__":
    ND.mkdir(parents=True, exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "collect":
        days = int(sys.argv[sys.argv.index("--backfill-days") + 1]) if "--backfill-days" in sys.argv else 0
        collect(days)
    elif cmd == "recompute":
        recompute(force="--force" in sys.argv)
    elif cmd == "weekly":
        recompute()
        weekly()
    elif cmd == "apply-review":
        apply_review(sys.argv[2], closing="--closing" in sys.argv)
    else:
        print(__doc__)
        sys.exit(2)
