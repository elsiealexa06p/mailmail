"""Live-HTML-Assembly aus Snippet-Pools.

Statt aus vorgefertigten Ganz-HTMLs zu picken, wird pro Send aus jedem
Snippet-Slot (header, intro, body, outro, footer) zufällig eines
ausgewählt und in dieser Reihenfolge zusammengefügt. Optional wird das
Ergebnis mit einem minimalen HTML-Wrapper umschlossen wenn keiner der
Snippets bereits <html>/<body> enthält.

Kombiniert mit Macros + Spintax + Anti-FP-Transforms ist die Zahl der
möglichen Ausgänge pro Kampagne effektiv unendlich groß.
"""
from __future__ import annotations
import random
import re
from typing import Optional


SLOTS = ("header", "intro", "body", "outro", "footer")


def assemble_html(snippets_by_slot: dict,
                    wrapper: Optional[str] = None,
                    slots: tuple = SLOTS) -> str:
    """snippets_by_slot: {slot_name: [{'content': '...'}, ...]}
    Für jeden Slot der Kandidaten hat wird random.choice gepickt.
    Slot ohne Kandidaten wird übersprungen.

    wrapper: Optional HTML-Rahmen mit `{BODY}`-Placeholder. Wenn None
    und Assembly keinen <html>-Tag enthält, wird ein minimaler Wrapper
    drumrum gesetzt."""
    parts = []
    for slot in slots:
        candidates = snippets_by_slot.get(slot) or []
        if not candidates:
            continue
        chosen = random.choice(candidates)
        content = chosen["content"] if isinstance(chosen, dict) else chosen
        if content:
            parts.append(content)
    body = "\n".join(parts)

    if wrapper:
        return wrapper.replace("{BODY}", body)

    # Auto-Wrap wenn kein Snippet schon <html> mitbringt
    if re.search(r"<html\b", body, re.IGNORECASE):
        return body
    return (
        '<!DOCTYPE html>\n'
        '<html lang="de">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '</head>\n<body style="margin:0;padding:0;font-family:Arial,'
        'Helvetica,sans-serif;color:#222">\n'
        + body +
        '\n</body>\n</html>'
    )


def group_snippets_by_slot(rows) -> dict:
    """Row-Liste (aus DB) → {slot: [{'id':..., 'content':...}, ...]}."""
    grouped = {}
    for row in rows:
        d = dict(row)
        slot = d.get("slot")
        if slot:
            grouped.setdefault(slot, []).append(d)
    return grouped
