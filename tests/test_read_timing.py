import importlib
from types import SimpleNamespace


def test_slow_read_separates_thread_cpu_from_elapsed_time(monkeypatch, caplog):
    module = importlib.import_module("controller.read_timing")
    wall = iter([10, 13, 14])
    cpu = iter([20, 20.04, 20.05])
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: next(wall), thread_time=lambda: next(cpu)))
    with module.read_timing("overview", "stream-2") as mark:
        mark("teams")
    record, = caplog.records
    assert record.args[3] == {"teams": 3}
    assert abs(record.args[4] - .05) < .000001
    assert record.args[5] == {"teams": .04}
