"""Host service output capture; no device collection or Frida callback work."""

import logging
import sys
from contextlib import contextmanager

from .diagnostic_log import LogHandler


class Tee:
    def __init__(self, stream, recorder, channel):
        self.stream, self.recorder, self.channel = stream, recorder, channel

    def write(self, text):
        result = self.stream.write(text)
        if text.strip():
            self.recorder.record(
                "runtime", "service_output", channel=self.channel, text=text
            )
        return result

    def flush(self):
        return self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


@contextmanager
def service_output(recorder):
    stdout, stderr = sys.stdout, sys.stderr
    handler = LogHandler(recorder)
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    sys.stdout, sys.stderr = (
        Tee(stdout, recorder, "stdout"),
        Tee(stderr, recorder, "stderr"),
    )
    try:
        yield
    except BaseException:
        logging.getLogger(__name__).exception("Player service exited")
        raise
    finally:
        sys.stdout, sys.stderr = stdout, stderr
        root.removeHandler(handler)
        root.setLevel(previous_level)

