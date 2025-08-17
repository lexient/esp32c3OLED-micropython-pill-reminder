import os
from machine import Pin, I2C
from ssd1306 import SSD1306_I2C

import time
import network

try:
    import urequests as requests
except ImportError:
    import requests
try:
    import ujson as json
except ImportError:
    import json
try:
    import ntptime
except ImportError:
    ntptime = None


from secrets import WIFI_SSID, WIFI_PASSWORD, SUPABASE_URL, SUPABASE_KEY


# Physical buffer (controller) dimensions
BUFFER_WIDTH = 128
BUFFER_HEIGHT = 64

# Visible area of the 0.42" OLED panel
DISPLAY_WIDTH = 72
DISPLAY_HEIGHT = 40

FONT_WIDTH = 8
FONT_HEIGHT = 8
TEXT_Y_ADJUST = 2

# I2C configuration
I2C_SCL_PIN = 6
I2C_SDA_PIN = 5
I2C_FREQ_HZ = 400000

# Center the 72x40 window within the 128x64 buffer
X_OFFSET = (BUFFER_WIDTH - DISPLAY_WIDTH) // 2

# If your 0.42" module is 40px-tall but in the 64px buffer,
# apply a vertical adjustment so the box appears centered.
PANEL_Y_ADJUST = 12
Y_OFFSET = (BUFFER_HEIGHT - DISPLAY_HEIGHT) // 2 + PANEL_Y_ADJUST

i2c = I2C(0, scl=Pin(I2C_SCL_PIN), sda=Pin(I2C_SDA_PIN), freq=I2C_FREQ_HZ)
oled = SSD1306_I2C(BUFFER_WIDTH, BUFFER_HEIGHT, i2c)


# Rotary encoder + switch pins
ENC_A_PIN = 1
ENC_B_PIN = 2
SW_PIN = 0
BUZZER_PIN = 10
LED_PIN = 8

enc_a = Pin(ENC_A_PIN, Pin.IN, Pin.PULL_UP)
enc_b = Pin(ENC_B_PIN, Pin.IN, Pin.PULL_UP)
sw = Pin(SW_PIN, Pin.IN, Pin.PULL_UP)

# Buzzer
buzzer = Pin(BUZZER_PIN, Pin.OUT)
buzzer.value(0)
led = Pin(LED_PIN, Pin.OUT)
led.value(1)

# Encoder state
position = 0
enc_last = (enc_a.value() << 1) | enc_b.value()
enc_changed = False
last_enc_ms = 0
last_reported_detent = 0

# Switch state
switch_state = sw.value()
switch_changed = False
last_sw_ms = 0

# App state
MODE_DISPLAY = 0
MODE_SUBMIT = 1
mode = MODE_DISPLAY

REQUEST_TIMEOUT_S = 2

def http_get(url, headers=None):
    try:
        return requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
    except TypeError:
        return requests.get(url, headers=headers)

def http_post(url, headers=None, data=None):
    try:
        return requests.post(url, headers=headers, data=data, timeout=REQUEST_TIMEOUT_S)
    except TypeError:
        return requests.post(url, headers=headers, data=data)

# Submission config
FRIENDLY_NAME = "dexies"
DRUG_NAME = "dexamphetamine"
DOSE = 5
DOSE_UNIT = "mg"

# Submission state
submit_qty = 0
submit_entry_detent = 0
last_user_input_ms = 0

def ticks_ms():
    return time.ticks_ms()

def ms_since(start_ms):
    return time.ticks_diff(time.ticks_ms(), start_ms)

def _enc_irq(_):
    global enc_last, position, enc_changed, last_enc_ms
    ms = time.ticks_ms()
    if time.ticks_diff(ms, last_enc_ms) < 2:
        return
    last_enc_ms = ms
    state = (enc_a.value() << 1) | enc_b.value()
    transition = (enc_last << 2) | state
    if transition in (0b0001, 0b0111, 0b1110, 0b1000):
        position += 1
        enc_changed = True
    elif transition in (0b0010, 0b0100, 0b1101, 0b1011):
        position -= 1
        enc_changed = True
    enc_last = state

def _sw_irq(_):
    global switch_state, switch_changed, last_sw_ms
    ms = time.ticks_ms()
    if time.ticks_diff(ms, last_sw_ms) < 50:
        return
    last_sw_ms = ms
    switch_state = sw.value()
    switch_changed = True

enc_a.irq(_enc_irq, Pin.IRQ_RISING | Pin.IRQ_FALLING)
enc_b.irq(_enc_irq, Pin.IRQ_RISING | Pin.IRQ_FALLING)
sw.irq(_sw_irq, Pin.IRQ_RISING | Pin.IRQ_FALLING)


def led_on():
    # Active LOW
    led.value(0)


def led_off():
    # Active LOW
    led.value(1)


def flash_led(times=1, on_ms=120, off_ms=120):
    for _ in range(times):
        led_on()
        time.sleep_ms(on_ms)
        led_off()
        time.sleep_ms(off_ms)

def calculate_line_positions(num_lines):
    total_text_height = num_lines * FONT_HEIGHT
    available_space = max(0, DISPLAY_HEIGHT - total_text_height)
    gap = available_space // (num_lines + 1) if num_lines > 0 else 0
    positions = []
    y = Y_OFFSET + gap
    for _ in range(num_lines):
        positions.append(y)
        y += FONT_HEIGHT + gap
    return positions


def x_for_alignment(text, align):
    text_width = len(text) * FONT_WIDTH
    if align == "center":
        return X_OFFSET + max(0, (DISPLAY_WIDTH - text_width) // 2)
    if align == "right":
        return X_OFFSET + max(0, DISPLAY_WIDTH - text_width)
    return X_OFFSET + 2


def draw_box_with_lines(lines, align="center"):
    oled.fill(0)
    oled.rect(X_OFFSET, Y_OFFSET, DISPLAY_WIDTH, DISPLAY_HEIGHT, 1)
    y_positions = calculate_line_positions(len(lines))
    for text, y in zip(lines, y_positions):
        x = x_for_alignment(text, align)
        oled.text(text, x, y + TEXT_Y_ADJUST)
    oled.show()


def show_message(lines, align="center"):
    draw_box_with_lines(lines, align=align)


def _draw_digit_7seg(x, y, w, h, t, digit):
    # Compute segment sizes
    top_y = y
    mid_y = y + h // 2
    bot_y = y + h - t
    inner_w = max(1, w - 2 * t)
    top_h = max(1, (h - 3 * t) // 2)
    bot_h = top_h

    # Segment drawers
    def seg_a():
        oled.fill_rect(x + t, top_y, inner_w, t, 1)
    def seg_g():
        oled.fill_rect(x + t, mid_y - (t // 2), inner_w, t, 1)
    def seg_d():
        oled.fill_rect(x + t, bot_y, inner_w, t, 1)
    def seg_f():
        oled.fill_rect(x, top_y + t, t, top_h, 1)
    def seg_b():
        oled.fill_rect(x + w - t, top_y + t, t, top_h, 1)
    def seg_e():
        oled.fill_rect(x, mid_y + (t // 2), t, bot_h, 1)
    def seg_c():
        oled.fill_rect(x + w - t, mid_y + (t // 2), t, bot_h, 1)

    segments = {
        0: (seg_a, seg_b, seg_c, seg_d, seg_e, seg_f),
        1: (seg_b, seg_c),
        2: (seg_a, seg_b, seg_g, seg_e, seg_d),
        3: (seg_a, seg_b, seg_g, seg_c, seg_d),
        4: (seg_f, seg_g, seg_b, seg_c),
        5: (seg_a, seg_f, seg_g, seg_c, seg_d),
        6: (seg_a, seg_f, seg_g, seg_e, seg_c, seg_d),
        7: (seg_a, seg_b, seg_c),
        8: (seg_a, seg_b, seg_c, seg_d, seg_e, seg_f, seg_g),
        9: (seg_a, seg_b, seg_c, seg_d, seg_f, seg_g),
    }
    for drawer in segments.get(digit, (seg_a, seg_d, seg_g)):
        drawer()


def _draw_big_number_centered(num_str):
    # Compute layout within the 72x40 window
    available_h = DISPLAY_HEIGHT - (FONT_HEIGHT + 4)
    h = min(24, max(16, available_h))
    t = max(2, h // 8)
    w = max(12, (h // 2) + t)
    gap = max(2, t)

    n = len(num_str)
    total_w = n * w + (n - 1) * gap
    if total_w > DISPLAY_WIDTH:
        # crude downscale to fit
        scale = DISPLAY_WIDTH / total_w
        h = max(12, int(h * scale))
        t = max(1, int(t * scale))
        w = max(8, int(w * scale))
        gap = max(1, int(gap * scale))
        total_w = n * w + (n - 1) * gap

    start_x = X_OFFSET + max(0, (DISPLAY_WIDTH - total_w) // 2)
    top_y = Y_OFFSET + FONT_HEIGHT + 4

    x = start_x
    for ch in num_str:
        if '0' <= ch <= '9':
            _draw_digit_7seg(x, top_y, w, h, t, ord(ch) - 48)
            x += w + gap
        else:
            # unsupported char, small spacer
            x += gap


def show_submission(qty):
    oled.fill(0)
    # Border
    oled.rect(X_OFFSET, Y_OFFSET, DISPLAY_WIDTH, DISPLAY_HEIGHT, 1)
    # Title on top line
    title = FRIENDLY_NAME
    tx = x_for_alignment(title, "center")
    ty = Y_OFFSET + TEXT_Y_ADJUST
    oled.text(title, tx, ty)
    # Big number centered below
    _draw_big_number_centered(str(qty))
    oled.show()


def connect_wifi(ssid, password, timeout_s=20):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        start = time.time()
        while not wlan.isconnected() and (time.time() - start) < timeout_s:
            show_message(["WiFi", "Loading"])
            time.sleep(1)
    ok = wlan.isconnected()
    if not ok:
        show_message(["WiFi", "failed"])
        flash_led(3, 80, 80)
        time.sleep(1)
    return ok


def sync_time_via_ntp(retries=5):
    if ntptime is None:
        return False
    for _ in range(retries):
        try:
            ntptime.settime()  # sets RTC to UTC
            return True
        except Exception:
            time.sleep(1)
    return False


def parse_iso8601_to_epoch(iso_str):
    try:
        # Example: 2025-08-14T02:34:56.123456+00:00 or 2025-08-14T02:34:56Z
        y = int(iso_str[0:4])
        mo = int(iso_str[5:7])
        d = int(iso_str[8:10])
        h = int(iso_str[11:13])
        mi = int(iso_str[14:16])
        s = int(iso_str[17:19])

        # Timezone offset
        tz_offset_sec = 0
        # Find last '+' or '-' after the seconds portion to get offset
        if len(iso_str) > 19:
            tail = iso_str[19:]
            plus_idx = tail.rfind('+')
            minus_idx = tail.rfind('-')
            idx = -1
            sign = '+'
            if plus_idx > -1 or minus_idx > -1:
                if plus_idx > minus_idx:
                    idx = plus_idx
                    sign = '+'
                else:
                    idx = minus_idx
                    sign = '-'
                # Adjust index to original string position
                idx = 19 + idx
                if idx > 19:
                    hh = int(iso_str[idx + 1:idx + 3])
                    mm = int(iso_str[idx + 4:idx + 6])
                    tz_offset_sec = (hh * 60 + mm) * 60
                    if sign == '-':
                        tz_offset_sec = -tz_offset_sec

        # mktime expects local time tuple; assume local==UTC on device
        epoch_local = time.mktime((y, mo, d, h, mi, s, 0, 0))
        # Convert to UTC epoch by subtracting the provided offset
        return epoch_local - tz_offset_sec
    except Exception:
        return None


def fetch_last_dose():
    url = "%s/rest/v1/stimulants?select=created_at,qty,drug,dose,dose_unit&order=created_at.desc&limit=1" % SUPABASE_URL
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer %s" % SUPABASE_KEY,
        "Accept": "application/json",
    }
    r = None
    try:
        r = http_get(url, headers=headers)
        if r.status_code >= 200 and r.status_code < 300:
            data = json.loads(r.text)
            if isinstance(data, list) and len(data) > 0:
                item = data[0]
                created_at = item.get("created_at")
                qty = item.get("qty")
                drug = item.get("drug")
                dose = item.get("dose")
                dose_unit = item.get("dose_unit")
                epoch = parse_iso8601_to_epoch(created_at) if created_at else None
                return {
                    "epoch": epoch,
                    "qty": qty,
                    "drug": drug,
                    "dose": dose,
                    "dose_unit": dose_unit,
                }
        # Error fetching
        flash_led(1, 150, 150)
        return None
    except Exception:
        flash_led(1, 150, 150)
        return None
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def submit_dose(qty):
    url = "%s/rest/v1/stimulants" % SUPABASE_URL
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer %s" % SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {
        "qty": int(qty),
        "drug": DRUG_NAME,
        "dose": DOSE,
        "dose_unit": DOSE_UNIT,
    }
    r = None
    try:
        r = http_post(url, headers=headers, data=json.dumps(payload))
        return 200 <= r.status_code < 300
    except Exception:
        return False
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def format_ago(seconds):
    if seconds is None or seconds < 0:
        return "unknown"
    minutes = int(seconds // 60)
    hours = minutes // 60
    minutes = minutes % 60
    days = hours // 24
    hours = hours % 24
    if days > 0:
        return "%dd %02dh" % (days, hours)
    if hours > 0:
        return "%dh %02dm" % (hours, minutes)
    return "%dm" % minutes


def render_last_dose(info):
    if not info or not info.get("epoch"):
        show_message(["Recent", "Missing"])
        return
    now = time.time()
    ago = format_ago(now - info["epoch"])
    qty = info.get("qty")
    drug = info.get("drug") or ""
    dose = info.get("dose")
    dose_unit = info.get("dose_unit") or ""

    # Build compact lines for the 72x40 area (max ~9 chars each)
    dose_str = ""
    if dose is not None and dose_unit:
        # Keep concise; e.g., "5mg"
        try:
            dose_val = int(dose) if int(dose) == dose else dose
            dose_str = "%s%s" % (dose_val, dose_unit)
        except Exception:
            dose_str = "%s%s" % (dose, dose_unit)

    qty_str = ("x%s" % qty) if qty is not None else ""

    line1 = "Last dose"
    line2 = ago
    line3 = (drug[:9]) if drug else ""
    line4 = (dose_str + (" " if dose_str and qty_str else "") + qty_str)[:9]

    lines = [l for l in [line2, line4] if l]
    show_message(lines)


def enter_submission_mode():
    global mode, submit_qty, submit_entry_detent, last_user_input_ms
    mode = MODE_SUBMIT
    led_on()
    det = position // 4 if position >= 0 else -((-position) // 4)
    submit_entry_detent = det
    submit_qty = 0
    last_user_input_ms = ticks_ms()
    show_submission(submit_qty)


def exit_submission_mode():
    global mode
    mode = MODE_DISPLAY
    led_off()


def main():
    show_message(["Booting"])

    if not connect_wifi(WIFI_SSID, WIFI_PASSWORD):
        # Stay in display mode with error shown briefly, then continue offline
        time.sleep(2)

    # Try NTP sync (optional but recommended for correct "ago")
    if not sync_time_via_ntp():
        # Proceed even if NTP fails; server time can still be shown as "unknown"
        pass

    last_info = fetch_last_dose()
    render_last_dose(last_info)

    last_fetch_ts = time.time()
    FETCH_INTERVAL_S = 5

    while True:
        global enc_changed, position, last_reported_detent, switch_changed, switch_state, mode, submit_qty, submit_entry_detent, last_user_input_ms
        now = time.time()

        # Periodically refresh from server while in display mode
        if mode == MODE_DISPLAY:
            if now - last_fetch_ts >= FETCH_INTERVAL_S:
                info = fetch_last_dose()
                if info:
                    last_info = info
                last_fetch_ts = now
            render_last_dose(last_info)

        # Polling interval ~50ms
        for _ in range(20):
            # Handle encoder changes
            if enc_changed:
                enc_changed = False
                det = position // 4 if position >= 0 else -((-position) // 4)
                if mode == MODE_DISPLAY:
                    # Any turn enters submission mode
                    enter_submission_mode()
                    last_reported_detent = det
                elif mode == MODE_SUBMIT:
                    last_user_input_ms = ticks_ms()
                    # qty based on delta from entry detent
                    submit_qty = det - submit_entry_detent
                    if submit_qty < 0:
                        submit_qty = 0
                    show_submission(submit_qty)
                last_reported_detent = det

            # Handle switch changes
            if switch_changed:
                switch_changed = False
                # press to enter submission mode from display
                if mode == MODE_DISPLAY and switch_state == 0:
                    enter_submission_mode()
                elif mode == MODE_SUBMIT:
                    # treat as user activity only
                    last_user_input_ms = ticks_ms()

            # Submission mode logic
            if mode == MODE_SUBMIT:
                # Inactivity handling
                if ms_since(last_user_input_ms) >= 5000:
                    if submit_qty <= 0:
                        # Timeout without qty => exit
                        exit_submission_mode()
                    else:
                        # Warn for 3 seconds, flashing every 0.5s, cancel if input occurs
                        canceled = False
                        for _warn_step in range(6):
                            # half-second window broken into 5x100ms to check input
                            # Toggle active-low LED
                            led.value(1 - led.value())
                            step_start = ticks_ms()
                            while ms_since(step_start) < 500:
                                if enc_changed or switch_changed:
                                    canceled = True
                                    break
                                time.sleep_ms(100)
                            if canceled:
                                break
                        # restore steady ON if canceled
                        if canceled:
                            led_on()
                            last_user_input_ms = ticks_ms()
                            # clear any pending change flags
                            if enc_changed:
                                enc_changed = False
                            if switch_changed:
                                switch_changed = False
                        else:
                            # Attempt submission with retries
                            attempt = 0
                            success = False
                            while attempt < 3 and not success:
                                attempt += 1
                                if attempt < 3:
                                    show_message(["retrying", "%d/3..." % attempt])
                                else:
                                    show_message(["retrying", "3/3..."])
                                success = submit_dose(submit_qty)
                                if not success:
                                    flash_led(1, 150, 150)
                                    time.sleep(0.3)
                            if success:
                                show_message(["success"]) 
                                flash_led(3, 60, 60)
                                exit_submission_mode()
                                # Refresh last dose quickly
                                info = fetch_last_dose()
                                if info:
                                    last_info = info
                                render_last_dose(last_info)
                            else:
                                show_message(["fail"])
                                flash_led(2, 200, 200)
                                time.sleep(1.0)
                                exit_submission_mode()
                                render_last_dose(last_info)

                # keep submission UI visible if active
                # show_submission called on qty change

            time.sleep(0.05)


if __name__ == "__main__":
    main()
