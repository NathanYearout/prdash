import { test } from "node:test";
import assert from "node:assert/strict";
globalThis.document = { documentElement: { dataset: {} } }; // render.js reads the icon sprite path at import
const { md } = await import("../prdash/web/js/render.js");

test("escapes HTML before adding any markup", () => {
  assert.equal(md('<img src=x onerror=alert(1)> **hi**'), "<p>&lt;img src=x onerror=alert(1)&gt; <b>hi</b></p>");
  assert.equal(md("[x](javascript:alert(1))"), "<p>[x](javascript:alert(1))</p>");
  assert.ok(!md('[a](https://e.com/"onclick="x)').includes('"onclick'));
});

test("never rewrites inside markup it already produced", () => {
  // each of these used to nest a second <a> inside the first one's href, breaking out of the attribute
  const attacks = ["[x](https://a.com/[k])\n\n[k]: https://b.com/onmouseover=alert(1)//",
                   "[x](https://a.com/(https://b.com/onmouseover=alert(1)//)",
                   "`https://a.com/x` [k] https://c.com/[k]\n\n[k]: https://b.com/onfocus=alert(1)",
                   "**[x](https://a.com/**onmouseover=alert(1)**)**"];
  for (const a of attacks) {
    const tags = md(a).match(/<[^>]*>/g) || [];
    for (const t of tags) assert.match(t, /^<\/?(p|a|code|b|i|br)( href="https?:\/\/[^"<>]*" target="_blank" rel="noopener")?>$/, `${a} -> ${t}`);
  }
  assert.equal(md("`a*b*c` **bold [x](https://e.com)**"),
    '<p><code>a*b*c</code> <b>bold <a href="https://e.com" target="_blank" rel="noopener">x</a></b></p>');
});

test("renders the parts PR descriptions use", () => {
  const out = md("# Description\n\nUses `two-only` on **Auto Withdraw**.\n\n- [x] Fix it\n- [ ] Test\n\n[PL-83]\n\n[PL-83]: https://jira.example.com/PL-83");
  assert.match(out, /<h5>Description<\/h5>/);
  assert.match(out, /<code>two-only<\/code> on <b>Auto Withdraw<\/b>/);
  assert.match(out, /<li class="task done"><i><\/i>Fix it<\/li>/);
  assert.match(out, /<a href="https:\/\/jira.example.com\/PL-83" target="_blank" rel="noopener">PL-83<\/a>/);
  assert.ok(!out.includes("]: https"));
  assert.equal(md("```\n<b>x</b>\n```"), "<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>");
  assert.equal(md("> quoted\n> more"), "<blockquote><p>quoted<br>more</p></blockquote>");
  assert.equal(md("snake_case_name and *em*"), "<p>snake_case_name and <i>em</i></p>");
  assert.equal(md("see https://e.com/a_b_c"), '<p>see <a href="https://e.com/a_b_c" target="_blank" rel="noopener">https://e.com/a_b_c</a></p>');
});
