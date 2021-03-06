from __future__ import absolute_import, division, print_function, unicode_literals
import collections
# noinspection PyUnresolvedReferences
import json
import os

# noinspection PyUnresolvedReferences
from future.builtins import *

from future import standard_library
standard_library.install_aliases()

ENV_JUMPER_MOCK_EVENT_STORE_JSON = 'JUMPER_MOCK_EVENT_STORE_JSON'


class MockEventStore(object):
    def __init__(self):
        self.events = []

    def add_events(self, events):
        self.events.extend(events)


class MockEventStoreInJson(MockEventStore):
    def __init__(self):
        super(MockEventStoreInJson, self).__init__()
        self.json_filename = os.environ.get(ENV_JUMPER_MOCK_EVENT_STORE_JSON)
        assert self.json_filename, 'Environment variable %s must be set' % (ENV_JUMPER_MOCK_EVENT_STORE_JSON,)

    def dump_to_file(self):
        with open(self.json_filename, b'wb') as f:
            print('dumping to file: %s', (self.events,))
            json.dump(self.events, f)

    def add_events(self, d):
        print('add events: %s' % (d,))
        super(MockEventStoreInJson, self).add_events(d)
        self.dump_to_file()

    def delete(self):
        try:
            os.remove(self.json_filename)
        except IOError:
            pass
