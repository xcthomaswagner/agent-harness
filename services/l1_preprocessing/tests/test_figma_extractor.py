"""Tests for Figma link detection, design extraction, and frame rendering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from figma_extractor import (
    FigmaExtractor,
    detect_figma_links,
    rendered_frames_to_attachments,
)
from models import DesignSpec


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


# --- Shared fixture data ---

_FILE_RESPONSE_JSON = {
    "name": "Test Design",
    "document": {
        "type": "DOCUMENT",
        "children": [
            {
                "type": "CANVAS",
                "id": "0:1",
                "name": "Page 1",
                "children": [
                    {
                        "type": "FRAME",
                        "id": "1:10",
                        "name": "Header",
                        "layoutMode": "HORIZONTAL",
                        "children": [
                            {
                                "type": "COMPONENT",
                                "name": "Logo",
                                "fills": [
                                    {
                                        "type": "SOLID",
                                        "color": {
                                            "r": 0.1, "g": 0.2,
                                            "b": 0.3, "a": 1,
                                        },
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
                    },
                    {
                        "type": "FRAME",
                        "id": "2:20",
                        "name": "Footer",
                        "children": [],
                    },
                ],
            }
        ],
    },
    "components": {
        "1:1": {"name": "Logo"},
        "1:2": {"name": "Button"},
    },
}

_DUMMY_REQUEST = httpx.Request("GET", "https://api.figma.com")


class TestFigmaExtractor:
    @pytest.fixture
    def mock_client(self) -> AsyncMock:
        client = AsyncMock(spec=httpx.AsyncClient)
        file_response = httpx.Response(
            200, json=_FILE_RESPONSE_JSON, request=_DUMMY_REQUEST,
        )
        client.get.return_value = file_response
        return client

    async def test_extract_returns_design_spec(
        self, mock_client: AsyncMock
    ) -> None:
        extractor = FigmaExtractor(
            api_token="test-token", client=mock_client
        )
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test"
        )

        assert spec is not None
        assert spec.figma_url == "https://www.figma.com/file/abc/Test"
        assert "Logo" in spec.components
        assert "Button" in spec.components
        assert len(spec.color_tokens) > 0
        assert "Title" in spec.typography
        assert "Inter" in spec.typography["Title"]

    async def test_extract_layout_patterns(
        self, mock_client: AsyncMock
    ) -> None:
        extractor = FigmaExtractor(
            api_token="test-token", client=mock_client
        )
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test"
        )

        assert spec is not None
        assert any("horizontal" in p.lower() for p in spec.layout_patterns)

    async def test_uses_depth_4(self, mock_client: AsyncMock) -> None:
        """Verify the file fetch uses depth=4."""
        extractor = FigmaExtractor(
            api_token="test-token", client=mock_client
        )
        await extractor.extract("https://www.figma.com/file/abc/Test")

        # First call is the file fetch
        first_call = mock_client.get.call_args_list[0]
        assert "depth=4" in first_call[0][0]

    async def test_returns_none_without_token(self) -> None:
        extractor = FigmaExtractor(api_token="")
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test"
        )
        assert spec is None

    async def test_returns_none_for_invalid_url(self) -> None:
        extractor = FigmaExtractor(api_token="test-token")
        spec = await extractor.extract("https://google.com")
        assert spec is None

    async def test_returns_none_on_api_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = httpx.Response(
            403, json={"error": "forbidden"}, request=_DUMMY_REQUEST,
        )
        extractor = FigmaExtractor(
            api_token="bad-token", client=mock_client
        )
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test"
        )
        assert spec is None

    async def test_no_rendered_frames_without_dest_dir(
        self, mock_client: AsyncMock
    ) -> None:
        """Without image_dest_dir, no frames are rendered."""
        extractor = FigmaExtractor(
            api_token="test-token", client=mock_client
        )
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test"
        )
        assert spec is not None
        assert spec.rendered_frames == []


class TestFrameRendering:
    @pytest.fixture
    def mock_client_with_images(self) -> AsyncMock:
        """Mock client that handles both file fetch and image render."""
        client = AsyncMock(spec=httpx.AsyncClient)

        file_resp = httpx.Response(
            200, json=_FILE_RESPONSE_JSON, request=_DUMMY_REQUEST,
        )
        image_api_resp = httpx.Response(
            200,
            json={
                "err": None,
                "images": {
                    "1:10": "https://figma-s3.amazonaws.com/img1.png",
                    "2:20": "https://figma-s3.amazonaws.com/img2.png",
                },
            },
            request=_DUMMY_REQUEST,
        )
        # Image download response (fake PNG bytes)
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        img_download_resp = httpx.Response(
            200, content=png_bytes, request=_DUMMY_REQUEST,
        )

        async def routed_get(url: str, **kwargs: object) -> httpx.Response:
            if "/v1/files/" in url:
                return file_resp
            if "/v1/images/" in url:
                return image_api_resp
            # Image download (S3 URL)
            return img_download_resp

        client.get = AsyncMock(side_effect=routed_get)
        return client

    async def test_renders_frames_to_files(
        self, mock_client_with_images: AsyncMock, tmp_path: Path
    ) -> None:
        extractor = FigmaExtractor(
            api_token="test-token", client=mock_client_with_images
        )
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test",
            image_dest_dir=str(tmp_path),
        )

        assert spec is not None
        assert len(spec.rendered_frames) == 2
        for path in spec.rendered_frames:
            assert Path(path).exists()
            assert Path(path).stat().st_size > 0

    async def test_rendered_frame_filenames(
        self, mock_client_with_images: AsyncMock, tmp_path: Path
    ) -> None:
        extractor = FigmaExtractor(
            api_token="test-token", client=mock_client_with_images
        )
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test",
            image_dest_dir=str(tmp_path),
        )

        assert spec is not None
        names = [Path(p).name for p in spec.rendered_frames]
        assert "figma-Header.png" in names
        assert "figma-Footer.png" in names

    async def test_image_api_error_returns_empty_frames(
        self, tmp_path: Path
    ) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        file_resp = httpx.Response(
            200, json=_FILE_RESPONSE_JSON, request=_DUMMY_REQUEST,
        )
        image_err_resp = httpx.Response(
            500, json={"err": "server error"}, request=_DUMMY_REQUEST,
        )

        async def routed_get(url: str, **kwargs: object) -> httpx.Response:
            if "/v1/files/" in url:
                return file_resp
            return image_err_resp

        client.get = AsyncMock(side_effect=routed_get)

        extractor = FigmaExtractor(
            api_token="test-token", client=client
        )
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test",
            image_dest_dir=str(tmp_path),
        )

        assert spec is not None
        assert spec.rendered_frames == []

    async def test_specific_node_id_renders_that_frame(
        self, mock_client_with_images: AsyncMock, tmp_path: Path
    ) -> None:
        """When URL has node-id pointing to a specific frame, render it."""
        extractor = FigmaExtractor(
            api_token="test-token", client=mock_client_with_images
        )
        spec = await extractor.extract(
            "https://www.figma.com/file/abc/Test?node-id=1:10",
            image_dest_dir=str(tmp_path),
        )

        assert spec is not None
        # Should have requested images for just the one node
        image_calls = [
            c for c in mock_client_with_images.get.call_args_list
            if "/v1/images/" in str(c)
        ]
        assert len(image_calls) == 1
        call_url = str(image_calls[0])
        assert "1:10" in call_url


class TestGetRenderableFrameIds:
    def test_page_node_returns_child_frames(self) -> None:
        extractor = FigmaExtractor(api_token="test")
        ids = extractor._get_renderable_frame_ids(
            _FILE_RESPONSE_JSON, "0:1"
        )
        assert len(ids) == 2
        assert ids[0] == ("1:10", "Header")
        assert ids[1] == ("2:20", "Footer")

    def test_specific_frame_returns_just_that(self) -> None:
        extractor = FigmaExtractor(api_token="test")
        ids = extractor._get_renderable_frame_ids(
            _FILE_RESPONSE_JSON, "1:10"
        )
        assert len(ids) == 1
        assert ids[0] == ("1:10", "Header")

    def test_no_node_id_returns_top_frames(self) -> None:
        extractor = FigmaExtractor(api_token="test")
        ids = extractor._get_renderable_frame_ids(
            _FILE_RESPONSE_JSON, ""
        )
        assert len(ids) == 2

    def test_dash_format_node_id(self) -> None:
        """node-id=0-1 (dash) should match page id 0:1 (colon)."""
        extractor = FigmaExtractor(api_token="test")
        ids = extractor._get_renderable_frame_ids(
            _FILE_RESPONSE_JSON, "0-1"
        )
        assert len(ids) == 2


class TestRenderedFramesToAttachments:
    def test_converts_existing_files(self, tmp_path: Path) -> None:
        img1 = tmp_path / "figma-Header.png"
        img1.write_bytes(b"\x89PNG" + b"\x00" * 10)
        img2 = tmp_path / "figma-Footer.png"
        img2.write_bytes(b"\x89PNG" + b"\x00" * 10)

        spec = DesignSpec(
            figma_url="https://figma.com/file/abc/Test",
            rendered_frames=[str(img1), str(img2)],
        )
        atts = rendered_frames_to_attachments(spec)

        assert len(atts) == 2
        assert atts[0].filename == "figma-Header.png"
        assert atts[0].content_type == "image/png"
        assert atts[0].local_path == str(img1)
        assert atts[0].is_design_image is True

    def test_skips_missing_files(self) -> None:
        spec = DesignSpec(
            figma_url="https://figma.com/file/abc/Test",
            rendered_frames=["/nonexistent/figma-Gone.png"],
        )
        atts = rendered_frames_to_attachments(spec)
        assert len(atts) == 0
