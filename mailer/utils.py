import os
import re
from typing import List

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def resolve_txt_paths(path: str) -> List[str]:
    """Resolve a path to a list of .txt files.

    If path is a file, returns [path].
    If path is a directory, returns all .txt files found recursively.
    """
    if not path:
        return []
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        found = []
        for root, _dirs, files in os.walk(path):
            for f in sorted(files):
                if f.lower().endswith(".txt"):
                    found.append(os.path.join(root, f))
        return found
    return []


def read_lines_from_paths(paths: List[str]) -> List[str]:
    """Read all non-empty lines from a list of files."""
    lines = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        lines.append(stripped)
        except OSError:
            continue
    return lines


def extract_emails(text: str) -> List[str]:
    """Extract all valid email addresses from a string."""
    return [m.lower() for m in EMAIL_RE.findall(text)]
