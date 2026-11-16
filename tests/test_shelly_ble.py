#!/usr/bin/python3

#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

import asyncio
import json
import struct
import time
import pytest
from pdudaemon.drivers import shelly_ble
from pdudaemon.drivers.driver import PDUDriver, FailedRequestException
from pdudaemon.drivers.shelly_ble import ShellyBLE, RPC_DATA, RPC_TX_CTL, RPC_RX_CTL


class FakeClient:
    """A Shelly that answers one RPC request, in chunks of `mtu` bytes.

    disconnect: "ok", "hang" (never returns) or "fail" (raises)."""

    def __init__(self, reply, mtu=20, not_ready=0, hang=False, disconnect="ok"):
        self.reply = json.dumps(reply).encode()
        self.mtu = mtu
        self.not_ready = not_ready
        self.hang = hang
        self.disconnect_mode = disconnect
        self.announced = None
        self.request = None
        self.connected = False
        self.disconnects = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False
        self.disconnects += 1
        if self.disconnect_mode == "hang":
            await asyncio.sleep(3600)
        if self.disconnect_mode == "fail":
            raise EOFError("bus closed")

    async def write_gatt_char(self, char, data, response=False):
        assert self.connected and response
        if char == RPC_TX_CTL:
            self.announced = struct.unpack(">I", data)[0]
        elif char == RPC_DATA:
            assert self.announced == len(data)
            self.request = json.loads(data)
        else:
            raise AssertionError("write to %s" % char)

    async def read_gatt_char(self, char):
        assert self.connected and self.request
        if char == RPC_RX_CTL:
            if self.hang or self.not_ready:
                self.not_ready -= 1
                return bytearray(4)
            return bytearray(struct.pack(">I", len(self.reply)))
        assert char == RPC_DATA
        chunk, self.reply = self.reply[: self.mtu], self.reply[self.mtu:]
        return bytearray(chunk)


def shelly(monkeypatch, client, settings=None):
    pdu = ShellyBLE("AA:BB:CC:DD:EE:FF", settings or {})
    monkeypatch.setattr(pdu, "_client", lambda: client)
    return pdu


def test_select():
    assert PDUDriver.select("shelly_ble") is ShellyBLE


def test_address():
    assert ShellyBLE("AA:BB:CC:DD:EE:FF", {}).address == "AA:BB:CC:DD:EE:FF"
    assert ShellyBLE("strip", {"address": "11:22:33:44:55:66"}).address == "11:22:33:44:55:66"


@pytest.mark.parametrize("command, on", [("on", True), ("off", False)])
def test_handle(monkeypatch, command, on):
    client = FakeClient({"id": 1, "src": "shellypstripg4-aabbccddeeff", "result": {"was_on": not on}})
    pdu = shelly(monkeypatch, client)

    pdu.handle(command, 3)

    assert client.request == {
        "id": 1,
        "src": "pdudaemon",
        "method": "Switch.Set",
        "params": {"id": 3, "on": on},
    }
    assert client.disconnects == 1


def test_get(monkeypatch):
    client = FakeClient({"id": 1, "result": {"id": 0, "output": True, "apower": 4.2}})
    pdu = shelly(monkeypatch, client)

    assert pdu.port_interaction("get", "0") is True
    assert client.request["method"] == "Switch.GetStatus"


def test_reply_not_ready_yet(monkeypatch):
    client = FakeClient({"id": 1, "result": {"was_on": False}}, not_ready=2)
    pdu = shelly(monkeypatch, client)

    pdu.port_interaction("on", 0)

    assert client.reply == b""


def test_rpc_error(monkeypatch):
    client = FakeClient({"id": 1, "error": {"code": -105, "message": "Argument 'id', value 7 not found!"}})
    pdu = shelly(monkeypatch, client)

    with pytest.raises(FailedRequestException, match="not found"):
        pdu.port_interaction("on", 7)
    assert client.disconnects == 1


def test_timeout(monkeypatch):
    client = FakeClient({}, hang=True)
    pdu = shelly(monkeypatch, client, {"timeout": 0.3})

    with pytest.raises(TimeoutError):
        pdu.port_interaction("on", 0)
    assert client.disconnects == 1


@pytest.mark.parametrize("mode", ["hang", "fail"])
def test_disconnect_trouble_after_reply(monkeypatch, mode):
    # The switch has answered: a slow or failing disconnect must not fail the
    # request (pdudaemon would retry it) nor count against its timeout
    monkeypatch.setattr(shelly_ble, "DISCONNECT_TIMEOUT", 0.2)
    client = FakeClient({"id": 1, "result": {"was_on": True}}, disconnect=mode)
    pdu = shelly(monkeypatch, client, {"timeout": 0.3})

    start = time.monotonic()
    pdu.port_interaction("off", 3)

    assert client.request["params"] == {"id": 3, "on": False}
    assert client.disconnects == 1
    assert time.monotonic() - start < 1


@pytest.mark.parametrize("port", ["abc", None, -1])
def test_bad_port(monkeypatch, port):
    pdu = shelly(monkeypatch, FakeClient({}))

    with pytest.raises(RuntimeError, match="switch id"):
        pdu.port_interaction("on", port)


def test_unknown_command(monkeypatch):
    client = FakeClient({})
    pdu = shelly(monkeypatch, client)

    pdu.port_interaction("reboot", 0)

    assert client.request is None
