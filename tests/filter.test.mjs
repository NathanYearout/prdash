// node --test tests/  (Node 18+)
import { test } from "node:test";
import assert from "node:assert/strict";
import { compile, flipTerm, getTerm, parse, setTerm } from "../prdash/web/js/filter.js";

const now = Date.parse("2026-10-27T12:00:00Z");
const pr = (o = {}) => ({
  url: "u1", number: 7, title: "[ABC-1] Fix payment link", repo: "web-app", owner: "acme", author: "alice", branch: "fix/ABC-1",
  base: "main", jira: ["ABC-1"], labels: ["bug"], reviewers: [{ who: "bob", state: "APPROVED" }, { who: "carol", state: "REQUESTED" }],
  status: "approved", review: "APPROVED", ci: "SUCCESS", draft: false, merged: null, state: "OPEN", mergeable: "MERGEABLE",
  adds: 400, dels: 150, files: 12, comments: 3, created: "2026-10-01T00:00:00Z", updated: "2026-10-26T00:00:00Z", ...o,
});
const local = { u1: { tags: "blocked, qa", note: "ask dana", waiting: "QA", prio: 2 } };
const ctx = { L: p => local[p.url] || {}, me: "alice", reviews: {}, staleDays: 5, now };
const m = (q, p = pr()) => compile(q)(p, ctx);

test("people", () => {
  assert.ok(m("author:ali")); assert.ok(m("author:zed,alice")); assert.ok(!m("author:bob")); assert.ok(m("author:@me"));
  assert.ok(m("reviewer:bob") && m("reviewer:carol")); assert.ok(m("requested:carol") && !m("requested:bob"));
  assert.ok(m("approved-by:bob") && !m("approved-by:carol")); assert.ok(m("-author:bob"));
});
test("time windows", () => {
  assert.ok(m("updated:<3d")); assert.ok(!m("updated:<12h")); assert.ok(m("created:>2w")); assert.ok(!m("created:<1w"));
  assert.ok(!m("merged:<7d")); assert.ok(m("merged:<7d", pr({ merged: "2026-10-25T00:00:00Z" })));
  assert.ok(m("updated:<3"), "incomplete spec while typing doesn't hide everything");
});
test("numbers, sizes, states", () => {
  assert.ok(m("size:>500")); assert.ok(m("size:l")); assert.ok(!m("size:xl")); assert.ok(m("files:>=12")); assert.ok(m("comments:3"));
  assert.ok(m("ci:passing") && !m("ci:failing")); assert.ok(m("review:approved")); assert.ok(m("is:approved") && !m("is:draft"));
  assert.ok(m("is:stale", pr({ updated: "2026-10-01T00:00:00Z" })) && !m("is:stale")); assert.ok(m("prio:>=2") && m("prio:medium"));
});
test("local fields and text", () => {
  assert.ok(m("tag:qa") && m("waiting:qa") && m("has:note") && !m("-has:note")); assert.ok(m("jira:abc") && !m("jira:xyz"));
  assert.ok(m("payment") && m('"payment link"') && !m("refund")); assert.ok(m("dana"), "text searches your note");
  assert.ok(m("label:bug repo:web author:alice"), "terms AND together"); assert.ok(!m("label:bug author:bob"));
});
test("new since your review", () => {
  const p = o => pr({ author: "bob", head: "h2", reviewers: [{ who: "alice", state: "APPROVED", commit: "h1" }], ...o });
  assert.ok(m("is:new-since-review", p())); assert.ok(!m("is:new-since-review", p({ head: "h1" })), "nothing new");
  assert.ok(!m("is:new-since-review", p({ author: "alice" })), "not your own"); assert.ok(!m("is:new-since-review", p({ merged: "x" })));
  assert.ok(!m("is:new-since-review"), "never reviewed");
});
test("merge blockers and auto-merge", () => {
  assert.ok(m("is:blocked", pr({ blocker: "Needs approval" })) && !m("is:blocked"));
  assert.ok(m("is:behind", pr({ merge_state: "BEHIND" })) && !m("is:behind", pr({ merge_state: "BLOCKED" })));
  assert.ok(m("is:auto-merge", pr({ auto_merge: { method: "squash" } })) && !m("is:auto-merge"));
});
test("review when green", () => {
  const l = { u1: { request_when_green: { teams: ["devs"], users: [] } } };
  assert.ok(compile("is:requesting")(pr(), { ...ctx, L: p => l[p.url] || {} })); assert.ok(!m("is:requesting"));
});
test("builder helpers", () => {
  assert.equal(setTerm("foo author:a", "author", "b"), "foo author:b");
  assert.equal(setTerm("foo author:a", "author", ""), "foo");
  assert.equal(setTerm("-author:x", "author", "y"), "-author:x author:y", "negated terms are left alone");
  assert.equal(setTerm("", "waiting", "on qa"), 'waiting:"on qa"');
  assert.equal(getTerm('a waiting:"on qa"', "waiting"), "on qa");
  assert.deepEqual(parse("-is:draft x"), [{ neg: true, key: "is", value: "draft" }, { neg: false, key: "text", value: "x" }]);
  assert.equal(flipTerm("x author:a", "author", "b"), "x author:b", "one author at a time");
  assert.equal(flipTerm("x author:b", "author", "b"), "x", "same value turns it off");
  assert.equal(flipTerm("is:urgent", "is", "stale"), "is:urgent is:stale", "is: stacks");
  assert.equal(flipTerm("is:urgent is:stale", "is", "urgent"), "is:stale");
});
