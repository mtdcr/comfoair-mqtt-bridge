#!/usr/bin/env python3
#
# Copyright 2020 Andreas Oberritter
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
import signal
from typing import Callable, Tuple, Union

from comfoair.asyncio import ComfoAir
from hbmqtt.client import ClientException, MQTTClient
from hbmqtt.mqtt.constants import QOS_2
from hbmqtt.mqtt.publish import PublishPacket
from slugify import slugify

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())


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
        ComfoAir.FAN_SPEED_MODE: ("speed_mode", None),
        ComfoAir.TEMP_COMFORT: ("comfort_temperature", "°C"),
        ComfoAir.TEMP_EXHAUST: ("exhaust_temperature", "°C"),
        ComfoAir.TEMP_OUTSIDE: ("outside_temperature", "°C"),
        ComfoAir.TEMP_RETURN: ("return_temperature", "°C"),
        ComfoAir.TEMP_SUPPLY: ("supply_temperature", "°C"),
    }

    def __init__(self, port: str, broker: str):
        self._ca = ComfoAir(port)
        self._broker = broker
        self._name = "ComfoAir"
        self._device_id = self._ca.device_id()
        self._base_topic = f"{self._name}/{self._device_id}"
        self._cache = {}
        self._callbacks = {}
        self._turn_on_speed = 2
        self._model = None
        self._fw_version = None
        self._has_fw_version = asyncio.Event()
        self._mainloop_task = None
        self._available = None
        self._data_received = asyncio.Event()
        self._cancelled = False
        self._mqtt = MQTTClient(
            config={
                "will": {
                    "topic": self._topic("availability"),
                    "message": b"offline",
                    "qos": QOS_2,
                    "retain": True,
                }
            }
        )

        self._ca.add_listener(self._raw_event)
        for sensor in self.SENSORS:
            self._ca.add_cooked_listener(sensor, self._cooked_event)

    def _topic(self, name):
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

    async def _cmd_on_off(self, data: str) -> bool:
        speed = {"ON": self._turn_on_speed, "OFF": 1}.get(data)
        return speed and await self._set_speed(speed)

    async def _cmd_speed(self, data: str) -> bool:
        return data.isnumeric() and await self._set_speed(int(data))

    async def _emulate_keypress(self, data: str, ms: int) -> None:
        if not data.isnumeric():
            return False

        mask = int(data)
        if not 1 <= mask <= 64:
            return False

        await self._ca.emulate_keypress(mask, ms)
        return True

    async def _keys_short(self, data: str) -> None:
        return await self._emulate_keypress(data, 100)

    async def _keys_long(self, data: str) -> None:
        return await self._emulate_keypress(data, 1000)

    async def _cooked_event(
        self, attribute: ComfoAir.Attribute, value: Union[float, int, str]
    ) -> None:
        logger.debug("Cooked event (%s): %s", attribute, value)

        name = self.SENSORS[attribute][0]
        await self._publish(self._topic(name), value)

        if attribute == ComfoAir.FAN_SPEED_MODE:
            await self._publish(self._topic("state"), value > 1)

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

    async def _process_packet(self, packet: PublishPacket) -> None:
        topic = packet.variable_header.topic_name
        callbacks = self._callbacks.get(topic)
        if not callbacks:
            logger.error("Unhandled topic: %s", topic)
            return

        try:
            data = packet.payload.data.decode("utf-8")
        except UnicodeDecodeError:
            logger.error("Invalid payload: %s", packet.payload.data)
        else:
            for callback in callbacks:
                if not await callback(data):
                    logger.error("Invalid parameter: %s", data)

    async def _publish(
        self, topic: str, message: Union[bool, bytes, dict, float, int, str]
    ) -> None:
        if isinstance(message, dict):
            message = json.dumps(message)
        elif isinstance(message, (float, int)):
            message = str(message)
        elif isinstance(message, bool):
            message = [b"OFF", b"ON"][message]

        if isinstance(message, str):
            message = message.encode("utf-8")

        logger.debug(f"Publish: {topic} {message}")
        assert isinstance(message, bytes)
        await self._mqtt.publish(topic, message, qos=QOS_2, retain=True)

    async def _publish_availability(self, status: bool) -> None:
        await self._publish(
            self._topic("availability"), [b"offline", b"online"][status]
        )

    async def _publish_hass_config(self) -> None:
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
            "payload_off_speed": "1",
            "payload_low_speed": "2",
            "payload_medium_speed": "3",
            "payload_high_speed": "4",
            "speed_command_topic": self._topic("speed_command"),
            "speed_state_topic": self._topic("speed_mode"),
            "state_topic": self._topic("state"),
            "unique_id": object_id,
        }

        topic = self._hass_topic("fan", object_id)
        await self._publish(topic, config)

        # https://www.home-assistant.io/integrations/sensor.mqtt/
        for sensor, attrs in self.SENSORS.items():
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
            await self._publish(topic, config)

    async def _subscribe(
        self, topic: str, callback: Callable[[str], None]
    ) -> None:
        if topic not in self._callbacks:
            self._callbacks[topic] = set()
        self._callbacks[topic].add(callback)
        topics = [(topic, QOS_2)]
        await self._mqtt.subscribe(topics)

    async def _unsubscribe(
        self, topic: str, callback: Callable[[str], None]
    ) -> None:
        self._callbacks[topic].remove(callback)
        if not self._callbacks[topic]:
            del self._callbacks[topic]
        await self._mqtt.unsubscribe([topic])

    async def _subscribe_commands(self) -> None:
        await self._subscribe(self._topic("command"), self._cmd_on_off)
        await self._subscribe(self._topic("speed_command"), self._cmd_speed)
        await self._subscribe(self._topic("keys_short"), self._keys_short)
        await self._subscribe(self._topic("keys_long"), self._keys_long)

    async def _unsubscribe_commands(self) -> None:
        await self._unsubscribe(self._topic("command"), self._cmd_on_off)
        await self._unsubscribe(self._topic("speed_command"), self._cmd_speed)
        await self._unsubscribe(self._topic("keys_short"), self._keys_short)
        await self._unsubscribe(self._topic("keys_long"), self._keys_long)

    def _cancel(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in {signal.SIGINT, signal.SIGTERM}:
            loop.remove_signal_handler(sig)
        self._cancelled = True
        if self._mainloop_task:
            self._mainloop_task.cancel()

    async def _mainloop(self) -> None:
        logger.debug("Running mainloop")
        while True:
            try:
                message = await self._mqtt.deliver_message()
            except ClientException as exc:
                logger.error("Client exception: %s", exc)
            else:
                await self._process_packet(message.publish_packet)

    async def _update_availability(self, status: bool) -> None:
        if self._available != status and self._mainloop_task:
            self._available = status
            await self._publish_availability(self._available)

    async def _watchdog(self) -> None:
        logger.info("Watchdog: Starting")
        while not self._cancelled:
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

            await self._update_availability(available)

    async def run(self, hass: bool = True) -> None:
        loop = asyncio.get_running_loop()
        for sig in {signal.SIGINT, signal.SIGTERM}:
            loop.add_signal_handler(sig, self._cancel)

        asyncio.create_task(self._watchdog())
        await self._mqtt.connect(self._broker)
        await self._ca.connect()
        await self._subscribe_commands()

        if hass:
            await self._ca.request_firmware_version()
            await self._has_fw_version.wait()
            await self._publish_hass_config()

        if not self._cancelled:
            self._mainloop_task = asyncio.create_task(self._mainloop())
            try:
                await self._mainloop_task
            except asyncio.CancelledError:
                pass

        await self._update_availability(False)
        await self._unsubscribe_commands()
        await self._ca.shutdown()
        await self._mqtt.disconnect()


async def main(args: argparse.Namespace) -> None:
    if args.debug:
        logger.setLevel(logging.DEBUG)
    await ComfoAirMqttBridge(args.port, args.broker).run(hass=args.hass)


parser = argparse.ArgumentParser()
parser.add_argument(
    "--broker",
    default="mqtt://localhost",
    type=str,
    required=False,
    help="MQTT broker (default: %(default)s)",
)
parser.add_argument(
    "--port",
    default="/dev/ttyUSB0",
    type=str,
    required=False,
    help="Serial port (default: %(default)s)",
)
parser.add_argument(
    "--hass",
    action="store_true",
    help="Publish discovery information for Home Assistant",
)
parser.add_argument(
    "--debug",
    action="store_true",
    help="Enable logging of debug messages",
)
asyncio.run(main(parser.parse_args()))
