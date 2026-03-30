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
    errors: list[str] = field(default_factory=list)
    latency: dict = field(default_factory=dict)
    journal: dict = field(default_factory=dict)


def _console_safe(obj: Any) -> str:
    """
    Return an ASCII-only string for console logging.
    This prevents Windows terminals (cp1252) from crashing on unexpected unicode
    such as U+FFFC from accessibility text.
    """
    s = str(obj)
    return s.encode("ascii", errors="backslashreplace").decode("ascii")


def _setup_logging(*, verbose: bool = False) -> None:
    """Configure the 'orbit' logger. Called once per Agent init."""
    logger = logging.getLogger("orbit")
    if logger.handlers:
        return  # already configured
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
        human_in_the_loop: Optional[HumanInTheLoopHandler] = None,
    ):
        self.task = task
        self.llm = llm
        self.desktop_llm = desktop_llm
        self.planner_llm = planner_llm
        self.measure_latency = measure_latency
        self.verbose = verbose
        self.max_steps = max_steps
        self._human_in_the_loop = human_in_the_loop

        # Configure logging based on verbose flag
        _setup_logging(verbose=verbose)

    async def run(self) -> RunResult:
        daemon = OculOSManager()
        await daemon.start()
        try:
            return await self._run()
        except Exception as e:
            log.error("Agent run failed: %s", e, exc_info=True)
            return RunResult(status="error", summary=str(e), errors=[str(e)])
        finally:
            daemon.stop()

    async def _run(self) -> RunResult:
        prompt = self.task
        ui = OrbitConsole(verbose=self.verbose)
        ui.task_start(prompt)

        # State tracked across the event loop for RunResult.
        final_text = ""
        errors: list[str] = []
        saw_request_human = False

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="desktop_app",
            user_id="local_admin",
            session_id="session_001",
            state={"_orbit_max_calls": self.max_steps},
        )

        # Ephemeral, attempt-scoped OS Action Journal for debugging/inspection.
        journal = Journal(core_key="desktop_attempt_0")
        desktop_attempt_idx = 0
        journal_active = False

        # Allow callers to provide separate model strings for planner vs desktop.
        # If not provided, use `llm` for both for backwards compatibility.
        # LiteLLM model strings are typically provider-prefixed (`provider/model-name`).
        # Your existing wrapper `llm` default is often a raw Gemini name, so we only
        # override desktop/planner models from `self.llm` when it looks LiteLLM-compatible
        # (contains a '/'); otherwise we let build_agents fall back to its defaults.
        desktop_model = self.desktop_llm or self.llm
        planner_model = self.planner_llm or self.llm

        build_kwargs: dict[str, str] = {}
        if desktop_model is not None:
            build_kwargs["desktop_model"] = desktop_model
        if planner_model is not None:
            build_kwargs["planner_model"] = planner_model

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
        user_id = "local_admin"
        content = types.Content(role="user", parts=[types.Part(text=prompt)])
        events = runner.run_async(
            session_id=session.id,
            user_id=user_id,
            new_message=content,
        )

        latency = _LatencyTracker() if self.measure_latency else None
        if latency:
            latency.start_run()

        _last = time.time()

        async for event in events:
            # When the desktop executor finishes, finalize the evidence slice.
            if (
                event.is_final_response()
                and event.author == DESKTOP_EXECUTOR_AGENT_NAME
                and journal_active
                and getattr(event, "content", None)
                and getattr(event.content, "parts", None)
                and event.content.parts
                and getattr(event.content.parts[0], "text", None) is not None
            ):
                journal.finalize_end_interactions()
                session.state["journal"] = journal.to_dict()
                journal_active = False

            if event.is_final_response():
                if latency:
                    latency.on_final_response()
                text = None
                if getattr(event, "content", None) and event.content.parts:
                    first = event.content.parts[0]
                    text = getattr(first, "text", None)
                if event.author == DESKTOP_EXECUTOR_AGENT_NAME:
                    ui.step_done()
                else:
                    final_text = _console_safe(text) if text else ""
                    ui.agent_done(final_text)
            elif getattr(event, "content", None) and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "function_call", None):
                        now = time.time()
                        name = part.function_call.name
                        args = (
                            dict(part.function_call.args)
                            if part.function_call.args
                            else {}
                        )

                        # Journal collection: only during desktop executor.
                        if (
                            event.author == DESKTOP_EXECUTOR_AGENT_NAME
                            and not journal_active
                        ):
                            desktop_attempt_idx += 1
                            phase_instruction = session.state.get(
                                "journal_phase_instruction", ""
                            )
                            journal.reset(
                                core_key=f"desktop_attempt_{desktop_attempt_idx}",
                                phase_instruction=str(phase_instruction or ""),
                            )
                            session.state["journal"] = journal.to_dict()
                            journal_active = True

                        # Track request_human calls for RunResult status.
                        if name == "request_human":
                            saw_request_human = True

                        # Rich UI: planner delegating a step vs desktop tool call
                        if name == DESKTOP_EXECUTOR_AGENT_NAME:
                            ui.step_start(args.get("request", str(args)))
                        elif event.author == DESKTOP_EXECUTOR_AGENT_NAME:
                            ui.step_tool(name)

                        if event.author == DESKTOP_EXECUTOR_AGENT_NAME:
                            journal.record_call(
                                call_id=getattr(part.function_call, "id", None),
                                tool_name=name,
                                tool_args=args,
                            )
                        if latency:
                            step_sec = latency.on_function_call(name, args)
                            log.debug(
                                "[%.3fs] %s(%s)", step_sec, name, _console_safe(args)
                            )
                        else:
                            log.debug(
                                "[%.2fs] %s(%s)",
                                round(now - _last, 2),
                                name,
                                _console_safe(args),
                            )
                        _last = now
                    elif getattr(part, "function_response", None):
                        name = getattr(part.function_response, "name", "?")

                        if event.author == DESKTOP_EXECUTOR_AGENT_NAME:
                            journal.record_response(
                                call_id=getattr(part.function_response, "id", None),
                                tool_name=name,
                                response=getattr(
                                    part.function_response, "response", None
                                ),
                            )

                        # Collect tool errors for RunResult.
                        resp = getattr(part.function_response, "response", None)
                        if isinstance(resp, dict) and resp.get("status") == "error":
                            errors.append(
                                f"{name}: {resp.get('message', 'unknown error')}"
                            )

                        if latency:
                            tool_sec = latency.on_function_response(name)
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
                        _last = time.time()

        # Clean up any lingering spinner.
        ui.step_done()

        # Determine status.
        if saw_request_human:
            status = "needs_human"
        elif errors:
            status = "failed"
        else:
            status = "success"

        latency_summary = latency.summary() if latency else {}
        # Re-read session to get state updates from callbacks.
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
        if latency:
            ui.latency(latency_summary)

        return RunResult(
            status=status,
            summary=final_text,
            errors=errors,
            latency=latency_summary,
            journal=journal.to_dict(),
        )
