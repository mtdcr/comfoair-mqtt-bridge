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

import asyncio
import json
import logging
import ssl
from functools import partial
from typing import Awaitable, Callable, Tuple, Union
from urllib.parse import urlparse

from aiomqtt import Client, Will
from comfoair.asyncio import ComfoAir
from slugify import slugify

from . import logger


class ComfoAirMqttBridge:
    CC_SEGMENTS = (
        ("Sa", "Su", "Mo", "Tu", "We", "Th", "Fri", ":"),
        # 1-2: Hour, e.g 3, 13 or 23
        ("1ADEG", "1B", "1C", "AUTO", "MANUAL", "FILTER", "I", "E"),
        ("2A", "2B", "2C", "2D", "2E", "2F", "2G", "Ventilation"),
        # 3-4: Minutes, e.g 52
        ("3A", "3B", "3C", "3D", "3E", "3F", "3G", "Extractor hood"),
        ("4A", "4B", "4C", "4D", "4E", "4F", "4G", "Pre-heater"),
        # 5: Stufe, 1, 2, 3 or A
        ("5A", "5B", "5C", "5D", "5E", "5F", "5G", "Frost"),
        # 6-9: Comfort temperature, e.g. 12.0°C
        ("6A", "6B", "6C", "6D", "6E", "6F", "6G", "EWT"),
        ("7A", "7B", "7C", "7D", "7E", "7F", "7G", "Post-heater"),
        ("8A", "8B", "8C", "8D", "8E", "8F", "8G", "."),
        (
            "°",
            "Bypass",
            "9AEF",
            "9G",
            "9D",
            "House",
            "Supply air",
            "Exhaust air",
        ),
    )

    SSD_CHR = {
        0b0000000: " ",
        0b0111111: "0",
        0b0000110: "1",
        0b1011011: "2",
        0b1001111: "3",
        0b1100110: "4",
        0b1101101: "5",
        0b1111101: "6",
        0b0000111: "7",
        0b1111111: "8",
        0b1101111: "9",
        0b1110111: "A",
        0b1111100: "B",
        0b0111001: "C",
        0b1011110: "D",
        0b1111001: "E",
        0b1110001: "F",
    }

    SENSORS = {
        ComfoAir.AIRFLOW_EXHAUST: ("exhaust_airflow", "%"),
        ComfoAir.AIRFLOW_SUPPLY: ("supply_airflow", "%"),
        ComfoAir.FAN_SPEED_MODE: ("preset_mode", None),
        ComfoAir.TEMP_COMFORT: ("comfort_temperature", "°C"),
        ComfoAir.TEMP_EXHAUST: ("exhaust_temperature", "°C"),
        ComfoAir.TEMP_OUTSIDE: ("outside_temperature", "°C"),
        ComfoAir.TEMP_RETURN: ("return_temperature", "°C"),
        ComfoAir.TEMP_SUPPLY: ("supply_temperature", "°C"),
    }

    PRESET_MODES = ["off", "low", "medium", "high"]

    def __init__(self, port: str):
        self._ca = ComfoAir(port)
        self._name = "ComfoAir"
        self._device_id = self._ca.device_id()
        self._base_topic = f"{self._name}/{self._device_id}"
        self._cache = {}
        self._callbacks = {}
        self._turn_on_speed = 2
        self._model = None
        self._fw_version = None
        self._has_fw_version = asyncio.Event()
        self._available = None
        self._data_received = asyncio.Event()

    def _topic(self, name: str) -> str:
        return f"{self._base_topic}/{name}"

    def _hass_topic(self, component: str, object_id: str) -> str:
        object_id = slugify(object_id)
        return f"homeassistant/{component}/comfoair/{object_id}/config"

    async def _set_speed(self, speed: int) -> bool:
        if not 1 <= speed <= 4:
            return False

        if speed > 1:
            self._turn_on_speed = speed
        await self._ca.set_speed(speed)
        return True

    async def _cmd_on_off(self, data: str) -> None:
        speed = {"ON": self._turn_on_speed, "OFF": 1}.get(data)
        if speed is not None:
            await self._set_speed(speed)

    async def _cmd_speed(self, data: str) -> None:
        if data not in self.PRESET_MODES:
            return
        speed = self.PRESET_MODES.index(data) + 1
        await self._set_speed(speed)

    async def _emulate_keypress(self, data: str, ms: int) -> None:
        if not data.isnumeric():
            return

        mask = int(data)
        if not 1 <= mask <= 64:
            return

        await self._ca.emulate_keypress(mask, ms)
        return

    async def _keys_short(self, data: str) -> None:
        return await self._emulate_keypress(data, 100)

    async def _keys_long(self, data: str) -> None:
        return await self._emulate_keypress(data, 1000)

    async def _cooked_event(
        self, mqtt, attribute: ComfoAir.Attribute, value: Union[float, int, str]
    ) -> None:
        logger.debug("Cooked event (%s): %s", attribute, value)

        name = self.SENSORS[attribute][0]

        if attribute == ComfoAir.FAN_SPEED_MODE:
            if value not in (1, 2, 3, 4):
                return
            await self._publish(mqtt, self._topic("state"), value > 1)
            value = self.PRESET_MODES[value - 1]

        await self._publish(mqtt, self._topic(name), value)

    async def _raw_event(self, ev: Tuple[int, bytes]) -> None:
        self._data_received.set()
        cmd, data = ev
        if self._cache.get(cmd) == data:
            return
        self._cache[cmd] = data

        logger.debug("Raw event (%#x): %s", cmd, data.hex())

        if cmd == 0x3C and len(self.CC_SEGMENTS) == len(data):
            segments = []
            for pos, val in enumerate(data):
                if pos == 1:
                    digit = val & 6
                    if val & 1:
                        digit |= 0b1011001
                    assert digit in self.SSD_CHR
                    segments.append(self.SSD_CHR[digit])
                    offset = 3
                elif 2 <= pos <= 8:
                    digit = val & 0x7F
                    assert digit in self.SSD_CHR
                    segments.append(self.SSD_CHR[digit])
                    offset = 7
                elif pos == 9:
                    digit = 0
                    if val & 4:
                        digit |= 0b0110001
                    if val & 8:
                        digit |= 0b1000000
                    if val & 0x10:
                        digit |= 0b0001000
                    assert digit in self.SSD_CHR
                    segments.append(self.SSD_CHR[digit])
                    for i in (0, 1, 5, 6, 7):
                        if val & (1 << i):
                            segments.append(self.CC_SEGMENTS[pos][i])
                    offset = 8
                else:
                    offset = 0

                for i in range(offset, 8):
                    if val & (1 << i):
                        segments.append(self.CC_SEGMENTS[pos][i])

            logger.debug("CC display segments: [%s]", "|".join(segments))

        elif cmd in (0x68, 0x6A) and len(data) == 13:
            # Information about Bootloader (0x68) and Firmware (0x6A)
            version = f"{data[0]}.{data[1]}.{data[2]}"
            model = data[3:].decode("ascii")

            if cmd == 0x68:
                what = "Bootloader"
            else:
                what = "Firmware"
                self._fw_version = version
                self._model = model
                self._has_fw_version.set()

            logger.debug(f"{what} version: {version}")
            logger.debug(f"{what} model: {model}")

        elif cmd == 0xA2 and len(data) == 14:
            # Information about connector board
            version = f"{data[0]}.{data[1]}"
            model = data[2:12].decode("ascii")

            what = "Board"
            logger.debug(f"{what} version: {version}")
            logger.debug(f"{what} model: {model}")

            if data[12]:
                logger.debug(
                    "CC-Ease: %s.%s", data[12] >> 4 & 0xF, data[12] & 0xF
                )
            if data[13]:
                logger.debug(
                    "CC-Luxe: %s.%s", data[13] >> 4 & 0xF, data[13] & 0xF
                )

    async def _process_packet(self, message) -> None:
        callbacks = self._callbacks.get(message.topic.value)
        if not callbacks:
            logger.error("Unhandled topic: %s", message.topic)
            return

        try:
            data = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            logger.error("Invalid payload: %s", message.payload)
        else:
            for callback in callbacks:
                if not await callback(data):
                    logger.error("Invalid parameter: %s", data)

    async def _publish(
        self,
        mqtt,
        topic: str,
        message: Union[bool, bytes, dict, float, int, str],
    ) -> None:
        if isinstance(message, dict):
            message = json.dumps(message)
        elif isinstance(message, bool):
            message = [b"OFF", b"ON"][message]
        elif isinstance(message, (float, int)):
            message = str(message)

        if isinstance(message, str):
            message = message.encode("utf-8")

        logger.debug(f"Publish: {topic} {message}")
        assert isinstance(message, bytes)
        await mqtt.publish(topic, message, qos=0, retain=True)

    async def _publish_availability(self, mqtt, status: bool) -> None:
        await self._publish(
            mqtt, self._topic("availability"), [b"offline", b"online"][status]
        )

    async def _publish_hass_config(self, mqtt) -> None:
        device = {
            "name": self._name,
            "identifiers": [self._device_id],
            "manufacturer": "Zehnder",
            "model": self._model,
            "sw_version": self._fw_version,
        }

        # https://www.home-assistant.io/integrations/fan.mqtt/
        object_id = f"{self._device_id}-fan"
        config = {
            "availability_topic": self._topic("availability"),
            "command_topic": self._topic("command"),
            "device": device,
            "name": self._name,
            "preset_mode_command_topic": self._topic("preset_mode_command"),
            "preset_mode_state_topic": self._topic("preset_mode"),
            "preset_modes": self.PRESET_MODES,
            "state_topic": self._topic("state"),
            "unique_id": object_id,
        }

        topic = self._hass_topic("fan", object_id)
        await self._publish(mqtt, topic, config)

        # https://www.home-assistant.io/integrations/sensor.mqtt/
        for attrs in self.SENSORS.values():
            name, unit = attrs
            object_id = f"{self._device_id}-{name}"
            config = {
                "availability_topic": self._topic("availability"),
                "device": device,
                "name": f"{self._name} {name}",
                "state_topic": self._topic(name),
                "unique_id": object_id,
            }
            if unit:
                config["unit_of_measurement"] = unit
                if unit == "°C":
                    config["device_class"] = "temperature"
                elif unit == "%":
                    config["icon"] = "mdi:fan"

            topic = self._hass_topic("sensor", object_id)
            await self._publish(mqtt, topic, config)

    async def _subscribe(
        self, mqtt, topic: str, callback: Callable[[str], Awaitable[None]]
    ) -> None:
        if topic not in self._callbacks:
            self._callbacks[topic] = set()
        self._callbacks[topic].add(callback)
        await mqtt.subscribe(topic)

    async def _unsubscribe(
        self, mqtt, topic: str, callback: Callable[[str], Awaitable[None]]
    ) -> None:
        self._callbacks[topic].remove(callback)
        if not self._callbacks[topic]:
            del self._callbacks[topic]
        await mqtt.unsubscribe(topic)

    async def _subscribe_commands(self, mqtt) -> None:
        await self._subscribe(mqtt, self._topic("command"), self._cmd_on_off)
        await self._subscribe(
            mqtt, self._topic("preset_mode_command"), self._cmd_speed
        )
        await self._subscribe(mqtt, self._topic("keys_short"), self._keys_short)
        await self._subscribe(mqtt, self._topic("keys_long"), self._keys_long)

    async def _unsubscribe_commands(self, mqtt) -> None:
        await self._unsubscribe(mqtt, self._topic("command"), self._cmd_on_off)
        await self._unsubscribe(
            mqtt, self._topic("preset_mode_command"), self._cmd_speed
        )
        await self._unsubscribe(
            mqtt, self._topic("keys_short"), self._keys_short
        )
        await self._unsubscribe(mqtt, self._topic("keys_long"), self._keys_long)

    async def _update_availability(self, mqtt, status: bool) -> None:
        if self._available != status:
            self._available = status
            await self._publish_availability(mqtt, self._available)

    async def _watchdog(self, mqtt) -> None:
        logger.info("Watchdog: Starting")
        while True:
            logger.debug("Watchdog: Waiting for data")
            self._data_received.clear()

            available = False
            try:
                await asyncio.wait_for(self._data_received.wait(), 10.0)
            except asyncio.TimeoutError:
                logger.warning("Watchdog: Timeout, trying to recover")
                asyncio.create_task(self._ca.request_firmware_version())
            else:
                available = True

            await self._update_availability(mqtt, available)

    async def run(self, broker: str, hass: bool) -> None:
        p = urlparse(broker, scheme="mqtt")
        if p.scheme not in ("mqtt", "mqtts") or not p.hostname:
            raise ValueError

        tls_context = None
        if p.scheme == "mqtts":
            tls_context = ssl.create_default_context()

        will = Will(
            self._topic("availability"), payload=b"offline", qos=1, retain=True
        )
        async with Client(
            p.hostname,
            port=p.port or p.scheme == "mqtt" and 1883 or 8883,
            username=p.username,
            password=p.password,
            logger=logger,
            tls_context=tls_context,
            will=will,
        ) as mqtt:
            asyncio.create_task(self._watchdog(mqtt))

            cooked_event = partial(self._cooked_event, mqtt)

            await self._ca.connect()
            self._ca.add_listener(self._raw_event)
            for sensor in self.SENSORS:
                self._ca.add_cooked_listener(sensor, cooked_event)

            await self._subscribe_commands(mqtt)

            if hass:
                await self._ca.request_firmware_version()
                await self._has_fw_version.wait()
                await self._publish_hass_config(mqtt)

            try:
                async for message in mqtt.messages:
                    await self._process_packet(message)
            except asyncio.CancelledError:
                pass

            await self._update_availability(mqtt, False)
            await self._unsubscribe_commands(mqtt)

            for sensor in self.SENSORS:
                self._ca.remove_cooked_listener(sensor, cooked_event)
            self._ca.remove_listener(self._raw_event)
            await self._ca.shutdown()
