"""Shared fixtures for the behaviour tests that run the generated Spec Kit scripts.

The scripts `eval` the quoted output of `get_feature_paths`, so their safety
rests on that quoting. These helpers let tests feed hostile names to the real
scripts and prove the guard can fail by running a deliberately unsafe copy.
"""

from pathlib import Path

MARKER = "PWNED"

# Names that must stay data. Each would run `touch PWNED` if a script ever
# evaluated a branch, feature-directory or argument value as shell text.
HOSTILE_NAMES = {
    "command-substitution": "013-$(touch PWNED)",
    "backticks": "013-`touch PWNED`",
    "semicolon": "013-x; touch PWNED; #",
    "single-quote": "013-it's '; touch PWNED; '",
    "double-quote": '013-"quoted" "; touch PWNED; "',
    "spaces": "013 name  with   spaces",
    "newline": "013-line1\ntouch PWNED\nline3",
}


def markers_under(*roots):
    """Return every file named PWNED* below the given roots."""
    return sorted({str(p) for root in roots for p in Path(root).rglob(MARKER + "*")})


def remove_markers(*roots):
    for marker in markers_under(*roots):
        Path(marker).unlink(missing_ok=True)


def mutate_common(source):
    """Return common.sh with %q replaced by naive double quoting (deliberately unsafe)."""
    mutated = source.replace("=%q\\n", '="%s"\\n')
    if mutated == source:
        raise AssertionError("common.sh no longer quotes values with %q")
    return mutated
