PORT ?= $(shell ls /dev/tty.usbmodem* 2>/dev/null | head -n1)

.PHONY: deploy monitor flash reset

deploy:
	@echo "Deploying files to device on $(PORT)"
	mpremote cp main.py :
	mpremote cp ssd1306.py :
	mpremote cp settings.json :
	- mpremote cp secrets.py : || true
	mpremote reset

monitor:
	mpremote repl

flash:
	@echo "Flash MicroPython to the ESP32-C3 (example commands):"
	@echo "esptool.py --chip esp32c3 --port $(PORT) erase_flash"
	@echo "esptool.py --chip esp32c3 --port $(PORT) --baud 460800 write_flash -z 0x0 ESP32_GENERIC_C3-<version>.bin"

reset:
	mpremote reset
