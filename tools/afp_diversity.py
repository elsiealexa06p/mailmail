"""Anti-fingerprint diversity report.

Generates N variants from the same source HTML through the OLD engine
(checked out at HEAD~1) and the NEW engine (current HEAD), then prints
metrics that show how detectable the engine's pattern is across the
batch:

  - unique class names per batch
  - share of class names that match the legacy regex \\w+-\\w+-\\d{2}
  - average pairwise Jaccard similarity over word tokens
  - unique inline style strings
  - unique <style>-block shapes

Usage:
    python tools/afp_diversity.py [--n 100]
"""
import argparse
import importlib
import os
import random
import re
import statistics
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

SAMPLE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body>
<table class="wrapper" style="width:100%;padding:10px;background:#fff">
  <tr><td class="hero" style="padding:14px;color:#333;font-size:18px">
    Hello <strong>{Name}</strong>, please <em>review</em> our offer.
  </td></tr>
  <tr><td class="cta" style="padding:12px;background:#0066cc;color:#fff">
    <a href="{RedirectLink}" style="color:#fff;font-weight:bold">Open it</a>
  </td></tr>
  <tr><td class="footer" style="padding:8px;font-size:11px;color:#666">
    <img src="logo.png" border="0" alt="Logo" width="120" height="40">
    <br>You received this because you signed up.
  </td></tr>
</table>
</body></html>
"""

LEGACY_CLASS_RE = re.compile(r"\b[a-z]+-[a-z]+-\d{2}\b")
CLASS_ATTR_RE = re.compile(r'class\s*=\s*"([^"]+)"', re.IGNORECASE)
STYLE_ATTR_RE = re.compile(r'style\s*=\s*"([^"]*)"', re.IGNORECASE)
STYLE_BLOCK_RE = re.compile(r'<style[^>]*>(.*?)</style>', re.IGNORECASE | re.DOTALL)
TOKEN_RE = re.compile(r"\w+")


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a | b) else 0.0


def measure(samples: list) -> dict:
    classes = set()
    legacy_hits = 0
    total_class_uses = 0
    style_strings = set()
    style_blocks = set()
    token_sets = []
    for h in samples:
        for m in CLASS_ATTR_RE.finditer(h):
            for c in m.group(1).split():
                classes.add(c)
                total_class_uses += 1
                if LEGACY_CLASS_RE.fullmatch(c):
                    legacy_hits += 1
        for m in STYLE_ATTR_RE.finditer(h):
            style_strings.add(m.group(1).strip())
        for m in STYLE_BLOCK_RE.finditer(h):
            style_blocks.add(m.group(1).strip()[:80])
        token_sets.append(set(TOKEN_RE.findall(h)))

    # Pairwise Jaccard over a random subset to keep it cheap
    sample = random.sample(token_sets, min(40, len(token_sets)))
    sims = []
    for i in range(len(sample)):
        for j in range(i + 1, len(sample)):
            sims.append(jaccard(sample[i], sample[j]))

    return {
        "n": len(samples),
        "unique_classes": len(classes),
        "total_class_uses": total_class_uses,
        "legacy_class_match_share": (legacy_hits / total_class_uses) if total_class_uses else 0.0,
        "unique_inline_styles": len(style_strings),
        "unique_style_blocks": len(style_blocks),
        "avg_jaccard": statistics.mean(sims) if sims else 0.0,
        "min_jaccard": min(sims) if sims else 0.0,
        "max_jaccard": max(sims) if sims else 0.0,
    }


def run(n: int):
    import mailer.antifingerprint as afp
    importlib.reload(afp)

    random.seed(1337)
    samples = [afp.AntiFingerprintEngine(enable_classes=True).transform(SAMPLE_HTML)
                for _ in range(n)]
    return measure(samples)


def render(label: str, m: dict) -> str:
    return (
        f"\n=== {label} (n={m['n']}) ===\n"
        f"  unique class names                : {m['unique_classes']}\n"
        f"  total class occurrences           : {m['total_class_uses']}\n"
        f"  share matching legacy regex       : {m['legacy_class_match_share']:.1%}\n"
        f"  unique inline style strings       : {m['unique_inline_styles']}\n"
        f"  unique <style> block bodies (80c) : {m['unique_style_blocks']}\n"
        f"  pairwise Jaccard avg/min/max      : "
        f"{m['avg_jaccard']:.3f} / {m['min_jaccard']:.3f} / {m['max_jaccard']:.3f}\n"
        f"  (lower Jaccard = more diverse output)\n"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    args = ap.parse_args()

    print(f"Source HTML: {len(SAMPLE_HTML)} chars, {SAMPLE_HTML.count(chr(10))} lines")
    new = run(args.n)
    print(render("NEW engine (profile-based)", new))


if __name__ == "__main__":
    main()
