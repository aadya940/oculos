from google.adk.agents import Agent
from google.genai import types

from google.adk.planners.built_in_planner import BuiltInPlanner
from google.adk.tools import AgentTool
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.models import Gemini, LiteLlm
from google.genai.errors import ClientError
from typing import Optional
import json
import logging
import os
import tempfile
import traceback

log = logging.getLogger("orbit.agents")

from .prompts import SYSTEM_PROMPT, PARENT_SYSTEM_PROMPT
from ._tools.ui import (
    list_active_windows,
    manage_window,
    fill_form_fields,
    find_ui_elements,
    find_ui_elements_hwnd,
    get_window_tree,
    get_window_tree_hwnd,
    interact_with_element,
    wait_for_element,
    click_first,
    type_into,
    navigate_to_url,
    launch_and_get_pid,
    take_screenshot,
    scroll_page,
    get_form_fields,
    select_dropdown_option,
    select_option_by_label,
    get_popuphost_menu_window,
)
from ._tools.clipboard import (
    clipboard_get,
    clipboard_set,
)
from ._tools.search import duckduckgo_search
from ._tools.filesystem import (
    get_system_info,
    read_file,
    read_pdf,
    read_csv,
    list_directory,
    search_files,
    file_exists,
    get_file_info,
    find_in_file,
)
from ._tools.hitl import (
    write_file as write_file_approval,
    append_to_file as append_to_file_approval,
    write_csv as write_csv_approval,
    copy_file as copy_file_approval,
    move_file as move_file_approval,
    move_files as move_files_approval,
    create_directory_and_move as create_directory_and_move_approval,
    delete_file,
    create_directory as create_directory_approval,
    upload_file as upload_file_approval,
    request_human,
)
from ._tools.hotkey import press_hotkey

DEFAULT_DESKTOP_MODEL = "gemini-3-flash-preview"
DEFAULT_PLANNER_MODEL = "gemini-3-flash-preview"

DESKTOP_EXECUTOR_AGENT_NAME = "desktop_agent"


def make_lite_llm(model: str):
    """
    Create an ADK LiteLlm from the user-provided model string.

    ADK + LiteLLM typically expects provider-prefixed model strings (`provider/model-name`).
    To keep user experience simple, we normalize the common raw Gemini format
    `gemini-3-pro-preview` into `gemini/gemini-3-pro-preview` (provider prefix).
    For any already provider-prefixed model (contains `/`), we pass it through unchanged.
    """
    m = (model or "").strip()
    # Use ADK native Gemini to preserve thought signatures/tool-calling behavior.
    # Accept common forms:
    # - gemini-3-pro-preview
    # - gemini/gemini-3-pro-preview
    # - google/gemini-3-pro-preview
    # - openrouter/google/gemini-3-pro-preview-customtools
    if "gemini-" in m:
        if m.startswith("gemini-"):
            return Gemini(model=m)
        parts = [p for p in m.split("/") if p]
        for part in reversed(parts):
            if part.startswith("gemini-"):
                return Gemini(model=part)
    return LiteLlm(model)


_DUMP_DIR = os.path.join(tempfile.gettempdir(), "orbit_llm_dumps")
_call_counter = 0


def _dump_llm_request(llm_request: LlmRequest, tag: str = "") -> str:
    """Serialize an LlmRequest to a JSON file for debugging 400 errors."""
    global _call_counter
    _call_counter += 1
    os.makedirs(_DUMP_DIR, exist_ok=True)
    path = os.path.join(_DUMP_DIR, f"call_{_call_counter:04d}_{tag}.json")
    try:
        payload = {
            "call_number": _call_counter,
            "tag": tag,
            "model": getattr(llm_request, "model", None),
            "num_contents": len(llm_request.contents) if llm_request.contents else 0,
            "contents_summary": [],
            "config_keys": [],
            "sys_instruction_len": 0,
            "num_tools": 0,
        }
        if llm_request.contents:
            for i, c in enumerate(llm_request.contents):
                parts_info = []
                for p in c.parts or []:
                    if getattr(p, "text", None) is not None:
                        parts_info.append(
                            {
                                "type": "text",
                                "len": len(p.text),
                                "preview": p.text[:200],
                            }
                        )
                    elif getattr(p, "function_call", None):
                        parts_info.append(
                            {"type": "function_call", "name": p.function_call.name}
                        )
                    elif getattr(p, "function_response", None):
                        parts_info.append(
                            {
                                "type": "function_response",
                                "name": p.function_response.name,
                            }
                        )
                    elif getattr(p, "inline_data", None):
                        parts_info.append(
                            {
                                "type": "inline_data",
                                "mime": getattr(p.inline_data, "mime_type", "?"),
                            }
                        )
                    else:
                        parts_info.append({"type": "other"})
                payload["contents_summary"].append(
                    {"index": i, "role": c.role, "parts": parts_info}
                )
        if llm_request.config:
            cfg = llm_request.config
            if cfg.system_instruction:
                si = cfg.system_instruction
                if isinstance(si, str):
                    payload["sys_instruction_len"] = len(si)
                elif hasattr(si, "parts") and si.parts:
                    payload["sys_instruction_len"] = sum(
                        len(getattr(p, "text", "") or "") for p in si.parts
                    )
            if cfg.tools:
                total_decls = 0
                for t in cfg.tools:
                    if hasattr(t, "function_declarations") and t.function_declarations:
                        total_decls += len(t.function_declarations)
                payload["num_tools"] = total_decls
            payload["config_keys"] = [
                k
                for k, v in (
                    cfg.model_dump(exclude_none=True)
                    if hasattr(cfg, "model_dump")
                    else {}
                ).items()
                if v is not None
            ]
            # Dump the FULL config as JSON for diagnosis
            try:
                payload["full_config"] = (
                    cfg.model_dump(exclude_none=True)
                    if hasattr(cfg, "model_dump")
                    else str(cfg)
                )
            except Exception:
                payload["full_config"] = str(cfg)
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            json.dump(payload, f, indent=2, default=str)
        log.debug("Dumped LLM request #%d (%s) -> %s", _call_counter, tag, path)
        log.debug(
            "  model=%s contents=%d sys_len=%d tools=%d",
            payload["model"],
            payload["num_contents"],
            payload["sys_instruction_len"],
            payload["num_tools"],
        )
    except Exception as e:
        log.warning("Failed to dump LLM request: %s", e)
    return path


_BUDGET_WARNING_THRESHOLD = 5  # Warn when this many calls remain.


async def inject_screenshot_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    """
    Before each desktop agent LLM call:
    1. Track call count and inject a budget warning when running low.
    2. Inject screenshot artifacts as inline images.
    """
    _dump_llm_request(llm_request, tag="desktop_before_model")

    # ── Budget tracking ────────────────────────────────────────────
    call_num = callback_context.state.get("_orbit_call_count", 0) + 1
    callback_context.state["_orbit_call_count"] = call_num
    max_calls = callback_context.state.get("_orbit_max_calls", 30)
    remaining = max(0, max_calls - call_num)

    if remaining <= _BUDGET_WARNING_THRESHOLD and llm_request.contents:
        llm_request.contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=f"[BUDGET] {remaining} LLM calls remaining. "
                        "Finish the current step now and return."
                    )
                ],
            )
        )

    if not llm_request.contents:
        return None
    content = llm_request.contents[-1]
    if not content.parts:
        return None
    for part in content.parts:
        if (
            hasattr(part, "function_response")
            and part.function_response
            and part.function_response.name == "take_screenshot"
        ):
            response = part.function_response.response
            if response.get("status") == "success":
                artifact = await callback_context.load_artifact("screenshot.jpg")
                if artifact and artifact.inline_data:
                    llm_request.contents.append(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part(
                                    inline_data=types.Blob(
                                        mime_type="image/jpeg",
                                        data=artifact.inline_data.data,
                                    )
                                ),
                                types.Part(text="This is the current screenshot."),
                            ],
                        )
                    )
    return None


def capture_phase_instruction_before_agent_callback(
    callback_context: CallbackContext,
) -> None:
    # Planner invokes desktop_agent via AgentTool; phase text arrives as user_content.
    user_content = callback_context.user_content
    phase_text = ""
    if user_content and user_content.parts:
        phase_text = getattr(user_content.parts[0], "text", "") or ""
    callback_context.state["journal_phase_instruction"] = phase_text
    return None


_planner = BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_budget=512))


def system_prompt_provider(context: ReadonlyContext) -> str:
    return SYSTEM_PROMPT


def parent_prompt_provider(context: ReadonlyContext) -> str:
    return PARENT_SYSTEM_PROMPT


def _handle_model_error(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
    error: Exception,
) -> Optional[LlmResponse]:
    """Handle 400 INVALID_ARGUMENT by dumping the full request for diagnosis."""
    is_400 = isinstance(error, ClientError) and getattr(error, "code", None) == 400
    path = _dump_llm_request(llm_request, tag="ERROR_400" if is_400 else "ERROR_other")
    log.error("Model error: %s: %s", type(error).__name__, error)
    log.error("Request dumped to: %s", path)
    if is_400:
        try:
            err_path = os.path.join(
                _DUMP_DIR, f"call_{_call_counter:04d}_FULL_CONFIG.json"
            )
            full = {}
            if llm_request.config and hasattr(llm_request.config, "model_dump"):
                full["config"] = llm_request.config.model_dump(mode="json")
            if llm_request.contents:
                full["contents"] = [
                    c.model_dump(mode="json", exclude_none=True)
                    for c in llm_request.contents
                ]
            full["model"] = getattr(llm_request, "model", None)
            with open(err_path, "w", encoding="utf-8", errors="replace") as f:
                json.dump(full, f, indent=2, default=str)
            log.error("Full config dumped to: %s", err_path)
        except Exception as dump_err:
            log.error("Failed to dump full config: %s", dump_err, exc_info=True)
    # Re-raise — don't swallow the error, just log it
    return None


def build_desktop_agent(desktop_model: str) -> Agent:
    return Agent(
        model=make_lite_llm(desktop_model),
        name=DESKTOP_EXECUTOR_AGENT_NAME,
        description="""Handles all desktop UI automation: browser control, forms, dropdowns,
        file uploads, job applications (LinkedIn Easy Apply, Indeed).
        Delegate any phase that requires interacting with the screen or apps to this agent.
        This agent is responsible for all the desktop UI automation tasks.""",
        instruction=system_prompt_provider,
        before_model_callback=inject_screenshot_callback,
        before_agent_callback=capture_phase_instruction_before_agent_callback,
        on_model_error_callback=_handle_model_error,
        tools=[
            list_active_windows,
            manage_window,
            click_first,
            type_into,
            find_ui_elements,
            fill_form_fields,
            find_ui_elements_hwnd,
            get_window_tree,
            get_window_tree_hwnd,
            interact_with_element,
            wait_for_element,
            scroll_page,
            get_form_fields,
            select_dropdown_option,
            select_option_by_label,
            clipboard_get,
            clipboard_set,
            list_directory,
            duckduckgo_search,
            move_file_approval,
            move_files_approval,
            create_directory_and_move_approval,
            write_file_approval,
            read_file,
            append_to_file_approval,
            read_pdf,
            read_csv,
            write_csv_approval,
            search_files,
            file_exists,
            get_file_info,
            copy_file_approval,
            delete_file,
            create_directory_approval,
            find_in_file,
            get_system_info,
            press_hotkey,
            navigate_to_url,
            launch_and_get_pid,
            get_popuphost_menu_window,
            upload_file_approval,
            request_human,
        ],
    )


def build_parent_agent(
    planner_model: str,
    desktop_agent: Agent,
) -> Agent:
    desktop_tool = AgentTool(desktop_agent)
    return Agent(
        model=make_lite_llm(planner_model),
        name="planner",
        planner=_planner,
        description="""High-level planner that breaks goals into phases and delegates desktop automation to desktop_agent.
        This agent is responsible for breaking down the user's goal into clear phases and delegating the tasks to the desktop_agent tool.""",
        instruction=parent_prompt_provider,
        tools=[duckduckgo_search, desktop_tool],
    )


def build_agents(
    *,
    desktop_model: str = DEFAULT_DESKTOP_MODEL,
    planner_model: str = DEFAULT_PLANNER_MODEL,
) -> tuple[Agent, Agent]:
    """Return (parent_agent, desktop_agent) for the requested model strings."""
    desktop_agent = build_desktop_agent(desktop_model)
    parent_agent = build_parent_agent(planner_model, desktop_agent)
    return parent_agent, desktop_agent


parent_agent, desktop_agent = build_agents()
