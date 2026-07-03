# 0004 — Library-first core; disposable Streamlit-class dashboard; no API server in MVP

Date: 2026-07-03
Status: Accepted

## Context

The MVP's goal is research velocity (answering "why is RN1 profitable?"), but the platform should later be commercializable as a SaaS product (FastAPI + React + multi-tenant). Building a product UI/API first would slow the research loop and would likely be discarded once the product is actually understood.

## Decision

1. **All ingestion, projections, exposure computation, analytics, detectors, reports, SQL access, and calculations live in one importable Python core library.** It is the only place business logic exists.
2. **The MVP UI is a Streamlit-class research shell and is disposable by contract**: it may only call core-library functions and render results. No metric computation, no raw SQL, no detection logic in dashboard code.
3. **No FastAPI/React in MVP.** The commercialization seam is the core library, not an early API server. Migration path: keep the library → wrap it in FastAPI → add React → retire the Streamlit shell.
4. Consumers of the library from day one: Streamlit dashboard, Jupyter notebooks, CLI tools; later: FastAPI backend.
5. **Enforcement**: project structure separates `core` (library) from `apps/dashboard` (shell); the deletion test is the acceptance criterion — *deleting the dashboard folder must leave ingestion, projections, analytics, reports, and detectors fully functional via library + CLI*. Tests import only the core library; dashboard code is excluded from coverage of business logic.

## Consequences

- New charts/views cost minutes; notebooks are first-class research surfaces for free.
- The dashboard can be thrown away at commercialization time with zero logic loss — what is discarded is genuinely disposable.
- Discipline burden: reviewers must reject any PR that puts logic in the shell; the deletion test makes violations detectable.

## Stack (confirmed alongside this decision)

Python throughout; SQLite + Alembic; Polars (DuckDB optional for ad-hoc/Parquet); APScheduler in-process for scheduling (no queues/brokers); Docker Compose on Dokploy VPS with code-only images and a mounted `/data` volume.
