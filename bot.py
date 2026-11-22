"""LondonNo1 mech bot: survive first, fight on good terms, stay in the zone.

Speaks mechfight/1 through the kit wrapper (mechfight.py). Standard library only.
"""
import math
import os
import random
import sys
import time

import mechfight as mf

try:
    import nav
except Exception:  # pragma: no cover
    nav = None

TAU = 2 * math.pi
BUDGET_S = 0.022
RETREAT_HP = 0.42
RECOVER_HP = 0.85
PEEK = True             # duck while reloading, peek when armed (sim-only so far)

UPGRADE_PRIORITY = [
    "armor", "plating", "nanites", "laser_charge", "laser_damage", "gun_rate", "gun_damage",
    "lightning_efficiency", "servos", "gun_velocity", "gyros", "lightning_range", "gun_mag",
    "capacitor", "jets", "reactor", "sensors", "laser_pierce", "lightning_chain",
    "mortar_reload", "mortar_velocity", "mortar_splash",
]
WEAPON_VALUE = {"laser": 3, "lightning": 2, "mortar": 0, "gun": 0}


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Bot:
    name = "LondonNo1"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.me_id = hello["slot"]
        self.rng = random.Random(hello.get("seed", 1))
        cfg = self.cfg
        self.reach = cfg["loot"]["pickup_radius"]
        self.cube_reach = cfg["cubes"]["pickup_radius"]
        self.size_per_cube = cfg["cubes"].get("size_per_cube", 0.05)
        self.map = None
        if nav is not None:
            dirs = []
            try:
                dirs.append(hello["map"]["dir"])
            except Exception:
                pass
            here = os.path.dirname(os.path.abspath(__file__))
            dirs += ["/arena", os.path.join(here, "arena"), os.path.join(here, "..", "mechfight-kit", "arena")]
            for d in dirs:
                try:
                    if d and os.path.exists(os.path.join(d, "v2", "world.json")):
                        self.map = nav.Map(d)
                        log("map loaded from %s in %.2fs" % (d, self.map.build_s))
                        break
                except Exception as e:
                    log("map load failed", d, e)
        self.flow = None
        self.flow_key = None
        self.strafe_dir = 1
        self.strafe_until = 0.0
        self.pos_hist = []
        self.stuck_until = 0.0
        self.unstick_dir = None
        self.last_dir = None
        self.cover = None
        self.cover_t = -10.0
        self.last_damage_t = -100.0
        self.last_damage_bearing = None
        self.last_threat_by = None
        self.attackers = {}      # id -> last time it damaged us
        self.enemy_seen = {}     # id -> (t, pos, vel)
        self.laser_hold_t = None
        self.retreating = False
        self.peek = None
        self.scan_yaw = None
        self.mode = ""
        self.last_aim = [0.0, 0.0]
        self.blocked_pickups = {}
        self.t = 0.0

    # ------------------------------------------------------------------ helpers
    def los(self, a, b):
        if self.map is None:
            return True
        return self.map.los(a[0], a[1], a[2], b[0], b[1], b[2])

    def mech_center(self, m):
        s = 1 + self.size_per_cube * (m.get("cubes") or 0)
        return [m["pos"][0], m["pos"][1], m["pos"][2] + 4.5 * s]

    def walk_ok(self, x, y, dx, dy, dist):
        """Can we walk from (x,y) along unit (dx,dy) for `dist` meters without a tall step up?"""
        m = self.map
        if m is None:
            return True
        prev = m.surf_at(x, y)
        d = 2.0
        while d <= dist + 1e-6:
            px, py = x + dx * d, y + dy * d
            if not m.inside(px, py, 4.0):
                return False
            s = m.surf_at(px, py)
            if s - prev > 1.15:
                return False
            # body width: check the sides too
            sx, sy = -dy * 1.8, dx * 1.8
            if m.surf_at(px + sx, py + sy) - prev > 1.6 or m.surf_at(px - sx, py - sy) - prev > 1.6:
                return False
            prev = s
            d += 2.0
        return True

    def steer(self, x, y, dx, dy, look=6.0):
        """Pick a walkable direction close to the desired one."""
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return 0.0, 0.0
        dx, dy = dx / L, dy / L
        base = math.atan2(dy, dx)
        sgn = 1.0
        if self.last_dir is not None and wrap(self.last_dir - base) < 0:
            sgn = -1.0  # try the side we turned to last time first
        offs = [0.0]
        for o in (0.35, 0.7, 1.05, 1.4, 1.9, 2.4):
            offs += [sgn * o, -sgn * o]
        offs.append(math.pi)
        for o in offs:
            a = base + o
            cx, cy = math.cos(a), math.sin(a)
            if self.walk_ok(x, y, cx, cy, look):
                self.last_dir = a
                return cx, cy
        self.last_dir = None
        return dx, dy

    def zone_info(self, obs):
        z = obs["zone"]
        t = obs["t"]
        cx, cy = z["center"]
        r = z["radius"]
        nx_, ny_ = z["next_center"]
        nr = z["next_radius"]
        s0, s1 = z["shrink_start"], z["shrink_end"]
        if t >= s0 and s1 > s0:
            f = min(1.0, (t - s0) / (s1 - s0))
            cx, cy, r = cx + (nx_ - cx) * f, cy + (ny_ - cy) * f, r + (nr - r) * f
        return (cx, cy, r), (nx_, ny_, nr), s0, s1

    def update_flow(self, nxt):
        if self.map is None:
            return
        key = (round(nxt[0], 1), round(nxt[1], 1), round(nxt[2], 1))
        if key != self.flow_key:
            m = self.map
            rg = max(4.0, nxt[2] * 0.55)
            goals = []
            c0x, c0y = nxt[0], nxt[1]
            I0 = int((c0x - m.minx) / nav.CRES)
            J0 = int((c0y - m.miny) / nav.CRES)
            R = int(rg / nav.CRES) + 1
            for J in range(max(0, J0 - R), min(m.cny, J0 + R + 1)):
                for I in range(max(0, I0 - R), min(m.cnx, I0 + R + 1)):
                    c = J * m.cnx + I
                    px, py = m.ccenter(c)
                    if math.hypot(px - c0x, py - c0y) <= rg and not m.cmix[c]:
                        goals.append(c)
            if not goals:
                goals = [m.ccell(c0x, c0y)]
            self.flow = nav.FlowField(m, goals, key)
            self.flow_key = key

    def find_cover(self, x, y, z, threats, nxt):
        """A nearby walkable point hidden from every threat eye position."""
        m = self.map
        if m is None or not threats:
            return None
        my_s = m.surf_at(x, y)
        best = None
        bests = 1e18
        t_end = time.perf_counter() + 0.006
        for r in (6.0, 11.0, 17.0, 24.0, 32.0):
            for k in range(16):
                a = k * TAU / 16
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                if not m.inside(px, py, 6.0):
                    continue
                s = m.surf_at(px, py)
                if s - my_s > 1.2:
                    continue
                if math.hypot(px - nxt[0], py - nxt[1]) > nxt[2] + 30 and nxt[2] > 0:
                    zpen = 60.0
                else:
                    zpen = 0.0
                score = r + zpen
                if score >= bests:
                    continue
                if not self.walk_ok(x, y, math.cos(a), math.sin(a), r):
                    continue
                hidden = True
                for (ex, ey, ez) in threats:
                    if m.los(ex, ey, ez, px, py, s + 4.5) or m.los(ex, ey, ez, px, py, s + 8.5):
                        hidden = False
                        break
                if hidden:
                    best = (px, py)
                    bests = score
                if time.perf_counter() > t_end:
                    return best
            if best is not None:
                return best
        return best

    def find_peek(self, x, y, z, tc):
        """Nearest walkable point with line of sight from our muzzle height to the target center."""
        m = self.map
        if m is None:
            return None
        my_s = m.surf_at(x, y)
        t_end = time.perf_counter() + 0.005
        for r in (4.0, 8.0, 13.0, 19.0, 26.0):
            best = None
            bd = 1e18
            for k in range(12):
                a = k * TAU / 12
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                if not m.inside(px, py, 6.0):
                    continue
                s = m.surf_at(px, py)
                if s - my_s > 1.2:
                    continue
                dd = math.hypot(px - tc[0], py - tc[1])
                if dd >= bd:
                    continue
                if not self.walk_ok(x, y, math.cos(a), math.sin(a), r):
                    continue
                if m.los(px, py, s + 7.0, tc[0], tc[1], tc[2]):
                    best = (px, py)
                    bd = dd
            if best is not None or time.perf_counter() > t_end:
                return best
        return None

    def intercept(self, src, tgt, vel, speed):
        """Point to aim at so a straight projectile at `speed` meets a target moving at `vel`."""
        px, py, pz = tgt
        t = 0.0
        for _ in range(4):
            ax, ay, az = px + vel[0] * t, py + vel[1] * t, pz + vel[2] * t * 0.3
            d = math.sqrt((ax - src[0]) ** 2 + (ay - src[1]) ** 2 + (az - src[2]) ** 2)
            t = d / speed
        return [px + vel[0] * t, py + vel[1] * t, pz + vel[2] * t * 0.3], t

    @staticmethod
    def aim_to(src, p):
        dx, dy, dz = p[0] - src[0], p[1] - src[1], p[2] - src[2]
        return [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t_start = time.perf_counter()
        me = obs["self"]
        t = obs["t"]
        self.t = t
        x, y, z = me["pos"]
        st = me["stats"]
        hp, max_hp = me["hp"], me["max_hp"]
        hpf = hp / max(1.0, max_hp)
        act = {"tick": obs["tick"]}
        dbg = []

        # ---- upgrades
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            ids = [c["id"] for c in offer["choices"]]
            best = min(range(len(ids)), key=lambda i: UPGRADE_PRIORITY.index(ids[i]) if ids[i] in UPGRADE_PRIORITY else 99)
            act["pick"] = best

        # ---- weapons
        weapons = me["weapons"]
        slot_of = {}
        for i, w in enumerate(weapons):
            if w:
                slot_of[w["kind"]] = i
        muzzle = [x, y, z + st.get("muzzle_height", 7)]
        eye = [x, y, z + st.get("eye_height", 8)]

        # ---- events
        for e in obs.get("events", []):
            et = e.get("type")
            if et == "damaged" and e.get("by") is not None:
                self.last_damage_t = t
                self.last_damage_bearing = e.get("bearing")
                self.attackers[e["by"]] = t
            elif et == "take_failed":
                self.blocked_pickups[e.get("id")] = t

        # ---- zone
        cur, nxt, s0, s1 = self.zone_info(obs)
        self.update_flow(nxt)
        d_cur = math.hypot(x - cur[0], y - cur[1])
        d_next = math.hypot(x - nxt[0], y - nxt[1])
        in_next_safe = d_next < max(0.0, nxt[2] - max(8.0, nxt[2] * 0.12))
        walk = st.get("walk_speed", 11.7)
        time_to_shrink = s0 - t
        need_zone = (not me["in_zone"]) or d_cur > cur[2] - 10 or (
            not in_next_safe and (time_to_shrink < (d_next - nxt[2] * 0.7) / walk + 25 or t >= s0))

        # ---- enemies
        enemies = []
        npcs = []
        for m in obs.get("mechs", []):
            if m["kind"] == "mech":
                if m["id"] == me["id"]:
                    continue
                enemies.append(m)
            else:
                npcs.append(m)
        vis = []
        for m in enemies:
            c = self.mech_center(m)
            d = math.dist(c[:2], (x, y))
            has_los = self.los(muzzle, c)
            vis.append((m, c, d, has_los))
        # target choice: line of sight first, then near and weak, attackers preferred
        target = None
        best = 1e18
        for (m, c, d, has_los) in vis:
            sc = d + 0.04 * m.get("hp", 1400)
            if not has_los:
                sc += 400
            if t - self.attackers.get(m["id"], -100) < 3:
                sc -= 40
            if sc < best:
                best = sc
                target = (m, c, d, has_los)
        threats = []
        for (m, c, d, has_los) in vis:
            if d < 260:
                s = 1 + self.size_per_cube * (m.get("cubes") or 0)
                threats.append((m["pos"][0], m["pos"][1], m["pos"][2] + 8 * s))

        # ---- aiming and firing
        fire = [False, False]
        aim = None
        gun = weapons[slot_of["gun"]] if "gun" in slot_of else None
        laser_i = slot_of.get("laser")
        light_i = slot_of.get("lightning")
        mortar_i = slot_of.get("mortar")
        my_aim = me["aim"]
        laser_charging = laser_i is not None and (weapons[laser_i].get("charge") or 0) > 0
        if target is not None:
            m, c, d, has_los = target
            tv = m.get("vel", [0, 0, 0])
            gspeed = gun["stats"]["speed"] if gun else 252
            p, tof = self.intercept(muzzle, c, tv, gspeed)
            aim_gun = self.aim_to(muzzle, p)
            aim_direct = self.aim_to(muzzle, c)
            s = 1 + self.size_per_cube * (m.get("cubes") or 0)
            tol = max(0.006, (2.0 * s) / max(d, 1.0))
            # laser takes priority on the torso while charging
            use_laser = laser_i is not None and has_los and d < weapons[laser_i]["stats"].get("range", 280) * 0.95
            if use_laser and (laser_charging or weapons[laser_i]["cooldown"] <= 0.15):
                aim = aim_direct
            else:
                aim = aim_gun
            yaw_err = abs(wrap(my_aim[0] - aim[0]))
            pit_err = abs(my_aim[1] - aim[1])
            # gun
            if gun and has_los and d < gun["stats"].get("range", 230) * 0.98:
                ge_y = abs(wrap(my_aim[0] - aim_gun[0]))
                ge_p = abs(my_aim[1] - aim_gun[1])
                if ge_y < tol * 1.3 and ge_p < tol * 1.5 + 0.01 and (gun.get("ammo") or 0) > 0 and (gun.get("reload") or 0) <= 0:
                    fire[slot_of["gun"]] = True
            # laser
            if laser_i is not None:
                lw = weapons[laser_i]
                full = lw["stats"].get("charge", 0.75)
                ch = lw.get("charge") or 0
                if ch > 0:
                    if not (use_laser and yaw_err < tol and pit_err < tol + 0.01 and ch >= full - 0.06):
                        fire[laser_i] = True  # keep holding until on target at full charge
                    if ch >= full + 1.6:
                        fire[laser_i] = False  # avoid the blind auto-fire, let go now
                elif use_laser and lw["cooldown"] <= 0.05 and yaw_err < 0.5:
                    fire[laser_i] = True
            # lightning
            if light_i is not None:
                lw = weapons[light_i]
                rng = lw["stats"].get("range", 45)
                cone = math.radians(lw["stats"].get("cone_deg", 30))
                if has_los and d < rng * 0.95 and abs(wrap(my_aim[0] - aim_direct[0])) < cone * 0.4 and me["energy"] > 8:
                    fire[light_i] = True
            # mortar (only if we happen to hold one)
            if mortar_i is not None and 25 < d < 210:
                mw = weapons[mortar_i]
                v = mw["stats"].get("speed", 72)
                g = mw["stats"].get("gravity", 22)
                h = c[2] - 4.5 * s * 0.8 - (z + st.get("height", 9) + 0.5)
                disc = v ** 4 - g * (g * d * d + 2 * h * v * v)
                if disc > 0 and mw["cooldown"] <= 0:
                    th = math.atan((v * v - math.sqrt(disc)) / (g * d)) if has_los else math.atan((v * v + math.sqrt(disc)) / (g * d))
                    if -0.6 < th < 1.25 and not laser_charging and not fire[slot_of.get("gun", 0)]:
                        flight = d / (v * math.cos(th))
                        lead = [c[0] + tv[0] * flight, c[1] + tv[1] * flight]
                        yaw_m = math.atan2(lead[1] - y, lead[0] - x)
                        if abs(wrap(my_aim[0] - yaw_m)) < 0.03 and abs(my_aim[1] - th) < 0.03:
                            fire[mortar_i] = True
                        elif not has_los:
                            aim = [yaw_m, th]
            dbg.append(("line", [x, y, z + 7], c, "#ff5040"))
        else:
            # keep a charging laser from wasting itself: let it auto-fire late only
            if laser_charging:
                ch = weapons[laser_i].get("charge") or 0
                fire[laser_i] = ch < weapons[laser_i]["stats"].get("charge", 0.75) + 1.0
            # secondary targets: NPC drones and crates when nothing threatens us
            sec = None
            if hpf > 0.5:
                bestd = 1e18
                for n in npcs:
                    c = [n["pos"][0], n["pos"][1], n["pos"][2] + 1.75]
                    d = math.dist(c, muzzle)
                    if d < 110 and d < bestd and self.los(muzzle, c):
                        bestd = d
                        sec = (c, n.get("vel", [0, 0, 0]), d)
                if sec is None and not need_zone:
                    for cr in obs.get("crates", []):
                        c = [cr["pos"][0], cr["pos"][1], cr["pos"][2] + 1.2]
                        d = math.dist(c, muzzle)
                        if d < 70 and d < bestd and self.los(muzzle, c):
                            bestd = d
                            sec = (c, [0, 0, 0], d)
            if sec is not None and gun:
                c, v, d = sec
                p, _ = self.intercept(muzzle, c, v, gun["stats"]["speed"])
                aim = self.aim_to(muzzle, p)
                tol = max(0.006, 1.3 / max(d, 1))
                if abs(wrap(my_aim[0] - aim[0])) < tol and abs(my_aim[1] - aim[1]) < tol + 0.01 and (gun.get("ammo") or 0) > 0:
                    fire[slot_of["gun"]] = True
            elif t - self.last_damage_t < 4 and self.last_damage_bearing is not None:
                aim = [self.last_damage_bearing, 0.0]
            else:
                heard = None
                for snd in obs.get("sounds", []):
                    if snd["kind"] in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "mortar", "reload"):
                        if heard is None or snd["dist"] == "near":
                            heard = snd
                if heard is not None:
                    self.scan_yaw = heard["bearing"]
                    self.scan_t = t
                if self.scan_yaw is not None and t - getattr(self, "scan_t", -10) < 3:
                    aim = [self.scan_yaw, 0.0]
        # reload when empty or idle
        if gun and (gun.get("reload") or 0) <= 0:
            ammo = gun.get("ammo") or 0
            if ammo == 0 or (target is None and ammo < gun["stats"].get("mag", 12) * 0.5):
                act["reload"] = True

        # ---- movement
        move = None
        boost = False
        jump = False
        self.mode = ""
        threat_near = target is not None and target[3] and target[2] < 200
        # weapon pickups
        want = None
        if not threat_near:
            bestd = 1e18
            for pk in obs.get("pickups", []):
                if pk["kind"] in slot_of or WEAPON_VALUE.get(pk["kind"], 0) <= 0:
                    continue
                if t - self.blocked_pickups.get(pk["id"], -100) < 3:
                    continue
                cur_val = max([WEAPON_VALUE.get(w["kind"], 0) for i, w in enumerate(weapons) if w and i == 1] or [-1])
                if WEAPON_VALUE[pk["kind"]] <= cur_val:
                    continue
                d = math.dist(pk["pos"][:2], (x, y))
                if abs(pk["pos"][2] - z) > 3.5 and d < 6:
                    continue
                if d < 110 and d < bestd and (math.hypot(pk["pos"][0] - cur[0], pk["pos"][1] - cur[1]) < cur[2] - 15):
                    bestd = d
                    want = pk
            if want is not None and bestd < self.reach - 0.3 and abs(want["pos"][2] - z) < 3.8:
                act["take"] = want["id"]
                if weapons[1] is not None:
                    act["drop"] = 1
        # cubes
        cube_goal = None
        if not threat_near:
            bestd = 1e18
            for cb in obs.get("cubes", []):
                d = math.dist(cb["pos"][:2], (x, y))
                if d < 90 and d < bestd and abs(cb["pos"][2] - z) < 4:
                    bestd = d
                    cube_goal = cb["pos"]

        recently_hit = t - self.last_damage_t < 3
        n_los = len([v for v in vis if v[3] and v[2] < 180])
        if hpf < RETREAT_HP or (n_los >= 2 and hpf < 0.7):
            self.retreating = True
        elif hpf > RECOVER_HP or (target is None and hpf > 0.6 and not recently_hit and t - self.last_damage_t > 12):
            self.retreating = False
        retreat = self.retreating and (target is not None or recently_hit or hpf < RECOVER_HP)
        # while recovering, hold fire unless pressed: shooting resets the regen timer
        if retreat and target is not None and not (t - self.last_damage_t < 2.5 or target[2] < 50):
            if not laser_charging:
                fire = [False, False]
        if need_zone:
            self.mode = "zone"
            dirv = self.flow.direction(x, y) if (self.flow is not None and self.flow.dist[self.map.ccell(x, y)] < 1e17) else None
            if dirv is None:
                dirv = (nxt[0] - x, nxt[1] - y)
            move = list(dirv)
            boost = (not me["in_zone"]) or d_cur > cur[2] - 20 or (t >= s0 and not in_next_safe)
        elif retreat or (target is None and recently_hit and hpf < 0.6):
            self.mode = "cover"
            th = threats if threats else []
            if not th and self.last_damage_bearing is not None:
                b = self.last_damage_bearing
                th = [(x + 80 * math.cos(b), y + 80 * math.sin(b), z + 8)]
            if t - self.cover_t > 0.5 or self.cover is None:
                self.cover = self.find_cover(x, y, z, th, nxt)
                self.cover_t = t
            if self.cover is not None:
                move = [self.cover[0] - x, self.cover[1] - y]
                if math.hypot(*move) < 1.5:
                    move = [0.0, 0.0]
                boost = me["energy"] > 25
            elif target is not None:
                # no cover: back away from the target and strafe
                m, c, d, _ = target
                ax_, ay_ = (x - c[0]) / max(d, 1), (y - c[1]) / max(d, 1)
                move = [ax_ - ay_ * self.strafe_dir * 0.8, ay_ + ax_ * self.strafe_dir * 0.8]
                boost = me["energy"] > 25
        elif target is not None:
            self.mode = "fight"
            m, c, d, has_los = target
            if t > self.strafe_until:
                self.strafe_dir = -self.strafe_dir if self.rng.random() < 0.7 else self.strafe_dir
                self.strafe_until = t + self.rng.uniform(0.6, 1.6)
            ux, uy = (c[0] - x) / max(d, 1), (c[1] - y) / max(d, 1)
            if light_i is not None and hpf > 0.6:
                lo, hi = 15, 35
            elif laser_i is not None:
                lo, hi = 70, 170
            else:
                lo, hi = 45, 120
            # can we hurt it right now?
            gun_ready = gun is not None and (gun.get("ammo") or 0) > 0 and (gun.get("reload") or 0) <= 0
            laser_ready = laser_i is not None and (weapons[laser_i].get("charge") or 0) >= weapons[laser_i]["stats"].get("charge", 0.75) - 0.1
            light_ready = light_i is not None and d < 45 and me["energy"] > 10
            armed = gun_ready or laser_ready or light_ready
            if PEEK and not armed and has_los and d > 40:
                # duck out of sight while reloading / charging
                self.mode = "duck"
                if t - self.cover_t > 0.5 or self.cover is None:
                    self.cover = self.find_cover(x, y, z, threats, nxt)
                    self.cover_t = t
                if self.cover is not None:
                    move = [self.cover[0] - x, self.cover[1] - y]
                    if math.hypot(*move) < 1.5:
                        move = [0.0, 0.0]
                else:
                    move = [-uy * self.strafe_dir - ux * 0.3, ux * self.strafe_dir - uy * 0.3]
                boost = me["energy"] > 30
            elif PEEK and not has_los:
                self.mode = "peek"
                if armed or hpf > 0.7:
                    if t - getattr(self, "peek_t", -10) > 0.5:
                        self.peek = self.find_peek(x, y, z, c)
                        self.peek_t = t
                    if self.peek is not None:
                        move = [self.peek[0] - x, self.peek[1] - y]
                    else:
                        move = [ux * 0.6, uy * 0.6]
                else:
                    move = [0.0, 0.0]
            else:
                radial = 0.0
                if d > hi:
                    radial = 1.0
                elif d < lo:
                    radial = -1.0
                if not has_los:
                    radial = 0.4 if hpf > 0.7 else 0.0
                px_, py_ = -uy * self.strafe_dir, ux * self.strafe_dir
                move = [ux * radial + px_ * 0.9, uy * radial + py_ * 0.9]
                boost = me["energy"] > 50 and d < 120
        elif want is not None:
            self.mode = "loot"
            move = [want["pos"][0] - x, want["pos"][1] - y]
            if math.hypot(*move) < 1.0:
                move = [0.0, 0.0]
        elif cube_goal is not None:
            self.mode = "cube"
            move = [cube_goal[0] - x, cube_goal[1] - y]
        else:
            self.mode = "roam"
            if not in_next_safe or d_next > nxt[2] * 0.6:
                dirv = self.flow.direction(x, y) if (self.flow is not None and self.flow.dist[self.map.ccell(x, y)] < 1e17) else None
                if dirv is None:
                    dirv = (nxt[0] - x, nxt[1] - y)
                move = [dirv[0] * 0.8, dirv[1] * 0.8]
            else:
                # quiet: slow walk below step-sound speed (silent), or hold still
                move = [0.0, 0.0]
            if hpf < 0.95 and not recently_hit:
                move = [move[0] * 0.35, move[1] * 0.35]
        # shells: step away from predicted impact points
        for pr in obs.get("projectiles", []):
            if pr["kind"] != "mortar":
                continue
            px, py, pz = pr["pos"]
            vx, vy, vz = pr["vel"]
            g = self.cfg["weapons"]["mortar"].get("gravity", 22)
            gz = self.map.surf_at(px, py) if self.map else z
            # time to fall to our height
            a, b_, cc = -0.5 * g, vz, pz - z
            disc = b_ * b_ - 4 * a * cc
            if disc < 0:
                continue
            tt = (-b_ - math.sqrt(disc)) / (2 * a)
            if tt <= 0:
                continue
            ix, iy = px + vx * tt, py + vy * tt
            dd = math.hypot(ix - x, iy - y)
            rad = self.cfg["weapons"]["mortar"].get("splash_radius", 7) + 4
            if dd < rad and tt < 3:
                awx, awy = (x - ix) / max(dd, 0.5), (y - iy) / max(dd, 0.5)
                move = [awx, awy]
                boost = True
        if move is None:
            move = [0.0, 0.0]
        mlen = math.hypot(*move)
        if mlen > 1e-6:
            scale = min(1.0, mlen)
            sx, sy = self.steer(x, y, move[0], move[1])
            move = [sx * scale, sy * scale]
        # stuck detection
        self.pos_hist.append((t, x, y, mlen > 0.5))
        while self.pos_hist and t - self.pos_hist[0][0] > 1.5:
            self.pos_hist.pop(0)
        if t < self.stuck_until and self.unstick_dir is not None:
            move = list(self.unstick_dir)
            jump = me["grounded"]
            boost = True
        elif len(self.pos_hist) > 10 and all(h[3] for h in self.pos_hist):
            h0 = self.pos_hist[0]
            if math.hypot(x - h0[1], y - h0[2]) < 1.5 and t - h0[0] > 1.2:
                self.stuck_until = t + 1.0
                if self.rng.random() < 0.5:
                    self.unstick_dir = (move[0], move[1])
                else:
                    a = self.rng.uniform(0, TAU)
                    self.unstick_dir = (math.cos(a), math.sin(a))
                self.pos_hist = []
        # keep a little energy in reserve for jets
        if boost and me["energy"] < 12:
            boost = False

        if aim is None:
            # face where we are going, or keep watching the threat direction
            # sweep the view around us: 120 degree cone, so a full turn every few seconds
            aim = [my_aim[0] + 0.6, 0.0]
        pmin, pmax = st.get("min_pitch", -0.7), st.get("max_pitch", 1.3)
        aim = [wrap(aim[0]), max(pmin, min(pmax, aim[1]))]
        act.update(move=[round(move[0], 3), round(move[1], 3)], aim=[round(aim[0], 4), round(aim[1], 4)],
                   fire=fire, boost=bool(boost), jump=bool(jump))
        # leftover time: grow the flow field
        if self.flow is not None and not self.flow.done:
            left = BUDGET_S - (time.perf_counter() - t_start)
            if left > 0.002:
                self.flow.step(min(left, 0.008))
        mf.draw.label("%s hp%.0f %s" % (self.mode, hp, "T%d" % target[0]["id"] if target else ""))
        return act


if __name__ == "__main__":
    mf.run(Bot)
