from orbit import Agent
from dotenv import load_dotenv
import asyncio

load_dotenv()

async def main():
    a1 = Agent(
        llm = "gemini-3.1-pro-preview-customtools",
        task=(
        """
        Open LinkedIn Internships in the existing Chrome window and apply to the first job
        for a suitable role using LinkedIn's “Easy Apply”  feature. Use my resume at 
        `Desktop\\RESUME.pdf`. You can read this resume to search for the role.

        Requirements:
        - Do not browse unnecessary jobs—pick the first relevant Easy Apply listing.
        - When you reach any “Resume” step, Upload the resume to the job.
        - Make sure what you're applying to is an Internship.
        """
        ),
        verbose=True,
        measure_latency=True,
    )
    await a1.run()
 
if __name__ == "__main__":
    asyncio.run(main())
    