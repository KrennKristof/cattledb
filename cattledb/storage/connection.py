#!/usr/bin/python
# coding: utf8

import logging
import time
import os
import random
import struct

from collections import OrderedDict

from google.cloud import bigtable
from google.auth.credentials import AnonymousCredentials
from google.cloud.bigtable.row_filters import CellsColumnLimitFilter, FamilyNameRegexFilter, RowFilterChain, RowFilterUnion, RowKeyRegexFilter
from google.cloud.bigtable.row_set import RowSet
from google.cloud._helpers import _to_bytes
#from google.oauth2 import service_account
import google.auth


logger = logging.getLogger(__name__)


class Connection(object):
    def __init__(self, project_id, instance_id, read_only=False, pool_size=8, table_prefix="mycdb",
                 credentials=None, metric_definition=None, event_definitions=None):
        self.project_id = project_id
        self.instance_id = instance_id
        self.read_only = read_only
        self.table_prefix = table_prefix
        self.credentials = credentials

        # self.credentials, project = google.auth.default()
        # credentials = service_account.Credentials.from_service_account_file('/path/to/key.json')
        bigtable_emu = os.environ.get('BIGTABLE_EMULATOR_HOST', None)
        if bigtable_emu:
            self.credentials = AnonymousCredentials()

        self.admin_instance = None
        self.current_tables = None
        self.instances = []
        self.pool_size = pool_size
        self.stores = {}

        self.metrics = []
        if metric_definition is not None:
            self.metrics += metric_definition

        self.event_definitions = []
        if event_definitions is not None:
            self.event_definitions += event_definitions

        # Register Default Data Stores
        from .stores import TimeSeriesStore
        self.timeseries = TimeSeriesStore(self)
        self.register_store(self.timeseries)
        from .stores import ActivityStore
        self.activity = ActivityStore(self)
        self.register_store(self.activity)
        from .stores import EventStore
        self.events = EventStore(self)
        self.register_store(self.events)
        from .stores import MetaDataStore
        self.metadata = MetaDataStore(self)
        self.register_store(self.metadata)

    def clone(self):
        return Connection(self.project_id, self.instance_id, read_only=self.read_only,
                          pool_size=self.pool_size, table_prefix=self.table_prefix,
                          credentials=self.credentials, metric_definition=self.metrics,
                          event_definitions=self.event_definitions)

    def create_instance(self, admin=False):
        return bigtable.Client(project=self.project_id, admin=admin,
                               credentials=self.credentials).instance(self.instance_id)

    def get_admin_instance(self):
        if self.read_only:
            raise RuntimeError("Cannot create admin instance in readonly mode")
        if self.admin_instance is None:
            self.admin_instance = self.create_instance(admin=True)
        return self.admin_instance

    def register_store(self, store):
        self.stores[store.STOREID] = store

    def get_current_tables(self, force_reload=False):
        if self.current_tables is None or force_reload:
            self.current_tables = self.get_admin_instance().list_tables()
        return self.current_tables

    def table_with_prefix(self, table_name):
        return "{}_{}".format(self.table_prefix, table_name)

    def create_tables(self, silent=False):
        for s in self.stores.values():
            s._create_tables(silent=silent)

    def create_all_metrics(self):
        self.timeseries._create_all_metrics()

    def create_metric(self, metric_name, silent=False):
        self.timeseries._create_metric(metric_name, silent=silent)

    def get_instance(self):
        if len(self.instances) < self.pool_size:
            con = self.create_instance(admin=False)
            logger.info("New Database Instance Connection created")
            self.instances.append(con)
            return con
        return random.choice(self.instances)

    # Table Access Methods
    def get_table(self, table_name):
        return Table(self.get_instance().table(self.table_with_prefix(table_name)))

    def timeseries_table(self):
        return Table(self.get_instance().table(self.table_with_prefix("timeseries")))

    def metadata_table(self):
        return Table(self.get_instance().table(self.table_with_prefix("metadata")))

    def events_table(self):
        return Table(self.get_instance().table(self.table_with_prefix("events")))

    def counter_table(self):
        return Table(self.get_instance().table(self.table_with_prefix("counter")))


    # Shared Methods
    def write_cell(self, table_id, row_id, column, value):
        t = self.get_table(table_id)
        return t.write_cell(row_id, column, value)

    def read_row(self, table_id, row_id):
        t = self.get_table(table_id)
        return t.read_row(row_id)


class Table(object):
    def __init__(self, low_level):
        self._low_level = low_level

    @classmethod
    def partial_row_to_ordered_dict(cls, row_data):
        result = OrderedDict()
        for column_family_id, columns in row_data._cells.items():
            for column_qual, cells in columns.items():
                key = _to_bytes(column_family_id) + b":" + _to_bytes(column_qual)
                result[key.decode("utf-8")] = cells[0].value
        return result

    @classmethod
    def partial_row_to_dict(cls, row_data):
        result = {}
        for cf, data in row_data.to_dict().items():
            result[cf.decode("utf-8")] = data[0].value
        return result

    def write_cell(self, row_id, column, value):
        row = self._low_level.row(row_id.encode("utf-8"))
        column_family, col = column.split(":", 1)
        row.set_cell(column_family.encode("utf-8"), col.encode("utf-8"), value)
        row.commit()
        return 1

    def read_row(self, row_id):
        res = self._low_level.read_row(row_id.encode("utf-8"), CellsColumnLimitFilter(1))
        if res is None:
            raise KeyError("row {} not found".format(row_id))
        return self.partial_row_to_dict(res)

    def delete_row(self, row_id, column_families=None):
        row = self._low_level.row(row_id.encode("utf-8"))
        if column_families is None:
            row.delete()
        else:
            for c in column_families:
                row.delete_cells(c.encode("utf-8"), row.ALL_COLUMNS)
        row.commit()
        return 1

    def upsert_rows(self, row_upserts):
        rows = []
        for r in row_upserts:
            row = self._low_level.row(r.row_key)
            for c, value in r.cells.items():
                column_family, col = c.split(":", 1)
                row.set_cell(column_family.encode("utf-8"), col.encode("utf-8"), value)
            rows.append(row)
        responses = self._low_level.mutate_rows(rows)
        for r in responses:
            if r.code != 0:
                raise ValueError("Bigtable upsert failed with: {} - {}".format(r.code, r.message))
        return responses

    def row_generator(self, row_keys=None, start_key=None, end_key=None,
                      column_families=None, check_prefix=None):
        if row_keys is None and start_key is None:
            raise ValueError("use row_keys or start_key parameter")
        if start_key is not None and (end_key is None and check_prefix is None):
            raise ValueError("use start_key together with end_key or check_prefix")

        filters = [CellsColumnLimitFilter(1)]
        if column_families is not None:
            c_filters = []
            for c in column_families:
                c_filters.append(FamilyNameRegexFilter(c))
            if len(c_filters) == 1:
                filters.append(c_filters[0])
            elif len(c_filters) > 1:
                filters.append(RowFilterUnion(c_filters))
        filter_ = RowFilterChain(filters=filters)

        row_set = RowSet()
        if row_keys:
            for r in row_keys:
                row_set.add_row_key(r)
        else:
            row_set.add_row_range_from_keys(start_key=start_key, end_key=end_key,
                                            start_inclusive=True, end_inclusive=True)

        generator = self._low_level.read_rows(filter_=filter_, row_set=row_set)

        i = -1
        for rowdata in generator:
            i += 1
            if rowdata is None:
                if row_keys:
                    yield (row_keys[i], {})
                continue
            rk = rowdata.row_key.decode("utf-8")
            if check_prefix:
                if not rk.startswith(check_prefix):
                    break
            curr_row_dict = self.partial_row_to_ordered_dict(rowdata)
            yield (rk, curr_row_dict)

    def get_first_row(self, row_key_prefix, column_families=None):
        filters = [CellsColumnLimitFilter(1)]
        if column_families is not None:
            c_filters = []
            for c in column_families:
                c_filters.append(FamilyNameRegexFilter(c))
            if len(c_filters) == 1:
                filters.append(c_filters[0])
            elif len(c_filters) > 1:
                filters.append(RowFilterUnion(c_filters))
        filter_ = RowFilterChain(filters=filters)

        row_set = RowSet()
        row_set.add_row_range_from_keys(start_key=row_key_prefix, start_inclusive=True)

        generator = self._low_level.read_rows(filter_=filter_, row_set=row_set)

        i = -1
        for rowdata in generator:
            i += 1
            # if rowdata is None:
            #     continue
            rk = rowdata.row_key.decode("utf-8")
            if not rk.startswith(row_key_prefix):
                break
            curr_row_dict = self.partial_row_to_dict(rowdata)
            return (rk, curr_row_dict)

    def read_rows(self, row_keys=None, start_key=None, end_key=None,
                  column_families=None):
        generator = self.row_generator(row_keys=row_keys, start_key=start_key, end_key=end_key,
                                       column_families=column_families)

        result = []
        for rk, data in generator:
            result.append((rk, data))
        return result

    # Taken from google-bigtable-happybase
    def increment_counter(self, row_id, column, value):
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
        row = self._low_level.row(row_id.encode("utf-8"), append=True)
        column_family_id, column_qualifier = column.split(':')
        row.increment_cell_value(column_family_id.encode("utf-8"),
                                 column_qualifier.encode("utf-8"), value)
        modified_cells = row.commit()

        inner_keys = list(modified_cells[column_family_id].keys())
        if not inner_keys:
            raise KeyError(column_qualifier)

        if isinstance(inner_keys[0], bytes):
            column_cells = modified_cells[
                column_family_id][column_qualifier.encode("latin-1")]
        elif isinstance(inner_keys[0], str):
            column_cells = modified_cells[
                column_family_id][column_qualifier]
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
