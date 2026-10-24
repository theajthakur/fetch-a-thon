import json
import os
import sys

# Ensure local imports work cleanly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sim import run_simulation, format_text

def main():
    candidates = [
        os.path.join(os.path.dirname(__file__), "dataset", "data.json"),
        os.path.join(os.path.dirname(__file__), "..", "dataset", "data.json"),
        "src/dataset/data.json",
        "dataset/data.json",
        "data.json",
    ]
    data_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not data_path:
        print("Error: Could not locate dataset/data.json")
        return

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("=" * 60)
    print("MediTwin Digital Twin Simulation - Hero Scenario (SC1)")
    print("=" * 60)
    res = run_simulation(data, {"scenario": "SC1", "item": "insulin"})
    print(format_text(res))
    print("=" * 60)

if __name__ == "__main__":
    main()
