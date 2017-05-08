from __future__ import absolute_import, division, print_function, unicode_literals

import atexit
import json
import stat
import os
import argparse
import select
import logging
import errno
import threading
from importlib import import_module
import itertools
import keen
import time

import signal
from future import standard_library
# noinspection PyUnresolvedReferences
from future.builtins import *
standard_library.install_aliases()

DEFAULT_INPUT_FILENAME = '/var/run/jumper_logging_agent'
DEFAULT_FLUSH_THRESHOLD = 100
DEFAULT_FLUSH_PRIORITY = 2
DEFAULT_FLUSH_INTERVAL = 1.0
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


class RecurringTimer(threading.Thread):
    def __init__(self, interval, target, *args, **kwargs):
        self.stop_event = threading.Event()

        def wrapped():
            while not self.stop_event.is_set():
                try:
                    target()
                except Exception as e:
                    log.warn('Caught exception in timer: %s', e)
                self.stop_event.wait(interval)

        super(RecurringTimer, self).__init__(target=wrapped, *args, **kwargs)

    def cancel(self):
        self.stop_event.set()


class Agent(object):
    def __init__(
            self, input_filename, flush_priority=DEFAULT_FLUSH_PRIORITY, flush_threshold=DEFAULT_FLUSH_THRESHOLD,
            flush_interval=DEFAULT_FLUSH_INTERVAL, event_store=None, default_event_type=DEFAULT_EVENT_TYPE,
            on_listening=None,
    ):
        self.input_filename = input_filename
        self.flush_priority = flush_priority
        self.flush_threshold = flush_threshold
        self.flush_interval = flush_interval
        self.event_count = 0
        self.pending_events = []
        self.event_store = event_store or keen
        self.default_event_type = default_event_type
        self.on_listening = on_listening

    def start(self):
        flush_timer = RecurringTimer(self.flush_interval, self.flush)
        flush_timer.start()
        input_file = None
        control_file = None
        should_stop = False

        def readline_with_retry(data):
            try:
                return data.readline()
            except IOError as line_read_exception:
                if line_read_exception.errno in (0, errno.EWOULDBLOCK):
                    time.sleep(0.01)
                else:
                    raise

        def on_data_available(data):
            should_flush = False

            while True:
                line = readline_with_retry(data)
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError as e:
                    log.warn('Invalid JSON: %s\n%s', line, e)
                    return None

                log.debug('Pending event: %s', repr(event))
                self.pending_events.append(event)
                self.event_count += 1
                should_flush = should_flush or len(self.pending_events) >= self.flush_threshold or \
                    event.get('priority') >= self.flush_priority

            if should_flush:
                log.debug('calling flush explicitly')
                self.flush()

        while not should_stop:
            try:
                input_file = open_fifo_read(self.input_filename)
                control_file = open_fifo_read(self.control_filename)

                if self.on_listening:
                    self.on_listening()

                while True:
                    select_result, _, _, = select.select((input_file, control_file), (), ())
                    on_data_available(input_file)

                    if control_file in select_result:
                        should_stop = True
                        break  # self.control_file has input - stop

            except select.error as e:
                if e.args[0] == errno.EINTR:
                    break
            except IOError as e:
                log.warn('got exception', exc_info=True)
                if e.errno not in (errno.EAGAIN, errno.EPIPE):
                    raise

            finally:
                flush_timer.cancel()
                flush_timer.join()
                if input_file:
                    input_file.close()
                if control_file:
                    control_file.close()
                self.cleanup()
                print('Agent stopped')

    def flush(self):
        events = self.pending_events
        self.pending_events = []

        if events:
            self.write_events(events)

    def key(self, event):
        return event.get('type', self.default_event_type)

    def write_events(self, events):
        grouped = itertools.groupby(sorted(events, key=self.key), self.key)
        event_dict = {k: list(v) for k, v in grouped}
        self.event_store.add_events(event_dict)

    @property
    def control_filename(self):
        return agent_control_filename(self.input_filename)

    def stop(self):
        stop_agent(self.input_filename)

    def cleanup(self):
        try:
            os.remove(self.control_filename)
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def agent_control_filename(agent_input_filename):
    return agent_input_filename + '.control'


def stop_agent(agent_input_filename):
    with open(agent_control_filename(agent_input_filename), b'wb') as f:
        f.write(b'stop')


def extract_class(s):
    module_name, class_name = s.rsplit('.', 1)
    mod = import_module(module_name)
    return getattr(mod, class_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', help='Named pipe to read from', type=str, default=DEFAULT_INPUT_FILENAME)
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

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format='%(asctime)s %(levelname)8s %(name)10s: %(message)s', level=log_level)

    print('Starting agent')

    def on_listening():
        print('Agent listening on named pipe %s' % (agent.input_filename,))

    agent = Agent(
        input_filename=args.input,
        flush_priority=args.flush_priority,
        flush_threshold=args.flush_threshold,
        flush_interval=args.flush_interval,
        default_event_type=args.default_event_type,
        event_store=event_store,
        on_listening=on_listening,
    )

    signal.signal(signal.SIGTERM, lambda *a: agent.stop())
    signal.signal(signal.SIGINT, lambda *a: agent.stop())

    atexit.register(agent.cleanup)

    agent.start()
    agent.cleanup()
    return 0



