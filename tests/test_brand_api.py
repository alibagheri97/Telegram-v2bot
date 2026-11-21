import base64
from urllib.parse import unquote

from vpnshopbot.brand_api import relabel_subscription, subscription_status_nodes


def subscriber(**overrides):
    result = {
        "subscriber_uuid": "00000000-0000-0000-0000-000000000001",
        "uploaded_bytes": 2 * 1024**3,
        "downloaded_bytes": 3 * 1024**3,
        "transfer_limit_bytes": 20 * 1024**3,
        "expires_at": 0,
    }
    result.update(overrides)
    return result


def test_status_nodes_include_live_usage_and_unlimited_time() -> None:
    nodes = subscription_status_nodes(subscriber(), now=100)
    decoded_labels = [node.split("#", 1)[1] for node in nodes]
    assert len(nodes) == 2
    assert any("%E2%99%BE%EF%B8%8F" in label for label in decoded_labels)
    assert any("5.0%20GB" in label for label in decoded_labels)


def test_relabel_subscription_preserves_connection_and_changes_label() -> None:
    source_node = "vless://uuid@example.com:443?security=tls#Old"
    source = base64.b64encode((source_node + "\n").encode()).decode()
    result = base64.b64decode(relabel_subscription(source, "My Brand", subscriber())).decode()
    assert "vless://uuid@example.com:443?security=tls#My%20Brand" in result
    assert "روزهای باقی‌مانده" in unquote(result)
