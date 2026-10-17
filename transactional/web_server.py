#!/usr/bin/env python3
"""Transactional Mailer Web Server. Run: python transactional/web_server.py"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import uvicorn
from transactional.web.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
