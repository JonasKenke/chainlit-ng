# SDK-native LLM instrumentation

Chainlit no longer ships the LiteralAI-backed `instrument_openai` or `instrument_mistralai` helpers. Applications can continue to call the SDKs directly; use `cl.Step` or the `@cl.step` decorator when a call should appear as a Chainlit step.

This guide records how to add optional SDK-specific adapters later without bringing back a tracing vendor dependency.

## Implementation direction

- Wrap documented SDK calls at the application boundary. Do not monkey-patch global SDK methods or add an implicit network service.
- Keep adapters opt-in and separate by SDK. The SDK should remain an application dependency, not a required Chainlit runtime dependency.
- Use Chainlit steps for lifecycle and display: create an `llm` step, set input/output, stream output when supported, record completion/error, then persist the step.
- Keep provider-specific metadata in a plain JSON-serializable dictionary. Never record API keys, authorization headers, or other credentials.
- Support synchronous and asynchronous clients, streamed and non-streamed responses, cancellation, and SDK errors.
- Add focused tests for input/output conversion, streaming, errors, and secret redaction before exposing an adapter.

## SDK references

- [OpenAI Python SDK usage](https://github.com/openai/openai-python#usage) — includes synchronous and asynchronous client examples.
- [Mistral Python SDK examples](https://github.com/mistralai/client-python#sdk-example-usage) — includes synchronous and asynchronous chat calls.
- [Chainlit step API](https://docs.chainlit.io/api-reference/step) — the existing Chainlit surface for representing work in the chat.

This is implementation guidance, not a promise that automatic SDK instrumentation is currently available.
