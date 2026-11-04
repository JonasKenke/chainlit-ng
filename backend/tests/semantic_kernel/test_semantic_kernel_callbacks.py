import pytest
from pydantic import BaseModel, Field
from semantic_kernel import Kernel
from semantic_kernel.functions import KernelArguments, kernel_function

import chainlit.semantic_kernel as chainlit_semantic_kernel
from chainlit.semantic_kernel import SemanticKernelFilter


@pytest.fixture
def recorded_steps(monkeypatch):
    steps = []

    class RecordingStep:
        def __init__(self, *, type, name):
            self.type = type
            self.name = name
            self.input = None
            self.output = None
            self.sent = False
            self.updated = False

        async def __aenter__(self):
            steps.append(self)
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self):
            self.sent = True

        async def update(self):
            self.updated = True

    monkeypatch.setattr(chainlit_semantic_kernel, "Step", RecordingStep)
    return steps


async def test_filter_records_function_call_in_chainlit_step(recorded_steps):
    @kernel_function
    def greet(name: str) -> str:
        return f"Hello {name}"

    kernel = Kernel()
    SemanticKernelFilter(kernel=kernel)
    kernel.add_function(plugin_name="sample", function=greet)

    result = await kernel.invoke(
        function_name="greet",
        plugin_name="sample",
        arguments=KernelArguments(name="Ada"),
    )

    assert result.value == "Hello Ada"
    assert len(recorded_steps) == 1
    step = recorded_steps[0]
    assert (step.type, step.name) == ("tool", "sample-greet")
    assert step.input == {"name": "Ada"}
    assert step.output == "Hello Ada"
    assert step.sent
    assert step.updated


@pytest.mark.parametrize(
    "exclusions",
    [{"excluded_plugins": ["sample"]}, {"excluded_functions": ["greet"]}],
)
async def test_filter_exclusions_skip_step_but_keep_function_call(
    recorded_steps, exclusions
):
    @kernel_function
    def greet(name: str) -> str:
        return f"Hello {name}"

    kernel = Kernel()
    SemanticKernelFilter(kernel=kernel, **exclusions)
    kernel.add_function(plugin_name="sample", function=greet)

    result = await kernel.invoke(
        function_name="greet",
        plugin_name="sample",
        arguments=KernelArguments(name="Ada"),
    )

    assert result.value == "Hello Ada"
    assert recorded_steps == []


def test_parse_arguments_serializes_pydantic_models():
    class UserInput(BaseModel):
        display_name: str = Field(alias="displayName")
        optional_note: str | None = None

    filter = SemanticKernelFilter()

    assert filter.parse_arguments(KernelArguments()) == ""
    assert filter.parse_arguments(
        KernelArguments(
            user=UserInput(displayName="Ada"),
            count=2,
        )
    ) == {"user": {"displayName": "Ada"}, "count": 2}
