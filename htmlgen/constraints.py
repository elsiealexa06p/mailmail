"""Constraint engine — reject-and-resample validation."""

import re

_TAG_RE = re.compile(r"<!--\s*tags:\s*(.+?)\s*-->", re.IGNORECASE)


def parse_tags(html: str) -> set[str]:
    """Extract tags from the first-line HTML comment."""
    first_line = html.split("\n", 1)[0]
    m = _TAG_RE.search(first_line)
    if not m:
        return set()
    return {t.strip().lower() for t in m.group(1).split(",")}


def check_constraints(selection: dict[str, dict], constraints: list[dict]) -> bool:
    """Check if a block selection satisfies all constraints.

    selection: {"logo": {"variant": "03", "tags": {"centered", "bold"}}, ...}
    constraints: list of {"if": {...}, "then_not": {...}} rules

    Returns True if all constraints are satisfied (selection is valid).
    """
    for rule in constraints:
        if_clause = rule.get("if", {})
        then_not = rule.get("then_not", {})

        if not _matches(selection, if_clause):
            continue

        if _matches(selection, then_not):
            return False

    return True


def _matches(selection: dict, clause: dict) -> bool:
    """Check if a selection matches a clause."""
    block = clause.get("block", "")
    if block not in selection:
        return False

    entry = selection[block]

    variant_tag = clause.get("variant_tag", "")
    if variant_tag and variant_tag.lower() not in entry.get("tags", set()):
        return False

    variant_id = clause.get("variant", "")
    if variant_id and entry.get("variant") != variant_id:
        return False

    return True
