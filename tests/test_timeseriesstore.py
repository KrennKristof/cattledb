#!/usr/bin/python
# coding: utf8

import unittest
import random
import logging
import pendulum
import os
import datetime
import mock
import time


from cattledb.storage.connection import Connection
from cattledb.storage.models import TimeSeries
from cattledb.settings import AVAILABLE_METRICS


class TimeSeriesStorageTest(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    @classmethod
    def tearDownClass(cls):
        pass

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO)
        # os.environ["BIGTABLE_EMULATOR_HOST"] = "localhost:8086"
        # os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/mnt/c/Users/mths/.ssh/google_gcp_credentials.json"

    def test_simple(self):
        db = Connection(project_id='test-system', instance_id='test', metric_definition=AVAILABLE_METRICS)
        db.create_tables(silent=True)
        db.timeseries._create_metric("ph", silent=True)
        db.timeseries._create_metric("act", silent=True)
        db.timeseries._create_metric("temp", silent=True)

        d1 = [(i * 600, 6.5) for i in range(502)]
        d2 = [(i * 600 + 24 * 60 * 60, 25.5) for i in range(502)]
        d3 = [(i * 600, 10.5) for i in range(502)]

        data = [{"key": "sensor1",
                 "metric": "ph",
                 "data": d1},
                {"key": "sensor1",
                 "metric": "temp",
                 "data": d2}]
        db.timeseries.insert_bulk(data)
        db.timeseries.insert("sensor1", "act", d3)
        db.timeseries.insert("sensor2", "ph", d3)

        r = db.timeseries.get_single_timeseries("sensor1", "act", 0, 500*600-1)
        a = list(r.all())
        d = list(r.aggregation("daily", "mean"))
        self.assertEqual(len(a), 500)
        self.assertEqual(len(d), 4)
        for ts, v, dt in d:
            self.assertAlmostEqual(v, 10.5, 4)

        r = db.timeseries.get_single_timeseries("sensor1", "ph", 0, 500*600-1)
        a = list(r.all())
        d = list(r.aggregation("daily", "mean"))
        self.assertEqual(len(a), 500)
        self.assertEqual(len(d), 4)
        for ts, v, dt in d:
            self.assertAlmostEqual(v, 6.5, 4)

        r = db.timeseries.get_single_timeseries("sensor1", "temp", 24 * 60 * 60, 24 * 60 * 60 + 500*600)
        a = list(r.all())
        d = list(r.aggregation("daily", "mean"))
        self.assertEqual(len(a), 501)
        self.assertEqual(len(d), 4)
        for ts, v, dt in d:
            self.assertAlmostEqual(v, 25.5, 4)

        s = db.timeseries.get_last_values("sensor1", ["temp", "ph"], count=200)
        temp = s[0]
        self.assertEqual(temp[0].ts, 302 * 600 + 24 * 60 * 60)
        self.assertEqual(temp[-1].ts, 501 * 600 + 24 * 60 * 60)
        ph = s[1]
        self.assertEqual(ph[0].ts, 302 * 600)
        self.assertEqual(ph[-1].ts, 501 * 600)

    def test_delete(self):
        db = Connection(project_id='test-system', instance_id='test', metric_definition=AVAILABLE_METRICS)
        db.create_tables(silent=True)
        db.timeseries._create_metric("ph", silent=True)

        base = datetime.datetime.now()
        data_list = [(base - datetime.timedelta(minutes=10*x), random.random() * 5) for x in range(0, 144*5)]
        ts = TimeSeries("device", "ph", values=data_list)
        from_pd = pendulum.instance(data_list[-1][0])
        from_ts = from_pd.int_timestamp
        to_pd = pendulum.instance(data_list[0][0])
        to_ts = to_pd.int_timestamp

        #delete all data just in case
        r = db.timeseries.delete_timeseries("device", ["ph"], from_ts-24*60*60, to_ts+24*60*60)

        #insert
        db.timeseries.insert_timeseries(ts)

        # get
        r = db.timeseries.get_single_timeseries("device", "ph", from_ts, to_ts)
        a = list(r.all())
        self.assertEqual(len(a), 144 * 5)

        # perform delete
        r = db.timeseries.delete_timeseries("device", ["ph"], from_ts, from_ts)
        self.assertEqual(r, 1)

        # get
        r = db.timeseries.get_single_timeseries("device", "ph", from_ts + 24*60*60, to_ts + 24*60*60)
        a = list(r.all())
        self.assertEqual(len(a), 144 * 4)

        # delete all
        r = db.timeseries.delete_timeseries("device", ["ph"], from_ts, to_ts)
        self.assertGreaterEqual(r, 5)


    def test_signal(self):
        db = Connection(project_id='test-system', instance_id='test', metric_definition=AVAILABLE_METRICS)
        db.create_tables(silent=True)
        db.timeseries._create_metric("temp", silent=True)

        d = [[int(time.time()), 11.1]]
        data = [{"key": "sensor15",
                 "metric": "ph",
                 "data": d}]

        from blinker import signal
        my_put_func = mock.MagicMock(spec={})
        s = signal("timeseries.put")
        s.connect(my_put_func)
        from blinker import signal
        my_get_func = mock.MagicMock(spec={})
        s = signal("timeseries.get")
        s.connect(my_get_func)

        db.timeseries.insert_bulk(data)
        r = db.timeseries.get_single_timeseries("sensor15", "ph", 0, 500*600-1)

        self.assertEqual(len(my_put_func.call_args_list), 1)
        self.assertIn("info", my_put_func.call_args_list[0][1])

    def test_large(self):
        db = Connection(project_id='test-system', instance_id='test', metric_definition=AVAILABLE_METRICS)
        db.create_tables(silent=True)
        db.timeseries._create_metric("act", silent=True)
        db.timeseries._create_metric("temp", silent=True)
        db.timeseries._create_metric("ph", silent=True)

        start = 1483272000

        for id in ["sensor41", "sensor45", "sensor23", "sensor47"]:
            d1 = [(start + i * 600, 6.5) for i in range(5000)]
            d2 = [(start + i * 600, 10.5) for i in range(5000)]
            d3 = [(start, 20.43)]

            data = [{"key": id,
                    "metric": "act",
                    "data": d1},
                    {"key": id,
                    "metric": "temp",
                    "data": d2},
                    {"key": id,
                    "metric": "ph",
                    "data": d3}]
            db.timeseries.insert_bulk(data)

        r = db.timeseries.get_timeseries("sensor47", ["act", "temp", "ph"], start, start+600*4999)
        self.assertEqual(len(r[0]), 5000)
        self.assertEqual(len(r[1]), 5000)
        self.assertEqual(len(r[2]), 1)

        s = db.timeseries.get_last_values("sensor47", ["act", "temp", "ph"], max_days=7, max_ts=start+600*5000)
        act = s[0]
        self.assertEqual(act[0].ts, start + 600 * 4999)
        temp = s[1]
        self.assertEqual(temp[0].ts, start + 600 * 4999)
        ph = s[2]
        self.assertEqual(len(ph), 0)

        # s = db.timeseries.get_last_values("sensor47", ["act", "temp", "ph"], max_days=2, max_ts=start+600*10000)
        # act = s[0]
        # self.assertEqual(len(act), 0)
        # temp = s[1]
        # self.assertEqual(len(temp), 0)
        # ph = s[2]
        # self.assertEqual(len(ph), 0)

        s = db.timeseries.get_last_values("sensor47", ["act", "temp", "ph"], max_days=100, max_ts=start+600*5000)
        act = s[0]
        self.assertEqual(act[0].ts, start + 600 * 4999)
        temp = s[1]
        self.assertEqual(temp[0].ts, start + 600 * 4999)
        ph = s[2]
        self.assertEqual(ph[0].ts, start)

        # s = db.timeseries.get_last_values("sensor47", ["ph"], max_days=7, max_ts=start+600*10000)
        # self.assertEqual(s[0][0], start)
