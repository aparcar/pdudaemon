#!/usr/bin/python3

#  Copyright 2024 PDUDaemon Contributors
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

import asyncio
import json
import time
import os
from datetime import datetime
from pathlib import Path
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger('pdud.powerlog')


class PowerSession:
    """Represents a power logging session"""

    def __init__(self, uuid: str, hostname: str, port: int, interval: float = 5.0):
        self.uuid = uuid
        self.hostname = hostname
        self.port = port
        self.interval = interval
        self.start_time = datetime.now()
        self.end_time = None
        self.data: List[Dict[str, Any]] = []
        self.task: Optional[asyncio.Task] = None
        self.active = True

    def add_reading(self, power_watts: float, timestamp: Optional[datetime] = None):
        """Add a power reading to the session"""
        if timestamp is None:
            timestamp = datetime.now()

        self.data.append({
            'timestamp': timestamp.isoformat(),
            'power_watts': power_watts,
            'elapsed_seconds': (timestamp - self.start_time).total_seconds()
        })

    def stop(self):
        """Stop the session"""
        self.active = False
        self.end_time = datetime.now()
        if self.task:
            self.task.cancel()

    def to_dict(self) -> Dict[str, Any]:
        """Convert session to dictionary for serialization"""
        return {
            'uuid': self.uuid,
            'hostname': self.hostname,
            'port': self.port,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'duration_seconds': (self.end_time - self.start_time).total_seconds() if self.end_time else None,
            'interval': self.interval,
            'data': self.data,
            'statistics': self._calculate_statistics()
        }

    def _calculate_statistics(self) -> Dict[str, float]:
        """Calculate basic statistics from the power data"""
        if not self.data:
            return {}

        powers = [d['power_watts'] for d in self.data]
        return {
            'min_watts': min(powers),
            'max_watts': max(powers),
            'avg_watts': sum(powers) / len(powers),
            'total_readings': len(powers)
        }


class PowerLogger:
    """Manages power logging sessions"""

    def __init__(self, storage_path: str = "/var/lib/pdudaemon/powerlogs"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.sessions: Dict[str, PowerSession] = {}
        self.daemon = None  # Will be set by the daemon

    def set_daemon(self, daemon):
        """Set reference to the PDU daemon for accessing runners"""
        self.daemon = daemon

    async def start_session(self, uuid: str, hostname: str, port: int,
                          interval: float = 5.0) -> bool:
        """Start a new power logging session"""
        if uuid in self.sessions:
            logger.warning(f"Session {uuid} already exists")
            return False

        if not self.daemon or hostname not in self.daemon.runners:
            logger.error(f"PDU {hostname} not found")
            return False

        session = PowerSession(uuid, hostname, port, interval)
        self.sessions[uuid] = session

        # Start the logging task
        session.task = asyncio.create_task(self._log_power_task(session))

        logger.info(f"Started power logging session {uuid} for {hostname}:{port}")
        return True

    async def stop_session(self, uuid: str) -> Optional[Dict[str, Any]]:
        """Stop a power logging session and return its data"""
        session = self.sessions.get(uuid)
        if not session:
            logger.warning(f"Session {uuid} not found")
            return None

        session.stop()

        # Wait a bit for the task to finish
        try:
            await asyncio.wait_for(session.task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        # Save session data to file
        session_data = session.to_dict()
        self._save_session(session_data)

        # Remove from active sessions
        del self.sessions[uuid]

        logger.info(f"Stopped power logging session {uuid}")
        return session_data

    async def get_session_data(self, uuid: str) -> Optional[Dict[str, Any]]:
        """Get data for a session (active or saved)"""
        # Check active sessions first
        if uuid in self.sessions:
            return self.sessions[uuid].to_dict()

        # Check saved sessions
        session_file = self.storage_path / f"{uuid}.json"
        if session_file.exists():
            with open(session_file, 'r') as f:
                return json.load(f)

        return None

    def list_sessions(self) -> Dict[str, List[str]]:
        """List all sessions (active and saved)"""
        active = list(self.sessions.keys())

        saved = []
        for file in self.storage_path.glob("*.json"):
            uuid = file.stem
            if uuid not in active:
                saved.append(uuid)

        return {
            'active': active,
            'saved': saved
        }

    async def _log_power_task(self, session: PowerSession):
        """Background task to periodically log power usage"""
        logger.debug(f"Starting power logging task for session {session.uuid}")

        while session.active:
            try:
                # Get power reading through the PDU runner
                runner = self.daemon.runners.get(session.hostname)
                if not runner:
                    logger.error(f"Runner not found for {session.hostname}")
                    break

                # Get power usage using the driver
                power_watts = await self._get_power_reading(runner, session.port)

                if power_watts is not None:
                    session.add_reading(power_watts)
                    logger.debug(f"Session {session.uuid}: {power_watts:.2f}W")

            except Exception as e:
                logger.error(f"Error logging power for session {session.uuid}: {e}")

            # Wait for the next interval
            try:
                await asyncio.sleep(session.interval)
            except asyncio.CancelledError:
                break

        logger.debug(f"Power logging task finished for session {session.uuid}")

    async def _get_power_reading(self, runner, port: int) -> Optional[float]:
        """Get power reading from a PDU runner"""
        try:
            # For ubus driver, we need to get the info and extract power
            driver = runner.driver
            print("OK")

            # Check if this is a ubus driver
            if hasattr(driver, 'jsonrpc_call'):
                poe_info = driver.jsonrpc_call("poe", "info", {})[1]

                port_key = f"lan{port}"
                if port_key in poe_info.get("ports", {}):
                    port_data = poe_info["ports"][port_key]

                    # Check if port is delivering power
                    if port_data.get("status") == "Delivering power":
                        return port_data.get("consumption", 0.0)
                    else:
                        # Port is not delivering power
                        return 0.0

            # For other drivers that might support power reading
            elif hasattr(driver, 'get_power_usage'):
                return driver.get_power_usage(port)

            logger.warning(f"Driver for {runner.hostname} does not support power reading")
            return None

        except Exception as e:
            logger.error(f"Failed to get power reading: {e}")
            return None

    def _save_session(self, session_data: Dict[str, Any]):
        """Save session data to file"""
        filename = self.storage_path / f"{session_data['uuid']}.json"
        with open(filename, 'w') as f:
            json.dump(session_data, f, indent=2)
        logger.info(f"Saved session data to {filename}")

    def cleanup_old_sessions(self, days: int = 30):
        """Clean up old session files"""
        cutoff_time = time.time() - (days * 24 * 60 * 60)

        for file in self.storage_path.glob("*.json"):
            if file.stat().st_mtime < cutoff_time:
                file.unlink()
                logger.info(f"Deleted old session file {file}")
