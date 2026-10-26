"""tools/set-key.ps1: an API key typed hidden into the user's environment variables, never shown,
kept in history or written to a file (read as text: tests start no processes)."""
from __future__ import annotations

import re
from pathlib import Path

from massive_collate.config import KEY_NAMES

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "set-key.ps1"


def test_it_offers_exactly_the_keys_the_app_reads():
    src = SCRIPT.read_text(encoding="utf-8")
    offered = re.search(r"\[ValidateSet\(([^)]*)\)\]", src).group(1)
    assert tuple(re.findall(r"'([A-Z_]+)'", offered)) == KEY_NAMES


def test_the_key_is_read_hidden_and_goes_only_into_the_user_environment():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "Read-Host \"$Name (typed hidden)\" -AsSecureString" in src
    assert "ZeroFreeBSTR($bstr)" in src and "$value = $null" in src
    assert "[Environment]::SetEnvironmentVariable($Name, $value, 'User')" in src
    for leak in ("Out-File", "Set-Content", "Add-Content", "Write-Host $value", "Write-Output", "Clipboard"):
        assert leak not in src
