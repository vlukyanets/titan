from __future__ import annotations

import time

from titan.storage.ids import uuid7


def test_version_and_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"


def test_embeds_current_unix_milliseconds() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= value.int >> 80 <= after + 1


def test_strictly_increasing_within_a_process() -> None:
    values = [uuid7() for _ in range(20_000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)
