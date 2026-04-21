"""Shared test fixtures for pygen/agents tests.

Adds the pygen directory to ``sys.path`` so the tests can import modules
the same way production code does (``import agents.tools_lc`` etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

PYGEN_DIR = Path(__file__).resolve().parent.parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))


import pytest


@pytest.fixture
def dummy_tool_context():
    """Minimal ToolContext-like stub for wrapper tests."""
    from tools import ToolContext  # imported here so sys.path is patched first

    class DummyBrowser:
        async def get_page_info(self):
            return {"title": "Test Page", "url": "https://example.com"}

        async def get_full_html(self):
            return "<html><body>hi</body></html>"

        async def analyze_page_structure(self):
            return {"links": 3}

        def get_captured_requests(self):
            return {"api_requests": []}

    class DummyConfig:
        qwen_api_key = "k"
        qwen_model = "test-model"
        qwen_base_url = "https://example.com/v1"

        def __init__(self):
            self.sandbox_backend = "local"
            self.sandbox_auto_start = False

    ctx = ToolContext(
        browser=DummyBrowser(),
        config=DummyConfig(),
        llm_agent=None,
        url="https://example.com",
        run_mode="enterprise_report",
        start_date="2024-01-01",
        end_date="2024-12-31",
        extra_requirements="",
        task_id="test-task",
        log_callback=lambda msg: None,
    )
    return ctx
