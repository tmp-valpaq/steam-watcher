"""Small shared helpers."""

from typing import Any


class BoundedDict(dict):
    """Dict with a hard size cap; oldest-written entries are evicted first.

    Re-setting an existing key moves it to the back of the eviction order, so
    hot cache entries survive as long as they keep being refreshed. Reads do
    not affect order — every user of this class rewrites entries on use.
    """

    def __init__(self, max_size: int, *args: Any, **kwargs: Any):
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        super().__init__(*args, **kwargs)
        self._evict()

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            super().__delitem__(key)
        super().__setitem__(key, value)
        self._evict()

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key in self:
            return self[key]
        self[key] = default
        return default

    def update(self, *args: Any, **kwargs: Any) -> None:
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def _evict(self) -> None:
        while len(self) > self._max_size:
            del self[next(iter(self))]
