#!/usr/bin/env python3
"""
Weekly OSINT refresh for the Eastern Flank Estimate.

Design rules, in order of importance:

1. FIXED SOURCES ONLY. No open-web, news or social media scraping, and no AI
   rewriting. Every number traces to one named source.
2. TIERED. Every figure carries a tier: 'independent' OSINT geometry, or a
   'belligerent claim'. Claims never feed a headline metric.
3. POLITICAL LAYERS STRIPPED. The territorial source mixes battlefield layers
   with political statements (e.g. Kaliningrad as "occupied East Prussia",
   Karelia, Petsamo, the Kurils). Only whitelisted battlefield layers inside
   Ukraine are ingested, and everything excluded is logged.
4. GUARDRAILS. Implausible values are rejected or flagged, never published
   silently. A failed fetch keeps the last good data and says so.
5. NO SILENT STALENESS. Hand-curated sections carry an as-of date and are
   marked stale automatically.

Sources:
  DeepStateMap.live   territorial geometry + change log (Ukrainian OSINT project)
  russianwarship.rip  mirror of Ukrainian General Staff loss claims (belligerent)

Deliberately NOT ingested:
  ISW control-of-terrain geodata: licence forbids use without written consent.
  Russian MoD claims: no stable machine-readable feed, and ISW documents
  systematic inflation. The asymmetry this creates (one side's claims shown,
  labelled, the other's absent) is disclosed on the page.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from shapely.geometry import shape, box, mapping, MultiLineString, LineString
from shapely.ops import unary_union, transform, linemerge
import pyproj

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HIST = DATA / "history"
UA_AREA_KM2 = 603_628

UA_BOX = box(22.0, 44.2, 40.3, 52.45)
UA = "Mozilla/5.0 (compatible; EasternFlankEstimate/1.0; weekly non-commercial research refresh)"

# ---- guardrail thresholds ----
AREA_MIN, AREA_MAX = 90_000, 160_000      # outside this, geometry is rejected outright
WEEKLY_JUMP_FLAG = 600                     # km2 per week; larger changes are flagged for review
LOSS_WEEK_MAX = 25_000                     # belligerent claim sanity bound per week
CONSISTENCY_MIN = 50                       # km2; below this, direction disagreements are noise

# Battlefield layers accepted from the territorial source. Everything else is logged and dropped.
OCCUPIED_PREFIXES = ("Окуповано /// Occupied", "ОРДЛО", "Окупований Крим", "Острів Тузла")
CONTESTED_PREFIXES = ("Статус невідомий /// Unknown status",)

LAEA = pyproj.Transformer.from_crs(
    "EPSG:4326", "+proj=laea +lat_0=48.5 +lon_0=31.5 +datum=WGS84 +units=m", always_xy=True
).transform


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def get_json(url, tries=4, timeout=60):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(4 * (i + 1))
    raise RuntimeError(f"fetch failed after {tries} tries: {url}: {last}")


def km2(geom):
    return transform(LAEA, geom).area / 1e6


def strip2d(g):
    return transform(lambda x, y, z=None: (x, y), g) if g.has_z else g


def rnd(o, p=3):
    if isinstance(o, (list, tuple)) and o and isinstance(o[0], (int, float)):
        return [round(o[0], p), round(o[1], p)]
    if isinstance(o, (list, tuple)):
        return [rnd(x, p) for x in o]
    return o


def clean_text(s):
    s = html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def first_link(s):
    m = re.search(r'href="([^"]+)"', s or "")
    return m.group(1) if m else "https://deepstatemap.live/en"


# ---------------------------------------------------------------- territory
def classify_layer(name):
    n = (name or "").strip()
    if n.startswith(OCCUPIED_PREFIXES):
        return "occupied"
    if n.startswith(CONTESTED_PREFIXES):
        return "contested"
    return None


def load_territory(snapshot_id):
    g = get_json(f"https://deepstatemap.live/api/history/{snapshot_id}/geojson", timeout=90)
    occ, con, excluded = [], [], {}
    for f in g.get("features", []):
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        name = (f.get("properties") or {}).get("name") or ""
        kind = classify_layer(name)
        s = strip2d(shape(geom)).buffer(0)
        if kind is None or not s.intersects(UA_BOX):
            label = name.split("///")[1].strip() if "///" in name else name.strip()
            label = re.sub(r"\s+", " ", label)[:60] or "(unnamed)"
            # collapse the many dated 'Liberated dd.mm' layers into one bucket
            label = "Liberated (dated layers)" if label.lower().startswith("liberated") else label
            excluded[label] = excluded.get(label, 0) + 1
            continue
        (occ if kind == "occupied" else con).append(s.intersection(UA_BOX))
    occupied = unary_union(occ) if occ else None
    contested = unary_union(con).difference(occupied) if con and occupied else None
    return occupied, contested, excluded


def derive_front(occupied, ua_outline):
    """Front line = boundary of occupied area that lies inside Ukraine,
    i.e. not along the Russian or Belarusian border and not along the coast."""
    inner = ua_outline.buffer(-0.03)          # ~3 km inside the national outline
    b = occupied.boundary.intersection(inner)
    lines = []
    for g in getattr(b, "geoms", [b]):
        if isinstance(g, LineString):
            lines.append(g)
        elif hasattr(g, "geoms"):
            lines.extend(x for x in g.geoms if isinstance(x, LineString))
    merged = linemerge(lines) if lines else None
    if merged is None:
        return None
    parts = list(getattr(merged, "geoms", [merged]))
    parts = [p.simplify(0.006) for p in parts if km_len(p) > 8]
    return MultiLineString(parts) if parts else None


def km_len(line):
    return transform(LAEA, line).length / 1000


# ---------------------------------------------------------------- change log
RU_GAIN = re.compile(r"\b(enemy|russian forces|occupiers?)\b.*\b(occupied|advanced|captured)\b|ворог\S*\s+(окупував|просунувся)", re.I)
UA_GAIN = re.compile(r"\b(liberated|regained control|cleared|pushed back|recaptured)\b|(ukrainian|defen[cs]e forces|armed forces of ukraine).*\badvanced\b|звільнил|відновили контроль", re.I)


def classify_entry(text):
    ru, ua = bool(RU_GAIN.search(text)), bool(UA_GAIN.search(text))
    if ru and not ua:
        return "russian_gain"
    if ua and not ru:
        return "ukrainian_gain"
    return "mixed" if (ru and ua) else "other"


# ---------------------------------------------------------------- main
def main():
    DATA.mkdir(exist_ok=True)
    HIST.mkdir(exist_ok=True)
    prev_latest = {}
    lp = DATA / "latest.json"
    if lp.exists():
        prev_latest = json.loads(lp.read_text())

    flags, notes = [], []
    run_time = now_utc()
    out = {
        "generated_utc": run_time.isoformat(timespec="seconds"),
        "method_version": 1,
        "sources": {
            "territory": {"name": "DeepStateMap.live", "url": "https://deepstatemap.live/en",
                          "tier": "independent OSINT (Ukrainian project)",
                          "licence_note": "Derived statistics and a generalised trace only; the raw API feed is not republished. Textual entries reused with attribution and link, as the licence permits."},
            "losses_claim": {"name": "Ukrainian General Staff, via russianwarship.rip mirror",
                             "url": "https://russianwarship.rip", "tier": "belligerent claim"},
        },
    }

    # ---------- territory ----------
    territory_ok = False
    try:
        hist = get_json("https://deepstatemap.live/api/history/public", timeout=90)

        def ts(e):
            return dt.datetime.fromisoformat((e.get("createdAt") or e.get("updatedAt")).replace("Z", "+00:00"))

        hist = sorted((e for e in hist if e.get("createdAt") or e.get("updatedAt")), key=ts)
        latest = hist[-1]
        target = ts(latest) - dt.timedelta(days=7)
        prior = max((e for e in hist if ts(e) <= target), key=ts)

        occ_now, con_now, excluded = load_territory(latest["id"])
        occ_prev, con_prev, _ = load_territory(prior["id"])
        if occ_now is None or occ_prev is None:
            raise RuntimeError("no occupied layers found; source format may have changed")

        a_now, a_prev = km2(occ_now), km2(occ_prev)
        c_now = km2(con_now) if con_now else 0.0
        c_prev = km2(con_prev) if con_prev else 0.0
        delta = a_now - a_prev

        if not (AREA_MIN <= a_now <= AREA_MAX):
            raise RuntimeError(f"occupied area {a_now:,.0f} km2 outside plausible range {AREA_MIN:,}-{AREA_MAX:,}; rejected")

        if abs(delta) > WEEKLY_JUMP_FLAG:
            flags.append({"level": "review",
                          "text": f"Unusually large weekly change of {delta:+,.0f} km2. Published, but verify against a second source before relying on it."})

        # generalised geometry for display (not the raw feed)
        ua_outline = shape(json.loads((DATA / "ua_outline.json").read_text())["geometry"])
        occ_disp = occ_now.simplify(0.01, preserve_topology=True)
        con_disp = con_now.simplify(0.005, preserve_topology=True) if con_now else None
        front = derive_front(occ_now, ua_outline)

        # change log window
        window = [e for e in hist if ts(prior) < ts(e) <= ts(latest)]
        entries = []
        counts = {"russian_gain": 0, "ukrainian_gain": 0, "mixed": 0, "other": 0}
        for e in window:
            raw = e.get("descriptionEn") or e.get("description") or ""
            txt = clean_text(raw)
            if not txt:
                continue
            k = classify_entry(txt)
            counts[k] += 1
            entries.append({"date": ts(e).date().isoformat(), "text": txt, "kind": k,
                            "lang": "en" if e.get("descriptionEn") else "uk", "link": first_link(raw)})

        net_dir = counts["russian_gain"] - counts["ukrainian_gain"]
        if abs(delta) >= CONSISTENCY_MIN and net_dir != 0 and (delta > 0) != (net_dir > 0):
            flags.append({"level": "review",
                          "text": "The geometry and the source's own change log point in opposite directions this week. Treat the headline change with caution."})

        out["territory"] = {
            "as_of": ts(latest).date().isoformat(),
            "compared_to": ts(prior).date().isoformat(),
            "occupied_km2": round(a_now), "occupied_prev_km2": round(a_prev),
            "delta_km2": round(delta), "occupied_pct": round(a_now / UA_AREA_KM2 * 100, 2),
            "contested_km2": round(c_now), "contested_delta_km2": round(c_now - c_prev),
            "excluded_layers": excluded,
            "geometry": {
                "occupied": rnd(mapping(occ_disp)),
                "contested": rnd(mapping(con_disp)) if con_disp and not con_disp.is_empty else None,
                "front": rnd(mapping(front)) if front else None,
            },
            "changelog": entries[-40:],
            "changelog_counts": counts,
        }
        territory_ok = True
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300]
        if "territory" in prev_latest:
            out["territory"] = prev_latest["territory"]
            out["territory"]["carried_forward"] = True
            flags.append({"level": "stale", "text": f"Territorial source unavailable or rejected this run; last good data from {prev_latest['territory'].get('as_of')} kept. ({msg})"})
        else:
            flags.append({"level": "error", "text": f"Territorial source unavailable and no prior data exists. ({msg})"})

    # ---------- belligerent loss claims ----------
    try:
        cur = get_json("https://russianwarship.rip/api/v2/statistics/latest")["data"]
        d_cur = dt.date.fromisoformat(cur["date"])
        prev = get_json(f"https://russianwarship.rip/api/v2/statistics/{(d_cur - dt.timedelta(days=7)).isoformat()}")["data"]
        prev2 = get_json(f"https://russianwarship.rip/api/v2/statistics/{(d_cur - dt.timedelta(days=14)).isoformat()}")["data"]
        wk = cur["stats"]["personnel_units"] - prev["stats"]["personnel_units"]
        wk_prior = prev["stats"]["personnel_units"] - prev2["stats"]["personnel_units"]
        claim = {
            "as_of": cur["date"], "tier": "belligerent claim",
            "personnel_total": cur["stats"]["personnel_units"],
            "personnel_week": wk, "personnel_prior_week": wk_prior,
            "uav_week": cur["stats"]["uav_systems"] - prev["stats"]["uav_systems"],
            "artillery_week": cur["stats"]["artillery_systems"] - prev["stats"]["artillery_systems"],
            "source_post": cur.get("resource"),
        }
        if not (0 <= wk <= LOSS_WEEK_MAX):
            flags.append({"level": "review", "text": f"Belligerent loss claim of {wk:,} for the week is outside the plausibility bound; not summarised."})
            claim["suppressed"] = True
        out["losses_claim"] = claim
    except Exception as e:  # noqa: BLE001
        if "losses_claim" in prev_latest:
            out["losses_claim"] = prev_latest["losses_claim"]
            out["losses_claim"]["carried_forward"] = True
        flags.append({"level": "stale", "text": f"Loss-claim source unavailable this run. ({str(e)[:160]})"})

    # ---------- curated section staleness ----------
    cur_path = DATA / "curated.json"
    stale = []
    if cur_path.exists():
        for s in json.loads(cur_path.read_text()).get("sections", []):
            age = (run_time.date() - dt.date.fromisoformat(s["as_of"])).days
            status = "stale" if age > s["stale_after_days"] else "current"
            stale.append({**s, "age_days": age, "status": status})
    out["curated"] = stale

    # ---------- deterministic summary ----------
    summary = []
    t = out.get("territory")
    if t:
        verb = "grew" if t["delta_km2"] > 0 else ("shrank" if t["delta_km2"] < 0 else "was unchanged")
        size = f" by {abs(t['delta_km2']):,} km²" if t["delta_km2"] else ""
        summary.append(f"Russian-controlled territory {verb}{size} between {t['compared_to']} and {t['as_of']}, to {t['occupied_km2']:,} km² ({t['occupied_pct']}% of Ukraine). Source: DeepState geometry, independently measured here.")
        if t.get("contested_km2") is not None:
            cd = t.get("contested_delta_km2", 0)
            summary.append(f"Contested or unconfirmed-status ground stands at {t['contested_km2']:,} km² ({cd:+,} km² on the week).")
        c = t.get("changelog_counts", {})
        if sum(c.values()):
            summary.append(f"The source logged {sum(c.values())} changes: {c.get('russian_gain',0)} Russian advances or occupations, {c.get('ukrainian_gain',0)} Ukrainian recaptures or clearances, {c.get('mixed',0)+c.get('other',0)} other.")
        if t.get("carried_forward"):
            summary.append("Territorial figures are carried forward from the last successful run.")
    lc = out.get("losses_claim")
    if lc and not lc.get("suppressed"):
        trend = "up" if lc["personnel_week"] > lc["personnel_prior_week"] else "down"
        summary.append(f"Ukraine's General Staff claims {lc['personnel_week']:,} Russian personnel losses this week, {trend} from {lc['personnel_prior_week']:,} the week before. This is a belligerent's figure, shown for trend only.")
    n_stale = sum(1 for s in stale if s["status"] == "stale")
    if n_stale:
        summary.append(f"{n_stale} hand-curated section{'s are' if n_stale != 1 else ' is'} past its review date and should be re-checked by an analyst.")
    out["summary"] = summary
    out["flags"] = flags

    # ---------- write ----------
    (DATA / "latest.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))

    stamp = run_time.date().isoformat()
    slim = {k: v for k, v in out.items() if k != "territory"}
    if t:
        slim["territory"] = {k: v for k, v in t.items() if k != "geometry"}
    (HIST / f"{stamp}.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1))

    idx_p = DATA / "history_index.json"
    idx = json.loads(idx_p.read_text()) if idx_p.exists() else []
    idx = [r for r in idx if r.get("run") != stamp]
    if t:
        idx.append({"run": stamp, "as_of": t["as_of"], "occupied_km2": t["occupied_km2"],
                    "delta_km2": t["delta_km2"], "contested_km2": t.get("contested_km2")})
    idx.sort(key=lambda r: r["run"])
    idx_p.write_text(json.dumps(idx, indent=1))

    # human-readable changelog in the repo
    cl = ROOT / "CHANGELOG.md"
    head = "# Weekly change log\n\nGenerated automatically each week. Figures are derived; see README for method and source tiers.\n\n"
    body = cl.read_text().split("\n\n", 2)[-1] if cl.exists() else ""
    if f"## {stamp}\n" in body:
        body = re.sub(rf"## {stamp}\n.*?(?=\n## |\Z)", "", body, flags=re.S).lstrip()
    block = f"## {stamp}\n\n" + "\n".join(f"- {s}" for s in summary)
    if flags:
        block += "\n\n**Flags**\n\n" + "\n".join(f"- [{f['level']}] {f['text']}" for f in flags)
    cl.write_text(head + block + "\n\n" + body)

    print(json.dumps({"summary": summary, "flags": flags}, ensure_ascii=False, indent=1))
    return 0 if territory_ok or "territory" in out else 1


if __name__ == "__main__":
    sys.exit(main())
