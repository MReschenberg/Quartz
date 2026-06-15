#!/opt/homebrew/bin/python3
"""
Backfill ALL Zoom AI Companion meeting summaries for a year into the Obsidian
vault, one markdown file per summary, filed under the week the meeting happened.

Comprehensive enumeration uses the same APIs that power hub.zoom.us/meetings:
  POST /api/meeting/list_my_meetings        (windowed, paginated)  -> meetings
  POST /api/hub/files/batch_get_meeting_assets                     -> summaryDoc per meeting
This catches every meeting with a summary, including ones shared with you and
ones the "recent summaries" list omits. The Bearer token the SPA uses is
captured live from the page (cookies alone return 401 on these endpoints).

Week placement follows the Week 23 / Week 24 convention: the 2026-Wnn.md week
file lives in its own "Week n" folder. If that folder doesn't exist it is
created and the loose week file moved into it. The week file is located
year-wide (not per-quarter) so weeks that straddle a quarter boundary follow
wherever the week file actually lives.

Filenames are "<topic> (YYYY-MM-DD).md" (a counter is added for two summaries
with the same topic on the same day). Idempotent: a summary already on disk
(matched by its embedded Source doc id) is left in place; a legacy-named file
from an earlier run is migrated to the canonical name.

Usage:
    python3 zoom_summaries_backfill.py [--year 2026] [--dry-run]
"""

import os, re, glob, json, argparse, logging, sqlite3, shutil, time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from playwright.sync_api import sync_playwright

VAULT   = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents")
LOG_FILE= os.path.join(VAULT, "Scripts", "zoom_summaries_backfill.log")
MANIFEST= os.path.join(VAULT, "Scripts", "zoom_summaries_backfill.manifest.json")
PACIFIC = ZoneInfo("America/Los_Angeles")
FF_DB   = os.path.expanduser("~/Library/Application Support/Firefox/Profiles/6gqnyzas.default-nightly/cookies.sqlite")
BASE    = "https://us01docs.zoom.us"
LIST_API   = BASE + "/api/meeting/list_my_meetings"
ASSETS_API = BASE + "/api/hub/files/batch_get_meeting_assets"
DOC_URL    = "https://hub.zoom.us/doc/{id}"
SOURCE_RE  = re.compile(r"Source: https://hub\.zoom\.us/doc/([A-Za-z0-9_-]+)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger(__name__)


# ── Auth ────────────────────────────────────────────────────────────────────────

def load_cookies():
    tmp = "/tmp/ff_cookies_backfill.sqlite"
    shutil.copy(FF_DB, tmp)
    con = sqlite3.connect(tmp)
    rows = con.execute("SELECT host,path,name,value,expiry,isSecure,isHttpOnly,sameSite "
                       "FROM moz_cookies WHERE host LIKE '%zoom%'").fetchall()
    con.close()
    SS = {0:"None",1:"Lax",2:"Strict"}
    return [{"name":n,"value":v,"domain":h,"path":p,
             "expires":(e//1000) if e>32503680000 else (e if e>0 else -1),
             "httpOnly":bool(ho),"secure":bool(s),"sameSite":SS.get(ss,"None")}
            for h,p,n,v,e,s,ho,ss in rows]

def ms(dt): return int(dt.timestamp()*1000)


# ── Vault helpers ───────────────────────────────────────────────────────────────

def find_week_file(d):
    """Locate 2026-Wnn.md anywhere under Work/<year> (handles quarter-straddling weeks)."""
    _, wn, _ = d.isocalendar()
    fn = f"{d.isocalendar()[0]}-W{wn:02d}.md"
    matches = glob.glob(os.path.join(VAULT, "Work", str(d.isocalendar()[0]), "**", fn), recursive=True)
    return matches[0] if matches else None

def ensure_week_folder(d, dry=False):
    iso_year, wn, _ = d.isocalendar()
    wf = find_week_file(d)
    if wf:
        folder = os.path.dirname(wf)
        if os.path.basename(folder) == f"Week {wn}":
            return folder  # already foldered
        new_folder = os.path.join(folder, f"Week {wn}")  # loose -> fold in same quarter dir
        if dry:
            log.info(f"[dry] create {os.path.relpath(new_folder, VAULT)} + move {os.path.basename(wf)} in")
            return new_folder
        os.makedirs(new_folder, exist_ok=True)
        dest = os.path.join(new_folder, os.path.basename(wf))
        if os.path.abspath(wf) != os.path.abspath(dest):
            shutil.move(wf, dest)
            log.info(f"Created {os.path.relpath(new_folder, VAULT)}/ and moved {os.path.basename(wf)} in")
        return new_folder
    # no week file at all -> fall back to quarter-by-month
    qdir = os.path.join(VAULT, "Work", str(iso_year), f"Q{(d.month-1)//3+1}")
    new_folder = os.path.join(qdir, f"Week {wn}")
    if dry:
        log.info(f"[dry] create {os.path.relpath(new_folder, VAULT)} (no week file to move)")
        return new_folder
    os.makedirs(new_folder, exist_ok=True)
    return new_folder

def meetings_dir_for(d, layout="auto", dry=False):
    """Return the 'Meetings' folder a meeting on date d belongs in.
    layout='auto': weekly vaults (a <year>-Wnn.md exists for that week) -> Week n/Meetings,
    otherwise -> Qn/Meetings. layout='quarter': always Qn/Meetings (e.g. for 2025,
    whose lone stray week file shouldn't pull a few meetings into a Week folder)."""
    if layout != "quarter" and find_week_file(d):
        return os.path.join(ensure_week_folder(d, dry=dry), "Meetings")
    return os.path.join(VAULT, "Work", str(d.year), f"Q{(d.month-1)//3+1}", "Meetings")

def base_name(topic):
    name = topic.replace("/", "-").replace(":", "-")
    name = re.sub(r'[\\*?"<>|]', "", name)
    return re.sub(r"\s+", " ", name).strip()

def scan_existing(year):
    """Map docId -> path for summaries we previously wrote anywhere under Work/<year>."""
    out = {}
    for p in glob.glob(os.path.join(VAULT, "Work", str(year), "**", "*.md"), recursive=True):
        try:
            with open(p) as f:
                head = f.read(600)
        except Exception:
            continue
        m = SOURCE_RE.search(head)
        if m:
            out[m.group(1)] = p
    return out


# ── Extraction ──────────────────────────────────────────────────────────────────

EXTRACT_JS = r"""
() => {
  const ZW = /[​‌‍﻿]/g;
  const clean = s => (s || '').replace(ZW, '').replace(/\s+/g, ' ').trim();
  let dateText = '';
  const dre = /Meeting summary\s*-?\s*((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+[A-Z][a-z]{2}\w*\s+\d{1,2},?\s*\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})/;
  for (const el of document.querySelectorAll('button, span, div')) {
    const m = clean(el.innerText).match(dre);
    if (m) { dateText = m[1].trim(); break; }
  }
  const MAP = {BLOCK_TYPE_HEADING1:'# ',BLOCK_TYPE_HEADING2:'## ',BLOCK_TYPE_HEADING3:'### ',BLOCK_TYPE_HEADING4:'#### '};
  const lines = [];
  document.querySelectorAll('[data-block-type]').forEach(block => {
    const type = block.getAttribute('data-block-type');
    if (type === 'BLOCK_TYPE_PAGE') return;
    const content = block.querySelector('.zm-block-content');
    if (!content) return;
    const text = clean(content.innerText);
    if (!text) return;
    if (MAP[type]) lines.push('\n' + MAP[type] + text);
    else if (type === 'BLOCK_TYPE_BULLET') lines.push('- ' + text.replace(/^[•◦▪‣·\-\*]\s*/, ''));
    else lines.push(text + '\n');
  });
  return { dateText, body: lines.join('\n'), title: document.title };
}
"""

def extract_doc(page, url):
    page.goto(url, wait_until="networkidle", timeout=45000)
    try:
        page.wait_for_selector("[data-block-type]", timeout=15000)
    except Exception:
        pass
    time.sleep(1.5)
    data = page.evaluate(EXTRACT_JS)
    if not (data.get("body") or "").strip():
        time.sleep(3)
        data = page.evaluate(EXTRACT_JS)
    return data

def build_markdown(title, date_text, source_url, body):
    header = [f"# {title}", "", "> **Zoom meeting summary**" + (f" — {date_text}" if date_text else ""),
              f"> Source: {source_url}", ""]
    md = "\n".join(header) + "\n" + body.strip() + "\n"
    return re.sub(r"\n{3,}", "\n\n", md)


# ── Enumeration ─────────────────────────────────────────────────────────────────

def capture_auth(ctx):
    captured = {}
    page = ctx.new_page()
    page.on("request", lambda r: captured.update(r.headers)
            if ("list_my_meetings" in r.url and r.method == "POST" and not captured) else None)
    try:
        page.goto("https://hub.zoom.us/meetings", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    for i in range(60):
        if captured:
            break
        if i % 4 == 0:
            try: page.mouse.wheel(0, 3000)
            except Exception: pass
        time.sleep(0.5)
    page.close()
    if not captured:
        raise RuntimeError("could not capture auth headers from /meetings")
    keep = ("authorization","x-zm-device-tracking-id","x-zm-cluster-id","x-requested-with",
            "x-zm-docs-container","accept","origin","referer")
    hdr = {k: captured[k] for k in keep if k in captured}
    hdr["content-type"] = "application/json"
    return hdr

def enumerate_summaries(ctx, hdr, year):
    start = ms(datetime(year,1,1,tzinfo=PACIFIC))
    end   = ms(datetime(year,12,31,23,59,tzinfo=PACIFIC))
    now   = ms(datetime.now(PACIFIC))
    end   = min(end, now)
    WINDOW = 28*24*3600*1000

    meetings, seen, ws = [], set(), start
    while ws < end:
        we = min(ws + WINDOW, end); pn = 0
        while True:
            body = {"startTime":ws,"endTime":we,"pageNum":pn,"pageSize":50,
                    "meetingFilter":0,"filters":{},"query":""}
            r = ctx.request.post(LIST_API, data=json.dumps(body), headers=hdr)
            if not r.ok:
                log.warning(f"list_my_meetings {r.status} for window starting {datetime.fromtimestamp(ws/1000,PACIFIC).date()}")
                break
            j = r.json()
            for m in j.get("meetings", []):
                if m["meetingId"] not in seen:
                    seen.add(m["meetingId"]); meetings.append(m)
            if not j.get("hasNext"):
                break
            pn += 1
            if pn > 40:
                break
        ws = we
    log.info(f"Enumerated {len(meetings)} meetings in {year}")

    by_acct = defaultdict(list)
    for m in meetings:
        by_acct[m["meetingAccountId"]].append(m["meetingId"])
    assets = {}
    for acct, ids in by_acct.items():
        for i in range(0, len(ids), 20):
            r = ctx.request.post(ASSETS_API,
                                 data=json.dumps({"meetingIds": ids[i:i+20], "meetingAccountId": acct}),
                                 headers=hdr)
            if r.ok:
                assets.update(r.json().get("meetingAssetsMap", {}))
            else:
                log.warning(f"batch_get_meeting_assets {r.status}")

    rows = []
    for m in meetings:
        a = assets.get(m["meetingId"])
        if not a:
            continue
        sd = a.get("summaryDoc") or {}
        doc = sd.get("doc") or {}
        if not doc.get("id"):
            continue
        d = datetime.fromtimestamp(int(m["startTime"])/1000, PACIFIC).date()
        rows.append({"date": d, "topic": m["topic"], "docId": doc["id"],
                     "acc": sd.get("accessStatus"), "title": doc.get("title")})

    # Union with the "recent summaries" list — it occasionally surfaces summary
    # docs (e.g. unsynced calendar meetings) that list_my_meetings doesn't.
    have = {r["docId"] for r in rows}
    added = 0
    for r in fetch_recent_summaries(ctx, hdr):
        if r["docId"] not in have and r["date"] and r["date"].year == year:
            rows.append(r); have.add(r["docId"]); added += 1
    if added:
        log.info(f"+{added} extra summaries from the recent-summaries list")

    rows.sort(key=lambda r: (r["date"], r["topic"], r["docId"]))
    return rows

def fetch_recent_summaries(ctx, hdr):
    url = BASE + "/api/file/recent?limit=50&fileFilters[]=FILE_FILTER_SUMMARY"
    out, token = [], None
    for _ in range(20):
        r = ctx.request.get(url + (f"&pagingToken={token}" if token else ""), headers=hdr)
        if not r.ok:
            log.warning(f"recent-summaries list {r.status}")
            break
        j = r.json()
        for f in j.get("recentFiles", []):
            fi = f["file"]; t = fi.get("createdInfo", {}).get("time", "")
            try:
                d = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(PACIFIC).date()
            except Exception:
                d = None
            out.append({"date": d, "topic": fi["title"], "docId": fi["id"],
                        "acc": 0, "title": fi["title"]})
        token = j.get("nextPagingToken")
        if not token:
            break
    return out


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--layout", choices=["auto", "quarter"], default="auto",
                    help="auto: Week n/Meetings where a week file exists, else Qn/Meetings; "
                         "quarter: always Qn/Meetings")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cookies = load_cookies()
    existing = scan_existing(args.year)
    log.info(f"{len(existing)} summaries already on disk for {args.year}")

    manifest = {"saved": [], "skipped": [], "migrated": [], "inaccessible": [], "errors": [], "empty": []}
    used_names = {}

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=not args.no_headless)
        ctx = browser.new_context(viewport={"width":1440,"height":1000})
        if cookies:
            ctx.add_cookies(cookies)

        hdr = capture_auth(ctx)
        rows = enumerate_summaries(ctx, hdr, args.year)
        ok = [r for r in rows if r["acc"] == 0]
        bad = [r for r in rows if r["acc"] != 0]
        manifest["inaccessible"] = [{"date": str(r["date"]), "topic": r["topic"], "acc": r["acc"]} for r in bad]
        log.info(f"{len(rows)} summaries found | {len(ok)} accessible | {len(bad)} inaccessible")

        page = ctx.new_page()
        for i, r in enumerate(ok, 1):
            d, topic, docId = r["date"], r["topic"], r["docId"]
            mdir = meetings_dir_for(d, layout=args.layout, dry=args.dry_run)

            # per-meeting folder: <Meetings>/<topic> (date)[ (n)]/summary.md
            stem = f"{base_name(topic)} ({d.isoformat()})"
            n = used_names.get((mdir, stem), 0) + 1
            used_names[(mdir, stem)] = n
            if n > 1:
                stem = f"{stem} ({n})"
            folder = os.path.join(mdir, stem)
            out_path = os.path.join(folder, "summary.md")

            # idempotency: a summary for this doc already on disk
            if docId in existing:
                old = existing[docId]
                if os.path.abspath(old) == os.path.abspath(out_path):
                    manifest["skipped"].append(out_path)
                    continue
                if not args.dry_run:
                    os.remove(old)
                manifest["migrated"].append({"from": old, "to": out_path})

            if args.dry_run:
                log.info(f"[dry] ({i}/{len(ok)}) {os.path.relpath(out_path, VAULT)}")
                manifest["saved"].append(out_path)
                continue

            try:
                data = extract_doc(page, DOC_URL.format(id=docId))
            except Exception as e:
                log.error(f"({i}/{len(ok)}) ERROR {topic} [{d}]: {e}")
                manifest["errors"].append({"date": str(d), "topic": topic, "error": str(e)})
                continue

            body = (data.get("body") or "").strip()
            if not body:
                log.warning(f"({i}/{len(ok)}) EMPTY {topic} [{d}]")
                manifest["empty"].append({"date": str(d), "topic": topic, "docId": docId})
                continue

            date_text = data.get("dateText") or d.strftime("%A %b %-d")
            title = (data.get("title") or topic).strip()
            md = build_markdown(title, date_text, DOC_URL.format(id=docId), body)
            os.makedirs(folder, exist_ok=True)
            with open(out_path, "w") as f:
                f.write(md)
            existing[docId] = out_path
            log.info(f"({i}/{len(ok)}) SAVED {os.path.relpath(out_path, VAULT)}")
            manifest["saved"].append(out_path)

        browser.close()

    json.dump(manifest, open(MANIFEST, "w"), indent=2)
    print("\n================ SUMMARY ================")
    print(f"Year {args.year}: saved={len(manifest['saved'])} skipped={len(manifest['skipped'])} "
          f"migrated={len(manifest['migrated'])} empty={len(manifest['empty'])} "
          f"errors={len(manifest['errors'])} inaccessible={len(manifest['inaccessible'])}")
    if manifest["errors"]:
        print("ERRORS:")
        for e in manifest["errors"]:
            print(f"  {e['date']} {e['topic']}: {e['error']}")
    if manifest["inaccessible"]:
        print("INACCESSIBLE (no access on hub):")
        for e in manifest["inaccessible"]:
            print(f"  {e['date']} {e['topic']} (acc={e['acc']})")
    print(f"manifest: {MANIFEST}")


if __name__ == "__main__":
    main()
