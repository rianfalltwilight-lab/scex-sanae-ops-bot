"""Default locations for mutable state and logs.

Production passes explicit paths from start-sanae.ps1, so these defaults only
matter when a variable is unset.  Offline tests (SCE_BOT_TEST_MODE=1) then
write under a temporary directory instead of the live bot tree.
"""
import os
import tempfile
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parent


def default_path(*parts):
    """Return BOT_ROOT/<parts>, or the same layout under a temp root in test mode."""
    if os.environ.get("SCE_BOT_TEST_MODE") == "1":
        base = Path(tempfile.gettempdir()) / "sanae-test-state"
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = BOT_ROOT
    return str(base.joinpath(*parts))
