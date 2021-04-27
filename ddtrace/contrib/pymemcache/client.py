import sys

import pymemcache
from pymemcache.client.base import Client
from pymemcache.exceptions import MemcacheClientError
from pymemcache.exceptions import MemcacheIllegalInputError
from pymemcache.exceptions import MemcacheServerError
from pymemcache.exceptions import MemcacheUnknownCommandError
from pymemcache.exceptions import MemcacheUnknownError

# 3p
from ddtrace import config
from ddtrace.vendor import wrapt

# project
from ...constants import ANALYTICS_SAMPLE_RATE_KEY
from ...constants import SPAN_MEASURED_KEY
from ...ext import SpanTypes
from ...ext import memcached as memcachedx
from ...ext import net
from ...internal.compat import reraise
from ...internal.logger import get_logger
from ...pin import Pin


log = get_logger(__name__)


# keep a reference to the original unpatched clients
_Client = Client


class WrappedClient(wrapt.ObjectProxy):
    """Wrapper providing patched methods of a pymemcache Client.

    Relevant connection information is obtained during initialization and
    attached to each span.

    Keys are tagged in spans for methods that act upon a key.
    """

    def __init__(self, *args, **kwargs):
        c = _Client(*args, **kwargs)
        super(WrappedClient, self).__init__(c)

        # tags to apply to each span generated by this client
        tags = _get_address_tags(*args, **kwargs)

        parent_pin = Pin.get_from(pymemcache)

        if parent_pin:
            pin = parent_pin.clone(tags=tags)
        else:
            pin = Pin(tags=tags)

        # attach the pin onto this instance
        pin.onto(self)

    def set(self, *args, **kwargs):
        return self._traced_cmd('set', *args, **kwargs)

    def set_many(self, *args, **kwargs):
        return self._traced_cmd('set_many', *args, **kwargs)

    def add(self, *args, **kwargs):
        return self._traced_cmd('add', *args, **kwargs)

    def replace(self, *args, **kwargs):
        return self._traced_cmd('replace', *args, **kwargs)

    def append(self, *args, **kwargs):
        return self._traced_cmd('append', *args, **kwargs)

    def prepend(self, *args, **kwargs):
        return self._traced_cmd('prepend', *args, **kwargs)

    def cas(self, *args, **kwargs):
        return self._traced_cmd('cas', *args, **kwargs)

    def get(self, *args, **kwargs):
        return self._traced_cmd('get', *args, **kwargs)

    def get_many(self, *args, **kwargs):
        return self._traced_cmd('get_many', *args, **kwargs)

    def gets(self, *args, **kwargs):
        return self._traced_cmd('gets', *args, **kwargs)

    def gets_many(self, *args, **kwargs):
        return self._traced_cmd('gets_many', *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._traced_cmd('delete', *args, **kwargs)

    def delete_many(self, *args, **kwargs):
        return self._traced_cmd('delete_many', *args, **kwargs)

    def incr(self, *args, **kwargs):
        return self._traced_cmd('incr', *args, **kwargs)

    def decr(self, *args, **kwargs):
        return self._traced_cmd('decr', *args, **kwargs)

    def touch(self, *args, **kwargs):
        return self._traced_cmd('touch', *args, **kwargs)

    def stats(self, *args, **kwargs):
        return self._traced_cmd('stats', *args, **kwargs)

    def version(self, *args, **kwargs):
        return self._traced_cmd('version', *args, **kwargs)

    def flush_all(self, *args, **kwargs):
        return self._traced_cmd('flush_all', *args, **kwargs)

    def quit(self, *args, **kwargs):
        return self._traced_cmd('quit', *args, **kwargs)

    def set_multi(self, *args, **kwargs):
        """set_multi is an alias for set_many"""
        return self._traced_cmd('set_many', *args, **kwargs)

    def get_multi(self, *args, **kwargs):
        """set_multi is an alias for set_many"""
        return self._traced_cmd('get_many', *args, **kwargs)

    def _traced_cmd(self, method_name, *args, **kwargs):
        """Run and trace the given command.

        Any pymemcache exception is caught and span error information is
        set. The exception is then reraised for the application to handle
        appropriately.

        Relevant tags are set in the span.
        """
        method = getattr(self.__wrapped__, method_name)
        p = Pin.get_from(self)

        # if the pin does not exist or is not enabled, shortcut
        if not p or not p.enabled():
            return method(*args, **kwargs)

        with p.tracer.trace(
            memcachedx.CMD,
            service=p.service,
            resource=method_name,
            span_type=SpanTypes.CACHE,
        ) as span:
            span.set_tag(SPAN_MEASURED_KEY)
            # set analytics sample rate
            span.set_tag(
                ANALYTICS_SAMPLE_RATE_KEY,
                config.pymemcache.get_analytics_sample_rate()
            )

            # try to set relevant tags, catch any exceptions so we don't mess
            # with the application
            try:
                span.set_tags(p.tags)
                vals = _get_query_string(args)
                query = '{}{}{}'.format(method_name, ' ' if vals else '', vals)
                span.set_tag(memcachedx.QUERY, query)
            except Exception:
                log.debug('Error setting relevant pymemcache tags')

            try:
                return method(*args, **kwargs)
            except (
                MemcacheClientError,
                MemcacheServerError,
                MemcacheUnknownCommandError,
                MemcacheUnknownError,
                MemcacheIllegalInputError,
            ):
                (typ, val, tb) = sys.exc_info()
                span.set_exc_info(typ, val, tb)
                reraise(typ, val, tb)


def _get_address_tags(*args, **kwargs):
    """Attempt to get host and port from args passed to Client initializer."""
    tags = {}
    try:
        if len(args):
            host, port = args[0]
            tags[net.TARGET_HOST] = host
            tags[net.TARGET_PORT] = port
    except Exception:
        log.debug('Error collecting client address tags')

    return tags


def _get_query_string(args):
    """Return the query values given the arguments to a pymemcache command.

    If there are multiple query values, they are joined together
    space-separated.
    """
    keys = ''

    # shortcut if no args
    if not args:
        return keys

    # pull out the first arg which will contain any key
    arg = args[0]

    # if we get a dict, convert to list of keys
    if type(arg) is dict:
        arg = list(arg)

    if type(arg) is str:
        keys = arg
    elif type(arg) is bytes:
        keys = arg.decode()
    elif type(arg) is list and len(arg):
        if type(arg[0]) is str:
            keys = ' '.join(arg)
        elif type(arg[0]) is bytes:
            keys = b' '.join(arg).decode()

    return keys
