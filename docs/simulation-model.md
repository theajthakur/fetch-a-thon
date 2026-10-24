# MediTwin Simulation Engine: Mathematical & Algorithmic Model

> **Module Reference**: [`simulation-agent.py`](../src/agents/simulation-agent.py)  
> *Pure Python standard library implementation (`math`, `random`, `json`). Zero external dependencies.*

---

## 1. Overview & Modeling Philosophy

MediTwin's simulation engine models hospital supply chains under stochastic demand and uncertain lead times over a **21-day planning horizon**.

In medical supply chain management, deterministic approximations (e.g., static Days of Cover = $\frac{\text{Current Stock}}{\text{Mean Daily Demand}}$) fail catastrophically because:
1. Daily patient demand fluctuates according to clinical distributions with non-trivial variances.
2. Deliveries do not always arrive on schedule; supplier reliability is probabilistic.
3. Single suppliers service multiple critical formulations (supply cascade vulnerability).

To eliminate model hallucination while retaining sub-second execution speeds, MediTwin implements a **discrete-time Monte Carlo simulation** executed over 1,000 randomized synthetic trajectories.

```
                    ┌────────────────────────────┐
                    │ Current Digital Twin State │
                    │ (Stock, Demand μ, σ, Sups) │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │ Disruption Scenario Engine │
                    │ (Delay days, Multiplier m) │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
           ┌──────────────────────────────────────────────┐
           │        Monte Carlo Evaluation Core           │
           │ (1,000 Iterations per option, seed-pinned)   │
           │                                              │
           │  • Stochastic Daily Demand: Gauss(μ·m, σ·m)  │
           │  • Inflow Arrivals: Lead ± Unreliable Delay  │
           │  • Trajectory: S_t = S_{t-1} + Inflow_t - D_t│
           │  • Accumulate Unmet Units & First Stockout   │
           └──────────────────────┬───────────────────────┘
                                  │
                                  ▼
           ┌──────────────────────────────────────────────┐
           │     Risk Scoring & Recommendation Engine     │
           │  • Stockout Probability                      │
           │  • Criticality-Weighted Risk Score           │
           │  • Multi-Modal Fix Generation                │
           │  • Budget-Constrained Pareto Recommendation  │
           └──────────────────────────────────────────────┘
```

---

## 2. Core Model Parameters & Assumptions

All mathematical constants are defined at the top of the engine in [`simulation-agent.py`](../src/agents/simulation-agent.py):

| Constant | Value | Definition & Rationale |
|---|---|---|
| `PLAN_DAYS` | `21` | Forward-looking planning horizon in days (~3 weeks). Balances visibility with actionable lead times. |
| `SAFETY` | `1.15` | Safety stock factor (+15% demand buffer above nominal demand over the 21-day period). |
| `RUNS` | `1000` | Monte Carlo runs per evaluation. Sampling error is at most about ±3 percentage points (95%, at p = 0.5) and much smaller near 0 or 1; see *Engine v1.1* below. |
| `UNRELIABLE_DELAY_DAYS` | `3` | Penalty days added to delivery if a supplier fails reliability check on a given run. |
| `EXPEDITE_LEAD_FACTOR` | `0.5` | Expedited order compresses supplier lead time by 50% ($\lceil \text{lead} \times 0.5 \rceil$). |
| `EXPEDITE_COST_FACTOR` | `1.2` | Expedited orders incur a 20% surcharge on unit purchase price. |
| `TRANSFER_COST_INR` | `1500` | Flat logistics and courier fee (₹1,500) per inter-hospital emergency stock transfer. |
| `OK_PROB` | `0.05` | Threshold for safety: any procurement plan with stockout probability $\le 5\%$ is considered safe. |

---

## 3. Mathematical Formulation

### 3.1. Effective Demand & Stock Adjustments
For any item $i$ under scenario $\mathcal{S}$:
- **Demand Multiplier ($m_i$)**:
  $$m_i = \text{clamp}\left(m_{\text{specific}, i} \times m_{\text{global}}, 0.1, 5.0\right)$$
- **Effective Daily Mean ($\mu_i^{\text{eff}}$) & Variance ($\sigma_i^{\text{eff}}$)**:
  $$\mu_i^{\text{eff}} = \mu_i \cdot m_i$$
  $$\sigma_i^{\text{eff}} = \sigma_i \cdot m_i$$
- **Effective On-Hand Stock ($S_0$)**:
  $$S_0 = \begin{cases}
  \text{Stock}_i \cdot (1 - \text{spoilage\_pct}), & \text{if item } i \text{ requires cold chain and spoilage} > 0 \\
  \text{Stock}_i, & \text{otherwise}
  \end{cases}$$

### 3.2. Order Sizing Algorithm
When placing an order for item $i$, the required gross quantity covers nominal demand for 21 days with safety padding minus existing stock:

$$\text{OrderQty}(i) = \left\lceil \max\left(0, \mu_i^{\text{eff}} \times \text{PLAN\_DAYS} \times \text{SAFETY} - S_0\right) \right\rceil$$

### 3.3. Stochastic Arrival Modeling
For each scheduled inflow $k = (\text{Day}_k, \text{Qty}_k, p_{\text{late}, k})$ where $p_{\text{late}, k} = 1 - \text{Reliability}_s$:
On simulation run $r \in [1, \text{RUNS}]$, draw uniform random variable $U_{k, r} \sim \mathcal{U}(0, 1)$:
$$\text{ArrivalDay}_{k, r} = \begin{cases}
\text{Day}_k + \text{UNRELIABLE\_DELAY\_DAYS}, & \text{if } U_{k, r} < p_{\text{late}, k} \\
\text{Day}_k, & \text{otherwise}
\end{cases}$$

All arrivals on day $t$ are summed into an arrival pool:
$$A_{t, r} = \sum_{k \mid \text{ArrivalDay}_{k, r} = t} \text{Qty}_k$$

### 3.4. Daily Inventory Trajectory Simulation
On each run $r$, starting with inventory $S_{0, r} = S_0$ and unmet units $U_r = 0$:

For each day $t \in [1, \text{PLAN\_DAYS}]$:
1. **Receive scheduled shipments**:
   $$S_{t, r}^{\text{available}} = S_{t-1, r} + A_{t, r}$$
2. **Sample daily patient consumption**:
   $$D_{t, r} = \max\left(0.0, Z_{t, r}\right) \quad \text{where } Z_{t, r} \sim \mathcal{N}\left(\mu_i^{\text{eff}}, \left(\sigma_i^{\text{eff}}\right)^2\right)$$
3. **Evaluate stockout / consumption**:
   $$\begin{cases}
   U_r \leftarrow U_r + \left(D_{t, r} - S_{t, r}^{\text{available}}\right), \quad S_{t, r} = 0, & \text{if } D_{t, r} > S_{t, r}^{\text{available}} \\
   S_{t, r} = S_{t, r}^{\text{available}} - D_{t, r}, & \text{if } D_{t, r} \le S_{t, r}^{\text{available}}
   \end{cases}$$
4. If $D_{t, r} > S_{t, r}^{\text{available}}$ and no previous stockout has occurred in run $r$, record $T_{\text{first}, r} = t$.

### 3.5. Metric Aggregation
Across all $N = \text{RUNS}$ iterations:
- **Stockout Probability ($P_{\text{stockout}}$)**:
  $$P_{\text{stockout}} = \frac{1}{N} \sum_{r=1}^N \mathbb{I}(U_r > 0)$$
- **Expected Unmet Units ($\mathbb{E}[U]$)**:
  $$\mathbb{E}[U] = \frac{1}{N} \sum_{r=1}^N U_r$$
- **Expected Day of First Stockout**:
  $$\mathbb{E}[T_{\text{first}}] = \frac{1}{\sum_{r=1}^N \mathbb{I}(U_r > 0)} \sum_{r \mid U_r > 0} T_{\text{first}, r}$$

---

## 4. Risk Scoring & Severity Stratification

Clinical severity depends not only on the probability of a stockout, but on the therapeutic criticality of the item:

$$\text{RiskScore}(i) = P_{\text{stockout}}(i) \times \frac{\text{Criticality}_i}{5.0} \times 100$$

Where $\text{Criticality}_i \in \{1, 2, 3, 4, 5\}$:
- **Level 5 (Life Critical)**: ICU essentials with no therapeutic substitute (e.g., Insulin, Adrenaline, Medical Oxygen).
- **Level 4 (High Criticality)**: Critical antibiotics, vaccines, IV fluids (e.g., Ceftriaxone, Normal Saline, Heparin).
- **Level 3 (Medium Criticality)**: Essential consumables & PPE (e.g., IV Cannulas, N95 Masks, Surgical Gloves).
- **Level 2 (Standard Formulary)**: Non-critical symptomatic medications (e.g., Paracetamol).

### Severity Classification Thresholds:
```
Risk Score >= 60.0  ──►  CRITICAL  (Immediate emergency intervention required)
Risk Score >= 30.0  ──►  HIGH      (Active mitigation required within 24-48 hours)
Risk Score >= 10.0  ──►  MEDIUM    (Procurement team alert, close monitoring)
Risk Score <  10.0  ──►  LOW       (Covered by nominal inventory / reorder cycle)
```

---

## 5. Multi-Modal Fix Option Generation

When an item exhibits $P_{\text{stockout}} > 0.02$, the engine simulates four distinct mitigation strategies:

### 5.1. Standard Supplier Order
- **Mechanics**: Order $\text{OrderQty}$ from each contracted supplier.
- **Lead Time**: $L_s = \text{base\_lead}_s + \Delta_{\text{scenario}}$.
- **Cost**: $\text{Cost} = \text{OrderQty} \times \text{unit\_cost}_s$.
- **Inflow**: $[(L_s, \text{OrderQty}, 1 - \text{reliability}_s)]$.

### 5.2. Expedited Supplier Order
- **Mechanics**: Fast-track dispatch using air/dedicated freight.
- **Lead Time**: $L_s^{\text{exp}} = \max(1, \text{round}(L_s \times 0.5))$.
- **Cost**: $\text{Cost} = \text{OrderQty} \times \text{unit\_cost}_s \times 1.20$.
- **Inflow**: $[(L_s^{\text{exp}}, \text{OrderQty}, 1 - \text{reliability}_s)]$.

### 5.3. Inter-Hospital Transfer
- **Mechanics**: Request available spare stock from sister hospitals (e.g., H2 District Hospital Noida, H3 Ghaziabad Community Hospital).
- **Lead Time**: $L_h = \max(1, \lceil \text{transfer\_hours}_h / 24 \rceil)$ (typically 1 day).
- **Cost**: Fixed logistics cost $\text{Cost} = \text{₹1,500}$ per transferring facility.
- **Inflow**: $[(L_h, \text{SpareUnits}_{h, i}, 0)]$ (inter-hospital ambulances/couriers have zero late probability).

### 5.4. Bridge Strategy (Transfer + Order)
- **Mechanics**: Combines immediate sister-hospital transfer to bridge the gap during the delayed supplier's transit, while simultaneously placing an order for the remainder of the 21-day planning horizon.
- **Bridge Order Quantity**:
  $$\text{BridgeQty} = \left\lceil \max\left(0, \mu_i^{\text{eff}} \times \text{PLAN\_DAYS} \times \text{SAFETY} - (S_0 + \text{TotalTransferred})\right)\right\rceil$$
- **Inflows**: All transfer deliveries + regular order delivery on day $L_s$.
- **Cost**: $\text{BridgeQty} \times \text{unit\_cost}_s + \text{₹1,500} \times \text{Number of Transfers}$.

---

## 6. Optimization & Recommendation Engine

The engine resolves a multi-objective optimization problem:
$$\min_{\omega \in \Omega} \left( \text{Cost}(\omega), \text{LeadTime}(\omega) \right)$$
$$\text{subject to } P_{\text{stockout}}(\omega) \le 0.05 \quad \text{and} \quad \text{Cost}(\omega) \le \text{BudgetCap}$$

### Recommendation Algorithm (`_recommend(opts)`):
1. **Budget Filter**: Filter candidate options $\Omega$ to those within the user's budget cap. (If all exceed budget, fall back to the full candidate pool).
2. **Safety Filter**: Identify safe plans where $P_{\text{stockout}} \le 0.05$.
3. **If Safe Plans Exist**:
   Select the plan that minimizes:
   $$\text{rank}(\omega) = \big( \mathbb{I}(\text{supplier} \notin \text{preferred}), \ \text{TotalCost}(\omega), \ \text{LeadTime}(\omega) \big)$$
   *Explanation: Prioritizes user's preferred supplier first, then lowest financial cost, then shortest delivery time.*
4. **If No Safe Plan Exists**:
   Select the plan minimizing:
   $$\text{rank}(\omega) = \big( P_{\text{stockout}}(\omega), \ \text{TotalCost}(\omega) \big)$$
   *Explanation: Selects the option that maximally reduces patient mortality risk.*

---

## 7. Supply Cascade Detection

A critical capability of MediTwin is identifying **supply cascades**.

In single-item procurement spreadsheets, a delay announcement for **Supplier SUP01 (Apex Pharma Distributors)** might only be flagged for the primary reorder item (Insulin Glargine). However, SUP01 supplies 4 separate items across the formulary:
- **I01 (Insulin Glargine)**: Primary supplier (7d base lead).
- **I02 (Hepatitis B Vaccine)**: Secondary supplier (6d base lead).
- **I03 (Oxytocin Ampoules)**: Secondary supplier (6d base lead).
- **I08 (Heparin 5000IU)**: Primary supplier (5d base lead).

When SUP01 suffers a 10-day delay, MediTwin scans the complete formulary and discovers:
- **Insulin**: Days of cover drops to 11.5 days vs. new supplier lead of 17 days $\to$ **100% stockout risk (Critical)**.
- **Heparin**: Days of cover is 15.9 days vs. new supplier lead of 15 days $\to$ **about 10% simulated stockout risk (Low alert, cascading impact)**.

By flagging Heparin alongside Insulin, procurement can bundle mitigation orders before secondary stockouts materialize.

---

## 8. Built-in Preset Scenarios

| Preset ID | Name | Parameters | Target Items Affected | Primary Clinical Risk |
|---|---|---|---|---|
| **`SC1`** | Supplier delay + Insulin surge | `supplier_id: "SUP01"`, `delay_days: 10`, `demand_multiplier: {"I01": 1.3}` | Insulin Glargine, Heparin | Diabetic ketoacidosis crisis, heparin shortage |
| **`SC2`** | Heatwave cold-chain failure | `spoilage_pct: 0.25`, `cold_chain: true` | Insulin, Hepatitis B, Oxytocin | Postpartum hemorrhage, neonatal vaccination failure |
| **`SC3`** | Respiratory outbreak | `demand_multiplier: {"I04": 1.6, "I10": 1.5}` | Medical Oxygen (D-type), N95 Masks | ICU hypoxemia, respiratory cross-infection |


## Engine v1.1: common random numbers and uncertainty reporting

**Common random numbers.** For each item, `scenario_paths()` generates one 21-day demand path and one supplier-delay draw `u` per run, once, from the seeded generator. Every plan for that item is evaluated on these same paths. A supplier delivery is late in a run when `u < 1 - reliability`, so a less reliable supplier is late in a superset of the runs where a more reliable one is late. Differences between plans therefore come from the plans themselves rather than from sampling noise, and adding stock or a transfer can never increase simulated risk (this is covered by a test).

**Uncertainty reporting.** Each result carries `stockout_runs`, `runs` and `stockout_prob_upper95`, a one-sided 95% upper bound: the exact Clopper-Pearson bound `1 - 0.05^(1/n)` when no stockout is observed (about 0.3% for n = 1,000) and the Wilson bound otherwise. A plan with no simulated stockouts is reported as "0/1,000 runs (95% bound <0.3%)", never as "0% risk". These are model estimates under the stated assumptions, not guarantees.

**Plan accounting.** Each plan records `supplier_order_qty`, `supplier_unit_cost_inr`, `supplier_cost_inr`, each transfer's `qty` and `cost_inr`, `transfer_units`, `transfer_cost_inr`, `total_units` and `total_cost_inr`. The same fields feed the simulation inflows, the recommendation and the purchase order. For `SC1` the recommended bridge is 754 vials x Rs 480 = Rs 361,920 from Himalaya MedSupply plus two transfers (150 + 80 vials, Rs 1,500 each), total Rs 364,920.

**Not modelled.** Supplier capacity limits, minimum order quantities, expiry, competition between hospitals for the same sister-hospital stock, and delay distributions (a late delivery is a flat +3 days). The engine lists these in `assumptions.not_modelled`.

**Hand-checked case (in the test suite).** 100 units, demand exactly 10/day, one supplier with 20-day lead and perfect reliability: stock lasts 10 days, days 11 to 19 are each short by 10 units, so stockout probability is 1.0, expected unmet units 90 and first stockout day 11.
