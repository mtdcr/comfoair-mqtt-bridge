#!/usr/bin/env python3
#
# Copyright 2025 Andreas Oberritter
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

import argparse
import asyncio
import json
import logging
import sys

from aiomqtt import MqttError

from . import logger
from .bridge import ComfoAirMqttBridge


async def async_main(cfg: dict) -> None:
    if cfg["debug"]:
        logger.setLevel(logging.DEBUG)
    try:
        await ComfoAirMqttBridge(cfg["port"]).run(cfg["broker"], cfg["hass"])
    except MqttError as exc:
        logger.critical(exc)
        sys.exit(1)


def options() -> dict:
    cfg = {
        "config": "/var/lib/comfoair-mqtt-bridge/config.json",
        "broker": "mqtt://localhost",
        "port": "/dev/ttyUSB0",
    }

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", help=f"Location of config file (default: {cfg['config']})"
    )
    parser.add_argument(
        "--broker", help=f"MQTT broker (default: {cfg['broker']})"
    )
    parser.add_argument("--port", help=f"Serial port (default: {cfg['port']})")
    parser.add_argument(
        "--hass",
        action="store_true",
        help="Publish discovery information for Home Assistant",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable logging of debug messages"
    )

    args = parser.parse_args()
    filename = args.config or cfg["config"]

    try:
        with open(filename, "r") as f:
            cfg.update(json.load(f))
    except OSError as exc:
        if args.config or not isinstance(exc, FileNotFoundError):
            logger.error("Failed to open configuration file: %s", exc)
            sys.exit(1)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse configuration file: %s", exc)
        sys.exit(1)

    for key, value in vars(args).items():
        if value is not None:
            cfg[key] = value

    return cfg

def main():
    try:
        asyncio.run(async_main(options()))
    except KeyboardInterrupt:
        pass
