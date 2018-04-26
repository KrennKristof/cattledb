#!/usr/bin/python
# coding: utf8

import logging
import time
import six
import struct
import json

from collections import namedtuple, defaultdict
from google.cloud import bigtable
from google.cloud.bigtable.row_filters import CellsColumnLimitFilter
from google.cloud.bigtable.column_family import MaxVersionsGCRule
from google.cloud import happybase
from google.cloud.happybase.batch import Batch

from .helper import from_ts, daily_timestamps
from .models import TimeSeries, Event, EventList
from ..grpcserver.cdb_pb2 import FloatTimeSeries, FloatTimeSeriesList
from ..timeseries_settings import METRIC_NAME_LOOKUP, METRIC_IDS, METRIC_NAMES


logger = logging.getLogger(__name__)


class MetaDataStore(object):
    def __init__(self, connection_object):
        self.connection_object = connection_object
        self.connection_pool = connection_object.pool

    TABLENAME = "metadata"
    TABLEOPTIONS = {}
    # row: deviceid data: last upload, last info


class ActivityStore(object):
    TABLENAME = "activity"
    TABLEOPTIONS = {}
    STOREID = "activity"
    MAX_GET_SIZE = 90*24*60*60
    # row: org/bs/total#reversets#reader colfam: seen:, data: hourminute_device, (device1, device2, rssi, readout_ts?)

    def __init__(self, connection_object):
        self.connection_object = connection_object
        self.connection_pool = connection_object.pool

    def table(self, connection):
        return self.connection_object.get_table(self.TABLENAME, connection=connection)

    def _create_tables(self, silent=False):
        i = self.connection_object.get_admin_instance()
        tables_before = [t.table_id for t in self.connection_object.get_current_tables()]
        logger.warning("CREATE: Existing Tables: {}".format(tables_before))
        table_name = self.connection_object.table_with_prefix(self.TABLENAME)
        if silent and table_name in tables_before:
            return
        table = i.table(table_name)
        table.create()

        # Create Column Family
        cf1 = table.column_family("c", gc_rule=MaxVersionsGCRule(1))
        cf1.create()

        tables_after = [t.table_id for t in self.connection_object.get_current_tables(force_reload=True)]
        logger.warning("CREATE: Created Tables After: {}".format(tables_after))

    @classmethod
    def reverse_day_key(cls, ts):
        time_tuple = time.gmtime(ts)
        y = 5000 - int(time_tuple.tm_year)
        m = 50 - int(time_tuple.tm_mon)
        d = 50 - int(time_tuple.tm_mday)
        return "{:04d}{:02d}{:02d}".format(y,m,d)

    @classmethod
    def reverse_day_key_to_day(cls, reverse_key):
        y = 5000 - int(reverse_key[0:4])
        m = 50 - int(reverse_key[4:6])
        d = 50 - int(reverse_key[6:8])
        return "{:04d}{:02d}{:02d}".format(y,m,d)

    @classmethod
    def get_hour_key(cls, ts):
        time_tuple = time.gmtime(ts)
        return "{:02d}".format(time_tuple.tm_hour)

    @classmethod
    def get_row_key(cls, base_key, day_ts, reader_id=None):
        reverse_day_ts = cls.reverse_day_key(day_ts)
        row_key = "{}#{}".format(base_key, reverse_day_ts)
        if reader_id is not None:
            row_key = "{}#{}".format(row_key, reader_id)
        return row_key

    @classmethod
    def get_insert_keys(cls, reader_id, day_ts, parent_ids=None):
        assert 3 <= len(reader_id) <= 32
        reverse_day_ts = cls.reverse_day_key(day_ts)
        total_key = "t#{}#{}".format(reverse_day_ts, reader_id)
        row_keys = [total_key]
        if parent_ids is not None:
            assert 1 <= len(parent_ids) <= 3
            for p in parent_ids:
                assert 3 <= len(p) <= 32
                row_keys.append("{}#{}#{}".format(p, reverse_day_ts, reader_id))
        return row_keys

    # My Counter inc
    # fix for bug in happybase driver
    # See Github: https://github.com/GoogleCloudPlatform/google-cloud-python-happybase/issues/23
    def counter_inc(self, table, row, column, value=1):
        """Atomically increment a counter column.
        This method atomically increments a counter column in ``row``.
        If the counter column does not exist, it is automatically initialized
        to ``0`` before being incremented.
        :type row: str
        :param row: Row key for the row we are incrementing a counter in.
        :type column: str
        :param column: Column we are incrementing a value in; of the
                       form ``fam:col``.
        :type value: int
        :param value: Amount to increment the counter by. (If negative,
                      this is equivalent to decrement.)
        :rtype: int
        :returns: Counter value after incrementing.
        """
        row = table._low_level_table.row(row, append=True)
        if isinstance(column, six.binary_type):
            column = column.decode('utf-8')
        column_family_id, column_qualifier = column.split(':')
        row.increment_cell_value(column_family_id, column_qualifier, value)
        # See AppendRow.commit() will return a dictionary:
        # {
        #     u'col-fam-id': {
        #         b'col-name1': [
        #             (b'cell-val', datetime.datetime(...)),
        #             ...
        #         ],
        #         ...
        #     },
        # }
        modified_cells = row.commit()
        # Get the cells in the modified column,
        # column_cells = modified_cells[column_family_id][column_qualifier]
        
        if six.PY2:
            column_cells = modified_cells[column_family_id][column_qualifier]
        else:
            inner_keys = list(six.iterkeys(modified_cells[column_family_id]))
            if not inner_keys:
                raise KeyError(column_qualifier)
            if isinstance(inner_keys[0], six.binary_type):
                column_cells = modified_cells[
                    column_family_id][six.b(column_qualifier)]
            elif isinstance(inner_keys[0], six.string_types):
                column_cells = modified_cells[
                    column_family_id][six.u(column_qualifier)]
            else:
                raise KeyError(column_qualifier)

        # Make sure there is exactly one cell in the column.
        if len(column_cells) != 1:
            raise ValueError('Expected server to return one modified cell.')
        column_cell = column_cells[0]
        # Get the bytes value from the column and convert it to an integer.
        bytes_value = column_cell[0]
        int_value, = struct.Struct('>q').unpack(bytes_value)
        return int_value

    def incr_activity(self, reader_id, device_id, timestamp, parent_ids=None, value=1):
        if self.connection_object.read_only:
            raise RuntimeError("Cannot execute incr_activity in readonly mode")    

        # todo check timestamp
        row_keys = self.get_insert_keys(reader_id, timestamp, parent_ids)
        column = "c:{}.{}".format(self.get_hour_key(timestamp), device_id)

        timer = time.time()
        res = []
        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            for r in row_keys:
                # bugfix
                res.append(self.counter_inc(dt, r, column, value))
                # res.append(dt.counter_inc(r, column.encode("utf-8"), value))

        timer = time.time() - timer
        logger.info("INCR ACTIVITY: {}, {} incrs in {}".format(device_id, len(res), timer))
        # print("INCR ACTIVITY: {}, {} incrs in {}".format(device_id, len(res), timer))
        return res

    def get_total_activity_for_day(self, day_ts):
        return self.get_activity_for_day("t", day_ts)

    def get_activity_for_reader(self, reader_id, from_ts, to_ts):
        assert from_ts <= to_ts
        assert to_ts - from_ts < self.MAX_GET_SIZE

        daily_ts =  daily_timestamps(from_ts, to_ts)
        row_keys = [self.get_row_key("t", ts, reader_id=reader_id).encode("utf-8") for ts in daily_ts]
        columns = "c:"
        print(row_keys)

        timer = time.time()

        activitys = defaultdict(lambda: defaultdict(list))
        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            res = dt.rows(row_keys, columns)

        for row_key, data_dict in res:
            day = self.reverse_day_key_to_day(row_key.decode("utf-8").split("#")[-2])
            # Append to Activity
            for k, value in data_dict.items():
                s = k.decode("utf-8").split(":")
                if len(s) != 2:
                    continue
                p = s[1].split(".")
                if len(p) != 2:
                    continue
                day_hour = "{}{}".format(day, p[0])
                # parse value
                int_value, = struct.Struct('>q').unpack(value)
                activitys[day_hour][p[1]].append(int_value)

        timer = time.time() - timer
        logger.info("GET ACTIVITY: {} rows in {}".format(len(row_keys), timer))
        # print("GET ACTIVITY: {} rows in {}".format(len(row_keys), timer))
        return [(k, dict(activitys[k])) for k in sorted(activitys.keys())]


    def get_activity_for_day(self, parent_id, day_ts):
        start_search_row = self.get_row_key(parent_id, day_ts).encode("utf-8")
        row_prefix = self.get_row_key(parent_id, day_ts).encode("utf-8")
        columns = "c:"

        timer = time.time()

        activitys = defaultdict(lambda: defaultdict(list))
        row_counter = 0

        # Start scanning
        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            # with prefix
            # res = dt.scan(row_prefix=row_prefix, columns=columns)
            # with row start
            res = dt.scan(row_start=start_search_row, columns=columns)
            for row_key, data_dict in res:
                # Break if we get another prefix
                if not row_key.startswith(row_prefix):
                    break

                row_counter += 1
                # Append to Activity
                readout_id = row_key.decode("utf-8").split("#")[-1]
                day = self.reverse_day_key_to_day(row_key.decode("utf-8").split("#")[-2])

                for k, value in data_dict.items():
                    s = k.decode("utf-8").split(":")
                    if len(s) != 2:
                        continue
                    p = s[1].split(".")
                    if len(p) != 2:
                        continue
                    day_hour = "{}{}".format(day, p[0])
                    activitys[day_hour][readout_id].append(p[1])

        timer = time.time() - timer
        logger.info("SCAN ACTIVITY: {} rows in {}".format(row_counter, timer))
        # print("SCAN ACTIVITY: {} rows in {}".format(row_counter, timer))
        return [(k, dict(activitys[k])) for k in sorted(activitys.keys())]


class TimeSeriesStore(object):
    TABLENAME = "timeseries"
    TABLEOPTIONS = {}
    STOREID = "timeseries"
    MAX_GET_SIZE = 400 * 24 * 60 * 60 # A bit more than a year

    def __init__(self, connection_object):
        self.connection_object = connection_object
        self.connection_pool = connection_object.pool

    def table(self, connection):
        return self.connection_object.get_table(self.TABLENAME, connection=connection)

    def _create_tables(self, silent=False):
        i = self.connection_object.get_admin_instance()
        tables_before = [t.table_id for t in self.connection_object.get_current_tables()]
        logger.warning("CREATE: Existing Tables: {}".format(tables_before))
        table_name = self.connection_object.table_with_prefix(self.TABLENAME)
        if silent and table_name in tables_before:
            return
        table = i.table(table_name)
        table.create()
        tables_after = [t.table_id for t in self.connection_object.get_current_tables(force_reload=True)]
        logger.warning("CREATE: Created Tables After: {}".format(tables_after))

    def _create_metric(self, metric_name, silent=False):
        if metric_name in METRIC_NAMES:
            metric_id = METRIC_NAME_LOOKUP[metric_name].id
        elif metric_name in METRIC_IDS:
            metric_id = metric_name
        else:
            raise KeyError("metric {} not known (add it to timeseries settings)".format(metric_name))

        i = self.connection_object.get_admin_instance()
        t = i.table(self.connection_object.table_with_prefix(self.TABLENAME))
        families_before = t.list_column_families()
        logger.warning("CREATE CF: Existing Families: {}".format(families_before))
        if silent and metric_id in families_before:
            logger.warning("CREATE CF: Ignoring existing family: {}".format(metric_id))
            return
        cf1 = t.column_family(metric_id, gc_rule=MaxVersionsGCRule(1))
        cf1.create()
        logger.warning("CREATE CF: Created Family: {}".format(metric_id))

    def _create_all_metrics(self):
        to_create = [m.id for m in METRIC_NAME_LOOKUP.values()]
        logger.warning("Performing CREATE CF ALL: this might take a minute")

        i = self.connection_object.get_admin_instance()
        t = i.table(self.connection_object.table_with_prefix(self.TABLENAME))
        families_before = t.list_column_families()
        logger.warning("CREATE CF ALL: Existing Families: {}".format(families_before))
        for metric_id in to_create:
            if metric_id in families_before:
                logger.warning("CREATE CF: Ignoring existing family: {}".format(metric_id))
                continue
            cf1 = t.column_family(metric_id, gc_rule=MaxVersionsGCRule(1))
            cf1.create()
            logger.warning("CREATE CF: Created Family: {}".format(metric_id))
            time.sleep(1)
        logger.warning("CREATE CF ALL: Finished")

    @classmethod
    def reverse_day_key(cls, ts):
        time_tuple = time.gmtime(ts)
        y = 5000 - int(time_tuple.tm_year)
        m = 50 - int(time_tuple.tm_mon)
        d = 50 - int(time_tuple.tm_mday)
        return "{:04d}{:02d}{:02d}".format(y,m,d)

    @classmethod
    def get_row_key(cls, base_key, day_ts):
        reverse_day_ts = cls.reverse_day_key(day_ts)
        row_key = "{}#{}".format(base_key, reverse_day_ts)
        return row_key

    @classmethod
    def get_metric_object(cls, metric_name):
        if metric_name in METRIC_NAMES:
            return METRIC_NAME_LOOKUP[metric_name]
        raise KeyError("metric {} not known".format(metric_name))

    def insert_timeseries(self, ts):
        if self.connection_object.read_only:
            raise RuntimeError("Cannot execute insert_timeseries command in readonly mode")    

        assert bool(ts)
        metric_object = self.get_metric_object(ts.metric)
        key = ts.key
        timer = time.time()
        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            with Batch(dt) as b:
                for day, bucket in ts.daily_storage_buckets():
                    row_key = self.get_row_key(key, day)
                    data = {}
                    for timestamp, val in bucket:
                        cn = "{}:{}".format(metric_object.id, timestamp)
                        data[cn] = val
                    b.put(row_key, data)
        timer = time.time() - timer
        logger.info("INSERT: {}.{}, {} points in {}".format(key, metric_object.name, len(ts), timer))
        # print("INSERT: {}.{}, {} points in {}".format(key, metric, len(ts), timer))
        return len(ts)
    
    def insert(self, key, metric, data, force_float=True):
        ts = TimeSeries(key, metric, data, force_float=force_float)
        return self.insert_timeseries(ts)

    def insert_bulk(self, inserts):
        out = []
        for i in inserts:
            out.append(self.insert(**i))
        return out

    def get_timeseries(self, key, metrics, from_ts, to_ts):
        assert from_ts <= to_ts
        assert to_ts - from_ts < self.MAX_GET_SIZE
        assert len(metrics) > 0
        assert len(metrics[0]) > 1
        timer = time.time()

        metric_objects = [self.get_metric_object(m) for m in metrics]

        row_keys = [self.get_row_key(key, ts).encode("utf-8") for ts in daily_timestamps(from_ts, to_ts)]
        columns = ["{}:".format(m.id) for m in metric_objects]
        print(row_keys)

        timeseries = {m.id: TimeSeries(key, m.name) for m in metric_objects}
        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            res = dt.rows(row_keys, columns)

        for row_key, data_dict in res:
            for k, value in data_dict.items():
                s = k.decode("utf-8").split(":")
                if len(s) != 2:
                    continue
                m = s[0]
                if m in timeseries.keys():
                    ts = int(s[1])
                    timeseries[m].insert_storage_item(ts, value)

        out = []
        size = 0
        for m in metric_objects:
            t = timeseries[m.id]
            t.trim(from_ts, to_ts)
            size += len(t)
            out.append(t)

        timer = time.time() - timer
        logger.info("GET: {}.{}, {} points in {}".format(key, metrics, size, timer))
        # print("GET: {}.{}, {} points in {}".format(key, metrics, size, timer))
        return out

    def get_single_timeseries(self, key, metric, from_ts, to_ts):
        return self.get_timeseries(key, [metric], from_ts, to_ts)[0]

    def get_last_values(self, key, metrics, count=1, max_days=365, max_ts=None):
        if max_ts is None:
            max_ts = int(time.time() + 24 * 60 * 60)

        start_search_row = self.get_row_key(key, max_ts).encode("utf-8")
        row_prefix = "{}#".format(key).encode("utf-8")
        metric_objects = [self.get_metric_object(m) for m in metrics]
        columns = ["{}:".format(m.id) for m in metric_objects]

        timer = time.time()

        timeseries = {m.id: TimeSeries(key, m.name) for m in metric_objects}

        # Start scanning
        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            # with prefix
            # res = dt.scan(row_prefix=row_prefix, limit=max_days, columns=columns)
            # with row start
            res = dt.scan(row_start=start_search_row, limit=max_days, columns=columns)
            for row_key, data_dict in res:
                # Break if we get another deviceid
                if not row_key.startswith(row_prefix):
                    break

                # Append to Timeseries
                for k, value in data_dict.items():
                    s = k.decode("utf-8").split(":")
                    if len(s) != 2:
                        continue
                    m = s[0]
                    if m in timeseries.keys():
                        ts = int(s[1])
                        timeseries[m].insert_storage_item(ts, value)

                if all([len(x) >= count for x in timeseries.values()]):
                    break

        out = []
        size = 0
        for m in metric_objects:
            t = timeseries[m.id]
            t.trim_count_newest(count)
            size += len(t)
            out.append(t)

        timer = time.time() - timer
        logger.info("SCAN: {}.{}, {} points in {}".format(key, metrics, 1, timer))
        # print("SCAN: {}.{}, {} points in {}".format(key, metrics, 1, timer))
        return out

    def delete_timeseries(self, key, metrics, from_ts, to_ts):
        if self.connection_object.read_only:
            raise RuntimeError("Cannot execute delete_timeseries command in readonly mode")

        assert from_ts <= to_ts
        assert len(metrics) > 0
        assert len(metrics[0]) > 1
        timer = time.time()

        row_keys = [self.get_row_key(key, ts).encode("utf-8") for ts in daily_timestamps(from_ts, to_ts)]
        metric_objects = [self.get_metric_object(m) for m in metrics]
        # Check for delete flag
        columns = []
        for m in metric_objects:
            if not m.delete_possible:
                raise RuntimeError("Delete not possible on metric {}".format(m.name))
            columns.append("{}:".format(m.id))

        print(row_keys)

        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            with Batch(dt) as b:
                for row_key in row_keys:
                    b.delete(row_key, columns=columns)

        timer = time.time() - timer
        count = len(row_keys)
        logger.info("DELETE: {}.{}, {} days in {}".format(key, metrics, count, timer))
        # print("DELETE: {}.{}, {} days in {}".format(key, metrics, count, timer))
        return count

class EventStore(object):
    TABLENAME = "events"
    TABLEOPTIONS = {}
    STOREID = "events"
    MAX_GET_SIZE = 45 * 24 * 60 * 60

    def __init__(self, connection_object):
        self.connection_object = connection_object
        self.connection_pool = connection_object.pool

    def table(self, connection):
        return self.connection_object.get_table(self.TABLENAME, connection=connection)

    def _create_tables(self, silent=False):
        i = self.connection_object.get_admin_instance()
        tables_before = [t.table_id for t in self.connection_object.get_current_tables()]
        logger.warning("CREATE: Existing Tables: {}".format(tables_before))
        table_name = self.connection_object.table_with_prefix(self.TABLENAME)
        if silent and table_name in tables_before:
            return
        table = i.table(table_name)
        table.create()

        # Create Column Family
        cf1 = table.column_family("e", gc_rule=MaxVersionsGCRule(1))
        cf1.create()

        tables_after = [t.table_id for t in self.connection_object.get_current_tables(force_reload=True)]
        logger.warning("CREATE: Created Tables After: {}".format(tables_after))

    @classmethod
    def reverse_day_key(cls, ts):
        time_tuple = time.gmtime(ts)
        y = 5000 - int(time_tuple.tm_year)
        m = 50 - int(time_tuple.tm_mon)
        d = 50 - int(time_tuple.tm_mday)
        return "{:04d}{:02d}{:02d}".format(y,m,d)

    @classmethod
    def get_row_key(cls, base_key, name, day_ts):
        reverse_day_ts = cls.reverse_day_key(day_ts)
        row_key = "{}#{}#{}".format(base_key, name, reverse_day_ts)
        return row_key

    def insert_events(self, events):
        if self.connection_object.read_only:
            raise RuntimeError("Cannot execute insert_events command in readonly mode")    

        key = events.key
        name = events.name
        evs = events.events

        assert 1 <= len(evs) < 100

        timer = time.time()
        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            with Batch(dt) as b:
                for ev in evs:
                    row_key = self.get_row_key(key, name, ev.ts)
                    cn = "e:{}".format(ev.ts)
                    data = {cn: json.dumps(ev.data).encode("utf-8")}
                    b.put(row_key, data)

        timer = time.time() - timer
        logger.info("INSERT EVENTS: {}.{}, {} points in {}".format(key, name, len(events.events), timer))
        # print("INSERT: {}.{}, {} points in {}".format(key, metric, len(ts), timer))
        return len(events.events)
    
    def insert_event(self, key, name, ts, data):
        return self.insert_events(EventList(key, name, [Event(ts, data)]))

    def get_events(self, key, name, from_ts, to_ts):
        assert from_ts <= to_ts
        assert to_ts - from_ts < self.MAX_GET_SIZE
        timer = time.time()

        row_keys = [self.get_row_key(key, name, ts).encode("utf-8") for ts in daily_timestamps(from_ts, to_ts)]
        columns = "e:"
        print(row_keys)

        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            res = dt.rows(row_keys, columns)

        events = []
        for row_key, data_dict in res:
            for k, value in data_dict.items():
                s = k.decode("utf-8").split(":")
                if len(s) != 2:
                    continue
                m = s[0]
                ts = int(s[1])
                # check ts
                if from_ts <= ts <= to_ts:
                    events.append(Event(ts, json.loads(value.decode("utf-8"))))

        out = EventList(key, name, sorted(events, key=lambda x: x.ts))

        timer = time.time() - timer
        logger.info("GET EVENTS: {}.{}, {} points in {}".format(key, name, len(out.events), timer))
        # print("GET: {}.{}, {} points in {}".format(key, metrics, size, timer))
        return out

    def get_last_event(self, key, name, count=1):
        assert False
        if max_ts is None:
            max_ts = int(time.time() + 24 * 60 * 60)

        start_search_row = self.get_row_key(key, max_ts).encode("utf-8")
        row_prefix = "{}#".format(key).encode("utf-8")
        metric_objects = [self.get_metric_object(m) for m in metrics]
        columns = ["{}:".format(m.id) for m in metric_objects]

        timer = time.time()

        timeseries = {m.id: TimeSeries(key, m.name) for m in metric_objects}

        # Start scanning
        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            # with prefix
            # res = dt.scan(row_prefix=row_prefix, limit=max_days, columns=columns)
            # with row start
            res = dt.scan(row_start=start_search_row, limit=max_days, columns=columns)
            for row_key, data_dict in res:
                # Break if we get another deviceid
                if not row_key.startswith(row_prefix):
                    break

                # Append to Timeseries
                for key, value in data_dict.items():
                    s = key.decode("utf-8").split(":")
                    if len(s) != 2:
                        continue
                    m = s[0]
                    if m in timeseries.keys():
                        ts = int(s[1])
                        timeseries[m].insert_storage_item(ts, value)

                if all([len(x) >= count for x in timeseries.values()]):
                    break

        out = []
        size = 0
        for m in metric_objects:
            t = timeseries[m.id]
            t.trim_count_newest(count)
            size += len(t)
            out.append(t)

        timer = time.time() - timer
        logger.info("SCAN: {}.{}, {} points in {}".format(key, metrics, 1, timer))
        # print("SCAN: {}.{}, {} points in {}".format(key, metrics, 1, timer))
        return out

    def delete_events(self, key, name, from_ts, to_ts):
        assert False
        if self.connection_object.read_only:
            raise RuntimeError("Cannot execute delete command in readonly mode")

        assert from_ts <= to_ts
        assert len(metrics) > 0
        assert len(metrics[0]) > 1
        timer = time.time()

        row_keys = [self.get_row_key(key, ts).encode("utf-8") for ts in daily_timestamps(from_ts, to_ts)]
        metric_objects = [self.get_metric_object(m) for m in metrics]
        # Check for delete flag
        columns = []
        for m in metric_objects:
            if not m.delete_possible:
                raise RuntimeError("Delete not possible on metric {}".format(m.name))
            columns.append("{}:".format(m.id))

        print(row_keys)

        with self.connection_pool.connection() as conn:
            dt = self.table(conn)
            with Batch(dt) as b:
                for row_key in row_keys:
                    b.delete(row_key, columns=columns)

        timer = time.time() - timer
        count = len(row_keys)
        logger.info("DELETE: {}.{}, {} days in {}".format(key, metrics, count, timer))
        # print("DELETE: {}.{}, {} days in {}".format(key, metrics, count, timer))
        return count