# MediTwin Technical Documentation Portal

Welcome to the comprehensive technical documentation for **MediTwin: a Digital Twin for the Medical Supply Chain**, developed for **Fetch-A-Thon 3.0 (GCET Edition)**.

---

## Documentation Index

| Document | Topic & Scope | Target Audience |
|---|---|---|
| 🏛️ **[System Architecture](architecture.md)** | Multi-agent design, orchestration topology, intent extraction dual pipeline, fault tolerance, and sequence diagrams | System architects, judges, evaluators |
| 🎲 **[Simulation Engine Specification](simulation-model.md)** | Mathematical formulation, Monte Carlo algorithm, order sizing equations, fix option mechanics, and Pareto optimization | Operations researchers, data scientists |
| 🤖 **[Agent & Protocol Reference](agent-spec.md)** | Agentverse endpoints, Agent Chat Protocol specs, Pydantic message schemas, Interactive Card payload format, and storage | uAgents & Agentverse engineers |
| 📊 **[Dataset & Twin Schema](dataset-and-twin-schema.md)** | 12 critical formulary items, 7 supplier reliability profiles, sister hospital network, and enterprise ERP integration guide | Domain specialists, hospital IT teams |
| 🚀 **[Deployment & Operations Runbook](deployment-and-run-guide.md)** | Local installation, Agentverse hosting steps, environment variables, startup sequence, and troubleshooting | Operators, DevOps, testers |
| 💳 **[Payment Agent](payment-agent.md)** | Agent Payment Protocol (seller), FET on the Fetch.ai testnet with on-chain verification, Stripe test-mode fallback, configuration and safeguards | uAgents engineers, judges |

---

## Quick Navigation

```
docs/
├── README.md                      # (This portal) Overview & navigation
├── architecture.md                # System topology, orchestration & state management
├── simulation-model.md            # Mathematical formulation & Monte Carlo mechanics
├── agent-spec.md                  # Protocol models, card specifications & handlers
├── dataset-and-twin-schema.md     # Formulary catalog, supplier reliability & ERP integration
└── deployment-and-run-guide.md    # Local & Agentverse setup, testing & troubleshooting
```