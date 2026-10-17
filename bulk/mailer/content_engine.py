"""Bulk Content Engine.

Processes macros, random strings, and sender/subject rotation.
NO Spintax file injection, NO Anti-Fingerprint — those are for transactional.
Macros come from the database, not from txt files.
"""
import re
import random
import string
from typing import List, Dict, Optional

_MACRO_RE = re.compile(r"\{([A-Za-z0-9_\-]+)\}")
_RANDSTR_RE = re.compile(r"\[RANDSTR:(\d+):([a-zA-Z0-9\-]+):(\w+)\]")

CHARSET_MAP = {
    "a-z": string.ascii_lowercase,
    "A-Z": string.ascii_uppercase,
    "0-9": string.digits,
    "a-z0-9": string.ascii_lowercase + string.digits,
    "A-Z0-9": string.ascii_uppercase + string.digits,
    "a-zA-Z": string.ascii_letters,
    "a-zA-Z0-9": string.ascii_letters + string.digits,
}


class BulkContentEngine:
    def __init__(self, macros: Dict[str, List[str]] = None):
        self._macros = macros or {}

    def set_macros(self, macros: Dict[str, List[str]]):
        self._macros = macros

    def process(self, template: str, email: str = "",
                extra: Dict[str, str] = None) -> str:
        text = template

        if extra:
            for key, val in extra.items():
                text = text.replace(f"{{{key}}}", val)

        if email:
            user = email.split("@")[0] if "@" in email else email
            domain = email.split("@")[1] if "@" in email else ""
            text = text.replace("{email}", email)
            text = text.replace("{email_user}", user)
            text = text.replace("{domain}", domain)

        text = self._resolve_macros(text)
        text = self._resolve_randstr(text)
        return text

    def _resolve_macros(self, text: str) -> str:
        def replacer(match: re.Match) -> str:
            name = match.group(1)
            if name in ("email", "email_user", "domain"):
                return match.group(0)
            values = self._macros.get(name, [])
            if values:
                return random.choice(values)
            return match.group(0)

        return _MACRO_RE.sub(replacer, text)

    def _resolve_randstr(self, text: str) -> str:
        def replacer(match: re.Match) -> str:
            length = int(match.group(1))
            charset_key = match.group(2)
            case = match.group(3).lower()
            charset = CHARSET_MAP.get(charset_key, charset_key)
            if not charset:
                charset = string.ascii_lowercase
            result = "".join(random.choices(charset, k=length))
            if case == "lower":
                result = result.lower()
            elif case == "upper":
                result = result.upper()
            return result

        return _RANDSTR_RE.sub(replacer, text)

    @staticmethod
    def html_to_plaintext(html: str) -> str:
        import html as html_mod
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)
        text = html_mod.unescape(text)
        lines = [line.strip() for line in text.splitlines()]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def get_rotated_value(self, values: list, send_index: int,
                          rotate_every: int = 1) -> str:
        if not values:
            return ""
        if rotate_every <= 0:
            return values[0]
        idx = (send_index // rotate_every) % len(values)
        return values[idx]
