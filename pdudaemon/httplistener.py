#!/usr/bin/python3

#  Copyright 2018 Matt Hart <matt@mattface.org>
#
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

import urllib.parse as urlparse
import logging
import json
import pdudaemon.listener as listener
from pdudaemon.power import PowerLogger

from aiohttp import web

logger = logging.getLogger('pdud.http')


class HTTPListener:

    def __init__(self, config, daemon):
        self.config = config
        self.daemon = daemon
        self.settings = config["daemon"]

        # Initialize power logger
        power_log_path = self.settings.get("power_log_path", "powerlogs")
        self.power_logger = PowerLogger(power_log_path)
        self.power_logger.set_daemon(daemon)

        self.app = web.Application()
        self.app.add_routes([
            web.get('/power/control/on', self.handle),
            web.get('/power/control/off', self.handle),
            web.get('/power/control/reboot', self.handle),
            # Power logging endpoints
            web.post('/power/log/start', self.handle_power_log_start),
            web.post('/power/log/stop', self.handle_power_log_stop),
            web.get('/power/log/data/{uuid}', self.handle_power_log_data),
            web.get('/power/log/list', self.handle_power_log_list),
        ])
        self.apprunner = None

    async def start(self):
        logger.info("Starting the HTTP server")
        self.apprunner = web.AppRunner(self.app)
        await self.apprunner.setup()
        listen_host = self.settings["hostname"]
        listen_port = self.settings.get("port", 16421)
        site = web.TCPSite(self.apprunner, host=listen_host, port=listen_port)
        await site.start()
        logger.info("Listening on %s:%s", listen_host, listen_port)

    async def shutdown(self):
        if self.apprunner:
            await self.apprunner.cleanup()
        self.apprunner = None

    async def handle(self, request):
        logger.info("Handling HTTP request from %s: %s", request.remote, request.path_qs)
        data = urlparse.parse_qs(urlparse.urlparse(request.path_qs).query)
        path = urlparse.urlparse(request.path_qs).path
        res = await self.insert_request(data, path)
        if res:
            return web.Response(status=200, text="OK - accepted request\n")
        else:
            return web.Response(status=500, text="Invalid request\n")

    async def insert_request(self, data, path):
        args = listener.parse_http(data, path)
        if args:
            return await listener.process_request(args, self.config, self.daemon)

    async def handle_power_log_start(self, request):
        """Start a new power logging session"""
        try:
            data = await request.json()
            uuid = data.get('uuid')
            hostname = data.get('hostname')
            port = data.get('port')
            interval = data.get('interval', 5.0)

            if not all([uuid, hostname, port]):
                return web.Response(
                    status=400,
                    text="Missing required parameters: uuid, hostname, port\n"
                )

            # Check if hostname exists in config
            if hostname not in self.config.get('pdus', {}):
                return web.Response(
                    status=404,
                    text=f"PDU {hostname} not found in configuration\n"
                )

            success = await self.power_logger.start_session(
                uuid, hostname, int(port), float(interval)
            )

            if success:
                return web.Response(
                    status=200,
                    text=json.dumps({"status": "started", "uuid": uuid}),
                    content_type='application/json'
                )
            else:
                return web.Response(
                    status=409,
                    text=f"Session {uuid} already exists or PDU not available\n"
                )

        except Exception as e:
            logger.error(f"Error starting power log session: {e}")
            return web.Response(status=500, text=f"Internal error: {str(e)}\n")

    async def handle_power_log_stop(self, request):
        """Stop a power logging session"""
        try:
            data = await request.json()
            uuid = data.get('uuid')

            if not uuid:
                return web.Response(
                    status=400,
                    text="Missing required parameter: uuid\n"
                )

            session_data = await self.power_logger.stop_session(uuid)

            if session_data:
                return web.Response(
                    status=200,
                    text=json.dumps(session_data),
                    content_type='application/json'
                )
            else:
                return web.Response(
                    status=404,
                    text=f"Session {uuid} not found\n"
                )

        except Exception as e:
            logger.error(f"Error stopping power log session: {e}")
            return web.Response(status=500, text=f"Internal error: {str(e)}\n")

    async def handle_power_log_data(self, request):
        """Get data for a power logging session"""
        try:
            uuid = request.match_info.get('uuid')

            if not uuid:
                return web.Response(
                    status=400,
                    text="Missing UUID in path\n"
                )

            session_data = await self.power_logger.get_session_data(uuid)

            if session_data:
                return web.Response(
                    status=200,
                    text=json.dumps(session_data),
                    content_type='application/json'
                )
            else:
                return web.Response(
                    status=404,
                    text=f"Session {uuid} not found\n"
                )

        except Exception as e:
            logger.error(f"Error getting power log data: {e}")
            return web.Response(status=500, text=f"Internal error: {str(e)}\n")

    async def handle_power_log_list(self, request):
        """List all power logging sessions"""
        try:
            sessions = self.power_logger.list_sessions()
            return web.Response(
                status=200,
                text=json.dumps(sessions),
                content_type='application/json'
            )
        except Exception as e:
            logger.error(f"Error listing power log sessions: {e}")
            return web.Response(status=500, text=f"Internal error: {str(e)}\n")
