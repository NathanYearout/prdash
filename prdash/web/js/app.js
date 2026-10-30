// prdash page. One Alpine component; data comes from the local server (/api/state) and live updates over SSE.
import Alpine from "../vendor/alpine.esm.js";
import { compile, flipTerm, getTerm, setTerm, sinceReview, SYNTAX } from "./filter.js";
import { card, md, patch, row } from "./render.js";

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
// Notification chimes, synthesized so there are no audio files: soft sine plucks, notes in Hz.
const CHIMES = { urgent: [988, 988], bad: [659, 523], good: [784, 1047], ping: [880] };
const CHIME_OF = { urgent: "urgent", ci_failed: "bad", conflict: "bad", retry_failed: "bad", request_failed: "bad", closed: "bad",
                   merged: "good", ci_passed: "good", retry_ok: "good", requested: "good" };
let audio;

// scrollIntoView(nearest) stops when the card touches the viewport. The sticky bar then covers the top of
// the card, and the column header above the first card stays scrolled off, so k never brings the top back.
function reveal(el) {
  el.scrollIntoView({ block: "nearest", inline: "nearest" });
  const bar = document.querySelector("header")?.getBoundingClientRect().bottom ?? 0;
  const col = el.closest(".col");
  const first = (col || el.closest("tbody"))?.querySelector("[data-url]");
  const head = first === el && (col?.querySelector(":scope > .col-h") || el.closest("table"));
  const anchor = head || el;
  const limit = bar + 8;
  for (let n = 0; n < 3; n++) { // content-visibility placeholders above the card settle as they render
    const top = anchor.getBoundingClientRect().top;
    if (top >= limit - 1) break;
    window.scrollBy(0, top - limit);
  }
}

function chime(kind, at = 0) {
  audio ||= new AudioContext();
  audio.resume(); // browsers start it suspended until the page has had a click
  CHIMES[kind].forEach((f, i) => {
    const o = audio.createOscillator(), g = audio.createGain(), t = audio.currentTime + at + i * 0.09;
    o.frequency.value = f;
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(0.3, t + 0.008);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.4);
    o.connect(g).connect(audio.destination);
    o.start(t); o.stop(t + 0.45);
  });
}
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
// age colors: days since a PR opened, on a log scale up to 6 months so very old PRs keep getting redder.
// Green at 0, amber around a week, red around a month, then a red tint that deepens until 6 months.
const AGE_MAX = 180;
const clamp01 = x => Math.max(0, Math.min(1, x));
const pct = x => `${Math.round(clamp01(x) * 100)}%`;
const splitTags = s => (s || "").split(",").map(t => t.trim()).filter(Boolean);
const hue = s => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 0); // stable color per name
function heatStyle(days) {
  const t = clamp01(Math.log2(1 + days) / Math.log2(1 + AGE_MAX));
  return `--a:${pct(t / .4)};--b:${pct((t - .4) / .26)};--tint:${Math.round(clamp01((t - .66) / .34) * 18)}%`;
}
const EVENT_ICON = {
  review_requested: "eye", review: "message-square", comment: "message-square", ci_failed: "triangle-alert", ci_passed: "check",
  merged: "git-merge", closed: "x", conflict: "triangle-alert", snooze: "clock", review_done: "sparkles", review_error: "triangle-alert",
  jira: "ticket", jira_error: "triangle-alert", bump: "alarm-clock", urgent: "siren", mention: "at-sign", pushed: "git-commit-horizontal",
  retrying: "repeat", retry_ok: "circle-check", retry_failed: "triangle-alert", requested: "user-plus", request_failed: "triangle-alert",
};
const BUILDER = [ // filter builder rows: [key, label, options or "people"/"repos"]
  ["author", "Author", "people"], ["reviewer", "Reviewer", "people"], ["requested", "Review requested from", "people"],
  ["approved-by", "Approved by", "people"], ["repo", "Repo", "repos"],
  ["updated", "Updated", [["<1d", "today"], ["<3d", "last 3 days"], ["<1w", "last week"], ["<2w", "last 2 weeks"], ["<1m", "last month"], [">1w", "over a week ago"], [">1m", "over a month ago"]]],
  ["created", "Opened", [["<1d", "today"], ["<1w", "last week"], ["<1m", "last month"], [">2w", "over 2 weeks ago"], [">1m", "over a month ago"], [">3m", "over 3 months ago"]]],
  ["size", "Size", [["xs", "XS (<10 lines)"], ["s", "S (<100)"], ["m", "M (<500)"], ["l", "L (<1000)"], ["xl", "XL (1000+)"]]],
  ["ci", "CI", [["failing", "Failing"], ["pending", "Pending"], ["passing", "Passing"], ["none", "No checks"]]],
  ["review", "Review", [["needed", "Needed"], ["changes", "Changes requested"], ["approved", "Approved"], ["none", "None"]]],
  ["is", "Is", [["urgent", "Urgent"], ["bumped", "Bumped"], ["pushed", "New commits since changes requested"], ["new-since-review", "New since your review"], ["mentioned", "Mentions you"],
                ["blocked", "Can't merge yet"], ["behind", "Behind its base"], ["auto-merge", "Auto-merge on"],
                ["retrying", "Retrying until green"], ["requesting", "Review when green"], ["following", "Following"], ["muted", "Muted"], ["draft", "Draft"], ["stale", "Stale"],
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
// PR data lives outside Alpine's reactivity (proxying 250 PRs made every read during filtering and rendering
// pay for dependency tracking). Alpine watches `rev` instead; __v_skip keeps @vue/reactivity from wrapping it.
const raw = o => Object.defineProperty(o, "__v_skip", { value: true });
let STORE = null, LOCAL = raw({}), LAST = null;
const plusDays = n => new Date(Date.now() + n * DAY).toISOString().slice(0, 10);

Alpine.data("prdash", () => ({
  STATUSES, PRESETS, SCOPES, COLUMNS, SYNTAX, BUILDER, EVENT_ICON, THEMES, AI_PRESETS, heatStyle,
  scope: "mine", preset: "open", layout: "board", q: "", sort: "updated", dir: -1, showHidden: false, heat: false,
  replyFor: null, replyBody: "", inboxFilter: "all", insights: null, insightsView: "mine",
  rev: 0, cfg: {}, status: { syncing: false, at: null, error: null }, now: Date.now(), sel: null, focus: null, picked: {}, detail: {}, detailFor: null,
  cbody: "", mention: null, convoAll: false, picker: null, armed: null, busy: {}, toastMsg: "", toastErr: false, panel: null, palQ: "", palI: 0, settings: null,
  settingsTab: "Appearance", events: [],
  standup: { window: "yesterday", text: "", summary: "", view: "details", loading: false, summarizing: false }, builderOpen: false, saved: [], more: {}, setup: null,
  notifyPermission: "Notification" in window ? Notification.permission : "unsupported", naming: null, tagging: null, lastPicked: null,
  pending: "", visual: null, tsel: null,

  init() {
    try { Object.assign(this, JSON.parse(localStorage.prdash || "{}")); } catch {}
    try { this.saved = JSON.parse(localStorage.prdashFilters || "[]"); } catch {}
    this.cfg = { team_view: localStorage.prdashTeam !== "0" }; // so the Team tab doesn't pop in and shift the header on load
    PERSISTED.forEach(k => this.$watch(k, () => {
      try { localStorage.prdash = JSON.stringify(Object.fromEntries(PERSISTED.map(k => [k, this[k]]))); } catch {}
    }));
    this.$watch("sel", url => { this.convoAll = false; this.tsel = null; url && this.loadDetail(url); });
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
      try { localStorage.prdashTeam = d.config.team_view ? "1" : "0"; } catch {}
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
      const want = PRESETS[preset].statuses, groups = {};
      for (const p of list) (groups[p.status] ||= []).push(p);
      // urgent first, then your priority; stable, so the chosen sort holds within each
      const byPrio = (a, b) => (!!b.urgent - !!a.urgent) || (heat && a.created.localeCompare(b.created)) || (this.L(b).prio || 0) - (this.L(a).prio || 0);
      return STATUSES.filter(s => (!want || want.includes(s.key)) && !(s.key === "merged" && scope === "review")).map(s => {
        const all = (groups[s.key] || []).sort(byPrio);
        return { ...s, total: all.length, prs: more[s.key] ? all : all.slice(0, COL_LIMIT) };
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
      for (const r of p.reviewers) if (!r.team) n[r.who] = (n[r.who] || 0) + 1;
    }
    return Object.keys(n).filter(x => x !== "ghost" && !x.endsWith("[bot]")).sort((a, b) => n[b] - n[a] || a.localeCompare(b));
  },
  get repos() { return [...new Set(Object.values(this.data?.prs || {}).map(p => p.repo))].sort(); },
  get allTags() { return [...new Set(Object.values(this.local || {}).flatMap(l => splitTags(l.tags)))].sort(); },
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
  // age mode: [inline style, label] for an open PR, or null
  heatOf(p) {
    if (!this.heat || p.merged || p.state !== "OPEN") return null;
    const d = this.days(p.created);
    return [heatStyle(d), this.age(d)];
  },
  age(d) { return d >= 365 ? `${(d / 365).toFixed(1).replace(".0", "")}y` : d >= 60 ? `${Math.round(d / 30)}mo` : this.ago(this.now - d * DAY); },
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
  sinceReview(p) { return p && sinceReview(p, this.data?.me); },
  sinceHref(p) { return `${p.url}/files/${this.sinceReview(p)}..${p.head}`; },
  jira(key) { return this.data?.jira?.[key]; },

  // ---- rendering (see render.js)
  // Each column reads its cards from `board` by key, not from its x-for scope: Alpine can run this effect before x-for
  // hands the element its new column, and it would render the old one. Only the visible layout renders.
  fillCol(el, key) { patch(el, this.layout !== "board" ? [] : (this.board.find(c => c.key === key)?.prs || []).map(p => [p.url, card(this, p)])); },
  fillTable(el) { patch(el, this.layout !== "table" ? [] : this.list.map(p => [p.url, row(this, p)])); },
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
  tags(p) { return splitTags(this.L(p).tags); },
  tagStyle(t) { return `--h:${hue(t)}`; },
  statusLabel(k) { return STATUSES.find(s => s.key === k)?.label; },
  reviewLabel(p) { return { APPROVED: "Approved", CHANGES_REQUESTED: "Changes", REVIEW_REQUIRED: "Needed" }[p.review] || "—"; },
  avatar(login) {
    if (!this.cfg.demo) return `https://github.com/${encodeURIComponent(login)}.png?size=48`;
    // demo: initials, no real people
    return `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><rect width="20" height="20" fill="hsl(${hue(login)},45%,45%)"/><text x="10" y="14" font-size="9" font-family="sans-serif" fill="white" text-anchor="middle">${login.slice(0, 2).toUpperCase()}</text></svg>`)}`;
  },
  jiraHref(k) { return this.cfg.jira_url ? `${this.cfg.jira_url}/browse/${k}` : null; },
  // [short, tone, icon, sentence, link]: why it's in Needs work, plus workflow nudges. The panel skips the ones its own sections show.
  flags(p, panel = false) {
    const f = [], idle = this.ago(p.updated);
    if (p.urgent && !p.merged) f.push(["Urgent", "urgent", null, "Marked urgent"]);
    if (p.bump && !p.merged) f.push([`Bumped ${this.ago(p.bump.at)}`, "amber", "alarm-clock", `${p.bump.who} bumped this ${this.ago(p.bump.at)} ago`]);
    if (p.ci === "FAILURE" || p.ci === "ERROR") f.push(["CI", "red", "triangle-alert", "CI is failing"]);
    if (p.review === "CHANGES_REQUESTED" && !p.fixes_pushed) f.push(["Changes", "red", "message-square", "Changes requested"]);
    if (p.mergeable === "CONFLICTING") f.push(["Conflicts", "red", "triangle-alert", `Merge conflicts with ${p.base}`]);
    if (this.sinceReview(p) && !panel) f.push(["New since your review", "amber", "git-commit-horizontal", "Commits since your last review: opens just those changes", this.sinceHref(p)]);
    else if (p.fixes_pushed && !p.merged) f.push(["New commits", "amber", "git-commit-horizontal", "New commits since changes were requested: ready for another look"]);
    if (p.blocker && p.blocker !== "Needs approval" && !panel) f.push([p.blocker, "muted", "hourglass", `Can't merge yet: ${p.blocker}`]);
    if (p.auto_merge && !p.merged && !panel) f.push(["Auto-merge", "green", "git-merge", `Merges itself (${p.auto_merge.method}) once it's ready; turned on by ${p.auto_merge.by}`]);
    if (p.open_threads && !p.merged && !panel) f.push([`${p.open_threads} open`, "muted", "message-square", `${p.open_threads} unresolved review thread${p.open_threads > 1 ? "s" : ""}`]);
    if (this.L(p).request_when_green && !p.merged && !panel) f.push(["Review when green", "amber", "user-plus", this.greenText(p)]);
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
  setTags(p, tags) { return this.setLocal(p, "tags", tags.join(", ") || null); },
  addTags(p, text) { return this.setTags(p, [...new Set([...this.tags(p), ...splitTags(text)])]); },
  // The tag input adds on Enter/blur (change) or on picking a datalist option (input without typed text)
  tagInput(p, e) {
    if (e.type === "input" && e.inputType && e.inputType !== "insertReplacementText") return;
    const t = e.target.value; e.target.value = "";
    if (t.trim()) this.addTags(p, t);
  },
  snooze(p, days) { if (days) this.toast(`Snoozed until ${plusDays(days)}`); return this.setLocal(p, "snooze", days ? plusDays(days) : null); },
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
    if (["merge", "automerge", "close", "approve"].includes(op) && this.armed !== op + p.url) {
      this.armed = op + p.url;
      this.toast(`Press again to ${{ automerge: "turn on auto-merge", close: "close the PR" }[op] || op}`); // keys can't see the armed button
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
      if (op === "request") { this.picker = null; this.load(); this.loadDetail(p.url); }
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
  // @mention autocomplete in the comment and reply boxes: this PR's people first, then everyone on synced PRs
  get mentionList() {
    const m = this.mention, p = this.pane;
    if (!m || !p) return [];
    const convo = (this.detail.conversation || []).flatMap(c => [c, ...(c.comments || [])]).map(c => c.who);
    const q = m.q.toLowerCase(), starts = u => !u.toLowerCase().startsWith(q);
    return [...new Set([p.author, ...p.reviewers.filter(r => !r.team).map(r => r.who), ...convo, ...this.people])]
      .filter(u => u && u !== this.data?.me && u !== "ghost" && !u.endsWith("[bot]") && u.toLowerCase().includes(q))
      .sort((a, b) => starts(a) - starts(b)).slice(0, 6);
  },
  mentionInput(e) {
    const el = e.target, m = el.value.slice(0, el.selectionStart).match(/(?:^|[^\w`])@([\w-]*)$/);
    this.mention = m ? { el, at: el.selectionStart - m[1].length, q: m[1], i: 0, r: el.getBoundingClientRect() } : null;
  },
  mentionKey(e) {
    const n = this.mentionList.length;
    if (!n) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") this.mention.i = (this.mention.i + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    else if ((e.key === "Enter" && !e.ctrlKey && !e.metaKey) || e.key === "Tab") this.pickMention();
    else if (e.key === "Escape") this.mention = null;
    else return;
    e.preventDefault(); e.stopImmediatePropagation(); // keep Esc from cancelling the reply or closing the drawer
  },
  pickMention(i = this.mention.i) {
    const { el, at, q } = this.mention, u = this.mentionList[i], v = el.value;
    el.value = v.slice(0, at) + u + " " + v.slice(at + q.length);
    el.dispatchEvent(new Event("input")); // x-model picks it up
    el.selectionStart = el.selectionEnd = at + u.length + 1;
    this.mention = null;
  },
  startReply(t) { this.replyFor = t.id; this.replyBody = ""; this.$nextTick(() => document.getElementById("reply-" + t.id)?.focus()); },

  // ---- request review: a picker in the panel. Defaults come from what's already requested and what you last picked
  // for this owner (detail.request.prefs, by owner so a still-loading panel can't carry another org's picks); picker only holds what you changed.
  openPicker(p) {
    if (!p || p.merged || p.state !== "OPEN") return;
    this.sel = p.url; this.picker = { url: p.url, owner: p.owner, q: "", teams: {}, users: {}, green: null, slack: null };
    setTimeout(() => document.getElementById("picker-q")?.focus(), 250);
  },
  // the org's teams, then people seen on its PRs (most active first) and the repo's collaborators; never the author.
  // load: open, ready PRs in this org currently requesting them
  reviewOptions(p) {
    const { rev, detail } = this;
    return memo("reviewOptions", [rev, p, detail], () => {
      const d = detail.request || {}, teams = d.teams || [], prs = Object.values(this.data?.prs || {}).filter(x => x.owner === p.owner);
      const seen = new Set(prs.flatMap(x => [x.author, ...x.reviewers.filter(r => !r.team).map(r => r.who)])), asked = new Set(p.requested || []), load = {};
      for (const x of prs) if (x.state === "OPEN" && !x.merged && !x.draft) for (const r of x.requested || []) load[r] = (load[r] || 0) + 1;
      const people = [...new Set([...this.people.filter(x => seen.has(x)), ...(d.people || [])])].filter(x => x !== p.author && !teams.includes(x));
      const item = kind => name => ({ kind, name, asked: asked.has(name), load: load[name] || 0 });
      return [...teams.map(item("teams")), ...people.map(item("users"))];
    });
  },
  pickerList(p) { const q = (this.picker?.q || "").toLowerCase().trim(); return this.reviewOptions(p).filter(it => it.name.toLowerCase().includes(q)); },
  isPicked(it) { return it.asked || (this.picker?.[it.kind][it.name] ?? (this.detail.request?.prefs?.[this.picker?.owner]?.[it.kind] || []).includes(it.name)); },
  pickerGreen(p) { return p.ci !== "SUCCESS" && (this.picker.green ?? true); },
  pickerSlack() { return !!this.cfg.slack && (this.picker.slack ?? !!this.detail.request?.prefs?.[this.picker.owner]?.slack); },
  requestLabel(p) { return (p.draft ? "Mark ready and request" : "Request") + (this.pickerGreen(p) ? " when green" : " review"); },
  requestReview(p) {
    const chosen = this.reviewOptions(p).filter(it => !it.asked && this.isPicked(it)), names = k => chosen.filter(it => it.kind === k).map(it => it.name);
    return this.act("request", p, { arg: { teams: names("teams"), users: names("users"), green: this.pickerGreen(p), slack: this.pickerSlack() } });
  },
  greenText(p) {
    const r = this.L(p).request_when_green;
    return r && `Requests review from ${[...r.teams, ...r.users].join(", ")}${r.slack ? " and posts to Slack" : ""} once CI is green` +
      (r.skip && r.skip === p.head ? " (after the next push: CI was failing when this was set)" : "");
  },

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
    await Promise.all(urls.map(u => this.addTags({ url: u }, t)));
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
    // one chime per sync, for the loudest kind in it
    if (this.cfg.notify_sound) chime(Object.keys(CHIMES).find(k => events.some(ev => (CHIME_OF[ev.kind] || "ping") === k)));
    if (!this.cfg.notify_browser || !("Notification" in window) || Notification.permission !== "granted") return;
    events.slice(0, 3).forEach(ev => {
      const n = new Notification(ev.kind === "urgent" ? "prdash: urgent" : "prdash", { body: ev.text, tag: ev.url + ev.kind,
                                     icon: "/static/logo.svg", requireInteraction: ev.kind === "urgent" });
      n.onclick = () => { window.focus(); if (ev.url) this.sel = ev.url; };
    });
  },
  previewChimes() { Object.keys(CHIMES).forEach((k, i) => chime(k, i * 0.6)); },
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
        ...(this.sinceReview(p) ? [[`${this.cfg.ai_name} review of changes since mine`, "sparkles", () => this.act("review", p, { arg: "since" })]] : []),
        ["Open in editor", "code", () => this.act("editor", p)], ["Open terminal", "terminal", () => this.act("terminal", p)],
        ["Create / open worktree", "git-branch", () => this.act("worktree", p)], ["Check out branch in clone", "git-branch", () => this.act("checkout", p)],
        ["Announce in Slack", "hash", () => this.announce(p)], ["Approve", "check", () => this.act("approve", p)],
        ...(!p.merged && p.state === "OPEN" ? [["Request review", "user-plus", () => this.openPicker(p)]] : []),
        ["Rebase on " + p.base, "refresh-cw", () => this.act("rebase", p)],
        ...(p.can_automerge && !p.auto_merge ? [["Merge when ready (press twice)", "git-merge", () => this.act("automerge", p)]] : []),
        ...(p.auto_merge && p.can_automerge_off ? [["Cancel auto-merge", "x", () => this.act("automerge_off", p)]] : []), ["Re-run failed checks", "refresh-cw", () => this.act("rerun", p)],
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

  // ---- keyboard, vim-style. One table drives the keys, the which-key hint after a prefix, and the help panel.
  // [group, keys, what, run]: "gg" makes g a prefix, " m" is Space then m (the leader, for less frequent PR actions), "C-d" is Ctrl+d, no run means it's handled elsewhere (shown in help only).
  // PR keys act on the open PR, else the focused one.
  keymap() {
    const p = this.cur || this.data?.prs[this.focus], ai = this.cfg.ai_name || "AI", on = f => () => p && f(p);
    const thread = () => this.sel && this.threads().find(t => t.id === this.tsel);
    return [
      ["Move", ["j", "ArrowDown"], "Next PR (with a PR open, walks the queue)", () => this.move(1)],
      ["Move", ["k", "ArrowUp"], "Previous PR", () => this.move(-1)],
      ["Move", ["h", "ArrowLeft"], "Column to the left", () => this.moveCol(-1)],
      ["Move", ["l", "ArrowRight"], "Column to the right", () => this.moveCol(1)],
      ["Move", ["gg", "G"], "First · last PR", null],
      ["Move", ["gg"], null, () => this.move(-Infinity)], ["Move", ["G"], null, () => this.move(Infinity)],
      ["Move", ["C-d", "C-u"], "Half page down · up (scrolls the PR when one is open)", null],
      ["Move", ["C-d"], null, () => this.page(1)], ["Move", ["C-u"], null, () => this.page(-1)],
      ["Move", ["Enter", "o"], "Open the PR", on(p => (this.sel = p.url))],
      ["Move", ["Escape"], "Close or step back; leaves a text box", null],
      ["Open PR", ["]", "["], "Next · previous unresolved thread", null],
      ["Open PR", ["]"], null, () => this.thread(1)], ["Open PR", ["["], null, () => this.thread(-1)],
      ["Open PR", ["}", "{"], "Next · previous section", null],
      ["Open PR", ["}"], null, () => this.section(1)], ["Open PR", ["{"], null, () => this.section(-1)],
      ["Open PR", ["r"], "Reply to the thread (or comment on the PR)", on(() => (thread() ? this.startReply(thread()) : this.focusComposer()))],
      ["Open PR", ["R"], "Resolve or unresolve the thread", on(p => { const t = thread(); t && this.act(t.resolved ? "thread_unresolve" : "thread_resolve", p, { arg: t.id }); })],
      ["PR", ["a"], "Approve (press twice)", on(p => this.act("approve", p))],
      ["PR", ["c"], `Open in ${ai}`, on(p => this.cfg.ai_terminal && this.act("agent", p))],
      ["PR", ["C"], `${ai} review`, on(p => this.act("review", p))],
      ["PR", ["m"], "Comment", on(() => this.focusComposer())],
      ["PR", ["q"], "Request review", on(p => this.openPicker(p))],
      ["PR", ["e"], "Open in editor", on(p => this.act("editor", p))],
      ["PR", ["w"], "Create or open worktree", on(p => this.act("worktree", p))],
      ["PR", ["p"], "Announce in Slack", on(p => this.announce(p))],
      ["PR", ["y"], "Copy link", on(p => this.copy(p.url, "Link copied"))],
      ["PR", ["gx", "O"], "Open on GitHub", on(p => window.open(p.url, "_blank"))],
      ["PR", ["z", "Z"], "Snooze a day · a week", null],
      ["PR", ["z"], null, on(p => this.dismiss(p, () => this.snooze(p, 1)))], ["PR", ["Z"], null, on(p => this.dismiss(p, () => this.snooze(p, 7)))],
      ["PR", ["dd"], "Hide (fh shows hidden)", on(p => this.dismiss(p, () => this.setLocal(p, "hidden", !this.L(p).hidden || null)))],
      ["Space (leader)", [" m"], "Merge (press twice)", on(p => this.act("merge", p))],
      ["Space (leader)", [" M"], "Merge when ready, or cancel auto-merge", on(p => this.act(p.auto_merge ? "automerge_off" : "automerge", p))],
      ["Space (leader)", [" r"], "Rebase on the base branch", on(p => this.act("rebase", p))],
      ["Space (leader)", [" d"], "Mark ready, or back to draft", on(p => this.act(p.draft ? "ready" : "draft", p))],
      ["Space (leader)", [" x"], "Re-run failed checks", on(p => this.act("rerun", p))],
      ["Space (leader)", [" g"], "Retry checks until green, or stop", on(p => this.act(this.L(p).retry ? "retry_stop" : "retry", p))],
      ["Space (leader)", [" q"], "Re-request review from earlier reviewers", on(p => { const r = this.rerequest(p); r ? this.act("rerequest", p, { arg: r.who.join(",") }) : this.toast("Nobody to re-ask yet"); })],
      ["Space (leader)", [" C"], `${ai} review of changes since mine`, on(p => (this.sinceReview(p) ? this.act("review", p, { arg: "since" }) : this.toast("No new commits since your review")))],
      ["Space (leader)", [" f"], `Fix review comments with ${ai}`, on(p => this.act("fix", p))],
      ["Space (leader)", [" c"], "Check out the branch in your clone", on(p => this.act("checkout", p))],
      ["Space (leader)", [" t"], "Open a terminal there", on(p => this.act("terminal", p))],
      ["Space (leader)", [" n"], "Mute or unmute notifications", on(p => this.setLocal(p, "muted", !this.L(p).muted || null))],
      ["Space (leader)", [" w"], "Follow (watch) someone else's PR", on(p => this.setLocal(p, "following", !this.L(p).following || null))],
      ["Space (leader)", [" X"], "Close the PR (press twice)", on(p => this.act("close", p))],
      ["Select", ["x"], "Select", on(p => this.toggle(p))],
      ["Select", ["v"], "Select while moving (visual mode)", () => this.visualMode()],
      ["Select", ["X"], "Select all", () => this.pickAll()],
      ["Filter", ["/"], "Search (Enter or Esc goes back to the board)", () => this.$refs.search.focus()],
      ["Filter", ["ff"], "Filter builder", () => this.openBuilder()],
      ["Filter", ["fa"], "This PR's author", on(p => this.flip("author", p.author))],
      ["Filter", ["fr"], "This PR's repo", on(p => this.flip("repo", p.repo))],
      ["Filter", ["fc"], "CI failing", () => this.flip("ci", "failing")],
      ["Filter", ["fn"], "New since your review", () => this.flip("is", "new-since-review")],
      ["Filter", ["fu"], "Urgent", () => this.flip("is", "urgent")],
      ["Filter", ["fp"], "Next preset (Open, Unfinished, Complete, All)", () => this.nextPreset()],
      ["Filter", ["fh"], "Show hidden & snoozed", () => (this.showHidden = !this.showHidden)],
      ["Filter", ["fs"], "Save this filter (then f1–f9 applies it)", () => this.q.trim() && ((this.naming = ""), this.$nextTick(() => this.$refs.naming.focus()))],
      ...this.saved.slice(0, 9).map((f, i) => ["Filter", [`f${i + 1}`], `Saved: ${f.name}`, () => (this.q = this.q === f.q ? "" : f.q)]),
      ["Filter", ["fx"], "Clear filters", () => Object.assign(this, { q: "", preset: "open", showHidden: false })],
      ["View", ["1", "2", "3"], "Mine · Review requested · Team", null],
      ...Object.keys(SCOPES).map((k, i) => ["View", [`${i + 1}`], null, () => this.setScope(k)]),
      ["View", ["b", "t"], "Board · table", null],
      ["View", ["b"], null, () => (this.layout = "board")], ["View", ["t"], null, () => (this.layout = "table")],
      ["View", ["ga"], "Color PRs by age", () => (this.heat = !this.heat)],
      ["View", ["i", "u", "s"], "Inbox · standup · insights", null],
      ["View", ["i"], null, () => this.openInbox()], ["View", ["u"], null, () => this.openStandup()], ["View", ["s"], null, () => this.openInsights()],
      ["View", ["C-k"], "Command palette: every action and PR", null],
      ["View", ["S"], "Sync now", () => this.sync()],
      ["View", [","], "Settings", () => this.openSettings()],
      ["View", ["?"], "This help", () => (this.panel = "help")],
    ];
  },
  keyLabel(k) {
    if (k.startsWith(" ")) return k.length > 1 ? "Space " + k.slice(1) : "Space";
    return { ArrowDown: "Down", ArrowUp: "Up", ArrowLeft: "Left", ArrowRight: "Right", Escape: "Esc", "C-d": "Ctrl d", "C-u": "Ctrl u", "C-k": "Ctrl K" }[k] || k;
  },
  // help panel: the described rows, by group
  get keyHelp() {
    const g = {};
    for (const [group, keys, what] of this.keymap()) if (what) (g[group] ||= []).push([keys, what]);
    return Object.entries(g);
  },
  // which-key: what can follow the prefix typed so far
  get nextKeys() {
    const n = this.pending.length;
    return n ? this.keymap().flatMap(([, keys, what, run]) => run ? keys.filter(k => k.length > n && k.startsWith(this.pending)).map(k => [k.slice(n), what]) : []) : [];
  },
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
    if (e.key === "Escape") return typing ? document.activeElement.blur() : this.back();
    if (typing || e.metaKey || e.altKey || ["Shift", "Control"].includes(e.key)) return; // Shift alone mustn't cancel a prefix before G
    const seq = this.pending + (e.ctrlKey ? "C-" : "") + e.key, keys = this.keymap().filter(k => k[3]);
    const hit = keys.find(k => k[1].includes(seq));
    const prefix = !hit && !e.ctrlKey && keys.some(k => k[1].some(x => x.length > seq.length && x.startsWith(seq)));
    this.pending = prefix ? seq : "";
    if (hit || prefix) e.preventDefault();
    if (hit) hit[3]();
  },
  back() { // Esc: close whatever is innermost
    if (this.pending) return (this.pending = "");
    if (this.visual) return (this.visual = null);
    if (this.panel) return (this.panel = null);
    if (this.picker) return (this.picker = null);
    if (this.sel) return (this.sel = null);
    if (this.builderOpen) return (this.builderOpen = false);
    this.clearPicked();
  },
  // focus a PR and scroll to it; an open PR follows, and visual mode extends the selection
  show(url) {
    this.focus = url;
    if (this.sel) this.sel = url;
    if (this.visual) this.pickRange();
    this.$nextTick(() => { // board and table both render the row; the hidden layout's copy has no box
      const el = [...document.querySelectorAll(`[data-url="${CSS.escape(url)}"]`)].find(e => e.getClientRects().length);
      if (el) reveal(el);
    });
  },
  move(d) { // ±Infinity goes to either end
    const o = this.order, i = o.findIndex(x => x.url === (this.sel || this.focus));
    const next = o[Math.min(o.length - 1, Math.max(0, i + d))] || o[0];
    if (next) this.show(next.url);
  },
  moveCol(d) { // h/l on the board: same row in the next column that has cards
    if (this.layout !== "board") return;
    const cols = this.board.filter(c => c.prs.length), u = this.sel || this.focus;
    const c = cols.findIndex(x => x.prs.some(p => p.url === u)), row = c < 0 ? 0 : cols[c].prs.findIndex(p => p.url === u);
    const to = cols[Math.min(cols.length - 1, Math.max(0, c + d))];
    if (to) this.show(to.prs[Math.min(row, to.prs.length - 1)].url);
  },
  page(d) { // half a screen of the open PR, else ten PRs
    const box = this.sel && this.$refs.drawer;
    if (box) box.scrollBy({ top: d * box.clientHeight / 2, behavior: "smooth" });
    else this.move(d * 10);
  },
  section(d) { // { }: scroll the open PR to the previous / next section
    const box = this.sel && this.$refs.drawer;
    if (!box) return;
    const top = box.getBoundingClientRect().top;
    const ys = [...box.querySelectorAll("section")].filter(s => s.getClientRects().length).map(s => s.getBoundingClientRect().top - top);
    const y = d > 0 ? ys.find(y => y > 2) : ys.findLast(y => y < -2);
    box.scrollBy({ top: y ?? (d > 0 ? box.scrollHeight : -box.scrollTop), behavior: "smooth" });
  },
  thread(d) { // ] [: next / previous unresolved review thread in the open PR
    if (!this.sel) return;
    const all = this.threads(), i = all.findIndex(t => t.id === this.tsel);
    const t = d > 0 ? all.find((t, j) => j > i && !t.resolved) : all.findLast((t, j) => j < (i < 0 ? all.length : i) && !t.resolved);
    if (!t) return this.toast(all.some(t => !t.resolved) ? `No ${d > 0 ? "later" : "earlier"} unresolved threads` : "No unresolved threads");
    this.tsel = t.id;
    this.$nextTick(() => document.getElementById("t-" + t.id)?.scrollIntoView({ block: "center", behavior: "smooth" }));
  },
  async dismiss(p, fn) { // z, Z, dd: the PR leaves the board, so focus moves to its neighbour
    const o = this.order, i = o.indexOf(p), next = o[i + 1] || o[i - 1];
    await fn();
    if (next && !this.order.includes(p)) this.show(next.url);
  },
  visualMode() { // v: everything you move over gets selected, on top of what already was
    if (this.visual) return (this.visual = null);
    const u = this.sel || this.focus || this.order[0]?.url;
    if (!u) return;
    this.focus = u; this.visual = { from: u, base: { ...this.picked } }; this.pickRange();
  },
  pickRange() {
    const o = this.order.map(p => p.url), [a, b] = [o.indexOf(this.visual.from), o.indexOf(this.sel || this.focus)].sort((x, y) => x - y);
    this.picked = { ...this.visual.base, ...(a < 0 ? {} : Object.fromEntries(o.slice(a, b + 1).map(u => [u, true]))) };
  },
  flip(key, value) { this.q = flipTerm(this.q, key, value); },
  nextPreset() { const k = Object.keys(PRESETS); this.preset = k[(k.indexOf(this.preset) + 1) % k.length]; },
  openBuilder() {
    this.builderOpen = !this.builderOpen;
    if (this.builderOpen) this.$nextTick(() => document.querySelector(".builder input, .builder select")?.focus());
  },

  toast(msg, err = false) {
    this.toastMsg = msg; this.toastErr = err;
    clearTimeout(this._t); this._t = setTimeout(() => (this.toastMsg = ""), err ? 7000 : 3000);
  },
}));

window.Alpine = Alpine; // for devtools and tests/e2e.mjs
Alpine.start();
