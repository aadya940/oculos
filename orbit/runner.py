"""Minimal Agent interface: Agent(llm=..., task=...)."""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.adk.artifacts import InMemoryArtifactService

from .agents import (
    build_agents,
    DESKTOP_EXECUTOR_AGENT_NAME,
)
from .daemon import OculOSManager
from ._ui.console import OrbitConsole
from .journal import Journal

log = logging.getLogger("orbit")


@dataclass
class RunResult:
    """Structured return type for Agent.run()."""

    status: Literal["success", "failed", "needs_human", "error"]
    summary: str = ""
    output: Any = None
    errors: list[str] = field(default_factory=list)
    latency: dict = field(default_factory=dict)
    journal: dict = field(default_factory=dict)


def _console_safe(obj: Any) -> str:
    """Return an ASCII-only string for console logging."""
    s = str(obj)
    return s.encode("ascii", errors="backslashreplace").decode("ascii")


def _setup_logging(*, verbose: bool = False) -> None:
    """Configure the 'orbit' logger. Called once per Agent init."""
    logger = logging.getLogger("orbit")
    if logger.handlers:
        return
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.propagate = False


class _LatencyTracker:
    def __init__(self):
        self.run_start = None
        self.step_start = None
        self.tool_call_times = []
        self.tool_latencies = []
        self.llm_step_latencies = []
        self.final_response_at = None

    def start_run(self):
        self.run_start = time.perf_counter()
        self.step_start = self.run_start

    def on_function_call(self, name: str, args: dict) -> float:
        now = time.perf_counter()
        step_sec = now - self.step_start
        self.llm_step_latencies.append(step_sec)
        self.tool_call_times.append((name, now))
        self.step_start = now
        return step_sec

    def on_function_response(self, name: str) -> float:
        now = time.perf_counter()
        latency = 0.0
        if self.tool_call_times:
            call_name, start = self.tool_call_times.pop(0)
            latency = now - start
            self.tool_latencies.append((call_name, latency))
        self.step_start = now
        return latency

    def on_final_response(self):
        self.final_response_at = time.perf_counter()

    def summary(self):
        total = (self.final_response_at or time.perf_counter()) - (self.run_start or 0)
        tool_total = sum(t for _, t in self.tool_latencies)
        llm_total = sum(self.llm_step_latencies) if self.llm_step_latencies else 0.0
        return {
            "total_sec": round(total, 3),
            "tool_calls": len(self.tool_latencies),
            "tool_time_sec": round(tool_total, 3),
            "llm_steps": len(self.llm_step_latencies),
            "llm_time_sec": round(llm_total, 3),
            "per_tool_sec": [(n, round(t, 3)) for n, t in self.tool_latencies],
        }


HumanInTheLoopHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class Agent:
    """Orbit agent. Pass llm (model name) and task, then await `agent.run`."""

    def __init__(
        self,
        task: str,
        llm: str = "gemini-3-pro-preview",
        desktop_llm: Optional[str] = None,
        planner_llm: Optional[str] = None,
        measure_latency: bool = True,
        verbose: bool = False,
        max_steps: int = 30,
        session: Optional[Any] = None,
        output_schema: Optional[Any] = None,
        extra_tools: Optional[list] = None,
        human_in_the_loop: Optional[HumanInTheLoopHandler] = None,
    ):
        self.task = task
        self.llm = llm
        self.desktop_llm = desktop_llm
        self.planner_llm = planner_llm
        self.measure_latency = measure_latency
        self.verbose = verbose
        self.max_steps = max_steps
        self._session = session
        self._output_schema = output_schema
        self._extra_tools = extra_tools or []
        self._human_in_the_loop = human_in_the_loop
        self._owns_session = False

        _setup_logging(verbose=verbose)

    # ── Lifecycle ─────────────────────────────────────────────────

    async def run(self) -> RunResult:
        if self._session and self._session.started:
            try:
                return await self._run()
            except Exception as e:
                log.error("Agent run failed: %s", e, exc_info=True)
                return RunResult(status="error", summary=str(e), errors=[str(e)])
        else:
            # Ephemeral session — backward compatible path.
            from .session import Session

            async with Session() as s:
                self._session = s
                try:
                    return await self._run()
                except Exception as e:
                    log.error("Agent run failed: %s", e, exc_info=True)
                    return RunResult(status="error", summary=str(e), errors=[str(e)])

    async def __aenter__(self):
        if not self._session:
            from .session import Session

            self._session = Session()
            await self._session.__aenter__()
            self._owns_session = True
        return self

    async def __aexit__(self, *exc):
        if self._owns_session and self._session:
            await self._session.__aexit__(*exc)
            self._session = None
            self._owns_session = False

    # ── Core orchestration ────────────────────────────────────────

    async def _run(self) -> RunResult:
        prompt = self.task
        self._ui = OrbitConsole(verbose=self.verbose)
        self._ui.task_start(prompt)

        # Run state.
        self._final_text = ""
        self._errors: list[str] = []
        self._saw_request_human = False

        # Reuse ADK session from orbit Session if available (shared state across verbs).
        if self._session and hasattr(self._session, "session_service"):
            session_service = self._session.session_service
            if self._session.adk_session is not None:
                session = self._session.adk_session
                # Reset budget for this verb's run.
                session.state["_orbit_max_calls"] = self.max_steps
                session.state["_orbit_call_count"] = 0
            else:
                session = await session_service.create_session(
                    app_name="desktop_app",
                    user_id="local_admin",
                    session_id="session_001",
                    state={"_orbit_max_calls": self.max_steps},
                )
                self._session.adk_session = session
        else:
            session_service = InMemorySessionService()
            session = await session_service.create_session(
                app_name="desktop_app",
                user_id="local_admin",
                session_id="session_001",
                state={"_orbit_max_calls": self.max_steps},
            )

        self._journal = Journal(core_key="desktop_attempt_0")
        self._desktop_attempt_idx = 0
        self._journal_active = False
        self._session_obj = session

        desktop_model = self.desktop_llm or self.llm
        planner_model = self.planner_llm or self.llm
        build_kwargs: dict[str, Any] = {}
        if desktop_model is not None:
            build_kwargs["desktop_model"] = desktop_model
        if planner_model is not None:
            build_kwargs["planner_model"] = planner_model
        if self._extra_tools:
            build_kwargs["extra_tools"] = self._extra_tools

        parent_agent, _desktop_agent = build_agents(**build_kwargs)

        app = App(
            name="desktop_app",
            root_agent=parent_agent,
            events_compaction_config=EventsCompactionConfig(
                compaction_interval=3,
                overlap_size=1,
            ),
        )
        runner = Runner(
            app=app,
            session_service=session_service,
            artifact_service=InMemoryArtifactService(),
        )
        content = types.Content(role="user", parts=[types.Part(text=prompt)])
        events = runner.run_async(
            session_id=session.id,
            user_id="local_admin",
            new_message=content,
        )

        self._latency = _LatencyTracker() if self.measure_latency else None
        if self._latency:
            self._latency.start_run()
        self._last_time = time.time()

        async for event in events:
            self._dispatch_event(event)

        self._ui.step_done()

        # Determine status.
        if self._saw_request_human:
            status = "needs_human"
        elif self._errors:
            status = "failed"
        else:
            status = "success"

        latency_summary = self._latency.summary() if self._latency else {}
        updated_session = await session_service.get_session(
            app_name="desktop_app",
            user_id="local_admin",
            session_id=session.id,
        )
        llm_calls_used = (
            updated_session.state if updated_session else session.state
        ).get("_orbit_call_count", 0)
        latency_summary["llm_calls"] = llm_calls_used
        latency_summary["max_llm_calls"] = self.max_steps
        if self._latency:
            self._ui.latency(latency_summary)

        return RunResult(
            status=status,
            summary=self._final_text,
            output=self._final_text,
            errors=self._errors,
            latency=latency_summary,
            journal=self._journal.to_dict(),
        )

    # ── Event dispatch ────────────────────────────────────────────

    def _dispatch_event(self, event) -> None:
        if event.is_final_response():
            self._on_final_response(event)
        elif getattr(event, "content", None) and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "function_call", None):
                    self._on_function_call(event, part)
                elif getattr(part, "function_response", None):
                    self._on_function_response(event, part)

    def _on_final_response(self, event) -> None:
        is_desktop = event.author == DESKTOP_EXECUTOR_AGENT_NAME

        # Finalize journal when desktop executor finishes.
        if (
            is_desktop
            and self._journal_active
            and getattr(event, "content", None)
            and getattr(event.content, "parts", None)
            and event.content.parts
            and getattr(event.content.parts[0], "text", None) is not None
        ):
            self._journal.finalize_end_interactions()
            self._session_obj.state["journal"] = self._journal.to_dict()
            self._journal_active = False

        if self._latency:
            self._latency.on_final_response()

        text = None
        if getattr(event, "content", None) and event.content.parts:
            text = getattr(event.content.parts[0], "text", None)

        if is_desktop:
            self._ui.step_done()
        else:
            self._final_text = _console_safe(text) if text else ""
            self._ui.agent_done(self._final_text)

    def _on_function_call(self, event, part) -> None:
        name = part.function_call.name
        args = dict(part.function_call.args) if part.function_call.args else {}
        is_desktop = event.author == DESKTOP_EXECUTOR_AGENT_NAME

        # Start journal for new desktop phase.
        if is_desktop and not self._journal_active:
            self._desktop_attempt_idx += 1
            phase_instruction = self._session_obj.state.get(
                "journal_phase_instruction", ""
            )
            self._journal.reset(
                core_key=f"desktop_attempt_{self._desktop_attempt_idx}",
                phase_instruction=str(phase_instruction or ""),
            )
            self._session_obj.state["journal"] = self._journal.to_dict()
            self._journal_active = True

        if name == "request_human":
            self._saw_request_human = True

        # UI updates.
        if name == DESKTOP_EXECUTOR_AGENT_NAME:
            self._ui.step_start(args.get("request", str(args)))
        elif is_desktop:
            self._ui.step_tool(name)

        # Journal.
        if is_desktop:
            self._journal.record_call(
                call_id=getattr(part.function_call, "id", None),
                tool_name=name,
                tool_args=args,
            )

        # Latency / logging.
        if self._latency:
            step_sec = self._latency.on_function_call(name, args)
            log.debug("[%.3fs] %s(%s)", step_sec, name, _console_safe(args))
        else:
            now = time.time()
            log.debug(
                "[%.2fs] %s(%s)",
                round(now - self._last_time, 2),
                name,
                _console_safe(args),
            )
            self._last_time = now

    def _on_function_response(self, event, part) -> None:
        name = getattr(part.function_response, "name", "?")
        is_desktop = event.author == DESKTOP_EXECUTOR_AGENT_NAME

        # Journal.
        if is_desktop:
            self._journal.record_response(
                call_id=getattr(part.function_response, "id", None),
                tool_name=name,
                response=getattr(part.function_response, "response", None),
            )

        # Collect errors.
        resp = getattr(part.function_response, "response", None)
        if isinstance(resp, dict) and resp.get("status") == "error":
            self._errors.append(
                f"{name}: {resp.get('message', 'unknown error')}"
            )

        # Latency / logging.
        if self._latency:
            tool_sec = self._latency.on_function_response(name)
            log.debug(
                "[tool %.3fs] %s -> %s",
                tool_sec,
                name,
                _console_safe(part.function_response.response),
            )
        else:
            log.debug(
                "%s -> %s",
                name,
                _console_safe(part.function_response.response),
            )
            self._last_time = time.time()
