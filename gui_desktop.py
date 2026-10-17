#!/usr/bin/env python3
"""Desktop GUI for Mass Mailer. Run: python gui_desktop.py"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.app import main

if __name__ == "__main__":
    main()
