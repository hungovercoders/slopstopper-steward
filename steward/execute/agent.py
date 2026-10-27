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
from typing import Sequence

from steward import gh, isolation, sandbox
from steward.config import Config
from steward.prompts import RESULT_FILE
from steward.workspace import Workspace, guarded_git_dir

SKILLS = ("slopstopper-triage", "slopstopper-install")
# Which `slopstopper run` targets the agent may call: the ones that read
# the tree and execute nothing from it. The browser and API checks build or
# serve the repo (its package.json scripts), which would hand the agent an
# arbitrary-exec path, so they are not on the list; the steward does not
# verify them in a clone either.
_RUN_TARGETS = ("hygiene:", "security:sast", "security:secrets", "security:vulnerability", "security:licences")
ALLOWED_TOOLS = (
    "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep",
    *(f"Bash(slopstopper run {t}*)" for t in _RUN_TARGETS),
    *(f"Bash(slopstopper run --json {t}*)" for t in _RUN_TARGETS),
    "Bash(slopstopper checks list*)", "Bash(slopstopper doctor*)", "Bash(slopstopper profile *)",
    "Bash(slopstopper config get*)",
    "Bash(git status*)", "Bash(cat *)", "Bash(ls *)", "Bash(grep *)", "Bash(head *)", "Bash(tail *)",
    "Bash(wc *)", "Bash(mkdir *)",
)
# Named as well as left off the allow-list, so a permission prompt the
# headless run would auto-answer never lets one through. `find -exec`,
# `task` (runs whatever Taskfile.ss.yml says), `git grep -O<cmd>` and the
# interpreters are all ways to run an arbitrary command.
DISALLOWED_TOOLS = (
    "Bash(git push*)", "Bash(git commit*)", "Bash(git remote*)", "Bash(git config*)", "Bash(git grep*)",
    "Bash(gh *)", "Bash(curl *)", "Bash(wget *)", "Bash(npx *)", "Bash(npm *)", "Bash(pip *)", "Bash(pipx *)",
    "Bash(find *)", "Bash(task *)", "Bash(python*)", "Bash(sh *)", "Bash(bash *)", "Bash(env *)",
    "Edit(./.git/**)", "Write(./.git/**)", "MultiEdit(./.git/**)",
    "WebFetch", "WebSearch",
)


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
    user = isolation.agent_user()
    return isolation.unavailable(user) if user else None


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
        # Kept out of the commit by pathspec (see Workspace.excluded): a
        # `.gitignore` negation the installer writes would otherwise win
        # over `.git/info/exclude` and `git add -A` would ship the skills.
        ws.excluded.append(f".claude/skills/{skill}")
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


def environment(cli_bin: Path | None = None, base: dict[str, str] | None = None,
                home: Path | None = None) -> dict[str, str]:
    """The agent's environment: `sandbox.environment` plus the model key.
    GH_TOKEN, GITHUB_TOKEN, STEWARD_* and every other secret the runner
    holds stay out, and HOME is a throwaway directory, so neither the agent
    nor anything it spawns can reach GitHub as the steward: the clone's
    credential helper finds no token in the environment and no stored login
    under HOME."""
    return sandbox.environment(cli_bin.parent if cli_bin else None, home, model_key=True, base=base)


def _command(prompt: str, cfg: Config, protect: Sequence[Path] = ()) -> list[str]:
    deny = [*DISALLOWED_TOOLS, *(f"{tool}(/{p}/**)" for p in protect for tool in ("Edit", "Write", "MultiEdit"))]
    args = [
        "claude", "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
        "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *deny,
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


def run(ws: Workspace, prompt: str, cfg: Config, cli_bin: Path | None = None, timeout: int = 3600,
        protect: Sequence[Path] = ()) -> AgentResult:
    """One boxed run. `protect` are absolute paths outside the clone the agent
    must not edit (the run's venvs); `timeout` comes from the run, so an
    agent never outlives the token the steward will push with."""
    why_not = available()
    if why_not:
        return AgentResult(False, error=why_not)
    (ws.path / ".steward-prompt.md").write_text(prompt, encoding="utf-8")
    result_path = ws.path / RESULT_FILE
    result_path.unlink(missing_ok=True)
    argv, user, rejected = _command(prompt, cfg, protect), isolation.agent_user(), []
    try:
        with guarded_git_dir(ws):
            if user:
                proc = _run_isolated(user, ws, argv, cli_bin, timeout, rejected)
            else:
                with sandbox.scratch_home("steward-agent-home-") as home:
                    proc = subprocess.run(argv, cwd=ws.path, capture_output=True, text=True,
                                          env=environment(cli_bin, home=home), timeout=timeout)
    except subprocess.TimeoutExpired:
        return AgentResult(True, error=f"agent timed out after {timeout} s", notes=_rejected_note(rejected))
    meta = _meta(proc.stdout)
    out = AgentResult(True, cost_usd=meta.get("total_cost_usd"), turns=meta.get("num_turns"))
    if result_path.is_file():
        _read_result(result_path, out)
        result_path.unlink()
    else:
        out.outcome = "partial" if ws.dirty() else "skipped"
        out.error = (meta.get("result") or proc.stderr.strip()[-500:] or "agent left no result file")[:500]
    out.notes = " ".join(n for n in (out.notes, _rejected_note(rejected)) if n)
    return out


# What the steward keeps for itself in the clone: never copied back from the
# agent's tree, never removed because the agent's tree lacks it.
STEWARD_FILES = (".steward-prompt.md", RESULT_FILE, ".steward-release-notes.md")


def _run_isolated(user: str, ws: Workspace, argv: list[str], cli_bin: Path | None, timeout: int,
                  rejected: list[str]) -> subprocess.CompletedProcess:
    """The agent as `user`, in its own copy of the clone. Whatever happens,
    its work is copied back before the copy is removed, so a timeout keeps
    what it did the same way an in-place run does."""
    with isolation.copy_for(user, ws.path) as iso:
        env = environment(None, home=iso.home)
        try:
            return subprocess.run(isolation.command(iso, argv, env, cli_bin.parent if cli_bin else None),
                                  cwd=ws.path, capture_output=True, text=True, env=env, timeout=timeout)
        finally:
            synced = isolation.sync_back(iso.tree, ws.path, skip=(*STEWARD_FILES, *ws.excluded))
            rejected += synced.rejected
            result = iso.tree / RESULT_FILE
            if result.is_file() and not result.is_symlink():
                shutil.copyfile(result, ws.path / RESULT_FILE)


def _rejected_note(rejected: list[str]) -> str:
    return ("The steward did not copy back from the agent's tree: " + "; ".join(rejected) + ".") if rejected else ""
