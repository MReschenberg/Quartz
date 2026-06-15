#!/opt/homebrew/bin/python3
"""
Restructure Zoom meeting assets in the Obsidian vault into one folder per
meeting: Work/<year>/Qn/Week n/<topic> (YYYY-MM-DD)/{summary.md, transcript.md}.

- Moves each existing summary file (matched by its embedded Source doc id) into
  its meeting folder as summary.md.
- For each meeting that still has an accessible transcript doc on the hub
  (the createFromMeeting doc), places transcript.md in the folder:
    * if a transcript was already downloaded by the notes cron (a "# Transcript"
      file in the same week), that file is moved/renamed in (matched by week +
      meeting-name token overlap, sanity-checked by speaker overlap);
    * otherwise the transcript is fetched fresh from the hub.

Enumeration uses the same meeting APIs as zoom_summaries_backfill.py. Idempotent.

Usage: python3 zoom_restructure.py [--year 2026] [--dry-run]
"""
import os, re, glob, json, argparse, logging, sqlite3, shutil, time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from playwright.sync_api import sync_playwright

VAULT   = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents")
LOG_FILE= os.path.join(VAULT, "Scripts", "zoom_restructure.log")
MANIFEST= os.path.join(VAULT, "Scripts", "zoom_restructure.manifest.json")
PACIFIC = ZoneInfo("America/Los_Angeles")
FF_DB   = os.path.expanduser("~/Library/Application Support/Firefox/Profiles/6gqnyzas.default-nightly/cookies.sqlite")
BASE    = "https://us01docs.zoom.us"
LIST_API= BASE + "/api/meeting/list_my_meetings"
ASSETS_API = BASE + "/api/hub/files/batch_get_meeting_assets"
DOC_URL = "https://hub.zoom.us/doc/{id}"
SOURCE_RE = re.compile(r"Source: https://hub\.zoom\.us/doc/([A-Za-z0-9_-]+)")
STOP = {"the","and","1x1","11","1-1","11","meeting","with","for","sync","discussion","call","partial","check","checkin","in"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger(__name__)


# ── auth + enumeration ──────────────────────────────────────────────────────────

def load_cookies():
    tmp="/tmp/ff_cookies_restructure.sqlite"; shutil.copy(FF_DB,tmp)
    con=sqlite3.connect(tmp); rows=con.execute("SELECT host,path,name,value,expiry,isSecure,isHttpOnly,sameSite FROM moz_cookies WHERE host LIKE '%zoom%'").fetchall(); con.close()
    SS={0:"None",1:"Lax",2:"Strict"}
    return [{"name":n,"value":v,"domain":h,"path":p,"expires":(e//1000) if e>32503680000 else (e if e>0 else -1),"httpOnly":bool(ho),"secure":bool(s),"sameSite":SS.get(ss,"None")} for h,p,n,v,e,s,ho,ss in rows]

def msdt(dt): return int(dt.timestamp()*1000)

def capture_auth(ctx):
    cap={}; page=ctx.new_page()
    page.on("request", lambda r: cap.update(r.headers) if ("list_my_meetings" in r.url and r.method=="POST" and not cap) else None)
    try: page.goto("https://hub.zoom.us/meetings", wait_until="domcontentloaded", timeout=30000)
    except Exception: pass
    for i in range(60):
        if cap: break
        if i%4==0:
            try: page.mouse.wheel(0,3000)
            except Exception: pass
        time.sleep(0.5)
    page.close()
    if not cap: raise RuntimeError("could not capture auth headers")
    keep=("authorization","x-zm-device-tracking-id","x-zm-cluster-id","x-requested-with","x-zm-docs-container","accept","origin","referer")
    h={k:cap[k] for k in keep if k in cap}; h["content-type"]="application/json"; return h

def enumerate_assets(ctx, hdr, year):
    start=msdt(datetime(year,1,1,tzinfo=PACIFIC)); end=min(msdt(datetime(year,12,31,23,59,tzinfo=PACIFIC)), msdt(datetime.now(PACIFIC))); W=28*24*3600*1000
    meetings=[]; seen=set(); ws=start
    while ws<end:
        we=min(ws+W,end); pn=0
        while True:
            r=ctx.request.post(LIST_API,data=json.dumps({"startTime":ws,"endTime":we,"pageNum":pn,"pageSize":50,"meetingFilter":0,"filters":{},"query":""}),headers=hdr)
            if not r.ok: break
            j=r.json()
            for m in j.get("meetings",[]):
                if m["meetingId"] not in seen: seen.add(m["meetingId"]); meetings.append(m)
            if not j.get("hasNext"): break
            pn+=1
            if pn>40: break
        ws=we
    by=defaultdict(list)
    for m in meetings: by[m["meetingAccountId"]].append(m["meetingId"])
    assets={}
    for acct,ids in by.items():
        for i in range(0,len(ids),20):
            r=ctx.request.post(ASSETS_API,data=json.dumps({"meetingIds":ids[i:i+20],"meetingAccountId":acct}),headers=hdr)
            if r.ok: assets.update(r.json().get("meetingAssetsMap",{}))
    out=[]
    for m in meetings:
        a=assets.get(m["meetingId"])
        if not a: continue
        sd=a.get("summaryDoc") or {}; sdoc=sd.get("doc") or {}
        sid=sdoc.get("id") if sd.get("accessStatus")==0 else None
        tid=None
        for item in (a.get("createFromMeeting") or []):
            doc=item.get("doc") or {}
            if doc.get("id") and doc["id"]!=sdoc.get("id") and item.get("accessStatus")==0:
                tid=doc["id"]; break
        if not sid and not tid: continue
        d=datetime.fromtimestamp(int(m["startTime"])/1000,PACIFIC).date()
        names=[(pp.get("displayName") or pp.get("name") or pp.get("email") or "") for pp in (m.get("participants") or [])]
        out.append({"date":d,"week":d.isocalendar()[1],"topic":m["topic"],
                    "summaryId":sid,"transcriptId":tid,"participants":names})
    out.sort(key=lambda r:(r["date"],r["topic"]))
    return out


# ── vault helpers ───────────────────────────────────────────────────────────────

def base_name(topic):
    name=topic.replace("/","-").replace(":","-"); name=re.sub(r'[\\*?"<>|]',"",name)
    return re.sub(r"\s+"," ",name).strip()

def find_week_file(d):
    iso_year,wn,_=d.isocalendar()
    fn=f"{iso_year}-W{wn:02d}.md"
    matches=glob.glob(os.path.join(VAULT,"Work",str(iso_year),"**",fn),recursive=True)
    return matches[0] if matches else None

def meetings_dir_for(d, layout="auto"):
    """Layout-aware 'Meetings' dir: weekly vault -> Week n/Meetings; quarter/daily
    vault (no week file, e.g. 2025) -> Qn/Meetings. layout='quarter' forces Qn/Meetings."""
    wf=find_week_file(d) if layout != "quarter" else None
    if wf:
        _,wn,_=d.isocalendar()
        folder=os.path.dirname(wf)
        if os.path.basename(folder)!=f"Week {wn}":
            folder=os.path.join(folder,f"Week {wn}")
        return os.path.join(folder,"Meetings")
    return os.path.join(VAULT,"Work",str(d.year),f"Q{(d.month-1)//3+1}","Meetings")

def scan_summaries(year):
    out={}
    for p in glob.glob(os.path.join(VAULT,"Work",str(year),"**","*.md"),recursive=True):
        try:
            with open(p) as f: head=f.read(600)
        except Exception: continue
        m=SOURCE_RE.search(head)
        if m: out[m.group(1)]=p
    return out

def scan_cron_transcripts(year):
    out=[]
    for p in glob.glob(os.path.join(VAULT,"Work",str(year),"**","*.md"),recursive=True):
        try:
            with open(p) as f: head=f.read(40)
        except Exception: continue
        if head.startswith("# Transcript"):
            out.append(p)
    return out

def tokens(s):
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP and len(t)>=2}

def week_of_path(p):
    m=re.search(r"/Week (\d+)/", p)
    return int(m.group(1)) if m else None

def speakers_in(path):
    try:
        with open(path) as f: txt=f.read()
    except Exception: return set()
    names=set()
    for m in re.finditer(r"\*\*([^*]+?)\*\*", txt):
        nm=m.group(1).strip()
        first=re.split(r"[\s(]", nm)[0].lower()
        if first: names.add(first)
    return names


# ── transcript extraction (from zoom_notes_scraper.py) ──────────────────────────

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

def fetch_transcript(page, doc_id):
    page.goto(DOC_URL.format(id=doc_id), wait_until="networkidle", timeout=45000)
    try: page.wait_for_selector('span[role="img"][aria-label]', timeout=15000)
    except Exception: pass
    page.evaluate("""async () => { const s=ms=>new Promise(r=>setTimeout(r,ms)); let prev=-1;
        for(let i=0;i<40;i++){ window.scrollTo(0,document.body.scrollHeight);
        document.querySelectorAll('*').forEach(e=>{if(e.scrollHeight>e.clientHeight+200)e.scrollTop=e.scrollHeight;});
        await s(500); if(document.body.scrollHeight===prev)break; prev=document.body.scrollHeight; } }""")
    time.sleep(1)
    return page.evaluate(EXTRACT_TRANSCRIPT_JS)


# ── main ────────────────────────────────────────────────────────────────────────

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--year",type=int,default=date.today().year)
    ap.add_argument("--layout",choices=["auto","quarter"],default="auto",
                    help="auto: Week n/Meetings where a week file exists, else Qn/Meetings; quarter: always Qn/Meetings")
    ap.add_argument("--no-headless",action="store_true")
    ap.add_argument("--dry-run",action="store_true")
    args=ap.parse_args()

    summaries=scan_summaries(args.year)
    cron=scan_cron_transcripts(args.year)
    log.info(f"{len(summaries)} summary files, {len(cron)} cron transcript files on disk")

    with sync_playwright() as p:
        browser=p.firefox.launch(headless=not args.no_headless)
        ctx=browser.new_context(viewport={"width":1440,"height":1000}); ctx.add_cookies(load_cookies())
        hdr=capture_auth(ctx)
        meetings=enumerate_assets(ctx,hdr,args.year)
        log.info(f"{len(meetings)} meetings with summary and/or transcript")

        # assign a per-meeting folder. If a summary for this meeting is already on
        # disk, co-locate the transcript in that same folder; otherwise compute the
        # layout-aware Meetings/<topic> (date)/ folder.
        used=set()
        for m in meetings:
            if m["summaryId"] and m["summaryId"] in summaries:
                sp=summaries[m["summaryId"]]
                m["summary_src"]=sp
                m["folder"]=os.path.dirname(sp)
            else:
                m["summary_src"]=None
                mdir=meetings_dir_for(m["date"], layout=args.layout)
                base=f"{base_name(m['topic'])} ({m['date'].isoformat()})"
                folder=os.path.join(mdir,base); n=1
                while folder in used: n+=1; folder=os.path.join(mdir,f"{base} ({n})")
                m["folder"]=folder
            used.add(m["folder"])

        # match cron transcript files -> meetings (same week, token overlap, speaker sanity)
        tmeet=[m for m in meetings if m["transcriptId"]]
        cron_by_week=defaultdict(list)
        for c in cron: cron_by_week[week_of_path(c)].append(c)
        match={}   # cron path -> meeting
        for wk, files in cron_by_week.items():
            cands=[m for m in tmeet if m["week"]==wk]
            for c in files:
                ctok=tokens(os.path.basename(c)[:-3]); csp=speakers_in(c)
                best=None; bestscore=(-1,-1)
                for m in cands:
                    if m.get("_claimed"): continue
                    mtok=tokens(m["topic"]); psp={re.split(r"[\s(]",p)[0].lower() for p in m["participants"] if p}
                    score=(len(ctok & mtok), len(csp & psp))
                    if score>bestscore: bestscore=score; best=m
                if best and bestscore[0]>=1:
                    best["_claimed"]=True; match[c]=best
                else:
                    match[c]=None

        # ── execute ──
        manifest={"summary_moves":[],"transcript_moves":[],"transcript_fetches":[],"unmatched_cron":[],"errors":[]}
        page=ctx.new_page()

        # Phase A: summaries -> folder/summary.md
        handled=set()
        for m in meetings:
            if not m.get("summary_src"): continue
            handled.add(os.path.abspath(m["summary_src"]))
            dst=os.path.join(m["folder"],"summary.md")
            if os.path.abspath(m["summary_src"])==os.path.abspath(dst): continue
            if args.dry_run:
                log.info(f"[dry] summary: {os.path.relpath(m['summary_src'],VAULT)} -> {os.path.relpath(dst,VAULT)}")
            else:
                os.makedirs(m["folder"],exist_ok=True); shutil.move(m["summary_src"],dst)
            manifest["summary_moves"].append({"to":os.path.relpath(dst,VAULT)})

        # Phase A2: fold any leftover flat summary file (e.g. summaries not in
        # list_my_meetings) sitting directly in a Week folder.
        for p in summaries.values():
            if os.path.abspath(p) in handled: continue
            parent=os.path.dirname(p)
            if not re.match(r"Week \d+$", os.path.basename(parent)): continue
            if os.path.basename(p)=="summary.md": continue
            stem=os.path.basename(p)[:-3]; folder=os.path.join(parent,stem)
            dst=os.path.join(folder,"summary.md")
            if args.dry_run:
                log.info(f"[dry] summary(leftover): {os.path.relpath(p,VAULT)} -> {os.path.relpath(dst,VAULT)}")
            else:
                os.makedirs(folder,exist_ok=True); shutil.move(p,dst)
            manifest["summary_moves"].append({"to":os.path.relpath(dst,VAULT)})

        # Phase B: transcripts
        cron_for={id(m):c for c,m in match.items() if m}
        for m in tmeet:
            dst=os.path.join(m["folder"],"transcript.md")
            cronfile=next((c for c,mm in match.items() if mm is m),None)
            if os.path.exists(dst) and not args.dry_run:
                continue
            if cronfile:
                if args.dry_run:
                    log.info(f"[dry] transcript MOVE: {os.path.relpath(cronfile,VAULT)} -> {os.path.relpath(dst,VAULT)}")
                else:
                    os.makedirs(m["folder"],exist_ok=True); shutil.move(cronfile,dst)
                manifest["transcript_moves"].append({"from":os.path.relpath(cronfile,VAULT),"to":os.path.relpath(dst,VAULT)})
            else:
                if args.dry_run:
                    log.info(f"[dry] transcript FETCH: {m['topic']} [{m['date']}] -> {os.path.relpath(dst,VAULT)}")
                    manifest["transcript_fetches"].append({"to":os.path.relpath(dst,VAULT)})
                else:
                    try:
                        content=fetch_transcript(page, m["transcriptId"])
                        if not content: raise RuntimeError("empty transcript")
                        os.makedirs(m["folder"],exist_ok=True)
                        with open(dst,"w") as f: f.write(content)
                        log.info(f"FETCHED transcript: {os.path.relpath(dst,VAULT)}")
                        manifest["transcript_fetches"].append({"to":os.path.relpath(dst,VAULT)})
                    except Exception as e:
                        log.error(f"transcript fetch failed {m['topic']} [{m['date']}]: {e}")
                        manifest["errors"].append({"topic":m["topic"],"date":str(m["date"]),"error":str(e)})

        for c,mm in match.items():
            if mm is None:
                manifest["unmatched_cron"].append(os.path.relpath(c,VAULT))
        manifest["_debug_matches"]=[{"cron":os.path.basename(c),
                                     "meeting":(f"{mm['topic']} [{mm['date']}]" if mm else None)}
                                    for c,mm in match.items()]
        manifest["_debug_counts"]={"cron":len(cron),"tmeet":len(tmeet)}
        browser.close()

    json.dump(manifest,open(MANIFEST,"w"),indent=2)
    print("\n================ RESTRUCTURE PLAN/RESULT ================")
    print(f"summary moves:      {len(manifest['summary_moves'])}")
    print(f"transcript moves:   {len(manifest['transcript_moves'])}")
    print(f"transcript fetches: {len(manifest['transcript_fetches'])}")
    print(f"unmatched cron:     {len(manifest['unmatched_cron'])}  {manifest['unmatched_cron']}")
    print(f"errors:             {len(manifest['errors'])}")
    print("\n--- transcript MOVES (cron file -> folder) ---")
    for t in manifest["transcript_moves"]:
        print(f"  {t['from']}\n     -> {t['to']}")
    print("\n--- transcript FETCHES ---")
    for t in manifest["transcript_fetches"]:
        print(f"  {t['to']}")
    print(f"\nmanifest: {MANIFEST}")

if __name__=="__main__":
    main()
