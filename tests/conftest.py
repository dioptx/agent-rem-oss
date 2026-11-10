"""Keep tests on default settings even after the user saves real ones in data/settings.json."""

import os
import tempfile
from pathlib import Path

os.environ.pop("CONSOLIDATE_WINDOW", None)
os.environ["REM_SETTINGS"] = str(Path(tempfile.mkdtemp()) / "settings.json")
