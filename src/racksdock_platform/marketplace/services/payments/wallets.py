"""Linking a coldkey to an account: the user signs a short challenge with the wallet; TAO sent from it then credits them.

The challenge is stateless: a token signed with the platform secret carries the user and an expiry, and the message the
wallet signs is derived from the same fields. Signatures come from a browser wallet extension (`signRaw`, which wraps the
bytes in <Bytes>...</Bytes>) or from `btcli wallet sign`; both forms verify."""
from __future__ import annotations

import datetime as dt
import time

CHALLENGE_TTL_S = 600
SDK_HINT = "TAO payments need the bittensor SDK on the platform server: pip install 'racksdock-platform[tao]'"


def sp_core():
    """The SDK's key primitives, imported on use so the platform runs without the SDK when TAO payments are off."""
    try:
        from bittensor import sp_core as m
    except ImportError as e:
        raise RuntimeError(SDK_HINT) from e
    return m


def normalize_address(address: str) -> str:
    """Any SS58 prefix -> Bittensor's (42). Raises ValueError for anything that is not an address."""
    core = sp_core()
    try:
        return core.ss58_encode(core.ss58_decode(address.strip()))
    except Exception as e:
        raise ValueError("not a valid SS58 address") from e


def message_for(email: str, exp: int) -> str:
    until = dt.datetime.fromtimestamp(exp, dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"RacksDock: link this wallet to {email} (valid until {until})"


def challenge(signer, user) -> dict:
    exp = int(time.time()) + CHALLENGE_TTL_S
    return {"token": signer.sign(f"link:{user.id}:{exp}"), "message": message_for(user.email, exp), "expires_at": exp}


def verify_link(signer, user, token: str, address: str, signature: str) -> str:
    """The address the signature proves ownership of, normalized. Raises ValueError with a short reason."""
    body = signer.verify(token)
    parts = (body or "").split(":")
    if len(parts) != 3 or parts[0] != "link" or parts[1] != user.id:
        raise ValueError("bad challenge token")
    exp = int(parts[2])
    if exp < time.time():
        raise ValueError("the challenge expired; start again")
    addr = normalize_address(address)
    sig = signature.strip()
    sig = sig[2:] if sig.lower().startswith("0x") else sig
    try:
        raw = bytes.fromhex(sig)
    except ValueError as e:
        raise ValueError("signature must be hex") from e
    if len(raw) != 64:
        raise ValueError("signature must be 64 bytes")
    if not sp_core().verify(message_for(user.email, exp).encode(), raw, addr):
        raise ValueError("signature does not match the address")
    return addr
