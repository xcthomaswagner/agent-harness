# Design Spec Template

The design spec format is defined in `FIGMA_EXTRACTION.md` and populated by the L1 pipeline's `figma_extractor.py`.

See the `DesignSpec` model in `services/l1_preprocessing/models.py` for the authoritative field definitions:
- `components` — list of component names
- `layout_patterns` — layout descriptions
- `color_tokens` — name → hex color mapping
- `typography` — name → font spec mapping
- `rendered_frames` — paths to PNG frame renders
- `raw_extraction` — full text summary (max 5000 chars)
