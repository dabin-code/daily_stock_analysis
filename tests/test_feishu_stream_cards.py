from bot.platforms.feishu_stream import FeishuReplyClient


def test_feishu_stream_chunked_send_disables_per_chunk_collapsing(monkeypatch):
    client = FeishuReplyClient.__new__(FeishuReplyClient)
    client._max_bytes = 2000
    calls = []

    def _fake_send_interactive_card(*args, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(client, "_send_interactive_card", _fake_send_interactive_card)

    assert client.send_to_chat("oc_test", "贵州茅台😀" * 500)
    assert len(calls) > 1
    assert all(call["collapse_long_content"] is False for call in calls)
