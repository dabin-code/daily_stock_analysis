from src.feishu_cards import (
    DEFAULT_COLLAPSE_THRESHOLD_BYTES,
    build_feishu_interactive_card,
    should_collapse_feishu_content,
)


def test_feishu_default_collapse_threshold_is_small_enough_for_compact_summaries():
    """选股紧凑摘要约 2~4 KB，阈值需 ≤ 1500 才能保证默认收起。"""
    assert DEFAULT_COLLAPSE_THRESHOLD_BYTES <= 1500


def test_feishu_default_threshold_triggers_collapse_for_compact_screening_summary():
    """常见的精简推送（标题 + 30 条单行候选 ≈ 3 KB）应默认进入折叠。"""
    compact_content = (
        "# 📣 2026-05-18 全市场筛选推荐名单\n\n"
        "> run_id: `run-001` | 模式: `balanced` | 候选数: **30**\n\n"
        "## Top 推荐\n\n"
        + "\n\n".join(
            f"### {i}. 股票{i} (60000{i}) — {90 - i}.0分 | 试探进场 | 来源: 规则输出"
            for i in range(1, 31)
        )
    )
    assert should_collapse_feishu_content(compact_content)
    card = build_feishu_interactive_card(compact_content)
    assert card["elements"][1]["tag"] == "collapsible_panel"
    assert card["elements"][1]["expanded"] is False


def test_feishu_card_keeps_short_content_flat():
    card = build_feishu_interactive_card("短消息", collapse_threshold_bytes=100)

    assert card["elements"] == [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "短消息",
            },
        }
    ]


def test_feishu_card_collapses_screening_details_after_summary():
    content = (
        "# 2026-05-18 全市场筛选推荐名单\n\n"
        "> run_id: `run-001` | 候选数: **10**\n\n"
        "## Top 推荐\n\n"
        "### 1. 贵州茅台 (600519)\n\n"
        "- 命中规则：趋势对齐\n"
    )

    card = build_feishu_interactive_card(content, collapse_threshold_bytes=20)

    assert card["elements"][0]["text"]["content"].startswith("# 2026-05-18")
    panel = card["elements"][1]
    assert panel["tag"] == "collapsible_panel"
    assert panel["expanded"] is False
    assert "## Top 推荐" in panel["elements"][0]["text"]["content"]


def test_feishu_card_limits_visible_summary_by_utf8_bytes():
    content = (
        "# 超长摘要\n\n"
        + ("😀" * 1000)
        + "\n\n## Top 推荐\n\n"
        + "### 1. 贵州茅台 (600519)"
    )

    card = build_feishu_interactive_card(content, collapse_threshold_bytes=20)

    visible_summary = card["elements"][0]["text"]["content"]
    details = card["elements"][1]["elements"][0]["text"]["content"]
    assert len(visible_summary.encode("utf-8")) < 2600
    assert "更多内容已收起" in visible_summary
    assert "## Top 推荐" in details
