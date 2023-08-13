# PEPesc：Performance Enhancing Proxy enhanced by streaming coding

PEPesc is a novel TCP performance enhancing proxy (PEP). PEPesc performs congestion control between entities, and between the PEP entities and the original TCP senders based on bandwidth estimation. PEPesc is retransmission-free and uses an adaptive forward erasure correction (FEC) method called streaming coding (SC) as the loss recovery mechanism. PEPesc is a distributed PEP, meaning that two PEPesc entities sit on the two side of the bottleneck link.

In the following, a 4-node network topology is used to demonstrate the deployment and test of PEPesc. You should modify network settings accordingly in your own environment. The following procedures have been verified on Ubuntu 20.04.

![4-node network topology](https://s2.loli.net/2022/08/20/PKbpVBHOykzQofg.jpg)

## Network Environment Setup and Validation
Make sure that routing has been set correctly, such that node A and D communications via B and C. You may use traceroute to verify. For example, on node A:

    traceroute 172.20.35.38

It should disply something like this:

> traceroute to 172.20.35.38 (172.20.35.38), 30 hops max, 60 byte packets
> 
>  1  172.20.35.34 (172.20.35.34)  5.947 ms  5.913 ms  5.898 ms
>  
>  2  172.20.35.92 (172.20.35.92)  601.088 ms  601.192 ms  601.170 ms
>  
>  3  172.20.35.38 (172.20.35.38)  602.153 ms  602.170 ms *

Similarly, on node D：

    traceroute 172.20.35.37

## iptables for Traffic Interception

PEPesc relies on iptables's TPRORXY to intercept TCP connections, where the specified TCP flows are re-directed to the PEPesc's listen ports. In the following, port 9999 is assumed.specified port of PEPesc. 

### Configuration of iptables

Run the following commands on node B as root to intercept TCP traffic from node B:

    sysctl -w net.ipv4.ip_forward=1
    iptables -t mangle -A PREROUTING -p tcp --source 172.20.35.37 --destination 172.20.35.38 -j TPROXY --on-port 9999 --tproxy-mark 1
    ip rule add fwmark 1 lookup 101
    ip route add local 0.0.0.0/0 dev lo table 101

Run the following commands on node C to intercept TCP traffic from node D:

    sysctl -w net.ipv4.ip_forward=1
    iptables -t mangle -A PREROUTING -p tcp --source 172.20.35.38 --destination 172.20.35.37 -j TPROXY --on-port 9999 --tproxy-mark 1
    ip rule add fwmark 1 lookup 101
    ip route add local 0.0.0.0/0 dev lo table 101

## PEPesc Deployment
### Compile and Link to Streaming Coding Library
PEPesc needs SC to achieve reliable packet transmissions on top of UDP between the entities, so first clone and compile the SC libary. Run

    git clone https://github.com/yeliqseu/streamc
    cd streamc/
    make clean
    make libstreamc.so
    mv libstreamc.so ../

Back to the directory of pep.py, run the following as root allow the shared library be linkable:

    export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:./

### Run PEPesc

Run `python3 pep.py -h` to check supported arguments.

Among the arguments, `--selfIp` and `--selfPort` specify the IP address and listening port of the local PEPes, and `--peerIp` and `--peerPort` specify the IP address and port of the host where the other PEPesc entity resides. These 4 arguments are mandatory.

PEPesc should be run as root. Using the above 4-node topology as an example, run the following commands.

Node B:

    python3 pep.py --selfIp 172.20.35.91 --selfPort 9999 --peerIp 172.20.35.92 --peerPort 9999 --detail

Node C:

    python3 pep.py --selfIp 172.20.35.92 --selfPort 9999 --peerIp 172.20.35.91 --peerPort 9999 --detail

## iperf Test
Run iperf client and server on node A and D, respectively, to test PEPesc. On node D run:
```
iperf -s -p 10000 -i 1
```
On node A run:
```
iperf -c 172.20.35.38 -p 10000 -i 1 -t 120
```

## Alternative: Test PEPesc Using an Mininet-Emulated Network Topology

To allow users who may not have a ready network environment to quickly deploy and try PEPesc, here we also provide a mininet script, which simulates a 4-node line network topology. You can use the script to try PEPesc in the emulated network.

First, add executable permission for root :

```
cd Mininet-scripts
chmod u+x *.sh
```

Second, run the python script as root:

```
sudo python 4_nodes_topo.py
```

Thild, open the terminals of the hosts:

```
xterm nodeA nodeB nodeC nodeD
```

On NodeB, run the bash script:

```
sh deploy-proxy-on-node-b.sh
```

On NodeC, run the bash script:

```
sh deploy-proxy-on-node-c.sh
```

Then, use PEPesc according to the section "PEPesc Deployment".The IP used by PEPesc needs to be changed to `10.0.1.2` and `10.0.1.3`.

Ps: The default conditions for the mininet script to simulate a satellite link are: 20Mbps, 300ms, 1%. If you need to change, go to line 52 of the script.

## Paper Citation

The detailed design and experiment results have been accepted as a regular paper by _IEEE Transactions on Mobile Computing_. Please cite the paper when appropriate.

<blockquote>
Ye Li, Liang Chen, Li Su, Kanglian Zhao, Jue Wang, Yongjie Yang, Ning Ge, "PEPesc: A TCP Performance Enhancing Proxy for Non-Terrestrial Networks", IEEE Transactions on Mobile Computing, 2023. (Early Access: https://ieeexplore.ieee.org/document/10107444)
</blockquote>
