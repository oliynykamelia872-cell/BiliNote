import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).with_name("batch_summarize_local.py")
spec = importlib.util.spec_from_file_location("batch_summarize_local_limits", SCRIPT_PATH)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


@pytest.mark.parametrize("value", ["0", "6", "99", "not-a-number"])
def test_concurrency_rejects_invalid_or_over_limit_values(value):
    with pytest.raises(Exception):
        batch.positive_int(value)


def test_concurrency_accepts_five():
    assert batch.positive_int("5") == 5
