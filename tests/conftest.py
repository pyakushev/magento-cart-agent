"""Shared fixtures: the mock shop server and a headless Chromium reachable over CDP."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest

# local browsers live in ./.playwright-browsers (see README); honour it before Playwright starts
_LOCAL_BROWSERS = Path(__file__).resolve().parents[1] / ".playwright-browsers"
if _LOCAL_BROWSERS.is_dir() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_LOCAL_BROWSERS)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock.serve import start  # noqa: E402

import magento_cart_agent as mca  # noqa: E402

# browser-use keeps its daemon state under $BH_HOME; on this machine ~/.config is read-only
mca.configure_browser_harness_home()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock_shops():
    server, thread, base = start(0)
    yield base
    server.shutdown()


def chromium_available() -> bool:
    """True when Playwright's Chromium binary exists (executable_path is async in the async API)."""
    try:
        from playwright.async_api import async_playwright

        async def probe():
            pw = await async_playwright().start()
            try:
                path = pw.chromium.executable_path
                return Path(await path if asyncio.iscoroutine(path) else path).exists()
            finally:
                await pw.stop()

        return asyncio.run(probe())
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not chromium_available(), reason="Playwright Chromium is not installed (run: playwright install chromium)")


class Browser:
    """A launched browser for the tests, shared per test."""

    def __init__(self, session):
        self.session = session

    @property
    def cdp_url(self):
        return self.session.cdp_url

    @property
    def page(self):
        return self.session.page

    def new_page(self):
        return self.session.context.new_page()


@pytest.fixture
def browser(tmp_path, run):
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed (run: playwright install chromium)")

    async def _launch():
        return await mca.launch_browser(
            mca.LaunchOptions(cdp_port=free_port(), headless=True, profile_dir=str(tmp_path / "profile"))
        )

    session = run(_launch())
    yield Browser(session)
    run(session.close())


_LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="session", autouse=True)
def _shared_loop():
    """One loop for the whole session: Playwright objects cannot cross different loops."""
    asyncio.set_event_loop(_LOOP)
    yield
    _LOOP.close()


@pytest.fixture()
def run():
    """Run a coroutine on the session loop, so the browser fixture stays usable."""

    def _run(coro):
        return _LOOP.run_until_complete(coro)

    return _run


class ToolRunner:
    """Call the registered tools exactly as the agent does (browser-use Tool objects)."""

    def __init__(self, tools):
        self.tools = {t.name: t for t in tools.functions}

    async def call(self, name: str, **arguments):
        """Invoke a tool through an MCP server — the interface the agent actually calls it through."""
        from mcp.server.mcpserver import MCPServer

        tool = self.tools[name]
        server = MCPServer("test-tools")
        server.add_tool(tool.function, name=tool.name, description=tool.description)
        try:
            result = await server.call_tool(name, arguments)
        except Exception as exc:  # surface the tool's own error, not the MCP wrapper
            raise RuntimeError(f"{name} failed: {getattr(exc, '__cause__', exc)}") from exc
        blocks = getattr(result, "content", None) or []
        text = getattr(blocks[0], "text", None) if blocks else None
        payload = json.loads(text) if isinstance(text, str) else text
        if isinstance(payload, dict) and list(payload) == ["result"]:  # a returned JSON list
            return payload["result"]
        return payload


@pytest.fixture()
def tools():
    return ToolRunner(mca.build_tools())
