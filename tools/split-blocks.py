#!/usr/bin/env python3
"""Splitter für htmlgen-Bausteine aus einem LLM-Bulk-Output.

Erwartetes Input-Format (eine .md/.txt Datei):

    ### <slot>/<nr>.html
    ```html
    <!-- tags: ... -->
    <html snippet>
    ```

    ### <slot>/<nr>.html
    ...

Legt jeden Baustein in `htmlgen/blocks/<slot>/<nr>.html` ab. Existierende
Files werden übersprungen (mit --force überschrieben).

Usage:
    python3 tools/split-blocks.py <input-file> [--force] [--dry-run]

Beispiel:
    python3 tools/split-blocks.py ~/Downloads/blocks.md
"""
import argparse
import os
import re
import sys

# Wo die Bausteine hin sollen — relativ zum Repo-Root (Script liegt in tools/)
BLOCKS_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "htmlgen", "blocks"))

VALID_SLOTS = {"logo", "gruss", "frist", "link", "footer",
                "hinweis", "satz", "referenz"}

# Header: "### <slot>/<nr>.html"
_HDR = re.compile(r"^###\s+([a-z]+)/(\d+)\.html\s*$", re.MULTILINE)


def parse_blocks(text: str) -> list:
    """Yields (slot, nr, content). content ist der HTML-Body ohne
    fenced-code-block Wrapper."""
    out = []
    # Alle Headers finden mit Positionen
    matches = list(_HDR.finditer(text))
    for i, m in enumerate(matches):
        slot = m.group(1)
        nr = m.group(2).zfill(2)
        # Content ist zwischen diesem Header und dem nächsten (oder EOF)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        # ```html ... ``` extrahieren
        code = re.search(r"```(?:html)?\s*\n(.*?)```", chunk, re.DOTALL)
        if not code:
            print(f"  ⚠ {slot}/{nr}: kein ```html-Block gefunden, skip",
                    file=sys.stderr)
            continue
        content = code.group(1).rstrip() + "\n"
        out.append((slot, nr, content))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="LLM-Output-Datei (.md/.txt)")
    ap.add_argument("--force", action="store_true",
                     help="Existierende Files überschreiben")
    ap.add_argument("--dry-run", action="store_true",
                     help="Nur zeigen was geschrieben würde")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as fh:
        text = fh.read()

    blocks = parse_blocks(text)
    if not blocks:
        print("Keine Bausteine gefunden. Format prüfen: '### slot/NN.html' + ```html-Block```")
        sys.exit(1)

    print(f"Gefunden: {len(blocks)} Bausteine")
    by_slot = {}
    for slot, nr, _c in blocks:
        by_slot.setdefault(slot, []).append(nr)
    for slot in sorted(by_slot):
        print(f"  {slot}: {len(by_slot[slot])} → {', '.join(by_slot[slot])}")

    invalid = [s for s in by_slot if s not in VALID_SLOTS]
    if invalid:
        print(f"\n⚠ Unbekannte Slots (werden trotzdem geschrieben): {invalid}",
                file=sys.stderr)
        print(f"   Erlaubt wäre: {sorted(VALID_SLOTS)}", file=sys.stderr)

    written = skipped = 0
    for slot, nr, content in blocks:
        slot_dir = os.path.join(BLOCKS_ROOT, slot)
        os.makedirs(slot_dir, exist_ok=True)
        dest = os.path.join(slot_dir, f"{nr}.html")
        if os.path.exists(dest) and not args.force:
            print(f"  SKIP {slot}/{nr}.html (existiert — --force zum Überschreiben)")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  DRY {slot}/{nr}.html ({len(content)} bytes)")
        else:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
            print(f"  ✓ {slot}/{nr}.html")
        written += 1
    print(f"\nFertig: {written} geschrieben, {skipped} übersprungen.")
    print(f"Ziel-Ordner: {BLOCKS_ROOT}")


if __name__ == "__main__":
    main()
