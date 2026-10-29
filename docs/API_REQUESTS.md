# API requests

Every request the system sends, generated from the adapters by `scripts/api_examples.py`. Keys are placeholders; values are the sample deal (Acme Corp, opportunity `006Ka00000AbCdEIAV`). Gmail goes through Google's client library (query `in:inbox -label:novacrm-processed newer_than:7d -from:me`, then label and reply in the thread).

## Rocketlane

### Step 4: is there a project for this opportunity? (archived ones included)

```http
GET https://api.rocketlane.com/api/1.0/projects?externalReferenceId.eq=006Ka00000AbCdEIAV&includeArchive.eq=true&includeFields=sources%2CplannedDuration%2CexternalReferenceId&pageSize=100
api-key: <API_KEY>
```

### Step 4: is there a project for this customer made by hand?

```http
GET https://api.rocketlane.com/api/1.0/projects?projectName.cn=Acme+Corp&includeFields=sources%2CplannedDuration%2CexternalReferenceId&pageSize=100
api-key: <API_KEY>
```

### Step 7, template mode: create the project from the template

Rocketlane computes the due date from the template, so none is sent.

```http
POST https://api.rocketlane.com/api/1.0/projects
api-key: <API_KEY>
```

```json
{
  "projectName": "Acme Corp - Enterprise onboarding",
  "owner": {
    "emailId": "priya@novacrm.example"
  },
  "customer": {
    "companyName": "Acme Corp"
  },
  "autoCreateCompany": true,
  "startDate": "2026-10-29",
  "externalReferenceId": "006Ka00000AbCdEIAV",
  "visibility": "MEMBERS",
  "teamMembers": {
    "members": [
      {
        "emailId": "asha@novacrm.example"
      }
    ],
    "customers": [
      {
        "emailId": "john.doe@acme.com"
      }
    ]
  },
  "sources": [
    {
      "templateId": 201,
      "startDate": "2026-10-29"
    }
  ],
  "placeholders": [
    {
      "placeholderId": 11,
      "user": {
        "emailId": "asha@novacrm.example"
      }
    },
    {
      "placeholderId": 12,
      "user": {
        "emailId": "john.doe@acme.com"
      }
    }
  ]
}
```

### (read back after create)

```http
GET https://api.rocketlane.com/api/1.0/projects/5001?includeFields=sources%2CplannedDuration%2CexternalReferenceId
api-key: <API_KEY>
```

### Step 7, api mode: create an empty project with explicit dates

```http
POST https://api.rocketlane.com/api/1.0/projects
api-key: <API_KEY>
```

```json
{
  "projectName": "Acme Corp - Enterprise onboarding",
  "owner": {
    "emailId": "priya@novacrm.example"
  },
  "customer": {
    "companyName": "Acme Corp"
  },
  "autoCreateCompany": true,
  "startDate": "2026-10-29",
  "externalReferenceId": "006Ka00000AbCdEIAV",
  "visibility": "MEMBERS",
  "teamMembers": {
    "members": [
      {
        "emailId": "asha@novacrm.example"
      }
    ],
    "customers": [
      {
        "emailId": "john.doe@acme.com"
      }
    ]
  },
  "dueDate": "2026-11-28"
}
```

### (read back after create)

```http
GET https://api.rocketlane.com/api/1.0/projects/5001?includeFields=sources%2CplannedDuration%2CexternalReferenceId
api-key: <API_KEY>
```

### Step 8: list the phases (`projectId` is a required parameter)

```http
GET https://api.rocketlane.com/api/1.0/phases?projectId=5001&pageSize=100
api-key: <API_KEY>
```

### Step 8: list the tasks, with their phase, assignees and dependencies

These are optional fields in v1.3 and must be requested.

```http
GET https://api.rocketlane.com/api/1.0/tasks?projectId.eq=5001&includeFields=phase%2Cassignees%2Cdependencies&pageSize=100
api-key: <API_KEY>
```

### api mode: create a phase

```http
POST https://api.rocketlane.com/api/1.0/phases
api-key: <API_KEY>
```

```json
{
  "phaseName": "Kickoff",
  "project": {
    "projectId": 5001
  },
  "startDate": "2026-10-29",
  "dueDate": "2026-11-03"
}
```

### Step 7: create the deal's kickoff task (both modes; api mode creates all 15 this way)

```http
POST https://api.rocketlane.com/api/1.0/tasks
api-key: <API_KEY>
```

```json
{
  "taskName": "Kickoff call with John Doe",
  "taskDescription": "<p>AE: Rahul Mehta. Notes: pipeline reports.</p>",
  "project": {
    "projectId": 5001
  },
  "phase": {
    "phaseId": 7
  },
  "dueDate": "2026-11-01",
  "assignees": {
    "members": [
      {
        "emailId": "asha@novacrm.example"
      }
    ]
  },
  "type": "TASK",
  "startDate": "2026-10-30"
}
```

### api mode: go-live readiness depends on the sign-off

The path task starts after the listed tasks are complete.

```http
POST https://api.rocketlane.com/api/1.0/tasks/9013/add-dependencies
api-key: <API_KEY>
```

```json
{
  "dependencies": [
    {
      "taskId": 9008
    }
  ]
}
```

## Slack

### Create the channel

Slack Web API calls are form-encoded. Production adds `conversations.inviteShared`.

```http
POST https://slack.com/api/conversations.create
authorization: <AUTHORIZATION>
```

```json
{
  "name": "onb-acme-corp"
}
```

### Mark it with the opportunity (its purpose)

```http
POST https://slack.com/api/conversations.setPurpose
authorization: <AUTHORIZATION>
```

```json
{
  "channel": "C0001",
  "purpose": "NovaCRM onboarding for Salesforce opportunity 006Ka00000AbCdEIAV"
}
```

### Set the topic

```http
POST https://slack.com/api/conversations.setTopic
authorization: <AUTHORIZATION>
```

```json
{
  "channel": "C0001",
  "topic": "Enterprise onboarding · 30 days (29 Oct → go-live 28 Nov) · ..."
}
```

### Find the CSM's Slack user

```http
POST https://slack.com/api/users.lookupByEmail
authorization: <AUTHORIZATION>
```

```json
{
  "email": "asha@novacrm.example"
}
```

### Invite the CSM and the AE

```http
POST https://slack.com/api/conversations.invite
authorization: <AUTHORIZATION>
```

```json
{
  "channel": "C0001",
  "users": "U0002"
}
```

### Was this deal's welcome already posted? (after a crash)

```http
POST https://slack.com/api/conversations.history
authorization: <AUTHORIZATION>
```

```json
{
  "channel": "C0001",
  "limit": "100",
  "include_all_metadata": "true"
}
```

### Post the welcome, marked with the opportunity

```http
POST https://slack.com/api/chat.postMessage
authorization: <AUTHORIZATION>
```

```json
{
  "channel": "C0001",
  "text": "We're thrilled to ...\n\nHi John, I'm Asha Rao, ...",
  "mrkdwn": "true",
  "metadata": {
    "event_type": "novacrm_welcome",
    "event_payload": {
      "marker": "006Ka00000AbCdEIAV"
    }
  }
}
```

## Vapi

### Step 5: phone the AE

The assistant itself is defined in config/vapi_assistant.json (`novacrm setup-voice`).

```http
POST https://api.vapi.ai/call
authorization: <AUTHORIZATION>
```

```json
{
  "assistantId": "<VAPI_ASSISTANT_ID>",
  "phoneNumberId": "<VAPI_PHONE_NUMBER_ID>",
  "customer": {
    "number": "+919800000001"
  },
  "assistantOverrides": {
    "variableValues": {
      "customer_name": "Acme Corp",
      "ae_first_name": "Rahul",
      "deal": "D1"
    }
  },
  "metadata": {
    "deal": "D1"
  }
}
```

### Step 5: read the finished call (status, endedReason, transcript)

```http
GET https://api.vapi.ai/call/call_123
authorization: <AUTHORIZATION>
```

## Gemini

### Step 3: extract the deal fields (schema-bound)

```http
POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent
x-goog-api-key: <X_GOOG_API_KEY>
```

```json
{
  "systemInstruction": {
    "parts": [
      {
        "text": "You extract facts from an email ..."
      }
    ]
  },
  "contents": [
    {
      "role": "user",
      "parts": [
        {
          "text": "<email>\n...\n</email>"
        }
      ]
    }
  ],
  "generationConfig": {
    "temperature": 0,
    "responseMimeType": "application/json",
    "responseSchema": {
      "type": "OBJECT",
      "properties": {
        "is_deal_notification": {
          "type": "BOOLEAN"
        },
        "customer_name": {
          "type": "STRING",
          "nullable": true
        },
        "contact_name": {
          "type": "STRING",
          "nullable": true
        },
        "contact_email": {
          "type": "STRING",
          "nullable": true
        },
        "opportunity_link": {
          "type": "STRING",
          "nullable": true
        },
        "ae_name": {
          "type": "STRING",
          "nullable": true
        },
        "close_date": {
          "type": "STRING",
          "nullable": true
        },
        "notes": {
          "type": "STRING",
          "nullable": true
        }
      },
      "required": [
        "is_deal_notification"
      ]
    }
  }
}
```
