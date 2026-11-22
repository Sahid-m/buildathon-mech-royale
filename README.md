# LondonNo1 — Mech Royale bot

A bot for the Builders Buildathon "Mech Royale" track (`mechfight/1` protocol). Python 3, standard library only.

## Approach

- **Map model (`nav.py`)**: at startup the bot reads `/arena/v2/world.json` and `terrain.i16` and rasterizes every
  prism onto a 2 m height grid (ground, solid tops, overhangs). That grid gives line-of-sight tests (for shooting and
  for finding cover) and walkability (step-up limit). A 4 m coarse grid holds a flow field (reverse Dijkstra) toward
  the next zone circle, computed a slice at a time in the spare time of each decision.
- **Survival first**: placement is the score, so the bot moves into the next zone circle early, retreats to a spot
  hidden from every visible enemy when hurt or outnumbered, and lets health regenerate before it fights again.
- **Fighting**: it faces its target (rear hits do 1.5x), leads gun shots from the target velocity, charges the laser
  and releases only when on target at full charge, fires lightning in its cone, strafes in random bursts, and steps
  away from predicted mortar impacts.
- **Growth**: picks up lasers and lightning, collects cubes, shoots NPC drones and crates when nothing threatens it,
  and chooses survival upgrades (armor, plating, nanites) first.

## Run

```sh
# from the kit folder
python3 check/check.py -- python3 -u /path/to/repo/bot.py
docker build --platform linux/amd64 -t my-bot /path/to/repo
python3 check/check.py --map-dir /arena -- docker run --rm -i --platform linux/amd64 --network none --cpus 1 \
  --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" my-bot
```

`tools/bench.py` replays the kit's recorded stream in-process and prints CPU time per decision.

## Libraries and credits

- `mechfight.py` is the protocol wrapper from the official bot kit, unchanged.
- No other libraries: Python standard library only (base image `python:3.13-slim`).

## License

MIT, see `LICENSE`.
