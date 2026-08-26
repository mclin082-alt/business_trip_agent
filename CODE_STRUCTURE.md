# Code Structure Overview

## Directory Layout

```
business_trip_agent/
├── main.py                          # Core backend: FastAPI + WebSocket + Qwen agent loop
├── multi_agent.py                   # Typed delegation, proposal/review and conflict governance
├── skills.py                        # Skill abstraction layer: tool contracts, permissions, risk levels
├── approval_utils.py                # Shared utility functions for approval classification & guardrails
├── session_store.py                 # Session serialization & SQLite persistence
├── tests/                            # Unit and contract tests
│   ├── test_session_store.py         # Session store round-trip
│   ├── test_memory_store.py          # Memory behavior and safety
│   ├── test_context_manager.py       # Context assembly and compaction
│   ├── test_calendar_adapter.py      # Calendar adapter contracts
│   └── test_multi_agent.py           # Multi-agent governance and wiring
├── generate_skill_registry_doc.py   # Script to generate skills_registry.md from SKILL definitions
├── requirements.txt                 # Python dependencies
├── eval/
│   ├── harness.py                   # Full evaluation framework (deterministic checks + LLM-as-judge)
│   └── replay_harness.py            # Offline log replayer: re-evaluate recorded events without calling LLM
├── static/
│   └── index.html                   # Frontend web UI
├── docs/
│   ├── MULTI_AGENT_DESIGN.md        # Delegation protocol, SDK mapping, Sandbox boundary
│   └── images/                      # Architecture screenshots (approval.png, reasoning.png, report.png)
├── eval_logs/                       # Auto-generated evaluation logs (created on demand)
└── venv/                            # Python virtual environment
```

---

## File-by-File Breakdown

### `main.py` (~1050 lines) — Core Backend Engine

**What it does:** The single most important file. Implements the entire travel agent as a FastAPI application with WebSocket communication and a Qwen (DashScope) LLM function-calling loop.

**Key components:**
- **FastAPI app**: Mounts static files, all interaction via WebSocket.
- **Qwen client setup**: Wraps DashScope's OpenAI-compatible API (`qwen-plus`, with backup model fallback). Handles quota errors, timeouts, and prompt caching.
- **Session dataclass**: Defines the full state needed to pause/resume agent execution — messages, checklist, approval_state, action_log, booked_flights/hotels, lifecycle_state, etc. Persists to SQLite at every pause point (AWAITING_APPROVAL / AWAITING_REPLY).
- **MOCK_DATA**: Hardcoded mock responses simulating CRM, calendar, flight search, hotel search, booking APIs, and travel policy. Used in both development and evaluation.
- **Tool execution layer** (`_call_with_tools`): Dispatches LLM tool calls to local handler functions. Wraps each call with permission checking (`wrap_with_permission_check`) before dispatch.
- **Agent loop** (`run_agent_loop`): Reentrant async loop — send messages → receive LLM response → execute tool calls → handle results → check termination conditions. Never blocks waiting for user input; persists to DB and returns at pause points. Resume by loading session back and passing `resume_input`.
- **WebSocket handler** (`ws_handler`): Receives three message types from frontend — `new_task` (start new conversation), `approval_response` (user approves/rejects), `user_reply` (free-text answer). Creates a `current_task` to drive `run_agent_loop`.
- **Guardrails**: Checks policy budget boundaries before hotel booking, detects fabricated order IDs in reports, prevents repeated readonly tool calls with same args.
