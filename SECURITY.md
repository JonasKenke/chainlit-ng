# Security Policy

> **No guarantee:** This fork is maintained on a best-effort basis. Reports,
> fixes, patches, backports, and disclosure timelines are not guaranteed. Use it
> at your own risk and assess the code and dependencies before production use.

## Reporting a Vulnerability

Please use GitHub's [private security advisory mechanism](https://github.com/JonasKenke/chainlit-ng/security/advisories/new)
to report vulnerabilities. On the repository page, go to **Security > Advisories** and click
**"Report a vulnerability"**. This opens a private channel visible only to maintainers,
keeping details out of public view until a fix is ready.

Do not file a public issue for security matters.

When writing your report it helps to include: a description of the vulnerability, the version
(or commit) you tested against, steps to reproduce or a proof-of-concept, and your assessment
of the likely impact. The more context you can provide, the faster we can triage and respond.

## What to Expect

- **Best effort**: this fork is maintained independently and as time permits.
  There is no guaranteed acknowledgement time, response time, fix, patch,
  backport, or disclosure timeline. If you have not received a response, a
  follow-up is welcome, but a response is not guaranteed.
- **Coordinated disclosure**: please do not publish details of the vulnerability until a
  patch has been released or 90 days have passed since the report, whichever comes first.
  If you need to publish sooner for any reason, let us know and we will do our best to
  work with your timeline.

## Scope

Chainlit is a Python/TypeScript framework for building conversational AI applications.
Security-relevant areas include the FastAPI/SocketIO backend, authentication flows (JWT,
OAuth), the data persistence layer, and file upload handling.

When reporting, please note which features you had enabled and how the app was configured,
as that context helps us triage accurately.

Issues in third-party dependencies are generally best reported upstream, though we are happy
to discuss whether a Chainlit-level workaround makes sense in the meantime.

## No Bug Bounty

This is an open-source project with no commercial bug bounty programme.
We cannot offer financial rewards, but we will credit researchers in release notes and
security advisories unless you prefer to remain anonymous.

## Good Faith

We appreciate researchers who take the time to report issues responsibly.
If you act in good faith — give us a reasonable window to respond, avoid accessing user
data beyond what is needed to demonstrate the issue, and avoid disrupting live services —
we will treat your report with the same good faith in return.

Thank you for helping keep Chainlit and its users safe.
