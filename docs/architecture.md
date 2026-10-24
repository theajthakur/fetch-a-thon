# MediTwin Architecture Specification

> **MediTwin: a Digital Twin for the Medical Supply Chain**  
> *Built for Fetch-A-Thon 3.0 (GCET Edition) — Challenge: ASI:One Agent Challenge: From Intent to Action*

---

## 1. Executive Summary

MediTwin is an autonomous, multi-agent digital twin system built on the **Fetch.ai `uagents` framework** and integrated with **ASI:One**. It transforms natural-language operational disruption alerts (e.g., supplier delays, regional demand surges, cold-chain temperature breaches) into deterministic risk assessments and actionable procurement resolutions without human spreadsheet wrangling.

### Core Architectural Tenet: Separation of Concerns
```
      Natural Language Intent                 Deterministic Execution
   ┌───────────────────────────┐           ┌───────────────────────────┐
   │    ASI:One & Action Agent │           │ Inventory & Simulation    │
   │  • Intent extraction      │ ────────> │  • Discrete digital twin  │
   │  • Dialogue orchestration │           │  • Monte Carlo simulation │
   │  • User preference memory │ <──────── │  • Zero LLM hallucination │
   │  • PO / Email formulation │           │  • 100% reproducible math │
   └───────────────────────────┘           └───────────────────────────┘
```
All quantitative analyses—stockout probabilities, shortfall timelines, days of cover, and cost calculations—are executed by deterministic, pure-Python algorithms. LLMs are strictly confined to natural language comprehension, preference parsing, and explanatory synthesis.

---

## 2. High-Level System Architecture

MediTwin consists of three specialized agents operating asynchronously over the **Fetch.ai Agent Network** via the **Agent Chat Protocol** and typed Pydantic messaging schemas.

```mermaid
flowchart TB
    subgraph UserInterface ["Human / ASI:One Interface"]
        Planner["Hospital Procurement Planner"]
        ASI1["ASI:One Chat Interface<br/>(asi1.ai)"]
    end

    subgraph ActionAgentLayer ["Action Agent (Orchestrator & Front Door)"]
        AA["Action Agent<br/>(agent1qt5pyuj...)"]
        LLM["ASI:One LLM API<br/>(api.asi1.ai/v1)"]
        KeywordParser["Strict Keyword / Heuristic<br/>Fallback Parser"]
        Watchdog["Watchdog Timer (5s)<br/>& Cache Invalidator"]
        StateStore["Agentverse Persistent Storage<br/>(Preferences, Orders, State)"]
    end

    subgraph DigitalTwinStateLayer ["Digital Twin Layer"]
        IA["Inventory Agent<br/>(agent1qw5sdhr...)"]
        LiveState["Hospital Twin State<br/>(12 Items, 7 Suppliers,<br/>Stock & Demand Distributions)"]
    end

    subgraph SimulationLayer ["Simulation & What-If Engine"]
        SA["Simulation Agent<br/>(agent1q229ftx...)"]
        SimCore["Monte Carlo Sim Core<br/>(simulation-agent.py - 1,000 runs)"]
        EmbeddedState["Fallback Twin Snapshot<br/>(data.json)"]
    end

    Planner <-->|"Natural Language<br/>(Agent Chat Protocol)"| ASI1
    ASI1 <-->|"ChatMessage / Interactive Card"| AA

    AA <-->|"Intent Extraction<br/>(HTTP POST / JSON)"| LLM
    AA -.->|"Cooldown / Quota Failover"| KeywordParser
    AA --- Watchdog
    AA --- StateStore

    AA -->|"StockRequest(request_id)"| IA
    IA -->|"StockSnapshot(request_id, items)"| AA
    IA --- LiveState

    AA -->|"SimRequest(request_id, items, scenario)"| SA
    SA -->|"SimResult(request_id, results)"| AA
    SA --- SimCore
    SA -.->|"Auto-fallback if IA unreachable"| EmbeddedState
```

---

## 3. Agent Topology & Responsibilities

| Agent | Module | Primary Role | Discoverable in ASI:One | Core State / Dependencies |
|---|---|---|---|---|
| **Action Agent** | [`action-agent.py`](../src/agents/action-agent.py) | Front door, dialogue state machine, intent parser, PO generator | **Yes** (User-facing) | Session storage, open request pool, LLM fallback circuit |
| **Inventory Agent** | [`inventory-agent.py`](../src/agents/inventory-agent.py) | Digital twin source of truth (stock, demand, lead times) | No (Internal) | In-memory catalog (12 items, 7 suppliers) |
| **Simulation Agent** | [`simulation-agent.py`](../src/agents/simulation-agent.py) | Wrapped Monte Carlo engine; what-if scenario evaluator | No (Internal) | Embedded baseline snapshot, simulation core |

### 3.1. Action Agent (`action-agent.py`)
The Action Agent acts as the orchestrator and personal assistant for the procurement officer:
- **Protocol Compliance**: Exposes the Fetch.ai Agent Chat Protocol (`chat_protocol_spec`), processing incoming `ChatMessage` payloads and emitting `ChatAcknowledgement`.
- **Hybrid Intent Extraction**:
  1. Primary: Calls the ASI:One LLM API (`https://api.asi1.ai/v1`) using a strictly constrained JSON-only system prompt.
  2. Failover: Automatically activates a deterministic keyword/regex heuristic parser if the LLM endpoint returns HTTP 401/403 (quota exceeded), timeouts (>8s), or connection errors. Engages a 600-second cooldown timer to prevent chat lag.
- **State Machine & Memory**:
  - Manages conversational history and pending approvals (`pending_<sender>`).
  - Stores user preferences (`prefs_<sender>`), such as supplier preferences and budget caps (in INR), persisting them between messages.
  - Logs approved Purchase Orders (`orders_<sender>`).
- **Synchronized Inventory Caching**:
  - Automatically queries the Inventory Agent every 60 seconds (`SYNC_PERIOD = 60.0s`).
  - Caches the snapshot in memory (`CACHE_MAX_AGE = 300s`) so incoming simulation requests can immediately route to the Simulation Agent, saving a round-trip network hop.
- **Action Synthesis**: Formats risk tables, generates interactive UI cards (Agentverse Metadata Protocol), drafts formal purchase orders with unique IDs (`PO-YYYYMMDD-XXXX`), and composes ready-to-dispatch vendor emails.

### 3.2. Inventory Agent (`inventory-agent.py`)
The Inventory Agent acts as the digital twin of City General Hospital's pharmacy and medical stores:
- **Data Catalog**: Maintains 12 critical medicines, fluids, and surgical supplies with mean daily consumption, standard deviation, current warehouse stock, reorder levels, cold-chain requirements, and contracted suppliers with unit costs and baseline lead times.
- **Request Handling**: Listens for `StockRequest`. Supports filtering by target items or returns the complete catalog snapshot via `StockSnapshot`.
- **Fault-Tolerant Delivery**: If catalog reconstruction encounters an anomaly, emits an empty list rather than dropping the message, triggering downstream embedded failover.

### 3.3. Simulation Agent (`simulation-agent.py`)
The Simulation Agent encapsulates the discrete-event Monte Carlo what-if simulation:
- **Payload Ingestion**: Receives `SimRequest` containing the current inventory snapshot, scenario parameters (preset or custom), focus item, and user preferences.
- **Dynamic Data Validation**: Validates the incoming inventory data using `validate_data()`. If the Action Agent passed an empty or corrupted inventory payload, the agent gracefully falls back to its embedded copy of `data.json`.
- **Execution**: Invokes the engine in [`simulation-agent.py`](../src/agents/simulation-agent.py) over 1,000 seeded Monte Carlo runs, with demand paths and supplier-delay draws shared across all mitigation options (common random numbers).
- **Response**: Returns a structured `SimResult` containing per-item risk profiles, criticality-weighted risk scores, days of cover, expected stockout dates, and ranked multi-modal fix options.

---

## 4. End-to-End Execution Flow

The sequence diagram below illustrates the complete lifecycle of a user interaction—from an initial what-if query to an approved and drafted purchase order.

```mermaid
sequenceDiagram
    autonumber
    actor Planner as Hospital Planner
    participant ASI as ASI:One Interface
    participant AA as Action Agent
    participant LLM as ASI:One LLM API
    participant IA as Inventory Agent
    participant SA as Simulation Agent

    Planner->>ASI: "SUP01 is delayed 10 days and insulin demand +30%. What should we do?"
    ASI->>AA: ChatMessage(text)
    AA->>ASI: ChatAcknowledgement

    rect rgb(240, 248, 255)
        note over AA, LLM: 1. Intent Extraction Pipeline
        AA->>LLM: POST /v1/chat/completions (SYSTEM_PROMPT, user_text)
        alt LLM Available
            LLM-->>AA: {"intent": "simulate", "scenario": {"supplier_id": "SUP01", "delay_days": 10, ...}}
        else Quota Exceeded / Timeout
            note over AA: Trigger Fallback Parser<br/>(Keyword / Regex Engine)
        end
    end

    rect rgb(255, 250, 240)
        note over AA, IA: 2. Digital Twin State Acquisition
        alt Cache Valid (< 300s)
            note over AA: Read from MEM["inv_cache"]
        else Cache Miss / Cold Start
            AA->>IA: StockRequest(request_id)
            IA-->>AA: StockSnapshot(request_id, items)
            note over AA: Update MEM["inv_cache"]
        end
    end

    rect rgb(240, 255, 240)
        note over AA, SA: 3. What-If Simulation Execution
        AA->>SA: SimRequest(request_id, items, scenario, prefs)
        note over SA: Run 1,000 Monte Carlo trajectories<br/>Rank options: Order, Expedite, Transfer, Bridge
        SA-->>AA: SimResult(request_id, results, meta)
    end

    rect rgb(255, 245, 245)
        note over AA, Planner: 4. Presentation & Interactive Approval
        note over AA: Store pending approval in storage<br/>Build Agentverse Interactive Card
        AA->>ASI: ChatMessage(Text + Metadata Card)
        ASI-->>Planner: Render Risk Summary & Plan Radio Options
    end

    rect rgb(245, 245, 255)
        note over Planner, AA: 5. Action Execution
        Planner->>ASI: Clicks "Approve & draft PO" (or types "YES")
        ASI->>AA: ChatMessage("YES" / selection JSON)
        AA->>ASI: ChatAcknowledgement
        note over AA: Generate PO-YYYYMMDD-XXXX<br/>Compose Supplier Dispatch Email<br/>Compose Transfer Notice
        AA->>ASI: ChatMessage(Purchase Order + Vendor Email)
        ASI-->>Planner: Ready-to-send PO and Email drafted
    end
```

---

## 5. Intent Extraction & Parsing Engine

The Action Agent employs a **dual-engine intent pipeline** that balances the flexibility of large language models with the reliability required for production infrastructure:

```
                          ┌───────────────────────────┐
                          │ Incoming User Chat Message│
                          └─────────────┬─────────────┘
                                        │
                         Is LLM in Cooldown? (600s)
                                       / \
                                Yes   /   \  No
                                     /     \
                                    │   Call ASI:One LLM API
                                    │   (Timeout: 8.0s)
                                    │       / \
                                    │  Fail/   \ Success
                                    │     /     \
                                    ▼    ▼       ▼
                       ┌───────────────────┐  ┌───────────────────┐
                       │ Strict Heuristic  │  │ Structured JSON   │
                       │ Keyword / Regex   │  │ Response          │
                       │ Fallback Parser   │  └───────────────────┘
                       └─────────┬─────────┘
                                 │
                                 ▼
                     Extracted Execution Context:
                     • Intent: simulate | brief | set_preference | help | none
                     • Scenario: Presets (SC1..SC3) or Custom (delay, surge, spoilage)
                     • Focus Item: matched via alias table (e.g. epinephrine -> adrenaline)
                     • Preferences: budget cap (INR), preferred supplier IDs
```

### 5.1. Strict Keyword Fallback Grammar
The heuristic parser guarantees system availability even during API outages. It is strictly calibrated to avoid false positives:
- **Delay Detection**: Matches patterns like `delay\w* ... (\d+) days` or `(\d+) days delay`, extracting vendor mentions (`SUP01`..`SUP07` or vendor brand tokens like "Apex" or "LifeLine").
- **Surge Detection**: Identifies percentage changes like `demand ... ([+-]?\d+)%` and converts them to demand multipliers (e.g., `+30%` $\to 1.3$).
- **Cold-Chain Spoilage**: Matches terms like `heatwave`, `fridge failure`, or `cold-chain break`, mapping them to 25% spoilage of temperature-sensitive items.
- **Budget Extraction**: Parses amounts expressed in Indian numbering syntax (e.g., `5 lakh` $\to \text{Rs } 500,000$, `50k` $\to \text{Rs } 50,000$).
- **Anti-Hallucination Guardrails**: General descriptive commentary (e.g., "5.4 days of cover", "1% risk") is rejected from triggering false scenario alterations.

---

## 6. Fault Tolerance, Timeouts, and Reliability

MediTwin is designed for extreme resilience against network partitioning, agent downtime, and rate limits:

```mermaid
flowchart TD
    Start([Action Agent initiates Run]) --> CheckCache{Valid Inventory<br/>Cache in RAM?}

    CheckCache -- Yes (<300s) --> SendSim[Send SimRequest to Simulation Agent]
    CheckCache -- No --> ReqInv[Send StockRequest to Inventory Agent]

    ReqInv --> WatchdogInv{Inventory Agent<br/>replies within 15s?}
    WatchdogInv -- Yes --> CacheAndSend[Cache items & send SimRequest] --> SendSim
    WatchdogInv -- No (Timeout) --> FallbackInv[Log Warning & send SimRequest<br/>with items = empty] --> SendSim

    SendSim --> WatchdogSim{Simulation Agent<br/>replies within 40s?}
    WatchdogSim -- Yes --> ProcessResult[Format results & present to user]
    WatchdogSim -- No (Timeout) --> FallbackSim[Notify user: Simulation Agent<br/>temporarily unavailable]

    subgraph SimulationAgentInternal ["Simulation Agent Internal Fallback"]
        ValidateInflow{items valid?}
        ValidateInflow -- Yes --> RunLive[Simulate using Live Inventory]
        ValidateInflow -- No / Empty --> RunEmbedded[Simulate using Embedded data.json]
    end

    SendSim -.-> ValidateInflow
```

### Resilience Mechanisms Summary:
1. **Dual Watchdogs**: A periodic background task (`watchdog` running every 5.0s) tracks open request IDs (`open_rids`).
   - If the Inventory Agent does not answer within **15 seconds** (`INV_TIMEOUT`), the Action Agent dispatches the simulation request with an empty payload.
   - If the Simulation Agent does not respond within **40 seconds** (`SIM_TIMEOUT`), the request is cleaned up and a graceful error explanation is sent to the user.
2. **Double Fallback in Simulation Agent**: When `pick_data()` receives an empty or invalid inventory list, it automatically falls back to its embedded snapshot (`DATA`), ensuring an answer is always produced.
3. **Seeded Monte Carlo Predictability**: Simulation seeds are standardized across comparison runs, preventing random drift from skewing plan selections.
4. **Duplicate Message Suppression**: Action Agent maintains a rolling buffer (`MEM["seen"]` of 200 message IDs) and suppresses duplicate transmissions within 20 seconds.

---

## 7. Interactive Cards & Action Protocol

MediTwin supports **Agentverse Interactive Cards** via the `MetadataContent` specification of the chat protocol:

- **Protocol Version**: `card_protocol_version: "1"`
- **Card Kind**: `"detail"`
- **Features**:
  - Displays summary rows (Focus item, active disruption scenario, unmitigated stockout risk).
  - Presents a dynamic radio button list containing all evaluated fix options (Order, Expedite, Transfer, Bridge), highlighting the recommended lowest-cost safe plan.
  - Interactive CTAs: Primary button (`Approve & draft PO`) and secondary button (`Cancel`).
- **Graceful Fallback**: If the client does not support interactive cards (or if `MetadataContent` is not installed), the Action Agent falls back to standard Markdown formatting with instructions: *"Reply YES to approve the recommended plan, option N to pick another, or NO to cancel."*

---

## 8. Directory & Module Mapping

```
fetch-a-thon/
├── README.md                          # Hackathon project overview & submission portal
├── requirements.txt                   # uagents, uagents-core, openai (agents only)
├── tests/test_simulation_agent.py     # 18 stdlib-only tests against the deployed engine
├── src/
│   ├── main.py                        # CLI: runs the engine from simulation-agent.py
│   ├── dataset/
│   │   └── data.json                  # Digital twin baseline: items, suppliers, sister hospitals
│   └── agents/
│       ├── action-agent.py            # User-facing orchestrator, LLM intent parser, PO generator
│       ├── inventory-agent.py         # Digital twin state provider (City General Hospital)
│       └── simulation-agent.py        # Simulation engine v1.1 + embedded dataset (single source)
└── docs/
    ├── architecture.md                # (This file) Complete system architecture & data flow
    ├── simulation-model.md            # Mathematical formulation of the Monte Carlo engine
    ├── agent-spec.md                  # Comprehensive agent messaging schemas & specifications
    ├── dataset-and-twin-schema.md     # Data dictionary, supplier model, and twin configuration
    └── deployment-and-run-guide.md    # Local & Agentverse setup, environment vars, and execution
```
