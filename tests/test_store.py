from app.store import KIND_ALERT, KIND_MESSAGE, STATUS_FAILED, STATUS_PENDING, Store

from tests.conftest import FakeClock


def test_enqueue_dedupes_and_claims_in_order(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    assert store.enqueue(KIND_MESSAGE, "wamid.1", {"a": 1}, clock())
    assert not store.enqueue(KIND_MESSAGE, "wamid.1", {"a": 2}, clock())
    clock.advance(1)
    assert store.enqueue(KIND_MESSAGE, "wamid.2", {"a": 2}, clock())
    first = store.claim_next(clock())
    assert (
        first is not None and first.key == "wamid.1" and first.attempts == 1 and first.status == "processing"
    )
    second = store.claim_next(clock())
    assert second is not None and second.key == "wamid.2"
    assert store.claim_next(clock()) is None
    store.mark_done(first.id, emails_sent=1, now=clock())
    assert store.get("wamid.1").status == "done"
    assert store.stats(clock())["done"] == 1


def test_retry_failed_requeue_purge(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    store.enqueue(KIND_MESSAGE, "wamid.1", {}, clock())
    item = store.claim_next(clock())
    store.mark_retry(item.id, clock() + 30, "boom")
    assert store.claim_next(clock()) is None  # not due yet
    assert store.next_due_at() == clock() + 30
    clock.advance(31)
    item = store.claim_next(clock())
    assert item is not None and item.attempts == 2 and item.last_error == "boom"
    store.mark_failed(item.id, "gave up", clock())
    assert [i.key for i in store.list_by_status(STATUS_FAILED)] == ["wamid.1"]
    assert store.requeue("wamid.1", clock()) == 1
    item = store.get("wamid.1")
    assert item.status == STATUS_PENDING and item.attempts == 0 and item.first_attempt_at is None
    item = store.claim_next(clock())
    store.mark_done(item.id, now=clock())
    assert store.purge_finished(clock() - 1) == 0
    assert store.purge_finished(clock() + 1) == 1


def test_recover_processing_and_alerts(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    store.enqueue(KIND_MESSAGE, "wamid.1", {}, clock())
    store.claim_next(clock())
    assert store.recover_processing(clock()) == 1
    assert store.get("wamid.1").status == STATUS_PENDING
    assert store.enqueue(KIND_ALERT, "alert:1", {"subject": "s", "body": "b"}, clock())
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    assert store.get_meta("missing") is None
