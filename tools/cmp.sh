#!/bin/sh
# usage: tools/cmp.sh N  -> mean sim score of working tree over N seeds (4 parallel chunks)
N=${1:-40}; C=$((N/4))
for k in 0 1 2 3; do python3 tools/sim.py --matches $C --seed $((1000+k*C)) 2>/dev/null | tail -1 & done; wait
