# Rocketlane setup (template mode): about 30 minutes in the UI

Skip this file entirely with `rocketlane.mode: api` in `config/team.yaml`: the agent then creates
the phases and tasks itself. The automations in step 4 are still configured here.

The plan below is `config/onboarding_plan.yaml`. Labels can differ slightly between Rocketlane
versions; the steps are what to look for.

## 1. Users

Invite three internal users (your own `+` aliases work): the CS Manager (project owner), a
dedicated CSM, and the pooled Growth CSM. Use the same emails in `config/team.yaml`.

## 2. Two templates

Templates → New template:

| | Enterprise | Growth |
|---|---|---|
| Name (must contain the tier) | `Enterprise Onboarding - 30 day` | `Growth Onboarding - 14 day` |
| Duration | 30 days | 14 days |

In each template, add two **placeholders** (roles): **Project Manager** (internal) and **Customer
data owner** (customer). Then add the 4 phases and 15 tasks:

| # | Phase | Task | Enterprise days | Growth days | Assign to |
|---|---|---|---|---|---|
| 1 | Kickoff | Internal handoff review with the AE | 0–1 | 0 | Project Manager |
| 2 | Kickoff | Schedule and run the kickoff call | 1–3 | 1–2 | Project Manager |
| 3 | Kickoff | Agree success criteria and the go-live date | 3–5 | 2 | Project Manager |
| 4 | Kickoff | Share the onboarding plan and set up customer admin access | 3–5 | 2 | Project Manager |
| 5 | Data Migration | Collect data exports and agree the field mapping | 5–9 | 3–4 | Customer data owner |
| 6 | Data Migration | Test import into a sandbox | 9–12 | 5 | Project Manager |
| 7 | Data Migration | Full data import | 12–15 | 6 | Project Manager |
| 8 | Data Migration | **Customer data verification sign-off** | 15–17 | 7 | **Customer data owner** |
| 9 | Configuration | Configure pipelines, fields and user roles | 15–20 | 6–8 | Project Manager |
| 10 | Configuration | Set up integrations | 18–22 | 8–9 | Project Manager |
| 11 | Configuration | Training | 20–25 | 9–10 | Project Manager |
| 12 | Go-Live | User acceptance testing with the customer's champions | 24–27 | 10–11 | Customer data owner |
| 13 | Go-Live | Go-live readiness check (**depends on task 8**) | 27–28 | 12 | Project Manager |
| 14 | Go-Live | Go-live and hypercare | 28–30 | 12–14 | Project Manager |
| 15 | Go-Live | Handoff summary and transfer to Support | 30 | 14 | Project Manager |

Task names must match exactly (the agent checks for task 8 by name). On task 13, add a
dependency on task 8.

## 3. IDs into `config/team.yaml`

Open each template: its ID is in the URL. The placeholder IDs come back in `verify-platform`'s
saved responses (`var/fixtures/*-project.json` and `*-tasks.json`) if the UI does not show them.

```yaml
rocketlane:
  mode: template
  templates:
    enterprise: {template_id: <id>, pm_placeholder_id: <id>, customer_placeholder_id: <id>, planned_duration_days: 30}
    growth:     {template_id: <id>, pm_placeholder_id: <id>, customer_placeholder_id: <id>, planned_duration_days: 14}
```

Run `uv run novacrm verify-platform --contact you+customer@gmail.com` and replace the two
`planned_duration_days` with the values it reports.

## 4. The overdue-escalation automation (the brief's third component)

On **each template** (Automations, or Settings → Automations → scope: template):

| Rule | When | Condition | Then |
|---|---|---|---|
| Overdue +1 day | A task becomes overdue | Overdue for 1 day | Notify: **Project Manager** placeholder (if not offered: the task's assignee) |
| Overdue +4 days | A task becomes overdue | Overdue for 4 days | Notify: **Project Owner** |

In `api` mode there is no template, so create the same two rules **globally** (all projects).

## 5. Make the rules fire before recording

Create a test project from the Enterprise template with a start date about 10 days ago, so some
tasks are already 1 and 4+ days overdue. Check the automation run history and the notifications
(the CSM's and the CS Manager's inboxes). If back-dated tasks do not fire the rules, record the
rule screens and the run history instead, and say so in the video. Note the result in §14.
