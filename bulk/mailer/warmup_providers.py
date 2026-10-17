"""Warmup Provider Config — IMAP/SMTP settings and folder names per provider."""

PROVIDERS = {
    "t-online": {
        "imap_host": "secureimap.t-online.de",
        "imap_port": 993,
        "smtp_host": "securesmtp.t-online.de",
        "smtp_port": 465,
        "spam_folder": "Spam",
        "auth": "password",
    },
    "gmx": {
        "imap_host": "imap.gmx.net",
        "imap_port": 993,
        "smtp_host": "mail.gmx.net",
        "smtp_port": 587,
        "spam_folder": "Spamverdacht",
        "auth": "password",
    },
    "web.de": {
        "imap_host": "imap.web.de",
        "imap_port": 993,
        "smtp_host": "smtp.web.de",
        "smtp_port": 587,
        "spam_folder": "Spamverdacht",
        "auth": "password",
    },
    "strato": {
        "imap_host": "imap.strato.de",
        "imap_port": 993,
        "smtp_host": "smtp.strato.de",
        "smtp_port": 465,
        "spam_folder": "Spam",
        "auth": "password",
    },
    "gmail": {
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "spam_folder": "[Gmail]/Spam",
        "auth": "app_password",
    },
    "outlook": {
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "spam_folder": "Junk",
        "auth": "app_password",
    },
    "yahoo": {
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 587,
        "spam_folder": "Bulk Mail",
        "auth": "app_password",
    },
    "freenet": {
        "imap_host": "mx.freenet.de",
        "imap_port": 993,
        "smtp_host": "mx.freenet.de",
        "smtp_port": 587,
        "spam_folder": "Spam",
        "auth": "password",
    },
    "mailbox.org": {
        "imap_host": "imap.mailbox.org",
        "imap_port": 993,
        "smtp_host": "smtp.mailbox.org",
        "smtp_port": 587,
        "spam_folder": "Junk",
        "auth": "password",
    },
    "posteo": {
        "imap_host": "posteo.de",
        "imap_port": 993,
        "smtp_host": "posteo.de",
        "smtp_port": 587,
        "spam_folder": "Spam",
        "auth": "password",
    },
}

WARMUP_CURVES = {
    "turbo": [
        (1, 2, 50, 100),
        (3, 4, 150, 80),
        (5, 7, 400, 60),
        (8, 10, 1000, 40),
        (11, 14, 2500, 25),
        (15, 18, 5000, 15),
        (19, 21, 10000, 10),
        (22, 28, 25000, 5),
        (29, 999, 50000, 3),
    ],
    "conservative": [
        (1, 3, 20, 100),
        (4, 7, 50, 90),
        (8, 14, 200, 70),
        (15, 21, 500, 50),
        (22, 30, 1500, 30),
        (31, 45, 5000, 15),
        (46, 60, 15000, 8),
        (61, 999, 50000, 3),
    ],
    "langsam": [
        (1, 5, 30, 100),
        (6, 14, 100, 80),
        (15, 30, 500, 50),
        (31, 60, 2000, 25),
        (61, 999, 10000, 5),
    ],
    "maintenance": [
        (1, 999, 200, 100),
    ],
}

CURVE_LABELS = {
    "turbo":        "Turbo (schnell hoch, ~4 Wochen bis 25k/Tag)",
    "conservative": "Conservative (2 Monate bis 15k/Tag)",
    "langsam":      "Langsam (2 Monate bis 2k/Tag)",
    "maintenance":  "Maintenance (immer 200/Tag, hält warm)",
}


def get_curve_values(curve_type: str, day: int) -> tuple:
    """Returns (daily_target, seed_pct) for a given day in the warmup curve."""
    curve = WARMUP_CURVES.get(curve_type, WARMUP_CURVES["turbo"])
    for start, end, volume, pct in curve:
        if start <= day <= end:
            return volume, pct
    last = curve[-1]
    return last[2], last[3]


def get_provider_config(provider: str) -> dict:
    return PROVIDERS.get(provider.lower(), PROVIDERS.get("strato", {}))


# ── Provider Auto-Detect aus Email-Domain ──

_DOMAIN_TO_PROVIDER = {
    "gmail.com": "gmail",
    "googlemail.com": "gmail",
    "outlook.com": "outlook", "hotmail.com": "outlook", "hotmail.de": "outlook",
    "live.com": "outlook", "live.de": "outlook", "msn.com": "outlook",
    "office365.com": "outlook",
    "yahoo.com": "yahoo", "yahoo.de": "yahoo",
    "gmx.de": "gmx", "gmx.net": "gmx", "gmx.at": "gmx", "gmx.ch": "gmx",
    "gmx.com": "gmx",
    "web.de": "web.de",
    "t-online.de": "t-online", "magenta.de": "t-online",
    "freenet.de": "freenet",
    "mailbox.org": "mailbox.org",
    "posteo.de": "posteo", "posteo.net": "posteo",
    # Strato-branded verwenden meist eigene Domain — bleibt Fallback
}


def detect_provider_from_email(email: str) -> str:
    """Ermittelt den Provider anhand der Email-Domain.
    Rückgabe: Provider-Key aus PROVIDERS, oder 'custom' wenn unbekannt."""
    if not email or "@" not in email:
        return "custom"
    domain = email.rsplit("@", 1)[1].lower().strip()
    if domain in _DOMAIN_TO_PROVIDER:
        return _DOMAIN_TO_PROVIDER[domain]
    # Fuzzy — hilft bei sub-Domains von Gmail G-Suite etc.
    for known, prov in _DOMAIN_TO_PROVIDER.items():
        if domain.endswith("." + known):
            return prov
    return "custom"


def infer_config_for_email(email: str) -> dict:
    """Vollständiger IMAP+SMTP-Config-Guess für eine Email. Für unbekannte
    Domains: `imap.{domain}:993` + `smtp.{domain}:587` als Fallback."""
    prov = detect_provider_from_email(email)
    if prov != "custom":
        cfg = dict(PROVIDERS[prov])
        cfg["provider"] = prov
        return cfg
    # Custom fallback
    domain = email.rsplit("@", 1)[1].lower().strip() if "@" in email else "example.com"
    return {
        "provider": "custom",
        "imap_host": f"imap.{domain}",
        "imap_port": 993,
        "smtp_host": f"smtp.{domain}",
        "smtp_port": 587,
        "spam_folder": "Spam",
        "auth": "password",
    }


def parse_seed_line(line: str):
    """Parst eine Zeile aus dem Bulk-Import. Akzeptierte Formate:
      email:password
      email:password:imap_host
      email:password:imap_host:imap_port
      email:password:imap_host:imap_port:smtp_host:smtp_port
      imap_host:port:email:password        (alternative Reihenfolge)
    Rückgabe: dict mit email/password/imap_host/imap_port/smtp_host/smtp_port/provider,
    oder None wenn nicht parsebar.
    """
    parts = [p.strip() for p in line.strip().split(":") if p.strip() != ""]
    if len(parts) < 2:
        return None

    def _find_email(items):
        for i, p in enumerate(items):
            if "@" in p and "." in p.split("@", 1)[1]:
                return i
        return -1

    ei = _find_email(parts)
    if ei < 0:
        return None
    email = parts[ei]
    # das nachfolgende Feld ist das Passwort
    if ei + 1 >= len(parts):
        return None
    password = parts[ei + 1]

    # Alle anderen Parts einsammeln, in Reihenfolge zuordnen
    others = [p for i, p in enumerate(parts) if i != ei and i != ei + 1]
    cfg = infer_config_for_email(email)
    # others könnte enthalten: [imap_host], [imap_host, imap_port], oder
    # [imap_host, imap_port, smtp_host, smtp_port] — oder gemischt
    if len(others) >= 1:
        # erster nicht-nummerischer Wert = imap_host
        first_host = next((o for o in others if not o.isdigit()), "")
        if first_host:
            cfg["imap_host"] = first_host
            # port danach?
            idx = others.index(first_host)
            if idx + 1 < len(others) and others[idx + 1].isdigit():
                cfg["imap_port"] = int(others[idx + 1])
            # smtp danach?
            remaining = others[idx + 2:] if idx + 1 < len(others) and others[idx + 1].isdigit() else others[idx + 1:]
            smtp_host = next((o for o in remaining if not o.isdigit()), "")
            if smtp_host:
                cfg["smtp_host"] = smtp_host
                sidx = remaining.index(smtp_host)
                if sidx + 1 < len(remaining) and remaining[sidx + 1].isdigit():
                    cfg["smtp_port"] = int(remaining[sidx + 1])

    return {
        "email": email,
        "password": password,
        "provider": cfg["provider"],
        "imap_host": cfg["imap_host"],
        "imap_port": int(cfg["imap_port"]),
        "smtp_host": cfg.get("smtp_host", ""),
        "smtp_port": int(cfg.get("smtp_port", 587)),
    }


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

REPLY_TEMPLATES_DE = [
    "Danke für die Info!",
    "Hab ich gesehen, danke.",
    "Interessant, werde ich mir anschauen.",
    "Vielen Dank für die Nachricht.",
    "Danke, sehr hilfreich.",
    "Super, danke für die Zusendung!",
    "Danke schön!",
    "Habe ich zur Kenntnis genommen.",
    "Perfekt, danke!",
    "Vielen Dank, sehr nützlich.",
]
