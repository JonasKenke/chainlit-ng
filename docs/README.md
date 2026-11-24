# Documentation for Chainlit NG developers, contributors, and AI coding assistants.

Repository, issues, and releases: [JonasKenke/chainlit-ng](https://github.com/JonasKenke/chainlit-ng).

See also:

- [CONTRIBUTING.md](../CONTRIBUTING.md) — contribution guidelines
- [CHANGELOG.md](../CHANGELOG.md) — release history

## Guides

- [Migrating to Chainlit NG 3.0.0](migration-3.0.md)
- [Request context in callbacks](request-context.md)
- [Skills: app-directory instructions and progressive disclosure](skills.md)
- [Prompt versioning and pinned sharing](prompt-versioning.md)
- [Per-user OAuth for MCP connections](mcp-oauth.md)
- [Scheduled prompts integration](scheduled-prompts.md)
- [Embedding Chainlit NG Copilot: supported authentication model](copilot-embedding.md)
- [SDK-native LLM instrumentation](integrations/sdk-instrumentation.md)
- [Controlling automatic side-panel opening](side-panel-auto-expand.md)
- [Thread resume with unavailable chat profiles](thread-resume.md)
- [Tool-call approvals](approvals.md)
- [Agent questions (`cl.ask`)](questions.md)
- [Ephemeral state store](state-store.md)
- [OpenTelemetry traces](telemetry.md)
- [Feature and test inventory after 2.12.0](features-after-2.12.0.md)

## Research

Internal notes on CI, tooling, and test layout:

- [CI cache optimization](research/ci-cache-optimization.md)
- [Copilot type-checking](research/copilot-type-checking.md)
- [OpenAI Python 3.x upgrade path](research/openai-v3-compatibility.md)
- [E2E parallel execution](research/e2e-parallel-execution.md)
- [Upstream PR and issue triage](research/upstream-triage.md)
- [Security hardening: Azure AD hybrid OAuth](research/security-hardening.md)
