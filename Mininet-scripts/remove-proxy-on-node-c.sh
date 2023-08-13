iptables -t mangle -D PREROUTING -p tcp --source 10.0.2.4 --destination 10.0.0.1 -j TPROXY --on-port 9999 --tproxy-mark 1
ip rule del fwmark 1 lookup 101
ip route del local 0.0.0.0/0 dev lo table 101