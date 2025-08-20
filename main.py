import os
from machine import Pin, I2C
from ssd1306 import SSD1306_I2C

import time
import network
import gc

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
MODE_WARNING = 2
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

# Settings (file-based)
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "dose_interval_min": 180,          # 3 hours
    "dose_window_start_hour": 8,       # 08:00
    "dose_window_end_hour": 16,        # 16:00
    "max_doses_per_day": 7,
    "tz_offset_min": 0,                # minutes offset from UTC (e.g. 600 for AEST, -420 for PDT)
}
settings = None

def load_settings():
    global settings
    try:
        with open(SETTINGS_FILE, "r") as f:
            raw = json.loads(f.read())
        settings = {k: int(raw.get(k, DEFAULT_SETTINGS[k])) for k in DEFAULT_SETTINGS}
    except Exception:
        settings = DEFAULT_SETTINGS.copy()
        try:
            with open(SETTINGS_FILE, "w") as f:
                f.write(json.dumps(settings))
        except Exception:
            pass

def get_setting(key):
    cfg = settings if settings else DEFAULT_SETTINGS
    try:
        return int(cfg.get(key, DEFAULT_SETTINGS[key]))
    except Exception:
        return DEFAULT_SETTINGS[key]

# Submission config
FRIENDLY_NAME = "dexies"
DRUG_NAME = "dexamphetamine"
DOSE = 5
DOSE_UNIT = "mg"

# Submission state
submit_qty = 0
submit_entry_detent = 0
last_user_input_ms = 0

# View system
VIEW_STATUS = "status"
VIEW_NEXT_DOSE = "next_dose"
VIEW_LAST_DOSE = "last_dose"
VIEW_DAILY_TOTAL = "daily_total"
VIEW_ERROR = "error"
VIEW_WARNING = "warning"

current_view = VIEW_STATUS
default_view = VIEW_STATUS
last_view_interaction_ms = 0
post_submit_display_until_s = 0

# Error/Warning state
error_type = None  # "no wifi" | "db fail"
warning_reason = None  # "limit" | "too late" | "too soon"
warning_end_ms = 0
last_warning_flash_ms = 0
last_warning_render_ms = 0

# Alarm state (beep + LED flash when zero-consumed within window)
alarm_active = False
alarm_buzzer_on = False
last_alarm_toggle_ms = 0
alarm_grace_until_s = 0

def ticks_ms():
    return time.ticks_ms()

def ms_since(start_ms):
    return time.ticks_diff(time.ticks_ms(), start_ms)


def log_mem(label=""):
    try:
        free_b = gc.mem_free()
        alloc_b = gc.mem_alloc()
        if label:
            print("mem", label, "free=", free_b, "alloc=", alloc_b)
        else:
            print("mem", "free=", free_b, "alloc=", alloc_b)
    except Exception:
        pass

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

    # Segment coordinates
    segs = {
        'a': (x + t, top_y, inner_w, t),
        'b': (x + w - t, top_y + t, t, top_h),
        'c': (x + w - t, mid_y + (t // 2), t, bot_h),
        'd': (x + t, bot_y, inner_w, t),
        'e': (x, mid_y + (t // 2), t, bot_h),
        'f': (x, top_y + t, t, top_h),
        'g': (x + t, mid_y - (t // 2), inner_w, t),
    }

    digit_segments = {
        0: 'abcdef',
        1: 'bc',
        2: 'abged',
        3: 'abgcd',
        4: 'fgbc',
        5: 'afgcd',
        6: 'afgecd',
        7: 'abc',
        8: 'abcdefg',
        9: 'abcdfg',
    }
    
    for seg_name in digit_segments.get(digit, 'adg'):
        oled.fill_rect(*segs[seg_name], 1)


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
    # Title on top line
    title = FRIENDLY_NAME
    tx = x_for_alignment(title, "center")
    ty = Y_OFFSET + TEXT_Y_ADJUST
    oled.text(title, tx, ty)
    # Big number centered below
    _draw_big_number_centered(str(qty))
    oled.show()


def _draw_big_time_centered(hours, minutes, lines=4):
    # Draw big HH and MM with small 'h' and 'm' centered within the 72x40 window.
    # This function only draws digits and labels; caller is responsible for clearing, border, and show.
    if lines <= 2:
        h = 16
    else:
        h = 24
    t = max(2, h // 8)
    w = max(10, (h // 2) + t)
    gap = max(2, t)

    h_str = str(int(hours))
    m_str = "%02d" % int(minutes)
    num_digits = len(h_str) + len(m_str)
    total_w = num_digits * w + (num_digits - 1) * gap + (2 * gap)
    start_x = X_OFFSET + max(0, (DISPLAY_WIDTH - total_w) // 2)
    top_y = Y_OFFSET + max(0, (DISPLAY_HEIGHT - h) // 2)

    x = start_x
    for ch in h_str:
        if '0' <= ch <= '9':
            _draw_digit_7seg(x, top_y, w, h, t, ord(ch) - 48)
        x += w + gap
    oled.text('h', x - gap // 2, top_y - 1 + TEXT_Y_ADJUST)
    x += gap
    for ch in m_str:
        if '0' <= ch <= '9':
            _draw_digit_7seg(x, top_y, w, h, t, ord(ch) - 48)
        x += w + gap
    oled.text('m', x - gap, top_y - 1 + TEXT_Y_ADJUST)


def _draw_big_minutes_centered(total_minutes, label="mins"):
    # Draw a slightly smaller big minute count centered, with a label below
    try:
        total_minutes = int(total_minutes)
    except Exception:
        total_minutes = 0
    num_str = str(max(0, total_minutes))

    # Slightly smaller than previous big time (h=24) → use 20
    h = 20
    t = max(2, h // 8)
    w = max(10, (h // 2) + t)
    gap = max(2, t)

    n = len(num_str)
    total_w = n * w + (n - 1) * gap
    if total_w > DISPLAY_WIDTH:
        scale = DISPLAY_WIDTH / total_w
        h = max(14, int(h * scale))
        t = max(2, int(t * scale))
        w = max(8, int(w * scale))
        gap = max(1, int(gap * scale))
        total_w = n * w + (n - 1) * gap

    start_x = X_OFFSET + max(0, (DISPLAY_WIDTH - total_w) // 2)
    # Leave room for the label under the digits
    label_h = FONT_HEIGHT
    label_gap = 2
    top_y = Y_OFFSET + max(0, (DISPLAY_HEIGHT - (h + label_h + label_gap)) // 2)

    x = start_x
    for ch in num_str:
        if '0' <= ch <= '9':
            _draw_digit_7seg(x, top_y, w, h, t, ord(ch) - 48)
        x += w + gap

    # Label centered below
    lx = x_for_alignment(label, "center")
    ly = top_y + h + label_gap
    oled.text(label, lx, ly + TEXT_Y_ADJUST)


def format_duration(seconds, show_days=False):
    if seconds is None or seconds < 0:
        return "unknown"
    minutes = int(seconds // 60)
    hours = minutes // 60
    minutes = minutes % 60
    days = hours // 24
    hours = hours % 24
    
    if show_days and days > 0:
        return "%dd %02dh" % (days, hours)
    if hours > 0:
        return "%dh %02dm" % (hours, minutes)
    return "%dm" % minutes


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
        gc.collect()
        log_mem("fetch_today_summary:after_collect")
        gc.collect()
        log_mem("fetch_last_dose:after_collect")
    gc.collect()
    log_mem("fetch_last_dose:end")


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
        gc.collect()
        log_mem("fetch_current_status:after_collect")
    gc.collect()
    log_mem("submit_dose:end")




def format_iso_z(epoch):
    try:
        tm = time.localtime(int(epoch))
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (tm[0], tm[1], tm[2], tm[3], tm[4], tm[5])
    except Exception:
        tm = time.localtime()
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (tm[0], tm[1], tm[2], tm[3], tm[4], tm[5])


def format_local_hhmm_ampm(epoch=None):
    try:
        if epoch is None:
            epoch = time.time()
        tz_s = get_setting("tz_offset_min") * 60
        tm = time.localtime(int(epoch) + tz_s)
        hour_24 = tm[3]
        minute = tm[4]
        ampm = "am" if hour_24 < 12 else "pm"
        hour_12 = hour_24 % 12
        if hour_12 == 0:
            hour_12 = 12
        return "%d:%02d%s" % (hour_12, minute, ampm)
    except Exception:
        tm = time.localtime()
        return "%02d:%02d" % (tm[3], tm[4])


def today_bounds_epoch():
    # Compute start/end of local day, return as UTC epoch, honoring tz_offset_min
    tz_s = get_setting("tz_offset_min") * 60
    now_utc = time.time()
    lt = time.localtime(now_utc + tz_s)
    start_local = (lt[0], lt[1], lt[2], 0, 0, 0, 0, 0)
    end_local = (lt[0], lt[1], lt[2], 23, 59, 59, 0, 0)
    start = time.mktime(start_local) - tz_s
    end = time.mktime(end_local) - tz_s
    return start, end


def window_bounds_epoch():
    # Compute local dosing window today, return as UTC epoch, honoring tz_offset_min
    tz_s = get_setting("tz_offset_min") * 60
    now_utc = time.time()
    lt = time.localtime(now_utc + tz_s)
    ws_local = (lt[0], lt[1], lt[2], get_setting("dose_window_start_hour"), 0, 0, 0, 0)
    we_local = (lt[0], lt[1], lt[2], get_setting("dose_window_end_hour"), 0, 0, 0, 0)
    ws = time.mktime(ws_local) - tz_s
    we = time.mktime(we_local) - tz_s
    return ws, we


def fetch_today_summary():
    global error_type
    r = None
    try:
        start, end = today_bounds_epoch()
        start_iso = format_iso_z(start)
        end_iso = format_iso_z(end)
        url = "%s/rest/v1/stimulants?select=created_at,qty,dose,dose_unit&created_at=gte.%s&created_at=lte.%s&order=created_at.asc" % (SUPABASE_URL, start_iso, end_iso)
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": "Bearer %s" % SUPABASE_KEY,
            "Accept": "application/json",
        }
        r = http_get(url, headers=headers)
        if 200 <= r.status_code < 300:
            error_type = None
            data = json.loads(r.text)
            qty_sum = 0
            total_mg = 0
            last_epoch = None
            for item in data:
                q = int(item.get("qty") or 0)
                d = item.get("dose")
                try:
                    d = int(d) if d is not None and int(d) == d else (d if d is not None else DOSE)
                except Exception:
                    d = DOSE
                qty_sum += q
                total_mg += q * d
                ce = item.get("created_at")
                if ce:
                    ep = parse_iso8601_to_epoch(ce)
                    if ep is not None:
                        last_epoch = ep
            return {"qty_sum": qty_sum, "total_mg": total_mg, "last_epoch": last_epoch}
        else:
            error_type = "db fail"
            return None
    except Exception:
        error_type = "db fail"
        return None
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def fetch_current_status():
    global error_type
    r = None
    try:
        url = "%s/rest/v1/daily_log?select=created_at,event_type&order=created_at.desc&limit=1" % SUPABASE_URL
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": "Bearer %s" % SUPABASE_KEY,
            "Accept": "application/json",
        }
        r = http_get(url, headers=headers)
        if 200 <= r.status_code < 300:
            error_type = None
            data = json.loads(r.text)
            if isinstance(data, list) and len(data) > 0:
                item = data[0]
                ev = (item.get("event_type") or "").strip()
                ce = item.get("created_at")
                ep = parse_iso8601_to_epoch(ce) if ce else None
                label_map = {
                    "work_start": "work",
                    "journal_start": "journal",
                    "awake": "awake",
                    "asleep": "asleep",
                    "work_end": "relax",
                }
                label = label_map.get(ev, ev or "status")
                return {"label": label, "epoch": ep}
            return {"label": "status", "epoch": None}
        else:
            error_type = "db fail"
            return {"label": "status", "epoch": None}
    except Exception:
        error_type = "db fail"
        return {"label": "status", "epoch": None}
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def compute_next_dose_time(last_dose_epoch, today_qty_sum):
    # Returns epoch of earliest next scheduled dose within window or None
    now = time.time()
    ws, we = window_bounds_epoch()
    max_pills = get_setting("max_doses_per_day")
    if today_qty_sum is not None and today_qty_sum >= max_pills:
        return None
    # First dose of day: schedule at window start only
    if today_qty_sum is not None and today_qty_sum == 0:
        if now < ws:
            return ws
        return None
    # Otherwise, based on interval since last dose
    interval_s = get_setting("dose_interval_min") * 60
    if last_dose_epoch is None:
        return ws if now < ws else None
    earliest = last_dose_epoch + interval_s
    if earliest < ws:
        earliest = ws
    if earliest > we:
        return None
    return earliest


def check_submission_restrictions(last_dose_epoch, today_qty_sum):
    # Returns (allowed: bool, reason: str or None)
    now = time.time()
    ws, we = window_bounds_epoch()
    max_pills = get_setting("max_doses_per_day")
    if today_qty_sum is not None and today_qty_sum >= max_pills:
        return False, "limit"
    if now < ws:
        return False, "too soon"
    if now > we:
        return False, "too late"
    if last_dose_epoch is not None:
        interval_s = get_setting("dose_interval_min") * 60
        if now < (last_dose_epoch + interval_s):
            return False, "too soon"
    return True, None

def render_last_dose(info):
    # Desired layout (no big time):
    #  last dose
    #  dexies
    #  2 @ 1:12pm
    oled.fill(0)
    if not info or not info.get("epoch"):
        show_message(["last dose"], align="left")
        return
    epoch = info.get("epoch")
    start, _end = today_bounds_epoch()
    # If last dose was not today, show only the title
    if epoch < start:
        show_message(["last dose"], align="left")
        return
    # Lines per spec:
    # 1: last dose
    # 2: FRIENDLY_NAME (name)
    # 3: qty(totalmg) e.g., 2(10mg)
    # 4: time e.g., 3:22pmwa
    qty = info.get("qty")
    dose_val = info.get("dose")
    try:
        dose_val = int(dose_val) if dose_val is not None and int(dose_val) == dose_val else (dose_val if dose_val is not None else DOSE)
    except Exception:
        dose_val = DOSE
    try:
        qty_int = int(qty) if qty is not None else None
    except Exception:
        qty_int = None
    if qty_int is not None:
        total_mg = qty_int * int(dose_val)
        qty_line = "%d(%d%s)" % (qty_int, total_mg, DOSE_UNIT)
    else:
        qty_line = "?"
    time_line = format_local_hhmm_ampm(epoch)
    show_message(["last dose", FRIENDLY_NAME, qty_line, time_line], align="left")


def render_time_until_next(seconds_remaining):
    if seconds_remaining is None:
        show_message(["next dose", "unknown"])
        return
    # When overdue, clamp to 0 minutes; alarm will flash the screen
    if seconds_remaining < 0:
        seconds_remaining = 0
    minutes = (seconds_remaining // 60)
    oled.fill(0)
    _draw_big_minutes_centered(minutes, label="mins")
    oled.show()


def render_daily_total_view(qty_sum, dose_val, unit):
    try:
        dose_val = int(dose_val)
    except Exception:
        dose_val = DOSE
    qty_sum = int(qty_sum or 0)
    total_mg = qty_sum * dose_val
    # Left-aligned summary like last-dose view
    top = "total"
    mid = FRIENDLY_NAME
    bot = "%d(%d%s)" % (qty_sum, total_mg, unit)
    show_message([top, mid, bot], align="left")


def render_status_view(status_info):
    label = status_info.get("label", "status")
    since_s = status_info.get("since_s")
    dur = format_duration(since_s if since_s is not None else 0)
    show_message([label, " %s " % dur])


def render_error_view(err):
    if not err:
        show_message(["ok"])
        return
    
    error_messages = {
        "no wifi": "no wifi",
        "db fail": "db fail"
    }
    msg = error_messages.get(err, str(err)[:12])
    show_message(["error", msg])


def enter_submission_mode():
    global mode, submit_qty, submit_entry_detent, last_user_input_ms, last_reported_detent, enc_changed, alarm_buzzer_on
    mode = MODE_SUBMIT
    # Immediately stop any active alarm effects
    buzzer.value(0)
    alarm_buzzer_on = False
    try:
        oled.invert(0)
    except Exception:
        pass
    led_on()
    det = position // 4 if position >= 0 else -((-position) // 4)
    submit_entry_detent = det
    submit_qty = 0
    last_user_input_ms = ticks_ms()
    last_reported_detent = det
    enc_changed = False
    show_submission(submit_qty)
    print("enter_submission_mode", submit_qty)


def exit_submission_mode():
    global mode
    mode = MODE_DISPLAY
    led_off()
    print("exit_submission_mode")


def enter_warning_mode(reason):
    global mode, warning_reason, warning_end_ms, last_warning_flash_ms, last_warning_render_ms
    mode = MODE_WARNING
    warning_reason = reason or "warning"
    try:
        warning_end_ms = time.ticks_add(ticks_ms(), 3000)
    except Exception:
        warning_end_ms = ticks_ms() + 3000
    last_warning_flash_ms = ticks_ms()
    last_warning_render_ms = 0
    print("enter_warning_mode", reason)


def render_warning_view():
    reason_text = warning_reason or "warning"
    show_message(["warning", reason_text, "ignore"])


def cycle_view(available_views):
    global current_view, last_view_interaction_ms
    try:
        i = available_views.index(current_view)
        current_view = available_views[(i + 1) % len(available_views)]
    except Exception:
        if available_views:
            current_view = available_views[0]
        else:
            current_view = VIEW_STATUS
    last_view_interaction_ms = ticks_ms()
    print("cycle_view", current_view)


def select_default_view(next_dose_epoch, last_dose_info, today_qty_sum, status_info):
    now = time.time()
    print("select_default_view", "| the time is", format_local_hhmm_ampm(now))
    if post_submit_display_until_s and now < post_submit_display_until_s:
        return VIEW_LAST_DOSE
    if next_dose_epoch is not None:
        if now >= (next_dose_epoch - 90 * 60) and now <= next_dose_epoch:
            return VIEW_NEXT_DOSE
    return VIEW_STATUS
    


def update_alarm_state(today_qty_sum, next_dose_epoch, last_dose_epoch):
    # Decide whether to enable alarm based on dosing rules and timing
    global alarm_active
    now = time.time()
    ws, we = window_bounds_epoch()

    # Suppress alarm during grace period
    if now < alarm_grace_until_s:
        alarm_active = False
        return

    # Do not alarm outside window
    if not (now >= ws and now <= we):
        alarm_active = False
        return

    # Respect submission restrictions (max per day, too soon, etc.)
    allow, _reason = check_submission_restrictions(last_dose_epoch, today_qty_sum)
    if not allow:
        alarm_active = False
        return

    # If zero taken so far today and within window, alarm to prompt first dose
    if (today_qty_sum or 0) == 0:
        alarm_active = True
        return

    # Otherwise, alarm when the next dose is due (countdown <= 0)
    if next_dose_epoch is not None and now >= next_dose_epoch:
        alarm_active = True
        return

    alarm_active = False


def tick_alarm_effects():
    global last_alarm_toggle_ms, alarm_buzzer_on
    if mode != MODE_DISPLAY or not alarm_active:
        if alarm_buzzer_on:
            buzzer.value(0)
            alarm_buzzer_on = False
        led_off()
        # Ensure display is back to normal
        try:
            oled.invert(0)
        except Exception:
            pass
        return
    now_ms = ticks_ms()
    if ms_since(last_alarm_toggle_ms) >= 250:
        last_alarm_toggle_ms = now_ms
        # Toggle LED
        led.value(1 - led.value())
        # Toggle buzzer and screen inversion in sync
        if alarm_buzzer_on:
            buzzer.value(0)
            try:
                oled.invert(0)
            except Exception:
                pass
            alarm_buzzer_on = False
        else:
            buzzer.value(1)
            try:
                oled.invert(1)
            except Exception:
                pass
            alarm_buzzer_on = True


def main():
    global error_type, current_view, default_view, last_view_interaction_ms, post_submit_display_until_s, last_warning_flash_ms, last_warning_render_ms
    show_message(["Booting"])

    load_settings()

    wifi_ok = connect_wifi(WIFI_SSID, WIFI_PASSWORD)
    print("wifi_ok", wifi_ok)
    if not wifi_ok:
        error_type = "no wifi"
        time.sleep(2)
        print("no wifi")

    sync_time_via_ntp()
    last_info = fetch_last_dose()
    today = fetch_today_summary()
    status_info = fetch_current_status()
    if today is None:
        today = {"qty_sum": 0, "total_mg": 0, "last_epoch": None}
    if status_info is None:
        status_info = {"label": "status", "epoch": None}

    next_dose_epoch = compute_next_dose_time((last_info or {}).get("epoch"), today.get("qty_sum"))
    last_view_interaction_ms = ticks_ms()
    current_view = select_default_view(next_dose_epoch, last_info, today.get("qty_sum"), status_info)
    default_view = VIEW_STATUS

    last_dose_fetch_ts = time.time()
    last_summary_fetch_ts = last_dose_fetch_ts
    last_status_fetch_ts = last_dose_fetch_ts
    FETCH_LAST_DOSE_INTERVAL_S = 30
    FETCH_TODAY_SUMMARY_INTERVAL_S = 60
    FETCH_STATUS_INTERVAL_S = 60

    while True:
        global enc_changed, position, last_reported_detent, switch_changed, switch_state, mode, submit_qty, submit_entry_detent, last_user_input_ms
        now = time.time()

        if mode == MODE_DISPLAY:
            if now - last_dose_fetch_ts >= FETCH_LAST_DOSE_INTERVAL_S:
                info = fetch_last_dose()
                if info:
                    last_info = info
                last_dose_fetch_ts = now
            if now - last_summary_fetch_ts >= FETCH_TODAY_SUMMARY_INTERVAL_S:
                t = fetch_today_summary()
                if t is not None:
                    today = t
                last_summary_fetch_ts = now
            if now - last_status_fetch_ts >= FETCH_STATUS_INTERVAL_S:
                s = fetch_current_status()
                if s is not None:
                    status_info = s
                last_status_fetch_ts = now
            next_dose_epoch = compute_next_dose_time((last_info or {}).get("epoch"), today.get("qty_sum"))

            update_alarm_state(today.get("qty_sum"), next_dose_epoch, (last_info or {}).get("epoch"))
            tick_alarm_effects()

            if ms_since(last_view_interaction_ms) >= 10000:
                current_view = select_default_view(next_dose_epoch, last_info, today.get("qty_sum"), status_info)
                last_view_interaction_ms = ticks_ms()

            if mode == MODE_DISPLAY:
                if current_view == VIEW_STATUS:
                    since_s = None
                    ep = status_info.get("epoch")
                    if ep is not None:
                        try:
                            ep_i = int(ep)
                        except Exception:
                            ep_i = int(now)
                        since_s = max(0, int(int(now) - ep_i))
                    render_status_view({"label": status_info.get("label", "status"), "since_s": since_s})
                elif current_view == VIEW_NEXT_DOSE:
                    if next_dose_epoch is not None:
                        remaining = max(0, int(next_dose_epoch - now))
                        render_time_until_next(remaining)
                    else:
                        show_message(["next", "unknown"])
                elif current_view == VIEW_LAST_DOSE:
                    render_last_dose(last_info)
                    print("render_last_dose", last_info)
                elif current_view == VIEW_DAILY_TOTAL:
                    render_daily_total_view(today.get("qty_sum"), (last_info or {}).get("dose") or DOSE, DOSE_UNIT)
                    print("render_daily_total_view", today.get("qty_sum"), (last_info or {}).get("dose") or DOSE, DOSE_UNIT)
                elif current_view == VIEW_ERROR:
                    render_error_view(error_type)
                    print("render_error_view", error_type)

        # Polling interval ~50ms
        for _ in range(20):
            # Handle encoder changes
            if enc_changed:
                enc_changed = False
                det = position // 4 if position >= 0 else -((-position) // 4)
                if mode == MODE_DISPLAY:
                    last_view_interaction_ms = ticks_ms()
                    allow, reason = check_submission_restrictions((last_info or {}).get("epoch"), (today or {}).get("qty_sum"))
                    if allow:
                        enter_submission_mode()
                    else:
                        enter_warning_mode(reason)
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
                if switch_state == 0:
                    if mode == MODE_DISPLAY:
                        # Single press cycles between views
                        available = [VIEW_STATUS]
                        if next_dose_epoch is not None:
                            available.append(VIEW_NEXT_DOSE)
                        available.append(VIEW_LAST_DOSE)
                        available.append(VIEW_DAILY_TOTAL)
                        if error_type:
                            available.append(VIEW_ERROR)
                        cycle_view(available)
                    elif mode == MODE_WARNING:
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
                                show_message(["retrying", "%d/3..." % attempt])
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
                                # Suppress alarm while backend updates daily summary
                                try:
                                    from time import time as _now
                                except Exception:
                                    _now = time.time
                                try:
                                    # 2-minute grace period
                                    globals()["alarm_grace_until_s"] = _now() + 120
                                except Exception:
                                    pass
                                post_submit_display_until_s = time.time() + 30
                                render_last_dose(last_info)
                            else:
                                show_message(["fail"])
                                flash_led(2, 200, 200)
                                time.sleep(1.0)
                                exit_submission_mode()
                                render_last_dose(last_info)

            # Warning mode logic
            if mode == MODE_WARNING:
                # Only render at most every 250ms to reduce redraws/logs
                if time.ticks_diff(ticks_ms(), last_warning_render_ms) >= 250:
                    last_warning_render_ms = ticks_ms()
                    render_warning_view()
                if time.ticks_diff(ticks_ms(), last_warning_flash_ms) >= 100:
                    last_warning_flash_ms = ticks_ms()
                    led.value(1 - led.value())
                if time.ticks_diff(ticks_ms(), warning_end_ms) >= 0:
                    mode = MODE_DISPLAY
                    led_off()
                    current_view = select_default_view(next_dose_epoch, last_info, today.get("qty_sum"), status_info)
                    last_view_interaction_ms = ticks_ms()

                # keep submission UI visible if active
                # show_submission called on qty change

            time.sleep(0.05)


if __name__ == "__main__":
    main()
