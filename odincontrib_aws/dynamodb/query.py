import logging

from odin.compatibility import deprecated
from odin.fields import NOT_PROVIDED
from odin.resources import create_resource_from_dict

from odincontrib_aws.dynamodb.indexes import Index

logger = logging.getLogger('odincontrib_aws.dynamodb.query')


class QueryResult(object):
    """
    Result of a Query or Scan operation.
    """
    def __init__(self, query, result):
        self.query = query
        self._result = result

    def __len__(self):
        return self.count

    def __iter__(self):
        table = self.query.table
        for item in self.raw_results:
            yield create_resource_from_dict(item, table, copy_dict=False, full_clean=False)

    @property
    def raw_results(self):
        return self._result['Items']

    @property
    def count(self):
        return self._result['Count']

    @property
    def scanned(self):
        return self._result['ScannedCount']

    @property
    def consumed_capacity(self):
        return self._result['ConsumedCapacity']

    @property
    def last_evaluated_key(self):
        return self._result.get('LastEvaluatedKey')


class PagedQueryResult(object):
    """
    Batched results of a Query or Scan operation.

    This result set will make multiple queries to Dynamo DB to get each page of results.
    """
    def __init__(self, query):
        self.query = query

        self.pages = 0
        self.count = 0
        self.scanned = 0
        self.last_page = False

    def __iter__(self):
        query = self.query
        params = query._get_params().copy()

        while True:
            logger.info("Fetching page: %s", self.pages)

            results = QueryResult(query, query._command(**params))

            # Update stats
            self.pages += 1
            self.count += results.count
            self.scanned += results.scanned
            self.last_page = results.last_evaluated_key is None

            # Yield results
            for idx, result in enumerate(results):
                yield result

            # Determine if we are done or need to get the next page
            if self.last_page:
                logger.info("Returned %s of %s records.", results.count, self.count)
                break
            else:
                logger.info("Returned %s of %s records; continuing from: %s",
                            results.count, self.count, results.last_evaluated_key)
                params['ExclusiveStartKey'] = results.last_evaluated_key


class QueryBase(object):
    """
    Base of Query objects
    """
    def __init__(self, session, table_of_index):
        self.session = session

        if isinstance(table_of_index, Index):
            self.table = table_of_index.table
            self.index = table_of_index
        else:
            self.table = table_of_index
            self.index = None

        self._expression_attributes = {}
        self._params = {}
        self._command = None

    def __iter__(self):
        return iter(self.all())

    def _get_params(self):
        params = self._params
        params['TableName'] = self.table._meta.table_name(self.session)
        if self.index:
            params['IndexName'] = self.index.name
        return params

    def copy(self):
        """
        Copy the Query.
        """
        query = self.__class__(self.session, self.table)
        query._params = self._params.copy()
        return query

    def single(self):
        """
        Execute operation and return a single page only.
        """
        result = self._command(**self._get_params())
        return QueryResult(self, result)

    def all(self):
        """
        Execute operation and return result object
        """
        return PagedQueryResult(self)

    def params(self, **params):
        """
        Apply params that you would execute.
        """
        self._params.update(params)

    @deprecated("Use either a GlobalIndex or LocalIndex class to defined indexes.")
    def index(self, name):
        """
        The name of a secondary index to scan. This index can be any local
        secondary index or global secondary index.
        """
        self._params['IndexName'] = name
        return self

    def limit(self, value):
        """
        The maximum number of items to evaluate (not necessarily the number of
        matching items). If DynamoDB processes the number of items up to the
        limit while processing the results, it stops the operation and returns
        the matching values up to that point, and a key in LastEvaluatedKey to
        apply in a subsequent operation, so that you can pick up where you
        left off.
        """
        self._params['Limit'] = value
        return self

    def select(self, value='ALL_ATTRIBUTES'):
        """
        The attributes to be returned in the result. You can retrieve all item
        attributes, specific item attributes, or the count of matching items.

        - ``ALL_ATTRIBUTES`` - Returns all of the item attributes.
        - ``ALL_PROJECTED_ATTRIBUTES`` - Allowed only when querying an index.
            Retrieves all attributes that have been projected into the index.
            If the index is configured to project all attributes, this return
            value is equivalent to specifying ``ALL_ATTRIBUTES``.
        - ``COUNT`` - Returns the number of matching items, rather than the
            matching items themselves.
        - ``SPECIFIC_ATTRIBUTES`` - Returns only the attributes listed in
            AttributesToGet . This return value is equivalent to specifying
            AttributesToGet without specifying any value for Select .

        If neither Select nor AttributesToGet are specified, DynamoDB
        defaults to ``ALL_ATTRIBUTES``. You cannot use both AttributesToGet
        and Select together in a single request, unless the value for Select
        is ``SPECIFIC_ATTRIBUTES``. (This usage is equivalent to specifying
        AttributesToGet without any value for Select.)
        """
        assert value in ('ALL_ATTRIBUTES', 'ALL_PROJECTED_ATTRIBUTES', 'COUNT', 'SPECIFIC_ATTRIBUTES')

        self._params['Select'] = value
        return self

    def consumed_capacity(self, value='TOTAL'):
        """
        Determines the level of detail about provisioned throughput
        consumption that is returned in the response:

        - ``INDEXES`` - The response includes the aggregate ConsumedCapacity
            for the operation, together with ConsumedCapacity for each table
            and secondary index that was accessed. Note that some operations,
            such as GetItem and BatchGetItem , do not access any indexes at
            all. In these cases, specifying INDEXES will only return
            ConsumedCapacity information for table(s).
        - ``TOTAL`` - The response includes only the aggregate
            ConsumedCapacity for the operation.
        - ``NONE`` - No ConsumedCapacity details are included in the response.
        """
        assert value in ('INDEXES', 'TOTAL', 'NONE')

        self._params['ReturnConsumedCapacity'] = value
        return self


class Scan(QueryBase):
    """
    Perform a scan operation against a table.
    """
    def __init__(self, *args):
        super(Scan, self).__init__(*args)
        self._command = self.session.client.scan


class Query(QueryBase):
    """
    perform a query operation against a table.
    """
    def __init__(self, hash_value, *args):
        super(Query, self).__init__(*args)
        self.hash_value = hash_value
        self._range_value = NOT_PROVIDED

        self._command = self.session.client.query

    def _get_params(self):
        params = super(Query, self)._get_params()

        # Define Key conditions
        key_fields = self.table._meta.key_fields
        key_values = (self.hash_value, self._range_value)
        params['KeyConditions'] = {
            f.name: {'AttributeValueList': [f.prepare_dynamo(v)]}
            for f, v in zip(key_fields, key_values) if v is not NOT_PROVIDED
        }

        return params

    def range(self, value):
        """
        Specify the range value.
        """
        self._range_value = value
        return self