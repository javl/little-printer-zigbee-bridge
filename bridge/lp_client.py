import asyncio
import base64
import json
import logging
import struct

import websockets

from . import config as cfg_module
from .protocol import split_into_blocks

log = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "wss://littleprinter.jaspervanloenen.com/api/v1/connection"
SUBPROTOCOL = "little-printer-v1"

_EVENT_HEARTBEAT = 0x0001
_EVENT_DID_PRINT = 0x0002


def _eui64_to_be(eui64_hex: str) -> str:
    """LE eui64_hex (from bellows) → BE hex string for the server."""
    return bytes.fromhex(eui64_hex)[::-1].hex()


def _be_to_eui64(be_addr: str) -> str:
    """BE hex string from server → LE eui64_hex for bellows."""
    return bytes.fromhex(be_addr)[::-1].hex()


class LPClient:
    def __init__(self, bridge, cfg: dict, server_url: str = DEFAULT_SERVER_URL,
                 usb_printers: dict | None = None):
        self._bridge = bridge
        self._cfg = cfg
        self._bridge_address = cfg.get("extended_pan_id", "0000000000000000")
        self._server_url = server_url
        self._ws = None
        self._usb_printers: dict = usb_printers or {}

    # --- Public API (called from main.py join_loop) ---

    async def send_encryption_key_required(self, device_address: str):
        await self._send({
            "type": "printer_join_request",
            "bridge_address": self._bridge_address,
            "device_address": device_address,
        })
        log.info("→ printer_join_request for %s", device_address)

    async def send_device_connect(self, device_address: str):
        await self._send({
            "type": "printer_connected",
            "bridge_address": self._bridge_address,
            "device_address": device_address,
        })
        log.info("→ printer_connected for %s", device_address)

    # --- Connection and receive loop ---

    async def connect(self):
        log.info("Connecting to server at %s", self._server_url)
        self._ws = await websockets.connect(
            self._server_url,
            subprotocols=[SUBPROTOCOL],
        )
        await self._send_bridge_online()
        if self._bridge:
            self._bridge.on_printer_event = self._on_printer_event
        log.info("Connected and bridge_online sent.")

    async def receive_forever(self):
        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                log.warning("Non-JSON from server: %s", exc)
                continue

            msg_type = data.get("type")
            command_id = data.get("command_id")

            if msg_type == "add_key":
                await self._handle_add_key(data, command_id)
            elif msg_type == "print":
                asyncio.get_event_loop().create_task(
                    self._handle_print(data, command_id)
                )
            else:
                log.info("Unhandled server message type: %s", msg_type)

    # --- Internal ---

    async def _send(self, msg: dict):
        await self._ws.send(json.dumps(msg))

    async def _send_bridge_online(self):
        await self._send({
            "type": "bridge_online",
            "bridge_address": self._bridge_address,
            "firmware_version": "1.0.0",
        })

    def _on_printer_event(self, eui64_hex, event_code: int, payload: bytes):
        if eui64_hex is None:
            return
        asyncio.get_event_loop().create_task(
            self._forward_device_event(eui64_hex, event_code, payload)
        )

    async def _forward_device_event(self, eui64_hex: str, event_code: int, payload: bytes):
        device_address = _eui64_to_be(eui64_hex)

        if event_code == _EVENT_HEARTBEAT:
            uptime = struct.unpack_from("<I", payload, 10)[0] if len(payload) >= 14 else 0
            binary = struct.pack("<HII", _EVENT_HEARTBEAT, 0, 4) + struct.pack("<I", uptime)
        elif event_code == _EVENT_DID_PRINT:
            if len(payload) >= 15:
                print_type = payload[10]
                print_id = struct.unpack_from("<I", payload, 11)[0]
            else:
                print_type, print_id = 0x01, 0
            binary = struct.pack("<HII", _EVENT_DID_PRINT, print_id, 5) + struct.pack("<BI", print_type, print_id)
        else:
            return

        try:
            await self._send({
                "type": "printer_event",
                "bridge_address": self._bridge_address,
                "device_address": device_address,
                "payload": base64.b64encode(binary).decode(),
            })
        except Exception as exc:
            log.warning("Failed to forward device event: %s", exc)

    async def _handle_add_key(self, data: dict, command_id):
        be_addr = data["device_address"]
        key = base64.b64decode(data["key"])

        if be_addr in self._usb_printers:
            log.info("← add_key for USB printer %s (not installing in Zigbee)", be_addr)
            for entry in self._cfg.get("usb_devices", {}).values():
                if entry.get("device_address") == be_addr:
                    entry["link_key"] = key.hex()
                    cfg_module.save(self._cfg)
                    break
            await self._send({"type": "key_ack", "command_id": command_id, "success": True})
            return

        eui64_le = bytes.fromhex(_be_to_eui64(be_addr))
        log.info("← add_key for %s", be_addr)
        if self._bridge:
            await self._bridge.install_link_key(eui64_le, key)
        eui64_hex = _be_to_eui64(be_addr)
        self._cfg["devices"][eui64_hex] = {"link_key": key.hex()}
        cfg_module.save(self._cfg)
        log.info("Saved device %s to config", eui64_hex)
        await self._send({
            "type": "key_ack",
            "command_id": command_id,
            "success": True,
        })

    async def _handle_print(self, data: dict, command_id):
        be_addr = data["device_address"]
        binary = base64.b64decode(data["payload"])
        rotate_180 = data.get("rotate_180", False)

        if be_addr in self._usb_printers:
            usb_printer = self._usb_printers[be_addr]
            log.info("← print (cmd_id=%s) for USB printer %s", command_id, be_addr)
            try:
                async with usb_printer._lock:
                    await usb_printer.print_lp_binary(binary, rotate_180=rotate_180)
                success = True
                asyncio.get_event_loop().create_task(self._send_usb_did_print(be_addr))
            except Exception as exc:
                log.error("USB print failed: %s", exc)
                success = False
            await self._send({"type": "print_ack", "command_id": command_id, "success": success})
            return

        eui64_hex = _be_to_eui64(be_addr)
        blocks = split_into_blocks(binary)
        log.info("← print (cmd_id=%s) for %s - %d block(s)", command_id, be_addr, len(blocks))
        try:
            await self._bridge.send_print_job(eui64_hex, blocks)
            success = True
        except Exception as exc:
            log.error("Print job failed: %s", exc)
            success = False
        await self._send({
            "type": "print_ack",
            "command_id": command_id,
            "success": success,
        })

    async def _send_usb_did_print(self, be_addr: str):
        binary = struct.pack("<HII", _EVENT_DID_PRINT, 0, 5) + struct.pack("<BI", 0x01, 0)
        try:
            await self._send({
                "type": "printer_event",
                "bridge_address": self._bridge_address,
                "device_address": be_addr,
                "payload": base64.b64encode(binary).decode(),
            })
        except Exception as exc:
            log.warning("Failed to send USB did_print event: %s", exc)
