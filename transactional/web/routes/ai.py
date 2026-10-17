"""AI Assistant — LLM-powered content generation for macros + templates."""
import json
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

logger = logging.getLogger("trans.ai")
router = APIRouter()


def _llm_call(api_url: str, api_key: str, model: str,
              system: str, prompt: str, temperature: float = 0.9) -> str:
    import requests
    try:
        resp = requests.post(api_url, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": 4000,
        }, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        else:
            logger.warning("LLM %d: %s", resp.status_code, resp.text[:200])
            return f"[API Error {resp.status_code}: {resp.text[:100]}]"
    except Exception as e:
        return f"[Error: {e}]"


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()
    macros = [dict(m) for m in db.get_macros(uid)]
    return request.app.state.templates.TemplateResponse(request, "ai.html", {
        "active": "ai", "cfg": cfg, "macros": macros, "db": db,
    })


@router.post("/ai/config")
async def save_ai_config(request: Request,
                         llm_api_url: str = Form(""),
                         llm_api_key: str = Form(""),
                         llm_model: str = Form("")):
    db = request.app.state.db
    db.update_config(
        llm_api_url=llm_api_url.strip() or "https://openrouter.ai/api/v1/chat/completions",
        llm_api_key=llm_api_key.strip(),
        llm_model=llm_model.strip() or "anthropic/claude-sonnet-4-20250514")
    return HTMLResponse('<div class="alert alert-success">Saved!</div>')


@router.post("/ai/test", response_class=HTMLResponse)
async def test_llm(request: Request):
    db = request.app.state.db
    cfg = db.get_config()
    if not cfg.get("llm_api_key"):
        return HTMLResponse('<span style="color:var(--red)">No API key</span>')
    result = _llm_call(cfg["llm_api_url"], cfg["llm_api_key"], cfg["llm_model"],
                        "You are a helpful assistant.", "Say hi in one sentence.", 0.5)
    if result.startswith("["):
        return HTMLResponse(f'<span style="color:var(--red)">{escape(result)}</span>')
    return HTMLResponse(f'<span style="color:var(--green)">&#10003; {escape(result)}</span>')


@router.post("/ai/expand-macro", response_class=HTMLResponse)
async def expand_macro(request: Request,
                       macro_id: int = Form(0),
                       count: int = Form(20),
                       custom_prompt: str = Form("")):
    """Expand a macro's value list using LLM."""
    db = request.app.state.db
    cfg = db.get_config()
    if not cfg.get("llm_api_key"):
        return HTMLResponse('<div class="alert alert-danger">No LLM API key configured.</div>')

    macro = db._conn().execute("SELECT * FROM trans_macros WHERE id=?", (macro_id,)).fetchone()
    if not macro:
        return HTMLResponse('<div class="alert alert-danger">Macro not found.</div>')
    macro = dict(macro)
    existing = [l.strip() for l in (macro.get("values_text") or "").splitlines() if l.strip()]

    system = ("Du bist ein Experte fuer E-Mail-Marketing-Texte. "
              "Erstelle Variationen im exakt gleichen Stil wie die Beispiele. "
              "Antworte NUR mit den neuen Eintraegen, einer pro Zeile. Kein Markdown, keine Nummerierung.")

    examples = "\n".join(existing[:20])
    prompt = f"Hier sind bestehende Eintraege fuer das Macro '{macro['name']}':\n\n{examples}\n\n"
    if custom_prompt.strip():
        prompt += f"Zusaetzliche Anweisung: {custom_prompt.strip()}\n\n"
    prompt += f"Erstelle {count} neue Eintraege im gleichen Stil. NUR die neuen Eintraege, einer pro Zeile."

    result = _llm_call(cfg["llm_api_url"], cfg["llm_api_key"], cfg["llm_model"],
                        system, prompt, 1.0)

    if result.startswith("["):
        return HTMLResponse(f'<div class="alert alert-danger">{escape(result)}</div>')

    new_lines = [l.strip() for l in result.splitlines() if l.strip() and not l.strip().startswith("#")]
    # Clean numbering like "1. " or "- "
    cleaned = []
    for line in new_lines:
        import re
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        line = re.sub(r"^[-*]\s+", "", line)
        line = line.strip('"').strip("'").strip()
        if line:
            cleaned.append(line)

    if not cleaned:
        return HTMLResponse(f'<div class="alert alert-warning">No usable results. Raw:\n<pre>{escape(result)}</pre></div>')

    rows = ""
    for i, line in enumerate(cleaned):
        rows += f'<div style="padding:4px 0;font-size:13px;border-bottom:1px solid var(--border-light)">{escape(line)}</div>'

    return HTMLResponse(
        f'<div style="margin-bottom:10px"><strong>{len(cleaned)} new entries generated</strong></div>'
        f'<div style="max-height:300px;overflow-y:auto;margin-bottom:12px">{rows}</div>'
        f'<form method="post" action="/ai/apply-macro">'
        f'<input type="hidden" name="macro_id" value="{macro_id}">'
        f'<textarea name="new_values" style="display:none">{escape(chr(10).join(cleaned))}</textarea>'
        f'<div class="btn-group">'
        f'<button class="btn btn-success btn-sm">Add All to Macro</button>'
        f'<a href="/ai" class="btn btn-secondary btn-sm">Discard</a>'
        f'</div></form>')


@router.post("/ai/apply-macro")
async def apply_macro(request: Request,
                      macro_id: int = Form(0),
                      new_values: str = Form("")):
    """Append AI-generated values to macro."""
    db = request.app.state.db
    macro = db._conn().execute("SELECT * FROM trans_macros WHERE id=?", (macro_id,)).fetchone()
    if not macro:
        return HTMLResponse('<div class="alert alert-danger">Macro not found.</div>')
    macro = dict(macro)
    existing = macro.get("values_text", "") or ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    db.update_macro(macro_id, existing + new_values.strip(), macro.get("rotate_every", 0))
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/macros", status_code=303)


@router.post("/ai/generate-html", response_class=HTMLResponse)
async def generate_html(request: Request,
                        description: str = Form(""),
                        style: str = Form("professional")):
    """Generate HTML email template from description."""
    db = request.app.state.db
    cfg = db.get_config()
    if not cfg.get("llm_api_key"):
        return HTMLResponse('<div class="alert alert-danger">No LLM API key.</div>')
    if not description.strip():
        return HTMLResponse('<div class="alert alert-warning">Enter a description.</div>')

    system = ("Du bist ein Experte fuer HTML-E-Mail-Templates. "
              "Erstelle saubere, responsive HTML-E-Mail-Vorlagen die in allen Mail-Clients funktionieren. "
              "Verwende Inline-CSS (kein externes CSS). Verwende Tabellen fuer Layout (Outlook-kompatibel). "
              "Antworte NUR mit dem HTML-Code, kein Markdown, keine Erklaerung.")

    prompt = (f"Erstelle ein HTML-E-Mail-Template:\n"
              f"Beschreibung: {description.strip()}\n"
              f"Stil: {style}\n\n"
              f"Verwende diese Platzhalter wo passend:\n"
              f"- {{email_user}} fuer den Empfaengernamen\n"
              f"- {{Logo}} fuer das Logo\n"
              f"- {{RedirectLink}} fuer Links\n"
              f"- Spintax {{Option1|Option2|Option3}} fuer Variationen\n\n"
              f"Antworte NUR mit dem kompletten HTML-Code.")

    result = _llm_call(cfg["llm_api_url"], cfg["llm_api_key"], cfg["llm_model"],
                        system, prompt, 0.8)

    if result.startswith("["):
        return HTMLResponse(f'<div class="alert alert-danger">{escape(result)}</div>')

    # Clean markdown code blocks
    if result.startswith("```"):
        result = result.split("\n", 1)[1] if "\n" in result else result[3:]
        result = result.rsplit("```", 1)[0]

    return HTMLResponse(
        f'<div style="margin-bottom:10px"><strong>Generated HTML Template</strong></div>'
        f'<div class="grid-2" style="align-items:start">'
        f'<div>'
        f'<label>Source</label>'
        f'<textarea id="ai-html-result" rows="15" style="font-family:monospace;font-size:11px;width:100%">{escape(result)}</textarea>'
        f'</div>'
        f'<div>'
        f'<label>Preview</label>'
        f'<iframe srcdoc="{escape(result)}" style="width:100%;height:350px;border:1px solid var(--border);border-radius:var(--radius);background:#fff"></iframe>'
        f'</div></div>'
        f'<form method="post" action="/ai/save-html" style="margin-top:10px">'
        f'<div class="form-row">'
        f'<div><input name="name" placeholder="Template name" required style="margin:0"></div>'
        f'<div class="shrink"><button class="btn btn-success btn-sm">Save as Template</button></div>'
        f'</div>'
        f'<textarea name="html_content" style="display:none">{escape(result)}</textarea>'
        f'</form>')


@router.post("/ai/save-html")
async def save_html(request: Request,
                    name: str = Form(""),
                    html_content: str = Form("")):
    if name.strip() and html_content.strip():
        request.app.state.db.add_template(name.strip(), html_content)
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/templates", status_code=303)
