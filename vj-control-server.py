#!/usr/bin/python
import logging.config
import signal
import sys
import time

from flask import Flask, send_from_directory, jsonify, request
from flask_socketio import SocketIO, emit
from recordclass import recordclass

from vj_serial import SerialPort


## Parameters
GPIO_FAN = 17
GPIO_PARACHUTE = 27
GPIO_WATERSPLASHER = 22

PWM_FREQUENCY = 1000

GPIO_BUTTON_START = 23
GPIO_BUTTON_READY = 24

WATERSPLASHER_DUTY_CYCLE = 0.25
MAX_WATERSPLASHER_DURATION = 10

## REST API URLs
BASE_URL = "/"
FAN_URL = BASE_URL + "fan/"
PARACHUTE_URL = BASE_URL + "parachute/"
WATERSPLASHER_URL = BASE_URL + "watersplasher/"
EVENT_URL = BASE_URL + "events/"
JUMP_STATE_URL = BASE_URL + "jumpState/"

## Serial communication with Arduino
SERIAL_NAME = "/dev/ttyUSB"


EnvState = recordclass(
	'EnvState', ['duty_cycle', 'parachute_state', 'watersplasher_state', 'watersplasher_intensity'])
JumpState = recordclass(
	'JumpState', ['jump_started', 'start_time'])


## Global variables
envState = EnvState(0, False, False, WATERSPLASHER_DUTY_CYCLE)
jumpState = JumpState(False, None)
serial = None
activeWaterStopThread = 0


## Instanciate Flask (Static files and REST API)
app = Flask(__name__)
## Instanciate SocketIO (Websockets, used for events) on top of it
socketio = SocketIO(app)


## Deliver statc files (index.html, css, images and js files)
@app.route(BASE_URL)
def index():
	return send_from_directory('static', 'index.html')

@app.route(BASE_URL + '<path:path>')
def static_proxy(path):
	return send_from_directory('static/', path)

## REST API
@app.route(FAN_URL + '<int:speed>', methods=['PUT', 'GET'])
def set_fan_speed(speed):
	set_fanspeed(speed)

	return jsonify({'error': 0}), 200

@app.route(FAN_URL, methods=['POST'])
def set_fan_speed_post():
	set_fanspeed(request.form['speed'])

	return jsonify({'error': 0}), 200

@app.route(FAN_URL, methods=['GET'])
def get_fan_speed():
	return jsonify({'speed': envState.duty_cycle}), 200

@app.route(PARACHUTE_URL, methods=['GET'])
def get_parachute_state():
	return jsonify({'parachute': envState.parachute_state}), 200

@app.route(WATERSPLASHER_URL, methods=['GET'])
def get_watersplasher_state():
	return jsonify({
		'watersplasher': envState.watersplasher_state,
		'intensity': envState.watersplasher_intensity
	}), 200

@app.route(JUMP_STATE_URL, methods=['GET'])
def get_jump_state():
	return jsonify({'jumpStarted': jumpState.jump_started}), 200

@app.route(EVENT_URL, methods=['POST'])
def broadcast_event():
	if request.json and 'data' in request.json:
		socketio.emit('serverEvent', {'data': request.json['data']}, namespace="/events")
	else:
		socketio.emit('serverEvent', {'data': request.form['data']}, namespace="/events")

	return jsonify({'error': 0}), 201


## Events
# Debug
@socketio.on('connect', namespace='/events')
def client_connected():
	logging.debug('Client connected to events')

# Section of the Jump
@socketio.on('unityReadyEvent', namespace='/events')
def unity_ready(message):
	logging.info("Got unity ready: %s", message)
	emit('raspiUnityReadyEvent', {'data': message}, broadcast=True)

@socketio.on('unityJumpStartedEvent', namespace='/events')
def unity_jump_started(message):
	logging.info("Got jump started: %s", message)
	trigger_start()
	emit('raspiJumpStartedEvent', {'data': message}, broadcast=True)

@socketio.on('unityParachuteOpenEvent', namespace='/events')
def unity_parachute(message):
	logging.info("Got open parachute: %s", message)
	open_parachute()

@socketio.on('unityLandingEvent', namespace='/events')
def unity_landing(message):
	logging.info("Got landing: %s", message)
	emit('raspiLandingEvent', {'data': message}, broadcast=True)

@socketio.on('unityResetLevel', namespace='/events')
def unity_reset(message):
	logging.info("Got Unity Reset: %s", message)
	close_parachute()
	set_fanspeed(0)
	watersplasher_off()
	reset_start_trigger()

# Enivronment control
@socketio.on('unityFanSpeedEvent', namespace='/events')
def unity_fanspeed(message):
	logging.info("Got fanspeed: %s", message)
	set_fanspeed(int(message))

@socketio.on('unityWaterSplasherEvent', namespace='/events')
def unity_watersplasher(message):
	logging.info("Got watersplasher: %s", message)
	if int(message) == 1:
		watersplasher_on()
	else:
		watersplasher_off()

# Config
@socketio.on('initSequence', namespace='/config')
def init_sequnce(_ = None):
	set_fan_speed(16)
	socketio.sleep(5)
	watersplasher_on(5)
	socketio.sleep(5)
	set_fan_speed(0)

@socketio.on('waterSplasherDutyCycle', namespace='/config')
def set_watersplasher_duty_cycle(duty_cycle):
	logging.info("Setting watersplasher to " + duty_cycle)
	envState.watersplasher_intensity = float(duty_cycle)

## Helpers
# Setter for fan speed
def set_fanspeed(speed):
	logging.debug("Setting fanspeed to %s", speed)

	# Set PWM-DutyCycle of pin
	envState.duty_cycle = min(max(speed, 0), 16)
	serial.send_serial_command('F', envState.duty_cycle)

	# TODO Remove when working
	socketio.emit('raspiFanEvent', speed, namespace="/events")

# Setter for parachute state
def open_parachute():
	logging.debug("Open parachute")

	serial.send_serial_command('P', 1)
	envState.parachute_state = True
	socketio.emit('raspiParachuteOpenEvent', None, namespace="/events")

def close_parachute():
	logging.debug("Close parachute")

	serial.send_serial_command('P', 0)
	envState.parachute_state = False
	socketio.emit('raspiParachuteCloseEvent', None, namespace="/events")

# Setter for Watersplasher
def stop_watersplasher_task(threadId, duration):
	global activeWaterStopThread

	socketio.sleep(duration)
	if activeWaterStopThread == threadId:
		watersplasher_off()
		activeWaterStopThread = 0
	else:
		logging.info("Not closing, active one is %s", activeWaterStopThread)

def watersplasher_on(duration = MAX_WATERSPLASHER_DURATION):
	global activeWaterStopThread

	logging.debug("Watersplasher on")

	socketio.start_background_task(watersplasher_task, envState.watersplasher_intensity)

	activeWaterStopThread += 1
	socketio.start_background_task(stop_watersplasher_task, activeWaterStopThread, duration)
	logging.info("Starting stopper thread: %s", activeWaterStopThread)

	socketio.emit('raspiWaterSplasherOnEvent', None, namespace="/events")

def watersplasher_task(duty_cycle = WATERSPLASHER_DUTY_CYCLE):
	if not envState.watersplasher_state:
		envState.watersplasher_state = True
		while envState.watersplasher_state:
			serial.send_serial_command('W', 16)
			socketio.sleep(duty_cycle)
			if duty_cycle < 1:
				serial.send_serial_command('W', 0)
				socketio.sleep(1 - duty_cycle)

		serial.send_serial_command('W', 0)

def watersplasher_off():
	logging.debug("Watersplasher off")

	envState.watersplasher_state = False
	socketio.emit('raspiWaterSplasherOffEvent', None, namespace="/events")

# Setter for start trigger
def trigger_start():
	if not jumpState.jump_started:
		serial.send_serial_command('S', 1)
		jumpState.jump_started = True

def reset_start_trigger():
	serial.send_serial_command('S', 0)
	jumpState.jump_started = False


# RasPi GPIO button callbacks
def ready_button_event_handler(pin):
	socketio.emit('raspiPlayerReadyEvent', 'Player is ready to go', namespace="/events")

def start_button_event_handler(pin):
	socketio.emit('raspiStartJumpEvent', 'Jump Started', namespace="/events")
	socketio.emit('serverEvent', {'data': 'Jump Started'}, namespace="/events")


## Shutdown signal handler
def sigTermHandler(signum, frame):
	raise KeyboardInterrupt('Signal %i receivied!' % signum)


## Main - Start Flask server through SocketIO for websocket support
def main():
	global serial

	# Set locale for Flask
	#locale.setlocale(locale.LC_ALL, '')

	## Initialize logger
	logging.config.fileConfig('log.ini')

	# Init serial port
	serial = SerialPort(SERIAL_NAME)

	# Set debug option if desired
	if "debug" in sys.argv:
		app.debug = True

	jumpState.start_time = time.time()

	try:
		# Set signal handler for Shutdown
		signal.signal(signal.SIGTERM, sigTermHandler)

		# Blocking! - Start Flask server
		socketio.run(app, host='0.0.0.0')
	except KeyboardInterrupt:
		pass
	finally:
		serial.close()

if __name__ == '__main__':
	main()
