"""Task notifications: 自动交易启停、回测/优化结束、任务异常。

These run on background threads, so the two things that matter are that they
are actually delivered as ``kind="task"`` (one toggle, not mixed into fills)
and that a broken gateway can never take the task down with it.
"""

from __future__ import annotations

from btrader.web import routes


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def notify(self, text: str, *, kind: str = "") -> bool:
        if self.fail:
            raise RuntimeError("网关挂了")
        self.calls.append((text, kind))
        return True


class Ctx:
    def __init__(self, fail: bool = False) -> None:
        self.notifier = Recorder(fail)


def test_task_notification_uses_the_task_kind() -> None:
    ctx = Ctx()
    routes._notify_task(ctx, "回测完成", ["BTCUSDT 1h", "收益 +2.10%"])
    text, kind = ctx.notifier.calls[0]
    assert kind == "task"
    assert text.splitlines() == ["【回测完成】", "BTCUSDT 1h", "收益 +2.10%"]


def test_task_notification_never_raises() -> None:
    """A dead gateway must not turn a finished backtest into a failed one."""
    routes._notify_task(Ctx(fail=True), "回测完成", ["BTCUSDT 1h"])


def test_metric_text_formats_fractions_as_percent() -> None:
    metrics = {"total_return": 0.021, "max_drawdown": -0.135, "win_rate": 0.54}
    assert routes._metric_text(metrics, "total_return", "%") == "+2.10%"
    assert routes._metric_text(metrics, "max_drawdown", "%", signed=False) == "-13.50%"
    assert routes._metric_text(metrics, "win_rate", "%", signed=False) == "54.00%"


def test_metric_text_shows_a_dash_when_the_run_reported_nothing() -> None:
    # "0.00%" would be a claim about performance; "—" says there is no number.
    assert routes._metric_text({}, "total_return", "%") == "—"
    assert routes._metric_text({"win_rate": None}, "win_rate", "%") == "—"
