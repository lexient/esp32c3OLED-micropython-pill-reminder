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


def connect_wifi(ssid, password, timeout_s=20):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        start = time.time()
        while not wlan.isconnected() and (time.time() - start) < timeout_s:
            show_message(["WiFi", "Loading"])
            time.sleep(1)
    return wlan.isconnected()


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
        r = requests.get(url, headers=headers)
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
        return None
    except Exception:
        return None
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


def main():
    show_message(["Booting"])

    if not connect_wifi(WIFI_SSID, WIFI_PASSWORD):
        show_message(["WiFi", "failed"])
        time.sleep(3)

    # Try NTP sync (optional but recommended for correct "ago")
    if not sync_time_via_ntp():
        # Proceed even if NTP fails; server time can still be shown as "unknown"
        pass

    last_info = fetch_last_dose()
    render_last_dose(last_info)

    last_fetch_ts = time.time()
    FETCH_INTERVAL_S = 60

    while True:
        now = time.time()
        # Periodically refresh from server
        if now - last_fetch_ts >= FETCH_INTERVAL_S:
            info = fetch_last_dose()
            if info:
                last_info = info
            last_fetch_ts = now
        # Update the "ago" text every second based on cached epoch
        render_last_dose(last_info)
        time.sleep(1)


if __name__ == "__main__":
    main()