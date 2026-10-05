#!/usr/bin/env python3
"""relay_join - transparent LDN relay, FOLLOWER side.

Joins the real FireRed/LeafGreen Direct Corner session hosted by a real Switch on THIS house, and
bridges every raw RFU word it exchanges with that console to a relay_host.py process running in the
OTHER house over a TCP tunnel (meant to run over Tailscale). No game logic: see
pokeldn/relay/engine.py.

Usage (this house's Switch hosts for real; the OTHER house runs relay_host.py and is waiting):
    sudo -E .venv/bin/python -u bin/relay_join.py --phy phy1 --keys /home/USER/.switch/prod.keys \
        --tunnel-host 100.x.x.x --tunnel-port 7777 --name "Casa A"
"""

import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pokeldn.ldn import crypto as cryptomod
from pokeldn.ldn import pia_connect
from pokeldn.ldn import transport as tmod
from pokeldn.frlg.link import sim as simmod
from pokeldn.frlg.link import trade_runtime
from pokeldn.relay import tunnel as tunnelmod
from pokeldn.relay.engine import FollowerRelayEngine

FRLG_COMM_ID = 0x01006FA0233F8000
PERIOD = 1.0 / 59.727


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phy", default="phy0", help="Wi-Fi phy for the LDN join (e.g. phy1)")
    ap.add_argument("--keys", default="~/.switch/prod.keys", help="path to this house's prod.keys")
    ap.add_argument("--comm-id", default=f"{FRLG_COMM_ID:016x}", help="LDN local_communication_id (hex)")
    ap.add_argument("--name", default="Relay", help="nickname announced to the real host console")
    ap.add_argument("--scan-dwell", type=float, default=0.6)
    ap.add_argument("--tunnel-host", required=True, help="the other house's Tailscale IP")
    ap.add_argument("--tunnel-port", type=int, default=17777)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # See the matching comment in relay_host.py: a real ConsoleLog instead of bare
    # print(..., flush=True) on every call, so --verbose off actually means quiet/fast.
    log = trade_runtime.ConsoleLog(args.verbose)

    tunnel = tunnelmod.connect_out(args.tunnel_host, args.tunnel_port, log=log)

    log.info(f"[live] scanning for FRLG LDN network (nickname={args.name}) - "
             "retrying indefinitely until it's actually hosting...")
    t = None
    while t is None:
        try:
            t = tmod.LiveTransport(
                nickname=args.name, keys_path=args.keys,
                local_comm_id=int(args.comm_id, 16), phyname=args.phy,
                scan_dwell=args.scan_dwell, log=log,
            ).start()
        except RuntimeError as e:
            log.info(f"[live] no host found yet ({e}); retrying...")

    pc = cryptomod.PiaCrypto(t.ssid)
    engine = FollowerRelayEngine(tunnel, log=log)

    if not t.our_mac or not t.host_mac:
        log.info(f"[live] WARNING: MAC(s) not resolved (us={t.our_mac and t.our_mac.hex()} "
                 f"host={t.host_mac and t.host_mac.hex()}); the Session join may be rejected.")

    conn = pia_connect.ConnectionManager(
        our_mac=t.our_mac or b"\x00" * 6, host_mac=t.host_mac or b"\x00" * 6,
        our_ip=t.our_ip, host_ip=t.host_ip, player_name=args.name,
        random4=os.urandom(4), log=log)

    connect_id = (int.from_bytes(os.urandom(2), "big") or 1).to_bytes(2, "big")
    log.info(f"[live] emulator connect id {connect_id.hex()} (random nonzero)")

    s = simmod.Sim(t, pc, engine, t.our_ip, t.host_ip, conn=conn, log=log, connect_id=connect_id)

    log.info("[live] joined LDN; awaiting the host's Pia connection handshake. Relaying once established.")

    interrupted = {"flag": False}

    def on_sigint(signum, frame):
        interrupted["flag"] = True

    old_sigint = signal.signal(signal.SIGINT, on_sigint)
    try:
        while True:
            s.tick()
            if interrupted["flag"]:
                log.info("[live] interrupted; disconnecting.")
                break
            if tunnel.closed:
                log.info("[relay] tunnel to the other house closed; disconnecting.")
                break
            time.sleep(PERIOD)
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        s.close()
        t.stop()
        tunnel.stop()
        log.info("[live] link closed.")


if __name__ == "__main__":
    main()
