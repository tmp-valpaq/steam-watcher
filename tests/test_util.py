import pytest

from src.util import BoundedDict


def test_evicts_oldest_when_over_cap():
    d = BoundedDict(3)
    for i in range(5):
        d[i] = i
    assert list(d) == [2, 3, 4]
    assert len(d) == 3


def test_rewriting_key_refreshes_eviction_order():
    d = BoundedDict(3)
    d["a"] = 1
    d["b"] = 2
    d["c"] = 3
    d["a"] = 10  # refresh: "a" becomes newest
    d["d"] = 4  # evicts "b", not "a"
    assert list(d) == ["c", "a", "d"]
    assert d["a"] == 10


def test_seeding_beyond_cap_trims_to_newest():
    seed = {str(i): i for i in range(10)}
    d = BoundedDict(4, seed)
    assert len(d) == 4
    assert list(d) == ["6", "7", "8", "9"]


def test_setdefault_and_update_respect_cap():
    d = BoundedDict(2)
    assert d.setdefault("a", 1) == 1
    assert d.setdefault("a", 99) == 1
    d.update({"b": 2, "c": 3})
    assert len(d) == 2
    assert "a" not in d


def test_rejects_nonpositive_cap():
    with pytest.raises(ValueError):
        BoundedDict(0)
