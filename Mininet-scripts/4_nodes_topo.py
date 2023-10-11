import os

from mininet.net import Mininet
from mininet.node import Node, Controller, RemoteController
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.cli import CLI
from mininet.log import info, setLogLevel

setLogLevel('info')

"""
  node name     |     node A             node B                       node C             node D
eth1 address    |    10.0.0.1 --------- 10.0.0.2                     10.0.2.3 --------- 10.0.2.4
eth2 address    |                       10.0.1.2 ------------------- 10.0.1.3
Link conditions |                                 20Mbps, 300ms, 1%
"""

nodeA_eth1 = '10.0.0.1/24'
nodeB_eth1 = '10.0.0.2/24'

nodeB_eth2 = '10.0.1.2/24'
nodeC_eth2 = '10.0.1.3/24'

nodeC_eth1 = '10.0.2.3/24'
nodeD_eth1 = '10.0.2.4/24'

class LinuxRouter( Node ):
    "A Node with IP forwarding enabled."

    def config( self, **params ):
        super( LinuxRouter, self).config( **params )
        # Enable forwarding on the router
        self.cmd( 'sysctl net.ipv4.ip_forward=1' )

    def terminate( self ):
        self.cmd( 'sysctl net.ipv4.ip_forward=0' )
        super( LinuxRouter, self ).terminate()

class NetworkTopo( Topo ):
    "A LinuxRouter connecting three IP subnets"

    def build( self, **_opts ): 
        nodeA = self.addNode( 'nodeA', cls=LinuxRouter, ip=nodeA_eth1 )
        nodeB = self.addNode( 'nodeB', cls=LinuxRouter, ip=nodeB_eth1 )
        nodeC = self.addNode( 'nodeC', cls=LinuxRouter, ip=nodeC_eth1 )
        nodeD = self.addNode( 'nodeD', cls=LinuxRouter, ip=nodeD_eth1 )

        self.addLink(nodeA, nodeB, intfName1='nodeA-eth1', params1={'ip':nodeA_eth1}, intfName2='nodeB-eth1', params2={'ip':nodeB_eth1})
        self.addLink(nodeC, nodeD, intfName1='nodeC-eth1', params1={'ip':nodeC_eth1}, intfName2='nodeD-eth1', params2={'ip':nodeD_eth1})
        self.addLink(nodeB, nodeC, intfName1='nodeB-eth2', params1={'ip':nodeB_eth2}, intfName2='nodeC-eth2', params2={'ip':nodeC_eth2}, bw=20, delay='300ms', loss=1)
        
class TestMininet(Mininet):
    def __init__(self,controller,topo):
        Mininet.__init__(self,controller=controller,topo=topo,link=TCLink)  

def run():
    "Test linux router"
    topo = NetworkTopo()
    net = TestMininet(controller=RemoteController,topo=topo)
    net.start()
    info( '*** Routing Table on Router:\n' )

    nodeA = net.getNodeByName('nodeA')
    nodeB = net.getNodeByName('nodeB')
    nodeC = net.getNodeByName('nodeC')
    nodeD = net.getNodeByName('nodeD')
    
    # Configure route table of 4 nodes

    nodeA.cmd('route add default gw %s' % nodeB_eth1.split('/')[0])
    nodeB.cmd('route add default gw %s' % nodeC_eth2.split('/')[0])
    nodeC.cmd('route add default gw %s' % nodeB_eth2.split('/')[0])
    nodeD.cmd('route add default gw %s' % nodeC_eth1.split('/')[0])
    
    CLI( net )
    net.stop()
    os.system("sudo mn -c")

if __name__ == '__main__':
    setLogLevel( 'info' )
    run()

