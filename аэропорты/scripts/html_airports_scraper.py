from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from airport_parser.config import *
from airport_parser.local_data import *
from airport_parser.downloader import *
from airport_parser.html_parser import *
from airport_parser.output import *
from airport_parser.pipeline import *
from airport_parser.cli import main

if __name__ == "__main__":
    sys.exit(main())
