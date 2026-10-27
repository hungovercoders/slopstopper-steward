"""Which checks the steward can verify inside a clone.

The set is asked of the pinned CLI, not written here: `slopstopper profile
expand library --json` names every workflow the `library` profile drops,
which is slopstopper's own definition of "needs a deployed surface", and
`checks list --json` says which of the rest this repo keeps. Two things
the steward does know about its own situation live here: a clone has no
installed project environment, so a check that inventories one cannot run
(the workflow for it installs the project first); and an older CLI without
`profile expand` gets the hygiene set as a fallback.
"""

# Checks that need the project's resolved dependency environment, which CI
# builds and a bare clone has not. `security:licences` reads `pip inspect`
# of an interpreter holding the project's dependencies.
NEEDS_ENVIRONMENT = ("security:licences",)

# When the pinned CLI cannot say (no `profile expand --json`): the checks
# that read the tree alone and apply to every repo shape.
FALLBACK_STATIC = (
    "hygiene:complexity", "hygiene:docs-accuracy", "hygiene:docs-size", "hygiene:docs-structure",
    "hygiene:entry-files", "hygiene:csp-exceptions", "hygiene:privacy",
)
