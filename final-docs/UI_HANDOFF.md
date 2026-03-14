# PRIYA UI Team Handoff

> Comprehensive Frontend Specification
> React + WebSocket + Canvas Architecture
> Pine Labs AI Hackathon | v1.0

---

## Table of Contents

1. [Product Overview](#1-product-overview)
2. [Tech Stack](#2-tech-stack)
3. [Layout Architecture](#3-layout-architecture)
4. [Backend Connection](#4-backend-connection)
5. [WebSocket Events — Complete Reference](#5-websocket-events)
6. [REST API Endpoints](#6-rest-api-endpoints)
7. [Component Breakdown](#7-component-breakdown)
8. [Canvas States — Five Views](#8-canvas-states)
9. [Top Bar — Pipeline Progress Strip](#9-top-bar)
10. [Right Panel — Chat + Logs](#10-right-panel)
11. [User Interactions & Flows](#11-user-interactions)
12. [Data Models (TypeScript)](#12-data-models)
13. [State Management](#13-state-management)
14. [Demo Walkthrough — What Judges See](#14-demo-walkthrough)
15. [Design Tokens & Styling](#15-design-tokens)
16. [File Structure](#16-file-structure)

---

## 1. Product Overview

PRIYA is an AI agent that autonomously pays vendor invoices through Pine Labs. The UI is a **real-time operational dashboard** that shows the agent working — not a form-based app.

**The UI does three things:**
1. **Shows the agent thinking** — chat panel streams reasoning in plain English
2. **Shows the agent working** — canvas animates through pipeline stages live
3. **Lets humans intervene** — policy gate approval, Schedule H authorization

**Two personas, same UI:**
- **Hospital** — Meera, procurement head. 6 suppliers, Schedule H drugs, compliance flags.
- **Kirana** — Ramesh, shop owner. 6 suppliers, credit float optimization, dairy priority.

---

## 2. Tech Stack

| Layer | Recommendation | Notes |
|-------|---------------|-------|
| Framework | React 18+ | Hooks, functional components |
| Build | Vite | Fast dev server |
| State | Zustand or React Context | Lightweight, sufficient for event-driven updates |
| Styling | Tailwind CSS | Rapid UI development |
| WebSocket | Native WebSocket API | No library needed |
| Charts | Recharts or Chart.js | For bar_chart render in query results |
| Icons | Lucide React | Clean, consistent |
| Animations | Framer Motion | For canvas transitions and status dots |

---

## 3. Layout Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  TOP BAR (fixed, 64px height)                                        │
│  PRIYA logo | Persona badge | RUN ID | Pipeline strip | Agent status │
├──────────────────────────────────────┬───────────────────────────────┤
│                                      │                               │
│  LEFT CANVAS (65% width)             │  RIGHT PANEL (35% width)      │
│  Height: calc(100vh - 64px)          │  Height: calc(100vh - 64px)   │
│                                      │                               │
│  Renders ONE of 5 canvas states:     │  ┌───────────────────────┐   │
│                                      │  │ CHAT (70% height)     │   │
│  1. Workflow diagram                 │  │ - NLP input at top    │   │
│  2. Policy gate overlay              │  │ - Agent messages      │   │
│  3. Payment run board                │  │ - Timestamped         │   │
│  4. Audit dashboard                  │  │ - Color by level      │   │
│  5. Query result view                │  ├───────────────────────┤   │
│                                      │  │ LOGS (30%, collapsed) │   │
│  Transitions driven by               │  │ - Raw WS events       │   │
│  CANVAS_STATE events                 │  │ - Expandable          │   │
│                                      │  └───────────────────────┘   │
└──────────────────────────────────────┴───────────────────────────────┘
```

### Responsive Behavior

- **Desktop (>1280px)**: 65/35 split as above
- **Tablet (768-1280px)**: Stack vertically — canvas on top, chat below
- **Mobile**: Not required for hackathon demo

---

## 4. Backend Connection

### Base URL

```
REST:      http://localhost:8000
WebSocket: ws://localhost:8000/ws/{run_id}
```

### WebSocket Connection

```typescript
// Connect when a run starts
const ws = new WebSocket(`ws://localhost:8000/ws/${runId}`);

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // data = { type: string, payload: object, run_id: string, timestamp: string }

  switch (data.type) {
    case "CANVAS_STATE":
      setCanvasState(data.payload.state);
      break;
    case "AGENT_NARRATION":
      addChatMessage(data.payload);
      break;
    case "PIPELINE_STEP":
      updatePipelineStrip(data.payload);
      break;
    case "VENDOR_STATE":
      updateVendorRow(data.payload);
      break;
    case "POLICY_GATE":
      showPolicyGate(data.payload);
      break;
    case "ESCALATION":
      showEscalation(data.payload);
      break;
    case "RAIL_SWITCH":
      showRailSwitch(data.payload);
      break;
    case "RUN_SUMMARY":
      showRunSummary(data.payload);
      break;
    case "QUERY_RESULT":
      showQueryResult(data.payload);
      break;
  }
};
```

### CORS

Backend allows origins `http://localhost:3000` and `http://localhost:5173`.

---

## 5. WebSocket Events — Complete Reference

### 5.1 CANVAS_STATE

**Purpose:** Controls which view renders in the left canvas panel.

```typescript
{
  type: "CANVAS_STATE",
  payload: {
    state: "workflow" | "policy_gate" | "run_board" | "audit" | "query_result"
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:23:00.000Z"
}
```

**UI Action:** Switch the canvas component. Use animated transition (fade or slide).

### 5.2 AGENT_NARRATION

**Purpose:** Agent's reasoning, streamed to chat panel.

```typescript
{
  type: "AGENT_NARRATION",
  payload: {
    text: "Prioritizing Insulin Depot — critical supply, 2 days overdue.",
    level: "info" | "warn" | "error"
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:23:15.000Z"
}
```

**UI Action:** Append to chat messages list. Color by level:
- `info` → default text color
- `warn` → amber/yellow, prefix with warning icon
- `error` → red, prefix with error icon

### 5.3 PIPELINE_STEP

**Purpose:** Updates the top bar progress strip.

```typescript
{
  type: "PIPELINE_STEP",
  payload: {
    stage: "order" | "pay" | "settle" | "recon",
    vendor_id: "vnd_insulin_depot_001",
    status: "CREATED" | "PENDING" | "PROCESSED" | "FAILED" | "CANCELLED" | "AUTHORIZED"
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:24:00.000Z"
}
```

**UI Action:** Update the corresponding stage segment dot for this vendor. Map status to dot state:
- `CREATED`, `PENDING` → in-progress (half-filled / pulsing)
- `PROCESSED` → done (filled green)
- `FAILED` → failed (red X)
- `CANCELLED` → cancelled (grey X)
- `AUTHORIZED` → waiting (yellow, pulsing)

### 5.4 VENDOR_STATE

**Purpose:** Updates a vendor row in the run board.

```typescript
{
  type: "VENDOR_STATE",
  payload: {
    vendor_id: "vnd_insulin_depot_001",
    name: "Insulin Depot",
    state: "CREATED" | "PENDING" | "PROCESSED" | "FAILED" | "AUTHORIZED" | "CANCELLED",
    rail: "neft" | "upi" | "upi_intent" | "hosted_checkout" | "payment_link",
    amount: 45000,           // in rupees
    pine_order_id: "v1-4405071524-aa-qlAtAf",
    attempt_number: 1
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:24:30.000Z"
}
```

**UI Action:** Update or insert vendor row in run board. Animate state transitions. Show rail icon.

### 5.5 POLICY_GATE

**Purpose:** Renders the approval overlay with vendor details.

```typescript
{
  type: "POLICY_GATE",
  payload: {
    vendors: [
      {
        vendor_id: "vnd_insulin_depot_001",
        name: "Insulin Depot",
        amount: 45000,
        rail: "neft",
        priority_reason: "Critical supply, 2 days overdue",
        action: "pay",
        priority_score: 70
      },
      {
        vendor_id: "vnd_medchem_pharma_002",
        name: "MedChem Pharma",
        amount: 32000,
        rail: "neft",
        priority_reason: "Schedule H drug — escalated",
        action: "escalate",
        priority_score: 60
      }
      // ... all vendors
    ],
    total: 144000
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:23:45.000Z"
}
```

**UI Action:** Show policy gate overlay on canvas. List all vendors with amounts, rails, actions. Show total. Show single **"Approve Run"** button.

### 5.6 ESCALATION

**Purpose:** Flags a vendor for human decision (Schedule H).

```typescript
{
  type: "ESCALATION",
  payload: {
    vendor_id: "vnd_medchem_pharma_002",
    name: "MedChem Pharma",
    flag_type: "schedule_h",
    action_required: true,
    details: "Schedule H drug detected. Pre-authorized. Awaiting capture/cancel decision."
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:25:00.000Z"
}
```

**UI Action:** In the run board, the vendor row should:
- Pulse red / show red border
- Show two action buttons: **"Authorize (Capture)"** and **"Reject (Cancel)"**
- Other vendor rows continue processing normally

### 5.7 RAIL_SWITCH

**Purpose:** Shows a payment rail change after failure.

```typescript
{
  type: "RAIL_SWITCH",
  payload: {
    vendor_id: "vnd_surgical_supplies_003",
    name: "Surgical Supplies Co",
    from_rail: "neft",
    to_rail: "upi_intent",
    reason: "NEFT payment failed — bank node down",
    attempt_number: 2
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:26:00.000Z"
}
```

**UI Action:** In vendor row, show rail transition animation: `NEFT` ~~→~~ `UPI Intent`. Update rail icon. Brief highlight animation.

### 5.8 RUN_SUMMARY

**Purpose:** Final run metrics after completion.

```typescript
{
  type: "RUN_SUMMARY",
  payload: {
    paid: 4,              // vendors successfully paid
    deferred: 1,          // vendors deferred (credit float)
    failed: 0,            // vendors where all attempts failed
    float_saved: 4200,    // rupees saved via credit deferral (kirana)
    total: 144000,        // total invoice amount
    vendors_processed: 6
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:29:00.000Z"
}
```

**UI Action:** Display summary card. In kirana persona, prominently show `float_saved`.

### 5.9 QUERY_RESULT

**Purpose:** Result of a natural language query.

```typescript
{
  type: "QUERY_RESULT",
  payload: {
    query_nl: "Which vendors weren't paid last run?",
    sql: "SELECT v.name, o.amount, o.action ... WHERE ...",
    columns: ["name", "amount", "action", "defer_reason"],
    rows: [
      ["Sharma Wholesale", 28000, "defer", "8 credit days remaining"],
      ["Cleaning Supplies Depot", 4800, "defer", "5 credit days remaining"]
    ],
    row_count: 2,
    render_as: "table" | "bar_chart" | "summary_card"
  },
  run_id: "run_20250114_001",
  timestamp: "2025-01-14T10:30:00.000Z"
}
```

**UI Action:** Switch canvas to query_result. Render based on `render_as`:
- `table` → data table with columns/rows
- `bar_chart` → bar chart (x = first column, y = second column)
- `summary_card` → large number with label (for single-value results like "Total fees: Rs 847")

Show SQL in a collapsed/expandable panel below the result.

---

## 6. REST API Endpoints

### 6.1 Start a Run

```
POST /run
Content-Type: multipart/form-data

Body:
  persona: "hospital" | "kirana"       (form field)
  instruction: "Clear this week invoices"  (form field)
  csv_file: <file>                       (file upload)

Response 200:
{
  "run_id": "run_20250114_001",
  "status": "started"
}
```

After receiving `run_id`, immediately connect WebSocket: `ws://localhost:8000/ws/run_20250114_001`

### 6.2 Approve Policy Gate

```
POST /approve/{run_id}

Response 200:
{
  "approved": true,
  "approved_at": "2025-01-14T10:25:00.000Z"
}
```

Called when user clicks **"Approve Run"** button on policy gate overlay.

### 6.3 Escalation Decision

```
POST /escalation/{run_id}/{vendor_id}
Content-Type: application/json

Body:
{
  "decision": "capture" | "cancel"
}

Response 200:
{
  "ok": true
}
```

Called when user clicks **"Authorize"** (capture) or **"Reject"** (cancel) on an escalated vendor row.

### 6.4 Natural Language Query

```
POST /query/{run_id}
Content-Type: application/json

Body:
{
  "question": "Which vendors weren't paid last run?"
}

Response 200:
{
  "query_id": "q_abc123"
}
```

Result arrives via WebSocket as `QUERY_RESULT` event. The response just confirms submission.

### 6.5 List Runs

```
GET /runs

Response 200:
[
  {
    "id": "run_20250114_001",
    "persona": "hospital",
    "status": "complete",
    "instruction": "Clear this week invoices",
    "total_vendors": 6,
    "total_amount": 144000,
    "paid_amount": 115500,
    "started_at": "2025-01-14T10:23:00.000Z"
  }
]
```

### 6.6 Get Run Detail

```
GET /run/{run_id}

Response 200:
{
  "run": { ... },
  "orders": [ ... ],
  "payments": [ ... ],
  "settlements": [ ... ],
  "reconciliations": [ ... ]
}
```

### 6.7 Export Audit CSV

```
GET /run/{run_id}/export

Response: CSV file download (Content-Disposition: attachment)

Columns: vendor_name, invoice_amount, paid_amount, settled_amount, variance,
         mdr_rate_actual, mdr_rate_contracted, rail_used, retries, outcome,
         agent_reasoning, ca_notes, pine_order_id, utr_number
```

### 6.8 Upload CSV

```
POST /upload-csv
Content-Type: multipart/form-data

Body:
  file: <csv_file>

Response 200:
{
  "file_path": "/tmp/uploads/hospital_invoices.csv",
  "vendor_count": 6
}
```

---

## 7. Component Breakdown

### Component Tree

```
<App>
  <TopBar>
    <Logo />
    <PersonaBadge persona="hospital" />
    <RunIdBadge runId="run_20250114_001" />
    <PipelineStrip>
      <StageSegment stage="order" vendors={vendors} />
      <StageSegment stage="pay" vendors={vendors} />
      <StageSegment stage="settle" vendors={vendors} />
    </PipelineStrip>
    <AgentStatusPill status="running" />
  </TopBar>

  <MainLayout>
    <LeftCanvas canvasState={canvasState}>
      {canvasState === "workflow"     && <WorkflowDiagram />}
      {canvasState === "policy_gate"  && <PolicyGate />}
      {canvasState === "run_board"    && <RunBoard />}
      {canvasState === "audit"        && <AuditDashboard />}
      {canvasState === "query_result" && <QueryResultView />}
    </LeftCanvas>

    <RightPanel>
      <ChatPanel>
        <ChatInput onSubmit={sendQuery} />
        <ChatMessages messages={messages} />
      </ChatPanel>
      <LogsDrawer expanded={logsExpanded}>
        <RawEventLog events={rawEvents} />
      </LogsDrawer>
    </RightPanel>
  </MainLayout>
</App>
```

---

## 8. Canvas States — Five Views

### 8.1 Workflow Diagram

**Triggered by:** `CANVAS_STATE { state: "workflow" }` — sent when NLP instruction is received.

**What it shows:** A flow graph of the PRIYA pipeline:

```
  [LOAD] → [SCORE] → [APPROVE] → [ORDER] → [PAY] → [SETTLE] → [RECON]
```

- Each node is a rounded rectangle
- Nodes light up (change color) as the agent executes that step
- Current node pulses/glows
- Failed nodes turn red
- Arrows connect the nodes with animated flow

**Data needed:** Track which step the agent is currently on via `PIPELINE_STEP` events.

### 8.2 Policy Gate

**Triggered by:** `CANVAS_STATE { state: "policy_gate" }` + `POLICY_GATE` event.

**What it shows:** Structured approval card.

```
┌─────────────────────────────────────────────────────────┐
│                    PAYMENT APPROVAL                      │
│                                                          │
│  Vendor               Amount      Rail     Action        │
│  ─────────────────────────────────────────────────────   │
│  Insulin Depot        Rs 45,000   NEFT     PAY    ✓     │
│  MedChem Pharma       Rs 32,000   NEFT     ESCALATE ⚠   │
│  Surgical Supplies    Rs 28,000   NEFT     PAY    ✓     │
│  Bandage World        Rs  8,500   NEFT     PAY    ✓     │
│  Spice House          Rs 12,000   UPI      PAY    ✓     │
│  Lab Reagents         Rs 18,500   NEFT     PAY    ✓     │
│  ─────────────────────────────────────────────────────   │
│  TOTAL                Rs 1,44,000                        │
│                                                          │
│  Priority Reasoning:                                     │
│  • Insulin Depot: Critical supply, 2 days overdue (70)   │
│  • MedChem Pharma: Schedule H — requires authorization   │
│  • Lab Reagents: Critical supply, 5 days overdue (80)    │
│                                                          │
│                    [ APPROVE RUN ]                        │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

- Rows with `action: "escalate"` → amber row + warning icon
- Rows with `action: "defer"` → greyed out + "DEFERRED" tag
- Single **"APPROVE RUN"** button at bottom
- On click: `POST /approve/{run_id}` → canvas transitions to run_board

### 8.3 Payment Run Board

**Triggered by:** `CANVAS_STATE { state: "run_board" }` — after approval.

**What it shows:** Live table of vendor payment progress.

```
┌───────────────────────────────────────────────────────────────────┐
│  PAYMENT RUN BOARD                                                │
│                                                                    │
│  Vendor               Amount     Rail          Status    Actions   │
│  ──────────────────────────────────────────────────────────────── │
│  ● Lab Reagents       Rs 18,500  NEFT          PROCESSED  ✓      │
│  ● Insulin Depot      Rs 45,000  NEFT          PROCESSED  ✓      │
│  ◐ Surgical Supplies  Rs 28,000  NEFT→UPI INT  RETRY #2   ↻      │
│  ⚠ MedChem Pharma     Rs 32,000  NEFT          AUTHORIZED [Auth] │
│  ◐ Spice House        Rs 12,000  UPI           PENDING    ...     │
│  ● Bandage World      Rs  8,500  NEFT          PROCESSED  ✓      │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘
```

**Row behavior per status:**
| Status | Dot | Color | Animation |
|--------|-----|-------|-----------|
| CREATED | ○ | Grey | None |
| PENDING | ◐ | Blue | Pulse |
| AUTHORIZED | ◐ | Yellow | Pulse |
| PROCESSED | ● | Green | Brief flash then static |
| FAILED | ✕ | Red | Brief shake |
| CANCELLED | ✕ | Grey | None |

**Escalated row (Schedule H):**
- Entire row has red/amber left border
- Pulses gently
- Shows two buttons: **[Authorize]** and **[Reject]**
- On Authorize click: `POST /escalation/{run_id}/{vendor_id}` with `{ decision: "capture" }`
- On Reject click: `POST /escalation/{run_id}/{vendor_id}` with `{ decision: "cancel" }`

**Rail switch animation:**
- When `RAIL_SWITCH` event arrives, show strikethrough on old rail → new rail with arrow
- e.g., `~~NEFT~~ → UPI Intent`

### 8.4 Audit Dashboard

**Triggered by:** `CANVAS_STATE { state: "audit" }` — after reconciliation completes.

**What it shows:**

```
┌──────────────────────────────────────────────────────────────────┐
│  AUDIT & RECONCILIATION                          [ CA EXPORT ]   │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐    │
│  │  PAID    │  │ DEFERRED │  │  FAILED  │  │ FLOAT SAVED  │    │
│  │    4     │  │    1     │  │    0     │  │   Rs 4,200   │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────┘    │
│                                                                   │
│  RECONCILIATION TABLE                                            │
│  ─────────────────────────────────────────────────────────────── │
│  Vendor          Invoice    Settled   Variance  MDR%    UTR      │
│  Insulin Depot   45,000     44,190    810       1.80%   UTR001   │
│  Surgical Sup.   28,000     27,496    504       1.80%   UTR001   │
│  Spice House     12,000     11,784    216       1.80%   UTR001   │
│  Bandage World    8,500      8,347    153       1.80%   UTR001   │
│  ─────────────────────────────────────────────────────────────── │
│  MedChem Pharma  32,000         —       —         —     ESCLTD   │
│                                                                   │
│  MDR DRIFT: None flagged ✓                                       │
│  REFUND DEBITS: Rs 0                                             │
│  PLATFORM FEES: Rs 1,683                                         │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

**Summary cards at top:**
- `paid` — green card
- `deferred` — blue card
- `failed` — red card (0 = grey)
- `float_saved` — purple card (kirana highlight, hide if 0 for hospital)

**Reconciliation table:**
- Sortable columns
- Variance column: red if positive (we lost money), green if zero
- MDR% column: red if different from contracted rate (mdr_drift_flagged)
- Escalated/deferred vendors shown with status tag instead of settlement data

**CA Export button:**
- Top right corner
- Calls `GET /run/{run_id}/export`
- Downloads CSV file

### 8.5 Query Result View

**Triggered by:** `CANVAS_STATE { state: "query_result" }` + `QUERY_RESULT` event.

**Renders based on `render_as`:**

**Table:**
```
┌────────────────────────────────────────────────────────┐
│  "Which vendors weren't paid last run?"                │
│                                                         │
│  name                 amount    action    defer_reason   │
│  ──────────────────────────────────────────────────────│
│  Sharma Wholesale     28,000    defer     8 credit days │
│  Cleaning Depot        4,800    defer     5 credit days │
│                                                         │
│  ▼ SQL Query                                           │
│  SELECT v.name, o.amount, o.action, o.defer_reason ... │
└────────────────────────────────────────────────────────┘
```

**Bar Chart:**
```
┌────────────────────────────────────────────────────────┐
│  "Total fees paid per vendor"                          │
│                                                         │
│  Insulin Depot    ████████████████ Rs 810               │
│  Surgical Sup.    ████████████ Rs 504                   │
│  Spice House      █████ Rs 216                          │
│  Bandage World    ████ Rs 153                           │
│                                                         │
│  ▼ SQL Query                                           │
└────────────────────────────────────────────────────────┘
```

**Summary Card:**
```
┌────────────────────────────────────────────────────────┐
│  "How much float did kirana save last week?"           │
│                                                         │
│              ┌─────────────────┐                       │
│              │    Rs 4,200     │                       │
│              │   Float Saved   │                       │
│              │   across 1 run  │                       │
│              └─────────────────┘                       │
│                                                         │
│  ▼ SQL Query                                           │
└────────────────────────────────────────────────────────┘
```

SQL is always shown in a collapsed panel below, expandable on click.

---

## 9. Top Bar — Pipeline Progress Strip

Always visible. Fixed to top of page.

### Layout

```
[PRIYA logo] [HOSPITAL badge] | RUN: run_20250114_001 | ① ORDER [●●●○○○] → ② PAY [●●○○○○] → ③ SETTLE [○○○○○○] | Running...
```

### Components

#### 9.1 Persona Badge

| Persona | Color | Text |
|---------|-------|------|
| hospital | Blue background, white text | HOSPITAL |
| kirana | Green background, white text | KIRANA |

#### 9.2 Run ID Badge

- Monospace font
- Clickable (future: loads run history)
- Format: `run_20250114_001`

#### 9.3 Pipeline Strip

Three segments: **ORDER → PAY → SETTLE**

Each segment shows dots equal to vendor count. Dot states:

| Symbol | Meaning | Visual |
|--------|---------|--------|
| ● | Done | Filled green circle |
| ◐ | In progress | Half-filled blue, pulsing |
| ○ | Pending | Empty grey circle |
| ✕ | Failed | Red X |

Update dots based on `PIPELINE_STEP` events. Each vendor gets one dot per stage.

#### 9.4 Agent Status Pill

| Status | Color | When |
|--------|-------|------|
| Running... | Blue, pulsing | Agent processing |
| Awaiting approval | Amber | Policy gate waiting |
| Escalation | Red, pulsing | Schedule H waiting |
| Complete | Green | Run finished |
| Partial | Yellow | Run finished with failures |

Derive from combination of events:
- `POLICY_GATE` → "Awaiting approval"
- `ESCALATION` → "Escalation"
- `RUN_SUMMARY` → "Complete" or "Partial" (if failed > 0)

---

## 10. Right Panel — Chat + Logs

### 10.1 Chat Panel

**Input field** at top (not bottom — matches Claude UX):
- Placeholder: "Type instruction or ask a question..."
- On first use: sends instruction to `POST /run` (with CSV upload)
- During/after run: sends NL query to `POST /query/{run_id}`

**Message list** scrolls down (newest at bottom):

```
10:23  Loaded hospital persona. 6 invoices fetched.              [info]
10:23  Prioritizing: Insulin Depot (critical, 2 days overdue).   [info]
10:24  ⚠ MedChem Pharma — Schedule H. pre_auth=true. Escalating. [warn]
10:24  Policy gate ready. Awaiting your approval.                 [info]
10:25  ✓ Approved. Executing payment run.                        [info]
10:26  Insulin Depot — pine_ord_abc → PROCESSED ✓                [info]
10:26  ✕ Surgical Supplies — NEFT FAILED. Switching to UPI.      [error]
10:27  ✓ Surgical Supplies — UPI intent → PROCESSED              [info]
10:29  Settlement: UTR001 matched. MDR Rs 11 on Spice House.     [info]
10:29  Recon complete. 4/5 paid. 1 escalated. Audit ready.       [info]
```

**Message formatting:**
- Timestamp (HH:MM) in muted color
- Text in white/dark
- Level indicators:
  - `info` → no prefix, default color
  - `warn` → ⚠ prefix, amber text
  - `error` → ✕ prefix, red text
- Auto-scroll to bottom on new message
- Messages from `AGENT_NARRATION` events

### 10.2 Logs Drawer

- Below chat, collapsed by default
- Toggle button: "Show Logs ▼" / "Hide Logs ▲"
- Shows raw WebSocket events in JSON
- Monospace font, dim color
- Useful for demo: "Here's what's happening under the hood"
- Shows Pine Labs request_id for traceability

```
[10:24:30] PIPELINE_STEP { stage: "order", vendor_id: "vnd_insulin_depot_001", status: "CREATED" }
[10:24:31] VENDOR_STATE { vendor_id: "vnd_insulin_depot_001", state: "CREATED", rail: "neft", ... }
[10:24:35] PIPELINE_STEP { stage: "pay", vendor_id: "vnd_insulin_depot_001", status: "PROCESSED" }
```

---

## 11. User Interactions & Flows

### 11.1 Start a Run

```
1. User selects persona (toggle/dropdown: Hospital | Kirana)
2. User uploads CSV file (drag-drop or file picker)
3. User types instruction in chat input: "Clear this week invoices"
4. Frontend: POST /run { persona, instruction, csv_file }
5. Frontend receives run_id → connects WebSocket
6. Events start flowing → canvas animates
```

### 11.2 Approve Policy Gate

```
1. POLICY_GATE event arrives → canvas shows approval overlay
2. User reviews vendor list, amounts, rails, priority reasons
3. User clicks "Approve Run"
4. Frontend: POST /approve/{run_id}
5. Canvas transitions to run_board
```

### 11.3 Schedule H Escalation

```
1. ESCALATION event arrives → vendor row in run board pulses red
2. User sees "Schedule H drug — requires authorization" details
3. User clicks "Authorize" or "Reject"
4. Frontend: POST /escalation/{run_id}/{vendor_id} { decision: "capture" | "cancel" }
5. Row updates: authorized → green / cancelled → grey
```

### 11.4 NL Query

```
1. User types question in chat: "Which vendors weren't paid?"
2. Frontend: POST /query/{run_id} { question }
3. QUERY_RESULT event arrives via WebSocket
4. Canvas switches to query_result view
5. Table/chart/card renders based on render_as
```

### 11.5 CA Export

```
1. User clicks "CA Export" button on audit dashboard
2. Frontend: GET /run/{run_id}/export (returns CSV download)
3. Browser downloads file
```

### 11.6 Switch Persona

```
1. Run completes for hospital
2. User changes persona toggle to "Kirana"
3. User uploads kirana_invoices.csv
4. User types new instruction
5. New run starts — fresh run_id, fresh WebSocket connection
6. Top bar persona badge switches color
```

---

## 12. Data Models (TypeScript)

```typescript
// -- Personas --
type Persona = "hospital" | "kirana";

// -- Canvas States --
type CanvasState = "workflow" | "policy_gate" | "run_board" | "audit" | "query_result";

// -- Agent Status --
type AgentStatus = "idle" | "running" | "awaiting_approval" | "escalation" | "complete" | "partial";

// -- Vendor in Run Board --
interface VendorRow {
  vendor_id: string;
  name: string;
  amount: number;              // rupees
  rail: string;
  state: "CREATED" | "PENDING" | "AUTHORIZED" | "PROCESSED" | "FAILED" | "CANCELLED";
  pine_order_id?: string;
  attempt_number: number;
  escalation?: {
    flag_type: string;
    action_required: boolean;
    details: string;
  };
  rail_history?: Array<{
    from_rail: string;
    to_rail: string;
    reason: string;
  }>;
}

// -- Chat Message --
interface ChatMessage {
  timestamp: string;           // ISO 8601
  text: string;
  level: "info" | "warn" | "error";
}

// -- Pipeline Dot --
interface PipelineDot {
  vendor_id: string;
  stage: "order" | "pay" | "settle" | "recon";
  status: "pending" | "in_progress" | "done" | "failed";
}

// -- Policy Gate Data --
interface PolicyGateData {
  vendors: Array<{
    vendor_id: string;
    name: string;
    amount: number;
    rail: string;
    priority_reason: string;
    action: "pay" | "defer" | "escalate" | "queue";
    priority_score: number;
  }>;
  total: number;
}

// -- Query Result --
interface QueryResult {
  query_nl: string;
  sql: string;
  columns: string[];
  rows: any[][];
  row_count: number;
  render_as: "table" | "bar_chart" | "summary_card";
}

// -- Run Summary --
interface RunSummary {
  paid: number;
  deferred: number;
  failed: number;
  float_saved: number;
  total: number;
  vendors_processed: number;
}

// -- Reconciliation Row (for audit table) --
interface ReconRow {
  vendor_name: string;
  invoice_amount: number;
  paid_amount: number;
  settled_amount: number;
  variance: number;
  mdr_rate_actual: number;
  mdr_rate_contracted: number;
  mdr_drift_flagged: boolean;
  rail_used: string;
  retries: number;
  outcome: "paid" | "deferred" | "escalated" | "failed" | "queued";
  agent_reasoning: string;
  ca_notes: string;
  pine_order_id: string;
  utr_number: string;
}

// -- WebSocket Event (generic) --
interface WSEvent {
  type: string;
  payload: Record<string, any>;
  run_id: string;
  timestamp: string;
}

// -- Raw Log Entry --
interface LogEntry {
  timestamp: string;
  event: WSEvent;
}
```

---

## 13. State Management

### Recommended: Zustand Store

```typescript
interface PriyaStore {
  // Session
  runId: string | null;
  persona: Persona;
  agentStatus: AgentStatus;

  // Canvas
  canvasState: CanvasState;
  policyGateData: PolicyGateData | null;
  queryResult: QueryResult | null;

  // Run Board
  vendors: Map<string, VendorRow>;    // vendor_id → VendorRow

  // Pipeline Strip
  pipelineDots: PipelineDot[];

  // Chat
  chatMessages: ChatMessage[];

  // Logs
  rawEvents: LogEntry[];
  logsExpanded: boolean;

  // Audit
  runSummary: RunSummary | null;
  reconRows: ReconRow[];

  // Actions
  setRunId: (id: string) => void;
  setPersona: (p: Persona) => void;
  handleWSEvent: (event: WSEvent) => void;  // Central event dispatcher
  reset: () => void;                         // Clear for new run
}
```

### `handleWSEvent` Dispatcher

```typescript
handleWSEvent: (event: WSEvent) => {
  // Always log raw event
  addRawEvent(event);

  switch (event.type) {
    case "CANVAS_STATE":
      set({ canvasState: event.payload.state });
      break;

    case "AGENT_NARRATION":
      addChatMessage({
        timestamp: event.timestamp,
        text: event.payload.text,
        level: event.payload.level,
      });
      break;

    case "PIPELINE_STEP":
      updatePipelineDot(event.payload);
      break;

    case "VENDOR_STATE":
      upsertVendor(event.payload);
      break;

    case "POLICY_GATE":
      set({
        policyGateData: event.payload,
        agentStatus: "awaiting_approval",
      });
      break;

    case "ESCALATION":
      markVendorEscalated(event.payload);
      set({ agentStatus: "escalation" });
      break;

    case "RAIL_SWITCH":
      updateVendorRail(event.payload);
      break;

    case "RUN_SUMMARY":
      set({
        runSummary: event.payload,
        agentStatus: event.payload.failed > 0 ? "partial" : "complete",
      });
      break;

    case "QUERY_RESULT":
      set({ queryResult: event.payload });
      break;
  }
}
```

---

## 14. Demo Walkthrough — What Judges See

Total time: ~8 minutes. Three star moments highlighted.

### Minute 0:00 — Opening

Screen shows PRIYA with empty state. Chat input visible. Persona selector set to "Hospital".

### Minute 0:30 — Setup

- Upload `hospital_invoices.csv` (drag & drop)
- Type: "Clear this week's hospital invoices"
- Hit enter
- Top bar: Run ID appears. Pipeline strip initializes. Status: "Running..."
- Canvas: Workflow diagram lights up at LOAD node

### Minute 1:30 — Prioritization

- Chat panel streams: "Loaded hospital persona. 6 invoices fetched."
- Chat: "Prioritizing Insulin Depot — critical supply, 2 days overdue"
- Workflow diagram: SCORE node lights up
- Chat: "⚠ MedChem Pharma — Schedule H. pre_auth=true. Escalating."

### Minute 2:00 — Policy Gate

- Canvas transitions to Policy Gate overlay
- Shows all 6 vendors with amounts, rails, priority scores
- MedChem row highlighted amber with "ESCALATE" tag
- Status pill: "Awaiting approval"
- **Meera clicks "Approve Run"**
- Canvas immediately transitions to Run Board

### Minute 2:30 — Schedule H Escalation (STAR MOMENT 1)

- Run Board: Vendors processing top to bottom
- MedChem Pharma row pulses red
- Chat: "⚠ Schedule H drug. pre_auth=true. Auto-execution paused."
- Two buttons visible on row: [Authorize] [Reject]
- **PAUSE 5 seconds** — let judges absorb
- Other vendors continue processing around it

### Minute 3:00 — Rail Switch (STAR MOMENT 2)

- Surgical Supplies row: status dot goes red (✕)
- Chat: "✕ Surgical Supplies — NEFT FAILED. Bank node down."
- `RAIL_SWITCH` event: row shows `~~NEFT~~ → UPI Intent`
- Chat: "Switching to UPI intent — retry attempt 2."
- Status dot: ✕ → ◐ → ● (failed → retrying → success)
- Pipeline strip PAY segment: dot transitions

### Minute 3:30 — Audit Canvas

- Run completes. Canvas → Audit Dashboard
- Summary cards: 4 Paid, 0 Deferred, 0 Failed
- Reconciliation table: UTR matched, MDR amounts shown
- Platform fee Rs 1,683 shown
- Chat: "Recon complete — audit trail ready."
- CA Export button visible

### Minute 4:00 — Switch to Kirana

- Change persona toggle to "Kirana"
- Upload `kirana_invoices.csv`
- Type: "Pay my suppliers for this week"
- New run starts — persona badge turns green

### Minute 5:00 — Credit Float (STAR MOMENT 3)

- Policy Gate shows Sharma Wholesale as "DEFERRED"
- Chat: "Sharma has 8 credit days remaining. Deferring saves Rs 4,200 in float."
- After approval + run: Audit dashboard shows `float_saved: Rs 4,200` prominently
- Purple summary card for float savings

### Minute 6:00 — NL Query Demo

- Type in chat: "Which vendors weren't paid last run?"
- Canvas switches to table view — shows deferred vendors
- Type: "Total fees paid to Pine Labs this month"
- Canvas switches to summary card — shows total fees
- This is the **CA moment** — accountant can query the system in English

### Minute 6:30 — YAML Extensibility

- (Optional slide or screen share showing hospital.yaml + kirana.yaml)
- "New persona = new YAML file. Same agent core."

### Minute 7:30 — Close

- Show audit dashboard one more time
- "PRIYA turns B2B payments into programmable workflows."

---

## 15. Design Tokens & Styling

### Colors

```css
/* Base */
--bg-primary: #0F172A;        /* Dark navy background */
--bg-secondary: #1E293B;      /* Card/panel background */
--bg-tertiary: #334155;       /* Hover states */
--text-primary: #F8FAFC;      /* White text */
--text-secondary: #94A3B8;    /* Muted text */
--text-muted: #64748B;        /* Timestamps, labels */
--border: #334155;

/* Status */
--success: #22C55E;           /* Green — PROCESSED, done */
--warning: #F59E0B;           /* Amber — AUTHORIZED, escalation */
--error: #EF4444;             /* Red — FAILED, error */
--info: #3B82F6;              /* Blue — PENDING, in-progress */
--deferred: #8B5CF6;          /* Purple — deferred, float saved */

/* Persona */
--hospital: #3B82F6;          /* Blue badge */
--kirana: #22C55E;            /* Green badge */

/* Pipeline strip */
--dot-done: #22C55E;
--dot-progress: #3B82F6;
--dot-pending: #475569;
--dot-failed: #EF4444;
```

### Typography

```css
--font-sans: 'Inter', system-ui, sans-serif;
--font-mono: 'JetBrains Mono', 'Fira Code', monospace;

--text-xs: 0.75rem;     /* Timestamps, labels */
--text-sm: 0.875rem;    /* Chat messages, table cells */
--text-base: 1rem;      /* Body text */
--text-lg: 1.125rem;    /* Section headers */
--text-xl: 1.25rem;     /* Page headers */
--text-3xl: 1.875rem;   /* Summary card numbers */
```

### Spacing

```css
--space-1: 0.25rem;
--space-2: 0.5rem;
--space-3: 0.75rem;
--space-4: 1rem;
--space-6: 1.5rem;
--space-8: 2rem;
```

### Border Radius

```css
--radius-sm: 0.375rem;   /* Buttons, badges */
--radius-md: 0.5rem;     /* Cards */
--radius-lg: 0.75rem;    /* Panels */
--radius-full: 9999px;   /* Pills, dots */
```

### Animations

```css
/* Pulse for in-progress dots and escalation rows */
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

/* Canvas transition */
.canvas-enter { opacity: 0; transform: translateY(10px); }
.canvas-enter-active { opacity: 1; transform: translateY(0); transition: all 300ms ease; }

/* Status dot transition */
.dot-transition { transition: background-color 300ms ease, transform 200ms ease; }
.dot-success { animation: pop 300ms ease; }

@keyframes pop {
  0% { transform: scale(1); }
  50% { transform: scale(1.3); }
  100% { transform: scale(1); }
}
```

---

## 16. File Structure (Suggested)

```
src/
├── App.tsx
├── main.tsx
├── index.css                  # Tailwind + custom tokens
│
├── components/
│   ├── TopBar/
│   │   ├── TopBar.tsx
│   │   ├── PersonaBadge.tsx
│   │   ├── RunIdBadge.tsx
│   │   ├── PipelineStrip.tsx
│   │   ├── StageSegment.tsx
│   │   └── AgentStatusPill.tsx
│   │
│   ├── Canvas/
│   │   ├── CanvasContainer.tsx     # Switches between 5 views
│   │   ├── WorkflowDiagram.tsx
│   │   ├── PolicyGate.tsx
│   │   ├── RunBoard.tsx
│   │   ├── VendorRow.tsx
│   │   ├── AuditDashboard.tsx
│   │   ├── ReconTable.tsx
│   │   ├── SummaryCards.tsx
│   │   └── QueryResultView.tsx
│   │
│   ├── RightPanel/
│   │   ├── RightPanel.tsx
│   │   ├── ChatPanel.tsx
│   │   ├── ChatInput.tsx
│   │   ├── ChatMessage.tsx
│   │   └── LogsDrawer.tsx
│   │
│   └── Shared/
│       ├── StatusDot.tsx
│       ├── RailIcon.tsx
│       ├── AmountDisplay.tsx       # Formats Rs with commas
│       └── FileUpload.tsx
│
├── hooks/
│   ├── useWebSocket.ts            # WS connection + event dispatching
│   └── usePriyaApi.ts             # REST API calls
│
├── store/
│   └── priyaStore.ts              # Zustand store
│
├── types/
│   └── index.ts                   # All TypeScript interfaces
│
└── utils/
    ├── formatAmount.ts            # Rs 45,000 formatting
    ├── formatTimestamp.ts         # HH:MM from ISO
    └── constants.ts               # API URLs, persona configs
```

---

## Quick Reference Card

| User Action | API Call | WS Event That Follows |
|-------------|----------|----------------------|
| Upload CSV + type instruction | `POST /run` | CANVAS_STATE → AGENT_NARRATION → POLICY_GATE |
| Click "Approve Run" | `POST /approve/{run_id}` | CANVAS_STATE:run_board → VENDOR_STATE stream |
| Click "Authorize" (Schedule H) | `POST /escalation/{run_id}/{vid}` | VENDOR_STATE update |
| Click "Reject" (Schedule H) | `POST /escalation/{run_id}/{vid}` | VENDOR_STATE → CANCELLED |
| Type NL question | `POST /query/{run_id}` | CANVAS_STATE:query_result → QUERY_RESULT |
| Click "CA Export" | `GET /run/{run_id}/export` | (direct download, no WS) |
| Switch persona | (client-side toggle) | (no API, just resets UI state) |

---

*This document contains everything needed to build the PRIYA frontend independently. The backend will serve the REST endpoints and WebSocket events exactly as documented above. Any questions — coordinate via the shared channel.*
