"""AI providers for reviews, standup summaries, and interactive sessions.

Coding CLIs (Claude Code, Codex) run headless and locked down, with the PR data on stdin. Any OpenAI-compatible
API (DeepSeek, Grok, OpenAI, Gemini, OpenRouter, Ollama...) works for reviews and summaries; prdash hands it the PR
text directly, so no tools are involved. Gemini CLI isn't a headless option: its plan mode still allows web fetches,
which a malicious PR could use to send code out.
"""
import json, os, shutil, subprocess, tempfile
from urllib.parse import urlsplit

from . import config, integrations

# provider -> (label, headless argv builder, interactive command template)
# Headless builders get (prompt, model, checkout or None) and return argv; PR data arrives on stdin.
# Headless runs always start in an empty temp dir, never the checkout: a PR can ship its own .claude/.codex
# config (hooks, MCP servers) and none of it should load. Claude may still read the checkout, read-only, via --add-dir.
CLIS = {
    "claude": ("Claude",
               lambda prompt, model, checkout: ["-p", prompt, "--permission-mode", "dontAsk", *(["--model", model] if model else []),
                                                # user settings only, no MCP servers, and no shell or web even if a rule allowed them
                                                "--setting-sources", "user", "--strict-mcp-config",
                                                "--disallowedTools", "Bash", "WebFetch", "WebSearch",
                                                *(["--add-dir", checkout, "--allowedTools", f"Read(/{checkout}/**)"] if checkout else [])],
               "{cmd} --permission-mode plan {prompt}"),
    "codex": ("Codex",
              lambda prompt, model, checkout: ["exec", "-s", "read-only", "--skip-git-repo-check", *(["-m", model] if model else []), prompt],
              "{cmd} --sandbox read-only {prompt}"),
}
API_NAMES = {"deepseek": "DeepSeek", "x.ai": "Grok", "openai": "OpenAI", "openrouter": "OpenRouter", "11434": "Ollama",
             "groq": "Groq", "mistral": "Mistral", "together": "Together"}


def provider():
    p = config.get("ai_provider")
    return p if p in CLIS else "api"


def name():
    """What buttons say: "Claude", "Codex", "Grok"..."""
    if provider() in CLIS:
        return CLIS[provider()][0]
    base = config.get("api_base_url")
    return next((n for k, n in API_NAMES.items() if k in base), urlsplit(base).hostname or "AI")


def _cmd():
    return config.get("ai_cmd").strip() or provider()


def interactive(prompt, cwd):
    """argv for a terminal session, or None when the provider has none (plain APIs without a custom command)."""
    template = config.get("ai_interactive_cmd").strip() or (CLIS[provider()][2] if provider() in CLIS else "")
    if not template:
        return None
    return config.expand(config.get("terminal"), dir=cwd) + config.expand(template, cmd=_cmd(), prompt=prompt)


def complete(prompt, data, *, fast=False, checkout=None, timeout=900):
    """Run one non-interactive request: instructions in `prompt`, the PR material in `data`. Returns the text."""
    model = config.get("ai_fast_model" if fast else "ai_model").strip()
    if provider() not in CLIS:
        return _api(prompt, data, model or config.get("api_model"), timeout)
    argv = config.expand(_cmd()) + CLIS[provider()][1](prompt, model, str(checkout) if checkout and provider() == "claude" else None)
    cwd = tempfile.mkdtemp(prefix="prdash-ai-")
    out_file = None
    if provider() == "codex":  # codex prints progress to stdout; the answer goes to a file
        out_file = os.path.join(tempfile.mkdtemp(prefix="prdash-ai-"), "answer.txt")
        argv[-1:-1] = ["-o", out_file]
    r = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    text = (open(out_file).read() if out_file and os.path.exists(out_file) else r.stdout).strip()
    if r.returncode or not text:
        raise RuntimeError((r.stderr or r.stdout or f"{name()} returned nothing").strip()[-600:])
    return text


def _api(prompt, data, model, timeout):
    base = config.get("api_base_url").rstrip("/")
    if not base or not model:
        raise ValueError("Set the API base URL and model in Settings → AI.")
    key = config.get("api_key")
    body = json.dumps({"model": model, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": data}]}).encode()
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    out = integrations._request(f"{base}/chat/completions", headers, body, timeout=timeout)
    try:
        return out["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError):
        raise RuntimeError(f"Unexpected response from {name()}: {json.dumps(out)[:300]}") from None
