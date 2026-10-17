import configparser
import os
import sys


class ConfigManager:
    REQUIRED_SECTIONS = ("paths", "sending", "sender", "test", "database")

    def __init__(self, config_path: str = "config.ini"):
        self._parser = configparser.ConfigParser()
        if not os.path.isfile(config_path):
            print(f"[!] Config file not found: {config_path}")
            sys.exit(1)
        self._parser.read(config_path, encoding="utf-8")
        self._validate()

    def _validate(self) -> None:
        for section in self.REQUIRED_SECTIONS:
            if not self._parser.has_section(section):
                print(f"[!] Missing config section: [{section}]")
                sys.exit(1)

    def get(self, section: str, key: str, fallback: str = "") -> str:
        return self._parser.get(section, key, fallback=fallback).strip()

    def getint(self, section: str, key: str, fallback: int = 0) -> int:
        return self._parser.getint(section, key, fallback=fallback)

    def getfloat(self, section: str, key: str, fallback: float = 0.0) -> float:
        return self._parser.getfloat(section, key, fallback=fallback)

    @property
    def smtp_file(self) -> str:
        return self.get("paths", "smtp_file")

    @property
    def leads_file(self) -> str:
        return self.get("paths", "leads_file")

    @property
    def names_file(self) -> str:
        return self.get("paths", "names_file")

    @property
    def subjects_file(self) -> str:
        return self.get("paths", "subjects_file")

    @property
    def html_dir(self) -> str:
        return self.get("paths", "html_dir")

    @property
    def attachments_dir(self) -> str:
        return self.get("paths", "attachments_dir")

    @property
    def spintax_dir(self) -> str:
        return self.get("paths", "spintax_dir")

    @property
    def thread_count(self) -> int:
        return self.getint("sending", "threads", fallback=40)

    @property
    def normal_delay(self) -> float:
        return self.getfloat("sending", "normal_delay", fallback=0.3)

    @property
    def provider_delay(self) -> float:
        return self.getfloat("sending", "provider_delay", fallback=6.0)

    @property
    def warmup_delay(self) -> float:
        return self.getfloat("sending", "warmup_delay", fallback=30.0)

    @property
    def warmup_count(self) -> int:
        return self.getint("sending", "warmup_count", fallback=5)

    @property
    def smtp_timeout(self) -> int:
        return self.getint("sending", "smtp_timeout", fallback=30)

    @property
    def ignore_ssl_errors(self) -> bool:
        raw = self.get("sending", "ignore_ssl_errors", fallback="true")
        return raw.lower() in ("true", "1", "yes")

    @property
    def from_name(self) -> str:
        return self.get("sender", "from_name")

    @property
    def from_email(self) -> str:
        return self.get("sender", "from_email")

    @property
    def subject(self) -> str:
        return self.get("sender", "subject")

    @property
    def test_recipients(self) -> list:
        raw = self.get("test", "test_recipients")
        if not raw:
            return []
        return [r.strip() for r in raw.split(",") if r.strip()]

    @property
    def test_interval(self) -> int:
        return self.getint("test", "test_interval", fallback=0)

    @property
    def proxy_file(self) -> str:
        return self.get("sending", "proxy_file")

    @property
    def proxy_rotate_every(self) -> int:
        return self.getint("sending", "proxy_rotate_every", fallback=0)

    @property
    def mxtoolbox_api_key(self) -> str:
        return self.get("sending", "mxtoolbox_api_key")

    @property
    def schedule_time(self) -> str:
        return self.get("sending", "schedule_time")

    @property
    def advanced_antifingerprint(self) -> bool:
        raw = self.get("content", "advanced_antifingerprint", fallback="false")
        return raw.lower() in ("true", "1", "yes")

    @property
    def structure_variation(self) -> float:
        return self.getfloat("content", "structure_variation", fallback=0.5)

    @property
    def antifingerprint_classes(self) -> bool:
        raw = self.get("content", "antifingerprint_classes", fallback="true")
        return raw.lower() in ("true", "1", "yes")

    @property
    def image_mode(self) -> str:
        return self.get("IMAGE_API", "mode", fallback="cloudinary")

    @property
    def image_downscale(self) -> bool:
        raw = self.get("IMAGE_API", "downscale", fallback="true")
        return raw.lower() in ("true", "1", "yes")

    @property
    def image_quantize(self) -> bool:
        raw = self.get("IMAGE_API", "quantize", fallback="true")
        return raw.lower() in ("true", "1", "yes")

    @property
    def logo_max_colors(self) -> int:
        return self.getint("IMAGE_API", "logo_max_colors", fallback=256)

    @property
    def logo_rotate_every(self) -> int:
        return self.getint("IMAGE_API", "logo_rotate_every", fallback=0)

    @property
    def image_api_enabled(self) -> bool:
        raw = self.get("IMAGE_API", "enabled", fallback="false")
        return raw.lower() in ("true", "1", "yes")

    @property
    def cloudinary_cloud_name(self) -> str:
        return self.get("CLOUDINARY", "cloud_name")

    @property
    def cloudinary_api_key(self) -> str:
        return self.get("CLOUDINARY", "api_key")

    @property
    def cloudinary_api_secret(self) -> str:
        return self.get("CLOUDINARY", "api_secret")

    @property
    def logos_dir(self) -> str:
        return self.get("paths", "logos_dir", fallback="logos")

    @property
    def alt_texts_file(self) -> str:
        return self.get("paths", "alt_texts_file")

    @property
    def redirect_enabled(self) -> bool:
        raw = self.get("redirect", "enabled", fallback="false")
        return raw.lower() in ("true", "1", "yes")

    @property
    def redirect_target_url(self) -> str:
        return self.get("redirect", "target_url")

    @property
    def redirect_db_path(self) -> str:
        return self.get("redirect", "db_path", fallback="redirects.db")

    @property
    def redirect_rotate_every(self) -> int:
        return self.getint("redirect", "rotate_every", fallback=10)

    @property
    def redirect_gen_threads(self) -> int:
        return self.getint("redirect", "gen_threads", fallback=3)

    @property
    def db_path(self) -> str:
        return self.get("database", "db_path", fallback="mailer.db")
