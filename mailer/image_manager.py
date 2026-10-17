import os
import io
import json
import time
import random
import hashlib
import secrets
import logging
import base64
from typing import List, Optional, Tuple

try:
    from PIL import Image, ImageEnhance, ImageFilter
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger("mailer.images")


def _calc_num_templates(lead_count: int) -> int:
    return min(max(lead_count // 50, 25), 500)


class ImageManager:
    MAX_VARIANTS = 500
    CACHE_FILE = "image_pool.json"

    def __init__(
        self,
        enabled: bool = False,
        cloud_name: str = "",
        api_key: str = "",
        api_secret: str = "",
        logos_dir: str = "logos",
        mode: str = "cloudinary",
        quantize: bool = True,
        downscale: bool = True,
        max_colors: int = 256,
        logo_rotate_every: int = 0,
    ):
        self._cloud_name = cloud_name
        self._api_key = api_key
        self._api_secret = api_secret
        self._logos_dir = logos_dir
        self._mode = mode.lower().strip()
        self._enabled = enabled
        self._urls: List[str] = []
        self._templates: List[Image.Image] = []
        self._fmt: str = "PNG"
        self._logo_width: int = 0
        self._quantize: bool = quantize
        self._downscale: bool = downscale
        self._max_colors: int = max(2, min(256, max_colors))
        self._logo_rotate_every: int = logo_rotate_every
        self._logo_groups: List[List[Image.Image]] = []

        if self._mode == "cloudinary":
            self._enabled = enabled and bool(cloud_name and api_key and api_secret)
            self._load_cache()
        elif self._mode == "cid":
            self._enabled = enabled and HAS_PILLOW
        else:
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def urls(self) -> List[str]:
        return self._urls

    @property
    def logo_width(self) -> int:
        return min(self._logo_width, 220)

    @property
    def pool_size(self) -> int:
        if self._mode == "cloudinary":
            return len(self._urls)
        return len(self._templates)

    def prepare(self, lead_count: int) -> None:
        if not self._enabled:
            return
        if self._mode == "cloudinary":
            self._prepare_cloudinary(lead_count)
        elif self._mode == "cid":
            self._prepare_cid_templates(lead_count)

    def get_random_url(self) -> str:
        if not self._urls:
            return ""
        return random.choice(self._urls)

    def get_cid_logo(self, send_index: int = -1) -> Optional[Tuple[bytes, str, str]]:
        if not self._logo_groups:
            return None
        if send_index < 0:
            group_idx = random.randint(0, len(self._logo_groups) - 1)
        elif self._logo_rotate_every > 0 and len(self._logo_groups) > 1:
            group_idx = (send_index // self._logo_rotate_every) % len(self._logo_groups)
        else:
            group_idx = 0
        group = self._logo_groups[group_idx]
        template = random.choice(group)
        img = template.copy()
        pixels = img.load()
        w, h = img.size
        x = w - 1 - random.randint(0, min(4, w - 1))
        y = h - 1 - random.randint(0, min(4, h - 1))
        pixel = pixels[x, y]

        if img.mode == "P":
            idx = pixel if isinstance(pixel, int) else pixel[0]
            pixels[x, y] = max(0, min(255, idx + random.choice([-1, 1])))
        elif img.mode == "RGBA" and isinstance(pixel, tuple) and len(pixel) == 4:
            p = list(pixel[:3])
            p[random.randint(0, 2)] = min(255, max(0, p[random.randint(0, 2)] + random.choice([-2, -1, 1, 2])))
            pixels[x, y] = (p[0], p[1], p[2], pixel[3])
        else:
            p = list(pixel[:3])
            p[random.randint(0, 2)] = min(255, max(0, p[random.randint(0, 2)] + random.choice([-2, -1, 1, 2])))
            pixels[x, y] = (p[0], p[1], p[2])

        buf = io.BytesIO()
        save_kw = {}
        if self._fmt == "PNG":
            from PIL.PngImagePlugin import PngInfo
            meta = PngInfo()
            meta.add_text("uid", secrets.token_hex(8))
            meta.add_text("Software", f"Mailer {secrets.token_hex(4)}")
            meta.add_text("DateTime", time.strftime("%Y:%m:%d %H:%M:%S"))
            save_kw["pnginfo"] = meta
            save_kw["optimize"] = True
            save_kw["compress_level"] = 9
        else:
            save_kw["quality"] = 95
            save_kw["optimize"] = True
        img.save(buf, format=self._fmt, **save_kw)
        raw = buf.getvalue()

        if self._fmt == "JPEG":
            uid = secrets.token_hex(8).encode()
            marker = b"\xff\xfe" + (len(uid) + 2).to_bytes(2, "big") + uid
            raw = raw[:2] + marker + raw[2:]

        cid = f"logo{secrets.token_hex(6)}"
        mime = "image/png" if self._fmt == "PNG" else "image/jpeg"
        return raw, cid, mime

    def _find_logos(self) -> List[str]:
        if not os.path.isdir(self._logos_dir):
            return []
        exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
        return sorted(
            os.path.join(self._logos_dir, f)
            for f in os.listdir(self._logos_dir)
            if f.lower().endswith(exts) and os.path.isfile(os.path.join(self._logos_dir, f))
        )

    def _load_base_image(self, path: str):
        img = Image.open(path)
        if img.mode == "P":
            img = img.convert("RGBA")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        if self._downscale and img.width > 220:
            ratio = 220 / img.width
            img = img.resize((220, max(1, round(img.height * ratio))), Image.LANCZOS)
        if img.mode == "RGBA":
            if img.getchannel("A").getextrema()[0] == 255:
                img = img.convert("RGB")
        return img

    def _make_variant(self, base_img, seed: int):
        rng = random.Random(seed)
        w, h = base_img.size
        variant = base_img.copy()

        scale = rng.uniform(0.98, 1.02)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        if (nw, nh) != (w, h):
            variant = variant.resize((nw, nh), Image.LANCZOS)

        angle = rng.uniform(-0.7, 0.7)
        if abs(angle) > 0.1:
            fill = (0, 0, 0, 0) if variant.mode == "RGBA" else (255, 255, 255)
            variant = variant.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=fill)

        if abs(rng.uniform(-0.01, 0.01)) > 0.002:
            variant = ImageEnhance.Color(variant).enhance(1.0 + rng.uniform(-0.1, 0.1))

        if abs(rng.uniform(-0.01, 0.01)) > 0.002:
            variant = ImageEnhance.Brightness(variant).enhance(1.0 + rng.uniform(-0.01, 0.01))

        ct, cb, cl, cr = rng.randint(0, 2), rng.randint(0, 2), rng.randint(0, 2), rng.randint(0, 2)
        vw, vh = variant.size
        if ct + cb < vh - 2 and cl + cr < vw - 2:
            variant = variant.crop((cl, ct, vw - cr, vh - cb))

        blur_r = rng.uniform(0.0, 0.3)
        if blur_r > 0.1:
            variant = variant.filter(ImageFilter.GaussianBlur(radius=blur_r))

        sx, sy = rng.choice([-1, 0, 1]), rng.choice([-1, 0, 1])
        if sx or sy:
            from PIL import ImageChops
            variant = ImageChops.offset(variant, sx, sy)

        if self._fmt == "PNG" and self._quantize:
            try:
                method = (Image.Quantize.FASTOCTREE if variant.mode == "RGBA"
                          else Image.Quantize.MEDIANCUT)
                variant = variant.quantize(colors=self._max_colors, method=method)
            except Exception:
                pass

        return variant

    def _prepare_cid_templates(self, lead_count: int) -> None:
        if self._logo_groups:
            return
        if not HAS_PILLOW:
            logger.error("Pillow not installed")
            return

        logos = self._find_logos()
        if not logos:
            logger.error("No images in %s/", self._logos_dir)
            self._enabled = False
            return

        self._fmt = "PNG" if logos[0].lower().endswith(".png") else "JPEG"
        num_per_logo = max(5, _calc_num_templates(lead_count) // len(logos))

        print(f"  CID logos: {len(logos)} logo(s), {num_per_logo} templates each ...")

        for logo_idx, logo_path in enumerate(logos):
            base_img = self._load_base_image(logo_path)
            if logo_idx == 0:
                self._logo_width = base_img.size[0]

            group = []
            for i in range(num_per_logo):
                group.append(self._make_variant(base_img, logo_idx * 10000 + i))
            self._logo_groups.append(group)
            self._templates.extend(group)
            print(f"    [{logo_idx+1}/{len(logos)}] {os.path.basename(logo_path)}: "
                  f"{base_img.size[0]}x{base_img.size[1]}px, {len(group)} variants")

        total = sum(len(g) for g in self._logo_groups)
        print(f"  CID logos: {total} templates ready ({len(logos)} logos)")

    def _load_cache(self) -> None:
        if not os.path.isfile(self.CACHE_FILE):
            return
        try:
            with open(self.CACHE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    self._urls = [u for u in data if isinstance(u, str) and u]
        except (json.JSONDecodeError, OSError):
            self._urls = []

    def _save_cache(self) -> None:
        try:
            with open(self.CACHE_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._urls, fh, indent=2)
        except OSError as exc:
            logger.error("Failed to save image cache: %s", exc)

    def _prepare_cloudinary(self, lead_count: int) -> None:
        if not HAS_PILLOW or not HAS_REQUESTS:
            logger.error("Pillow or requests not installed")
            return
        target = min(lead_count, self.MAX_VARIANTS)
        if len(self._urls) >= target:
            return
        logos = self._find_logos()
        if not logos:
            logger.error("No images in %s/", self._logos_dir)
            return
        needed = target - len(self._urls)
        print(f"  Cloudinary: uploading {needed} variants ...")
        for i in range(needed):
            vid = len(self._urls) + i
            data = self._obfuscate_for_upload(logos[vid % len(logos)], vid)
            if data is None:
                continue
            url = self._upload_to_cloudinary(data, f"logo_v{vid}")
            if url:
                self._urls.append(url)
                self._save_cache()
            if (i + 1) % 25 == 0 or (i + 1) == needed:
                print(f"    [{i + 1}/{needed}]")
        print(f"  Cloudinary pool: {len(self._urls)} URLs")

    @staticmethod
    def _obfuscate_for_upload(image_path: str, variant_id: int) -> Optional[bytes]:
        try:
            img = Image.open(image_path)
            if img.mode == "P":
                img = img.convert("RGBA")
            elif img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            rng = random.Random(variant_id)
            w, h = img.size
            scale = rng.uniform(0.98, 1.02)
            nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
            if (nw, nh) != (w, h):
                img = img.resize((nw, nh), Image.LANCZOS)
            pixels = img.load()
            w, h = img.size
            for _ in range(rng.randint(3, 6)):
                x = w - 1 - rng.randint(0, min(9, w - 1))
                y = h - 1 - rng.randint(0, min(9, h - 1))
                ch = rng.randint(0, 2)
                delta = rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5])
                pixel = pixels[x, y]
                p = list(pixel[:3])
                p[ch] = min(255, max(0, p[ch] + delta))
                if img.mode == "RGBA" and len(pixel) == 4:
                    pixels[x, y] = (p[0], p[1], p[2], pixel[3])
                else:
                    pixels[x, y] = (p[0], p[1], p[2])
            buf = io.BytesIO()
            fmt = "PNG" if image_path.lower().endswith(".png") else "JPEG"
            uid = hashlib.md5(str(variant_id).encode()).hexdigest()
            kw = {}
            if fmt == "PNG":
                from PIL.PngImagePlugin import PngInfo
                m = PngInfo(); m.add_text("uid", uid); kw["pnginfo"] = m
            else:
                kw["quality"] = 95
            img.save(buf, format=fmt, **kw)
            raw = buf.getvalue()
            if fmt == "JPEG":
                c = uid.encode("ascii")
                marker = b"\xff\xfe" + (len(c) + 2).to_bytes(2, "big") + c
                raw = raw[:2] + marker + raw[2:]
            return raw
        except Exception as exc:
            logger.error("Obfuscation failed: %s", exc)
            return None

    def _upload_to_cloudinary(self, image_data: bytes, public_id: str) -> Optional[str]:
        timestamp = str(int(time.time()))
        to_sign = f"public_id={public_id}&timestamp={timestamp}{self._api_secret}"
        signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
        b64 = base64.b64encode(image_data).decode("ascii")
        url = f"https://api.cloudinary.com/v1_1/{self._cloud_name}/image/upload"
        try:
            resp = _requests.post(url, data={
                "file": f"data:image/png;base64,{b64}",
                "public_id": public_id, "timestamp": timestamp,
                "api_key": self._api_key, "signature": signature,
            }, timeout=60)
            if resp.status_code == 200:
                return resp.json().get("secure_url", "")
            logger.error("Cloudinary %d: %s", resp.status_code, resp.text[:300])
            return None
        except Exception as exc:
            logger.error("Cloudinary error: %s", exc)
            return None
