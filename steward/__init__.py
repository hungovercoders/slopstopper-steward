"""slopstopper-steward: the org-wide operator for slopstopper adopters.

slopstopper decides what is wrong in one repo and ships how to fix it (the
CLI, the ss-* workflows, the two Claude Code skills). The steward decides
which repos across an organisation need a hand, and drives slopstopper's
own tooling at them: it finds adopters, moves CLI pins that fell behind,
and works the issues slopstopper raised, opening one pull request per
piece of work. It carries no check logic and no fix knowledge of its own;
`steward/contract.py` lists the slopstopper surfaces it relies on.
"""

__version__ = "0.1.0"
