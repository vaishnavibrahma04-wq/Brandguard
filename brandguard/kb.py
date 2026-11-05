"""Approved Brand knowledge base (Section 13.2), retrieval, template library and the unsupported-claim guard.
Public text may contain ONLY approved template sentences and approved fact sentences retrieved from this KB.
Doc numbering 01-07 is an assumption (dataset codes are unnamed)."""
from __future__ import annotations
import re
from pathlib import Path

KB_OWNER_DATE = {"owner": "Brand Communications (simulated)", "date": "2026-10-01"}
DOCS = {"01": "Product fact sheets", "02": "Return, refund and service policy", "03": "Approved statements and reply templates",
        "04": "Crisis playbook and escalation matrix", "05": "Past-incident log", "06": "Creator and media guidance",
        "07": "Governance rules and standing prohibitions"}
SUPPORT_CHANNEL = "direct message or the Customer Care contact listed on the Brand's website"

FACTS = [
    {"id": "F-ING-01", "doc": "01", "topics": r"ingredient|contains? (a|an)|formula|undisclosed",
     "text": "The complete ingredient list for each product is published on its label and product page, and the approved product fact sheet lists no additional ingredient."},
    {"id": "F-FORM-01", "doc": "01", "topics": r"secretly changed|quietly changing|changed (every|the) formula|specification",
     "text": "Any change to a product formula or specification is reflected on the label and product page, and the approved fact sheet records no undisclosed change."},
    {"id": "F-REC-01", "doc": "01", "topics": r"recycl",
     "text": "Packaging carries recyclability guidance, and whether it can be recycled depends on local collection systems."},
    {"id": "F-RES-01", "doc": "01", "topics": r"guarantee|identical results|permanent|all skin types|one use",
     "text": "Results vary between individuals and skin types, and the Brand does not guarantee identical or permanent results."},
    {"id": "F-CLN-01", "doc": "01", "topics": r"pollut|every trace|removes every",
     "text": "The cleanser is described as helping to remove everyday impurities, and the Brand does not claim to remove all pollutants in one wash."},
    {"id": "F-RCL-01", "doc": "01", "topics": r"recall|banned|ban\b",
     "text": "Product recall or withdrawal notices are published only on the Brand's official channels, and the approved record shows no notice for current products."},
    {"id": "F-AUTH-01", "doc": "01", "topics": r"fake|counterfeit|marketplace",
     "text": "Authentic products are sold through the authorised channels listed on the Brand's website."},
    {"id": "F-RET-01", "doc": "02", "topics": r"refund|return|cancell",
     "text": "Refund and return timelines are described in the Brand's Return and Refund Policy."},
]

TEMPLATES = {
    "T-S3-01": {"name": "Service / delivery status", "auto_send": False, "sentences": [
        ("Thank you for letting us know, and we're sorry for the experience.", False),
        ("Our team is reviewing your order details and the support history.", False),
        ("We will follow up with you directly through {support_channel}.", False)]},
    "T-S3-02": {"name": "Refund / return status", "auto_send": False, "sentences": [
        ("Thank you for letting us know, and we're sorry for the wait.", False),
        ("Our team is checking the return and refund record for your order.", False),
        ("We will update you directly through {support_channel}.", False),
        ("Refund timelines are described in our Return and Refund Policy.", True)]},
    "T-PQ-01": {"name": "Product-quality complaint", "auto_send": False, "sentences": [
        ("Thank you for flagging this, and we're sorry to hear it.", False),
        ("Please share your order reference and photos through {support_channel} so our team can review the product and batch record.", False),
        ("We will update you once the review is complete.", False),
        ("Please keep the packaging until the review is complete.", True)]},
    "T-MIS-01": {"name": "Bounded correction from approved facts", "auto_send": False, "sentences": [
        ("Thank you for raising this.", False), ("{approved_fact}", False),
        ("For questions, please contact us through {support_channel}.", False)]},
    "T-MIS-02": {"name": "Record-check reply (contradicted case claim)", "auto_send": False, "sentences": [
        ("Thank you for raising this.", False),
        ("Our team has reviewed the order record and will share the recorded status and next steps with you directly through {support_channel}.", False)]},
    "T-E1-01": {"name": "Unverified claim: request details, no factual statement", "auto_send": False, "sentences": [
        ("Thank you for raising this.", False),
        ("We are not able to confirm the details shared at this stage.", False),
        ("Please send the source, batch or order reference through {support_channel} so our team can look into it.", False)]},
    "T-VER-01": {"name": "Verification-only reply (no factual promise)", "auto_send": False, "sentences": [
        ("Thank you for raising this.", False),
        ("Our team is verifying the details against our approved records.", False),
        ("We will update you through {support_channel}.", False)]},
    "T-S1-01": {"name": "Creator / high-reach feedback acknowledgement", "auto_send": False, "sentences": [
        ("Thank you for sharing your feedback.", False),
        ("We would like to understand your concern properly, so our communications team will reach out to you directly.", False),
        ("Your feedback has been noted and shared with the relevant team.", True)]},
    "T-S4-01": {"name": "Low-risk opinion acknowledgement", "auto_send": True, "sentences": [
        ("Thank you for sharing your feedback.", False), ("We have noted it and passed it to the relevant team.", False)]},
    "T-PROMO-01": {"name": "Promotion / checkout issue", "auto_send": False, "sentences": [
        ("Thank you for letting us know about the offer issue.", False),
        ("Our team is checking the checkout and promotion record.", False),
        ("We will update you through {support_channel}.", False)]},
    "T-MEDIA-01": {"name": "Media enquiry acknowledgement", "auto_send": False, "sentences": [
        ("Thank you for your interest.", False), ("Our communications team will respond to media enquiries directly.", False)]},
}
HOLDING_STATEMENT = ("We are aware of the concerns raised and are looking into them carefully. "
                     "We will share verified information as soon as it is available.")

PROHIBITED_PATTERNS = [
    (r"\bour fault\b|\bwe (are|were) (to blame|responsible|liable)\b|\bwe (admit|accept (full )?(responsibility|liability))\b", "admits liability"),
    (r"\b(caused by|due to|because of|root cause|the reason (is|was))\b", "states the cause of an incident"),
    (r"\b(compensat\w*|voucher|free of charge|reimburs\w*|goodwill|we will refund|refund you|full refund|replacement (will|has) been)\b", "offers compensation"),
    (r"\b\d{10}\b|[\w.]+@[\w.]+\.\w+|\border (id|number|no\.?)\s*[:#]?\s*\w*\d{3,}", "reveals personal / order data"),
    (r"\b(we guarantee|guaranteed|100 ?%|permanent(ly)?|always|never)\b", "unsupported guarantee or absolute"),
    (r"\b(delete|take ?down|report(ed)? (your|this) (post|content|account))\b", "content deletion or reporting"),
]

PLAYBOOK = {
    "S1": "Suggest only: case brief, evidence and holding-statement option; Head of Communications decides and acts.",
    "S2": "Draft from approved template; PR Manager sign-off before anything is published.",
    "S3": "Template reply with one-step approval by Social Media Manager / Customer Care Lead.",
    "S4": "Pre-approved low-risk template may be sent and logged; weekly sample audit.",
}


def _slot_regex(sentence: str) -> re.Pattern:
    parts = re.split(r"(\{[a-z_]+\})", sentence)
    rx = "".join("(?P<%s>.+?)" % p[1:-1] if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile("^" + rx + "$", re.S)


class KnowledgeBase:
    def __init__(self):
        self.facts = {f["id"]: f for f in FACTS}
        self.templates = TEMPLATES
        self.docs = DOCS

    def fact_for_claim(self, text: str) -> dict | None:
        t = text.lower()
        for f in FACTS:
            if f["id"] != "F-RET-01" and re.search(f["topics"], t):
                return f
        return None

    def policy_fact(self, text: str) -> dict | None:
        return self.facts["F-RET-01"] if re.search(self.facts["F-RET-01"]["topics"], text.lower()) else None

    def search(self, query: str, k: int = 3) -> list[dict]:
        q = set(re.findall(r"[a-z]{4,}", query.lower()))
        scored = []
        for f in FACTS:
            ov = len(q & set(re.findall(r"[a-z]{4,}", f["text"].lower() + " " + f["topics"])))
            if ov:
                scored.append((ov, f))
        return [f for _, f in sorted(scored, key=lambda x: -x[0])[:k]]

    def template(self, tid: str) -> dict | None:
        return self.templates.get(tid)

    def render(self, tid: str, fact: dict | None = None, include_optional: bool = True) -> dict:
        tpl = self.templates[tid]
        slots = {"support_channel": SUPPORT_CHANNEL, "approved_fact": fact["text"] if fact else ""}
        sents = [t.format(**slots) for t, opt in tpl["sentences"] if include_optional or not opt]
        return {"template_id": tid, "text": " ".join(sents),
                "kb_refs": ["03:" + tid] + ([fact["id"]] if fact else []), "include_optional": include_optional}

    def guard(self, text: str, template_id: str | None, fact_ids: list[str] | None = None, mode: str = "agent") -> dict:
        """mode='agent': strict template/fact traceability + prohibitions. mode='human': prohibitions only (human owns edit)."""
        fact_texts = [self.facts[i]["text"] for i in (fact_ids or []) if i in self.facts]
        unsupported, prohibited, residual = [], [], text
        if mode == "agent":
            tpl = self.templates.get(template_id or "")
            rxs = [_slot_regex(s) for s, _ in tpl["sentences"]] if tpl else []
            residual = ""
            for s in [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]:
                if s in fact_texts:
                    continue
                ok = False
                for rx in rxs:
                    m = rx.match(s)
                    if m:
                        gd = m.groupdict()
                        if gd.get("support_channel", SUPPORT_CHANNEL) != SUPPORT_CHANNEL:
                            continue
                        if "approved_fact" in gd and gd["approved_fact"] not in fact_texts:
                            continue
                        ok = True
                        break
                if not ok:
                    unsupported.append(s)
                    residual += " " + s
        for pat, label in PROHIBITED_PATTERNS:
            if re.search(pat, residual if mode == "agent" else text, re.I):
                prohibited.append(label)
        return {"passed": not unsupported and not prohibited, "unsupported_sentences": unsupported, "prohibited": prohibited, "mode": mode}

    def export_markdown(self, folder: str) -> None:
        from .config import ESCALATION_MATRIX, SLA_MINUTES
        p = Path(folder); p.mkdir(parents=True, exist_ok=True)
        hdr = "Owner: %(owner)s | Date: %(date)s\n\n" % KB_OWNER_DATE
        for fn, title, doc in (("01_product_fact_sheets.md", "01 Product fact sheets", "01"),
                               ("02_return_refund_service_policy.md", "02 Return, refund and service policy", "02")):
            (p / fn).write_text(f"# {title}\n" + hdr + "\n".join(f"- **{f['id']}**: {f['text']}" for f in FACTS if f["doc"] == doc) + "\n")
        body = ["# 03 Approved statements and reply templates\n" + hdr]
        for tid, t in TEMPLATES.items():
            body.append(f"## {tid} - {t['name']}  (auto-send allowed: {t['auto_send']})\n" +
                        "\n".join(f"- {s}{'  _(optional)_' if o else ''}" for s, o in t["sentences"]) + "\n")
        body.append("## T-HOLD-01 - Holding statement (S1 case-brief option only; never auto-sent)\n- " + HOLDING_STATEMENT + "\n")
        (p / "03_templates.md").write_text("\n".join(body))
        (p / "04_playbook_escalation_matrix.md").write_text("# 04 Crisis playbook and escalation matrix\n" + hdr + "\n".join(
            f"- **{k}** approver: {ESCALATION_MATRIX[k] or 'none (template / weekly audit)'}; SLA {SLA_MINUTES[k]} min. {PLAYBOOK[k]}" for k in PLAYBOOK) + "\n")
        (p / "07_governance_prohibitions.md").write_text("# 07 Governance rules and standing prohibitions\n" + hdr +
            "Agents never: admit liability; state the cause of an incident; offer compensation; reveal personal data; "
            "delete or report content; make a public factual statement not supported by the approved knowledge base.\n")
