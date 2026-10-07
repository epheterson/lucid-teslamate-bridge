# lucid-teslamate-bridge

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/epheterson/lucid-teslamate-bridge/badge)](https://scorecard.dev/viewer/?uri=github.com/epheterson/lucid-teslamate-bridge)

Log a **Lucid** with **TeslaMate**.

TeslaMate supports pointing at a third-party API host via `TESLA_API_HOST` — that's how services like Teslemetry and MyTeslaMate work. This is a small service that answers that API, backed by the Lucid gRPC API. TeslaMate runs **stock and upgradable**, does the real work — drive segmentation, charge sessions, battery degradation, its Grafana dashboards — and never knows the car isn't a Tesla.

No fork. No patch. No modified TeslaMate image.

## Why

Lucid publishes no official API, so no commercial logger supports the car — TeslaFi, Tessie and Tronity are all Tesla-only. Owners have been asking each other for an equivalent since 2023. This is the smallest thing that produces one, by borrowing an excellent logger that already exists.

## How it works

```
TeslaMate  ──TESLA_API_HOST──▶  lucid-bridge  ──gRPC──▶  Lucid
   │                                                       │
   └── Postgres + Grafana                        your car's state
```

The bridge implements the three endpoints TeslaMate actually calls (`/api/1/products`, `/api/1/vehicles/{id}`, `/api/1/vehicles/{id}/vehicle_data`) plus a token endpoint, and maps Lucid's vehicle state onto the Tesla Owner API shape.

## Setup

1. **Mint a refresh token.** The bridge never sees or stores your password — log in once, keep only the token:

   ```python
   import asyncio, json
   from lucidmotors import LucidAPI

   async def main():
       api = LucidAPI()
       async with api:
           await api.login(input("email: "), input("password: "))
           json.dump({"refresh_token": api._refresh_token}, open("lucid-token.json", "w"))

   asyncio.run(main())
   ```

   (If [nshp/python-lucidmotors#26](https://github.com/nshp/python-lucidmotors/pull/26) lands, that becomes the public `api.refresh_token`.)

   `chmod 600 lucid-token.json`.

2. **Point the compose file at it** — set `LUCID_TOKEN_HOST_PATH` in a `.env`, along with `TM_ENCRYPTION_KEY`, `TM_DB_PASS` and `TM_GRAFANA_PASS`.

3. `docker compose up -d`, then open TeslaMate and sign in with any two strings — the bridge's token endpoint accepts anything, because it's only reachable from TeslaMate on the compose network.

4. **Turn off streaming** for the car in TeslaMate's settings. There's no WebSocket streaming API here, and left on, TeslaMate reaches for Tesla's real streaming host.

## Things that will bite you

Everything below was found the hard way against a real car. They're the reason this is more than 200 lines.

**Units.** TeslaMate calls `Convert.miles_to_km()` on everything it receives, and Lucid reports **kilometres**. Pass them through raw and every odometer, range and efficiency figure is inflated by 1.609 — permanently, since TeslaMate stores derived values rather than re-deriving. The bridge converts using TeslaMate's own constant so the round trip is lossless.

**Sleep.** TeslaMate detects a sleeping car from an HTTP **408** whose body starts `"vehicle unavailable:"`. Return a payload for a sleeping car instead and TeslaMate believes it never sleeps, making every idle statistic fiction.

**Reads never wake a Lucid.** The Python client only wakes the car on *commands* with `auto_wake=True`, which this never sets. The vampire-drain problem that makes aggressive Tesla polling risky doesn't apply, so TeslaMate can poll as hard as it likes.

**Don't pretend to be a Tesla model.** An early version sent `car_type: "models"` to satisfy TeslaMate's efficiency lookup, and the UI proudly displayed a Lucid Gravity as a "Model S P100D". The lookup was never load-bearing — TeslaMate derives efficiency empirically from charge data. An unrecognised `car_type` leaves `model` nil and TeslaMate simply omits the model line.

**Transient sensor spikes.** The car has been observed reporting an exterior temperature of 109.6 °C against a 25.8 °C cabin, correcting itself two minutes later. That comes from Lucid, not from any conversion here — but TeslaMate charts what it's given, and one sample flattens a temperature axis for a day. Readings outside −60…70 °C are dropped.

**The refresh token does not rotate.** Measured: repeated refreshes return a byte-identical token and replaying an earlier one still works. Convenient — processes can share a stored token without coordination — but treat it as a long-lived bearer credential. It's revocable with "log out all devices"; a password is not.

## Status

Working, and young. Verified end to end against a Lucid Gravity: state, range, odometer, temperatures, GPS, doors, locks, tyre pressures and software version all flow into TeslaMate correctly, with the km→mi→km round trip exact.

Drive and charge segmentation depend on mappings that are unit-tested but have seen limited real-world mileage. If something looks wrong, open an issue with what the car reports versus what TeslaMate stores — that comparison is almost always enough to find it.

Air owners: the mapping is model-agnostic and should work, but it has only been exercised on a Gravity. Reports welcome.

## Tests

```
pip install -r requirements.txt pytest
PYTHONPATH=. pytest tests/ -q
```

The tests build real protobuf objects rather than mocks, so they fail loudly if the upstream schema shifts. The unit and sleep mappings are pinned hardest — those are the two that silently corrupt an archive.

## Related

- [nshp/python-lucidmotors](https://github.com/nshp/python-lucidmotors) — the Lucid API client this is built on
- [teslamate-org/teslamate](https://github.com/teslamate-org/teslamate) — the logger doing all the real work
- [borski/ha-lucidmotors](https://github.com/borski/ha-lucidmotors) — Home Assistant integration, if you want control rather than history

## License

MIT
