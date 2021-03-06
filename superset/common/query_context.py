# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import logging
from typing import Any, ClassVar, Dict, List, Optional, Union

import numpy as np
import pandas as pd
from flask_babel import _

from superset import app, db, is_feature_enabled
from superset.annotation_layers.dao import AnnotationLayerDAO
from superset.charts.dao import ChartDAO
from superset.common.query_actions import get_query_results
from superset.common.query_object import QueryObject
from superset.connectors.base.models import BaseDatasource
from superset.connectors.connector_registry import ConnectorRegistry
from superset.exceptions import (
    CacheLoadError,
    QueryObjectValidationError,
    SupersetException,
)
from superset.extensions import cache_manager, security_manager
from superset.stats_logger import BaseStatsLogger
from superset.utils.cache import generate_cache_key, set_and_log_cache
from superset.utils.core import (
    ChartDataResultFormat,
    ChartDataResultType,
    DatasourceDict,
    DTTM_ALIAS,
    error_msg_from_exception,
    get_column_names_from_metrics,
    get_stacktrace,
    normalize_dttm_col,
    QueryStatus,
)
from superset.views.utils import get_viz

config = app.config
stats_logger: BaseStatsLogger = config["STATS_LOGGER"]
logger = logging.getLogger(__name__)


class QueryContext:
    """
    The query context contains the query object and additional fields necessary
    to retrieve the data payload for a given viz.
    """

    cache_type: ClassVar[str] = "df"
    enforce_numerical_metrics: ClassVar[bool] = True

    datasource: BaseDatasource
    queries: List[QueryObject]
    force: bool
    custom_cache_timeout: Optional[int]
    result_type: ChartDataResultType
    result_format: ChartDataResultFormat

    # TODO: Type datasource and query_object dictionary with TypedDict when it becomes
    #  a vanilla python type https://github.com/python/mypy/issues/5288
    def __init__(  # pylint: disable=too-many-arguments
        self,
        datasource: DatasourceDict,
        queries: List[Dict[str, Any]],
        force: bool = False,
        custom_cache_timeout: Optional[int] = None,
        result_type: Optional[ChartDataResultType] = None,
        result_format: Optional[ChartDataResultFormat] = None,
    ) -> None:
        self.datasource = ConnectorRegistry.get_datasource(
            str(datasource["type"]), int(datasource["id"]), db.session
        )
        self.queries = [QueryObject(**query_obj) for query_obj in queries]
        self.force = force
        self.custom_cache_timeout = custom_cache_timeout
        self.result_type = result_type or ChartDataResultType.FULL
        self.result_format = result_format or ChartDataResultFormat.JSON
        self.cache_values = {
            "datasource": datasource,
            "queries": queries,
            "result_type": self.result_type,
            "result_format": self.result_format,
        }

    def get_query_result(self, query_object: QueryObject) -> Dict[str, Any]:
        """Returns a pandas dataframe based on the query object"""

        # Here, we assume that all the queries will use the same datasource, which is
        # a valid assumption for current setting. In the long term, we may
        # support multiple queries from different data sources.

        timestamp_format = None
        if self.datasource.type == "table":
            dttm_col = self.datasource.get_column(query_object.granularity)
            if dttm_col:
                timestamp_format = dttm_col.python_date_format

        # The datasource here can be different backend but the interface is common
        result = self.datasource.query(query_object.to_dict())

        df = result.df
        # Transform the timestamp we received from database to pandas supported
        # datetime format. If no python_date_format is specified, the pattern will
        # be considered as the default ISO date format
        # If the datetime format is unix, the parse will use the corresponding
        # parsing logic
        if not df.empty:
            normalize_dttm_col(
                df=df,
                timestamp_format=timestamp_format,
                offset=self.datasource.offset,
                time_shift=query_object.time_shift,
            )

            if self.enforce_numerical_metrics:
                self.df_metrics_to_num(df, query_object)

            df.replace([np.inf, -np.inf], np.nan, inplace=True)
            df = query_object.exec_post_processing(df)

        return {
            "query": result.query,
            "status": result.status,
            "error_message": result.error_message,
            "df": df,
        }

    @staticmethod
    def df_metrics_to_num(df: pd.DataFrame, query_object: QueryObject) -> None:
        """Converting metrics to numeric when pandas.read_sql cannot"""
        for col, dtype in df.dtypes.items():
            if dtype.type == np.object_ and col in query_object.metric_names:
                # soft-convert a metric column to numeric
                # will stay as strings if conversion fails
                df[col] = df[col].infer_objects()

    def get_data(self, df: pd.DataFrame,) -> Union[str, List[Dict[str, Any]]]:
        if self.result_format == ChartDataResultFormat.CSV:
            include_index = not isinstance(df.index, pd.RangeIndex)
            result = df.to_csv(index=include_index, **config["CSV_EXPORT"])
            return result or ""

        return df.to_dict(orient="records")

    def get_payload(
        self, cache_query_context: Optional[bool] = False, force_cached: bool = False,
    ) -> Dict[str, Any]:
        """Returns the query results with both metadata and data"""

        # Get all the payloads from the QueryObjects
        query_results = [
            get_query_results(
                query_obj.result_type or self.result_type, self, query_obj, force_cached
            )
            for query_obj in self.queries
        ]
        return_value = {"queries": query_results}

        if cache_query_context:
            cache_key = self.cache_key()
            set_and_log_cache(
                cache_manager.cache,
                cache_key,
                {"data": self.cache_values},
                self.cache_timeout,
            )
            return_value["cache_key"] = cache_key  # type: ignore

        return return_value

    @property
    def cache_timeout(self) -> int:
        if self.custom_cache_timeout is not None:
            return self.custom_cache_timeout
        if self.datasource.cache_timeout is not None:
            return self.datasource.cache_timeout
        if (
            hasattr(self.datasource, "database")
            and self.datasource.database.cache_timeout
        ) is not None:
            return self.datasource.database.cache_timeout
        return config["CACHE_DEFAULT_TIMEOUT"]

    def cache_key(self, **extra: Any) -> str:
        """
        The QueryContext cache key is made out of the key/values from
        self.cached_values, plus any other key/values in `extra`. It includes only data
        required to rehydrate a QueryContext object.
        """
        key_prefix = "qc-"
        cache_dict = self.cache_values.copy()
        cache_dict.update(extra)

        return generate_cache_key(cache_dict, key_prefix)

    def query_cache_key(self, query_obj: QueryObject, **kwargs: Any) -> Optional[str]:
        """
        Returns a QueryObject cache key for objects in self.queries
        """
        extra_cache_keys = self.datasource.get_extra_cache_keys(query_obj.to_dict())

        cache_key = (
            query_obj.cache_key(
                datasource=self.datasource.uid,
                extra_cache_keys=extra_cache_keys,
                rls=security_manager.get_rls_ids(self.datasource)
                if is_feature_enabled("ROW_LEVEL_SECURITY")
                and self.datasource.is_rls_supported
                else [],
                changed_on=self.datasource.changed_on,
                **kwargs,
            )
            if query_obj
            else None
        )
        return cache_key

    @staticmethod
    def get_native_annotation_data(query_obj: QueryObject) -> Dict[str, Any]:
        annotation_data = {}
        annotation_layers = [
            layer
            for layer in query_obj.annotation_layers
            if layer["sourceType"] == "NATIVE"
        ]
        layer_ids = [layer["value"] for layer in annotation_layers]
        layer_objects = {
            layer_object.id: layer_object
            for layer_object in AnnotationLayerDAO.find_by_ids(layer_ids)
        }

        # annotations
        for layer in annotation_layers:
            layer_id = layer["value"]
            layer_name = layer["name"]
            columns = [
                "start_dttm",
                "end_dttm",
                "short_descr",
                "long_descr",
                "json_metadata",
            ]
            layer_object = layer_objects[layer_id]
            records = [
                {column: getattr(annotation, column) for column in columns}
                for annotation in layer_object.annotation
            ]
            result = {"columns": columns, "records": records}
            annotation_data[layer_name] = result
        return annotation_data

    @staticmethod
    def get_viz_annotation_data(
        annotation_layer: Dict[str, Any], force: bool
    ) -> Dict[str, Any]:
        chart = ChartDAO.find_by_id(annotation_layer["value"])
        form_data = chart.form_data.copy()
        if not chart:
            raise QueryObjectValidationError(_("The chart does not exist"))
        try:
            viz_obj = get_viz(
                datasource_type=chart.datasource.type,
                datasource_id=chart.datasource.id,
                form_data=form_data,
                force=force,
            )
            payload = viz_obj.get_payload()
            return payload["data"]
        except SupersetException as ex:
            raise QueryObjectValidationError(error_msg_from_exception(ex))

    def get_annotation_data(self, query_obj: QueryObject) -> Dict[str, Any]:
        """

        :param query_obj:
        :return:
        """
        annotation_data: Dict[str, Any] = self.get_native_annotation_data(query_obj)
        for annotation_layer in [
            layer
            for layer in query_obj.annotation_layers
            if layer["sourceType"] in ("line", "table")
        ]:
            name = annotation_layer["name"]
            annotation_data[name] = self.get_viz_annotation_data(
                annotation_layer, self.force
            )
        return annotation_data

    def get_df_payload(  # pylint: disable=too-many-statements,too-many-locals
        self, query_obj: QueryObject, force_cached: Optional[bool] = False,
    ) -> Dict[str, Any]:
        """Handles caching around the df payload retrieval"""
        cache_key = self.query_cache_key(query_obj)
        logger.info("Cache key: %s", cache_key)
        is_loaded = False
        stacktrace = None
        df = pd.DataFrame()
        cache_value = None
        status = None
        query = ""
        annotation_data = {}
        error_message = None
        if cache_key and cache_manager.data_cache and not self.force:
            cache_value = cache_manager.data_cache.get(cache_key)
            if cache_value:
                stats_logger.incr("loading_from_cache")
                try:
                    df = cache_value["df"]
                    query = cache_value["query"]
                    annotation_data = cache_value.get("annotation_data", {})
                    status = QueryStatus.SUCCESS
                    is_loaded = True
                    stats_logger.incr("loaded_from_cache")
                except KeyError as ex:
                    logger.exception(ex)
                    logger.error(
                        "Error reading cache: %s", error_msg_from_exception(ex)
                    )
                logger.info("Serving from cache")

        if force_cached and not is_loaded:
            logger.warning(
                "force_cached (QueryContext): value not found for key %s", cache_key
            )
            raise CacheLoadError("Error loading data from cache")

        if query_obj and not is_loaded:
            try:
                invalid_columns = [
                    col
                    for col in query_obj.columns
                    + query_obj.groupby
                    + get_column_names_from_metrics(query_obj.metrics or [])
                    if col not in self.datasource.column_names and col != DTTM_ALIAS
                ]
                if invalid_columns:
                    raise QueryObjectValidationError(
                        _(
                            "Columns missing in datasource: %(invalid_columns)s",
                            invalid_columns=invalid_columns,
                        )
                    )
                query_result = self.get_query_result(query_obj)
                status = query_result["status"]
                query = query_result["query"]
                error_message = query_result["error_message"]
                df = query_result["df"]
                annotation_data = self.get_annotation_data(query_obj)

                if status != QueryStatus.FAILED:
                    stats_logger.incr("loaded_from_source")
                    if not self.force:
                        stats_logger.incr("loaded_from_source_without_force")
                    is_loaded = True
            except QueryObjectValidationError as ex:
                error_message = str(ex)
                status = QueryStatus.FAILED
            except Exception as ex:  # pylint: disable=broad-except
                logger.exception(ex)
                if not error_message:
                    error_message = str(ex)
                status = QueryStatus.FAILED
                stacktrace = get_stacktrace()

            if is_loaded and cache_key and status != QueryStatus.FAILED:
                set_and_log_cache(
                    cache_manager.data_cache,
                    cache_key,
                    {"df": df, "query": query, "annotation_data": annotation_data},
                    self.cache_timeout,
                    self.datasource.uid,
                )
        return {
            "cache_key": cache_key,
            "cached_dttm": cache_value["dttm"] if cache_value is not None else None,
            "cache_timeout": self.cache_timeout,
            "df": df,
            "annotation_data": annotation_data,
            "error": error_message,
            "is_cached": cache_value is not None,
            "query": query,
            "status": status,
            "stacktrace": stacktrace,
            "rowcount": len(df.index),
        }

    def raise_for_access(self) -> None:
        """
        Raise an exception if the user cannot access the resource.

        :raises SupersetSecurityException: If the user cannot access the resource
        """
        for query in self.queries:
            query.validate()
        security_manager.raise_for_access(query_context=self)
