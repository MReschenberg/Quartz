#!/opt/homebrew/bin/python3
"""
Download a Zoom AI Companion *meeting summary* (a Zoom Doc) into the Obsidian
vault, filed under the week the meeting took place.

Companion to zoom_notes_scraper.py — reuses its Firefox-cookie auth approach but
targets summary docs (hub.zoom.us/doc/...) rather than /notes transcripts.

Find a summary either by its title on hub.zoom.us/home (--name) or by a direct
doc URL (--url). The meeting date is read from the doc's "Meeting summary -
<date>" header and used to pick the Work/<year>/<quarter>/Week <n> folder,
following the convention in Week 23 / Week 24 (the 2026-Wnn.md week file lives
inside its own "Week n" folder). If that folder doesn't exist yet, it is created
and the loose 2026-Wnn.md week file is moved into it before the summary is saved.

Usage:
    python3 zoom_summary_download.py --name "[Check-in] Natalie Performance VI"
    python3 zoom_summary_download.py --url https://hub.zoom.us/doc/XXXX
    Add --dry-run to print the markdown + target path without writing.
"""

import os, re, glob, argparse, logging, sqlite3, shutil, time
from datetime import date, datetime
from playwright.sync_api import sync_playwright

VAULT         = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents")
LOG_FILE      = os.path.join(VAULT, "Scripts", "zoom_summary_download.log")
ZOOM_HOME     = "https://hub.zoom.us/home"
FF_COOKIES_DB = os.path.expanduser(
    "~/Library/Application Support/Firefox/Profiles/6gqnyzas.default-nightly/cookies.sqlite"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ── Auth (reused from zoom_notes_scraper.py) ────────────────────────────────────

def load_zoom_cookies_from_firefox():
    tmp = "/tmp/ff_cookies_summary.sqlite"
    try:
        shutil.copy(FF_COOKIES_DB, tmp)
        con = sqlite3.connect(tmp)
        rows = con.execute(
            "SELECT host, path, name, value, expiry, isSecure, isHttpOnly, sameSite "
            "FROM moz_cookies WHERE host LIKE '%zoom%'"
        ).fetchall()
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


# ── Vault helpers (reused from zoom_notes_scraper.py) ───────────────────────────

def quarter(d):
    return f"Q{(d.month - 1) // 3 + 1}"

def week_file_for(d):
    """Path to the 2026-Wnn.md week file, wherever it lives under the quarter."""
    _, week_num, _ = d.isocalendar()
    filename = f"{d.year}-W{week_num:02d}.md"
    quarter_dir = os.path.join(VAULT, "Work", str(d.year), quarter(d))
    matches = glob.glob(os.path.join(quarter_dir, "**", filename), recursive=True)
    return matches[0] if matches else None

def ensure_week_folder(d):
    """
    Return the 'Week <n>' folder for date d, creating it if necessary.

    If the 2026-Wnn.md week file is still loose in the quarter directory, create
    a 'Week <n>' folder and move the week file into it (Week 23 / Week 24 style),
    then return the new folder. If the week file already lives inside a folder,
    return that folder.
    """
    _, week_num, _ = d.isocalendar()
    quarter_dir = os.path.join(VAULT, "Work", str(d.year), quarter(d))
    wf = week_file_for(d)

    if wf and os.path.abspath(os.path.dirname(wf)) != os.path.abspath(quarter_dir):
        return os.path.dirname(wf)  # already inside its own folder

    new_folder = os.path.join(quarter_dir, f"Week {week_num}")
    os.makedirs(new_folder, exist_ok=True)
    if wf:  # move the loose week file into the new folder
        dest = os.path.join(new_folder, os.path.basename(wf))
        if os.path.abspath(wf) != os.path.abspath(dest):
            shutil.move(wf, dest)
            log.info(f"Moved week file into folder: {dest}")
    return new_folder

def safe_filename(name):
    clean = re.sub(r'[\\/*?:"<>|]', '', name).strip()
    return f"{clean}.md"


# ── Doc extraction ──────────────────────────────────────────────────────────────

EXTRACT_JS = r"""
() => {
  const ZW = /[​‌‍﻿]/g;
  const clean = s => (s || '').replace(ZW, '').replace(/\s+/g, ' ').trim();

  // Meeting date/time from the "Meeting summary - <date>" header button.
  // Bounded capture (through the HH:MM-HH:MM range) so a large ancestor's
  // innerText can't drag the whole document into the match.
  let dateText = '';
  const dre = /Meeting summary\s*-?\s*((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+[A-Z][a-z]{2}\w*\s+\d{1,2},?\s*\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})/;
  for (const el of document.querySelectorAll('button, span, div')) {
    const m = clean(el.innerText).match(dre);
    if (m) { dateText = m[1].trim(); break; }
  }

  // Walk content blocks in document order
  const MAP = {
    BLOCK_TYPE_HEADING1: '# ',
    BLOCK_TYPE_HEADING2: '## ',
    BLOCK_TYPE_HEADING3: '### ',
    BLOCK_TYPE_HEADING4: '#### ',
  };
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
    else lines.push(text + '\n');  // paragraph
  });

  return { dateText, body: lines.join('\n'), title: document.title };
}
"""

DATE_RE = re.compile(
    r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+([A-Z][a-z]{2})\w*\s+(\d{1,2})'
)
MONTHS = {m: i for i, m in enumerate(
    ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'], 1)}

def parse_meeting_date(date_text, default_year):
    """Parse 'Tuesday Jun 2, 15:01-16:12' -> date(default_year, 6, 2)."""
    m = DATE_RE.search(date_text or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(2))
    if not mon:
        return None
    return date(default_year, mon, int(m.group(3)))


def open_doc(page, context, name=None, url=None):
    if url:
        page.goto(url, wait_until="networkidle", timeout=45000)
        time.sleep(2)
        return page

    page.goto(ZOOM_HOME, wait_until="networkidle", timeout=45000)
    time.sleep(3)
    target = page.get_by_role("link", name=name, exact=True).first
    try:
        target.wait_for(state="visible", timeout=8000)
    except Exception:
        target = page.get_by_text(name, exact=True).first

    new_page = None
    try:
        with context.expect_page(timeout=5000) as pinfo:
            target.click()
        new_page = pinfo.value
    except Exception:
        target.click()
    active = new_page or page
    time.sleep(3)
    try:
        active.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(2)
    return active


def build_markdown(title, date_text, source_url, body):
    header = [f"# {title}", ""]
    meta = "> **Zoom meeting summary**"
    if date_text:
        meta += f" — {date_text}"
    header.append(meta)
    if source_url:
        header.append(f"> Source: {source_url}")
    header.append("")
    md = "\n".join(header) + "\n" + body.strip() + "\n"
    return re.sub(r"\n{3,}", "\n\n", md)  # collapse runs of blank lines


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="Summary doc title as shown on hub.zoom.us/home")
    ap.add_argument("--url",  help="Direct hub.zoom.us/doc/... URL")
    ap.add_argument("--date", help="Override meeting date (YYYY-MM-DD)")
    ap.add_argument("--year", type=int, default=date.today().year,
                    help="Year to assume for the doc's date header (default: this year)")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Print, do not write")
    args = ap.parse_args()

    if not args.name and not args.url:
        ap.error("provide --name or --url")

    cookies = load_zoom_cookies_from_firefox()

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=not args.no_headless)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()

        active = open_doc(page, context, name=args.name, url=args.url)
        doc_url = active.url
        log.info(f"Opened doc: {doc_url}")

        data = active.evaluate(EXTRACT_JS)
        browser.close()

    title = (data.get("title") or args.name or "Zoom summary").strip()
    date_text = data.get("dateText", "")
    body = data.get("body", "")
    if not body.strip():
        log.error("No summary content extracted — aborting.")
        return

    meeting_date = (date.fromisoformat(args.date) if args.date
                    else parse_meeting_date(date_text, args.year))
    if not meeting_date:
        log.error(f"Could not determine meeting date (header was: {date_text!r}). "
                  f"Re-run with --date YYYY-MM-DD.")
        return
    log.info(f"Meeting date: {meeting_date} (ISO week {meeting_date.isocalendar()[1]})")

    markdown = build_markdown(title, date_text, doc_url, body)
    folder = ensure_week_folder(meeting_date)
    out_path = os.path.join(folder, safe_filename(title))

    if args.dry_run:
        print(f"--- TARGET: {out_path}\n")
        print(markdown)
        return

    with open(out_path, "w") as f:
        f.write(markdown)
    log.info(f"Saved: {out_path}")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
