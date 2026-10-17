#!/usr/bin/env python3
"""Bulk Mailer Desktop GUI. Run: python bulk/gui_bulk.py"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from bulk.gui.app import main

if __name__ == "__main__":
    main()
