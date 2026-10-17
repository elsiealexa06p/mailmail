"""Freshness reset helpers — wipe and regenerate HTML templates and
logo variants on demand during a long-running campaign.

Both functions are designed to be called from a worker thread mid-send.
They never raise; on failure they log and return the previous list so
the campaign keeps running on stale-but-working content.
"""
import glob
import logging
import os
import random
import shutil
from pathlib import Path

logger = logging.getLogger("mailer.freshness")


def regenerate_html_pool(count: int, layouts: list | None = None,
                          primary_color: str = "",
                          accent_color: str = "") -> list[str]:
    """Run the htmlgen engine `count` times and return the raw HTML
    strings. Returns an empty list on any failure."""
    try:
        # The htmlgen package sits next to this one.
        from htmlgen.config import load_config
        from htmlgen.engine import generate_one, _load_all

        base = Path(__file__).resolve().parent.parent / "htmlgen"
        if not base.exists():
            logger.warning("htmlgen base not found at %s", base)
            return []

        cfg = load_config(base / "config.yaml")

        if primary_color.strip():
            cfg.setdefault("colors", {})["primary"] = [primary_color.strip()]
            try:
                from htmlgen.colors import lighten_color
                cfg["colors"]["light_accent_bg"] = [
                    lighten_color(primary_color.strip(), cfg.get("lighten_amount", 0.85))
                ]
            except Exception:
                pass
        if accent_color.strip():
            cfg.setdefault("colors", {})["accent"] = [accent_color.strip()]

        block_variants, all_layouts = _load_all(base)
        if layouts:
            filtered = [l for l in all_layouts if l["name"] in layouts]
            if filtered:
                all_layouts = filtered
        cache = (block_variants, all_layouts)

        out = []
        for _ in range(max(1, count)):
            try:
                out.append(generate_one(cfg, base, _cache=cache))
            except Exception as e:
                logger.warning("generate_one failed: %s", e)
        return out
    except Exception as e:
        logger.error("regenerate_html_pool failed: %s", e, exc_info=True)
        return []


def regenerate_logo_variants(logos: list[dict], output_dir: str,
                              total_count: int, max_colors: int = 256,
                              quantize: bool = True,
                              downscale: bool = False) -> list[str]:
    """Wipe `output_dir` and produce `total_count` new variants spread
    evenly across the input logos. Returns the list of absolute paths
    to the new variants. Empty list on failure (and the directory is
    untouched in that case)."""
    if not logos:
        logger.warning("regenerate_logo_variants: no source logos")
        return []

    try:
        from PIL import Image, ImageEnhance
    except ImportError:
        logger.error("Pillow not installed — cannot regenerate variants")
        return []

    # Stage everything into a sibling temp dir first so a failure
    # halfway leaves the existing variants intact.
    tmp_dir = output_dir.rstrip("/") + ".regen"
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    per_logo = max(1, total_count // len(logos))
    generated_paths: list[str] = []

    try:
        for li, logo in enumerate(logos):
            src = logo.get("file_path") or ""
            # Resolve /static/... -> filesystem path
            if src.startswith("/static/"):
                src = os.path.normpath(os.path.join(
                    os.path.dirname(__file__), "..",
                    "transactional", "web", src.lstrip("/")))
            if not os.path.isfile(src):
                logger.warning("logo missing: %s", src)
                continue

            try:
                img = Image.open(src)
                if img.mode == "P":
                    img = img.convert("RGBA")
                elif img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
            except Exception as e:
                logger.warning("failed to open %s: %s", src, e)
                continue

            ext = os.path.splitext(src)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg"):
                ext = ".png"
            fmt = "PNG" if ext == ".png" else "JPEG"

            for v in range(per_logo):
                try:
                    work = img.copy()

                    if downscale and work.width > 400:
                        ratio = 400 / work.width
                        work = work.resize(
                            (400, int(work.height * ratio)),
                            Image.Resampling.LANCZOS)

                    # Mild colour jitter (preserves visual identity)
                    if work.mode == "RGB":
                        bright = ImageEnhance.Brightness(work).enhance(
                            random.uniform(0.92, 1.08))
                        cont = ImageEnhance.Contrast(bright).enhance(
                            random.uniform(0.92, 1.08))
                        col = ImageEnhance.Color(cont).enhance(
                            random.uniform(0.90, 1.10))
                        work = col

                    if quantize and work.mode in ("RGB", "RGBA"):
                        try:
                            colors = random.randint(
                                max(8, max_colors // 4), max_colors)
                            if work.mode == "RGBA":
                                work = work.quantize(
                                    colors=colors,
                                    method=Image.Quantize.MEDIANCUT).convert("RGBA")
                            else:
                                work = work.quantize(
                                    colors=colors,
                                    method=Image.Quantize.MEDIANCUT).convert("RGB")
                        except Exception:
                            pass

                    fname = f"v_{li}_{v}_{random.randint(1000, 9999)}{ext}"
                    out_path = os.path.join(tmp_dir, fname)
                    work.save(out_path, fmt, optimize=(fmt == "PNG"))
                    generated_paths.append(out_path)
                except Exception as e:
                    logger.warning("variant %d/%d failed: %s", li, v, e)

        if not generated_paths:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return []

        # Atomic swap
        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir)
        os.rename(tmp_dir, output_dir)
        # Re-resolve paths to point at the now-renamed output_dir
        new_paths = [os.path.join(output_dir, os.path.basename(p))
                      for p in generated_paths]
        return sorted(new_paths)
    except Exception as e:
        logger.error("regenerate_logo_variants failed: %s", e, exc_info=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return []
