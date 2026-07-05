# Skillmesh AI Delivery Contract

This repository uses an L4 delivery workflow: an AI agent executes the full
engineering cycle and a human only accepts or rejects the final pull request.

## Required workflow

1. Read the internal spec supplied for the run and record its SHA-256. Never
   copy private spec content into committed files or a pull request.
2. Create a new `.ai/runs/<run-id>/` directory. Run IDs are immutable; a retry
   uses a new ID and references the failed run in `run.json`.
3. Write `plan.md` before editing code. Cover behavior, safety, compatibility,
   tests, and documentation.
4. Implement without asking a human to edit code or make an implementation
   decision. If the spec is incomplete or contradictory, stop with `blocked`.
5. Record material commands in `commands.jsonl` and checks in `tests.json`.
6. Review the completed diff in `review.md` with `verdict: PASS` or `FAIL`.
7. Write `final.json`, then run `python3 scripts/l4_gate.py validate ...`.
8. Put only the generated sanitized summary in the pull request. The human's
   only role is the final approve/reject decision.

## Mandatory quality gates

- `python -m ruff check .`
- `python -m mypy skillmesh`
- `python -m pytest tests/ -q -p no:cacheprovider` with an isolated `HOME`
- `python skillmesh.py --help` and `python skillmesh.py --version`
- No destructive test may access real Agent, config, backup, or hub paths.

## Safety rules

- Preserve runtime zero-dependency support.
- Preserve confirmation semantics for destructive user operations.
- Never commit secrets, personal paths, private spec text, or full transcripts.
- Never rewrite or delete previous run evidence.
