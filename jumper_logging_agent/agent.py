from __future__ import absolute_import, division, print_function, unicode_literals

import atexit
import json

import datetime
import stat
import os
import argparse
import select
import logging
import errno
import threading
from importlib import import_module
import time

import signal
from future import standard_library
# noinspection PyUnresolvedReferences
from future.builtins import *
import requests

standard_library.install_aliases()

DEFAULT_INPUT_FILENAME = '/var/run/jumper_logging_agent/events'
DEFAULT_FLUSH_THRESHOLD = 100
DEFAULT_FLUSH_PRIORITY = 2
DEFAULT_FLUSH_INTERVAL = 5.0
DEFAULT_EVENT_TYPE = 'default'


def is_fifo(filename):
    return stat.S_ISFIFO(os.stat(filename).st_mode)


def open_fifo_read(filename):
    if not os.path.exists(filename):
        dirname = os.path.dirname(filename)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
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
                    log.warn('Caught exception in timer: %s', e, exc_info=True)
                self.stop_event.wait(interval)

        super(RecurringTimer, self).__init__(target=wrapped, *args, **kwargs)

    def cancel(self):
        self.stop_event.set()


class DefaultEventStore(object):
    BASE_URL = 'https://eventsapi.jumper.io/1.0'
    BASE_URL_DEV = 'https://eventsapi-dev.jumper.io/1.0'

    def __init__(self, project_id, write_key, dev_mode=False):
        base_url = self.BASE_URL_DEV if dev_mode else self.BASE_URL
        self.url = '%s/projects/%s/events' % (base_url, project_id)
        self.headers = {
            'Content-Type': 'application/json',
            'Authorization': write_key,
        }

    def add_events(self, events):
        response = requests.post(self.url, headers=self.headers, json=events)
        response.raise_for_status()


class Agent(object):
    EVENT_TYPE_PROPERTY = 'type'

    def __init__(
            self, input_filename, project_id, write_key, flush_priority=DEFAULT_FLUSH_PRIORITY, flush_threshold=DEFAULT_FLUSH_THRESHOLD,
            flush_interval=DEFAULT_FLUSH_INTERVAL, event_store=None, default_event_type=DEFAULT_EVENT_TYPE,
            on_listening=None, dev_mode=False
    ):
        self.input_filename = input_filename
        self.flush_priority = flush_priority
        self.flush_threshold = flush_threshold
        self.flush_interval = flush_interval
        self.event_count = 0
        self.pending_events = []
        self.event_store = event_store or DefaultEventStore(project_id, write_key, dev_mode=dev_mode)
        self.default_event_type = default_event_type
        self.on_listening = on_listening
        self.project_id = project_id

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

        while not should_stop:
            try:
                input_file = open_fifo_read(self.input_filename)
                control_file = open_fifo_read(self.control_filename)

                if self.on_listening:
                    self.on_listening()

                while True:
                    select_result, _, _, = select.select((input_file, control_file), (), ())

                    if input_file in select_result:
                        should_flush = False

                        line = readline_with_retry(input_file)
                        if not line:
                            # Empty line after select means that the other side has closed its handle
                            input_file.close()
                            input_file = open_fifo_read(self.input_filename)
                        else:
                            while True:
                                try:
                                    event = json.loads(line)
                                except ValueError as e:
                                    log.warn('Invalid JSON: %s\n%s', line, e)
                                    break

                                log.debug('Pending event: %s', repr(event))
                                self.pending_events.append(event)
                                self.event_count += 1
                                should_flush = should_flush or len(self.pending_events) >= self.flush_threshold or \
                                    event.get('priority') >= self.flush_priority

                                line = readline_with_retry(input_file)
                                if not line:
                                    break

                            if should_flush:
                                log.debug('calling flush explicitly')
                                self.flush()

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
            self.event_store.add_events(events)

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
    filename = agent_control_filename(agent_input_filename)
    log.debug('Writing stop to %s', filename)
    with open(filename, b'wb') as f:
        f.write(b'stop')


def extract_class(s):
    module_name, class_name = s.rsplit('.', 1)
    mod = import_module(module_name)
    return getattr(mod, class_name)


def main(args=None):
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
    parser.add_argument(
        '--config-file',
        help='Location of config file in JSON format.',
        type=str,
        default='/etc/jumper_logging_agent/config.json'
    )
    parser.add_argument('-v', '--verbose', help='Print logs', action='store_true')
    parser.add_argument('-d', '--dev-mode', help='Sends data to development BE', action='store_true')
    args = parser.parse_args(args=args)

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

    if not os.path.isfile(args.config_file):
        print('Config file is missing: {}'.format(args.config_file))
        return 3

    with open(args.config_file) as fd:
        try:
            config = json.load(fd)
        except ValueError:
            print('Config file must be in JSON format: {}'.format(args.config_file))
            return 4
    try:
        project_id = config['project_id']
        write_key = config['write_key']
    except KeyError as e:
        print('Missing entry in config file: {}. {}'.format(args.config_file, e))
        return 5

    print('Starting agent')

    def on_listening():
        print('Agent listening on named pipe %s' % (agent.input_filename,))

    agent = Agent(
        input_filename=args.input,
        project_id=project_id,
        write_key=write_key,
        flush_priority=args.flush_priority,
        flush_threshold=args.flush_threshold,
        flush_interval=args.flush_interval,
        default_event_type=args.default_event_type,
        event_store=event_store,
        on_listening=on_listening,
        dev_mode=args.dev_mode
    )

    signal.signal(signal.SIGTERM, lambda *a: agent.stop())
    signal.signal(signal.SIGINT, lambda *a: agent.stop())

    atexit.register(agent.cleanup)

    agent.start()
    agent.cleanup()
    return 0
