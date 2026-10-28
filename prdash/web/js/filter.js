// Query language for the search box, e.g.  author:alice reviewer:bob updated:<3d size:>500 -is:draft "payment link"
// Terms AND together; commas OR within a term (author:alice,bob); a leading - negates.

const DAY = 864e5;
const UNITS = { h: DAY / 24, d: DAY, w: 7 * DAY, m: 30 * DAY, y: 365 * DAY };
const SIZES = { xs: [0, 10], s: [10, 100], m: [100, 500], l: [500, 1000], xl: [1000, Infinity] };
const CI = { failing: ["FAILURE", "ERROR"], passing: ["SUCCESS"], pending: ["PENDING", "EXPECTED"], none: [null] };
const REVIEW = { approved: "APPROVED", changes: "CHANGES_REQUESTED", needed: "REVIEW_REQUIRED", none: null };
const PRIO = { none: 0, low: 1, medium: 2, med: 2, high: 3 };

// Reference shown in the help panel; also the list of keys the builder knows about.
export const SYNTAX = [
  ["author:alice,bob", "Opened by any of these people"],
  ["reviewer:alice", "Alice is a reviewer (requested or already reviewed)"],
  ["requested:alice", "Review currently requested from Alice"],
  ["approved-by:alice", "Alice approved it"],
  ["repo:webapp", "Repo name contains text"],
  ["updated:<3d  created:>2w  merged:<7d", "Within / older than: h d w m y"],
  ["size:>500  size:xl  files:>20", "Lines changed (or xs s m l xl), files changed"],
  ["ci:failing  review:approved", "ci: failing passing pending none · review: approved changes needed none"],
  ["is:draft  is:stale  is:mine", "draft open merged approved needs-work in-review mine stale snoozed hidden posted conflict"],
  ["has:jira  -has:note", "jira note tags review waiting slack labels"],
  ["label:bug  tag:blocked  jira:ENG-4", "GitHub label, your tag, ticket key prefix"],
  ["waiting:qa  prio:>=2  comments:>5", "Your waiting-on text, priority 0-3, comment count"],
  ["-author:dependabot", "A leading - negates any term"],
];

export function parse(q) {
  const terms = [];
  for (const m of q.matchAll(/(-?)(?:([\w-]+):)?("[^"]*"|\S+)/g)) {
    const [, neg, key, raw] = m;
    const value = raw.replace(/^"|"$/g, "");
    if (!value) continue;
    terms.push({ neg: !!neg, key: key ? key.toLowerCase() : "text", value });
  }
  return terms;
}

// "<3d" -> pr is newer than 3 days; ">2w" -> older than 2 weeks
function age(iso, spec, now) {
  if (!iso) return false;
  const m = /^([<>]=?)?(\d+(?:\.\d+)?)([hdwmy])$/.exec(spec);
  if (!m) return true; // incomplete while typing: don't hide everything
  const elapsed = now - new Date(iso), limit = +m[2] * UNITS[m[3]];
  return (m[1] || "<").startsWith(">") ? elapsed > limit : elapsed <= limit;
}

function num(value, spec) {
  const m = /^(<=|>=|<|>|=)?(\d+)$/.exec(spec);
  if (!m) return true;
  const n = +m[2];
  return { "<": value < n, "<=": value <= n, ">": value > n, ">=": value >= n, "=": value === n, undefined: value === n }[m[1]];
}

const any = (value, fn) => value.toLowerCase().split(",").filter(Boolean).some(fn);
const has = (s, v) => (s || "").toLowerCase().includes(v);

// ctx: { L(pr) -> local fields, me, reviews: {url: review}, staleDays, now }
const MATCH = {
  text: (p, v, c) => has([p.title, p.repo, p.author, p.branch, `#${p.number}`, ...p.jira, ...p.labels, c.L(p).tags, c.L(p).note, c.L(p).waiting].join(" "), v.toLowerCase()),
  author: (p, v, c) => any(v, a => (a === "@me" ? p.author === c.me : has(p.author, a))),
  reviewer: (p, v, c) => any(v, a => p.reviewers.some(r => (a === "@me" ? r.who === c.me : has(r.who, a)))),
  requested: (p, v) => any(v, a => p.reviewers.some(r => r.state === "REQUESTED" && has(r.who, a))),
  "approved-by": (p, v) => any(v, a => p.reviewers.some(r => r.state === "APPROVED" && has(r.who, a))),
  "reviewed-by": (p, v) => any(v, a => p.reviewers.some(r => r.state !== "REQUESTED" && has(r.who, a))),
  repo: (p, v) => any(v, r => has(p.repo, r)),
  org: (p, v) => any(v, o => has(p.owner, o)),
  label: (p, v) => any(v, l => p.labels.some(x => has(x, l))),
  tag: (p, v, c) => any(v, t => has(c.L(p).tags, t)),
  jira: (p, v) => any(v, k => p.jira.some(x => x.toLowerCase().startsWith(k))),
  waiting: (p, v, c) => any(v, w => has(c.L(p).waiting, w)),
  branch: (p, v) => any(v, b => has(p.branch, b)),
  base: (p, v) => any(v, b => has(p.base, b)),
  updated: (p, v, c) => age(p.updated, v, c.now),
  created: (p, v, c) => age(p.created, v, c.now),
  age: (p, v, c) => age(p.created, v, c.now),
  merged: (p, v, c) => age(p.merged, v, c.now),
  size: (p, v) => (SIZES[v.toLowerCase()] ? p.adds + p.dels >= SIZES[v.toLowerCase()][0] && p.adds + p.dels < SIZES[v.toLowerCase()][1] : num(p.adds + p.dels, v)),
  files: (p, v) => num(p.files, v),
  comments: (p, v) => num(p.comments, v),
  prio: (p, v, c) => (PRIO[v.toLowerCase()] !== undefined ? (c.L(p).prio || 0) === PRIO[v.toLowerCase()] : num(c.L(p).prio || 0, v)),
  ci: (p, v) => any(v, s => (CI[s] || [s.toUpperCase()]).includes(p.ci)),
  review: (p, v) => any(v, s => (s in REVIEW ? p.review === REVIEW[s] : has(p.review, s))),
  status: (p, v) => any(v, s => p.status === s.replace("-", "_")),
  is: (p, v, c) => any(v, s => IS[s]?.(p, c) ?? p.status === s.replace("-", "_")),
  has: (p, v, c) => any(v, s => HAS[s]?.(p, c) ?? false),
  no: (p, v, c) => !MATCH.has(p, v, c),
};
const IS = {
  draft: p => p.draft, open: p => !p.merged && p.state !== "CLOSED", merged: p => !!p.merged,
  mine: (p, c) => p.author === c.me, stale: (p, c) => !p.merged && (c.now - new Date(p.updated)) / DAY > c.staleDays,
  snoozed: (p, c) => !!c.L(p).snooze, hidden: (p, c) => !!c.L(p).hidden, posted: (p, c) => !!c.L(p).posted,
  conflict: p => p.mergeable === "CONFLICTING", failing: p => CI.failing.includes(p.ci),
};
const HAS = {
  jira: p => p.jira.length > 0, note: (p, c) => !!c.L(p).note, tags: (p, c) => !!c.L(p).tags, waiting: (p, c) => !!c.L(p).waiting,
  review: (p, c) => c.reviews[p.url]?.status === "done", slack: (p, c) => !!c.L(p).posted, labels: p => p.labels.length > 0,
  reviewers: p => p.reviewers.length > 0,
};
export const KEYS = Object.keys(MATCH).filter(k => k !== "text");

export function compile(q) {
  const terms = parse(q);
  return (p, ctx) => terms.every(t => {
    const fn = MATCH[t.key] || ((p, v, c) => MATCH.text(p, `${t.key}:${v}`, c)); // unknown key: plain text
    return fn(p, t.value, ctx) !== t.neg;
  });
}

// Builder helpers: read / replace one key's (non-negated) value in the query text.
export function getTerm(q, key) {
  return parse(q).find(t => t.key === key && !t.neg)?.value ?? "";
}
export function setTerm(q, key, value) {
  const re = new RegExp(`(^|\\s)${key}:("[^"]*"|\\S+)`, "g");
  const rest = q.replace(re, "").replace(/\s+/g, " ").trim();
  if (!value) return rest;
  const v = /\s/.test(value) ? `"${value}"` : value;
  return `${rest} ${key}:${v}`.trim();
}
