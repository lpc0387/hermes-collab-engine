# Good-first-issues launch backlog

These recommendations are templates for maintainers to create GitHub issues after the v5.0 launch. They are not published issues. Each task is intentionally scoped for a first-time contributor and should be paired with a focused pull request plus the narrow verification command named in the issue body.

## Summary list

1. Document common sandbox startup failures
2. Add a concise Agent Backend example
3. Add dashboard screenshot refresh guidance
4. Add CLI status JSON regression coverage
5. Add sandbox TTL edge-case tests
6. Polish dashboard empty-state copy
7. Add issue-template label guidance
8. Add README localization alignment checklist

## 1. Document common sandbox startup failures

**Labels:** `good first issue`, `documentation`, `sandbox`

**Body:**

New users may hit predictable sandbox setup problems: a busy port, missing editable install, a stale demo database, or confusion about mock mode versus `--real`. Add a short troubleshooting section to `sandbox/README.md` that explains these cases and points back to the existing safe defaults.

Suggested scope:

- Keep the section concise and beginner-friendly.
- Mention `./scripts/start_sandbox.sh --port 8877` for port conflicts.
- Mention that mock mode is the default and does not call real workers.
- Mention that generated databases and logs should not be committed.
- Do not add new runtime behavior.

**Acceptance criteria:**

- `sandbox/README.md` has a new troubleshooting section covering at least three common startup problems.
- The section preserves the repository safety boundary around mock data, runtime databases, and secrets.
- Existing quick-start commands remain unchanged unless they are being clarified.
- Verification notes include a Markdown review and, if shell snippets are changed, `bash -n scripts/start_sandbox.sh`.

**Why it is newcomer-friendly:**

This is a documentation-only task with a clear target file, concrete examples, and no need to understand the full collaboration engine internals.

## 2. Add a concise Agent Backend example

**Labels:** `good first issue`, `documentation`, `examples`

**Body:**

The README explains that Claude Code, Hermes Agent, Codex, OpenCode, and custom command-backed agents can share the same Agent Backend abstraction. Add a small example document that shows the shape of a custom command backend configuration and the questions a contributor should answer before implementing one.

Suggested scope:

- Create a concise example under `examples/` or a short section linked from `README.en.md`.
- Include a minimal command-backed backend sketch using placeholder values only.
- Explain what should be verified locally before proposing a backend.
- Avoid real credentials, local machine paths, or private model configuration.

**Acceptance criteria:**

- A newcomer can read the example and understand what a custom backend is responsible for: command invocation, working directory, timeout, and result capture.
- The example uses placeholders and does not imply that secrets belong in the repository.
- The README or contributor docs link to the example if a new file is added.
- Verification notes include a Markdown review and any narrow syntax check relevant to files changed.

**Why it is newcomer-friendly:**

The task is mostly explanatory writing. It introduces an important project concept while avoiding changes to backend execution code.

## 3. Add dashboard screenshot refresh guidance

**Labels:** `good first issue`, `documentation`, `dashboard`, `assets`

**Body:**

The README displays `docs/screenshots/dashboard.png`, but contributors do not yet have a short checklist for refreshing screenshots safely. Add documentation that explains when to update the screenshot, how to use the sandbox rather than production data, and what details to inspect before committing an image.

Suggested scope:

- Add a small section to `CONTRIBUTING.md` or a launch/docs page.
- Point contributors to the sandbox quick start.
- Require sanitized data and no visible secrets, usernames, private paths, or real logs.
- Keep image replacement outside the issue unless the current screenshot is intentionally being refreshed.

**Acceptance criteria:**

- The documentation names `docs/screenshots/dashboard.png` as the canonical dashboard image used by the README.
- The guidance tells contributors to capture screenshots from sandbox or sanitized data only.
- The guidance includes a short pre-commit review checklist for images.
- Verification notes include manual Markdown review; no full test suite is required.

**Why it is newcomer-friendly:**

This is a bounded contributor-experience improvement that helps future visual changes without requiring frontend or backend code changes.

## 4. Add CLI status JSON regression coverage

**Labels:** `good first issue`, `tests`, `cli`

**Body:**

`hermes-collab status --json` is listed as a quick way to inspect local state. Add a focused regression test that checks the command returns valid JSON with the expected top-level fields, using existing CLI test patterns.

Suggested scope:

- Look at existing tests under `tests/` for CLI invocation style.
- Add one narrow test for `status --json` rather than broad CLI coverage.
- Assert stable structural fields, not volatile machine-specific values.
- Keep the test independent from real runtime databases, credentials, and agent installations.

**Acceptance criteria:**

- A new or updated test covers the JSON status command.
- The test avoids brittle assertions on local paths, timestamps, installed agents, or private configuration.
- The narrow test command passes locally, for example `PYTHONPATH=src python3 -m unittest tests.test_startup_mode -v` or the closest existing CLI test file.
- No production data, secrets, or generated runtime artifacts are required.

**Why it is newcomer-friendly:**

The task has a small testing surface and can follow nearby unit-test patterns without changing runtime behavior.

## 5. Add sandbox TTL edge-case tests

**Labels:** `good first issue`, `tests`, `sandbox`

**Body:**

The sandbox launcher supports custom TTL values such as `0.5` hours and defaults to an auto-stop timeout. Add targeted tests or script checks for TTL parsing edge cases that are easy to reason about and do not start long-lived processes.

Suggested scope:

- Inspect existing sandbox tests before choosing the exact test location.
- Cover one or two edge cases such as fractional hours and invalid values.
- Prefer testing parsing/helper behavior over launching a real server.
- Keep the test fast and deterministic.

**Acceptance criteria:**

- The added coverage proves a valid fractional TTL is accepted or represented correctly.
- The added coverage proves an invalid TTL is rejected or handled with the documented error path.
- The verification command is narrow, such as `PYTHONPATH=src python3 -m unittest tests.test_sandbox_ttl -v`.
- The test does not leave behind sandbox processes, databases, logs, or workspace files.

**Why it is newcomer-friendly:**

The behavior is concrete and already documented. The contributor can focus on one existing test area instead of learning the full engine.

## 6. Polish dashboard empty-state copy

**Labels:** `good first issue`, `ui polish`, `dashboard`

**Body:**

When the dashboard has no runs, logs, lessons, or workers to show, the empty-state text should help new users understand what to do next. Review the dashboard HTML and improve one or two empty states with clearer copy that points to the sandbox or CLI quick start.

Suggested scope:

- Keep the change limited to static copy in the dashboard UI.
- Do not redesign layout, colors, or data fetching in the same PR.
- Keep wording concise and consistent with the local-first safety boundary.
- If both `web/index.html` and `sandbox/index.html` share the same copy, keep them aligned.

**Acceptance criteria:**

- At least one confusing or generic empty state is replaced with actionable text.
- The copy does not imply the sandbox uses production data or real workers by default.
- If mirrored dashboard files are updated, the same user-facing copy appears in each relevant file.
- Verification notes include manual browser or static review; no full test suite is required.

**Why it is newcomer-friendly:**

This is a small UI writing task. It is easy to review visually and does not require changing dashboard APIs.

## 7. Add issue-template label guidance

**Labels:** `good first issue`, `documentation`, `github`

**Body:**

The repository has GitHub issue templates for bug reports, docs, and feature requests. Add a short maintainer-facing note that suggests labels for first-response triage, including `good first issue`, `documentation`, `tests`, `dashboard`, and `sandbox`.

Suggested scope:

- Update `CONTRIBUTING.md`, a launch doc, or the issue template descriptions with lightweight label guidance.
- Keep guidance advisory; do not require a specific GitHub label configuration file.
- Mention that security vulnerabilities should still follow `SECURITY.md` instead of public issues.
- Do not create labels or issues through the GitHub API.

**Acceptance criteria:**

- Maintainers have a concise reference for which labels fit common newcomer-friendly issues.
- The note preserves the existing security-reporting boundary.
- The guidance does not depend on labels already existing in the remote repository.
- Verification notes include Markdown review only.

**Why it is newcomer-friendly:**

This is a low-risk repository hygiene task that helps future contributors and maintainers without requiring code changes.

## 8. Add README localization alignment checklist

**Labels:** `good first issue`, `documentation`, `localization`

**Body:**

The project keeps concise README files in Chinese, English, and Japanese. Add a small checklist that helps contributors decide when a README change needs corresponding localization follow-up.

Suggested scope:

- Add the checklist to `CONTRIBUTING.md` or a short docs page.
- Name `README.md`, `README.en.md`, and `README.ja.md` explicitly.
- Encourage concise updates and a note in the PR when translations are intentionally deferred.
- Do not rewrite the README files as part of this issue unless the checklist reveals a tiny inconsistency.

**Acceptance criteria:**

- The checklist explains which user-facing changes should be reflected across all three README files.
- The checklist tells contributors how to document intentional localization follow-up in a PR.
- The guidance remains brief and does not duplicate the full README content.
- Verification notes include Markdown review only.

**Why it is newcomer-friendly:**

The task has a clear documentation target and helps contributors make safe, small multilingual updates without needing deep project knowledge.
