#!/usr/bin/python
# coding: utf8

import logging
import logging.config
import pendulum
import time

from datetime import datetime
from functools import partial

from ..storage.connection import Connection
from ..storage.models import TimeSeries, EventList, MetaDataItem


def logging_setup(config):
    if hasattr(config, "LOGGING_CONFIG"):
        logging.config.dictConfig(config.LOGGING_CONFIG)
    else:
        logging.basicConfig(level=logging.INFO)


def create_client(config, setup_logging=True):
    # Setup DB
    project_id = config.GCP_PROJECT_ID
    instance_id = config.GCP_INSTANCE_ID
    credentials = config.GCP_CREDENTIALS
    read_only = config.READ_ONLY
    pool_size = config.POOL_SIZE
    table_prefix = config.TABLE_PREFIX
    metrics = config.METRICS
    if config.STAGING:
        read_only = True

    if setup_logging:
        logging_setup(config)

    return CDBClient(project_id=project_id, instance_id=instance_id, read_only=read_only,
                     pool_size=pool_size, table_prefix=table_prefix, credentials=credentials,
                     metric_definition=metrics)


def to_pendulum(dt, allow_int=True):
    if isinstance(dt, pendulum.DateTime):
        return dt
    elif isinstance(dt, datetime):
        return pendulum.instance(dt)
    elif allow_int and isinstance(dt, (int, float)):
        return pendulum.from_timestamp(int(dt))
    else:
        raise ValueError("dt is not instance of pendulum or python datetime")


class CDBClient(object):
    _enforce_read_only = False

    def __init__(self, project_id, instance_id, credentials, table_prefix, metric_definition,
                 pool_size=1, read_only=True, event_definitions=None):
        if CDBClient._enforce_read_only and not read_only:
            raise RuntimeError("Direct CDBClient only allowed for read_only access")
        self.read_only = read_only
        self.db = Connection(project_id=project_id, instance_id=instance_id, read_only=read_only,
                             pool_size=pool_size, table_prefix=table_prefix, credentials=credentials,
                             metric_definition=metric_definition, event_definitions=event_definitions)

    def raise_on_read_only(self):
        if self.read_only:
            raise RuntimeError("not possible in read only mode")

    # --------------------------------------------------------------------------
    # Timeseries
    # --------------------------------------------------------------------------

    def get_timeseries(self, key, metrics, from_datetime, to_datetime):
        from_ts = to_pendulum(from_datetime).int_timestamp
        to_ts = to_pendulum(to_datetime).int_timestamp
        return self.db.timeseries.get_timeseries(key, metrics, from_ts, to_ts)

    def delete_timeseries(self, key, metrics, from_datetime, to_datetime):
        self.raise_on_read_only()
        from_ts = to_pendulum(from_datetime).int_timestamp
        to_ts = to_pendulum(to_datetime).int_timestamp
        return self.db.timeseries.delete_timeseries(key, metrics, from_ts, to_ts)

    def get_last_values(self, key, metrics):
        return self.db.timeseries.get_last_values(key, metrics)

    def put_timeseries(self, key, metric, data):
        self.raise_on_read_only()
        ts = TimeSeries(key, metric, values=data)
        return self.db.timeseries.insert_timeseries(ts)

    def put_timeseries_multi(self, data):
        self.raise_on_read_only()
        res = []
        for item in data:
            ts = TimeSeries(item["key"], item["metric"], values=item["data"])
            res.append(self.db.timeseries.insert_timeseries(ts))
        return res

    # --------------------------------------------------------------------------
    # Events
    # --------------------------------------------------------------------------

    def put_events(self, key, name, events):
        self.raise_on_read_only()
        ev = EventList(key, name, events)
        return self.db.events.insert_events(ev)

    def get_events(self, key, name, from_datetime, to_datetime):
        from_ts = to_pendulum(from_datetime).int_timestamp
        to_ts = to_pendulum(to_datetime).int_timestamp
        return self.db.events.get_events(key, name, from_ts, to_ts)

    def get_last_events(self, key, name):
        return self.db.events.get_last_events(key, name)

    def delete_events(self, key, name, from_datetime, to_datetime):
        self.raise_on_read_only()
        from_ts = to_pendulum(from_datetime).int_timestamp
        to_ts = to_pendulum(to_datetime).int_timestamp
        return self.db.events.delete_event_days(key, name, from_ts, to_ts)

    # --------------------------------------------------------------------------
    # Metadata
    # --------------------------------------------------------------------------

    def put_metadata(self, object_name, object_key, namespace, data, internal=False):
        self.raise_on_read_only()
        if not isinstance(data, dict):
            raise ValueError("data should be a dict")
        md = MetaDataItem(object_name, object_key, namespace, data)
        return self.db.metadata.put_metadata_items([md], internal=internal)

    def get_metadata(self, object_name, object_key, namespaces=None, internal=False):
        return self.db.metadata.get_metadata(object_name, object_key, keys=namespaces, internal=internal)

    # --------------------------------------------------------------------------
    # Activity
    # --------------------------------------------------------------------------

    def incr_activity(self, reader_id, device_id, timestamp, parent_ids=None, value=1):
        self.raise_on_read_only()
        ts = to_pendulum(timestamp, allow_int=True).int_timestamp
        return self.db.activity.incr_activity(reader_id, device_id,
                                              timestamp=ts, parent_ids=parent_ids, value=value)

    def get_total_activity(self, day):
        day_ts = to_pendulum(day, allow_int=True).int_timestamp
        return self.db.activity.get_total_activity_for_day(day_ts)

    def get_day_activity(self, parent_id, day):
        day_ts = to_pendulum(day, allow_int=True).int_timestamp
        return self.db.activity.get_activity_for_day(parent_id, day_ts)

    def get_reader_activity(self, reader_id, from_datetime, to_datetime):
        from_ts = to_pendulum(from_datetime).int_timestamp
        to_ts = to_pendulum(to_datetime).int_timestamp
        return self.db.activity.get_activity_for_reader(reader_id, from_ts, to_ts)


class AsyncCDBClient(object):
    def __init__(self, *args, **kwargs):
        if "pool_size" in kwargs:
            self.pool_size = kwargs["pool_size"]
        else:
            self.pool_size = 1
            kwargs["pool_size"] = 1
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        self.loop = asyncio.get_event_loop()
        self.executor = ThreadPoolExecutor(max_workers=self.pool_size)
        self._client = CDBClient(*args, **kwargs)

    def block(self, *args, timer=1, **kwargs):
        time.sleep(timer)
        return timer

    async def async_block(self, *args, timer=1, **kwargs):
        call = partial(self.block, *args, timer=timer, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    # --------------------------------------------------------------------------
    # Timeseries
    # --------------------------------------------------------------------------

    async def get_timeseries(self, *args, **kwargs):
        call = partial(self._client.get_timeseries, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def delete_timeseries(self, *args, **kwargs):
        call = partial(self._client.delete_timeseries, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def get_last_values(self, *args, **kwargs):
        call = partial(self._client.get_last_values, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def put_timeseries(self, *args, **kwargs):
        call = partial(self._client.put_timeseries, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def put_timeseries_multi(self, *args, **kwargs):
        call = partial(self._client.put_timeseries_multi, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    # --------------------------------------------------------------------------
    # Events
    # --------------------------------------------------------------------------

    async def put_events(self, *args, **kwargs):
        call = partial(self._client.put_events, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def get_events(self, *args, **kwargs):
        call = partial(self._client.get_events, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def get_last_events(self, *args, **kwargs):
        call = partial(self._client.get_last_events, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def delete_events(self, *args, **kwargs):
        call = partial(self._client.delete_events, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    # --------------------------------------------------------------------------
    # Metadata
    # --------------------------------------------------------------------------

    async def put_metadata(self, *args, **kwargs):
        call = partial(self._client.put_metadata, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def get_metadata(self, *args, **kwargs):
        call = partial(self._client.get_metadata, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    # --------------------------------------------------------------------------
    # Activity
    # --------------------------------------------------------------------------

    async def incr_activity(self, *args, **kwargs):
        call = partial(self._client.incr_activity, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def get_total_activity(self, *args, **kwargs):
        call = partial(self._client.get_total_activity, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def get_day_activity(self, *args, **kwargs):
        call = partial(self._client.get_day_activity, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)

    async def get_reader_activity(self, *args, **kwargs):
        call = partial(self._client.get_reader_activity, *args, **kwargs)
        return await self.loop.run_in_executor(self.executor, call)
