# OpenAI Python 3.x upgrade path

## Current status

Keep `openai>=2,<3` in the combined test extra. The current lock resolves `llama-index` 0.14.25 with `llama-index-llms-openai` 0.7.10, whose metadata requires `openai>=1.108.1,<3`. The latest checked plugin release, 0.8.3, still has the same `<3` cap (checked 2026-10-27). Raising the test extra to OpenAI 3 makes the supported test environment unsatisfiable; wait for a LlamaIndex release that accepts OpenAI 3 rather than overriding its constraint.

Chainlit no longer provides OpenAI SDK instrumentation. The Python SDK is not a core runtime dependency, so applications can choose their own OpenAI version independently of this test-extra constraint.

## Upgrade steps

1. Check that the selected `llama-index-llms-openai` release supports OpenAI 3 and still resolves across the project's Python 3.10–3.14 matrix.
2. Raise the test extra to `openai>=3,<4` and update `uv.lock` together with the LlamaIndex packages.
3. Run the LangChain, LlamaIndex, and full backend suites. The project currently has no OpenAI instrumentation helper tests because that helper was removed with LiteralAI.
4. Review the OpenAI SDK v3 HTTPX2 migration guide if Chainlit later adds SDK-owned HTTP client, transport, or hook code. Chainlit currently creates no OpenAI client.

## References

- [OpenAI Python SDK changelog](https://github.com/openai/openai-python/blob/main/CHANGELOG.md)
- [OpenAI Python HTTPX2 migration](https://github.com/openai/openai-python/blob/main/httpx2.md)
- [`llama-index-llms-openai` package metadata](https://pypi.org/project/llama-index-llms-openai/)
- [SDK-native Chainlit instrumentation direction](../integrations/sdk-instrumentation.md)
