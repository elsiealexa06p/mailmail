"""Spam Score Checker — Rspamd + SpamAssassin integration."""
import re
import subprocess
import logging
from html import escape

logger = logging.getLogger("trans.spam")

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


def check_rspamd(raw_mime: str, url: str = "http://127.0.0.1:11333/checkv2") -> dict:
    if not HAS_REQUESTS:
        return {"error": "requests library not installed", "checker": "rspamd"}
    try:
        resp = _requests.post(url, data=raw_mime.encode("utf-8"),
                               headers={"Content-Type": "text/plain"}, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            symbols = []
            for name, info in data.get("symbols", {}).items():
                symbols.append({
                    "name": name,
                    "score": info.get("score", 0),
                    "description": info.get("description", ""),
                })
            symbols.sort(key=lambda s: abs(s["score"]), reverse=True)
            return {
                "score": data.get("score", 0),
                "action": data.get("action", "unknown"),
                "threshold": data.get("required_score", 15),
                "symbols": symbols,
                "error": None,
                "checker": "rspamd",
            }
        return {"error": f"Rspamd {resp.status_code}: {resp.text[:200]}", "checker": "rspamd"}
    except Exception as e:
        return {"error": str(e), "checker": "rspamd"}


def check_spamassassin(raw_mime: str) -> dict:
    try:
        result = subprocess.run(
            ["spamc", "-R"],
            input=raw_mime.encode("utf-8"),
            capture_output=True, timeout=30)
        output = result.stdout.decode("utf-8", errors="replace")

        score = 0.0
        threshold = 5.0
        symbols = []

        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith("-") and not line[0:1].isdigit():
                continue
            score_match = re.match(r"^(-?[\d]+\.[\d]+)\s*/\s*([\d]+\.[\d]+)", line)
            if score_match:
                try:
                    score = float(score_match.group(1))
                    threshold = float(score_match.group(2))
                except ValueError:
                    pass
                continue
            rule_match = re.match(r"^\s*(-?[\d]+\.[\d]+)\s+([A-Z][A-Z0-9_]+)\s+(.*)", line)
            if rule_match:
                try:
                    sc = float(rule_match.group(1))
                    symbols.append({
                        "name": rule_match.group(2),
                        "score": sc,
                        "description": rule_match.group(3).strip(),
                    })
                except ValueError:
                    pass

        symbols.sort(key=lambda s: abs(s["score"]), reverse=True)
        action = "no action" if score < threshold else "reject"
        return {
            "score": score, "action": action, "threshold": threshold,
            "symbols": symbols, "error": None, "checker": "spamassassin",
        }
    except FileNotFoundError:
        return {"error": "spamc not found. Install: sudo apt install spamassassin spamc", "checker": "spamassassin"}
    except Exception as e:
        return {"error": str(e), "checker": "spamassassin"}


def check_both(raw_mime: str, rspamd_url: str = "http://127.0.0.1:11333/checkv2") -> list:
    """Run both checkers, return list of results."""
    results = []
    results.append(check_rspamd(raw_mime, rspamd_url))
    results.append(check_spamassassin(raw_mime))
    return results


def check_spam(raw_mime: str, checker: str = "both",
               url: str = "http://127.0.0.1:11333/checkv2") -> list:
    if checker == "both":
        return check_both(raw_mime, url)
    elif checker == "rspamd":
        return [check_rspamd(raw_mime, url)]
    elif checker == "spamassassin":
        return [check_spamassassin(raw_mime)]
    return [{"error": f"Unknown checker: {checker}", "checker": "?"}]


def _format_one(result: dict) -> str:
    checker = result.get("checker", "?")
    if result.get("error"):
        return (f'<div style="margin-bottom:12px">'
                f'<strong>{checker.upper()}</strong>: '
                f'<span style="color:var(--red)">{escape(result["error"])}</span></div>')

    score = result.get("score", 0)
    action = result.get("action", "unknown")
    threshold = result.get("threshold", 15)
    symbols = result.get("symbols", [])

    if score <= 3:
        color = "var(--green)"
        rating = "Good"
    elif score <= 6:
        color = "var(--yellow)"
        rating = "Warning"
    else:
        color = "var(--red)"
        rating = "High Risk"

    action_badge = {
        "no action": "badge-running", "greylist": "badge-paused",
        "add header": "badge-draft", "rewrite subject": "badge-draft",
        "reject": "badge-failed",
    }.get(action, "badge-draft")

    html = f'<div style="margin-bottom:16px;padding:12px;border:1px solid var(--border-light);border-radius:var(--radius)">'
    html += f'<div style="display:flex;gap:16px;align-items:center;margin-bottom:10px">'
    html += f'<strong>{checker.upper()}</strong>'
    html += f'<span style="font-size:24px;font-weight:700;color:{color}">{score:.1f}</span>'
    html += f'<span style="font-size:13px;color:var(--fg2)">/ {threshold:.0f}</span>'
    html += f'<span class="badge {action_badge}">{action}</span>'
    html += f'<span style="font-size:12px;color:var(--fg2)">{rating}</span>'
    html += f'</div>'

    if symbols:
        html += '<table style="font-size:12px;width:100%"><thead><tr>'
        html += '<th style="width:60px">Score</th><th>Rule</th><th>Description</th></tr></thead><tbody>'
        for s in symbols[:15]:
            sc = s["score"]
            sc_color = "var(--red)" if sc > 0 else "var(--green)" if sc < 0 else "var(--fg2)"
            html += (f'<tr><td style="color:{sc_color};font-weight:600">{sc:+.1f}</td>'
                     f'<td style="font-family:monospace;font-size:11px">{escape(s["name"])}</td>'
                     f'<td style="color:var(--fg2);font-size:11px">{escape(s["description"][:60])}</td></tr>')
        if len(symbols) > 15:
            html += f'<tr><td colspan="3" style="color:var(--fg2)">+{len(symbols)-15} more</td></tr>'
        html += '</tbody></table>'
    html += '</div>'
    return html


def format_result_html(results) -> str:
    if isinstance(results, dict):
        results = [results]
    return "".join(_format_one(r) for r in results)
