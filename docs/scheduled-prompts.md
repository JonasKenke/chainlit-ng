# Scheduled prompts integration

**Reason for existence:** integrate scheduled prompts with model selection, optional chat profiles, and after-hooks without adding a second model setting system.

## Enable it

Prompt scheduling requires authentication, a data layer implementing scheduled-task methods, and an `on_message` callback:

```toml
[features]
prompt_scheduling = true
prompt_gallery = true # optional
```

## Expose models in the schedule form

`set_scheduled_task_models` supplies the independent model dropdown shown when creating or editing a schedule. The selected value is stored on the task as `model`.

```python
import chainlit as cl

MODELS = {
    "⚡ GPT 5 Mini": "gpt-5-mini",
    "⚖️ GPT 5": "gpt-5",
    "🔷 Gemini 3.5 Flash": "gemini-3.5-flash",
}


@cl.set_scheduled_task_models
async def scheduled_task_models(user, language):
    return list(MODELS)
```

The callback may return a user-specific list. Return one model to keep the dropdown hidden; the single model is used automatically. If the callback is not registered, schedules use the app's default model and reject explicit model values. Every saved model must be returned by this callback for the current user.

## Apply the selected model

Use the same `model` session setting as the normal Chainlit NG settings panel. The scheduler calls `on_settings_update` with the saved model before it calls `on_message`.

```python
@cl.on_chat_start
async def start_chat():
    default_model = "🔷 Gemini 3.5 Flash"
    cl.user_session.set("selected_model", default_model)
    await cl.ChatSettings(
        [
            cl.input_widget.Select(
                id="model",
                label="Model",
                values=list(MODELS),
                initial_value=default_model,
            )
        ]
    ).send()


@cl.on_settings_update
async def update_settings(settings):
    model = settings.get("model")
    if model in MODELS:
        cl.user_session.set("selected_model", model)


@cl.on_message
async def on_message(message: cl.Message):
    deployment = MODELS[cl.user_session.get("selected_model")]
    # Pass deployment to the model client used by the application.
    await cl.Message(content=f"Using {deployment}").send()
```

The schedule dropdown is independent from the normal settings panel. Changing it does not change the current chat's model.

## Optional chat profiles

`set_chat_profiles` remains separate. The schedule form only shows a chat-profile selector when more than one profile is available. With zero or one profile, the default is used and no selector is rendered.

## After-hooks

Register optional work after a scheduled run. The hook appears as a switch in the schedule form when `default=False`:

```python
@cl.after_scheduled_task("Test notification", default=False)
async def test_notification(result: cl.ScheduledTaskResult):
    print(result.runId, result.status, result.model)
```

Only hooks selected for a task run. Keep unconditional logging in the application logger or callback, not as an optional hook.

## Recurrence and timezone

The UI uses simple recurrence presets plus a custom selector for every N hours, daily, weekly, or monthly schedules. It generates the cron expression internally; direct API clients may continue sending `cron` for compatibility.

The UI uses the browser's IANA timezone automatically. No timezone field is shown. Direct API clients must continue sending `timezone` for backward compatibility.

## Verify

```bash
rg -n "set_scheduled_task_models|scheduledTaskModels|model.*ScheduledTask" \
  backend/chainlit frontend libs/react-client
```
