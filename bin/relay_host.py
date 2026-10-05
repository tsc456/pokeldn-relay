#!/usr/bin/env python3
"""relay_host - transparent LDN relay, LEADER side.

Hosts a fake FireRed/LeafGreen Direct Corner network for a real joining Switch on THIS house, and
bridges every raw RFU word it exchanges with that console to a relay_join.py process running in the
OTHER house over a TCP tunnel (meant to run over Tailscale). No game logic: see
pokeldn/relay/engine.py.

Usage (this house hosts; the other house's Switch is the one hosting for real):
    sudo -E .venv/bin/python -u bin/relay_host.py --phy phy1 --keys /home/USER/.switch/prod.keys \
        --tunnel-port 7777 --name "Casa B"

Waits for the relay_join.py process on the other house to dial in before advertising the LDN
network, so the tunnel is always up before a real console could join.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pokeldn import config as configmod
from pokeldn.frlg.link import host_app, host_session, trade_runtime
from pokeldn.ldn.host_beacon import build_trade_app_data
from pokeldn.ldn.host_pia import HostPeerProtocol
from pokeldn.relay import tunnel as tunnelmod
from pokeldn.relay.engine import LeaderRelayEngine

FRLG_COMM_ID = 0x01006FA0233F8000


class RelayHostApplication(host_app.HostApplication):
    def __init__(self, config, tunnel, **kwargs):
        super().__init__(config, **kwargs)
        self.tunnel = tunnel

    def _build_components(self):
        phy, keys = self._resolve_phy_and_keys()
        link_player = self.profile.to_link_player()
        engine = LeaderRelayEngine(self.tunnel, log=self.log)
        self.session = host_session.HostSession(engine=engine, log=self.log)
        inactive, active = build_trade_app_data(self.profile, self.session.rfu.host_session_id)
        self.tracer = None
        self.network = self.transport_factory(
            app_data=inactive, password=self.ldn.password,
            nickname=self.profile.discovery_name, keys_path=keys,
            local_comm_id=self.ldn.local_comm_id,
            scene_id=self.options.scene_id,
            max_participants=self.options.max_participants,
            phyname=phy, channel=self.options.channel,
            skip_encryption=self.options.skip_encryption,
            accept_decrypted_ccmp=self.options.accept_decrypted_ccmp,
            tracer=self.tracer, log=self.log)
        self.peer = HostPeerProtocol(
            self.network, self.profile, self.session, active,
            native_nonce_sequence=self.options.native_nonce_sequence,
            session_response_first=self.options.session_response_first,
            protocol_tick_seconds=1.0 / self.options.protocol_tick_hz,
            tracer=self.tracer, log=self.log)
        return link_player


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phy", default="phy0", help="Wi-Fi phy for the LDN AP (e.g. phy1)")
    ap.add_argument("--keys", default="~/.switch/prod.keys", help="path to this house's prod.keys")
    ap.add_argument("--comm-id", default=f"{FRLG_COMM_ID:016x}", help="LDN local_communication_id (hex)")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--skip-encryption", action="store_true", default=True)
    ap.add_argument("--no-skip-encryption", dest="skip_encryption", action="store_false")
    ap.add_argument("--accept-decrypted-ccmp", action="store_true", default=True)
    ap.add_argument("--no-accept-decrypted-ccmp", dest="accept_decrypted_ccmp", action="store_false")
    ap.add_argument("--name", default="Casa-B", help="placeholder trainer name shown to the joiner")
    ap.add_argument("--tid", default="0001", help="placeholder trainer id, hex")
    ap.add_argument("--tunnel-port", type=int, default=17777,
                     help="TCP port this house listens on for the other house's relay_join.py")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # ConsoleLog: lg(...) only prints when --verbose (per-frame diagnostics); lg.info(...) always
    # prints curated milestones. Using the same real logger pokeldn's own CLIs use, instead of a
    # bare print(..., flush=True) on every call, matters here: flush=True on every log line is a
    # real, measurable source of latency in a hot loop (confirmed overhead, not just in theory),
    # and --verbose was also turning on pokeldn's own very chatty internal per-packet logging.
    log = trade_runtime.ConsoleLog(args.verbose)

    tunnel = tunnelmod.listen_and_accept(args.tunnel_port, log=log)

    # "Ghost OK" fix (2026-10-06): this house used to start advertising (and then auto-accepting,
    # via pokeldn's own HostPeerProtocol) the moment this process ran, with no relationship at all
    # to whether the OTHER house's relay_join.py actually had a genuine connection to its own real
    # host. The friend's real console would see "accepted" even though nothing real had happened on
    # the user's end yet. Block here, with no timeout, for the one-time "READY" marker that
    # FollowerRelayEngine.feed_in_frame() sends (see its own comment) the moment the other house's
    # leg genuinely completes its NI/UNI handshake with the real host - only then do we ever start
    # the LDN network at all, so the friend sees nothing until there is something real to join.
    log.info("[relay] waiting for the other house's connection to its own real host to be confirmed "
             "(no timeout - nothing is advertised here until then)...")
    ready = tunnel.recv_blocking(timeout=None)
    if ready != b"READY":
        log.info(f"[relay] WARNING: expected the ready marker, got {ready!r} instead - proceeding anyway.")

    profile = configmod.TrainerProfile(name=args.name, tid=int(args.tid, 16), sid=0)
    ldn = configmod.LdnConfig(phy=args.phy, keys_path=args.keys,
                               local_comm_id=int(args.comm_id, 16))
    role = configmod.HostOptions(channel=args.channel,
                                  skip_encryption=args.skip_encryption,
                                  accept_decrypted_ccmp=args.accept_decrypted_ccmp)
    # TradeRunConfig.plan is required by the dataclass but never read by RelayHostApplication
    # (_build_components is fully overridden); a syntactically valid placeholder satisfies it.
    plan = configmod.TradePlan(party_paths=("unused",), trade_slot=0)
    run_config = configmod.TradeRunConfig(profile=profile, plan=plan, ldn=ldn, role=role)

    app = RelayHostApplication(run_config, tunnel, log=log)
    try:
        app.run()
    finally:
        tunnel.stop()


if __name__ == "__main__":
    main()
