// Regenerates the README screenshots from demo mode (fictional data only).
//   PRDASH_HOME=/tmp/demo python3 -m prdash --demo --no-open --port 8770 &   PRDASH_HOME=/tmp/demo node tests/screenshots.mjs
import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";

const URL = process.env.URL || "http://127.0.0.1:8770/";
const OUT = new globalThis.URL("../docs/", import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });
const chrome = spawn(process.env.CHROME || "chromium-browser", ["--headless=new", "--remote-debugging-port=9338", "--hide-scrollbars",
  "--user-data-dir=" + new globalThis.URL("./out/shots-profile", import.meta.url).pathname, "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws;
for (let i = 0; i < 60 && !ws; i++) {
  await sleep(250);
  try { const p = (await (await fetch("http://127.0.0.1:9338/json")).json()).find(t => t.type === "page"); if (p) ws = new WebSocket(p.webSocketDebuggerUrl); } catch {}
}
await new Promise(r => ws.addEventListener("open", r));
let id = 0; const pending = {};
ws.addEventListener("message", e => { const m = JSON.parse(e.data); if (m.id && pending[m.id]) { pending[m.id](m); delete pending[m.id]; } });
const cdp = (method, params = {}) => new Promise(r => { const i = ++id; pending[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
const js = async e => (await cdp("Runtime.evaluate", { expression: e, awaitPromise: true, returnByValue: true })).result.result?.value;
const app = e => js(`(() => { const a = Alpine.$data(document.body); return (${e}); })()`);
const shot = async name => { await sleep(500); writeFileSync(`${OUT}/${name}.png`, Buffer.from((await cdp("Page.captureScreenshot", { format: "png" })).result.data, "base64")); console.log("docs/" + name + ".png"); };
const theme = t => cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: t }] });

await cdp("Page.enable"); await cdp("Runtime.enable");
await cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 2, mobile: false });
await theme("dark");
const login = execFileSync("python3", ["-m", "prdash", "url", "--port", new globalThis.URL(URL).port], {
  cwd: new globalThis.URL("..", import.meta.url).pathname, encoding: "utf8" }).trim(); // same PRDASH_HOME as the server
await cdp("Page.navigate", { url: login });
for (let i = 0; i < 100 && !(await js("!!window.Alpine && !!Alpine.$data(document.body).data")); i++) await sleep(200);
await js("localStorage.clear()");
const reset = (extra = "") => app(`(a.panel = null, a.sel = null, a.builderOpen = false, a.picked = {}, a.q = "", a.preset = "open", a.layout = "board", a.scope = "mine"${extra})`);

await reset(); await shot("board-dark");
await theme("light"); await shot("board-light"); await theme("dark");
await reset(`, a.sel = a.data.views.mine.find(u => a.data.reviews[u])`); await shot("pr-panel");
await reset(`, a.scope = "team", a.builderOpen = true, a.q = "reviewer:maya-chen updated:<2w -is:draft"`); await shot("filters");
await reset(`, a.scope = "team", a.layout = "table", a.preset = "all"`); await shot("table");
await reset(`, a.focus = a.data.views.mine[0]`); await app(`(a.openPalette(), a.palQ = "claude")`); await shot("palette");
await reset(); await app("a.openInbox()"); await shot("inbox");
await reset(`, a.panel = "setup", a.setup = { me: "octo-dev", orgs: ["acme", "acme-labs", "oss-tools"], picked: { acme: true, "acme-labs": true },
  dirs: [{ path: "~/code", repos: 23, on: true }, { path: "~/work/acme", repos: 11, on: true }, { path: "~/Documents/experiments", repos: 4, on: false }],
  terminal: "kitty --directory {dir}", editor: "{terminal} nvim .", claude: true }`); await shot("setup");

await reset(); await app("a.openStandup()"); await sleep(300);
await app(`a.loadStandup("this_week")`); await sleep(600);
await app("a.summarizeStandup()"); await sleep(600); await shot("standup");

// docs/urgent.gif: step the CSS animations frame by frame (paused Web Animations), so the GIF is smooth and exact
await reset();
await cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(400);
const box = await js(`(() => {
  const u = document.querySelector(".card.urgent").getBoundingClientRect(), b = document.querySelector(".col.s-needs_work .card").getBoundingClientRect();
  return { x: Math.round(b.left) - 16, y: Math.round(Math.min(u.top, b.top)) - 56, width: Math.round(u.right - b.left) + 44, height: Math.round(Math.max(u.bottom, b.bottom) - Math.min(u.top, b.top)) + 74 };
})()`);
const FRAMES = new globalThis.URL("./out/gif-frames/", import.meta.url).pathname;
rmSync(FRAMES, { recursive: true, force: true }); mkdirSync(FRAMES, { recursive: true });
for (let i = 0, t = 0; t < 4500; i++, t += 50) {
  await js(`document.getAnimations().forEach(a => { a.pause(); a.currentTime = ${t}; })`);
  const r = await cdp("Page.captureScreenshot", { format: "png", clip: { ...box, scale: 1 } });
  writeFileSync(`${FRAMES}/${String(i).padStart(3, "0")}.png`, Buffer.from(r.result.data, "base64"));
}
execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", "20", "-i", `${FRAMES}/%03d.png`, "-vf",
  "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
  "-loop", "0", `${OUT}/urgent.gif`]);
console.log("docs/urgent.gif");

ws.close(); chrome.kill(); process.exit(0);
