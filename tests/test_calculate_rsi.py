import numpy as np
import pytest

from trading_bot import calculate_rsi, RSI_PERIOD

@pytest.fixture
def short_price_series():
    """Return a price series shorter than RSI_PERIOD."""
    return [float(i) for i in range(1, RSI_PERIOD)]

@pytest.fixture
def long_price_series():
    """Return a price series longer than RSI_PERIOD."""
    return [float(i) for i in range(1, RSI_PERIOD + 10)]


def test_returns_none_for_short_series(short_price_series):
    assert calculate_rsi(short_price_series) is None


def test_returns_value_for_long_series(long_price_series):
    rsi = calculate_rsi(long_price_series)
    assert rsi is not None
    assert isinstance(rsi, (float, np.floating))
