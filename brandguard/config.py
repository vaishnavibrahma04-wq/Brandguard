"""Fixed policy parameters from the Group 17 proposal (Sections 9-11). ASSUMPTION items are prototype assumptions."""
from __future__ import annotations

TIER_ORDER = ["S1", "S2", "S3", "S4"]
TIER_THRESHOLDS = [("S1", 75), ("S2", 55), ("S3", 30), ("S4", 0)]
CONFIDENCE_THRESHOLD = 0.70

HEAD_COMMS = "Head of Communications"
PR_MANAGER = "PR Manager"
SOCIAL_MGR = "Social Media Manager / Customer Care Lead"
LEGAL = "Legal Contact"

SLA_MINUTES = {"S1": 15, "S2": 60, "S3": 240, "S4": 1440}
ESCALATION_MATRIX = {"S1": HEAD_COMMS, "S2": PR_MANAGER, "S3": SOCIAL_MGR, "S4": None}
APPROVER_LADDER = [SOCIAL_MGR, PR_MANAGER, HEAD_COMMS]
ROLE_SLA_MINUTES = {SOCIAL_MGR: 240, PR_MANAGER: 60, HEAD_COMMS: 15}
REMINDER_GRACE_FRACTION = 0.5

STAGE_AUTONOMY = {
    1: "Fully autonomous", 2: "Act then report", 3: "Act then report", 4: "Act then report",
    5: "Approve (S2,S3); Suggest (S1); Act then report (S4)", 6: "Approve",
    7: "Act then report", 8: "Approve (S1,S2); Act then report (S3,S4)",
}
STAGE_NAMES = {1: "Incident intake", 2: "Listening & Triage", 3: "Severity & Verification",
               4: "Orchestrator decision", 5: "Response & Routing", 6: "Human approval",
               7: "Monitoring & Resolution", 8: "Closure & audit"}

ISSUE_RISK_POINTS = {
    "Safety / Legal": 30, "Misinformation": 25, "Spam / Coordinated Activity": 25,
    "Product Quality": 15, "Service Failure": 10, "Refund / Return": 10,
    "Influencer Criticism": 5, "General Negative Opinion": 5,
}
SOURCE_POINTS = {"MEDIA": 20, "INFLUENTIAL": 20, "CUSTOMER": 10, "ANONYMOUS": 5}
VERIFICATION_POINTS = {"SUPPORTED": 20, "UNVERIFIABLE": 12, "CONTRADICTED": 8, "OPINION": 0}
INCIDENT_TYPES = list(ISSUE_RISK_POINTS)

REACH_TIERS = ["Negligible", "Low", "Moderate", "High"]
KNOWN_PLATFORMS = {"Instagram", "Google Review", "Reddit", "YouTube", "X", "E-commerce Review", "Facebook",
                   "Online News", "Brand Support Inbox", "Marketplace Review", "Twitter", "News Portal"}

MONITOR_WINDOW_HOURS = {"S1": 48, "S2": 24, "S3": 12, "S4": 6}
VELOCITY_ALERT_PCT = 100.0
OUTBOUND_RATE_LIMIT_PER_HOUR = 50
TOOL_RETRIES = 2

GATE_HARD_BLOCK = "HARD_BLOCK_PUBLIC_TEXT"
GATE_S1_SUGGEST = "S1_SUGGEST_ONLY"
GATE_HUMAN_APPROVAL = "HUMAN_APPROVAL_REQUIRED"
GATE_LOW_RISK = "APPROVED_LOW_RISK_ONLY"
GATE_REVIEW = "HUMAN_REVIEW_OR_FAIL_CLOSED"
GATE_MEDIA = "MEDIA_BRIEF_AND_ESCALATE"
GATE_VERIFY = "VERIFY_BEFORE_PUBLIC_ACTION"
NO_SEND_GATES = {GATE_HARD_BLOCK, GATE_S1_SUGGEST, GATE_REVIEW, GATE_MEDIA}
