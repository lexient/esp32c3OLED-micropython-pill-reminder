import os, time, network, gc
from machine import Pin, I2C
from ssd1306 import SSD1306_I2C
try: import urequests as requests
except: import requests
try: import ujson as json
except: import json
try: import ntptime
except: ntptime = None

try:
    from secrets import WIFI_SSID, WIFI_PASSWORD, SUPABASE_URL, SUPABASE_KEY
except Exception:
    WIFI_SSID = None
    WIFI_PASSWORD = None
    SUPABASE_URL = None
    SUPABASE_KEY = None

# display constants
BUFFER_WIDTH, BUFFER_HEIGHT = 128, 64
DISPLAY_WIDTH, DISPLAY_HEIGHT = 72, 40
FONT_WIDTH, FONT_HEIGHT = 8, 8
TEXT_Y_ADJUST = 2
X_OFFSET = (BUFFER_WIDTH - DISPLAY_WIDTH) // 2
Y_OFFSET = (BUFFER_HEIGHT - DISPLAY_HEIGHT) // 2 + 12  # panel y adjustment

# hardware setup
i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=400000)
oled = SSD1306_I2C(BUFFER_WIDTH, BUFFER_HEIGHT, i2c)
enc_a = Pin(1, Pin.IN, Pin.PULL_UP)
enc_b = Pin(2, Pin.IN, Pin.PULL_UP)
sw = Pin(0, Pin.IN, Pin.PULL_UP)
buzzer = Pin(10, Pin.OUT)
buzzer.value(0)
led = Pin(8, Pin.OUT)
led.value(1)  # active low

# encoder state
position = enc_last = enc_changed = last_enc_ms = last_reported_detent = 0
switch_state = sw.value()
switch_changed = last_sw_ms = 0

# app state
MODE_DISPLAY, MODE_SUBMIT, MODE_WARNING = 0, 1, 2
mode = MODE_DISPLAY
REQUEST_TIMEOUT_S = 2

# settings
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "dose_interval_min": 180, "dose_window_start_hour": 8,
    "dose_window_end_hour": 16, "max_doses_per_day": 7, "tz_offset_min": 0
}
settings = None

# submission config
FRIENDLY_NAME, DRUG_NAME = "dexies", "dexamphetamine"
DOSE, DOSE_UNIT = 5, "mg"

# submission state
submit_qty = submit_entry_detent = last_user_input_ms = 0

# view system
VIEW_STATUS, VIEW_NEXT_DOSE, VIEW_LAST_DOSE, VIEW_DAILY_TOTAL, VIEW_ERROR, VIEW_WARNING = range(6)
current_view = default_view = VIEW_STATUS
last_view_interaction_ms = post_submit_display_until_s = 0

# error/warning state
error_type = warning_reason = None
warning_end_ms = last_warning_flash_ms = last_warning_render_ms = 0

# alarm state
alarm_active = alarm_buzzer_on = False
last_alarm_toggle_ms = alarm_grace_until_s = 0

def http_req(method, url, headers=None, data=None):
    # unified http request handler
    try:
        fn = requests.get if method == 'GET' else requests.post
        params = {'url': url, 'headers': headers}
        if method == 'POST': params['data'] = data
        try: params['timeout'] = REQUEST_TIMEOUT_S
        except: pass
        return fn(**params)
    except: return None

def load_settings():
    # load settings from file or use defaults
    global settings
    try:
        with open(SETTINGS_FILE, "r") as f:
            raw = json.loads(f.read())
        settings = {k: int(raw.get(k, DEFAULT_SETTINGS[k])) for k in DEFAULT_SETTINGS}
    except:
        settings = DEFAULT_SETTINGS.copy()
        try:
            with open(SETTINGS_FILE, "w") as f:
                f.write(json.dumps(settings))
        except: pass

def get_setting(key):
    # get setting value with fallback
    return (settings or DEFAULT_SETTINGS).get(key, DEFAULT_SETTINGS[key])

def ms_since(start_ms):
    return time.ticks_diff(time.ticks_ms(), start_ms)

def _enc_irq(_):
    # encoder interrupt handler
    global enc_last, position, enc_changed, last_enc_ms
    ms = time.ticks_ms()
    if time.ticks_diff(ms, last_enc_ms) < 2: return
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
    # switch interrupt handler
    global switch_state, switch_changed, last_sw_ms
    ms = time.ticks_ms()
    if time.ticks_diff(ms, last_sw_ms) < 50: return
    last_sw_ms = ms
    switch_state = sw.value()
    switch_changed = True

enc_a.irq(_enc_irq, Pin.IRQ_RISING | Pin.IRQ_FALLING)
enc_b.irq(_enc_irq, Pin.IRQ_RISING | Pin.IRQ_FALLING)
sw.irq(_sw_irq, Pin.IRQ_RISING | Pin.IRQ_FALLING)

def flash_led(times=1, on_ms=120, off_ms=120):
    # flash led indication
    for _ in range(times):
        led.value(0)  # on
        time.sleep_ms(on_ms)
        led.value(1)  # off
        time.sleep_ms(off_ms)

def x_for_alignment(text, align):
    # calculate x position for text alignment
    text_width = len(text) * FONT_WIDTH
    if align == "centre": return X_OFFSET + max(0, (DISPLAY_WIDTH - text_width) // 2)
    if align == "right": return X_OFFSET + max(0, DISPLAY_WIDTH - text_width)
    return X_OFFSET + 2

def show_message(lines, align="centre"):
    # display centred text message
    oled.fill(0)
    num_lines = len(lines)
    total_text_height = num_lines * FONT_HEIGHT
    gap = max(0, DISPLAY_HEIGHT - total_text_height) // (num_lines + 1) if num_lines else 0
    y = Y_OFFSET + gap
    for text in lines:
        oled.text(text, x_for_alignment(text, align), y + TEXT_Y_ADJUST)
        y += FONT_HEIGHT + gap
    oled.show()

def _draw_digit_7seg(x, y, w, h, t, digit):
    # draw 7-segment digit
    mid_y = y + h // 2
    inner_w = max(1, w - 2 * t)
    top_h = bot_h = max(1, (h - 3 * t) // 2)
    
    segs = {
        'a': (x + t, y, inner_w, t),
        'b': (x + w - t, y + t, t, top_h),
        'c': (x + w - t, mid_y + t // 2, t, bot_h),
        'd': (x + t, y + h - t, inner_w, t),
        'e': (x, mid_y + t // 2, t, bot_h),
        'f': (x, y + t, t, top_h),
        'g': (x + t, mid_y - t // 2, inner_w, t),
    }
    
    digit_map = ['abcdef', 'bc', 'abged', 'abgcd', 'fgbc', 'afgcd', 'afgecd', 'abc', 'abcdefg', 'abcdfg']
    for seg in digit_map[digit % 10]:
        oled.fill_rect(*segs[seg], 1)

def _draw_big_number_centred(num_str):
    # draw large centred number
    h = min(24, max(16, DISPLAY_HEIGHT - (FONT_HEIGHT + 4)))
    t = max(2, h // 8)
    w = max(12, h // 2 + t)
    gap = max(2, t)
    
    n = len(num_str)
    total_w = n * w + (n - 1) * gap
    if total_w > DISPLAY_WIDTH:
        scale = DISPLAY_WIDTH / total_w
        h, t, w, gap = (max(12, int(h * scale)), max(1, int(t * scale)), 
                        max(8, int(w * scale)), max(1, int(gap * scale)))
        total_w = n * w + (n - 1) * gap
    
    x = X_OFFSET + max(0, (DISPLAY_WIDTH - total_w) // 2)
    y = Y_OFFSET + FONT_HEIGHT + 4
    
    for ch in num_str:
        if ch.isdigit():
            _draw_digit_7seg(x, y, w, h, t, int(ch))
            x += w + gap

def show_submission(qty):
    # display submission screen with big number
    oled.fill(0)
    oled.text(FRIENDLY_NAME, x_for_alignment(FRIENDLY_NAME, "centre"), Y_OFFSET + TEXT_Y_ADJUST)
    _draw_big_number_centred(str(qty))
    oled.show()

def _draw_big_minutes_centred(minutes, label="mins"):
    # draw centred minute count with label
    minutes = max(0, int(minutes or 0))
    num_str = str(minutes)
    
    h, t = 20, 2
    w = max(10, h // 2 + t)
    gap = max(2, t)
    
    n = len(num_str)
    total_w = n * w + (n - 1) * gap
    if total_w > DISPLAY_WIDTH:
        scale = DISPLAY_WIDTH / total_w
        h, t, w, gap = (max(14, int(h * scale)), max(2, int(t * scale)),
                        max(8, int(w * scale)), max(1, int(gap * scale)))
        total_w = n * w + (n - 1) * gap
    
    x = X_OFFSET + max(0, (DISPLAY_WIDTH - total_w) // 2)
    y = Y_OFFSET + max(0, (DISPLAY_HEIGHT - (h + FONT_HEIGHT + 2)) // 2)
    
    for ch in num_str:
        if ch.isdigit():
            _draw_digit_7seg(x, y, w, h, t, int(ch))
            x += w + gap
    
    oled.text(label, x_for_alignment(label, "centre"), y + h + 2 + TEXT_Y_ADJUST)

def format_duration(seconds, show_days=False):
    # format duration as human readable
    if seconds is None or seconds < 0: return "unknown"
    minutes = int(seconds // 60)
    hours, minutes = minutes // 60, minutes % 60
    days, hours = hours // 24, hours % 24
    
    if show_days and days > 0: return "%dd %02dh" % (days, hours)
    if hours > 0: return "%dh %02dm" % (hours, minutes)
    return "%dm" % minutes

def connect_wifi(ssid, password, timeout_s=20):
    # connect to wifi network
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
    # synchronise time via ntp
    if not ntptime: return False
    for _ in range(retries):
        try:
            ntptime.settime()
            return True
        except: time.sleep(1)
    return False

def parse_iso8601_to_epoch(iso_str):
    # parse iso8601 datetime to epoch
    try:
        y, mo, d = int(iso_str[0:4]), int(iso_str[5:7]), int(iso_str[8:10])
        h, mi, s = int(iso_str[11:13]), int(iso_str[14:16]), int(iso_str[17:19])
        
        tz_offset_sec = 0
        if len(iso_str) > 19:
            tail = iso_str[19:]
            for sign, idx in [('+', tail.rfind('+')), ('-', tail.rfind('-'))]:
                if idx > -1:
                    idx = 19 + idx
                    if idx > 19:
                        hh, mm = int(iso_str[idx+1:idx+3]), int(iso_str[idx+4:idx+6])
                        tz_offset_sec = (hh * 60 + mm) * 60 * (1 if sign == '+' else -1)
                    break
        
        return time.mktime((y, mo, d, h, mi, s, 0, 0)) - tz_offset_sec
    except: return None

def fetch_supabase_get(endpoint, params=""):
    # fetch data from supabase (get request)
    global error_type
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}{params}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Accept": "application/json"}
    
    r = None
    try:
        r = http_req("GET", url, headers)
        if r and 200 <= r.status_code < 300:
            error_type = None
            return json.loads(r.text)
        error_type = "db fail"
        flash_led(1, 150, 150)
        return None
    except:
        error_type = "db fail"
        flash_led(1, 150, 150)
        return None
    finally:
        if r: 
            try: r.close()
            except: pass
        gc.collect()

def fetch_supabase_post(endpoint, payload):
    # post data to supabase
    global error_type
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", 
               "Content-Type": "application/json", "Accept": "application/json"}
    
    r = None
    try:
        r = http_req("POST", url, headers, json.dumps(payload))
        if r and 200 <= r.status_code < 300:
            error_type = None
            return True
        error_type = "db fail"
        return False
    except:
        error_type = "db fail"
        return False
    finally:
        if r: 
            try: r.close()
            except: pass
        gc.collect()

def fetch_last_dose():
    # fetch most recent dose from database
    data = fetch_supabase_get("stimulants", "?select=created_at,qty,drug,dose,dose_unit&order=created_at.desc&limit=1")
    if data and isinstance(data, list) and len(data) > 0:
        item = data[0]
        return {
            "epoch": parse_iso8601_to_epoch(item.get("created_at")),
            "qty": item.get("qty"),
            "drug": item.get("drug"),
            "dose": item.get("dose", DOSE),
            "dose_unit": item.get("dose_unit", DOSE_UNIT)
        }
    return None

def submit_dose(qty):
    # submit dose to database
    payload = {"qty": int(qty), "drug": DRUG_NAME, "dose": DOSE, "dose_unit": DOSE_UNIT}
    return fetch_supabase_post("stimulants", payload)

def format_iso_z(epoch):
    # format epoch as iso8601 utc
    try:
        tm = time.localtime(int(epoch))
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % tm[:6]
    except:
        tm = time.localtime()
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % tm[:6]

def format_local_hhmm_ampm(epoch=None):
    # format time as local 12-hour with am/pm
    try:
        if epoch is None: epoch = time.time()
        tz_s = get_setting("tz_offset_min") * 60
        tm = time.localtime(int(epoch) + tz_s)
        hour_24, minute = tm[3], tm[4]
        ampm = "am" if hour_24 < 12 else "pm"
        hour_12 = hour_24 % 12 or 12
        return "%d:%02d%s" % (hour_12, minute, ampm)
    except:
        tm = time.localtime()
        return "%02d:%02d" % (tm[3], tm[4])

def get_day_bounds(bounds_type="day"):
    # get start/end epoch for today or window
    tz_s = get_setting("tz_offset_min") * 60
    lt = time.localtime(time.time() + tz_s)
    
    if bounds_type == "window":
        start_h, end_h = get_setting("dose_window_start_hour"), get_setting("dose_window_end_hour")
    else:
        start_h, end_h = 0, 23
        
    start = time.mktime((lt[0], lt[1], lt[2], start_h, 0 if bounds_type == "window" else 0, 0, 0, 0)) - tz_s
    end = time.mktime((lt[0], lt[1], lt[2], end_h, 0 if bounds_type == "window" else 59, 59 if bounds_type == "day" else 0, 0, 0)) - tz_s
    return start, end

def fetch_today_summary():
    # fetch today's dosing summary
    start, end = get_day_bounds("day")
    params = f"?select=created_at,qty,dose,dose_unit&created_at=gte.{format_iso_z(start)}&created_at=lte.{format_iso_z(end)}&order=created_at.asc"
    data = fetch_supabase_get("stimulants", params)
    
    if data is None: return None
    
    qty_sum = total_mg = 0
    last_epoch = None
    for item in data:
        q = int(item.get("qty") or 0)
        d = int(item.get("dose", DOSE))
        qty_sum += q
        total_mg += q * d
        if ce := item.get("created_at"):
            if ep := parse_iso8601_to_epoch(ce):
                last_epoch = ep
    
    return {"qty_sum": qty_sum, "total_mg": total_mg, "last_epoch": last_epoch}

def fetch_current_status():
    # fetch current status from daily log
    data = fetch_supabase_get("daily_log", "?select=created_at,event_type&order=created_at.desc&limit=1")
    
    if data and isinstance(data, list) and len(data) > 0:
        item = data[0]
        ev = (item.get("event_type") or "").strip()
        ep = parse_iso8601_to_epoch(item.get("created_at"))
        label_map = {"work_start": "work", "journal_start": "journal", "awake": "awake", 
                     "asleep": "asleep", "work_end": "relax"}
        return {"label": label_map.get(ev, ev or "status"), "epoch": ep}
    
    return {"label": "status", "epoch": None}

def compute_next_dose_time(last_dose_epoch, today_qty_sum):
    # calculate next allowed dose time
    now = time.time()
    ws, we = get_day_bounds("window")
    max_pills = get_setting("max_doses_per_day")
    
    if today_qty_sum is not None and today_qty_sum >= max_pills: return None
    if today_qty_sum is not None and today_qty_sum == 0:
        return ws if now < ws else None
    
    interval_s = get_setting("dose_interval_min") * 60
    if last_dose_epoch is None: return ws if now < ws else None
    
    earliest = last_dose_epoch + interval_s
    if earliest < ws: earliest = ws
    if earliest > we: return None
    return earliest

def check_submission_restrictions(last_dose_epoch, today_qty_sum):
    # check if submission allowed and reason if not
    now = time.time()
    ws, we = get_day_bounds("window")
    max_pills = get_setting("max_doses_per_day")
    
    if today_qty_sum is not None and today_qty_sum >= max_pills: return False, "limit"
    if now < ws or now > we: return False, "too soon" if now < ws else "too late"
    if last_dose_epoch and now < (last_dose_epoch + get_setting("dose_interval_min") * 60):
        return False, "too soon"
    return True, None

def render_last_dose(info):
    # render last dose view
    oled.fill(0)
    if not info or not info.get("epoch"):
        show_message(["recent"], align="left")
        return
    
    epoch = info.get("epoch")
    start, _ = get_day_bounds("day")
    if epoch < start:
        show_message(["recent"], align="left")
        return
    
    qty = info.get("qty")
    dose_val = int(info.get("dose", DOSE))
    qty_int = int(qty) if qty is not None else None
    qty_line = f"{qty_int}({qty_int * dose_val}{DOSE_UNIT})" if qty_int is not None else "?"
    time_line = format_local_hhmm_ampm(epoch)
    show_message(["recent", FRIENDLY_NAME, qty_line, time_line], align="left")

def render_time_until_next(seconds_remaining):
    # render next dose countdown
    if seconds_remaining is None:
        show_message(["next dose", "unknown"])
        return
    oled.fill(0)
    _draw_big_minutes_centred(max(0, seconds_remaining // 60), "mins")
    oled.show()

def render_views(view, data):
    # render different views based on current selection
    if view == VIEW_STATUS:
        since_s = max(0, int(time.time() - data['status']['epoch'])) if data['status'].get('epoch') else 0
        show_message([data['status'].get('label', 'status'), f" {format_duration(since_s)} "])
    elif view == VIEW_NEXT_DOSE:
        if data['next_dose_epoch']:
            render_time_until_next(max(0, int(data['next_dose_epoch'] - time.time())))
        else:
            show_message(["next", "unknown"])
    elif view == VIEW_LAST_DOSE:
        render_last_dose(data['last_info'])
    elif view == VIEW_DAILY_TOTAL:
        qty = int(data['today'].get('qty_sum', 0))
        dose = int(data['last_info'].get('dose', DOSE)) if data['last_info'] else DOSE
        total = qty * dose
        show_message(["total", FRIENDLY_NAME, f"{qty}({total}{DOSE_UNIT})"], align="left")
    elif view == VIEW_ERROR:
        show_message(["error", error_type or "ok"])

def enter_submission_mode():
    # enter dose submission mode
    global mode, submit_qty, submit_entry_detent, last_user_input_ms, alarm_buzzer_on
    mode = MODE_SUBMIT
    buzzer.value(0)
    alarm_buzzer_on = False
    try: oled.invert(0)
    except: pass
    led.value(0)  # on
    submit_entry_detent = position // 4 if position >= 0 else -((-position) // 4)
    submit_qty = 0
    last_user_input_ms = time.ticks_ms()
    show_submission(submit_qty)

def enter_warning_mode(reason):
    # enter warning mode
    global mode, warning_reason, warning_end_ms, last_warning_flash_ms
    mode = MODE_WARNING
    warning_reason = reason or "warning"
    warning_end_ms = time.ticks_ms() + 3000
    last_warning_flash_ms = time.ticks_ms()

def update_alarm_state(today_qty_sum, next_dose_epoch, last_dose_epoch):
    # update alarm activation state
    global alarm_active
    now = time.time()
    ws, we = get_day_bounds("window")
    
    if now < alarm_grace_until_s or not (ws <= now <= we):
        alarm_active = False
        return
    
    allow, _ = check_submission_restrictions(last_dose_epoch, today_qty_sum)
    if not allow:
        alarm_active = False
        return
    
    alarm_active = ((today_qty_sum or 0) == 0) or (next_dose_epoch and now >= next_dose_epoch)

def tick_alarm_effects():
    # handle alarm visual/audio effects
    global last_alarm_toggle_ms, alarm_buzzer_on
    if mode != MODE_DISPLAY or not alarm_active:
        if alarm_buzzer_on:
            buzzer.value(0)
            alarm_buzzer_on = False
        led.value(1)  # off
        try: oled.invert(0)
        except: pass
        return
    
    if ms_since(last_alarm_toggle_ms) >= 250:
        last_alarm_toggle_ms = time.ticks_ms()
        led.value(1 - led.value())
        alarm_buzzer_on = not alarm_buzzer_on
        buzzer.value(1 if alarm_buzzer_on else 0)
        try: oled.invert(1 if alarm_buzzer_on else 0)
        except: pass

def main():
    # main program loop
    global error_type, current_view, last_view_interaction_ms, post_submit_display_until_s
    global enc_changed, position, switch_changed, switch_state, mode, submit_qty, alarm_grace_until_s
    global warning_end_ms, last_warning_flash_ms, last_warning_render_ms
    
    show_message(["Booting"])
    load_settings()
    
    secrets_missing = not all([WIFI_SSID, WIFI_PASSWORD, SUPABASE_URL, SUPABASE_KEY])
    wifi_ok = False if secrets_missing else connect_wifi(WIFI_SSID, WIFI_PASSWORD)
    print("wifi_ok", wifi_ok)
    if secrets_missing:
        error_type = "no secrets"
        show_message(["secrets.py", "missing"]) 
        time.sleep(2)
    elif not wifi_ok:
        error_type = "no wifi"
        time.sleep(2)
    
    sync_time_via_ntp()
    
    # initial data fetch
    last_info = fetch_last_dose()
    today = fetch_today_summary() or {"qty_sum": 0, "total_mg": 0, "last_epoch": None}
    status_info = fetch_current_status() or {"label": "status", "epoch": None}
    
    next_dose_epoch = compute_next_dose_time((last_info or {}).get("epoch"), today.get("qty_sum"))
    last_view_interaction_ms = time.ticks_ms()
    
    # select initial view
    now = time.time()
    if next_dose_epoch and (next_dose_epoch - 90 * 60) <= now <= next_dose_epoch:
        current_view = VIEW_NEXT_DOSE
    else:
        current_view = VIEW_STATUS
    
    last_fetch_ts = time.time()
    FETCH_INTERVALS = {"dose": 30, "summary": 60, "status": 60}
    last_fetch = {k: last_fetch_ts for k in FETCH_INTERVALS}
    
    while True:
        now = time.time()
        
        if mode == MODE_DISPLAY:
            # periodic data refresh
            if now - last_fetch["dose"] >= FETCH_INTERVALS["dose"]:
                if info := fetch_last_dose(): last_info = info
                last_fetch["dose"] = now
            
            if now - last_fetch["summary"] >= FETCH_INTERVALS["summary"]:
                if t := fetch_today_summary(): today = t
                last_fetch["summary"] = now
            
            if now - last_fetch["status"] >= FETCH_INTERVALS["status"]:
                if s := fetch_current_status(): status_info = s
                last_fetch["status"] = now
            
            next_dose_epoch = compute_next_dose_time((last_info or {}).get("epoch"), today.get("qty_sum"))
            update_alarm_state(today.get("qty_sum"), next_dose_epoch, (last_info or {}).get("epoch"))
            tick_alarm_effects()
            
            # auto-select view after inactivity
            if ms_since(last_view_interaction_ms) >= 10000:
                if next_dose_epoch and (next_dose_epoch - 90 * 60) <= now <= next_dose_epoch:
                    current_view = VIEW_NEXT_DOSE
                else:
                    current_view = VIEW_STATUS
                last_view_interaction_ms = time.ticks_ms()
            
            # render current view
            if mode == MODE_DISPLAY:
                render_views(current_view, {
                    'status': status_info, 'next_dose_epoch': next_dose_epoch,
                    'last_info': last_info, 'today': today
                })
        
        # handle input events
        for _ in range(20):
            if enc_changed:
                enc_changed = False
                det = position // 4 if position >= 0 else -((-position) // 4)
                
                if mode == MODE_DISPLAY:
                    last_view_interaction_ms = time.ticks_ms()
                    allow, reason = check_submission_restrictions((last_info or {}).get("epoch"), (today or {}).get("qty_sum"))
                    enter_submission_mode() if allow else enter_warning_mode(reason)
                elif mode == MODE_SUBMIT:
                    last_user_input_ms = time.ticks_ms()
                    submit_qty = max(0, det - submit_entry_detent)
                    show_submission(submit_qty)
            
            if switch_changed:
                switch_changed = False
                if switch_state == 0:
                    if mode == MODE_DISPLAY:
                        # cycle through available views
                        available = [VIEW_STATUS]
                        if next_dose_epoch: available.append(VIEW_NEXT_DOSE)
                        available.extend([VIEW_LAST_DOSE, VIEW_DAILY_TOTAL])
                        if error_type: available.append(VIEW_ERROR)
                        
                        try:
                            i = available.index(current_view)
                            current_view = available[(i + 1) % len(available)]
                        except:
                            current_view = available[0] if available else VIEW_STATUS
                        last_view_interaction_ms = time.ticks_ms()
                    elif mode == MODE_WARNING:
                        enter_submission_mode()
                elif mode == MODE_SUBMIT:
                    last_user_input_ms = time.ticks_ms()
            
            # submission mode timeout and processing
            if mode == MODE_SUBMIT and ms_since(last_user_input_ms) >= 5000:
                if submit_qty <= 0:
                    mode = MODE_DISPLAY
                    led.value(1)  # off
                else:
                    # warn with flashing then submit
                    canceled = False
                    for _ in range(6):
                        led.value(1 - led.value())
                        step_start = time.ticks_ms()
                        while ms_since(step_start) < 500:
                            if enc_changed or switch_changed:
                                canceled = True
                                break
                            time.sleep_ms(100)
                        if canceled: break
                    
                    if canceled:
                        led.value(0)  # on
                        last_user_input_ms = time.ticks_ms()
                        enc_changed = switch_changed = False
                    else:
                        # submit with retries
                        success = False
                        for attempt in range(1, 4):
                            show_message(["retrying", f"{attempt}/3..."])
                            if submit_dose(submit_qty):
                                success = True
                                break
                            flash_led(1, 150, 150)
                            time.sleep(0.3)
                        
                        if success:
                            show_message(["success"])
                            flash_led(3, 60, 60)
                            mode = MODE_DISPLAY
                            led.value(1)  # off
                            if info := fetch_last_dose(): last_info = info
                            alarm_grace_until_s = time.time() + 120
                        else:
                            show_message(["fail"])
                            flash_led(2, 200, 200)
                            time.sleep(1)
                            mode = MODE_DISPLAY
                            led.value(1)  # off
            
            # warning mode handling
            if mode == MODE_WARNING:
                if ms_since(last_warning_render_ms) >= 250:
                    last_warning_render_ms = time.ticks_ms()
                    show_message(["warning", warning_reason or "warning", "ignore"])
                
                if ms_since(last_warning_flash_ms) >= 100:
                    last_warning_flash_ms = time.ticks_ms()
                    led.value(1 - led.value())
                
                if ms_since(warning_end_ms) >= 0:
                    mode = MODE_DISPLAY
                    led.value(1)  # off
                    last_view_interaction_ms = time.ticks_ms()
            
            time.sleep(0.05)

if __name__ == "__main__":
    main()