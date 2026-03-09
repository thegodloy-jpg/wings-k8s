#!/bin/sh
# k3s-server container (HEAD on .148): OUTPUT DNAT HEAD→WORKER
# All TCP to 172.17.0.3 (except local ZMQ bind 13355) → redirect to WORKER at 7.6.16.150
iptables -t nat -I OUTPUT 1 -p tcp -d 172.17.0.3 ! --dport 13355 -j DNAT --to-destination 7.6.16.150
echo "HEAD OUTPUT DNAT added (HEAD→WORKER)"
iptables -t nat -L OUTPUT -n | grep 172.17.0.3
