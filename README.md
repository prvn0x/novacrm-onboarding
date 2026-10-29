# NovaCRM onboarding agents

A deal email arrives in the CS inbox, and the right Rocketlane onboarding project and customer
Slack channel exist a few minutes later, with no one guessing the plan tier.

- **Intake & Routing Agent** watches a Gmail inbox, reads the AE's deal email (an LLM extracts,
  code validates every field against the email), asks the AE in the thread when something is
  missing, **phones the AE** (Vapi) to confirm Enterprise or Growth, and creates the Rocketlane
  project from the matching template, then verifies it.
- **Communication Agent** creates the customer's Slack channel with a tier-specific topic and
  welcome message.
- **Rocketlane automation** (configured in Rocketlane) notifies the Project Manager when a task is
  1 day overdue and the Project Owner at 4 days.

The rule throughout: **the model reads and writes text; plain code decides.** Every action is in
an audit log with its reason. The design, assumptions and trade-offs are in
[docs/REQUIREMENTS_AND_DESIGN.md](docs/REQUIREMENTS_AND_DESIGN.md).

```
Gmail ─► Intake: sender? ─► extract + ground ─► duplicate? ─► call the AE ─► tier ─► Rocketlane
                  │              │ missing → ask AE    │ yes → tell AE   │ unsure/ambiguous/no answer
                  ▼              ▼                     ▼                 ▼ → retry or a person decides
               reject        NEEDS_INFO            DUPLICATE        verify ─► handoff ─► Communication
                                                                                      ─► Slack channel
```

## Run the tests (no accounts needed)

```bash
make install
make check        # ruff, mypy --strict, and 220 tests against in-memory fakes, in under a second
```

The tests cover every case in the brief: the happy path for both tiers, malformed and incomplete
emails, template accuracy, Rocketlane down (before and after the call), existing projects (by
reference and made by hand), the AE not answering, ambiguous and unsure answers, Slack failures,
crashes after each external write, and prompt injection.

## See every scenario without any account

```bash
uv run novacrm demo          # the brief's scenarios, offline, each with its audit trail
```

## Run it for real

Full instructions: [docs/SETUP.md](docs/SETUP.md). Every open question and every account is a
config value; three switches cover what the trial might not support: `rocketlane.mode`
(`template` | `api`), `VOICE_MODE` (`phone` | `web`) and `SLACK_MODE` (`real` | `console`).
`novacrm verify-platform` runs the day-1 Rocketlane checks; `novacrm test-call` checks the voice
route. Every request the system sends is in [docs/API_REQUESTS.md](docs/API_REQUESTS.md).


1. `cp .env.example .env` and `cp config/team.example.yaml config/team.yaml` (both git-ignored).
2. **Rocketlane:** build the two templates from §9 of the design doc (`Enterprise Onboarding - 30
   day`, `Growth Onboarding - 14 day`, each with a "Project Manager" placeholder and a "Customer
   data owner" placeholder), add the two overdue automations from §10, and put the template and
   placeholder IDs in `team.yaml`. Create an API key and set `ROCKETLANE_API_KEY`.
3. **Vapi:** set `VAPI_API_KEY` and `VAPI_PHONE_NUMBER_ID`, then `uv run novacrm setup-voice`
   (creates the assistant from `config/vapi_assistant.json`) and set `VAPI_ASSISTANT_ID`.
4. **Slack:** a bot with `channels:manage channels:read channels:join channels:history chat:write
   users:read users:read.email`; set `SLACK_BOT_TOKEN`; create `#cs-onboarding-escalations`.
5. **Gmail:** an OAuth client (desktop app) saved as `var/gmail-credentials.json`, then
   `uv run novacrm gmail-auth`.
6. **LLM:** `GEMINI_API_KEY`.
7. `uv run novacrm check`, then `make run`.

For the demo, `CALL_SPACING_SECONDS=20` shortens the 15-minute gap between calls.

## Commands

```
novacrm run [--once]                          watch the inbox, advance every open deal
novacrm deals                                 every deal and its state
novacrm trace D3                              one deal's audit trail with the reasons
novacrm resolve D3 --tier growth --by priya   a person sets the tier (tier reviews only)
novacrm retry D3 --by priya [--not-duplicate] resume after fixing the cause
novacrm close D3 --by priya --reason "..."
novacrm check | verify-platform | test-call | demo | fault rocketlane down|up
```

## What is different in production

Gmail push instead of polling, Postgres instead of SQLite, a worker queue with the call-ended
webhook, Slack Connect to invite the customer, a secrets manager, the AE directory from Salesforce,
and, most importantly, the tier read from the Salesforce opportunity, with the call kept as the
fallback. Details in §13 of the design doc.
