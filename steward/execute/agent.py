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
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from steward import gh, isolation, sandbox
from steward.config import Config
from steward.prompts import RESULT_FILE
from steward.workspace import LOCAL_ONLY, Workspace, guarded_git_dir

SKILLS = ("slopstopper-triage", "slopstopper-install")
# Which `slopstopper run` targets the agent may call: the ones that read
# the tree and execute nothing from it. The browser and API checks build or
# serve the repo (its package.json scripts), and `security:licences` runs
# the interpreter the repo's own config names (`security.licences.python`,
# or a committed `.venv`); each would hand the agent an arbitrary-exec
# path, so none is on the list. A check whose command or interpreter comes
# from the repo never is. The steward does not verify those in a clone
# either.
_RUN_TARGETS = ("hygiene:", "security:sast", "security:secrets", "security:vulnerability")
_NEVER_RUN = ("security:licences",)
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
# interpreters are all ways to run an arbitrary command. So is `git status`
# inside a repository the agent wrote (a `.git` anywhere, or a `.git` file
# pointing at one, whose config names an fsmonitor command): writing any
# `.git` and changing directory are denied, so `git status` only ever runs
# in the copy's own repository.
DISALLOWED_TOOLS = (
    "Bash(git push*)", "Bash(git commit*)", "Bash(git remote*)", "Bash(git config*)", "Bash(git grep*)",
    "Bash(gh *)", "Bash(curl *)", "Bash(wget *)", "Bash(npx *)", "Bash(npm *)", "Bash(pip *)", "Bash(pipx *)",
    "Bash(find *)", "Bash(task *)", "Bash(python*)", "Bash(sh *)", "Bash(bash *)", "Bash(env *)", "Bash(cd *)",
    *(f"Bash(slopstopper run {t}*)" for t in _NEVER_RUN),
    *(f"Bash(slopstopper run --json {t}*)" for t in _NEVER_RUN),
    *(f"{tool}({path})" for tool in ("Edit", "Write", "MultiEdit")
      for path in ("./.git/**", "**/.git/**", "**/.git")),
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
    """Why the agent cannot run, or None when it can. Isolated, the probe
    runs `claude --version` as the agent user, the way the run will: the
    steward's own PATH says nothing about what that user can find."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")):
        return "no ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN"
    user = isolation.agent_user()
    if user:
        return isolation.unavailable(user)
    return None if shutil.which("claude") else "claude CLI not on PATH"


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


def _read_result(raw: bytes, out: AgentResult) -> None:
    """The agent's result, which is the agent's to write: anything but the
    shape the brief asks for reads as partial, never as a crash."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        out.outcome, out.error = "partial", f"result file unreadable: {exc}"
        return
    if not isinstance(data, dict):
        out.outcome, out.error = "partial", f"result file holds a JSON {type(data).__name__}, not an object"
        return
    out.outcome = str(data.get("outcome") or "partial")
    out.summary = str(data.get("summary") or "")
    out.category = str(data.get("category") or "")
    verified = data.get("verified") or []
    out.verified = [str(v) for v in verified] if isinstance(verified, list) else [str(verified)]
    upstream = data.get("upstream")
    out.upstream = upstream if isinstance(upstream, dict) and upstream else None
    out.notes = str(data.get("notes") or "")


def run(ws: Workspace, prompt: str, cfg: Config, cli_bin: Path | None = None, timeout: int = 3600,
        protect: Sequence[Path] = (), preflight: Callable[[], str | None] | None = None,
        checked: set[Path] | None = None) -> AgentResult:
    """One boxed run. `protect` are absolute paths outside the clone the agent
    must not edit (the run's venvs); `timeout` comes from the run, so an
    agent never outlives the token the steward will push with. `preflight`
    says why the agent cannot run, or None; a run passes its own cached
    answer, so the probe runs once per run, not once per order. `checked`
    holds the CLIs already proven to run as the agent user, likewise."""
    why_not = (preflight or available)()
    if why_not:
        return AgentResult(False, error=why_not)
    (ws.path / ".steward-prompt.md").write_text(prompt, encoding="utf-8")
    result_path = ws.path / RESULT_FILE
    result_path.unlink(missing_ok=True)
    argv, rejected = _command(prompt, cfg, protect), []
    try:
        proc, raw = _launch(ws, argv, cli_bin, timeout, rejected, checked)
    except subprocess.TimeoutExpired:
        return AgentResult(True, error=f"agent timed out after {timeout} s", notes=_rejected_note(rejected))
    except isolation.SetupError as exc:
        return AgentResult(False, error=f"the agent could not be started in isolation: {exc}"[:500])
    except isolation.IsolationError as exc:
        # The clone is as it was (the copy-back is fail-closed): nothing from
        # this run lands, and the order says why instead of failing outright.
        return AgentResult(True, outcome="skipped", error=f"the agent's work was not copied back: {exc}"[:500],
                           notes=_rejected_note(rejected))
    meta = _meta(proc.stdout)
    out = AgentResult(True, cost_usd=meta.get("total_cost_usd"), turns=meta.get("num_turns"))
    if raw is not None:
        _read_result(raw, out)
    else:
        out.outcome = "partial" if ws.dirty() else "skipped"
        out.error = (meta.get("result") or proc.stderr.strip()[-500:] or "agent left no result file")[:500]
        if proc.returncode < 0 or proc.returncode >= 128:
            out.error = f"the agent was killed by a signal (exit {proc.returncode}; out of memory?). {out.error}"[:500]
    out.notes = " ".join(n for n in (out.notes, _rejected_note(rejected)) if n)
    return out


def _launch(ws: Workspace, argv: list[str], cli_bin: Path | None, timeout: int, rejected: list[str],
            checked: set[Path] | None) -> tuple[subprocess.CompletedProcess, bytes | None]:
    """Run the agent, isolated or in place, and return its process and its
    result file's bytes (None without a regular one). In place, the result
    file is read and removed from the clone; isolated, it never lands there."""
    user = isolation.agent_user()
    with guarded_git_dir(ws):
        if user:
            return _run_isolated(user, ws, argv, cli_bin, timeout, rejected, checked)
        with sandbox.scratch_home("steward-agent-home-") as home:
            proc = _run_to_files(argv, ws.path, environment(cli_bin, home=home), timeout, own_group=True)
    raw = _result_bytes(ws.path, rejected)
    if raw is not None:
        (ws.path / RESULT_FILE).unlink()
    return proc, raw


# What the steward keeps for itself in the clone: never copied back from the
# agent's tree, never removed because the agent's tree lacks it. One list,
# the one the clone's `.git/info/exclude` is written from.
STEWARD_FILES = LOCAL_ONLY


def _run_isolated(user: str, ws: Workspace, argv: list[str], cli_bin: Path | None, timeout: int,
                  rejected: list[str], checked: set[Path] | None = None
                  ) -> tuple[subprocess.CompletedProcess, bytes | None]:
    """The agent as `user`, in its own copy of the clone. The time limit is
    enforced inside, by `timeout` running as that user. Finished or timed
    out, the agent's work is then brought back before the copy is removed;
    a timed-out run raises TimeoutExpired after that, the same as an
    in-place run. An interrupted one brings nothing back."""
    if cli_bin is not None and cli_bin not in (checked or ()):
        isolation.check_cli(user, cli_bin)
        if checked is not None:
            checked.add(cli_bin)
    with isolation.copy_for(user, ws.path) as iso:
        env = environment(None, home=iso.home)
        cmd = isolation.command(iso, argv, env, cli_bin.parent if cli_bin else None, timeout=timeout)
        started = time.monotonic()
        try:
            # On the backstop, `sudo` is root's and cannot be signalled by the
            # steward: stopping the agent user's processes is what ends it.
            proc = _run_to_files(cmd, ws.path, env, timeout + isolation.KILL_AFTER + 60,
                                 on_abort=lambda: isolation.stop_all(user))
        except subprocess.TimeoutExpired:
            proc = None
        late = proc is None or isolation.timed_out(proc.returncode, time.monotonic() - started, timeout)
        try:
            raw = _bring_back(iso, ws, rejected)
        except isolation.IsolationError as exc:
            if late:
                raise isolation.IsolationError(f"the agent timed out after {timeout} s, and {exc}") from exc
            raise
    if late:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return proc, raw


def _bring_back(iso: isolation.Isolated, ws: Workspace, rejected: list[str]) -> bytes | None:
    """Stop every process of the agent user, export its tree into a
    directory only the steward can write, and copy back from there.
    Returns the result file's bytes, read from the export: it is never
    written into the clone, so nothing can fail or be left behind there."""
    isolation.stop_all(iso.user)
    isolation.open_up(iso)
    staging = Path(tempfile.mkdtemp(prefix=".steward-export-", dir=ws.path.parent))
    try:
        refused = isolation.export(iso, staging)
        rejected += isolation.refusal_notes(refused, ws.path)
        # A path the export could not carry is kept as the clone has it, never read as deleted.
        skip = (*STEWARD_FILES, *ws.excluded, *(r.rel for r in refused))
        rejected += isolation.sync_back(staging, ws.path, skip=skip).rejected
        return _result_bytes(staging, rejected)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _result_bytes(root: Path, rejected: list[str]) -> bytes | None:
    """The agent's result file under `root`, if it is a regular file, read
    without following a link. Anything else (a link to a file of the
    steward's, a directory, a FIFO) is noted and treated as no result file:
    its fields go into a public PR body."""
    if not os.path.lexists(root / RESULT_FILE):
        return None
    try:
        return isolation.read_regular(root, RESULT_FILE)[0]
    except (OSError, isolation.IsolationError) as exc:
        rejected.append(f"{RESULT_FILE}: not a regular file ({getattr(exc, 'strerror', None) or exc})")
        return None


def _run_to_files(cmd: list[str], cwd: Path, env: dict, timeout: float, own_group: bool = False,
                  on_abort: Callable[[], None] | None = None) -> subprocess.CompletedProcess:
    """Run `cmd` and wait for it, not for its output pipes. A process the
    agent detached inherits its stdout and stderr; with pipes the steward
    would wait for that process to close them, which it never has to, and
    the run would stall until the limit. Into files the steward owns, the
    wait ends when the agent itself exits.

    `own_group` (the in-place run) starts it in its own session and kills
    that whole group once it exits, so a background job the agent started
    cannot keep editing the clone while the steward verifies and commits.
    Only a process that leaves the session escapes that; running the agent
    as its own user (`STEWARD_AGENT_USER`) closes that too.

    However the wait ends early (the limit, an interrupt), the run is
    stopped (`on_abort`, else a kill) and reaped before the error goes on."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=err, env=env, start_new_session=own_group)
        try:
            returncode = proc.wait(timeout=timeout)
        except BaseException:
            _abort(proc, own_group, on_abort)
            raise
        finally:
            if own_group:
                _kill(proc, True)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(cmd, returncode, out.read().decode("utf-8", "replace"),
                                           err.read().decode("utf-8", "replace"))


REAP_SECONDS = 60


def _abort(proc: subprocess.Popen, group: bool, on_abort: Callable[[], None] | None) -> None:
    """Stop `proc` and reap it. A stop that fails must not replace the error
    that called for it (a timeout would read as something else); the caller
    stops the agent again, and reports what it could not do."""
    try:
        (on_abort or (lambda: _kill(proc, group)))()
    except Exception:  # see the docstring
        pass
    try:
        proc.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass  # still running past a failed stop; the caller's own stop reports it


def _kill(proc: subprocess.Popen, group: bool) -> None:
    try:
        if group:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _rejected_note(rejected: list[str]) -> str:
    return ("The steward left out of the agent's work: " + "; ".join(rejected) + ".") if rejected else ""
