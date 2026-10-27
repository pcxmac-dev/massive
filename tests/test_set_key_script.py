"""tools/set-key.ps1: an API key typed hidden into the user's environment variables, never shown,
kept in history or written to a file (read as text: tests start no processes)."""
from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "set-key.ps1"


def test_its_menu_is_the_provider_plugins_not_a_list_of_its_own():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "-m massive_collate plugins --json" in src  # the app's plugins, names only (no key's value)
    assert "ValidateSet" not in src
    assert not re.search(r"'(MASSIVE|ALPACA|APCA|TIINGO)_[A-Z_]+'", src)  # no provider's key written in


def test_the_key_is_read_hidden_and_goes_only_into_the_user_environment():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "Read-Host \"$Name (typed hidden)\" -AsSecureString" in src
    assert "ZeroFreeBSTR($bstr)" in src and "$value = $null" in src
    assert "[Environment]::SetEnvironmentVariable($Name, $value, 'User')" in src
    for leak in ("Out-File", "Set-Content", "Add-Content", "Write-Host $value", "Write-Output", "Clipboard"):
        assert leak not in src


def test_it_runs_on_windows_powershell():
    """Windows PowerShell 5.1 reads a file without a byte order mark as the ANSI code page."""
    raw = SCRIPT.read_bytes()
    assert raw.isascii()
    src = raw.decode()
    for newer in ("??", "?.", " && ", " || "):
        assert newer not in src
