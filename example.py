"""Orbit examples — run any task with `python example.py`."""

import asyncio
from dotenv import load_dotenv
from orbit import Agent, RunResult

load_dotenv()

# ── Pick a task ────────────────────────────────────────────────────
# Uncomment one, or write your own.

TASK = """
Open Notepad, type "Hello from Orbit!", save the file to the Desktop
as hello.txt, then close Notepad.
"""

# TASK = """
# Open Chrome, go to https://news.ycombinator.com, and tell me the
# title of the #1 post on the front page.
# """

# TASK = """
# Open LinkedIn Internships in the existing Chrome window and apply
# to the first relevant Easy Apply listing. Use my resume at
# `Desktop\\RESUME.pdf` — read it to determine a suitable role.
# Requirements:
# - Pick the first relevant Easy Apply listing, don't browse.
# - Upload the resume when you reach a "Resume" step.
# - Make sure it's an Internship.
# """


async def main():
    result: RunResult = await Agent(
        task=TASK,
        llm="gemini-2.5-pro",
        max_steps=15,
        verbose=True,
    ).run()

    # Inspect the result.
    print(f"Status:  {result.status}")
    print(f"Summary: {result.summary}")
    if result.errors:
        print(f"Errors:  {result.errors}")
    if result.latency:
        lat = result.latency
        print(f"Latency: {lat['total_sec']}s total, "
              f"{lat.get('llm_calls', '?')}/{lat.get('max_llm_calls', '?')} LLM calls, "
              f"{lat['tool_calls']} tool calls")


if __name__ == "__main__":
    asyncio.run(main())
