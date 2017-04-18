from __future__ import absolute_import, division, print_function, unicode_literals

import collections
import json
import os
import random
import socket
import string
import unittest
import threading
import subprocess

import time

import errno

import functools
from unittest import skip

from future import standard_library

from . import mock_event_store
from .mock_event_store import MockEventStore

standard_library.install_aliases()
from future.builtins import *

from jumper_logging_agent.agent import Agent, is_fifo

MAIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def delete_file(filename):
    try:
        os.remove(filename)
    except OSError:
        pass


def random_string(n=5):
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def wait_for(predicate, description, timeout=3.0, sample_interval=None):
    sample_interval = sample_interval or timeout / 10
    end_time = time.time() + timeout
    while not predicate():
        if time.time() >= end_time:
            raise Exception('Timed out while waiting for %s' % (description,))
        time.sleep(sample_interval)


def open_fifo_readwrite(filename):
    try:
        os.mkfifo(filename)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    fd = os.open(filename, os.O_RDWR | os.O_NONBLOCK)
    return os.fdopen(fd, 'wb')


local = threading.local()


def close_local_agent_file():
    agent_file = getattr(local, 'agent_file', None)
    if agent_file:
        agent_file.close()
        local.agent_file = None


def retry_with_thread_local_agent_file(f):
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        while True:
            try:
                agent_file = self.thread_local_agent_file()
                return f(self, agent_file, *args, **kwargs)
            except IOError as e:
                # print('thread %s caught IOError %s' % (threading.current_thread().name, e.errno))
                if e.errno == errno.EAGAIN:
                    # time.sleep(random.randint(1, 50) / 1000)
                    pass
                elif e.errno == errno.EPIPE:
                    close_local_agent_file()
                else:
                    print('thread %s caught IOError %s' % (threading.current_thread().name, e.errno))
                    raise

    return wrapper


class ThreadSafeCounter(object):
    def __init__(self, initial_value=0):
        self._lock = threading.Lock()
        self._value = initial_value

    def increment(self, n=1):
        with self._lock:
            self._value += n
            return self._value

    @property
    def value(self):
        return self._value


class _AbstractAgentTestCase(unittest.TestCase):
    def setUp(self):
        self.agent = None
        self.run_id = random_string()
        self.agent_filename = '/tmp/agent_' + self.run_id

    def tearDown(self):
        close_local_agent_file()
        self.stop_agent()
        delete_file(self.agent_filename)

    def thread_local_agent_file(self):
        agent_file = getattr(local, 'agent_file', None)
        if not agent_file:
            agent_file = open_fifo_readwrite(self.agent_filename)
            local.agent_file = agent_file
        return agent_file

    @retry_with_thread_local_agent_file
    def push_events_to_agent(self, f, event_ids=None, priority=None, event_type='t'):
        if event_ids is None:
            event_ids = [12]

        for event_id in event_ids:
            event = {'event_id': event_id}
            if event_type is not None:
                event['type'] = event_type
            if priority is not None:
                event['priority'] = priority
            b = json.dumps(event).encode() + b'\n'
            f.write(b)

        f.flush()

    def written_events(self, t=None):
        raise NotImplementedError()

    def start_agent(self, **kwargs):
        raise NotImplementedError()

    def stop_agent(self):
        raise NotImplementedError()

    def test_flush_threshold(self):
        flush_threshold = 2
        self.start_agent(flush_interval=10, flush_threshold=flush_threshold)
        self.push_events_to_agent(range(flush_threshold))
        wait_for(lambda: len(self.written_events('t')) == flush_threshold, 'events to be flushed')

    def test_flush_priority(self):
        flush_priority = 2
        self.start_agent(flush_interval=10, flush_threshold=10, flush_priority=flush_priority)
        self.push_events_to_agent()
        self.push_events_to_agent(priority=flush_priority)
        wait_for(lambda: len(self.written_events('t')) == 2, 'events to be flushed')

    def test_flush_interval(self):
        flush_interval = 0.3
        self.start_agent(flush_interval=flush_interval, flush_threshold=10)
        self.push_events_to_agent(range(2))
        wait_for(lambda: len(self.written_events('t')) >= 2, 'events to be flushed', 5.0)

    def test_multiple_writers(self):
        flush_interval = 2.0
        self.start_agent(flush_interval=flush_interval)
        should_stop = False
        total_events = 2000
        num_threads = 3
        counter = ThreadSafeCounter()

        def writer(i):
            while not should_stop:
                num_events = random.randint(1, 10)
                event_ids = [counter.increment() for _ in range(num_events)]
                # print('%s writing %s events' % (i, num_events))
                self.push_events_to_agent(event_ids=event_ids)

        threads = [threading.Thread(target=functools.partial(writer, i)) for i in range(num_threads)]

        for i, t in enumerate(threads):
            t.name = 'writer-%s' % i
            t.daemon = True
            t.start()

        wait_for(lambda: counter.value >= total_events, '%s events pushed' % (total_events,), 5.0, 0.1)

        should_stop = True
        for t in threads:
            t.join()

        wait_for(lambda: len(self.written_events('t')) == counter.value, 'all events to be written to event store', 4.0)

        written_event_ids = {e['event_id'] for e in self.written_events('t')}
        expected_event_ids = {i for i in range(1, counter.value+1)}
        self.assertSetEqual(written_event_ids, expected_event_ids)


class AgentTestsInThread(_AbstractAgentTestCase):
    def setUp(self):
        super(AgentTestsInThread, self).setUp()
        self.mock_event_store = None
        self.thread = None

    def start_agent(self, **kwargs):
        self.mock_event_store = MockEventStore()
        listening_event = threading.Event()
        self.agent = Agent(
            input_filename=self.agent_filename,
            event_store=self.mock_event_store, on_listening=lambda: listening_event.set(), **kwargs
        )
        self.thread = threading.Thread(target=self.agent.start)
        self.thread.daemon = True
        self.thread.name = 'Agent_thread'
        self.thread.start()
        if not listening_event.wait(3.0):
            raise Exception('Agent has not started in time')

    def stop_agent(self):
        if self.agent:
            self.agent.stop()
        if self.thread:
            self.thread.join(3.0)
            if self.thread.is_alive():
                raise Exception('Agent thread has not ended')

    def written_events(self, t=None):
        if t is None:
            return self.mock_event_store.events
        else:
            return self.mock_event_store.events[t]

    def test_not_flushed_before_reaching_threshold(self):
        self.start_agent(flush_interval=10, flush_threshold=2)
        self.push_events_to_agent()
        wait_for(lambda: self.agent.event_count, 'event to reach agent')
        self.assertFalse(self.written_events())


class AgentProcessTests(_AbstractAgentTestCase):
    def setUp(self):
        super(AgentProcessTests, self).setUp()
        self.mock_event_store_json = '/tmp/mock_event_store_' + self.run_id
        self.agent_print_stdout_thread = None
        self.agent_output = []

    def tearDown(self):
        super(AgentProcessTests, self).tearDown()
        delete_file(self.mock_event_store_json)

    def start_agent(self, **kwargs):
        args = ['python', '-u', '%s/agent_main.py' % (MAIN_DIR,)]
        args.extend(['--input', self.agent_filename])
        args.extend(['--event-store', 'jumper_logging_agent.tests.mock_event_store.MockEventStoreInJson'])
        for k, v in kwargs.items():
            args.append('--%s' % (k.replace('_', '-')))
            args.append(str(v))

        env = os.environ.copy()
        env[mock_event_store.ENV_JUMPER_MOCK_EVENT_STORE_JSON] = self.mock_event_store_json

        self.agent = subprocess.Popen(args, env=env, stdout=subprocess.PIPE)

        def agent_print_stdout():
            for stdout_line in iter(self.agent.stdout.readline, b''):
                self.agent_output.append(stdout_line)

        self.agent_print_stdout_thread = threading.Thread(target=agent_print_stdout)
        self.agent_print_stdout_thread.start()

        wait_for(lambda: any(l for l in self.agent_output if 'Agent listening' in l), 'agent to start listening')

    def stop_agent(self):
        if self.agent:
            self.agent.terminate()
        if self.agent_print_stdout_thread:
            self.agent_print_stdout_thread.join()

    def written_events(self, t=None):
        try:
            with open(self.mock_event_store_json, b'r') as f:
                events = json.load(f)
        except (ValueError, IOError):
            events = collections.defaultdict(list)

        return events[t] if t is not None else events

