#!/usr/bin/env python3
"""Fake Little Printer — connects to LP server via WebSocket for local testing."""

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import pathlib
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import websockets

from PIL import Image

from bridge.claiming import generate_claim_code
from bridge.config import load as load_config, save as save_config
from bridge.image_encoding import lp_binary_to_pil, PRINT_WIDTH
from bridge.lp_client import DEFAULT_SERVER_URL, SUBPROTOCOL
from bridge.protocol import CMD_SET_DELIVERY_AND_PRINT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_DEVICE_ID = "deadbeef00000001"
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).parent / "bridge" / "config.json"
FACE_IMAGE_PATH = pathlib.Path(__file__).parent / "face_regular.png"

HEARTBEAT_INTERVAL = 30
RECONNECT_DELAY_MIN = 2
RECONNECT_DELAY_MAX = 60

_EVENT_HEARTBEAT = 0x0001
_EVENT_DID_PRINT = 0x0002


def _eui64_le_to_be(eui64_hex: str) -> str:
    return bytes.fromhex(eui64_hex)[::-1].hex()


def _fake_bridge_address(eui64_le: bytes) -> str:
    prefix = hashlib.sha256(eui64_le).digest()[:4].hex()
    return prefix + "47524542"  # BERG suffix matches real bridges


def _heartbeat_binary(uptime_s: int) -> bytes:
    return struct.pack("<HII", _EVENT_HEARTBEAT, 0, 4) + struct.pack("<I", uptime_s)


def _did_print_binary() -> bytes:
    return struct.pack("<HII", _EVENT_DID_PRINT, 0, 5) + struct.pack("<BI", 0x01, 0)


class FakePrinter:
    def __init__(self, device_id: str, server_url: str, config_path: pathlib.Path):
        self._device_id = device_id
        self._eui64_le = bytes.fromhex(device_id)
        self._be_addr = _eui64_le_to_be(device_id)
        self._bridge_address = _fake_bridge_address(self._eui64_le)
        self._server_url = server_url
        self._config_path = config_path
        self._output_path = "receipt.png"
        self._ws = None
        self._start_time = time.monotonic()

    def _uptime(self) -> int:
        return int(time.monotonic() - self._start_time)

    def _load_config(self) -> dict:
        return load_config(str(self._config_path))

    def _is_registered(self, cfg: dict) -> bool:
        return self._be_addr in cfg.get("ws_devices", {})

    async def _send(self, msg: dict):
        await self._ws.send(json.dumps(msg))

    async def _send_bridge_online(self):
        await self._send({
            "type": "bridge_online",
            "bridge_address": self._bridge_address,
            "firmware_version": "1.0.0",
        })

    async def _send_printer_join_request(self):
        await self._send({
            "type": "printer_join_request",
            "bridge_address": self._bridge_address,
            "device_address": self._be_addr,
        })
        log.info("Sent printer_join_request for %s", self._be_addr)

    async def _send_printer_connected(self):
        await self._send({
            "type": "printer_connected",
            "bridge_address": self._bridge_address,
            "device_address": self._be_addr,
        })
        log.info("Sent printer_connected for %s", self._be_addr)

    async def _send_printer_event(self, payload: bytes):
        await self._send({
            "type": "printer_event",
            "bridge_address": self._bridge_address,
            "device_address": self._be_addr,
            "payload": base64.b64encode(payload).decode(),
        })

    async def _handle_add_key(self, data: dict, command_id):
        key = base64.b64decode(data["key"])
        cfg = self._load_config()
        ws_devices = cfg.setdefault("ws_devices", {})
        entry = ws_devices.get(self._be_addr, {})
        entry["link_key"] = key.hex()
        ws_devices[self._be_addr] = entry
        save_config(cfg, str(self._config_path))
        log.info("Saved link_key for %s", self._be_addr)
        await self._send({"type": "key_ack", "command_id": command_id, "success": True})
        await self._send_printer_connected()

    async def _handle_print(self, data: dict, command_id):
        binary = base64.b64decode(data["payload"])
        log.info("Print job received (cmd_id=%s, %d bytes)", command_id, len(binary))
        try:
            receipt = lp_binary_to_pil(binary)
            show_face = (
                len(binary) >= 4
                and struct.unpack_from("<H", binary, 2)[0] == CMD_SET_DELIVERY_AND_PRINT
            )
            if show_face and FACE_IMAGE_PATH.exists():
                face = Image.open(FACE_IMAGE_PATH).convert("L")
                if face.width != PRINT_WIDTH:
                    new_h = int(face.height * PRINT_WIDTH / face.width)
                    face = face.resize((PRINT_WIDTH, new_h), Image.Resampling.LANCZOS)
                im = Image.new("L", (PRINT_WIDTH, face.height + receipt.height), 255)
                im.paste(face, (0, 0))
                im.paste(receipt, (0, face.height))
            else:
                im = receipt
            im.save(self._output_path)
            log.info("Saved to receipt.png (%dx%d, face=%s)", im.width, im.height, show_face)
            success = True
        except Exception as exc:
            log.error("Failed to render print job: %s", exc)
            success = False

        await self._send({"type": "print_ack", "command_id": command_id, "success": success})
        if success:
            await self._send_printer_event(_did_print_binary())

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self._send_printer_event(_heartbeat_binary(self._uptime()))
                log.debug("Heartbeat sent (uptime=%ds)", self._uptime())
            except Exception:
                break

    async def _receive_loop(self):
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
                log.debug("Unhandled message type: %s", msg_type)

    async def connect_and_run(self):
        cfg = self._load_config()
        registered = self._is_registered(cfg)

        if not registered:
            claim_code, _link_key = generate_claim_code(self._eui64_le)
            ws_devices = cfg.setdefault("ws_devices", {})
            ws_devices[self._be_addr] = {"claim_code": claim_code}
            save_config(cfg, str(self._config_path))
            print(f"\nClaim code for fake printer {self._be_addr}:")
            print(f"  {claim_code}\n")

        log.info("Connecting to %s", self._server_url)
        async with websockets.connect(self._server_url, subprotocols=[SUBPROTOCOL]) as ws:
            self._ws = ws
            await self._send_bridge_online()

            if registered:
                await self._send_printer_connected()
            else:
                await self._send_printer_join_request()

            heartbeat_task = asyncio.get_event_loop().create_task(
                self._heartbeat_loop()
            )
            try:
                await self._receive_loop()
            finally:
                heartbeat_task.cancel()

    async def run(self):
        delay = RECONNECT_DELAY_MIN
        while True:
            try:
                await self.connect_and_run()
                delay = RECONNECT_DELAY_MIN
            except (websockets.ConnectionClosed, OSError) as exc:
                log.warning("Connection lost: %s — reconnecting in %ds", exc, delay)
            except Exception as exc:
                log.error("Unexpected error: %s — reconnecting in %ds", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)


def main():
    parser = argparse.ArgumentParser(description="Fake Little Printer for local testing")
    parser.add_argument(
        "--device-id", default=DEFAULT_DEVICE_ID,
        help="EUI64 little-endian hex (16 chars). Default: %(default)s",
    )
    parser.add_argument(
        "--server-url", default=DEFAULT_SERVER_URL,
        help="LP server WebSocket URL. Default: %(default)s",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help="Path to config.json. Default: %(default)s",
    )
    args = parser.parse_args()

    device_id = args.device_id.replace("-", "").replace(":", "").lower()
    if len(device_id) != 16 or not all(c in "0123456789abcdef" for c in device_id):
        print(f"Error: --device-id must be 16 hex chars (8 bytes), got: {args.device_id!r}",
              file=sys.stderr)
        sys.exit(1)

    printer = FakePrinter(
        device_id=device_id,
        server_url=args.server_url,
        config_path=pathlib.Path(args.config),
    )

    try:
        asyncio.run(printer.run())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
