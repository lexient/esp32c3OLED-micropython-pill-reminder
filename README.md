## MicroPython Pill Timer
I'm a full stack web dev, and this is my first ever MicroPython project.

I'll be creating a web-based pill reminder that tracks doses using a postgres server.  

Aimed at other noobs (with some dev / python experience) that just want to get started with MicroPython.

### Introduction
I've dabbled with Arduino but never MicroPython. 

This is what I found out:

|  | Arduino | MicroPython |
|--------|---------|-------------|
| **Language** | C/C++ | Python |
| **Speed** | Fast | Slow |
| **Memory Usage** | Minimal | RAM hungry |

So it sucks, right? Kinda!

Do you know what else sucks? (from experience)
* Learning C/C++ 
* Debugging in Arduino
* Developing on a compiled language

MicroPython makes a tempting promise: build cool things without learning C/C++, see error messages in the shell WITHOUT 100s of print statements, and iterate quickly without the repeated compilation step. 


### How it works for noobs
When writing an Arduino sketch, your cursed C/C++ code is compiled and uploaded.

In MicroPython, your Python code runs on a Python interpreter that's already living on the microcontroller. 

### How to get started
This assumes you're using an ESP32 (since that's the only reasonable option)

**Setting up MicroPython**
1. [Download the latest MicroPython binary](https://micropython.org/download/ESP32_GENERIC_C3/) 
2. Run: `brew install minicom esptool`
3. `cd` to wheverever it's downloaded to
4. Hold down 'boot' and plug it in, then run: `ls /dev/tty.usbmodem*` to find the port (mine was /dev/tty.usbmodem3231301)
5. Erase the old image with: `esptool.py --chip esp32c3 --port [port from before] erase_flash`
6. Then flash the downloaded binary `esptool.py --chip esp32c3 --port [port from before] --baud 460800 write_flash -z 0x0 ESP32_GENERIC_C3-20250809-v1.26.0.bin`
7. Test with Minicom: `minicom -D [port from before] -b 115200` and just run some python `print("hello world")`

**Set up a new project and Move files to ESP32** (skipping over git and python setup for now)
1. Create a new project folder, set up git, and a venv, and `pip install mpremote`
2. Copy a file to the ESP32 and reset: `mpremote cp main.py : + reset`

**Adding the OLED driver**
1. `ssd1306.py` is included in this repo.
2. Copy it to the device: `mpremote cp ssd1306.py :`



#### Troubleshooting
**Issues with mpremote**
Run `mpremote connect [port from before]` to make sure its actually connected


### Hardware
ESP32-C3 with 0.42" OLED

![ESP32-C3 with OLED](images/esp32-c3-oled.png)

https://www.aliexpress.com/item/1005009045080441.html

This bad boy has Wifi and 4MB of RAM



#### This project is still in development
