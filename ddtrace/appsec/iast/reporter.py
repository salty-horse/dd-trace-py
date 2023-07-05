from functools import reduce
import json
import operator
import os
from typing import Set
import zlib

import attr

from ddtrace.internal.compat import PY2


def _only_if_true(value):
    return value if value else None


@attr.s(eq=False, hash=False)
class Evidence(object):
    value = attr.ib(type=str, default=None)
    pattern = attr.ib(type=str, default=None)
    valueParts = attr.ib(type=list, default=None)
    redacted = attr.ib(type=bool, default=False, converter=_only_if_true)

    def _valueParts_hash(self):
        if not self.valueParts:
            return

        _hash = 0
        for part in self.valueParts:
            if isinstance(part, dict):
                json_str = json.dumps(part, sort_keys=True)
                part_hash = zlib.crc32(json_str.encode())
            else:
                part_hash = hash(part)
            _hash ^= part_hash

        return _hash

    def __hash__(self):
        return hash((self.value, self.pattern, self._valueParts_hash(), self.redacted))

    def __eq__(self, other):
        return (
            self.value == other.value
            and self.pattern == other.pattern
            and self._valueParts_hash() == other._valueParts_hash()
            and self.redacted == other.redacted
        )


@attr.s(eq=True, hash=True)
class Location(object):
    path = attr.ib(type=str)
    line = attr.ib(type=int)
    spanId = attr.ib(type=int, eq=False, hash=False, repr=False)


@attr.s(eq=True, hash=True)
class Vulnerability(object):
    type = attr.ib(type=str)
    evidence = attr.ib(type=Evidence, repr=True)
    location = attr.ib(type=Location, hash="PYTEST_CURRENT_TEST" in os.environ)
    hash = attr.ib(init=False, eq=False, hash=False, repr=False)

    def __attrs_post_init__(self):
        self.hash = zlib.crc32(repr(self).encode())
        if PY2 and self.hash < 0:
            self.hash += 1 << 32


@attr.s(eq=True, hash=True)
class Source(object):
    origin = attr.ib(type=str)
    name = attr.ib(type=str)
    value = attr.ib(type=str)
    redacted = attr.ib(type=bool, default=False, converter=_only_if_true)


@attr.s(eq=False, hash=False)
class IastSpanReporter(object):
    sources = attr.ib(type=Set[Source], factory=set)
    vulnerabilities = attr.ib(type=Set[Vulnerability], factory=set)

    def __hash__(self):
        for obj in self.sources | self.vulnerabilities:
            print("Hash of %s -> %s" % (obj, hash(obj)))
        return reduce(operator.xor, (hash(obj) for obj in self.sources | self.vulnerabilities))
