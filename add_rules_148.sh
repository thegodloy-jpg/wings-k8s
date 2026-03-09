#!/bin/bash
for port in 13357 13358 13359 13360 13361; do
    iptables -t nat -A PREROUTING -p tcp --dport $port -j DNAT --to-destination 172.17.0.3
    iptables -A FORWARD -p tcp -d 172.17.0.3 --dport $port -j ACCEPT
done
echo "DONE .148 fixed port rules"
