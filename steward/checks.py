"""What a clone cannot verify, beyond what the pinned CLI reports.

Everything else is asked of the pinned CLI at run time (`checks list`,
`profile expand library`, `doctor`; see `execute/refresh.static_checks`).
The one thing the CLI does not say is which checks inventory the project's
*installed* dependency environment, which a fresh clone does not have
(their workflows install the project first). Each name here is a row in
`steward/contract.py`, so the contract test fails the day slopstopper
renames one or changes why it needs an environment.
"""

NEEDS_ENVIRONMENT = ("security:licences",)
