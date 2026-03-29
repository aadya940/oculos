"""Minimal Agent interface: Agent(llm=..., task=...)."""

import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.adk.artifacts import InMemoryArtifactService

from .agents import (
    build_agents,
    DESKTOP_EXECUTOR_AGENT_NAME,
)
from .daemon import OculOSManager
from ._ui import default_human_in_the_loop
from .journal import Journal


def _console_safe(obj: Any) -> str:
    """
    Return an ASCII-only string for console logging.
    This prevents Windows terminals (cp1252) from crashing on unexpected unicode
    such as U+FFFC from accessibility text.
    """
    s = str(obj)
    return s.encode("ascii", errors="backslashreplace").decode("ascii")



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

    def print_report(self):
        s = self.summary()
        print("AGENT LATENCY REPORT")
        print("--------------------------------")
        print(f"  Total run:           {s['total_sec']:.3f}s")
        print(
            f"  LLM steps:           {s['llm_steps']} (total {s['llm_time_sec']:.3f}s)"
        )
        print(
            f"  Tool calls:          {s['tool_calls']} (total {s['tool_time_sec']:.3f}s)"
        )
        for name, sec in s.get("per_tool_sec", []):
            print(f"    {name}: {sec:.3f}s")
        print("--------------------------------")


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
        human_in_the_loop: Optional[HumanInTheLoopHandler] = None,
    ):
        self.task = task
        self.llm = llm
        self.desktop_llm = desktop_llm
        self.planner_llm = planner_llm
        self.measure_latency = measure_latency
        self.verbose = verbose
        self._human_in_the_loop = human_in_the_loop

    async def run(self):
        daemon = OculOSManager(verbose=self.verbose)
        await daemon.start()
        try:
            return await self._run()
        finally:
            daemon.stop()

    async def _run(self):
        prompt = self.task
        if self.verbose:
            print(f"\n[User]: {prompt}\n--------------------------------")

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="desktop_app", user_id="local_admin", session_id="session_001"
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

        runner = Runner(
            agent=parent_agent,
            app_name="desktop_app",
            session_service=session_service,
            artifact_service=InMemoryArtifactService(),
        )
        user_id = "local_admin"
        content = types.Content(role="user", parts=[types.Part(text=prompt)])
        events = runner.run_async(
            session_id=session.id, user_id=user_id, new_message=content
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
                if getattr(event, "content", None) and event.content.parts:
                    first = event.content.parts[0]
                    if getattr(first, "text", None) is not None:
                        print(f"\n{_console_safe(first.text)}")
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

                        if event.author == DESKTOP_EXECUTOR_AGENT_NAME:
                            journal.record_call(
                                call_id=getattr(part.function_call, "id", None),
                                tool_name=name,
                                tool_args=args,
                            )
                        if latency:
                            step_sec = latency.on_function_call(name, args)
                            if self.verbose:
                                print(
                                    _console_safe(
                                        f"[{step_sec:.3f}s LLM->tool] [Action]: {name}({args})"
                                    )
                                )
                        else:
                            if self.verbose:
                                print(
                                    _console_safe(
                                        f"[{round(now - _last, 2)}s] [Action]: {name}({args})"
                                    )
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

                        if latency:
                            tool_sec = latency.on_function_response(name)
                            if self.verbose:
                                print(
                                    _console_safe(
                                        f"[tool {tool_sec:.3f}s] [Result]: {part.function_response.response}"
                                    )
                                )
                        else:
                            if self.verbose:
                                print(
                                    _console_safe(
                                        f"[Result]: {part.function_response.response}"
                                    )
                                )
                        _last = time.time()

        if latency and self.verbose:
            latency.print_report()
        return latency.summary() if latency else None
