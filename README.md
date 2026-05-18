# Little Printer Zigbee Bridge

![Little Printer and a custom bridge device](https://github.com/javl/little-printer-zigbee-bridge/blob/main/header.jpg?raw=true)

Replace your Little Printer's original bridge device with a Python script and EZSP USB Zigbee dongle. Especially useful if your original bridge has become corrupted (as many have over the years).

You can control the Little Printer from your computer, or simply use a Raspberry Pi to fully replace the bridge. Flash the image, update the network settings and you're ready to print! Full instructions [over here](https://github.com/javl/little-printer-zigbee-bridge/wiki/Install-on-Raspberry-Pi-for-use-with-Sirius)

Tested on Linux and Windows using a Sonoff ZBDongle-E. Feel free to share issues or comments if you've tested it working on other systems.

## note
I've also been working on a different bridge alternative (ESP32 based) which uses even cheaper hardware and is truely plug and play. So unless you want to play around with this code yourself you might want to wait for a little bit longer to see if this alternative will suit you better!

## License
In the spirit of open source this project is shared under a GNU GPLv3 license. This means you can use it pretty much in any way you like (including commercially) as long as you give proper attribution and share any changes you make. If you do make any changes that might benefit others, please share them here as a pull request as well, to prevent too many fractured versions of this code.

## Support
Did you find this tool useful? Feel free to support my open source tools - especially when using them commercially:

[![GitHub Sponsor](https://img.shields.io/badge/_-sponsor_on_Github-blue?logo=github)](https://github.com/sponsors/javl) [![BMC](https://img.shields.io/badge/Buy_Me_a_Coffee-orange?logo=buymeacoffee)](https://www.buymeacoffee.com/javl)

---

- [Little Printer Zigbee Bridge](#little-printer-zigbee-bridge)
  - [License](#license)
  - [Support](#support)
  - [Installation](#installation)
  - [Get claim code](#get-claim-code)
  - [Option 1. Connect to Sirius](#option-1-connect-to-sirius)
  - [Option 2. Local server and design tool](#option-2-local-server-and-design-tool)
  - [Option 3. Print from commandline](#option-3-print-from-commandline)
    - [Arguments](#arguments)
    - [CLI Examples](#cli-examples)
  - [Faces / personality](#faces--personality)
  - [config.json](#configjson)
  - [Thanks](#thanks)
  - [Random Error Fixes / Notes](#random-error-fixes--notes)

---

## Installation

Below are the instructions on running the code on your own system for use as a commandline tool.

If all you want to do is get a working bridge you can ignore this section: check out [these instructions on the wiki](https://github.com/javl/little-printer-zigbee-bridge/wiki/Install-on-Raspberry-Pi) on flashing the code to a Raspberry Pi with minumal effort instead.

---

You'll need Python 3 installed. I suggest creating a virtual environment to keep dependencies isolated:

```bash
git clone git@github.com:javl/little-printer-zigbee-bridge.git
cd little-printer-zigbee-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r bridge/requirements.txt
```

Connecting a Little Printer to your bridge takes a few steps:

1. Start the server (see options below)
2. Power on the Little Printer and get its claim code (see [Get claim code](#get-claim-code))
3. Enter the claim code on the commandline or via the Sirius website
4. Print!

> **Note:** On first run the script creates a `config.json` file and sets the device port to `/dev/ttyUSB0` on Unix or `COM3` on Windows. Update this value to the actual port used if needed and restart the script

From here there are three options:

1. Use with my new server
2. Run with the old Sirius server
3. Print directly from the commandline

---

## Get claim code

1. Plug in your Little Printer.
2. Open it up and use something like a paperclip to press the button on the inside. Hold it until the light turns off.
3. Unplug the power adapter, put the paper back, and close the printer.
4. Power the printer again. Once it detects your network the LED on top will change. Press the button on top of the printer to print the claim code.

---

## Option 1. Connect to the new server

Connect the Zigbee bridge to the new server by passing the `--lp-server` flag, and optionally `--lp-server-url`. :

```bash
python3 -m bridge.main --lp-sever --lp-server-url wss://littleprinter.jaspervanloenen.com/api/v1/connection
```
Then visit the server in your browser at [littleprinter.jaspervanloenen.com](https://littleprinter.jaspervanloenen.com)

## Option 2. Connect to Sirius

Connect the Zigbee bridge to Sirius by passing the `--sirius` flag. By default it connects to the [Nord Projects' Sirius instance](https://littleprinter.nordprojects.co/):

```bash
python3 -m bridge.main --sirius
```

## Option 3. Local server and design tool

This project includes a simple tool for creating receipts to print. Add text or image blocks using the buttons on the bottom left, then press the print button on the top right.

![Simple receipt design tool](https://github.com/javl/little-printer-zigbee-bridge/blob/main/receipt-tool.jpg?raw=true)

Start the server:

```bash
python3 -m bridge.main --serve
```

The server URL is printed on the commandline (default: [http://127.0.0.1:8080/](http://127.0.0.1:8080/)). Open it in your browser to visit the design tool.

## Option 3. Print from commandline

Run the bridge directly from the commandline with various arguments.

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--image PATH` | - | Image file to print |
| `--text TEXT` | - | Text to print (mutually exclusive with `--image`) |
| `--personality` | - | Updates faces with images from `faces` directory |
| `--face_dir PATH` | - | Directory to get faces from. Defaults to `faces` |
| `--no-face` | face on | Skip printing the face after the message |
| `--max-height PX` | - | Cap image height in pixels before encoding |
| `--no-dither` | dithering on | Disable Floyd-Steinberg dithering |
| `--port PORT` | from config | Serial port of the EZSP dongle (e.g. `/dev/ttyUSB0`) |
| `--baud RATE` | from config | Baud rate for the serial port |
| `--once` | - | Exit after printing instead of staying alive for heartbeats |
| `--serve` | - | Run as a persistent HTTP server |
| `--host HOST` | `127.0.0.1` | Bind address for the HTTP server |
| `--http-port PORT` | `8080` | Port for the HTTP server |
| `--to-image` | - | Render to `print.jpg` instead of sending to printer (skips Zigbee) |
| `--sirius` | - | Connect to a Sirius server as a Berg bridge client |
| `--sirius-server URL` | Nord Projects instance | WebSocket URL of the Sirius server |
| `--debug` | - | Enable DEBUG-level logging |

### CLI Examples

```bash
# Print an image:
python3 -m bridge.main --image photo.jpg

# Print text:
python3 -m bridge.main --text "Hello, World"

# Parse image and save as print.jpg without sending to printer (for testing):
python3 -m bridge.main --image photo.jpg --to-image

# Print an image without dithering:
python3 -m bridge.main --image photo.jpg --no-dither

# Override serial port:
python3 -m bridge.main --port /dev/ttyUSB1 --text "test"

# Exit after printing instead of staying alive for heartbeats:
python3 -m bridge.main --text "test" --once
```

---

## Faces / personality

Originally the Little Printer had a "personality": its face would change over time (hair would grow, it would get glasses, etc.). You can update the personality (the face printed at the end of each delivery) plus three status images (`nothing to print`, `can't see bridge`, `can't see internet`). These images will be used until you send new ones, or simply power off the printer.

Use `--face PATH` to update the personality and status images, where `PATH` is a directory containing four images:

- personality.png
- nothing_to_print.png
- no_bridge.png
- no_internet.png

These images can have any height you want, but if you want to match the original files `personality.png` is 328 x 492 pixels, and the other three files are 328 x 200 pixels.

---

## config.json

Generated automatically on first run:

| Field | Description |
|---|---|
| `ezsp_port` | Serial port of the EZSP dongle (e.g. `/dev/ttyUSB0`) |
| `ezsp_baud` | Baud rate (typically 115200) |
| `channel` | Zigbee channel (one of 11, 14, 15, 19, 20, 24, 25) |
| `extended_pan_id` | 8-byte hex. First 4 bytes are always `42455247` ("BERG") - the printer scans for this prefix |
| `network_key` | 16-byte hex AES network key, randomly generated |
| `print_id` | Auto-incrementing counter used to match print confirmations |
| `devices` | Dict of EUI64 → `{claim_code, link_key}` for each paired printer |

> **Note:** Do not change `extended_pan_id` or `network_key` after a printer has been paired. The printer will need to be re-paired.

---

## Thanks

- Thanks to [BERG](https://berglondon.com/projects/) for creating the Little Printer in the first place
- Huge thanks to [Nord Projects](https://nordprojects.com) for reviving the cloud service, providing instructions for updating the bridge device, and creating a new mobile app
- Anyone who donated to support my open source projects


---

## Random Error Fixes / Notes
- `FileNotFoundError: [Errno 2] No such file or directory: '/dev/ttyUSB0'`

    This means your dongle wasn't detected at the given port. Make sure it is plugged in and update the port in `config.json` if needed.
- On Windows you'll need some drivers for the Sonos Zigbee dongle:

    1. Download `CP210x Universal Windows Driver` from [over here](https://www.silabs.com/software-and-tools/usb-to-uart-bridge-vcp-drivers?tab=downloads).
    2. Extract the `.zip` file somewhere on your system
    3. Open Device Manager, right click your dongle in the list and select `Update Driver`. Select the directory you extracted the driver to.
    4. Find the Sonoff device under the `ports` section and note what com port it uses (like `COM3`) and update `bridge/config.json` accordingly (or pass `--port COMx` to the script)

- `ImportError: libopenjp2.so.7: cannot open shared object file: No such file or directory`

        Install the the missing module: `sudo apt-get install libopenjp2-7`
