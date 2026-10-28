// End-to-end walkthrough in headless Chromium over the DevTools protocol (no dependencies).
//   node tests/e2e.mjs [url]   -> screenshots in tests/out/shots, exits non-zero on page errors or failed checks
// Only exercises read-only and local-only features: it never approves, merges, comments, or posts.
import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";

const URL = process.argv[2] || "http://127.0.0.1:8766/";
const OUT = new globalThis.URL("./out/shots/", import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });
const browser = process.env.CHROME || "chromium-browser";
const chrome = spawn(browser, ["--headless=new", "--remote-debugging-port=9334", "--window-size=1600,1000",
  `--user-data-dir=${OUT}/../profile`, "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));

let ws;
for (let i = 0; i < 60 && !ws; i++) {
  await sleep(250);
  try {
    const page = (await (await fetch("http://127.0.0.1:9334/json")).json()).find(t => t.type === "page");
    if (page) ws = new WebSocket(page.webSocketDebuggerUrl);
  } catch {}
}
await new Promise(r => ws.addEventListener("open", r));
let id = 0; const pending = {}, errors = [], failures = [], rawErrors = [];
ws.addEventListener("message", e => {
  const m = JSON.parse(e.data);
  if (m.id && pending[m.id]) { pending[m.id](m); delete pending[m.id]; }
  if (m.method === "Runtime.exceptionThrown" || (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type))) rawErrors.push(m.params);
  // Alpine rejects a promise when an x-show transition is interrupted (e.g. j/k mid-animation); harmless
  const ex = m.params?.exceptionDetails;
  const cancelled = ex?.exception?.preview?.properties?.some(x => x.name === "isFromCancelledTransition")
    || ex?.stackTrace?.callFrames?.some(f => f.functionName.includes("_x_toggleAndCascadeWithTransitions"));
  if (m.method === "Runtime.exceptionThrown" && !cancelled) errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) console.log("     page:", String(m.params.args[0]?.value ?? "").split("\n")[0]);
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) errors.push(m.params.args.map(a => a.value ?? a.preview?.properties?.map(p => `${p.name}=${p.value}`).join(",") ?? a.description).join(" "));
  if (m.method === "Page.javascriptDialogOpening") { errors.push("native dialog opened: " + m.params.message); cdp("Page.handleJavaScriptDialog", { accept: false }); }
});
const cdp = (method, params = {}) => new Promise(r => { const i = ++id; pending[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
const js = async expr => {
  const r = (await cdp("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result;
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result?.value;
};
const app = expr => js(`(() => { const a = Alpine.$data(document.body); return ${expr}; })()`);
const shot = async name => writeFileSync(`${OUT}/${name}.png`, Buffer.from((await cdp("Page.captureScreenshot", { format: "png" })).result.data, "base64"));
const key = (k, opts = {}) => js(`document.dispatchEvent(new KeyboardEvent("keydown", { key: ${JSON.stringify(k)}, bubbles: true, ...${JSON.stringify(opts)} }))`);
const waitFor = async (expr, ms = 60000) => { for (let t = 0; t < ms; t += 200) { try { if (await js(expr)) return true; } catch {} await sleep(200); } return false; };
const check = (name, ok, detail = "") => { console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? "  " + detail : ""}`); if (!ok) failures.push(name); };
const time = async (label, fn) => { const t = performance.now(); await fn(); const ms = Math.round(performance.now() - t); console.log(`     ${label}: ${ms}ms`); return ms; };

await cdp("Page.enable"); await cdp("Runtime.enable");
await cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: process.env.THEME || "dark" }] });
const login = execFileSync("python3", ["-m", "prdash", "url", "--port", new globalThis.URL(URL).port], {
  cwd: new globalThis.URL("..", import.meta.url).pathname, encoding: "utf8" }).trim(); // same PRDASH_HOME as the server
await cdp("Page.navigate", { url: login });
check("loads data", await waitFor(`window.Alpine && Alpine.$data(document.body).data?.prs`));
await js(`localStorage.clear()`);
await app(`(a.panel = null, a.scope = "mine", a.preset = "open", a.layout = "board", a.q = "", a.showHidden = false)`);
await sleep(300);
const cols = await app(`a.board.map(c => c.label + " " + c.prs.length).join(" | ")`);
check("board renders columns", (await js(`document.querySelectorAll(".col:not(:has(.skel)) .col-h").length`)) >= 4, cols);
await shot("01-board-mine");

// scope switching + render cost of the largest view
const teamMs = await time("render team board", async () => { await key("3"); await js(`new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`); });
check("team view renders fast", teamMs < 400, `${await app("a.list.length")} PRs`);
await shot("02-board-team");

// advanced filters
for (const [q, expect] of [["updated:<3d", n => n >= 0], ["reviewer:@me", n => n >= 0], ["size:>500 -is:draft", n => n >= 0], ["author:zzz-nobody", n => n === 0]]) {
  await app(`a.q = ${JSON.stringify(q)}`); await sleep(150);
  const n = await app("a.list.length");
  check(`filter ${q}`, expect(n), `${n} match`);
}
const someone = await app(`a.people.find(p => p !== a.data.me)`);
await app(`(a.q = "", a.builderOpen = true)`); await sleep(100);
await app(`(a.setTerm("reviewer", ${JSON.stringify(someone)}), a.setTerm("updated", "<1m"))`); await sleep(150);
check("builder writes query", (await app("a.q")) === `reviewer:${someone} updated:<1m`, await app("a.q"));
await shot("03-filter-builder");
await app(`a.saveFilter("Waiting on ${someone}")`); await sleep(100);
check("saved filter chip", (await js(`[...document.querySelectorAll(".fchip.saved")].length`)) === 1);
await app(`(a.q = "", a.builderOpen = false, a.dropFilter("Waiting on ${someone}"))`);

// keyboard navigation + drawer
await key("1"); await sleep(200);
await key("j"); await key("j");
const focused = await app("a.focus");
check("j moves focus", !!focused);
await key("Enter"); await sleep(200);
check("Enter opens drawer", (await app("a.sel")) === focused);
check("detail loads checks + git", await waitFor(`Alpine.$data(document.body).detail.checks || Alpine.$data(document.body).detail.checks_error`, 20000));
await sleep(300);
await shot("04-drawer");
await key("j"); await sleep(200);
check("j in drawer walks queue", (await app("a.sel")) !== focused);

// local fields: waiting, snooze, tags (restored afterwards)
const url = await app("a.sel");
await app(`a.setLocal(a.cur, "waiting", "QA")`); await app(`a.setLocal(a.cur, "tags", "e2e-test")`); await sleep(300);
await app(`a.sel = null`); await sleep(200);
await app(`a.q = "waiting:qa tag:e2e"`); await sleep(150);
check("waiting + tag filterable", (await app("a.list.length")) === 1);
await shot("05-waiting-filter");
await app(`a.q = ""`);
await app(`a.snooze({ url: ${JSON.stringify(url)} }, 1)`); await sleep(300);
check("snooze hides PR", !(await app(`a.list.some(p => p.url === ${JSON.stringify(url)})`)));
await app(`Promise.all(["waiting", "tags", "snooze"].map(f => a.setLocal({ url: ${JSON.stringify(url)} }, f, null)))`); await sleep(300);
check("cleanup restores PR", await app(`a.list.some(p => p.url === ${JSON.stringify(url)})`));

// selection + batch bar (local-only action)
await app(`a.focus = a.order[0].url`); await key("x"); await key("j"); await key("x"); await sleep(150);
check("x selects", (await app("a.pickedUrls.length")) === 2);
await shot("06-batch");
await key("Escape"); await sleep(100);
check("esc clears selection", (await app("a.pickedUrls.length")) === 0);

// palette
await key("k", { ctrlKey: true }); await sleep(150);
await js(`(() => { const i = document.querySelector(".pal-in input"); i.value = "table"; i.dispatchEvent(new Event("input")); })()`); await sleep(150);
await shot("07-palette");
await key("Enter"); await sleep(200);
check("palette runs command", (await app("a.layout")) === "table");
await shot("08-table");
await app(`a.layout = "board"`);

// panels
await key("i"); await sleep(400); await shot("09-inbox"); check("inbox opens", (await app("a.panel")) === "inbox"); await key("Escape");
await key(","); check("settings load", await waitFor(`Alpine.$data(document.body).settings?.fields?.length > 10`, 5000)); await sleep(200); await shot("10-settings");
await app(`a.settingsTab = "Local"`); await sleep(150); await shot("11-settings-local"); await key("Escape");
await key("u"); await sleep(200); check("standup has text", (await app("a.standupText.length")) > 20); await shot("12-standup"); await key("Escape");
await key("?"); await sleep(200); await shot("13-help"); await key("Escape");

// logo goes home: clears filters, panels, and selection
await app(`(a.scope = "team", a.q = "ci:failing", a.preset = "all", a.builderOpen = true)`); await sleep(150);
await js(`document.querySelector("h1 .home").click()`); await sleep(200);
check("logo returns home", await app(`a.scope === "mine" && a.q === "" && a.preset === "open" && !a.builderOpen && !a.sel`));

// phone width
await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true }); await sleep(400);
check("no sideways scroll on phones", !(await js(`document.documentElement.scrollWidth > innerWidth`)));
await shot("14-mobile");

writeFileSync(`${OUT}/../errors.json`, JSON.stringify(rawErrors, null, 1));
check("no page errors", errors.length === 0, errors.slice(0, 5).join("\n"));
ws.close(); chrome.kill();
process.exit(failures.length ? 1 : 0);
