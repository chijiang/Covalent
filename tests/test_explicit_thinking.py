"""Unit tests for the explicit-thinking prompt lever: the ``<think>`` block
stripper, the streaming tag splitter, and the reasoning-fragment grouper.
"""

from __future__ import annotations

from covalent.runtime.react import ThinkTagSplitter, strip_think_blocks
from covalent.runtime.run_manager import _group_reasoning_fragments


def test_strip_think_blocks_removes_complete_blocks() -> None:
    visible, reasoning = strip_think_blocks("a<think>plan</think>b<think>more</think>c")
    assert visible == "abc"
    assert reasoning == "planmore"


def test_strip_think_blocks_captures_unclosed_tail() -> None:
    visible, reasoning = strip_think_blocks("answer<think>half a thought")
    assert visible == "answer"
    assert reasoning == "half a thought"


def test_strip_think_blocks_no_blocks_is_identity() -> None:
    visible, reasoning = strip_think_blocks("just text")
    assert visible == "just text"
    assert reasoning == ""


def test_splitter_holds_partial_open_tag_across_chunks() -> None:
    splitter = ThinkTagSplitter()
    assert splitter.feed("hello <thi") == ("hello ", "")
    # The held "<thi" completes with the next chunk; the block opens.
    assert splitter.feed("nk>plan</think>world") == ("world", "plan")
    assert splitter.flush() == ("", "")


def test_splitter_keeps_unclosed_block_as_reasoning() -> None:
    splitter = ThinkTagSplitter()
    # The block never closes: its text is reasoning, and nothing is left held.
    assert splitter.feed("<think>still thinking") == ("", "still thinking")
    assert splitter.flush() == ("", "")


def test_splitter_flush_releases_partial_close_tag_as_reasoning() -> None:
    splitter = ThinkTagSplitter()
    assert splitter.feed("<think>x</thi") == ("", "x")
    # The held "</thi" can never complete now; it belongs to the reasoning stream.
    assert splitter.flush() == ("", "</thi")


def test_splitter_passes_plain_text_through_visible() -> None:
    splitter = ThinkTagSplitter()
    assert splitter.feed("no tags here") == ("no tags here", "")
    assert splitter.feed(" more") == (" more", "")


def test_splitter_reassembles_cleanly_across_char_chunks() -> None:
    splitter = ThinkTagSplitter()
    visible_parts: list[str] = []
    reasoning_parts: list[str] = []
    for char in "x<think>why</think>y":
        visible, reasoning = splitter.feed(char)
        visible_parts.append(visible)
        reasoning_parts.append(reasoning)
    visible, reasoning = splitter.flush()
    visible_parts.append(visible)
    reasoning_parts.append(reasoning)
    assert "".join(visible_parts) == "xy"
    assert "".join(reasoning_parts) == "why"


def test_group_reasoning_fragments_merges_adjacent_same_name() -> None:
    grouped = _group_reasoning_fragments(
        [
            ("reasoning_delta", "a"),
            ("reasoning_delta", "b"),
            ("delegate_reasoning_delta", "c"),
            ("reasoning_delta", "d"),
        ]
    )
    assert grouped == [
        ("reasoning_delta", "ab"),
        ("delegate_reasoning_delta", "c"),
        ("reasoning_delta", "d"),
    ]


def test_group_reasoning_fragments_drops_empty_merges() -> None:
    assert _group_reasoning_fragments([("reasoning_delta", "")]) == []
