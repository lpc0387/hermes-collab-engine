# Contributing to Hermes Collab Engine

Thanks for helping improve Hermes Collab Engine. This project coordinates AI workers, local dashboards, sandbox runs, and release documentation, so small, well-scoped contributions are easiest to review.

## Start here

1. Read the release notes in [`CHANGELOG.md`](CHANGELOG.md) and the current roadmap in [`ROADMAP.md`](ROADMAP.md).
2. Install locally:

   ```bash
   python3 -m pip install -e .
   ```

3. Run the narrow check for the area you changed. For general Python changes:

   ```bash
   PYTHONPATH=src python3 -m unittest discover -s tests -v
   ```

4. For dashboard or sandbox changes, also review [`sandbox/README.md`](sandbox/README.md) and avoid committing runtime databases or generated logs.

## Issue triage

Use the GitHub issue templates when possible:

- Bug reports should include reproduction steps, expected behavior, actual behavior, environment, and relevant logs with secrets removed.
- Feature requests should describe the user workflow, proposed behavior, alternatives considered, and safety impact.
- Documentation issues should name the affected language or page and the command or workflow that confused you.

Please keep one issue focused on one problem. Security vulnerabilities should not be filed as public issues; follow [`SECURITY.md`](SECURITY.md).

## Pull request workflow

- Open focused PRs with a clear summary, linked issue, and verification commands.
- Keep generated/runtime artifacts out of the diff, especially `data/*.sqlite3`, logs, credentials, local `.env` files, and real Hermes or Claude configuration.
- Match existing style and naming. Prefer concise Markdown, simple Python, and explicit safety boundaries.
- Update docs and localization together when user-facing behavior changes. At minimum, note whether `README.md`, `README.en.md`, and `README.ja.md` need follow-up.
- Include screenshots or API payload examples for dashboard-visible changes when practical.

## README localization checklist

The project maintains three README files in parallel:

- `README.md` — Chinese (primary)
- `README.en.md` — English
- `README.ja.md` — Japanese

Use this checklist when your PR touches any README to decide whether the other two files need a corresponding update.

**Changes that should be reflected in all three files:**

- [ ] New CLI command or flag added or removed
- [ ] New API endpoint or changed response shape
- [ ] New installation step or changed quick-start command
- [ ] New top-level feature added to the highlights table
- [ ] Changed default behavior (e.g. sandbox TTL, port, model env var)
- [ ] Corrected factual error or outdated description

**Changes that do not require immediate localization:**

- Minor wording or formatting fixes within a single language
- Adding or updating a code example that is language-neutral (copy it across if easy, skip if not)
- Internal-only documentation (e.g. `sandbox/README.md`, `docs/`)

**When you defer a translation:**

Add a short note in your PR description, for example:

> Localization follow-up deferred: `README.en.md` and `README.ja.md` need the new `--real` flag documented. Filed as a follow-up.

This keeps the repo history honest without blocking your PR on translation work.

**Verification:** Markdown lint only — no automated translation check is required.

---

## Tests and verification

Choose the smallest command that proves the change:

```bash
python3 -m py_compile src/hermes_collab_engine/*.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
bash -n scripts/start_sandbox.sh scripts/install.sh
```

If a command fails, include the failure in the PR and explain whether it is related to your change.

## Safety boundaries

Hermes Collab Engine should remain safe for public release:

- Do not commit real API keys, auth files, tokens, session data, SQLite runtime state, or private logs.
- Do not make sandbox examples call real workers by default.
- Keep dashboard exposure local-first unless a deployment guide explicitly adds authentication, network binding guidance, and risk warnings.
- Do not broaden tool permissions, git write access, or process execution without documenting the reason and review path.
