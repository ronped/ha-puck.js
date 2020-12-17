"""
Support for puck.js as a sensor through the use of BLE advertising
"""
import logging

import voluptuous as vol
import os
import sys
import subprocess
import datetime

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.helpers.entity import Entity
import homeassistant.helpers.config_validation as cv
from homeassistant.const import (
    CONF_FORCE_UPDATE, CONF_NAME, CONF_MAC, DEVICE_CLASS_TEMPERATURE, DEVICE_CLASS_BATTERY
)


REQUIREMENTS = ['bluepy==1.3.0']

_LOGGER = logging.getLogger(__name__)

CONF_ADAPTER = 'adapter'
CONF_TIMEOUT = 'timeout'
CONF_ESPRUINO_PATH = 'path_to_espruino'

DEFAULT_ADAPTER = 'hci0'
DEFAULT_FORCE_UPDATE = False
DEFAULT_NAME = 'Puck.js'
DEFAULT_TIMEOUT = 10
DEFAULT_ESPRUINO_PATH = '/usr/bin/espruino'

DOMAIN = "puckjs"

PUCKJS_SOURCE_CODE = os.path.join(os.path.dirname(__file__), "ha-puck.js")


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_MAC): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_FORCE_UPDATE, default=DEFAULT_FORCE_UPDATE): cv.boolean,
    vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
    vol.Optional(CONF_ADAPTER, default=DEFAULT_ADAPTER): cv.string,
    vol.Optional(CONF_ESPRUINO_PATH, default=DEFAULT_ESPRUINO_PATH): cv.string,
})


def program_puckjs(espruino_path, mac):
    _LOGGER.info('Programming Puck.js with mac: %s.', mac)
    try:
        output = subprocess.check_output([espruino_path, '-p', mac, PUCKJS_SOURCE_CODE],
                                         stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as err:
        _LOGGER.warning("Running '{}'".format(err.cmd) + "\ngave the following error:\n" + str(err.output.decode()))

              
def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Puck.js sensor."""
    
    mac = config.get(CONF_MAC).lower()
    adapter = config.get(CONF_ADAPTER)
    name = config.get(CONF_NAME)
    force_update = config.get(CONF_FORCE_UPDATE)
    timeout = config.get(CONF_TIMEOUT)
    espruino_path = config.get(CONF_ESPRUINO_PATH)
    _LOGGER.info('Setting up Puck.js with mac: %s.', mac)

    entities = []
    for device in [("button", None), ("direction", None), ("temperature", DEVICE_CLASS_TEMPERATURE),("battery", DEVICE_CLASS_BATTERY)]:
        entities.append(PuckJsBtSensor(mac, device[1], name + "_" + device[0], force_update))

    _LOGGER.info(entities)
    from bluepy.btle import Scanner, DefaultDelegate
    from threading import Thread, Lock

    lock = Lock()

    class ScannerThread(Thread, Scanner):
        def __init__(self, index, timeout, espruino_path):
            Scanner.__init__(self, index)
            Thread.__init__(self)
            self.index = index
            self.timeout = timeout
            self.espruino_path = espruino_path
            
        def thread_start(self):
            Thread.start(self)

        def scanner_start(self):
            Scanner.start(self)

        def run(self):
            Scanner.clear(self)
            Scanner.start(self)
            while True:
                with lock:
                    self.delegate.found_devices = False
                    _LOGGER.debug('Puck.js start scanning....')
                    Scanner.process(self, self.timeout)
                    _LOGGER.info('Puck.js scanning....')
                    if not self.delegate.found_devices:
                        _LOGGER.info('Puck.js Nothing found, try restarting scanner...')
                        try:
                            Scanner.stop(self)
                        except:
                            pass
                        Scanner.clear(self)
                        Scanner.start(self)
                    elif ((self.delegate.last_found_dev_but_not_manufacturer_data_time-
                           self.delegate.last_found_manufacturer_data_time).total_seconds() > 60):
                        # Device found but over 60 seconds since any manufacturer data received
                        # This could mean that the puck does not have a loaded program so try to update this
                        _LOGGER.info('Puck.js no manufacturer data found for a long time. Trying to reprogram!')
                        try:
                            Scanner.stop(self)
                        except:
                            pass
                        for device in self.delegate.devices:
                            program_puckjs(self.espruino_path, device)
                        # Reset last_found_manufacturer_data_time to force waiting until another programming attempt is done
                        self.delegate.last_found_manufacturer_data_time = datetime.datetime.now() 
                        Scanner.clear(self)
                        Scanner.start(self)
                    
            Scanner.stop(self)
        
    # Gets the actual scanning data
    class ScanDelegate(DefaultDelegate):
        def __init__(self, devices, entities):
            self.devices = devices
            self.entities = entities
            self.lastAdvertising = {}
            self.found_devices = False
            self.last_found_manufacturer_data_time = datetime.datetime.min
            self.last_found_dev_but_not_manufacturer_data_time = datetime.datetime.min
            DefaultDelegate.__init__(self)
    
        def handleDiscovery(self, dev, isNewDev, isNewData):
            self.found_devices = True
            found_manufacturer_data = False
            if not dev.addr in self.devices: return
            #_LOGGER.info('Discovered: %s.', dev.addr)
            for (adtype, desc, value) in dev.getScanData():
                if adtype==255 and value[:4]=="9005": # Manufacturer Data
                    found_manufacturer_data = True
                    data = bytearray.fromhex(value[4:]).decode()
                    if not dev.addr in self.lastAdvertising or self.lastAdvertising[dev.addr] != data:
                        _LOGGER.info('Data changed: %s.', data)
                        for entity in self.entities:
                            if "temperature" in entity._name:
                                entity._state = float(data[-6:-1])
                            elif "battery" in entity._name:
                                entity._state = float(data[-9:-6])
                            elif "direction" in entity._name:
                                entity._state = "normal" if (int(data[-1]) & 2) == 0 else "flipped" 
                            elif "button" in entity._name:
                                entity._state = "off" if (int(data[-1]) & 1) == 0 else "on"
                            if entity.hass:
                                entity.schedule_update_ha_state(False)
                    self.lastAdvertising[dev.addr] = data

                if found_manufacturer_data:
                    self.last_found_manufacturer_data_time = datetime.datetime.now()
                    #_LOGGER.info('Found dev at time : %s', self.last_found_manufacturer_data_time.isoformat())
                else:
                    self.last_found_dev_but_not_manufacturer_data_time = datetime.datetime.now()
                    #_LOGGER.info('Found no dev at time : %s', self.last_found_dev_but_not_manufacturer_data_time.isoformat())
                
    scanner = ScannerThread(adapter[-1], timeout, espruino_path).withDelegate(ScanDelegate([mac], entities))
    add_entities(entities)
    for entity in entities:
        entity.set_scanner(scanner)
    scanner.thread_start()

    def handle_program_puckjs(call):
        """Handle the service call."""
        with lock:
            scanner.stop()
            scanner.clear()
            program_puckjs(espruino_path, mac)              
            scanner.scanner_start()

    hass.services.register(DOMAIN, "program", handle_program_puckjs)
              
    
class PuckJsBtSensor(Entity):
    """Implementing the PuckJsBt sensor."""
    def __init__(self, mac, device_class, name, force_update):
        """Initialize the sensor."""
        self._name = name
        self._state = None
        self._force_update = force_update
        self._found_devices = False
        self._device_class = device_class
        

    def set_scanner(self, scanner):
        self._scanner = scanner

    @property
    def scanner(self):
        """Return the scanner."""
        return self._scanner

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
        """Return the units of measurement."""
        if self._device_class == DEVICE_CLASS_TEMPERATURE:
            return '°C'
        elif self._device_class == DEVICE_CLASS_BATTERY:
            return '%'
        else:
            return None

    @property
    def device_class(self):
        """Device class of this entity."""
        return self._device_class

    @property
    def force_update(self):
        """Force update."""
        return self._force_update

    @property
    def should_poll(self):
        """No polling needed."""
        return False
    

            
