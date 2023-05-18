# coding: utf-8
from collections import defaultdict
import gzip
import os
import struct
import threading
import time
import typing
from typing import DefaultDict
from typing import Dict
from typing import List
from typing import Optional
from typing import Union
from typing import NamedTuple

from ddsketch import LogCollapsingLowestDenseDDSketch
from ddsketch.pb.proto import DDSketchProto
import six
import tenacity

import ddtrace
from ddtrace import config

from .._encoding import packb
from ..agent import get_connection
from ..compat import get_connection_response
from ..compat import httplib
from ..forksafe import Lock
from ..hostname import get_hostname
from ..logger import get_logger
from ..periodic import PeriodicService
from ..writer import _human_size
from .encoding import decode_var_int_64
from .encoding import encode_var_int_64
from .fnv import fnv1_64

log = get_logger(__name__)

PROPAGATION_KEY = "dd-pathway-ctx"
"""
PathwayAggrKey uniquely identifies a pathway to aggregate stats on.
"""
PathwayAggrKey = typing.Tuple[
    str,  # edge tags
    int,  # hash_value
    int,  # parent hash
]


class PathwayStats(object):
    """Aggregated pathway statistics."""

    __slots__ = ("full_pathway_latency", "edge_latency")

    def __init__(self):
        self.full_pathway_latency = LogCollapsingLowestDenseDDSketch(0.00775, bin_limit=2048)
        self.edge_latency = LogCollapsingLowestDenseDDSketch(0.00775, bin_limit=2048)


class PartitionKey(NamedTuple):
    topic: str
    partition: int


class ConsumerPartitionKey(NamedTuple):
    group: str
    topic: str
    partition: int


class Bucket(NamedTuple):
    pathway_stats: DefaultDict[PathwayAggrKey, PathwayStats]
    latest_produce_offsets: DefaultDict[PartitionKey, int]
    latest_commit_offsets: DefaultDict[ConsumerPartitionKey, int]


class DataStreamsProcessor(PeriodicService):
    """DataStreamsProcessor for computing, collecting and submitting data stream stats to the Datadog Agent."""

    def __init__(self, agent_url, interval=None, timeout=1.0, retry_attempts=3):
        # type: (str, Optional[float], float, int) -> None
        if interval is None:
            interval = float(os.getenv("_DD_TRACE_STATS_WRITER_INTERVAL") or 10.0)
        super(DataStreamsProcessor, self).__init__(interval=interval)
        self._agent_url = agent_url
        self._endpoint = "/v0.1/pipeline_stats"
        self._agent_endpoint = "%s%s" % (self._agent_url, self._endpoint)
        self._timeout = timeout
        # Have the bucket size match the interval in which flushes occur.
        self._bucket_size_ns = int(interval * 1e9)  # type: int
        self._buckets = defaultdict(
            lambda: Bucket(defaultdict(PathwayStats), defaultdict(int), defaultdict(int))
        )  # type: DefaultDict[int, Bucket]
        self._headers = {
            "Datadog-Meta-Lang": "python",
            "Datadog-Meta-Tracer-Version": ddtrace.__version__,
            "Content-Type": "application/msgpack",
            "Content-Encoding": "gzip",
        }  # type: Dict[str, str]
        self._hostname = six.ensure_text(get_hostname())
        self._service = six.ensure_text(config._get_service("unnamed-python-service"))
        self._lock = Lock()
        self._current_context = threading.local()
        self._enabled = True
        self._retry_request = tenacity.Retrying(
            # Use a Fibonacci policy with jitter, same as AgentWriter.
            wait=tenacity.wait_random_exponential(
                multiplier=0.618 * self.interval / (1.618 ** retry_attempts) / 2, exp_base=1.618
            ),
            stop=tenacity.stop_after_attempt(retry_attempts),
            retry=tenacity.retry_if_exception_type((httplib.HTTPException, OSError, IOError)),
        )
        self.start()

    def on_checkpoint_creation(
        self, hash_value, parent_hash, edge_tags, now_sec, edge_latency_sec, full_pathway_latency_sec
    ):
        # type: (int, int, List[str], float, float, float) -> None
        if not self._enabled:
            return

        now_ns = int(now_sec * 1e9)

        with self._lock:
            # Align the span into the corresponding stats bucket
            bucket_time_ns = now_ns - (now_ns % self._bucket_size_ns)
            aggr_key = (",".join(edge_tags), hash_value, parent_hash)
            stats = self._buckets[bucket_time_ns].pathway_stats[aggr_key]
            stats.full_pathway_latency.add(full_pathway_latency_sec)
            stats.edge_latency.add(edge_latency_sec)

    # for now, we only track consumer lag for Kafka. We might want to generalize that logic later on.
    def track_kafka_produce(self, topic, partition, offset, now_sec):
        print("tracking kafka produce", topic, partition, offset, now_sec)
        now_ns = int(now_sec * 1e9)
        key = PartitionKey(topic, partition)
        with self._lock:
            bucket_time_ns = now_ns - (now_ns % self._bucket_size_ns)
            self._buckets[bucket_time_ns].latest_produce_offsets[key] = max(
                offset, self._buckets[bucket_time_ns].latest_produce_offsets[key]
            )

    def track_kafka_commit(self, group, topic, partition, offset, now_sec):
        print("tracking kafka commit", group, topic, partition, offset, now_sec)
        now_ns = int(now_sec * 1e9)
        key = ConsumerPartitionKey(group, topic, partition)
        with self._lock:
            bucket_time_ns = now_ns - (now_ns % self._bucket_size_ns)
            self._buckets[bucket_time_ns].latest_commit_offsets[key] = max(
                offset, self._buckets[bucket_time_ns].latest_commit_offsets[key]
            )

    def _serialize_buckets(self):
        # type: () -> List[Dict]
        """Serialize and update the buckets."""
        serialized_buckets = []
        serialized_bucket_keys = []
        for bucket_time_ns, bucket in self._buckets.items():
            bucket_aggr_stats = []
            backlogs = []
            serialized_bucket_keys.append(bucket_time_ns)

            for aggr_key, stat_aggr in bucket.pathway_stats.items():
                edge_tags, hash_value, parent_hash = aggr_key
                serialized_bucket = {
                    u"EdgeTags": [six.ensure_text(tag) for tag in edge_tags.split(",")],
                    u"Hash": hash_value,
                    u"ParentHash": parent_hash,
                    u"PathwayLatency": DDSketchProto.to_proto(stat_aggr.full_pathway_latency).SerializeToString(),
                    u"EdgeLatency": DDSketchProto.to_proto(stat_aggr.edge_latency).SerializeToString(),
                }
                bucket_aggr_stats.append(serialized_bucket)
            for key, offset in bucket.latest_commit_offsets.items():
                backlogs.append(
                    {
                        u"Tags": [
                            "type:kafka_commit",
                            "consumer_group:" + key.group,
                            "topic:" + key.topic,
                            "partition:" + str(key.partition),
                        ],
                        u"Value": offset,
                    }
                )
            for key, offset in bucket.latest_produce_offsets.items():
                backlogs.append(
                    {
                        u"Tags": ["type:kafka_produce", "topic:" + key.topic, "partition:" + str(key.partition)],
                        u"Value": offset,
                    }
                )
            print("backlogs")
            print(backlogs)
            serialized_buckets.append(
                {
                    u"Start": bucket_time_ns,
                    u"Duration": self._bucket_size_ns,
                    u"Stats": bucket_aggr_stats,
                    u"Backlogs": backlogs,
                }
            )

        # Clear out buckets that have been serialized
        for key in serialized_bucket_keys:
            del self._buckets[key]

        return serialized_buckets

    def _flush_stats(self, payload):
        # type: (bytes) -> None
        try:
            conn = get_connection(self._agent_url, self._timeout)
            conn.request("POST", self._endpoint, payload, self._headers)
            resp = get_connection_response(conn)
        except Exception:
            log.error("failed to submit pathway stats to the Datadog agent at %s", self._agent_endpoint, exc_info=True)
            raise
        else:
            if resp.status == 404:
                log.error("Datadog agent does not support data streams monitoring. Upgrade to 7.34+")
                self._enabled = False
                return
            elif resp.status >= 400:
                log.error(
                    "failed to send data stream stats payload, %s (%s) (%s) response from Datadog agent at %s",
                    resp.status,
                    resp.reason,
                    resp.read(),
                    self._agent_endpoint,
                )
            else:
                log.debug("sent %s to %s", _human_size(len(payload)), self._agent_endpoint)

    def periodic(self):
        # type: () -> None

        with self._lock:
            serialized_stats = self._serialize_buckets()

        if not serialized_stats:
            # No stats to report, short-circuit.
            return
        raw_payload = {
            u"Service": self._service,
            u"TracerVersion": ddtrace.__version__,
            u"Lang": "python",
            u"Stats": serialized_stats,
            u"Hostname": self._hostname,
        }  # type: Dict[str, Union[List[Dict], str]]
        if config.env:
            raw_payload[u"Env"] = six.ensure_text(config.env)
        if config.version:
            raw_payload[u"Version"] = six.ensure_text(config.version)

        payload = packb(raw_payload)
        compressed = gzip.compress(payload, 1)
        try:
            self._retry_request(self._flush_stats, compressed)
        except tenacity.RetryError:
            log.error("retry limit exceeded submitting pathway stats to the Datadog agent at %s", self._agent_endpoint)

    def shutdown(self, timeout):
        # type: (Optional[float]) -> None
        self.periodic()
        self.stop(timeout)

    def decode_pathway(self, data):
        # type: (bytes) -> DataStreamsCtx
        try:
            hash_value = struct.unpack("<Q", data[:8])[0]
            data = data[8:]
            pathway_start_ms, data = decode_var_int_64(data)
            current_edge_start_ms, data = decode_var_int_64(data)
            ctx = DataStreamsCtx(self, hash_value, float(pathway_start_ms) / 1e3, float(current_edge_start_ms) / 1e3)
            # reset context of current thread every time we decode
            self._current_context.value = ctx
            return ctx
        except EOFError:
            return self.new_pathway()

    def new_pathway(self):
        # type: () -> DataStreamsCtx
        now_sec = time.time()
        ctx = DataStreamsCtx(self, 0, now_sec, now_sec)
        return ctx

    def set_checkpoint(self, tags):
        if hasattr(self._current_context, "value"):
            ctx = self._current_context.value
        else:
            ctx = self.new_pathway()
            self._current_context.value = ctx
        ctx.set_checkpoint(tags)
        return ctx


class DataStreamsCtx:
    def __init__(self, processor, hash_value, pathway_start_sec, current_edge_start_sec):
        # type: (DataStreamsProcessor, int, float, float) -> None
        self.processor = processor
        self.pathway_start_sec = pathway_start_sec
        self.current_edge_start_sec = current_edge_start_sec
        self.hash = hash_value
        self.service = six.ensure_text(config._get_service("unnamed-python-service"))
        self.env = six.ensure_text(config.env or "none")

    def encode(self):
        # type: () -> bytes
        return (
            struct.pack("<Q", self.hash)
            + encode_var_int_64(int(self.pathway_start_sec * 1e3))
            + encode_var_int_64(int(self.current_edge_start_sec * 1e3))
        )

    def set_checkpoint(self, tags):
        # type: (List[str]) -> None
        now_sec = time.time()
        tags = sorted(tags)
        b = bytes(self.service, "utf-8") + bytes(self.env, "utf-8")
        for t in tags:
            b += bytes(t, "utf-8")
        node_hash = fnv1_64(b)
        parent_hash = self.hash
        hash_value = fnv1_64(struct.pack("<Q", node_hash) + struct.pack("<Q", parent_hash))
        edge_latency_sec = now_sec - self.current_edge_start_sec
        pathway_latency_sec = now_sec - self.pathway_start_sec
        self.hash = hash_value
        self.current_edge_start_sec = now_sec
        self.processor.on_checkpoint_creation(
            hash_value, parent_hash, tags, now_sec, edge_latency_sec, pathway_latency_sec
        )