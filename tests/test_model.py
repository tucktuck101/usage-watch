from usage_watch.model import (
    PartialSum,
    TokenFields,
    UsageEvent,
    UsageObservation,
    partial_sum,
    session_key,
)


def test_total_input_tokens_when_all_known():
    t = TokenFields(uncached_input_tokens=10, cache_read_input_tokens=200, cache_write_input_tokens=5)
    assert t.total_input_tokens == 215


def test_total_input_tokens_null_if_any_component_null():
    assert TokenFields(uncached_input_tokens=10, cache_read_input_tokens=200).total_input_tokens is None
    assert TokenFields().total_input_tokens is None


def test_total_input_tokens_zero_is_known_not_null():
    t = TokenFields(uncached_input_tokens=0, cache_read_input_tokens=0, cache_write_input_tokens=0)
    assert t.total_input_tokens == 0


def test_observation_and_event_derive_total_input_tokens():
    obs = UsageObservation(
        source="claude.transcript", stream_key="claude:s", source_request_key="m+r",
        confidence="authoritative", observed_at=1,
        uncached_input_tokens=1, cache_read_input_tokens=2, cache_write_input_tokens=3,
    )
    assert obs.total_input_tokens == 6
    ev = UsageEvent(accounting_observation_id=1, observed_at=1, session_key=None,
                    reconciled_version=1, uncached_input_tokens=1)
    assert ev.total_input_tokens is None


def test_partial_sum_reports_known_sum_and_unknown_count():
    s = partial_sum([10, None, 5, None, 0])
    assert s == PartialSum(known=15, unknown=2)
    assert not s.complete


def test_partial_sum_complete_and_empty():
    assert partial_sum([1, 2]).complete
    assert partial_sum([]) == PartialSum(0, 0)


def test_session_key_form():
    assert session_key("claude", "1d73") == "claude:1d73"
