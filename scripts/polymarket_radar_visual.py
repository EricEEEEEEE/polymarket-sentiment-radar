#!/usr/bin/env python3
"""Source-bound TG Watch hero renderer for the Polymarket radar."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, PngImagePlugin

import tg_watch_layout as L


CANVAS = (233, 236, 241)
CARD = (255, 255, 255)
INK = (17, 24, 33)
MIN_BODY_FONT = 36
MIN_METADATA_FONT = 36
MIN_TITLE_FONT = 48

CJK_FONT_PATHS = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansCN-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


class VisualBundleError(ValueError):
    """Raised when visual evidence cannot be traced to the render payload."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _path_tokens(path: str) -> list[str | int]:
    if path == "$":
        return []
    if not path.startswith("$."):
        raise VisualBundleError(f"unsupported source path: {path}")
    tokens: list[str | int] = []
    for name, index in re.findall(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]", path[2:]):
        if name:
            tokens.append(name)
        elif index:
            tokens.append(int(index))
    return tokens


def _resolve(value: Any, path: str) -> Any:
    current = value
    for token in _path_tokens(path):
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                raise VisualBundleError(f"path does not resolve: {path}")
            current = current[token]
        else:
            if not isinstance(current, Mapping) or token not in current:
                raise VisualBundleError(f"path does not resolve: {path}")
            current = current[token]
    return current


def validate_visual_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    source = bundle.get("source_payload")
    visual = bundle.get("visual_spec")
    render = bundle.get("render_spec")
    if not isinstance(source, Mapping) or not isinstance(visual, Mapping) or not isinstance(render, Mapping):
        raise VisualBundleError("bundle requires source_payload, visual_spec, and render_spec objects")
    if visual.get("selected_modality") != "image" or visual.get("grammar") != "hero-card":
        raise VisualBundleError("Polymarket front page requires image + hero-card")
    if render.get("kind") != "hero":
        raise VisualBundleError("hero-card must compile to RenderSpec kind=hero")

    evidence_paths = {
        str(item.get("source_path"))
        for item in visual.get("evidence", [])
        if isinstance(item, Mapping) and item.get("source_path")
    }
    bindings = render.get("source_bindings")
    if not isinstance(bindings, Mapping) or not bindings:
        raise VisualBundleError("RenderSpec.source_bindings is required")

    checked: list[str] = []
    for target_path, binding in bindings.items():
        if not isinstance(binding, Mapping):
            raise VisualBundleError(f"invalid binding for {target_path}")
        source_path = str(binding.get("source_path") or "")
        if not source_path or source_path not in evidence_paths:
            raise VisualBundleError(f"{target_path} is not backed by VisualSpec evidence")
        source_value = _resolve(source, source_path)
        render_value = _resolve(render, f"$.{target_path}")
        if type(source_value) is not type(render_value) or source_value != render_value:
            raise VisualBundleError(f"{target_path} differs from {source_path}")
        checked.append(str(target_path))

    required = {
        "title",
        "subtitle",
        "data.section_label",
        "data.headline",
        "data.value_label",
        "data.value",
        "data.probability",
        "data.delta_24h",
        "data.volume",
        "data.why_care",
        "meta.source",
        "meta.timestamp",
        "meta.version",
    }
    missing = sorted(required - set(checked))
    if missing:
        raise VisualBundleError("missing source bindings: " + ", ".join(missing))
    return {
        "status": "verified",
        "binding_count": len(checked),
        "visual_spec_sha256": payload_digest(visual),
        "render_spec_sha256": payload_digest(render),
        "source_bindings_sha256": payload_digest(bindings),
    }


def _hex_color(value: str) -> tuple[int, int, int]:
    raw = str(value).strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return (37, 99, 235)
    return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))


def _headline_size(text: str, max_width: int) -> int:
    for size in (58, 54, 50, 48):
        lines = L.wrap_text(text, size, True, max_width, 2)
        if lines and not lines[-1].endswith("…"):
            return size
    raise VisualBundleError("headline cannot fit in two lines at the mobile-safe font floor")


def _cjk_sample(value: Any, limit: int = 16) -> str:
    sample = "".join(
        character
        for character in canonical_json(value)
        if 0x3400 <= ord(character) <= 0x9FFF
    )
    return "".join(dict.fromkeys(sample))[:limit]


def _font_path() -> str:
    return next((path for path in CJK_FONT_PATHS if Path(path).is_file()), "")


def _annotate_png(
    path: Path,
    bundle: Mapping[str, Any],
    validation: Mapping[str, Any],
    title_font: int,
) -> dict[str, str]:
    visual = bundle["visual_spec"]
    render = bundle["render_spec"]
    source = bundle["source_payload"]
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    font_path = _font_path()
    metadata = {
        "render_spec_version": str(render.get("version", "1.0")),
        "render_kind": str(render["kind"]),
        "title": str(render["title"]),
        "source": str(render["meta"]["source"]),
        "timestamp": str(render["meta"]["timestamp"]),
        "render_spec_sha256": str(validation["render_spec_sha256"]),
        "visual_spec_sha256": str(validation["visual_spec_sha256"]),
        "source_bindings_sha256": str(validation["source_bindings_sha256"]),
        "content_bbox": canonical_json([70, 70, width - 70, height - 70]),
        "card_bbox": canonical_json([26, 26, width - 26, height - 26]),
        "canvas_background_color": "#e9ecf1",
        "background_color": "#ffffff",
        "foreground_color": "#111821",
        "cjk_required": "true",
        "cjk_sample": _cjk_sample(source),
        "font_path": font_path,
        "source_binding_status": "verified",
        "source_binding_warning": "",
        "traceability_status": "verified",
        "selected_modality": str(visual["selected_modality"]),
        "decision_grammar": str(visual["grammar"]),
        "min_title_font_px": str(title_font),
        "min_body_font_px": str(MIN_BODY_FONT),
        "min_metadata_font_px": str(MIN_METADATA_FONT),
        "text_truncated": "false",
        "render_engine": "tg-watch-layout+pillow",
    }
    pnginfo = PngImagePlugin.PngInfo()
    for key, value in metadata.items():
        pnginfo.add_text(key, value)
    image.save(path, format="PNG", optimize=True, pnginfo=pnginfo)
    return metadata


def render_front_page(bundle: Mapping[str, Any], output_path: Path) -> dict[str, Any]:
    validation = validate_visual_bundle(bundle)
    render = bundle["render_spec"]
    data = render["data"]
    meta = render["meta"]
    accent = _hex_color(str(render.get("accent", "#2563eb")))
    palette = L.light_palette(accent=accent)
    headline = str(data["headline"])
    title_font = _headline_size(headline, 1060)
    probability = float(data["probability"])
    if not 0.0 <= probability <= 1.0:
        raise VisualBundleError("probability must be between 0 and 1")

    stat_delta = L.Column(
        [
            L.Text("24 小时变化", MIN_METADATA_FONT, palette.muted, bold=True),
            L.Gap(12),
            L.Text(str(data["delta_24h"]), 52, palette.text, bold=True),
        ],
        pad=(22, 24, 24, 24),
        bg=palette.panel,
        radius=16,
        border=palette.line,
    )
    stat_volume = L.Column(
        [
            L.Text("24 小时成交", MIN_METADATA_FONT, palette.muted, bold=True),
            L.Gap(12),
            L.Text(str(data["volume"]), 52, palette.text, bold=True),
        ],
        pad=(22, 24, 24, 24),
        bg=palette.panel,
        radius=16,
        border=palette.line,
    )
    probability_block = L.Column(
        [
            L.Text(str(data["value_label"]), MIN_METADATA_FONT, palette.muted, bold=True),
            L.Gap(12),
            L.Text(str(data["value"]), 96, palette.accent, bold=True),
            L.Gap(18),
            L.Bar(probability, palette.line, palette.accent, height=28),
            L.Gap(10),
            L.Row(
                [
                    L.Text("0%", MIN_METADATA_FONT, palette.muted),
                    L.Text("100%", MIN_METADATA_FONT, palette.muted, align="right"),
                ],
                widths=[None, None],
            ),
        ],
        pad=(28, 30, 28, 30),
        bg=palette.panel,
        radius=18,
        border=palette.line,
    )

    children: list[L.Node] = [
        L.Row(
            [
                L.Text("NE", 40, palette.accent, bold=True),
                L.Text(str(render["title"]), 40, palette.text, bold=True),
                L.Text(str(meta["timestamp"]), MIN_METADATA_FONT, palette.muted, align="right"),
            ],
            gap=18,
            widths=[90, None, 390],
            valign="center",
        ),
        L.Gap(20),
        L.Divider(palette.line),
        L.Gap(26),
        L.Text(
            f"{data['section_label']} · {render['subtitle']}",
            MIN_METADATA_FONT,
            palette.accent,
            bold=True,
            max_lines=1,
        ),
        L.Gap(18),
        L.Text(headline, title_font, palette.text, bold=True, max_lines=2, leading=1.25),
        L.Gap(28),
        probability_block,
        L.Gap(22),
        L.Row([stat_delta, stat_volume], gap=22, widths=[None, None]),
        L.Gap(24),
        L.Column(
            [
                L.Text("为什么值得看", MIN_METADATA_FONT, palette.accent, bold=True),
                L.Gap(12),
                L.Text(str(data["why_care"]), 40, palette.text, max_lines=3, leading=1.35),
            ],
            pad=(24, 28, 26, 28),
            bg=palette.card,
            radius=16,
            border=palette.line,
        ),
        L.Gap(28),
        L.Divider(palette.line),
        L.Gap(20),
        L.Text(
            f"来源：{meta['source']} · {meta['timestamp']}",
            MIN_METADATA_FONT,
            palette.muted,
            max_lines=2,
        ),
        L.Gap(10),
        L.Text(
            f"概率是市场定价，不代表事实或建议 · 只读监控 · {meta['version']}",
            MIN_METADATA_FONT,
            palette.muted,
            max_lines=2,
        ),
    ]

    output_path = Path(output_path)
    L.render_card(
        output_path,
        children=children,
        palette=palette,
        width=1200,
        margin=26,
        pad=44,
        radius=34,
        rail_color=accent,
        min_height=1040,
    )
    metadata = _annotate_png(output_path, bundle, validation, title_font)
    return {
        "image_path": str(output_path),
        "render_engine": metadata["render_engine"],
        "font_warning": not bool(metadata["font_path"]),
        "validation": dict(validation),
        "metadata": metadata,
    }
