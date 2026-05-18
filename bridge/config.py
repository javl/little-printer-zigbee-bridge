import json
import os
import random
import secrets
import sys

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

BERG_EPAN_SUFFIX_LE = bytes([0x47, 0x52, 0x45, 0x42])  # "BERG" reversed for little-endian EZSP
BERG_CHANNELS = [11, 14, 15, 19, 20, 24, 25]
DEFAULT_CHANNEL = 15


def _defaults():
    """ Return a dict of default configuration values. """
    default_port = "COM3" if sys.platform == "win32" else "/dev/ttyUSB0"
    return {
        "ezsp_port": default_port,
        "ezsp_baud": 115200,
        "channel": DEFAULT_CHANNEL,
        "pan_id": random.randint(1, 0xFFFE),
        "extended_pan_id": (secrets.token_bytes(4) + BERG_EPAN_SUFFIX_LE).hex(),
        "network_key": secrets.token_hex(16),
        "print_id": 1,
        "devices": {},
    }


def new_network_params(cfg):
    """Regenerate network identity (EPAN, network key, PAN ID) in place."""
    cfg["pan_id"] = random.randint(1, 0xFFFE)
    cfg["extended_pan_id"] = (secrets.token_bytes(4) + BERG_EPAN_SUFFIX_LE).hex()
    cfg["network_key"] = secrets.token_hex(16)


def load(path=CONFIG_PATH):
    """ Load the configuration from a JSON file. If the file does not exist, create it with default values."""
    if os.path.exists(path):
        with open(path) as f:
            content = f.read().strip()
        if not content:
            cfg = _defaults()
            save(cfg, path)
            return cfg
        cfg = json.loads(content)
        defaults = _defaults()
        for k, v in defaults.items():
            if k not in cfg or cfg[k] == "":
                cfg[k] = v
        save(cfg, path)
        return cfg
    cfg = _defaults()
    save(cfg, path)
    return cfg


def save(cfg, path=CONFIG_PATH):
    """ Save the configuration to a JSON file."""
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def next_print_id(cfg, path=CONFIG_PATH):
    """ Get the next print ID from the config, incrementing and saving it back"""
    pid = cfg.get("print_id", 1)
    cfg["print_id"] = (pid % 0xFFFFFFFF) + 1
    save(cfg, path)
    return pid
