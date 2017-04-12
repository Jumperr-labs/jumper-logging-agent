from __future__ import absolute_import, division, print_function, unicode_literals

import collections
import json
import os
import random
import string
import unittest
import threading
import subprocess

import time

import errno

from future import standard_library

from . import mock_event_store
from .mock_event_store import MockEventStore

standard_library.install_aliases()
from future.builtins import *

from jumper_logging_agent.agent import Agent, is_fifo

MAIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


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

    if not is_fifo(filename):
        raise ValueError('file "%s" is not a named pipe' % (filename,))

    fd = os.open(filename, os.O_RDWR | os.O_NONBLOCK)
    return os.fdopen(fd, 'wb')


class _AbstractAgentTestCase(unittest.TestCase):
    def setUp(self):
        self.agent = None
        self.run_id = random_string()
        self.agent_filename = '/tmp/agent_' + self.run_id
        self.output_file = open_fifo_readwrite(self.agent_filename)
        self.event_count = 0

    def tearDown(self):
        self.output_file.close()
        self.stop_agent()
        os.remove(self.agent_filename)

    def push_event_to_agent(self, priority=None, event_type='t', num_events=1):
        for _ in range(num_events):
            event = {'event_id': self.event_count}
            self.event_count += 1
            if event_type is not None:
                event['type'] = event_type
            if priority is not None:
                event['priority'] = priority
            b = json.dumps(event) + '\n'
            self.output_file.write(b)
        self.output_file.flush()

    def written_events(self, t=None):
        raise NotImplementedError()

    def start_agent(self, **kwargs):
        raise NotImplementedError()

    def stop_agent(self):
        raise NotImplementedError()

    def test_flush_threshold(self):
        flush_threshold=2
        self.start_agent(flush_interval=10, flush_threshold=flush_threshold)
        self.push_event_to_agent(num_events=flush_threshold)
        wait_for(lambda: len(self.written_events('t')) == flush_threshold, 'events to be flushed')

    def test_flush_priority(self):
        flush_priority = 2
        self.start_agent(flush_interval=10, flush_threshold=10, flush_priority=flush_priority)
        self.push_event_to_agent()
        self.push_event_to_agent(priority=flush_priority)
        wait_for(lambda: len(self.written_events('t')) == 2, 'events to be flushed')

    def test_flush_interval(self):
        flush_interval = 0.3
        self.start_agent(flush_interval=flush_interval, flush_threshold=10)
        self.push_event_to_agent(num_events=2)
        wait_for(lambda: len(self.written_events('t')) >= 2, 'events to be flushed', flush_interval * 3)

    def test_multiple_writers(self):
        self.start_agent()
        time.sleep(1)
        should_stop = False
        num_events = 1000
        num_threads = 2

        def writer():
            while not should_stop:
                self.push_event_to_agent(num_events=random.randint(1, 10))

        threads = [threading.Thread(target=writer) for _ in range(num_threads)]

        for t in threads:
            t.start()

        wait_for(lambda: self.event_count >= num_events, '%s events pushed' % (num_events,), 5.0)

        should_stop = True
        for t in threads:
            t.join()

        wait_for(
            lambda: len(self.written_events('t')) == self.event_count,
            '%s events written to store' % (num_events,),
            5.0
        )


class AgentTestsInThread(_AbstractAgentTestCase):
    def setUp(self):
        super(AgentTestsInThread, self).setUp()
        self.mock_event_store = None
        self.thread = None

    def tearDown(self):
        super(AgentTestsInThread, self).tearDown()

    def start_agent(self, **kwargs):
        self.mock_event_store = MockEventStore()
        self.agent = Agent(self.agent_filename, event_store=self.mock_event_store, **kwargs)
        self.thread = threading.Thread(target=self.agent.start)
        self.thread.daemon = True
        self.thread.start()

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
        self.push_event_to_agent()
        wait_for(lambda: self.agent.pending_events, 'event to reach agent')
        self.assertFalse(self.written_events())


class AgentProcessTests(_AbstractAgentTestCase):
    def setUp(self):
        super(AgentProcessTests, self).setUp()
        self.mock_event_store_json = '/tmp/mock_event_store_' + self.run_id

    def tearDown(self):
        super(AgentProcessTests, self).tearDown()

    def start_agent(self, **kwargs):
        args = ['python', '%s/agent_main.py' % (MAIN_DIR,)]
        args.extend(['--input', self.agent_filename])
        args.extend(['--event-store', 'jumper_logging_agent.tests.mock_event_store.MockEventStoreInJson'])
        for k, v in kwargs.items():
            args.append('--%s' % (k.replace('_', '-')))
            args.append(str(v))

        env = os.environ.copy()
        env[mock_event_store.ENV_JUMPER_MOCK_EVENT_STORE_JSON] = self.mock_event_store_json
        self.agent = subprocess.Popen(args, env=env)

    def stop_agent(self):
        if self.agent:
            self.agent.terminate()

    def written_events(self, t=None):
        try:
            with open(self.mock_event_store_json, b'r') as f:
                events = json.load(f)
        except (ValueError, IOError):
            events = collections.defaultdict(list)

        return events[t] if t is not None else events

