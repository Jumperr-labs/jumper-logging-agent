from __future__ import absolute_import, division, print_function, unicode_literals

import json
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
from future import standard_library
# noinspection PyUnresolvedReferences
from future.builtins import *
standard_library.install_aliases()

DEFAULT_INPUT_FILENAME = '/var/run/jumper_logging_agent'
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


class Agent(object):
    def __init__(
            self, input_filename, flush_priority=DEFAULT_FLUSH_PRIORITY, flush_threshold=DEFAULT_FLUSH_THRESHOLD,
            flush_interval=DEFAULT_FLUSH_INTERVAL, event_store=None, default_event_type=DEFAULT_EVENT_TYPE
    ):
        self.input_file = open_fifo_read(input_filename)
        self.flush_priority = flush_priority
        self.flush_threshold = flush_threshold
        self.flush_interval = flush_interval
        self.control_filename = input_filename + '.control'
        self.control_file = open_fifo_read(self.control_filename)
        self.pending_events = []
        self.event_store = event_store or keen
        self.default_event_type = default_event_type
        self.flush_lock = threading.Lock()
        self.stop_event = threading.Event()

    def start(self):
        flush_timer = threading.Timer(self.flush_interval, self.flush)
        flush_timer.start()

        try:
            while True:
                select_result, _, _, = select.select((self.input_file, self.control_file), (), ())
                if self.input_file not in select_result:
                    break  # self.control_file has input - stop

                should_flush = False
                read_events = []
                lines = self.input_file.read()
                log.debug('read from input: %s', lines)
                for line in lines.split('\n'):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as e:
                        log.warn('Invalid JSON: %s\n%s', line, e)
                    else:
                        read_events.append(event)
                        should_flush = should_flush or event.get('priority') >= self.flush_priority

                with self.flush_lock:
                    self.pending_events.extend(read_events)

                should_flush = should_flush or len(self.pending_events) >= self.flush_threshold

                if should_flush:
                    self.flush()

        finally:
            flush_timer.cancel()
            self.cleanup()

    def flush(self):
        with self.flush_lock:
            events = self.pending_events
            self.pending_events = []

        self.write_events(events)

    def key(self, event):
        return event.get('type', self.default_event_type)

    def write_events(self, events):
        grouped = itertools.groupby(sorted(events, key=self.key), self.key)
        event_dict = {k: list(v) for k, v in grouped}
        self.event_store.add_events(event_dict)

    def stop(self):
        with open(self.control_filename, b'wb') as f:
            f.write(b'stop')

    def cleanup(self):
        log.info('cleaning up')
        if self.input_file:
            log.debug('closing input file')
            self.input_file.close()
            self.input_file = None

        if self.control_file:
            log.debug('closing control file')
            self.control_file.close()
            self.control_file = None

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
    parser.add_argument('--input', help='Name of named pipe to read from', type=str, default=DEFAULT_INPUT_FILENAME)
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
    args = parser.parse_args()

    event_store = None
    if args.event_store:
        try:
            event_store_class = extract_class(args.event_store)
            event_store = event_store_class()
        except Exception as e:
            print('Could not load or instantiate event store %s: %s' % (args.event_store, e))
            return 2

    agent = Agent(
        args.input,
        flush_priority=args.flush_priority,
        flush_threshold=args.flush_threshold,
        flush_interval=args.flush_interval,
        default_event_type=args.default_event_type,
        event_store=event_store,
    )
    atexit.register(lambda: agent.cleanup)
    agent.start()
    return 0



