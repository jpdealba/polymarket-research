from sqlalchemy import text

from pmresearch.rawstore.store import RawStore


def test_dedupe_by_content_hash(settings, session):
    store = RawStore(settings, session)

    result1 = store.persist(
        source="dataapi",
        endpoint="activity",
        wallet="0xabc",
        params={"user": "0xabc", "offset": 0},
        payload=[{"a": 1}],
        http_status=200,
    )
    assert not result1.deduped
    assert result1.file_path.exists()

    result2 = store.persist(
        source="dataapi",
        endpoint="activity",
        wallet="0xabc",
        params={"user": "0xabc", "offset": 0},
        payload=[{"a": 1}],
        http_status=200,
    )
    assert result2.deduped
    assert result2.raw_fetch_id == result1.raw_fetch_id
    assert result2.file_path == result1.file_path

    count = session.execute(text("SELECT COUNT(*) FROM raw_fetches")).scalar()
    assert count == 1


def test_different_content_same_params_writes_new_row(settings, session):
    store = RawStore(settings, session)

    result1 = store.persist(
        source="dataapi",
        endpoint="activity",
        wallet="0xabc",
        params={"user": "0xabc", "offset": 0},
        payload=[{"a": 1}],
        http_status=200,
    )
    result2 = store.persist(
        source="dataapi",
        endpoint="activity",
        wallet="0xabc",
        params={"user": "0xabc", "offset": 0},
        payload=[{"a": 1}, {"a": 2}],
        http_status=200,
    )

    assert not result2.deduped
    assert result2.raw_fetch_id != result1.raw_fetch_id

    count = session.execute(text("SELECT COUNT(*) FROM raw_fetches")).scalar()
    assert count == 2


def test_file_path_layout(settings, session):
    store = RawStore(settings, session)
    result = store.persist(
        source="dataapi",
        endpoint="/activity",
        wallet="0xabc",
        params={"user": "0xabc"},
        payload=[],
        http_status=200,
    )
    relative = result.file_path.relative_to(settings.raw_dir)
    assert relative.parts[:3] == ("dataapi", "activity", "0xabc")
    assert relative.name.endswith(".json.gz")
