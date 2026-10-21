from __future__ import annotations

from portal.sessions import SessionStore


def test_idle_and_absolute_expiry() -> None:
    store = SessionStore(idle_s=10, max_s=100)
    s = store.new(now=0)
    assert store.get(s.id, now=9) is s
    assert store.get(s.id, now=18) is s  # idle timer was reset at 9
    assert store.get(s.id, now=29) is None
    t = store.new(now=0)
    for now in range(5, 100, 5):
        assert store.get(t.id, now=now) is t
    assert store.get(t.id, now=101) is None


def test_login_rotates_id_and_csrf() -> None:
    store = SessionStore(idle_s=10, max_s=100)
    anon = store.new(now=0)
    fresh = store.login(anon, "owner", now=1)
    assert fresh.id != anon.id and fresh.csrf != anon.csrf
    assert store.get(anon.id, now=2) is None
    assert store.get(fresh.id, now=2) is fresh
    assert fresh.recently_authenticated(5, now=5)
    assert not fresh.recently_authenticated(5, now=7)


def test_unknown_or_oversized_ids() -> None:
    store = SessionStore(idle_s=10, max_s=100)
    assert store.get(None) is None
    assert store.get("x" * 500) is None
    assert store.get("nope") is None


def test_pruning_keeps_logged_in_sessions() -> None:
    store = SessionStore(idle_s=1000, max_s=1000, max_sessions=10)
    owner = store.login(store.new(now=0), "owner", now=0)
    for i in range(30):
        store.new(now=i + 1)
    assert len(store) <= 11
    assert store.get(owner.id, now=50) is owner


def test_looking_without_touching_does_not_extend_idle_time() -> None:
    store = SessionStore(idle_s=100, max_s=1000)
    s = store.new(now=0)
    assert store.get(s.id, now=90, touch=False) is s
    assert store.get(s.id, now=150, touch=False) is None  # idle since 0
    t = store.new(now=0)
    assert store.get(t.id, now=90) is t  # counts as activity
    assert store.get(t.id, now=150) is t
