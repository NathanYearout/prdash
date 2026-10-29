// Cards and table rows render as HTML strings, one x-html per column/table, with click delegation via data-act.
// Alpine per-element bindings cost ~10ms per card at this density; strings render 200 cards in a few ms.

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const SPRITE = document.documentElement.dataset.icons || "/static/icons.svg"; // versioned by the server
const icon = (name, cls = "") => `<svg class="ic ${cls}"><use href="${SPRITE}#${name}"/></svg>`;
const STATE_WORD = s => s.toLowerCase().replace("_", " ");
const PRIO = ["", "low", "medium", "high"];
// "[ABC-1] Fix x" / "ABC-1: Fix x" -> "Fix x" when the key is already shown as a chip
const TICKET_PREFIX = /^\s*(\[[A-Z][A-Z0-9]+-\d+\]|[A-Z][A-Z0-9]+-\d+:)\s*/;
export const shortTitle = p => (p.jira.length ? p.title.replace(TICKET_PREFIX, "") : p.title);

function jiraChip(a, k) {
  const j = a.jira(k), href = a.jiraHref(k);
  const inner = `<span>${esc(k)}</span>${j ? `<span class="jstat">${esc(j.status)}</span>` : ""}`;
  const title = j ? `${j.summary || ""} · ${j.assignee || "unassigned"}` : k;
  return href ? `<a class="chip jira jc-${esc(j?.category || "")}" href="${esc(href)}" target="_blank" title="${esc(title)}">${inner}</a>`
    : `<span class="chip jira">${inner}</span>`;
}

function chips(a, p) {
  const l = a.L(p);
  return [
    ...p.jira.map(k => jiraChip(a, k)),
    l.waiting ? `<span class="chip wait">${icon("hourglass")}<span>${esc(l.waiting)}</span></span>` : "",
    l.snooze ? `<span class="chip snz">${icon("clock")}<span>until ${esc(l.snooze)}</span></span>` : "",
    ...a.tags(p).map(t => `<span class="chip tag" style="${a.tagStyle(t)}">${esc(t)}</span>`),
    ...a.flags(p).map(([short, tone, ic, long, href]) => {
      const tag = href ? "a" : "span", link = href ? ` href="${esc(href)}" target="_blank" rel="noopener"` : "";
      return `<${tag} class="chip flag ${tone}" title="${esc(long)}"${link}>${ic ? icon(ic) : ""}<span>${esc(short)}</span></${tag}>`;
    }),
  ].join("");
}

const avatar = (a, login, cls = "", title = login) =>
  `<img class="av ${cls}" src="${esc(a.avatar(login))}" loading="lazy" alt="" title="${esc(title)}" onerror="this.style.visibility='hidden'">`;
// Selection, focus, and the open PR are painted as classes by app.paint(), so moving around never re-renders.
const PICK = `<button class="pick" data-act="pick" title="Select (x, shift-click for a range)">${icon("git-pull-request", "i-pr")}${icon("square", "i-off")}${icon("square-check", "i-on")}</button>`;

export function card(a, p) {
  const l = a.L(p), r = a.review(p), prio = l.prio || 0;
  const urgent = p.urgent && !p.merged, heat = a.heatOf(p);
  const cls = ["card", (l.hidden || l.snooze) && "hid", urgent && "urgent", heat && "heat"].filter(Boolean).join(" ");
  const chipHtml = chips(a, p);
  const html = `<article class="${cls}" data-url="${esc(p.url)}"${heat ? ` style="${heat[0]}"` : ""}>
  <div class="card-top">
    ${PICK}
    <span class="repo">${esc(p.repo)}</span><span class="num">#${p.number}</span>
    ${prio ? `<span class="prio p${prio}" title="Priority: ${PRIO[prio]}">${"●".repeat(prio)}</span>` : ""}
    <span class="grow"></span>
    ${r.status === "running" ? icon("sparkles", "state spin") : r.status === "done" ? `<span title="${esc(a.cfg.ai_name)} reviewed">${icon("sparkles", "state ok")}</span>` : ""}
    ${l.posted ? `<span title="Announced in Slack">${icon("hash", "state")}</span>` : ""}
    ${heat ? `<span class="age" title="Opened ${esc(new Date(p.created).toLocaleString())}">${heat[1]} old</span>`
      : `<span class="t" title="${p.merged ? "Merged" : "Updated"} ${esc(new Date(p.merged || p.updated).toLocaleString())}">${a.ago(p.merged || p.updated)}</span>`}
    <i class="dot ${p.ci || ""}" title="CI: ${esc(p.ci || "no checks")}"></i>
  </div>
  <div class="card-title" title="${esc(p.title)}">${esc(shortTitle(p))}</div>
  ${chipHtml ? `<div class="chips">${chipHtml}</div>` : ""}
  ${l.note ? `<div class="card-note">${esc(l.note)}</div>` : ""}
  <div class="card-foot">
    ${p.author !== a.data?.me ? avatar(a, p.author) : ""}
    <span class="reviewers">${p.reviewers.map(x => avatar(a, x.who, `sm rv-${x.state}`, `${x.who}: ${STATE_WORD(x.state)}`)).join("")}</span>
    <span class="grow"></span>
    <span class="meta size" title="${p.files} files changed"><span class="a">+${p.adds}</span> <span class="d">−${p.dels}</span></span>
    <span class="hover-acts">
      ${p.merged ? "" : `<button class="mini" data-act="announce" title="Announce in Slack (p)">${icon("hash")}</button>`}
      <button class="mini" data-act="editor" title="Open in editor (e)">${icon("code")}</button>
      ${a.cfg.ai_terminal ? `<button class="mini accent" data-act="agent" title="Open in ${esc(a.cfg.ai_name)} (c)">${icon("sparkles")}${esc(a.cfg.ai_name)}</button>` : ""}
    </span>
  </div>
</article>`;
  // urgent cards sit in a ring whose highlight travels around the border (see .urgent-ring in style.css)
  return urgent ? `<div class="urgent-ring">${html}</div>` : html;
}

export function row(a, p) {
  const l = a.L(p), prio = l.prio || 0;
  const heat = a.heatOf(p);
  const cls = [(l.hidden || l.snooze) && "hid", p.urgent && !p.merged && "urgent", heat && "heat"].filter(Boolean).join(" ");
  return `<tr class="${cls}" data-url="${esc(p.url)}"${heat ? ` style="${heat[0]}"` : ""}>
  <td class="pickcol">${PICK}</td>
  <td><span class="prio p${prio}">${"●".repeat(prio) || "·"}</span></td>
  <td class="repo">${esc(p.repo)}</td>
  <td class="ttl" title="${esc(p.title)}"><span class="num">#${p.number}</span><span>${esc(shortTitle(p))}</span>${chips(a, p)}</td>
  <td><span class="pill s-${p.status}"><i class="sdot"></i><span>${esc(a.statusLabel(p.status))}</span></span></td>
  <td><span class="author">${avatar(a, p.author, "sm")}<span>${esc(p.author)}</span></span></td>
  <td><i class="dot ${p.ci || ""}" title="${esc(p.ci || "no checks")}"></i></td>
  <td class="small rv-${p.review || ""}">${a.reviewLabel(p)}</td>
  <td class="size"><span class="a">+${p.adds}</span> <span class="d">−${p.dels}</span></td>
  ${heat ? `<td class="t age">${heat[1]}</td>` : `<td class="t">${a.ago(p.created)}</td>`}
  <td class="t ${!p.merged && a.days(p.updated) > 14 ? "stale" : ""}">${a.ago(p.updated)}</td>
  <td class="note-cell">${esc(l.note)}</td>
  <td>${a.cfg.ai_terminal ? `<button class="mini accent icon-only" data-act="agent" title="Open in ${esc(a.cfg.ai_name)}">${icon("sparkles")}</button>` : ""}</td>
</tr>`;
}

// GitHub markdown for PR descriptions and comments: everything is escaped first, then a small subset is turned back into tags
// (headings, lists and task lists, quotes, code, bold, italics, http(s) links). Anything else shows as plain text.
export function md(src) {
  const refs = {};
  src = String(src ?? "").replace(/^\[([^\]]+)\]:\s*(https?:\/\/\S+)\s*$/gm, (_, k, u) => ((refs[k.toLowerCase()] = u), ""));
  const link = (text, url) => `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>`;
  // Code and links become finished HTML behind \0n\0 placeholders before anything else runs, so no later pass
  // ever sees (and rewrites inside) markup an earlier pass produced.
  const inline = s => {
    const done = [], keep = html => `\0${done.push(html) - 1}\0`;
    s = String(s).replace(/\0/g, "")
      .replace(/`([^`]+)`/g, (_, c) => keep(`<code>${esc(c)}</code>`))
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s()[\]]+)\)/g, (_, t, u) => keep(link(t, u)))
      .replace(/\[([^\]]+)\](?!\()/g, (m, t) => refs[t.toLowerCase()] ? keep(link(t, refs[t.toLowerCase()])) : m)
      .replace(/(^|[\s(])(https?:\/\/[^\s<>()[\]]+)/g, (_, pre, u) => pre + keep(link(u, u)));
    return esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^\w*])\*([^*\s][^*]*?)\*(?![\w*])/g, "$1<i>$2</i>") // * only: _ shows up inside URLs and names
      .replace(/\0(\d+)\0/g, (_, i) => done[i]);
  };
  const out = [], lines = src.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/^\s*```/.test(l)) {
      const code = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
    } else if (/^#{1,6}\s/.test(l)) out.push(`<h5>${inline(l.replace(/^#+\s*/, ""))}</h5>`);
    else if (/^\s*>/.test(l)) {
      const q = [l];
      while (i + 1 < lines.length && /^\s*>/.test(lines[i + 1])) q.push(lines[++i]);
      const body = q.map(x => x.replace(/^\s*>\s?/, "")).join("\n").trim();
      if (body) out.push(`<blockquote>${md(body)}</blockquote>`);
    } else if (/^\s*([-*+]|\d+[.)])\s/.test(l)) {
      const items = [l];
      while (i + 1 < lines.length && /^\s*([-*+]|\d+[.)])\s/.test(lines[i + 1])) items.push(lines[++i]);
      const tag = /^\s*\d/.test(l) ? "ol" : "ul";
      out.push(`<${tag}>${items.map(x => {
        const t = x.replace(/^\s*([-*+]|\d+[.)])\s+/, ""), task = t.match(/^\[([ xX])\]\s*/);
        return task ? `<li class="task${task[1] !== " " ? " done" : ""}"><i></i>${inline(t.slice(task[0].length))}</li>` : `<li>${inline(t)}</li>`;
      }).join("")}</${tag}>`);
    } else if (l.trim()) {
      const para = [l];
      while (i + 1 < lines.length && lines[i + 1].trim() && !/^\s*(```|#{1,6}\s|>|[-*+]\s|\d+[.)]\s)/.test(lines[i + 1])) para.push(lines[++i]);
      out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    }
  }
  return out.join("");
}
