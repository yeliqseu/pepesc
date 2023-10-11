# PEPesc：Performance Enhancing Proxy enhanced by streaming coding

PEPesc是一种新型TCP性能增强代理（PEP）。基于带宽估计进行拥塞控制，并基于一种称为流编码（Streaming Coding，SC）的分组前向纠错编码（FEC）实现无重传、可靠UDP传输。PEPesc支持双向数据传输，并支持拦截双向连接请求。PEPesc属于分布式PEP，需部署两个PEPesc实体分别于瓶颈链路两端。

下面以如图所示4节点网络拓扑为例，部署PEPesc于B和C节点。请根据你的网络实际情况，调整下文中的IP地址等设置。

![4节点网络拓扑](https://s2.loli.net/2022/08/20/PKbpVBHOykzQofg.jpg)

## 网络环境设置与验证
首先验证网络中路由已设置正确，可通过节点A和D分别使用Linux工具traceroute进行。节点A执行：

    traceroute 172.20.35.38

应显示类似以下结果：

> traceroute to 172.20.35.38 (172.20.35.38), 30 hops max, 60 byte packets
> 
>  1  172.20.35.34 (172.20.35.34)  5.947 ms  5.913 ms  5.898 ms
>  
>  2  172.20.35.92 (172.20.35.92)  601.088 ms  601.192 ms  601.170 ms
>  
>  3  172.20.35.38 (172.20.35.38)  602.153 ms  602.170 ms *

节点D执行：

    traceroute 172.20.35.37

应显示类似以下结果：

> traceroute to 172.20.35.37 (172.20.35.37), 30 hops max, 60 byte packets
> 
>  1  172.20.35.35 (172.20.35.35)  0.894 ms  0.631 ms  0.830 ms
>  
>  2  172.20.35.91 (172.20.35.91)  601.507 ms  601.582 ms  601.568 ms
>  
>  3  172.20.35.37 (172.20.35.37)  602.188 ms  602.174 ms  602.161 ms


## iptables流量拦截代理

PEPesc依靠iptables提供的代理工具TPROXY拦截TCP连接请求，将符合条件的TCP包拦截转发至指定的代理地址。以下假设PEPesc的TCP监听端口为9999。

### 配置流量拦截

节点B以root权限执行以下命令，拦截来自节点A的TCP流量，导向PEPesc所侦听的9999端口：

    sysctl -w net.ipv4.ip_forward=1
    iptables -t mangle -A PREROUTING -p tcp --source 172.20.35.37 --destination 172.20.35.38 -j TPROXY --on-port 9999 --tproxy-mark 1
    ip rule add fwmark 1 lookup 101
    ip route add local 0.0.0.0/0 dev lo table 101

节点C以root权限执行以下命令，拦截来自节点D的TCP流量，导向PEPesc所侦听的9999端口：：

    sysctl -w net.ipv4.ip_forward=1
    iptables -t mangle -A PREROUTING -p tcp --source 172.20.35.38 --destination 172.20.35.37 -j TPROXY --on-port 9999 --tproxy-mark 1
    ip rule add fwmark 1 lookup 101
    ip route add local 0.0.0.0/0 dev lo table 101

### 回退配置

若需回退配置，则将上述命令ip rule和ip route的命令中 `add` 修改为 `del` 。iptables中 `-A` 修改为 `-D` 按条删除即可，也可执行 `iptables -t mangle -F` ，删除表mangle所配置的规则。

## 部署PEPesc
### 编译和链接动态库
PEPesc基于流编码实现可靠UDP传输，运行前需要先编译和允许链接流编码动态库,执行

    git clone https://github.com/yeliqseu/streamc
    cd streamc/
    make clean
    make libstreamc.so
    mv libstreamc.so ../

回到pep.py所在目录下，以root权限执行以下命令，使得libstreamc.so可以被动态链接到：

    export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:./

### 运行PEPesc

执行 `python3 pep.py -h` 查看命令行参数说明：

> usage: pep.py [-h] --selfIp SELFIP --selfPort SELFPORT --peerIp PEERIP --peerPort PEERPORT [-d]
> 
> optional arguments:
> 
>   -h, --help   show this help message and exit
>   
>   --selfIp SELFIP  IP for local PEPesc to bind
>   
>   --selfPort SELFPORT  Port for local PEPesc to bind
>   
>   --peerIp PEERIP  Peer PEPesc's ip
>   
>   --peerPort PEERPORT  Peer PEPesc's port
>   
>   -d, --detail Displays the details

`--selfIp` 和 `--selfPort` 设置本地PEPesc地址和端口，`--peerIp` 和 `--peerPort` 向本地PEPesc告知对端PEPesc的地址和端口，这四个参数为必需参数。`--detail` 指定打印PEPesc详细日志，默认不显示。

PEPesc应以root权限执行。在上述4节点网络拓扑中，节点B网卡eth1为172.20.35.91， 节点C网卡eth1为172.20.35.92。假设PEPesc分别绑定各节点的eth1网卡，端口号为9999，并打开详细日志。则运行命令如下。

节点B ：

    python3 pep.py --selfIp 172.20.35.91 --selfPort 9999 --peerIp 172.20.35.92 --peerPort 9999 --detail

节点C：

    python3 pep.py --selfIp 172.20.35.92 --selfPort 9999 --peerIp 172.20.35.91 --peerPort 9999 --detail

注意两端PEPesc的地址端口需要对应，即本端的 `selfIp` 和 `selfPort` 为对端的 `peerIp` 和 `peerPort`。

## iperf测试
可在节点A和D分别运行iperf客户端和服务端，测试PEPesc。节点D运行：
```
iperf -s -p 10000 -i 1
```
节点A运行：
```
iperf -c 172.20.35.38 -p 10000 -i 1 -t 120
```

## 替代方案：使用Mininet模拟网络拓扑测试PEPesc

如果没有现成网络环境，我们提供了一个Mininet脚本，创建类似前面4节点线形网络拓扑，使用户能能够快速部署和试用PEPesc。

首先, 为root用户添加脚本可执行权限：

```
cd Mininet-scripts
chmod u+x *.sh
```

接着，以root权限运行Mininet网络模拟脚本：

```
sudo python3 4_nodes_topo.py
```

然后，打开四个节点主机终端：

```
xterm nodeA nodeB nodeC nodeD
```

节点B运行bash脚本：

```
sh deploy-proxy-on-node-b.sh
```

节点C运行bash脚本：

```
sh deploy-proxy-on-node-c.sh
```

最后，依据前文所示部署运行PEPesc。其中PEPesc使用的IP需要更改为`10.0.1.2`和`10.0.1.3`。

注：该脚本模拟卫星链路的默认条件为：带宽20Mbps，单向时延300ms，随机丢包率1%。若需要自定义，请更改脚本第52行。

## 论文引用

PEPesc的详细设计和测试结果，请参阅和引用如下论文：
<blockquote>
Ye Li, Liang Chen, Li Su, Kanglian Zhao, Jue Wang, Yongjie Yang, Ning Ge, "PEPesc: A TCP Performance Enhancing Proxy for Non-Terrestrial Networks", IEEE Transactions on Mobile Computing, 2023. (Early Access: https://ieeexplore.ieee.org/document/10107444)
</blockquote>
