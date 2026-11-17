from unittest.mock import patch

import pytest
from pdudaemon.drivers.ubus import Ubus

NULL = "00000000000000000000000000000000"
SESSION = "0123456789abcdef0123456789abcdef"


def ok(data=None):
    result = [0] if data is None else [0, data]
    return {"jsonrpc": "2.0", "id": 1, "result": result}


@pytest.fixture(name="post")
def fixture_post():
    with patch("pdudaemon.drivers.ubus.requests.post") as post:
        yield post


def replies(post, *bodies):
    post.return_value.json.side_effect = list(bodies)


def params(post, n):
    return post.call_args_list[n].kwargs["json"]["params"]


def test_login_before_every_action_by_default(post):
    replies(post, ok({"ubus_rpc_session": SESSION}), ok(),
            ok({"ubus_rpc_session": SESSION}), ok())
    pdu = Ubus("192.168.128.2", {})
    pdu.port_interaction("off", 1)
    pdu.port_interaction("on", 1)

    assert post.call_count == 4
    assert post.call_args.args == ("http://192.168.128.2/ubus",)
    assert params(post, 0) == [NULL, "session", "login",
                               {"username": "root", "password": "",
                                "timeout": 60}]
    assert params(post, 1) == [SESSION, "poe", "manage",
                               {"port": "lan1", "enable": False}]
    assert params(post, 2)[1:3] == ["session", "login"]


def test_login_with_credentials(post):
    replies(post, ok({"ubus_rpc_session": SESSION}), ok())
    Ubus("switch", {"username": "pdu",
                    "password": "secret"}).port_interaction("on", 1)

    assert params(post, 0)[3]["username"] == "pdu"
    assert params(post, 0)[3]["password"] == "secret"


def test_unauthenticated_without_login(post):
    replies(post, ok())
    Ubus("switch", {"login": False}).port_interaction("on", 3)

    assert post.call_count == 1
    assert params(post, 0) == [NULL, "poe", "manage",
                               {"port": "lan3", "enable": True}]


def test_wrong_password_raises(post):
    replies(post, {"jsonrpc": "2.0", "id": 1, "result": [6]})
    with pytest.raises(RuntimeError, match="session login failed with status 6"):
        Ubus("switch", {"password": "wrong"}).port_interaction("on", 1)


def test_realtek_poe_1_3_manage_status(post):
    replies(post, {"jsonrpc": "2.0", "id": 1, "result": [12]},
            {"jsonrpc": "2.0", "id": 1, "result": [2]})
    pdu = Ubus("switch", {"login": False})
    pdu.port_interaction("off", 1)
    with pytest.raises(RuntimeError, match="poe manage failed with status 2"):
        pdu.port_interaction("off", 1)


def test_port_get(post):
    ports = {"lan1": "Delivering power", "lan2": "Disabled"}
    replies(post, ok({"ports": ports}), ok({"ports": ports}),
            ok({"ports": ports}))
    pdu = Ubus("switch", {"login": False})

    assert pdu.port_interaction("get", 1) is True
    assert pdu.port_interaction("get", 2) is False
    with pytest.raises(RuntimeError, match="lan9"):
        pdu.port_interaction("get", 9)
