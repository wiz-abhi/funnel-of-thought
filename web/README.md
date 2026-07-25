# Funnel of Thought — web UI

A one-screen, deployable view of the project's finding: an AI agent's reasoning
contract `plan → tool → validate → respond`, measured as a SigNoz Trace Funnel.

**A counter measures presence; a funnel measures sequence.**

Over 125 real traces from the observed agent:

| measurement                                        | result                |
| -------------------------------------------------- | --------------------- |
| naive span counter — `agent.validate` present       | **125/125 = 100.0 %** |
| ordered funnel — plan 125 → tool 125 → validate 80 → respond 80 | **64.0 %** |
| gap                                                 | **36.0 pp = 45 traces** |

Those 45 traces emitted a `agent.validate` span *before* the tool result it was
supposed to be checking even existed. A `GROUP BY span name, COUNT` cannot see
that; a funnel can, because it requires each step to happen **after** the one
before it.

## Two data modes

The page always tells you which one it is rendering, with a badge in the header.
Snapshot data is never presented as live.

| mode                                     | what it reads                                                                                                  | badge                                      |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| **live**                                 | the real SigNoz funnel analytics, via the documented client in [`../fot/signoz.py`](../fot/signoz.py) — no REST logic is reimplemented here | green — `LIVE · reading your SigNoz`       |
| **snapshot** (what the public deploy uses) | the frozen fixture in [`data/snapshot.json`](data/snapshot.json), captured from a real run on 2026-07-22        | amber — `SNAPSHOT · frozen from a real run` |

Selection is automatic: live is used when credentials **and** a reachable SigNoz
exist, otherwise the app falls back to the snapshot and prints the reason in the
footer. A public deploy cannot reach a laptop's `localhost:8080`, so the hosted
demo is honestly labelled SNAPSHOT.

### Environment

| variable                                        | default                 | meaning                              |
| ----------------------------------------------- | ----------------------- | ------------------------------------ |
| `FOT_MODE`                                      | `auto`                  | `auto` \| `live` \| `snapshot`       |
| `SIGNOZ_URL`                                    | `http://localhost:8080` | SigNoz base URL                      |
| `SIGNOZ_API_KEY` / `SIGNOZ_JWT` / `SIGNOZ_EMAIL` + `SIGNOZ_PASSWORD` | —   | auth, resolved in that order by the client |
| `FOT_FUNNEL`                                    | `cognition`             | funnel name                          |
| `FOT_SERVICE`                                   | `fot-agent`             | `service.name` on the observed spans |
| `FOT_WINDOW_DAYS`                               | `30`                    | analytics lookback window            |

Funnel-analytics reads work with an API key. The naive ClickHouse counter needs
`docker exec` access to the SigNoz ClickHouse container; when that is not
available, live mode degrades that one number rather than inventing it.

## Routes

| route                | returns                                                    |
| -------------------- | ---------------------------------------------------------- |
| `GET /`              | the page                                                   |
| `GET /api/funnel`    | the exact JSON the page renders (`?refresh=1` skips the 30 s cache) |
| `GET /healthz`       | `{"status": "ok"}`                                         |
| `GET /docs`          | OpenAPI                                                    |

## Run locally

```bash
cd web
pip install -r requirements.txt

# snapshot mode (no SigNoz needed)
FOT_MODE=snapshot uvicorn app:app --reload --port 7860

# live mode against a local SigNoz
export SIGNOZ_URL=http://localhost:8080
export SIGNOZ_API_KEY=...        # or SIGNOZ_JWT, or SIGNOZ_EMAIL + SIGNOZ_PASSWORD
FOT_MODE=live uvicorn app:app --reload --port 7860
```

Then open <http://localhost:7860>.

## Docker

Build from the **repo root** so the sibling `fot/` package is in the context:

```bash
docker build -f web/Dockerfile -t fot-web .
docker run --rm -p 7860:7860 fot-web
```

`Dockerfile.dockerignore` keeps that context small (BuildKit prefers a
`<dockerfile>.dockerignore` over a root `.dockerignore`), so `.git`, `.venv` and
the rest of the repo never enter the build.

## Deploy to HuggingFace Spaces

Spaces expects the app on **port 7860**; the Dockerfile already does that.

1. Create a Space: <https://huggingface.co/new-space> → SDK **Docker** → Blank →
   name it e.g. `funnel-of-thought`.
2. Push this repo to the Space remote (from the repo root):

   ```bash
   git remote add space https://huggingface.co/spaces/<your-username>/funnel-of-thought
   git push space main
   ```

3. Spaces builds `Dockerfile` at the repo root. This repo keeps its Dockerfile in
   `web/`, so add a root-level `Dockerfile` that is a copy of `web/Dockerfile`
   (its `COPY` paths are already repo-root relative), **or** point the Space at it
   with a `dockerfile_path` entry in the Space README front matter:

   ```yaml
   ---
   title: Funnel of Thought
   emoji: 🫙
   colorFrom: green
   colorTo: gray
   sdk: docker
   app_port: 7860
   dockerfile_path: web/Dockerfile
   ---
   ```

4. The Space runs in `snapshot` mode by default — correct, since it cannot reach
   your laptop's SigNoz. To force it explicitly, add a Space **variable**
   `FOT_MODE=snapshot` under Settings → Variables and secrets. If you ever point
   it at a publicly reachable SigNoz, add `SIGNOZ_URL` as a variable and
   `SIGNOZ_API_KEY` as a **secret**, and set `FOT_MODE=auto`.

## Stack

FastAPI + Jinja2 + Tailwind via CDN and vanilla JS — no npm, no build step, no
charting library. The funnel bars and the violating-trace waterfall are plain
divs.
