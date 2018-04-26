#!/usr/bin/python
# coding: utf8

from enum import Enum
from collections import MutableSequence
from collections import namedtuple
import pendulum
from datetime import datetime
import msgpack
import struct

import bisect
import logging
import array
import hashlib

from ..grpcserver.cdb_pb2 import FloatTimeSeries

from .helper import ts_daily_left, ts_daily_right
from .helper import ts_hourly_left, ts_hourly_right
from .helper import ts_weekly_left, ts_weekly_right
from .helper import ts_monthly_left, ts_monthly_right


Aggregation = namedtuple('Aggregation', ['min', 'max', 'sum', 'count'])
Point = namedtuple('Point', ['ts', 'value', 'dt'])
EventList = namedtuple('EventList', ['key', 'name', 'events'])
Event = namedtuple('Event', ['ts', 'data'])


class TimeSeries(object):
    def __init__(self, key, metric, values=None, force_float=True):
        self._timestamps = array.array("I")
        self._timestamp_offsets = array.array("i")
        self._values = list()
        self.force_float = force_float
        if values is not None:
            self.insert(values)
        self.key = key.lower()
        self.metric = metric.lower()
        assert len(self.key) >= 2
        assert len(self.metric) >= 2

    @classmethod
    def from_proto(cls, p):
        i = cls(p.key, p.metric, force_float=True)
        i._timestamps = array.array("I", p.timestamps)
        i._timestamp_offsets = array.array("i", p.timestamp_offsets)
        i._values = list(p.values)
        if not bool(i):
            raise ValueError("empty or invalid timeseries")
        return i

    @classmethod
    def from_list(cls, key, metric, values):
        return cls(key, metric, values)

    def __len__(self):
        return len(self._timestamps)

    def __bool__(self):  # Python 3
        if len(self) < 1:
            return False
        
        assert len(self._timestamps) == len(self._values) == len(self._timestamp_offsets)
        self.checkSorted()
        return True

    def checkSorted(self):
        it = iter(self._timestamps)
        it.__next__()
        assert all(b >= a for a, b in zip(self._timestamps, it))

    def to_hash(self):
        s = "{}.{}.{}.{}.{}".format(self.key, self.metric, len(self), 
                                    self.ts_min, self.ts_max)
        return hashlib.sha1(s.encode("utf-8")).hexdigest()

    def __eq__(self, other):
        if not isinstance(other, TimeSeries):
            return False
        # Is Hashing a Performance Problem ?
        h1 = self.to_hash()
        h2 = other.to_hash()
        return h1 == h2

    def append_timeseries(self, other):
        if not isinstance(other, TimeSeries):
            raise ValueError("cannot append %s to TimeSeries", other)

        assert bool(other)
        assert self.ts_max < other.ts_min
        assert self.key == other.key
        assert self.metric == other.metric
        assert self.force_float == other.force_float

        self._timestamps += other._timestamps
        self._timestamp_offsets += other._timestamp_offsets
        self._values += other._values

    def __ne__(self, other):
        return not self == other  # NOT return not self.__eq__(other)

    def __repr__(self):
        l = len(self._timestamps)
        if l > 0:
            m = self._timestamps[0]
        else:
            m = -1
        return "<{}.{} series({}), min_ts: {}>".format(
            self.key, self.metric, l, m)

    @property
    def ts_max(self):
        if len(self._timestamps) > 0:
            return self._timestamps[-1]
        return -1

    @property
    def ts_min(self):
        if len(self._timestamps) > 0:
            return self._timestamps[0]
        return -1

    @property
    def count(self):
        return len(self._timestamps)

    def _at(self, i):
        dt = pendulum.from_timestamp(self._timestamps[i], self._timestamp_offsets[i]/3600.0)
        return Point(self._timestamps[i], self._values[i], dt)

    def _storage_item_at(self, i):
        if self.force_float:
            by = struct.pack("B", 1) + struct.pack("i", self._timestamp_offsets[i]) + struct.pack("f", self._values[i])
        else:
            by = struct.pack("B", 2) + struct.pack("i", self._timestamp_offsets[i]) + msgpack.packb(self._values[i])
        return (self._timestamps[i], by)

    def _serializable_at(self, i):
        dt = pendulum.from_timestamp(self._timestamps[i], self._timestamp_offsets[i]/3600.0)
        return (dt.isoformat(), self._values[i])

    def __getitem__(self, key):
        return self._at(key)

    def to_list(self):
        out = list()
        for i in range(len(self._timestamps)):
            out.append(self._at(i))
        return out

    def insert_storage_item(self, timestamp, by, overwrite=False):
        f = int(struct.unpack("B", by[0:1])[0])
        offset = int(struct.unpack("i", by[1:5])[0])
        if f == 1 or self.force_float:
            self.force_float = True
            value = float(struct.unpack("f", by[5:9])[0])
        else:
            value = msgpack.unpackb(by[5:])

        idx = bisect.bisect_left(self._timestamps, timestamp)
        # Append
        if idx == len(self._timestamps):
            self._timestamps.append(timestamp)
            self._values.append(value)
            self._timestamp_offsets.append(offset)
            return 1
        # Already Existing
        if self._timestamps[idx] == timestamp:
            # Replace
            logging.debug("duplicate insert")
            if overwrite:
                self._timestamp_offsets[idx] = offset
                self._values[idx] = value
                return 1
            return 0
        # Insert
        self._timestamps.insert(idx, timestamp)
        self._values.insert(idx, value)
        self._timestamp_offsets.insert(idx, offset)
        return 1

    def insert_point(self, dt, value, overwrite=False):
        if isinstance(dt, int):
            timestamp = dt
            offset = 0
        elif isinstance(dt, float):
            timestamp = int(dt)
            offset = 0
        elif isinstance(dt, pendulum.Pendulum):
            timestamp = dt.int_timestamp
            offset = dt.offset
        elif isinstance(dt, datetime):
            pd = pendulum.instance(dt)
            timestamp = pd.int_timestamp
            offset = pd.offset
        elif isinstance(dt, tuple):
            timestamp = int(dt[0])
            offset = int(dt[1])
        else:
            raise ValueError("Invalid TS format: %s", dt)

        idx = bisect.bisect_left(self._timestamps, timestamp)
        # Force Float
        if self.force_float:
            value = float(value)
        # Append
        if idx == len(self._timestamps):
            self._timestamps.append(timestamp)
            self._values.append(value)
            self._timestamp_offsets.append(offset)
            return 1
        # Already Existing
        if self._timestamps[idx] == timestamp:
            # Replace
            logging.debug("duplicate insert")
            if overwrite:
                self._timestamp_offsets[idx] = offset
                self._values[idx] = value
                return 1
            return 0
        # Insert
        self._timestamps.insert(idx, timestamp)
        self._values.insert(idx, value)
        self._timestamp_offsets.insert(idx, offset)
        return 1

    def insert(self, series):
        counter = 0
        for timestamp, value in series:
            counter += self.insert_point(timestamp, value)
        self.checkSorted() # may be removed
        return counter

    def trim(self, ts_min, ts_max):
        low = bisect.bisect_left(self._timestamps, ts_min)
        high = bisect.bisect_right(self._timestamps, ts_max)
        self._timestamps = self._timestamps[low:high]
        self._values = self._values[low:high]
        self._timestamp_offsets = self._timestamp_offsets[low:high]

    def trim_count_newest(self, count):
        if len(self) <= count:
            return
        self._timestamps = self._timestamps[-int(count):]
        self._values = self._values[-int(count):]
        self._timestamp_offsets = self._timestamp_offsets[-int(count):]

    def trim_count_oldest(self, count):
        if len(self) <= count:
            return
        self._timestamps = self._timestamps[:int(count)]
        self._values = self._values[:int(count)]
        self._timestamp_offsets = self._timestamp_offsets[:int(count)]

    def all(self):
        """Return an iterator to get all ts value pairs.
        """
        i = 0
        while i < len(self._timestamps):
            yield self._at(i)
            i += 1

    def daily(self):
        """Generator to access daily data.
        This will return an inner generator.
        """
        i = 0
        while i < len(self._timestamps):
            j = 0
            lower_bound = ts_daily_left(self._timestamps[i])
            upper_bound = ts_daily_right(self._timestamps[i])
            while (i + j < len(self._timestamps) and
                   lower_bound <= self._timestamps[i + j] <= upper_bound):
                j += 1
            yield (self._at(x) for x in range(i, i + j))
            i += j

    def daily_storage_buckets(self):
        i = 0
        while i < len(self._timestamps):
            j = 0
            lower_bound = ts_daily_left(self._timestamps[i])
            upper_bound = ts_daily_right(self._timestamps[i])
            while (i + j < len(self._timestamps) and
                   lower_bound <= self._timestamps[i + j] <= upper_bound):
                j += 1
            yield (lower_bound, [self._storage_item_at(x) for x in range(i, i + j)])
            i += j

    def to_proto(self):
        if not self.force_float:
            raise ValueError("cannot encode non-float timeseries to protobuf")
        ts = FloatTimeSeries()
        ts.metric = self.metric
        ts.key = self.key
        ts.values[:] = self._values
        ts.timestamps[:] = self._timestamps
        ts.timestamp_offsets[:] = self._timestamp_offsets
        return ts

    def to_serializable(self):
        i = 0
        while i < len(self._timestamps):
            yield self._serializable_at(i)
            i += 1

    def hourly(self):
        """Generator to access hourly data.
        This will return an inner generator.
        """
        i = 0
        while i < len(self._timestamps):
            j = 0
            lower_bound = ts_hourly_left(self._timestamps[i])
            upper_bound = ts_hourly_right(self._timestamps[i])
            while (i + j < len(self._timestamps) and
                   lower_bound <= self._timestamps[i + j] <= upper_bound):
                j += 1
            yield (self._at(x) for x in range(i, i + j))
            i += j

    def aggregation(self, group="hourly", function="mean"):
        """Aggregation Generator.
        """
        if group == "hourly":
            it = self.hourly
            left = ts_hourly_left
        elif group == "daily":
            it = self.daily
            left = ts_daily_left
        else:
            raise ValueError("Invalid aggregation group")

        if function == "sum":
            func = sum
        elif function == "count":
            func = len
        elif function == "min":
            func = min
        elif function == "max":
            func = max
        elif function == "amp":
            def amp(x):
                return max(x) - min(x)
            func = amp
        elif function == "mean":
            def mean(x):
                return sum(x) / len(x)
            func = mean
        else:
            raise ValueError("Invalid aggregation group")

        for g in it():
            t = list(g)
            ts = left(t[0].ts)
            offset = t[0].dt.offset
            dt = pendulum.from_timestamp(ts, offset/3600.0)
            value = func([x.value for x in t])
            yield Point(ts, value, dt)
