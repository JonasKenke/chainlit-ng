# Ephemeral state store

`chainlit.state_store` is a small coordination layer for short-lived state:
pending and terminal interaction records, capacity counters, idempotency keys,
locks, and pub/sub notifications. It is deliberately **not** a durable database
and not a replacement for the data layer.

Two backends share one interface:

| Backend            | Scope                     | Use                                                  |
| ------------------ | ------------------------- | ---------------------------------------------------- |
| `MemoryStateStore` | One process               | Development, tests, single-worker deployments        |
| `RedisStateStore`  | All workers sharing Redis | Multi-worker decisions, shared capacity, and pub/sub |

## Configuration

```toml
[project.state_store]
backend = "memory" # or "redis"
# redis_url = "redis://localhost:6379/0"
prefix = "chainlit"
```

`backend = "redis"` requires `redis_url`. The memory backend is intentionally
process-local; multi-worker decisions, shared capacity, and pub/sub require
Redis. Interaction records may contain question answers, so protect Redis like
the data layer: authentication, TLS, and network isolation. Install the
optional dependency:

```bash
pip install "chainlit-ng[redis]"
```

The store is selected once per process:

```python
from chainlit.state_store import get_state_store

store = get_state_store()
await store.set("key", b"value", ttl=30)
assert await store.get("key") == b"value"
```

## What it provides

- byte values with per-key TTLs
- atomic `set_if_absent`
- string sets with TTLs
- atomic integer increments
- exclusive locks with a wait budget
- publish/subscribe channels

Values and messages are bytes. Use `set_json` / `get_json` for JSON payloads.

## Limits

- Memory state is process-local and disappears on restart.
- Redis TTLs, eviction, and restarts can lose entries.
- A lock is a single-key lock, not a Redlock quorum. It serializes writers, but
  the durable record still needs a compare-and-set write or read-back check.
- Pub/sub is best-effort; a worker that is not subscribed at publish time does
  not receive the message.
- Sticky sessions are still required for the live WebSocket and the waiting
  approval/question run. The store coordinates decisions and liveness; it does
  not move the run between workers.

## Tests

The store contract is covered in `backend/tests/test_state_store.py` and runs
against both backends. Redis tests are skipped unless `CHAINLIT_TEST_REDIS_URL`
is set:

```bash
CHAINLIT_TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
  .venv/bin/python -m pytest backend/tests/test_state_store.py
```
