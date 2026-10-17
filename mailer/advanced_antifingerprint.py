"""Advanced Anti-Fingerprint Engine.

Includes all transforms from the base AntiFingerprintEngine plus
structural HTML transforms that alter the DOM tree itself.
Only single-cell table rows are converted to divs (multi-cell rows
are left as tables to avoid display:flex which breaks in Outlook/Gmail).
"""
import re
import random

from .antifingerprint import AntiFingerprintEngine

_TABLE_BLOCK_RE = re.compile(
    r"<table\b([^>]*)>(.*?)</table>",
    re.IGNORECASE | re.DOTALL,
)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b([^>]*)>(.*?)</td>", re.IGNORECASE | re.DOTALL)


class AdvancedAntiFingerprintEngine(AntiFingerprintEngine):
    def __init__(self, enable_classes: bool = True,
                  structure_variation: float = 0.5,
                  pass_through_rate: float = 0.02,
                  light_touch_rate: float = 0.10):
        super().__init__(enable_classes=enable_classes,
                          pass_through_rate=pass_through_rate,
                          light_touch_rate=light_touch_rate)
        self._structure_variation = max(0.0, min(1.0, structure_variation))

    def transform(self, html: str) -> str:
        html = super().transform(html)
        if self._structure_variation > 0:
            html = self._transform_structure(html)
        return html

    def _transform_structure(self, html: str) -> str:
        def _convert_table(match: re.Match) -> str:
            if random.random() > self._structure_variation:
                return match.group(0)

            table_attrs = match.group(1).strip()
            table_content = match.group(2)

            if "<table" in table_content.lower():
                return match.group(0)

            rows = _TR_RE.findall(table_content)
            if not rows:
                return match.group(0)

            for row_content in rows:
                cells = _TD_RE.findall(row_content)
                if len(cells) > 1:
                    return match.group(0)

            style = _extract_style(table_attrs)
            wrapper_style = style if style else "width:100%"
            parts = [f'<div style="{wrapper_style}">']

            for row_content in rows:
                cells = _TD_RE.findall(row_content)
                if cells:
                    cell_attrs, cell_content = cells[0]
                    cell_style = _extract_style(cell_attrs)
                    s = f' style="{cell_style}"' if cell_style else ""
                    parts.append(f"<div{s}>{cell_content}</div>")
                else:
                    parts.append(f"<div>{row_content}</div>")

            parts.append("</div>")
            return "\n".join(parts)

        return _TABLE_BLOCK_RE.sub(_convert_table, html)


def _extract_style(attrs: str) -> str:
    m = re.search(r'style\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
    return m.group(1) if m else ""
