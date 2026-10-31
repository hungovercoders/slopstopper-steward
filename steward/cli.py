"""`steward`: the command line.

    steward discover [--org X]        the adopters, with the evidence for each
    steward inventory [--repo o/n]    pins, findings, disabled workflows, open steward PRs
    steward plan [--repo o/n]         the work orders a run would open, no writes
    steward run [--repo o/n] [--dry-run] [--workdir DIR]
                                      clone, fix, push, open PRs; the report lands in .steward/reports/
    steward contract <slopstopper-src>
                                      check a slopstopper checkout still carries every surface relied on

Exit codes follow slopstopper's: 0 ran clean, 1 ran and something failed
(an order errored, a surface broke), 2 could not run (no gh, bad config).
`--json` prints one JSON value on stdout and nothing else.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

from steward import __version__, contract, discover, gh, report, runner
from steward.config import DEFAULT_PATH, Config, load
from steward.plan import plan_all


class CannotRun(Exception):
    """Exit 2: the steward could not run at all."""


def _cfg(args) -> Config:
    try:
        return load(Path(args.config))
    except (OSError, ValueError, TypeError) as exc:
        raise CannotRun(str(exc)) from exc


def _need_gh() -> None:
    if not gh.available():
        raise CannotRun("gh is not on PATH; the steward reads and writes GitHub through it")
    if not gh.authenticated():
        raise CannotRun("gh has no token (set GH_TOKEN or run gh auth login)")


def cmd_discover(args) -> int:
    cfg = _cfg(args)
    _need_gh()
    if args.org:
        cfg.orgs = [args.org]
    repos = discover.discover(cfg)
    if args.json:
        print(json.dumps([r.to_dict() for r in repos], indent=2))
        return 0
    print(f"{len(repos)} adopter(s) across {', '.join(cfg.orgs)}")
    for r in repos:
        print(f"  {r.full_name:45} pin {r.pinned or '—':8} suite {r.installed_from or '—':8} [{', '.join(r.evidence)}]")
    return 0


def cmd_inventory(args) -> int:
    cfg = _cfg(args)
    _need_gh()
    invs, targets, latest = runner.gather(cfg, args.repo)
    if args.json:
        print(json.dumps([{**inv.to_dict(), "target": targets[inv.repo.full_name]} for inv in invs], indent=2))
        return 0
    print(f"PyPI latest {cfg.package}: {latest}")
    for inv in invs:
        t = targets[inv.repo.full_name]
        print(f"\n{inv.repo.full_name}  pin {inv.repo.pinned or '—'} → {t or 'current'}  profile {inv.profile or 'ui'}"
              f"  skills {'yes' if inv.skills_present else 'no'}")
        for f in inv.findings:
            print(f"  #{f.number} [{f.kind}{' security' if f.security else ''}] {f.check or f.workflow or ''}: {f.title}")
        for w in inv.disabled_workflows:
            print(f"  workflow {w['path']} is {w['state']}")
        for oid, pr in inv.open_orders.items():
            print(f"  open steward PR #{pr['number']} ({oid})")
    return 0


def cmd_plan(args) -> int:
    cfg = _cfg(args)
    _need_gh()
    invs, targets, _ = runner.gather(cfg, args.repo)
    plan = plan_all(invs, targets, cfg)
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
        return 0
    if not plan.orders:
        print("Nothing to do.")
    for o in plan.orders:
        print(f"  {o.repo:40} {o.kind:8} {o.branch:40} {o.reason}")
    for o in plan.deferred:
        print(f"  deferred {o.repo:31} {o.kind:8} {o.branch:40} {o.reason}")
    for s in plan.skipped:
        print(f"  skipped  {s['repo']:31} {s['order']:49} {s['why']}")
    return 0


def cmd_run(args) -> int:
    cfg = _cfg(args)
    _need_gh()
    rep = runner.run(cfg, args.repo, args.dry_run, Path(args.workdir) if args.workdir else None)
    md, js = report.write(rep, Path(args.report_dir))
    if args.json:
        print(json.dumps(rep.to_dict(), indent=2))
    else:
        print(rep.markdown())
        print(f"report: {md} · {js}")
    return 1 if rep.failed else 0


def cmd_contract(args) -> int:
    problems = contract.check(Path(args.src))
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems, "surfaces": len(contract.SURFACES)}, indent=2))
    elif problems:
        print(f"✗ {len(problems)} surface(s) moved in {args.src}:")
        for p in problems:
            print(f"  - {p}")
    else:
        print(f"✓ all {len(contract.SURFACES)} slopstopper surfaces hold in {args.src}")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="steward", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"slopstopper-steward {__version__}")
    parser.add_argument("--config", default=str(DEFAULT_PATH), help="path to steward.yml (default ./steward.yml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="list adopters across the configured orgs")
    p.add_argument("--org")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("inventory", help="what slopstopper knows about each adopter")
    p.add_argument("--repo", help="one owner/name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_inventory)

    p = sub.add_parser("plan", help="the orders a run would open (no writes)")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("run", help="work the plan: clone, fix, push, open PRs")
    p.add_argument("--repo")
    p.add_argument("--dry-run", action="store_true", help="plan only, whatever steward.yml says")
    p.add_argument("--workdir", help="where clones go (default a temp dir)")
    p.add_argument("--report-dir", default=str(report.REPORT_DIR))
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("contract", help="check a slopstopper checkout still carries every relied-on surface")
    p.add_argument("src", help="path to a slopstopper checkout")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_contract)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (CannotRun, gh.GhError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"✗ PyPI or GitHub unreachable or answered badly: {exc}", file=sys.stderr)
        return 2
    except (subprocess.TimeoutExpired, OSError) as exc:
        # A tool that hung or could not start is "could not run", never "ran and failed".
        print(f"✗ could not run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
