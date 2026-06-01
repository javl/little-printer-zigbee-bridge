"""USB ESC/POS printer support for the Little Printer bridge."""

import asyncio
import logging
import os
import secrets
import struct
import subprocess
import sys
from dataclasses import dataclass

import usb.core
import usb.util

from . import config as cfg_module
from .claiming import generate_claim_code

log = logging.getLogger(__name__)

USB_CLASS_PRINTER = 0x07

# Known ESC/POS printer vendor IDs; many use vendor-specific USB class (0xff) instead of 0x07
_KNOWN_PRINTER_VIDS = {
    0x04b8,  # Epson
    0x0519,  # Star Micronics
    0x154f,  # SNBC
    0x0dd4,  # Custom S.p.A.
    0x1fc9,  # Woosim
    0x0e26,  # Bixolon
}


# DEFAULT_PAPER_WIDTH_PIXELS = 576  # 80mm paper at 203 DPI; override in config.json
DEFAULT_PAPER_WIDTH_PIXELS = 512  # 80mm paper at 203 DPI; override in config.json


@dataclass
class USBPrinterInfo:
    usb_key: str          # "04b8:0005"
    vendor_id: int
    product_id: int
    device_address: str   # random 8-byte BE hex, stable per device
    claim_code: str       # formatted "XXXX-XXXX-XXXX-XXXX"
    link_key: bytes       # stored after server sends add_key, unused for printing
    paper_width_pixels: int = DEFAULT_PAPER_WIDTH_PIXELS
    print_face: bool = False


def _is_printer_device(dev) -> bool:
    """Return True if the USB device is likely an ESC/POS printer."""
    if dev.bDeviceClass == USB_CLASS_PRINTER:
        return True
    if dev.idVendor in _KNOWN_PRINTER_VIDS:
        return True
    # Check interface classes (some printers report class at interface level)
    try:
        for cfg in dev:
            for intf in cfg:
                if intf.bInterfaceClass == USB_CLASS_PRINTER:
                    return True
    except Exception:
        pass
    return False


def discover_usb_printers() -> list[tuple[int, int]]:
    """Return list of (vendor_id, product_id) for connected USB printers."""
    found = []
    seen: set[tuple[int, int]] = set()
    try:
        devices = usb.core.find(find_all=True)
    except Exception as e:
        log.warning("USB device discovery failed: %s", e)
        return []
    for dev in (devices or []):
        if _is_printer_device(dev):
            key = (dev.idVendor, dev.idProduct)
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


def _udev_hint(vendor_id: int, product_id: int):
    log.warning(
        "USB printer %04x:%04x is not accessible (permission denied).\n"
        "Fix with a udev rule:\n\n"
        "  echo 'SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"%04x\", "
        "ATTRS{idProduct}==\"%04x\", MODE=\"0666\"' "
        "| sudo tee /etc/udev/rules.d/99-usb-printer.rules\n"
        "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
        "Then unplug and replug the printer.",
        vendor_id, product_id, vendor_id, product_id,
    )


def write_udev_rule(vendor_id: int, product_id: int) -> bool:
    """Write a udev rule granting access to the printer. Returns True on success."""
    if sys.platform != "linux":
        log.warning("--setup-udev only supported on Linux")
        return False

    rule = (
        f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vendor_id:04x}", '
        f'ATTRS{{idProduct}}=="{product_id:04x}", MODE="0666"\n'
    )
    rule_path = f"/etc/udev/rules.d/99-little-printer-usb-{vendor_id:04x}{product_id:04x}.rules"

    try:
        with open(rule_path, "w") as f:
            f.write(rule)
        log.info("Wrote udev rule: %s", rule_path)
    except PermissionError:
        log.error(
            "Cannot write %s: permission denied. Run as root or with sudo.", rule_path
        )
        return False
    except OSError as e:
        log.error("Failed to write udev rule %s: %s", rule_path, e)
        return False

    try:
        subprocess.run(["udevadm", "control", "--reload-rules"], check=True)
        subprocess.run(["udevadm", "trigger"], check=True)
        log.info("udev rules reloaded. Unplug and replug the USB printer.")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.warning("udevadm failed: %s. Reload rules manually or reboot.", e)
        return False

    return True


def check_udev_access(vendor_id: int, product_id: int) -> bool:
    """Return True if the printer is accessible. Logs a udev hint if not."""
    from escpos.printer import Usb as EscposUsb
    try:
        p = EscposUsb(vendor_id, product_id)
        p.close()
        return True
    except usb.core.USBError as e:
        if e.errno in (13, 1) or "permission" in str(e).lower() or "access" in str(e).lower():
            _udev_hint(vendor_id, product_id)
        else:
            log.warning("USB error checking %04x:%04x: %s", vendor_id, product_id, e)
        return False
    except Exception as e:
        log.debug("Access check for %04x:%04x: %s", vendor_id, product_id, e)
        # Unknown error; don't block on it
        return True


def get_or_create_usb_device(
    cfg: dict, vendor_id: int, product_id: int
) -> tuple[USBPrinterInfo, bool]:
    """Look up or create a stable identity for a USB printer.

    Returns (info, is_new) where is_new is True if the printer was first seen now.
    """
    usb_key = f"{vendor_id:04x}:{product_id:04x}"
    existing = cfg.get("usb_devices", {}).get(usb_key)
    if existing:
        return USBPrinterInfo(
            usb_key=usb_key,
            vendor_id=vendor_id,
            product_id=product_id,
            device_address=existing["device_address"],
            claim_code=existing["claim_code"],
            link_key=bytes.fromhex(existing.get("link_key", "")),
            paper_width_pixels=existing.get("paper_width_pixels", DEFAULT_PAPER_WIDTH_PIXELS),
        ), False

    # New printer: generate stable synthetic identity
    eui64_le = secrets.token_bytes(8)
    be_hex = eui64_le[::-1].hex()
    claim_code, link_key = generate_claim_code(eui64_le)

    entry = {
        "device_address": be_hex,
        "claim_code": claim_code,
        "link_key": link_key.hex(),
        "paper_width_pixels": DEFAULT_PAPER_WIDTH_PIXELS,
    }
    cfg_module.save_usb_device(cfg, usb_key, entry)

    return USBPrinterInfo(
        usb_key=usb_key,
        vendor_id=vendor_id,
        product_id=product_id,
        device_address=be_hex,
        claim_code=claim_code,
        link_key=link_key,
    ), True


def print_claim_slip(vendor_id: int, product_id: int, claim_code: str):
    """Print a claim slip directly on the USB printer showing the claim code."""
    from escpos.printer import Usb as EscposUsb
    try:
        p = EscposUsb(vendor_id, product_id)

        p.set(align="center", bold=False)
        p.text("New USB Printer found!\n\n")

        p.text("Visit ")

        p.set(align="center", bold=True, custom_size=True, width=2, height=2)
        p.text("://jaspervanloenen.com\n")

        p.set(align="center", bold=False, custom_size=True, width=1, height=1)
        p.text("and enter this claim code\nto link your printer:\n\n")

        p.set(align="center", bold=True, custom_size=True, width=5, height=5)
        p.text(f"{claim_code}\n\n\n")

        p.cut()
        p.close()
    except Exception as e:
        log.warning("Could not print claim slip on USB printer: %s", e)
        print(f"\nNew USB printer claim code: {claim_code}")


_FACE_IMAGE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "face_regular.png")


class USBPrinter:
    """Wraps a single USB ESC/POS printer for async print jobs."""

    def __init__(self, info: USBPrinterInfo):
        self.info = info
        self._lock = asyncio.Lock()

    async def print_lp_binary(self, binary: bytes):
        """Decode an LP thermal binary payload and print it via ESC/POS."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._print_sync, binary)

    def _scale(self, im, paper_width: int):
        from PIL import Image
        if im.width == paper_width:
            return im
        new_height = round(im.height * paper_width / im.width)
        return im.resize((paper_width, new_height), Image.Resampling.LANCZOS)

    def _print_sync(self, binary: bytes):
        from escpos.printer import Usb as EscposUsb
        from .image_encoding import lp_binary_to_pil, load_image
        from .protocol import CMD_SET_DELIVERY_AND_PRINT

        # Command ID at offset 2 distinguishes face/no-face variants
        command_id = struct.unpack_from("<H", binary, 2)[0]
        print("command id: ", command_id, CMD_SET_DELIVERY_AND_PRINT)
        show_face = command_id == CMD_SET_DELIVERY_AND_PRINT

        paper_width = self.info.paper_width_pixels
        im = self._scale(lp_binary_to_pil(binary), paper_width)
        p = EscposUsb(self.info.vendor_id, self.info.product_id)
        p.profile.profile_data['media']['width']['pixels'] = paper_width
        try:
            p.image(im, impl="bitImageColumn", center=False)
            p.cut()
            print("show face: ", show_face)
            if show_face:
                if os.path.exists(_FACE_IMAGE_PATH):
                    face_im = self._scale(load_image(_FACE_IMAGE_PATH), paper_width)
                    p.image(face_im, impl="bitImageColumn", center=False)
                    p.ln(8)
                else:
                    log.warning("show_face requested but face_regular.png not found")
        finally:
            p.close()


def setup_usb_printers(cfg: dict, setup_udev: bool = False) -> dict[str, USBPrinter]:
    """Discover USB printers, register new ones, return {device_address: USBPrinter}."""
    found = discover_usb_printers()
    result: dict[str, USBPrinter] = {}

    for vendor_id, product_id in found:
        usb_key = f"{vendor_id:04x}:{product_id:04x}"
        accessible = check_udev_access(vendor_id, product_id)

        if not accessible and setup_udev:
            log.info("Attempting to write udev rule for %s ...", usb_key)
            if write_udev_rule(vendor_id, product_id):
                log.info("udev rule written. Restart the service after replugging the printer.")
            accessible = False  # still inaccessible until replug

        info, is_new = get_or_create_usb_device(cfg, vendor_id, product_id)

        if is_new:
            log.info("New USB printer %s - claim code: %s", usb_key, info.claim_code)
            if accessible:
                print_claim_slip(vendor_id, product_id, info.claim_code)
            else:
                log.info("New USB printer claim code: %s", info.claim_code)
                log.info("(Could not print claim slip - fix udev permissions first)")
        else:
            log.info(
                "Known USB printer %s (device_address=%s)", usb_key, info.device_address
            )

        result[info.device_address] = USBPrinter(info)

    if not found:
        log.info("No USB printers found")

    return result
