#!/usr/bin/env python3
"""Mass Mailer - High-Performance Multithreaded Email Sender."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mailer.mailer_core import MailerCore


def main() -> None:
    config_path = "config.ini"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    core = MailerCore(config_path)
    core.run()


if __name__ == "__main__":
    main()
