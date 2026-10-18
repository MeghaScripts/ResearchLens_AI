"""
report_notice.py -- appends a "Verification status & limitations" page to a
finished PDF report, so a run whose paper verification did NOT complete (or
that dropped papers) says so inside the report itself instead of looking
fully verified. Pure post-processing (PyPDF2 + reportlab, both already
dependencies); it never edits the report generator.
"""
import io
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph as _P, SimpleDocTemplate, Spacer

try:                                   # reuse the glyph cleaner if app.py defined one
    from PyPDF2 import PdfReader, PdfWriter
except ImportError:                    # pragma: no cover
    PdfReader = PdfWriter = None

_MAP = {'\u2010': '-', '\u2011': '-', '\u2012': '-', '\u2212': '-', '\u2192': '->', '\u2265': '>=',
        '\u2264': '<=', '\u2009': ' ', '\u202f': ' ', '\u200b': ''}


def _clean(s):
    out = []
    for ch in str(s):
        ch = _MAP.get(ch, ch)
        try:
            ch.encode('cp1252')
            out.append(ch)
        except UnicodeEncodeError:
            out.append('')
    return ''.join(out)


def notice_lines(verification):
    """verification: the dict app.py returns under 'verification' (or None)."""
    v = verification or {}
    if not v.get('ran'):
        why = v.get('error') or v.get('reason') or 'verification did not run'
        return ('NOT COMPLETED',
                ["Paper verification did not finish for this run (" + str(why) + ").",
                 "None of the papers listed in this report were checked for authenticity "
                 "(identifier resolution, cross-source agreement, retraction status).",
                 "The research gaps are AI-generated suggestions based on those unchecked papers. "
                 "Treat them as candidates to verify, not as established findings."])
    lines = []
    removed = v.get('rejected_removed_from_analysis')
    if removed:
        lines.append(f"{removed} paper(s) were rejected by verification and excluded from the analysis.")
    if v.get('all_rejected_kept_unfiltered'):
        lines.append("Every paper failed verification, so the unfiltered set was used. Treat results with caution.")
    lines.append("Verification completed. A passing paper is one with no evidence of a problem; "
                 "it does not mean its claims are correct.")
    lines.append("Research gaps remain AI-generated judgments: verify each one against the cited papers "
                 "before relying on it.")
    return ('COMPLETED', lines)


def append_verification_notice(pdf_path, verification):
    """Append the notice page to pdf_path in place. Returns True on success."""
    if PdfReader is None or not os.path.isfile(pdf_path):
        return False
    status, lines = notice_lines(verification)
    ss = getSampleStyleSheet()
    h = ParagraphStyle('NH', parent=ss['Heading1'], fontSize=16, textColor=colors.HexColor('#16213e'), spaceAfter=10)
    badge = ParagraphStyle('NB', parent=ss['Normal'], fontSize=11, leading=15, spaceAfter=10,
                           textColor=colors.white,
                           backColor=colors.HexColor('#c0392b' if status != 'COMPLETED' else '#1e8449'),
                           borderPadding=(6, 8, 6, 8))
    body = ParagraphStyle('NP', parent=ss['Normal'], fontSize=10.5, leading=15, spaceAfter=8)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=0.85 * inch, rightMargin=0.85 * inch,
                            topMargin=0.85 * inch, bottomMargin=0.85 * inch)
    story = [_P('Verification Status & Limitations', h),
             _P(f'<b>Paper verification: {status}</b>', badge), Spacer(1, 6)]
    story += [_P(_clean(l), body) for l in lines]
    doc.build(story)
    buf.seek(0)
    writer = PdfWriter()
    for page in PdfReader(pdf_path).pages:
        writer.add_page(page)
    for page in PdfReader(buf).pages:
        writer.add_page(page)
    tmp = pdf_path + '.tmp'
    with open(tmp, 'wb') as f:
        writer.write(f)
    os.replace(tmp, pdf_path)
    return True
