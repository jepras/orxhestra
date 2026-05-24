"""Deterministic tests for multimodal Content → LangChain conversion.

Covers part-order preservation, unsupported MIME handling, history-budget
trimming, and the strip_images placeholder behavior. No live model calls.
"""

from __future__ import annotations

import asyncio
import base64

from langchain_core.messages import HumanMessage

from orxhestra.agents.message_builder import MessageBuilder
from orxhestra.events.event import Event, EventType
from orxhestra.models.part import (
    Content,
    FilePart,
    TextPart,
    ToolCallPart,
    ToolResponsePart,
)


PNG_B64 = base64.b64encode(b"fake-png-bytes").decode()


def _img(mime: str = "image/png", data: str = PNG_B64) -> FilePart:
    return FilePart(inline_bytes=data, mime_type=mime)


def test_text_only_returns_plain_string():
    c = Content(parts=[TextPart(text="hi")])
    assert c.to_langchain_content() == "hi"


def test_order_preserved_text_image_text_image_text():
    c = Content(parts=[
        TextPart(text="A"),
        _img(),
        TextPart(text="B"),
        _img("image/jpeg"),
        TextPart(text="C"),
    ])
    blocks = c.to_langchain_content()
    assert isinstance(blocks, list)
    types = [(b["type"], b.get("text")) for b in blocks]
    assert types == [
        ("text", "A"),
        ("image_url", None),
        ("text", "B"),
        ("image_url", None),
        ("text", "C"),
    ]
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert blocks[3]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_only_no_text_block_emitted():
    c = Content(parts=[_img()])
    blocks = c.to_langchain_content()
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image_url"


def test_empty_text_parts_are_skipped_but_order_kept():
    c = Content(parts=[TextPart(text=""), _img(), TextPart(text="caption")])
    blocks = c.to_langchain_content()
    assert [b["type"] for b in blocks] == ["image_url", "text"]
    assert blocks[1]["text"] == "caption"


def test_non_image_fileparts_ignored():
    pdf = FilePart(inline_bytes=PNG_B64, mime_type="application/pdf")
    c = Content(parts=[TextPart(text="hello"), pdf])
    # No image present → backwards-compatible plain string
    assert c.to_langchain_content() == "hello"


def test_fileparts_without_inline_bytes_ignored():
    uri_only = FilePart(uri="s3://bucket/x.png", mime_type="image/png")
    c = Content(parts=[TextPart(text="x"), uri_only])
    assert c.to_langchain_content() == "x"


def test_strip_images_replaces_with_placeholder_in_order():
    c = Content(parts=[
        TextPart(text="before "),
        _img(),
        TextPart(text=" middle "),
        _img(),
        TextPart(text=" after"),
    ])
    out = c.to_langchain_content(strip_images=True)
    assert out == (
        "before [image omitted from history] middle "
        "[image omitted from history] after"
    )


def test_strip_images_on_text_only_unchanged():
    c = Content(parts=[TextPart(text="just text")])
    assert c.to_langchain_content(strip_images=True) == "just text"


def test_event_to_langchain_message_forwards_strip_images():
    event = Event(
        type=EventType.USER_MESSAGE,
        content=Content(parts=[TextPart(text="q"), _img()]),
    )
    full = event.to_langchain_message()
    assert isinstance(full, HumanMessage)
    assert isinstance(full.content, list)

    stripped = event.to_langchain_message(strip_images=True)
    assert isinstance(stripped, HumanMessage)
    assert stripped.content == "q[image omitted from history]"


def test_history_budget_keeps_only_most_recent_images():
    """Five user turns with one image each, budget=2 → only the last two keep images."""
    builder = MessageBuilder(
        instructions="sys",
        include_contents="default",
        max_images_in_history=2,
    )
    events = [
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id=f"inv-{i}",
            content=Content(parts=[TextPart(text=f"turn {i} "), _img()]),
        )
        for i in range(5)
    ]
    msgs = builder.events_to_messages(events)
    assert len(msgs) == 5
    # Oldest three should be stripped to plain strings; newest two are lists.
    for older in msgs[:3]:
        assert isinstance(older.content, str)
        assert "[image omitted from history]" in older.content
    for newer in msgs[3:]:
        assert isinstance(newer.content, list)
        assert any(b["type"] == "image_url" for b in newer.content)


def test_history_budget_zero_strips_all_past_images():
    builder = MessageBuilder(
        instructions="sys",
        max_images_in_history=0,
    )
    events = [
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="inv",
            content=Content(parts=[TextPart(text="t"), _img()]),
        ),
    ]
    msgs = builder.events_to_messages(events)
    assert msgs[0].content == "t[image omitted from history]"


def test_history_budget_counts_multiple_images_per_event():
    """A single event with 3 images consumes 3 of the budget."""
    builder = MessageBuilder(instructions="sys", max_images_in_history=2)
    events = [
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="inv-0",
            content=Content(parts=[
                TextPart(text="newer "),
                _img(), _img(),
            ]),
        ),
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="inv-1",
            content=Content(parts=[TextPart(text="older "), _img()]),
        ),
    ]
    # Order is iterated as given; "reversed" treats inv-1 as most recent.
    msgs = builder.events_to_messages(events)
    # inv-1 (last in list, "most recent") keeps its 1 image; budget left = 1,
    # but inv-0 needs 2 → exceeds → stripped.
    assert isinstance(msgs[0].content, str)
    assert msgs[0].content.count("[image omitted from history]") == 2
    assert isinstance(msgs[1].content, list)


class _StubCtx:
    """Minimal InvocationContext stub for build_conversation_history."""

    def __init__(self, events: list[Event]) -> None:
        self._events = events
        self.state: dict = {}
        self.branch = None
        self.invocation_id = "current-inv"

    def get_previous_final_responses(self) -> list[Event]:
        return []

    def get_events(self, **_: object) -> list[Event]:
        return list(self._events)


def test_current_turn_input_is_never_stripped():
    """The new_message Content for *this* turn must always carry its images."""
    builder = MessageBuilder(instructions="sys", max_images_in_history=0)
    history = [
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="inv-past",
            content=Content(parts=[TextPart(text="past "), _img()]),
        ),
    ]
    ctx = _StubCtx(history)
    current = Content(parts=[TextPart(text="now "), _img()])
    _, msgs = asyncio.run(builder.build_conversation_history(ctx, current))

    # Last message is the current turn — must keep its image block.
    last = msgs[-1]
    assert isinstance(last, HumanMessage)
    assert isinstance(last.content, list)
    assert any(b["type"] == "image_url" for b in last.content)

    # Earlier (history) user message had its image stripped (budget=0).
    past = msgs[-2]
    assert isinstance(past, HumanMessage)
    assert isinstance(past.content, str)
    assert "[image omitted from history]" in past.content


def test_mime_type_match_is_case_insensitive():
    """`IMAGE/PNG` should be treated as an image (RFC 2045)."""
    c = Content(parts=[
        TextPart(text="x"),
        FilePart(inline_bytes=PNG_B64, mime_type="IMAGE/PNG"),
    ])
    blocks = c.to_langchain_content()
    assert isinstance(blocks, list)
    assert blocks[-1]["type"] == "image_url"
    # MIME is echoed in the data URL as-given (provider tolerates either case).
    assert "IMAGE/PNG" in blocks[-1]["image_url"]["url"]


def test_consecutive_images_no_text_preserved_in_order():
    """`[img, img, img]` → three image_url blocks, no text blocks, in order."""
    parts = [_img("image/png"), _img("image/jpeg"), _img("image/webp")]
    c = Content(parts=parts)
    blocks = c.to_langchain_content()
    assert isinstance(blocks, list)
    assert [b["type"] for b in blocks] == ["image_url"] * 3
    mimes = [b["image_url"]["url"].split(";")[0] for b in blocks]
    assert mimes == ["data:image/png", "data:image/jpeg", "data:image/webp"]


def test_budget_walk_skips_non_user_events_correctly():
    """Interleaved AGENT_MESSAGE / TOOL_RESPONSE events must not consume budget."""
    builder = MessageBuilder(instructions="sys", max_images_in_history=2)
    events = [
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="u0",
            content=Content(parts=[TextPart(text="oldest "), _img()]),
        ),
        Event(
            type=EventType.AGENT_MESSAGE,
            session_id="s",
            invocation_id="a0",
            content=Content(parts=[
                ToolCallPart(tool_call_id="tc1", tool_name="search", args={}),
            ]),
        ),
        Event(
            type=EventType.TOOL_RESPONSE,
            session_id="s",
            invocation_id="t0",
            content=Content(parts=[
                ToolResponsePart(tool_call_id="tc1", tool_name="search", result="r"),
            ]),
        ),
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="u1",
            content=Content(parts=[TextPart(text="middle "), _img()]),
        ),
        Event(
            type=EventType.AGENT_MESSAGE,
            session_id="s",
            invocation_id="a1",
            content=Content.from_text("plain agent reply"),
        ),
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="u2",
            content=Content(parts=[TextPart(text="newest "), _img()]),
        ),
    ]
    msgs = builder.events_to_messages(events)

    # Find the three user messages in order: oldest stripped, middle and newest keep images.
    user_msgs = [m for m in msgs if isinstance(m, HumanMessage)]
    assert len(user_msgs) == 3
    assert isinstance(user_msgs[0].content, str)
    assert "[image omitted from history]" in user_msgs[0].content
    for keeper in user_msgs[1:]:
        assert isinstance(keeper.content, list)
        assert any(b["type"] == "image_url" for b in keeper.content)


def test_current_invocation_events_are_filtered_from_history():
    """Events sharing the current ctx.invocation_id must be excluded from history,
    so they don't double-count against the image budget or appear twice."""

    class _Ctx(_StubCtx):
        pass

    builder = MessageBuilder(instructions="sys", max_images_in_history=10)
    history = [
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="current-inv",  # same as ctx.invocation_id
            content=Content(parts=[TextPart(text="should not appear "), _img()]),
        ),
        Event(
            type=EventType.USER_MESSAGE,
            session_id="s",
            invocation_id="past-inv",
            content=Content.from_text("past text only"),
        ),
    ]
    ctx = _Ctx(history)
    current = Content.from_text("now")
    _, msgs = asyncio.run(builder.build_conversation_history(ctx, current))

    # Collect HumanMessage contents excluding the trailing current turn.
    human_msgs = [m for m in msgs if isinstance(m, HumanMessage)]
    # System prompt is SystemMessage; the trailing current turn is the last HumanMessage.
    assert human_msgs[-1].content == "now"
    earlier = [m.content for m in human_msgs[:-1]]
    # The current-inv event must not appear at all.
    for c in earlier:
        if isinstance(c, str):
            assert "should not appear" not in c
        else:
            for block in c:
                assert block.get("text") != "should not appear "
