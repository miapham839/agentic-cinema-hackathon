# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This file builds the actual web server for this app: it asks Google ADK
# for a ready-made FastAPI server (chat endpoints + browser UI, already
# wired to app/agent.py's root_agent), then bolts two more sets of routes
# onto that same server — A2A (a different way for other agents to call
# this one) and /suggestions (our own HITL routes). The `app` object built
# at the bottom of this file is what actually gets run — see the Dockerfile
# and the `uvicorn app.fast_api_app:app` command mentioned in chat.

import contextlib
import os
from collections.abc import AsyncIterator

from dotenv import load_dotenv

# load_dotenv() reads the .env file and copies its values (CLICKHOUSE_HOST,
# etc.) into this process's environment variables, so os.getenv(...) calls
# elsewhere in the app can find them.
#
# The REAL fix for this is one file up, in app/__init__.py — it loads .env
# before its own `from .agent import app`, which is what actually matters
# (see that file's comment for the full story: importing ANY part of the
# `app` package forces app/__init__.py to run first, and it eagerly imports
# the agent code, which reads CLICKHOUSE_HOST etc. as constants at that
# exact moment). This call here is just a harmless, belt-and-suspenders
# extra safeguard for this specific file.
load_dotenv()

from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.graph_api import attach_graph_routes
from app.app_utils.project_api import attach_project_routes
from app.app_utils.suggestions_api import attach_suggestions_routes

# CORS setting: which other websites' JavaScript is allowed to call this
# API from a browser. Empty/unset means "use ADK's default" (None below).
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# The project's root folder (two levels up from this file: app/fast_api_app.py
# -> app/ -> project root). ADK needs this to find app/agent.py and load the
# agent definitions from it.
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# `lifespan` is a FastAPI hook: the code before `yield` runs ONCE when the
# server starts up (not once per request), and anything after `yield` would
# run once when the server shuts down (there isn't any here). This is where
# one-time setup work belongs — building the shared Runner, registering
# extra routes — instead of redoing it on every single request.
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Imported here (inside the function), not at the top of the file, so
    # that this import doesn't happen until the server actually starts —
    # by then .env is guaranteed loaded. `adk_app` is the ADK `App` object
    # (see the bottom of app/agent.py); `root_agent` is the supervisor
    # agent itself.
    from app.agent import app as adk_app
    from app.agent import root_agent

    # The Runner is the piece of ADK that actually executes an agent turn:
    # given a session and a new message, it runs the agent and produces a
    # reply. One Runner is built here and shared by every route below (and
    # by ADK's own /run, /run_sse routes) — everyone talks to the same
    # conversations/sessions instead of each route inventing its own.
    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    # Stash the runner (and the agent's name) on FastAPI's own app.state —
    # a place any route handler can reach into later via `request.app.state`.
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name

    # Adds the A2A (Agent2Agent) protocol routes to this same server — a
    # way for OTHER agents/systems to call root_agent programmatically,
    # separate from the human-facing chat routes ADK already built in.
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    # Adds our own /suggestions routes (app/app_utils/suggestions_api.py)
    # to this same server, the same way attach_a2a_routes just did. Unlike
    # attach_a2a_routes, this doesn't need `runner` — approve is a direct,
    # deterministic Python call (apply_graph_correction), not an agent turn.
    await attach_suggestions_routes(app)
    # Adds /graph and /graph/stream (app/app_utils/graph_api.py) — a
    # read-only live view of the story graph for a frontend visualization,
    # same no-agent-turn pattern as /suggestions.
    await attach_graph_routes(app)
    # Adds POST /project/reset (app/app_utils/project_api.py) — clears the
    # production so a different screenplay can be uploaded. Destructive, so
    # it's a UI button behind a confirmation, never an agent tool.
    await attach_project_routes(app)

    # Everything before this line ran once, at startup. `yield` hands
    # control to the running server; it stays "paused" here for as long as
    # the server is up and serving requests.
    yield


# This is the actual FastAPI application object — the thing that gets run.
# get_fast_api_app() is ADK's own builder: point it at AGENT_DIR and it
# scans that folder, finds app/agent.py's agent, and returns a fully-working
# server with the chat endpoints (/run, /run_sse), session-management
# routes, and — because web=True — the browser chat UI at /dev-ui already
# built in. `lifespan=lifespan` tells it to run OUR startup code (above)
# when the server boots, which is how the A2A and /suggestions routes end
# up attached to this exact object.
app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    # Ships traces/metrics to Google Cloud Monitoring. Left on for deployed
    # runs, but switchable off via OTEL_TO_CLOUD=false — locally the export
    # has no usable credentials/project and retries every second, filling
    # the log with "Failed to export metrics batch code: 400".
    otel_to_cloud=os.getenv("OTEL_TO_CLOUD", "true").lower() not in ("false", "0", "no"),
    lifespan=lifespan,
)
app.title = "interactive-cinema-agent"
app.description = "API for interacting with the Agent interactive-cinema-agent"


# Only runs if this file is executed directly (`python app/fast_api_app.py`)
# rather than imported by something else (like uvicorn normally does via
# `uvicorn app.fast_api_app:app`, which just imports `app` and never hits
# this block). A convenience for quick local testing.
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
