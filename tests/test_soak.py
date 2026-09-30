from controller.soak import run_soak


async def test_accelerated_multi_day_recovery_restore_and_retention():
    report = await run_soak(days=2)
    assert report["ok"]
    assert report["ticks"] == 196
    assert report["restarts"] == 4
    assert report["restores"] == 1
    assert report["handoffs"] >= 1
    assert report["recovery_requests"] >= 2
