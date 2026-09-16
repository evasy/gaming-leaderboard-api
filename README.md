# Global Gaming Leaderboard API

A production-shaped REST service that ranks players by score, in real time,
across any number of games — with all-time, daily and weekly boards maintained
from a single submission.

[![CI](https://github.com/evasy/gaming-leaderboard-api/actions/workflows/ci.yml/badge.svg)](https://github.com/evasy/gaming-leaderboard-api/actions/workflows/ci.yml)

**Live:** <https://leaderboard-api-juove.ondigitalocean.app> · [interactive docs](https://leaderboard-api-juove.ondigitalocean.app/docs) · [health](https://leaderboard-api-juove.ondigitalocean.app/readyz) · [metrics](https://leaderboard-api-juove.ondigitalocean.app/metrics)

Deployed on DigitalOcean App Platform: 2 instances behind a managed load
balancer, backed by a managed Valkey cluster.

> **Architecture, trade-offs and scaling notes live in
> [`docs/architecture.md`](docs/architecture.md).** Start there for the "why".

---

## Contents

- [Quick start](#quick-start)
- [API](#api)
- [Design in one minute](#design-in-one-minute)
- [Configuration](#configuration)
- [Testing](#testing)
- [Operations](#operations)
- [Deployment](#deployment)
- [Project layout](#project-layout)

---

## Quick start

Requires Python 3.11+. Redis is optional for local development.

```bash
make install     # create .venv and install dependencies
make run         # serve on http://localhost:8080 with the in-memory backend
```

Then open <http://localhost:8080/docs> for interactive OpenAPI documentation.

Run it the way production does — API plus a real Redis:

```bash
make up          # docker compose: API + Redis
make smoke       # end-to-end checks against the running instance
```

Or point the app at a Redis you already have:

```bash
LEADERBOARD_STORE_BACKEND=redis LEADERBOARD_REDIS_URL=redis://localhost:6379/0 make run
```

### 30-second tour

```bash
# Submit some scores
curl -sX POST localhost:8080/v1/games/space-invaders/scores \
  -H 'content-type: application/json' \
  -d '{"user_id":"alice","score":900,"display_name":"Alice A."}' | jq

curl -sX POST localhost:8080/v1/games/space-invaders/scores \
  -H 'content-type: application/json' -d '{"user_id":"bob","score":900}' | jq

curl -sX POST localhost:8080/v1/games/space-invaders/scores \
  -H 'content-type: application/json' -d '{"user_id":"carol","score":700}' | jq

# Top of the board — note that the tie shares rank 1 and carol is 3rd
curl -s 'localhost:8080/v1/games/space-invaders/leaderboard?limit=10' | jq

# Where am I, and who is near me?
curl -s 'localhost:8080/v1/games/space-invaders/users/carol/context?radius=1' | jq

# Today's board only
curl -s 'localhost:8080/v1/games/space-invaders/leaderboard?window=daily' | jq
```

---

## API

Base path `/v1`. Full OpenAPI schema at `/openapi.json`, browsable at `/docs`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/games/{game_id}/scores` | Submit a score |
| `GET` | `/v1/games/{game_id}/leaderboard` | Top X players, paginated |
| `GET` | `/v1/games/{game_id}/users/{user_id}` | One player's score and rank |
| `GET` | `/v1/games/{game_id}/users/{user_id}/context` | A player's rank plus neighbours |
| `DELETE` | `/v1/games/{game_id}/users/{user_id}` | Erase a player from every board |
| `GET` | `/v1/games` | List known games |
| `GET` | `/healthz` · `/readyz` · `/metrics` | Liveness · readiness · Prometheus |

All endpoints accept `?window=all_time|daily|weekly` (default `all_time`).

### Submit a score

```http
POST /v1/games/space-invaders/scores
```

```json
{
  "user_id": "alice",
  "score": 9000,
  "mode": "best",
  "display_name": "Alice A.",
  "idempotency_key": "match-8f21c4"
}
```

| Field | Required | Notes |
|---|---|---|
| `user_id` | yes | 1–64 chars, `[A-Za-z0-9._-]`, must start alphanumeric |
| `score` | yes | Integer within the configured bounds |
| `mode` | no | `best` (default), `absolute`, or `increment` |
| `display_name` | no | 1–64 chars, shown on the board |
| `idempotency_key` | no | Repeat submissions with the same key are applied once |

**Score modes**

- **`best`** — keep the higher of the old and new score. Arcade semantics; a bad
  run never costs you your high score.
- **`absolute`** — overwrite unconditionally, including downward. For an
  authoritative resync from the game server, or a correction.
- **`increment`** — add to a running total. For accumulating seasons and battle
  passes. Negative values are allowed, so penalties are expressible.

Returns `201` with the player's standing on **each** window:

```json
[
  {"game_id": "space-invaders", "user_id": "alice", "window": "all_time",
   "score": 9000, "previous_score": 7000, "rank": 1, "updated": true, "deduplicated": false},
  {"...": "daily"},
  {"...": "weekly"}
]
```

### Top X players

```http
GET /v1/games/space-invaders/leaderboard?limit=10&offset=0&window=all_time
```

```json
{
  "game_id": "space-invaders",
  "window": "all_time",
  "total_players": 4,
  "limit": 10,
  "offset": 0,
  "entries": [
    {"user_id": "carol", "display_name": null,      "score": 900, "rank": 1},
    {"user_id": "bob",   "display_name": null,      "score": 900, "rank": 1},
    {"user_id": "alice", "display_name": "Alice A.", "score": 700, "rank": 3}
  ]
}
```

`limit` is 1–1000 (default 10). Ranks are **absolute**, so page 5 reports ranks
41–50, not 1–10.

### A player's surroundings

```http
GET /v1/games/space-invaders/users/alice/context?radius=2
```

```json
{
  "game_id": "space-invaders",
  "window": "all_time",
  "total_players": 10482,
  "user":  {"user_id": "alice", "score": 700, "rank": 5123},
  "above": [{"user_id": "zoe", "score": 712, "rank": 5121},
            {"user_id": "yan", "score": 704, "rank": 5122}],
  "below": [{"user_id": "wes", "score": 699, "rank": 5124},
            {"user_id": "vic", "score": 690, "rank": 5125}]
}
```

`above` is ordered nearest-last and `below` nearest-first, so concatenating
`above + [user] + below` gives one correctly ordered strip. Both lists clamp at
the ends of the board.

### Ranking rules

1-based **competition ranking**: tied players share the better rank and the next
distinct score skips ahead.

| Player | Score | Rank |
|---|---|---|
| carol | 900 | **1** |
| bob | 900 | **1** |
| alice | 700 | **3** |

Within a tie, ordering is by descending `user_id` — arbitrary but deterministic,
so pagination never shows or skips a player twice.

### Errors

Every error is [RFC 7807](https://www.rfc-editor.org/rfc/rfc7807)
`application/problem+json`:

```json
{
  "type": "https://docs.leaderboard.dev/errors/validation_failed",
  "title": "Validation Failed",
  "status": 422,
  "detail": "One or more fields failed validation.",
  "code": "validation_failed",
  "instance": "/v1/games/space-invaders/scores",
  "request_id": "0f3c1a9e4b7d4c2f8a1e6b5d3c7a9f21",
  "errors": [{"field": "score", "message": "Input should be a valid integer", "type": "int_parsing"}]
}
```

| Status | When |
|---|---|
| `422` | Validation failed — always names the offending field |
| `404` | Player has no score on that board |
| `503` | Backing store unreachable |
| `500` | Unexpected — logged in full, never echoed to the client |

`request_id` is echoed in the `X-Request-ID` header and appears in every log
line for that request. Send your own `X-Request-ID` and it is preserved.

---

## Design in one minute

```
HTTP  →  routes (app/api)      thin; HTTP in, HTTP out
      →  schemas (app/models)  validation at the edge
      →  service (app/service) windows, score modes, idempotency, bounds
      →  store  (app/store)    persistence only
```

The store is an abstract port with two adapters:

| | `memory` | `redis` |
|---|---|---|
| Structure | `SortedList` index | sorted sets (ZSET) |
| Submit / rank | `O(log n)` | `O(log N)` |
| Survives restart | no | yes |
| Multiple replicas | no | yes |
| Use for | tests, local dev | **production** |

Both are held to the *same* conformance suite, so they cannot diverge. See
[`docs/architecture.md`](docs/architecture.md) for the reasoning, the Lua
concurrency story, and the scaling path.

---

## Configuration

Every setting is an environment variable prefixed `LEADERBOARD_`. All have safe
defaults; see [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `LEADERBOARD_STORE_BACKEND` | `memory` | `memory` or `redis` |
| `LEADERBOARD_REDIS_URL` | `redis://localhost:6379/0` | Redis DSN |
| `LEADERBOARD_REDIS_MAX_CONNECTIONS` | `50` | Connection pool ceiling |
| `LEADERBOARD_ENVIRONMENT` | `local` | `local`/`dev`/`staging`/`production` |
| `LEADERBOARD_LOG_LEVEL` | `INFO` | Log verbosity |
| `LEADERBOARD_LOG_FORMAT` | `json` | `json` or `console` |
| `LEADERBOARD_MIN_SCORE` / `MAX_SCORE` | `0` / `1000000000` | Accepted score domain |
| `LEADERBOARD_MAX_PAGE_SIZE` | `1000` | Upper bound on `limit` |
| `LEADERBOARD_MAX_CONTEXT_RADIUS` | `50` | Upper bound on `radius` |
| `LEADERBOARD_DAILY_TTL_SECONDS` | `691200` (8 d) | Daily board retention |
| `LEADERBOARD_WEEKLY_TTL_SECONDS` | `3024000` (35 d) | Weekly board retention |
| `LEADERBOARD_IDEMPOTENCY_TTL_SECONDS` | `86400` (1 d) | Replay window |
| `LEADERBOARD_CORS_ALLOW_ORIGINS` | `*` | Comma-separated origins |
| `LEADERBOARD_DOCS_ENABLED` | `true` | Serve `/docs` and `/openapi.json` |

Invalid configuration fails at **startup**, not on the first request.

---

## Testing

```bash
make test     # full suite
make cov      # with a coverage report
make check    # lint + types + tests, exactly what CI runs
```

The suite has three layers:

| File | Layer | What it pins down |
|---|---|---|
| `tests/test_units.py` | pure functions | window bucketing (incl. UTC rollover, ISO week/year boundaries), competition-rank assignment, config parsing |
| `tests/test_store_conformance.py` | storage | ranking, ties, all three score modes, pagination, context clamping, deletion, idempotency, concurrency — **run against every backend** |
| `tests/test_api.py` | HTTP | status codes, payload shapes, validation rejections, `problem+json` contract, health and metrics endpoints |

Redis-backed parameters are **skipped**, not failed, when no Redis is reachable
— so `make test` works on a clean machine, while CI runs both backends against a
real Redis service container.

```bash
# Exercise the Redis backend locally
docker run -d -p 6379:6379 redis:7-alpine
make test
```

Worth a look if you are reviewing: `test_best_mode_is_safe_under_concurrent_submissions`
fires five concurrent submissions at one player and asserts the highest score
survives — the race that motivated the Lua script.

Beyond the unit suite:

```bash
./scripts/smoke.sh https://leaderboard-api-juove.ondigitalocean.app   # deployed-artifact checks
make bench BASE_URL=http://localhost:8080                # latency percentiles
```

---

## Operations

**Health.** `/healthz` is liveness — it answers "is the process up?". `/readyz`
is readiness — it pings the store and returns `503` when the store is
unreachable. They are separate on purpose: losing Redis should pull an instance
out of rotation, not restart it in a loop.

**Logs.** One JSON object per line via `structlog`, every line carrying
`request_id`. Set `LEADERBOARD_LOG_FORMAT=console` locally for readable output.

```json
{"event":"score_submitted","game_id":"space-invaders","user_id":"alice","mode":"best","score":9000,"rank":1,"updated":true,"request_id":"0f3c…","level":"info","timestamp":"2026-09-16T17:42:11.004Z"}
```

**Metrics.** Prometheus text at `/metrics`:

| Metric | Type | Labels |
|---|---|---|
| `leaderboard_http_requests_total` | counter | `method`, `route`, `status` |
| `leaderboard_http_request_duration_seconds` | histogram | `method`, `route` |
| `leaderboard_score_submissions_total` | counter | `game_id`, `mode`, `result` |
| `leaderboard_store_operation_duration_seconds` | histogram | `backend`, `operation` |

Routes are labelled by **template** (`/v1/games/{game_id}/scores`), not by raw
path, so per-player ids cannot explode metric cardinality.

---

## Measured performance

One uvicorn worker against a single Redis, 5,000 players on the board, load
generator on the same host (so these are conservative — they include the
generator's own CPU contention):

| Operation | p50 | p95 | p99 |
|---|---|---|---|
| `POST /scores` (fans out to 3 windows) | 3.8 ms | 6.0 ms | 8.7 ms |
| `GET /leaderboard?limit=100` | 6.0 ms | 8.1 ms | 17.2 ms |
| `GET /users/{id}` | 2.8 ms | 4.5 ms | 5.6 ms |
| `GET /users/{id}/context?radius=5` | 2.9 ms | 4.3 ms | 5.4 ms |

Reproduce with `make bench BASE_URL=...`. The point is not the absolute
numbers — it is that rank lookup does not care how large the board is, because
nothing on the read path is proportional to player count.

---

## Deployment

CI (`.github/workflows/ci.yml`) runs lint, type checks, tests on three Python
versions against a real Redis, and a Docker build whose image is smoke-tested
before anything ships. `main` deploys only when all of that is green.

### DigitalOcean App Platform

```bash
doctl auth init

# 1. Managed Valkey for leaderboard state
doctl databases create leaderboard-redis --engine valkey \
  --size db-s-1vcpu-1gb --region nyc3 --num-nodes 1

# 2. Create the app from the spec (it already points at this repo;
#    change the `github.repo` field if you forked it)
doctl apps create --spec .do/app.yaml

# 3. Verify the deployed artifact
doctl apps list
./scripts/smoke.sh https://leaderboard-api-juove.ondigitalocean.app
```

The spec ([`.do/app.yaml`](.do/app.yaml)) runs two instances, binds the managed
Valkey DSN, and gates rollout traffic on `/readyz`. CPU-metric autoscaling is
the preferred configuration but App Platform allows it only on dedicated
instance slugs; the spec documents that upgrade path inline.

`deploy_on_push` is deliberately **off**. App Platform's native auto-deploy
fires in parallel with CI, so a commit failing its tests would still ship.
Deployment instead runs from the `deploy` job in CI, after the full matrix and
the image smoke test pass.

For continuous deployment, add `DIGITALOCEAN_ACCESS_TOKEN` and `DO_APP_ID` as
repository secrets; the `deploy` job then runs on every green push to `main`.

---

## Project layout

```
app/
  main.py               application factory, middleware, lifespan
  models.py             request/response schemas and validation
  service.py            use cases: windows, score modes, idempotency
  api/
    routes.py           leaderboard endpoints
    health.py           healthz / readyz / metrics
    deps.py             dependency injection
  core/
    config.py           environment-driven settings
    errors.py           domain errors + RFC 7807 rendering
    observability.py    structured logging, request ids, metrics
    windows.py          all-time / daily / weekly bucketing
  store/
    base.py             the storage port + shared ranking logic
    memory.py           in-process ordered index (dev, tests)
    redis_store.py      sorted sets + Lua (production)
    factory.py          backend selection
tests/                  units, cross-backend conformance, HTTP
scripts/
  smoke.sh              end-to-end checks against a live instance
  bench.py              latency/throughput probe
docs/architecture.md    diagrams, trade-offs, scaling path
.do/app.yaml            DigitalOcean App Platform spec
```

---

## License

MIT
