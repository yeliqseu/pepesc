import struct
import pickle

from ctypes import c_ushort, c_ubyte, c_int, sizeof

# Control parameters of pepesc

# Length of PEP header,
# contains 1 unsigned short and 1 unsigned char,
# including pep packet type and length
PepHeaderLength = sizeof(c_ubyte) + sizeof(c_ushort)

# Length of TCP header,
# contains 4 unsigned shorts and 8 unsigned chars,
# including real data length, destination address and port
TcpHeaderLength = 4 * sizeof(c_ushort) + 8 * sizeof(c_ubyte)

# The maximum length of the source data that can be filled in the sc-udp package
MsgDataMaxLength = 1430

# class SCPayload's packed length
SCPayloadPackedLength = TcpHeaderLength + MsgDataMaxLength

# class parameters in pystreamc.py
PacketSize = SCPayloadPackedLength

# streamc function serialize_packet(): sourceid, repairid, win_s, win_e and syms. And scpacket header length.
ScPacketSize = PacketSize + 4 * sizeof(c_int) + 3 #PepHeaderLength

# UDP receive buffer size
UdpBufSize = ScPacketSize

# Time interval of doing handshake
# If the peer PEPesc does not have any response before timeout, will be considered offline.
# If the times of handshake reaches 10, turn off directly.
HandShakeInterval = 3.0 # sec.
MaxHandShakeTimes = 10

# If the peer PEPesc does not have any response before timeout, will do heartbeat.
# If the times of heartbeat reaches 5, turn off directly.
MaxHeartBeatWaitTime = 10.0 # sec.
HeartBeatInterval = 3.0     # sec.
MaxHeartBeatTimes = 3

# proportion of repaired packets exceeding the required compensation for packet loss rate 
# (ordered decoding delay is proportional to 1/repairExcess)
ExtraRepairRate = 0.02

# max length of PEPesc's buffer queue for enqueue packets
MaxBufferQueueLength = 100

# interval of sending ACK for source packets
SourceAckInterval = 1

# gain of updating cWnd by using max estimate bandwidth
CwndGain = 1.0

# length of time that the estimate bandwidth sample survives in the maxBwFilter
BwWindowPeriod = 60 # sec.

# gain of pacing
PacingGain = 10

# parameters of packet-train bandwidth estimation
ProbeInterval    = 30 # sec.
ProbePacketSize  = ScPacketSize
ProbeTrainLength = 6

# Message types
PepPacketType = {
                # PEPesc sender to receiver
                'HANDSHAKE'        : 0,     # Establish connection between peer PEPesc
                'WAVEHAND'         : 1,     # Close connection between peer PEPesc
                'HEARTBEAT'        : 2,     # Probe peer PEPesc survival status
                'SC_PROTECTED_PKT' : 3,     # Send tcp flows' data
                'PROBE'            : 4,     # probe channel's bandwidth and RTT
                # PEPesc receiver to sender
                'HANDSHAKE_ACK'    : 10,    # ACK for handshake
                'HEARTBEAT_ACK'    : 11,    # ACK for heartbeat
                'SC_DATA_ACK'      : 12,    # ACK for data packets
                'PROBE_ACK'        : 13,    # ACK for probe packets
                'ADVERTISE_BURST'  : 14,    # Report receive buffer overflow
                'DECODE_SUCCESS'   : 15,    # Report that decoding is successful
}

ScProtectedMsg = {
                'REMOTE_REQUEST'   : 100,   # report that remote TCP Point requests connection
                'REMOTE_EXIST'     : 101,   # report that remote TCP Point exists and is connected
                'REMOTE_NOT_EXIST' : 102,   # report that remote TCP Point not exists and connection failed
                'REMOTE_EXIT'      : 103,   # report that remote TCP Point has exited
                'TCP_RAW_DATA'     : 104,   # receive tcp flows' raw data
}

PollChannelMsg = {
                'CONNECT_SUCCESS'  : 1001,  # report that connecting to neighbor successfully
                'CONNECT_FAILED'   : 1002,  # report that connecting to neighbor unsuccessfully
                'NEIGHBOR_EXIT'    : 1003,  # report that neighbor TCP Point has exited
}
# Info packet types
PacketInfoType = {
                'SOURCE_PACKET': 10000,
                'REPAIR_PACKET': 10001
                 }

class PepHeader :
    """ PEPesc's protocol header
        The header contains two int-length members. It must always be packed into a
        bytearray before being sent out via UDP connections. This is required because
        paritial read/write may happen with non-blocking sockets. So header size must be
        constant...
    """
    def __init__(self, mtype=-1) :
        self.mtype  = mtype     # int type
        self.length = 0         # int type (0 if packet has no body)

    def __str__(self) :
        str = 'Packet Type: %d, length: %d' % (self.mtype, self.length)
        return str

    def packed(self) :
        # Can not use struct.pack('BH', self.mtype, self.length), or will get 4 bytes 
        return struct.pack('B', self.mtype) + struct.pack('H', self.length)

    def parse(self, data) :
        if len(data) != PepHeaderLength :
            print("Error: I can't parse an invalid header!")
            self.mtype  = -1
            self.length = 0
            return
        self.mtype  = struct.unpack('B', data[0 : sizeof(c_ubyte)])[0]
        self.length = struct.unpack('H', data[sizeof(c_ubyte) : ])[0]

class SCPayload() :
    def __init__(self, msg=None, tcpSourceAddr=None, tcpDestinationAddr=None, msgData=b"") :
        self.msg                = msg                   # int type
        self.tcpSourceAddr      = tcpSourceAddr         # tuple type, (ip, port)
        self.tcpDestinationAddr = tcpDestinationAddr    # tuple type, (ip, port)
        self.msgData            = msgData               # class 'bytes'
        self.msgDataLength      = len(msgData)          # int type
    
    def packed(self) :
        tcpSourceIpv4Numbers = [int(i) for i in self.tcpSourceAddr[0].split('.')]
        tcpDestIpv4Numbers   = [int(i) for i in self.tcpDestinationAddr[0].split('.')]
        filler = ' ' * (MsgDataMaxLength - len(self.msgData))

        # C Type: unsigned short
        payload =  struct.pack('H'*4, self.msg, self.msgDataLength, self.tcpSourceAddr[1], self.tcpDestinationAddr[1])
        # C Type: unsigned char
        payload += struct.pack('B'*4, tcpSourceIpv4Numbers[0], tcpSourceIpv4Numbers[1], tcpSourceIpv4Numbers[2], tcpSourceIpv4Numbers[3])
        payload += struct.pack('B'*4, tcpDestIpv4Numbers[0], tcpDestIpv4Numbers[1], tcpDestIpv4Numbers[2], tcpDestIpv4Numbers[3])
        payload += self.msgData
        payload += filler.encode()

        return payload

    def parse(self, payload) :
        if len(payload) != SCPayloadPackedLength :
            print("Error: payload length not equal to specify packing length, cannot parse.")
            return
        
        controlDatas = struct.unpack('H'*4+'B'*8, payload[0 : TcpHeaderLength])
        self.msg                = controlDatas[0]
        self.msgDataLength      = controlDatas[1]
        self.tcpSourceAddr      = ('.'.join([str(i) for i in controlDatas[4:8]]), controlDatas[2])
        self.tcpDestinationAddr = ('.'.join([str(i) for i in controlDatas[8:12]]), controlDatas[3])

        self.msgData = payload[TcpHeaderLength : TcpHeaderLength+self.msgDataLength]

class PepPacket :
    def __init__(self, header = None, body = None) :
        if not header:
            self.header = PepHeader()
        else:
            self.header = header
        
        self.body = body
    
    def getsize(self) :
        return self.header.length
    
    def packed(self):
        if self.body is None:
            return self.header.packed()
        else :
            self.header.length = len(self.body)
            return (self.header.packed() + self.body)

    def parse(self, data) :
        if len(data) < PepHeaderLength :
            print("Error: len(data)<PepHeaderLength, cannot parse.")
            return
        self.header.parse(data[0 : PepHeaderLength])
        
        # Verify if packet size is correct
        if len(data) != self.header.length + PepHeaderLength :
            print("Error: data length seems not match with header!")
            print("%d, %d" % (len(data), self.header.length + PepHeaderLength))
        
        if self.header.length == 0 :
            #print("The length is 0.")
            return
        else :
            self.body = data[PepHeaderLength : ]


class PacketInfo :
    """ PacketInfo contains the information of the sent packets.
        So the members in the class are as follows:
            - id of the packet sent;
            - type of the packet sent;
            - send time of the packet;
            - the number of another type of packets sent;
            - as of the sending time, the receiver acknowledges the number of packets received;
            - as of the sending time, the sending time of the latest packet acked;
            - as of the sending time, the arrive time of latest ACK.
    """
        
    def __init__(self, pktId=0, pktType=0, sendTime=0.0, anotherPktNum=0, delivered=0, firstSentTime=0.0, deliveredTime=0.0) :
        self.pktId         = pktId
        self.pktType       = pktType
        self.sendTime      = sendTime
        self.anotherPktNum = anotherPktNum
        self.delivered     = delivered
        self.firstSentTime = firstSentTime
        self.deliveredTime = deliveredTime
        
    def update(self, pktId, pktType, sendTime) :
        self.pktId   = pktId
        self.pktType = pktType
        self.sendTime    = sendTime

    def __str__(self) :
        infoStr = 'PacketInfo pktId: %d'   % self.pktId
        infoStr += ' sendTime: %.5f'       % self.sendTime
        infoStr += ' pktType: source packet' if self.pktType == PacketInfoType['SOURCE_PACKET'] else '  pktType: repair packet'
        infoStr += ' anotherPktNum: %d'    % self.anotherPktNum
        infoStr += ' delivered: %d'        % self.delivered
        infoStr += ' firstSentTime: %d'    % self.firstSentTime
        infoStr += ' deliveredTime: %d'    % self.deliveredTime
        return infoStr
    
class InorderACK :
    """ACK is the information that the client feeds back to the sender in real time. 
    The server learns in real time to make decisions based on the status information of the client. 
    So the members in the class are as follows:
        - id of ACK packets;
        - inorder of the decoder;
        - number of source packets;
        - number of repair packets;
        - type of the latest received packet;
        - id of the latest received SOURCE packet;
        - id of the latest received REPAIR packet.
    """
    def __init__(self, ackId=0, inorder=-1, sourceNum=0, repairNum=0, latestRecvPktType=-1, latestRecvSourceId=-1, latestRecvRepairId=-1) :
        self.ackId    = ackId
        self.inorder  = inorder
        self.nsource  = sourceNum
        self.nrepair  = repairNum
        self.latestRecvPktType  = latestRecvPktType
        self.latestRecvSourceId = latestRecvSourceId
        self.latestRecvRepairId = latestRecvRepairId
            
    def packed(self) :
        return struct.pack('i'*7, self.ackId, self.inorder, self.nsource, self.nrepair, self.latestRecvPktType, self.latestRecvSourceId, self.latestRecvRepairId)
        
    def parse(self, data) :
        hdr = struct.unpack('i'*7, data)
        self.ackId    = hdr[0]
        self.inorder  = hdr[1]
        self.nsource  = hdr[2]
        self.nrepair  = hdr[3]
        self.latestRecvPktType  = hdr[4]
        self.latestRecvSourceId = hdr[5]
        self.latestRecvRepairId = hdr[6]

    def getPackedSize(self) :
        return len(struct.pack('i'*7, 1, 1, 1, 1, 1, 1, 1))
        
    def __str__(self) :
        infoStr =  'ACK id: %d'     % (self.ackId) 
        infoStr += ' inorder: %d'   % (self.inorder)
        infoStr += ' sourceNum: %d' % (self.nsource)
        infoStr += ' repairNum: %d' % (self.nrepair)
        if self.latestRecvPktType == PacketInfoType['SOURCE_PACKET'] :
            infoStr += ' latestRecvPktType: SOURCE'
        else :
            infoStr += ' latestRecvPktType: REPAIR'
        infoStr += ' latestRecvSourceId: %d' % (self.latestRecvSourceId)
        infoStr += ' latestRecvRepairId: %d' % (self.latestRecvRepairId)

        return infoStr


