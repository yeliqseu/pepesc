import time
import socket
import select
import errno

from collections import deque
from select      import POLLIN, POLLOUT, POLLERR, POLLHUP, POLLNVAL, POLLRDHUP
from protocol    import MsgDataMaxLength, ScPacketSize, PollChannelMsg

# Channel log file
channelLogFlag = False
channelLog     = None
if channelLogFlag == True :
    channelLog = open("./log-channel.dat", 'w')

# Define channel state
CH_STATE_PRECONN       = -1
CH_STATE_NOTCONN       = 0
CH_STATE_CONNECT       = 1    # TCP connected
CH_STATE_PARTIAL_CLOSE = 2    # TCP connection peer is closed
CH_STATE_PRECLOSE      = 3    # Channel will be closed after recvq and sendq is empty
CH_STATE_CLOSE         = 4    # Channel will be closed when doing next PollChannels

# Define channel eventmask
CH_READ  = POLLIN
CH_WRITE = POLLOUT
CH_ERROR = POLLERR

# Record the last time when all sockets received data without blocking,
# to control the receiving rate not to exceed the available bandwidth of the link.
lastDoRecvTimes = {}

# Map channel's socket handle fileno to channel id.
mapHandleFilenoToChid = {}

# We additionally create channels for tcpListener and udpSocketFd,
# just to be able to poll them together in the function PollChannels.
# So we have to record the channels' id used by tcpListener and udpSocketFd and their filenos,
# to distinguish them from TCP channels.
tcpListenerFd = -1
udpSocketFd   = -1
tcpListenChid = -1
udpChid       = -1

# poller for polling channels
poller = select.poll()

class Buffer:
    """ Message buffer
        Each buffer contains the serialized wait-to-send/wait-to-receive ccfd packet
    """
    def __init__(self, data=None) :
        if not data :
            self.data = bytearray()        # message itself
            self.length = MsgDataMaxLength # length of message to receive
        else :
            self.data = data
            self.length = len(data)        # length of message to receive
        self.pos = 0                       # Position of read/write

class MsgQueue :
    def __init__(self) :
        self.messages = deque()

    def enqueue(self, buf) :
        self.messages.append(buf)

    def dequeue(self) :
        return self.messages.popleft()

    def size(self) :
        return len(self.messages)

    def isEmpty(self) :
        return (not self.size())

    def first(self) :
        """ Return the very first message in the queue
        """
        if self.isEmpty() :
            return None
        else:
            return self.messages[0]

    def last(self) :
        """ Return most recent message being worked on
        """
        if self.isEmpty() :
            return None
        else:
            return self.messages[self.size()-1]


class Channel :
    """ Each communication between PEPesc and neighbor TCP Point is performed via a channel.
        But we additionally create channels for tcpListener and udpSocketFd,
        just to be able to poll them together in the function PollChannels.
    """
    def __init__(self, handle=-1, neighbor=None, remote=None, maxWaitTime=0.01) :
        self.chid           = -1
        self.handle         = handle           # handle of the channel with neighbor (fd)
        self.neighbor       = neighbor         # neighbor TCP Point address tuple (ip, port)
        self.remote         = remote           # remote TCP Point address tuple (ip, port)
        self.state          = CH_STATE_NOTCONN
        self.sendq          = MsgQueue()       # send queue (first-in-first-out)
        self.recvq          = MsgQueue()       # recv queue (first-in-first-out)
        self.eventmask      = 0                # events on the handle
        self.lastDoRecvTime = 0                # last receiving TCP data time in this channel, for avoiding long time waiting
        self.maxWaitTime    = maxWaitTime      # max waiting time for reading channel

    def setChannelId(self, id) :
        self.chid = id

    def send(self, data) :
        self.sendq.enqueue(Buffer(data))
        return

    def receive(self) :
        if not self.recvq.isEmpty() : #and self.recvq.first().length == self.recvq.first().pos :
            return self.recvq.dequeue().data    # class 'bytes'
        else:
            self.eventmask &= CH_READ
            return None

    def doRecv(self) :
        """ Receive from the handle and store messages to recvq
        """
        if self.recvq.isEmpty():
            self.recvq.enqueue(Buffer())
        
        # A complete message is here, set ready to process.
        if self.recvq.last().length == self.recvq.last().pos :
            self.eventmask |= CH_READ
            return
        
        try :
            data, addr = self.handle.recvfrom(self.recvq.last().length - self.recvq.last().pos, socket.MSG_DONTWAIT)
        except Exception as details :
            print("Channel.doread() cannot recvfrom(). Error: %s" % (details, ))
            self.eventmask = CH_ERROR
            return
        
        if len(data) == 0 :
            #print("Channel.doread() didn't read anything, something is wrong")
            #self.eventmask = CH_ERROR
            if self.state == CH_STATE_PARTIAL_CLOSE :
                self.state = CH_STATE_PRECLOSE
            return
        
        self.recvq.last().data += data
        self.recvq.last().pos  += len(data)
        
        currentTime = time.time()

        # If a complete message is received, mark the channel as readable
        if self.recvq.last().length == self.recvq.last().pos or currentTime - self.lastDoRecvTime >= self.maxWaitTime :
            #print("[Channel] recvq of channel %s is readable" % (self.chid))
            self.eventmask |= CH_READ
        
        self.lastDoRecvTime = currentTime
        return

    def doSend(self) :
        """ Write messages in send queue to socket
        """
        if self.sendq.isEmpty() :
            return
        
        offset = self.sendq.first().pos
        cc = self.handle.send(self.sendq.first().data[offset : ])  # FIXME: am I non-blocking?
        
        if cc < 0:
            self.eventmask = CH_ERROR
            return
        
        self.sendq.first().pos += cc
        
        # remove message if sent was complete
        if self.sendq.first().pos == self.sendq.first().length :
            self.sendq.dequeue()
        
        return

def OpenTcpListenChannel(chans, tcpListener) :
    global tcpListenerFd, tcpListenChid
    chid = FindOneFreeChannel(chans)
    ch = Channel(tcpListener)
    ch.setChannelId(chid)
    chans[chid] = ch
    tcpListenerFd = tcpListener.fileno()
    tcpListenChid = chid
    poller.register(tcpListenerFd, POLLIN)
    return chid

def OpenUdpChannel(chans, udpSocket) :
    global udpSocketFd, udpChid
    chid = FindOneFreeChannel(chans)
    ch = Channel(udpSocket)
    ch.setChannelId(chid)
    chans[chid] = ch
    udpSocketFd = udpSocket.fileno()
    udpChid = chid
    return chid

def OpenInConnChannel(chans, sockfd, remote, maxWaitTime) :
    chid = FindOneFreeChannel(chans)
    sockfd.setblocking(0)  # Set it non-blocking
    ch = Channel(sockfd, sockfd.getpeername(), remote, maxWaitTime)
    ch.state = CH_STATE_CONNECT
    ch.setChannelId(chid)
    chans[chid] = ch
    lastDoRecvTimes[chid] = 0
    mapHandleFilenoToChid[sockfd.fileno()] = chid
    poller.register(sockfd.fileno(), POLLIN | POLLOUT | POLLRDHUP)
    return chid

def OpenOutConnChannel(chans, neighbor, remote, maxWaitTime) :
    """ Open a channel for connection with neighbor TCP Point
        Need to handle non-blocking connect()
    """
    chid = FindOneFreeChannel(chans)
    sockfd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sockfd.setblocking(0)
    err = sockfd.connect_ex(neighbor)
    ch = Channel(sockfd, neighbor, remote, maxWaitTime)
    lastDoRecvTimes[chid] = 0
    mapHandleFilenoToChid[sockfd.fileno()] = chid
    poller.register(sockfd.fileno(), POLLIN | POLLOUT | POLLRDHUP)
    if not err:
        # Connected successfully
        ch.state = CH_STATE_CONNECT
        ch.setChannelId(chid)
        chans[chid] = ch
        return chid
    elif err == errno.EINPROGRESS:
        # Connection in progress
        ch.state = CH_STATE_PRECONN
        ch.setChannelId(chid)
        chans[chid] = ch
        return chid
    else:
        # Other errors
        print("Cannot open outgoing channel to %s:%d: %s" % (neighbor[0], neighbor[1], errno.errorcode[err]))
        del lastDoRecvTimes[chid]
        del mapHandleFilenoToChid[sockfd.fileno()]
        poller.unregister(sockfd.fileno())
        return -1

def CloseChannel(chans, chid):
    # Properly close connections
    global poller
    try :
        if chid != udpChid :
            poller.unregister(chans[chid].handle.fileno())
        if chid != tcpListenChid and chid != udpChid :
            del mapHandleFilenoToChid[chans[chid].handle.fileno()]
            del lastDoRecvTimes[chid]
        chans[chid].handle.close()
        del chans[chid]
    except Exception as details :
        print("Error: close channel: %s" % (details, ))

def FindOneFreeChannel(chans):
    """ Find a free channel, return the id
    """
    i = 0
    while True:
        if i not in chans.keys():
            break
        i += 1
    return i

def PollChannels(chans, tcpAvailableBw, bufferRemain, udpPollEvents) :
    """ Poll channels in the list, return readable TCP channel ids and reports
    """
    global poller 

    # function's return variables, including readable TCP channel ids and reports
    readableTcpChannelIds = []
    pollReports = []
    
    for i in list(chans) :
        ch = chans[i]

        if i == tcpListenChid and ch.eventmask == CH_ERROR :
            print("TCP Listen channel error! Close it!")
            CloseChannel(chans, i)
            continue
        
        if i == udpChid :
            if ch.eventmask == CH_ERROR :
                print("UDP channel error! Close it!")
                CloseChannel(chans, i)
            else :
                ch.eventmask = 0
                poller.register(udpSocketFd, udpPollEvents)
            continue

        if ch.state == CH_STATE_PRECLOSE :
            if not ch.recvq.isEmpty() :
                ch.eventmask |= CH_READ
                readableTcpChannelIds.append(i)
            elif ch.sendq.isEmpty() and ch.recvq.isEmpty() : 
                ch.state = CH_STATE_CLOSE
            #continue

        if ch.state == CH_STATE_CLOSE or ch.eventmask == CH_ERROR :
            msg = -1
            if ch.state == CH_STATE_PRECONN :
                msg = PollChannelMsg['CONNECT_FAILED']
            elif ch.state == CH_STATE_CONNECT or ch.state == CH_STATE_CLOSE :
                msg = PollChannelMsg['NEIGHBOR_EXIT']
            pollReports.append((i, msg, ch.neighbor, ch.remote))
            CloseChannel(chans, i)
            continue

        ch.eventmask = 0    # Reset eventmask before each round of polling
    
    # Poll
    result = poller.poll(1)
    
    # Get number of readable TCP channel. Be careful not to count tcpListener and udpSocktFd.
    readableTcpChannelNumber = len([fd for fd, event in result if event & (POLLIN | POLLRDHUP) and fd != tcpListenerFd and fd != udpSocketFd])

    # DoRecv function call interval, definition: 
    #     the maximum allowable length of the socket to receive data at one time,
    #     divided by the estimated available bandwidth of the current link(unit: bps), 
    #     multiplied by the current number of channels.
    doRecvInterval = MsgDataMaxLength * 8 / tcpAvailableBw * readableTcpChannelNumber / 1.2
    currentTime = time.time()

    if channelLogFlag == True and len(chans) != 0 :
        channelLog.write("doRecvInterval %f %f\n" % (currentTime, doRecvInterval))
        channelLog.write("socketNumber %f %d\n" % (currentTime, len(chans)))
        channelLog.write("readableChannelIds %f %d\n" % (currentTime, readableTcpChannelNumber))

    for fd, event in result :
        # Check tcpListener's and udpSocketFd's executable events
        if fd == tcpListenerFd :
            if event & (POLLHUP | POLLERR | POLLNVAL) :
                chans[tcpListenChid].eventmask = CH_ERROR
            if event & POLLIN :
                chans[tcpListenChid].eventmask |= CH_READ
            continue
        
        if fd == udpSocketFd :
            if event & (POLLHUP | POLLERR | POLLNVAL) :
                chans[tcpListenChid].eventmask = CH_ERROR
            if event & POLLIN :
                chans[udpChid].eventmask |= CH_READ
            if event & POLLOUT :
                chans[udpChid].eventmask |= CH_WRITE
            continue
        
        # Handle TCP channel fd's executable events
        i = mapHandleFilenoToChid[fd]

        if event & (POLLHUP | POLLERR | POLLNVAL) :
            chans[i].eventmask = CH_ERROR
            continue
        
        if event & POLLRDHUP and chans[i].state != CH_STATE_CLOSE :
            chans[i].state = CH_STATE_PARTIAL_CLOSE

        if chans[i].state == CH_STATE_PRECONN :
            if event & POLLOUT:
                chans[i].state = CH_STATE_CONNECT
                chans[i].eventmask |= CH_WRITE
                msg = PollChannelMsg['CONNECT_SUCCESS']
                pollReports.append((i, msg, chans[i].neighbor, chans[i].remote))
        else:
            if event & (POLLIN | POLLRDHUP) and currentTime - lastDoRecvTimes[i] >= doRecvInterval and bufferRemain > 0 :
                # try our best to receive
                chans[i].doRecv()
                lastDoRecvTimes[i] = currentTime
            if not chans[i].sendq.isEmpty() and (event & POLLOUT) :
                # try out best to send
                chans[i].doSend()

        if currentTime - chans[i].lastDoRecvTime >= chans[i].maxWaitTime and not chans[i].recvq.isEmpty():
            chans[i].eventmask |= CH_READ
        
        if chans[i].eventmask & CH_READ :
            readableTcpChannelIds.append(i)

    poller.unregister(udpSocketFd)

    return (readableTcpChannelIds, pollReports)



