import sys
from pathlib import Path

# The emulator's modules import each other, and the Beken tools, by bare name:
# so are they imported here.
TOOLS = Path(__file__).resolve().parents[3] / "tools" / "beken"
for path in (TOOLS / "emulator", TOOLS):
    if str(path) not in sys.path:
        sys.path.append(str(path))
