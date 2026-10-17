"""Warmup AI — LLM-powered content generation for warmup emails and replies."""
import json
import random
import hashlib
import logging
from typing import Optional

logger = logging.getLogger("bulk.warmup.ai")

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

_cache = {}


def _llm_call(api_url: str, api_key: str, model: str,
              system: str, prompt: str, temperature: float = 0.9) -> str:
    if not HAS_REQUESTS or not api_key:
        return ""
    try:
        resp = _requests.post(api_url, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": 1500,
        }, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        else:
            logger.warning("LLM API %d: %s", resp.status_code, resp.text[:200])
            return ""
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        return ""


def generate_warmup_email(api_url: str, api_key: str, model: str,
                          domain: str, language: str = "de",
                          topic: str = "") -> dict:
    """Generate a unique warmup newsletter email.
    Returns {"subject": "...", "html": "...", "plain": "..."}
    """
    if not api_key:
        return _fallback_email(domain, language)

    topics = [
        "Branchennews und aktuelle Entwicklungen",
        "Produktivitaetstipps fuer den Arbeitsalltag",
        "Marktanalyse und Trends",
        "Best Practices und Erfahrungsberichte",
        "Digitalisierung und Innovation",
        "Fuehrung und Teamarbeit",
        "Finanztipps und Wirtschaftsnachrichten",
        "Nachhaltigkeit im Unternehmen",
        "IT-Sicherheit und Datenschutz",
        "Kundenservice und Kundenbindung",
    ]
    if not topic:
        topic = random.choice(topics)

    lang_name = "Deutsch" if language == "de" else "English"

    system = (f"Du bist ein professioneller Newsletter-Autor. Schreibe auf {lang_name}. "
              f"Erstelle realistische, professionelle Business-Newsletter die wie echte "
              f"Firmen-Newsletter aussehen. Kein Spam-Charakter. Verwende HTML-Formatierung.")

    prompt = (f"Erstelle einen kurzen Business-Newsletter fuer die Domain {domain}. "
              f"Thema: {topic}. "
              f"Format: JSON mit den Keys 'subject' (Betreffzeile, max 60 Zeichen), "
              f"'html' (HTML-Body mit <p>, <h2>, <ul> Tags, 150-300 Woerter), "
              f"'plain' (Plaintext-Version, 100-200 Woerter). "
              f"Der Newsletter soll wie ein echter professioneller Newsletter aussehen. "
              f"Antworte NUR mit dem JSON, kein anderer Text.")

    raw = _llm_call(api_url, api_key, model, system, prompt, temperature=1.0)
    if not raw:
        return _fallback_email(domain, language)

    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("```", 1)[0]
        data = json.loads(raw)
        if "subject" in data and "html" in data:
            if "plain" not in data:
                data["plain"] = data["html"].replace("<p>", "").replace("</p>", "\n")
            return data
    except (json.JSONDecodeError, KeyError):
        pass

    return _fallback_email(domain, language)


def generate_reply(api_url: str, api_key: str, model: str,
                   original_subject: str, original_snippet: str = "",
                   language: str = "de") -> str:
    """Generate a natural, short reply to a newsletter."""
    if not api_key:
        return _fallback_reply(language)

    lang_name = "Deutsch" if language == "de" else "English"
    system = (f"Du bist eine echte Person die auf einen Newsletter antwortet. "
              f"Schreibe auf {lang_name}. Kurz, natuerlich, menschlich. "
              f"1-2 Saetze maximal. Keine Formalitaeten, keine Grussformel. "
              f"Variiere stark zwischen verschiedenen Antwort-Stilen.")

    prompt = (f"Antworte kurz und natuerlich auf diesen Newsletter:\n"
              f"Betreff: {original_subject}\n"
              f"{('Inhalt: ' + original_snippet[:200]) if original_snippet else ''}\n"
              f"Schreibe NUR die Antwort, nichts anderes. 1-2 Saetze.")

    reply = _llm_call(api_url, api_key, model, system, prompt, temperature=1.2)
    if reply:
        reply = reply.strip().strip('"').strip("'")
        if len(reply) > 5:
            return reply

    return _fallback_reply(language)


def generate_batch_replies(api_url: str, api_key: str, model: str,
                           count: int = 10, language: str = "de") -> list:
    """Pre-generate a batch of varied reply templates."""
    if not api_key:
        return _FALLBACK_REPLIES_DE if language == "de" else _FALLBACK_REPLIES_EN

    lang_name = "Deutsch" if language == "de" else "English"
    system = (f"Du generierst kurze, natuerliche E-Mail-Antworten auf {lang_name}. "
              f"Jede Antwort soll einzigartig klingen, wie von verschiedenen echten "
              f"Personen geschrieben. Manche formell, manche casual.")

    prompt = (f"Generiere {count} verschiedene kurze Antworten auf einen Business-Newsletter. "
              f"Jede Antwort 1-2 Saetze, komplett unterschiedliche Stile und Formulierungen. "
              f"Format: JSON Array von Strings. NUR das Array, kein anderer Text.")

    raw = _llm_call(api_url, api_key, model, system, prompt, temperature=1.3)
    if raw:
        try:
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                raw = raw.rsplit("```", 1)[0]
            replies = json.loads(raw)
            if isinstance(replies, list) and len(replies) >= 3:
                return [r for r in replies if isinstance(r, str) and len(r) > 3]
        except (json.JSONDecodeError, TypeError):
            pass

    return _FALLBACK_REPLIES_DE if language == "de" else _FALLBACK_REPLIES_EN


_FALLBACK_REPLIES_DE = [
    "Danke fuer die Info!",
    "Hab ich gesehen, danke.",
    "Interessant, werde ich mir anschauen.",
    "Vielen Dank fuer die Nachricht.",
    "Danke, sehr hilfreich.",
    "Super, danke fuer die Zusendung!",
    "Danke schoen!",
    "Habe ich zur Kenntnis genommen.",
    "Perfekt, danke!",
    "Vielen Dank, sehr nuetzlich.",
    "Guter Artikel, danke fuers Teilen.",
    "Wird weitergeleitet, danke!",
    "Spannend, da lese ich mich mal rein.",
    "Top, genau was ich gesucht habe.",
    "Kurz und knapp: danke!",
]

_FALLBACK_REPLIES_EN = [
    "Thanks for the info!",
    "Got it, thanks.",
    "Interesting, will check it out.",
    "Thanks for sharing.",
    "Very helpful, appreciate it.",
    "Great newsletter, thank you!",
    "Noted, thanks.",
    "Good stuff, thanks for sending.",
    "Will take a look, cheers.",
    "Thanks for keeping us updated!",
]


def _fallback_email(domain: str, language: str = "de") -> dict:
    """Generate a simple fallback email without LLM."""
    seed = hashlib.md5(f"{domain}{random.random()}".encode()).hexdigest()[:8]
    if language == "de":
        subjects = [
            f"Newsletter Update — {domain}",
            f"Neues von {domain}",
            f"Aktuelle Informationen — {domain}",
            f"Ihr Update von {domain}",
            f"News & Tipps — {domain}",
        ]
        html = (f"<h2>Newsletter von {domain}</h2>"
                f"<p>Vielen Dank fuer Ihr Interesse an unseren Neuigkeiten.</p>"
                f"<p>In dieser Ausgabe informieren wir Sie ueber aktuelle Entwicklungen "
                f"und Trends in unserer Branche. Bleiben Sie auf dem Laufenden und "
                f"profitieren Sie von exklusiven Einblicken.</p>"
                f"<p>Wir freuen uns auf Ihr Feedback.</p>"
                f"<p>Mit freundlichen Gruessen<br>Ihr {domain} Team</p>"
                f"<p style='font-size:11px;color:#999'>Ref: {seed}</p>")
    else:
        subjects = [
            f"Newsletter Update — {domain}",
            f"News from {domain}",
            f"Your weekly update — {domain}",
        ]
        html = (f"<h2>Newsletter from {domain}</h2>"
                f"<p>Thank you for your interest in our updates.</p>"
                f"<p>In this edition, we cover the latest developments and trends. "
                f"Stay informed and benefit from exclusive insights.</p>"
                f"<p>We look forward to your feedback.</p>"
                f"<p>Best regards,<br>The {domain} Team</p>"
                f"<p style='font-size:11px;color:#999'>Ref: {seed}</p>")
    return {
        "subject": random.choice(subjects),
        "html": html,
        "plain": html.replace("<p>", "").replace("</p>", "\n").replace("<br>", "\n")
                     .replace("<h2>", "").replace("</h2>", "\n\n"),
    }


def _fallback_reply(language: str = "de") -> str:
    pool = _FALLBACK_REPLIES_DE if language == "de" else _FALLBACK_REPLIES_EN
    return random.choice(pool)
