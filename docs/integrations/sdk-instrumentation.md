# SDK-native LLM instrumentation

Chainlit NG no longer ships the LiteralAI-backed `instrument_openai` or `instrument_mistralai` helpers. Applications can continue to call the SDKs directly; use `cl.Step` or the `@cl.step` decorator when a call should appear as a Chainlit NG step.

This guide records how to add optional SDK-specific adapters later without bringing back a tracing vendor dependency.

## Removed public API

| Removed                                                                    | Replacement                                                                                     |
| -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `cl.instrument_openai()`, `cl.instrument_mistralai()`                      | No drop-in replacement. Wrap SDK calls in `cl.Step(type="llm")`, or rely on SDK-native tracing. |
| `cl.ChatGeneration`, `cl.CompletionGeneration`, `cl.GenerationMessage`     | Plain dictionaries assigned to `Step.generation`.                                               |
| `chainlit.data.literalai.LiteralDataLayer` and `LITERAL_API_KEY` auto-init | `@cl.data_layer`, or the built-in SQLAlchemy/DynamoDB data layers.                              |

`Step.generation` accepts a JSON-serializable dict. Chat generations use `provider`, `model`, `messages`, `messageCompletion`, `settings`, `tools`, `variables`, and `type: "CHAT"`; completion generations use `provider`, `model`, `prompt`, `completion`, `settings`, `variables`, and `type: "COMPLETION"`. Optional keys include `duration`, `ttFirstToken`, `tokenThroughputInSeconds`, `promptId`, and `id`.

## Implementation direction

- Wrap documented SDK calls at the application boundary. Do not monkey-patch global SDK methods or add an implicit network service.
- Keep adapters opt-in and separate by SDK. The SDK should remain an application dependency, not a required Chainlit NG runtime dependency.
- Use Chainlit NG steps for lifecycle and display: create an `llm` step, set input/output, stream output when supported, record completion/error, then persist the step.
- Keep provider-specific metadata in a plain JSON-serializable dictionary. Never record API keys, authorization headers, or other credentials.
- Support synchronous and asynchronous clients, streamed and non-streamed responses, cancellation, and SDK errors.
- Add focused tests for input/output conversion, streaming, errors, and secret redaction before exposing an adapter.

## SDK references

- [OpenAI Python SDK usage](https://github.com/openai/openai-python#usage) — includes synchronous and asynchronous client examples.
- [Mistral Python SDK examples](https://github.com/mistralai/client-python#sdk-example-usage) — includes synchronous and asynchronous chat calls.
- [Chainlit step API](https://docs.chainlit.io/api-reference/step) — the existing Chainlit surface for representing work in the chat.

This is implementation guidance, not a promise that automatic SDK instrumentation is currently available.
