"""Orbit examples — showcasing the composable SDK API."""

import asyncio
from dotenv import load_dotenv
from orbit import Agent, RunResult, Do, Read, Check, Navigate, Fill, session

load_dotenv()

#    Example 1: Simple agent


async def example_simple():
    """One-shot task — daemon starts and stops automatically."""
    result: RunResult = await Agent(
        task="Open Notepad, type 'Hello from Orbit!', save to Desktop as hello.txt, close Notepad.",
        llm="gemini-3.1-flash-lite-preview",
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


#    Example 2: Composable verbs with session


async def example_verbs():
    async with session() as s:
        # 1) End-to-end job discovery + open Easy Apply
        await Do(
            """
            Go to linkedin.com/jobs.

            Search for 'Software Engineering Intern'.
            Enable the 'Easy Apply' filter.

            If no Easy Apply jobs are visible, scroll until they appear.
            If still not found, request_human.

            Click the first job with an 'Easy Apply' badge.
            Click the 'Easy Apply' button.

            STOP when the Easy Apply wizard is open.
            """,
            session=s,
            verbose=True,
            llm="gemini-3-flash-preview",
        ).run()

        # 2) Full application loop (single control block)
        await Do(
            """
            Complete the Easy Apply flow end-to-end:

            - Upload RESUME.pdf (locate it via desktop tools, do not hardcode path)
            - Fill all visible fields using the resume
            - If a required field is unknown, request_human
            - Progress using 'Next' as needed

            Continue iterating through steps until:
            - The 'Submit application' button is visible

            Then:
            - Click 'Submit application'
            - STOP when a confirmation message appears

            If the flow gets stuck at any step, try reasonable recovery (scroll, click Next, etc.)
            before requesting human help.
            """,
            session=s,
            verbose=True,
            llm="gemini-3-flash-preview",
        ).run()

        # 3) Lightweight verification (optional, keep 1 gate max)
        if await Check(
            "A confirmation message like 'Application submitted' is visible",
            session=s,
            verbose=True,
            llm="gemini-3-flash-preview",
        ).check():
            print("Applied Successfully!")
        else:
            print("Submission uncertain — manual check recommended.")


#    Example 3: Custom domain agent

from orbit import BaseActionAgent
from pydantic import BaseModel


class ResumeData(BaseModel):
    name: str
    skills: list[str]
    experience_years: int


class ReadResume(Read):
    def __init__(self, path: str, **kw):
        super().__init__(f"read the resume at {path}", schema=ResumeData, **kw)


async def example_domain_agent():
    async with session() as s:
        result = await ReadResume("Desktop/RESUME.pdf", session=s).run()
        print(result.output)  # ResumeData instance


# if __name__ == "__main__":
#     asyncio.run(example_verbs())
#     asyncio.run(example_simple())
#     asyncio.run(example_domain_agent())
