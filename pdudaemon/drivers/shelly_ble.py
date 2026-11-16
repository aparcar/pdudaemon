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
import logging
import os
import struct
import threading
from pdudaemon.drivers.driver import PDUDriver, FailedRequestException

log = logging.getLogger("pdud.drivers." + os.path.basename(__file__))

# GATT service and characteristics of the Shelly RPC channel, see
# https://shelly-api-docs.shelly.cloud/gen2/General/RPCChannels#bluetooth-low-energy
RPC_SERVICE = "5f6d4f53-5f52-5043-5f53-56435f49445f"
RPC_DATA = "5f6d4f53-5f52-5043-5f64-6174615f5f5f"
RPC_TX_CTL = "5f6d4f53-5f52-5043-5f74-785f63746c5f"
RPC_RX_CTL = "5f6d4f53-5f52-5043-5f72-785f63746c5f"

# One adapter handles one connection attempt at a time, so requests to
# different PDUs (each has its own worker thread) take turns.
ble_lock = threading.Lock()

# Seconds the disconnect after a request may take. BlueZ sometimes takes
# longer to tear the link down than the whole request: the device has
# answered by then, so a slow or failed disconnect doesn't fail the request.
DISCONNECT_TIMEOUT = 5


class ShellyBLE(PDUDriver):
    """Switch Shelly Gen2+ devices through their Bluetooth LE RPC channel.

    For devices that are not on the network, e.g. a Shelly Power Strip 4
    Gen4 next to a lab host with a Bluetooth adapter. Needs the 'bleak'
    package, and Bluetooth with RPC enabled on the device (the default).
    Authentication is not supported and must be turned off on the device.

    The port number is the switch id, starting at 0.

    Settings:
      address: Bluetooth address of the device, e.g. "AA:BB:CC:DD:EE:FF".
        Defaults to the PDU name.
      adapter: local Bluetooth adapter, e.g. "hci1". Defaults to the
        system default.
      timeout: seconds one request may take, including the connection but
        not the disconnect afterwards (default 20).
    """

    def __init__(self, hostname, settings):
        self.hostname = hostname
        self.settings = settings
        self.address = settings.get("address", hostname)
        self.adapter = settings.get("adapter")
        self.timeout = float(settings.get("timeout", 20))
        super(ShellyBLE, self).__init__()

    @classmethod
    def accepts(cls, drivername):
        return drivername == "shelly_ble"

    def _client(self):
        # Imported here so pdudaemon still starts without bleak when no
        # PDU uses this driver.
        from bleak import BleakClient

        # Only the RPC service is resolved: Gen4 devices also have a Matter
        # service (FFF6), which macOS refuses to let applications inspect.
        kwargs = {"timeout": self.timeout, "services": [RPC_SERVICE]}
        if self.adapter:
            kwargs["adapter"] = self.adapter
        return BleakClient(self.address, **kwargs)

    async def _exchange(self, client, method, params):
        frame = json.dumps(
            {"id": 1, "src": "pdudaemon", "method": method, "params": params}
        ).encode()
        await client.connect()

        # Announce the frame length, then send the frame.
        await client.write_gatt_char(RPC_TX_CTL, struct.pack(">I", len(frame)), response=True)
        await client.write_gatt_char(RPC_DATA, frame, response=True)

        # The device publishes the length of the reply once it is ready.
        length = 0
        while not length:
            raw = bytes(await client.read_gatt_char(RPC_RX_CTL))
            if len(raw) != 4:
                raise FailedRequestException("unexpected reply length field %r" % (raw,))
            length = struct.unpack(">I", raw)[0]
            if not length:
                await asyncio.sleep(0.1)

        # The reply is read in chunks of at most one MTU.
        data = b""
        while len(data) < length:
            chunk = bytes(await client.read_gatt_char(RPC_DATA))
            if not chunk:
                raise FailedRequestException("reply ended after %d of %d bytes" % (len(data), length))
            data += chunk
        return data

    async def _disconnect(self, client):
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
        except Exception as e:
            log.debug("%s: disconnect failed: %r", self.address, e)

    async def _rpc(self, method, params):
        client = self._client()
        try:
            data = await asyncio.wait_for(self._exchange(client, method, params), self.timeout)
        finally:
            # Also after a failed or timed out exchange, so that a link
            # BlueZ has set up doesn't stay open.
            await self._disconnect(client)

        reply = json.loads(data)
        if "error" in reply:
            raise FailedRequestException("%s failed: %s" % (method, reply["error"]))
        return reply.get("result")

    def jsonrpc_call(self, method, params):
        with ble_lock:
            return asyncio.run(self._rpc(method, params))

    def port_interaction(self, command, port_number):
        try:
            switch = int(port_number)
        except (TypeError, ValueError):
            raise RuntimeError("port_number must be a switch id, got %r" % (port_number,))
        if switch < 0:
            raise RuntimeError("port_number must be a switch id, got %r" % (port_number,))

        if command == "get":
            return self.jsonrpc_call("Switch.GetStatus", {"id": switch})["output"]
        if command not in ("on", "off"):
            log.debug("Unknown command!")
            return

        log.debug("%s: switch %d %s", self.address, switch, command)
        self.jsonrpc_call("Switch.Set", {"id": switch, "on": command == "on"})
