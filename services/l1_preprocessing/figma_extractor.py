"""Figma design extraction — detects Figma links and extracts design context.

Uses the Figma REST API to fetch file metadata, node details, and generate
a structured DesignSpec for downstream agents.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

from models import DesignSpec

logger = structlog.get_logger()

# Regex to match Figma URLs
FIGMA_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?figma\.com/"
    r"(?:file|design|proto)/"
    r"([a-zA-Z0-9]+)"  # file_key
    r"(?:/([^?\s]*))?"  # file_name (optional)
    r"(?:\?.*?node-id=([^&\s]+))?"  # node_id (optional)
)


def detect_figma_links(text: str) -> list[dict[str, str]]:
    """Find Figma URLs in text and extract their components.

    Returns list of dicts with: url, file_key, file_name, node_id
    """
    links: list[dict[str, str]] = []
    for match in FIGMA_URL_PATTERN.finditer(text):
        links.append({
            "url": match.group(0),
            "file_key": match.group(1),
            "file_name": match.group(2) or "",
            "node_id": (match.group(3) or "").replace("%3A", ":"),
        })
    return links


class FigmaExtractor:
    """Extracts design context from Figma files via the REST API."""

    def __init__(self, api_token: str = "", client: httpx.AsyncClient | None = None) -> None:
        self._token = api_token
        self._client = client or httpx.AsyncClient(
            base_url="https://api.figma.com",
            headers={"X-Figma-Token": api_token} if api_token else {},
            timeout=30.0,
        )

    async def extract(self, figma_url: str) -> DesignSpec | None:
        """Extract a DesignSpec from a Figma URL.

        Returns None if the API token is missing or the request fails.
        """
        if not self._token:
            logger.warning("figma_api_token_not_configured")
            return None

        links = detect_figma_links(figma_url)
        if not links:
            logger.warning("no_figma_link_found", url=figma_url)
            return None

        link = links[0]
        file_key = link["file_key"]
        node_id = link["node_id"]

        log = logger.bind(file_key=file_key, node_id=node_id)

        try:
            # Fetch file metadata
            file_data = await self._fetch_file(file_key)
            if not file_data:
                return None

            # Fetch specific node if available
            node_data = None
            if node_id:
                node_data = await self._fetch_node(file_key, node_id)

            # Build design spec
            spec = self._build_spec(figma_url, file_data, node_data)
            log.info("figma_extraction_complete", components=len(spec.components))
            return spec

        except httpx.HTTPError as exc:
            log.error("figma_api_error", error=str(exc))
            return None

    async def _fetch_file(self, file_key: str) -> dict[str, Any] | None:
        """Fetch file metadata from Figma API."""
        response = await self._client.get(f"/v1/files/{file_key}?depth=2")
        if response.status_code != 200:
            logger.error("figma_file_fetch_failed", status=response.status_code)
            return None
        result: dict[str, Any] = response.json()
        return result

    async def _fetch_node(self, file_key: str, node_id: str) -> dict[str, Any] | None:
        """Fetch a specific node from a Figma file."""
        response = await self._client.get(f"/v1/files/{file_key}/nodes?ids={node_id}")
        if response.status_code != 200:
            logger.error("figma_node_fetch_failed", status=response.status_code)
            return None
        result: dict[str, Any] = response.json()
        return result

    def _build_spec(
        self,
        figma_url: str,
        file_data: dict[str, Any],
        node_data: dict[str, Any] | None,
    ) -> DesignSpec:
        """Build a DesignSpec from Figma API responses."""
        components: list[str] = []
        colors: dict[str, str] = {}
        typography: dict[str, str] = {}
        layout_patterns: list[str] = []

        # Extract components from the file
        if "components" in file_data:
            for comp_id, comp in file_data["components"].items():
                name = comp.get("name", comp_id)
                if name and name not in components:
                    components.append(name)

        # Extract from node data if available
        target = node_data or file_data
        self._walk_node_tree(
            target.get("document", target),
            components, colors, typography, layout_patterns,
        )

        # Build raw extraction text for context
        raw = f"File: {file_data.get('name', 'Unknown')}\n"
        raw += f"Components: {', '.join(components[:20])}\n"
        raw += f"Colors: {colors}\n"
        raw += f"Typography: {typography}\n"

        return DesignSpec(
            figma_url=figma_url,
            components=components[:50],
            layout_patterns=layout_patterns[:20],
            color_tokens=colors,
            typography=typography,
            raw_extraction=raw[:5000],
        )

    def _walk_node_tree(
        self,
        node: Any,
        components: list[str],
        colors: dict[str, str],
        typography: dict[str, str],
        layouts: list[str],
        depth: int = 0,
    ) -> None:
        """Recursively walk the Figma node tree to extract design details."""
        if not isinstance(node, dict) or depth > 10:
            return

        node_type = node.get("type", "")
        name = node.get("name", "")

        # Components
        if node_type in ("COMPONENT", "INSTANCE") and name and name not in components:
            components.append(name)

        # Layout
        layout_mode = node.get("layoutMode")
        if layout_mode:
            pattern = f"{layout_mode.lower()} layout"
            if name:
                pattern = f"{name}: {pattern}"
            if pattern not in layouts:
                layouts.append(pattern)

        # Colors from fills
        for fill in node.get("fills", []):
            if fill.get("type") == "SOLID" and "color" in fill:
                c = fill["color"]
                r, g, b = int(c["r"] * 255), int(c["g"] * 255), int(c["b"] * 255)
                hex_color = f"#{r:02x}{g:02x}{b:02x}"
                if name and hex_color not in colors.values():
                    colors[name] = hex_color

        # Typography from text nodes
        if node_type == "TEXT":
            style = node.get("style", {})
            font = style.get("fontFamily", "")
            size = style.get("fontSize", "")
            weight = style.get("fontWeight", "")
            if font and size:
                key = name or f"text-{len(typography)}"
                typography[key] = f"{font} {size}px {weight}".strip()

        # Recurse into children
        for child in node.get("children", []):
            self._walk_node_tree(child, components, colors, typography, layouts, depth + 1)


async def extract_from_ticket_text(
    text: str, api_token: str = ""
) -> DesignSpec | None:
    """Convenience function: detect Figma links in text and extract the first one."""
    links = detect_figma_links(text)
    if not links:
        return None

    extractor = FigmaExtractor(api_token=api_token)
    return await extractor.extract(links[0]["url"])
