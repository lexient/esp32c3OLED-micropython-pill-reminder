from machine import Pin, I2C
from ssd1306 import SSD1306_I2C

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


def main():
    lines = ["12345678", "12345678", "12345678", "12345678"]
    draw_box_with_lines(lines, align="center")


if __name__ == "__main__":
    main()