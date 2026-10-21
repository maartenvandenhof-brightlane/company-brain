import pytest

from wiki_mcp import __version__
from wiki_mcp.__main__ import main


def test_version_is_set() -> None:
    assert __version__


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "wiki-mcp" in capsys.readouterr().out


def test_mcp_sdk_v2_available() -> None:
    from mcp.server.mcpserver import MCPServer

    assert MCPServer("probe") is not None
