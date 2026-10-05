# pokeldn-relay

A transparent network bridge that lets two **real, unmodified** Nintendo Switch consoles in two
different houses play a Pokémon FireRed/LeafGreen Direct Corner session (trade) together over the
internet, as if they were sitting next to each other on local wireless.

This is **not** pokeldn itself — it's a small add-on built on top of [pokeldn](https://github.com/Decryptu/pokeldn),
Decryptu's reverse-engineered implementation of the Switch's local wireless (LDN) and GBA wireless
adapter protocols. pokeldn already lets a Linux box *be* one side of a trade (hosting with a virtual
party, or joining to pull/push one). This project instead makes Linux **invisible**: two Linux boxes,
one per house, each pretend to be the other player's real console, and shuttle the raw protocol bytes
between two genuine physical Switches over a TCP tunnel. Neither console needs any modification,
homebrew, or awareness that it isn't on the same Wi-Fi network as the other.

## How it works

```
House A (real Switch, hosting for real)          House B (real Switch, joining for real)
        |                                                  |
        | local wireless (LDN)                             | local wireless (LDN)
        |                                                  |
  relay_join.py                                       relay_host.py
  (pretends to be a                                    (pretends to host a
   joining console)                                     fake Direct Corner room)
        |                                                  |
        +--------------------- TCP tunnel ----------------+
                      (e.g. over Tailscale)
```

- **`bin/relay_join.py`** runs in the house whose Switch is **hosting for real**. It scans for that
  real console's own LDN beacon, joins it as a fake "child" console, and forwards every raw RFU comm
  slot it receives to the tunnel.
- **`bin/relay_host.py`** runs in the other house, whose Switch is going to **join for real**. It
  hosts a fake Direct Corner network, waits for the real console to join it, and forwards every raw
  RFU comm slot it receives to the tunnel in the other direction.
- **`pokeldn/relay/engine.py`** (`LeaderRelayEngine` / `FollowerRelayEngine`) is the part that plugs
  into pokeldn's own `HostSession`/`Sim` machinery in place of its real trade/gift engines. It
  understands **nothing** about trades, battles, or chat — it just holds and forwards 14-byte RFU
  comm-slot values. All real game logic lives entirely on the two physical consoles; this relay is
  deliberately dumb in between. That also means it is activity-agnostic in principle, though only
  Direct Corner trading has actually been exercised (see Limitations).
- **`pokeldn/relay/tunnel.py`** is a minimal length-prefixed TCP tunnel carrying those raw bytes
  between the two relay processes.

### "Ghost OK" gate

Earlier versions of this relay would start advertising the fake Direct Corner room the moment
`relay_host.py` launched, regardless of whether the other house's `relay_join.py` had actually
established a real connection to its own console yet. The joining player would see the host's name,
connect, and immediately see "accepted" — even though nothing real had happened yet on the other
end. `relay_host.py` now blocks (no timeout) until `relay_join.py`'s `FollowerRelayEngine` confirms,
over the tunnel, that it has a genuine NI/UNI-level connection to the real host console. Nothing is
advertised to the joining player until there is something real to join.

## How we used it

Two houses, each with one Linux PC and a USB Wi-Fi adapter capable of LDN hosting *and* monitor-mode
joining (see Hardware below), connected to each other over **[Tailscale](https://tailscale.com/)**.
Tailscale gave us a stable point-to-point IP between the two houses without exposing anything to the
public internet or dealing with NAT/port-forwarding — `relay_host.py`'s `--tunnel-port` just needs to
be reachable at the other house's Tailscale IP. We did not try it over a plain public-internet
connection; it should work identically over a different VPN, SSH port-forward, etc. as long as both
processes can open one TCP connection to each other, but Tailscale is what we validated in practice.

### Running it

Both houses need:
1. A working [pokeldn](https://github.com/Decryptu/pokeldn) checkout, set up per its own README
   (venv, `vendor/LDN`, `prod.keys` installed, etc.).
2. This repo's `bin/relay_host.py` and `bin/relay_join.py` copied into pokeldn's own `bin/` directory,
   and this repo's `pokeldn/relay/` copied into pokeldn's own `pokeldn/` package directory.
3. Tailscale (or any other mechanism that gives the two houses a routable IP to each other) running.

In the house whose Switch will **join for real**, start the listener first:

```bash
sudo -E .venv/bin/python -u bin/relay_host.py --phy phy1 \
    --keys /home/USER/.switch/prod.keys --tunnel-port 17777
```

In the house whose Switch will **host for real**, point it at the first house's Tailscale IP:

```bash
sudo -E .venv/bin/python -u bin/relay_join.py --phy phy0 \
    --keys /home/USER/.switch/prod.keys \
    --tunnel-host 100.x.x.x --tunnel-port 17777
```

Order doesn't matter much — `relay_host.py` just waits for `relay_join.py` to connect, and then waits
again for it to confirm a real connection before advertising anything. On the real hosting console,
go to the Pokémon Center's wireless club desk → Direct Corner → Become Leader (Hostear). On the real
joining console, go to the same desk → Direct Corner → Join Group (search) — it will see a room named
whatever `relay_host.py`'s `--name` placeholder is set to (default `Casa-B`), not the real host's
name (see Limitations).

`--phy` must point at an adapter capable of the role each script needs: `relay_host.py` needs to host
an LDN access point (beacon injection), `relay_join.py` needs to scan and associate as a station.
Both roles were validated on the **same physical adapter model** in each house (see Hardware), but
the two roles don't have to be the same adapter if you have two different capable ones.

A broken tunnel (either side crashing, Wi-Fi dropping, etc.) ends the session on both sides — the TCP
listener in `relay_host.py` is single-use, so **both scripts must be relaunched together** after any
disconnect, not just the one that failed.

## Hardware

### What pokeldn itself documents as tested

From pokeldn's own `docs/hardware_adapters.md`, as of this writing:

| model | type | driver | reliability |
|---|---|---|---|
| TP-Link Archer T3U (`2357:012d`) | external USB | `rtw88_8822bu` | high; pokeldn's reference host adapter |
| ALFA AWUS036ACHM | external USB | `mt76x0u` | high |
| Realtek RTL8821CE | internal PCIe | `rtw88_8821ce` | high |
| AMD RZ616 | internal M.2 | `mt7921e` | low; about half speed, sometimes deadlocks before exiting |
| MT7601U | external USB | `mt7601u` | needs a project-pinned DKMS module; stock driver lacks AP mode |

Known **not** to work (per pokeldn): Intel AX200 (`iwlwifi`, can't get an IP), Atheros AR9271
(`ath9k_htc`, can't get an IP most of the time).

### What we actually tested this relay with

**TP-Link Archer T2U** (plain, not T2U Plus/Nano), **RTL8821AU** chipset, `rtw88_8821au` driver —
**one unit per house**, used for both roles (hosting the fake network *and* scanning/joining the real
one, run sequentially, never simultaneously on the same adapter). This specific model/chipset is
**not** in pokeldn's own tested table above; we validated it ourselves: `aireplay-ng --test` confirmed
working packet injection, and it successfully hosted an LDN AP, injected beacons, and associated as a
station against real FireRed/LeafGreen consoles across multiple full sessions. The internal Wi-Fi
cards on both our machines (`RTL8821CE` on one, unidentified on the other) were confirmed **not**
usable for monitor-mode/injection, matching pokeldn's own notes about internal cards generally being
unreliable for this.

If you use a different adapter, check pokeldn's own hardware docs first — `--skip-encryption` and
`--accept-decrypted-ccmp` behavior is adapter/driver-specific, as is whether a single adapter can do
both the AP role and the station role at different times.

## Limitations / what's missing

- **No real identity relay.** Both sides currently see placeholder names (`relay_host.py`'s
  `--name`/`--tid`, default `Casa-B`/`0001`, on the joining side; the joining console shows up as
  `EMU`, pokeldn's own `LinkPlayer` default, on the hosting side). A real-identity handshake (each
  house's relay scanning its own real console's beacon first and forwarding the real name/TID to the
  other house before advertising) was designed and implemented, but caused the real NI/UNI handshake
  to intermittently stall against real hardware in testing, for reasons not fully root-caused yet
  (suspected to be related to the extra wall-clock delay it introduces before the real join attempt
  begins, not a resource leak in our own code — see the git history on the identity-handshake
  branch/commits for the full investigation). It was reverted rather than shipped half-working.
- **Direct Corner (trade) only.** The relay engines are activity-agnostic in principle (they just move
  raw bytes), but only the plain trade centre flow has actually been exercised end-to-end. Union Room
  (which needs a different NI handshake — no parent NI, a different keepalive/timeout model per
  pokeldn's own docs) and the Direct Corner → Colosseum → Single Battle path (which only needs a
  different beacon activity byte, per pokeldn's `build_colosseum_app_data`) are both plausible next
  steps but neither has been tried live.
- **No chat support.**
- **No automatic reconnection.** A dropped tunnel requires manually relaunching both `relay_host.py`
  and `relay_join.py` together.
- **No TID relay for the joining player**, even if the identity handshake above is revisited — the
  LDN-level `JoinEvent` only exposes a name, not a trainer ID.
- Tested on **FireRed/LeafGreen only**. pokeldn itself supports more games (see its own README); this
  relay's engines were only ever plugged into the FRLG host/join stack.

## Credits

Built entirely on top of [Decryptu/pokeldn](https://github.com/Decryptu/pokeldn) — all of the actual
LDN/Pia/RFU protocol reverse-engineering and implementation is that project's work. This repo is
just the two-house relay glue on top of it. Licensed AGPL-3.0, same as pokeldn.
