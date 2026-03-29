"""Rich console display for planner-level progress."""

from rich.console import Console
from rich.panel import Panel


def _extract_step_goal(request: str) -> str:
    """Pull the STEP_GOAL line from a planner request block, or fall back to the first line."""
    for line in request.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("STEP_GOAL"):
            parts = stripped.split(None, 1)
            return parts[1] if len(parts) > 1 else stripped
    for line in request.splitlines():
        if line.strip():
            return line.strip()[:120]
    return "..."


class OrbitConsole:
    def __init__(self):
        self.console = Console()
        self._step = 0
        self._status = None

    def task_start(self, task: str):
        self.console.print(Panel(task.strip(), title="Task", border_style="blue"))

    def step_start(self, request: str):
        self._step += 1
        goal = _extract_step_goal(request)
        self.console.print(f"\n[bold cyan]Step {self._step}[/bold cyan]  {goal}")
        self._status = self.console.status("[dim]Working...[/dim]", spinner="dots")
        self._status.start()

    def step_tool(self, tool_name: str):
        if self._status:
            self._status.update(f"[dim]{tool_name}[/dim]")

    def step_done(self):
        if self._status:
            self._status.stop()
            self._status = None

    def agent_done(self, summary: str):
        self.step_done()
        if summary:
            self.console.print(Panel(summary.strip(), title="Done", border_style="green"))

    def error(self, msg: str):
        self.console.print(f"[bold red]Error:[/bold red] {msg}")

    def latency(self, summary: dict):
        s = summary
        self.console.print(
            f"\n[dim]{s['total_sec']:.1f}s total  |  "
            f"{s['llm_steps']} LLM calls ({s['llm_time_sec']:.1f}s)  |  "
            f"{s['tool_calls']} tools ({s['tool_time_sec']:.1f}s)[/dim]"
        )
