var button_state = 0;
var magDiff = 0;
var upside_down = 0;
var push_timer = 0;
var hold_timeout = 0;

var battery = Puck.getBatteryPercentage();
var temp = E.getTemperature();

function pad(s,size) {
    while (s.length < (size || 2)) {s = "0" + s;}
    return s;
}

function advertise(button_state, upside_down){
  puck_data = (button_state & 0x1) | (upside_down & 0x1) << 1;
  battery = (battery + Puck.getBatteryPercentage())/2;
  temp = (temp + E.getTemperature())/2;
  data = pad(battery.toFixed(0), 3) + pad(temp.toFixed(2),4) + puck_data.toString();
  NRF.setAdvertising({},{manufacturer: 0x0590, manufacturerData:data});
  console.log(data);
}

var led_on = false;
function calibrationBlink(){
  led_on = !led_on;
  LED.write(led_on);
}

var calibration_mode = false;
var mag_normal;
var mag_upside_down;
var mag_z_upside_down_boundary = 300;
var mag_z_upside_down_hysteresis = 100;
function buttonPushedForTwoSeconds(){
  hold_timeout = 0;  
  if (calibration_mode){
    // We are already in calibration mode so step out from it
    // Calculate upside down boundary value
    mag_z_upside_down_boundary = (mag_normal.z + mag_upside_down.z)/2;
    mag_z_upside_down_hysteresis = (mag_normal.z - mag_z_upside_down_boundary)/3;
    console.log("New boundary value: " + mag_z_upside_down_boundary);
    console.log("New hysteresis value: " + mag_z_upside_down_hysteresis);
    clearInterval(blink_interval);
    calibration_mode = false;  
    LED.write(false);
    console.log("Normal mode");
  } else {
    // We have reached the timeout so this means we
    // are in calibration mode
    blink_interval = setInterval(calibrationBlink, 1000);
    calibration_mode = true;
    mag_normal = Puck.mag();
    console.log("Calibration mode");    
  } 
  
}

setWatch(function() {
  hold_timeout = setTimeout(buttonPushedForTwoSeconds, 2000); 
}, BTN, {edge:"rising", repeat:1, debounce:20});


setWatch(function() {
  if (hold_timeout){
    // We haven't reached the timeout so clear it
    clearTimeout(hold_timeout);
    if (calibration_mode){
      // We are calibratating - record state for upside down
      mag_upside_down = Puck.mag();
      console.log("Recording upside down state: " + mag_upside_down.z);
    } else {
      // This is a normal button push
      button_state ^= 1;
      advertise(button_state, upside_down);
    }
  }  
}, BTN, {edge:"falling", repeat:1, debounce:20});


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
  console.log(avr);
  if (!upside_down && avr.z < (mag_z_upside_down_boundary - mag_z_upside_down_hysteresis)){
    upside_down = 1;    
    advertise(button_state, upside_down);
  } else if (upside_down && avr.z >= (mag_z_upside_down_boundary + mag_z_upside_down_hysteresis)){
    upside_down = 0;
    advertise(button_state, upside_down);
  }
  //console.log(xyz);
  LED.write(magDiff > 50);
});
Puck.magOn();

setInterval(function() { advertise(button_state, upside_down); }, 10000);