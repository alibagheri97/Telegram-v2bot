import pytest

from vpnshopbot.services import price_for_data, price_for_time, toman


@pytest.mark.parametrize(
    ("connections", "months", "expected"),
    [(1, 1, 450_000), (1, 3, 1_000_000), (1, 6, 1_500_000), (2, 1, 650_000)],
)
def test_time_prices(connections: int, months: int, expected: int) -> None:
    assert price_for_time(connections, months) == expected


@pytest.mark.parametrize("gigabytes", [10, 15, 20, 25, 30, 35, 40, 45, 50])
def test_valid_data_steps(gigabytes: int) -> None:
    assert price_for_data(gigabytes) == gigabytes * 20_000


@pytest.mark.parametrize("gigabytes", [0, 9, 11, 51, 100])
def test_invalid_data_steps(gigabytes: int) -> None:
    with pytest.raises(ValueError):
        price_for_data(gigabytes)


def test_toman_format() -> None:
    assert toman(450_000) == "450,000 تومان"
