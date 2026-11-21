import logging

from vpnshopbot.app import RedactingFormatter


def test_log_formatter_redacts_bot_token() -> None:
    token = "123456:super-secret-token"
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=f"request failed at /bot{token}/getUpdates",
        args=(),
        exc_info=None,
    )
    output = RedactingFormatter(token).format(record)
    assert token not in output
    assert "<redacted>" in output
