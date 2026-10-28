// Cards and table rows render as HTML strings, one x-html per column/table, with click delegation via data-act.
// Alpine per-element bindings cost ~10ms per card at this density; strings render 200 cards in a few ms.

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const icon = (name, cls = "") => `<svg class="ic ${cls}"><use href="/static/icons.svg#${name}"/></svg>`;
const STATE_WORD = s => s.toLowerCase().replace("_", " ");

function jiraChip(a, k) {
  const j = a.jira(k), href = a.jiraHref(k);
  const inner = `<span>${esc(k)}</span>${j ? `<span class="jstat">${esc(j.status)}</span>` : ""}`;
  const title = j ? `${j.summary || ""} · ${j.assignee || "unassigned"}` : k;
  return href ? `<a class="chip jira jc-${esc(j?.category || "")}" href="${esc(href)}" target="_blank" title="${esc(title)}">${inner}</a>`
    : `<span class="chip jira">${inner}</span>`;
}

export function chips(a, p, { labels = false } = {}) {
  const l = a.L(p);
  return [
    ...p.jira.map(k => jiraChip(a, k)),
    l.waiting ? `<span class="chip wait">${icon("hourglass")}<span>${esc(l.waiting)}</span></span>` : "",
    l.snooze ? `<span class="chip snz">${icon("clock")}<span>until ${esc(l.snooze)}</span></span>` : "",
    ...a.tags(p).map(t => `<span class="chip tag">${esc(t)}</span>`),
    ...(labels ? p.labels.map(x => `<span class="chip">${esc(x)}</span>`) : []),
    ...a.flags(p).map(([short, tone, ic, long]) => `<span class="chip flag ${tone}" title="${esc(long)}">${icon(ic)}<span>${esc(short)}</span></span>`),
  ].join("");
}

const avatar = (a, login, cls = "", title = login) =>
  `<img class="av ${cls}" src="${esc(a.avatar(login))}" loading="lazy" alt="" title="${esc(title)}" onerror="this.style.visibility='hidden'">`;
// Selection, focus, and the open PR are painted as classes by app.paint(), so moving around never re-renders.
const PICK = `<button class="pick" data-act="pick" title="Select (x, shift-click for a range)">${icon("git-pull-request", "i-off")}${icon("square-check", "i-on")}</button>`;

export function card(a, p) {
  const l = a.L(p), r = a.review(p), prio = l.prio || 0;
  const cls = (l.hidden || l.snooze) ? "card hid" : "card";
  const chipHtml = chips(a, p);
  return `<article class="${cls}" data-url="${esc(p.url)}">
  <div class="card-top">
    ${PICK}
    <span class="repo">${esc(p.repo)}</span><span class="num">#${p.number}</span>
    ${prio ? `<span class="prio p${prio}">${"●".repeat(prio)}</span>` : ""}
    <span class="grow"></span>
    ${r.status === "running" ? icon("sparkles", "state spin") : r.status === "done" ? `<span title="Claude reviewed">${icon("sparkles", "state ok")}</span>` : ""}
    ${l.posted ? `<span title="Announced in Slack">${icon("hash", "state")}</span>` : ""}
    <i class="dot ${p.ci || ""}" title="CI: ${esc(p.ci || "no checks")}"></i>
  </div>
  <div class="card-title">${esc(p.title)}</div>
  ${chipHtml ? `<div class="chips">${chipHtml}</div>` : ""}
  ${l.note ? `<div class="card-note">${esc(l.note)}</div>` : ""}
  <div class="card-foot">
    ${p.author !== a.data?.me ? avatar(a, p.author) : ""}
    <span class="reviewers">${p.reviewers.map(x => avatar(a, x.who, `sm rv-${x.state}`, `${x.who}: ${STATE_WORD(x.state)}`)).join("")}</span>
    <span class="grow"></span>
    <span class="meta"><span class="size"><span class="a">+${p.adds}</span> <span class="d">−${p.dels}</span></span>
      <span class="t" title="Updated ${esc(new Date(p.updated).toLocaleString())}">${a.ago(p.merged || p.updated)}</span></span>
    <span class="hover-acts">
      ${p.merged ? "" : `<button class="mini" data-act="announce" title="Announce in Slack (p)">${icon("hash")}</button>`}
      <button class="mini" data-act="editor" title="Open in editor (e)">${icon("code")}</button>
      <button class="mini accent" data-act="claude" title="Open in Claude (c)">${icon("sparkles")}Claude</button>
    </span>
  </div>
</article>`;
}

export function row(a, p) {
  const l = a.L(p), prio = l.prio || 0;
  const cls = (l.hidden || l.snooze) ? "hid" : "";
  return `<tr class="${cls}" data-url="${esc(p.url)}">
  <td class="pickcol">${PICK}</td>
  <td><span class="prio p${prio}">${"●".repeat(prio) || "·"}</span></td>
  <td class="repo">${esc(p.repo)}</td>
  <td class="ttl"><span class="num">#${p.number}</span><span>${esc(p.title)}</span>${chips(a, p)}</td>
  <td><span class="pill s-${p.status}"><i class="sdot"></i><span>${esc(a.statusLabel(p.status))}</span></span></td>
  <td><span class="author">${avatar(a, p.author, "sm")}<span>${esc(p.author)}</span></span></td>
  <td><i class="dot ${p.ci || ""}" title="${esc(p.ci || "no checks")}"></i></td>
  <td class="small rv-${p.review || ""}">${a.reviewLabel(p)}</td>
  <td class="size"><span class="a">+${p.adds}</span> <span class="d">−${p.dels}</span></td>
  <td class="t">${a.ago(p.created)}</td>
  <td class="t ${!p.merged && a.days(p.updated) > 14 ? "stale" : ""}">${a.ago(p.updated)}</td>
  <td class="note-cell">${esc(l.note)}</td>
  <td><button class="mini accent icon-only" data-act="claude" title="Open in Claude">${icon("sparkles")}</button></td>
</tr>`;
}
