"""Passive BLE monitor sensor platform."""
import asyncio
from datetime import timedelta
import logging
import statistics as sts
import struct
import subprocess
import os
from threading import Thread, Lock
from time import sleep

import aioblescan as aiobs

from homeassistant.const import (
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_TEMPERATURE,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
    ATTR_BATTERY_LEVEL,
    STATE_OFF,
    STATE_ON,
)

from . import (
    DOMAIN,
    CONF_DEVICES,
    CONF_DISCOVERY,
    CONF_ROUNDING,
    CONF_DECIMALS,
    CONF_PERIOD,
    CONF_LOG_SPIKES,
    CONF_USE_MEDIAN,
    CONF_ACTIVE_SCAN,
    CONF_HCI_INTERFACE,
    CONF_BATT_ENTITIES,
    CONF_REPORT_UNKNOWN,
    CONF_ESPRUINO_PATH
)

from .const import (
    CONF_TMIN,
    CONF_TMAX,
    CONF_HMIN,
    CONF_HMAX
)


from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import track_point_in_utc_time
import homeassistant.util.dt as dt_util

_LOGGER = logging.getLogger(__name__)

PUCKJS_SOURCE_CODE = os.path.join(os.path.dirname(__file__), "ha-puck.js")

_LOGGER = logging.getLogger(__name__)

class HCIdump(Thread):
    """Mimic deprecated hcidump tool."""

    def __init__(self, dumplist, interface=0, active=0):
        """Initiate HCIdump thread."""
        Thread.__init__(self)
        _LOGGER.debug("HCIdump thread: Init")
        self._interface = interface
        self._active = active
        self.dumplist = dumplist
        self._event_loop = None
        _LOGGER.debug("HCIdump thread: Init finished")

    def process_hci_events(self, data):
        """Collect HCI events."""
        self.dumplist.append(data)

    def run(self):
        """Run HCIdump thread."""
        _LOGGER.debug("HCIdump thread: Run")
        try:
            mysocket = aiobs.create_bt_socket(self._interface)
        except OSError as error:
            _LOGGER.error("HCIdump thread: OS error: %s", error)
        else:
            self._event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._event_loop)
            fac = self._event_loop._create_connection_transport(
                mysocket, aiobs.BLEScanRequester, None, None
            )
            _LOGGER.debug("HCIdump thread: Connection")
            conn, btctrl = self._event_loop.run_until_complete(fac)
            _LOGGER.debug("HCIdump thread: Connected")
            btctrl.process = self.process_hci_events
            btctrl.send_command(
                aiobs.HCI_Cmd_LE_Set_Scan_Params(scan_type=self._active)
            )
            btctrl.send_scan_request()
            _LOGGER.debug("HCIdump thread: start main event_loop")
            try:
                self._event_loop.run_forever()
            finally:
                _LOGGER.debug(
                    "HCIdump thread: main event_loop stopped, finishing",
                )
                btctrl.stop_scan_request()
                conn.close()
                self._event_loop.run_until_complete(asyncio.sleep(0))
                self._event_loop.close()
                _LOGGER.debug("HCIdump thread: Run finished")

    def join(self, timeout=10):
        """Join HCIdump thread."""
        _LOGGER.debug("HCIdump thread: joining")
        try:
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        except AttributeError as error:
            _LOGGER.debug("%s", error)
        finally:
            Thread.join(self, timeout)
            _LOGGER.debug("HCIdump thread: joined")



def parse_raw_message(data, whitelist, report_unknown=False):
    """Parse the raw data."""
    if data is None:
        return None

    ev=aiobs.HCI_Event()
    decoded_msg=ev.decode(data)
    mac= ev.retrieve("peer")
    mac= mac[0].val if len(mac) > 0 else None

    if not mac or (whitelist and mac not in whitelist):
        return
    
    rssi = ev.retrieve("rssi")
    rssi = rssi[0].val if len(rssi) > 0 else None
    
    manufacturer_data = ev.retrieve("Manufacturer Specific Data")
    if len(manufacturer_data) == 0:
        # This message is not a manufacture data
        return { "rssi": rssi, "mac": mac, "type": "puck.js" }

    manufacturer_id = manufacturer_data[0].retrieve("Manufacturer ID")
    manufacturer_id = manufacturer_id[0].val

    if manufacturer_id != 1424:
        # This is not the puck
        return

    payload = manufacturer_data[0].retrieve("Payload")
    if len(payload) == 0:
        return
    
    payload = payload[0].val

    battery     = float(payload[0:3])
    temperature = float(payload[3:8])
    direction   = bool(int(payload[8]) & 2)
    button      = bool(int(payload[8]) & 1)

    result = {
        "rssi": rssi,
        "mac": mac,
        "temperature" : temperature,
        "battery" : battery,
        "direction" : direction,
        "button" : button,
        "type": "puck.js" }
    
    return result


def sensor_name(config, mac, sensor_type):
    """Set sensor name."""

    if config[CONF_DEVICES]:
        for device in config[CONF_DEVICES]:
            if mac.upper() in device["mac"].upper():
                if "name" in device:
                    custom_name = device["name"]
                    _LOGGER.debug(
                        "Name of %s sensor with mac adress %s is set to: %s",
                        sensor_type,
                        mac,
                        custom_name,
                    )
                    return custom_name
                break
    return mac


def temperature_unit(config, mac):
    """Set temperature unit to °C or °F."""

    if config[CONF_DEVICES]:
        for device in config[CONF_DEVICES]:
            if mac in device["mac"].upper():
                if "temperature_unit" in device:
                    _LOGGER.debug(
                        "Temperature sensor with mac address %s is set to receive data in %s",
                        mac,
                        device["temperature_unit"],
                    )
                    return device["temperature_unit"]
                break
    _LOGGER.debug(
        "Temperature sensor with mac address %s is set to receive data in °C",
        mac,
    )
    return TEMP_CELSIUS


def temperature_limit(config, mac, temp):
    """Set limits for temperature measurement in °C or °F."""
    if config[CONF_DEVICES]:
        for device in config[CONF_DEVICES]:
            if mac in device["mac"].upper():
                if "temperature_unit" in device:
                    if device["temperature_unit"] == TEMP_FAHRENHEIT:
                        temp_fahrenheit = temp * 9 / 5 + 32
                        return temp_fahrenheit
                break
    return temp


class BLEScanner:
    """BLE scanner."""

    dumpthreads = []
    hcidump_data = []

    def start(self, config):
        """Start receiving broadcasts."""
        active_scan = config[CONF_ACTIVE_SCAN]
        hci_interfaces = config[CONF_HCI_INTERFACE]
        self.hcidump_data.clear()
        _LOGGER.debug("Spawning HCIdump thread(s).")
        for hci_int in hci_interfaces:
            dumpthread = HCIdump(
                dumplist=self.hcidump_data,
                interface=hci_int,
                active=int(active_scan is True),
            )
            self.dumpthreads.append(dumpthread)
            _LOGGER.debug("Starting HCIdump thread for hci%s", hci_int)
            dumpthread.start()
        _LOGGER.debug("HCIdump threads count = %s", len(self.dumpthreads))

    def stop(self):
        """Stop HCIdump thread(s)."""
        result = True
        for dumpthread in self.dumpthreads:
            if dumpthread.is_alive():
                dumpthread.join()
                if dumpthread.is_alive():
                    result = False
                    _LOGGER.error(
                        "Waiting for the HCIdump thread to finish took too long! (>10s)"
                    )
        if result is True:
            self.dumpthreads.clear()
        return result

    def shutdown_handler(self, event):
        """Run homeassistant_stop event handler."""
        _LOGGER.debug("Running homeassistant_stop event handler: %s", event)
        self.stop()

def program_puckjs(espruino_path, mac):
    _LOGGER.info('Programming Puck.js with mac: %s.', mac)
    try:
        output = subprocess.check_output([espruino_path, '-p', mac, PUCKJS_SOURCE_CODE],
                                         stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as err:
        _LOGGER.warning("Running '{}'".format(err.cmd) + "\ngave the following error:\n" + str(err.output.decode()))

        
def setup_platform(hass, conf, add_entities, discovery_info=None):
    """Set up the sensor platform."""

    _LOGGER.debug("Starting")
    config = hass.data[DOMAIN]
    firstrun = True
    scanner = BLEScanner()
    hass.bus.listen("homeassistant_stop", scanner.shutdown_handler)
    scanner.start(config)
    sensors_by_mac = {}
    lock = Lock()

    if config[CONF_REPORT_UNKNOWN]:
        _LOGGER.info(
            "Attention! Option report_unknown is enabled, be ready for a huge output..."
        )

    whitelist = []
    if isinstance(config[CONF_DISCOVERY], bool):
        if config[CONF_DISCOVERY] is False:
            if config[CONF_DEVICES]:
                for device in config[CONF_DEVICES]:
                    whitelist.append(device["mac"])
    # remove duplicates from whitelist
    whitelist = list(dict.fromkeys(whitelist))
    _LOGGER.debug("whitelist: [%s]", ", ".join(whitelist).upper())
    _LOGGER.debug("%s whitelist item(s) loaded.", len(whitelist))
    sleep(1)

    def handle_program_puckjs(call, explicit_macs=[]):
        """Handle the service call."""
        with lock:
            jres = scanner.stop()
            if jres is False:
                _LOGGER.error("HCIdump thread(s) is not completed, interrupting !")
                return
            if explicit_macs:
                macs = explicit_macs
            else:
                macs = sensors_by_mac.keys()
                
            for mac in macs:
                program_puckjs(config.get(CONF_ESPRUINO_PATH), mac)              

            scanner.start(config)  # minimum delay between HCIdumps

    def calc_update_state(
        entity_to_update,
        sensor_mac,
        config,
        measurements_list,
        stype=None,
        fdec=0,
    ):
        """Averages according to options and updates the entity state."""
        textattr = ""
        success = False
        error = ""
        rdecimals = config[CONF_DECIMALS]
        # formaldehyde decimals workaround
        if fdec > 0:
            rdecimals = fdec

        measurements = measurements_list
        try:
            if config[CONF_ROUNDING]:
                state_median = round(sts.median(measurements), rdecimals)
                state_mean = round(sts.mean(measurements), rdecimals)
            else:
                state_median = sts.median(measurements)
                state_mean = sts.mean(measurements)
            if config[CONF_USE_MEDIAN]:
                textattr = "last median of"
                setattr(entity_to_update, "_state", state_median)
            else:
                textattr = "last mean of"
                setattr(entity_to_update, "_state", state_mean)
            getattr(entity_to_update, "_device_state_attributes")[
                textattr
            ] = len(measurements)
            getattr(entity_to_update, "_device_state_attributes")[
                "median"
            ] = state_median
            getattr(entity_to_update, "_device_state_attributes")[
                "mean"
            ] = state_mean
            entity_to_update.schedule_update_ha_state()
            success = True
        except (AttributeError, AssertionError):
            _LOGGER.debug("Sensor %s not yet ready for update", sensor_mac)
            success = True
        except ZeroDivisionError as err:
            error = err
        except IndexError as err:
            error = err
        except RuntimeError as err:
            error = err
        return success, error

    def discover_ble_devices(config, whitelist):
        """Discover Bluetooth LE devices."""
        nonlocal firstrun
        if firstrun:
            firstrun = False
            _LOGGER.debug("First run, skip parsing.")
            return []
        _LOGGER.debug("Discovering Bluetooth LE devices")
        log_spikes = config[CONF_LOG_SPIKES]
        _LOGGER.debug("Time to analyze...")
        stype = {}
        temp_m_data = {}
        direction_m_data = {}
        button_m_data = {}
        batt = {}  # battery
        rssi = {}
        macs = {}  # all found macs
        fw_not_found = {}
        with lock:
            _LOGGER.debug("Getting data from HCIdump thread")
            jres = scanner.stop()
            if jres is False:
                _LOGGER.error("HCIdump thread(s) is not completed, interrupting data processing!")
                return []
            hcidump_raw = [*scanner.hcidump_data]
            scanner.start(config)  # minimum delay between HCIdumps
        report_unknown = config[CONF_REPORT_UNKNOWN]
        for msg in hcidump_raw:
            data = parse_raw_message(msg, whitelist, report_unknown)

            if data and "mac" in data:
                # ignore duplicated message
                mac = data["mac"]
                # store found readings per device
                if "temperature" in data:
                    if (
                        temperature_limit(config, mac, CONF_TMAX)
                        >= data["temperature"]
                        >= temperature_limit(config, mac, CONF_TMIN)
                    ):
                        if mac not in temp_m_data:
                            temp_m_data[mac] = []
                        temp_m_data[mac].append(data["temperature"])
                        macs[mac] = mac
                    elif log_spikes:
                        _LOGGER.error(
                            "Temperature spike: %s (%s)",
                            data["temperature"],
                            mac,
                        )
                    fw_not_found[max] = None
                else:
                    # No temperature info so this indicates that we don't have a proper FW
                    # in the puck
                    if fw_not_found.get(mac, None):
                        time_since_last_data_found = dt_util.utcnow() - fw_not_found[max]
                        if time_since_last_data_found > timedelta(seconds=60):
                            # If more than 60 seconds without a temperature reading
                            # try to update the FW
                            handle_program_puckjs(None,[mac])                             
                            fw_not_found[max] = None
                    else:
                        fw_not_found[max] = dt_util.utcnow()
                    
                if "direction" in data:
                    direction_m_data[mac] = int(data["direction"])
                    macs[mac] = mac
                if "button" in data:
                    button_m_data[mac] = int(data["button"])
                    macs[mac] = mac
                if "battery" in data:
                    batt[mac] = int(data["battery"])
                    macs[mac] = mac
                if mac not in rssi:
                    rssi[mac] = []
                if "rssi" in data and data["rssi"]:
                    rssi[mac].append(int(data["rssi"]))
                stype[mac] = data["type"]
            else:
                # "empty" loop high cpu usage workaround
                sleep(0.0001)
        # for every seen device
        for mac in macs:
            # if necessary, create a list of entities
            # according to the sensor implementation
            t_i, sw_i, d_i, b_i = range(4)
            if mac in sensors_by_mac:
                sensors = sensors_by_mac[mac]
            else:
                sensors = []
                sensors.insert(t_i, TemperatureSensor(config, mac))
                sensors.insert(sw_i, SwitchBinarySensor(config, mac, "button"))
                sensors.insert(d_i, SwitchBinarySensor(config, mac, "direction"))
                if config[CONF_BATT_ENTITIES]:
                    sensors.insert(b_i, BatterySensor(config, mac))
                sensors_by_mac[mac] = sensors
                add_entities(sensors)
            # append joint attributes
            sensortype = stype[mac]
            for sensor in sensors:
                getattr(sensor, "_device_state_attributes")["rssi"] = round(
                    sts.mean(rssi[mac])
                )
                getattr(sensor, "_device_state_attributes")["sensor type"] = sensortype
                getattr(sensor, "_device_state_attributes")["mac address"] = mac
                if not isinstance(sensor, BatterySensor) and mac in batt:
                    getattr(sensor, "_device_state_attributes")[
                        ATTR_BATTERY_LEVEL
                    ] = batt[mac]

            # averaging and states updating
            if mac in batt:
                if config[CONF_BATT_ENTITIES]:
                    setattr(sensors[b_i], "_state", batt[mac])
                    try:
                        sensors[b_i].schedule_update_ha_state()
                    except (AttributeError, AssertionError):
                        _LOGGER.debug(
                            "Sensor %s (%s, batt.) not yet ready for update",
                            mac,
                            sensortype,
                        )
                    except RuntimeError as err:
                        _LOGGER.error(
                            "Sensor %s (%s, batt.) update error:",
                            mac,
                            sensortype,
                        )
                        _LOGGER.error(err)
            if mac in temp_m_data:
                success, error = calc_update_state(
                    sensors[t_i], mac, config, temp_m_data[mac]
                )
                if not success:
                    _LOGGER.error(
                        "Sensor %s (%s, temp.) update error:", mac, sensortype
                    )
                    _LOGGER.error(error)
            if mac in button_m_data:
                setattr(sensors[sw_i], "_state", button_m_data[mac])
                try:
                    sensors[sw_i].schedule_update_ha_state()
                except (AttributeError, AssertionError):
                    _LOGGER.debug(
                        "Sensor %s (%s, switch) not yet ready for update",
                        mac,
                        sensortype,
                    )
                except RuntimeError as err:
                    _LOGGER.error(
                        "Sensor %s (%s, switch) update error:", mac, sensortype
                    )
                    _LOGGER.error(err)
            if mac in direction_m_data:
                setattr(sensors[d_i], "_state", direction_m_data[mac])
                try:
                    sensors[d_i].schedule_update_ha_state()
                except (AttributeError, AssertionError):
                    _LOGGER.debug(
                        "Sensor %s (%s, switch) not yet ready for update",
                        mac,
                        sensortype,
                    )
                except RuntimeError as err:
                    _LOGGER.error(
                        "Sensor %s (%s, switch) update error:", mac, sensortype
                    )
                    _LOGGER.error(err)
        _LOGGER.debug(
            "Finished. Parsed: %i hci events, %i puckjs devices.",
            len(hcidump_raw),
            len(macs),
        )
        return []

    def update_ble(now):
        """Lookup Bluetooth LE devices and update status."""
        period = config[CONF_PERIOD]
        _LOGGER.debug("update_ble called")
        try:
            discover_ble_devices(config, whitelist)
        except RuntimeError as error:
            _LOGGER.error("Error during Bluetooth LE scan: %s", error)
        track_point_in_utc_time(
            hass, update_ble, dt_util.utcnow() + timedelta(seconds=period)
        )

    # Register program service
    hass.services.register(DOMAIN, "program", handle_program_puckjs)

    update_ble(dt_util.utcnow())
    # Return successful setup
    return True


class MeasuringSensor(Entity):
    """Base class for measuring sensor entity."""

    def __init__(self, config, mac):
        """Initialize the sensor."""
        self._name = ""
        self._state = None
        self._unit_of_measurement = ""
        self._device_class = None
        self._device_state_attributes = {}
        self._unique_id = ""

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self._unit_of_measurement

    @property
    def device_class(self):
        """Return the device class."""
        return self._device_class

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return self._device_state_attributes

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._unique_id

    @property
    def force_update(self):
        """Force update."""
        return True


class TemperatureSensor(MeasuringSensor):
    """Representation of a sensor."""

    def __init__(self, config, mac):
        """Initialize the sensor."""
        super().__init__(config, mac)
        self._sensor_name = sensor_name(config, mac, "temperature")
        self._name = "puckjs temperature {}".format(self._sensor_name)
        self._unique_id = "t_" + self._sensor_name
        self._unit_of_measurement = temperature_unit(config, mac)
        self._device_class = DEVICE_CLASS_TEMPERATURE


class BatterySensor(MeasuringSensor):
    """Representation of a Sensor."""

    def __init__(self, config, mac):
        """Initialize the sensor."""
        super().__init__(config, mac)
        self._sensor_name = sensor_name(config, mac, "battery")
        self._name = "puckjs battery {}".format(self._sensor_name)
        self._unique_id = "batt_" + self._sensor_name
        self._unit_of_measurement = "%"
        self._device_class = DEVICE_CLASS_BATTERY


class SwitchBinarySensor(BinarySensorEntity):
    """Representation of a Sensor."""

    def __init__(self, config, mac, switch_name):
        """Initialize the sensor."""
        self._sensor_name = sensor_name(config, mac, "switch")
        self._name = "puckjs {} {}".format(switch_name, self._sensor_name)
        self._state = None
        self._unique_id = switch_name + "_" + self._sensor_name
        self._device_state_attributes = {}
        self._device_class = None

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        return bool(self._state)

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the binary sensor."""
        return STATE_ON if self.is_on else STATE_OFF

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return self._device_state_attributes

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._unique_id

    @property
    def device_class(self):
        """Return the device class."""
        return self._device_class

    @property
    def force_update(self):
        """Force update."""
        return True
