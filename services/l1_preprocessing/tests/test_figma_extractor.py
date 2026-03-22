"""Tests for Figma link detection and design extraction."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from figma_extractor import FigmaExtractor, detect_figma_links


class TestDetectFigmaLinks:
    def test_standard_file_url(self) -> None:
        text = "See design at https://www.figma.com/file/abc123/MyDesign?node-id=1:2"
        links = detect_figma_links(text)
        assert len(links) == 1
        assert links[0]["file_key"] == "abc123"
        assert links[0]["file_name"] == "MyDesign"
        assert links[0]["node_id"] == "1:2"

    def test_design_url(self) -> None:
        text = "https://www.figma.com/design/xyz789/Dashboard"
        links = detect_figma_links(text)
        assert len(links) == 1
        assert links[0]["file_key"] == "xyz789"

    def test_proto_url(self) -> None:
        text = "Prototype: https://figma.com/proto/def456/Login-Flow"
        links = detect_figma_links(text)
        assert len(links) == 1
        assert links[0]["file_key"] == "def456"

    def test_no_figma_link(self) -> None:
        text = "No design link here, just a regular ticket description."
        assert detect_figma_links(text) == []

    def test_multiple_links(self) -> None:
        text = (
            "Desktop: https://www.figma.com/file/aaa/Desktop\n"
            "Mobile: https://www.figma.com/file/bbb/Mobile"
        )
        links = detect_figma_links(text)
        assert len(links) == 2
        assert links[0]["file_key"] == "aaa"
        assert links[1]["file_key"] == "bbb"

    def test_encoded_node_id(self) -> None:
        text = "https://www.figma.com/file/abc/Name?node-id=123%3A456"
        links = detect_figma_links(text)
        assert links[0]["node_id"] == "123:456"

    def test_url_without_node_id(self) -> None:
        text = "https://www.figma.com/file/abc/Name"
        links = detect_figma_links(text)
        assert links[0]["node_id"] == ""


class TestFigmaExtractor:
    @pytest.fixture
    def mock_client(self) -> AsyncMock:
        client = AsyncMock(spec=httpx.AsyncClient)
        dummy_request = httpx.Request("GET", "https://api.figma.com")

        # File response
        file_response = httpx.Response(
            200,
            json={
                "name": "Test Design",
                "document": {
                    "type": "DOCUMENT",
                    "children": [
                        {
                            "type": "CANVAS",
                            "name": "Page 1",
                            "children": [
                                {
                                    "type": "FRAME",
                                    "name": "Header",
                                    "layoutMode": "HORIZONTAL",
                                    "children": [
                                        {
                                            "type": "COMPONENT",
                                            "name": "Logo",
                                            "fills": [
                                                {
                                                    "type": "SOLID",
                                                    "color": {"r": 0.1, "g": 0.2, "b": 0.3, "a": 1},
                                                }
                                            ],
                                            "children": [],
                                        },
                                        {
                                            "type": "TEXT",
                                            "name": "Title",
                                            "style": {
                                                "fontFamily": "Inter",
                                                "fontSize": 24,
                                                "fontWeight": 700,
                                            },
                                            "children": [],
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                },
                "components": {
                    "1:1": {"name": "Logo"},
                    "1:2": {"name": "Button"},
                },
            },
            request=dummy_request,
        )
        client.get.return_value = file_response
        return client

    async def test_extract_returns_design_spec(self, mock_client: AsyncMock) -> None:
        extractor = FigmaExtractor(api_token="test-token", client=mock_client)
        spec = await extractor.extract("https://www.figma.com/file/abc/Test")

        assert spec is not None
        assert spec.figma_url == "https://www.figma.com/file/abc/Test"
        assert "Logo" in spec.components
        assert "Button" in spec.components
        assert len(spec.color_tokens) > 0
        assert "Title" in spec.typography
        assert "Inter" in spec.typography["Title"]

    async def test_extract_layout_patterns(self, mock_client: AsyncMock) -> None:
        extractor = FigmaExtractor(api_token="test-token", client=mock_client)
        spec = await extractor.extract("https://www.figma.com/file/abc/Test")

        assert spec is not None
        assert any("horizontal" in p.lower() for p in spec.layout_patterns)

    async def test_returns_none_without_token(self) -> None:
        extractor = FigmaExtractor(api_token="")
        spec = await extractor.extract("https://www.figma.com/file/abc/Test")
        assert spec is None

    async def test_returns_none_for_invalid_url(self) -> None:
        extractor = FigmaExtractor(api_token="test-token")
        spec = await extractor.extract("https://google.com")
        assert spec is None

    async def test_returns_none_on_api_error(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = httpx.Response(
            403, json={"error": "forbidden"},
            request=httpx.Request("GET", "https://api.figma.com"),
        )
        extractor = FigmaExtractor(api_token="bad-token", client=mock_client)
        spec = await extractor.extract("https://www.figma.com/file/abc/Test")
        assert spec is None
