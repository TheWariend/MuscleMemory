# Smoke run: baseline vs skill

Model: `gpt-6-luna`. CLI: `codex-cli 0.160.0`. Reasoning: high. Smoke campaign; no full campaign.

| Metric | Baseline | Skill |
|---|---:|---:|
| Pass | 1/1 (100%) | 1/1 (100%) |
| Wrong submissions | 0 | 0 |
| Median browser tool calls | 36 | 14 |
| Tokens total / uncached | 559,761 / 42,641 | 498,059 / 47,883 |
| Median time (s) | 55.2 | 37.7 |
| Skill found / called / finished | 0 / 0 / 0 | 1 / 1 / 0 |

## Resource selection

| Arm | Submitted resource | Cheapest eligible | Cheapest total | Payload mismatches |
|---|---|---|---:|---|
| baseline | Resource 07 | Resource 07 | 1022 cents | [{}] |
| skill | Resource 07 | Resource 07 | 1022 cents | [{}] |

## Every run_skill reply

### Call 1

Codex CLI receipt (raw log not published).

```json
{
  "notice": "MuscleMemory carried out the proven steps below with checks and clicked no final submit. Check the page, do anything left for you, then submit yourself.",
  "done": [
    "Requests",
    "Equipment services",
    "Equipment loan",
    "Given name",
    "Family name",
    "Applicant email",
    "Contact telephone",
    "Organization",
    "Street address",
    "City",
    "Postal code",
    "Given name",
    "Family name",
    "Applicant email",
    "Contact telephone",
    "Street address",
    "City",
    "Postal code",
    "Region",
    "Service district",
    "Requested service",
    "Delivery or session slot",
    "Reserved resource"
  ],
  "left_for_you": [
    {
      "field": "Application reference",
      "value": "APP-EQUIPMENT-10000",
      "why": "not found, maybe renamed"
    },
    {
      "field": "Cost center",
      "value": "CC-10000",
      "why": "not found, maybe renamed"
    }
  ],
  "stopped": "fill_these_then_run_again",
  "at": "Continue to review",
  "page_notices": [],
  "next": "Fill the fields left for you, then call this skill again with the same inputs. It carries on from there (later steps and pages too) and stops before the submit."
}
```

Page before call: `http://127.0.0.1:49444/?case=score-equipment-renamed-10000-skill`.

Stopped at `Continue to review`: `fill_these_then_run_again`.

Learned browser step 26: `click` on `Continue to review` in `Service application > Applicant details`.

Full page read after call 1. This was the next tool call.

Page: `http://127.0.0.1:49444/forms/equipment?case=score-equipment-renamed-10000-skill`. Notices: ["Service changed; choose a slot."]

```json
[
  {
    "ref": "74:0",
    "role": "textbox",
    "name": "Service application ID",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:1",
    "role": "textbox",
    "name": "Given name",
    "value": "Ari10000",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:2",
    "role": "textbox",
    "name": "Family name",
    "value": "Patel10000",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:3",
    "role": "textbox",
    "name": "Applicant email",
    "value": "ari.equipment.10000@example.test",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:4",
    "role": "textbox",
    "name": "Contact telephone",
    "value": "+1-202-555-0000",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:5",
    "role": "textbox",
    "name": "Organization",
    "value": "Studio 10000",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:6",
    "role": "textbox",
    "name": "Billing cost center",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:7",
    "role": "textbox",
    "name": "Street address",
    "value": "110 Cedar Avenue",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:8",
    "role": "textbox",
    "name": "City",
    "value": "Cedar City 10000",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:9",
    "role": "textbox",
    "name": "Postal code",
    "value": "20000",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:10",
    "role": "textbox",
    "name": "Given name",
    "value": "Sam10000",
    "section": "Service application > Applicant details > Billing contact"
  },
  {
    "ref": "74:11",
    "role": "textbox",
    "name": "Family name",
    "value": "Chen10000",
    "section": "Service application > Applicant details > Billing contact"
  },
  {
    "ref": "74:12",
    "role": "textbox",
    "name": "Applicant email",
    "value": "billing.equipment.10000@example.test",
    "section": "Service application > Applicant details > Billing contact"
  },
  {
    "ref": "74:13",
    "role": "textbox",
    "name": "Contact telephone",
    "value": "+1-303-555-0000",
    "section": "Service application > Applicant details > Billing contact"
  },
  {
    "ref": "74:14",
    "role": "textbox",
    "name": "Street address",
    "value": "420 Oak Street",
    "section": "Service application > Applicant details > Billing contact"
  },
  {
    "ref": "74:15",
    "role": "textbox",
    "name": "City",
    "value": "Oak City 10000",
    "section": "Service application > Applicant details > Billing contact"
  },
  {
    "ref": "74:16",
    "role": "textbox",
    "name": "Postal code",
    "value": "30000",
    "section": "Service application > Applicant details > Billing contact"
  },
  {
    "ref": "74:17",
    "role": "combobox",
    "name": "Region",
    "value": "North",
    "section": "Service application > Applicant details",
    "options": [
      "Choose…",
      "South",
      "North",
      "West"
    ]
  },
  {
    "ref": "74:18",
    "role": "combobox",
    "name": "Service district",
    "value": "North / Central",
    "section": "Service application > Applicant details",
    "options": [
      "Choose…",
      "North / Central",
      "North / Outer"
    ]
  },
  {
    "ref": "74:19",
    "role": "combobox",
    "name": "Requested service",
    "value": "Laptop delivery",
    "section": "Service application > Applicant details",
    "options": [
      "Choose…",
      "General inquiry",
      "Laptop delivery",
      "Premium package"
    ]
  },
  {
    "ref": "74:20",
    "role": "combobox",
    "name": "Delivery or session slot",
    "value": "14:00–16:00",
    "section": "Service application > Applicant details",
    "options": [
      "Choose…",
      "09:00–11:00",
      "14:00–16:00"
    ]
  },
  {
    "ref": "74:21",
    "role": "combobox",
    "name": "Reserved resource",
    "section": "Service application > Applicant details",
    "options": [
      "Choose…",
      "Resource 22",
      "Resource 24",
      "Resource 20",
      "Resource 06",
      "Resource 09",
      "Resource 04",
      "Resource 08",
      "Resource 16",
      "Resource 15",
      "Resource 01",
      "Resource 23",
      "Resource 14",
      "Resource 18",
      "Resource 12",
      "Resource 10",
      "Resource 11",
      "Resource 17",
      "Resource 07",
      "Resource 21",
      "Resource 03",
      "Resource 05",
      "Resource 13",
      "Resource 19",
      "Resource 02"
    ]
  },
  {
    "ref": "74:22",
    "role": "button",
    "name": "Continue to review",
    "section": "Service application > Applicant details"
  },
  {
    "ref": "74:28",
    "role": "textbox",
    "name": "Application reference",
    "section": "Archived applications"
  },
  {
    "ref": "74:29",
    "role": "textbox",
    "name": "Applicant email",
    "section": "Archived applications"
  },
  {
    "ref": "74:30",
    "role": "button",
    "name": "Submit application",
    "section": "Archived applications"
  }
]
```

### Call 2

Codex CLI receipt (raw log not published).

```json
{
  "notice": "MuscleMemory carried out the proven steps below with checks and clicked no final submit. Check the page, do anything left for you, then submit yourself.",
  "done": [
    "Application reference",
    "Given name",
    "Family name",
    "Applicant email",
    "Contact telephone",
    "Organization",
    "Cost center",
    "Street address",
    "City",
    "Postal code",
    "Given name",
    "Family name",
    "Applicant email",
    "Contact telephone",
    "Street address",
    "City",
    "Postal code",
    "Region",
    "Service district",
    "Requested service",
    "Delivery or session slot",
    "Reserved resource",
    "Continue to review",
    "Confirm applicant email"
  ],
  "left_for_you": [
    {
      "field": "Authorization code",
      "why": "needs your input"
    }
  ],
  "stopped": "fill_these_then_run_again",
  "at": "Copy applicant address to billing",
  "page_notices": [
    "Review stage. Confirm email and authorization code."
  ],
  "next": "Fill the fields left for you, then call this skill again with the same inputs. It carries on from there (later steps and pages too) and stops before the submit."
}
```

Page before call: `http://127.0.0.1:49444/forms/equipment?case=score-equipment-renamed-10000-skill`.

Stopped at `Copy applicant address to billing`: `fill_these_then_run_again`.

Learned browser step 29: `click` on `Copy applicant address to billing` in `Service application > Application review`.

Full page read after call 2. This was the next tool call.

Page: `http://127.0.0.1:49444/forms/equipment?case=score-equipment-renamed-10000-skill`. Notices: ["Review stage. Confirm email and authorization code."]

```json
[
  {
    "ref": "110:23",
    "role": "textbox",
    "name": "Confirm applicant email",
    "value": "ari.equipment.10000@example.test",
    "section": "Service application > Application review"
  },
  {
    "ref": "110:24",
    "role": "textbox",
    "name": "Authorization code",
    "section": "Service application > Application review"
  },
  {
    "ref": "110:25",
    "role": "checkbox",
    "name": "Copy applicant address to billing",
    "value": "true",
    "section": "Service application > Application review"
  },
  {
    "ref": "110:26",
    "role": "checkbox",
    "name": "Add paid extras",
    "value": "false",
    "section": "Service application > Application review"
  },
  {
    "ref": "110:27",
    "role": "button",
    "name": "Submit application",
    "section": "Service application > Application review > Primary application"
  },
  {
    "ref": "110:28",
    "role": "textbox",
    "name": "Application reference",
    "section": "Archived applications"
  },
  {
    "ref": "110:29",
    "role": "textbox",
    "name": "Applicant email",
    "section": "Archived applications"
  },
  {
    "ref": "110:30",
    "role": "button",
    "name": "Submit application",
    "section": "Archived applications"
  }
]
```

## Validation and limits

Full suite before inference: 264 passed, 7 optional skips. Ruff lint and format checks passed. All six tool logs reconcile with the CLI receipts. No commits or pushes.

Synthetic local site; one form, one renamed variant, one seed per arm. Browser calls count public MCP invocations; skill internals are not extra tool calls. Total = input + output. Uncached = input − cached input + output.


Observed skill behavior: the first call started at the portal home, navigated
through Requests and Equipment services to Equipment loan, and populated 19 of
22 visible form fields. It stopped before learned browser step 26, `click Continue
to review`, with `fill_these_then_run_again`: Application reference and Cost center
had been renamed. The agent also deliberately passed an empty resource input on
this first call; Reserved resource was still blank in the immediately following
page read, even though the reply included that field in `done`.

After reading the table, the agent filled the renamed fields and selected Resource
07 itself before call 2. It also passed Resource 07 in call 2 inputs. That call
accepted the existing selection, reached review and filled Confirm applicant email. It stopped before learned browser step 29, `click Copy applicant address
to billing`, with `fill_these_then_run_again`: Authorization code needed the agent's
input. The immediately following page read showed the email filled, authorization
blank, billing copy checked, and paid extras unchecked. The agent unchecked billing copy,
entered the code, checked the page, submitted and called browser_done. There was no third skill
call and neither reply returned `ready`; finished therefore remains 0.

Both high-reasoning arms submitted Resource 07, the cheapest eligible option:
323 + 409 + 58 × 5 = 1,022 cents. Each high arm made exactly one correct submission and no wrong
submissions.

See [the side-by-side comparison and every full skill reply](comparison.md) and
[structured comparison data](comparison.json). The comparison includes the full
page read immediately after each reply. Test and Ruff receipts are in [validation.json](validation.json).

The full 36-run campaign was not started.
