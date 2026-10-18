"""
PDF text fixes for the generated reports.

1) Black boxes (■) in place of letters: reportlab's built-in Helvetica only
   knows Latin-1, so characters such as the non-breaking hyphen (U+2011),
   Greek letters, arrows or maths symbols print as boxes. This module
   registers the bundled DejaVu Sans Condensed (full Unicode, about the width of Helvetica, in backend/fonts/)
   under the names 'Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique' and
   'Helvetica-BoldOblique', so every existing style and table in app.py
   uses it without any other change.

2) Invisible "Severity" label: the severity cell is given a coloured
   BACKGROUND, but a ROWBACKGROUNDS command listed afterwards painted over
   it, leaving white text on a pale background. TableStyle commands are
   re-ordered so row backgrounds are applied first.

Install once at startup:   pdf_text.install(sys.modules[__name__])
"""
import os

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import TableStyle

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
_FILES = {
    'Helvetica': 'DejaVuSansCondensed.ttf',
    'Helvetica-Bold': 'DejaVuSansCondensed-Bold.ttf',
    'Helvetica-Oblique': 'DejaVuSansCondensed-Oblique.ttf',
    'Helvetica-BoldOblique': 'DejaVuSansCondensed-BoldOblique.ttf',
}


def install_fonts():
    paths = {n: os.path.join(FONT_DIR, f) for n, f in _FILES.items()}
    if not all(os.path.exists(p) for p in paths.values()):
        print('pdf_text: fonts/ folder missing - PDF reports keep the built-in font (some symbols may print as boxes)')
        return False
    for name, path in paths.items():
        pdfmetrics.registerFont(TTFont(name, path))
    pdfmetrics.registerFontFamily('Helvetica', normal='Helvetica', bold='Helvetica-Bold',
                                  italic='Helvetica-Oblique', boldItalic='Helvetica-BoldOblique')
    return True


class FixedTableStyle(TableStyle):
    """TableStyle that applies ROWBACKGROUNDS before cell-specific BACKGROUND commands."""
    def __init__(self, cmds=None, parent=None, **kw):
        if cmds:
            cmds = sorted(cmds, key=lambda c: 0 if c and c[0] == 'ROWBACKGROUNDS' else 1)   # stable sort
        super().__init__(cmds, parent, **kw)


def install(app_module=None):
    ok = install_fonts()
    if app_module is not None:
        app_module.TableStyle = FixedTableStyle
    return ok
