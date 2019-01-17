#!/usr/bin/python
# coding: utf8
from __future__ import unicode_literals
from builtins import str

import sys
from enum import Enum
from collections import MutableSequence, namedtuple, deque
import itertools
import pendulum
from datetime import datetime
import msgpack
import struct
import json

import bisect
import logging
import array
import hashlib
from enum import Enum

from ..grpcserver.cdb_pb2 import FloatTimeSeries, Dictionary, DictTimeSeries, Pair, MetaDataDict, ReaderActivity, DeviceActivity

from .helper import ts_daily_left, ts_daily_right
from .helper import ts_hourly_left, ts_hourly_right
from .helper import ts_weekly_left, ts_weekly_right
from .helper import ts_monthly_left, ts_monthly_right


class sliceable_deque(deque):
    def __getitem__(self, index):
        try:
            return deque.__getitem__(self, index)
        except TypeError:
            start = index.start
            if index.start is not None and index.start < 0:
                start = len(self) + index.start

            stop = index.stop
            if index.stop is not None and index.stop < 0:
                stop = len(self) + index.stop

            if index.step is not None and index.step < 0:
                if index.start is not None or index.stop is not None:
                    raise ValueError("reverse iteration on slice is not possible")
                step = abs(index.step)
                sli = itertools.islice(self, start, stop, step)
                return type(self)(reversed(type(self)(sli)))

            sli = itertools.islice(self, start, stop, index.step)
            return type(self)(sli)


Point = namedtuple('Point', ['ts', 'value', 'dt'])
MetaDataItem = namedtuple('MetaDataItem', ["object_name", "object_id", "key", "data"])

TimestampWithOffset = namedtuple('TimestampWithOffset', ["ts", "offset"])
RowUpsert = namedtuple('RowUpsert', ['row_key', 'cells'])

class SeriesType(Enum):
    FLOATSERIES = 1
    DICTSERIES = 2


class TimeSeries(object):
    DEFAULT_TYPE = SeriesType.FLOATSERIES
    TYPE_WRAPPER = Point

    def __init__(self, key, metric, values=None, series_type=None):
        self._timestamps = array.array("I")
        self._timestamp_offsets = array.array("i")
        self._values = sliceable_deque()
        if series_type is None:
            self.series_type = self.DEFAULT_TYPE
        else:
            self.series_type = series_type
        if values is not None:
            self.insert(values)
        self.key = key.lower()
        self.metric = metric.lower()
        assert len(self.key) >= 2
        assert len(self.metric) >= 2

    @classmethod
    def from_proto_bytes(cls, b, series_type=None):
        if series_type is None:
            series_type = cls.DEFAULT_TYPE
        if series_type == SeriesType.FLOATSERIES:
            f = FloatTimeSeries()
        elif series_type == SeriesType.DICTSERIES:
            f = DictTimeSeries()
        else:
            raise NotImplementedError("wrong series type")
        f.ParseFromString(b)
        return cls.from_proto(f, series_type=series_type)

    @classmethod
    def from_proto(cls, p, series_type=None):
        if series_type is None:
            series_type = cls.DEFAULT_TYPE
        i = cls(p.key, p.metric, series_type=series_type)
        i._timestamps = array.array("I", p.timestamps)
        i._timestamp_offsets = array.array("i", p.timestamp_offsets)
        i._values = sliceable_deque(p.values)
        i.check_series()
        return i

    @classmethod
    def from_list(cls, key, metric, values):
        return cls(key, metric, values)

    def __len__(self):
        return len(self._timestamps)

    def empty(self):
        if len(self) < 1:
            return True
        return False

    def check_series(self):
        assert len(self._timestamps) == len(self._values) == len(self._timestamp_offsets)
        if len(self) > 0:
            self.check_sorted()

    def __bool__(self):  # Python 3
        self.check_series()
        if len(self) > 0:
            return True
        return False

    def check_sorted(self):
        it = iter(self._timestamps)
        if (sys.version_info > (3, 0)):
            it.__next__()
        else:
            it.next()
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

        other.check_series()

        if len(other) < 1:
            return

        assert self.ts_max < other.ts_min
        assert self.key == other.key
        assert self.metric == other.metric
        assert self.series_type == other.series_type

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
    def first(self):
        return None if self.empty() else self[0]

    @property
    def last(self):
        return None if self.empty() else self[-1]

    @property
    def count(self):
        return len(self._timestamps)

    def _at(self, i):
        dt = pendulum.from_timestamp(self._timestamps[i], self._timestamp_offsets[i]/3600.0)
        return self.TYPE_WRAPPER(self._timestamps[i], self._values[i], dt)

    def _storage_item_at(self, i):
        if self.series_type == SeriesType.FLOATSERIES:
            by = struct.pack("B", 1) + struct.pack("i", self._timestamp_offsets[i]) + struct.pack("f", self._values[i])
        elif self.series_type == SeriesType.DICTSERIES:
            by = struct.pack("B", 2) + struct.pack("i", self._timestamp_offsets[i]) + msgpack.packb(self._values[i], use_bin_type=True)
        else:
            raise NotImplementedError("wrong series type")
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

        if f == 1 and self.series_type == SeriesType.FLOATSERIES:
            value = float(struct.unpack("f", by[5:9])[0])
        elif f == 2 and self.series_type == SeriesType.DICTSERIES:
            value = msgpack.unpackb(by[5:], raw=False)
        else:
            raise RuntimeError("Invalid series type or type miss match")

        idx = bisect.bisect_left(self._timestamps, timestamp)
        # Prepend
        if idx == 0:
            self._timestamps.insert(0, timestamp)
            self._values.appendleft(value)
            self._timestamp_offsets.insert(0, offset)
            return 1
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
        elif isinstance(dt, TimestampWithOffset):
            timestamp = int(dt.ts)
            offset = int(dt.offset)
        elif isinstance(dt, pendulum.DateTime):
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
        if self.series_type == SeriesType.FLOATSERIES:
            value = float(value)
        # Force Dict
        if self.series_type == SeriesType.DICTSERIES:
            value = dict(value)
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
        self.check_series() # may be removed
        return counter

    def get_index_below_ts(self, ts):
        if self.empty():
            return None
        low = bisect.bisect_left(self._timestamps, ts) - 1
        if low >= 0:
            return low
        return None

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

    def yield_range(self, ts_min, ts_max):
        """Return an iterator to get all ts value pairs in range.
        """
        low = bisect.bisect_left(self._timestamps, ts_min)
        high = bisect.bisect_right(self._timestamps, ts_max)

        i = low
        while i < high:
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
        if self.series_type == SeriesType.FLOATSERIES:
            ts = FloatTimeSeries()
            ts.values.extend(self._values)
        elif self.series_type == SeriesType.DICTSERIES:
            ts = DictTimeSeries()
            proto_dicts = []
            for v in self._values:
                proto_dicts.append(SerializableDict(v).to_proto())
            ts.values.extend(proto_dicts)
        else:
            raise NotImplementedError("wrong series type")
        ts.metric = self.metric
        ts.key = self.key
        ts.timestamps.extend(self._timestamps)
        ts.timestamp_offsets.extend(self._timestamp_offsets)
        return ts

    def to_proto_bytes(self):
        p = self.to_proto()
        return p.SerializeToString()

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
            yield self.TYPE_WRAPPER(ts, value, dt)


class EventList(TimeSeries):
    DEFAULT_TYPE = SeriesType.DICTSERIES

    def __init__(self, key, name, events=None):
        super(EventList, self).__init__(key=key, metric=name, values=events,
                                        series_type=SeriesType.DICTSERIES)

    @property
    def name(self):
        return self.metric

    @classmethod
    def from_proto(cls, p):
        i = cls(p.key, p.name)
        i._timestamps = array.array("I", p.timestamps)
        i._timestamp_offsets = array.array("i", p.timestamp_offsets)
        i._values = [SerializableDict.from_proto(x) for x in p.values]
        i.check_series()
        return i


class SerializableDict(dict):
    def __init__(self, *args, **kwargs):
        super(SerializableDict, self).__init__(*args, **kwargs)
        self.__dict__ = self

    @classmethod
    def from_proto_bytes(cls, b):
        d = Dictionary()
        d.ParseFromString(b)
        return cls.from_proto(d)

    @classmethod
    def from_proto(cls, p):
        i = cls()
        for pair in p.pairs:
            i[pair.key] = json.loads(pair.value)
        return i

    def to_proto(self):
        d = Dictionary()
        pairs = []
        for k, v in self.items():
            pairs.append(Pair(key=str(k), value=json.dumps(v)))
        d.pairs.extend(pairs)
        return d

    def to_proto_bytes(self):
        p = self.to_proto()
        return p.SerializeToString()

    def to_msgpack(self):
        return msgpack.packb(dict(self), use_bin_type=True)

    @classmethod
    def from_msgpack(cls, b):
        i = cls(dict(msgpack.unpackb(b, raw=False)))
        return i

    def to_dict(self):
        return dict(self)


class SerializableNamespaceDict(object):
    def __init__(self, namespace, data):
        if len(namespace) < 2:
            raise ValueError("Namespace should be at least 2 chars")

        if len(data) < 1:
            raise ValueError("Empty dict")

        self.namespace = namespace
        self.data = SerializableDict(data)

    @classmethod
    def from_proto_bytes(cls, b):
        d = MetaDataDict()
        d.ParseFromString(b)
        return cls.from_proto(d)

    @classmethod
    def from_proto(cls, p):
        d = {}
        for pair in p.pairs:
            d[pair.key] = json.loads(pair.value)
        return cls(p.namespace, d)

    def to_proto(self):
        d = MetaDataDict(namespace=self.namespace)
        pairs = []
        for k, v in self.data.items():
            pairs.append(Pair(key=str(k), value=json.dumps(v)))
        d.pairs.extend(pairs)
        return d

    def to_proto_bytes(self):
        p = self.to_proto()
        return p.SerializeToString()

    def to_dict(self):
        return self.data.to_dict()


class ReaderActivityItem(object):
    def __init__(self, day_hour, reader_id, device_ids):
        self.day_hour = day_hour
        self.reader_id = reader_id
        self.device_ids = list(device_ids)

    def __repr__(self):
        return "<{}.{}: {}>".format(
            self.reader_id, self.day_hour, self.device_ids)

    @classmethod
    def from_proto_bytes(cls, b):
        d = ReaderActivity()
        d.ParseFromString(b)
        return cls.from_proto(d)

    @classmethod
    def from_proto(cls, p):
        return cls(p.day_hour, p.reader_id, p.device_ids)

    def to_proto(self):
        d = ReaderActivity(day_hour=self.day_hour, reader_id=self.reader_id,
                           device_ids=list(self.device_ids))
        return d

    def to_proto_bytes(self):
        p = self.to_proto()
        return p.SerializeToString()

    def to_dict(self):
        return {"day_hour": self.day_hour_dt, "reader_id": self.reader_id, "device_ids": self.device_ids}

    @property
    def day_hour_dt(self):
        y = int(self.day_hour[0:4])
        m = int(self.day_hour[4:6])
        d = int(self.day_hour[6:8])
        h = int(self.day_hour[8:10])
        return pendulum.datetime(y, m, d, h)


class DeviceActivityItem(object):
    def __init__(self, day_hour, device_id, counter):
        self.day_hour = day_hour
        self.device_id = device_id
        self.counter = int(counter)

    def __repr__(self):
        return "<{}.{}: {}>".format(
            self.device_id, self.day_hour, self.counter)

    @classmethod
    def from_proto_bytes(cls, b):
        d = DeviceActivity()
        d.ParseFromString(b)
        return cls.from_proto(d)

    @classmethod
    def from_proto(cls, p):
        return cls(p.day_hour, p.device_id, p.counter)

    def to_proto(self):
        d = DeviceActivity(day_hour=self.day_hour, device_id=self.device_id,
                           counter=int(self.counter))
        return d

    def to_proto_bytes(self):
        p = self.to_proto()
        return p.SerializeToString()

    def to_dict(self):
        return {"day_hour": self.day_hour_dt, "device_id": self.device_id, "counter": self.counter}

    @property
    def day_hour_dt(self):
        y = int(self.day_hour[0:4])
        m = int(self.day_hour[4:6])
        d = int(self.day_hour[6:8])
        h = int(self.day_hour[8:10])
        return pendulum.datetime(y, m, d, h)
