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

from dotenv import load_dotenv

# Must run before the .agent import below. Importing ANY submodule of this
# package (e.g. `app.fast_api_app`, even `app.schemas`) requires Python to
# import this __init__.py first, which then eagerly imports .agent ->
# .tools -> .app_utils.clickhouse_client — and that module reads
# CLICKHOUSE_HOST/PORT/USER/PASSWORD as module-level constants, once, at
# import time. If .env hasn't been loaded yet at that exact moment, those
# constants get baked in as their placeholder defaults, permanently, for
# the life of the process (Python caches the module; a later load_dotenv()
# elsewhere can't retroactively fix an already-evaluated `os.getenv(...)`).
# `adk web`/`agents-cli playground` never hit this, since ADK's own CLI
# loads .env at its own entry point before touching this package at all —
# but `uvicorn app.fast_api_app:app` (the Dockerfile's actual CMD) imports
# this package directly, with nothing upstream guaranteed to have loaded
# .env first. Loading it here, as literally the first thing this package
# does, makes it correct regardless of entrypoint.
load_dotenv()

from .agent import app  # noqa: E402  (deliberate; see the comment above)

__all__ = ["app"]
