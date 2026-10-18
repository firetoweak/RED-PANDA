"""Write the few existing operation timings to stderr, without request data."""

import json
import logging


_FIELDS = ("stage", "operation", "attempt", "elapsed_ms", "outcome", "provider", "model")
_LOGGERS = (
    "thinllm.chat_completions",
    "redpanda.assistant.decision",
    __name__,
)


def _values(record):
    return {"type": "timing", **{
        name: record.__dict__[name] for name in _FIELDS if name in record.__dict__
    }}


class _JsonTimingFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(_values(record), ensure_ascii=False, separators=(",", ":"))


class _TimingHandler(logging.StreamHandler):
    def __init__(self, forward):
        super().__init__()
        self.forward = forward
        self.setFormatter(_JsonTimingFormatter())
        self.addFilter(lambda record: "elapsed_ms" in record.__dict__)

    def emit(self, record):
        if self.forward is None:
            super().emit(record)
        else:
            self.forward(_values(record))


def install_timing_logging(*, forward=None):
    """Host writes stderr; Worker forwards through its existing notification pipe."""

    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(handler, _TimingHandler) for handler in logger.handlers):
            logger.addHandler(_TimingHandler(forward))
        logger.setLevel(logging.INFO)
        logger.propagate = False


def emit_worker_timing(values):
    logging.getLogger(__name__).info("worker timing", extra={
        name: values[name] for name in _FIELDS if name in values
    })
