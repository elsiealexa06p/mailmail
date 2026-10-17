"""HTML Content Engine — generate randomized email templates from modular blocks."""
import os
import json
import threading
import logging
from pathlib import Path
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger("trans.htmlgen")
router = APIRouter()

_HTMLGEN_BASE = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "htmlgen")))
_OUTPUT_DIR = _HTMLGEN_BASE / "output"
_gen_progress = {"running": False, "total": 0, "done": 0}


def _list_generated() -> list[dict]:
    if not _OUTPUT_DIR.is_dir():
        return []
    return [{"name": f.stem, "html": f.read_text(encoding="utf-8")}
            for f in sorted(_OUTPUT_DIR.glob("*.html"))]


def _count_generated() -> int:
    if not _OUTPUT_DIR.is_dir():
        return 0
    return len(list(_OUTPUT_DIR.glob("*.html")))


@router.get("/htmlgen", response_class=HTMLResponse)
async def htmlgen_page(request: Request):
    from htmlgen.engine import _load_all, BLOCK_NAMES
    from htmlgen.config import load_config

    cfg_path = _HTMLGEN_BASE / "config.yaml"
    cfg = load_config(cfg_path)
    block_variants, layouts = _load_all(_HTMLGEN_BASE)

    blocks_info = []
    for name in BLOCK_NAMES:
        variants = block_variants.get(name, [])
        blocks_info.append({
            "name": name,
            "count": len(variants),
            "enabled": cfg.get("blocks", {}).get(name, True),
        })

    layout_names = [l["name"] for l in layouts]
    gen_count = _count_generated()
    uid = request.state.user["id"]
    db = request.app.state.db
    templates = [dict(t) for t in db.get_templates(uid)]

    return request.app.state.templates.TemplateResponse(request, "htmlgen.html", {
        "active": "htmlgen",
        "blocks_info": blocks_info,
        "layout_names": layout_names,
        "cfg": cfg,
        "generated": [None] * gen_count,
        "gen_count": gen_count,
        "gen_progress": _gen_progress,
        "templates": templates,
    })


@router.post("/htmlgen/generate", response_class=HTMLResponse)
async def generate_templates(request: Request):
    form = await request.form()
    count = max(1, min(int(form.get("count", 100)), 50000))
    selected_layouts = form.getlist("layouts")
    primary_color = form.get("primary_color", "")
    accent_color = form.get("accent_color", "")

    if _gen_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Generation already running.</div>')

    _gen_progress.update(running=True, total=count, done=0)

    def worker():
        try:
            from htmlgen.config import load_config
            from htmlgen.engine import generate_one, _load_all

            cfg_path = _HTMLGEN_BASE / "config.yaml"
            cfg = load_config(cfg_path)

            if primary_color.strip():
                cfg.setdefault("colors", {})["primary"] = [primary_color.strip()]
                from htmlgen.colors import lighten_color
                cfg["colors"]["light_accent_bg"] = [lighten_color(primary_color.strip(), cfg.get("lighten_amount", 0.85))]
            if accent_color.strip():
                cfg.setdefault("colors", {})["accent"] = [accent_color.strip()]

            _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            block_variants, all_layouts = _load_all(_HTMLGEN_BASE)
            if selected_layouts:
                filtered = [l for l in all_layouts if l["name"] in selected_layouts]
                if filtered:
                    all_layouts = filtered
            cache = (block_variants, all_layouts)

            for i in range(1, count + 1):
                html = generate_one(cfg, _HTMLGEN_BASE, _cache=cache)
                (_OUTPUT_DIR / f"template_{i:04d}.html").write_text(html, encoding="utf-8")
                _gen_progress["done"] = i

        except Exception as e:
            logger.error("HTMLGen error: %s", e, exc_info=True)
        finally:
            _gen_progress["running"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return HTMLResponse(
        f'<div class="alert alert-info">Generating {count} templates...</div>'
        f'<div id="hg-progress" hx-get="/htmlgen/status" '
        f'hx-trigger="every 1s" hx-swap="innerHTML"></div>'
    )


@router.get("/htmlgen/status", response_class=HTMLResponse)
async def gen_status(request: Request):
    p = _gen_progress
    if p["running"]:
        done = p["done"]
        total = p["total"]
        pct = int(done / total * 100) if total > 0 else 0
        return HTMLResponse(
            f'<div class="progress" style="margin-bottom:8px">'
            f'<div class="progress-bar" style="width:{pct}%">{done}/{total}</div></div>'
        )
    if p["done"] > 0:
        return HTMLResponse(
            f'<div class="alert alert-success">Done! {p["done"]} templates generated. '
            f'<a href="/htmlgen" style="color:var(--accent)">Reload page</a></div>'
        )
    return HTMLResponse("")


@router.post("/htmlgen/preview", response_class=HTMLResponse)
async def preview_template(request: Request,
                            layout: str = Form(""),
                            primary_color: str = Form(""),
                            accent_color: str = Form("")):
    from htmlgen.config import load_config
    from htmlgen.engine import generate_one, _load_all

    cfg_path = _HTMLGEN_BASE / "config.yaml"
    cfg = load_config(cfg_path)

    if layout and layout != "random":
        cfg["layout"] = layout
    if primary_color.strip():
        cfg.setdefault("colors", {})["primary"] = [primary_color.strip()]
        from htmlgen.colors import lighten_color
        cfg["colors"]["light_accent_bg"] = [lighten_color(primary_color.strip(), cfg.get("lighten_amount", 0.85))]
    if accent_color.strip():
        cfg.setdefault("colors", {})["accent"] = [accent_color.strip()]

    cache = _load_all(_HTMLGEN_BASE)
    html = generate_one(cfg, _HTMLGEN_BASE, _cache=cache)

    escaped = escape(html)
    return HTMLResponse(
        f'<div style="margin-bottom:12px">'
        f'<button class="btn btn-secondary btn-sm" onclick="'
        f"document.getElementById('preview-source').style.display="
        f"document.getElementById('preview-source').style.display==='none'?'block':'none'"
        f'">Toggle Source</button></div>'
        f'<div style="border:1px solid var(--border);border-radius:4px;overflow:hidden;margin-bottom:12px">'
        f'<iframe srcdoc="{escape(html, quote=True)}" style="width:100%;height:600px;border:none"></iframe></div>'
        f'<pre id="preview-source" style="display:none;background:#f5f5f5;padding:12px;border-radius:4px;'
        f'font-size:12px;overflow-x:auto;max-height:400px">{escaped}</pre>'
    )


@router.post("/htmlgen/export")
async def export_templates(request: Request):
    import io
    import zipfile

    generated = _list_generated()
    if not generated:
        return RedirectResponse("/htmlgen", status_code=303)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, tpl in enumerate(generated, 1):
            zf.writestr(f"template_{i:04d}.html", tpl["html"])
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=htmlgen_{len(generated)}.zip"},
    )


@router.post("/htmlgen/load-to-mailer", response_class=HTMLResponse)
async def load_to_mailer(request: Request,
                          load_name: str = Form("HtmlGen"),
                          existing_template_id: int = Form(0)):
    db = request.app.state.db
    uid = request.state.user["id"]
    generated = _list_generated()

    if not generated:
        return HTMLResponse('<div class="alert alert-warning">No templates to load.</div>')

    prefix = load_name.strip() or "HtmlGen"

    if existing_template_id:
        template_id = existing_template_id
    else:
        template_id = db.add_template(f"{prefix} ({len(generated)} HTMLs)", "", uid)

    for i, tpl in enumerate(generated, 1):
        db.add_template_file(template_id, f"{prefix}_{i:04d}.html", tpl["html"])

    return HTMLResponse(
        f'<div class="alert alert-success">{len(generated)} HTMLs loaded into '
        f'<a href="/templates" style="color:var(--accent)">HTML Editor</a>.</div>'
    )


@router.post("/htmlgen/clear")
async def clear_templates(request: Request):
    import shutil
    if _OUTPUT_DIR.is_dir():
        shutil.rmtree(_OUTPUT_DIR)
    return RedirectResponse("/htmlgen", status_code=303)


@router.post("/htmlgen/layout/{name}/delete")
async def delete_layout(request: Request, name: str):
    """Delete one layout file from htmlgen/layouts/. Name is the stem
    (filename without .html). Returns 200 OK on success or a 4xx with
    a short body on refusal so HTMX can render/skip appropriately."""
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    if not safe:
        return HTMLResponse("Invalid name", status_code=400)
    layout_path = _HTMLGEN_BASE / "layouts" / f"{safe}.html"
    if not layout_path.is_file():
        return HTMLResponse("Already gone", status_code=200)
    remaining = list((_HTMLGEN_BASE / "layouts").glob("*.html"))
    if len(remaining) <= 1:
        return HTMLResponse("Cannot delete the last remaining layout", status_code=400)
    layout_path.unlink()
    return HTMLResponse("", status_code=200)


# --- Preview placeholder samples ---
_PREVIEW_PLACEHOLDERS = {
    "{Logo}": "Muster GmbH",
    "{Satz1}": "Sehr geehrte Damen und Herren, hiermit informieren wir Sie über eine wichtige Aktualisierung Ihres Kontos.",
    "{Satz2}": "Bitte überprüfen Sie die nachfolgenden Informationen und bestätigen Sie Ihre Daten.",
    "{Hinweis}": "Dieser Vorgang ist aus sicherheitstechnischen Gründen erforderlich und muss innerhalb der angegebenen Frist abgeschlossen werden.",
    "{FristText1}": "Frist: 15. Mai 2026",
    "{FristText2}": "Nach Ablauf dieser Frist wird Ihr Zugang vorübergehend eingeschränkt.",
    "{RedirectLink}": "#",
    "{Link}": "Jetzt bestätigen",
    "{Ende}": "Max Mustermann\nKundenservice",
    "{Footer1}": "Muster GmbH · Musterstraße 1 · 10115 Berlin",
    "{Footer2}": "Diese E-Mail wurde automatisch generiert. Bitte antworten Sie nicht auf diese Nachricht.",
}


# --- Builder ---

@router.get("/htmlgen/builder", response_class=HTMLResponse)
async def builder_page(request: Request):
    from htmlgen.engine import _load_all, BLOCK_NAMES

    block_variants, layouts = _load_all(_HTMLGEN_BASE)

    all_blocks = {}
    for name in BLOCK_NAMES:
        variants = block_variants.get(name, [])
        all_blocks[name] = [
            {"variant": v["variant"], "tags": sorted(v["tags"])}
            for v in variants
        ]

    layout_names = [l["name"] for l in layouts]

    custom_placeholders = _get_custom_placeholders(request)
    generic_wrappers = _load_generic_wrappers()

    return request.app.state.templates.TemplateResponse(request, "htmlgen_builder.html", {
        "active": "htmlgen",
        "all_blocks": all_blocks,
        "all_blocks_json": json.dumps(all_blocks, ensure_ascii=False),
        "layout_names": layout_names,
        "custom_placeholders": custom_placeholders,
        "custom_placeholders_json": json.dumps(custom_placeholders, ensure_ascii=False),
        "generic_wrappers": generic_wrappers,
        "generic_wrappers_json": json.dumps(generic_wrappers, ensure_ascii=False),
    })


def _get_custom_placeholders(request) -> list[dict]:
    """Collect custom placeholders from DB macros + spintaxes/ folder."""
    placeholders = []

    try:
        db = request.app.state.db
        uid = request.state.user["id"]
        for m in db.get_macros(uid):
            md = dict(m)
            name = md.get("name", "")
            if name:
                placeholders.append({"name": name, "source": "db"})
    except Exception:
        pass

    spintax_dir = _HTMLGEN_BASE.parent / "spintaxes"
    if spintax_dir.is_dir():
        for f in sorted(spintax_dir.glob("*.txt")):
            name = f.stem
            if name and name != ".gitkeep" and not any(p["name"] == name for p in placeholders):
                placeholders.append({"name": name, "source": "file"})

    return placeholders


def _load_generic_wrappers() -> list[dict]:
    """Load generic wrapper block templates from blocks/_generic/."""
    generic_dir = _HTMLGEN_BASE / "blocks" / "_generic"
    if not generic_dir.is_dir():
        return []
    wrappers = []
    for f in sorted(generic_dir.glob("*.html")):
        content = f.read_text(encoding="utf-8")
        first_line = content.split("\n", 1)[0]
        if first_line.strip().startswith("<!--") and "tags:" in first_line.lower():
            content = content.split("\n", 1)[1] if "\n" in content else ""
        wrappers.append({"name": f.stem, "html": content.strip()})
    return wrappers


@router.post("/htmlgen/builder/preview", response_class=HTMLResponse)
async def builder_preview(request: Request,
                           layout: str = Form("card"),
                           primary_color: str = Form("#005eb8"),
                           accent_color: str = Form("#c0392b"),
                           blocks: str = Form("[]")):
    import random
    import re as _re
    from htmlgen.engine import _load_all
    from htmlgen.colors import lighten_color
    from htmlgen.placeholders import resolve_engine_placeholders

    block_list = json.loads(blocks)
    block_variants, layouts = _load_all(_HTMLGEN_BASE)

    layout_html = None
    for l in layouts:
        if l["name"] == layout:
            layout_html = l["html"]
            break
    if not layout_html:
        layout_html = layouts[0]["html"] if layouts else "<p>No layout</p>"

    generic_wrappers = _load_generic_wrappers()

    # Build all rows in stack order
    content_rows = ""
    for entry in block_list:
        name = entry["name"]
        variant_id = entry.get("variant", "random")

        if entry.get("custom") or name not in block_variants:
            ph_name = entry.get("placeholder", name)
            wrapper_name = entry.get("wrapper", "plain")
            wrapper_html = None
            for w in generic_wrappers:
                if w["name"] == wrapper_name:
                    wrapper_html = w["html"]
                    break
            if not wrapper_html:
                wrapper_html = '<p style="margin:0;font-family:Arial,sans-serif;font-size:14px;color:#333;">{CONTENT}</p>'
            block_html = wrapper_html.replace("{CONTENT}", "{" + ph_name + "}")
        else:
            variants = block_variants.get(name, [])
            if not variants:
                continue
            if variant_id == "random":
                chosen = random.choice(variants)
            else:
                chosen = next((v for v in variants if v["variant"] == variant_id), random.choice(variants))
            block_html = chosen["html"]
            first_line = block_html.split("\n", 1)[0]
            if first_line.strip().startswith("<!--") and "tags:" in first_line.lower():
                block_html = block_html.split("\n", 1)[1] if "\n" in block_html else ""

        content_rows += f'<tr><td style="padding:6px 40px 10px;">{block_html}</td></tr>\n'

    # Strip all {BLOCK_*} rows from layout, insert our rows
    result_html = _re.sub(
        r'<tr>\s*<td[^>]*>\s*\{BLOCK_\w+\}\s*</td>\s*</tr>\s*',
        '', layout_html)
    # Also clean any remaining {BLOCK_*} (e.g. in nested tables like ref-in-header)
    result_html = _re.sub(r'\{BLOCK_\w+\}', '', result_html)

    # Insert content rows before the inner </table>
    last_table = result_html.rfind("</table>")
    if last_table > 0:
        second_last = result_html.rfind("</table>", 0, last_table)
        insert_pos = second_last if second_last > 0 else last_table
        result_html = result_html[:insert_pos] + content_rows + result_html[insert_pos:]

    pc = primary_color.strip() or "#005eb8"
    ac = accent_color.strip() or "#c0392b"
    light_bg = lighten_color(pc, 0.85)

    cfg = {
        "colors": {
            "primary": [pc], "accent": [ac],
            "light_accent_bg": [light_bg],
            "footer_bg": ["#2c3e50"], "footer_text": ["#cccccc"],
        },
        "fonts": ["Arial, Helvetica, sans-serif"],
    }
    result_html = resolve_engine_placeholders(result_html, cfg)

    for placeholder, sample in _PREVIEW_PLACEHOLDERS.items():
        result_html = result_html.replace(placeholder, sample)

    import re
    result_html = re.sub(r"\[RANDSTR:\d+:[a-zA-Z0-9\-]+:\w+\]", "XK7BM2", result_html)

    return HTMLResponse(result_html)


@router.post("/htmlgen/builder/save", response_class=HTMLResponse)
async def builder_save(request: Request):
    form = await request.form()
    layout_name = form.get("layout_name", "").strip()
    layout_base = form.get("layout_base", "card")
    blocks_json = form.get("blocks", "[]")

    if not layout_name:
        return HTMLResponse('<div class="alert alert-warning">Enter a layout name.</div>')

    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in layout_name.lower())
    if not safe_name:
        return HTMLResponse('<div class="alert alert-warning">Invalid name.</div>')

    import re as _re
    import random
    from htmlgen.engine import _load_all

    block_list = json.loads(blocks_json)
    block_variants, layouts = _load_all(_HTMLGEN_BASE)
    generic_wrappers = _load_generic_wrappers()

    base_layout = None
    for l in layouts:
        if l["name"] == layout_base:
            base_layout = l["html"]
            break
    if not base_layout:
        return HTMLResponse('<div class="alert alert-danger">Base layout not found.</div>')

    content_rows = ""
    for entry in block_list:
        name = entry["name"]
        variant_id = entry.get("variant", "random")

        if entry.get("custom") or name not in block_variants:
            ph_name = entry.get("placeholder", name)
            wrapper_name = entry.get("wrapper", "plain")
            wrapper_html = None
            for w in generic_wrappers:
                if w["name"] == wrapper_name:
                    wrapper_html = w["html"]
                    break
            if not wrapper_html:
                wrapper_html = '<p style="margin:0;font-family:{font};font-size:14px;color:#333;">{CONTENT}</p>'
            block_html = wrapper_html.replace("{CONTENT}", "{" + ph_name + "}")
        else:
            placeholder = "{BLOCK_" + name.upper() + "}"
            content_rows += f'<tr><td style="padding:6px 40px 10px;">{placeholder}</td></tr>\n'
            continue

        content_rows += f'<tr><td style="padding:6px 40px 10px;">{block_html}</td></tr>\n'

    custom_html = _re.sub(
        r'<tr>\s*<td[^>]*>\s*\{BLOCK_\w+\}\s*</td>\s*</tr>\s*',
        '', base_layout)
    custom_html = _re.sub(r'\{BLOCK_\w+\}', '', custom_html)

    last_table = custom_html.rfind("</table>")
    if last_table > 0:
        second_last = custom_html.rfind("</table>", 0, last_table)
        insert_pos = second_last if second_last > 0 else last_table
        custom_html = custom_html[:insert_pos] + content_rows + custom_html[insert_pos:]

    out_path = _HTMLGEN_BASE / "layouts" / f"{safe_name}.html"
    out_path.write_text(custom_html, encoding="utf-8")

    return HTMLResponse(
        f'<div class="alert alert-success">Layout saved as <code>{safe_name}.html</code>. '
        f'Use it in <a href="/htmlgen" style="color:var(--accent)">HTML Engine</a> to generate templates.</div>'
    )
