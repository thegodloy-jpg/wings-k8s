#!/bin/bash
# In k3s-agent container on .150: add OUTPUT DNAT for fixed TCPStore ports
for port in 13357 13358 13359 13360 13361; do
    iptables -t nat -A OUTPUT -p tcp -d 172.17.0.3 --dport $port -j DNAT --to-destination 7.6.52.148
done
echo "DONE .150 OUTPUT DNAT rules"
