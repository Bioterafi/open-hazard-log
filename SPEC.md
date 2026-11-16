# Open Hazard Log — v1 build spec

**Status:** draft for review · 16 Nov 2026 · cut down from the OHL product spec v0.1
**Owner:** Dr Akanimoh Osutuk (Bytes Medical Ltd / Open Nucleus)
**Name:** "Open Hazard Log (OHL)" is a placeholder.

---

## 1. What it is

A hazard log that stays current.

- An **agent** reads a system's docs and code and **proposes** hazards.
- Simple **checks** (no AI) flag when a control's evidence changes or disappears, or when code linked to a hazard changes.
- The **Clinical Safety Officer (CSO)** approves, edits or rejects every proposal. Nothing reaches the log any other way.
- Every change is tied to a named person and kept in a tamper-evident history.
- The log exports in the NHS template layout at any time.

**The one rule:** agents propose, the CSO decides. This is enforced in code: the agent and the checks can only write to the `proposal` table.

---

## 2. Scope

**v1 does**

1. Hold one or more systems' hazard logs (hazards, causes, controls, evidence, risk ratings).
2. Let the CSO add and edit hazards by hand, and import an existing log from CSV/XLSX.
3. Run an agent scan over Markdown docs and a local Git repo, producing proposals with quoted sources.
4. Run three no-AI checks: evidence changed or missing, linked code changed, review overdue.
5. Give the CSO one review screen: approve, edit, reject (with reason).
6. Let the CSO issue numbered, approved versions of the log (DCB0129 3.3.2).
7. Export the hazard log (CSV, XLSX, JSON) in the NHS template layout, a deployer-actions list, and the audit trail.

**v1 does not** (later, in this order of likely value)

- DCB0160 deployer instances and the transfer/acknowledgement workflow (v1 only exports the deployer-actions list).
- Clinical Safety Case Report drafting.
- Incident ingestion (needs PID redaction first).
- GitHub webhooks, CI release gate, Jira/Linear, Google Docs.
- SSO, multi-tenant hosting, self-hosted models.
- Reviewer-vigilance dashboards and seeded checks.

---

## 3. NHS grounding

Read from the primary documents on 16 Nov 2026: DCB0129 specification v4.2 and implementation guidance v3.2 (Amd 24/2018), DCB0160 specification v3.2 and guidance v4.2 (Amd 25/2018), the NHS Digital hazard log template v3.6, and the DTAC form v2.0 (Feb 2026). Links are in §14.

### 3.1 Where the standards stand

- The **2018 versions are still current**.
- NHS England is reviewing both standards. The public consultation ran from 29 Jun to 11 Sep 2026 and is closed. **No response or revised text has been published yet.** Themes from the focus groups include a risk-tiered approach, AI governance and post-implementation monitoring.
- So anything the standard leaves to the organisation goes in `config/`, not code. That covers the risk matrix, acceptability wording, hazard log columns and statuses. When the revised standards land, changing the config should be enough.
- The legal basis is changing too: s250 of the Health and Social Care Act 2012 as amended, and the Data (Use and Access) Act 2025, which adds IT providers. OHL makes no claim about legal compliance.

### 3.2 Requirement → feature

| Clause | Standard asks (paraphrased) | OHL v1 | Stays with people |
|---|---|---|---|
| 0129 2.3.1–2.3.2 | CSO is a suitably qualified, experienced clinician with current professional registration | `cso` users store name and registration body/number; both appear on every export | Appointing the CSO |
| 0129 3.2.1 | The Clinical Risk Management Plan sets the risk acceptability criteria | Matrix and acceptability text come from `config/risk_matrix.toml`; exports name the scheme used | Writing and approving the Plan |
| 0129 3.3.1 | Establish and maintain a Hazard Log (guidance: "a living document") | The app *is* the hazard log | — |
| 0129 3.3.2 | CSO approves **each version** of the Hazard Log | **Issue version** (§7): the CSO freezes a numbered snapshot with their name, time, repo commit and chain hash | The approval |
| 0129 3.3.3, 3.5.3 | An issued Hazard Log accompanies each Clinical Safety Case Report, which is made available to the deploying organisation | Export any issued version; deployer-actions export | Writing the Safety Case Report |
| 0129 4.1.2 | Analysis should be multidisciplinary | The scan supplements a workshop and never replaces it; "Ask someone" routes questions to engineers and clinicians | Running workshops |
| 0129 4.2.1–4.2.2 | Define clinical scope and intended use | The system record holds intended use and clinical context; the scan won't run without them | — |
| 0129 4.3.1 | Identify known and foreseeable hazards in normal and fault conditions | Agent scan (§5) | Deciding what is a hazard |
| 0129 4.4.1, 5.1.1 | Estimate severity and likelihood, derive risk, judge acceptability | Risk is derived from the matrix; acceptability text is shown with every rating; only the CSO sets ratings | The judgement |
| 0129 guidance §6.1 | Controls in order of preference: design → verification/testing → procedures → training → information/warnings | Control types are sorted in this order everywhere; the agent suggests in this order | — |
| 0129 6.3.1–6.3.3 | Implement controls, verify each one and verify its effectiveness | Evidence links per control, re-hashed by the checks (§6) | Judging effectiveness |
| 0129 7.3.1–7.3.4 | Apply the process to every change; keep an audit trail of all versions and patches | Linked-code check (§6); hash-chained event log; issued versions record the commit | Deciding whether a change needs a new Safety Case Report |
| 0129 7.2, 3.6.1 | Monitor incidents; keep a Safety Incident Management Log | **Not in v1** | The organisation's incident process |
| 0160 2.5.1, guidance §3.5 | The deployer uses the manufacturer's safety case and transferred hazards as input | Deployer-actions export (hazards with status Transferred) | The deployer's DCB0160 work |
| 0160 3.3.1–3.3.2 | The deployer keeps its own Hazard Log, approved by its CSO | The same app works for a deployer's system; importing transfer packs is v2 | — |

### 3.3 Hazard log fields

These are the guidance Table 5 fields and the hazard log template v3.6 columns, mapped to OHL:

| Standard / template | OHL field |
|---|---|
| Hazard number, name, description | `id`, `title`, `description` |
| Potential clinical impact (template: Effect, Harm) | `effects`, `harm` |
| Possible causes | `causes[]` |
| Existing controls | `control` with `stage = existing` |
| Initial risk: severity, likelihood, risk, justification | `initial_*` |
| Additional controls: design, test, training, business process | `control` with `stage = additional` and `type` |
| Residual risk: severity, likelihood, risk, justification | `residual_*` |
| Actions: summary, owner | `action`, `owner` |
| Status | `open` (manufacturer actions outstanding) · `transferred` (manufacturer done, deployer actions outstanding) · `closed` (all done) |
| Date added | `created_at` |

### 3.4 Risk matrix (default config)

The guidance marks Tables 7–10 as examples "for illustrative purposes only". Each organisation sets its own in its Plan. OHL ships the example as the default:

| Likelihood ↓ / Severity → | Minor | Significant | Considerable | Major | Catastrophic |
|---|---|---|---|---|---|
| Very high | 3 | 4 | 4 | 5 | 5 |
| High | 2 | 3 | 3 | 4 | 5 |
| Medium | 2 | 2 | 3 | 3 | 4 |
| Low | 1 | 2 | 2 | 3 | 4 |
| Very low | 1 | 1 | 2 | 2 | 3 |

Acceptability (Table 10, paraphrased):

- **5:** unacceptable.
- **4:** the hazard must be eliminated or controlled down to an acceptable level.
- **3:** undesirable; acceptable only where further reduction is impractical.
- **2:** acceptable where further reduction costs more than it gains, or is impractical.
- **1:** acceptable, no further action.

The severity and likelihood definitions from Tables 7–8 go into the config and show as help text beside the rating fields. Severity combines the degree of harm with whether one patient or many are affected. Changing the matrix is a CSO-only action, recorded as an event.

### 3.5 What OHL is not

- **Not the Clinical Risk Management System, Plan or Safety Case Report.** Those stay as documents the organisation writes and approves. OHL holds the Hazard Log and its evidence, and makes it easy to issue alongside them.
- **Doesn't classify medical devices.** Where a product is also a medical device, DCB0129 still applies alongside MHRA regulation. The standard describes itself as broadly consistent with ISO 14971.
- **Helps with DTAC section C1.** C1 asks for the hazard log, the safety case report, and the CSO's name and registration; OHL produces the issued hazard log and the CSO details.

---

## 4. Data

SQLite. JSON columns where a list is simpler than a join. Field names follow §3.3.

```
user       email, name, role: cso | contributor | viewer, registration (body + number), password_hash

system     id, name, version, intended_use, clinical_context, no_pid_confirmed (bool)
           sources (json: doc folders, repo path, code globs)

hazard     id (H-001), system_id, title, description
           causes  (json: [{text, category, paths[]}])     category: software | data | ai-model |
                                                           human-factors | integration | infrastructure
           effects, harm
           initial_severity, initial_likelihood, initial_risk, initial_justification
           residual_severity, residual_likelihood, residual_risk, residual_justification
                                                           risk = matrix lookup, never typed
           status: open | transferred | closed
           deployer_action (text, blank if none), action, owner
           created_at, last_reviewed, review_due, version

control    id (C-001), hazard_ids (json), description
           stage: existing | additional
           type: design | test | process | training | warning     (the standard's order of preference)
           status: proposed | in_place | verified | invalid
           evidence (json: [{label, path_or_url, sha256, checked_at, state: unchecked | ok | changed | missing}])

issue      id (v1, v2…), system_id, issued_at, issued_by, note, repo_commit, chain_head, snapshot (json)

proposal   id (P-001), system_id, origin: scan | check
           kind: new_hazard | change_hazard | evidence_problem | re_review
           target (H-/C- id or null), payload (json: the proposed record, full)
           reasoning, assumptions, citations (json: [{path, lines, quote, found: bool}])
           confidence: low | medium | high, suggested_rating (json, hidden until CSO rates)
           run_id, model_used, prompt_version, inputs_sha256
           status: pending | approved | edited | rejected,  reject_reason

event      seq, at, actor (user email | "agent:<run_id>" | "check:<run_id>")
           action, object_id, before (json), after (json), note
           prev_hash, hash = sha256(prev_hash + canonical_json(row without hash))
```

Rules:

- Only a user with the CSO role can set ratings, approve proposals, close hazards, change the risk matrix, or issue a version.
- Every write to `hazard` or `control` happens in the same transaction as its `event` row.
- `event` is append-only. `ohl verify` re-walks the hash chain and reports the first broken link.
- The log at any date is rebuilt by replaying events up to that date.

---

## 5. The agent (scan)

**Run:** `ohl scan <system>` from the CLI, or the "Run scan" button. One run → at most 15 proposals (configurable); any extra are summarised in one line, not dropped silently.

**Inputs** (assembled by code, in this order):

1. System record: intended use, clinical context.
2. Markdown files under the system's doc folders.
3. Repo file tree, plus files matching configured globs (default: none — the CSO picks which code areas matter).
4. Titles and causes of hazards already in the log (to avoid duplicates).
5. The last 30 rejection reasons for this system (to cut repeat noise).

**What it is asked to do** — one prompt, four lenses, then de-duplicate:

| Lens | Asks |
|---|---|
| Workflow | Step through each clinical workflow: what could go wrong here that harms a patient? |
| Failure modes | Wrong patient, wrong/missing/delayed data, wrong display or calculation, unavailable, integration failure, security failure with clinical impact. |
| AI-specific (if the system uses ML/LLM) | Fabrication, omission, automation bias, deskilling, distribution shift, subgroup under-performance, prompt injection, model version change, silent degradation. |
| Human factors | Alert fatigue, workarounds, ambiguous UI, handover, time pressure. |

Each lens covers normal use and fault conditions (DCB0129 4.3.1).

**Output** (enforced JSON schema, per candidate): harm-framed title, causes (with category and linked paths), effects, harm, suggested controls (ranked design → test → process → training → warning), suggested severity and likelihood with rationale, citations (path, line range, exact quote), confidence, assumptions about clinical context, and which existing hazard it duplicates or modifies (if any).

**Guardrails, all in code:**

- **Quote check.** Every citation's quote must appear verbatim in the cited file. Failed quotes are marked `found: false`, shown in red, and the proposal's confidence drops to `low`. A proposal with no found quote is not shown in the queue; it's counted in the run summary instead.
- **Schema check.** Output that fails the schema is rejected and logged; no partial proposals.
- **Data, not instructions.** Source files go into the prompt inside delimited blocks with a fixed instruction that their content is data to analyse, never instructions to follow.
- **No write path.** The scan module imports only `proposals.create()`. It has no handle on `hazard`, `control` or `event` writes beyond its own run event.
- **No patient data.** v1 ingests docs and code only. The system record has a checkbox the CSO ticks to confirm sources contain no PID; the scan refuses to run until it's ticked.

**Model settings** (Python `anthropic` SDK):

- Model `claude-opus-5-5`, configurable per install. Structured outputs (`output_config.format` with the JSON schema). Effort `high` set explicitly (the model's default is `medium`). Streaming, since inputs and outputs are long.
- Server-side fallback on refusal enabled (`fallbacks: "default"`). Because the answering model can then differ, store **`response.model` as `model_used`**, not the requested model.
- The API no longer takes `temperature`, so runs are not bit-for-bit repeatable. Reproducibility means: store `prompt_version`, `model_used`, and a SHA-256 of the exact inputs on every proposal, and keep a small regression set of known hazards (§10, OHL-H6) to re-run when either changes.
- Prompts live in `prompts/scan_v1.md`. Changing a prompt means a new file and a new version string, never an edit in place.

---

## 6. The checks (no AI)

`ohl check` runs all three. Run it by cron, CI, or the "Run checks" button. Each finding becomes a proposal so the CSO decides what it means.

| Check | How | Becomes |
|---|---|---|
| Evidence integrity | For each evidence item with a file path: re-hash it. Hash differs → `changed`; file gone → `missing`. | `evidence_problem` proposal on that control |
| Linked code changed | `git diff --name-only <last_checked_sha>..HEAD` against every cause's `paths[]`. | `re_review` proposal on that hazard, listing commits and files |
| Review overdue | `review_due < today` | Shown on the Log page; no proposal |

The Log page shows when the last scan and last check ran. If checks haven't run for more than 7 days (configurable), the freshness line turns red. This is the control for "the log looks current but isn't" (OHL-H4).

URL evidence (e.g. a training record in another system) is not fetched in v1; it carries a "last confirmed by" date the CSO updates by hand.

---

## 7. CSO review

| Action | Rule |
|---|---|
| **Approve** | For new hazards and any rating change: enabled only after the CSO enters their own severity and likelihood. The agent's suggestion is then shown next to theirs. |
| **Edit** | Opens the payload as a form; the CSO's edits are stored as a diff in the event. |
| **Reject** | Reason required: picklist (duplicate · not a hazard · out of scope · wrong context · already controlled · other) plus free text. |
| **Ask someone** | Assigns to a named user with a question; stays pending. |
| **Bulk approve** | Only for `evidence_problem` items where the CSO has re-confirmed the evidence. Never for new hazards or ratings. |
| **Issue version** | CSO only, from the Log page. Shows what changed since the last issued version and how many proposals are still pending, then asks for a note. Saves a numbered snapshot with the CSO's name and registration, the time, the repo commit and the audit chain head. This is the DCB0129 3.3.2 approval. |

Reasoning, assumptions and citations are shown open on the same screen, not behind a click. Time on each proposal is recorded for later metrics.

**Roles:** `cso` (everything) · `contributor` (add evidence, answer questions, run scans/checks) · `viewer` (read and export). Local accounts with hashed passwords in v1. A deployment may have more than one CSO, but every approval names the individual.

---

## 8. Screens

Three pages. Nothing else.

| Page | Shows |
|---|---|
| **Log** | System name; freshness line (working draft vs last issued version, last scan, last checks, proposals waiting, audit chain intact); one table: ID, hazard, risk (initial → residual), controls/evidence state, review due. Buttons: Add hazard, Export, Issue version. |
| **Review** | Pending proposals as a list; selected one opens below as two columns: proposal (as a diff) + rating + actions on the left, reasoning, assumptions and quoted sources on the right. |
| **Hazard** | All fields, controls with each evidence item's state and last check, and the event history. Edit button (CSO only). |

Settings (system details, sources, users, risk matrix) is a plain form page linked from the user menu.

**Reference mockup:** [design/mockup.html](design/mockup.html). The build copies its CSS.

**Style** — black and white, typewriter. Like a typed safety file.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#FFFFFF` | `#000000` | page |
| `--ink` | `#000000` | `#FFFFFF` | text, rules, borders, primary button |
| `--faint` | `#595959` | `#A6A6A6` | secondary text only (7:1 contrast) |

- **No other colours.** Dark mode just swaps paper and ink.
- **One typeface:** Courier Prime (SIL Open Font License), bundled locally because some NHS networks block Google Fonts. Falls back to Courier New, then the system monospace.
- **Headings:** bold. Small section headings are uppercase with letter-spacing.
- **Lines:** square corners, no shadows. 2px solid rules under headers, 1px boxes, dashed rules between rows.
- **Emphasis without colour:**
  - **Risk** is shown as number, blocks and word, e.g. `4 → 2 ■■□□□ acceptable where impractical`. In the log it's one column, initial → **residual**.
  - **Alerts** (evidence missing) are inverted: white text on black.
  - **The selected item and the primary button** are inverted.
  - **The active tab** is underlined.
  - **Proposed text** is prefixed `+`.
  - **Status labels** sit in brackets: `[verified]`, `[software]`, `[quote found]`.
- **Restraint:** one primary button per screen. No charts, no animation, no icons beyond `↳` and `→`.
- Works at phone width (tables scroll sideways inside their box).

---

## 9. Exports

| Export | Format | Notes |
|---|---|---|
| Hazard log | CSV, XLSX, JSON | The working draft or any issued version. Columns follow template v3.6 (§3.3). The header states system, version, the risk scheme used, export time, and the CSO's name and registration. A working-draft export is stamped "Draft, not issued". |
| Deployer actions | CSV, JSON | Hazards with a `deployer_action`, for the deploying organisation's DCB0160 log. |
| Audit trail | JSON | All events plus the chain head hash; `ohl verify` checks a file offline. |

---

## 10. OHL's own hazard log

OHL is safety-relevant software, so it ships with its own log as the first system in the app. The v1 controls below are the ones this spec actually builds.

| ID | Hazard | v1 controls |
|---|---|---|
| OHL-H1 | Agent misses a significant hazard; CSO treats the scan as complete. | Scan is labelled "supplements, never replaces, a hazard workshop"; run summary lists which lenses ran. |
| OHL-H2 | CSO approves a wrong proposal (automation bias). | Reasoning and sources always open; own rating required before approve; no bulk approve for hazards. |
| OHL-H3 | Agent's rating anchors the CSO. | Blind rating (§7). |
| OHL-H4 | Checks stop running; log looks current. | Freshness line with last-run times; turns red after 7 days. |
| OHL-H5 | Evidence reported valid after it changed. | SHA-256 re-hash on every check. |
| OHL-H6 | Model or prompt change alters behaviour unnoticed. | Versioned prompts; `model_used` stored; regression set of 10 known hazards re-run on change. |
| OHL-H7 | Prompt injection via ingested text. | Delimited data blocks; schema check; quote check; no write path to the log. |
| OHL-H8 | Deployer never actions a transferred hazard. | v1: deployer-actions export only. Acknowledgement workflow is v2. |

---

## 11. Stack and layout

One language, one process, one database file.

- **Python 3.12**, **FastAPI** + **Jinja2** server-rendered pages (plain HTML forms, no JS build), **SQLite** (stdlib `sqlite3`), **anthropic** SDK, **openpyxl** for XLSX.
- Run: `uv run ohl serve` (web), `ohl scan`, `ohl check`, `ohl export`, `ohl verify`, `ohl import`.

```
ohl/
  app.py         routes for the three pages + settings
  db.py          schema (append-only triggers), transactions
  audit.py       event writes, hash chain, verify, replay
  risk.py        matrix lookup from config
  service.py     every change to the log, with its event, in one transaction
  cli.py         command line
  scan.py        agent: build inputs, call model, validate, quote-check
  checks.py      evidence hashes, git diff, overdue
  export.py      CSV / XLSX / JSON
  importer.py    CSV / XLSX import
  templates/     log.html, review.html, hazard.html, settings.html, base.html
  static/ohl.css (from design/mockup.html)
prompts/scan_v1.md
config/risk_matrix.toml
tests/
```

---

## 12. Build order

| Step | Builds | Done when |
|---|---|---|
| **M1 Log** ✓ built 16 Nov 2026 | Tables, audit chain, risk matrix config, Log + Hazard pages, manual add/edit, import, export, Issue version. | An existing hazard log can be imported, edited, issued as v1 and exported in template v3.6 columns; `ohl verify` passes; tampering with one event row makes it fail. |
| **M2 Scan + Review** | Agent, quote check, Review page, blind rating. | A scan of a real repo yields ≤15 proposals, every shown quote is verified, and approve/edit/reject each leave the right event. |
| **M3 Checks** | Evidence hashing, git-diff re-review, overdue, freshness line. | Deleting a linked test file and changing a linked source file each produce the right proposal on the next `ohl check`. |
| **M4 Pilot** | Run on one real product alongside its human-built log. | Approval/edit/reject rates and CSO minutes per proposal recorded; hazards found only by the agent listed. |

---

## 13. Decisions needed before building

1. **Stack:** Python as above? (Go was in the original spec; Python is simpler with one language and the Anthropic SDK.)
2. **Blind rating on by default?** Recommended yes (answers open question 3 of the original spec).
3. **Pilot product:** Petrios or the ENABLE-AI reporting tool?
4. **Where the pilot runs:** a single laptop/VM is enough for v1.
5. **Licence** (Apache-2.0 vs AGPL) and **name** — don't block v1.
6. **Revised DCB0129/0160:** build against the 2018 versions now, with the config seams in §3.1. Re-check when NHS England publishes the consultation response.

---

## 14. Sources

- DCB0129 standard page — https://digital.nhs.uk/data-and-information/information-standards/governance/latest-activity/standards-and-collections/dcb0129-clinical-risk-management-its-application-in-the-manufacture-of-health-it-systems
- DCB0129 specification v4.2 — https://digital.nhs.uk/binaries/content/assets/website-assets/data-and-information/information-standards/standards-and-collections/dcb0129-clinical-risk-management-its-application-in-the-manufacture-of-health-it-systems/0129242018spec.pdf
- DCB0129 implementation guidance v3.2 (Tables 5, 7–10; §6.1) — https://digital.nhs.uk/binaries/content/assets/website-assets/data-and-information/information-standards/standards-and-collections/dcb0129-clinical-risk-management-its-application-in-the-manufacture-of-health-it-systems/0129242018impguid.pdf
- DCB0160 standard page — https://digital.nhs.uk/data-and-information/information-standards/governance/latest-activity/standards-and-collections/dcb0160-clinical-risk-management-its-application-in-the-deployment-and-use-of-health-it-systems
- DCB0160 specification v3.2 — https://digital.nhs.uk/binaries/content/assets/website-assets/data-and-information/information-standards/standards-and-collections/dcb0160/0160252018spec.pdf
- DCB0160 implementation guidance v4.2 — https://digital.nhs.uk/binaries/content/assets/website-assets/data-and-information/information-standards/standards-and-collections/dcb0160/0160252018impguid.pdf
- Review of DCB0129 and DCB0160 — https://digital.nhs.uk/data-and-information/information-standards/governance/latest-activity/standards-and-collections/review-of-digital-clinical-safety-standards-dcb0129-and-dcb0160
- NHS England review: supporting information — https://www.england.nhs.uk/long-read/national-review-of-clinical-risk-management-standardsdcb0129-and-dcb0160-supporting-information/
- Clinical safety templates (CRMS, CRM Plan, Safety Case Report, Hazard Log) — https://digital.nhs.uk/services/clinical-safety/documentation
- Hazard log template v3.6 — https://digital.nhs.uk/binaries/content/assets/website-assets/services/clinical-safety/clinical_safety_hazard_log_template.xlsx
- Applicability of DCB0129/0160 — https://digital.nhs.uk/services/clinical-safety/applicability-of-dcb-0129-and-dcb-0160/step-by-step-guidance
- DTAC — https://digital.nhs.uk/services/digital-technology-assessment-criteria-dtac
- Health and Social Care Act 2012 s250 — https://www.legislation.gov.uk/ukpga/2012/7/section/250
