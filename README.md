# BrandGuard: governed multi-agent brand-incident workflow (Group 17)

Working prototype of the proposal "Agentic AI for Brand Reputation Incident Management", run on the 400-case synthetic dataset.
It implements the eight-stage workflow, the S1-S4 rubric and overrides, the seven exception paths (E1-E7, plus the E8/E9 variants),
the low-confidence review rule, approval-timeout escalation and the restriction on unsupported factual claims, as enforced code.

## Run it
    pip install -r requirements.txt
    export BG_DATASET=/path/to/Brand_Reputation_Incident_Dataset_400_Cases_CROSSWALK_REPAIRED_CLEAN_FINAL_v2.xlsx
    python app.py                    # console at http://127.0.0.1:5000
    python run_evaluation.py $BG_DATASET   # headless run, writes outputs/BrandGuard_Run_Results.xlsx
    python -m unittest tests.test_brandguard -v

## How the proposal maps to the code
| Proposal | Where |
|---|---|
| 1 Incident intake (validate required fields) | `brandguard/intake.py` |
| 2 Listening & Triage Agent (classify, entities, duplicates, injection / coordination) | `brandguard/triage.py` |
| 3 Severity & Verification Agent (evidence retrieval, fixed rubric, overrides) | `verification.py`, `severity.py`, `Orchestrator._a_score` |
| 4 Orchestrator decision (gate, approver, template, autonomy) | `orchestrator.py` (plan-act-observe loop), `response.decide` |
| 5 Response & Routing Agent (template draft, approval package) | `response.py`, `kb.py` (templates, claim guard) |
| 6 Human approval (approve / edit / reject / escalate, SLA, timeout) | `approval.py`, console Approvals tab |
| 7 Monitoring & Resolution Agent (observations, re-planning) | `Orchestrator.observe`, `_reevaluate`, `_apply_event` |
| 8 Closure & audit | `monitoring.py`, `Orchestrator.try_close`, decision log in SQLite |

Design rules worth knowing:
* Severity, tier, approver and permitted actions are rules, never model output. The classifier is swappable (`RuleClassifier`, or `ClaudeClassifier` which validates JSON and falls back to rules).
* `Outbox.send` is the only path for public text. It refuses on: pause, no-send gate, active exception, failed claim guard, missing human approval (except S4 pre-approved templates), rate limit.
* Ground-truth columns are never passed to the agents (`simulation.raw_from_row`); `evaluate.py` reads them only after the run.
* Every plan step, score, override, tool call, draft, approval and send is written to `decision_log` with a timestamp and role.

## Results on the 400-case dataset (simulated approvers)
See `outputs/BrandGuard_Run_Results.xlsx` (KPI formulas are live over the Case_Results sheet) or the Results tab.

## Known limits (read before presenting)
* **Development-set accuracy.** The classifier cues and rules were refined while looking at this dataset, so type and severity accuracy are not held-out figures. The rubric weights and thresholds were not changed.
* **Synthetic and templated text.** Many dataset texts are formulaic, which flatters a rule-based classifier. Real code-mixed and sarcastic text will do worse; that is what the confidence threshold is for.
* **Simulated environment.** The evidence connector reads the dataset's evidence fields; approvers are deterministic simulated people. SLA adherence and S4 straight-through are therefore properties of the simulation, not field measurements.
* **Efficiency KPIs need your manual baseline.** Time-to-triage, touchpoint and analyst-time reductions cannot be claimed until the team times the manual run (sheet `Baseline_Inputs`). The agent-side compute time is only milliseconds and excludes real LLM and network latency.
* **Zero-mention cases.** Four dataset cases (N08, N09, N16, N22) have 0 mentions in both windows. The dataset formula scores them as 0% growth, but its Data_Dictionary says undefined velocity must go to human review, so the system withholds scoring and asks a person. They count as misses in tier accuracy.
* Document numbers 01-07 in the knowledge base are assumed (the dataset uses unnamed codes).
* Not built: Copilot Studio / Google Sheets / Gmail wiring from the proposal. This is a standalone Python implementation of the same logic; the same rules could be ported.


## The console (redesigned)

- **Dashboard**: four outcome tiles (needs your decision, escalated, reply sent/monitoring, resolved), decisions waiting on you, recent incidents and an activity feed.
- **New incident** and **Upload complaints** are always in the top bar. Upload accepts .csv/.xlsx; only the complaint text is required (use *Download the template*). Missing platform/author/reach fall back to the cautious defaults (Not stated / Anonymous Account / Low).
- **Case page** is one continuous flow: Received, Checked, Decision, Follow-up, Closed, with the next action at the top.
- **Testing** (bottom of the sidebar) holds everything simulated: sample incidents from the 400-case workbook, the full evaluation and Excel download, simulated clock, and Clear all.
- The Automation switch pauses all sending.

Run `python -m unittest discover -s tests` (40 tests).
