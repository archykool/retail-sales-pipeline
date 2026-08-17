# Object-Oriented Sales Data Pipeline

A batch ETL pipeline that reads daily retail sales files (CSV + JSON reference
data), validates every record, transforms survivors into star-schema facts, and
loads them into PostgreSQL running in Docker. Invalid records are quarantined
with typed reason codes, not dropped silently.

> **Status:** under construction. Building one step at a time per
> [`docs/SPEC.md`](docs/SPEC.md). This README is a stub and will be filled in at
> Step 14.

## Documentation

- [`docs/SPEC.md`](docs/SPEC.md) — the authoritative development specification.
- [`docs/DECISION.md`](docs/DECISION.md) — architecture decision log (ADRs).
- [`docs/Student_Project_Instructions.pdf`](docs/Student_Project_Instructions.pdf) — assignment brief.
- [`CLAUDE.md`](CLAUDE.md) — working agreement and dependency rules.

## Quick start (placeholder — finalized at Step 14)

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
```
