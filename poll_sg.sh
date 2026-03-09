#!/bin/bash
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  echo "$(date) attempt $i"
  result=$(curl -s --max-time 5 http://127.0.0.1:17000/health 2>&1)
  if [ -n "$result" ]; then
    echo "READY: $result"
    break
  fi
  ss -tlnp | grep -E "17000|18000" || echo "(not listening)"
  sleep 30
done
echo "Poll finished"
