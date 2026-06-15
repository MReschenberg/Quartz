#!/opt/homebrew/bin/python3
"""
End-of-day Zoom meeting-asset scraper.

Reads the meeting titles logged under "### Meetings" in today's day entry of the
current week note (added by meeting_monitor.py when a calendar meeting starts and
you're in a Zoom call), matches each title to that day's Zoom meeting, and saves
its available AI Companion assets — the summary and the transcript (NOT the
recording) — into a per-meeting folder under the current week's Meetings/ folder:

    Work/<year>/Qn/Week n/Meetings/<topic> (YYYY-MM-DD)/summary.md
                                                       /transcript.md

Enumeration uses the same APIs that power hub.zoom.us/meetings
(list_my_meetings + batch_get_meeting_assets); those need a Bearer token, which
is captured live from the page. Doc content is then read with the browser's Zoom
session (cookies imported from Firefox Nightly). Idempotent: existing
summary.md / transcript.md are left untouched.

Usage:
    python3 zoom_notes_scraper.py [--date YYYY-MM-DD] [--headless]
"""

import os, re, glob, argparse, logging, sqlite3, shutil, json, time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

VAULT         = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents")
LOG_FILE      = os.path.join(VAULT, "Scripts", "zoom_notes_scraper.log")
ZOOM_MEETINGS = "https://hub.zoom.us/meetings"
DOC_URL       = "https://hub.zoom.us/doc/{id}"
BASE          = "https://us01docs.zoom.us"
LIST_API      = BASE + "/api/meeting/list_my_meetings"
ASSETS_API    = BASE + "/api/hub/files/batch_get_meeting_assets"
PACIFIC       = ZoneInfo("America/Los_Angeles")
FF_COOKIES_DB = os.path.expanduser(
    "~/Library/Application Support/Firefox/Profiles/6gqnyzas.default-nightly/cookies.sqlite"
)
# Tokens ignored when matching a calendar title to a Zoom meeting topic.
STOP = {"the","and","with","for","a","to","of","meeting","1x1","1-1","x"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

parser = argparse.ArgumentParser()
parser.add_argument("--date",     help="Override date (YYYY-MM-DD)")
parser.add_argument("--headless", action="store_true", help="Run headless (needs prior login)")
args = parser.parse_args()
TARGET = date.fromisoformat(args.date) if args.date else date.today()


# ── Note reading ─────────────────────────────────────────────────────────────────

def quarter(d):
    return f"Q{(d.month - 1) // 3 + 1}"

def find_week_file(d):
    """Locate <year>-Wnn.md anywhere under Work/<year> (handles quarter-straddling weeks)."""
    iso_year, week_num, _ = d.isocalendar()
    filename = f"{iso_year}-W{week_num:02d}.md"
    matches = glob.glob(os.path.join(VAULT, "Work", str(iso_year), "**", filename), recursive=True)
    return matches[0] if matches else None

def find_day_idx(lines, d):
    month, day_num, year = d.strftime('%b'), str(d.day), str(d.year)
    for i, line in enumerate(lines):
        if '📅' not in line:
            continue
        if month in line and year in line:
            if re.search(r'(?<!\d)' + re.escape(day_num) + r'(?!\d)', line):
                return i
    return -1

def find_next_h2(lines, start):
    for i in range(start + 1, len(lines)):
        if lines[i].startswith('## '):
            return i
    return len(lines)

def get_meetings_from_standup(d):
    """Return the list of meeting titles from the ### Meetings section of d's day entry."""
    wf = find_week_file(d)
    if not wf:
        log.error(f"No week file for {d}")
        return []
    with open(wf) as f:
        lines = f.read().split('\n')

    day_idx = find_day_idx(lines, d)
    if day_idx == -1:
        log.error(f"No day entry for {d}")
        return []
    day_end = find_next_h2(lines, day_idx)

    meetings_idx = next(
        (i for i in range(day_idx, day_end) if lines[i].strip() == '### Meetings'), -1)
    if meetings_idx == -1:
        log.info("No ### Meetings section found in today's note")
        return []

    meetings_end = meetings_idx + 1
    while meetings_end < day_end and not (
        lines[meetings_end].startswith('###') or lines[meetings_end].startswith('## ')):
        meetings_end += 1

    names = []
    for line in lines[meetings_idx + 1: meetings_end]:
        m = re.match(r'^-\s+(.+)', line.strip())
        if m and m.group(1).strip():
            names.append(m.group(1).strip())
    return names


# ── Vault folders ────────────────────────────────────────────────────────────────

def ensure_week_folder(d):
    """Return the 'Week n' folder for d, creating it (and moving the loose week file
    in) when needed — same convention as the rest of the vault."""
    iso_year, wn, _ = d.isocalendar()
    wf = find_week_file(d)
    if wf:
        folder = os.path.dirname(wf)
        if os.path.basename(folder) == f"Week {wn}":
            return folder
        new_folder = os.path.join(folder, f"Week {wn}")
        os.makedirs(new_folder, exist_ok=True)
        dest = os.path.join(new_folder, os.path.basename(wf))
        if os.path.abspath(wf) != os.path.abspath(dest):
            shutil.move(wf, dest)
            log.info(f"Created {os.path.basename(new_folder)}/ and moved {os.path.basename(wf)} in")
        return new_folder
    qdir = os.path.join(VAULT, "Work", str(iso_year), quarter(d))
    new_folder = os.path.join(qdir, f"Week {wn}")
    os.makedirs(new_folder, exist_ok=True)
    return new_folder

def base_name(topic):
    name = topic.replace("/", "-").replace(":", "-")
    name = re.sub(r'[\\*?"<>|]', "", name)
    return re.sub(r"\s+", " ", name).strip()


# ── Auth (cookies from Firefox Nightly + live Bearer token) ──────────────────────

def load_zoom_cookies_from_firefox():
    tmp = "/tmp/ff_cookies_scraper.sqlite"
    try:
        shutil.copy(FF_COOKIES_DB, tmp)
        con = sqlite3.connect(tmp)
        rows = con.execute(
            "SELECT host, path, name, value, expiry, isSecure, isHttpOnly, sameSite "
            "FROM moz_cookies WHERE host LIKE '%zoom%'").fetchall()
        con.close()
    except Exception as e:
        log.warning(f"Could not read Firefox cookies: {e}")
        return []
    SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}
    cookies = []
    for host, path, name, value, expiry, secure, http_only, same_site in rows:
        cookies.append({
            "name": name, "value": value, "domain": host, "path": path,
            "expires": (expiry // 1000) if expiry > 32503680000 else (expiry if expiry > 0 else -1),
            "httpOnly": bool(http_only), "secure": bool(secure),
            "sameSite": SAME_SITE.get(same_site, "None"),
        })
    log.info(f"Loaded {len(cookies)} Zoom cookies from Firefox Nightly")
    return cookies

def ms(dt): return int(dt.timestamp() * 1000)

def capture_auth(ctx):
    """Navigate /meetings and grab the Bearer token the SPA attaches to its API calls."""
    cap = {}
    page = ctx.new_page()
    page.on("request", lambda r: cap.update(r.headers)
            if ("list_my_meetings" in r.url and r.method == "POST" and not cap) else None)
    try:
        page.goto(ZOOM_MEETINGS, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    for i in range(60):
        if cap:
            break
        if i % 4 == 0:
            try: page.mouse.wheel(0, 3000)
            except Exception: pass
        time.sleep(0.5)
    url = page.url
    page.close()
    if not cap:
        raise RuntimeError(f"could not capture auth token (landed on {url})")
    keep = ("authorization","x-zm-device-tracking-id","x-zm-cluster-id","x-requested-with",
            "x-zm-docs-container","accept","origin","referer")
    hdr = {k: cap[k] for k in keep if k in cap}
    hdr["content-type"] = "application/json"
    return hdr


# ── Enumeration ──────────────────────────────────────────────────────────────────

def enumerate_day_meetings(ctx, hdr, day):
    start = ms(datetime(day.year, day.month, day.day, 0, 0, tzinfo=PACIFIC))
    end   = ms(datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=PACIFIC))
    meetings, pn = [], 0
    while True:
        r = ctx.request.post(LIST_API, data=json.dumps(
            {"startTime": start, "endTime": end, "pageNum": pn, "pageSize": 50,
             "meetingFilter": 0, "filters": {}, "query": ""}), headers=hdr)
        if not r.ok:
            log.warning(f"list_my_meetings HTTP {r.status}")
            break
        j = r.json()
        meetings += j.get("meetings", [])
        if not j.get("hasNext"):
            break
        pn += 1
        if pn > 20:
            break

    by_acct = defaultdict(list)
    for m in meetings:
        by_acct[m["meetingAccountId"]].append(m["meetingId"])
    assets = {}
    for acct, ids in by_acct.items():
        for i in range(0, len(ids), 20):
            r = ctx.request.post(ASSETS_API, data=json.dumps(
                {"meetingIds": ids[i:i+20], "meetingAccountId": acct}), headers=hdr)
            if r.ok:
                assets.update(r.json().get("meetingAssetsMap", {}))

    out = []
    for m in meetings:
        a = assets.get(m["meetingId"]) or {}
        sd = a.get("summaryDoc") or {}
        sdoc = sd.get("doc") or {}
        sid = sdoc.get("id") if sd.get("accessStatus") == 0 else None
        tid = None
        for item in (a.get("createFromMeeting") or []):
            doc = item.get("doc") or {}
            if doc.get("id") and doc["id"] != sdoc.get("id") and item.get("accessStatus") == 0:
                tid = doc["id"]; break
        d = datetime.fromtimestamp(int(m["startTime"]) / 1000, PACIFIC).date()
        out.append({"id": m["meetingId"], "topic": m["topic"], "date": d,
                    "summaryId": sid, "transcriptId": tid})
    return out


# ── Title → meeting matching ─────────────────────────────────────────────────────

def _norm(s): return re.sub(r"\s+", " ", s).strip().lower()
def _tokens(s): return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 2 and t not in STOP}

def match_title(title, meetings, claimed):
    """Best unclaimed meeting for a calendar title: exact-normalized topic wins,
    else most shared significant tokens (>=1)."""
    nt, tt = _norm(title), _tokens(title)
    best, best_score = None, (-1, -1)
    for m in meetings:
        if m["id"] in claimed:
            continue
        score = (1 if _norm(m["topic"]) == nt else 0, len(tt & _tokens(m["topic"])))
        if score > best_score:
            best_score, best = score, m
    if best and (best_score[0] == 1 or best_score[1] >= 1):
        return best
    return None


# ── Doc extraction ───────────────────────────────────────────────────────────────

EXTRACT_SUMMARY_JS = r"""
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

# Bookmarklet logic: reconstruct the transcript from speaker turns.
EXTRACT_TRANSCRIPT_JS = r"""
(function() {
    var t = [];
    document.querySelectorAll('span[role="img"][aria-label]').forEach(function(a) {
        var name = a.getAttribute('aria-label');
        var header = a.parentElement; if (!header) return;
        var turn = header.parentElement; if (!turn) return;
        var time = '';
        header.querySelectorAll('span').forEach(function(s) {
            if (/^\d{2}:\d{2}:\d{2}$/.test(s.textContent.trim())) time = s.textContent.trim();
        });
        var msgs = [];
        Array.prototype.forEach.call(turn.children, function(c) {
            if (c === header) return;
            var x = c.textContent.trim(); if (x) msgs.push(x);
        });
        if (!msgs.length) return;
        t.push('**' + name + '**' + (time ? ' [' + time + ']' : '') + '\n' + msgs.join(' '));
    });
    if (!t.length) return null;
    return '# Transcript\n\n' + t.join('\n\n') + '\n';
})()
"""

def _scroll_all(page):
    page.evaluate("""async () => { const s=ms=>new Promise(r=>setTimeout(r,ms)); let prev=-1;
        for(let i=0;i<40;i++){ window.scrollTo(0,document.body.scrollHeight);
        document.querySelectorAll('*').forEach(e=>{if(e.scrollHeight>e.clientHeight+200)e.scrollTop=e.scrollHeight;});
        await s(500); if(document.body.scrollHeight===prev)break; prev=document.body.scrollHeight; } }""")
    page.wait_for_timeout(500)

def extract_summary(page, doc_id, meeting_date):
    page.goto(DOC_URL.format(id=doc_id), wait_until="networkidle", timeout=45000)
    try: page.wait_for_selector("[data-block-type]", timeout=15000)
    except PWTimeoutError: pass
    time.sleep(1.5)
    data = page.evaluate(EXTRACT_SUMMARY_JS)
    if not (data.get("body") or "").strip():
        time.sleep(3)
        data = page.evaluate(EXTRACT_SUMMARY_JS)
    body = (data.get("body") or "").strip()
    if not body:
        return None
    date_text = data.get("dateText") or meeting_date.strftime("%A %b %-d")
    title = (data.get("title") or "").strip()
    header = [f"# {title}", "", "> **Zoom meeting summary**" + (f" — {date_text}" if date_text else ""),
              f"> Source: {DOC_URL.format(id=doc_id)}", ""]
    md = "\n".join(header) + "\n" + body + "\n"
    return re.sub(r"\n{3,}", "\n\n", md)

def extract_transcript(page, doc_id):
    page.goto(DOC_URL.format(id=doc_id), wait_until="networkidle", timeout=45000)
    try: page.wait_for_selector('span[role="img"][aria-label]', timeout=15000)
    except PWTimeoutError: pass
    _scroll_all(page)
    return page.evaluate(EXTRACT_TRANSCRIPT_JS)


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    titles = get_meetings_from_standup(TARGET)
    if not titles:
        log.info("No meetings to process — done.")
        return
    log.info(f"{len(titles)} meeting title(s) in note for {TARGET}: {titles}")

    cookies = load_zoom_cookies_from_firefox()

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=args.headless)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        if cookies:
            ctx.add_cookies(cookies)

        try:
            hdr = capture_auth(ctx)
        except Exception as e:
            if not args.headless:
                log.info(f"Auth capture failed ({e}); log in to Zoom in the browser, then press Enter.")
                input("Press Enter after logging in... ")
                hdr = capture_auth(ctx)
            else:
                log.error(f"Auth failed and running headless — aborting: {e}")
                browser.close()
                return

        meetings = enumerate_day_meetings(ctx, hdr, TARGET)
        log.info(f"{len(meetings)} Zoom meeting(s) found for {TARGET}")

        page = ctx.new_page()
        claimed, matched_topics, used_stems = set(), set(), {}

        for title in titles:
            m = match_title(title, meetings, claimed)
            if not m:
                log.warning(f"No Zoom meeting matched note title: '{title}'")
                continue
            claimed.add(m["id"]); matched_topics.add(m["topic"])

            if not (m["summaryId"] or m["transcriptId"]):
                log.info(f"'{title}' -> '{m['topic']}': no summary/transcript available yet")
                continue

            # per-meeting folder, under the week's Meetings/ subfolder
            stem = f"{base_name(m['topic'])} ({m['date'].isoformat()})"
            meetings_root = os.path.join(ensure_week_folder(m["date"]), "Meetings")
            n = used_stems.get((meetings_root, stem), 0) + 1
            used_stems[(meetings_root, stem)] = n
            if n > 1:
                stem = f"{stem} ({n})"
            folder = os.path.join(meetings_root, stem)
            os.makedirs(folder, exist_ok=True)

            wrote = []
            # summary.md
            if m["summaryId"]:
                dst = os.path.join(folder, "summary.md")
                if os.path.exists(dst):
                    log.info(f"summary exists, skip: {os.path.relpath(dst, VAULT)}")
                else:
                    try:
                        md = extract_summary(page, m["summaryId"], m["date"])
                        if md:
                            with open(dst, "w") as f: f.write(md)
                            wrote.append("summary"); log.info(f"SAVED {os.path.relpath(dst, VAULT)}")
                        else:
                            log.info(f"summary empty/not ready for '{m['topic']}'")
                    except Exception as e:
                        log.error(f"summary failed for '{m['topic']}': {e}")
            # transcript.md
            if m["transcriptId"]:
                dst = os.path.join(folder, "transcript.md")
                if os.path.exists(dst):
                    log.info(f"transcript exists, skip: {os.path.relpath(dst, VAULT)}")
                else:
                    try:
                        content = extract_transcript(page, m["transcriptId"])
                        if content:
                            with open(dst, "w") as f: f.write(content)
                            wrote.append("transcript"); log.info(f"SAVED {os.path.relpath(dst, VAULT)}")
                        else:
                            log.info(f"transcript empty/not ready for '{m['topic']}'")
                    except Exception as e:
                        log.error(f"transcript failed for '{m['topic']}': {e}")

            if wrote:
                log.info(f"'{title}' -> {stem}/ [{', '.join(wrote)}]")

        # Visibility: meetings with assets today that weren't in the note
        for m in meetings:
            if m["topic"] not in matched_topics and (m["summaryId"] or m["transcriptId"]):
                log.info(f"(not in note) Zoom meeting with assets today: '{m['topic']}'")

        browser.close()

    log.info("Done.")


if __name__ == "__main__":
    main()
