"""CLI entry point — python -m htmlgen"""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog="htmlgen",
        description="Generate randomized HTML email templates from modular blocks.",
    )
    sub = parser.add_subparsers(dest="command")

    # --- generate ---
    gen = sub.add_parser("generate", help="Generate HTML templates")
    gen.add_argument("-c", "--config", default=None, help="Path to config.yaml")
    gen.add_argument("-n", "--count", type=int, default=None, help="Number of templates")
    gen.add_argument("-o", "--output", default=None, help="Output directory")
    gen.add_argument("--base-dir", default=None, help="Base dir for blocks/layouts (default: htmlgen/)")

    # --- preview ---
    prev = sub.add_parser("preview", help="Generate one template and print to stdout")
    prev.add_argument("-c", "--config", default=None, help="Path to config.yaml")
    prev.add_argument("--base-dir", default=None, help="Base dir for blocks/layouts")

    # --- list-blocks ---
    sub.add_parser("list-blocks", help="List available blocks and their variants")

    # --- list-layouts ---
    sub.add_parser("list-layouts", help="List available layouts")

    args = parser.parse_args()

    if args.command == "generate":
        from .engine import generate
        base = Path(args.base_dir) if args.base_dir else None
        written = generate(
            config_path=args.config,
            count=args.count,
            output_dir=args.output,
            base_dir=base,
        )
        print(f"Generated {len(written)} templates in {written[0].parent}")

    elif args.command == "preview":
        from .engine import generate_one
        from .config import load_config
        cfg = load_config(args.config)
        base = Path(args.base_dir) if args.base_dir else None
        html = generate_one(cfg, base)
        print(html)

    elif args.command == "list-blocks":
        from .engine import _load_variants, _BASE
        block_names = ["logo", "referenz", "satz", "hinweis", "frist", "link", "gruss", "footer"]
        for name in block_names:
            variants = _load_variants(name)
            print(f"{name}: {len(variants)} variants")
            for v in variants:
                tags = ", ".join(sorted(v["tags"])) if v["tags"] else "(no tags)"
                print(f"  {v['variant']}: {tags}")

    elif args.command == "list-layouts":
        from .engine import _load_layouts, _BASE
        layouts = _load_layouts()
        print(f"{len(layouts)} layouts:")
        for l in layouts:
            print(f"  {l['name']}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
