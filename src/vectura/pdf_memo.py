"""Executive dispute memo PDF (ReportLab, BSD-licensed)."""
from __future__ import annotations

from io import BytesIO
from typing import List, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .charge_clock_engine import ReconciliationResult


def build_memo(result: ReconciliationResult, documents: Optional[List[dict]] = None) -> bytes:
    """PDF listing every line with variance > 0 with its evidence references."""
    disputed = [li for li in result.line_items if li.variance > 0]
    st = getSampleStyleSheet()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title=f"Dispute memo {result.movement_id}")
    flow: List = [
        Paragraph("Dispute Memo", st["Title"]),
        Paragraph(f"Movement <b>{escape(result.movement_id)}</b> · stage {result.stage} · "
                  f"proof pack {escape(result.audit_proof_pack_id or '-')}", st["Normal"]),
        Paragraph(f"Claimed ${result.total_claimed:,.2f} · supported ${result.total_supported:,.2f} · "
                  f"<b>disputed ${result.total_variance:,.2f}</b>", st["Normal"]),
        Spacer(1, 10),
    ]
    for a in result.fraud_alerts:
        flow.append(Paragraph(f"<font color='red'><b>ALERT</b></font> {escape(a)}", st["Normal"]))
    rows = [["Code", "Claimed", "Supported", "Variance", "Action"]] + [
        [li.charge_code, f"${li.amount_claimed:,.2f}", f"${li.amount_supported:,.2f}",
         f"${li.variance:,.2f}", li.recommended_action.value] for li in disputed]
    t = Table(rows, hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                           ("GRID", (0, 0), (-1, -1), 0.4, colors.grey)]))
    flow += [Spacer(1, 8), t, Spacer(1, 10)]
    for li in disputed:
        flow.append(Paragraph(f"<b>{escape(li.charge_code)} – {escape(li.description)}</b>", st["Heading4"]))
        flow.append(Paragraph(escape(li.findings), st["Normal"]))
        if li.clause:
            flow.append(Paragraph(f"<b>Contract clause {escape(li.clause)}</b>", st["Normal"]))
        for fact in li.evidence_facts:
            flow.append(Paragraph(f"fact: {escape(fact)}", st["Code"]))
        for ref in li.proof_references:
            flow.append(Paragraph(f"evidence: {escape(ref)}", st["Code"]))
    if documents:
        flow.append(Paragraph("Attached evidence", st["Heading3"]))
        for d in documents:
            flow.append(Paragraph(escape(f"{d['kind']}: {d['filename']} (sha256 {d['sha256'][:16]}..., {d['size_bytes']} bytes, "
                                         f"uploaded by {d['uploaded_by']})"), st["Code"]))
    doc.build(flow)
    return buf.getvalue()
