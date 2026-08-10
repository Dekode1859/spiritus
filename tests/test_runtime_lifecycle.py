from __future__ import annotations

from spiritus.runtime.lifecycle import ShutdownCoordinator


def test_shutdown_coordinator_runs_callbacks_once_in_reverse_order():
    calls = []
    lifecycle = ShutdownCoordinator()
    lifecycle.add(lambda: calls.append("first"))
    lifecycle.add(lambda: calls.append("second"))

    lifecycle.stop_once()
    lifecycle.stop_once()

    assert calls == ["second", "first"]


def test_shutdown_coordinator_continues_after_a_callback_fails():
    calls = []
    lifecycle = ShutdownCoordinator()
    lifecycle.add(lambda: (_ for _ in ()).throw(RuntimeError("ignored")))
    lifecycle.add(lambda: calls.append("released"))

    lifecycle.stop_once()

    assert calls == ["released"]


def test_shutdown_coordinator_runs_late_registration_immediately():
    calls = []
    lifecycle = ShutdownCoordinator()
    lifecycle.stop_once()

    lifecycle.add(lambda: calls.append("late"))

    assert calls == ["late"]
