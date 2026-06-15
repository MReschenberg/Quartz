/**
 * A11y Bugs Dashboard — reopen transition analysis (follow-up to audit.ts)
 * ========================================================================
 * The Step 0 audit found ~28% of access bugs are resolved more than once.
 * This script answers: *what do those reopens turn into?* It reconstructs each
 * sampled bug's ordered RESOLUTION sequence from its Bugzilla history and reports:
 *   - how many times bugs are resolved (2×, 3×, 4+×),
 *   - the most common full resolution sequences (e.g. FIXED → FIXED, FIXED → DUPLICATE),
 *   - a first-resolution → final-resolution cross-tab,
 *   - among re-resolved bugs, what share ultimately land on FIXED.
 *
 * It samples CLOSED bugs (those with a final resolution) since that's where the
 * cf_last_resolved / time-to-close distortion lives.
 *
 * RUN (Node 18+):
 *   cd "<.../dashboarding>"
 *   npx -y tsx ingest/reopen-analysis.ts                 # default sample 300 closed bugs
 *   SAMPLE_SIZE=500 npx -y tsx ingest/reopen-analysis.ts
 */

const BMO = "https://bugzilla.mozilla.org/rest";
const UA = "a11y-bugs-dashboard-audit/0.1 (mreschenberg@mozilla.com)";
const PAGE = 500;
const MAX_PAGES = 200;
const SAMPLE_SIZE = Number(process.env.SAMPLE_SIZE ?? 300);
const FIELDS = ["id", "creation_time", "cf_last_resolved", "status", "resolution"].join(",");

interface Bug { id: number; status: string; resolution: string }
interface HistoryChange { field_name: string; added: string; removed: string }
interface HistoryEntry { when: string; changes: HistoryChange[] }

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function bmoGet(path: string, params: Record<string, string> = {}): Promise<any> {
  const url = new URL(`${BMO}${path}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  for (let attempt = 0; attempt < 5; attempt++) {
    const res = await fetch(url, { headers: { "User-Agent": UA, Accept: "application/json" } });
    if (res.ok) return res.json();
    if (res.status === 429 || res.status >= 500) { await sleep(1000 * 2 ** attempt); continue; }
    throw new Error(`BMO ${res.status} ${res.statusText} for ${url}`);
  }
  throw new Error(`BMO request failed after retries: ${url}`);
}

async function fetchAllAccessBugs(): Promise<Bug[]> {
  const byId = new Map<number, Bug>();
  for (let page = 0; page < MAX_PAGES; page++) {
    const json = await bmoGet("/bug", {
      keywords: "access", keywords_type: "allwords",
      include_fields: FIELDS, order: "bug_id",
      limit: String(PAGE), offset: String(page * PAGE),
    });
    const bugs: Bug[] = json.bugs ?? [];
    for (const b of bugs) byId.set(b.id, b);
    process.stderr.write(`  fetched ${byId.size} bugs…\r`);
    if (bugs.length < PAGE) break;
  }
  process.stderr.write("\n");
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

function sample<T>(arr: T[], n: number): T[] {
  if (arr.length <= n) return arr;
  const step = arr.length / n;
  return Array.from({ length: n }, (_, i) => arr[Math.floor(i * step)]);
}

// Ordered list of resolutions applied over the bug's life, from history.
// Each `resolution` change with a non-empty `added` is one (re-)resolution.
function resolutionSequence(hist: HistoryEntry[], current: Bug): string[] {
  const seq: string[] = [];
  for (const entry of hist) {
    for (const ch of entry.changes) {
      if (ch.field_name === "resolution" && ch.added) seq.push(ch.added);
    }
  }
  // Fallback: resolution set at creation with no recorded change.
  if (seq.length === 0 && current.resolution) seq.push(current.resolution);
  return seq;
}

function tally(items: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const it of items) out[it] = (out[it] ?? 0) + 1;
  return out;
}
const sorted = (rec: Record<string, number>) => Object.entries(rec).sort((a, b) => b[1] - a[1]);
function bar(n: number, total: number, w = 20) {
  const f = total ? Math.round((n / total) * w) : 0;
  return "█".repeat(f) + "·".repeat(w - f);
}

async function main() {
  console.error("Fetching access population…");
  const all = await fetchAllAccessBugs();
  const closed = all.filter((b) => b.resolution); // has a final resolution
  const picked = sample(closed, SAMPLE_SIZE);
  console.error(`${all.length} total, ${closed.length} closed; sampling ${picked.length} closed bugs for history…`);

  const seqLenDist: string[] = [];          // "1","2","3","4+"
  const fullPatterns: string[] = [];        // "FIXED → DUPLICATE"
  const firstFinal: string[] = [];          // "FIXED ⇒ DUPLICATE"
  let reResolved = 0;
  const reResolvedFinal: string[] = [];     // final resolution among re-resolved
  let firstFixedHeld = 0;                   // first FIXED and final FIXED
  let firstFixedBroke = 0;                  // first FIXED but final ≠ FIXED
  let analyzed = 0;

  for (let i = 0; i < picked.length; i++) {
    const b = picked[i];
    process.stderr.write(`  history ${i + 1}/${picked.length} (bug ${b.id})…\r`);
    let hist: HistoryEntry[];
    try { hist = (await bmoGet(`/bug/${b.id}/history`)).bugs?.[0]?.history ?? []; }
    catch (e) { console.error(`\n  history failed ${b.id}: ${e}`); continue; }
    analyzed++;

    const seq = resolutionSequence(hist, b);
    const len = seq.length;
    seqLenDist.push(len >= 4 ? "4+" : String(len));
    if (len >= 2) {
      reResolved++;
      fullPatterns.push(seq.join(" → "));
      firstFinal.push(`${seq[0]} ⇒ ${seq[len - 1]}`);
      reResolvedFinal.push(seq[len - 1]);
      if (seq[0] === "FIXED") (seq[len - 1] === "FIXED" ? firstFixedHeld++ : firstFixedBroke++);
    }
    await sleep(90);
  }
  process.stderr.write("\n");

  const lines: string[] = [];
  const pct = (n: number, d: number) => `${((n / d) * 100).toFixed(1)}%`;
  lines.push(`# Reopen transition analysis`);
  lines.push("");
  lines.push(`> source: \`${BMO}/bug?keywords=access\` · sample of ${analyzed} closed bugs · public only`);
  lines.push("");
  lines.push(`## How many times are bugs resolved?`);
  lines.push("");
  const lenT = tally(seqLenDist);
  lines.push(`| # resolutions | bugs | % | |`);
  lines.push(`|---|---:|---:|---|`);
  for (const [k, v] of sorted(lenT)) lines.push(`| ${k}× | ${v} | ${pct(v, analyzed)} | ${bar(v, analyzed)} |`);
  lines.push("");
  lines.push(`**Re-resolved (≥2×): ${reResolved} / ${analyzed} (${pct(reResolved, analyzed)})**`);
  lines.push("");

  lines.push(`## Most common full resolution sequences (re-resolved bugs)`);
  lines.push("");
  lines.push(`| sequence | count | % of re-resolved | |`);
  lines.push(`|---|---:|---:|---|`);
  for (const [k, v] of sorted(tally(fullPatterns)).slice(0, 15))
    lines.push(`| ${k} | ${v} | ${pct(v, reResolved)} | ${bar(v, reResolved)} |`);
  lines.push("");

  lines.push(`## First → final resolution (re-resolved bugs)`);
  lines.push("");
  lines.push(`| first ⇒ final | count | % of re-resolved | |`);
  lines.push(`|---|---:|---:|---|`);
  for (const [k, v] of sorted(tally(firstFinal)).slice(0, 15))
    lines.push(`| ${k} | ${v} | ${pct(v, reResolved)} | ${bar(v, reResolved)} |`);
  lines.push("");

  lines.push(`## Where re-resolved bugs ultimately land`);
  lines.push("");
  lines.push(`| final resolution | count | % of re-resolved | |`);
  lines.push(`|---|---:|---:|---|`);
  for (const [k, v] of sorted(tally(reResolvedFinal)))
    lines.push(`| ${k} | ${v} | ${pct(v, reResolved)} | ${bar(v, reResolved)} |`);
  lines.push("");
  const firstFixedTotal = firstFixedHeld + firstFixedBroke;
  lines.push(`## Did the first FIXED hold?`);
  lines.push("");
  lines.push(`Of re-resolved bugs whose **first** resolution was FIXED (${firstFixedTotal}):`);
  lines.push(`- **held** (final also FIXED): ${firstFixedHeld} (${firstFixedTotal ? pct(firstFixedHeld, firstFixedTotal) : "n/a"})`);
  lines.push(`- **broke** (final ≠ FIXED): ${firstFixedBroke} (${firstFixedTotal ? pct(firstFixedBroke, firstFixedTotal) : "n/a"})`);
  lines.push("");
  lines.push(`_Interpretation: 'held' bugs are genuine fixes that bounced (latest cf_last_resolved overstates time-to-FIRST-fix but the FIXED label is right). 'broke' bugs were marked FIXED then ended elsewhere — counting them as FIXED would be wrong._`);

  const report = lines.join("\n");
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const url = await import("node:url");
  const outPath = path.join(path.dirname(url.fileURLToPath(import.meta.url)), "reopen-analysis-report.md");
  await fs.writeFile(outPath, report, "utf8");

  console.log("\n" + "=".repeat(60));
  console.log(`Analyzed ${analyzed} closed bugs; ${reResolved} re-resolved (${pct(reResolved, analyzed)}).`);
  console.log(`Top sequences: ${sorted(tally(fullPatterns)).slice(0, 5).map(([k, v]) => `${k} (${v})`).join("; ")}`);
  console.log(`First FIXED held: ${firstFixedHeld}/${firstFixedTotal}; broke: ${firstFixedBroke}/${firstFixedTotal}`);
  console.log(`\nFull report → ${outPath}`);
  console.log("=".repeat(60));
}

main().catch((e) => { console.error("\nFailed:", e); process.exit(1); });
