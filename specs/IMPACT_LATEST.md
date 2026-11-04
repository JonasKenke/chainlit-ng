## Target

Selective upstream security backport: `backend/chainlit/auth/cookie.py`, backend dependency floors, and the pnpm immutable override.

## Dependents (3)

- `backend/chainlit/server.py`: sets and clears auth cookies during login/logout and validates OAuth state on callback.
- `backend/tests/auth/test_cookie.py`: exercises cookie setup, cleanup, and lifetime behavior; focused tests now cover sub-path cookies and OAuth state.
- `backend/pyproject.toml` and `pnpm-workspace.yaml`: package metadata and frontend dependency resolution; lockfiles retain patched versions.

## Affected Stories

- None: this checkout has no `specs/release-plan.yaml` or epic capsules.

## Test Coverage

- `backend/tests/auth/test_cookie.py`: 13 passed, including root-path cookie behavior and matching, missing, and mismatched OAuth state.
- Backend suite: 802 passed, with 18 existing warnings. Frontend: 62 passed.
- Frontend, React-client, Copilot, and root pnpm audits: zero advisories after resolving `immutable` to 3.8.4.
- Type-checks, lint, formatting, `uv lock --check`, and all JS builds passed.

## Risk: High

The auth module protects OAuth state and authentication cookies; the dependency changes are version floors and overrides for patches already resolved in the lockfiles.

## Recommended action

Publish this selective backport as `2.11.12.dev2`; do not merge unrelated upstream commits.
