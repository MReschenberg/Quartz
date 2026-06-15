/**
 * A11y Bugs Dashboard — sensitivity analyses (round-2 review follow-up)
 * ====================================================================
 * Answers the two questions the second technical review asked us to settle with
 * real data BEFORE committing the severity mapping and TTC labeling:
 *
 *   1. SEVERITY mapping: does `major` behave like S2 or S3? We show per-raw-value
 *      time-to-close so the legacy→S mapping isn't a blind guess, plus how the S2
 *      and S3 buckets shift under `major→S2` vs `major→S3`.
 *   2. REOPENED-TTC: does excluding re-resolved bugs move the aging numbers? If
 *      median/mean barely move, the "keep cf_last_resolved" decision is safe.
 *
 * Also quantifies the GRAVEYARD/engine population split (we're dropping graveyard
 * products from P0 trends).
 *
 * RUN (Node 18+):
 *   cd "<.../dashboarding>"
 *   npx -y tsx ingest/sensitivity.ts
 *   SAMPLE_SIZE=400 npx -y tsx ingest/sensitivity.ts   # bigger reopened-TTC sample
 */

const BMO = "https://bugzilla.mozilla.org/rest";
const UA = "a11y-bugs-dashboard-audit/0.1 (mreschenberg@mozilla.com)";
const PAGE = 500, MAX_PAGES = 200;
const SAMPLE_SIZE = Number(process.env.SAMPLE_SIZE ?? 300);
const FIELDS = ["id", "creation_time", "cf_last_resolved", "status", "resolution", "severity", "product", "component"].join(",");

interface Bug {
  id: number; creation_time: string; cf_last_resolved?: string | null;
  status: string; resolution: string; severity?: string | null; product: string; component: string;
}
interface HistoryEntry { when: string; changes: { field_name: string; added: string; removed: string }[] }

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const days = (a: string, b: string) => (new Date(b).getTime() - new Date(a).getTime()) / 86_400_000;

async function bmoGet(path: string, params: Record<string, string> = {}): Promise<any> {
  const url = new URL(`${BMO}${path}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  for (let attempt = 0; attempt < 5; attempt++) {
    const res = await fetch(url, { headers: { "User-Agent": UA, Accept: "application/json" } });
    if (res.ok) return res.json();
    if (res.status === 429 || res.status >= 500) { await sleep(1000 * 2 ** attempt); continue; }
    throw new Error(`BMO ${res.status} for ${url}`);
  }
  throw new Error(`failed: ${url}`);
}

async function fetchAll(): Promise<Bug[]> {
  const byId = new Map<number, Bug>();
  for (let p = 0; p < MAX_PAGES; p++) {
    const json = await bmoGet("/bug", { keywords: "access", keywords_type: "allwords", include_fields: FIELDS, order: "bug_id", limit: String(PAGE), offset: String(p * PAGE) });
    const bugs: Bug[] = json.bugs ?? [];
    for (const b of bugs) byId.set(b.id, b);
    process.stderr.write(`  fetched ${byId.size}…\r`);
    if (bugs.length < PAGE) break;
  }
  process.stderr.write("\n");
  return [...byId.values()];
}

function stats(vals: number[]) {
  if (!vals.length) return { n: 0, median: 0, mean: 0, max: 0 };
  const s = [...vals].sort((a, b) => a - b), mid = Math.floor(s.length / 2);
  return {
    n: s.length,
    median: s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2,
    mean: s.reduce((a, v) => a + v, 0) / s.length,
    max: s[s.length - 1],
  };
}
const isGraveyard = (b: Bug) => /graveyard/i.test(b.product);

// Best-effort legacy→S map; `major` target is the variable under test.
function mapSeverity(raw: string | null | undefined, majorTo: "S2" | "S3"): string {
  const s = (raw || "").toLowerCase();
  if (s === "s1" || s === "blocker") return "S1";
  if (s === "s2" || s === "critical") return "S2";
  if (s === "major") return majorTo;
  if (s === "s3" || s === "normal") return "S3";
  if (s === "s4" || s === "minor" || s === "trivial") return "S4";
  return "unknown";
}

function sample<T>(arr: T[], n: number): T[] {
  if (arr.length <= n) return arr;
  const step = arr.length / n;
  return Array.from({ length: n }, (_, i) => arr[Math.floor(i * step)]);
}

async function main() {
  console.error("Fetching population…");
  const all = await fetchAll();
  const fixed = all.filter((b) => b.resolution === "FIXED" && b.cf_last_resolved);
  const ttcOf = (b: Bug) => days(b.creation_time, b.cf_last_resolved!);

  const L: string[] = [];
  const row = (label: string, vals: number[]) => {
    const s = stats(vals);
    return `| ${label} | ${s.n} | ${s.median.toFixed(0)}d | ${s.mean.toFixed(0)}d | ${s.max.toFixed(0)}d |`;
  };

  L.push(`# Sensitivity analyses`);
  L.push(`> source: \`${BMO}/bug?keywords=access\` · ${all.length} bugs, ${fixed.length} FIXED w/ dates · public only`);
  L.push("");

  // ---- 1a. TTC per RAW severity (FIXED, all-time): is `major` like S2 or S3? ----
  L.push(`## 1a. Time-to-close by RAW severity (FIXED, all-time)`);
  L.push(`Shows where each legacy value actually sits — the empirical basis for the mapping.`);
  L.push("");
  L.push(`| raw severity | n | median | mean | max |`);
  L.push(`|---|---:|---:|---:|---:|`);
  for (const v of ["S1", "S2", "S3", "S4", "blocker", "critical", "major", "normal", "minor", "trivial", "--", "N/A"]) {
    L.push(row(v, fixed.filter((b) => (b.severity || "") === v).map(ttcOf)));
  }
  L.push("");
  L.push(`**Read this for the \`major\` decision:** compare \`major\`'s median/mean against native \`S2\` and \`S3\`.`);
  L.push("");

  // ---- 1b. How S2 / S3 buckets shift under major→S2 vs major→S3 ----
  L.push(`## 1b. S2 / S3 buckets under \`major→S2\` vs \`major→S3\``);
  L.push("");
  for (const majorTo of ["S2", "S3"] as const) {
    const bucket = (name: string) => fixed.filter((b) => mapSeverity(b.severity, majorTo) === name);
    L.push(`**mapping \`major→${majorTo}\`** (FIXED TTC)`);
    L.push(`| mapped bucket | n | median | mean | max |`);
    L.push(`|---|---:|---:|---:|---:|`);
    L.push(row("S2", bucket("S2").map(ttcOf)));
    L.push(row("S3", bucket("S3").map(ttcOf)));
    // full-population counts (not just FIXED)
    const cntS2 = all.filter((b) => mapSeverity(b.severity, majorTo) === "S2").length;
    const cntS3 = all.filter((b) => mapSeverity(b.severity, majorTo) === "S3").length;
    L.push(`| _all-bug counts_ | S2=${cntS2} · S3=${cntS3} | | | |`);
    L.push("");
  }

  // ---- 1c. graveyard / engine split ----
  const grave = all.filter(isGraveyard);
  const engine = all.filter((b) => b.product === "Core" && /Disability Access/i.test(b.component));
  L.push(`## 1c. Population split (we're dropping graveyards in P0)`);
  L.push(`- **Graveyard products:** ${grave.length} / ${all.length} (${((grave.length / all.length) * 100).toFixed(1)}%) — excluded from P0 trends.`);
  L.push(`- **A11y engine (\`Core :: Disability Access*\`):** ${engine.length} / ${all.length} (${((engine.length / all.length) * 100).toFixed(1)}%) — kept, but flagged.`);
  L.push(`- **Active, non-engine (the "product accessibility" story):** ${all.length - grave.length - engine.length}.`);
  const fixedActive = fixed.filter((b) => !isGraveyard(b));
  L.push(`- FIXED TTC excluding graveyards: ${(() => { const s = stats(fixedActive.map(ttcOf)); return `n=${s.n} median=${s.median.toFixed(0)}d mean=${s.mean.toFixed(0)}d`; })()}`);
  L.push("");

  // ---- 2. reopened-TTC sensitivity (sampled, needs history) ----
  L.push(`## 2. Reopened-TTC sensitivity (sample of FIXED bugs)`);
  const picked = sample(fixed, SAMPLE_SIZE);
  const ttcReResolved: number[] = [], ttcClean: number[] = [];
  for (let i = 0; i < picked.length; i++) {
    const b = picked[i];
    process.stderr.write(`  history ${i + 1}/${picked.length} (bug ${b.id})…\r`);
    let hist: HistoryEntry[] = [];
    try { hist = (await bmoGet(`/bug/${b.id}/history`)).bugs?.[0]?.history ?? []; } catch { /* skip */ }
    let resCount = 0;
    for (const e of hist) for (const c of e.changes) if (c.field_name === "resolution" && c.added) resCount++;
    (resCount > 1 ? ttcReResolved : ttcClean).push(ttcOf(b));
    await sleep(90);
  }
  process.stderr.write("\n");
  L.push("");
  L.push(`| group | n | median | mean | max |`);
  L.push(`|---|---:|---:|---:|---:|`);
  L.push(row("all sampled FIXED", [...ttcClean, ...ttcReResolved]));
  L.push(row("never re-resolved", ttcClean));
  L.push(row("re-resolved (≥2×)", ttcReResolved));
  L.push("");
  const sAll = stats([...ttcClean, ...ttcReResolved]), sClean = stats(ttcClean);
  L.push(`**Shift from excluding re-resolved bugs:** median ${sAll.median.toFixed(0)}d → ${sClean.median.toFixed(0)}d · mean ${sAll.mean.toFixed(0)}d → ${sClean.mean.toFixed(0)}d.`);
  L.push(`_Small shift ⇒ the "keep cf_last_resolved / latest resolution" decision is safe._`);

  const report = L.join("\n");
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const url = await import("node:url");
  const out = path.join(path.dirname(url.fileURLToPath(import.meta.url)), "sensitivity-report.md");
  await fs.writeFile(out, report, "utf8");

  // console summary
  const majS2 = stats(fixed.filter((b) => (b.severity || "") === "major").map(ttcOf));
  const natS2 = stats(fixed.filter((b) => (b.severity || "") === "S2").map(ttcOf));
  const natS3 = stats(fixed.filter((b) => (b.severity || "") === "S3").map(ttcOf));
  console.log("\n" + "=".repeat(60));
  console.log(`major TTC median=${majS2.median.toFixed(0)}d mean=${majS2.mean.toFixed(0)}d (n=${majS2.n})`);
  console.log(`  vs S2 median=${natS2.median.toFixed(0)}d mean=${natS2.mean.toFixed(0)}d | S3 median=${natS3.median.toFixed(0)}d mean=${natS3.mean.toFixed(0)}d`);
  console.log(`graveyards=${grave.length}, engine=${engine.length}, active-non-engine=${all.length - grave.length - engine.length}`);
  console.log(`reopened-TTC: all median=${sAll.median.toFixed(0)}d → clean median=${sClean.median.toFixed(0)}d; mean ${sAll.mean.toFixed(0)}→${sClean.mean.toFixed(0)}`);
  console.log(`\nFull report → ${out}`);
  console.log("=".repeat(60));
}

main().catch((e) => { console.error("\nFailed:", e); process.exit(1); });
