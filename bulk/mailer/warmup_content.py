"""Warmup Content Pool — deutsche Werbe-/Newsletter-Templates mit Spintax.
Wird verwendet wenn kein LLM-API-Key konfiguriert ist ODER wenn der
LLM-Call scheitert. Jede Variante wird durch Spintax + Random-Sender
leicht einzigartig."""
import re
import random
import secrets
from datetime import datetime


# 30+ Absender-Namen (deutsche + international)
SENDER_NAMES = [
    "Team Marketing", "Sales-Team", "Kundenservice", "Newsletter-Team",
    "Vertrieb", "Support", "Info-Team", "Marketing-Abteilung",
    "Anna Schmidt", "Thomas Weber", "Julia Bauer", "Michael Fischer",
    "Sabine Müller", "Andreas Klein", "Katrin Wagner", "Stefan Hoffmann",
    "Nicole Schulz", "Markus Becker", "Christian Wolf", "Petra Schwarz",
    "Daniel Schneider", "Melanie Braun", "Vertriebsservice", "Marketing",
    "Kundenbetreuung", "Beratung", "Service-Team", "Business-Team",
]


# 40+ Werbe-/Newsletter-Templates mit Spintax {a|b|c} und Placeholder {name}
# Struktur: (subject_spintax, body_html_spintax)
TEMPLATES = [
    (
        "{Newsletter|Neuigkeiten|Info} {für Sie|zu Ihrem Konto|aus unserem Haus} — {KW|Woche|Ausgabe} {kw}",
        """<p>{Guten Tag|Hallo|Sehr geehrte Damen und Herren},</p>
        <p>{wir möchten Sie kurz über|hier ein kurzer Überblick zu|kurze Info zu} {unsere aktuellen Themen|die neuesten Entwicklungen|die wichtigsten Punkte} {informieren|auf dem Laufenden halten}:</p>
        <ul>
            <li>{Neue Angebote|Aktualisierte Konditionen|Neue Produktvarianten} {ab sofort verfügbar|in unserem Sortiment|jetzt online}</li>
            <li>{Termine|Wichtige Fristen|Anmeldeschluss} {für kommende Veranstaltungen|zu beachten|für dieses Quartal}</li>
            <li>{Feedback|Rückmeldungen|Erfahrungsberichte} {unserer Kunden|aus dem letzten Quartal|zu unserer Beratung}</li>
        </ul>
        <p>{Bei Rückfragen|Für Details|Bei Interesse} {stehen wir gerne zur Verfügung|einfach antworten|melden Sie sich jederzeit}.</p>
        <p>{Freundliche Grüße|Beste Grüße|Viele Grüße},<br>{sender}</p>"""
    ),
    (
        "{Angebot|Aktion|Exklusiv} {für Bestandskunden|im {monat}|jetzt gültig}",
        """<p>{Hallo|Guten Tag},</p>
        <p>{im Rahmen unserer|als Teil unserer|während unserer} {aktuellen Aktion|neuen Kampagne|Sonderaktion} {möchten wir Ihnen|dürfen wir Ihnen|freuen wir uns Ihnen} {folgende Konditionen|ein besonderes Angebot|Preisvorteile} {zukommen lassen|anbieten|präsentieren}:</p>
        <p><strong>{Nur bis zum|Gültig bis|Aktion läuft bis} {datum}</strong> {erhalten Sie|profitieren Sie von|sichern Sie sich} {vergünstigte Konditionen|attraktive Rabatte|besondere Vorteile} {auf unsere Produkte|bei allen Bestellungen|in unserem Sortiment}.</p>
        <p>{Details|Weitere Infos|Alle Bedingungen} {gerne auf Nachfrage|senden wir Ihnen zu|finden Sie im Anhang}.</p>
        <p>{Beste Grüße|Freundliche Grüße},<br>{sender}</p>"""
    ),
    (
        "{Update|Nachricht} {zu Ihrer letzten Anfrage|zum Vorgang|zu unserem Gespräch}",
        """<p>{Sehr geehrte Damen und Herren|Guten Tag},</p>
        <p>{im Anschluss an|bezugnehmend auf|zu} {Ihre kürzliche Anfrage|unser letztes Telefonat|die zuvor besprochenen Punkte} {möchte ich Ihnen|senden wir Ihnen|erhalten Sie} {die zugesagten Informationen|die relevanten Unterlagen|eine Zusammenfassung}.</p>
        <p>{Kurzüberblick|Wichtigste Punkte|Zusammenfassung}:</p>
        <ol>
            <li>{Der Zeitplan|Der Ablauf|Das weitere Vorgehen} {wurde angepasst|ist finalisiert|wurde bestätigt}</li>
            <li>{Die Konditionen|Die Rahmenbedingungen|Die Preise} {sind gleichgeblieben|wurden aktualisiert|bleiben wie besprochen}</li>
            <li>{Wir bitten|Freuen uns} {um Ihre Rückmeldung|auf Ihre Antwort|über kurzes Feedback}</li>
        </ol>
        <p>{Beste Grüße|Freundliche Grüße|Mit freundlichen Grüßen},<br>{sender}</p>"""
    ),
    (
        "{Erinnerung|Reminder}: {Termin|Deadline|Frist} {am|bis} {datum}",
        """<p>{Guten Tag|Hallo},</p>
        <p>{kurze|freundliche} {Erinnerung|Info}: {der von uns vereinbarte|der geplante|unser gemeinsamer} {Termin|Meeting-Slot|Call} {steht am|ist auf} {datum} {geplant|terminiert}.</p>
        <p>{Bitte bestätigen Sie|Kurze Rückmeldung wäre klasse|Falls sich etwas ändert} {kurz per E-Mail|damit wir planen können|bitte Bescheid geben}.</p>
        <p>{Beste Grüße|Viele Grüße},<br>{sender}</p>"""
    ),
    (
        "{Ihre|Deine|Aktuelle} {Bestellung|Rechnung|Auftrag} #{ordernum}",
        """<p>{Hallo|Guten Tag},</p>
        <p>{anbei|beigefügt|im Anhang} {finden Sie|erhalten Sie|senden wir Ihnen} {die Rechnung|den Auftragsbestätigung|die Unterlagen} zu {Ihrer|dem} {Bestellung|Auftrag} <strong>#{ordernum}</strong> {vom|per} {datum}.</p>
        <p>{Zahlbar innerhalb von|Zahlungsziel|Rechnungsbetrag fällig binnen} 14 Tagen {ohne Abzug|per Überweisung|auf das genannte Konto}.</p>
        <p>{Bei Rückfragen|Fragen|Falls etwas unklar ist} {gerne melden|einfach antworten|jederzeit anrufen}.</p>
        <p>{Freundliche Grüße|Beste Grüße},<br>{sender}</p>"""
    ),
    (
        "{Empfehlung|Tipp|Hinweis}: {Neuer Artikel|Blog-Beitrag|Fachartikel} zum Thema {topic}",
        """<p>{Hallo|Guten Tag},</p>
        <p>{vielleicht interessant|womöglich relevant|könnte für Sie spannend sein}: {unser neuer|ein aktueller|frisch veröffentlichter} {Fachartikel|Blog-Beitrag|Ratgeber} zu {topic}.</p>
        <p>{Kurzüberblick|Kerninhalte|Themenschwerpunkte}:</p>
        <ul>
            <li>{Praxisbeispiele|Konkrete Anwendungsfälle|Erfahrungswerte}</li>
            <li>{Best Practices|Empfehlungen|Vorgehensweise}</li>
            <li>{Häufige Fehler|Fallstricke|Was zu vermeiden ist}</li>
        </ul>
        <p>{Feedback willkommen|Rückmeldungen gerne|Bei Fragen einfach antworten}.</p>
        <p>{Grüße|Viele Grüße},<br>{sender}</p>"""
    ),
    (
        "{Zusammenfassung|Bericht|Report}: {KW {kw}|{monat} {jahr}|Q{quartal}/{jahr}}",
        """<p>{Guten Tag|Hallo zusammen},</p>
        <p>{unsere Übersicht|die Zusammenfassung|der aktuelle Bericht} {zur vergangenen Woche|zum letzten Monat|zum Quartal}:</p>
        <ul>
            <li>{Umsatz|Kennzahlen|Highlights}: {leicht über|im Rahmen der|deutlich über} {Plan|Erwartung|Zielsetzung}</li>
            <li>{Neukunden|Anfragen|Leads}: {solide Entwicklung|stabile Zahlen|positiver Trend}</li>
            <li>{Ausblick|Nächste Schritte|Prognose}: {weitere Verbesserung erwartet|Kurs beibehalten|neue Kampagnen geplant}</li>
        </ul>
        <p>{Details bei Bedarf|Ausführlicher Report auf Anfrage|Rückfragen jederzeit willkommen}.</p>
        <p>{Grüße|Beste Grüße},<br>{sender}</p>"""
    ),
    (
        "{Einladung|Save the date}: {Webinar|Event|Info-Termin} am {datum}",
        """<p>{Hallo|Guten Tag},</p>
        <p>{wir laden Sie ein|Sie sind eingeladen|hiermit möchten wir Sie einladen} zu {unserem nächsten|einem exklusiven|einem kostenfreien} {Webinar|Info-Event|Online-Termin} am <strong>{datum}</strong>.</p>
        <p>{Themen|Agenda|Inhalte}:</p>
        <ul>
            <li>{Marktentwicklung|Aktuelle Trends|Branchen-News}</li>
            <li>{Praxiserfahrungen|Case Studies|Best Practices}</li>
            <li>{Live-Q&A|Diskussionsrunde|Fragen und Antworten}</li>
        </ul>
        <p>{Anmeldung|Registrierung|Teilnahme} {formlos per Antwort|über den beigefügten Link|via kurze Rückmeldung}.</p>
        <p>{Beste Grüße|Freundliche Grüße},<br>{sender}</p>"""
    ),
    (
        "{Neuigkeiten|Update|Info} {von {sender}|aus dem Team|Unser}: {Was gibt's Neues}",
        """<p>{Hallo|Guten Tag},</p>
        <p>{lange nichts gehört|kurzes Update|freuen uns Sie wieder zu erreichen} — hier ein {schneller|kompakter|kurzer} {Überblick|Statusreport|Rückblick}:</p>
        <p>{Neu bei uns|Neu im Portfolio|Frisch dabei}: {erweiterte Servicezeiten|zusätzliche Beratungsangebote|neue Produkt-Kategorien}.</p>
        <p>{Kommen bald|Demnächst|In Vorbereitung}: {weitere Details|umfassende Infos|ein neues Portal}, {folgt in Kürze|bleiben Sie dran|wir halten Sie informiert}.</p>
        <p>{Bis dahin|Viel Erfolg|Bis bald}<br>{sender}</p>"""
    ),
    (
        "{Wichtige|Neue|Aktualisierte} {Info|Mitteilung}: {AGB|Konditionen|Nutzungsbedingungen} {Update|angepasst}",
        """<p>{Sehr geehrte Damen und Herren|Guten Tag},</p>
        <p>{wir informieren Sie darüber|kurze Info|bitte beachten Sie}, dass unsere {AGB|Nutzungsbedingungen|Konditionen} zum {datum} {aktualisiert wurden|leicht angepasst wurden|in Teilen überarbeitet wurden}.</p>
        <p>{Die wichtigsten Änderungen|Was neu ist|Zusammenfassung}:</p>
        <ul>
            <li>{Klarere Formulierungen|Redaktionelle Anpassungen|Präzisere Definitionen} {ohne inhaltliche Änderung|in verschiedenen Absätzen|bei Widerruf und Datenschutz}</li>
            <li>{Ergänzungen zu|Neue Passagen für|Anpassungen im Bereich} {digitalen Diensten|elektronischer Kommunikation|Datenschutz-Regelungen}</li>
        </ul>
        <p>{Alle Details|Vollständiger Text|Volltext} {auf unserer Website|auf Anfrage|im Kundenbereich}.</p>
        <p>{Freundliche Grüße|Mit freundlichen Grüßen},<br>{sender}</p>"""
    ),
    (
        "{Kurz|Schnell} zwischendurch — {Frage|Anfrage} zu {topic}",
        """<p>{Hallo|Guten Tag},</p>
        <p>{eine kurze Frage|nur zwischendurch|schneller Check-in}: {besteht bei Ihnen aktuell|haben Sie momentan|gibt es aktuell} Bedarf im Bereich {topic}?</p>
        <p>{Falls ja|Bei Interesse|Sollte das relevant sein}, {gerne kurz zurückrufen|einfach antworten|Termin gerne per Antwort}.</p>
        <p>{Falls nein|Kein Bedarf|Momentan nicht}, {bitte kurz Bescheid|kurze Rückmeldung reicht|ich vermerke das entsprechend}.</p>
        <p>{Grüße|Danke und Grüße|Beste Grüße},<br>{sender}</p>"""
    ),
    (
        "{Feedback erbeten|Kurze Umfrage|Ihre Meinung}: {2 Minuten Ihrer Zeit|Kurzumfrage|Kundenzufriedenheit}",
        """<p>{Hallo|Guten Tag},</p>
        <p>{um unsere Services|damit wir uns|zur laufenden Verbesserung} {weiter zu verbessern|besser aufstellen können|beitragen können}, würden wir uns {über 2 Minuten|über kurzes Feedback|über Ihre Rückmeldung} {sehr freuen|freuen|dankbar sein}.</p>
        <p>{Drei kurze Fragen|Fünf Punkte|Ein paar Themen}:</p>
        <ol>
            <li>{Wie zufrieden|Wie bewerten Sie|Wie fanden Sie} {die letzte Beratung|die Zusammenarbeit|unsere Kommunikation}?</li>
            <li>{Was können wir|Wo können wir|Was sollten wir} {besser machen|verbessern|angehen}?</li>
            <li>{Empfehlungen|Vorschläge|Anregungen} {für die Zukunft|für neue Angebote|zu unserem Sortiment}?</li>
        </ol>
        <p>{Danke im Voraus|Vielen Dank|Herzlichen Dank}!<br>{sender}</p>"""
    ),
    (
        "{Bestätigung|Info}: {Ihre Registrierung|Ihr Konto|Ihr Zugang}",
        """<p>{Willkommen|Hallo}!</p>
        <p>{vielen Dank für|danke für|schön dass Sie sich für} {Ihre Registrierung|Ihr Interesse|die Anmeldung} {angemeldet haben|entschieden haben}. {Ihr Zugang ist|Ihr Konto ist|Sie sind} {aktiviert|einsatzbereit|freigeschaltet}.</p>
        <p>{Nächste Schritte|Was jetzt|So geht's weiter}:</p>
        <ol>
            <li>{Einloggen|Anmelden|Login} {mit den Zugangsdaten|über den Link|im Kundenbereich}</li>
            <li>{Profil vervollständigen|Daten prüfen|Einstellungen anpassen}</li>
            <li>{Los legen|Erste Aktionen|Erkunden}</li>
        </ol>
        <p>{Fragen? Einfach antworten|Bei Problemen: gerne melden|Support jederzeit erreichbar}.</p>
        <p>{Viele Grüße|Beste Grüße},<br>{sender}</p>"""
    ),
]


TOPICS = [
    "Vertriebsoptimierung", "Marketing-Automatisierung", "Kundenservice",
    "Kostensenkung", "Prozessdigitalisierung", "Datenschutz-Compliance",
    "Cloud-Migration", "Effizienzsteigerung", "Nachhaltigkeit im Büro",
    "Zeitmanagement", "Kundenzufriedenheit", "Weiterbildung",
]

MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni",
             "Juli", "August", "September", "Oktober", "November", "Dezember"]

REPLY_TEMPLATES = [
    "Danke für die Info!",
    "Vielen Dank, wird geprüft.",
    "Erhalten, danke.",
    "Danke für die Nachricht, sehr hilfreich.",
    "Habe ich gesehen, danke schön.",
    "Kurze Bestätigung: angekommen.",
    "Danke, klingt gut.",
    "Interessant, komme darauf zurück.",
    "Vielen Dank, prüfe ich in Ruhe.",
    "Danke für die Zusendung.",
    "Habe ich zur Kenntnis genommen.",
    "Alles klar, danke!",
    "Perfekt, danke für die Info.",
    "Danke, schaue ich mir an.",
    "Notiert, vielen Dank.",
]


# ── Spintax + Placeholder Expansion ─────────────────────

_SPINTAX_RE = re.compile(r"\{([^{}]+)\}")


def _resolve_spintax(text: str, rng: random.Random) -> str:
    """Wiederholt bis keine {a|b|c}-Muster mehr da sind."""
    for _ in range(10):
        def _pick(m):
            options = m.group(1).split("|")
            if len(options) < 2:
                return m.group(0)  # Placeholder ohne | — lassen für _fill_placeholders
            return rng.choice(options)
        new = _SPINTAX_RE.sub(_pick, text)
        if new == text:
            break
        text = new
    return text


def _fill_placeholders(text: str, rng: random.Random, ctx: dict) -> str:
    """Ersetzt {name}, {monat}, {kw}, {jahr}, {datum}, {topic}, {sender}, {ordernum}, {quartal}."""
    now = datetime.now()
    defaults = {
        "monat": MONTHS_DE[now.month - 1],
        "kw": f"{now.isocalendar()[1]:02d}",
        "jahr": str(now.year),
        "datum": now.strftime("%d.%m.%Y"),
        "quartal": str((now.month - 1) // 3 + 1),
        "topic": rng.choice(TOPICS),
        "ordernum": f"{rng.randint(100000, 999999)}",
        "name": ctx.get("name", ""),
        "sender": ctx.get("sender", rng.choice(SENDER_NAMES)),
    }
    for k, v in defaults.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def generate_local_email(rng: random.Random = None, sender_hint: str = "") -> dict:
    """Erzeugt eine komplette Warmup-Mail aus dem lokalen Pool.
    Returns {subject, html, plain, sender}."""
    if rng is None:
        rng = random.Random(secrets.randbits(64))
    subj_tpl, body_tpl = rng.choice(TEMPLATES)
    sender = sender_hint or rng.choice(SENDER_NAMES)
    ctx = {"sender": sender}
    subject = _fill_placeholders(_resolve_spintax(subj_tpl, rng), rng, ctx)
    html = _fill_placeholders(_resolve_spintax(body_tpl, rng), rng, ctx)
    plain = re.sub(r"<[^>]+>", "", html)
    plain = re.sub(r"\s+", " ", plain).strip()
    return {"subject": subject.strip(), "html": html.strip(),
            "plain": plain, "sender": sender}


def generate_local_reply(rng: random.Random = None) -> str:
    if rng is None:
        rng = random.Random(secrets.randbits(64))
    return rng.choice(REPLY_TEMPLATES)


# ── Byte-Uniquify: kleine unsichtbare Änderung damit jede Mail unique bleibt ─

def make_unique_html(html: str, rng: random.Random = None) -> str:
    """Fügt einen unsichtbaren HTML-Kommentar mit random UUID ans Ende +
    ändert eine random Anzahl Leerzeichen im Text. So sind zwei aus dem
    gleichen Template gebaute Mails byte-verschieden."""
    if rng is None:
        rng = random.Random(secrets.randbits(64))
    marker = secrets.token_hex(rng.randint(6, 12))
    # unsichtbarer HTML-Kommentar mit unique Token
    return html + f"\n<!-- x{marker} -->"
