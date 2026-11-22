"""A rough local simulator of mechfight/1 for smoke-testing bot behaviour (not the real server).

Physics, weapons, vision and the zone follow protocol.md loosely. Opponents are either scripted
"hunters" (know where we are, close in, shoot with lead) or copies of our own bot.

    python3 tools/sim.py --matches 5 --mode hunt
    python3 tools/sim.py --matches 2 --mode ffa
"""
import argparse
import copy
import gzip
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import mechfight as mf  # noqa
mf.run = lambda *a, **k: None
import nav  # noqa
import bot as B  # noqa

KIT = os.path.join(ROOT, "..", "mechfight-kit")
HELLO = json.loads(gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt").readline())
CFG = HELLO["config"]
MAP = nav.Map(os.path.join(KIT, "arena"))
DT = 0.05
TAU = 2 * math.pi


def wrap(a):
    return (a + math.pi) % TAU - math.pi


class Mech:
    def __init__(self, mid, pos, brain, kind):
        self.id = mid
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [random.uniform(-math.pi, math.pi), 0.0]
        self.cmd = {"move": [0, 0], "aim": list(self.aim), "fire": [False, False]}
        self.hp = 1400.0
        self.max_hp = 1400.0
        self.energy = 100.0
        self.last_spend = -10
        self.last_combat = -10
        self.grounded = True
        self.brain = brain
        self.kind = kind
        self.alive = True
        self.cubes = 0
        g = CFG["weapons"]["gun"]
        self.weapons = [{"kind": "gun", "cooldown": 0.0, "ammo": g["mag"], "reload": 0.0, "stats": dict(g)}, None]
        self.events = []
        self.seen = {}  # id -> time last seen
        self.trigger_prev = [False, False]
        self.charge = 0.0
        self.stats = dict(CFG["mech"])
        self.place = None


class Sim:
    def __init__(self, mode, seed, n_enemies=15, verbose=False):
        random.seed(seed)
        self.rng = random.Random(seed)
        self.t = 0.0
        self.tick = 0
        self.mode = mode
        self.verbose = verbose
        z = CFG["zone"]
        # random zone sequence
        self.zone_seq = [(z["start_center"][0], z["start_center"][1], z["start_radius"])]
        cx, cy, r = self.zone_seq[0]
        for ph in z["phases"]:
            nr = ph["radius"]
            for _ in range(100):
                a = self.rng.uniform(0, TAU)
                d = self.rng.uniform(0, r - nr)
                nx_, ny_ = cx + d * math.cos(a), cy + d * math.sin(a)
                if MAP.inside(nx_, ny_, 40):
                    break
            self.zone_seq.append((nx_, ny_, nr))
            cx, cy, r = nx_, ny_, nr
        self.mechs = []
        spawns = []
        while len(spawns) < 16:
            a = self.rng.uniform(0, TAU)
            d = self.rng.uniform(0, 0.8 * 560)
            x, y = 41.3 + d * math.cos(a), -72.8 + d * math.sin(a)
            if not MAP.inside(x, y, 30):
                continue
            if MAP.top[MAP.cell(x, y)] > MAP.ground[MAP.cell(x, y)]:
                continue
            if all(math.hypot(x - sx, y - sy) > 100 for sx, sy in spawns):
                spawns.append((x, y))
        for i in range(16):
            x, y = spawns[i]
            pos = (x, y, MAP.surf_at(x, y))
            if i == 0 or mode == "ffa":
                h = copy.deepcopy(HELLO)
                h["slot"] = i
                h["seed"] = seed * 100 + i
                h["map"]["dir"] = os.path.join(KIT, "arena")
                b = B.Bot.__new__(B.Bot)
                B.Bot.__init__(b, h) if False else None
                # reuse the loaded map to keep startup cheap
                orig = nav.Map
                nav.Map = lambda d: MAP
                try:
                    b = B.Bot(h)
                finally:
                    nav.Map = orig
                self.mechs.append(Mech(i, pos, b, "bot"))
            else:
                skill = [0.6, 0.6, 0.6, 0.6, 0.8, 0.8, 0.8, 0.8, 1.0, 1.0, 1.0, 1.0, 1.2, 1.2, 1.2][i - 1]
                m = Mech(i, pos, None, "hunter")
                m.skill = skill
                if self.rng.random() < 0.5:
                    m.weapons[1] = {"kind": "laser", "cooldown": 0.0, "charge": 0.0, "stats": dict(CFG["weapons"]["laser"])}
                self.mechs.append(m)
        self.bullets = []
        self.deaths = []
        self.pickups = []
        for k in range(40):
            for _ in range(50):
                x = self.rng.uniform(MAP.bminx + 40, MAP.bmaxx - 40)
                y = self.rng.uniform(MAP.bminy + 40, MAP.bmaxy - 40)
                c = MAP.cell(x, y)
                if MAP.top[c] <= MAP.ground[c]:
                    break
            self.pickups.append({"id": 8000 + k, "kind": self.rng.choice(["laser", "lightning", "mortar"]), "pos": [x, y, MAP.surf_at(x, y)]})
        self.hunt_n = 1

    # ------------------------------------------------------------------ zone
    def zone(self):
        t = self.t
        acc = 0.0
        phases = CFG["zone"]["phases"]
        for k, ph in enumerate(phases):
            cur = self.zone_seq[k]
            nxt = self.zone_seq[k + 1]
            s0 = acc + ph["hold_s"]
            s1 = s0 + ph["shrink_s"]
            if t < s1 or k == len(phases) - 1:
                return {"center": [cur[0], cur[1]], "radius": cur[2], "next_center": [nxt[0], nxt[1]],
                        "next_radius": nxt[2], "shrink_start": s0, "shrink_end": s1, "dps": ph["dps"]}
            acc = s1
        return None

    def zone_now(self):
        z = self.zone()
        f = 0.0
        if self.t > z["shrink_start"]:
            f = min(1.0, (self.t - z["shrink_start"]) / (z["shrink_end"] - z["shrink_start"]))
        cx = z["center"][0] + (z["next_center"][0] - z["center"][0]) * f
        cy = z["center"][1] + (z["next_center"][1] - z["center"][1]) * f
        r = z["radius"] + (z["next_radius"] - z["radius"]) * f
        return cx, cy, r, z["dps"]

    # ------------------------------------------------------------------ vision
    def sees(self, a, b):
        dx, dy = b.pos[0] - a.pos[0], b.pos[1] - a.pos[1]
        d = math.hypot(dx, dy)
        if d > 200:
            return False
        if d > 45:
            if abs(wrap(math.atan2(dy, dx) - a.aim[0])) > math.radians(60):
                return False
        e = (a.pos[0], a.pos[1], a.pos[2] + 8)
        return MAP.los(*e, b.pos[0], b.pos[1], b.pos[2] + 4.5) or MAP.los(*e, b.pos[0], b.pos[1], b.pos[2] + 9)

    def obs_for(self, m):
        mechs = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.sees(m, o):
                m.seen[o.id] = self.t
            if self.t - m.seen.get(o.id, -100) <= 5:
                mechs.append({"id": o.id, "kind": "mech", "pos": [round(v, 2) for v in o.pos], "vel": [round(v, 2) for v in o.vel],
                              "aim": [round(o.aim[0], 3), round(o.aim[1], 3)], "hp": round(o.hp, 1), "max_hp": o.max_hp,
                              "cubes": o.cubes, "weapons": [w["kind"] for w in o.weapons if w], "charging": o.charge > 0})
        projs = []
        for b in self.bullets:
            if b["owner"] != m.id and math.dist(b["pos"], m.pos) < 150:
                projs.append({"id": b["id"], "kind": "gun", "pos": b["pos"][:], "vel": b["vel"][:]})
        pk = [p for p in self.pickups if math.dist(p["pos"][:2], m.pos[:2]) < 120 and
              MAP.los(m.pos[0], m.pos[1], m.pos[2] + 8, p["pos"][0], p["pos"][1], p["pos"][2] + 0.5)]
        cx, cy, r, dps = self.zone_now()
        st = dict(m.stats)
        ws = []
        for w in m.weapons:
            if w is None:
                ws.append(None)
            else:
                ww = dict(w)
                if w["kind"] == "laser":
                    ww["charge"] = round(m.charge, 3)
                ws.append(ww)
        ev = m.events
        m.events = []
        return {"type": "obs", "tick": self.tick, "t": round(self.t, 2), "alive": sum(1 for o in self.mechs if o.alive),
                "self": {"id": m.id, "pos": [round(v, 2) for v in m.pos], "vel": [round(v, 2) for v in m.vel],
                         "aim": [round(m.aim[0], 3), round(m.aim[1], 3)], "hp": round(m.hp, 1), "max_hp": m.max_hp,
                         "energy": round(m.energy, 1), "max_energy": 100, "grounded": m.grounded, "cubes": m.cubes,
                         "upgrades": [], "in_zone": math.hypot(m.pos[0] - cx, m.pos[1] - cy) <= r, "weapons": ws, "stats": st},
                "mechs": mechs, "projectiles": projs, "crates": [], "cubes": [], "pickups": pk, "sounds": [], "beams": [],
                "events": ev, "drops": [], "zone": self.zone(), "offer": None}

    # ------------------------------------------------------------------ scripted hunter
    def hunter_cmd(self, h):
        me = self.mechs[0]
        cx, cy, r, _ = self.zone_now()
        z = self.zone()
        cmd = {"move": [0, 0], "fire": [False, False], "aim": h.cmd["aim"]}
        hunting = me.alive and h.hunting
        tgt = me if hunting else None
        if tgt is not None:
            d = math.dist(tgt.pos[:2], h.pos[:2])
            dx, dy = (tgt.pos[0] - h.pos[0]) / max(d, 1), (tgt.pos[1] - h.pos[1]) / max(d, 1)
            want = 110 if h.weapons[1] else 70
            rad = 1 if d > want else (-0.5 if d < want - 40 else 0)
            side = 1 if (int(self.t / 2 + h.id) % 2) else -1
            mv = [dx * rad - dy * side * 0.6, dy * rad + dx * side * 0.6]
            visible = self.sees(h, tgt) or self.squad_sees(tgt)
            if visible or d < 60:
                mz = h.pos[2] + 7
                tc = [tgt.pos[0], tgt.pos[1], tgt.pos[2] + 4.5]
                tof = math.dist(tc, (h.pos[0], h.pos[1], mz)) / 252
                lead = h.skill * 0.9
                px, py = tc[0] + tgt.vel[0] * tof * lead, tc[1] + tgt.vel[1] * tof * lead
                yaw = math.atan2(py - h.pos[1], px - h.pos[0]) + self.rng.gauss(0, 0.01 / h.skill)
                pit = math.atan2(tc[2] - mz, math.hypot(px - h.pos[0], py - h.pos[1]))
                cmd["aim"] = [yaw, pit]
                los = MAP.los(h.pos[0], h.pos[1], mz, *tc)
                if los and d < 220:
                    cmd["fire"][0] = abs(wrap(h.aim[0] - yaw)) < 0.03
                    if h.weapons[1]:
                        if h.charge >= 0.75 and abs(wrap(h.aim[0] - yaw)) < 0.01:
                            cmd["fire"][1] = False
                        else:
                            cmd["fire"][1] = True
                elif h.weapons[1] and h.charge > 0:
                    cmd["fire"][1] = h.charge < 2.0
            # path: crude steer via our flow toward target
            cmd["move"] = self.crude_steer(h, mv)
        else:
            z = self.zone()
            dx, dy = z["next_center"][0] - h.pos[0], z["next_center"][1] - h.pos[1]
            d = math.hypot(dx, dy)
            if d > z["next_radius"] * 0.5:
                cmd["move"] = self.crude_steer(h, [dx / d, dy / d])
        return cmd

    def squad_sees(self, tgt):
        return any(o.alive and o.kind == "hunter" and self.sees(o, tgt) for o in self.mechs)

    def crude_steer(self, h, mv):
        L = math.hypot(*mv)
        if L < 1e-6:
            return [0, 0]
        base = math.atan2(mv[1], mv[0])
        for o in (0, 0.5, -0.5, 1.0, -1.0, 1.6, -1.6, 2.4, -2.4):
            a = base + o
            ok = True
            s0 = MAP.surf_at(h.pos[0], h.pos[1])
            for dd in (3, 6):
                s = MAP.surf_at(h.pos[0] + math.cos(a) * dd, h.pos[1] + math.sin(a) * dd)
                if s - s0 > 1.2:
                    ok = False
                    break
            if ok:
                return [math.cos(a) * min(1, L), math.sin(a) * min(1, L)]
        return mv

    # ------------------------------------------------------------------ physics
    def step_mech(self, m):
        st = m.stats
        cmd = m.cmd
        mv = cmd.get("move", [0, 0]) or [0, 0]
        L = math.hypot(*mv)
        if L > 1:
            mv = [mv[0] / L, mv[1] / L]
        boost = cmd.get("boost", False) and m.energy > 0
        speed = st["walk_speed"]
        if m.charge > 0:
            speed *= 0.5
        if boost and m.grounded and L > 0.1:
            speed = st["boost_speed"]
            m.energy -= st["boost_drain"] * DT
            m.last_spend = self.t
        tvx, tvy = mv[0] * speed, mv[1] * speed
        acc = (st["ground_accel"] if m.grounded else st["air_accel"]) * DT
        for k, tv in ((0, tvx), (1, tvy)):
            dv = tv - m.vel[k]
            m.vel[k] += max(-acc, min(acc, dv))
        if cmd.get("jump") and m.grounded:
            m.vel[2] = st["jump_speed"]
            m.grounded = False
        if boost and not m.grounded:
            m.vel[2] = min(st["max_rise_speed"], m.vel[2] + st["jet_accel"] * DT)
            m.energy -= st["jet_drain"] * DT
            m.last_spend = self.t
        m.vel[2] -= st["gravity"] * DT
        if self.t - m.last_spend > st["energy_regen_delay"]:
            m.energy = min(100.0, m.energy + st["energy_regen"] * DT)
        m.energy = max(0.0, m.energy)
        # horizontal move with wall slide
        nx_, ny_ = m.pos[0] + m.vel[0] * DT, m.pos[1] + m.vel[1] * DT

        def ok(x, y):
            if not MAP.inside(x, y, 3):
                return False
            for ox, oy in ((0, 0), (2.2, 0), (-2.2, 0), (0, 2.2), (0, -2.2)):
                if MAP.surf_at(x + ox, y + oy) - m.pos[2] > st["step_up"]:
                    return False
            return True
        if ok(nx_, ny_):
            m.pos[0], m.pos[1] = nx_, ny_
        elif ok(nx_, m.pos[1]):
            m.pos[0] = nx_
            m.vel[1] = 0
        elif ok(m.pos[0], ny_):
            m.pos[1] = ny_
            m.vel[0] = 0
        else:
            m.vel[0] = m.vel[1] = 0
        m.pos[2] += m.vel[2] * DT
        s = MAP.surf_at(m.pos[0], m.pos[1])
        if m.pos[2] <= s:
            m.pos[2] = s
            m.vel[2] = max(0.0, m.vel[2])
            m.grounded = True
        else:
            m.grounded = m.pos[2] - s < 0.05
        # aim slew
        ca = cmd.get("aim", m.aim)
        dy_ = wrap(ca[0] - m.aim[0])
        mx = st["turn_rate"] * DT
        m.aim[0] = wrap(m.aim[0] + max(-mx, min(mx, dy_)))
        dp = max(st["min_pitch"], min(st["max_pitch"], ca[1])) - m.aim[1]
        mp = st["pitch_rate"] * DT
        m.aim[1] += max(-mp, min(mp, dp))

    def damage(self, victim, amount, weapon, attacker, src):
        if not victim.alive:
            return
        rear = False
        if src is not None:
            bearing = math.atan2(src[1] - victim.pos[1], src[0] - victim.pos[0])
            if abs(wrap(bearing - victim.aim[0])) > math.pi / 2:
                amount *= 1.5
                rear = True
        else:
            bearing = None
        victim.hp -= amount
        victim.last_combat = self.t
        victim.events.append({"type": "damaged", "amount": round(amount, 1), "weapon": weapon, "bearing": bearing,
                              "by": attacker.id if attacker else None, "rear": rear})
        if attacker:
            attacker.last_combat = self.t
            attacker.events.append({"type": "hit", "target": victim.id, "amount": amount, "weapon": weapon})
            if victim.id == 0 and victim.hp > 0 and attacker.kind == "hunter":
                pass
        if victim.hp <= 0:
            victim.alive = False
            self.deaths.append((self.t, victim.id))
            victim.place = sum(1 for o in self.mechs if o.alive) + 1
            if attacker:
                attacker.events.append({"type": "kill", "target": victim.id})

    def fire_weapons(self, m):
        fire = m.cmd.get("fire", [False, False]) or [False, False]
        for i, w in enumerate(m.weapons):
            if w is None:
                continue
            w["cooldown"] = max(0.0, w["cooldown"] - DT)
            if w["kind"] == "gun":
                if w["reload"] > 0:
                    w["reload"] = max(0.0, w["reload"] - DT)
                    if w["reload"] == 0:
                        w["ammo"] = w["stats"]["mag"]
                    continue
                if m.cmd.get("reload") and w["ammo"] < w["stats"]["mag"]:
                    w["reload"] = w["stats"]["reload"]
                    continue
                if fire[i] and w["cooldown"] <= 0 and w["ammo"] > 0:
                    w["cooldown"] = w["stats"]["cooldown"]
                    w["ammo"] -= 1
                    if w["ammo"] == 0:
                        w["reload"] = w["stats"]["reload"]
                    sp = math.radians(w["stats"]["spread_deg"])
                    yaw = m.aim[0] + self.rng.gauss(0, sp)
                    pit = m.aim[1] + self.rng.gauss(0, sp)
                    d = [math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit)]
                    o = [m.pos[0] + 3 * d[0], m.pos[1] + 3 * d[1], m.pos[2] + 7 + 3 * d[2]]
                    s = w["stats"]["speed"]
                    self.bullets.append({"id": len(self.bullets) + self.tick * 100, "owner": m.id, "pos": o,
                                         "vel": [d[0] * s, d[1] * s, d[2] * s], "left": w["stats"]["range"],
                                         "dmg": w["stats"]["damage"] * (1 + 0.04 * min(m.cubes, 10))})
            elif w["kind"] == "laser":
                st = w["stats"]
                if m.charge > 0:
                    m.charge += DT
                    if (not fire[i] and m.charge >= st["min_charge"]) or m.charge >= st["charge"] + st["hold_s"]:
                        frac = st["min_frac"] + (1 - st["min_frac"]) * min(1.0, m.charge / st["charge"])
                        self.laser(m, st["damage"] * frac, st["range"])
                        m.charge = 0.0
                        w["cooldown"] = st["cooldown"]
                elif fire[i] and w["cooldown"] <= 0:
                    m.charge = DT
            elif w["kind"] == "lightning":
                st = w["stats"]
                if fire[i] and w["cooldown"] <= 0 and m.energy >= st["energy"]:
                    w["cooldown"] = st["cooldown"]
                    m.energy -= st["energy"]
                    m.last_spend = self.t
                    best = None
                    for o in self.mechs:
                        if o is m or not o.alive:
                            continue
                        d = math.dist(o.pos[:2], m.pos[:2])
                        b = math.atan2(o.pos[1] - m.pos[1], o.pos[0] - m.pos[0])
                        if d < st["range"] and abs(wrap(b - m.aim[0])) < math.radians(st["cone_deg"]) / 2:
                            if MAP.los(m.pos[0], m.pos[1], m.pos[2] + 7, o.pos[0], o.pos[1], o.pos[2] + 4.5):
                                if best is None or d < best[0]:
                                    best = (d, o)
                    if best:
                        self.damage(best[1], st["damage"], "lightning", m, m.pos)

    def laser(self, m, dmg, rng):
        yaw, pit = m.aim
        d = [math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit)]
        o = [m.pos[0], m.pos[1], m.pos[2] + 7]
        best = None
        for v in self.mechs:
            if v is m or not v.alive:
                continue
            # closest approach of ray to cylinder axis (2D) and height check
            rx, ry = v.pos[0] - o[0], v.pos[1] - o[1]
            h2 = math.hypot(d[0], d[1])
            tt = (rx * d[0] + ry * d[1]) / max(h2 * h2, 1e-9)
            if tt <= 0 or tt > rng:
                continue
            px, py, pz = o[0] + d[0] * tt, o[1] + d[1] * tt, o[2] + d[2] * tt
            if math.hypot(px - v.pos[0], py - v.pos[1]) < 2.5 and v.pos[2] <= pz <= v.pos[2] + 9:
                if MAP.los(o[0], o[1], o[2], px, py, pz) and (best is None or tt < best[0]):
                    best = (tt, v)
        if best:
            self.damage(best[1], dmg, "laser", m, m.pos)

    def step_bullets(self):
        keep = []
        for b in self.bullets:
            p0 = b["pos"]
            p1 = [p0[k] + b["vel"][k] * DT for k in range(3)]
            seg = math.dist(p0, p1)
            hit = None
            for v in self.mechs:
                if v.id == b["owner"] or not v.alive:
                    continue
                # sample along segment
                for k in range(1, 7):
                    f = k / 6
                    x, y, z = p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f, p0[2] + (p1[2] - p0[2]) * f
                    if math.hypot(x - v.pos[0], y - v.pos[1]) < 2.8 and v.pos[2] - 0.3 <= z <= v.pos[2] + 9.3:
                        hit = v
                        break
                if hit:
                    break
            if hit:
                owner = self.mechs[b["owner"]]
                self.damage(hit, b["dmg"], "gun", owner, p0)
                continue
            if not MAP.los(p0[0], p0[1], p0[2], p1[0], p1[1], p1[2], step=1.0):
                continue
            b["pos"] = p1
            b["left"] -= seg
            if b["left"] > 0:
                keep.append(b)
        self.bullets = keep

    def run(self, max_t=480):
        hunters = [m for m in self.mechs if m.kind == "hunter"]
        for h in hunters:
            h.hunting = False
        timing = []
        last_print = -10
        while self.t < max_t:
            alive = [m for m in self.mechs if m.alive]
            if len(alive) <= 1 or (self.mode == "hunt" and not self.mechs[0].alive):
                break
            # hunters join: one early, more as the squad thins out, all late
            if self.mode == "hunt":
                killed = 15 - sum(1 for h in hunters if h.alive)
                n = 1 + killed + int(self.t / 90)
                order = sorted([h for h in hunters if h.alive], key=lambda h: h.skill)
                for k, h in enumerate(order):
                    h.hunting = k < n
            if self.tick % 2 == 0:
                for m in alive:
                    if m.kind == "bot":
                        o = self.obs_for(m)
                        t0 = time.perf_counter()
                        try:
                            a = m.brain.act(o)
                        except Exception:
                            import traceback
                            traceback.print_exc()
                            a = {}
                        timing.append(time.perf_counter() - t0)
                        mf.draw.clear()
                        for k in ("move", "aim", "boost"):
                            if k in a:
                                m.cmd[k] = a[k]
                        m.cmd["fire"] = a.get("fire", [False, False])
                        m.cmd["jump"] = a.get("jump", False)
                        m.cmd["reload"] = a.get("reload", False)
                        if "take" in a:
                            self.take(m, a["take"], a.get("drop"))
                    else:
                        m.cmd = self.hunter_cmd(m)
                        m.events = []
            for m in alive:
                self.step_mech(m)
                self.fire_weapons(m)
            self.step_bullets()
            cx, cy, r, dps = self.zone_now()
            for m in alive:
                if not m.alive:
                    continue
                if math.hypot(m.pos[0] - cx, m.pos[1] - cy) > r:
                    m.hp -= dps * DT
                    m.last_combat = self.t
                    if m.hp <= 0:
                        self.damage(m, 0.01, "zone", None, None)
                elif self.t - m.last_combat > 5 and m.hp < m.max_hp:
                    m.hp = min(m.max_hp, m.hp + 45 * DT)
            if self.verbose and self.t - last_print >= 5:
                last_print = self.t
                me = self.mechs[0]
                print("t=%5.1f me hp=%6.1f pos=(%6.1f,%6.1f,%5.1f) mode=%s alive=%d zone r=%.0f d=%.0f w=%s" % (
                    self.t, me.hp, me.pos[0], me.pos[1], me.pos[2], getattr(me.brain, "mode", "?"), len(alive), r,
                    math.hypot(me.pos[0] - cx, me.pos[1] - cy), [w and w["kind"] for w in me.weapons]))
            self.t += DT
            self.tick += 1
        # placement for survivors by hp
        alive = sorted([m for m in self.mechs if m.alive], key=lambda m: -m.hp)
        for k, m in enumerate(alive):
            m.place = k + 1
        timing.sort()
        return self.mechs, timing

    def take(self, m, pid, drop):
        for p in self.pickups:
            if p["id"] == pid and math.dist(p["pos"][:2], m.pos[:2]) <= 4:
                slot = 1 if m.weapons[1] is None else (drop if drop is not None else None)
                if slot is None:
                    return
                st = dict(CFG["weapons"][p["kind"]])
                m.weapons[slot] = {"kind": p["kind"], "cooldown": 1.0, "stats": st}
                if p["kind"] == "laser":
                    m.weapons[slot]["charge"] = 0.0
                self.pickups.remove(p)
                m.events.append({"type": "took", "id": pid, "kind": p["kind"], "slot": slot})
                return


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=3)
    ap.add_argument("--mode", default="hunt")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    scores = []
    for k in range(a.matches):
        s = Sim(a.mode, a.seed + k, verbose=a.v)
        mechs, timing = s.run()
        me = mechs[0]
        kills = sum(1 for (t, v) in s.deaths if v != 0)
        death_t = next((t for (t, v) in s.deaths if v == 0), None)
        scores.append(16 - me.place)
        print("match %d: place %d (score %d) died at %s kills-ish %d act p50 %.1fms p99 %.1fms" % (
            k, me.place, 16 - me.place, "%.1f" % death_t if death_t else "-", kills,
            1000 * timing[len(timing) // 2], 1000 * timing[int(len(timing) * 0.99)]))
    print("mean score %.2f" % (sum(scores) / len(scores)))
