"""Headless run: process all 400 cases, print KPIs, write outputs/BrandGuard_Run_Results.xlsx and the knowledge-base files.
Usage: python run_evaluation.py [path/to/dataset.xlsx]"""
import json, sys, os
from brandguard.simulation import load_dataset, run_dataset
from brandguard.metrics import compute
import export_run

path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BG_DATASET", "dataset.xlsx")
df = load_dataset(path)
orch, store = run_dataset(df)
m = compute(df, store, orch)
m.pop("eval_rows"); m.pop("type_pr")
print(json.dumps(m, indent=1, default=str))
os.makedirs("outputs", exist_ok=True)
export_run.build(df, orch, "outputs/BrandGuard_Run_Results.xlsx")
orch.kb.export_markdown("knowledge_base")
print("wrote outputs/BrandGuard_Run_Results.xlsx and knowledge_base/")
