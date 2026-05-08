# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository

`orxhestra` is a Python multi-agent orchestration framework (PyPI package, Apache-2.0). It publishes both a library (`import orxhestra`) and a CLI (`orx`). This checkout is a **fork** of `NicolaiLassen/orxhestra` — see `JEPRAS-GITHUB-WORKFLOW.md` for the fork workflow (origin = fork, upstream = original, never commit to `main`, work on `feat/jepras/*` branches, propose via PR to upstream).

## Commands

Use **uv**, not pip. Python >= 3.10.

```bash
# Install with all test-relevant extras (mirrors CI exactly)
uv pip install -e ".[mcp,a2a,composer,database]"
uv pip install "pytest>=9.0" "pytest-asyncio>=0.25"

# Full test suite (what CI runs)
pytest -v

# Single test file / single test
pytest tests/test_runner.py -v
pytest tests/test_runner.py::test_name -v

# Lint (CI gate — only lints the orxhestra/ package, not tests/)
ruff check orxhestra/

# Build distribution
python -m build

# Run the CLI from source
orx                              # uses ./orx.yaml or the bundled default
orx my-agents.yaml --serve -p 9000
```

Tests use `pytest-asyncio` in `asyncio_mode = "auto"` — async test functions do not need the `@pytest.mark.asyncio` decorator. Default fixture loop scope is `function`.

Optional-dependency extras are numerous (29 LLM providers + `auth`, `database`, `mcp`, `composer`, `cli`, `a2a`). Install only what a given task needs; `all` pulls the common set. The `cli` extra requires `pyinklib>=1.1.15` (a custom dep pinned tightly — see commit 80539c3).

## Architecture

### The core loop

Everything flows through `Runner` → `BaseAgent.astream()` → `Event` stream. The `Runner` (`orxhestra/runner.py`) is the single entry point: it loads or creates a `Session` via a `BaseSessionService`, builds an `InvocationContext`, streams events from the agent, and persists every event back to the session. Callers only consume the event iterator.

`Event` (`orxhestra/events/event.py`) is a **single unified type** — no subclasses. An `EventType` enum plus the `Content` payload (typed `TextPart` / `DataPart` / `FilePart` / `ToolCallPart` / `ToolResponsePart` / `ThinkingPart`) carries everything. `to_langchain_message()` / `from_langchain_message()` bridge to LangChain messages.

### Agent hierarchy

All agents extend `BaseAgent` (`orxhestra/agents/base_agent.py`) and override `astream()`. The public surface matches LangChain's `Runnable` interface (`astream` / `ainvoke` / `stream` / `invoke`).

- `LlmAgent` — the workhorse. Implements a **manual tool-call loop** using `BaseChatModel.bind_tools(...).astream(...)`. **No LangGraph** — orchestration is pure Python async. Read the module docstring at `orxhestra/agents/llm_agent.py` before editing; it describes the loop, branch/invocation filtering, visibility filtering, compaction, and `include_contents` behavior.
- `ReActAgent` — reasoning+acting loop.
- `SequentialAgent` / `ParallelAgent` / `LoopAgent` — composite agents. They derive child `InvocationContext`s with distinct `branch` values so each agent's history is isolated in the event stream.
- `A2AAgent` — calls a remote agent over the A2A protocol.

### InvocationContext & branching

`InvocationContext` (`orxhestra/agents/invocation_context.py`) is the runtime state object threaded through a call tree. Key fields:
- `branch` — dot-separated path (e.g. `"root.child"`) used to filter which events an agent sees. Composite agents call `derive()` to give children a new branch.
- `invocation_id` — one per top-level run; isolates LoopAgent iterations and separate runs.
- `state` — mutable per-run key/value store (not auto-persisted — the Runner handles persistence).
- `agent_states` — per-agent persisted state, keyed by agent name. The Runner now honors an `active_agent_state_key` so sub-agents can continue multi-turn (see commits 3d764d3/15ce540).

`ReadonlyContext` (planners) and `CallbackContext` (callbacks) are constrained views over the same state.

### Sessions, compaction, middleware

- `BaseSessionService` has two concrete backends: `InMemorySessionService` and `DatabaseSessionService` (needs `[database]` extra — SQLAlchemy + aiosqlite).
- `CompactionConfig` passed to `Runner` triggers automatic LLM-driven summarization of old events after each turn. Compacted events carry `actions.compaction` and replace their timestamp range in the LLM context (see `orxhestra/sessions/compaction.py`).
- **Middleware** (`orxhestra/middleware/`) wraps agent invocations, LLM calls, tool calls, and events using an onion pattern (first-registered is outermost). `CallbackMiddleware` provides backward compatibility with the older `LlmAgentCallbacks` API.

### Tools

Tool implementations live in `orxhestra/tools/`. `function_tool` decorates a Python callable into a tool; `AgentTool` exposes an entire agent as a tool to another agent; `make_transfer_tool` lets an agent hand control off. Notable tool modules: `filesystem.py`, `shell.py`, `todo_tool.py`, `task_tools.py` (background-task spawn/monitor lifecycle), `memory_tools.py` (auto-memory save/load), `long_running_tool.py`.

`FilesystemBackend` (`orxhestra/filesystem/`) is a protocol with `LocalFilesystemBackend` and `InMemoryFilesystemBackend` implementations — filesystem tools route through this so tests can run against in-memory FS without touching disk.

### Composer (declarative YAML)

`orxhestra/composer/` turns a YAML document into an agent tree. `Composer` is the entry point; `builders/` contains per-type builders (agents, models, tools) with three extension registries exported from `orxhestra.composer`: `register_builder`, `register_provider`, `register_builtin_tool`. The built-in coding agent is at `orxhestra/cli/orx.yaml` (shipped as package data via `[tool.setuptools.package-data]`).

### CLI (`orx`)

Entry point: `orxhestra.cli.app:main` (see `[project.scripts]`). The CLI is built on `pyinklib` (an Ink-style terminal UI). Key modules: `app.py` (arg parsing + lifecycle), `ink_app.py` (UI), `commands.py` (slash commands), `approval.py` (tool approval prompts), `summarization.py` (`/compact`), `memory.py` (`/memory`, auto-memory persistence), `context_injection.py` (auto language / git / package-manager context), `builder.py` / `builtins.py` (loads orx.yaml into a Composer). CLI can run as a REPL, a single `-c` command, or an A2A server (`--serve`).

### A2A and MCP

- `orxhestra/a2a/` — server and type converters for the Agent-to-Agent protocol (FastAPI-based, needs `[a2a]` extra).
- `orxhestra/integrations/mcp/` — Model Context Protocol client/adapter (needs `[mcp]` extra — `fastmcp`).

### Signing / auth (optional)

`orxhestra/auth/` provides Ed25519 event signing. `BaseAgent` exposes `signing_key` / `signing_did` parameters; when set, emitted events are signed (agent-level signing key takes priority over the context-level key). Requires `[auth]` extra (`cryptography`, `base58`, `PyJWT`).

## Conventions

- **Ruff** is configured for `py310`, line length 100, rules `E`, `F`, `I`, `UP`. CI only lints `orxhestra/` — test files are exempt.
- Module-level docstrings are rich and describe *the contract* (loop steps, filtering rules, extension points). Preserve this style when editing; when the behavior changes, the docstring is usually the source of truth to update.
- Public surface is deliberately re-exported from `orxhestra/__init__.py`. When adding a new public type, also add it there and to the relevant sub-package `__init__.py`.
- Examples in `examples/` are executable demos (not tests) — they double as documentation. If you change public API, check whether an example needs updating.
- Tests mirror module names (`tests/test_<module>.py`).

## Docs

Documentation source lives in `docs/` (MDX format for docs.orxhestra.com) and `docs/skills/` (code-level references). `docs/docs.json` is the site navigation manifest.
