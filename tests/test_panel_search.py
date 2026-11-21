from vpnshopbot.panel import parse_search


def test_exact_id_search() -> None:
    expression = parse_search("id=32")
    assert expression.field == "id"
    assert expression.operator == "="
    assert expression.value == "32"


def test_partial_email_search() -> None:
    expression = parse_search("email?ali")
    assert expression.field == "email"
    assert expression.operator == "?"
    assert expression.value == "ali"


def test_general_search() -> None:
    expression = parse_search("some token")
    assert expression.field is None
    assert expression.value == "some token"


def test_unknown_column_cannot_be_selected() -> None:
    expression = parse_search("password=secret")
    assert expression.field is None
