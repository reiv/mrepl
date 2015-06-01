import asyncio
import struct
import os
import tempfile
import subprocess
import uuid
import enum
import sys
import code
import queue
import re
import threading
import types

# Change this to your favorite editor!
EDITOR = 'geany'

# Monkeypatch input() and print() to play nice with each other when called
# from separate threads.
from termtest2 import *

# webbrowser module hack
import webbrowser

open_browser = webbrowser.open

def _open(url, *args, **kwargs):
    for _, proto in world.connections.items():
        proto.send_packet(PacketType.OPEN_BROWSER_URL, url)

webbrowser.open = _open

re_module_statement = re.compile('module ([a-zA-Z][0-9a-zA-Z]*)$')

PYTHON_VERSION_STRING = ("Python %s on %s" %
    (sys.version, sys.platform))

get_inputs = queue.Queue()

loop = None
world = None

stupid_lock = threading.Lock()
_shutdown = False
current_user = None

@enum.unique
class PacketType(enum.Enum):
    TEXT = 0
    FILE = 1
    ASSIGN_ID = 2
    GET_INPUT = 3
    GET_INPUT_MORE = 4
    SEND_INPUT = 5
    OPEN_BROWSER_URL = 6

class LocalsDict(dict):
    def __getitem__(self, key):
        if key == 'me':
            return current_user
        else:
            return super().__getitem__(key)

class World(object):
    def __init__(self):
        self.namespaces = {}
        self.users = {}
        self.shared_namespace = LocalsDict()
        #self.shared_namespace = {}
        self.shared_namespace['world'] = self

def newworld():
    world = types.ModuleType('world', 'The world')
    world.modules = types.ModuleType('modules', 'User-defined modules')
    world.modules.__sources__ = {}
    sys.modules['world'] = world
    sys.modules['world.modules'] = world.modules
    world.shared_namespace = LocalsDict()
    world.shared_namespace['world'] = world
    world.users = {}
    world.connections = {}
    return world


class User(object):
    def __init__(self, name, proto):
        self.protocol = proto
        self._name = name
    
    @property
    def name(self): return self._name

    def send(self, s):
        self.protocol.send_packet(PacketType.TEXT, str(s))

    def __repr__(self):
        return '<User %r>' % self.name


class Interpreter(code.InteractiveConsole):
    def __init__(self, writefunc, locals=None, filename='<console>'):
        self.write = writefunc
        super().__init__(locals, filename)
    
    def runsource(self, source, filename='<input>', symbol='single',
        *args, **kwargs):
        try:
            code = self.compile(source, filename, symbol)
        except (OverflowError, SyntaxError, ValueError):
            # Case 1
            self.showsyntaxerror(filename)
            return False

        if code is None:
            # Case 2
            return True

        # Case 3
        self.runcode(code, *args, **kwargs)
        return False

    runsource.__doc__ = code.InteractiveConsole.runsource.__doc__

    def runcode(self, code, *args, **kwargs):
        from io import StringIO
        
        old_stdout = sys.stdout
        redirected_output = sys.stdout = StringIO()

        try:
            if args or kwargs:
                exec(code, *args, **kwargs)
            else:
                exec(code, self.locals)
        except SystemExit:
            raise
        except:
            self.showtraceback()
        else:
            val = redirected_output.getvalue()
            if val:
                self.write(val.rstrip('\r\n'))
        finally:
            sys.stdout = old_stdout

    runcode.__doc__ = code.InteractiveConsole.runcode.__doc__

@asyncio.coroutine
def wait_for_edit(filename):
    """
    Wait until the file is changed.
    """
    initial_mtime = os.stat(filename).st_mtime
    while True:
        yield from asyncio.sleep(2)
        try:
            mtime = os.stat(filename).st_mtime
        except OSError:
            # File doesn't exist (anymore).
            return False
        if mtime != initial_mtime:
            # File was modified.
            return True


class PacketProtocol(asyncio.Protocol):
    
    def connection_made(self, transport):
        self.transport = transport
        self.connected = True

    def data_received(self, data):
        try:
            self.__buffer += data
        except AttributeError:
            self.__buffer = data
        
        while self.__buffer:
            try:
                state, bytes_to_read = self.__state
            except AttributeError:
                state, bytes_to_read = self.__state = ('length', 4)

            buflen = len(self.__buffer)


            if buflen >= bytes_to_read:
                data, self.__buffer = (
                    self.__buffer[:bytes_to_read],
                    self.__buffer[bytes_to_read:])
                if state == 'length':
                    self.__state = 'packet', struct.unpack('!L', data)[0]
                elif state == 'packet':
                    self.__state = 'length', 4
                    packet_type, data = PacketType(
                        struct.unpack('!H', data[:2])[0]), data[2:]
                    self.packet_received(packet_type, data)
            else:
                break
    
    def packet_received(self, data):
        pass

    def send_packet(self, packet_type, data):
        
        try:
            packet_type = packet_type.value
        except AttributeError:
            pass

        if not isinstance(data, bytes):
            data = data.encode('utf-8')
            
        transport = self.transport
        length = len(data) + 2
        lbytes = struct.pack('!L', length)
        transport.write(lbytes)
        transport.write(struct.pack('!H', packet_type))
        transport.write(data)


class ClientProtocol(PacketProtocol):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._open_files = set()

    def packet_received(self, packet_type, data):
        if packet_type is PacketType.FILE:
            text = data.decode('utf-8')
            filename, contents = text.split(' ', 1)
            asyncio.async(self.handle_file(filename, contents))
        elif packet_type is PacketType.TEXT:
            text = data.decode('utf-8')
            print(text)
        elif packet_type is PacketType.GET_INPUT:
            prompt = data.decode('utf-8')
            asyncio.async(self.handle_get_input(prompt))
        elif packet_type is PacketType.OPEN_BROWSER_URL:
            open_browser(data.decode('utf-8'))

    @asyncio.coroutine
    def handle_file(self, filename, contents):
        from concurrent.futures import FIRST_COMPLETED
        
        if filename in self._open_files:
            print('Already editing file "%s"' % filename)
            return
        else:
            self._open_files.add(filename)

        tempdir = tempfile.gettempdir()
        filepath = os.path.join(tempdir, 'temp_' + filename)

        # Write contents to temporary file.
        with open(filepath, 'wb') as outfile:
            outfile.write(contents.encode('utf-8'))

        # Open it in external editor.
        # subprocess.call((EDITOR, filepath))
        process = yield from asyncio.create_subprocess_exec(EDITOR, filepath)

        #terminated = asyncio.Task(process.wait())

        while True:
            edited = asyncio.Task(wait_for_edit(filepath))
            exists = yield from edited
            if not exists:
                break
            else:
                # Read changed contents.
                with open(filepath, 'rb') as infile:
                    contents = infile.read()
                
                # Send contents to server.
                self.send_packet(PacketType.FILE, filename.encode('utf-8') +
                    b' ' + contents)

        print('Stopped editing file "%s"' % filename)
        self._open_files.remove(filename)
        # Delete the temporary file.
        #os.remove(filepath)

    def connection_made(self, transport):
        super().connection_made(transport)
        self.connected = True
    
    def connection_lost(self, exc):
        super().connection_lost(exc)
        print('Connection lost.')
        from signal import SIGINT
        sys.stdout.write('\n')
        os.kill(os.getpid(), SIGINT)
        sys.exit()

    @asyncio.coroutine
    def handle_get_input(self, prompt):
        fut = asyncio.Future()
        get_inputs.put((prompt, fut))
        #s = yield from loop.run_in_executor(None, input, prompt)
        s = yield from fut
        self.send_packet(PacketType.SEND_INPUT, s.encode('utf-8'))


class ServerProtocol(PacketProtocol):

    def __init__(self, world):
        self.world = world
    
    def packet_received(self, packet_type, data):
        if packet_type is PacketType.TEXT:
            text = data.decode('utf-8')
            self.interpreter.runsource(text, symbol='single')

        elif packet_type is PacketType.FILE:
            text = data.decode('utf-8')
            filename, contents = text.split(' ', 1)
            
            if filename.lower().endswith('.py'):
                module_name = filename[:-3]
                # compile it
                module_key = 'world.modules.%s' % module_name
                new = False
                try:
                    # module exists?
                    module = sys.modules[module_key]
                except KeyError:
                    new = True
                    # nope. create it.
                    module = sys.modules[module_key] = types.ModuleType(module_name, '')
                # update the module.
                if not new:
                    self.broadcast('Reloading module %s' % module_key)
                # XXX: Should we wipe the module.__dict__ before reloading?
                self.interpreter.runsource(contents, module_name + '.py', 'exec', module.__dict__)
                if new:
                    self.broadcast("Module %s created." % module_key)
                    setattr(world.modules, module_name, module)
                # save the source code.
                self.world.modules.__sources__[module_name] = contents

        elif packet_type is PacketType.SEND_INPUT:
            text = data.decode('utf-8')
            if self.waiter:
                self.waiter.set_result(text)
                self.waiter = None
            else:
                # ignore it I guess.
                pass

    @asyncio.coroutine
    def get_input(self, prompt):
        self.waiter = asyncio.Future()
        self.send_packet(PacketType.GET_INPUT, prompt)
        response = yield from self.waiter
        return response

    def connection_made(self, transport):
        super().connection_made(transport)
        self.waiter = None
        self.name = None#'anon'
        self.uuid = uuid.uuid4()
        #self.namespace = self.world.namespaces[self.uuid] = {}
        self.namespace = self.world.shared_namespace
        self.world.connections[self.uuid] = self
        self.interpreter = Interpreter(
            lambda s: self.broadcast(s), self.namespace)
        asyncio.async(self.do_login())

    def connection_lost(self, exc):
        super().connection_lost(exc)
        try:
            del self.world.connections[self.uuid]
            del self.world.users[self.name]
        except KeyError: pass
        self.broadcast('%s disconnected.' % self.name, exclude={self.uuid})
        del self.world

    def broadcast(self, text, exclude=frozenset()):
        for uuid, proto in self.world.connections.items():
            if uuid in exclude: continue
            proto.send_packet(PacketType.TEXT, text)
    
    @asyncio.coroutine
    def do_login(self):
        self.send_packet(PacketType.TEXT, PYTHON_VERSION_STRING)
        while True:
            name = yield from self.get_input('What is your name?  ')
            name = name.strip().replace(' ', '_').lower()
            if name in world.users or name == 'me':
                self.send_packet(PacketType.TEXT, 'That name is unavailable.')
            else:
                break
        self.name = name
        self.user = world.users[name] = User(name, self)
        self.send_packet(PacketType.TEXT, 'Welcome, %s.' % name)
        self.broadcast('%s connected.' % self.name, exclude={self.uuid})
        more = False
        while self.connected:
            prompt = '...' if more else '>>>'
            s = yield from self.get_input('%s %s ' % (name, prompt))
            self.broadcast('%s %s %s' % (self.name, prompt, s),
                exclude={self.uuid})
            global current_user
            current_user = self.user

            # `module` statement
            match = re_module_statement.match(s)
            if match:
                module_name = match.group(1)
                filename = module_name + '.py'
                if module_name not in world.modules.__sources__:
                    self.send_packet(PacketType.FILE, '{0} # {0}'.format(filename))
                else:
                    self.send_packet(PacketType.FILE, '{0} {1}'.format(filename, world.modules.__sources__[module_name]))
                continue
            
            more = self.interpreter.push(s)


def run(host, port, server=False):
    global loop, world
    #world = World()
    world = newworld()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if server:
        coro = loop.create_server(lambda: ServerProtocol(world), host, port)
    else:
        coro = loop.create_connection(ClientProtocol, host, port)

    try:
        conn = loop.run_until_complete(coro)
    except OSError as e:
        print(e)
        global _shutdown
        _shutdown = True
        return
    finally:
        stupid_lock.release()

    if server:
        print('Server running on %s:%d [PID: %d]' % (
            conn.sockets[0].getsockname() + (os.getpid(),)))
    else:
        conn = conn[0]
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    conn.close()
    try:
        loop.run_until_complete(conn.wait_closed())
    except AttributeError:
        pass
    loop.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        prog='mrepl',
        description='Multiplayer Python REPL')
    parser.add_argument('host', nargs='?', default='', type=str, help='host name / ip address')
    parser.add_argument('port', type=int, help='port number')
    parser.add_argument('--server', dest='server', action='store_const',
        const=True, default=False, help='run as server')

    args = parser.parse_args()
    run_server = args.server

    stupid_lock.acquire()

    if args.server:
        if args.host not in ('', '0.0.0.0'):
            print('Warning: binding to host other than INADDR_ANY -- '
                  'nonlocal connections will be refused!')
        run(args.host, args.port, True)
    else:
        if not args.host:
            args.host = 'localhost'
    
        import threading

        threading.Thread(target=lambda: run(args.host, args.port, args.server)).start()

        stupid_lock.acquire()
        stupid_lock.release()
        while not _shutdown:
            prompt, fut = get_inputs.get()
            try:
                result = input(prompt)
            except KeyboardInterrupt:
                break
            else:
                loop.call_soon_threadsafe(fut.set_result, result)
        

#set upnpc="upnpc -u http://192.168.2.1:80/igd.xml"
