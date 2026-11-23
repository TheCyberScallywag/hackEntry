"""Stand-in models for the assistants: a script of tool calls and a final answer.

No test talks to a real model (`ALLOW_MODEL_REQUESTS` is off, tests/conftest.py). A script says
exactly what the "model" does, so a test can prove what the application does about it: which
tools run, what is refused, what waits for approval, what a validator sends back.
"""

from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


def call(tool: str, **args: Any) -> tuple[str, str, dict]:
    return ("tool", tool, args)


def final(**payload: Any) -> tuple[str, str, dict]:
    return ("final", "", payload)


class Scripted(FunctionModel):
    """Plays its steps in order, one per model request, across a run and its resumption."""

    def __init__(self, *steps: tuple[str, str, dict]) -> None:
        super().__init__(self._respond, model_name="scripted")
        self.steps = list(steps)
        self.requests: list[list[ModelMessage]] = []
        self.tools_offered: list[str] = []
        self.instructions = ""

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(messages)
        self.tools_offered = [t.name for t in info.function_tools]
        self.instructions = info.instructions or ""
        kind, tool, args = self.steps.pop(0)
        name = info.output_tools[0].name if kind == "final" else tool
        return ModelResponse(parts=[ToolCallPart(name, args)])

    def tool_results(self) -> list[str]:
        """Everything tools (and validators) said back to the model, in order."""
        said = []
        for part in (p for m in self.requests[-1] for p in m.parts):
            if part.part_kind == "tool-return":
                said.append(str(part.content))
            elif isinstance(part, RetryPromptPart):
                said.append(part.model_response())
        return said
