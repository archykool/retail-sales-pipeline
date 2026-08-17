"""
§3.1 enforced rather than asserted, plus a check that no reason code goes undocumented.

§3.1 is the central structural claim of the whole walkthrough: imports point one way, and
only `pipeline.py` knows the whole story. A paragraph saying so is worth very little — a
back-edge is one added import line, it is the hardest rejection trigger to catch in a diff,
and nothing about it fails at runtime. Everything keeps working; the architecture just
stops being true.

**Imports are read from the AST, not by searching the file text.** Earlier versions of this
guard did `assert "from .loaders" not in source`, which is wrong twice over: it reports a
violation for the phrase appearing in a docstring, and it misses `import src.loaders`,
`from src import loaders`, and `from src.loaders import PostgresLoader`. Parsing sees what
Python sees.

The general form of that lesson: **a guard that can false-positive is worse than no guard,
because it trains you to ignore it.** And its mirror image, which is the step most often
skipped — a test asserting something's *absence* passes for free when it is looking in the
wrong place. So the parser's output was checked against the real dependency graph before
these assertions were trusted; a `src_imports` that returned empty sets would make every
test in the first half of this file pass while enforcing nothing.

**`ALLOWED` is an upper bound, and the gap between it and reality is the point.** Every
layer currently imports strictly less than it is permitted to: `extractors`, `validators`
and `transformers` import only `models`, against a permission of two, three and four
modules respectively. If the two matched exactly there would be no way to tell a real
constraint from one traced around whatever the code happened to already do — the bound
would be a description rather than a rule, and it would have nothing left to catch.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

MODULES = {
    "config",
    "models",
    "extractors",
    "validators",
    "transformers",
    "loaders",
    "pipeline",
}

# §3.1:
#
#   config ─┐
#           ├─> extractors ─> validators ─> transformers ─> loaders
#   models ─┘
#                       pipeline imports all of the above
#
# A module may import anything to its left in that chain, plus the two leaves. The sets
# below are the *permission*, not the current reality — most modules import far less than
# they are allowed to, and that is a good sign rather than a reason to tighten this.
LEAVES = {"config", "models"}
ALLOWED: dict[str, set[str]] = {
    "config": set(),
    "models": set(),
    "extractors": LEAVES,
    "validators": LEAVES | {"extractors"},
    "transformers": LEAVES | {"extractors", "validators"},
    "loaders": LEAVES | {"extractors", "validators", "transformers"},
    "pipeline": MODULES - {"pipeline"},
}


def src_imports(module: str) -> set[str]:
    """Every `src` module that `module` imports, in any of the four spellings.

    Counts `TYPE_CHECKING`-guarded imports too. An import that only exists for annotations
    is still a statement about which layer this file depends on, and §3.1 is a claim about
    dependency direction rather than about runtime behaviour.
    """
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:                  # from .models import X
                found.add(node.module.split(".")[0])
            elif node.level and node.module is None:         # from . import models
                found.update(alias.name for alias in node.names)
            elif node.module == "src":                       # from src import models
                found.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith("src."):
                found.add(node.module.split(".")[1])         # from src.models import X
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src."):            # import src.models
                    found.add(alias.name.split(".")[1])

    return found & MODULES


# ======================================================================
# The one-way rule
# ======================================================================


def test_every_module_is_covered_by_the_rule() -> None:
    """A new file in src/ must be placed in the diagram, not silently exempted.

    Without this, adding `src/enrichers.py` gets no dependency constraint at all and the
    guard below passes while the rule quietly stops covering the codebase.
    """
    on_disk = {path.stem for path in SRC.glob("*.py") if path.stem != "__init__"}

    assert on_disk == MODULES, (
        f"src/ and §3.1 disagree. Only in src/: {on_disk - MODULES}. "
        f"Only in the rule: {MODULES - on_disk}"
    )


@pytest.mark.parametrize("module", sorted(MODULES))
def test_module_imports_respect_the_one_way_rule(module: str) -> None:
    """No module imports a layer to its right."""
    forbidden = src_imports(module) - ALLOWED[module]

    assert not forbidden, (
        f"src/{module}.py imports {sorted(forbidden)}, which §3.1 forbids. "
        f"Permitted: {sorted(ALLOWED[module]) or 'nothing from src/'}"
    )


@pytest.mark.parametrize("leaf", sorted(LEAVES))
def test_leaves_import_nothing_from_the_pipeline(leaf: str) -> None:
    """`config` and `models` are the two files every other layer may depend on.

    The moment either imports a pipeline layer, the graph has a cycle and the type
    definitions can no longer be read without reading the logic that uses them.
    """
    assert src_imports(leaf) == set()


def test_transformers_does_not_import_loaders() -> None:
    """The specific edge CLAUDE.md names, and the one D-005 turns on.

    If the transformer could reach the loader it could resolve surrogate keys, and
    `FactSalesRecord` would carry `customer_key` instead of `customer_id`. The natural keys
    in that model are a consequence of this edge not existing.
    """
    assert "loaders" not in src_imports("transformers")


def test_loaders_does_not_import_validators() -> None:
    """The other edge CLAUDE.md names, and a genuinely tempting one.

    `loaders.py` serialises the sales columns and `validators.py` already defines their
    canonical order. Importing it would save a duplicated tuple and invert the dependency.
    """
    assert "validators" not in src_imports("loaders")


def test_only_pipeline_knows_the_whole_story() -> None:
    """`pipeline.py` imports every layer; nothing else imports more than two.

    This is the shape that makes the orchestrator the only file needing the full picture,
    and it is what "how do the files interact" is actually asking about.
    """
    assert src_imports("pipeline") == MODULES - {"pipeline"}

    for module in MODULES - {"pipeline"}:
        assert len(src_imports(module)) <= 2, (
            f"src/{module}.py imports {sorted(src_imports(module))} — only pipeline.py "
            f"should need a broad view"
        )


def test_no_module_imports_itself() -> None:
    for module in MODULES:
        assert module not in src_imports(module)


def test_import_direction_is_not_call_order() -> None:
    """Guards the distinction §3.1 makes explicitly, which the diagram alone obscures.

    At runtime the transformer runs *before* the validator for reference data and *after* it
    for sales (§7.0). If that ordering were achieved by imports, `transformers` and
    `validators` would each need the other. Neither does: `pipeline.py` sequences the calls,
    and both depend only on `models`.
    """
    assert "validators" not in src_imports("transformers")
    assert "transformers" not in src_imports("validators")
    assert src_imports("transformers") <= {"models"}
    assert src_imports("validators") <= {"models"}


# ======================================================================
# No reason code is undocumented, and none is documented but unimplemented
# ======================================================================

# Mirrors SPEC §6.1 (11 required) and §6.2 (9 additional). Duplicated deliberately: the
# point of the test is to fail when the code and the spec diverge, which it cannot do if it
# reads the codes from the implementation.
SPEC_REASON_CODES = {
    # §6.1 — required by the assignment
    "MISSING_FIELD",
    "BAD_INT_ORDER_ID",
    "BAD_INT_QUANTITY",
    "BAD_DATE_FORMAT",
    "BAD_DECIMAL_PRICE",
    "BAD_DECIMAL_DISCOUNT",
    "QTY_NOT_POSITIVE",
    "PRICE_NOT_POSITIVE",
    "DISCOUNT_OUT_OF_RANGE",
    "UNKNOWN_CUSTOMER",
    "UNKNOWN_PRODUCT",
    # §6.2 — ours
    "DUPLICATE_ORDER_ID",
    "SCHEMA_MISMATCH",
    "DATE_IN_FUTURE",
    "DATE_OUT_OF_PERIOD",
    "DISCOUNT_EQ_ONE",
    "QTY_EXCEEDS_THRESHOLD",
    "NON_NUMERIC_CURRENCY",
    "KEY_NORMALIZED",
    "PRICE_PRECISION",
}

_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{4,}$")


def implemented_reason_codes() -> set[str]:
    """Every SCREAMING_CASE string literal in the two files that emit codes.

    Read from string constants rather than a registry because that is how they are actually
    written — a registry would make this test tautological.
    """
    found: set[str] = set()
    for module in ("validators", "extractors"):
        tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _CODE_PATTERN.match(node.value):
                    found.add(node.value)
    return found


def test_no_undocumented_reason_code_exists() -> None:
    """A code the validator can emit but §6 does not list.

    Where it bites: `GROUP BY reason_code` in the analytics query returns a row nobody can
    explain, and the bad-record catalogue has no expectation for it.
    """
    undocumented = implemented_reason_codes() - SPEC_REASON_CODES

    assert not undocumented, f"emitted but not in SPEC §6: {sorted(undocumented)}"


def test_every_documented_reason_code_is_implemented() -> None:
    """A code §6 promises but nothing emits.

    The more embarrassing direction: the spec claims a rule the pipeline does not enforce,
    and the data it was meant to catch flows straight through.

    **This test found exactly that on its first run, and it is the clearest argument for
    this whole file.** `KEY_NORMALIZED` existed only as a substring inside a log *format*
    string — `"KEY_NORMALIZED %s row %d: ..."` — and never as an identifier. Three
    consequences, all silent:

    - `GROUP BY reason_code` could never see it, so D-013's claim that "rejections are
      aggregable in SQL" was hollow for that one code while appearing to hold for all of
      them.
    - A typo in it would have been invisible to every layer at once: no test referenced it,
      no type contained it, no constraint mentioned it, because it was not a name.
    - Nothing would ever have failed. The cleaning worked; only the label was unreachable.

    Review had passed over it repeatedly. A mechanical check caught it immediately, because
    the claim "every documented code is implemented" is the kind of thing that can be
    executed instead of asserted — which is what this file does to §3.1 as well.
    """
    missing = SPEC_REASON_CODES - implemented_reason_codes()

    assert not missing, f"in SPEC §6 but never emitted: {sorted(missing)}"


# ======================================================================
# The database driver is confined to one module
# ======================================================================


def third_party_imports(module: str) -> set[str]:
    """Top-level non-stdlib package names imported by a module."""
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])

    return found


def test_psycopg_is_imported_only_by_loaders() -> None:
    """Only one module may know what database this is.

    §3.1 constrains which layers depend on each other; this constrains where the *driver*
    may appear, which is a related claim the diagram does not state. It is what makes the
    dry-run guarantee structural rather than a matter of discipline: if `validators.py` could
    import psycopg, "dry-run opens no connection" would depend on every layer choosing not
    to open one. As it stands, only `loaders.py` can, and only `pipeline.py` decides whether
    to call it.

    It is also what would have to change first to support another database, which makes the
    blast radius of that hypothetical exactly one file.
    """
    offenders = {
        module
        for module in MODULES
        if module != "loaders" and "psycopg" in third_party_imports(module)
    }

    assert not offenders, f"psycopg imported outside loaders.py: {sorted(offenders)}"


def test_no_module_imports_an_undeclared_dependency() -> None:
    """Every third-party import must be in requirements.txt (§13 rejection trigger).

    Catches the failure a fresh clone hits and the developer never does: the package is
    installed locally, so nothing breaks here, and `pip install -r requirements.txt`
    produces a broken environment somewhere else.
    """
    requirements = (
        Path(__file__).resolve().parent.parent / "requirements.txt"
    ).read_text(encoding="utf-8")
    declared = {
        re.split(r"[\[><=;\s]", line.strip())[0].lower()
        for line in requirements.splitlines()
        if line.strip() and not line.startswith("#")
    }

    stdlib = set(__import__("sys").stdlib_module_names)

    for module in sorted(MODULES):
        for package in sorted(third_party_imports(module)):
            if package in stdlib or package in MODULES or package == "src":
                continue
            assert package.lower() in declared, (
                f"src/{module}.py imports {package!r}, which is not in requirements.txt "
                f"(declared: {sorted(declared)})"
            )
