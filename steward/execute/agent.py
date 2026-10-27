"""One Claude Code headless run inside the clone, for the judgement calls.

The agent is given the work order and the repo's own `slopstopper-triage`
skill; the skill is slopstopper's and arrives with the install, so the
steward teaches the agent nothing about checks. The run is boxed: no
network tools, no git write, a dollar and a turn cap, and whatever it
leaves in the tree is what the steward commits.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from steward import gh
from steward.config import Config
from steward.prompts import RESULT_FILE
from steward.workspace import Workspace

SKILLS = ("slopstopper-triage", "slopstopper-install")
ALLOWED_TOOLS = (
    "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep",
    "Bash(slopstopper *)", "Bash(task ss:*)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
    "Bash(git grep*)", "Bash(cat *)", "Bash(ls *)", "Bash(grep *)", "Bash(find *)", "Bash(head *)", "Bash(tail *)",
    "Bash(wc *)", "Bash(python3 -m lizard*)", "Bash(mkdir *)", "Bash(rm .ss/reports*)",
)
# Named as well as left off the allow-list, so a permission prompt the
# headless run would auto-answer never lets one through.
DISALLOWED_TOOLS = (
    "Bash(git push*)", "Bash(git commit*)", "Bash(git remote*)", "Bash(git config*)", "Bash(gh *)",
    "Bash(curl *)", "Bash(wget *)", "Bash(npx *)", "Bash(npm *)", "Bash(pip *)", "Bash(pipx *)",
    "WebFetch", "WebSearch",
)
# What the agent's process may see. Everything that can write to GitHub
# stays with the steward; the model key has to go through, since the
# agent is the model, and that is the one secret the run grants it.
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
            "NODE_OPTIONS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS", "CI", "RUNNER_TEMP")


@dataclass
class AgentResult:
    ran: bool
    outcome: str = "skipped"
    summary: str = ""
    category: str = ""
    verified: list[str] = field(default_factory=list)
    upstream: dict | None = None
    notes: str = ""
    cost_usd: float | None = None
    turns: int | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def available() -> str | None:
    """Why the agent cannot run, or None when it can."""
    if shutil.which("claude") is None:
        return "claude CLI not on PATH"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")):
        return "no ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN"
    return None


def ensure_skills(ws: Workspace, pinned: str | None, slopstopper_repo: str) -> str:
    """The skills the agent reads. The install ships them, so usually they are
    there; a `--no-skills` install gets them fetched at the pinned release's
    tag (main only when that tag does not exist, and then the whole tree
    from main, so the files agree with each other) into the clone, excluded
    from git so the PR does not carry them. Returns where they came from."""
    root = ws.path / ".claude" / "skills"
    if all((root / s / "SKILL.md").is_file() for s in SKILLS):
        return "present"
    ref = _resolve_ref(slopstopper_repo, f"v{pinned}" if pinned else "main")
    for skill in SKILLS:
        if (root / skill / "SKILL.md").is_file():
            continue
        _fetch_tree(slopstopper_repo, f".claude/skills/{skill}", ref, root / skill)
        if not (root / skill / "SKILL.md").is_file():
            raise RuntimeError(f"could not fetch skill {skill} from {slopstopper_repo}@{ref}")
    with (ws.path / ".git" / "info" / "exclude").open("a", encoding="utf-8") as fh:
        fh.write(".claude/skills/slopstopper-*/\n")
    return f"fetched@{ref}"


def _resolve_ref(repo: str, ref: str) -> str:
    """`ref` when the repo has that tag, else main."""
    if ref == "main" or gh.api(f"repos/{repo}/git/ref/tags/{ref}") is not None:
        return ref
    return "main"


def _fetch_tree(repo: str, path: str, ref: str, into: Path) -> None:
    for row in gh.api(f"repos/{repo}/contents/{path}?ref={ref}") or []:
        target = into / row["name"]
        if row["type"] == "dir":
            _fetch_tree(repo, f"{path}/{row['name']}", ref, target)
        elif row["type"] == "file":
            text = gh.contents(repo, f"{path}/{row['name']}", ref)
            if text is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")


def environment(cli_bin: Path | None = None, base: dict[str, str] | None = None) -> dict[str, str]:
    """The agent's environment: the allow-list above, nothing else. GH_TOKEN,
    GITHUB_TOKEN, STEWARD_* and every other secret the runner holds stay out,
    so neither the agent nor anything it spawns can reach GitHub as the
    steward, and the clone's credential helper (which reads GH_TOKEN) has
    nothing to hand to a `git push`."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if k in ENV_KEEP}
    if cli_bin:
        env["PATH"] = f"{cli_bin.parent}{os.pathsep}{env.get('PATH', '')}"
    return env


def _command(prompt: str, cfg: Config) -> list[str]:
    args = [
        "claude", "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
        "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DISALLOWED_TOOLS,
        "--max-turns", str(cfg.agent_turns), "--max-budget-usd", str(cfg.agent_usd),
    ]
    if cfg.agent_model:
        args += ["--model", cfg.agent_model]
    return args


def _meta(stdout: str) -> dict:
    try:
        return json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        return {}


def _read_result(path: Path, out: AgentResult) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        out.outcome, out.error = "partial", f"result file unreadable: {exc}"
        return
    out.outcome = str(data.get("outcome") or "partial")
    out.summary = str(data.get("summary") or "")
    out.category = str(data.get("category") or "")
    out.verified = [str(v) for v in data.get("verified") or []]
    out.upstream = data.get("upstream") or None
    out.notes = str(data.get("notes") or "")


def run(ws: Workspace, prompt: str, cfg: Config, cli_bin: Path | None = None) -> AgentResult:
    why_not = available()
    if why_not:
        return AgentResult(False, error=why_not)
    (ws.path / ".steward-prompt.md").write_text(prompt, encoding="utf-8")
    result_path = ws.path / RESULT_FILE
    result_path.unlink(missing_ok=True)
    try:
        proc = subprocess.run(_command(prompt, cfg), cwd=ws.path, capture_output=True, text=True,
                              env=environment(cli_bin), timeout=3600)
    except subprocess.TimeoutExpired:
        return AgentResult(True, error="agent timed out after an hour")
    meta = _meta(proc.stdout)
    out = AgentResult(True, cost_usd=meta.get("total_cost_usd"), turns=meta.get("num_turns"))
    if result_path.is_file():
        _read_result(result_path, out)
        result_path.unlink()
    else:
        out.outcome = "partial" if ws.dirty() else "skipped"
        out.error = (meta.get("result") or proc.stderr.strip()[-500:] or "agent left no result file")[:500]
    return out
