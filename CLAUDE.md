# CLAUDE.md — Working agreement for the coding agent

This project is graded on a 10-minute explainer video, not on the code alone.
`docs/SPEC.md` is the authoritative contract. If SPEC and the assignment PDF
disagree, **do not resolve it — flag it and ask.** SPEC wins over the PDF; the
human owner wins over everything.

---

## How we work (SPEC §13)

- **One step at a time, in the order §9 lists them.** Each step is exactly **one
  commit**. Step labels are the assignment PDF's own numbers, so they are *not*
  sequential in build order — see the two-way map in SPEC §9.0 and never renumber
  a step without being asked.
- **Do not start step N+1 until the owner explicitly says so.** Wait for go-ahead.
- **Holding means stop writing code, not just stop committing.** If you are waiting on a
  ruling, wait with a **clean tree**. Writing ahead while a decision is outstanding is how
  one-step-one-commit gets broken: two steps end up in one file, no path-scoped commit can
  separate them, and the git log stops being evidence of the process.

  **The rule carries its own applicability test: the path-scoped commit is the escape
  hatch, so ask whether the hatch exists before writing ahead.** If the pending work and
  the uncommitted work touch *disjoint* file paths, `git add <paths>` can still produce
  two honest commits — say so explicitly and keep them disjoint. If they touch the same
  file, the hatch is not there, no staging command can separate them afterwards, and the
  only options are a combined commit that misrepresents the sequence or rework. In that
  case: stop and wait.
- **Do not modify files outside the current step's scope.**
- **Do not add dependencies** that aren't already in `requirements.txt`.
- **No `float` for money. Ever.** Use `decimal.Decimal`; round once at the end,
  `ROUND_HALF_UP`, 2 dp. Never round intermediates.
- **Respect the one-way dependency rule (§3.1).** See below.
- **Docstrings explain WHY, not WHAT.**
- Type hints on all public methods.
- Never `print` inside `src/` — use the `logging` module.
- The owner reviews every file before it is committed. **The agent writes; the
  owner decides.**
- **Deliverable per step:** the complete file(s) for that step, plus a short
  summary of every design choice made that the spec did not dictate. Anything
  not dictated by the spec gets appended to `docs/DECISION.md` **in the owner's
  own words** — the agent does not write those rationales.

### Design-choice reporting format

After each file, the agent reports decisions in **two separate lists:**

**A. SPEC DEVIATIONS** — spec says X; I implemented Y instead; here's why.
(Example: SPEC says "with sane defaults for all DB settings" but I made them
all required. Reason: production containers won't have a .env file, so failing
loudly at startup catches misconfigurations that silently wrong defaults would hide.)

**B. UNSPECIFIED** — spec was silent on this; I chose Y; here's why.
(Example: I made `from_env()` read `os.environ` only, not calling `load_dotenv()`
internally. Reason: main.py owns calling `load_dotenv()` at startup, keeping the
same code path for dev and prod.)

**Only report items where there was a real alternative.** Spec compliance is not
a design choice. If a choice is ordinary good practice (use immutable dataclasses,
use `pathlib.Path`, mask passwords), don't report it unless the spec was genuinely
silent *and* a real alternative exists. ADR threshold: "I chose Y over Z because..."
If the list is noisy, important deviations get buried.

Only the owner appends these to DECISION.md, in their own words after reviewing
the code.

---

## Dependency rule (SPEC §3.1) — the single most important structural claim

```
config ─┐
        ├─> extractors ─> validators ─> transformers ─> loaders
models ─┘                     ▲
                              └──── pipeline imports all of the above
```

Arrows point **one way only**:

- `transformers.py` must **not** import `loaders.py`.
- `loaders.py` must **not** import `validators.py`.
- Only `pipeline.py` may know the whole story and import everything.
- `config.py` and `models.py` are leaf dependencies — they import nothing from
  the pipeline layers.

If any change introduces a back-edge against this diagram, it is rejected.

---

## Rejection triggers (SPEC §13) — send it back without discussion if:

- a file **outside the current step's scope** was modified
- a **dependency** appeared that is not in `requirements.txt`
- **business logic drifted into `pipeline.py`** (it orchestrates only)
- an exception is **swallowed with a bare `except:` or `pass`**
- **money touches `float`**
- there is **any code the owner cannot explain out loud in 30 seconds**

That last one is the real gate: nothing gets committed if it can't be narrated
in 30 seconds, regardless of whether it works.

---

## Repo facts

- Repo root is this directory (`Beaconfire_Sales`), **not** a nested
  `sales-pipeline/` folder.
- Platform is Windows; graders may not be. `.gitattributes` enforces `eol=lf`.
- Setup uses `python -m venv venv` and `venv\Scripts\activate`.
- Container runtime is Docker Desktop.
- No secrets in the repo: `.env` is gitignored; `.env.example` holds placeholders.
