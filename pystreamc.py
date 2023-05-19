#This file wraps APIs from libstreamc.so in Python
from ctypes import cdll, c_int, c_double, c_ubyte, c_ulong, Structure, POINTER
N = 624
EWIN = 100
DEC_ALLOC = 10000


class MT19937(Structure):
    _fields_ = [("mt"  , c_ulong * N),
                ("mti" , c_int)]


class parameters(Structure):
    _fields_ = [("gfpower"  , c_int),
                ("pktsize"  , c_int),
                ("repfreq"  , c_double),
                ("seed"     , c_int)]


class packet(Structure) :
    _fields_ = [("sourceid" , c_int),
                ("repairid" , c_int),
                ("win_s"    , c_int),
                ("win_e"    , c_int),
                ("coes"     , POINTER(c_ubyte)),
                ("syms"     , POINTER(c_ubyte))]
    

class ROW_VEC(Structure) :
    pass
    	

class encoder(Structure):
    _fields_ = [("cp" , POINTER(parameters)),
                ("count"   , c_int),
                ("nextsid" , c_int),
                ("rcount"  , c_int),
                ("bufsize" , c_int),
                ("snum"    , c_int),
                ("head"    , c_int),
                ("tail"    , c_int),
                ("headsid" , c_int),
                ("tailsid" , c_int),
                ("srcpkt"  , POINTER(POINTER(c_ubyte)))]
    

class decoder(Structure):
    _fields_ = [("cp"         , POINTER(parameters)),
                ("pbuf"     , POINTER(packet)),
                ("active"    , c_int),
                ("inorder"   , c_int),
                ("win_s"     , c_int),
                ("win_e"     , c_int),
                ("dof"       , c_int),
                ("row"       , POINTER(POINTER(ROW_VEC))),
                ("message"   , POINTER(POINTER(c_ubyte))),
                ("recovered" , POINTER(POINTER(c_ubyte))),
                ("prev_rep"  , c_int),
                ("prng"      , MT19937)]


streamc = cdll.LoadLibrary("libstreamc.so")

##########################
# Wrap encoder functions #
##########################

streamc.initialize_encoder.argtypes = [POINTER(parameters), POINTER(c_ubyte), c_int]
streamc.initialize_encoder.restype  = POINTER(encoder)

streamc.enqueue_packet.argtypes = [POINTER(encoder), c_int, POINTER(c_ubyte)]
streamc.enqueue_packet.restype  = c_int

streamc.output_repair_packet.argtypes = [POINTER(encoder)]
streamc.output_repair_packet.restype  = POINTER(packet)

streamc.output_repair_packet_short.argtypes = [POINTER(encoder), c_int]
streamc.output_repair_packet_short.restype  = POINTER(packet)

streamc.output_source_packet.argtypes = [POINTER(encoder)]
streamc.output_source_packet.restype  = POINTER(packet)

streamc.flush_acked_packets.argtypes = [POINTER(encoder), c_int]
streamc.flush_acked_packets.restype  = None

streamc.visualize_buffer.argtypes = [POINTER(encoder)]
streamc.visualize_buffer.restype  = None

streamc.free_packet.argtypes = [POINTER(packet)]
streamc.free_packet.restype  = None

streamc.serialize_packet.argtypes = [POINTER(encoder), POINTER(packet)]
streamc.serialize_packet.restype  = POINTER(c_ubyte)

streamc.free_serialized_packet.argtypes = [POINTER(c_ubyte)]
streamc.free_serialized_packet.restype  = None

streamc.free_encoder.argtypes = [POINTER(encoder)]
streamc.free_encoder.restype  = None

##########################
# Wrap decoder functions #
##########################

streamc.initialize_decoder.argtypes = [POINTER(parameters)]
streamc.initialize_decoder.restype  = POINTER(decoder)

streamc.activate_decoder.argtypes = [POINTER(decoder), POINTER(packet)]
streamc.activate_decoder.restype  = c_int

streamc.deactivate_decoder.argtypes = [POINTER(decoder)]
streamc.deactivate_decoder.restype  = c_int

streamc.receive_packet.argtypes = [POINTER(decoder), POINTER(packet)]
streamc.receive_packet.restype  = c_int

streamc.process_packet.argtypes = [POINTER(decoder), POINTER(packet)]
streamc.process_packet.restype  = c_int

streamc.deserialize_packet.argtypes = [POINTER(decoder), POINTER(c_ubyte)]
streamc.deserialize_packet.restype  = POINTER(packet)

streamc.free_decoder.argtypes = [POINTER(decoder)]
streamc.free_decoder.restype  = None

#################################################
# Wrap pseudo-random number generator functions #
#################################################

#streamc.mt19937_init.argtypes = [c_ulong, c_ulong]
#streamc.mt19937_init.restype  = None

#streamc.mt19937_randint.argtypes = [POINTER(c_ulong) , POINTER(c_int)]
#streamc.mt19937_randint.restype  = c_ulong

