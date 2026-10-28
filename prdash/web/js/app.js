// prdash page. One Alpine component; data comes from the local server (/api/state) and live updates over SSE.
import Alpine from "../vendor/alpine.esm.js";
import { compile, getTerm, setTerm, SYNTAX } from "./filter.js";
import { card, md, row } from "./render.js";

const DAY = 864e5;

const STATUSES = [
  { key: "draft", label: "Draft", hint: "Not ready for eyes yet" },
  { key: "needs_work", label: "Needs work", hint: "CI failing, changes requested, or conflicts" },
  { key: "in_review", label: "In review", hint: "Waiting on reviewers" },
  { key: "approved", label: "Approved", hint: "Ready to merge" },
  { key: "merged", label: "Merged", hint: "Recently merged" },
];
const PRESETS = {
  open: { label: "Open", statuses: ["draft", "needs_work", "in_review", "approved"] },
  unfinished: { label: "Unfinished", statuses: ["draft", "needs_work"] },
  complete: { label: "Complete", statuses: ["approved", "merged"] },
  all: { label: "All", statuses: null },
};
const SCOPES = { mine: "Mine", review: "Review requested", team: "Team" };
// Swatch previews for the theme picker: [label, background, card, accent, text]. Full palettes live in style.css.
const THEMES = {
  system: ["System", "linear-gradient(135deg, #0b0d10 50%, #f5f6f8 50%)", "#151920", "#7aa2f7", "#e3e6eb"],
  dark: ["Midnight", "#0b0d10", "#151920", "#7aa2f7", "#e3e6eb"], light: ["Daylight", "#f5f6f8", "#ffffff", "#3b6fe0", "#161a20"],
  nord: ["Nord", "#242933", "#3b4252", "#88c0d0", "#eceff4"], dracula: ["Dracula", "#1e1f29", "#282a36", "#bd93f9", "#f8f8f2"],
  tokyo: ["Tokyo Night", "#16161e", "#1f2335", "#7aa2f7", "#c0caf5"], catppuccin: ["Catppuccin", "#11111b", "#1e1e2e", "#89b4fa", "#cdd6f4"],
  gruvbox: ["Gruvbox", "#1d2021", "#32302f", "#83a598", "#ebdbb2"], solarized: ["Solarized", "#eee8d5", "#fdf6e3", "#268bd2", "#073642"],
  rosepine: ["Rosé Pine Dawn", "#f2e9e1", "#fffaf3", "#286983", "#575279"],
};
const AI_PRESETS = {
  DeepSeek: ["https://api.deepseek.com", "deepseek-chat"], Grok: ["https://api.x.ai/v1", "grok-4"],
  OpenAI: ["https://api.openai.com/v1", "gpt-5-mini"], Gemini: ["https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"],
  OpenRouter: ["https://openrouter.ai/api/v1", "deepseek/deepseek-chat"], Ollama: ["http://localhost:11434/v1", ""],
};
const RANK = {
  ci: { FAILURE: 0, ERROR: 0, PENDING: 1, EXPECTED: 1, SUCCESS: 2 },
  review: { CHANGES_REQUESTED: 0, REVIEW_REQUIRED: 1, APPROVED: 2 },
  status: Object.fromEntries(STATUSES.map((s, i) => [s.key, i])),
};
// Table columns: key -> [label, sort fn(pr, local)]. Add one here plus a <td> in index.html.
const COLUMNS = {
  prio: ["P", (p, l) => l.prio || 0], repo: ["Repo", p => p.repo], title: ["Pull request", p => p.title.toLowerCase()],
  status: ["Status", p => RANK.status[p.status]], author: ["Author", p => p.author], ci: ["CI", p => RANK.ci[p.ci] ?? 3],
  review: ["Review", p => RANK.review[p.review] ?? 3], size: ["Size", p => p.adds + p.dels],
  created: ["Age", p => p.created], updated: ["Updated", p => p.updated], note: ["Note", (p, l) => l.note || ""],
};
const PERSISTED = ["scope", "preset", "layout", "q", "sort", "dir", "showHidden", "heat"];
// age colors: an open PR's days since it opened -> 0 (fresh) to 4 (two weeks or more)
const HEAT = [[1, "under a day"], [3, "1 to 3 days"], [7, "3 to 7 days"], [14, "1 to 2 weeks"], [Infinity, "2 weeks or more"]];
const EVENT_ICON = {
  review_requested: "eye", review: "message-square", comment: "message-square", ci_failed: "triangle-alert", ci_passed: "check",
  merged: "git-merge", closed: "x", conflict: "triangle-alert", snooze: "clock", review_done: "sparkles", review_error: "triangle-alert",
  jira: "ticket", jira_error: "triangle-alert", bump: "alarm-clock", urgent: "siren", mention: "at-sign", pushed: "git-commit-horizontal",
  retrying: "repeat", retry_ok: "circle-check", retry_failed: "triangle-alert",
};
const BUILDER = [ // filter builder rows: [key, label, options or "people"/"repos"]
  ["author", "Author", "people"], ["reviewer", "Reviewer", "people"], ["requested", "Review requested from", "people"],
  ["approved-by", "Approved by", "people"], ["repo", "Repo", "repos"],
  ["updated", "Updated", [["<1d", "today"], ["<3d", "last 3 days"], ["<1w", "last week"], ["<2w", "last 2 weeks"], ["<1m", "last month"], [">1w", "over a week ago"], [">1m", "over a month ago"]]],
  ["created", "Opened", [["<1d", "today"], ["<1w", "last week"], ["<1m", "last month"], [">2w", "over 2 weeks ago"], [">1m", "over a month ago"], [">3m", "over 3 months ago"]]],
  ["size", "Size", [["xs", "XS (<10 lines)"], ["s", "S (<100)"], ["m", "M (<500)"], ["l", "L (<1000)"], ["xl", "XL (1000+)"]]],
  ["ci", "CI", [["failing", "Failing"], ["pending", "Pending"], ["passing", "Passing"], ["none", "No checks"]]],
  ["review", "Review", [["needed", "Needed"], ["changes", "Changes requested"], ["approved", "Approved"], ["none", "None"]]],
  ["is", "Is", [["urgent", "Urgent"], ["bumped", "Bumped"], ["pushed", "New commits since changes requested"], ["mentioned", "Mentions you"],
                ["retrying", "Retrying until green"], ["following", "Following"], ["muted", "Muted"], ["draft", "Draft"], ["stale", "Stale"],
                ["conflict", "Conflicting"], ["posted", "Posted to Slack"], ["snoozed", "Snoozed"]]],
  ["has", "Has", [["threads", "Unresolved threads"], ["jira", "Ticket"], ["note", "Note"], ["tags", "Tags"], ["waiting", "Waiting on"], ["review", "AI review"]]],
];

const CSRF = document.querySelector('meta[name="csrf"]').content;

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json", "X-Prdash": CSRF }, // required on writes, see server.authed()
    body: body && JSON.stringify(body),
  });
  if (r.status === 403) throw new Error("Signed out. Run `prdash` to open a fresh session.");
  const j = await r.json();
  if (!r.ok) throw new Error(j.error);
  return j;
}
// Derived lists are cached until an input changes: Alpine getters recompute on every read, and the board
// reads them many times per render. Kept outside Alpine so the cache itself isn't reactive.
const MEMO = new Map();
function memo(name, deps, fn) {
  const hit = MEMO.get(name);
  if (hit && hit.deps.length === deps.length && hit.deps.every((d, i) => d === deps[i])) return hit.value;
  const value = fn();
  MEMO.set(name, { deps, value });
  return value;
}
const COL_LIMIT = 40;
// A column's x-html depends on both its x-for scope and `rev` (cards read local data). Alpine runs an effect once
// per flush, so after a sync the column could render before x-for handed it the new column, then miss the refresh.
// Keying columns by board generation gives every recomputed board fresh column elements.
let BOARD_GEN = 0;
// PR data lives outside Alpine's reactivity (proxying 250 PRs made every read during filtering and rendering
// pay for dependency tracking). Alpine watches `rev` instead; __v_skip keeps @vue/reactivity from wrapping it.
const raw = o => Object.defineProperty(o, "__v_skip", { value: true });
let STORE = null, LOCAL = raw({}), LAST = null;
const plusDays = n => new Date(Date.now() + n * DAY).toISOString().slice(0, 10);

Alpine.data("prdash", () => ({
  STATUSES, PRESETS, SCOPES, COLUMNS, SYNTAX, BUILDER, EVENT_ICON, THEMES, AI_PRESETS, HEAT,
  scope: "mine", preset: "open", layout: "board", q: "", sort: "updated", dir: -1, showHidden: false, heat: false,
  replyFor: null, replyBody: "", inboxFilter: "all", insights: null, insightsView: "mine",
  rev: 0, cfg: {}, status: { syncing: false, at: null, error: null }, now: Date.now(), sel: null, focus: null, picked: {}, detail: {}, detailFor: null,
  cbody: "", convoAll: false, armed: null, busy: {}, toastMsg: "", toastErr: false, panel: null, palQ: "", palI: 0, settings: null,
  settingsTab: "Appearance", events: [],
  standup: { window: "yesterday", text: "", summary: "", view: "details", loading: false, summarizing: false }, builderOpen: false, saved: [], more: {}, setup: null,
  notifyPermission: "Notification" in window ? Notification.permission : "unsupported", naming: null, tagging: null, lastPicked: null,

  init() {
    try { Object.assign(this, JSON.parse(localStorage.prdash || "{}")); } catch {}
    try { this.saved = JSON.parse(localStorage.prdashFilters || "[]"); } catch {}
    PERSISTED.forEach(k => this.$watch(k, () => {
      try { localStorage.prdash = JSON.stringify(Object.fromEntries(PERSISTED.map(k => [k, this[k]]))); } catch {}
    }));
    this.$watch("sel", url => { this.convoAll = false; url && this.loadDetail(url); });
    this.load();
    this.connect();
    setInterval(() => (this.now = Date.now()), 30_000);
    document.addEventListener("keydown", e => this.key(e));
  },

  // ---- data
  async load() {
    try {
      const d = await api("/api/state");
      LOCAL = raw(d.local); delete d.local;
      STORE = raw(d); this.cfg = d.config; this.rev++;
      this.status = { syncing: d.syncing, at: d.at, error: d.error };
      if (!d.config.setup_done && !d.config.demo && !this.setup) this.openSetup();
    } catch (e) { this.toast(e.message, true); }
  },
  connect() {
    const es = new EventSource("/api/stream");
    es.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.type === "syncing") this.status.syncing = true;
      if (m.type === "sync") {
        this.status = { syncing: false, at: m.at, error: m.error };
        if (!m.changed) return; // nothing moved: skip the refetch and re-render
        this.load();
        if (m.events.length) this.notify(m.events);
        if (this.sel) this.loadDetail(this.sel);
      }
      if (m.type === "review" || m.type === "inbox") { this.load(); if (this.panel === "inbox") this.openInbox(); }
    };
  },
  async loadDetail(url) {
    this.detailFor = url;
    try { const d = await api(`/api/detail?url=${encodeURIComponent(url)}`); if (this.detailFor === url) this.detail = d; }
    catch (e) { if (this.detailFor === url) this.detail = { error: e.message }; }
  },
  sync() { api("/api/sync", {}).catch(e => this.toast(e.message, true)); this.status.syncing = true; },

  // ---- derived
  get ctx() { return { L: p => this.L(p), me: this.data?.me, reviews: this.data?.reviews || {}, staleDays: this.cfg.stale_days || 5, now: this.now }; },
  get data() { return this.rev ? STORE : null; },
  get local() { return this.rev ? LOCAL : null; },
  get visible() {
    const { data, rev, scope, q, showHidden, now } = this;
    return memo("visible", [rev, scope, q, showHidden, now], () => {
      if (!data) return [];
      const ctx = this.ctx, match = compile(q), revealed = showHidden || /\bis:(hidden|snoozed)\b/.test(q);
      return (data.views[scope] || []).map(u => data.prs[u])
        .filter(p => p && (revealed || (!this.L(p).hidden && !this.L(p).snooze)) && match(p, ctx));
    });
  },
  get list() {
    const visible = this.visible, { preset, sort, dir, rev } = this;
    return memo("list", [visible, preset, sort, dir, rev], () => {
      const want = PRESETS[preset].statuses, by = COLUMNS[sort]?.[1] || COLUMNS.updated[1];
      const keyed = visible.filter(p => !want || want.includes(p.status)).map(p => [by(p, this.L(p)), p]);
      return keyed.sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0) * dir).map(([, p]) => p);
    });
  },
  get board() {
    const list = this.list, { preset, scope, rev, more, q, heat } = this;
    return memo("board", [list, preset, scope, rev, more, q, heat], () => {
      const want = PRESETS[preset].statuses, groups = {}, gen = ++BOARD_GEN;
      for (const p of list) (groups[p.status] ||= []).push(p);
      // urgent first, then your priority; stable, so the chosen sort holds within each
      const byPrio = (a, b) => (!!b.urgent - !!a.urgent) || (heat && a.created.localeCompare(b.created)) || (this.L(b).prio || 0) - (this.L(a).prio || 0);
      return STATUSES.filter(s => (!want || want.includes(s.key)) && !(s.key === "merged" && scope === "review")).map(s => {
        const all = (groups[s.key] || []).sort(byPrio);
        return { ...s, id: `${s.key}:${gen}`, total: all.length, prs: more[s.key] ? all : all.slice(0, COL_LIMIT) };
      }).filter((c, _, cols) => !this.q.trim() || c.total || cols.every(x => !x.total)); // filtering: hide empty columns
    });
  },
  get order() { return this.layout === "board" ? memo("order", [this.board], () => this.board.flatMap(c => c.prs)) : this.list; }, // j/k order
  count(preset) {
    const visible = this.visible;
    return memo("counts", [visible], () => {
      const by = {};
      for (const p of visible) by[p.status] = (by[p.status] || 0) + 1;
      return Object.fromEntries(Object.entries(PRESETS).map(([k, v]) => [k, (v.statuses || STATUSES.map(s => s.key)).reduce((n, s) => n + (by[s] || 0), 0)]));
    })[preset];
  },
  scopeCount(s) { return (this.data?.views[s] || []).filter(u => !this.data.prs[u]?.merged).length; },
  get cur() { const p = this.sel ? this.data?.prs[this.sel] : null; if (p) LAST = p; return p; },
  get pane() { return this.cur || LAST; }, // the drawer keeps its last PR while it animates closed
  get people() {
    const n = {};
    for (const p of Object.values(this.data?.prs || {})) {
      n[p.author] = (n[p.author] || 0) + 1;
      for (const r of p.reviewers) n[r.who] = (n[r.who] || 0) + 1;
    }
    return Object.keys(n).filter(x => x !== "ghost" && !x.endsWith("[bot]")).sort((a, b) => n[b] - n[a] || a.localeCompare(b));
  },
  get repos() { return [...new Set(Object.values(this.data?.prs || {}).map(p => p.repo))].sort(); },
  // open PRs by "owner/repo:branch", for stacks: a PR whose base is another open PR's branch sits on top of it
  get heads() {
    const { rev } = this;
    return memo("heads", [rev], () => {
      const m = {};
      for (const p of Object.values(this.data?.prs || {})) if (!p.merged && p.state === "OPEN" && !p.fork) m[`${p.owner}/${p.repo}:${p.branch}`] = p;
      return m;
    });
  },
  stack(p) {
    if (!p) return [];
    const below = [], above = [], heads = this.heads, all = Object.values(heads);
    for (let b = heads[`${p.owner}/${p.repo}:${p.base}`]; b && below.length < 10 && b !== p; b = heads[`${b.owner}/${b.repo}:${b.base}`]) below.unshift(b);
    const on = b => all.find(x => x.owner === b.owner && x.repo === b.repo && x.base === b.branch && x !== b);
    for (let t = p.fork ? null : on(p); t && above.length < 10; t = on(t)) above.push(t);
    return below.length || above.length ? [...below, p, ...above] : [];
  },
  // people to ask for another look: whoever reviewed without approving, on your own open PR, if not already re-asked.
  // Individuals, never the team they were requested through.
  rerequest(p) {
    if (!p || p.merged || p.state !== "OPEN" || p.author !== this.data?.me) return null;
    const asked = new Set(p.requested || []), me = this.data.me;
    const who = p.reviewers.filter(r => r.who !== me && !asked.has(r.who) && ["CHANGES_REQUESTED", "COMMENTED", "DISMISSED"].includes(r.state)).map(r => r.who);
    if (!who.length) return null;
    if (p.fixes_pushed) return { who, why: `You pushed new commits after ${who.join(", ")} requested changes.` };
    if (p.status === "in_review" && this.days(p.updated) > (this.cfg.stale_days || 5)) return { who, why: `Quiet for ${this.ago(p.updated)}.` };
    return null;
  },
  heatLevel(p) {
    if (!this.heat || p.merged || p.state !== "OPEN") return null;
    const d = this.days(p.created);
    return HEAT.findIndex(([max]) => d < max);
  },
  threads() { return (this.detail.conversation || []).filter(c => c.what === "thread"); },
  openThreads() { return this.threads().filter(t => !t.resolved).length; },
  canFix(p) {
    return !!p && p.author === this.data?.me && !p.merged && this.cfg.ai_terminal &&
      (this.openThreads() > 0 || (this.detail.conversation || []).some(c => c.what === "requested changes" && c.text));
  },
  get pickedUrls() { return Object.keys(this.picked).filter(u => this.picked[u]); },
  get unread() { return this.data?.unread || 0; },
  L(p) { return (p && this.local?.[p.url]) || {}; },
  review(p) { return (p && this.data?.reviews[p.url]) || {}; },
  jira(key) { return this.data?.jira?.[key]; },

  // ---- rendering (see render.js)
  colHTML(col) { return col.prs.map(p => card(this, p)).join(""); },
  tableHTML() { return this.list.map(p => row(this, p)).join(""); },
  paint() { // sel / focus / picked as classes: cheap, and keeps j/k from re-rendering the board
    const { sel, focus, picked } = this;
    requestAnimationFrame(() => {
      for (const el of document.querySelectorAll("main [data-url]")) {
        const u = el.dataset.url;
        el.classList.toggle("sel", u === sel); el.classList.toggle("focus", u === focus); el.classList.toggle("picked", !!picked[u]);
      }
    });
  },
  listClick(e) {
    const el = e.target.closest("[data-url]");
    if (!el || e.target.closest("a")) return; // links (Jira) behave normally
    const p = this.data?.prs[el.dataset.url];
    if (!p) return;
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "pick") return this.toggle(p, e);
    if (act === "announce") return this.announce(p);
    if (act) return this.act(act, p);
    if (e.shiftKey || this.pickedUrls.length) return this.toggle(p, e);
    this.sel = p.url; this.focus = p.url;
  },

  // ---- formatting
  days(iso) { return (this.now - new Date(iso)) / DAY; },
  ago(t) {
    const d = (this.now - new Date(t)) / DAY;
    return d < 1 / 24 ? Math.max(1, Math.round(d * 1440)) + "m" : d < 1 ? Math.round(d * 24) + "h" : Math.round(d) + "d";
  },
  tags(p) { return (this.L(p).tags || "").split(",").map(t => t.trim()).filter(Boolean); },
  statusLabel(k) { return STATUSES.find(s => s.key === k)?.label; },
  reviewLabel(p) { return { APPROVED: "Approved", CHANGES_REQUESTED: "Changes", REVIEW_REQUIRED: "Needed" }[p.review] || "—"; },
  avatar(login) {
    if (!this.cfg.demo) return `https://github.com/${encodeURIComponent(login)}.png?size=48`;
    const hue = [...login].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 0); // demo: initials, no real people
    return `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><rect width="20" height="20" fill="hsl(${hue},45%,45%)"/><text x="10" y="14" font-size="9" font-family="sans-serif" fill="white" text-anchor="middle">${login.slice(0, 2).toUpperCase()}</text></svg>`)}`;
  },
  jiraHref(k) { return this.cfg.jira_url ? `${this.cfg.jira_url}/browse/${k}` : null; },
  // [short, tone, icon, sentence]: why it's in Needs work, plus workflow nudges. The panel skips the ones its own sections show.
  flags(p, panel = false) {
    const f = [], idle = this.ago(p.updated);
    if (p.urgent && !p.merged) f.push(["Urgent", "urgent", null, "Marked urgent"]);
    if (p.bump && !p.merged) f.push([`Bumped ${this.ago(p.bump.at)}`, "amber", "alarm-clock", `${p.bump.who} bumped this ${this.ago(p.bump.at)} ago`]);
    if (p.ci === "FAILURE" || p.ci === "ERROR") f.push(["CI", "red", "triangle-alert", "CI is failing"]);
    if (p.review === "CHANGES_REQUESTED" && !p.fixes_pushed) f.push(["Changes", "red", "message-square", "Changes requested"]);
    if (p.mergeable === "CONFLICTING") f.push(["Conflicts", "red", "triangle-alert", `Merge conflicts with ${p.base}`]);
    if (p.fixes_pushed && !p.merged) f.push(["New commits", "amber", "git-commit-horizontal", "New commits since changes were requested: ready for another look"]);
    if (p.open_threads && !p.merged && !panel) f.push([`${p.open_threads} open`, "muted", "message-square", `${p.open_threads} unresolved review thread${p.open_threads > 1 ? "s" : ""}`]);
    const r = this.L(p).retry;
    if (r && !p.merged) f.push([`Retrying ${r.attempts}/${r.max}`, "amber", "repeat", `Re-running failed jobs until CI passes (${r.attempts} of ${r.max} re-runs)`]);
    const base = !p.merged && this.heads[`${p.owner}/${p.repo}:${p.base}`];
    if (base && base !== p && !panel) f.push([`on #${base.number}`, "muted", "layers", `Stacked on #${base.number} ${base.title}`]);
    if (this.L(p).muted) f.push(["Muted", "muted", "bell-off", "Muted: no inbox items for this PR"]);
    if (p.author === this.data?.me && p.status === "in_review" && !this.L(p).posted && this.days(p.created) < 3)
      f.push(["Post", "amber", "hash", "Not announced in Slack yet"]);
    if (p.status === "approved" && this.days(p.updated) > 1) f.push(["Merge?", "amber", "git-merge", `Approved but idle ${idle}: ping to merge`]);
    if (p.status === "in_review" && this.days(p.updated) > (this.cfg.stale_days || 5) && !(panel && this.rerequest(p))) f.push(["Quiet", "muted", "clock", `No activity for ${idle}: nudge reviewers`]);
    return f;
  },
  slackText(p) {
    const t = p.title.replace(/^\s*(\[[A-Z][A-Z0-9]+-\d+\]|[A-Z][A-Z0-9]+-\d+:)\s*/, "");
    const title = /^[A-Z][a-z]/.test(t) ? t[0].toLowerCase() + t.slice(1) : t;
    const vars = { title, raw_title: p.title, url: p.url, repo: p.repo, number: p.number };
    return (this.cfg.slack_template || "PR to {title}: {url}").replace(/\{(\w+)\}/g, (m, k) => vars[k] ?? m);
  },

  // ---- filters
  term(key) { return getTerm(this.q, key); },
  setTerm(key, value) { this.q = setTerm(this.q, key, value); },
  saveFilter(name) {
    name = (name || "").trim();
    this.naming = null;
    if (!name || !this.q.trim()) return;
    this.saved = [...this.saved.filter(s => s.name !== name), { name, q: this.q }];
    try { localStorage.prdashFilters = JSON.stringify(this.saved); } catch {}
  },
  dropFilter(name) {
    this.saved = this.saved.filter(s => s.name !== name);
    try { localStorage.prdashFilters = JSON.stringify(this.saved); } catch {}
  },
  sortBy(k) { this.dir = this.sort === k ? -this.dir : -1; this.sort = k; },

  // ---- mutations
  async setLocal(p, field, value) {
    try { const r = await api("/api/local", { url: p.url, fields: { [field]: value } }); LOCAL[p.url] = r.local; this.rev++; }
    catch (e) { this.toast(e.message, true); }
  },
  snooze(p, days) { this.setLocal(p, "snooze", days ? plusDays(days) : null); if (days) this.toast(`Snoozed until ${plusDays(days)}`); },
  async copy(text, msg) {
    try { await navigator.clipboard.writeText(text); this.toast(msg || "Copied"); }
    catch (e) { this.toast("Clipboard blocked: " + e.message, true); }
  },
  async announce(p) {
    if (this.cfg.slack) return this.act("slack_post", p);
    await this.copy(this.slackText(p), "Copied. Paste it in Slack");
    if (!this.L(p).posted) this.setLocal(p, "posted", new Date().toISOString());
  },
  md,
  convoDesc() { return (this.detail.conversation || []).filter(c => c.what === "opened"); },
  convoCount() { return (this.detail.conversation || []).filter(c => c.what !== "opened").length; },
  // the drawer's conversation, oldest first; earlier entries tucked behind "Show N earlier", except unresolved threads
  convo() {
    const out = (this.detail.conversation || []).filter(c => c.what !== "opened")
      .map(c => c.what === "thread" ? { type: "thread", key: c.id, t: c } : { type: "item", key: c.url + c.at, c });
    if (this.convoAll || out.length <= 4) return out;
    const keep = out.filter((g, i) => i >= out.length - 3 || (g.type === "thread" && !g.t.resolved));
    return keep.length < out.length ? [{ type: "more", key: "more", n: out.length - keep.length }, ...keep] : out;
  },
  async act(op, p, extra = {}) {
    if (["merge", "close", "approve"].includes(op) && this.armed !== op + p.url) {
      this.armed = op + p.url;
      return setTimeout(() => this.armed === op + p.url && (this.armed = null), 3000);
    }
    this.armed = null; this.busy = { ...this.busy, [op + p.url]: true };
    try {
      const r = await api("/api/action", { url: p.url, op, body: extra.body ?? this.cbody, arg: extra.arg });
      this.toast(r.out);
      if (["comment", "approve", "request_changes", "close"].includes(op)) this.cbody = "";
      if (op === "thread_reply") { this.replyFor = null; this.replyBody = ""; }
      if (op.startsWith("thread_") || op === "rerequest") this.loadDetail(p.url);
      if (["retry", "retry_stop"].includes(op)) this.load();
      if (op === "review") this.load();
      if (["comment", "approve", "request_changes"].includes(op)) this.loadDetail(p.url);
      if (["slack_post", "slack_find", "worktree", "worktree_remove", "checkout", "clone"].includes(op)) { this.load(); this.loadDetail(p.url); }
    } catch (e) { this.toast(e.message, true); }
    finally { const b = { ...this.busy }; delete b[op + p.url]; this.busy = b; }
  },
  isBusy(op, p) { return !!this.busy[op + p?.url]; },
  focusComposer() {
    const p = this.cur || this.data?.prs[this.focus];
    if (!p) return;
    this.sel = p.url;
    setTimeout(() => { const el = this.$refs.composer; el?.scrollIntoView({ block: "center" }); el?.focus(); }, 250);
  },
  startReply(t) { this.replyFor = t.id; this.replyBody = ""; this.$nextTick(() => document.getElementById("reply-" + t.id)?.focus()); },

  // ---- selection + batch
  toggle(p, e) {
    if (e?.shiftKey && this.lastPicked) { // range select in current order
      const o = this.order.map(x => x.url), [a, b] = [o.indexOf(this.lastPicked), o.indexOf(p.url)].sort((x, y) => x - y);
      if (a >= 0) { o.slice(a, b + 1).forEach(u => (this.picked[u] = true)); return; }
    }
    this.picked[p.url] = !this.picked[p.url]; this.lastPicked = p.url;
  },
  pickAll() { this.order.forEach(p => (this.picked[p.url] = true)); },
  clearPicked() { this.picked = {}; },
  async batch(op, extra = {}) {
    const urls = this.pickedUrls;
    if (!urls.length) return;
    if (["approve", "close"].includes(op) && this.armed !== "batch" + op) {
      this.armed = "batch" + op;
      return setTimeout(() => this.armed === "batch" + op && (this.armed = null), 3000);
    }
    this.armed = null;
    this.busy = { ...this.busy, batch: true };
    try {
      const r = await api("/api/batch", { urls, op, ...extra });
      const bad = r.results.filter(x => !x.ok);
      this.toast(bad.length ? `${urls.length - bad.length} done, ${bad.length} failed: ${bad[0].out}` : `${op}: ${urls.length} done`, bad.length > 0);
      await this.load();
      if (!bad.length) this.clearPicked();
    } catch (e) { this.toast(e.message, true); }
    finally { this.busy = { ...this.busy, batch: false }; }
  },
  batchLocal(fields) { return this.batch("local", { fields }); },
  async batchTag(t) {
    t = (t || "").trim(); this.tagging = null;
    if (!t) return;
    const urls = this.pickedUrls;
    await Promise.all(urls.map(u => this.setLocal({ url: u }, "tags", [...new Set([...this.tags({ url: u }), t])].join(", "))));
    this.toast(`Tagged ${urls.length} with ${t}`);
  },

  // ---- panels
  async openInbox() {
    this.panel = "inbox";
    try { this.events = (await api("/api/events")).events; } catch (e) { this.toast(e.message, true); }
  },
  async readAll() { await api("/api/events/read", {}); this.events = this.events.map(e => ({ ...e, read: 1 })); this.load(); },
  get shownEvents() {
    const f = this.inboxFilter;
    return this.events.filter(e => f === "all" || (f === "unread" && !e.read) || (f === "mentions" && e.kind === "mention"));
  },
  async deleteEvents(ids) {
    try { await api("/api/events/delete", { ids }); this.events = ids ? this.events.filter(e => !ids.includes(e.id)) : this.events.filter(e => !e.read); this.load(); }
    catch (e) { this.toast(e.message, true); }
  },
  async muteFromEvent(ev) {
    const p = this.data?.prs[ev.url];
    if (!p) return this.toast("That PR is no longer in your views.", true);
    await this.setLocal(p, "muted", true); this.toast(`Muted ${p.repo}#${p.number}. Unmute it from the PR panel.`);
  },
  // ---- insights: turnaround and flaky checks
  async openInsights(view = this.insightsView) {
    this.panel = "insights"; this.insightsView = view;
    try { const r = await api(`/api/insights?view=${view}`); if (this.insightsView === view) this.insights = r; } catch (e) { this.toast(e.message, true); }
  },
  hours(h) { return h == null ? "—" : h < 1 ? "<1h" : h < 48 ? Math.round(h) + "h" : Math.round(h / 24) + "d"; },
  prAt(url) { return this.data?.prs[url]; },
  openEvent(ev) {
    api("/api/events/read", { ids: [ev.id] }).then(() => this.load());
    ev.read = 1;
    if (ev.url && this.data?.prs[ev.url]) { this.panel = null; this.sel = ev.url; }
    else this.toast("That PR is no longer in your views.");
  },
  notify(events) {
    if (!this.cfg.notify_browser || !("Notification" in window) || Notification.permission !== "granted") return;
    events.slice(0, 3).forEach(ev => {
      const n = new Notification(ev.kind === "urgent" ? "prdash: urgent" : "prdash", { body: ev.text, tag: ev.url + ev.kind,
                                     icon: "/static/logo.svg", requireInteraction: ev.kind === "urgent" });
      n.onclick = () => { window.focus(); if (ev.url) this.sel = ev.url; };
    });
  },
  async askNotify() { const p = await Notification.requestPermission(); this.notifyPermission = p; this.toast(p === "granted" ? "Desktop notifications on" : "Notifications blocked by the browser", p !== "granted"); },
  async openSettings() {
    this.panel = "settings";
    try { this.settings = await api("/api/settings"); } catch (e) { this.toast(e.message, true); }
  },
  async saveSetting(key, value) {
    if (key === "theme") this.applyTheme(value);
    try { this.settings = await api("/api/settings", { [key]: value }); this.toast("Saved"); this.load(); }
    catch (e) { this.toast(e.message, true); }
  },
  applyTheme(theme) {
    if (!theme || theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  },
  async testSetting(what) {
    try { this.toast((await api("/api/settings/test", { what })).out); } catch (e) { this.toast(e.message, true); }
  },
  get settingsSections() { return [...new Set((this.settings?.fields || []).map(f => f.section))]; },
  // ---- standup: what changed in a window, plus an optional AI summary
  openStandup() { this.panel = "standup"; this.loadStandup(); },
  async loadStandup(window = this.standup.window) {
    Object.assign(this.standup, { window, loading: true, summary: "", view: "details" });
    try {
      const r = await api(`/api/standup?window=${window}`);
      if (this.standup.window === window) this.standup.text = r.text;
    } catch (e) { this.standup.text = ""; this.toast(e.message, true); }
    finally { if (this.standup.window === window) this.standup.loading = false; }
  },
  async summarizeStandup() {
    this.standup.summarizing = true;
    try {
      this.standup.summary = (await api("/api/standup/summary", { text: this.standup.text })).summary;
      this.standup.view = "summary";
    } catch (e) { this.toast(e.message, true); }
    finally { this.standup.summarizing = false; }
  },

  // ---- first-run setup
  async openSetup() {
    this.panel = "setup";
    try {
      const s = await api("/api/setup");
      const current = s.repo_dirs.split(":").filter(Boolean);
      const known = new Set(s.dirs.map(d => d.path));
      s.dirs = [...s.dirs, ...current.filter(c => !known.has(c)).map(path => ({ path, repos: null }))]
        .map((d, i) => ({ ...d, on: current.includes(d.path) || (!current.some(c => known.has(c)) && i < 3) }));
      s.picked = Object.fromEntries(s.orgs.map(o => [o, s.selected.includes(o)]));
      Object.assign(s, { ai_provider: s.ai.provider, api_base_url: s.ai.api_base_url, api_model: s.ai.api_model, api_key: "", aiTest: "", aiTesting: false });
      this.setup = s;
    } catch (e) { this.toast(e.message, true); }
  },
  setupAI() {
    const s = this.setup, f = { ai_provider: s.ai_provider };
    if (s.ai_provider === "api") Object.assign(f, { api_base_url: s.api_base_url, api_model: s.api_model, ...(s.api_key ? { api_key: s.api_key } : {}) });
    return f;
  },
  async testSetupAI() {
    const s = this.setup;
    s.aiTesting = true; s.aiTest = "";
    try {
      await api("/api/settings", this.setupAI());
      s.aiTest = "✓ " + (await api("/api/settings/test", { what: "ai" })).out;
      this.load();
    } catch (e) { s.aiTest = "✗ " + e.message; }
    finally { s.aiTesting = false; }
  },
  async finishSetup() {
    const s = this.setup;
    try {
      await api("/api/setup", { orgs: s.orgs.filter(o => s.picked[o]), repo_dirs: s.dirs.filter(d => d.on).map(d => d.path).join(":"),
                                terminal: s.terminal, editor: s.editor, notify_cmd: s.notify_cmd, ...this.setupAI() });
      this.panel = null; this.toast("You're set. First sync takes a few seconds."); this.load();
    } catch (e) { this.toast(e.message, true); }
  },

  // ---- command palette
  get palItems() {
    const q = this.palQ.toLowerCase().trim(), words = q.split(/\s+/).filter(Boolean), p = this.cur || this.data?.prs[this.focus];
    const cmds = [
      ...(p ? [
        ...(this.cfg.ai_terminal ? [[`Open in ${this.cfg.ai_name}`, "sparkles", () => this.act("agent", p)]] : []),
        [`${this.cfg.ai_name} review`, "sparkles", () => this.act("review", p)],
        ["Open in editor", "code", () => this.act("editor", p)], ["Open terminal", "terminal", () => this.act("terminal", p)],
        ["Create / open worktree", "git-branch", () => this.act("worktree", p)], ["Check out branch in clone", "git-branch", () => this.act("checkout", p)],
        ["Announce in Slack", "hash", () => this.announce(p)], ["Approve", "check", () => this.act("approve", p)],
        ["Rebase on " + p.base, "refresh-cw", () => this.act("rebase", p)], ["Re-run failed checks", "refresh-cw", () => this.act("rerun", p)],
        ["Comment", "message-square", () => this.focusComposer()],
        ["Retry failed checks until green", "repeat", () => this.act("retry", p)],
        ...(this.rerequest(p) ? [[`Re-request review from ${this.rerequest(p).who.join(", ")}`, "user-plus", () => this.act("rerequest", p, { arg: this.rerequest(p).who.join(",") })]] : []),
        ...(p.author === this.data?.me && this.cfg.ai_terminal ? [[`Fix review comments with ${this.cfg.ai_name}`, "sparkles", () => this.act("fix", p)]] : []),
        [this.L(p).muted ? "Unmute" : "Mute notifications", "bell-off", () => this.setLocal(p, "muted", !this.L(p).muted || null)],
        ...(p.author !== this.data?.me ? [[this.L(p).following ? "Unfollow" : "Follow (CI, reviews, comments, merge)", "eye", () => this.setLocal(p, "following", !this.L(p).following || null)]] : []),
        ["Snooze 1 day", "clock", () => this.snooze(p, 1)], ["Snooze 1 week", "clock", () => this.snooze(p, 7)],
        ["Copy link", "link", () => this.copy(p.url)], ["Open on GitHub", "external-link", () => window.open(p.url, "_blank")],
      ].map(([t, i, f]) => ({ title: `${t} · #${p.number}`, icon: i, run: f })) : []),
      ...Object.entries(SCOPES).map(([k, v]) => ({ title: `Go to ${v}`, icon: "list", run: () => this.setScope(k) })),
      ...Object.entries(PRESETS).map(([k, v]) => ({ title: `Show ${v.label}`, icon: "filter", run: () => (this.preset = k) })),
      ...this.saved.map(s => ({ title: `Filter: ${s.name}`, icon: "filter", run: () => (this.q = s.q) })),
      { title: "Board view", icon: "kanban", run: () => (this.layout = "board") }, { title: "Table view", icon: "table-2", run: () => (this.layout = "table") },
      { title: "Inbox", icon: "inbox", run: () => this.openInbox() }, { title: "Standup summary", icon: "clipboard", run: () => this.openStandup() },
      { title: "Insights: turnaround and flaky checks", icon: "chart-column", run: () => this.openInsights() },
      { title: this.heat ? "Turn off age colors" : "Color PRs by age", icon: "thermometer", run: () => (this.heat = !this.heat) },
      { title: "Settings", icon: "settings", run: () => this.openSettings() }, { title: "Sync now", icon: "refresh-cw", run: () => this.sync() },
      { title: "Keyboard shortcuts & filter syntax", icon: "keyboard", run: () => (this.panel = "help") },
      { title: "Clear filters", icon: "x", run: () => { this.q = ""; this.preset = "open"; } },
    ];
    const hit = s => words.every(w => s.toLowerCase().includes(w));
    const prs = q ? Object.values(this.data?.prs || {}).filter(p => hit(`${p.repo} #${p.number} ${p.title} ${p.author} ${p.jira.join(" ")}`)).slice(0, 30)
      .map(p => ({ title: p.title, sub: `${p.repo}#${p.number} · ${p.author}`, icon: "git-pull-request", run: () => { this.sel = p.url; this.focus = p.url; } }))
      : [];
    return [...cmds.filter(c => hit(c.title)), ...prs].slice(0, 60);
  },
  openPalette() { this.palQ = ""; this.palI = 0; this.panel = "palette"; this.$nextTick(() => this.$refs.pal?.focus()); },
  runPal(i = this.palI) { const it = this.palItems[i]; if (!it) return; this.panel = null; it.run(); },
  setScope(s) { this.scope = s; this.sel = null; this.focus = null; this.more = {}; },
  home() { // logo: your open PRs, nothing filtered or open; keeps the board/table choice
    Object.assign(this, { q: "", preset: "open", panel: null, builderOpen: false, showHidden: false, picked: {} });
    this.setScope("mine");
    window.scrollTo({ top: 0, behavior: "smooth" });
  },

  // ---- keyboard
  key(e) {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    if ((e.ctrlKey || e.metaKey) && e.key === "k") { e.preventDefault(); return this.panel === "palette" ? (this.panel = null) : this.openPalette(); }
    if (this.panel === "palette") {
      const n = this.palItems.length;
      if (e.key === "ArrowDown") { e.preventDefault(); this.palI = (this.palI + 1) % n; }
      else if (e.key === "ArrowUp") { e.preventDefault(); this.palI = (this.palI - 1 + n) % n; }
      else if (e.key === "Enter") { e.preventDefault(); this.runPal(); }
      else if (e.key === "Escape") this.panel = null;
      return;
    }
    if (e.key === "Escape") {
      if (typing) return document.activeElement.blur();
      return this.panel ? (this.panel = null) : this.sel ? (this.sel = null) : this.builderOpen ? (this.builderOpen = false) : this.clearPicked();
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    const p = this.cur || this.data?.prs[this.focus];
    const move = d => {
      const o = this.order, i = o.findIndex(x => x.url === (this.sel || this.focus));
      const next = o[Math.min(o.length - 1, Math.max(0, i + d))] || o[0];
      if (!next) return;
      this.focus = next.url;
      if (this.sel) this.sel = next.url; // drawer open: j/k walks the queue
      this.$nextTick(() => document.querySelector(`[data-url="${CSS.escape(next.url)}"]`)?.scrollIntoView({ block: "nearest" }));
    };
    const keys = {
      "/": () => this.$refs.search.focus(), "?": () => (this.panel = "help"), f: () => (this.builderOpen = !this.builderOpen),
      1: () => this.setScope("mine"), 2: () => this.setScope("review"), 3: () => this.setScope("team"),
      b: () => (this.layout = "board"), t: () => (this.layout = "table"), r: () => this.sync(),
      i: () => this.openInbox(), ",": () => this.openSettings(), u: () => this.openStandup(), s: () => this.openInsights(),
      g: () => (this.heat = !this.heat),
      j: () => move(1), k: () => move(-1), ArrowDown: () => move(1), ArrowUp: () => move(-1),
      X: () => this.pickAll(),
    };
    const prKeys = p && {
      Enter: () => (this.sel = p.url), o: () => (this.sel = p.url), O: () => window.open(p.url, "_blank"),
      x: () => this.toggle(p), c: () => this.cfg.ai_terminal && this.act("agent", p), v: () => this.act("review", p), e: () => this.act("editor", p),
      w: () => this.act("worktree", p), p: () => this.announce(p), a: () => this.act("approve", p), y: () => this.copy(p.url, "Link copied"),
      z: () => this.snooze(p, 1), h: () => this.setLocal(p, "hidden", !this.L(p).hidden), m: () => this.focusComposer(),
    };
    const fn = keys[e.key] || prKeys?.[e.key];
    if (fn) { e.preventDefault(); fn(); }
  },

  toast(msg, err = false) {
    this.toastMsg = msg; this.toastErr = err;
    clearTimeout(this._t); this._t = setTimeout(() => (this.toastMsg = ""), err ? 7000 : 3000);
  },
}));

window.Alpine = Alpine; // for devtools and tests/e2e.mjs
Alpine.start();
