"""Settings: one schema drives defaults, env-var overrides, validation, and the settings page."""
import os, shlex, shutil
from pathlib import Path

from . import db

DEFAULT_REVIEW_PROMPT = ""


def _first_installed(candidates, fallback):
    return next((cmd for exe, cmd in candidates if shutil.which(exe)), fallback)


def default_terminal():
    return _first_installed([
        ("kitty", "kitty --directory {dir}"),
        ("wezterm", "wezterm start --cwd {dir} --"),
        ("ghostty", "ghostty --working-directory={dir} -e"),
        ("alacritty", "alacritty --working-directory {dir} -e"),
        ("foot", "foot --working-directory={dir}"),
        ("konsole", "konsole --workdir {dir} -e"),
        ("gnome-terminal", "gnome-terminal --working-directory={dir} --"),
        ("xterm", "xterm -e"),
    ], "xterm -e")


def default_editor():
    return _first_installed([("code", "code {dir}"), ("cursor", "cursor {dir}"), ("zed", "zed {dir}"),
                             ("nvim", "{terminal} nvim ."), ("vim", "{terminal} vim .")], "{terminal} vi .")


def default_ai():
    return next((p for p in ("claude", "codex", "gemini") if shutil.which(p)), "api")


def default_notify():
    return _first_installed([("notify-send", "notify-send --app-name=prdash prdash {text}"),
                             ("terminal-notifier", "terminal-notifier -title prdash -message {text}")], "")


# key: (default, kind, section, label, help). kind: str | text | int | bool | secret | choice:a,b
FIELDS = {
    "orgs": ("", "str", "GitHub", "Organizations", "Comma-separated. Leave empty to use every org you belong to."),
    "extra_query": ("", "str", "GitHub", "Extra search filter",
                    "Added to every GitHub search, e.g. -author:app/dependabot -label:wip"),
    "team_view": (True, "bool", "GitHub", "Team view", "Track every open PR in your orgs. Turn off for very large orgs."),
    "merged_days": (14, "int", "GitHub", "Merged column (days)", "How far back merged PRs are kept."),
    "poll_seconds": (60, "int", "GitHub", "Sync every (seconds)", "Background refresh interval. Minimum 20."),
    "merge_method": ("squash", "choice:squash,merge,rebase", "GitHub", "Merge method", ""),
    "stale_days": (5, "int", "GitHub", "Nudge after (days)", "In-review PRs quiet this long get a nudge chip."),
    "repo_dirs": ("~/code:~/src:~/projects:~/Documents", "str", "Local", "Clone folders",
                  "Colon-separated, searched in order for <dir>/<repo>. New clones go into the first."),
    "worktree_dir": ("~/worktrees", "str", "Local", "Worktree folder", "Per-PR worktrees are created here as <repo>-<number>."),
    "terminal": (default_terminal, "str", "Local", "Terminal command",
                 "{dir} is replaced with the folder; the command to run is appended. e.g. kitty --directory {dir}"),
    "editor": (default_editor, "str", "Local", "Editor command",
               "e.g. code {dir}, or {terminal} nvim . to open a terminal editor."),
    "ai_provider": (default_ai, "choice:claude,codex,gemini,api", "AI", "Provider",
                    "claude, codex, or gemini use that CLI, read-only. api is any OpenAI-compatible API: DeepSeek, Grok, OpenAI, OpenRouter, Ollama."),
    "ai_cmd": ("", "str", "AI", "CLI path", "Leave empty to use the provider's own command (claude, codex, gemini)."),
    "ai_model": ("", "str", "AI", "Review model", "Empty uses the CLI's default (or the API model below)."),
    "ai_fast_model": (lambda: "haiku" if get("ai_provider") == "claude" else "", "str", "AI", "Summary model",
                      "A small, fast model for standup summaries, e.g. haiku. Empty uses the review model."),
    "ai_interactive_cmd": ("", "str", "AI", "Interactive command",
                           "What Open in… runs in your terminal. {cmd} is the CLI, {prompt} the PR context. Empty uses the provider's "
                           "default; set one (e.g. aider --model deepseek/deepseek-chat --message {prompt}) to pair an API model with a terminal tool."),
    "api_base_url": ("", "str", "AI", "API base URL",
                     "OpenAI-compatible: https://api.deepseek.com, https://api.x.ai/v1, https://api.openai.com/v1, "
                     "https://openrouter.ai/api/v1, http://localhost:11434/v1 (Ollama). PR text and diffs are sent here."),
    "api_key": ("", "secret", "AI", "API key", "Not needed for a local Ollama."),
    "api_model": ("", "str", "AI", "API model", "e.g. deepseek-chat, grok-4, gpt-5-mini, qwen3"),
    "review_prompt": (DEFAULT_REVIEW_PROMPT, "text", "AI", "Extra review instructions",
                      "Appended to every review, e.g. house style rules or a skill to use."),
    "notify_browser": (True, "bool", "Notifications", "Browser notifications", "While a prdash tab is open."),
    "notify_cmd": (default_notify, "str", "Notifications", "System notification command",
                   "Runs for every inbox item, even with no prdash tab open. {text} is the message. Clear it to turn off."),
    "slack_token": ("", "secret", "Slack", "User token",
                    "xoxp-… with chat:write, search:read, channels:history, groups:history, users:read."),
    "slack_channel": ("", "str", "Slack", "Channel", "Where PRs get announced, e.g. #dev"),
    "slack_template": ("PR to {title}: {url}", "str", "Slack", "Announcement",
                       "{title} (lowercased, ticket prefix removed), {raw_title}, {url}, {repo}, {number}"),
    "jira_url": ("", "str", "Jira", "Jira URL", "e.g. https://yourco.atlassian.net. Enables ticket links."),
    "jira_email": ("", "str", "Jira", "Account email", "For ticket status. Needs an API token too."),
    "jira_token": ("", "secret", "Jira", "API token", "id.atlassian.com → Security → API tokens."),
    "jira_on_merge": ("", "str", "Jira", "On merge, move ticket to",
                      "Status name, e.g. Done. Applies to your PRs. Empty turns it off."),
}
# older env var names keep working
ENV_ALIASES = {"jira_url": "PRDASH_JIRA", "merge_method": "PRDASH_MERGE", "ai_cmd": "PRDASH_CLAUDE_CMD", "ai_fast_model": "PRDASH_SUMMARY_MODEL"}


def _parse(kind, value):
    if kind == "int":
        return int(value)
    if kind == "bool":
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if kind.startswith("choice:"):
        if value not in kind[7:].split(","):
            raise ValueError(f"must be one of {kind[7:]}")
    return str(value)


def get(key):
    default, kind = FIELDS[key][:2]
    stored = db.get("setting:" + key)
    if stored is not None:
        return stored
    env = os.environ.get(ENV_ALIASES.get(key, "PRDASH_" + key.upper()))
    if env is not None:
        return _parse(kind, env)
    return default() if callable(default) else default


def put(key, value):
    if key not in FIELDS:
        raise ValueError(f"unknown setting {key}")
    kind = FIELDS[key][1]
    if value is None:  # reset to default
        return db.put("setting:" + key, None)
    value = _parse(kind, value)
    if key == "poll_seconds":
        value = max(20, value)
    db.put("setting:" + key, value)


def public():
    """Settings for the page. Secrets are never sent back, only whether they're set."""
    out = []
    for key, (default, kind, section, label, help_) in FIELDS.items():
        value = get(key)
        item = {"key": key, "kind": kind, "section": section, "label": label, "help": help_}
        if kind == "secret":
            item["set"] = bool(value)
        else:
            item["value"] = value
            item["custom"] = db.get("setting:" + key) is not None
        out.append(item)
    return out


def expand(template, **subs):
    """Command template -> argv. Each {name} is replaced inside its own argv element, never re-split,
    so values can't inject extra arguments. A bare {terminal} token expands to the terminal command."""
    argv = []
    for tok in shlex.split(template):
        if tok == "{terminal}":
            argv += expand(get("terminal"), **subs)
            continue
        for k, v in subs.items():
            tok = tok.replace("{" + k + "}", str(v))
        argv.append(tok)
    return argv


def paths(key):
    return [Path(p).expanduser() for p in get(key).split(":") if p]
