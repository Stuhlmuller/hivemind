# Security Policy

Hivemind is security-focused infrastructure for running agents without handing
raw credentials to agent execution contexts. Please report vulnerabilities
privately before discussing exploit details in public issues, discussions, pull
requests, or chat logs.

## Supported Versions

Hivemind is currently pre-1.0. Security fixes target the current `main` branch
and the latest published container image or version tag. Older pre-release
commits and local forks are not supported unless a maintainer explicitly
coordinates a backport for a specific report.

## Reporting A Vulnerability

Email `rodman@stuhlmuller.net` with `Hivemind security` in the subject. If
GitHub private vulnerability reporting is enabled for this repository in the
future, you may use the repository's **Security** tab instead.

In the first message, include:

- A short description of the issue and affected component.
- The Hivemind commit, tag, or container image digest you tested.
- A minimal reproduction or proof of concept using fake data.
- The security impact you expect, such as credential exposure, lease bypass,
  session compromise, audit log tampering, or release artifact compromise.

Do not include real tokens, passwords, OAuth refresh tokens, production
databases, tenant data, private logs, or other sensitive operator data. If a
report needs sensitive supporting material, send only a high-level summary
first so the maintainer can arrange a safer transfer path.

Please allow time for private triage before public disclosure. The maintainer
will acknowledge actionable reports as availability allows, coordinate fixes in
the open when doing so does not disclose exploit details, and credit reporters
on request.

## Good-Faith Research

Good-faith security research is welcome when it uses minimal access, avoids
privacy impact, and stops once a vulnerability is confirmed. Do not persist
access, modify other operators' data, exfiltrate data, degrade service, or test
against systems you do not own or have permission to assess.

## In Scope

Security reports are especially useful in these areas:

- Credential separation between agents and the credentials service.
- JIT credential lease issuance, scoping, expiry, and enforcement.
- Broker-managed secret storage, encryption, redaction, backup, and recovery.
- OAuth state, token storage, callback validation, and provider configuration.
- Local admin setup, login, sessions, cookies, CSRF, and browser security
  boundaries.
- Authorization for task, schedule, heartbeat, audit, and control-plane APIs.
- Audit log integrity, attribution, and secret redaction.
- Container packaging, release workflows, published artifacts, and dependency
  supply chain controls.

## Public Issues

Use public GitHub issues for hardening ideas, documentation gaps, ordinary bugs,
and security-adjacent improvements that do not reveal a live exploit path or
sensitive data. If you are unsure whether a report is sensitive, use the private
reporting path first.
