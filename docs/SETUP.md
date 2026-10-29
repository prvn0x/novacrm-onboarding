# Setup: accounts, configuration, and the day-1 checks

Everything that depends on an account or on an answer from Rocketlane is a configuration value.
Nothing in the code changes when the answers arrive: fill in the files below, run the checks,
and run the agent.

| File | What goes in it | In git? |
|---|---|---|
| `.env` | API keys, and the three mode switches | No (`.env.example` is) |
| `config/team.yaml` | People, the AE directory, Rocketlane template IDs and durations | No (`team.example.yaml` is) |
| `config/onboarding_plan.yaml` | The 4 phases and 15 tasks for both tiers | Yes |
| `config/vapi_assistant.json` | The voice assistant | Yes |
| `config/slack_app_manifest.yaml` | The Slack app | Yes |

## 1. The mode switches

| Setting | Values | Use |
|---|---|---|
| `rocketlane.mode` in `team.yaml` | `template` (default), `api` | `template` applies the Rocketlane template through the API. `api` creates every phase and task from `onboarding_plan.yaml` through the API; use it if the trial cannot apply templates through the API |
| `VOICE_MODE` in `.env` | `phone` (default), `web` | `web` reaches the same Vapi assistant through the browser, if the voice tier cannot call your +91 number |
| `SLACK_MODE` in `.env` | `real` (default), `console` | `console` prints every channel, topic and message in the terminal (the brief allows a mocked Slack) |

## 2. Where each open question lands

The questions emailed to Karthik on 27 Oct, and the one value each answer changes:

| Question | Today (assumption) | When answered, change |
|---|---|---|
| Rocketlane trial access | Built against the fake | `.env`: `ROCKETLANE_API_KEY` |
| API and Automations in the trial | Template mode | `team.yaml`: `rocketlane.mode` (`api` if templates cannot be applied) |
| Voice to Indian numbers | Phone call | `.env`: `VOICE_MODE=web` if it cannot |
| Slack Connect | Our own workspace | Nothing (production note), or `SLACK_MODE=console` |
| Where the AE's phone comes from | AE directory | `team.yaml`: `account_executives` |
| Duplicate key | Opportunity ID | Nothing |
| PM and Owner per tier | Owner = CS Manager, PM = CSM | `team.yaml`: `cs_manager`, `dedicated_csms`, `pooled_csm` |
| Escalation target | CS Manager in Slack | `team.yaml`: `escalation_channel` |
| Timeline start | Next business day | `domain.project_start` (one function) |

## 3. Accounts (personal accounts only)

**Gemini.** aistudio.google.com → Get API key → `.env`: `GEMINI_API_KEY`.

**Gmail (the "CS inbox").** Create a new Gmail, e.g. `novacrm.cs.demo@gmail.com`. Then in
console.cloud.google.com: new project → enable the Gmail API → OAuth consent screen (External; add
the demo Gmail as a test user) → Credentials → OAuth client ID → Desktop app → download the JSON
to `var/gmail-credentials.json`. Run `uv run novacrm gmail-auth` and sign in **as the demo Gmail**.

**Slack.** Create a free workspace. api.slack.com/apps → Create New App → From an app manifest →
paste `config/slack_app_manifest.yaml` → Install to workspace → copy the Bot User OAuth Token to
`.env`: `SLACK_BOT_TOKEN`. Create the channel `#cs-onboarding-escalations`. Invite your own
addresses as the CSM, AE and CS Manager so the bot can find them by email.

**Vapi.** dashboard.vapi.ai (sign up with your personal Gmail) → API Keys: copy the private key to
`VAPI_API_KEY` (and the public key to `VAPI_PUBLIC_KEY` for web mode) → Phone Numbers: create a
free number, or import a Twilio trial number (verify your +91 number in Twilio and allow India in
its geo permissions) → copy its ID to `VAPI_PHONE_NUMBER_ID`. Then:

```bash
uv run novacrm setup-voice                     # creates the assistant; copy the id to VAPI_ASSISTANT_ID
uv run novacrm test-call --to +9198XXXXXXXX    # rings your phone; answer "Enterprise", then "Yes"
```

If the call cannot reach +91, set `VOICE_MODE=web`: the agent opens a page and you answer in the
browser. Same assistant, same transcript, same decision code.

**Rocketlane.** When access arrives: Settings → API → generate a key → `ROCKETLANE_API_KEY`. Build
the templates and automations with [ROCKETLANE_SETUP.md](ROCKETLANE_SETUP.md) (about 30 minutes),
or set `rocketlane.mode: api` and skip the templates.

## 4. Fill in `team.yaml`

```bash
cp config/team.example.yaml config/team.yaml
```

Use your own addresses with `+` aliases (`you+asha@gmail.com` and so on); Gmail delivers them all
to you. Put your own number as the AE's phone.

## 5. The day-1 checks, then the run

```bash
uv run novacrm check                                       # every account answers (read-only)
uv run novacrm verify-platform --contact you+customer@gmail.com
```

`verify-platform` creates one test project per tier the way the agent will, reads back what
Rocketlane really returns, and prints a PASS/FAIL table: template applied, planned duration (copy
the number into `team.yaml`), phases, tasks, who owns the sign-off, the go-live dependency, and
the archived-project lookup. It archives its test projects afterwards (`--delete` to delete them),
saves raw responses under `var/fixtures/` and the results in `var/platform-checks.json`, and lists
the checks only the Rocketlane UI can show (the automations). Copy the results into §14 of the
design doc.

```bash
CALL_SPACING_SECONDS=20 uv run novacrm run     # the agent; send the deal email from your own Gmail
uv run novacrm trace D1                        # what it did and why
```

## 6. Without any account

```bash
make check                  # lint, types and the full test suite
uv run novacrm demo         # every scenario of the brief, offline, with its audit trail
```

Every request the system sends is in [API_REQUESTS.md](API_REQUESTS.md), generated from the code
by `scripts/api_examples.py`.
