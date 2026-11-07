from typing import Any, Dict, List

from pydantic import Field
from pydantic.dataclasses import dataclass

from chainlit.context import context
from chainlit.input_widget import InputWidget, RadioGroup, Select, Tab


@dataclass
class ChatSettings:
    """Useful to create chat settings that the user can change."""

    inputs: List[InputWidget] | List[Tab] = Field(default_factory=list, exclude=True)

    def __post_init__(self) -> None:
        # pydantic dataclasses replace a custom __init__, so validate here.
        self._validate_inputs()

    def _validate_inputs(self) -> None:
        flat_inputs: List[InputWidget] = []
        for input_widget in self.inputs:
            if isinstance(input_widget, Tab):
                flat_inputs.extend(input_widget.inputs)
            else:
                flat_inputs.append(input_widget)

        inputs_by_id = {input_widget.id: input_widget for input_widget in flat_inputs}

        for input_widget in flat_inputs:
            depends_on = getattr(input_widget, "depends_on", None)
            dependent_items = getattr(input_widget, "dependent_items", {})

            if not depends_on:
                if dependent_items:
                    raise ValueError(
                        f"'{input_widget.id}' has 'dependent_items' without 'depends_on'"
                    )
                continue

            if not dependent_items:
                raise ValueError(
                    f"'{input_widget.id}' has 'depends_on' set to '{depends_on}' but no 'dependent_items'"
                )

            parent = inputs_by_id.get(depends_on)
            if parent is None:
                raise ValueError(
                    f"'{input_widget.id}' depends on unknown input '{depends_on}' ('depends_on')"
                )

            if parent is input_widget:
                raise ValueError(
                    f"'{input_widget.id}' cannot depend on itself ('depends_on')"
                )

            if not isinstance(parent, (Select, RadioGroup)):
                raise ValueError(
                    f"'{input_widget.id}' depends on '{depends_on}', which must be a select or radio input ('depends_on')"
                )

        self._validate_no_cycles(flat_inputs, inputs_by_id)

        for input_widget in flat_inputs:
            depends_on = getattr(input_widget, "depends_on", None)
            if depends_on:
                self._validate_initial_value(input_widget, inputs_by_id[depends_on])

    @staticmethod
    def _validate_no_cycles(
        inputs: List[InputWidget], inputs_by_id: Dict[str, InputWidget]
    ) -> None:
        for input_widget in inputs:
            path = [input_widget.id]
            current = getattr(input_widget, "depends_on", None)
            while current:
                path.append(current)
                if path.count(current) > 1:
                    cycle = path[path.index(current) :]
                    raise ValueError(
                        "Circular 'depends_on' dependency detected: "
                        + " -> ".join(f"'{id}'" for id in cycle)
                    )
                parent = inputs_by_id.get(current)
                current = getattr(parent, "depends_on", None) if parent else None

    @staticmethod
    def _validate_initial_value(input_widget: InputWidget, parent: InputWidget) -> None:
        parent_initial = parent.initial
        child_initial = input_widget.initial
        if parent_initial is None or child_initial is None:
            return

        dependent_items = getattr(input_widget, "dependent_items", {})
        child_items = getattr(input_widget, "items", {})
        allowed_items = dependent_items.get(parent_initial, child_items)
        allowed_values = set(allowed_items.values())
        child_values = (
            child_initial if isinstance(child_initial, list) else [child_initial]
        )

        invalid_values = [
            value for value in child_values if value not in allowed_values
        ]
        if invalid_values:
            raise ValueError(
                f"Initial value '{invalid_values[0]}' of '{input_widget.id}' is not valid for '{parent.id}' when it is '{parent_initial}' ('depends_on')"
            )

    def settings(self):
        def collect_settings(
            values: dict[str, Any], inputs: List[InputWidget] | List[Tab]
        ) -> None:
            for input in inputs:
                if isinstance(input, Tab):
                    collect_settings(values, input.inputs)
                else:
                    values[input.id] = input.initial

        settings: dict[str, Any] = {}
        collect_settings(settings, self.inputs)
        return settings

    def _inputs_as_dicts(self) -> List[dict[str, Any]]:
        return [input_widget.to_dict() for input_widget in self.inputs]

    async def refresh(self):
        """Push settings widgets to the UI without updating session chat_settings."""
        settings = self.settings()
        await context.emitter.emit("chat_settings", self._inputs_as_dicts())
        return settings

    async def send(self):
        settings = self.settings()
        context.emitter.set_chat_settings(settings)
        await context.emitter.emit("chat_settings", self._inputs_as_dicts())
        return settings
