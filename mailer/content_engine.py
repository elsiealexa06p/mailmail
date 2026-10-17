import os
import re
import random
import string
import html as html_module
from typing import Optional, List


_INNER_PIPE_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")
_BARE_GROUP_RE = re.compile(r"\{([^{}|]*\s[^{}|]*)\}")


class ContentEngine:
    _RANDSTR_RE = re.compile(r"\[RANDSTR:(\d+):([a-zA-Z0-9\-]+):(\w+)\]")
    _TAG_RE = re.compile(r"\{([A-Za-z0-9_\-]+)\}")

    _RESERVED = frozenset({"email", "email_user", "domain", "RedirectLink"})

    CHARSET_MAP = {
        "a-z": string.ascii_lowercase,
        "A-Z": string.ascii_uppercase,
        "0-9": string.digits,
        "a-z0-9": string.ascii_lowercase + string.digits,
        "A-Z0-9": string.ascii_uppercase + string.digits,
        "a-zA-Z": string.ascii_letters,
        "a-zA-Z0-9": string.ascii_letters + string.digits,
    }

    def __init__(
        self,
        html_dir: str,
        attachments_dir: str,
        spintax_dir: str,
        names_file: str = "",
        subjects_file: str = "",
        alt_texts_file: str = "",
    ):
        self._html_dir = html_dir
        self._attachments_dir = attachments_dir
        self._spintax_dir = spintax_dir
        self._names_file = names_file
        self._subjects_file = subjects_file
        self._alt_texts_file = alt_texts_file
        self._html_files: List[str] = []
        self._attachment_files: List[str] = []
        self._file_cache: dict = {}
        self._logo_urls: List[str] = []
        self._load_html_and_attachments()

    def _load_html_and_attachments(self) -> None:
        if os.path.isdir(self._html_dir):
            self._html_files = [
                os.path.join(self._html_dir, f)
                for f in os.listdir(self._html_dir)
                if f.lower().endswith((".html", ".htm"))
            ]
        if self._attachments_dir and os.path.isdir(self._attachments_dir):
            self._attachment_files = [
                os.path.join(self._attachments_dir, f)
                for f in os.listdir(self._attachments_dir)
                if os.path.isfile(os.path.join(self._attachments_dir, f))
            ]

    def set_logo_urls(self, urls: List[str]) -> None:
        self._logo_urls = list(urls)

    @property
    def has_attachments(self) -> bool:
        return bool(self._attachment_files)

    @property
    def has_names(self) -> bool:
        return bool(self._read_lines(self._names_file))

    @property
    def has_subjects(self) -> bool:
        return bool(self._read_lines(self._subjects_file))

    def get_random_html(self) -> Optional[str]:
        if not self._html_files:
            return None
        path = random.choice(self._html_files)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def get_random_attachment(self) -> Optional[tuple]:
        if not self._attachment_files:
            return None
        path = random.choice(self._attachment_files)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        return os.path.basename(path), data

    def get_random_subject(self) -> str:
        lines = self._read_lines(self._subjects_file)
        return random.choice(lines) if lines else ""

    def get_random_name(self) -> str:
        lines = self._read_lines(self._names_file)
        return random.choice(lines) if lines else ""

    def process(self, template: str, email: str) -> str:
        text = self._resolve_spintax(template)
        text = self._resolve_placeholders(text, email)
        text = self._resolve_spintax(text)
        text = self._resolve_special(text)
        text = self._resolve_file_injection(text)
        text = self._resolve_randstr(text)
        return text

    @staticmethod
    def _resolve_spintax(text: str) -> str:
        for _ in range(50):
            new_text = _INNER_PIPE_RE.sub(
                lambda m: random.choice(m.group(1).split("|")), text
            )
            if new_text == text:
                break
            text = new_text
        text = _BARE_GROUP_RE.sub(r"\1", text)
        return text

    @staticmethod
    def _resolve_placeholders(text: str, email: str) -> str:
        email_user = email.split("@")[0] if "@" in email else email
        domain = email.split("@")[1] if "@" in email else ""
        text = text.replace("{email}", email)
        text = text.replace("{email_user}", email_user)
        text = text.replace("{domain}", domain)
        return text

    def _get_random_alt(self) -> str:
        lines = self._read_lines(self._alt_texts_file)
        if lines:
            return random.choice(lines)
        return random.choice(["Logo", "Image", "Service", "Info", "Banner"])

    def resolve_logo_tag(self, text: str, src: str, width: int = 0) -> str:
        if "{Logo}" not in text:
            return text
        if width <= 0:
            width = 200
        alt = self._get_random_alt()
        tag = (
            f'<img src="{src}" width="{width}" '
            f'alt="{alt}" style="display:block;border:0;'
            f'max-height:50px;width:auto;">'
        )
        return text.replace("{Logo}", tag)

    def _resolve_special(self, text: str) -> str:
        if "{subject}" in text:
            text = text.replace("{subject}", self.get_random_subject())
        if "{from_name}" in text:
            text = text.replace("{from_name}", self.get_random_name())
        if "{Logo}" in text and self._logo_urls:
            text = self.resolve_logo_tag(text, random.choice(self._logo_urls))
        return text

    def _resolve_file_injection(self, text: str) -> str:
        def replacer(match: re.Match) -> str:
            tag = match.group(1)
            if tag in self._RESERVED:
                return match.group(0)
            lines = self._load_spintax_file(tag)
            if lines:
                return random.choice(lines)
            return match.group(0)

        return self._TAG_RE.sub(replacer, text)

    def _load_spintax_file(self, name: str) -> List[str]:
        if not self._spintax_dir:
            return []
        path = os.path.join(self._spintax_dir, f"{name}.txt")
        return self._read_lines(path)

    def _read_lines(self, path: str) -> List[str]:
        if not path:
            return []
        if path in self._file_cache:
            return self._file_cache[path]
        if not os.path.isfile(path):
            self._file_cache[path] = []
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = [line.strip() for line in fh if line.strip()]
        except OSError:
            lines = []
        self._file_cache[path] = lines
        return lines

    def _resolve_randstr(self, text: str) -> str:
        def replacer(match: re.Match) -> str:
            length = int(match.group(1))
            charset_key = match.group(2)
            case = match.group(3).lower()

            charset = self.CHARSET_MAP.get(charset_key, charset_key)
            if not charset:
                charset = string.ascii_lowercase

            result = "".join(random.choices(charset, k=length))
            if case == "lower":
                result = result.lower()
            elif case == "upper":
                result = result.upper()
            return result

        return self._RANDSTR_RE.sub(replacer, text)

    @staticmethod
    def html_to_plaintext(html_content: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", html_content, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)
        text = html_module.unescape(text)
        lines = [line.strip() for line in text.splitlines()]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
