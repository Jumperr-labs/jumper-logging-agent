from __future__ import absolute_import, division, print_function, unicode_literals

import json
import pprint
import socket
import stat
import os
import argparse
import select
import atexit
import logging
import errno
import threading
from importlib import import_module
import itertools
import keen
import time

import queue
from future import standard_library
# noinspection PyUnresolvedReferences
from future.builtins import *
standard_library.install_aliases()

DEFAULT_PORT = 5009
DEFAULT_FLUSH_THRESHOLD = 100
DEFAULT_FLUSH_PRIORITY = 2
DEFAULT_FLUSH_INTERVAL = 15.0
DEFAULT_EVENT_TYPE = 'default'


def is_fifo(filename):
    return stat.S_ISFIFO(os.stat(filename).st_mode)


def open_fifo_read(filename):
    try:
        os.mkfifo(filename)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    if not is_fifo(filename):
        raise ValueError('file "%s" is not a named pipe' % (filename,))

    fd = os.open(filename, os.O_RDONLY | os.O_NONBLOCK)
    return os.fdopen(fd, 'rb')

log = logging.getLogger('jumper.LoggingAgent')


class Timer(threading.Thread):
    def __init__(self, interval, target, *args, **kwargs):
        self.stop_event = threading.Event()

        def wrapped():
            while not self.stop_event.is_set():
                try:
                    target()
                except Exception as e:
                    log.warn('Caught exception in timer: %s', e)
                self.stop_event.wait(interval)

        super(Timer, self).__init__(target=wrapped, *args, **kwargs)

    def cancel(self):
        self.stop_event.set()


class Agent(object):
    def __init__(
            self, port=0, flush_priority=DEFAULT_FLUSH_PRIORITY, flush_threshold=DEFAULT_FLUSH_THRESHOLD,
            flush_interval=DEFAULT_FLUSH_INTERVAL, event_store=None, default_event_type=DEFAULT_EVENT_TYPE,
            on_listening=None,
    ):
        self.port = port
        self.flush_priority = flush_priority
        self.flush_threshold = flush_threshold
        self.flush_interval = flush_interval
        self.event_count = 0
        self.pending_events = []
        self.event_store = event_store or keen
        self.default_event_type = default_event_type
        self.flush_timer_lock = threading.Lock()
        self.flush_timer = None
        self.on_listening = on_listening

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.flush_timer = Timer(self.flush_interval, self.flush)
        self.flush_timer.start()

        q = queue.Queue()

        def worker():
            while True:
                received = q.get()

                if received == b'stop':
                    break

                try:
                    received = received.decode()
                    event = json.loads(received)
                except ValueError as e:
                    # log.warn('Invalid JSON: %s\n%s', received, e)
                    print('Invalid JSON: %s\n%s', received, e)
                    continue

                self.event_count += 1

                self.pending_events.append(event)
                # print('appended event, count=%s' % (self.event_count,))
                should_flush = len(self.pending_events) >= self.flush_threshold or \
                    event.get('priority') >= self.flush_priority

                if should_flush:
                    print('calling explicit flush')
                    self.flush()

        t = threading.Thread(target=worker)
        t.start()

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(('', self.port))
            self.port = sock.getsockname()[1]
            if self.on_listening:
                self.on_listening()

            while True:
                packet = sock.recv(65535)
                if not packet:
                    continue
                q.put(packet)
                if packet == b'stop':
                    break

        finally:
            self.flush_timer.cancel()
            self.flush_timer.join()
            sock.close()
            t.join()

    def flush(self):
        # print('flush entering')
        events = self.pending_events
        self.pending_events = []

        if events:
            # print('flush writing events: %s', events)
            self.write_events(events)

        # print('flush exiting')

    def key(self, event):
        return event.get('type', self.default_event_type)

    def write_events(self, events):
        grouped = itertools.groupby(sorted(events, key=self.key), self.key)
        event_dict = {k: list(v) for k, v in grouped}
        self.event_store.add_events(event_dict)

    def stop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(b'stop', ('127.0.0.1', self.port))

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def extract_class(s):
    module_name, class_name = s.rsplit('.', 1)
    mod = import_module(module_name)
    return getattr(mod, class_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', help='UDP port to read from', type=int, default=DEFAULT_PORT)
    parser.add_argument(
        '--flush-threshold', help='Number of events buffered until flushing', type=int, default=DEFAULT_FLUSH_THRESHOLD
    )
    parser.add_argument(
        '--flush-priority', help='Event priority (integer) upon which to flush pending events', type=int,
        default=DEFAULT_FLUSH_PRIORITY
    )
    parser.add_argument(
        '--flush-interval', help='Interval in seconds after which pending events will be flushed', type=float,
        default=DEFAULT_FLUSH_INTERVAL
    )
    parser.add_argument(
        '--default-event-type', help='Default event type if not specified in the event itself', type=str,
        default=DEFAULT_EVENT_TYPE
    )
    parser.add_argument('--event-store', help='Module to use as event store', type=str, default=None)
    parser.add_argument('-v', '--verbose', help='Print logs', action='store_true')
    args = parser.parse_args()

    event_store = None
    if args.event_store:
        try:
            event_store_class = extract_class(args.event_store)
            event_store = event_store_class()
        except Exception as e:
            print('Could not load or instantiate event store %s: %s' % (args.event_store, e))
            return 2

    if args.verbose:
        logging.basicConfig(format='%(name)s: %(message)s', level=logging.DEBUG)

    print('Starting agent')

    def on_listening():
        print('Agent listening on port %s' % (agent.port,))

    agent = Agent(
        port=args.port,
        flush_priority=args.flush_priority,
        flush_threshold=args.flush_threshold,
        flush_interval=args.flush_interval,
        default_event_type=args.default_event_type,
        event_store=event_store,
        on_listening=on_listening,
    )
    # atexit.register(lambda: agent.cleanup)
    agent.start()
    return 0



