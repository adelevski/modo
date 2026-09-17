# modo

A meeting-point service that minimizes the longest drive among several origins.
The public interface is minimax-only with a fixed 60-second region; library
APIs may retain other objectives, and experiments are not production.

## Scope and sources

- Read `README.md` for setup, `docs/model.md` for mathematical contracts,
  `docs/architecture.md` for boundaries, and `SERVICE.md` for the hosted policy.
- Read the [snowball principles](https://snowball-projects.github.io/principles/)
  before public claims or product, data and architecture decisions. Keep the
  simplest reliable design; measure before adding infrastructure.
- Keep modo and fairway independent. Preserve exact results over the named
  graph, complete regions, explicit provenance, and clear failures at limits.

## Development and verification

- Use Python 3.12+ and the checked-in `uv.lock`; install with
  `uv sync --extra app --extra test --locked`.
- Before shipping, run `uv run --locked ruff check .`,
  `uv run --locked ruff format --check .`,
  `uv run --locked python -m pytest tests experiments`, and
  `uv run --locked python -m build`.
- For touched experiments, run their documented checks. For browser changes,
  run `node --check` on changed JavaScript and inspect keyboard and mobile
  behavior.
- Snapshot changes also require `scripts/validate_snapshot.py`; production
  snapshots must remain checksummed, directed and positively weighted.
- Preserve existing local work. Remove code or tests only when their purpose is
  demonstrably obsolete; keep regression and backend-equivalence coverage.

## Data and privacy

- Never commit or print `.env` values, credentials, personal locations or
  benchmark results. Keep `.env.example` to empty variable declarations.
- No accounts, analytics, advertising or stored origin coordinates. Document
  provider disclosures and limits before introducing an external service.
- Do not run bulk provider calls or expand paid infrastructure without an
  explicit budget. A provider key does not establish data reuse rights.

## Publication

- `.github/workflows/ci.yml` defines the supported runtime and SciPy-floor
  checks; `render.yaml` defines deployment. Keep runtime pins and lockfiles
  consistent.
- Verify the workflow and the live service before claiming publication. Do not
  force-push shared history.

## Stewardship

- Write `modo` in lowercase. Do not change product direction by assumption.
- Original software is MIT. Preserve the separate road-data and Leaflet
  notices; `LICENSE`, `THIRD-PARTY-NOTICES.md` and `CONTRIBUTING.md` are authoritative.
- Do not add AI-builder labels or production credits to public copy.
- Use regular hyphens instead of em dashes. Keep public copy terse and honest.
- `CLAUDE.md` imports this file. Keep operational detail in docs rather than
  duplicating agent instructions.
