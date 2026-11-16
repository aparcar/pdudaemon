#!/usr/bin/python3

"""List nearby Shelly devices and the address to use with the shelly_ble driver.

Needs the 'bleak' package. Pass --all to list every Bluetooth LE device seen.
On macOS the address is a UUID that is only valid on that machine.
"""

import asyncio
import sys

from bleak import BleakScanner

RPC_SERVICE = "5f6d4f53-5f52-5043-5f53-56435f49445f"


async def main(seconds):
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    print("%d devices seen" % len(found))
    for address, (device, adv) in sorted(found.items(), key=lambda i: -i[1][1].rssi):
        name = adv.local_name or device.name or ""
        shelly = "shelly" in name.lower() or RPC_SERVICE in adv.service_uuids
        if shelly or "--all" in sys.argv:
            print(
                "%s  rssi=%d  name=%r  services=%s  manufacturer=%s"
                % (address, adv.rssi, name, adv.service_uuids, list(adv.manufacturer_data))
            )


asyncio.run(main(10.0))
