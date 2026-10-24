# MediTwin Deployment & Operations Runbook

> **Target Audience**: Developers, judges, and operators deploying MediTwin locally or to Agentverse.

---

## 1. Prerequisites & Environment Setup

- **Python Version**: Python 3.10, 3.11, or 3.12 (standard 64-bit).
- **Package Manager**: `pip` with `venv`.
- **Operating System**: Linux, macOS, or Windows (tested on Windows 11 with PowerShell).

### 1.1. Virtual Environment Installation
From the root of the repository:

```bash
# Navigate to fetch-a-thon directory
cd fetch-a-thon

# Create isolated virtual environment
python -m venv venv

# Activate virtual environment
# On Windows PowerShell:
.\venv\Scripts\Activate.ps1
# On macOS / Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Configuration & Environment Variables

MediTwin configures its agents via environment variables or `.env` files:

| Variable | Required? | Default | Description |
|---|---|---|---|
| `INVENTORY_ADDR` | **Yes** (in production) | Code constant | Bech32 uAgent address of the Inventory Agent (`agent1qw5...`) |
| `SIMULATION_ADDR` | **Yes** (in production) | Code constant | Bech32 uAgent address of the Simulation Agent (`agent1q22...`) |
| `ASI1_API_KEY` | Optional | `""` | ASI:One LLM API token. If omitted or quota exhausted, the agent automatically falls back to the keyword parser. |
| `ASI1_BASE_URL` | Optional | `https://api.asi1.ai/v1` | Base URL for the OpenAI-compatible ASI:One LLM gateway |

### Sample `.env` File:
```env
INVENTORY_ADDR="agent1qw5sdhru2eml9w08k65waf0rnqpxz6xazqvcmnen57t4p75gvgegjke70eg"
SIMULATION_ADDR="agent1q229ftx9ae3vlwjy87hw7cnvaa6dm8g99q5lh0ky5e824kz69xcage63jj3"
ASI1_API_KEY="your-asi1-api-key"
ASI1_BASE_URL="https://api.asi1.ai/v1"
```

---

## 3. Operational Modes

### Mode A: Standalone Simulation Engine (Zero Dependencies)
You can verify the mathematical models and Monte Carlo simulations immediately without starting any network agents or installing uagents:

```bash
cd fetch-a-thon
python src/main.py
python -m unittest discover -s tests -v
```

**Expected Output:**
```text
Scenario: Supplier delay + insulin demand surge
Risk overview: 1 critical, 0 high, 0 medium, 11 low

- Insulin Glargine 100IU/mL vial: 11.5 days of cover, stockout probability 100%, risk 100.0 (critical)
- Heparin 5000IU vial: 15.9 days of cover, simulated stockout 9.8%, risk 7.8 (low)
...
Focus: Insulin Glargine 100IU/mL vial
  * Bridge: transfer 230 from sister hospitals + order 754 from Himalaya MedSupply (6d) | stockout 0/1,000 runs (95% bound <0.3%) | Rs 364,920 <- recommended
  * Bridge: transfer 230 from sister hospitals + order 754 from LifeLine Biologics (4d) | stockout 0/1,000 runs (95% bound <0.3%) | Rs 395,080
  * Order 984 from Himalaya MedSupply (6d lead) | stockout 0/1,000 runs (95% bound <0.3%) | Rs 472,320
  * Order 984 from LifeLine Biologics (4d lead) | stockout 0/1,000 runs (95% bound <0.3%) | Rs 511,680
  Why: Lowest-cost plan keeping stockout risk <= 5%
```

---

### Mode B: Local Multi-Agent Orchestration
To run the full multi-agent cluster locally on your development machine:

#### Step 1: Start the Inventory Agent
Open Terminal 1:
```bash
cd fetch-a-thon
python -c "from src.agents import inventory_agent; inventory_agent.agent.run()"
```
*Take note of the printed Agent Address (e.g., `agent1qw5...`).*

#### Step 2: Start the Simulation Agent
Open Terminal 2:
```bash
cd fetch-a-thon
python -c "from src.agents import simulation_agent; simulation_agent.agent.run()"
```
*Take note of the printed Agent Address (e.g., `agent1q22...`).*

#### Step 3: Configure and Start the Action Agent
In Terminal 3:
Ensure `INVENTORY_ADDR` and `SIMULATION_ADDR` in `src/agents/action-agent.py` match the addresses from Steps 1 & 2, then run:
```bash
cd fetch-a-thon
python -c "from src.agents import action_agent; action_agent.agent.run()"
```

---

### Mode C: Agentverse Cloud Deployment (Hackathon Submission Mode)

1. **Log in to Agentverse**: Navigate to [https://agentverse.ai](https://agentverse.ai).
2. **Deploy the Agents in Sequence**:
   - **Agent 1: Inventory Agent**:
     - Create new Hosted Agent $\to$ Name: `meditwin-inventory`.
     - Paste code from [`src/agents/inventory-agent.py`](../src/agents/inventory-agent.py).
     - Start agent and copy its address (`agent1qw5...`).
   - **Agent 2: Simulation Agent**:
     - Create new Hosted Agent $\to$ Name: `meditwin-simulation`.
     - Paste code from [`src/agents/simulation-agent.py`](../src/agents/simulation-agent.py).
     - Start agent and copy its address (`agent1q22...`).
   - **Agent 3: Action Agent**:
     - Create new Hosted Agent $\to$ Name: `Supply Chain Action Agent`.
     - Paste code from [`src/agents/action-agent.py`](../src/agents/action-agent.py).
     - Default addresses for `INVENTORY_ADDR` and `SIMULATION_ADDR` are already pre-configured.
     - Start agent.
3. **Configure Agentverse Profiles & Hackpack Badges**:
   - In each agent's **README / Description** tab on Agentverse, paste its short overview and the required hackpack discovery badges:
     ```markdown
     ![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
     ![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)
     ```
   - Ready-to-copy markdown descriptions for all three agents are provided in the main [README.md](../README.md#agentverse-profile-descriptions--discovery-badges).
4. **Register Action Agent on ASI:One**:
   - In the Action Agent's profile on Agentverse, ensure the **Chat Protocol** is published and the agent handle (`@supply-chain-action`) is discoverable.
   - Open [ASI:One](https://asi1.ai/) and search for `@supply-chain-action` or query the agent address directly.

---

## 4. Test Scenarios & Verification Commands

Test the end-to-end conversational pipeline with these sample prompts:

### 1. Daily Risk Brief:
> *"Give me today's risk brief."*  
> **Expected Response**: Ranked summary of items by days of cover; flags medical oxygen (8.6 days) and insulin (15 days).

### 2. The Hero Scenario (`SC1`):
> *"Supplier SUP01 is delayed 10 days and insulin demand is up 30%. What happens and what should we do?"*  
> **Expected Response**:
> - Insulin stockout probability: 100% (Critical).
> - Cascade detection: Heparin simulated stockout probability lifts to about 10%.
> - Fix plan: Recommends Bridge Strategy (inter-hospital transfer + order from Himalaya MedSupply for ₹364,920).

### 3. Setting User Preferences:
> *"Prefer LifeLine Biologics and keep budget under 5 lakh."*  
> **Expected Response**: Confirms preference saved; automatically recalculates fix recommendations using LifeLine Biologics (₹395,080, 0/1,000 simulated stockouts).

### 4. Interactive Approval & PO Drafting:
> *"YES"* (or click **Approve & draft PO** on the interactive card).  
> **Expected Response**: Generates official Purchase Order `PO-YYYYMMDD-XXXX` with itemized costs, delivery timeline, and a drafted supplier email to LifeLine Biologics.

---

## 5. Troubleshooting & FAQ

### Issue: "ASI:One parse failed: 403 - insufficient_quota"
- **Cause**: The Agentverse LLM token quota for the current billing window has been exhausted.
- **Remedy**: **No action required.** MediTwin features an automated circuit breaker. Upon detecting HTTP 403/401, the Action Agent enters a 600-second LLM cooldown and automatically routes incoming queries through its deterministic keyword/regex heuristic engine. All scenario parameters, numbers, and intents will continue to function seamlessly.

### Issue: "Inventory Agent did not respond; used Simulation Agent's snapshot"
- **Cause**: Network latency or temporary downtime of the Inventory Agent exceeded 15 seconds (`INV_TIMEOUT`).
- **Remedy**: MediTwin's watchdog automatically fell back to the Simulation Agent's embedded copy of `data.json`. The user is served without interruption, and a subtle note is appended to the report.

### Issue: Duplicate message received in chat
- **Cause**: High network latency triggering client retransmissions.
- **Remedy**: The Action Agent maintains a 200-message deduplication cache (`MEM["seen"]`) and discards duplicate message IDs within a 20-second rolling window.
