"""Transparent RFU relay engines.

Neither engine understands trade, chat or battle. Each one just moves the raw RFU comm-slot bytes
(pokeldn.gba.rfu.COMM_SLOT_LENGTH == 14 bytes) between the real console on this house and a Tunnel
to the other house's matching engine. All the actual game logic lives entirely in the two real
consoles; we are deliberately dumb in between.

Confirmed from pokeldn's own code (2026-10-04 session):
- RFULeader.tick(parent_words) accepts parent_words as raw bytes/bytearray directly - no
  reinterpretation needed on the leader side (pokeldn/gba/rfu_leader.py).
- Sim's follower-side loop does `words = engine.tick() or [0]*7` and then `self.slot.build(words)`,
  which needs an indexable sequence of 7 ints - so the follower side's tick() must unpack the raw
  14 bytes back into 7 little-endian u16 words.

Dedup history (2026-10-04, same session, three attempts):
1. Plain FIFO (`tunnel.send(bytes(slot))` on every feed_*() call). A live two-house trade exposed a
   real hang: the real console's Reliable layer retries heavily during the heavier trade-data phase
   ("Console ack is behind by N frames; retransmitting the gap" in host_session.py's log), and every
   retry of an already-sent value got queued as its own duplicate entry. A busy stretch can grow an
   unbounded backlog of stale duplicates, so a later DISTINCT one-shot signal (e.g. the room-exit key
   - docs/frlg_link.md: the child must answer it once for the host to ever release
   KeyInterCB_WaitForPlayersToExit) ends up buried behind minutes of old repeats before it is ever
   relayed - looking like a permanent hang even though the tunnel never actually dropped anything.
2. ChildEcho + a dedicated drain thread at a fixed ~VBlank cadence. Fixed the hang in a local test
   (confirmed: a 20x retry storm no longer buried a distinct value behind it), but introduced a NEW
   regression on real hardware: the extra always-on thread (on top of the tunnel's own two socket
   threads) added enough GIL contention that plain room movement - which was already confirmed
   stable earlier in this same session - got noticeably laggy again. A background thread ticking
   independently of the real protocol loop was the wrong tool here.
3. No extra thread, no queue. Each feed_*() call compares the new slot against the last slot this
   engine actually sent; an exact repeat (a Reliable-layer retry of unchanged content) is silently
   skipped instead of re-sent, and any genuinely different value is sent immediately, synchronously,
   exactly as it was before any of this - zero change to call cadence or threading for the case that
   was already working.

A SEPARATE, more fundamental bug was found by re-reading this file carefully after step 3 still
produced intermittent hangs at unpredictable points in the trade sequence: a real RFU comm slot is a
*held register* - polling it returns whatever was last written until something overwrites it, it is
not a one-shot event stream. But tick() was returning tunnel.recv_nowait() directly, which is `None`
whenever nothing NEW has arrived since the last poll - and RFULeader.tick(None) (leader side) / `or
[0]*7` (follower side, in sim.py) both treat `None` as the *idle slot*, not "repeat the last value".
So a genuinely new value from the other house got delivered for exactly one tick and then silently
reverted to all-zero idle on every subsequent tick until the other house produced another distinct
value - if the real console's own polling didn't happen to land on that exact single tick, the
content was effectively lost even though the tunnel delivered it correctly. This fits the observed
symptom far better than congestion does: hangs at unpredictable, different points in the sequence
each attempt, consistent with "whichever one-shot transition's single delivered tick got missed by
bad luck this run" rather than one broken step. Fixed by having both engines hold and keep returning
the last value *received* (mirroring the hold-last-value pattern rfu_leader.ChildEcho already uses
for the analogous held-and-repeated semantics on its own side) instead of defaulting to idle/None
whenever the tunnel's queue is momentarily empty.
"""

import struct

from pokeldn.gba import rfu

COMM_SLOT_STRUCT = "<7H"  # 7 little-endian u16 words == 14 bytes == rfu.COMM_SLOT_LENGTH


def _describe(slot, tag, log):
    """Diagnostic only: decode a non-idle slot's RFU opcode so the logs show what's actually
    crossing the tunnel, without flooding them with hundreds of identical idle ticks."""
    parsed = rfu.parse_slot(bytes(slot))
    if parsed is None:
        return
    log(f"[relay] {tag}: {parsed['name']} word0=0x{parsed['word0']:04x} raw={bytes(slot).hex()}")


class LeaderRelayEngine:
    """Plugs into HostSession(engine=...) in place of HostTradeEngine/HostMysteryGiftEngine.

    We are the LEADER to whatever real console joins us on THIS house. Everything it sends gets
    shipped to the other house; whatever arrives from the other house (the real leader's own output,
    captured there by a FollowerRelayEngine) is replayed to our local console verbatim.
    """

    def __init__(self, tunnel, log=print):
        self.tunnel = tunnel
        self.log = log
        self._last_sent = None      # dedup state: the last slot we actually put on the tunnel
        self._last_received = None  # held-register state: what we keep returning from tick() until
        # a genuinely new value arrives - see the module docstring's "separate, more fundamental bug".
        self.disconnect_requested = False
        self.done = False  # host_app.py's main loop checks this directly; never set True by us -
        # the session ends via the normal "console left LDN" / idle-timeout paths instead.
        self.established = False  # host_pia.py's HostPeerProtocol.tick() reads this directly (no
        # getattr guard) to gate the one-time "active application-data" property update. Flipping
        # it True from __init__ fired that update before any real console had joined, which crashed
        # with pia_crypto still None (it's only set once a real participant's session key is
        # derived). Instead we flip it the first time feed_child_slot() runs, which only happens
        # after a real console has completed its NI/UNI handshake - a precise, protocol-grounded
        # "established" signal instead of a guess.
        # HostSession pokes these onto self.activity every tick(); accepted and ignored.
        self.echo_backlog = 0
        self.echo_progress = 0
        self.last_echo_cmd = None
        self.echo_emissions = 0
        self.echo_blocks = []
        self.echo_dropped = 0
        self.echo_coalesced = 0
        self.echo_backlog_peak = 0
        # host_app.py reads these directly (no getattr guard) for its own progress-logging and
        # disconnect-grace bookkeeping. None of them drive protocol behaviour; they just have to
        # exist with sane values so the base class's loop doesn't crash on a relay engine that has
        # no concept of trade-room state.
        self.state = None            # never matches any host_trade.H_* constant -> no spurious log
        self.commits = 0             # "mons saved so far"; always 0, we never save anything
        self.close_confirmed = True  # lets the base class's disconnect-grace path run normally
        # once the real console leaves, instead of raising on a missing attribute first.

    def feed_child_slot(self, slot):
        """Called once per UNI frame with our local real console's current child row."""
        self.established = True
        slot = bytes(slot)
        _describe(slot, "LEADER recv from local console (child)", self.log)
        if slot == self._last_sent:
            return  # an exact repeat (a Reliable-layer retry) - the other house already has this
        self._last_sent = slot
        self.tunnel.send(slot)

    def tick(self):
        """Called once per VBlank while in UNI; return value goes straight into
        RFULeader.tick(parent_words), which accepts raw bytes as-is.

        Must keep returning the last value received even when the tunnel has nothing new this
        tick - RFULeader.tick(None) treats None as "transmit the idle slot", not "repeat the last
        one", so returning None here whenever the queue is momentarily empty would silently blank
        out real content between genuinely new values instead of holding it steady."""
        payload = self.tunnel.recv_nowait()
        if payload is not None:
            _describe(payload, "LEADER send to local console (parent)", self.log)
            self._last_received = payload
        return self._last_received

    def mark_disconnect_sent(self):
        pass


class FollowerRelayEngine:
    """Plugs into Sim(engine=...) in place of TradeEngine. Mirror image of LeaderRelayEngine: we
    are the FOLLOWER to whatever real console hosts on THIS house.
    """

    def __init__(self, tunnel, log=print):
        self.tunnel = tunnel
        self.log = log
        self._last_sent = None      # dedup state: the last slot we actually put on the tunnel
        self._last_received = None  # held-register state: see LeaderRelayEngine.tick()'s docstring
        self.disconnect_requested = False
        self.established = False  # set True the first time a real UNI-level row actually arrives
        # from the real host - i.e. the NI/UNI handshake with it has genuinely completed. Mirrors
        # LeaderRelayEngine.established's own protocol-grounded signal. relay_join.py's own loop
        # watches this to know when it is safe to tell the other house's relay_host.py to start
        # advertising - see the "ghost OK" fix (2026-10-06): relay_host.py used to advertise and
        # accept the friend's real console unconditionally, with no relationship to whether this
        # leg (us <-> the real host) was connected at all.

    def feed_in_frame(self, unwrapped):
        """`unwrapped` is the decoded per-mpId row table for this frame. mpId 0 is always the host's
        own row - the raw content a real leader actually sent, which is all we forward."""
        if not unwrapped:
            return
        for mpid, slot in unwrapped.get("positional", []):
            if mpid == 0:
                if not self.established:
                    self.established = True
                    # One-time marker, sent before any real slot ever hits the tunnel so the other
                    # house's relay_host.py can safely consume it with a single recv_blocking() call
                    # ahead of its own main loop - ordering is guaranteed because this send happens
                    # synchronously, in the same call, strictly before the real slot below.
                    self.tunnel.send(b"READY")
                slot = bytes(slot)
                _describe(slot, "FOLLOWER recv from local console (host's row)", self.log)
                if slot == self._last_sent:
                    continue  # an exact repeat - the other house already has this
                self._last_sent = slot
                self.tunnel.send(slot)

    def tick(self, sender_only=False):
        """Sim does `words = engine.tick() or [0]*7`, so returning None here whenever the tunnel is
        momentarily empty blanks out to idle instead of holding the last value - same bug and same
        fix as LeaderRelayEngine.tick(); see the module docstring."""
        payload = self.tunnel.recv_nowait()
        if payload is not None:
            if len(payload) != 14:
                self.log(f"[relay] dropped a {len(payload)}-byte tunnel payload (expected 14)")
            else:
                _describe(payload, "FOLLOWER send to local console (our row)", self.log)
                self._last_received = struct.unpack(COMM_SLOT_STRUCT, payload)
        return self._last_received

    def poll_send_done(self):
        """Sim calls this directly (no getattr guard) to pump a real engine's block-sender state
        machine on a window-gated frame. We have no block sender at all - we never fragment or
        reassemble Pokemon data blocks, just pass raw slot content through - so this is a pure
        no-op."""
        pass
