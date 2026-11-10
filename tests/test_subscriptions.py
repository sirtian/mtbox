from __future__ import annotations

from pathlib import Path

from app.services.subscriptions import SubscriptionService, target_episode_range
from app.timeutil import utc_now_iso
from tests.helpers import insert_subscription, make_db, make_settings


def setup_db(tmp_path: Path):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "library")
    return make_db(settings)


def mark_organized(db, sub_id: int, episodes: list[int], season: int = 1) -> None:
    now = utc_now_iso()
    with db.tx() as conn:
        for episode in episodes:
            conn.execute(
                "INSERT INTO episode_state(subscription_id, season, episode, state, updated_at) "
                "VALUES (?, ?, ?, 'organized', ?)",
                (sub_id, season, episode, now),
            )


def test_finish_completed_uses_total_episodes_as_target(tmp_path: Path) -> None:
    db = setup_db(tmp_path)
    sub_id = insert_subscription(db, kind="tv", episode_from=1, total_episodes=2)
    mark_organized(db, sub_id, [1, 2])
    service = SubscriptionService(db)

    assert service.finish_completed() == [sub_id]
    assert service.get(sub_id).status == "done"


def test_finish_completed_keeps_partial_subscription_active(tmp_path: Path) -> None:
    db = setup_db(tmp_path)
    sub_id = insert_subscription(db, kind="tv", episode_from=1, total_episodes=3)
    mark_organized(db, sub_id, [1, 2])
    service = SubscriptionService(db)

    assert service.finish_completed() == []
    assert service.get(sub_id).status == "active"


def test_finish_completed_needs_a_known_target(tmp_path: Path) -> None:
    # 连播中的剧可能还没有总集数；只整理了一集不能算完成。
    db = setup_db(tmp_path)
    sub_id = insert_subscription(db, kind="tv", episode_from=1)
    mark_organized(db, sub_id, [1])
    service = SubscriptionService(db)

    assert service.finish_completed() == []
    assert service.get(sub_id).status == "active"


def test_finish_completed_ignores_paused_subscriptions(tmp_path: Path) -> None:
    db = setup_db(tmp_path)
    sub_id = insert_subscription(
        db, kind="tv", episode_from=1, total_episodes=1, status="paused"
    )
    mark_organized(db, sub_id, [1])

    assert SubscriptionService(db).finish_completed() == []
    assert SubscriptionService(db).get(sub_id).status == "paused"


def test_target_episode_range_prefers_explicit_episode_to(tmp_path: Path) -> None:
    db = setup_db(tmp_path)
    service = SubscriptionService(db)
    explicit = service.get(
        insert_subscription(db, kind="tv", episode_from=1, episode_to=2, total_episodes=9)
    )
    discovered = service.get(
        insert_subscription(db, kind="tv", episode_from=1, total_episodes=9)
    )
    movie = service.get(insert_subscription(db, kind="movie"))

    assert target_episode_range(explicit) == (1, 2)
    assert target_episode_range(discovered) == (1, 9)
    assert target_episode_range(movie) is None
