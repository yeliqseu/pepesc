import os
import sys
import time
import math
import errno
import random
import select
import struct
import socket
import logging
import argparse

from pickle      import dumps
from collections import deque
from ctypes      import sizeof, c_int, c_ubyte, string_at, byref, POINTER, cast

from pystreamc   import DEC_ALLOC, parameters, streamc
from channel     import CH_READ, CH_WRITE, OpenTcpListenChannel, OpenUdpChannel, OpenInConnChannel, OpenOutConnChannel, PollChannels, CloseChannel
from protocol    import *

import cProfile

class InfoQueue :
    """ The information queue for sent packets, 
        divided into two queues: source queue and repair queue.
    """
    def __init__(self) :
        self.sourceQueue = []
        self.repairQueue = []
        
    def Add(self, pktType, pktId, sendTime, anotherPktNum, delivered, firstSentTime, deliveredTime) :
        pktInfo = PacketInfo(pktId, pktType, sendTime, anotherPktNum, delivered, firstSentTime, deliveredTime)
        if pktInfo.pktType == PacketInfoType['SOURCE_PACKET'] :
            self.sourceQueue.append(pktInfo)
        else :
            self.repairQueue.append(pktInfo)
    
    def Find(self, pktType, pktId) :
        """ Binary search
            To obtain the information of the specified id of packets when they were sent out,
        """
        pos = -1
        resultInfo = None
        if pktType == PacketInfoType['SOURCE_PACKET'] :
            left = 0
            right = len(self.sourceQueue) - 1
            while left <= right :
                mid = int(left + (right - left) / 2)
                if self.sourceQueue[mid].pktId < pktId :
                    left = mid + 1
                elif self.sourceQueue[mid].pktId > pktId :
                    right = mid - 1
                else :
                    pos = mid
                    resultInfo = self.sourceQueue[mid]
                    break
            if pos >= 0 :
                self.sourceQueue = self.sourceQueue[pos + 1 : ]
        
        else :
            left = 0
            right = len(self.repairQueue) - 1
            while left <= right :
                mid = int(left + (right - left) / 2)
                if self.repairQueue[mid].pktId < pktId :
                    left = mid + 1
                elif self.repairQueue[mid].pktId > pktId :
                    right = mid - 1
                else :
                    pos = mid
                    resultInfo = self.repairQueue[mid]
                    break
            if pos >= 0 :
                self.repairQueue = self.repairQueue[pos + 1 : ]
        return resultInfo
    
    def GetSourceQueueSize(self) :
        return len(self.sourceQueue)
    
    def GetRepairQueueSize(self) :
        return len(self.repairQueue)

class BwQueue :
    def __init__(self, maxSize) :
        self.m_size = maxSize
        self.m_BwQueue    = deque()
        self.m_BwMaxQueue = deque()
    
    def GetMaxBw(self) :
        return self.m_BwMaxQueue[0]
    
    def Insert(self, newBw) :
        self.m_BwQueue.append(newBw)
        if len(self.m_BwQueue) > self.m_size :
            if self.m_BwQueue[0] == self.m_BwMaxQueue[0] :
                self.m_BwMaxQueue.popleft()
            self.m_BwQueue.popleft()
        while len(self.m_BwMaxQueue) != 0 and newBw > self.m_BwMaxQueue[-1] :
            self.m_BwMaxQueue.pop()
        self.m_BwMaxQueue.append(newBw)


class MaxBwFilter :
    def __init__(self, roundPeriod) :
        self.m_roundPeriod = roundPeriod
        self.m_estTime     = deque()
        self.m_BwQueue     = deque()
        self.m_BwMaxQueue  = deque()
    
    def GetMaxBw(self) :
        return self.m_BwMaxQueue[0]
    
    def IsEmpty(self) :
        return (not len(self.m_BwQueue))

    def Insert(self, newEstTime, newBw) :
        self.m_estTime.append(newEstTime)
        self.m_BwQueue.append(newBw)
        
        while self.m_estTime[0] <= newEstTime - self.m_roundPeriod :
            if self.m_BwQueue[0] == self.m_BwMaxQueue[0] :
                self.m_BwMaxQueue.popleft()
            self.m_estTime.popleft()
            self.m_BwQueue.popleft()
        
        while len(self.m_BwMaxQueue) != 0 and newBw > self.m_BwMaxQueue[-1] :
            self.m_BwMaxQueue.pop()
        self.m_BwMaxQueue.append(newBw)


class pepApp :
    def __init__(self) :
        # Sockets
        self.m_tcpListener = None
        self.m_udpSocket   = None

        # Self address and peer PEPesc's address
        self.m_selfAddress = None
        self.m_peerAddress = None

        # Statistics the size of the transmitted data
        self.m_totalDataSentSize = 0    # byte
        self.m_totalDataRecvSize = 0    # byte
        self.m_rejectConnectionNum = 0

        # Connection with peer PEPesc
        self.m_peerOnline        = False
        self.m_handShakeTimes    = 0
        self.m_lastHandShakeTime = 0.0
        self.m_selfClose         = False 
        self.m_selfPreClose      = False 

        # HeartBeat
        self.m_lastResponseTime   = 0.0
        self.m_heartBeatTimes    = 0
        self.m_lastHeartBeatTime = 0.0

        # Flags
        self.m_detailFlag   = False 
        self.m_maxAllowedBw = None
        self.m_constBw      = None

        # Channels for non-blocking IO
        self.m_channels      = {}       # Every TCP channel only serves one TCP connection，format：{channel id : channel}
        self.m_tcpListenChid = -1       # Channel's id used by tcpListener 
        self.m_udpChid       = -1       # Channel's id used by udpSocket 
        
        self.m_findChidForPEPescToSend = {}    # 存储TCP连接使用的channel的id，格式：{(from ip, from port ,to ip, to port) : channel id}
        self.m_tcpSentBufLen   = {}            # 存储每个neighbor已enqueue TCP数据长度，当TCP Client断开连接时，通知对端PEPesc此时的长度，对端接收并发送完整长度TCP数据至TCP Server后，关闭连接。格式：{(from ip, from port ,to ip, to port) : int}
        self.m_tcpRecvBufLen   = {}            # 存储每个neighbor已接收TCP数据长度，对端PEPesc通知该连接即将关闭后，在发送完整TCP数据后，关闭与TCP Server的连接。格式：{(from ip, from port ,to ip, to port) : int}
        self.m_linkToBeClosed  = {}            # 存储将要被关闭的连接中对端PEPesc通知的TCP数据长度，当发送完整TCP数据后，即刻关闭。格式：{(from ip, from port ,to ip, to port) : int}
        
        # Waiting for a connection to be established
        self.m_tcpReceiverWaiting = {}
        self.m_tcpSenderWaiting   = {}
            
        # Parameters used for streaming coding
        self.m_cp           = parameters()
        self.m_cp.gfpower   = 8
        self.m_cp.pktsize   = 0
        self.m_cp.repfreq   = 0.0               # This Sc parameter is not used in PEP, where the frequency of sending repair packet is determine by TimeToSendRepairPacket
        self.m_cp.seed      = 0
        
        # initialize encoder and decoder
        self.m_enc = streamc.initialize_encoder(byref(self.m_cp), None, 0)
        self.m_dec = streamc.initialize_decoder(byref(self.m_cp))
        
        # Parameters used for repair packets selective sending
        self.m_lastSentSourceTime       = 0.0
        self.m_lastSentRepairTime       = 0.0
        self.m_newDataIdleState         = False
        self.m_idleStateChangeTime      = -1.0
        self.m_numSentRepairExcludeIdle = 0
        self.m_numSentRepairAfterIdle   = 0
        self.m_idleCanSendRepairCount   = 0
        self.m_duplicatedInorder        = False
        self.m_lastStuckInorder         = -1
        self.m_numSentRepairAfterStuck  = 0
        self.m_numSentRepairAfterRtt    = 0

        # Detect consecutive packet loss and avoid again
        self.m_burstAvoidPeriod = 3.0
        self.m_lastBurstTime    = 0.0
        self.m_lastRecvSourceId = -1
        self.m_lastRecvRepairId = -1

        # number of current encoded source packets
        self.m_currentMaxSourceId   = -1
        self.m_lastStreamcQueueSize = 0

        # for sending ack
        self.m_latestRecvPktType     = -1
        self.m_latestRecvSourceNum   = 0    # decoder目前收到的源分组数目
        self.m_latestRecvRepairNum   = 0    # decoder目前收到的修复分组数目
        self.m_numLastAcked          = 0    # 上一次反馈ACK时收到的分组数
        self.m_inorderNext           = 0
        self.m_inorderAckId          = 0
        self.m_lastDataAckSendTime = 0
        self.m_numRecvSinceLastSourceAck = 0
        self.m_inorderAckPacketSize  = InorderACK().getPackedSize()+PepHeaderLength
        
        # for receiving ack
        self.m_inorderAck         = InorderACK()
        self.m_lastAckedSourceId  = -1
        self.m_lastAckedRepairId  = -1
        self.m_lastAckedInorderId = -1
        self.m_lastAckedSourceNum = 0
        self.m_lastAckedRepairNum = 0
        self.m_lastSentSourceId   = -1
        self.m_lastSentRepairId   = -1
        self.m_lastAckedPacketSentTime = 0
        
        # CWND Adaption
        self.m_initCWnd        = 10
        self.m_cWnd            = 10
        self.m_packetsInFlight = 0
        
        # rtt estimation
        self.m_rtt               = 0.0
        self.m_rttMin            = 1e6
        self.m_lastDecSuccTime   = 0.0      # Record the time when the client notified the last successful decoding, which is used to ignore the RTT estimation of some packets
        
        # bandwidth estimation
        self.m_useJersyFlag      = True
        self.m_estBw             = 0.0      # estimated end-to-end bandwidth (in pkt/sec)
        self.m_estBwMax          = 0.0
        self.m_maxBwFilter       = MaxBwFilter(BwWindowPeriod)
        self.m_lastAckTime       = -1.0     # time of receiving last ACK (in sec.)
        self.m_lastFirstSentTime = -1.0     # sent time of last acked packet (in sec.)
        #self.m_bwWindowLength    = 10
        #self.m_maxBwFilter       = BwQueue(self.m_bwWindowLength)
        #self.m_acksSinceLastEst  = 0
        #self.m_nRecvSinceLastEst = 0
        #self.m_lastBwEstTime     = 0.0
        #self.m_lastEstBw         = 0
        
        # loss rate estimation
        self.m_lossRate = 0.0               # estimated transmission loss rate based on ACKs
        
        # pacing
        self.m_pacing      = True#False
        self.m_pacingRate  = 5 * 1024 * 1024
        self.m_pacingTimer = 0.0            # !< Pacing Event
        self.m_lastPacketSentTime = 0
        
        # bookkeeping information for sent not-yet acked packets
        self.m_pktInfoQueue = InfoQueue()

        # active bandwidth probe
        self.m_activeProbeBw        = True
        self.m_lastProbedTime       = -1
        self.m_probeValidity        = False
        self.m_probeBw              = 0
        self.m_probePacketSentTimes = []
        self.m_firstProbeArriveTime = 0
        self.m_lastProbeArrivedId   = -1


    def SetAttribute(self, args, packetSize) :
        self.m_selfAddress   = (args.selfIp, args.selfPort)
        self.m_peerAddress   = (args.peerIp, args.peerPort)
        self.m_cp.pktsize    = packetSize
        self.m_detailFlag    = args.detail
        self.m_activeProbeBw = not args.deactivateProbeBw#False if args.maxBw else not args.deactivateProbeBw
        self.m_maxAllowedBw  = float(args.maxBw[0:-4]) / PacingGain * 1024 * 1024 / (ScPacketSize * 8) if args.maxBw else None
        self.m_constBw       = float(args.ConstBw[0:-4]) * 1024 * 1024 / (ScPacketSize * 8) if args.ConstBw else None
        self.m_useJersyFlag  = False if args.bwEstMethod == "BBR" else True
        
        self.m_tcpListener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.m_tcpListener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.m_tcpListener.setsockopt(socket.SOL_IP, socket.IP_TRANSPARENT, 1)
        self.m_tcpListener.bind(("0.0.0.0", self.m_selfAddress[1]))
        self.m_tcpListener.listen(128)

        self.m_udpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.m_udpSocket.bind((self.m_selfAddress[0], self.m_selfAddress[1]))

        self.m_tcpListenChid = OpenTcpListenChannel(self.m_channels, self.m_tcpListener)
        self.m_udpChid = OpenUdpChannel(self.m_channels, self.m_udpSocket)

        return 
    
    
    def HandleLog(self, logLevel, logType, log, detailFlag=False) :
        if logLevel == 'DEBUG' :
            logging.debug(logType + log)
        elif logLevel == 'INFO' :
            logging.info(logType + log)
        elif logLevel == 'WARNING' :
            logging.warning(logType + log)
        elif logLevel == 'ERROR' :
            logging.error(logType + log)
        if detailFlag :
            print("[%s][%s:%d] %s."  % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))


    def EnqueuePackets(self, msg, tcpSourceAddr=None, tcpDestinationAddr=None, contents=b"") :
        if msg != ScProtectedMsg['TCP_RAW_DATA'] :
            scPayLoad = SCPayload(msg, tcpSourceAddr, tcpDestinationAddr, contents)
            buf = (c_ubyte * self.m_cp.pktsize).from_buffer_copy(scPayLoad.packed())         # class bytes to ctypes.c_ubyte_Array
            streamc.enqueue_packet(self.m_enc, self.m_currentMaxSourceId+1, buf)
            self.m_currentMaxSourceId += 1
            logging.debug("[EncoderStatus] headsid: %d tailsid: %d nextsid: %d" \
                % (self.m_enc.contents.headsid, self.m_enc.contents.tailsid, self.m_enc.contents.nextsid))

        else :
            num = math.ceil(len(contents) / MsgDataMaxLength)
            for i in range(num) :
                tcpRawData = contents[i * MsgDataMaxLength : (i+1) * MsgDataMaxLength] # class 'bytes'
                scPayLoad = SCPayload(msg, tcpSourceAddr, tcpDestinationAddr, tcpRawData)
                buf = (c_ubyte * self.m_cp.pktsize).from_buffer_copy(scPayLoad.packed())    # class bytes to ctypes.c_ubyte_Array
                streamc.enqueue_packet(self.m_enc, self.m_currentMaxSourceId+1, buf)
                self.m_currentMaxSourceId += 1
                logging.debug("[EncoderStatus] headsid: %d tailsid: %d nextsid: %d" \
                    % (self.m_enc.contents.headsid, self.m_enc.contents.tailsid, self.m_enc.contents.nextsid))


    def RttEstimation(self, receiveTime, sendTime) :
        if sendTime == -1 :
            return 
        alpha = 0.9
        historyRtt = self.m_rtt
        newRtt = receiveTime - sendTime
        
        if historyRtt == 0 :
            self.m_rtt    = newRtt
            self.m_rttMin = newRtt
        else :
            self.m_rtt = alpha * historyRtt + (1 - alpha) * newRtt    # smoothed RTT estimation (follow standard TCP, Karn's algorithm is not needed since no retransmission is incurred)
            self.m_rttMin = min(self.m_rttMin, newRtt)                # update minimum rtt observed (which should be close to the true propagation delay)
    

    def BwEstimationJersy(self, numAcked) :
        # TCP Jersy's TSW algorithm
        currentTime = time.time()
        ackInterval = currentTime - self.m_lastAckTime

        if self.m_lastAckTime == -1.0 :
            # The first data ack, the interval cannot be calculated, no estimation is made
            self.m_lastAckTime = currentTime 
            return 
        
        self.m_estBw = (self.m_rtt * self.m_estBw + numAcked) / (ackInterval + self.m_rtt)
        self.m_maxBwFilter.Insert(currentTime , self.m_estBw)
        
        log = "[Jersy-ABE] ackInterval: %f" % ackInterval
        log += " numAcked: %d" % numAcked
        log += " Estimated-BW: %f" % self.m_estBw
        log += " Current-Max-Bw: %f" % self.m_maxBwFilter.GetMaxBw()
        logging.debug(log)


    def BwEstimationBBR(self, delivered, ackElapsed, sendElapsed) :
        # BBR's delivery rate estimation algorithm
        # https://datatracker.ietf.org/doc/html/draft-cheng-iccrg-delivery-rate-estimation
        deliveryElapsed = max(ackElapsed, sendElapsed)
        self.m_estBw = delivered / deliveryElapsed      # pkts/sec.
        self.m_maxBwFilter.Insert(time.time() , self.m_estBw)
        log = "[BBR-ABE] delivered: %d ackElapsed: %f sendElapsed: %f Estimated-BW: %f Current-Max-Bw: %f" \
            % (delivered, ackElapsed, sendElapsed, self.m_estBw, self.m_maxBwFilter.GetMaxBw())
        logging.debug(log)


    def PeEstimation(self, nTotalLoss, nTotalSent) :
        alpha = 0.9
        new_lossRate = nTotalLoss / nTotalSent
        if self.m_lossRate == 0 :
            self.m_lossRate = new_lossRate
        else :
            self.m_lossRate = self.m_lossRate * alpha + new_lossRate * (1 - alpha)


    def UpdateCwnd(self) :
        cWndGain = CwndGain #if self.m_useJersyFlag else 1.2
        
        # Update estimated maximum bandwidth
        if self.m_maxBwFilter.IsEmpty() :
            self.m_estBwMax = self.m_probeBw * 0.8
        else :
            self.m_estBwMax = self.m_maxBwFilter.GetMaxBw()

        # Update CWND
        if self.m_constBw :
            cWndPreset = self.m_constBw * self.m_rttMin * cWndGain
        elif self.m_maxAllowedBw :
            cWndPreset = min(self.m_maxAllowedBw, self.m_estBwMax) * self.m_rttMin * cWndGain
        else :
            cWndPreset = self.m_estBwMax * self.m_rttMin * cWndGain
        self.m_cWnd = math.floor(max(10.0, cWndPreset))
        self.m_pacing = True if self.m_cWnd > 10 else False
        #self.m_pacing = False if self.m_constBw else self.m_pacing
        # Update pacing rate
        if self.m_pacing == True and self.m_cWnd > self.m_packetsInFlight :
            self.m_pacingRate = self.m_constBw * ScPacketSize if self.m_constBw \
                                else self.m_estBwMax * ScPacketSize * PacingGain
    

    def SendDataAck(self) :
        inorderAck = InorderACK(self.m_inorderAckId, self.m_dec.contents.inorder, self.m_latestRecvSourceNum, self.m_latestRecvRepairNum,
                                self.m_latestRecvPktType, self.m_lastRecvSourceId, self.m_lastRecvRepairId)
        rpkt = PepPacket(PepHeader(PepPacketType['SC_DATA_ACK']), inorderAck.packed())
        
        self.m_udpSocket.sendto(rpkt.packed(), self.m_peerAddress)
        
        self.m_inorderAckId += 1
        self.m_lastDataAckSendTime = time.time() 
        self.m_numLastAcked = self.m_latestRecvSourceNum + self.m_latestRecvRepairNum

        logging.debug("[SendDataAck] Send data ACK %s" % inorderAck)
        

    def RecvDataAck(self, pkt) :
        self.m_inorderAck.parse(pkt.body)
        inorder, nsource, nrepair = self.m_inorderAck.inorder, self.m_inorderAck.nsource, self.m_inorderAck.nrepair
        latestRecvPktType, latestRecvSourceId, latestRecvRepairId = self.m_inorderAck.latestRecvPktType, self.m_inorderAck.latestRecvSourceId, self.m_inorderAck.latestRecvRepairId
        latestRecvPktId = latestRecvSourceId if latestRecvPktType == PacketInfoType['SOURCE_PACKET'] else latestRecvRepairId

        log = "[RecvDataAck] current inorder: %d [nsource, nrepair] = [ %d , %d ] ACKed inorder: %d [nsource, nrepair] = [ %d , %d ]" % (self.m_lastAckedInorderId,  
                                                                                                                                         self.m_lastAckedSourceNum, 
                                                                                                                                         self.m_lastAckedRepairNum, 
                                                                                                                                         inorder, nsource, nrepair)
        
        # If the following three ACK messages are the same as the last time,
        # it means that the state of the receiving end has not changed,
        # so there is no need to estimate the information, and do not print.
        if self.m_lastAckedInorderId == inorder and self.m_lastAckedSourceNum == nsource and self.m_lastAckedRepairNum == nrepair :
            return 
        
        # Discard the ACK if the reported numbers are out-dated. This can happen if there are re-ordering of ACK packets over the network
        if inorder < self.m_lastAckedInorderId or nsource < self.m_lastAckedSourceNum or nrepair < self.m_lastAckedRepairNum :
            return
        
        # A source packet is lost, and the inorder of the peer PEPesc is stuck.
        # Prepare to send quantitative repair packets for minor compensation.
        if self.m_lastAckedInorderId == inorder \
            and self.m_lastStuckInorder != inorder \
                and latestRecvPktType == PacketInfoType['SOURCE_PACKET'] :
            self.m_duplicatedInorder = True
            self.m_lastStuckInorder = inorder

        resultInfo = self.m_pktInfoQueue.Find(latestRecvPktType, latestRecvPktId)
        sendTime, otherTypePacketNum = resultInfo.sendTime, resultInfo.anotherPktNum
        deliveredAsOfSend, firstSentTime, deliveredTime = resultInfo.delivered, resultInfo.firstSentTime, resultInfo.deliveredTime
        recvAckTime = time.time()
        
        # Available bandwidth estimation
        #if sendTime > self.m_idleStateChangeTime :
        #if sendTime - self.m_lastAckedPacketSentTime <= 2 * self.CalculateBytesTxTime() :
        if self.m_useJersyFlag :
            numAcked = nsource + nrepair - self.m_lastAckedSourceNum - self.m_lastAckedRepairNum
            self.BwEstimationJersy(numAcked)
        else :
            delivered = nsource+nrepair-deliveredAsOfSend
            ackElapsed = recvAckTime-deliveredTime
            sendElapsed = sendTime-firstSentTime
            self.BwEstimationBBR(delivered, ackElapsed, sendElapsed)

        self.m_lastAckTime = recvAckTime
        self.m_lastFirstSentTime = sendTime
        self.m_lastAckedPacketSentTime = sendTime

        self.m_lastAckedInorderId = inorder
        self.m_lastAckedSourceNum = nsource
        self.m_lastAckedRepairNum = nrepair
        
        # Only perform RTT estimation for packets whose sending time is later than the successful decoding time
        if sendTime >= self.m_lastDecSuccTime :
            self.RttEstimation(recvAckTime, sendTime)
        
        # Pe estimation
        # TODO：收到out-of-order分组的ACK时不应进行丢包率估计 (totalLoss和in-flight无法判断)
        sourceSentCount, repairSentCount = 0, 0    # The number of two types of packets sent by the sender at the moment of the latest packet received by the sending client
        nTotalLoss = 0
        if latestRecvPktType == PacketInfoType['SOURCE_PACKET'] :
            pktTypeStr = "SOURCE"
            sourceSentCount = latestRecvPktId + 1
            repairSentCount = otherTypePacketNum
        else :
            pktTypeStr = "REPAIR"
            repairSentCount = latestRecvPktId + 1
            sourceSentCount = otherTypePacketNum
        nTotalLoss = sourceSentCount + repairSentCount - nsource - nrepair
        self.PeEstimation(nTotalLoss, sourceSentCount + repairSentCount)

        # Update packets-in-flight and cWnd
        self.m_lastAckedSourceId = latestRecvSourceId
        self.m_lastAckedRepairId = latestRecvRepairId
        oldPacketsInFlight = self.m_packetsInFlight
        self.m_packetsInFlight = (self.m_lastSentSourceId - self.m_lastAckedSourceId + self.m_lastSentRepairId - self.m_lastAckedRepairId) * (1 - self.m_lossRate)
        self.UpdateCwnd()
        
        if inorder >= 0 and inorder < self.m_currentMaxSourceId :
            streamc.flush_acked_packets(self.m_enc, inorder)
        
        log += " latest ACKed %s packet of ID: %d" % (pktTypeStr, latestRecvPktId)
        logging.debug(log)

        log = "[Estimation] BandWidth: %f" % self.m_estBw
        log += " (pkts./sec.) RTT: %f" % (self.m_rtt * 1000)
        log += " (ms) LossRate: %f" % self.m_lossRate
        log += " Current cWnd: %d (pkts.)"  % self.m_cWnd
        log += " packetsInFlight: %d" % self.m_packetsInFlight
        log += " totalLoss: %d" % nTotalLoss
        log += " maxBw: %f." % self.m_estBwMax
        log += " totalSourceSent: %d" % sourceSentCount
        log += " totalRepairSent: %d" % repairSentCount
        log += " minRTT: %f" % self.m_rttMin
        log += " oldPacketsInFlight: %d" % oldPacketsInFlight
        logging.debug(log)

        # Record current encoder status
        logging.debug("[UpdatedEncoderStatusOnAck] headsid: %d tailsid: %d nextsid: %d" \
            % (self.m_enc.contents.headsid, self.m_enc.contents.tailsid, self.m_enc.contents.nextsid))

        """
        # Record the arrival time when the packet acknowledges the receipt of the ACK
        if pktInfo.pktType == PacketInfoType['SOURCE_PACKET'] :
            log = "[ %f ]" % recvAckTime + "[RecvAck] pepSender receives SOURCE packet ACK %d" % pktInfo.pktId
        else :
            log = "[ %f ]" % recvAckTime + "[RecvAck] pepSender receives REPAIR packet ACK %d" % pktInfo.pktId
        logging.info(log)
        """


    # Calculates the transmission time at this data rate.
    def CalculateBytesTxTime(self) :
        return ScPacketSize / self.m_pacingRate
    

    def SendDataPackets(self) :
        # Continue to send data packets if there is data and cWnd allows
        while self.m_cWnd > self.m_packetsInFlight :
            currentTime = time.time()
            # if pacing, sending is controlled by pacingTimer
            if self.m_pacing == True and self.m_cWnd != self.m_initCWnd :
                remainTime = currentTime  - self.m_lastPacketSentTime - self.m_pacingTimer
                if remainTime < 0 :
                    break
                else :
                    self.m_pacingTimer = 0

            # Output packet
            cpkt = None
            # Decide whether to send repair packet
            if self.TimeToSendRepairPacket() == True :
                if random.uniform(0, 1) < 0.95 :
                    cpkt = streamc.output_repair_packet_short(self.m_enc, 128)
                else :
                    cpkt = streamc.output_repair_packet(self.m_enc)
            # Decide whether to send source packet
            elif self.m_lastSentSourceId < self.m_currentMaxSourceId and cpkt == None :
                cpkt = streamc.output_source_packet(self.m_enc)
            if cpkt == None :
                break

            # Serialize packets and do SC-UDP encapsulation
            pktstr = streamc.serialize_packet(self.m_enc, cpkt)               # class ctypes.LP_c_ubyte
            pp = string_at(pktstr, self.m_cp.pktsize + 4 * sizeof(c_int))     # class 'bytes'
            pkt = PepPacket(PepHeader(PepPacketType['SC_PROTECTED_PKT']), pp)
            #pkt = PepPacket(PepHeader(PepPacketType['SC_PROTECTED_PKT']), cpkt.contents.serialize(self.m_cp.pktsize))
            
            # Send the scpkt and record the sending time
            self.m_udpSocket.sendto(pkt.packed(), self.m_peerAddress)
            sendTime = currentTime 
            self.m_lastPacketSentTime = sendTime

            # Record the packet sending time and other corresponding status values
            log = ""
            if cpkt.contents.sourceid != -1 :
                log = "[SendDataPacket] Send SOURCE packet %d" % cpkt.contents.sourceid
                self.m_pktInfoQueue.Add(PacketInfoType['SOURCE_PACKET'], cpkt.contents.sourceid, sendTime, self.m_enc.contents.rcount, self.m_lastAckedSourceNum+self.m_lastAckedRepairNum, self.m_lastFirstSentTime, self.m_lastAckTime)
                self.m_lastSentSourceId = cpkt.contents.sourceid
                self.m_lastSentSourceTime = currentTime
            else :
                log = "[SendDataPacket] Send REPAIR packet %d" % cpkt.contents.repairid
                self.m_pktInfoQueue.Add(PacketInfoType['REPAIR_PACKET'], cpkt.contents.repairid, sendTime, self.m_enc.contents.nextsid, self.m_lastAckedSourceNum+self.m_lastAckedRepairNum, self.m_lastFirstSentTime, self.m_lastAckTime)
                self.m_lastSentRepairId = cpkt.contents.repairid
                self.m_lastSentRepairTime = currentTime
                
            log += " Idle State: True" if self.m_newDataIdleState else " Idle State: False"
            logging.debug(log)

            # Record current encoder status
            logging.debug("[EncoderStatus] headsid: %d tailsid: %d nextsid: %d" \
                % (self.m_enc.contents.headsid, self.m_enc.contents.tailsid, self.m_enc.contents.nextsid))
            
            # Free the packet and pktstr, then count in-flight
            streamc.free_packet(cpkt)
            streamc.free_serialized_packet(pktstr)
            self.m_packetsInFlight += 1
            
            # Set the next sending time according to the pacing rate
            if self.m_pacing == True :
                if self.m_pacingTimer == 0 :
                    # print("Current Pacing Rate %f." % self.m_pacingRate)
                    # print("Timer is in expired state, activate it %f." % self.CalculateBytesTxTime())
                    self.m_pacingTimer = self.CalculateBytesTxTime()
                    break
            
            currentStreamcQueueSize = self.m_currentMaxSourceId - self.m_lastAckedSourceId
            if currentStreamcQueueSize != self.m_lastStreamcQueueSize :
                logging.debug("[StreamcQueueSize] %d" % currentStreamcQueueSize)
                self.m_lastStreamcQueueSize = currentStreamcQueueSize
            

    def TimeToSendRepairPacket(self) :
        # If all currently existing source packets are sent, PEPesc will be marked as idle 
        # and use 'self.m_numSentRepairAfterIdle' to count repair packets sent. 
        # Until new data arrives, pepesc will get out of the idle state 
        # and use 'self.m_numSentRepairExcludeIdle' to count repair packets sent.
        currentTime = time.time()
        if self.m_lastSentSourceId == self.m_currentMaxSourceId :
            if self.m_newDataIdleState == False :
                self.m_idleStateChangeTime = currentTime
            self.m_newDataIdleState = True
        else :
            if self.m_newDataIdleState == True :
                self.m_idleStateChangeTime = currentTime
            self.m_numSentRepairAfterIdle = 0
            self.m_idleCanSendRepairCount = 0
            self.m_newDataIdleState = False
        
        # The inoder of the peer PEPesc is stuck, send repair packets earlier when the source packets are under-saturated.
        if self.m_duplicatedInorder == True :
            if self.m_numSentRepairAfterStuck < 2 :
                self.m_numSentRepairAfterStuck += 1
                return True
            else :
                self.m_duplicatedInorder = False
                self.m_numSentRepairAfterStuck = 0
        
        if self.m_newDataIdleState :
            # heuristic
            if self.m_numSentRepairAfterIdle < 1 :
                self.m_idleCanSendRepairCount += 1
                if self.m_idleCanSendRepairCount == round (1 / (self.m_lossRate + ExtraRepairRate)) :
                    self.m_numSentRepairAfterIdle += 1
                    return True
            
            # If the current last source packet has been sent,
            # and the Ack has not been received within the past min rtt, send repair packets
            if currentTime - max(self.m_lastSentSourceTime, self.m_lastSentRepairTime) >= self.m_rttMin :
                return True
        else :
            # Repair the target insertion frequency of packets so that the expected mean decoding delay is 1/repairExcess packets
            targetRepairFreq = self.m_lossRate + ExtraRepairRate
            # Calculate the current required repair packet insertion frequency
            currentRepairFreq = self.m_numSentRepairExcludeIdle / (self.m_lastSentSourceId+1 + self.m_numSentRepairExcludeIdle) if self.m_lastSentSourceId >= 0 else 1
            if currentRepairFreq < targetRepairFreq and self.m_enc.contents.headsid < self.m_enc.contents.nextsid - 1 :
                self.m_numSentRepairExcludeIdle += 1
                return True
            else :
                return False
        return False
    

    def RecvDataPackets(self, pkt) :      
        buf = cast(pkt.body, POINTER(c_ubyte))
        rpkt = streamc.deserialize_packet(self.m_dec, buf)
        #rpkt.deserialize(buf, self.m_cp.pktsize)
        receiveTime = time.time() 
        outOrderRecv = False
        
        if rpkt.contents.sourceid != -1 :
            self.m_latestRecvSourceNum += 1
            self.m_numRecvSinceLastSourceAck += 1
            self.m_latestRecvPktType = PacketInfoType['SOURCE_PACKET']
            # reject the received source packet
            if rpkt.contents.sourceid <= self.m_dec.contents.inorder :
                logging.warning("[RecvDataPacket] Received out-dated source packet: %d current inorder: %d" % (rpkt.contents.sourceid, self.m_dec.contents.inorder))
                outOrderRecv = True
                return
            
            if rpkt.contents.sourceid < self.m_dec.contents.win_e :
                logging.warning("[RecvDataPacket] Out-of-order source packet %d received, inorder: %d, win_e: %d" % (rpkt.contents.sourceid, 
                                                                                                                      self.m_dec.contents.inorder, 
                                                                                                                      self.m_dec.contents.win_e))
                outOrderRecv = True
        else :
            self.m_latestRecvRepairNum += 1
            self.m_latestRecvPktType = PacketInfoType['REPAIR_PACKET']
            if rpkt.contents.repairid < self.m_lastRecvRepairId :
                outOrderRecv = True
        
        oldState   = self.m_dec.contents.active
        oldInorder = self.m_dec.contents.inorder
        
        streamc.receive_packet(self.m_dec, rpkt)
        # streamc.free_packet(rpkt)
        
        newState   = self.m_dec.contents.active
        newInorder = self.m_dec.contents.inorder

        log = "[DecoderStatus] inorder: %d" % self.m_dec.contents.inorder
        log += " SOURCE packet %d" % rpkt.contents.sourceid if rpkt.contents.sourceid != -1 else " REPAIR packet %d" % rpkt.contents.repairid
        
        if rpkt.contents.repairid != -1 :
            log += " encoding window: [ %d , %d ]" % (rpkt.contents.win_s, rpkt.contents.win_e)
        
        if self.m_dec.contents.active :
            log += " current decoder state: active with window: [ %d , %d ]" % (self.m_dec.contents.win_s, self.m_dec.contents.win_e)
        else :
            log += " current decoder state: inactive"
        logging.debug(log)

        # Record the time of decoding source packets
        if newInorder > oldInorder :
            for i in range(oldInorder+1, newInorder+1) : 
                logging.debug("[RecvDataPacket] Receive SOURCE packet %d" % i)
         
        # 解码器成功解码恢复出丢失分组，通知发送端解码成功
        currentTime = time.time()
        if oldState == 1 and newState == 0 :
            self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['DECODE_SUCCESS']), str(currentTime).encode()).packed(), self.m_peerAddress)
            
            log = "[DecodingSuccess] Decoder is inactivated and delivered in-order source packets between: [%d , %d]" % (oldInorder + 1 , newInorder)
            log += " with repair packet %d" % rpkt.contents.repairid
            log += " arrive time: %f" % receiveTime
            log += " decoding cost time: %f" % (currentTime - receiveTime)
            logging.debug(log)
        
        # 检测是否发生连续分组丢失现象
        if rpkt.contents.sourceid != -1 :
            if rpkt.contents.sourceid - self.m_lastRecvSourceId > 9 :
                message = str(str(currentTime)) + ' SOURCE ' + str(rpkt.contents.sourceid - self.m_lastRecvSourceId)
                self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['ADVERTISE_BURST']), message.encode()).packed(), self.m_peerAddress)
            self.m_lastRecvSourceId = rpkt.contents.sourceid
        else :
            if rpkt.contents.repairid - self.m_lastRecvRepairId > 9 :
                message = str(str(currentTime) + ' REPAIR ' + str(rpkt.contents.repairid - self.m_lastRecvRepairId))
                self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['ADVERTISE_BURST']), message.encode()).packed(), self.m_peerAddress)
            self.m_lastRecvRepairId = rpkt.contents.repairid
        
        if not outOrderRecv and self.m_latestRecvSourceNum + self.m_latestRecvRepairNum != self.m_numLastAcked :
            threshold = self.m_initCWnd if self.m_activeProbeBw else 1000
            if rpkt.contents.repairid != -1 :
                self.SendDataAck()
            elif self.m_numRecvSinceLastSourceAck >= SourceAckInterval or rpkt.contents.sourceid < threshold :
                self.m_numRecvSinceLastSourceAck = 0
                self.SendDataAck()
            
        return 


    def SendProbePackets(self) :
        self.m_probeBw = 0
        self.m_probePacketSentTimes = []

        probePacketId = 0
        while probePacketId < ProbeTrainLength :
            filler = ' ' * (ProbePacketSize - PepHeaderLength - len(str(probePacketId).encode()))
            self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['PROBE']), str(probePacketId).encode() + filler.encode()).packed(), self.m_peerAddress)
            probePacketId += 1
            self.m_probePacketSentTimes.append(time.time())
        
        self.m_lastProbedTime = time.time() 

        return 


    def RecvProbePacketAndSendProbeAck(self, pkt) :
        probePacketId = int(pkt.body)
        if probePacketId == 0 :
            self.m_lastProbeArrivedId = 0
            self.m_probeValidity = True
            self.m_firstProbeArriveTime = time.time()
        else :
            # Regardless of whether the last packet arrived, or whether the variable was reset in the last bandwidth probe, 
            # if any of the packets in this bandwidth probe are lost (including the first one), the condition will not be valid, 
            # the variables will be reset, and the bandwidth probe will be invalidated.
            if probePacketId != self.m_lastProbeArrivedId+1 :
                self.m_lastProbeArrivedId = -1
                self.m_probeValidity = False
            else :
                self.m_lastProbeArrivedId += 1
        
        if self.m_probeValidity :
            if probePacketId == ProbeTrainLength-1 :
                trainDispersion = time.time() - self.m_firstProbeArriveTime
                message = str(probePacketId) + ' ' + str(trainDispersion)
            else :
                message = str(probePacketId) 
            self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['PROBE_ACK']), message.encode()).packed(), self.m_peerAddress)


    def RecvProbeAcks(self, pkt) :
        message = pkt.body.decode().split(' ')
        probeAckId = int(message[0])

        sendTime = self.m_probePacketSentTimes[probeAckId]
        recvTime = time.time()
        self.RttEstimation(recvTime, sendTime)

        if probeAckId == ProbeTrainLength-1 :
            alpha = 0.9
            trainDispersion = float(message[1])
            instantaneousEstBw = (ProbeTrainLength-1) / trainDispersion * ProbePacketSize / ScPacketSize # pkts/sec.
            self.m_probeBw = alpha * self.m_probeBw + (1-alpha) * instantaneousEstBw  if self.m_probeBw != 0 else instantaneousEstBw # smoothed probe bandwidth
            self.m_estBw = self.m_probeBw * 0.8
            self.m_maxBwFilter.Insert(recvTime, self.m_estBw)
            self.UpdateCwnd()
            log = "EstBw: %f Mbps CWND: %d pkts RTT: %f ms." % (self.m_estBw*ScPacketSize*8/1024/1024, self.m_cWnd, self.m_rtt*1000)
            logging.info("[BwProbe] %s" % log)
            if self.m_detailFlag :
                print("[%s][%s:%d] %s" % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))
        
        return 


    def ReceiveAndHandlePepPacket(self) :
        # Receive pep packet
        data, addr = self.m_udpSocket.recvfrom(UdpBufSize)
        if len(data) == 0 :
            return
        pkt = PepPacket()
        pkt.parse(data)
        
        # Handle pep packet
        if pkt.header.mtype == PepPacketType['HANDSHAKE'] :
            self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['HANDSHAKE_ACK'])).packed(), self.m_peerAddress)

        elif pkt.header.mtype == PepPacketType['HANDSHAKE_ACK'] :
            self.EstablishPEPConnection(pkt)
        
        elif pkt.header.mtype == PepPacketType['WAVEHAND'] :
            self.ClosePEPConnection(pkt)

        elif pkt.header.mtype == PepPacketType['HEARTBEAT'] :
            self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['HEARTBEAT_ACK'])).packed(), self.m_peerAddress)

        elif pkt.header.mtype == PepPacketType['SC_PROTECTED_PKT'] :
            self.RecvDataPackets(pkt)

        elif pkt.header.mtype == PepPacketType['SC_DATA_ACK'] :
            self.RecvDataAck(pkt)

        elif pkt.header.mtype == PepPacketType['PROBE'] :
            self.RecvProbePacketAndSendProbeAck(pkt)
            
        elif pkt.header.mtype == PepPacketType['PROBE_ACK'] :
            self.RecvProbeAcks(pkt)

        elif pkt.header.mtype == PepPacketType['ADVERTISE_BURST'] : 
            # The receiver feedback that a continuous packet loss occurred before one RTT
            message = pkt.body.decode().split(' ')
            peerBurstTime      = float(message[0])
            burstPacketType    = message[1]
            burstPacketsNumber = int(message[2])

            self.m_lastBurstTime = time.time()
            
            logging.warning("[Burst] Peer PEPesc Receiver advertised burst %s %d packets." % (burstPacketType, burstPacketsNumber))
        
        elif pkt.header.mtype == PepPacketType['DECODE_SUCCESS'] :
            self.m_lastDecSuccTime = float(pkt.body.decode())
        
        elif pkt.header.mtype == PepPacketType['HEARTBEAT_ACK'] :
            logging.debug("[HEARTBEAT_ACK] heartbeat ACK received.")    # nothing needs to be done here, since response update has been done when reading packets from channel
        
        else :
            logging.debug("Unknown PepPacket type")

        return 


    def HandleScPayloads(self) :
        readScPayloadNumber = 0 
        maxAllowReadOnce    = 10
        while self.m_dec.contents.inorder >= self.m_inorderNext and readScPayloadNumber < maxAllowReadOnce :
            buf = self.m_dec.contents.recovered[self.m_inorderNext % DEC_ALLOC]
            readScPayloadNumber += 1
            self.m_inorderNext += 1
            if buf is None :
                break
            payload = bytes(cast(buf, POINTER(c_ubyte * self.m_cp.pktsize))[0]) # class 'bytes'

            scPayLoad = SCPayload()
            scPayLoad.parse(payload)
            neighbor = scPayLoad.tcpDestinationAddr
            remote   = scPayLoad.tcpSourceAddr
            recvAddr = (remote[0], remote[1], neighbor[0], neighbor[1])
            sendAddr = (neighbor[0], neighbor[1], remote[0], remote[1])

            if scPayLoad.msg == ScProtectedMsg['TCP_RAW_DATA'] :
                # Use the corresponding channel for application sending
                if recvAddr not in self.m_findChidForPEPescToSend :
                    return
                channel = self.m_channels[self.m_findChidForPEPescToSend[recvAddr]]
                channel.send(scPayLoad.msgData)
                self.m_tcpRecvBufLen[recvAddr] += len(scPayLoad.msgData)

                if recvAddr in self.m_linkToBeClosed and self.m_tcpRecvBufLen[recvAddr] == self.m_linkToBeClosed[recvAddr] :
                    neighborRecvTcpDataLength = self.m_tcpRecvBufLen[recvAddr]
                    neighborSentTcpDataLength = self.m_tcpSentBufLen[sendAddr]
                    chid = self.m_findChidForPEPescToSend[recvAddr]
                    CloseChannel(self.m_channels, chid)
                    del self.m_findChidForPEPescToSend[recvAddr]
                    del self.m_linkToBeClosed[recvAddr]
                    del self.m_tcpRecvBufLen[recvAddr]
                    del self.m_tcpSentBufLen[sendAddr]
                    if self.m_detailFlag :
                        print("[%s][%s:%d] Close channel with %s:%d."\
                            % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], neighbor[0], neighbor[1]))
                        print("[%s][%s:%d] TCP connection {%s:%d -> %s:%d} Total sent %.3fMBytes Total recv %.3fMBytes."\
                            % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1],\
                            neighbor[0], neighbor[1], remote[0], remote[1], neighborSentTcpDataLength/1024/1024, neighborRecvTcpDataLength/1024/1024))
            
            elif scPayLoad.msg == ScProtectedMsg['REMOTE_REQUEST'] :
                chid = OpenOutConnChannel(self.m_channels, neighbor, remote, maxWaitTime=0.02)
                if chid == -1 :
                    print("Open channel error,Exit!")
                    sys.exit()

                self.m_findChidForPEPescToSend[recvAddr] = chid
                self.m_tcpRecvBufLen[recvAddr] = 0
                self.m_tcpSentBufLen[sendAddr] = 0
                #print("[%s][%s:%d] Try to connect to %s:%d."\
                #        % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], neighbor[0], neighbor[1]))

            elif scPayLoad.msg == ScProtectedMsg['REMOTE_EXIST'] :
                tcpReceiver = self.m_tcpReceiverWaiting[sendAddr]
                del self.m_tcpReceiverWaiting[sendAddr]
                chid = OpenInConnChannel(self.m_channels, tcpReceiver, remote, maxWaitTime=0.02)

                self.m_findChidForPEPescToSend[recvAddr] = chid
                self.m_tcpRecvBufLen[recvAddr] = 0
                self.m_tcpSentBufLen[sendAddr] = 0
                
                if self.m_detailFlag :
                    print("[%s][%s:%d] Peer PEPesc reports that connecting to %s:%d successfully."\
                        % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], remote[0], remote[1]))
                logging.info("[TCP] Connect success {%s:%d -> %s:%d}" % (neighbor[0], neighbor[1], remote[0], remote[1]))

            elif scPayLoad.msg == ScProtectedMsg['REMOTE_NOT_EXIST'] :
                tcpReceiver = self.m_tcpReceiverWaiting[sendAddr]
                tcpReceiver.close()
                del self.m_tcpReceiverWaiting[sendAddr]
                if self.m_detailFlag :
                    print("[%s][%s:%d] Peer PEPesc reports that failed to connect to %s:%d."\
                        % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], remote[0], remote[1]))

            elif scPayLoad.msg == ScProtectedMsg['REMOTE_EXIT'] :
                remoteTotalSentTcpDataLength = int(scPayLoad.msgData)
                # If I have not created a channel for the connection or has closed the channel, ignore this notification
                if recvAddr not in self.m_findChidForPEPescToSend :
                    return
                if self.m_detailFlag :
                    print("[%s][%s:%d] Peer PEPesc reports that %s:%d has exited."\
                        % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], remote[0], remote[1]))

                # If I have sent all the data, immediately closing the connection with my neighbor, or waiting to receive and send full TCP data
                if self.m_tcpRecvBufLen[recvAddr] == remoteTotalSentTcpDataLength :
                    chid = self.m_findChidForPEPescToSend[recvAddr]
                    CloseChannel(self.m_channels, chid)
                    
                    neighborRecvTcpDataLength = self.m_tcpRecvBufLen[recvAddr]
                    neighborSentTcpDataLength = self.m_tcpSentBufLen[sendAddr]
                    self.m_totalDataSentSize += neighborSentTcpDataLength
                    self.m_totalDataRecvSize += neighborRecvTcpDataLength
                    
                    del self.m_findChidForPEPescToSend[recvAddr]
                    del self.m_tcpRecvBufLen[recvAddr]
                    del self.m_tcpSentBufLen[sendAddr]
                    
                    if self.m_detailFlag :
                        print("[%s][%s:%d] Close channel with %s:%d."\
                            % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], neighbor[0], neighbor[1]))
                        print("[%s][%s:%d] TCP connection {%s:%d -> %s:%d} Total sent %.3fMBytes Total recv %.3fMBytes."\
                            % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1],\
                            neighbor[0], neighbor[1], remote[0], remote[1], neighborSentTcpDataLength/1024/1024, neighborRecvTcpDataLength/1024/1024))
                else :
                    self.m_linkToBeClosed[recvAddr] = remoteTotalSentTcpDataLength

        return 


    def ReadChannels(self, readableChannelIds) :
        for chid in readableChannelIds :#list(self.m_channels) :
            ch = self.m_channels[chid]
            sendAddr = (ch.neighbor[0], ch.neighbor[1], ch.remote[0], ch.remote[1])
            if (ch.eventmask & CH_READ) :
                while True :
                    tcpRawData = ch.receive() # class 'bytes'
                    if not tcpRawData :
                        break
                    self.EnqueuePackets(ScProtectedMsg['TCP_RAW_DATA'], ch.neighbor, ch.remote, tcpRawData)
                    self.m_tcpSentBufLen[sendAddr] += len(tcpRawData)

                    logging.debug("[MsgQueueSize] %d %d" % (chid, self.m_channels[chid].recvq.size()))
                    logging.debug("[StreamcQueueSize] %d" % (self.m_currentMaxSourceId - self.m_lastAckedSourceId))
        
        return 

    
    def HandlePollReports(self, pollReports) :
        for (chid, msg, neighbor, remote) in pollReports :
            recvAddr = (remote[0], remote[1], neighbor[0], neighbor[1])
            sendAddr = (neighbor[0], neighbor[1], remote[0], remote[1])

            if msg == PollChannelMsg['CONNECT_SUCCESS'] : 
                # Notify the peer PEPesc that the connection to the original destination is successful
                self.EnqueuePackets(ScProtectedMsg['REMOTE_EXIST'], neighbor, remote)
                if self.m_detailFlag :
                    print("[%s][%s:%d] Connect to %s:%d successfully, notify peer PEPesc."\
                        % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], neighbor[0], neighbor[1]))
                continue

            elif msg == PollChannelMsg['CONNECT_FAILED'] :
                # Notify the peer PEPesc that the connection to the original destination failed
                self.EnqueuePackets(ScProtectedMsg['REMOTE_NOT_EXIST'], neighbor, remote)
                if self.m_detailFlag :
                    print("[%s][%s:%d] Failed to connect to %s:%d, notify peer PEPesc."\
                        % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], neighbor[0], neighbor[1]))

            elif msg == PollChannelMsg['NEIGHBOR_EXIT'] :
                # Notify the peer PEPesc that my neighbor has exited, 
                # request to mark the connection as about to be closed, 
                # and close it immediately after receiving and sending complete TCP data.
                neighborRecvTcpDataLength = self.m_tcpRecvBufLen[recvAddr]
                neighborSentTcpDataLength = self.m_tcpSentBufLen[sendAddr]
                self.EnqueuePackets(ScProtectedMsg['REMOTE_EXIT'], neighbor, remote, str(neighborSentTcpDataLength).encode())
                self.m_totalDataSentSize += neighborSentTcpDataLength
                self.m_totalDataRecvSize += neighborRecvTcpDataLength

                if self.m_detailFlag :
                    print("[%s][%s:%d] %s:%d has exited, notify peer PEPesc."\
                        % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], neighbor[0], neighbor[1]))
                    print("[%s][%s:%d] TCP connection {%s:%d -> %s:%d} Total sent %.3fMBytes Total recv %.3fMBytes."\
                        % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1],\
                        neighbor[0], neighbor[1], remote[0], remote[1], neighborSentTcpDataLength/1024/1024, neighborRecvTcpDataLength/1024/1024))
                
            del self.m_findChidForPEPescToSend[recvAddr]
            del self.m_tcpRecvBufLen[recvAddr]
            del self.m_tcpSentBufLen[sendAddr]
        
        return


    def InterceptTcpConnection(self) :
        tcpReceiver, neighbor = self.m_tcpListener.accept()
        remote = self.GetOriginalDst(tcpReceiver)

        if not self.m_peerOnline :
            tcpReceiver.close()
            log = "Peer PEPesc is offline or haven't connected, reject "
        elif self.m_selfPreClose :
            tcpReceiver.close()
            log = "PEPesc will be closed, reject "
        else :
            sendAddr = (neighbor[0], neighbor[1], remote[0], remote[1])
            self.m_tcpReceiverWaiting[sendAddr] = tcpReceiver
            self.EnqueuePackets(ScProtectedMsg['REMOTE_REQUEST'], neighbor, remote)
            log = "Intercept "
        
        self.m_rejectConnectionNum += 1

        log += "connection request {%s:%d -> %s:%d}." % (neighbor[0], neighbor[1], remote[0], remote[1])
        logging.info("[TCP] %s" % log)
        if self.m_detailFlag :
            print("[%s][%s:%d] %s" % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))

        return 


    # Get the client source and destination addresses of the tcp connection, 
    # which are used to create tcpHeader to ensure successful packet forwarding
    def GetOriginalDst(self, sock) :
        try :
            SO_ORIGINAL_DST = 80
            SOCKADDR_MIN = 16
            sockaddr = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, SOCKADDR_MIN)
            (proto, port, a, b, c, d) = struct.unpack('!HHBBBB', sockaddr[:8])
            assert(socket.htons(proto) == socket.AF_INET)
            ip = '%d.%d.%d.%d' % (a, b, c, d)
            return (ip, port)
        except socket.error as e :
            if e.args[0] == errno.ENOPROTOOPT :
                return sock.getsockname()
            raise

    # Establish connection between PEP entities
    # 2-way hand shake
    def EstablishPEPConnection(self, pkt=None) :
        if not pkt :
            self.m_handShakeTimes += 1
            if self.m_handShakeTimes > MaxHandShakeTimes :
                self.m_selfClose = True
                log = "Have tried to shake hand %d times. Turn off directly." % MaxHandShakeTimes
                logging.warning("[PEPesc] %s" % log)
                if self.m_detailFlag :
                    print("[%s][%s:%d] %s" % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))
                return 
                
            if self.m_handShakeTimes > 1 :
                log = "Failed to connect peer PEPesc %s:%d. Retry again." % (self.m_peerAddress[0], self.m_peerAddress[1])
                logging.warning("[PEPesc] %s" % log)
                if self.m_detailFlag :
                    print("[%s][%s:%d] %s" % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))
            
            self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['HANDSHAKE'])).packed(), self.m_peerAddress)
            self.m_lastHandShakeTime = time.time()
        
        else :
            # After the connection is successful, randomly backoff bandwidth probing (to avoid burst congestion)
            self.m_peerOnline = True
            self.m_lastProbedTime = time.time() - random.uniform(1/2*ProbeInterval, ProbeInterval)
            log = "Connect peer PEPesc %s:%d successfully." % (self.m_peerAddress[0], self.m_peerAddress[1])
            if self.m_detailFlag :
                print("[%s][%s:%d] %s" % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))

    
    # Close connection between PEP entities
    def ClosePEPConnection(self, pkt=None) :
        self.m_selfClose = True
        if not pkt :
            self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['WAVEHAND'])).packed(), self.m_peerAddress)
            log = "Close connection with peer PEPesc %s:%d." % (self.m_peerAddress[0], self.m_peerAddress[1])
            logging.info("[PEPesc] %s" % log)
            if self.m_detailFlag :
                print("[%s][%s:%d] %s" % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))
        else :
            log = "Peer PEPesc %s:%d closed. Turn off instantly." % (self.m_peerAddress[0], self.m_peerAddress[1])
            logging.info("[PEPesc] %s" % log)
            if self.m_detailFlag :
                print("[%s][%s:%d] %s" % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))

    
    # Heartbeat between PEP entities if idle for long 
    def HeartBeat(self) :
        self.m_heartBeatTimes += 1
        if self.m_heartBeatTimes > MaxHeartBeatTimes :
            self.m_selfClose = True
            log = "Have tried heartbeat %d times but no response. Turn off instantly." % MaxHeartBeatTimes
            logging.warning("[PEPesc] %s" % log)
            if self.m_detailFlag :
                print("[%s][%s:%d] %s" % (time.strftime('%Y-%m-%d %X',time.localtime()), self.m_selfAddress[0], self.m_selfAddress[1], log))
        else :
            self.m_lastHeartBeatTime = time.time()
            self.m_udpSocket.sendto(PepPacket(PepHeader(PepPacketType['HEARTBEAT'])).packed(), self.m_peerAddress)


    # Main loop
    def Start(self) :
        while True :
            try :
                if self.m_selfClose :
                    break
                
                udpPollEvents = (select.POLLIN)

                currentTime = time.time()
                if not self.m_peerOnline :
                    if currentTime - self.m_lastHandShakeTime >= HandShakeInterval :
                        udpPollEvents |= select.POLLOUT
                else :
                    if self.m_selfPreClose :
                        udpPollEvents |= select.POLLOUT
                    else :
                        if (self.m_heartBeatTimes == 0 and currentTime - self.m_lastResponseTime >= MaxHeartBeatWaitTime) \
                            or (self.m_heartBeatTimes > 0 and currentTime - self.m_lastHeartBeatTime >= HeartBeatInterval) :
                            udpPollEvents |= select.POLLOUT

                        if self.m_cWnd > self.m_packetsInFlight and self.m_inorderAck.inorder < self.m_currentMaxSourceId :
                            udpPollEvents |= select.POLLOUT

                        if self.m_activeProbeBw and \
                            (self.m_currentMaxSourceId == -1 or self.m_inorderAck.inorder == self.m_currentMaxSourceId) \
                                and currentTime - max(self.m_lastProbedTime, self.m_lastSentSourceTime, self.m_lastSentRepairTime) >= ProbeInterval :
                            udpPollEvents |= select.POLLOUT

                # Poll tcpListener, udpSocket and TCP channels
                pepescAvailableBw = self.m_constBw if self.m_constBw else self.m_estBwMax # pkts/sec. 
                tcpAvailableBwMax = (1-self.m_lossRate-ExtraRepairRate) * pepescAvailableBw * MsgDataMaxLength * 8 if pepescAvailableBw != 0 else 5 * 1024 * 1024 # bps
                bufferRemain = MaxBufferQueueLength - (self.m_currentMaxSourceId - self.m_lastSentSourceId)
                (readableTcpChannelIds, pollReports) = PollChannels(self.m_channels, tcpAvailableBwMax, bufferRemain, udpPollEvents)

                # tcpListener catch tcp connection
                if self.m_channels[self.m_tcpListenChid].eventmask & CH_READ :
                    self.InterceptTcpConnection()
                
                # Always reset the heartbeat counter and update response time when receiving something from the peer entity
                if self.m_channels[self.m_udpChid].eventmask & CH_READ :
                    self.m_lastResponseTime = time.time()
                    self.m_heartBeatTimes = 0
                    self.ReceiveAndHandlePepPacket()
                
                # Read TCP raw data from readable TCP channels
                self.ReadChannels(readableTcpChannelIds)
                
                # Handle poll reports
                self.HandlePollReports(pollReports)

                # Handle ScPayload from source packets in decoder's recoverd queue
                self.HandleScPayloads()

                # Have something to send
                currentTime = time.time()
                if self.m_channels[self.m_udpChid].eventmask & CH_WRITE :
                    if not self.m_peerOnline :
                        if currentTime - self.m_lastHandShakeTime >= HandShakeInterval :
                            self.EstablishPEPConnection()
                    
                    else :
                        if self.m_selfPreClose :
                            self.ClosePEPConnection()
                            break
                        
                        if (self.m_heartBeatTimes == 0 and currentTime - self.m_lastResponseTime >= MaxHeartBeatWaitTime) \
                            or (self.m_heartBeatTimes > 0 and currentTime - self.m_lastHeartBeatTime >= HeartBeatInterval) :
                            self.HeartBeat()
                        
                        if self.m_cWnd > self.m_packetsInFlight and self.m_inorderAck.inorder < self.m_currentMaxSourceId :
                            self.SendDataPackets()
                        
                        if self.m_activeProbeBw and \
                            (self.m_currentMaxSourceId == -1 or self.m_inorderAck.inorder == self.m_currentMaxSourceId) \
                                and currentTime - max(self.m_lastProbedTime, self.m_lastSentSourceTime, self.m_lastSentRepairTime) >= ProbeInterval :
                            self.SendProbePackets()
                
                # Update heartbeat time
                self.m_lastHeartBeatTime = max(self.m_lastHeartBeatTime, self.m_lastHandShakeTime, self.m_lastProbedTime, self.m_lastSentSourceTime, self.m_lastSentRepairTime)

            except KeyboardInterrupt :
                if self.m_detailFlag :
                    print()
                if self.m_peerOnline :
                    self.m_selfPreClose = True
                else :
                    self.m_selfClose = True
                

    def Stop(self) :
        # Free encoder and decoder
        streamc.free_encoder(self.m_enc)
        streamc.free_decoder(self.m_dec)
        
        # Close channels
        for i in list(self.m_channels) :
            CloseChannel(self.m_channels, i)


def BandwidthParameterUnit(bw) :
    try :
        if bw != None and 'Mbps' not in bw :
            raise ValueError()
        else :
            return bw
    except ValueError :
        raise argparse.ArgumentTypeError("The unit of bandwidth parameter must be 'Mbps'! : {}".format(bw))

if __name__ == "__main__" :
    parser = argparse.ArgumentParser()
    parser.add_argument('--selfIp', required=True, type=str, help="IP for local PEPesc to bind")
    parser.add_argument('--selfPort', required=True, type=int, help="Port for local PEPesc to bind")
    parser.add_argument('--peerIp', required=True, type=str, help="Peer PEPesc's ip")
    parser.add_argument('--peerPort', required=True, type=int, help="Peer PEPesc's port")
    parser.add_argument('--bwEstMethod', required=False, type=str, default='Jersy', choices=['Jersy', 'BBR'], help="Select the bandwidth estimation algorithm, choices:Jersy, BBR(default:Jersy)")
    parser.add_argument('--deactivateProbeBw', action='store_true', default=False, help="Deactivate the active packet-train bandwidth probe")
    parser.add_argument('--maxBw', required=False, default=None, type=BandwidthParameterUnit, help="Maximum allowable bandwidth(Mbps)")
    parser.add_argument('--ConstBw', required=False, default=None, type=BandwidthParameterUnit, help="Constant rate mode(Mbps)")
    parser.add_argument('-d', '--detail', action='store_true', default=False, help="Display the details")
    parser.add_argument('-l', '--logging', required=False, type=str, default=None, choices=['INFO', 'WARNING', 'ERROR', 'DEBUG'], help="Save the logs, choices:INFO, WARNING, ERROR, DEBUG(default:ERROR)")
    
    args = parser.parse_args()
    
    logLevel = logging.ERROR if not args.logging else getattr(logging, args.logging.upper())
    logging.basicConfig(filename='./pep.log',
                        filemode='w',#'a',
                        level=logLevel, 
                        format='%(levelname)s: [%(asctime)s] %(message)s')

    pep = pepApp()
    pep.SetAttribute(args, PacketSize)

    if pep.m_detailFlag :
            print("[%s][%s:%d] PEPesc starting..."\
                    % (time.strftime('%Y-%m-%d %X',time.localtime()), pep.m_selfAddress[0], pep.m_selfAddress[1]))

    #cProfile.run('pep.Start()', 'PEPesc.stats')
    pep.Start()
    pep.Stop()

    if pep.m_detailFlag :
        print("[%s][%s:%d] PEPesc closed. %.6f Mbytes total sent, %.6f MBytes total received, %d TCP connection rejected."\
            % (time.strftime('%Y-%m-%d %X',time.localtime()), pep.m_selfAddress[0], pep.m_selfAddress[1], pep.m_totalDataSentSize/1024/1024, pep.m_totalDataRecvSize/1024/1024, pep.m_rejectConnectionNum))
    
    os._exit(0)
    
