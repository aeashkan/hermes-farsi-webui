#!/usr/bin/env python3
"""
Alias launcher for Hermes Farsi Web UI.
Delegates to app.py.
"""
import sys
import subprocess
import os

if __name__ == "__main__":
    app_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    sys.exit(subprocess.call([sys.executable, app_path] + sys.argv[1:]))
