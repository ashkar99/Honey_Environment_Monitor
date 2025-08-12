import machine
import dht
import _thread
import network
import uasyncio as asyncio
import ujson
import time
from mqtt_as import MQTTClient, config

# ======== CONFIG ========
wifi_ssid = 'iotlab'  #Change to your own wifi
wifi_password = 'modermodemet'

config['server'] = 'test.mosquitto.org'
config['port'] = 1883
config['user'] = ''
config['password'] = ''
config['ssid'] = wifi_ssid
config['wifi_pw'] = wifi_password
config['queue_len'] = 10

# Thresholds
TEMP_OK_MIN = 18
TEMP_OK_MAX = 25
RH_ALERT = 70
LID_MAX_OPEN_SEC = 60
LID_OPEN_CONFIRM_MS = 600
LID_CLOSE_CONFIRM_MS = 400
TILT_ACTIVE_LOW = False

# Pins
PIN_DHT = 15
PIN_TILT = 14
PIN_BUZZ = 6

# ======== HARDWARE ========
sensor = dht.DHT11(machine.Pin(PIN_DHT, machine.Pin.IN, machine.Pin.PULL_UP))
tilt = machine.Pin(PIN_TILT, machine.Pin.IN, machine.Pin.PULL_UP)
buzz = machine.Pin(PIN_BUZZ, machine.Pin.OUT, value=0)

# ======== GLOBAL STATE ========
lock = _thread.allocate_lock()
temperature_c = None
humidity = None
tilt_state = 0
lid_open_in_secs = 0
lid_state = 0
lid_open_started = None

# ======== BUZZER ========
def _beep(ms_on, ms_off, times):
    for _ in range(times):
        buzz.value(1)
        time.sleep_ms(ms_on)
        buzz.value(0)
        time.sleep_ms(ms_off)

def buzzer_event(kind):
    if kind == "cold":
        _beep(120, 120, 2)
    elif kind == "warm":
        _beep(400, 150, 1)
    elif kind == "lid":
        _beep(90, 90, 3)
    elif kind == "humid":
        _beep(250, 150, 2)

# ======== CORE 1: SENSOR LOOP ========
def core1():
    global temperature_c, humidity, tilt_state, lid_open_in_secs, lid_state, lid_open_started

    while True:
        # Read DHT
        try:
            sensor.measure()
            with lock:
                temperature_c = sensor.temperature()
                humidity = sensor.humidity()
            print(f"[Core1] Temp={temperature_c}C Hum={humidity}%")
        except Exception as e:
            print("[Core1] DHT Error:", e)

        # Read Tilt (lid)
        ones = 0
        for _ in range(8):
            ones += tilt.value()
            time.sleep_ms(5)
        avg = ones / 8
        active = (avg < 0.5) if TILT_ACTIVE_LOW else (avg > 0.5)
        with lock:
            tilt_state = 1 if active else 0

        now = time.ticks_ms()
        if tilt_state == 1 and lid_state == 0:
            if lid_open_started is None:
                lid_open_started = time.time()
            lid_state = 1
            print("[Core1] Lid Opened")
        elif tilt_state == 0 and lid_state == 1:
            lid_state = 0
            if lid_open_started:
                dur = int(time.time() - lid_open_started)
                print(f"[Core1] Lid Closed after {dur}s")
                lid_open_started = None

        with lock:
            lid_open_in_secs = int(time.time() - lid_open_started) if lid_open_started else 0

        # Buzzer events
        if temperature_c is not None and temperature_c < TEMP_OK_MIN:
            buzzer_event("cold")
        if temperature_c is not None and temperature_c > TEMP_OK_MAX:
            buzzer_event("warm")
        if lid_open_in_secs > 0:
            buzzer_event("lid")
        if humidity is not None and humidity > RH_ALERT:
            buzzer_event("humid")

        time.sleep(1)

# ======== WIFI & MQTT ========
async def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(wifi_ssid, wifi_password)
    max_wait = 10
    while max_wait > 0:
        if wlan.status() < 0 or wlan.status() >= 3:
            break
        max_wait -= 1
        print('waiting for connection...')
        await asyncio.sleep(1)
    if wlan.status() != 3:
        raise RuntimeError('network connection failed')
    else:
        print('connected, IP:', wlan.ifconfig()[0])

async def mqtt_connect():
    try:
        await client.connect()
        print('MQTT connected')
    except Exception as e:
        print('MQTT connection failed:', str(e))
        raise

# ======== MQTT PUBLISH LOOP ========
async def mqtt_publish():
    while True:
        with lock:
            # Make a safe copy of current sensor values
            temp = temperature_c
            hum = humidity
            tilt_val = tilt_state
            lid_secs = lid_open_in_secs

        # Default status
        status_val = "ok"

        # Determine status
        if temp is not None and hum is not None and hum > RH_ALERT:
            status_val = "alert"
        elif temp is not None and (temp < TEMP_OK_MIN or temp > TEMP_OK_MAX):
            status_val = "warn"
        if lid_secs > LID_MAX_OPEN_SEC:
            status_val = "alert"

        # Publish each value to its own topic (convert to string)
        await client.publish("honeybox/temp", str(temp), qos=1)
        await client.publish("honeybox/humidity", str(hum), qos=1)
        await client.publish("honeybox/tilt", str(tilt_val), qos=1)
        await client.publish("honeybox/lid_open_in_secs", str(lid_secs), qos=1)
        await client.publish("honeybox/status", status_val, qos=1)

        print("[Core0] Published:", {
            "temp": temp,
            "humidity": hum,
            "tilt": tilt_val,
            "lid_open_secs": lid_secs,
            "status": status_val
        })

        await asyncio.sleep(5)  # publish every 5s


# ======== MAIN ========
client = MQTTClient(config)

async def main():
    await wifi_connect()
    await mqtt_connect()
    _thread.start_new_thread(core1, ())
    await mqtt_publish()

asyncio.run(main())


