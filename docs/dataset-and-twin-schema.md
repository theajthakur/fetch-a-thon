# MediTwin Digital Twin Schema & Dataset Reference

> **Data Source**: [`src/dataset/data.json`](../src/dataset/data.json)  
> *Synthetic benchmark modeled after tier-2 Indian district hospital logistics (Delhi-NCR).*

---

## 1. Overview

The MediTwin digital twin maintains a real-time synthetic state of **City General Hospital (H1)** in Greater Noida, Uttar Pradesh, India.

The dataset captures:
- 12 critical medicines, fluids, and surgical supplies spanning cold-chain biologics, life-critical emergency items, surgical consumables, and high-volume analgesics.
- 7 licensed pharmaceutical and medical gas suppliers across Delhi-NCR with modeled delivery reliability scores, lead times, and unit pricing.
- 2 neighboring public healthcare facilities in the district network with available spare inventory and ground courier transfer transit windows.
- Preconfigured scenario presets modeling real-world disruptions (vendor delays, heatwaves, viral respiratory surges).

---

## 2. Hospital Formulary: The 12 Critical Items

Each item in the twin is modeled with baseline consumption statistics and supply contracts:

| ID | Item Name | Unit | Criticality (1-5) | Cold Chain | Current Stock | Daily Demand ($\mu$) | Demand Std ($\sigma$) | Reorder Level | Shelf Life (Days) | Contracted Suppliers (Lead Time, Unit Cost) |
|---|---|---|---|---|---|---|---|---|---|---|
| **`I01`** | Insulin Glargine 100IU/mL | vial | **5** (Life-critical) | **Yes** ($2\text{--}8^\circ\text{C}$) | 900 | 60 | 12 | 600 | 120 | SUP01 (7d, ₹450)<br/>SUP02 (4d, ₹520)<br/>SUP03 (6d, ₹480) |
| **`I02`** | Hepatitis B Vaccine | vial | **4** (High) | **Yes** ($2\text{--}8^\circ\text{C}$) | 400 | 20 | 5 | 250 | 90 | SUP02 (5d, ₹180)<br/>SUP01 (6d, ₹195) |
| **`I03`** | Oxytocin 10IU/mL ampoule | ampoule | **5** (Life-critical) | **Yes** ($2\text{--}8^\circ\text{C}$) | 300 | 15 | 4 | 200 | 100 | SUP03 (5d, ₹22)<br/>SUP01 (6d, ₹25) |
| **`I04`** | Medical Oxygen Cylinder (D-type) | cylinder | **5** (Life-critical) | No | 120 | 14 | 4 | 80 | 3,650 | SUP04 (2d, ₹350)<br/>SUP05 (3d, ₹390) |
| **`I05`** | Adrenaline 1mg/mL ampoule | ampoule | **5** (Life-critical) | No | 500 | 18 | 5 | 250 | 730 | SUP05 (3d, ₹15)<br/>SUP07 (4d, ₹14) |
| **`I06`** | Ceftriaxone 1g injection | vial | **4** (High) | No | 3,000 | 150 | 30 | 1,800 | 730 | SUP07 (4d, ₹28)<br/>SUP05 (3d, ₹31) |
| **`I07`** | Normal Saline 0.9% 500mL | bottle | **4** (High) | No | 4,000 | 260 | 50 | 2,600 | 1,095 | SUP05 (3d, ₹22)<br/>SUP07 (5d, ₹20) |
| **`I08`** | Heparin 5000IU vial | vial | **4** (High) | No | 350 | 22 | 6 | 220 | 730 | SUP01 (5d, ₹95)<br/>SUP07 (6d, ₹88) |
| **`I09`** | Surgical Gloves (box of 100) | box | **3** (Medium) | No | 600 | 30 | 8 | 400 | 1,825 | SUP06 (5d, ₹420)<br/>SUP05 (3d, ₹450) |
| **`I10`** | N95 Respirator Mask | piece | **3** (Medium) | No | 2,000 | 70 | 15 | 1,000 | 1,825 | SUP06 (6d, ₹35)<br/>SUP05 (3d, ₹40) |
| **`I11`** | IV Cannula 20G | piece | **3** (Medium) | No | 3,500 | 120 | 25 | 1,800 | 1,825 | SUP06 (5d, ₹18)<br/>SUP07 (4d, ₹19) |
| **`I12`** | Paracetamol 500mg (strip of 10) | strip | **2** (Standard) | No | 5,000 | 200 | 40 | 2,500 | 1,095 | SUP07 (4d, ₹12)<br/>SUP05 (3d, ₹13) |

---

## 3. Supplier Profiles & Reliability Model

The digital twin includes 7 regional distributors with distinct geographical locations and historical on-time delivery metrics:

| Supplier ID | Supplier Name | Operating City | Reliability ($P_{\text{on-time}}$) | Late Penalty Probability | Cold Chain Certified | Typical Catalog Items |
|---|---|---|---|---|---|---|
| **`SUP01`** | Apex Pharma Distributors | Delhi | 92% | 8% | **Yes** | Insulin, Hepatitis B, Oxytocin, Heparin |
| **`SUP02`** | LifeLine Biologics | Noida | 88% | 12% | **Yes** | Insulin, Hepatitis B |
| **`SUP03`** | Himalaya MedSupply | Chandigarh | 85% | 15% | **Yes** | Insulin, Oxytocin |
| **`SUP04`** | GasPro Industrial Gases | Ghaziabad | 90% | 10% | No | Medical Oxygen |
| **`SUP05`** | CityMed Wholesale | Delhi | 95% | 5% | No | Oxygen, Adrenaline, Ceftriaxone, Saline, Gloves, N95, Paracetamol |
| **`SUP06`** | SafeGuard Surgicals | Meerut | 80% | 20% | No | Gloves, N95 Masks, Cannulas |
| **`SUP07`** | Bharat Generics | Gurugram | 86% | 14% | No | Adrenaline, Ceftriaxone, Saline, Heparin, Cannulas, Paracetamol |

---

## 4. Sister Hospital Network

When commercial suppliers are delayed or out of stock, City General Hospital can initiate emergency transfers from partner public hospitals:

### 4.1. Network Facilities:
- **`H2` - District Hospital Noida**:
  - Distance: ~25 km | Courier Transfer Window: **4 hours**
  - Logistics Cost: ₹1,500 flat per courier dispatch
- **`H3` - Ghaziabad Community Hospital**:
  - Distance: ~40 km | Courier Transfer Window: **6 hours**
  - Logistics Cost: ₹1,500 flat per courier dispatch

### 4.2. Spare Stock Allocation Table:
| Item ID | Item Name | District Hospital Noida (`H2`) Spare Units | Ghaziabad Community Hospital (`H3`) Spare Units | Total Transferrable Units |
|---|---|---|---|---|
| `I01` | Insulin Glargine | 120 | 80 | **200 vials** |
| `I02` | Hepatitis B Vaccine | 50 | 30 | **80 vials** |
| `I03` | Oxytocin 10IU/mL | 40 | 20 | **60 ampoules** |
| `I04` | Medical Oxygen (D-type) | 20 | 15 | **35 cylinders** |
| `I05` | Adrenaline 1mg/mL | 100 | 50 | **150 ampoules** |
| `I06` | Ceftriaxone 1g | 500 | 250 | **750 vials** |
| `I07` | Normal Saline 0.9% | 600 | 400 | **1,000 bottles** |
| `I08` | Heparin 5000IU | 60 | 30 | **90 vials** |
| `I09` | Surgical Gloves | 100 | 50 | **150 boxes** |
| `I10` | N95 Respirators | 300 | 200 | **500 masks** |
| `I11` | IV Cannulas | 500 | 300 | **800 pieces** |
| `I12` | Paracetamol 500mg | 700 | 500 | **1,200 strips** |

---

## 5. Enterprise Integration Guide

To deploy MediTwin in a live healthcare environment, replace the synthetic `data.json` with an ERP adapter.

```
       Live Hospital System
┌─────────────────────────────────┐
│ • Epic / Cerner / SAP Health    │
│ • OpenLMIS (Medical Stores)     │
│ • HL7 FHIR Server (R4/R5)       │
└────────────────┬────────────────┘
                 │ (REST API / Webhooks)
                 ▼
┌─────────────────────────────────┐
│     ERP Data Adapter / ETL      │
│  • Maps inventory levels to I01 │
│  • Calculates rolling μ and σ   │
│  • Fetches live vendor metrics  │
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│     MediTwin Inventory Agent    │
│  • Serves real-time snapshots   │
│  • Fully backward compatible    │
└─────────────────────────────────┘
```

### 5.1. Validation Contract (`validate_data(data)`)
Any custom or live data source must conform to the following schema to pass validation:
1. Root object must contain `"items"` (list) and `"suppliers"` (dict).
2. Each item must contain:
   - `"id"`: Unique string identifier.
   - `"name"`: Descriptive string.
   - `"criticality"`: Integer between 1 and 5.
   - `"stock"`: Non-negative number.
   - `"daily_demand"`: Number strictly $> 0$.
   - `"demand_std"`: Non-negative number.
   - `"suppliers"`: Non-empty list of supplier objects with `"id"`, `"lead_days"`, and `"unit_cost"`.
3. If an incoming payload violates these rules, the Simulation Agent will automatically fall back to the embedded snapshot and log a warning in `results["meta"]["warnings"]`.
