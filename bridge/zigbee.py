"""
Zigbee coordinator for the Little Printer bridge.

Uses bellows (https://github.com/zigpy/bellows) for EZSP serial communication.
Target: bellows >= 0.36.0

EZSP types are imported from bellows.types. If your version spells things
differently, check bellows/types/named_array.py and bellows/types/struct.py.
"""

import asyncio
import logging
import random
import struct
from typing import Callable, Optional

from bellows.ezsp import EZSP
import bellows.types as t

log = logging.getLogger(__name__)

# ── Zigbee constants ──────────────────────────────────────────────────────────
PROFILE_ID = 0xC000
CLUSTER_ID = 0xFF00   # Weminuche cluster; firmware: WEMINUCHE_CLUSTER_ID = 65280
ENDPOINT = 1
MANUFACTURER_ID = 0x1002

# ZCL frame header (manufacturer-specific cluster-specific command)
# frame_control=0x05: cluster-specific(01) | mfg-specific(1<<2) | client→server(0<<3)
ZCL_FRAME_CONTROL = 0x05
ZCL_CMD_ID = 0x01
ZCL_HEADER_SIZE = 5  # frame_ctrl(1) + mfg_lo(1) + mfg_hi(1) + seq(1) + cmd(1)

# ── Device event codes (little-endian 16-bit) ─────────────────────────────────
EVENT_HEADER_SIZE  = 10  # 2B code + 4B cmd_id + 4B payload_len
EVENT_HEARTBEAT = 0x0001
EVENT_DID_PRINT = 0x0002
EVENT_DID_POWER_ON = 0x0003
EVENT_HEARTBEAT_SIZE = 0x0004
EVENT_DID_PRINT_SIZE = 0x0005
EVENT_DID_POWER_ON_SIZE_LONG = 0x004A # 74
EVENT_DID_POWER_ON_SIZE_SHORT = 0x003A # 58

# ── Block transfer ────────────────────────────────────────────────────────────
BLOCK_RETRY_ATTEMPTS = 4
BLOCK_RETRY_DELAY    = 1.0
BLOCK_SEND_TIMEOUT   = 5.0   # wait for MAC delivery per fragment
MAX_APS_PAYLOAD      = 80    # 82 (ZigBee Pro with security) - 2 (APS fragmentation overhead)

# ── EmberStatus integer values (matches bellows/zigpy) ───────────────────────
EMBER_SUCCESS    = 0x00
EMBER_NETWORK_UP = 0x90
EMBER_KEY_TABLE_SIZE = 16

class PrinterJoinEvent:
    def __init__(self, node_id: int, eui64_le: bytes, policy_decision: int):
        self.node_id = node_id           # Zigbee short address (16-bit)
        self.eui64_le = eui64_le         # EUI64, 8 bytes little-endian
        self.eui64_hex = eui64_le.hex()  # convenience
        self.policy_decision = policy_decision  # 0x00=accepted, 0x02=denied


class LittlePrinterBridge:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._ezsp: Optional[EZSP] = None
        self._zcl_seq = 0

        self._network_up = asyncio.Event()
        self._join_queue: asyncio.Queue[PrinterJoinEvent] = asyncio.Queue()
        self._zcl_response_event = asyncio.Event()
        self._zcl_response_code = -1
        self._print_done = asyncio.Event()

        # Per-block fragment tracking (reset before each block send).
        # Original firmware matches messageSentHandler by apsFrame.sequence,
        # not by messageTag: EZSP v13 NCP returns tag=0xFF for all but the
        # first fragment in a window.
        self._expected_aps_seq: int = -1
        self._pending_frag_count: int = 0
        self._frag_ok_count: int = 0
        self._frag_all_ok: bool = True
        self._all_frags_done = asyncio.Event()

        # EUI64 hex → short address, populated as printers join or send messages
        self._addr_map: dict[str, int] = {}
        self._printer_ready: dict[str, asyncio.Event] = {}
        self._pending_sender_eui64: Optional[str] = None

        # Caller can set this to receive EVENT_DID_PRINT / EVENT_DID_POWER_ON
        self.on_printer_event: Optional[Callable] = None

        self._loop: asyncio.AbstractEventLoop | None = None

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self, force_new_network: bool = False):
        cfg = self._cfg
        log.info("Connecting to EZSP on %s at %d baud", cfg["ezsp_port"], cfg["ezsp_baud"])

        self._loop = asyncio.get_running_loop()
        self._ezsp = EZSP({"path": cfg["ezsp_port"], "baudrate": cfg["ezsp_baud"], "flow_control": None})
        try:
            await self._ezsp.connect()
        except Exception as exc:
            # log.error("Failed to connect to EZSP: %s", exc)
            raise RuntimeError(f"error on connecting EZSP: {exc}") from exc
        self._ezsp.add_callback(self._on_frame)

        await self._configure_stack()
        await self._set_trust_center_policy()
        await self._init_or_form_network(force=force_new_network)

        log.info("Waiting for network to come up...")
        await asyncio.wait_for(self._network_up.wait(), timeout=15.0)
        log.info("Network up")

        await self._register_endpoint()
        await self._set_tx_power()
        await self._open_joining()
        await self._log_network_params()
        log.info("Bridge ready: printer may now join")

    async def stop(self):
        if self._ezsp is not None:
            try:
                await self._ezsp.disconnect()
            except Exception as exc:
                log.info("EZSP disconnect: %s", exc)
            self._ezsp = None

    async def preinstall_known_keys(self, devices: dict):
        """Install all link keys (if any) from config into the NCP key table on startup."""
        for eui64_hex, info in devices.items():
            if "link_key" not in info:
                continue
            try:
                eui64_le = bytes.fromhex(eui64_hex)
                link_key = bytes.fromhex(info["link_key"])
                await self.install_link_key(eui64_le, link_key)
            except Exception as exc:
                log.warning("Could not preinstall key for %s: %s", eui64_hex, exc)

    async def clear_link_keys(self):
        """Remove all link keys from the NCP key table."""
        try:
            (status,) = await self._ezsp.clearKeyTable() # pyright: ignore[reportOptionalMemberAccess]
            if int(status) != EMBER_SUCCESS:
                log.warning("clearKeyTable: %s", status)
                return False
            log.info("NCP key table cleared")
            return True
        except Exception as exc:
            log.warning("clearKeyTable failed: %s", exc)
            return False

    # ── Stack setup ───────────────────────────────────────────────────────────

    async def _configure_stack(self):
        configs = {
            t.EzspConfigId.CONFIG_SECURITY_LEVEL:                  5,
            t.EzspConfigId.CONFIG_STACK_PROFILE:                   2,  # ZigBee Pro: required for printer to find us
            t.EzspConfigId.CONFIG_ADDRESS_TABLE_SIZE:              8,
            t.EzspConfigId.CONFIG_TRUST_CENTER_ADDRESS_CACHE_SIZE: 2,
            t.EzspConfigId.CONFIG_KEY_TABLE_SIZE:                  EMBER_KEY_TABLE_SIZE,  # 16
            t.EzspConfigId.CONFIG_SOURCE_ROUTE_TABLE_SIZE:         0,
            t.EzspConfigId.CONFIG_FRAGMENT_WINDOW_SIZE:            8,
            t.EzspConfigId.CONFIG_FRAGMENT_DELAY_MS:               0,
            t.EzspConfigId.CONFIG_END_DEVICE_POLL_TIMEOUT:         1,
            t.EzspConfigId.CONFIG_END_DEVICE_POLL_TIMEOUT_SHIFT:    6,
            t.EzspConfigId.CONFIG_TX_POWER_MODE:                   1,  # EMBER_TX_POWER_MODE_BOOST
            t.EzspConfigId.CONFIG_DISABLE_RELAY:                   1,
            t.EzspConfigId.CONFIG_MAX_HOPS:                        30,
        }
        for cid, val in configs.items():
            (status,) = await self._ezsp.setConfigurationValue(cid, val) # pyright: ignore[reportOptionalMemberAccess]
            if int(status) != EMBER_SUCCESS:
                log.info("setConfigurationValue %s=%s: %s", cid, val, status)

        # Tell NCP the max reassembly buffer size (1024 matches original firmware)
        size_bytes = bytes([1024 & 0xFF, (1024 >> 8) & 0xFF])
        for vid in (t.EzspValueId.VALUE_MAXIMUM_INCOMING_TRANSFER_SIZE,
                    t.EzspValueId.VALUE_MAXIMUM_OUTGOING_TRANSFER_SIZE):
            try:
                (status,) = await self._ezsp.setValue(vid, size_bytes) # pyright: ignore[reportOptionalMemberAccess]
                if int(status) != EMBER_SUCCESS:
                    log.info("setValue %s: %s", vid, status)
            except Exception as exc:
                log.info("setValue %s: %s", vid, exc)

    async def _set_trust_center_policy(self):
        # EZSP v8+ uses a bitmask for TC policy; v4–v7 uses an enum.
        # 0x01 means "ALLOW_PRECONFIGURED_KEY_JOINS" in v4 (joins + rejoins OK) but
        # only "ALLOW_JOINS" in v8+ (rejoins blocked). Use 0x03 on v8+ to restore the
        # equivalent behaviour: ALLOW_JOINS | ALLOW_UNSECURED_REJOINS.
        # if self._ezsp.ezsp_version >= 8:  # pyright: ignore[reportOptionalMemberAccess]
        #     tc_decision = t.EzspDecisionBitmask.ALLOW_JOINS | t.EzspDecisionBitmask.ALLOW_UNSECURED_REJOINS
        # else:
        #     tc_decision = t.EzspDecisionId.ALLOW_PRECONFIGURED_KEY_JOINS
        tc_decision = t.EzspDecisionId.ALLOW_PRECONFIGURED_KEY_JOINS

        policies = [
            (t.EzspPolicyId.TRUST_CENTER_POLICY,                 tc_decision),
            (t.EzspPolicyId.TC_KEY_REQUEST_POLICY,               t.EzspDecisionId.DENY_TC_KEY_REQUESTS),
            (t.EzspPolicyId.APP_KEY_REQUEST_POLICY,              t.EzspDecisionId.DENY_APP_KEY_REQUESTS),
            (t.EzspPolicyId.BINDING_MODIFICATION_POLICY,         t.EzspDecisionId.DISALLOW_BINDING_MODIFICATION),
            (t.EzspPolicyId.MESSAGE_CONTENTS_IN_CALLBACK_POLICY, t.EzspDecisionId.MESSAGE_TAG_ONLY_IN_CALLBACK),
        ]
        for policy_id, decision_id in policies:
            (status,) = await self._ezsp.setPolicy(policy_id, decision_id) # pyright: ignore[reportOptionalMemberAccess]
            if int(status) != EMBER_SUCCESS:
                log.warning("setPolicy %s=%s: %s", policy_id, decision_id, status)

    async def _init_or_form_network(self, force: bool = False):
        if force:
            # networkInit first so leaveNetwork has something to act on (same pattern as inspect_dongle.py)
            if not self._network_up.is_set():
                try:
                    if self._ezsp.ezsp_version >= 6: # pyright: ignore[reportOptionalMemberAccess]
                        await self._ezsp.networkInit( # pyright: ignore[reportOptionalMemberAccess]
                            networkInitBitmask=t.EmberNetworkInitBitmask.NETWORK_INIT_NO_OPTIONS
                        )
                    else:
                        await self._ezsp.networkInit() # pyright: ignore[reportOptionalMemberAccess]
                except Exception as exc:
                    log.info("networkInit (pre-leave): %s", exc)
            try:
                await self._ezsp.leaveNetwork() # pyright: ignore[reportOptionalMemberAccess]
                await asyncio.sleep(1.0)
            except Exception as exc:
                log.warning("leaveNetwork: %s", exc)
            self._network_up.clear()
            log.info("Forming new network")
            await self._set_security()
            await self._form_network()
            return

        # Network can come up via callback during _configure_stack if the NCP already
        # has a network and fires NETWORK_UP at one of the config await points.
        if self._network_up.is_set():
            log.info("Network already up (came up during configuration)")
            if await self._ncp_network_matches_config():
                return
            log.warning("NCP network does not match config — leaving and re-forming with config values")
            try:
                await self._ezsp.leaveNetwork() # pyright: ignore[reportOptionalMemberAccess]
                await asyncio.sleep(1.0)
            except Exception as exc:
                log.warning("leaveNetwork: %s", exc)
            self._network_up.clear()

        try:
            if self._ezsp.ezsp_version >= 6: # pyright: ignore[reportOptionalMemberAccess]
                (status,) = await self._ezsp.networkInit( # pyright: ignore[reportOptionalMemberAccess]
                    networkInitBitmask=t.EmberNetworkInitBitmask.NETWORK_INIT_NO_OPTIONS
                )
            else:
                (status,) = await self._ezsp.networkInit() # pyright: ignore[reportOptionalMemberAccess]
        except Exception as exc:
            log.info("networkInit error: %s", exc)
            status = None

        if status is not None and int(status) == EMBER_SUCCESS:
            log.info("Restored existing network from NCP")
            if await self._ncp_network_matches_config():
                return
            log.warning("NCP network does not match config — leaving and re-forming with config values")
            try:
                await self._ezsp.leaveNetwork() # pyright: ignore[reportOptionalMemberAccess]
                await asyncio.sleep(1.0)
            except Exception as exc:
                log.warning("leaveNetwork: %s", exc)
            self._network_up.clear()

        log.info("No existing network: forming new one")
        await self._set_security()
        await self._form_network()

    async def _ncp_network_matches_config(self) -> bool:
        cfg = self._cfg
        try:
            status, _node_type, params = await self._ezsp.getNetworkParameters() # pyright: ignore[reportOptionalMemberAccess]
            if int(status) != EMBER_SUCCESS:
                return True  # can't read params, don't disturb existing network
        except Exception as exc:
            log.debug("getNetworkParameters: %s", exc)
            return True
        ncp_epan = bytes(params.extendedPanId).hex()
        ncp_channel = int(params.radioChannel)
        cfg_epan = cfg.get("extended_pan_id", "")
        cfg_channel = cfg.get("channel", 0)
        cfg_pan_id = cfg.get("pan_id", 0)
        ncp_pan_id = int(params.panId)
        match = (ncp_epan == cfg_epan and ncp_channel == cfg_channel
                 and (cfg_pan_id == 0 or ncp_pan_id == cfg_pan_id))
        if not match:
            log.warning(
                "NCP: EPAN=%s ch=%d PAN=0x%04x  config: EPAN=%s ch=%d PAN=0x%04x",
                ncp_epan, ncp_channel, ncp_pan_id, cfg_epan, cfg_channel, cfg_pan_id,
            )
        return match

    async def _set_security(self):
        cfg = self._cfg
        network_key = bytes.fromhex(cfg["network_key"])
        security = t.EmberInitialSecurityState(
            bitmask=t.EmberInitialSecurityBitmask.HAVE_NETWORK_KEY,
            preconfiguredKey=t.KeyData([0] * 16),
            networkKey=t.KeyData(list(network_key)),
            networkKeySequenceNumber=0,
            preconfiguredTrustCenterEui64=t.EUI64([0] * 8),
        )
        (status,) = await self._ezsp.setInitialSecurityState(security) # pyright: ignore[reportOptionalMemberAccess]
        if int(status) != EMBER_SUCCESS:
            raise RuntimeError(f"setInitialSecurityState failed: {status}")

    async def _form_network(self):
        log.info("Forming network...")
        cfg = self._cfg
        epan = bytes.fromhex(cfg["extended_pan_id"])
        channel = cfg["channel"]
        params = t.EmberNetworkParameters(
            extendedPanId=t.ExtendedPanId(list(epan)),
            panId=cfg.get("pan_id", random.randint(1, 0xFFFE)),
            radioTxPower=8,
            radioChannel=channel,
            joinMethod=t.EmberJoinMethod.USE_MAC_ASSOCIATION,
            nwkManagerId=0,
            nwkUpdateId=0,
            channels=0,
        )
        # bellows 0.36+ formNetwork returns None and raises zigpy.exceptions.FormationFailure on error;
        # it also waits internally for NETWORK_UP before returning.
        await self._ezsp.formNetwork(params) # pyright: ignore[reportOptionalMemberAccess]
        log.info("Formed network on channel %d (EPAN: %s)", channel, epan.hex())

    async def _register_endpoint(self):
        (status,) = await self._ezsp.addEndpoint( # pyright: ignore[reportOptionalMemberAccess]
            endpoint=ENDPOINT,
            profileId=PROFILE_ID,
            deviceId=1,
            deviceVersion=0,
            inputClusterCount=2,
            outputClusterCount=2,
            inputClusterList=[0x0000, CLUSTER_ID],
            outputClusterList=[0x0000, CLUSTER_ID],
        )
        if int(status) != EMBER_SUCCESS:
            # ERROR_INVALID_CALL means the endpoint is already registered (NCP retains state across restarts)
            log.debug("addEndpoint: %s", status)

    async def _set_tx_power(self):
        (status,) = await self._ezsp.setRadioPower(8) # pyright: ignore[reportOptionalMemberAccess]
        if int(status) != EMBER_SUCCESS:
            log.warning("setRadioPower: %s", status)

    async def _open_joining(self):
        (status,) = await self._ezsp.permitJoining(0xFF)  # 0xFF = always open # pyright: ignore[reportOptionalMemberAccess]
        if int(status) != EMBER_SUCCESS:
            log.warning("permitJoining: %s", status)

    async def _log_network_params(self):
        try:
            status, _node_type, params = await self._ezsp.getNetworkParameters() # pyright: ignore[reportOptionalMemberAccess]
            epan = bytes(params.extendedPanId).hex()
            log.info(
                "NCP network: channel=%d  EPAN=%s  PAN=0x%04x  (BERG prefix: %s)",
                params.radioChannel,
                epan,
                params.panId,
                epan.endswith("47524542"),
            )
        except Exception as exc:
            log.warning("Could not read network parameters: %s", exc)

    # ── Key management ────────────────────────────────────────────────────────

    async def install_link_key(self, eui64_le: bytes, link_key: bytes):
        eui64 = t.EUI64(list(eui64_le))
        key_data = t.KeyData(list(link_key))
        if "importLinkKey" in self._ezsp._protocol.COMMANDS: # pyright: ignore[reportOptionalMemberAccess]
            (status,) = await self._ezsp.importLinkKey( # pyright: ignore[reportOptionalMemberAccess]
                index=0, address=eui64, key=key_data
            )
            ok = int(status) == int(t.sl_Status.OK)
        else:
            (status,) = await self._ezsp.addOrUpdateKeyTableEntry( # pyright: ignore[reportOptionalMemberAccess]
                eui64, True, key_data
            )
            ok = int(status) == EMBER_SUCCESS
        if not ok:
            raise RuntimeError(f"install_link_key failed: {status}")
        log.info("Link key installed for %s", eui64_le.hex())

    # ── Join handling ─────────────────────────────────────────────────────────

    async def wait_for_join(self) -> PrinterJoinEvent:
        return await self._join_queue.get()

    def register_short_addr(self, eui64_hex: str, node_id: int):
        self._set_addr(eui64_hex, node_id)

    def _set_addr(self, eui64_hex: str, node_id: int):
        self._addr_map[eui64_hex] = node_id
        if eui64_hex in self._printer_ready:
            self._printer_ready[eui64_hex].set()

    async def wait_for_printer_reachable(self, eui64_hex: str):
        """Return once the printer's short address is known (join or incoming message)."""
        if eui64_hex in self._addr_map:
            return
        event = asyncio.Event()
        self._printer_ready[eui64_hex] = event
        await event.wait()

    def short_addr_for(self, eui64_hex: str) -> Optional[int]:
        return self._addr_map.get(eui64_hex)

    # ── Print job ─────────────────────────────────────────────────────────────

    async def send_print_job(self, eui64_hex: str, blocks: list[bytes]):
        short_addr = self._addr_map.get(eui64_hex)
        if short_addr is None:
            raise RuntimeError(f"Unknown short address for {eui64_hex}; printer must join first")

        self._print_done.clear()
        su_params = list(self._ezsp._protocol.COMMANDS["sendUnicast"][1].keys()) # pyright: ignore[reportOptionalMemberAccess]
        use_v14 = "message_type" in su_params

        def _next_block_id(bid):
            nxt = bid + 1
            return nxt if nxt <= 255 else 1

        block_id = 0
        # Pipeline: while waiting for block N's ZCL ACK, send block N+1's fragments.
        # The two phases are independent: fragment state vs ZCL response state don't overlap.
        prefetch_task: asyncio.Task | None = None

        for i, block in enumerate(blocks):
            log.info("Sending block %d/%d (id=%d)", i + 1, len(blocks), block_id)

            if prefetch_task is not None:
                frag_ok = await prefetch_task
                prefetch_task = None
            else:
                frag_ok = await self._send_fragments(short_addr, block_id, block, use_v14)

            if not frag_ok:
                # Fragment failure: retry this block sequentially (no pipeline during retry)
                retried = False
                for attempt in range(1, BLOCK_RETRY_ATTEMPTS):
                    log.warning("Block %d attempt %d failed, retrying...", block_id, attempt)
                    await asyncio.sleep(BLOCK_RETRY_DELAY)
                    if await self._send_fragments(short_addr, block_id, block, use_v14):
                        retried = True
                        break
                if not retried:
                    raise RuntimeError(f"Block {block_id} fragment send failed after {BLOCK_RETRY_ATTEMPTS} attempts")

            # Increment ZCL seq and arm the ZCL response event for this block.
            self._zcl_seq = (self._zcl_seq + 1) & 0xFF
            self._zcl_response_event.clear()
            self._zcl_response_code = -1

            # Pipeline: kick off next block's fragments now, overlapping with ZCL wait.
            if i + 1 < len(blocks):
                next_id = _next_block_id(block_id)
                prefetch_task = asyncio.create_task(
                    self._send_fragments(short_addr, next_id, blocks[i + 1], use_v14)
                )

            # Wait for this block's ZCL ACK (original: _pending_response.wait(5))
            try:
                await asyncio.wait_for(self._zcl_response_event.wait(), timeout=BLOCK_SEND_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("ZCL response timeout for block_id=%d", block_id)
                if prefetch_task:
                    prefetch_task.cancel()
                    prefetch_task = None
                # Retry from scratch: re-send this block sequentially
                success = False
                for attempt in range(1, BLOCK_RETRY_ATTEMPTS):
                    log.warning("Block %d ZCL timeout, retry %d...", block_id, attempt)
                    await asyncio.sleep(BLOCK_RETRY_DELAY)
                    if await self._send_block(short_addr, block_id, block, use_v14):
                        success = True
                        break
                if not success:
                    raise RuntimeError(f"Block {block_id} ZCL ACK failed after retries")
                block_id = _next_block_id(block_id)
                continue

            if self._zcl_response_code != 0x00:
                log.warning("Printer error 0x%02x for block_id=%d", self._zcl_response_code, block_id)
                if prefetch_task:
                    prefetch_task.cancel()
                    prefetch_task = None
                raise RuntimeError(f"Block {block_id} rejected by printer: 0x{self._zcl_response_code:02x}")

            block_id = _next_block_id(block_id)

        # log.info("All blocks sent, waiting for print confirmation...")
        # try:
        #     await asyncio.wait_for(self._print_done.wait(), timeout=PRINT_DONE_TIMEOUT)
        #     log.info("Print confirmed by printer")
        # except asyncio.TimeoutError:
        #     log.warning("No print confirmation received within %ds", PRINT_DONE_TIMEOUT)

        log.info("All blocks sent.")

    async def _send_block(self, short_addr: int, block_id: int, data: bytes, use_v14: bool) -> bool:
        """Send one block (fragments + ZCL ACK wait). Used for sequential retries."""
        if not await self._send_fragments(short_addr, block_id, data, use_v14):
            return False
        self._zcl_seq = (self._zcl_seq + 1) & 0xFF
        self._zcl_response_event.clear()
        self._zcl_response_code = -1
        try:
            await asyncio.wait_for(self._zcl_response_event.wait(), timeout=BLOCK_SEND_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("ZCL response timeout for block_id=%d", block_id)
            return False
        if self._zcl_response_code != 0x00:
            log.warning("Printer error 0x%02x for block_id=%d", self._zcl_response_code, block_id)
            return False
        return True

    async def _send_fragments(self, short_addr: int, block_id: int, data: bytes, use_v14: bool) -> bool:
        """Send all APS fragments for one block and wait for MAC delivery callbacks."""
        zcl_frame = self._build_zcl_frame(block_id, data)

        # APS fragmentation: split ZCL frame into MAX_APS_PAYLOAD-byte chunks.
        # Original firmware (fragmentation.pyc.py) sends the entire window at once
        # before waiting: the printer only sends its window ACK after the last
        # fragment arrives, so waiting between fragments causes a deadlock.
        fragments = [
            zcl_frame[i:i + MAX_APS_PAYLOAD]
            for i in range(0, len(zcl_frame), MAX_APS_PAYLOAD)
        ]
        total_frags = len(fragments)
        base_options = (t.EmberApsOption.APS_OPTION_FRAGMENT
                        | t.EmberApsOption.APS_OPTION_RETRY
                        | t.EmberApsOption.APS_OPTION_ENABLE_ROUTE_DISCOVERY
                        | t.EmberApsOption.APS_OPTION_ENABLE_ADDRESS_DISCOVERY)

        # Reset fragment tracking before sending anything.
        # We match messageSentHandler callbacks by apsFrame.sequence (same for all
        # fragments in one window), because EZSP v13 NCP returns messageTag=0xFF
        # for all fragments after the first: matching by tag is unreliable.
        self._expected_aps_seq = -1
        self._pending_frag_count = total_frags
        self._frag_ok_count = 0
        self._frag_all_ok = True
        self._all_frags_done.clear()

        aps_sequence = 0

        # Send ALL fragments immediately (whole window), matching original firmware.
        for frag_idx, frag_data in enumerate(fragments):
            aps_frame = t.EmberApsFrame(
                profileId=PROFILE_ID,
                clusterId=CLUSTER_ID,
                sourceEndpoint=ENDPOINT,
                destinationEndpoint=ENDPOINT,
                options=base_options,
                groupId=(total_frags << 8) | frag_idx,
                sequence=aps_sequence,
            )
            tag = frag_idx & 0xFF

            if use_v14:
                status, seq = await self._ezsp.sendUnicast( # pyright: ignore[reportOptionalMemberAccess]
                    message_type=t.EmberOutgoingMessageType.OUTGOING_DIRECT,
                    nwk=short_addr,
                    aps_frame=aps_frame,
                    message_tag=tag,
                    message=frag_data,
                )
            else:
                status, seq = await self._ezsp.sendUnicast( # pyright: ignore[reportOptionalMemberAccess]
                    type=t.EmberOutgoingMessageType.OUTGOING_DIRECT,
                    indexOrDestination=short_addr,
                    apsFrame=aps_frame,
                    messageTag=tag,
                    messageContents=frag_data,
                )
            aps_sequence = int(seq)
            if int(status) != EMBER_SUCCESS:
                log.warning("sendUnicast fragment %d/%d: %s", frag_idx + 1, total_frags, status)
                self._pending_frag_count = 0
                return False
            if frag_idx == 0:
                # All fragments share the same APS sequence (per spec). Set it now so
                # messageSentHandler callbacks that fire during subsequent sends are counted.
                self._expected_aps_seq = aps_sequence
            log.info("Fragment %d/%d queued (tag=%d aps_seq=%d)", frag_idx + 1, total_frags, tag, aps_sequence)

        # Wait for ALL messageSentHandler callbacks (original: _pending_sent.wait(5))
        try:
            await asyncio.wait_for(self._all_frags_done.wait(), timeout=BLOCK_SEND_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("messageSent timeout: %d/%d fragment(s) for block %d",
                        self._frag_ok_count, total_frags, block_id)
            self._pending_frag_count = 0
            return False

        if not self._frag_all_ok:
            log.warning("One or more fragment deliveries failed for block %d", block_id)
            return False

        return True

    def _build_zcl_frame(self, block_id: int, block_data: bytes) -> bytes:
        seq = self._zcl_seq & 0xFF
        header = struct.pack(
            "BBBBB",
            ZCL_FRAME_CONTROL,
            MANUFACTURER_ID & 0xFF,
            (MANUFACTURER_ID >> 8) & 0xFF,
            seq,
            ZCL_CMD_ID,
        )
        return header + bytes([block_id]) + block_data

    # ── EZSP callback dispatcher ──────────────────────────────────────────────

    def _on_frame(self, frame_name: str, args: tuple):
        try:
            if frame_name == "stackStatusHandler":
                self._handle_stack_status(args)
            elif frame_name == "trustCenterJoinHandler":
                self._handle_tc_join(args)
            elif frame_name == "incomingSenderEui64Handler":
                (eui64,) = args
                self._pending_sender_eui64 = bytes(eui64).hex()
            elif frame_name == "incomingMessageHandler":
                self._handle_incoming(args)
            elif frame_name == "messageSentHandler":
                self._handle_message_sent(args)
            else:
                log.info("Unknown EZSP frame: %s %s", frame_name, args)
        except Exception:
            log.exception("Error in EZSP callback %s", frame_name)

    def _handle_stack_status(self, args):
        (status,) = args
        code = int(status)
        if code == EMBER_NETWORK_UP:
            log.info("Stack: NETWORK_UP")
            self._network_up.set()
        elif code in (0x91, 0x92):  # NETWORK_DOWN, NETWORK_LOST
            log.warning("Stack: network lost (0x%02x)", code)
            self._network_up.clear()

    def _handle_tc_join(self, args):
        # (newNodeId, newNodeEui64, status, policyDecision, parentOfNewNode)
        node_id, eui64, _status, policy_decision, _parent = args
        eui64_le = bytes(eui64)
        eui64_hex = eui64_le.hex()

        log.info(
            "TC join: node=0x%04x eui64=%s decision=0x%02x",
            int(node_id), eui64_hex, int(policy_decision),
        )

        if int(policy_decision) in (0x00, 0x03):  # USE_PRECONFIGURED_KEY or NO_ACTION (secure rejoin)
            self._set_addr(eui64_hex, int(node_id))

        event = PrinterJoinEvent(int(node_id), eui64_le, int(policy_decision))
        self._loop.create_task(self._join_queue.put(event))  # type: ignore[union-attr]

    def _handle_incoming(self, args):
        # (type, apsFrame, lastHopLqi, lastHopRssi, sender, bindingIndex, addressIndex, message)
        _msg_type, aps_frame, _lqi, _rssi, sender, _binding, _address, message = args

        # incomingSenderEui64Handler fires just before this: use it to map addr
        eui64_hex = self._pending_sender_eui64
        self._pending_sender_eui64 = None
        if eui64_hex:
            is_new = eui64_hex not in self._addr_map
            self._set_addr(eui64_hex, int(sender))
            if is_new:
                # Printer is already on the network (no join event fired). Synthesize an
                # accepted join so the main loop can trigger the claim-code / config-save flow.
                eui64_le = bytes.fromhex(eui64_hex)
                synthetic = PrinterJoinEvent(int(sender), eui64_le, 0)
                self._loop.create_task(self._join_queue.put(synthetic))  # type: ignore[union-attr]

        if int(aps_frame.profileId) != PROFILE_ID or int(aps_frame.clusterId) != CLUSTER_ID:
            return

        raw = bytes(message)
        if len(raw) <= ZCL_HEADER_SIZE:
            return

        # Strip ZCL header; remaining bytes are the device event payload
        frame_ctrl = raw[0]
        header_size = ZCL_HEADER_SIZE if (frame_ctrl & 0x04) else 3
        payload = raw[header_size:]

        self._parse_device_event(int(sender), payload, eui64_hex=eui64_hex)

    # eventCode == 0x80 is a command response (block ACK); anything else is a device event
    _COMMAND_RESPONSE_CODE = 0x80

    def _parse_device_event(self, sender: int, payload: bytes, eui64_hex: Optional[str] = None):
        if len(payload) < EVENT_HEADER_SIZE:
            return

        event_code, _cmd_id, _payload_len = struct.unpack_from("<HII", payload, 0)

        if event_code == self._COMMAND_RESPONSE_CODE:
            # Printer ACK for the last block. Byte 10 of payload = return code.
            return_code = payload[10] if len(payload) > 10 else 0xFF
            log.info("Block ACK from 0x%04x: return_code=0x%02x", sender, return_code)
            self._zcl_response_code = return_code
            self._zcl_response_event.set()
            return

        if event_code == EVENT_HEARTBEAT:
            log.info("Heartbeat from 0x%04x", sender)

        elif event_code == EVENT_DID_POWER_ON:
            log.info("Printer 0x%04x powered on", sender)

        elif event_code == EVENT_DID_PRINT:
            log.info("Printer 0x%04x confirmed print done", sender)
            self._print_done.set()

        else:
            log.info("Unknown event 0x%04x from 0x%04x", event_code, sender)

        if self.on_printer_event:
            self.on_printer_event(eui64_hex, event_code, payload)

    def _handle_message_sent(self, args):
        # v9-v13:  [type, indexOrDest, apsFrame, messageTag, status, message]
        # v14+:    [status, message_type, nwk, aps_frame, message_tag, message]
        # Match by apsFrame.sequence (identical for all fragments in one window),
        # not by messageTag: EZSP v13 NCP returns tag=0xFF for fragments 1..N.
        try:
            schema = self._ezsp._protocol.COMMANDS["messageSentHandler"][2] # pyright: ignore[reportOptionalMemberAccess]
            keys = list(schema.keys())
            if keys[0] == "status":  # v14+ layout
                status = int(args[0])
                aps_frame = args[3]
            else:                    # v9-v13 layout
                aps_frame = args[2]
                status = int(args[4])
            aps_seq = int(aps_frame.sequence)
        except (IndexError, TypeError, KeyError, AttributeError):
            return

        log.info("messageSent: aps_seq=%d status=%d (expecting seq=%d, %d remaining)",
                  aps_seq, status, self._expected_aps_seq, self._pending_frag_count)

        if self._pending_frag_count > 0 and aps_seq == self._expected_aps_seq:
            if status != EMBER_SUCCESS:
                self._frag_all_ok = False
            self._frag_ok_count += 1
            if self._frag_ok_count >= self._pending_frag_count:
                self._pending_frag_count = 0
                self._all_frags_done.set()
