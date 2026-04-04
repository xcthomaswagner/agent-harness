"""Figma design extraction — detects Figma links and extracts design context.

Uses the Figma REST API to fetch file metadata, node details, render frames
as PNG images, and generate a structured DesignSpec for downstream agents.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import structlog

from models import Attachment, DesignSpec

logger = structlog.get_logger()

# Regex to match Figma URLs
FIGMA_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?figma\.com/"
    r"(?:file|design|proto)/"
    r"([a-zA-Z0-9]+)"  # file_key
    r"(?:/([^?\s]*))?"  # file_name (optional)
    r"(?:\?.*?node-id=([^&\s]+))?"  # node_id (optional)
)

# Max frames to render as images (avoid huge API calls)
MAX_RENDERED_FRAMES = 5


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

    def __init__(
        self,
        api_token: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = api_token
        self._client = client or httpx.AsyncClient(
            base_url="https://api.figma.com",
            headers={"X-Figma-Token": api_token} if api_token else {},
            timeout=30.0,
        )

    async def extract(
        self,
        figma_url: str,
        image_dest_dir: str = "",
    ) -> DesignSpec | None:
        """Extract a DesignSpec from a Figma URL.

        Args:
            figma_url: The Figma file/design URL.
            image_dest_dir: Directory to save rendered frame PNGs.
                If empty, frame rendering is skipped.

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
            # Fetch file metadata (depth=4 to capture nested components)
            file_data = await self._fetch_file(file_key)
            if not file_data:
                return None

            # Fetch specific node if available
            node_data = None
            if node_id:
                node_data = await self._fetch_node(file_key, node_id)

            # Build design spec from structured data
            spec = self._build_spec(figma_url, file_data, node_data)

            # Render frames as PNG images
            if image_dest_dir:
                frame_ids = self._get_renderable_frame_ids(
                    file_data, node_id
                )
                if frame_ids:
                    rendered = await self._render_frames(
                        file_key, frame_ids, image_dest_dir
                    )
                    spec.rendered_frames = rendered
                    log.info(
                        "figma_frames_rendered",
                        count=len(rendered),
                    )

            log.info(
                "figma_extraction_complete",
                components=len(spec.components),
                colors=len(spec.color_tokens),
                rendered=len(spec.rendered_frames),
            )
            return spec

        except httpx.HTTPError as exc:
            log.error("figma_api_error", error=str(exc))
            return None

    async def _fetch_file(
        self, file_key: str
    ) -> dict[str, Any] | None:
        """Fetch file metadata from Figma API."""
        response = await self._client.get(
            f"/v1/files/{file_key}?depth=4"
        )
        if response.status_code != 200:
            logger.error(
                "figma_file_fetch_failed", status=response.status_code
            )
            return None
        result: dict[str, Any] = response.json()
        return result

    async def _fetch_node(
        self, file_key: str, node_id: str
    ) -> dict[str, Any] | None:
        """Fetch a specific node from a Figma file."""
        response = await self._client.get(
            f"/v1/files/{file_key}/nodes?ids={node_id}"
        )
        if response.status_code != 200:
            logger.error(
                "figma_node_fetch_failed", status=response.status_code
            )
            return None
        result: dict[str, Any] = response.json()
        return result

    def _get_renderable_frame_ids(
        self,
        file_data: dict[str, Any],
        node_id: str,
    ) -> list[tuple[str, str]]:
        """Get frame IDs to render as images.

        If node_id points to a specific frame, render just that.
        Otherwise render top-level frames from the first page.

        Returns list of (node_id, frame_name) tuples.
        """
        doc = file_data.get("document", {})
        pages = doc.get("children", [])
        if not pages:
            return []

        first_page = pages[0]
        frames = first_page.get("children", [])

        # If node_id is a page (like 0-1 or 0:1), render its child frames
        page_ids = {p.get("id", "") for p in pages}
        if node_id in page_ids or node_id.replace("-", ":") in page_ids:
            return [
                (f["id"], f.get("name", f["id"]))
                for f in frames[:MAX_RENDERED_FRAMES]
                if f.get("type") == "FRAME"
            ]

        # If node_id is a specific frame, render just that
        if node_id:
            for f in frames:
                if f.get("id") == node_id:
                    return [(node_id, f.get("name", node_id))]
            # Node might be deeper — render it anyway
            return [(node_id, node_id)]

        # No node_id — render top-level frames
        return [
            (f["id"], f.get("name", f["id"]))
            for f in frames[:MAX_RENDERED_FRAMES]
            if f.get("type") == "FRAME"
        ]

    async def _render_frames(
        self,
        file_key: str,
        frame_ids: list[tuple[str, str]],
        dest_dir: str,
    ) -> list[str]:
        """Render Figma frames as PNG images via the Image API.

        Returns list of local file paths for successfully rendered frames.
        """
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        ids_param = ",".join(fid for fid, _ in frame_ids)
        response = await self._client.get(
            f"/v1/images/{file_key}?ids={ids_param}"
            f"&format=png&scale=2"
        )
        if response.status_code != 200:
            logger.error(
                "figma_image_api_failed", status=response.status_code
            )
            return []

        data = response.json()
        if data.get("err"):
            logger.error("figma_image_api_error", error=data["err"])
            return []

        images = data.get("images", {})
        rendered: list[str] = []

        # Build name lookup
        name_map = {fid: name for fid, name in frame_ids}

        for fid, url in images.items():
            if not url:
                continue
            name = name_map.get(fid, fid)
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
            file_path = dest / f"figma-{safe_name}.png"

            try:
                img_resp = await self._client.get(
                    url, follow_redirects=True
                )
                if img_resp.status_code == 200:
                    file_path.write_bytes(img_resp.content)
                    rendered.append(str(file_path))
                    logger.info(
                        "figma_frame_saved",
                        frame=name,
                        path=str(file_path),
                        size=len(img_resp.content),
                    )
                else:
                    logger.warning(
                        "figma_frame_download_failed",
                        frame=name,
                        status=img_resp.status_code,
                    )
            except httpx.HTTPError as exc:
                logger.error(
                    "figma_frame_download_error",
                    frame=name,
                    error=str(exc),
                )

        return rendered

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
        raw += f"Layouts: {', '.join(layout_patterns[:10])}\n"

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

        # Colors from fills (values are 0-1 floats, clamp to 0-255)
        for fill in node.get("fills", []):
            if fill.get("type") == "SOLID" and "color" in fill:
                c = fill["color"]
                try:
                    r = max(0, min(255, int(c.get("r", 0) * 255)))
                    g = max(0, min(255, int(c.get("g", 0) * 255)))
                    b = max(0, min(255, int(c.get("b", 0) * 255)))
                except (TypeError, KeyError):
                    continue
                hex_color = f"#{r:02x}{g:02x}{b:02x}"
                key = name or f"fill-{len(colors)}"
                if hex_color not in colors.values():
                    colors[key] = hex_color

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
            self._walk_node_tree(
                child, components, colors, typography, layouts, depth + 1
            )


def rendered_frames_to_attachments(
    spec: DesignSpec,
) -> list[Attachment]:
    """Convert rendered Figma frame paths into Attachment objects.

    These can be merged into the ticket's attachment list so the analyst
    sees them as vision content blocks and L2 agents get them in the
    worktree.
    """
    attachments: list[Attachment] = []
    for path in spec.rendered_frames:
        p = Path(path)
        if p.exists():
            attachments.append(Attachment(
                filename=p.name,
                url=spec.figma_url,
                content_type="image/png",
                local_path=str(p),
            ))
    return attachments


async def extract_from_ticket_text(
    text: str, api_token: str = "", image_dest_dir: str = ""
) -> DesignSpec | None:
    """Convenience: detect Figma links in text and extract the first one."""
    links = detect_figma_links(text)
    if not links:
        return None

    extractor = FigmaExtractor(api_token=api_token)
    return await extractor.extract(
        links[0]["url"], image_dest_dir=image_dest_dir
    )
