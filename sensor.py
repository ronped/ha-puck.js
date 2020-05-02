"""
Support for puck.js as a sensor through the use of BLE advertising
"""
import logging

import voluptuous as vol

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

DEFAULT_ADAPTER = 'hci0'
DEFAULT_FORCE_UPDATE = False
DEFAULT_NAME = 'Puck.js'
DEFAULT_TIMEOUT = 10


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_MAC): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_FORCE_UPDATE, default=DEFAULT_FORCE_UPDATE): cv.boolean,
    vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
    vol.Optional(CONF_ADAPTER, default=DEFAULT_ADAPTER): cv.string,
})


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Puck.js sensor."""
    
    mac = config.get(CONF_MAC).lower()
    adapter = config.get(CONF_ADAPTER)
    name = config.get(CONF_NAME)
    force_update = config.get(CONF_FORCE_UPDATE)
    timeout = config.get(CONF_TIMEOUT)
    _LOGGER.info('Setting up Puck.js with mac: %s.', mac)

    entities = []
    for device in [("button", None), ("direction", None), ("temperature", DEVICE_CLASS_TEMPERATURE),("battery", DEVICE_CLASS_BATTERY)]:
        entities.append(PuckJsBtSensor(mac, device[1], name + "_" + device[0], force_update))

    _LOGGER.info(entities)
    from bluepy.btle import Scanner, DefaultDelegate
    from threading import Thread

    class ScannerThread(Thread, Scanner):
        def __init__(self, index, timeout):
            Scanner.__init__(self, index)
            Thread.__init__(self)
            self.index = index
            self.timeout = timeout
            
        def thread_start(self):
            Thread.start(self)

        def run(self):
            Scanner.clear(self)
            Scanner.start(self)
            while True:
                self.delegate.found_devices = False
                Scanner.process(self, self.timeout)
                #_LOGGER.info('Puck.js scanning....')
                if not self.delegate.found_devices:
                    _LOGGER.info('Puck.js Nothing found, try restarting scanner...')
                    try:
                        Scanner.stop(self)
                    except:
                        pass
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
            DefaultDelegate.__init__(self)
    
        def handleDiscovery(self, dev, isNewDev, isNewData):
            self.found_devices = True
            if not dev.addr in self.devices: return
            #_LOGGER.info('Discovered: %s.', dev.addr)
            for (adtype, desc, value) in dev.getScanData():
                if adtype==255 and value[:4]=="9005": # Manufacturer Data
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
    
    scanner = ScannerThread(adapter[-1], timeout).withDelegate(ScanDelegate([mac], entities))
    add_entities(entities)
    scanner.thread_start()


    
class PuckJsBtSensor(Entity):
    """Implementing the PuckJsBt sensor."""
    def __init__(self, mac, device_class, name, force_update):
        """Initialize the sensor."""
        self._name = name
        self._state = None
        self._force_update = force_update
        self._found_devices = False
        self._device_class = device_class
        

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
    

            
