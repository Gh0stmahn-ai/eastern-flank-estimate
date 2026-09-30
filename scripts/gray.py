#!/usr/bin/env python3
"""
Gray-zone indicators beyond the battlefield.

  daily                    Central Bank of Russia key rate and rouble rates        (changes daily)
  weekly                   Oryx losses, sanctioned shadow-fleet vessels,
                           and new influence-operation reporting for review         (changes weekly)
  apply-review BODY [--closing]   record your decisions on reporting candidates

Everything written here is a derived figure with its source and retrieval time.
Nothing from the publications monitor is shown in the app until you approve it.
"""
import csv
import datetime as dt
import hashlib
import html
import io
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
G = ROOT / "data" / "gray"
UA = {"User-Agent": "EasternFlankEstimate/1.0 (non-commercial research; github.com/Gh0stmahn-ai/eastern-flank-estimate)"}


def now():
    return dt.datetime.now(dt.timezone.utc)


def get(url, timeout=60, binary=False):
    last = None
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.content if binary else r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404, 410):
                break
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def load(name, default):
    p = G / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    G.mkdir(parents=True, exist_ok=True)
    (G / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ============================================================ DAILY: economy
def economy():
    out = load("economy.json", {"series": [], "source": "Central Bank of Russia", "errors": []})
    today = now().date().isoformat()
    row = {"date": today}
    errs = []
    try:
        xml = get("https://www.cbr.ru/scripts/XML_daily_eng.asp")
        for code in ("USD", "EUR", "CNY"):
            m = re.search(rf"<CharCode>{code}</CharCode>.*?<Nominal>(\d+)</Nominal>.*?<Value>([\d,]+)</Value>", xml, re.S)
            if m:
                row[code] = round(float(m.group(2).replace(",", ".")) / int(m.group(1)), 4)
        m = re.search(r'Date="([\d.]+)"', xml)
        if m:
            d, mo, y = m.group(1).split(".")
            row["cbr_date"] = f"{y}-{mo}-{d}"
    except Exception as e:  # noqa: BLE001
        errs.append(f"rates: {str(e)[:120]}")
    try:
        page = get("https://www.cbr.ru/eng/hd_base/KeyRate/")
        cells = re.findall(r"<td>(\d{2}\.\d{2}\.\d{4})</td>\s*<td>([\d.,]+)</td>", page)
        if cells:
            d, v = cells[0]
            dd, mm, yy = d.split(".")
            row["key_rate"] = float(v.replace(",", "."))
            row["key_rate_date"] = f"{yy}-{mm}-{dd}"
    except Exception as e:  # noqa: BLE001
        errs.append(f"key rate: {str(e)[:120]}")
    # guardrail: reject implausible values rather than publish them
    for k, lo, hi in (("USD", 20, 400), ("EUR", 20, 450), ("CNY", 2, 60), ("key_rate", 1, 60)):
        if k in row and not (lo <= row[k] <= hi):
            errs.append(f"{k}={row[k]} outside plausible range; withheld")
            row.pop(k)
    if len(row) > 1:
        out["series"] = [r for r in out["series"] if r["date"] != today] + [row]
        out["series"] = out["series"][-500:]
    out["updated_utc"] = now().isoformat(timespec="seconds")
    out["errors"] = errs
    save("economy.json", out)
    print(json.dumps(row), errs)


# ============================================================ WEEKLY: Oryx
def oryx():
    out = load("oryx.json", {"series": [], "source": "Oryx (visually confirmed losses)",
                             "url": "https://www.oryxspioenkop.com/2022/02/attack-on-europe-documenting-equipment.html"})
    page = clean(get(out["url"], timeout=90))
    m = re.search(r"Russia\s*-\s*([\d,]+),\s*of which:\s*destroyed:\s*([\d,]+),\s*damaged:\s*([\d,]+),\s*abandoned:\s*([\d,]+),\s*captured:\s*([\d,]+)", page)
    if not m:
        raise RuntimeError("Oryx headline not found; page format may have changed")
    n = lambda s: int(s.replace(",", ""))
    row = {"date": now().date().isoformat(), "total": n(m.group(1)), "destroyed": n(m.group(2)),
           "damaged": n(m.group(3)), "abandoned": n(m.group(4)), "captured": n(m.group(5))}
    for cat, key in (("Tanks", "tanks"), ("Armoured Fighting Vehicles", "afv"), ("Infantry Fighting Vehicles", "ifv"),
                     ("Self-Propelled Artillery", "spg"), ("Aircraft", "aircraft"), ("Helicopters", "helicopters"),
                     ("Naval Ships", "ships")):
        c = re.search(re.escape(cat) + r"\s*\(([\d,]+),", page)
        if c:
            row[key] = n(c.group(1))
    prev = out["series"][-1] if out["series"] else None
    # guardrail: a confirmed-loss count only grows, and never by thousands in a week
    if prev and (row["total"] < prev["total"] or row["total"] - prev["total"] > 3000):
        out["flag"] = f"Implausible change from {prev['total']} to {row['total']}; withheld for review."
        save("oryx.json", out)
        return
    out.pop("flag", None)
    out["series"] = [r for r in out["series"] if r["date"] != row["date"]] + [row]
    out["updated_utc"] = now().isoformat(timespec="seconds")
    save("oryx.json", out)
    print("oryx", row)


# ============================================================ WEEKLY: shadow fleet
JUR = {"EU": ("eu_sanctions_map", "eu_journal_sanctions", "eu_fsf"), "UK": ("gb_fcdo_sanctions", "gb_hmt_sanctions"),
       "US": ("us_ofac_sdn",), "Canada": ("ca_dfatd_sema_sanctions",), "Switzerland": ("ch_seco_sanctions",),
       "Ukraine": ("ua_war_sanctions",)}


def fleet():
    """Vessels listed by each sanctioning authority. Counts cover ALL sanctions regimes, because the
    source does not reliably separate programmes; week-on-week jumps in EU and UK counts usually mark a
    new Russia package. Ukraine's own shadow-fleet classification is reported separately and labelled
    as a belligerent government's list."""
    out = load("fleet.json", {"series": [], "source": "OpenSanctions maritime collection (CC BY-NC 4.0)",
                              "url": "https://www.opensanctions.org/datasets/maritime/"})
    raw = get("https://data.opensanctions.org/datasets/latest/maritime/maritime.csv", timeout=120)
    rows = [r for r in csv.DictReader(io.StringIO(raw)) if r.get("type") == "VESSEL"]
    def listed(dss):
        # one ship can appear once per source list; count distinct ships by IMO number (or name if absent)
        seen, out = set(), []
        for r in rows:
            if any(d in (r.get("datasets") or "").split(";") for d in dss):
                k = (r.get("imo") or "").strip().upper() or ("NAME:" + (r.get("caption") or "").strip().upper())
                if k not in seen:
                    seen.add(k); out.append(r)
        return out
    by = {j: len(listed(dss)) for j, dss in JUR.items() if j != "Ukraine"}
    ua_shadow = len({(r.get("imo") or r.get("caption") or "").upper() for r in rows if "mare.shadow" in (r.get("risk") or "")})
    eu = listed(JUR["EU"])
    flags = {}
    for r in eu:
        f = (r.get("flag") or "").upper() or "UNKNOWN"
        flags[f] = flags.get(f, 0) + 1
    row = {"date": now().date().isoformat(), "listed_by": by, "ua_shadow_list": ua_shadow,
           "eu_top_flags": sorted(flags.items(), key=lambda x: -x[1])[:8]}
    prev = out["series"][-1] if out["series"] else None
    if prev and any(abs(row["listed_by"].get(k, 0) - prev["listed_by"].get(k, 0)) > 600 for k in row["listed_by"]):
        out["flag"] = "A jurisdiction's vessel count moved by more than 600 in a week; withheld for review."
        save("fleet.json", out)
        return
    out.pop("flag", None)
    out["series"] = [r for r in out["series"] if r["date"] != row["date"]] + [row]
    out["updated_utc"] = now().isoformat(timespec="seconds")
    save("fleet.json", out)
    print("fleet", by, "ua_shadow", ua_shadow)


# ============================================================ WEEKLY: reporting monitor
FEEDS = [
    ("DFRLab", "B", "https://dfrlab.org/feed/"),
    ("Microsoft On the Issues", "B", "https://blogs.microsoft.com/on-the-issues/feed/"),
    ("OpenAI", "B", "https://openai.com/news/rss.xml"),
    ("Meta", "B", "https://about.fb.com/feed/"),
    ("Institute for Strategic Dialogue", "B", "https://www.isdglobal.org/feed/"),
    ("EUvsDisinfo", "B", "https://euvsdisinfo.eu/feed/"),
    ("NewsGuard", "C", "https://www.newsguardrealitycheck.com/feed"),
]
OPS_TERMS = {"storm1516": ["storm-1516", "storm 1516"], "doppelganger": ["doppelganger", "doppelgänger", "reliable recent news", "social design agency"],
             "matryoshka": ["matryoshka", "matriochka", "operation overload", "storm-1679"], "pravda": ["pravda network", "portal kombat", "tigerweb"],
             "copycop": ["copycop", "john mark dougan"], "rybar": ["rybar", "storm-1841"]}
RUSSIA_IO = re.compile(r"\b(russia|russian|kremlin)\b.*\b(influence operation|disinformation|propaganda|inauthentic|information manipulation|interference|fimi)\b|\b(influence operation|disinformation|propaganda|inauthentic|information manipulation|interference|fimi)\b.*\b(russia|russian|kremlin)\b", re.I)


def reports():
    seen = set(load("reports_seen.json", []))
    pend = load("reports_pending.json", [])
    errs = []
    cutoff = (now() - dt.timedelta(days=21)).date().isoformat()
    for pub, grade, url in FEEDS:
        try:
            xml = get(url)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{pub}: {str(e)[:100]}")
            continue
        for it in re.findall(r"<item[\s>].*?</item>", xml, re.S):
            def f(tag):
                m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", it, re.S)
                return clean(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))) if m else ""
            title, link, desc, pd = f("title"), f("link"), f("description"), f("pubDate")
            try:
                date = dt.datetime.strptime(pd[:25].strip(), "%a, %d %b %Y %H:%M:%S").date().isoformat()
            except Exception:  # noqa: BLE001
                date = now().date().isoformat()
            if date < cutoff or not link:
                continue
            text = (title + " " + desc).lower()
            ops = [k for k, terms in OPS_TERMS.items() if any(t in text for t in terms)]
            if not ops and not RUSSIA_IO.search(text):
                continue
            cid = "r" + hashlib.sha1(link.encode()).hexdigest()[:9]
            if cid in seen:
                continue
            seen.add(cid)
            pend.append({"cid": cid, "publisher": pub, "grade": grade, "date": date, "title": title[:220], "link": link, "ops": ops})
    save("reports_pending.json", pend[-40:])
    save("reports_seen.json", sorted(seen)[-3000:])
    save("reports_status.json", {"updated_utc": now().isoformat(timespec="seconds"), "errors": errs, "pending": len(pend[-40:])})
    # section appended to the weekly review issue
    lines = ["", "## New influence-operation reporting", "",
             "Tick reports that are genuinely about a Russian influence operation. Approved reports appear in the app's "
             "operations registry with a link; titles are shown, never article text.", ""]
    for p in pend[-40:]:
        tag = (" · " + ", ".join(p["ops"])) if p["ops"] else ""
        lines.append(f"- [ ] `{p['cid']}` **{p['publisher']}** ({p['grade']}) · {p['date']}{tag}  ")
        lines.append(f"  [{p['title']}]({p['link']})")
    if not pend:
        lines.append("_No new reporting matched this week._")
    (G / "review_section.md").write_text("\n".join(lines))
    print("reports pending", len(pend), errs)


def apply_review(body_file, closing=False):
    body = Path(body_file).read_text()
    pend = load("reports_pending.json", [])
    by = {p["cid"]: p for p in pend}
    appr = load("reports_approved.json", [])
    done = {a["cid"] for a in appr}
    ticked = set(re.findall(r"- \[[xX]\] `(r[0-9a-f]{9})`", body))
    unticked = set(re.findall(r"- \[ \] `(r[0-9a-f]{9})`", body))
    stamp = now().isoformat(timespec="seconds")
    for c in ticked:
        if c in by and c not in done:
            appr.append({**by[c], "approved_utc": stamp})
    keep = [p for p in pend if p["cid"] not in ticked and not (closing and p["cid"] in unticked)]
    save("reports_approved.json", appr[-200:])
    save("reports_pending.json", keep)
    print("reports approved", len(ticked & set(by)))


def weekly():
    status = {}
    for name, fn in (("oryx", oryx), ("fleet", fleet), ("reports", reports)):
        try:
            fn()
            status[name] = "ok"
        except Exception as e:  # noqa: BLE001
            status[name] = f"error: {str(e)[:160]}"
    save("weekly_status.json", {"updated_utc": now().isoformat(timespec="seconds"), **status})
    print(status)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "daily":
        economy()
    elif cmd == "weekly":
        weekly()
    elif cmd == "apply-review":
        apply_review(sys.argv[2], closing="--closing" in sys.argv)
    else:
        print(__doc__); sys.exit(2)
