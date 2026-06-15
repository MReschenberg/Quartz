/**
 * A11y Bugs Dashboard — Step 0 data audit
 * =======================================
 * A standalone, read-only probe of bugzilla.mozilla.org (BMO) that answers the
 * questions the P0 implementation plan says we must answer BEFORE writing any UI:
 *
 *   - R2  What resolution values actually occur? (finalize the closed-outcome buckets)
 *   - R3  Where does a11y severity actually live — `severity` field, `access-sN`
 *         keyword, or `whiteboard` tag?
 *   - R10 How long after a bug is created is the `access` keyword added?
 *         How often are bugs reopened (so `cf_last_resolved` ≠ first fixed)?
 *   - Data quality: how many resolved bugs are missing `cf_last_resolved` (breaks TTC)?
 *   - Context: product/component spread, creation/resolution year coverage,
 *     duplicate rate, and a quick all-time + trailing-6-month TTC sanity check.
 *
 * It writes a Markdown report to `ingest/audit-report.md` and prints a summary.
 * Nothing here is part of the shipped dashboard — it's a one-off de-risking tool.
 *
 * RUN (needs Node 18+ for global fetch; tsx runs the .ts directly):
 *   cd "<.../dashboarding>"
 *   npx -y tsx ingest/audit.ts                 # full run (samples bug history)
 *   npx -y tsx ingest/audit.ts --no-history    # fast: skip the history sampling
 *   SAMPLE_SIZE=200 npx -y tsx ingest/audit.ts # larger history sample (default 120)
 *
 * No API key is used, so security-restricted bugs are invisible (documented undercount).
 */

const BMO = "https://bugzilla.mozilla.org/rest";
const UA = "a11y-bugs-dashboard-audit/0.1 (mreschenberg@mozilla.com)";
const PAGE = 500;
const MAX_PAGES = 200; // safety cap (= 100k bugs); population is a few thousand
const SAMPLE_SIZE = Number(process.env.SAMPLE_SIZE ?? 120);
const SKIP_HISTORY = process.argv.includes("--no-history");

// Fields we need to characterize the population. We pull `severity`, `keywords`,
// and `whiteboard` together precisely so we can see which one carries a11y severity.
const FIELDS = [
  "id", "creation_time", "cf_last_resolved", "last_change_time",
  "status", "resolution", "dupe_of", "severity", "priority",
  "keywords", "whiteboard", "product", "component",
].join(",");

interface Bug {
  id: number;
  creation_time: string;
  cf_last_resolved?: string | null;
  last_change_time?: string;
  status: string;
  resolution: string; // "" when open
  dupe_of?: number | null;
  severity?: string | null;
  priority?: string | null;
  keywords: string[];
  whiteboard?: string | null;
  product: string;
  component: string;
}

interface HistoryChange { field_name: string; added: string; removed: string }
interface HistoryEntry { when: string; who?: string; changes: HistoryChange[] }

// ---------------------------------------------------------------------------
// HTTP helper with a polite UA and basic retry/backoff (handles 429/5xx).
// ---------------------------------------------------------------------------
async function bmoGet(path: string, params: Record<string, string> = {}): Promise<any> {
  const url = new URL(`${BMO}${path}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  for (let attempt = 0; attempt < 5; attempt++) {
    const res = await fetch(url, { headers: { "User-Agent": UA, Accept: "application/json" } });
    if (res.ok) return res.json();
    if (res.status === 429 || res.status >= 500) {
      const wait = 1000 * 2 ** attempt;
      console.error(`  ${res.status} from BMO, backing off ${wait}ms…`);
      await sleep(wait);
      continue;
    }
    throw new Error(`BMO ${res.status} ${res.statusText} for ${url}`);
  }
  throw new Error(`BMO request failed after retries: ${url}`);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// Fetch the full `access` population: paged, stable-sorted, de-duped.
// Mirrors the hardened pagination the plan specifies (§4.1) so the audit
// doubles as a smoke test of that approach.
// ---------------------------------------------------------------------------
async function fetchAllAccessBugs(): Promise<Bug[]> {
  const byId = new Map<number, Bug>();
  let dupes = 0;
  for (let page = 0; page < MAX_PAGES; page++) {
    const offset = page * PAGE;
    const json = await bmoGet("/bug", {
      keywords: "access",
      keywords_type: "allwords",
      include_fields: FIELDS,
      order: "bug_id",
      limit: String(PAGE),
      offset: String(offset),
    });
    const bugs: Bug[] = json.bugs ?? [];
    for (const b of bugs) {
      if (byId.has(b.id)) dupes++;
      byId.set(b.id, b);
    }
    process.stderr.write(`  fetched ${byId.size} bugs…\r`);
    if (bugs.length < PAGE) break;
  }
  process.stderr.write("\n");
  if (dupes > 0) console.error(`  ⚠ pagination returned ${dupes} duplicate IDs (would fail the real ingest validator)`);
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

async function fetchHistory(id: number): Promise<HistoryEntry[]> {
  const json = await bmoGet(`/bug/${id}/history`);
  return json.bugs?.[0]?.history ?? [];
}

// ---------------------------------------------------------------------------
// Small stats / tally utilities.
// ---------------------------------------------------------------------------
function tally<T>(items: T[], key: (t: T) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const it of items) {
    const k = key(it);
    out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}

function sortedEntries(rec: Record<string, number>): [string, number][] {
  return Object.entries(rec).sort((a, b) => b[1] - a[1]);
}

function stats(values: number[]) {
  if (values.length === 0) return { n: 0, min: 0, max: 0, mean: 0, median: 0 };
  const s = [...values].sort((a, b) => a - b);
  const sum = s.reduce((acc, v) => acc + v, 0);
  const mid = Math.floor(s.length / 2);
  const median = s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
  return { n: s.length, min: s[0], max: s[s.length - 1], mean: sum / s.length, median };
}

const days = (a: string, b: string) => (new Date(b).getTime() - new Date(a).getTime()) / 86_400_000;
const isOpen = (b: Bug) => !b.resolution;
const year = (iso?: string | null) => (iso ? new Date(iso).getUTCFullYear() : null);

// Deterministic sample (every Nth bug) so repeated runs are comparable.
function sample<T>(arr: T[], n: number): T[] {
  if (arr.length <= n) return arr;
  const step = arr.length / n;
  const out: T[] = [];
  for (let i = 0; i < n; i++) out.push(arr[Math.floor(i * step)]);
  return out;
}

// ---------------------------------------------------------------------------
// Severity-source investigation (R3): look in all three candidate locations.
// ---------------------------------------------------------------------------
const ACCESS_SN = /\baccess-s([1-4])\b/i;          // keyword form, e.g. access-s2
const WB_SN = /\[(?:access[- ]?)?s([1-4])\]/i;     // whiteboard form, e.g. [access-s2] or [s2]

function severitySources(bugs: Bug[]) {
  const sevField = tally(bugs, (b) => b.severity || "(none)");
  let kwSn = 0, wbSn = 0;
  const kwSnDist: Record<string, number> = {};
  for (const b of bugs) {
    const kw = b.keywords?.find((k) => ACCESS_SN.test(k));
    if (kw) { kwSn++; kwSnDist[kw.toLowerCase()] = (kwSnDist[kw.toLowerCase()] ?? 0) + 1; }
    if (b.whiteboard && WB_SN.test(b.whiteboard)) wbSn++;
  }
  return { sevField, kwSn, kwSnDist, wbSn };
}

// ---------------------------------------------------------------------------
// History sampling (R10): access-keyword-add lag + reopen rate.
// ---------------------------------------------------------------------------
async function analyzeHistory(bugs: Bug[]) {
  const picked = sample(bugs, SAMPLE_SIZE);
  const lags: number[] = [];        // days from creation to first `access` keyword add
  let setAtCreation = 0;            // `access` never appears as a later addition → present at creation
  let reResolved = 0;               // resolution applied more than once (true reopen signal)
  let analyzed = 0;
  for (let i = 0; i < picked.length; i++) {
    const b = picked[i];
    process.stderr.write(`  history ${i + 1}/${picked.length} (bug ${b.id})…\r`);
    let hist: HistoryEntry[];
    try { hist = await fetchHistory(b.id); } catch (e) { console.error(`\n  history fetch failed for ${b.id}: ${e}`); continue; }
    analyzed++;

    // First time the `access` keyword was *added* (keywords changes list added kw names).
    let firstAccessAdd: string | null = null;
    let resolutionAdds = 0;
    for (const entry of hist) {
      for (const ch of entry.changes) {
        if (ch.field_name === "keywords" && /\baccess\b/.test(ch.added) && !firstAccessAdd) {
          firstAccessAdd = entry.when;
        }
        // Count (re-)resolutions, NOT status→RESOLVED/VERIFIED: the normal QA flow
        // RESOLVED FIXED → VERIFIED FIXED would otherwise look like a reopen.
        if (ch.field_name === "resolution" && ch.added) {
          resolutionAdds++;
        }
      }
    }
    if (firstAccessAdd) lags.push(Math.max(0, days(b.creation_time, firstAccessAdd)));
    else setAtCreation++; // keyword present from creation (no later add recorded)
    if (resolutionAdds > 1) reResolved++;

    await sleep(120); // be polite to BMO
  }
  process.stderr.write("\n");
  return {
    analyzed,
    setAtCreation,
    lagStats: stats(lags),
    lagOver30d: lags.filter((d) => d > 30).length,
    lagOver180d: lags.filter((d) => d > 180).length,
    reResolved,
  };
}

// ---------------------------------------------------------------------------
// Report rendering.
// ---------------------------------------------------------------------------
function bar(n: number, total: number, width = 24): string {
  const filled = total ? Math.round((n / total) * width) : 0;
  return "█".repeat(filled) + "·".repeat(width - filled);
}

function distTable(title: string, rec: Record<string, number>, total: number, limit = 30): string {
  const rows = sortedEntries(rec).slice(0, limit)
    .map(([k, v]) => `| \`${k}\` | ${v} | ${((v / total) * 100).toFixed(1)}% | ${bar(v, total)} |`)
    .join("\n");
  return `**${title}**\n\n| value | count | % | |\n|---|---:|---:|---|\n${rows}\n`;
}

function statLine(s: ReturnType<typeof stats>, unit = "d"): string {
  return `n=${s.n} · min=${s.min.toFixed(0)}${unit} · median=${s.median.toFixed(0)}${unit} · mean=${s.mean.toFixed(0)}${unit} · max=${s.max.toFixed(0)}${unit}`;
}

async function main() {
  const startedAt = new Date().toISOString();
  console.error("Fetching the full `access` bug population from BMO…");
  const bugs = await fetchAllAccessBugs();
  const total = bugs.length;
  if (total === 0) throw new Error("No bugs returned — check the query or BMO availability.");
  console.error(`Got ${total} bugs. Analyzing…`);

  // Distributions
  const resolutionDist = tally(bugs, (b) => b.resolution || "(open)");
  const statusDist = tally(bugs, (b) => b.status);
  const productDist = tally(bugs, (b) => b.product);
  const componentDist = tally(bugs, (b) => `${b.product} :: ${b.component}`);
  const sev = severitySources(bugs);

  // Year coverage
  const createdByYear = tally(bugs, (b) => String(year(b.creation_time)));
  const resolvedByYear = tally(bugs.filter((b) => b.cf_last_resolved), (b) => String(year(b.cf_last_resolved)));

  // Data-quality: resolved bugs missing cf_last_resolved (breaks TTC)
  const closed = bugs.filter((b) => !isOpen(b));
  const closedMissingResolved = closed.filter((b) => !b.cf_last_resolved);
  const openCount = total - closed.length;

  // Duplicate signal
  const dupCount = resolutionDist["DUPLICATE"] ?? 0;
  const dupWithTarget = bugs.filter((b) => b.resolution === "DUPLICATE" && b.dupe_of).length;

  // Quick TTC sanity (FIXED bugs with both dates)
  const fixedWithDates = bugs.filter((b) => b.resolution === "FIXED" && b.cf_last_resolved);
  const ttcAll = stats(fixedWithDates.map((b) => days(b.creation_time, b.cf_last_resolved!)));
  const sixMonthsAgo = Date.now() - 182 * 86_400_000;
  const ttc6mo = stats(
    fixedWithDates.filter((b) => new Date(b.cf_last_resolved!).getTime() >= sixMonthsAgo)
      .map((b) => days(b.creation_time, b.cf_last_resolved!)),
  );

  // History sample (optional)
  const hist = SKIP_HISTORY ? null : await analyzeHistory(bugs);

  // ---- Render Markdown ----
  const lines: string[] = [];
  lines.push(`# A11y Bugs Dashboard — Step 0 Data Audit`);
  lines.push("");
  lines.push(`> Generated ${startedAt} · source: \`${BMO}/bug?keywords=access\` · public bugs only (restricted bugs excluded).`);
  lines.push("");
  lines.push(`## Headline numbers`);
  lines.push("");
  lines.push(`- **Total \`access\` bugs:** ${total}`);
  lines.push(`- **Open:** ${openCount} (${((openCount / total) * 100).toFixed(1)}%) · **Closed:** ${closed.length} (${((closed.length / total) * 100).toFixed(1)}%)`);
  lines.push(`- **Closed but missing \`cf_last_resolved\`:** ${closedMissingResolved.length} ${closedMissingResolved.length ? "⚠ these break time-to-close" : "✓"}`);
  lines.push(`- **Duplicates:** ${dupCount} (${dupWithTarget} have a \`dupe_of\` target)`);
  lines.push("");

  lines.push(`## R2 — Resolution distribution (finalize the closed-outcome buckets)`);
  lines.push("");
  lines.push(distTable("resolution", resolutionDist, total));
  lines.push(`_Any value above that the plan's §4.4 buckets don't name should be added (or mapped to \`other_closed\`)._`);
  lines.push("");
  lines.push(distTable("status", statusDist, total));
  lines.push("");

  lines.push(`## R3 — Where does a11y severity live?`);
  lines.push("");
  lines.push(`- **\`severity\` field:** distribution below.`);
  lines.push(`- **\`access-sN\` keyword:** present on **${sev.kwSn}** / ${total} bugs (${((sev.kwSn / total) * 100).toFixed(1)}%).`);
  lines.push(`- **\`[sN]\`-style whiteboard tag:** present on **${sev.wbSn}** / ${total} bugs (${((sev.wbSn / total) * 100).toFixed(1)}%).`);
  lines.push("");
  lines.push(distTable("severity field", sev.sevField, total));
  if (Object.keys(sev.kwSnDist).length) {
    lines.push("");
    lines.push(distTable("access-sN keyword", sev.kwSnDist, total));
  }
  lines.push(`_Pick the source with the best coverage as the severity dimension in \`classify.ts\`._`);
  lines.push("");

  lines.push(`## TTC sanity check (FIXED bugs, days from created → resolved)`);
  lines.push("");
  lines.push(`- **All-time:** ${statLine(ttcAll)}`);
  lines.push(`- **Resolved in trailing ~6 months (FR-2 window):** ${statLine(ttc6mo)}`);
  lines.push("");

  if (hist) {
    lines.push(`## R10 — \`access\` keyword lag & reopen rate (history sample, n=${hist.analyzed})`);
    lines.push("");
    lines.push(`- **Keyword present at creation:** ${hist.setAtCreation} / ${hist.analyzed} (${((hist.setAtCreation / hist.analyzed) * 100).toFixed(1)}%)`);
    lines.push(`- **Added later — lag (days):** ${statLine(hist.lagStats)}`);
    lines.push(`- **Added > 30 days after creation:** ${hist.lagOver30d} · **> 180 days:** ${hist.lagOver180d}`);
    lines.push(`- **Re-resolved (resolution applied >1× — true reopen signal):** ${hist.reResolved} / ${hist.analyzed} (${((hist.reResolved / hist.analyzed) * 100).toFixed(1)}%)`);
    lines.push("");
    lines.push(`_If lag is large, "filed = created" misstates when bugs entered the a11y population (consider the history API)._`);
    lines.push(`_If reopen rate is high, \`cf_last_resolved\` ≠ first-fixed and time-to-close is distorted._`);
    lines.push("");
  } else {
    lines.push(`## R10 — skipped (\`--no-history\`)`);
    lines.push("");
    lines.push(`Re-run without \`--no-history\` to measure keyword-add lag and reopen rate.`);
    lines.push("");
  }

  lines.push(`## Coverage`);
  lines.push("");
  lines.push(distTable("bugs created by year", createdByYear, total));
  lines.push("");
  lines.push(distTable("bugs resolved by year", resolvedByYear, closed.length || total));
  lines.push("");
  lines.push(distTable("top products", productDist, total, 15));
  lines.push("");
  lines.push(distTable("top components", componentDist, total, 20));
  lines.push("");

  lines.push(`## What this resolves`);
  lines.push("");
  lines.push(`- **R2:** finalize §4.4 buckets from the resolution distribution above.`);
  lines.push(`- **R3:** set the severity dimension to whichever source has real coverage.`);
  lines.push(`- **R10:** decide whether to invest in the bug-history API or keep "created"/"latest resolved" labels.`);
  lines.push(`- **Data quality:** the missing-\`cf_last_resolved\` count tells us how many closed bugs we can't age.`);

  const report = lines.join("\n");

  // Write next to this script.
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const url = await import("node:url");
  const here = path.dirname(url.fileURLToPath(import.meta.url));
  const outPath = path.join(here, "audit-report.md");
  await fs.writeFile(outPath, report, "utf8");

  // Console summary.
  console.log("\n" + "=".repeat(60));
  console.log(`Audit complete: ${total} access bugs (${openCount} open, ${closed.length} closed).`);
  console.log(`Resolutions: ${sortedEntries(resolutionDist).map(([k, v]) => `${k}=${v}`).join(", ")}`);
  console.log(`Severity coverage — field:${total - (sev.sevField["(none)"] ?? 0)}/${total}, access-sN kw:${sev.kwSn}/${total}, whiteboard:${sev.wbSn}/${total}`);
  console.log(`FIXED TTC (all-time): ${statLine(ttcAll)}`);
  if (closedMissingResolved.length) console.log(`⚠ ${closedMissingResolved.length} closed bugs lack cf_last_resolved.`);
  console.log(`\nFull report → ${outPath}`);
  console.log("=".repeat(60));
}

main().catch((e) => {
  console.error("\nAudit failed:", e);
  process.exit(1);
});
