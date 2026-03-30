"""Orbit examples — showcasing the composable SDK API."""

import asyncio
from dotenv import load_dotenv
from orbit import Agent, RunResult, Do, Read, Check, Navigate, Fill, session

load_dotenv()

# ── Example 1: Simple agent (backward compatible) ────────────────


async def example_simple():
    """One-shot task — daemon starts and stops automatically."""
    result: RunResult = await Agent(
        task="Open Notepad, type 'Hello from Orbit!', save to Desktop as hello.txt, close Notepad.",
        llm="gemini-2.5-pro",
        max_steps=15,
        verbose=True,
    ).run()

    print(f"Status:  {result.status}")
    print(f"Summary: {result.summary}")
    if result.latency:
        lat = result.latency
        print(
            f"Latency: {lat['total_sec']}s total, "
            f"{lat.get('llm_calls', '?')}/{lat.get('max_llm_calls', '?')} LLM calls, "
            f"{lat['tool_calls']} tool calls"
        )


# ── Example 2: Composable verbs with session ─────────────────────


async def example_verbs():
    """Use verbs for programmatic screen control with shared daemon."""
    async with session() as s:
        await Navigate("Notepad", session=s, verbose=True, llm="gemini-3-flash-preview").run()
        await Do(
            "type 'Hello from Orbit verbs!'",
            session=s,
            verbose=True,
            llm="gemini-3-flash-preview",
            max_steps=5,
        ).run()

        if await Check(
            "the text 'Hello from Orbit verbs!' is visible",
            session=s,
            verbose=True,
            llm="gemini-3-flash-preview",
            max_steps=5,
        ).check():
            print("Text verified!")

        await Do(
            "save the file to Desktop as hello_verbs.txt immediately and close Notepad immediately.",
            session=s,
            verbose=True,
            llm="gemini-3.1-flash-lite-preview",
            max_steps=30,
        ).run()


# ── Example 3: Custom domain agent ───────────────────────────────

# from orbit import BaseActionAgent
# from pydantic import BaseModel
#
# class ResumeData(BaseModel):
#     name: str
#     skills: list[str]
#     experience_years: int
#
# class ReadResume(Read):
#     def __init__(self, path: str, **kw):
#         super().__init__(f"read the resume at {path}", schema=ResumeData, **kw)
#
# async def example_domain_agent():
#     async with session() as s:
#         result = await ReadResume("Desktop/RESUME.pdf", session=s).run()
#         print(result.output)  # ResumeData instance


if __name__ == "__main__":
    asyncio.run(example_verbs())
    # asyncio.run(example_verbs())
