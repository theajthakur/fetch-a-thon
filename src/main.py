"""MediTwin CLI: runs the simulation engine embedded in src/agents/simulation-agent.py (standard library only).
Examples:
  python src/main.py                                  # hero scenario SC1, focus insulin
  python src/main.py --scenario none --item oxygen    # baseline
  python src/main.py --json                           # full JSON result
"""
import argparse
import importlib.util
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_FILE = os.path.join(HERE, "agents", "simulation-agent.py")


def load_engine():
    """Import the hosted agent file without uagents installed (agent decorators become no-ops)."""
    if "uagents" not in sys.modules:
        stub = types.ModuleType("uagents")

        class Model:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Agent:
            def __init__(self, *a, **kw):
                pass

            def __getattr__(self, name):
                return lambda *a, **kw: (lambda f: f)

        stub.Model, stub.Agent, stub.Context = Model, Agent, object
        sys.modules["uagents"] = stub
    spec = importlib.util.spec_from_file_location("simulation_agent", AGENT_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_scenario(s):
    if s is None or s.lower() in ("none", "baseline", ""):
        return None
    return json.loads(s) if s.strip().startswith("{") else s


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the MediTwin simulation engine")
    ap.add_argument("--scenario", default="SC1", help="SC1 | SC2 | SC3 | none | JSON object")
    ap.add_argument("--item", default="insulin", help="focus item (fuzzy match)")
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", action="store_true", help="print the full JSON result")
    a = ap.parse_args(argv)
    eng = load_engine()
    res = eng.run_simulation(eng.DATA, {"scenario": parse_scenario(a.scenario), "item": a.item,
                                        "runs": a.runs, "seed": a.seed})
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else eng.format_text(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
