"""Stage 3a - Verification via the (simulated) evidence connector. Connector failure (E6) is distinct from 'no evidence'."""
from __future__ import annotations
from dataclasses import dataclass


class ConnectorError(Exception):
    pass


@dataclass
class Evidence:
    status: str
    ref: str
    note: str = ""


class SimulatedEvidenceConnector:
    """table maps case_id -> (availability, basis) from the dataset's evidence fields (environment, not ground-truth labels)."""

    def __init__(self, table: dict[str, tuple[str, str]], outage: set[str] | None = None):
        self.table, self.outage, self.calls = table, outage or set(), 0

    def fetch(self, case_id: str) -> Evidence:
        self.calls += 1
        avail, basis = self.table.get(case_id, ("", ""))
        a = avail.lower()
        if "unavailable to the agent" in a or case_id in self.outage:
            raise ConnectorError("evidence connector unavailable")
        if "contradicts" in a:
            return Evidence("CONTRADICTED", basis, "approved facts contradict the claim")
        if "supports" in a:
            return Evidence("SUPPORTED", basis, "approved/simulated record supports the claim")
        if "not applicable" in a:
            return Evidence("OPINION", basis, "subjective opinion; no factual claim to verify")
        if "governance rule" in a:
            return Evidence("CONTRADICTED" if "contradicts" in basis.lower() else "UNVERIFIABLE", basis,
                            "no factual evidence in post; governance policy applied")
        if "partial" in a:
            return Evidence("UNVERIFIABLE", basis, "source signal present but approved evidence insufficient")
        return Evidence("UNVERIFIABLE", basis, "no evidence record found")
