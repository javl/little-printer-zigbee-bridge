"""
Little Printer bridge. Runs on Linux, connects via EZSP USB dongle.

Example usage:
    python -m bridge.main [--image PATH | --text "hello"] [--port /dev/ttyUSB0]

See README.md in top directory for all arguments.

On first run the printer will print a claim code. Enter it when prompted.
All state (network key, link keys, etc.) is saved to bridge/config.json.
"""

import argparse
import asyncio
import logging
import sys

from PIL import Image

from . import config as cfg_module
from .claiming import link_key_from_claim_code, hardware_xor_from_eui64, InvalidClaimCode
from .protocol import prepare_print_job, prepare_personality_job
from .server import make_app, run_server
from .sirius_client import SiriusClient, DEFAULT_SIRIUS_SERVER_URL
from .lp_client import LPClient, DEFAULT_SERVER_URL
from .zigbee import LittlePrinterBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

DENY_JOIN = 0x02


def _apply_new_network_params(cfg):
    cfg_module.new_network_params(cfg)
    cfg_module.save(cfg)
    log.info("New network params: EPAN=%s", cfg["extended_pan_id"])


def save_to_image(args):
    from .image_encoding import load_image, text_to_image, prepare_image

    if args.image:
        img = load_image(args.image, max_height=args.max_height)
    elif args.text:
        img = text_to_image(args.text)
    else:
        print("Error: --to-image requires --image or --text", file=sys.stderr)
        sys.exit(1)

    prepare_image(img, dither=args.no_dither).convert("L").save("print.jpg")
    print("Saved to print.jpg")


async def handle_join(bridge: LittlePrinterBridge, event, cfg: dict):
    """Process a printer join event; prompt for claim code if new device."""
    eui64_hex = event.eui64_hex
    known = cfg["devices"].get(eui64_hex)
    accepted = event.policy_decision != DENY_JOIN

    if accepted:
        bridge.register_short_addr(eui64_hex, event.node_id)
        if known:
            log.info("Known printer joined: %s (short addr 0x%04x)", eui64_hex, event.node_id)
            return
        # Key is in the NCP's NVRAM but config was cleared: prompt to save the entry.
        print(f"\nPrinter {eui64_hex} joined (key already in NCP) but is missing from config.")
        print("Enter its claim code to save it (dashes optional):\n")
    else:
        # Join was denied: no key for this printer yet.
        # The printer will have printed its claim code by now.
        if known and "link_key" in known:
            # Key is in config but not in NCP (e.g. new dongle). Reinstall and let it retry.
            log.info("Reinstalling key for known printer %s", eui64_hex)
            await bridge.install_link_key(event.eui64_le, bytes.fromhex(known["link_key"]))
            return
        print(f"\nNew printer detected (EUI64: {eui64_hex})")
        print("Press the button on the printer to have it print its claim code")
        print("Enter it below (dashes optional, e.g. XXXX-XXXX-XXXX-XXXX):\n")

    while True:
        try:
            raw = input("Claim code: ").strip()
        except EOFError:
            log.error("No input available; cannot pair printer")
            return

        try:
            link_key, hardware_xor = link_key_from_claim_code(raw)
        except InvalidClaimCode as e:
            print(f"  Invalid: {e}. Try again.")
            continue

        expected_xor = hardware_xor_from_eui64(event.eui64_le)
        if hardware_xor != expected_xor:
            print(
                f"  Claim code does not match this printer's EUI64.\n"
                f"  (got xor 0x{hardware_xor:06x}, expected 0x{expected_xor:06x})\n"
                f"  Make sure you're entering the code from THIS printer."
            )
            continue

        break

    if not accepted:
        await bridge.install_link_key(event.eui64_le, link_key)

    cfg["devices"][eui64_hex] = {
        "claim_code": raw,
        "link_key": link_key.hex(),
    }
    cfg_module.save(cfg)
    log.info("Printer %s paired and saved to config", eui64_hex)
    print(f"\nPaired! Config saved.\n")

async def run(args):
    cfg = cfg_module.load()

    # Allow CLI to override serial port
    if args.port:
        cfg["ezsp_port"] = args.port
    if args.baud:
        cfg["ezsp_baud"] = args.baud

    if args.new_network:
        _apply_new_network_params(cfg)
    bridge = LittlePrinterBridge(cfg)
    try:
        await bridge.start(force_new_network=args.new_network)
        await bridge.preinstall_known_keys(cfg["devices"])

        # Bridge is now running. Rest of this method contains various modes of
        # operation (run webserver, direct print, etc.) which can be triggered
        # via CLI args.
        print_target: dict | None = None
        if args.image or args.text or args.personality:
            print_target = {
                "image": args.image,
                "text": args.text,
                "personality": args.personality,
                "faces_dir": args.faces_dir,
                "no_face": args.no_face,
                "max_height": args.max_height,
                "dither": args.no_dither
            }

        # If we already know a printer and have its short address mapped, skip join wait.
        # Otherwise, wait for it to join (or rejoin).
        known_eui64 = _find_paired_printer(cfg)

        if known_eui64:
            # Printer is in config: wait for it to show up (join or heartbeat), then print.
            log.info("Waiting for printer %s to be reachable...", known_eui64)
            await bridge.wait_for_printer_reachable(known_eui64)
            if print_target:
                await _do_print(bridge, known_eui64, print_target, cfg)
            if not args.once:
                await _run_forever(bridge, cfg)
            return

        # No known printer: wait for one to join and go through the claim code flow.
        # Flow for new printer:
        #   1st event: DENY_JOIN  → handle_join installs key, loops back
        #   2nd event: ACCEPTED   → printer is ready
        target_eui64: str | None = None

        while target_eui64 is None:
            log.info("Waiting for printer to join...")
            event = await bridge.wait_for_join()
            await handle_join(bridge, event, cfg)
            if event.policy_decision != DENY_JOIN:
                target_eui64 = event.eui64_hex

        if print_target:
            await _do_print(bridge, target_eui64, print_target, cfg)

        if not args.once:
            await _run_forever(bridge, cfg)
    finally:
        await bridge.stop()


async def _do_print(bridge: LittlePrinterBridge, eui64_hex: str, target: dict, cfg: dict):
    personality = target.get("personality", False)

    if personality:
        face_path_directory = target.get("faces_dir") or "faces"
        print_id = cfg_module.next_print_id(cfg)
        log.info("Sending personality (face: %s, id=%d)...", face_path_directory, print_id)
        try:
            blocks = prepare_personality_job(face_path_directory, print_id)
        except Exception as exc:
            log.error("Failed to prepare personality: %s", exc)
            return
        try:
            await bridge.send_print_job(eui64_hex, blocks)
        except Exception as exc:
            log.error("Personality send failed: %s", exc)
            return

    if not target.get("image") and not target.get("text"):
        return

    print_id = cfg_module.next_print_id(cfg)
    log.info("Preparing print job (id=%d)...", print_id)

    try:
        blocks = prepare_print_job(
            image_path=target.get("image"),
            text=target.get("text"),
            print_id=print_id,
            no_face=bool(target.get("no_face")),
            max_height=target.get("max_height"),
            dither=target.get("dither", False),
        )
    except Exception as exc:
        log.error("Failed to prepare print job: %s", exc)
        return

    log.info("Sending %d block(s) to printer %s...", len(blocks), eui64_hex)
    try:
        await bridge.send_print_job(eui64_hex, blocks)
    except Exception as exc:
        log.error("Print job failed: %s", exc, exc_info=True)


async def _run_forever(bridge: LittlePrinterBridge, cfg: dict):
    log.info("Bridge running. Press Ctrl+C to stop.")
    try:
        while True:
            event = await bridge.wait_for_join()
            await handle_join(bridge, event, cfg)
    except asyncio.CancelledError:
        pass


async def clear_devices_mode(args):
    """ Clear all paired devices from NCP key table and config, then exit."""
    cfg = cfg_module.load()
    if args.port:
        cfg["ezsp_port"] = args.port
    if args.baud:
        cfg["ezsp_baud"] = args.baud

    if args.new_network:
        _apply_new_network_params(cfg)
    bridge = LittlePrinterBridge(cfg)
    try:
        await bridge.start(force_new_network=args.new_network)
        ok = await bridge.clear_link_keys()
        if ok:
            cfg["devices"] = {}
            cfg_module.save(cfg)
            print("Cleared all devices from NCP key table and config.")
        else:
            print("Failed to clear NCP key table; config not modified.", file=sys.stderr)
    finally:
        await bridge.stop()


async def serve_mode(args):
    """ Run in HTTP server mode, allowing prints to be sent via HTTP and monitoring printer joins. """
    cfg = cfg_module.load()
    if args.port:
        cfg["ezsp_port"] = args.port
    if args.baud:
        cfg["ezsp_baud"] = args.baud

    if args.new_network:
        _apply_new_network_params(cfg)
    bridge = LittlePrinterBridge(cfg)
    try:
        await bridge.start(force_new_network=args.new_network)
        await bridge.preinstall_known_keys(cfg["devices"])

        async def join_loop():
            while True:
                event = await bridge.wait_for_join()
                await handle_join(bridge, event, cfg)

        print_lock = asyncio.Lock()
        app = make_app(bridge, cfg, print_lock)

        log.info("Serving. Send prints to http://%s:%d/print", args.host, args.http_port)
        await asyncio.gather(join_loop(), run_server(app, args.host, args.http_port))
    finally:
        await bridge.stop()

async def run_sirius(args):
    """ Run in Sirius mode, connecting to a Nord server as a Berg bridge client.
    This is legacy mode for compatibility with the original Sirius implementation"""
    cfg = cfg_module.load()
    if args.port:
        cfg["ezsp_port"] = args.port
    if args.baud:
        cfg["ezsp_baud"] = args.baud

    if args.new_network:
        _apply_new_network_params(cfg)
    bridge = LittlePrinterBridge(cfg)
    try:
        await bridge.start(force_new_network=args.new_network)
        await bridge.preinstall_known_keys(cfg["devices"])

        server_url = args.sirius_server_url
        sirius = SiriusClient(bridge, cfg, server_url)
        await sirius.connect()

        async def join_loop():
            while True:
                event = await bridge.wait_for_join()
                device_address = event.eui64_le[::-1].hex()  # BE for sirius
                if event.policy_decision == DENY_JOIN:
                    await sirius.send_encryption_key_required(device_address)
                else:
                    await sirius.send_device_connect(device_address)

        log.info("Sirius mode running. Waiting for printer and Nord server commands.")
        try:
            await asyncio.gather(join_loop(), sirius.receive_forever())
        except asyncio.CancelledError:
            pass
    finally:
        await bridge.stop()

async def run_lp_server(args):
    """ Run in the new server mode, connecting to our more modern server """
    cfg = cfg_module.load()
    if args.port:
        cfg["ezsp_port"] = args.port
    if args.baud:
        cfg["ezsp_baud"] = args.baud

    if args.new_network:
        _apply_new_network_params(cfg)

    usb_printers: dict = {}
    if not args.no_usb:
        from .usb_printer import setup_usb_printers
        usb_printers = setup_usb_printers(cfg)

    bridge = None
    if not args.no_zigbee:
        bridge = LittlePrinterBridge(cfg)

    try:
        if bridge is not None:
            await bridge.start(force_new_network=args.new_network)
            await bridge.preinstall_known_keys(cfg["devices"])

        server_url = args.lp_server_url
        lp_server = LPClient(bridge, cfg, server_url, usb_printers=usb_printers)

        join_queue: asyncio.Queue = asyncio.Queue()

        async def join_loop():
            while True:
                event = await bridge.wait_for_join()
                await join_queue.put(event)

        async def drain_join_queue():
            while True:
                event = await join_queue.get()
                device_address = event.eui64_le[::-1].hex()  # BE for server
                if event.policy_decision == DENY_JOIN:
                    await lp_server.send_encryption_key_required(device_address)
                else:
                    await lp_server.send_device_connect(device_address)

        async def server_loop():
            delay = 1.0
            while True:
                try:
                    await lp_server.connect()
                    delay = 1.0
                    # Announce USB printers on each (re)connect so the server
                    # (re)registers them after any connection drop
                    for device_address in usb_printers:
                        await lp_server.send_encryption_key_required(device_address)
                    if bridge is not None:
                        await asyncio.gather(drain_join_queue(), lp_server.receive_forever())
                    else:
                        await lp_server.receive_forever()
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    log.warning("Server connection lost: %s - reconnecting in %.0fs", exc, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)

        log.info("lp_server mode running. Waiting for printer and server commands.")
        tasks = [server_loop()]
        if bridge is not None:
            tasks.append(join_loop())
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
    finally:
        if bridge is not None:
            await bridge.stop()


def _find_paired_printer(cfg: dict) -> str | None:
    devices = cfg.get("devices", {})
    if devices:
        return next(iter(devices))
    return None


def main():
    parser = argparse.ArgumentParser(description="Little Printer bridge")
    parser.add_argument("--image", metavar="PATH", help="Image file to print")
    parser.add_argument("--text", metavar="TEXT", help="Text to print")
    parser.add_argument("--faces_dir", metavar="PATH", help="Directory containing face images")
    parser.add_argument("--personality", action="store_true", help="Update the personality face")
    parser.add_argument("--no-face", action="store_false", help="Do not show the face after printing")
    parser.add_argument("--max-height", type=int, metavar="PX", help="Cap image height (pixels) before encoding")
    parser.add_argument("--no-dither", action="store_false", help="Disable Floyd-Steinberg dithering before encoding")
    parser.add_argument("--port", help="Serial port (default: from config)")
    parser.add_argument("--baud", type=int, help="Baud rate (default: from config)")
    parser.add_argument("--once", action="store_true", help="Exit after printing")
    parser.add_argument("--serve", action="store_true", help="Run as persistent HTTP server")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default: 127.0.0.1)")
    parser.add_argument("--http-port", type=int, default=8080, metavar="PORT",
                        help="HTTP port (default: 8080)")
    parser.add_argument("--to-image", action="store_true", help="Write the result to print.jpg instead of sending to printer, useful for debugging without a Zigbee module or printer")
    parser.add_argument("--lp-server-url", metavar="URL", default=DEFAULT_SERVER_URL, help="URL of the LP server to connect to")
    parser.add_argument("--sirius", action="store_true", help="Connect to Nord server (Sirius) as a Berg bridge client")
    parser.add_argument("--sirius-server-url", metavar="URL", default=DEFAULT_SIRIUS_SERVER_URL,
                        help=f"Nord server WebSocket URL (default: {DEFAULT_SIRIUS_SERVER_URL})")
    parser.add_argument("--no-usb", action="store_true", help="Disable USB ESC/POS printer discovery and printing")
    parser.add_argument("--no-zigbee", action="store_true", help="Skip Zigbee init (for USB-only setups without a Zigbee dongle)")
    parser.add_argument("--clear-devices", action="store_true", help="Remove all paired devices from NCP key table and config, then exit")
    parser.add_argument("--new-network", action="store_true", help="Discard stored network and form a new one with fresh EPAN and key")
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.image and args.text:
        print("Error: use --image OR --text, not both", file=sys.stderr)
        sys.exit(1)

    if args.to_image:
        save_to_image(args)
        return

    try:
        if args.clear_devices:
            asyncio.run(clear_devices_mode(args))
        elif args.serve:
            asyncio.run(serve_mode(args))
        elif args.sirius:  # legacy sirius mode
            asyncio.run(run_sirius(args))
        else: # run in default lp-server mode
            asyncio.run(run_lp_server(args))
        # else:
        #     asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
