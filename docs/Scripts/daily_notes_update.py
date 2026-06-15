#!/opt/homebrew/bin/python3
"""
Daily Obsidian notes updater.

Scans today's Firefox Nightly browser history for Bugzilla and Phabricator
activity, then adds it to the appropriate day entry in the correct week note.

- Creates the week note from Templates/Weekly if it doesn't exist.
- Creates the day entry from Templates/Day - Work if it doesn't exist.
- Adds bug/phab sub-sections if they don't exist in the day entry.
- Never removes or moves existing content — additive only.
- Idempotent: re-running the same day won't create duplicates.

Usage:
    python3 daily_notes_update.py [--date YYYY-MM-DD] [--dry-run]
"""

import re, os, sys, json, time, sqlite3, shutil, logging, argparse, glob
from datetime import date, datetime, timezone
from collections import defaultdict
import urllib.request, urllib.parse
from urllib.error import HTTPError

# ── Config ─────────────────────────────────────────────────────────────────────
VAULT        = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents")
ARCRC        = os.path.expanduser("~/.arcrc")
PLACES_DB    = os.path.expanduser(
    "~/Library/Application Support/Firefox/Profiles/6gqnyzas.default-nightly/places.sqlite"
)
USER_EMAIL   = "mreschenberg@mozilla.com"
USER_PHID    = "PHID-USER-lmegrwffx67e2xxsnkfv"
BZ_BASE      = "https://bugzilla.mozilla.org/rest/bug"
PHAB_BASE    = "https://phabricator.services.mozilla.com/api"
LOG_FILE     = os.path.join(VAULT, "Scripts", "daily_notes_update.log")

def quarter(d):
    return f"Q{(d.month - 1) // 3 + 1}"

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Arg parsing ────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--date", help="Process this date instead of today (YYYY-MM-DD)")
parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
args = parser.parse_args()

TARGET = date.fromisoformat(args.date) if args.date else date.today()
DRY_RUN = args.dry_run
log.info(f"Running for {TARGET} {'(dry-run)' if DRY_RUN else ''}")

# ── Phabricator auth ───────────────────────────────────────────────────────────
try:
    with open(ARCRC) as f:
        _arc = json.load(f)
    PHAB_TOKEN = _arc["hosts"]["https://phabricator.services.mozilla.com/api/"]["token"]
except Exception as e:
    log.error(f"Could not load Phabricator token from {ARCRC}: {e}")
    PHAB_TOKEN = None

# ── HTTP helpers ───────────────────────────────────────────────────────────────
def bz_get(url, retries=3):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as r:
                return json.loads(r.read())
        except HTTPError as e:
            if e.code == 429:
                time.sleep(15 * (attempt + 1))
            else:
                return None
        except Exception as e:
            log.warning(f"bz_get {url}: {e}")
            time.sleep(2)
    return None

def conduit(method, params, retries=4):
    if not PHAB_TOKEN:
        return None
    params = dict(params)
    params["api.token"] = PHAB_TOKEN
    data = urllib.parse.urlencode(params).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"{PHAB_BASE}/{method}", data=data)
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
            if resp.get("error_code"):
                return None
            return resp
        except HTTPError as e:
            if e.code == 429:
                wait = 30 * (attempt + 1)
                log.warning(f"Phab 429, waiting {wait}s")
                time.sleep(wait)
            else:
                return None
        except Exception as e:
            log.warning(f"conduit {method}: {e}")
            time.sleep(3)
    return None

# ── 1. Query browser history for today ────────────────────────────────────────
log.info("Querying browser history...")
tmp_db = None
try:
    tmp_db = shutil.copy(PLACES_DB, f"/tmp/places_daily_{TARGET}.sqlite")
    con = sqlite3.connect(tmp_db)
    rows = con.execute("""
        SELECT url, title
        FROM moz_places
        WHERE last_visit_date IS NOT NULL
          AND date(last_visit_date/1000000,'unixepoch','localtime') = ?
          AND (url LIKE '%bugzilla.mozilla.org%' OR url LIKE '%phabricator.services.mozilla.com%')
        GROUP BY url
        ORDER BY last_visit_date ASC
    """, (str(TARGET),)).fetchall()
    con.close()
except Exception as e:
    log.error(f"Could not read browser history: {e}")
    rows = []

bz_urls  = {}   # bug_id -> url
phab_revs = {}  # rev_id -> url, title

for url, title in rows:
    bz_m = re.search(r'show_bug\.cgi\?id=(\d+)', url)
    if bz_m:
        bz_urls[bz_m.group(1)] = url
        continue
    ph_m = re.search(r'/(D\d+)', url)
    if ph_m:
        rid = ph_m.group(1)
        if rid not in phab_revs or (title and len(title) > len(phab_revs[rid].get('title',''))):
            phab_revs[rid] = {'url': f"https://phabricator.services.mozilla.com/{rid}", 'title': title or ''}

log.info(f"Found {len(bz_urls)} Bugzilla bugs, {len(phab_revs)} Phab revisions")

if not bz_urls and not phab_revs:
    log.info("No activity today — exiting.")
    sys.exit(0)

# ── 2. Classify Bugzilla activity ─────────────────────────────────────────────
def extract_reply_name(text):
    m = re.match(r'\(In reply to (.+?)(?:\s+\[.*?\])? from comment', text)
    return m.group(1).split()[0] if m else None

def clean_bz_title(raw):
    t = re.sub(r'^\d+ - ', '', raw or '').strip()
    return t if t and 'Log in' not in t else None

bz_interacted = []   # formatted description strings
bz_triaged    = []   # formatted link strings

for bug_id, bug_url in bz_urls.items():
    cr = bz_get(f"{BZ_BASE}/{bug_id}/comment")
    hr = bz_get(f"{BZ_BASE}/{bug_id}/history")
    time.sleep(0.5)

    comments = (cr or {}).get('bugs', {}).get(bug_id, {}).get('comments', []) if cr else []
    history  = (hr or {}).get('bugs', [{}])[0].get('history', []) if hr else []

    day_comments = [c for c in comments
                    if c.get('creator') == USER_EMAIL and c.get('creation_time','')[:10] == str(TARGET)]
    day_history  = [h for h in history
                    if h.get('who') == USER_EMAIL and h.get('when','')[:10] == str(TARGET)]

    # Get best title from API or fall back to URL
    all_comments_titles = [c.get('text','')[:80] for c in comments[:1]]
    bug_summary = None
    info_resp = bz_get(f"{BZ_BASE}/{bug_id}?include_fields=summary")
    if info_resp:
        bugs = info_resp.get('bugs', [])
        if bugs:
            bug_summary = bugs[0].get('summary','')
    title = bug_summary or f"Bug {bug_id}"
    link = f"[{title}]({bug_url})"
    time.sleep(0.3)

    acted = False

    # Filed?
    first = next((c for c in comments if c.get('count', 99) == 0), None)
    if first and first.get('creator') == USER_EMAIL and first.get('creation_time','')[:10] == str(TARGET):
        bz_interacted.append(f"Filed {link}")
        acted = True
        continue

    for c in day_comments:
        if c.get('attachment_id'):
            bz_interacted.append(f"Submitted a patch for {link}")
            acted = True
        else:
            text = c.get('text', '')
            reply_name = extract_reply_name(text)
            if reply_name:
                bz_interacted.append(f"Replied to {reply_name}'s comments on {link}")
            else:
                bz_interacted.append(f"Commented on {link}")
            acted = True

    if not acted and day_history:
        bz_triaged.append(link)

# Deduplicate while preserving order
def dedup(lst):
    seen, out = set(), []
    for x in lst:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

bz_interacted = dedup(bz_interacted)
bz_triaged    = dedup(bz_triaged)
log.info(f"BZ: {len(bz_interacted)} interacted, {len(bz_triaged)} triaged")

# ── 3. Classify Phabricator activity ──────────────────────────────────────────
SKIP_TYPES = {None,'subscribers','projects','reviewers','testPlan','title','summary',
              'description','inlineState','phamBundle','parent','unsubscribe',
              'void','buildable','request','status'}

user_names = {}

def fname(phid):
    if phid in user_names:
        return user_names[phid]
    resp = conduit("user.search", {f'constraints[phids][0]': phid})
    if resp:
        for u in (resp.get('result') or {}).get('data') or []:
            real = u.get('fields',{}).get('realName','')
            user_names[phid] = real.split()[0] if real else u.get('fields',{}).get('username','someone')
            return user_names[phid]
    return 'someone'

ph_submitted = []
ph_reviewed  = []

for rev_id, rev_data in phab_revs.items():
    # Get revision metadata
    rev_num = int(rev_id[1:])
    meta_resp = conduit("differential.revision.search", {'constraints[ids][0]': rev_num})
    time.sleep(2)

    title, author_phid, status = rev_id, '', ''
    if meta_resp:
        revs = (meta_resp.get('result') or {}).get('data') or []
        if revs:
            fields = revs[0].get('fields', {})
            title       = fields.get('title', rev_id)
            author_phid = fields.get('authorPHID', '')
            status      = (fields.get('status') or {}).get('value', '')

    is_mine = (author_phid == USER_PHID)
    rev_url = rev_data['url']
    suffix  = " *(abandoned)*" if status == 'abandoned' else ""
    aname   = fname(author_phid) if (author_phid and not is_mine) else 'someone'

    # Fetch today's transactions
    txn_resp = conduit("transaction.search", {"objectIdentifier": rev_id})
    time.sleep(6)

    day_txns = []
    if txn_resp:
        for t in (txn_resp.get('result') or {}).get('data') or []:
            if (t.get('authorPHID') == USER_PHID and
                    datetime.fromtimestamp(t['dateCreated'], tz=timezone.utc).strftime('%Y-%m-%d') == str(TARGET) and
                    t.get('type') not in SKIP_TYPES):
                day_txns.append(t.get('type'))

    seen_actions = set()
    def add_action(key, line, bucket):
        if key not in seen_actions:
            bucket.append(line); seen_actions.add(key)

    for tp in day_txns:
        link = f"[{title}]({rev_url})"
        if   tp == 'create'  and is_mine: add_action('create',  f"Created patch {link}{suffix}",          ph_submitted)
        elif tp == 'update'  and is_mine: add_action('update',  f"Pushed a new diff for {link}{suffix}",  ph_submitted)
        elif tp == 'abandon' and is_mine: add_action('abandon', f"Abandoned my patch for {link} *(abandoned)*", ph_submitted)
        elif tp == 'close'   and is_mine: add_action('close',   f"Landed my patch {link}",                ph_submitted)
        elif tp == 'reopen'  and is_mine: add_action('reopen',  f"Reopened my patch {link}",              ph_submitted)
        elif tp == 'comment':
            if is_mine: add_action('comment', f"Posted [a comment]({rev_url}) on my patch {link}{suffix}", ph_submitted)
            else:       add_action('comment', f"Posted [a comment]({rev_url}) on {aname}'s patch {link}",  ph_reviewed)
        elif tp == 'inline':
            if is_mine: add_action('inline', f"Added inline comments on my patch {link}{suffix}",            ph_submitted)
            else:       add_action('inline', f"Added some comments to {aname}'s patch {link}",               ph_reviewed)
        elif tp == 'accept':
            add_action('accept', f"Approved {aname}'s patch for {link}", ph_reviewed)
        elif tp in ('reject', 'request-changes'):
            add_action('rc', f"Requested changes on {aname}'s patch {link}", ph_reviewed)

    # None-type fallback for own revisions
    if not seen_actions and is_mine:
        none_txns = []
        if txn_resp:
            for t in (txn_resp.get('result') or {}).get('data') or []:
                if (t.get('authorPHID') == USER_PHID and
                        datetime.fromtimestamp(t['dateCreated'], tz=timezone.utc).strftime('%Y-%m-%d') == str(TARGET) and
                        t.get('type') is None):
                    none_txns.append(t)
        if none_txns:
            ph_submitted.append(f"Modified my patch [{title}]({rev_url}){suffix}")

log.info(f"Phab: {len(ph_submitted)} submitted, {len(ph_reviewed)} reviewed")

# If nothing to add, bail early
if not any([bz_interacted, bz_triaged, ph_submitted, ph_reviewed]):
    log.info("No interacted content to add — exiting.")
    sys.exit(0)

# ── 4. Find/create week note ───────────────────────────────────────────────────
_, week_num, _ = TARGET.isocalendar()
q = quarter(TARGET)
_week_filename = f"{TARGET.year}-W{week_num:02d}.md"
_quarter_dir   = os.path.join(VAULT, "Work", str(TARGET.year), q)
_matches       = glob.glob(os.path.join(_quarter_dir, "**", _week_filename), recursive=True)
week_file      = _matches[0] if _matches else os.path.join(_quarter_dir, _week_filename)

if not os.path.exists(week_file):
    log.info(f"Creating week note: {week_file}")
    template = os.path.join(VAULT, "Templates", "Weekly.md")
    with open(template) as f:
        tmpl = f.read()
    content = tmpl.replace("{{date:W}}", str(week_num))
    if not DRY_RUN:
        os.makedirs(os.path.dirname(week_file), exist_ok=True)
        with open(week_file, 'w') as f:
            f.write(content)
else:
    with open(week_file) as f:
        content = f.read()

lines = content.split('\n')

# ── 5. Find/create day entry ───────────────────────────────────────────────────
day_heading_str = f"## 📅 {TARGET.strftime('%a - %b %-d %Y')}"

def find_day_idx(lines, d):
    month, day_num, year = d.strftime('%b'), str(d.day), str(d.year)
    for i, line in enumerate(lines):
        if '📅' not in line: continue
        if month in line and year in line:
            if re.search(r'(?<!\d)' + re.escape(day_num) + r'(?!\d)', line):
                return i
    return -1

def find_next_h2(lines, start):
    for i in range(start + 1, len(lines)):
        if lines[i].startswith('## '):
            return i
    return len(lines)

DAY_TEMPLATE = f"""\
{day_heading_str}
### 🏁 Today:
-
### ✅ Tasks
#### Work
- [ ] Post standup
- [ ]
#### Personal
- [ ] Lunch
#### Claude
- [ ]
### 🔗 Bugs Touched
#### Bugs Modified or Created
-
#### Bugs viewed
-
#### Triaged
-
### Phab Revisions Touched
#### Phab Posted/Updated
-
#### Phab Reviewed
-
#### Phab Viewed
-
### 🧠 Thoughts
"""

day_idx = find_day_idx(lines, TARGET)
if day_idx == -1:
    log.info(f"Creating day entry for {TARGET}")
    new_lines = DAY_TEMPLATE.split('\n')
    # Insert in chronological order
    insert_pos = len(lines)
    for i, line in enumerate(lines):
        if line.startswith('## 📅'):
            m = re.search(r'## 📅 \S+ - (\w+ \d+ \d+)', line)
            if m:
                try:
                    existing_d = datetime.strptime(m.group(1).strip(), '%b %d %Y').date()
                    if existing_d > TARGET:
                        insert_pos = i
                        break
                except ValueError:
                    pass
    lines[insert_pos:insert_pos] = new_lines + ['']
    day_idx = find_day_idx(lines, TARGET)

# ── 6. Add content to sections ─────────────────────────────────────────────────
def urls_in_section(lines, start, end):
    """Return set of URLs already present in a section."""
    found = set()
    for l in lines[start:end]:
        for url in re.findall(r'https?://\S+\)', l):
            found.add(url.rstrip(')'))
        for url in re.findall(r'https?://[^\s\)]+', l):
            found.add(url)
    return found

def section_bounds(lines, header, day_start, day_end):
    """Return (header_idx, content_end_idx) for a section within the day, or (-1,-1)."""
    for i in range(day_start, day_end):
        if lines[i].strip() == header.strip():
            end = i + 1
            while end < day_end and not (lines[end].startswith('####') or
                                          lines[end].startswith('###') or
                                          lines[end].startswith('## ')):
                end += 1
            return i, end
    return -1, -1

def insert_items_into_section(lines, header, items, day_start, day_end):
    """Add items to an existing section, skipping duplicates by URL."""
    if not items: return lines, 0
    hdr_idx, content_end = section_bounds(lines, header, day_start, day_end)
    if hdr_idx == -1: return lines, 0
    existing_urls = urls_in_section(lines, hdr_idx, content_end)
    new = []
    for item in items:
        item_urls = set(re.findall(r'https?://[^\s\)\]]+', item))
        if not item_urls or not item_urls.intersection(existing_urls):
            new.append(f"- {item}")
    if not new: return lines, 0
    # Rebuild the section body: keep real bullets, drop empty "- " placeholders,
    # append the new items, and end with a single blank-line separator.
    real = [l for l in lines[hdr_idx + 1:content_end] if l.strip() and l.strip() != '-']
    lines[hdr_idx + 1:content_end] = real + new + ['']
    return lines, len(new)

def ensure_section_exists(lines, section_header, subsections, before_header, day_start, day_end):
    """Create a section with subsections if it doesn't exist."""
    if any(lines[i].strip() == section_header.strip() for i in range(day_start, day_end)):
        return lines, 0
    # Find insertion point (before before_header within day)
    ins = day_end
    for i in range(day_start, day_end):
        if lines[i].strip() == before_header.strip():
            ins = i; break
    block = [section_header]
    for sub in subsections:
        block += [sub, '- ', '']
    lines[ins:ins] = block
    return lines, len(block)

# Work on the day chunk
day_end = find_next_h2(lines, day_idx)
added_total = 0

# Ensure ### 🔗 Bugs Touched sub-sections exist
bugs_section_exists = any(lines[i].strip() == '### 🔗 Bugs Touched' for i in range(day_idx, day_end))
if not bugs_section_exists:
    # Add the whole Bugs Touched section before Phab or Thoughts
    anchor = next((h for h in ['### Phab Revisions Touched', '### 🧠 Thoughts', '### 🧠 Thought']
                   if any(lines[i].strip() == h for i in range(day_idx, day_end))), None)
    ins = day_end
    if anchor:
        for i in range(day_idx, day_end):
            if lines[i].strip() == anchor: ins = i; break
    block = ['### 🔗 Bugs Touched', '#### Bugs Modified or Created', '- ', '',
             '#### Bugs viewed', '- ', '', '#### Triaged', '- ', '']
    lines[ins:ins] = block
    day_end = find_next_h2(lines, day_idx)

bugs_modified_exists = any(lines[i].strip() == '#### Bugs Modified or Created' for i in range(day_idx, day_end))
if not bugs_modified_exists:
    for i in range(day_idx, day_end):
        if lines[i].strip() == '### 🔗 Bugs Touched':
            end = i + 1
            while end < day_end and not (lines[end].startswith('####') or lines[end].startswith('###') or lines[end].startswith('## ')):
                end += 1
            block = ['#### Bugs Modified or Created', '- ', '', '#### Bugs viewed', '- ', '']
            lines[end:end] = block
            day_end = find_next_h2(lines, day_idx)
            break

triaged_exists = any(lines[i].strip() == '#### Triaged' for i in range(day_idx, day_end))
if not triaged_exists and bz_triaged:
    for i in range(day_idx, day_end):
        if lines[i].strip() == '#### Bugs viewed':
            end = i + 1
            while end < day_end and not (lines[end].startswith('####') or lines[end].startswith('###') or lines[end].startswith('## ')):
                end += 1
            lines[end:end] = ['#### Triaged', '- ', '']
            day_end = find_next_h2(lines, day_idx)
            break

# Ensure ### Phab Revisions Touched exists
phab_section_exists = any(lines[i].strip() == '### Phab Revisions Touched' for i in range(day_idx, day_end))
if not phab_section_exists:
    thoughts = next((h for h in ['### 🧠 Thoughts', '### 🧠 Thought']
                     if any(lines[i].strip() == h for i in range(day_idx, day_end))), None)
    ins = day_end
    if thoughts:
        for i in range(day_idx, day_end):
            if lines[i].strip() == thoughts: ins = i; break
    block = ['### Phab Revisions Touched',
             '#### Phab Posted/Updated', '- ', '',
             '#### Phab Reviewed', '- ', '',
             '#### Phab Viewed', '- ', '']
    lines[ins:ins] = block
    day_end = find_next_h2(lines, day_idx)

# Ensure Phab subsections exist even when ### Phab Revisions Touched is already present
# (the day template defines the parent header but not the #### subsections)
phab_posted_exists = any(lines[i].strip() == '#### Phab Posted/Updated' for i in range(day_idx, day_end))
if not phab_posted_exists:
    for i in range(day_idx, day_end):
        if lines[i].strip() == '### Phab Revisions Touched':
            end = i + 1
            while end < day_end and not (lines[end].startswith('####') or lines[end].startswith('###') or lines[end].startswith('## ')):
                end += 1
            block = ['#### Phab Posted/Updated', '- ', '',
                     '#### Phab Reviewed', '- ', '',
                     '#### Phab Viewed', '- ', '']
            lines[end:end] = block
            day_end = find_next_h2(lines, day_idx)
            break

# Insert content
lines, n = insert_items_into_section(lines, '#### Bugs Modified or Created', bz_interacted, day_idx, day_end)
added_total += n; day_end = find_next_h2(lines, day_idx)

if bz_triaged:
    lines, n = insert_items_into_section(lines, '#### Triaged', bz_triaged, day_idx, day_end)
    added_total += n; day_end = find_next_h2(lines, day_idx)

lines, n = insert_items_into_section(lines, '#### Phab Posted/Updated', ph_submitted, day_idx, day_end)
added_total += n; day_end = find_next_h2(lines, day_idx)

lines, n = insert_items_into_section(lines, '#### Phab Reviewed', ph_reviewed, day_idx, day_end)
added_total += n

log.info(f"Added {added_total} new items to {os.path.basename(week_file)}")

# ── 7. Write file ──────────────────────────────────────────────────────────────
new_content = '\n'.join(lines)
if DRY_RUN:
    log.info("DRY RUN — not writing file.")
    # Print the day section for review
    day_end2 = find_next_h2(lines, day_idx)
    print('\n'.join(lines[day_idx:day_end2]))
else:
    with open(week_file, 'w') as f:
        f.write(new_content)
    log.info(f"Written: {week_file}")

# Clean up temp db
if tmp_db and os.path.exists(tmp_db):
    os.unlink(tmp_db)

log.info("Done.")
