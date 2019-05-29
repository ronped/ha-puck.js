var button_state = 0;
var magDiff = 0;
var upside_down = 0;
var temp_calibration = 3.2;

var battery = Puck.getBatteryPercentage();
var temp = E.getTemperature() + temp_calibration;

function pad(s,size) {
    while (s.length < (size || 2)) {s = "0" + s;}
    return s;
}

function advertise(button_state, upside_down){
  puck_data = (button_state & 0x1) | (upside_down & 0x1) << 1;
  battery = (battery + Puck.getBatteryPercentage())/2;
  temp = (temp + E.getTemperature() + temp_calibration)/2;
  data = pad(battery.toFixed(0), 3) + pad(temp.toFixed(2),4) + puck_data.toString();
  NRF.setAdvertising({},{manufacturer: 0x0590, manufacturerData:data});
  console.log(data);
}

setWatch(function() {
  button_state ^= 1;
  advertise(button_state, upside_down);
}, BTN, {edge:"rising", repeat:1, debounce:20});

var avr = Puck.mag();
Puck.on('mag', function(xyz) {
  // work out difference in field
  var dx = xyz.x-avr.x;
  var dy = xyz.y-avr.y;
  var dz = xyz.z-avr.z;
  magDiff = Math.sqrt(dx*dx+dy*dy+dz*dz);
  // update average
  avr.x += dx/2;
  avr.y += dy/2;
  avr.z += dz/2;  
  //console.log(magDiff);
  //console.log(dx + " " + dy + " " + dz);
  if (!upside_down && avr.z < -700){
    upside_down = 1;    
    advertise(button_state, upside_down);
  } else if (upside_down && avr.z >= -700){
    upside_down = 0;
    advertise(button_state, upside_down);
  }
  //console.log(xyz);
  LED.write(magDiff > 50);
});
Puck.magOn();

setInterval(function() { advertise(button_state, upside_down); }, 10000);