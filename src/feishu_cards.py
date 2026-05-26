"""Helpers for building Feishu interactive cards."""

from __future__ import annotations

from typing import Any, Dict, Tuple


DEFAULT_COLLAPSE_THRESHOLD_BYTES = 1500
SUMMARY_PREVIEW_BYTES = 2000


def should_collapse_feishu_content(
    content: str,
    threshold_bytes: int = DEFAULT_COLLAPSE_THRESHOLD_BYTES,
) -> bool:
    """Return True when Feishu content should be hidden behind a collapsed panel."""
    return len((content or "").encode("utf-8")) > threshold_bytes


def build_feishu_interactive_card(
    content: str,
    *,
    title: str = "A股智能分析报告",
    collapse_long_content: bool = True,
    collapse_threshold_bytes: int = DEFAULT_COLLAPSE_THRESHOLD_BYTES,
) -> Dict[str, Any]:
    """Build a Feishu interactive card, collapsing long content by default."""
    content = content or ""
    if collapse_long_content and should_collapse_feishu_content(content, collapse_threshold_bytes):
        summary, details = split_feishu_summary_and_details(content)
        elements = [
            _markdown_div(summary),
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "展开查看完整内容",
                    }
                },
                "elements": [_markdown_div(details)],
            },
        ]
    else:
        elements = [_markdown_div(content)]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": title,
            }
        },
        "elements": elements,
    }


def split_feishu_summary_and_details(content: str) -> Tuple[str, str]:
    """Split a Markdown notification into visible summary and collapsed details."""
    content = (content or "").strip()
    if not content:
        return "内容为空", "内容为空"

    marker_index = _find_first_detail_marker(content)
    if marker_index > 0:
        summary = content[:marker_index].strip()
        details = content[marker_index:].strip()
        summary, overflow = _preview_with_overflow(summary)
        if overflow:
            details = f"{overflow.strip()}\n\n{details}".strip()
        return summary or _preview(content), details or content

    return _preview(content), content


def _find_first_detail_marker(content: str) -> int:
    markers = (
        "\n## Top 推荐",
        "\n## 今日结果",
        "\n### ",
        "\n**Top 推荐**",
        "\n**今日结果**",
    )
    indexes = [content.find(marker) for marker in markers]
    candidates = [idx for idx in indexes if idx > 0]
    return min(candidates) if candidates else -1


def _preview(content: str) -> str:
    preview, overflow = _preview_with_overflow(content)
    if not overflow:
        return preview
    return f"{preview.rstrip()}\n\n...内容较长，点击下方折叠面板查看完整内容。"


def _preview_with_overflow(content: str) -> Tuple[str, str]:
    if len(content.encode("utf-8")) <= SUMMARY_PREVIEW_BYTES:
        return content, ""

    raw = content.encode("utf-8")
    preview = raw[:SUMMARY_PREVIEW_BYTES].decode("utf-8", errors="ignore").rstrip()
    overflow = content[len(preview):]
    return (
        f"{preview}\n\n...摘要较长，更多内容已收起。",
        overflow,
    )


def _markdown_div(content: str) -> Dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": content,
        },
    }
