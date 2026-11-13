#!/usr/bin/env python3
"""Graphical entry for the mod-host panel.

Run:  python3 -I panel_launcher.py --port 8765
Then open http://127.0.0.1:8765/ in a browser.

Isolated mode (-I) intentionally strips PYTHONPATH, so this launcher adds
the repository root explicitly before importing the host package.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modhost.panel import main

if __name__ == "__main__":
    main()
