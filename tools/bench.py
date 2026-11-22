"""Feed the kit's recorded stream to Bot.act in-process and report CPU time per decision."""
import gzip, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mechfight as mf
mf.run = lambda *a, **k: None
import bot as B
stream = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "..", "mechfight-kit", "check", "stream.jsonl.gz")
lines = gzip.open(stream, "rt").read().splitlines()
hello = json.loads(lines[0])
hello["map"]["dir"] = os.path.join(HERE, "..", "..", "mechfight-kit", "arena")
c0 = time.process_time()
b = B.Bot(hello)
print("init cpu %.3fs" % (time.process_time() - c0))
ts = []
for l in lines[1:]:
    o = json.loads(l)
    if o["type"] != "obs":
        continue
    c0 = time.process_time()
    a = b.act(o)
    json.dumps(a)
    ts.append((time.process_time() - c0) * 1000)
ts.sort()
print("act cpu ms: p50 %.2f p90 %.2f p99 %.2f max %.2f" % (ts[len(ts)//2], ts[int(len(ts)*.9)], ts[int(len(ts)*.99)], ts[-1]))
