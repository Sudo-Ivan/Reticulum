from .Interface import Interface
import socketserver
import threading
import platform
import socket
import time
import sys
import os
import RNS
import asyncio

# TODO: Remove
import logging
logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)

class HDLC():
    FLAG              = 0x7E
    ESC               = 0x7D
    ESC_MASK          = 0x20

    @staticmethod
    def escape(data):
        data = data.replace(bytes([HDLC.ESC]), bytes([HDLC.ESC, HDLC.ESC^HDLC.ESC_MASK]))
        data = data.replace(bytes([HDLC.FLAG]), bytes([HDLC.ESC, HDLC.FLAG^HDLC.ESC_MASK]))
        return data

class KISS():
    FEND              = 0xC0
    FESC              = 0xDB
    TFEND             = 0xDC
    TFESC             = 0xDD
    CMD_DATA          = 0x00
    CMD_UNKNOWN       = 0xFE

    @staticmethod
    def escape(data):
        data = data.replace(bytes([0xdb]), bytes([0xdb, 0xdd]))
        data = data.replace(bytes([0xc0]), bytes([0xdb, 0xdc]))
        return data

class I2PController:
    def __init__(self, rns_storagepath):
        import RNS.vendor.i2plib as i2plib
        import RNS.vendor.i2plib.utils

        self.loop = None
        self.i2plib = i2plib
        self.utils = i2plib.utils
        self.sam_address = i2plib.get_sam_address()

        self.storagepath = rns_storagepath+"/i2p"
        if not os.path.isdir(self.storagepath):
            os.makedirs(self.storagepath)


    def start(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        self.loop = asyncio.get_event_loop()
        try:
            self.loop.run_forever()
        except Exception as e:
            RNS.log("Exception on event loop for "+str(self)+": "+str(e), RNS.LOG_ERROR)
        finally:
            self.loop.close()

        RNS.log("EVENT LOOP DOWN")

    def stop(self):
        for task in asyncio.Task.all_tasks(loop=self.loop):
            task.cancel()

        self.loop.stop()

    def get_free_port(self):
        return self.i2plib.utils.get_free_port()

    def client_tunnel(self, owner, i2p_destination):
        try:
            async def tunnel_up():
                RNS.log("Bringing up I2P tunnel to "+str(owner)+" in background, this may take a while...", RNS.LOG_INFO)
                tunnel = self.i2plib.ClientTunnel(i2p_destination, owner.local_addr, sam_address=self.sam_address)
                await tunnel.run()
                RNS.log(str(owner)+ " tunnel setup complete", RNS.LOG_VERBOSE)

            asyncio.run_coroutine_threadsafe(tunnel_up(), self.loop)

        except Exception as e:
            raise IOError("Could not connect to I2P SAM API while configuring to "+str(owner)+". Check that I2P is running and SAM is enabled.")


    def server_tunnel(self, owner):
        i2p_dest_hash = RNS.Identity.full_hash(RNS.Identity.full_hash(owner.name.encode("utf-8")))
        i2p_keyfile   = self.storagepath+"/"+RNS.hexrep(i2p_dest_hash, delimit=False)+".i2p"

        i2p_dest = None
        if not os.path.isfile(i2p_keyfile):
            coro = self.i2plib.new_destination(sam_address=self.sam_address, loop=self.loop)
            i2p_dest = asyncio.run_coroutine_threadsafe(coro, self.loop).result()
            key_file = open(i2p_keyfile, "w")
            key_file.write(i2p_dest.private_key.base64)
            key_file.close()
            # TODO: Remove
            RNS.log("Created")
        else:
            key_file = open(i2p_keyfile, "r")
            prvd = key_file.read()
            key_file.close()
            i2p_dest = self.i2plib.Destination(data=prvd, has_private_key=True)
            # TODO: Remove
            RNS.log("Loaded")

        i2p_b32 = i2p_dest.base32

        try:
            async def tunnel_up():
                RNS.log(str(owner)+" Bringing up I2P tunnel in background, this may take a while...", RNS.LOG_INFO)
                tunnel = self.i2plib.ServerTunnel((owner.bind_ip, owner.bind_port), loop=self.loop, destination=i2p_dest, sam_address=self.sam_address)
                await tunnel.run()
                RNS.log(str(owner)+ " tunnel setup complete, instance reachable at: "+str(i2p_dest.base32)+".b32.i2p", RNS.LOG_VERBOSE)

            asyncio.run_coroutine_threadsafe(tunnel_up(), self.loop)

        except Exception as e:
            raise IOError("Could not connect to I2P SAM API while configuring "+str(self)+". Check that I2P is running and SAM is enabled.")

    def get_loop(self):
        return asyncio.get_event_loop()


class ThreadingI2PServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass

class I2PInterfacePeer(Interface):
    RECONNECT_WAIT = 10
    RECONNECT_MAX_TRIES = None

    # TCP socket options
    TCP_USER_TIMEOUT = 20
    TCP_PROBE_AFTER = 5
    TCP_PROBE_INTERVAL = 3
    TCP_PROBES = 5

    I2P_USER_TIMEOUT = 40
    I2P_PROBE_AFTER = 10
    I2P_PROBE_INTERVAL = 5
    I2P_PROBES = 6

    def __init__(self, owner, name, target_i2p_dest=None, connected_socket=None, max_reconnect_tries=None):
        self.rxb = 0
        self.txb = 0
        
        self.IN               = True
        self.OUT              = False
        self.socket           = None
        self.parent_interface = None
        self.name             = name
        self.initiator        = False
        self.reconnecting     = False
        self.never_connected  = True
        self.owner            = owner
        self.writing          = False
        self.online           = False
        self.detached         = False
        self.kiss_framing     = False
        self.i2p_tunneled     = True
        self.i2p_dest         = None
        self.i2p_tunnel_ready = False

        if max_reconnect_tries == None:
            self.max_reconnect_tries = I2PInterfacePeer.RECONNECT_MAX_TRIES
        else:
            self.max_reconnect_tries = max_reconnect_tries

        if connected_socket != None:
            self.receives    = True
            self.target_ip   = None
            self.target_port = None
            self.socket      = connected_socket

            if platform.system() == "Linux":
                self.set_timeouts_linux()
            elif platform.system() == "Darwin":
                self.set_timeouts_osx()

        elif target_i2p_dest != None:
            self.receives    = True
            self.initiator   = True

            self.bind_ip     = "127.0.0.1"
            self.bind_port   = self.owner.i2p.get_free_port()
            self.local_addr  = (self.bind_ip, self.bind_port)
            self.target_ip = self.bind_ip
            self.target_port = self.bind_port

            self.owner.i2p.client_tunnel(self, target_i2p_dest)

            # TODO: Remove
            RNS.log("TCP params: "+str((self.bind_ip, self.bind_port)))
            
            if not self.connect(initial=True):
                # TODO: Remove
                RNS.log("Initial TCP attempt failed, trying reconnects...")
                thread = threading.Thread(target=self.reconnect)
                thread.setDaemon(True)
                thread.start()
            else:
                # TODO: Remove
                RNS.log("Initial TCP attempt OK, entering read loop")
                thread = threading.Thread(target=self.read_loop)
                thread.setDaemon(True)
                thread.start()
                if not self.kiss_framing:
                    self.wants_tunnel = True


    def set_timeouts_linux(self):
        if not self.i2p_tunneled:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, int(I2PInterfacePeer.TCP_USER_TIMEOUT * 1000))
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, int(I2PInterfacePeer.TCP_PROBE_AFTER))
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, int(I2PInterfacePeer.TCP_PROBE_INTERVAL))
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, int(I2PInterfacePeer.TCP_PROBES))
        else:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, int(I2PInterfacePeer.I2P_USER_TIMEOUT * 1000))
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, int(I2PInterfacePeer.I2P_PROBE_AFTER))
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, int(I2PInterfacePeer.I2P_PROBE_INTERVAL))
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, int(I2PInterfacePeer.I2P_PROBES))

    def set_timeouts_osx(self):
        if hasattr(socket, "TCP_KEEPALIVE"):
            TCP_KEEPIDLE = socket.TCP_KEEPALIVE
        else:
            TCP_KEEPIDLE = 0x10

        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        
        if not self.i2p_tunneled:
            self.socket.setsockopt(socket.IPPROTO_TCP, TCP_KEEPIDLE, int(I2PInterfacePeer.TCP_PROBE_AFTER))
        else:
            self.socket.setsockopt(socket.IPPROTO_TCP, TCP_KEEPIDLE, int(I2PInterfacePeer.I2P_PROBE_AFTER))
        
    def detach(self):
        if self.socket != None:
            if hasattr(self.socket, "close"):
                if callable(self.socket.close):
                    RNS.log("Detaching "+str(self), RNS.LOG_DEBUG)
                    self.detached = True
                    
                    try:
                        self.socket.shutdown(socket.SHUT_RDWR)
                    except Exception as e:
                        RNS.log("Error while shutting down socket for "+str(self)+": "+str(e))

                    try:
                        self.socket.close()
                    except Exception as e:
                        RNS.log("Error while closing socket for "+str(self)+": "+str(e))

                    self.socket = None

    def connect(self, initial=False):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.target_ip, self.target_port))
            self.online  = True
        
        except Exception as e:
            if initial:
                RNS.log("Initial connection for "+str(self)+" could not be established: "+str(e), RNS.LOG_ERROR)
                RNS.log("Leaving unconnected and retrying connection in "+str(I2PInterfacePeer.RECONNECT_WAIT)+" seconds.", RNS.LOG_ERROR)
                return False
            
            else:
                raise e

        if platform.system() == "Linux":
            self.set_timeouts_linux()
        elif platform.system() == "Darwin":
            self.set_timeouts_osx()
        
        self.online  = True
        self.writing = False
        self.never_connected = False

        return True


    def reconnect(self):
        if self.initiator:
            if not self.reconnecting:
                self.reconnecting = True
                attempts = 0
                while not self.online:
                    time.sleep(I2PInterfacePeer.RECONNECT_WAIT)
                    attempts += 1

                    if self.max_reconnect_tries != None and attempts > self.max_reconnect_tries:
                        RNS.log("Max reconnection attempts reached for "+str(self), RNS.LOG_ERROR)
                        self.teardown()
                        break

                    try:
                        self.connect()

                    except Exception as e:
                        RNS.log("Connection attempt for "+str(self)+" failed: "+str(e), RNS.LOG_DEBUG)

                if not self.never_connected:
                    RNS.log("Reconnected TCP socket for "+str(self)+".", RNS.LOG_INFO)

                self.reconnecting = False
                thread = threading.Thread(target=self.read_loop)
                thread.setDaemon(True)
                thread.start()
                if not self.kiss_framing:
                    RNS.Transport.synthesize_tunnel(self)

        else:
            RNS.log("Attempt to reconnect on a non-initiator TCP interface. This should not happen.", RNS.LOG_ERROR)
            raise IOError("Attempt to reconnect on a non-initiator TCP interface")

    def processIncoming(self, data):
        self.rxb += len(data)
        if hasattr(self, "parent_interface") and self.parent_interface != None:
            self.parent_interface.rxb += len(data)
                    
        self.owner.inbound(data, self)

    def processOutgoing(self, data):
        if self.online:
            while self.writing:
                time.sleep(0.01)

            try:
                self.writing = True

                if self.kiss_framing:
                    data = bytes([KISS.FEND])+bytes([KISS.CMD_DATA])+KISS.escape(data)+bytes([KISS.FEND])
                else:
                    data = bytes([HDLC.FLAG])+HDLC.escape(data)+bytes([HDLC.FLAG])

                self.socket.sendall(data)
                self.writing = False
                self.txb += len(data)
                if hasattr(self, "parent_interface") and self.parent_interface != None:
                    self.parent_interface.txb += len(data)

            except Exception as e:
                RNS.log("Exception occurred while transmitting via "+str(self)+", tearing down interface", RNS.LOG_ERROR)
                RNS.log("The contained exception was: "+str(e), RNS.LOG_ERROR)
                self.teardown()


    def read_loop(self):
        try:
            in_frame = False
            escape = False
            data_buffer = b""
            command = KISS.CMD_UNKNOWN

            while True:
                data_in = self.socket.recv(4096)
                if len(data_in) > 0:
                    # TODO: Remove
                    RNS.log("Read "+str(len(data_in)))
                    pointer = 0
                    while pointer < len(data_in):
                        byte = data_in[pointer]
                        pointer += 1

                        if self.kiss_framing:
                            # Read loop for KISS framing
                            if (in_frame and byte == KISS.FEND and command == KISS.CMD_DATA):
                                in_frame = False
                                self.processIncoming(data_buffer)
                            elif (byte == KISS.FEND):
                                in_frame = True
                                command = KISS.CMD_UNKNOWN
                                data_buffer = b""
                            elif (in_frame and len(data_buffer) < RNS.Reticulum.MTU):
                                if (len(data_buffer) == 0 and command == KISS.CMD_UNKNOWN):
                                    # We only support one HDLC port for now, so
                                    # strip off the port nibble
                                    byte = byte & 0x0F
                                    command = byte
                                elif (command == KISS.CMD_DATA):
                                    if (byte == KISS.FESC):
                                        escape = True
                                    else:
                                        if (escape):
                                            if (byte == KISS.TFEND):
                                                byte = KISS.FEND
                                            if (byte == KISS.TFESC):
                                                byte = KISS.FESC
                                            escape = False
                                        data_buffer = data_buffer+bytes([byte])

                        else:
                            # Read loop for HDLC framing
                            if (in_frame and byte == HDLC.FLAG):
                                in_frame = False
                                self.processIncoming(data_buffer)
                            elif (byte == HDLC.FLAG):
                                in_frame = True
                                data_buffer = b""
                            elif (in_frame and len(data_buffer) < RNS.Reticulum.MTU):
                                if (byte == HDLC.ESC):
                                    escape = True
                                else:
                                    if (escape):
                                        if (byte == HDLC.FLAG ^ HDLC.ESC_MASK):
                                            byte = HDLC.FLAG
                                        if (byte == HDLC.ESC  ^ HDLC.ESC_MASK):
                                            byte = HDLC.ESC
                                        escape = False
                                    data_buffer = data_buffer+bytes([byte])
                else:
                    self.online = False
                    if self.initiator and not self.detached:
                        RNS.log("TCP socket for "+str(self)+" was closed, attempting to reconnect...", RNS.LOG_WARNING)
                        self.reconnect()
                    else:
                        RNS.log("TCP socket for remote client "+str(self)+" was closed.", RNS.LOG_VERBOSE)
                        self.teardown()

                    break

                
        except Exception as e:
            self.online = False
            RNS.log("An interface error occurred for "+str(self)+", the contained exception was: "+str(e), RNS.LOG_WARNING)

            if self.initiator:
                RNS.log("Attempting to reconnect...", RNS.LOG_WARNING)
                self.reconnect()
            else:
                self.teardown()

    def teardown(self):
        if self.initiator and not self.detached:
            RNS.log("The interface "+str(self)+" experienced an unrecoverable error and is being torn down. Restart Reticulum to attempt to open this interface again.", RNS.LOG_ERROR)
            if RNS.Reticulum.panic_on_interface_error:
                RNS.panic()

        else:
            RNS.log("The interface "+str(self)+" is being torn down.", RNS.LOG_VERBOSE)

        self.online = False
        self.OUT = False
        self.IN = False

        if hasattr(self, "parent_interface") and self.parent_interface != None:
            self.parent_interface.clients -= 1

        if self in RNS.Transport.interfaces:
            if not self.initiator:
                RNS.Transport.interfaces.remove(self)


    def __str__(self):
        return "I2PInterfacePeer["+str(self.name)+"]"


class I2PInterface(Interface):

    def __init__(self, owner, name, rns_storagepath, peers):
        self.rxb = 0
        self.txb = 0
        self.online = False
        self.clients = 0
        self.owner = owner
        self.i2p_tunneled = True

        self.i2p = I2PController(rns_storagepath)
        
        self.IN  = True
        self.OUT = False
        self.name = name


        self.receives = True
        self.bind_ip     = "127.0.0.1"
        self.bind_port   = self.i2p.get_free_port()
        self.address = (self.bind_ip, self.bind_port)

        i2p_thread = threading.Thread(target=self.i2p.start)
        i2p_thread.setDaemon(True)
        i2p_thread.start()

        def handlerFactory(callback):
            def createHandler(*args, **keys):
                return I2PInterfaceHandler(callback, *args, **keys)
            return createHandler
        
        ThreadingI2PServer.allow_reuse_address = True
        self.server = ThreadingI2PServer(self.address, handlerFactory(self.incoming_connection))

        thread = threading.Thread(target=self.server.serve_forever)
        thread.setDaemon(True)
        thread.start()

        # TODO: Remove
        RNS.log("Started TCP server for I2P on "+str(self.address)+" "+str(self.server))

        self.i2p.server_tunnel(self)

        if peers != None:
            for peer_addr in peers:
                interface_name = peer_addr
                peer_interface = I2PInterfacePeer(self, interface_name, peer_addr)
                RNS.Transport.interfaces.append(peer_interface)

        self.online = True


    def incoming_connection(self, handler):
        RNS.log("Accepting incoming I2P connection", RNS.LOG_VERBOSE)
        interface_name = "Connected peer on "+self.name
        spawned_interface = I2PInterfacePeer(self.owner, interface_name, connected_socket=handler.request)
        spawned_interface.OUT = self.OUT
        spawned_interface.IN  = self.IN
        spawned_interface.parent_interface = self
        spawned_interface.online = True
        RNS.log("Spawned new I2PInterface Peer: "+str(spawned_interface), RNS.LOG_VERBOSE)
        RNS.Transport.interfaces.append(spawned_interface)
        self.clients += 1
        spawned_interface.read_loop()

    def processOutgoing(self, data):
        pass

    def detach(self):
        self.i2p.stop()

    def __str__(self):
        return "I2PInterface["+self.name+"]"

class I2PInterfaceHandler(socketserver.BaseRequestHandler):
    def __init__(self, callback, *args, **keys):
        self.callback = callback
        socketserver.BaseRequestHandler.__init__(self, *args, **keys)

    def handle(self):
        self.callback(handler=self)
