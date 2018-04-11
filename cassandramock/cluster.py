import sqlite3
import uuid
import re

from cassandra import InvalidRequest
from .statements import PreparedStatement

class Future(object):

    def __init__(self, result):
        self._result = result

    def result(self):
        return [element for element in self._result] if self._result else []

    def add_callbacks(self, callback=None, errback=None, callback_args=(),
                      callback_kwargs={}):
        try:
            callback(self.result(), **callback_kwargs)
        except Exception as ex:
            errback(ex)

class Session(object):

    def __init__(self, conn, keyspace):

        self.conn = conn
        self.keyspace = keyspace
        self.mappings = {}
        self.tokens = ['AND', 'OR']
        # regex to find the db.table_name
        self.insert_stmt_regex = re.compile('^INSERT INTO (?P<dbname>([\w"]+\.)?[\w"]+)*', re.I)
        self.create_stmt_regex = re.compile('^CREATE TABLE (?P<dbname>([\w"]+\.)?[\w"]+)*', re.I)
        #self.select_stmt_regex = re.COMPILE
        
    def execute(self, query, parameters=None, **kwargs):
        # Health check.
        is_query_prepared = False
        if isinstance(query, PreparedStatement) and parameters:
            query = query.bind(parameters)
            parameters = None
            is_query_prepared = True
            
        if 'system.local' in query:
            return 'true'

        original_query = query
        res = None
        query = query.upper()
        query = query.replace('FALSE', '0')
        query = query.replace('TRUE', '1')

        if isinstance(parameters, (tuple, list)) and not is_query_prepared:

            # convert UUID to string
            parameters = tuple([str(s) if isinstance(s, uuid.UUID)
                           else s for s in parameters])

            # sqlite prefers ? over %s for positional args
            query = query.replace('%S', '?')

        elif isinstance(parameters, dict):

            # If the user passed dictionary arguments, assume that they
            # used that cassandra %(fieldname)s and convert to sqlite's
            # :fieldname

            for k, v in parameters.items():
                cass_style_arg = "%({0})S".format(k)
                sqlite_style_arg = ":{0}".format(k)
                query = query.replace(cass_style_arg, sqlite_style_arg)

                # Convert UUID parameters to strings
                if isinstance(v, uuid.UUID):
                    parameters[k] = str(v)

        elif parameters == None:
            pass

        if "JOIN" in query.strip():
            raise InvalidRequest("Cassandra doesn't support JOINS")

        if query.strip().startswith("INSERT"):
            # It's all upserts in Cassandra
            k = re.match(self.insert_stmt_regex, query)
            if k:
                dbname = k.group('dbname')
                if '.' in dbname:
                    keyspace, table = dbname.split('.')
                    query = query.replace(dbname, table)
            query = query.replace("INSERT", "INSERT OR REPLACE")

        if query.strip().startswith("CREATE TABLE"):
            # create a mapping of table_name and associated primary key
            k = re.match(self.create_stmt_regex, query)
            if k:
                dbname = k.group('dbname')
                if '.' in dbname:
                    keyspace, table = dbname.split('.')
                    query = query.replace(dbname, table)
            cluster_key = False
            table_name = query.split()[2][:-1]
            self.mappings[table_name] = {}
            primary_key_present = query.rfind('PRIMARY KEY')
            if primary_key_present == -1:
                raise Exception('Primary key not present for {0}'.format(table_name))
            primary_key_builder = query.rsplit('PRIMARY KEY')[1].strip()[1:][:-4]
            if ('('  in primary_key_builder) and (')' in primary_key_builder):
                cluster_key = True

            primary_key_builder = primary_key_builder.replace('(','')
            primary_key_builder = primary_key_builder.replace(')','')
            primary_key_builder = primary_key_builder.replace(',','')

            all_keys = primary_key_builder.split()
            if cluster_key:
                self.mappings[table_name]['primary'] = all_keys[:-1]
                self.mappings[table_name]['clustering'] = all_keys[-1:]
            else:
                self.mappings[table_name]['primary'] = all_keys

            self.mappings[table_name]['index'] = None


        if query.strip().startswith("CREATE INDEX"):
            # create a mapping of table_name and associated index key
            index_builder = query.strip().split('ON')[1].strip()[:-1]
            index_builder = index_builder.replace('(','')
            index_builder = index_builder.replace(')','')
            table_name , index_key = index_builder.split()

            self.mappings[table_name]['index'] = index_key

        if query.strip().startswith("SELECT"):

            # when querying with a where clause, the primary key must be supplied
            prim_count = 0
            prim_keys_present = []
            index_count = 0
            index_keys_present = False
            where_clause_present = query.rfind('WHERE')

            if where_clause_present == -1:

                 if ',' in query.rsplit('FROM')[1].strip():
                     raise InvalidRequest("Cassandra doesn't support JOINS")
            else:
                table_name = query.rsplit('FROM')[1].rsplit("WHERE")[0].strip()

                if ',' in table_name:
                    raise InvalidRequest("Cassandra doesn't support JOINS")

                query_builder =  query.strip().rsplit("WHERE")[1].split()

                for token in self.tokens:
                    try:
                        query_builder.remove(token)
                    except ValueError:
                        pass

                for key in query_builder:
                    for prim_key in self.mappings[table_name]['primary']:
                        if key.startswith(prim_key):
                            prim_keys_present.append(prim_key)
                            prim_count+=1

                if self.mappings[table_name]['index']:
                    for key in query_builder:
                        for index_key in self.mappings[table_name]['index']:
                            if key.startswith(index_key):
                                index_keys_present = True
                                index_count+=1

                if not index_keys_present:
                    missed_prim_keys = set(self.mappings[table_name]['primary']) - set(prim_keys_present)
                    if missed_prim_keys != set():
                        raise InvalidRequest('Primary key(s): {0} are missing from where clause'.format(missed_prim_keys))
                    else:
                        if len(query_builder) > prim_count:
                            raise InvalidRequest('Non primary key present in where clause')
                else:
                    if prim_count == 0:
                        pass
                    else:
                        raise InvalidRequest('Query will require explicit filtering')

        res = {}
        
        if not parameters:
            res = self.conn.execute(query)
        else:
            res = self.conn.execute(query, parameters)
        res = list(res)

        return MockResultSet(res)


    def prepare(self, query, custom_payload=None):
        return PreparedStatement(query)

    def execute_async(self, query, parameters, **kwargs):

        res = self.execute(query, parameters, **kwargs)

        return Future(res)

class MockResultSet(object):
    def __init__(self, results):
        self.results = results
        self.current_rows = 1 if results else None

    def __iter__(self):
        return iter(self.results)

    def __repr__(self):
        return "[%s]" % ' '.join(repr(el) for el in self.results)


class Cluster(object):

    def __init__(self, contact_points=None, auth_provider=None, ssl_options=None):
        self.cluster_contact_points = contact_points

    @property
    def contact_points(self):
        return self.cluster_contact_points

    def connect(self, keyspace):
        self._sqliteconn = sqlite3.connect(':memory:')
        return Session(self._sqliteconn, keyspace)
