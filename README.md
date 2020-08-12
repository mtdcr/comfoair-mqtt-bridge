# A bridge between Zehnder ComfoAir ventilation units and MQTT with support for Home Assistant
This program receives events from a ComfoAir connected to a serial port and publishes contained data on [MQTT](https://mqtt.org/). It also subscribes to commands from MQTT and relays them to the ventilation unit.

Currently it assumes presence of a CC-Ease control unit. It's able to switch between CC-Ease- and PC-Modes quite reliably on demand.

Tested devices:
* [Zehnder ComfoAir 350](https://www.international.zehnder-systems.com/products-and-systems/comfosystems/zehnder-comfoair-350)

[Home Assistant](https://www.home-assistant.io/), when configured for [MQTT discovery](https://www.home-assistant.io/docs/mqtt/discovery/), can auto-detect fans and sensors published by this program.

## Dependencies
* Python >= 3.7
* [hbmqtt](https://pypi.org/project/hbmqtt/)
* [pycomfoair](https://pypi.org/project/pycomfoair/) >= 0.0.4
* [python-slugify](https://pypi.org/project/python-slugify/)

## How to use

* Using a real serial port:
```sh
comfoair-mqtt-bridge.py --broker mqtt://broker.local --port /dev/ttyUSB0
```

* With a serial port reachable over tcp, e.g. using ser2net:
```sh
comfoair-mqtt-bridge.py --broker mqtt://broker.local --port socket://sbc.local:51765
```
When used in combination with Home Assistant, add the `--hass` option.

To see debug messages, add `--debug`.

A `/etc/ser2net.conf` should look similar to this one:
```
192.168.3.50,51765:raw:0:/dev/serial/by-id/usb-067b_2303-if00-port0:9600 8DATABITS NONE 1STOPBIT -XONXOFF -RTSCTS -HANGUP_WHEN_DONE
```

## Running the bridge as a systemd service
Save this file as `/etc/systemd/system/comfoair-mqtt-bridge.service`:
```ini
[Unit]
Description=ComfoAir-MQTT-Bridge
After=network-online.target

[Service]
Type=simple
DynamicUser=yes
ExecStart=/srv/homeassistant/bin/python /usr/local/bin/comfoair-mqtt-bridge.py --broker mqtt://broker.local --port /dev/ttyUSB0 --hass
Restart=on-failure
RestartSec=5s
DeviceAllow=/dev/null rw

[Install]
WantedBy=multi-user.target
```
Edit paths and URLs to match your setup. In this example, `/srv/homeassistant` is a Python [venv](https://docs.python.org/3/library/venv.html), into which [hbmqtt](https://pypi.org/project/hbmqtt/) and [pycomfoair](https://pypi.org/project/pycomfoair/) were installed using [pip](https://docs.python.org/3/installing/index.html).

Then enable and start the service:
```sh
systemctl enable comfoair-mqtt-bridge.service
systemctl start comfoair-mqtt-bridge.service
```

## Notes regarding Home Assistant
* Home Assistant will auto-detect a device with multiple entities (currently `fan.comfoair` and some sensors).
* All entities get marked as unavailable if the bridge gets shutdown cleanly (SIGINT, SIGTERM) or if there's no data on the serial line.
* You can emulate a pressed button on the CC-Ease.
  * Each key resembles one bit: **1=fan**, **2=mode**, **4=clock**, **8=temp**, **16=plus**, **32=minus**
  * To press multiple keys at the same time, sum up their values. Example: fan + mode = 3
  * You can either send a short key press by publishing the *keys_short* attribute, or a long key press by publishing the *keys_long* attribute on MQTT.
  * Pressing the *mode* key toggles between three operating modes:
    * Supply + Exhaust
    * Exhaust only
    * Supply only
  * What follows is an example for a [tap_action in Lovelace](https://www.home-assistant.io/lovelace/actions/) to send a short *mode* key press event to your ventilation unit. The *topic* gets derived from your command-line parameters. To find it in Home Assistant, open *Configuration* -> *Devices* -> *ComfoAir* -> *MQTT INFO* and look for *Subscribed Topics*. Then replace e.g. *availability* with *keys_short*.
```yaml
"tap_action": {
  "action": "call-service",
  "service": "mqtt.publish",
  "service_data": {
    "payload": 2,
    "retain": false,
    "topic": "ComfoAir/sbc.local:51765/keys_short"
  }
}
```
