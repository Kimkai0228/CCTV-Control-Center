"""Health alert de-duplication and cooldown behaviour."""

from __future__ import annotations

import datetime as dt

from app.services.health import HealthChecker
from app.settings_service import set_setting


def test_alert_cooldown_suppresses_repeats(db):
    set_setting(db, "health.alert_cooldown_minutes", "60")
    checker = HealthChecker(db)

    first = checker._alert(
        fingerprint="test:cooldown",
        alert_type="hdd",
        severity="warning",
        message="disk warning",
    )
    second = checker._alert(
        fingerprint="test:cooldown",
        alert_type="hdd",
        severity="warning",
        message="disk warning again",
    )

    assert first is True
    assert second is False

    # A later sweep after the cooldown elapses notifies again.
    from app import models

    alert = (
        db.query(models.Alert)
        .filter(models.Alert.fingerprint == "test:cooldown")
        .one()
    )
    alert.last_notified_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=61)
    db.commit()

    third = checker._alert(
        fingerprint="test:cooldown",
        alert_type="hdd",
        severity="warning",
        message="disk warning again",
    )
    assert third is True


def test_alert_cooldown_zero_always_notifies(db):
    set_setting(db, "health.alert_cooldown_minutes", "0")
    checker = HealthChecker(db)

    fingerprint = "test:no-cooldown"
    assert checker._alert(
        fingerprint=fingerprint, alert_type="hdd", severity="warning", message="a"
    )
    assert checker._alert(
        fingerprint=fingerprint, alert_type="hdd", severity="warning", message="b"
    )
