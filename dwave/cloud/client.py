# Copyright 2017 D-Wave Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
D-Wave API clients handle communications with :term:`solver` resources: problem submittal,
monitoring, samples retrieval, etc.

Examples:
    This example creates a client using the local system's default D-Wave Cloud Client
    configuration file, which is configured to access a D-Wave 2000Q QPU, submits
    a :term:`QUBO` problem (a Boolean NOT gate represented by a penalty model), and
    samples 5 times.

    >>> from dwave.cloud import Client
    >>> Q = {(0, 0): -1, (0, 4): 0, (4, 0): 2, (4, 4): -1}
    >>> with Client.from_config() as client:  # doctest: +SKIP
    ...     solver = client.get_solver()
    ...     computation = solver.sample_qubo(Q, num_reads=5)
    ...
    >>> for i in range(5):     # doctest: +SKIP
    ...     print(computation.samples[i][0], computation.samples[i][4])
    ...
    (1, 0)
    (1, 0)
    (0, 1)
    (0, 1)
    (0, 1)

"""

from __future__ import division, absolute_import

import re
import sys
import time
import json
import logging
import threading
import posixpath
import requests
import warnings
import operator
import collections
from itertools import chain
from functools import partial, wraps

from dateutil.parser import parse as parse_datetime
from plucky import pluck
from six.moves import queue, range
import six

from dwave.cloud.package_info import __packagename__, __version__
from dwave.cloud.exceptions import *
from dwave.cloud.config import load_config, legacy_load_config, parse_float
from dwave.cloud.solver import Solver, available_solvers
from dwave.cloud.utils import (
    datetime_to_timestamp, utcnow, TimeoutingHTTPAdapter, user_agent,
    epochnow, cached)

__all__ = ['Client']

logger = logging.getLogger(__name__)


class Client(object):
    """
    Base client class for all D-Wave API clients. Used by QPU and software :term:`sampler`
    classes.

    Manages workers and handles thread pools for submitting problems, cancelling tasks,
    polling problem status, and retrieving results.

    Args:
        endpoint (str):
            D-Wave API endpoint URL.

        token (str):
            Authentication token for the D-Wave API.

        solver (dict/str):
            Default solver features (or simply solver name).

        proxy (str):
            Proxy URL to be used for accessing the D-Wave API.

        permissive_ssl (bool, default=False):
            Disables SSL verification.

        request_timeout (float, default=60):
            Connect and read timeout (in seconds) for all requests to the D-Wave API.

        polling_timeout (float, default=None):
            Problem status polling timeout (in seconds), after which polling is aborted.

        connection_close (bool, default=False):
            Force HTTP(S) connection close after each request.

    Other Parameters:
        Unrecognized keys (str):
            All unrecognized keys are passed through to the appropriate client class constructor
            as string keyword arguments.

            An explicit key value overrides an identical user-defined key value loaded from a
            configuration file.

    Examples:
        This example directly initializes a :class:`~dwave.cloud.client.Client`.
        Direct initialization uses class constructor arguments, the minimum being
        a value for `token`.

        >>> from dwave.cloud import Client
        >>> client = Client(token='secret')
        >>> # code that uses client
        >>> client.close()


    """

    # The status flags that a problem can have
    STATUS_IN_PROGRESS = 'IN_PROGRESS'
    STATUS_PENDING = 'PENDING'
    STATUS_COMPLETE = 'COMPLETED'
    STATUS_FAILED = 'FAILED'
    STATUS_CANCELLED = 'CANCELLED'

    # Default API endpoint
    DEFAULT_API_ENDPOINT = 'https://cloud.dwavesys.com/sapi'

    # Cases when multiple status flags qualify
    ANY_STATUS_ONGOING = [STATUS_IN_PROGRESS, STATUS_PENDING]
    ANY_STATUS_NO_RESULT = [STATUS_FAILED, STATUS_CANCELLED]

    # Number of problems to include in a submit/status query
    _SUBMIT_BATCH_SIZE = 20
    _STATUS_QUERY_SIZE = 100

    # Number of worker threads for each problem processing task
    _SUBMISSION_THREAD_COUNT = 5
    _CANCEL_THREAD_COUNT = 1
    _POLL_THREAD_COUNT = 2
    _LOAD_THREAD_COUNT = 5

    # Poll back-off interval [sec]
    _POLL_BACKOFF_MIN = 1
    _POLL_BACKOFF_MAX = 60

    # Tolerance for server-client clocks difference (approx) [sec]
    _CLOCK_DIFF_MAX = 1

    # Poll grouping time frame; two scheduled polls are grouped if closer than [sec]:
    _POLL_GROUP_TIMEFRAME = 2

    # Downloaded solver definition cache maxage [sec]
    _SOLVERS_CACHE_MAXAGE = 300

    @classmethod
    def from_config(cls, config_file=None, profile=None, client=None,
                    endpoint=None, token=None, solver=None, proxy=None,
                    legacy_config_fallback=False, **kwargs):
        """Client factory method to instantiate a client instance from configuration.

        Configuration values can be specified in multiple ways, ranked in the following
        order (with 1 the highest ranked):

        1. Values specified as keyword arguments in :func:`from_config()`
        2. Values specified as environment variables
        3. Values specified in the configuration file

        Configuration-file format is described in :mod:`dwave.cloud.config`.

        If the location of the configuration file is not specified, auto-detection
        searches for existing configuration files in the standard directories
        of :func:`get_configfile_paths`.

        If a configuration file explicitly specified, via an argument or
        environment variable, does not exist or is unreadable, loading fails with
        :exc:`~dwave.cloud.exceptions.ConfigFileReadError`. Loading fails
        with :exc:`~dwave.cloud.exceptions.ConfigFileParseError` if the file is
        readable but invalid as a configuration file.

        Similarly, if a profile explicitly specified, via an argument or
        environment variable, is not present in the loaded configuration, loading fails
        with :exc:`ValueError`. Explicit profile selection also fails if the configuration
        file is not explicitly specified, detected on the system, or defined via
        an environment variable.

        Environment variables: ``DWAVE_CONFIG_FILE``, ``DWAVE_PROFILE``, ``DWAVE_API_CLIENT``,
        ``DWAVE_API_ENDPOINT``, ``DWAVE_API_TOKEN``, ``DWAVE_API_SOLVER``, ``DWAVE_API_PROXY``.

        Environment variables are described in :mod:`dwave.cloud.config`.

        Args:
            config_file (str/[str]/None/False/True, default=None):
                Path to configuration file.

                If ``None``, the value is taken from ``DWAVE_CONFIG_FILE`` environment
                variable if defined. If the environment variable is undefined or empty,
                auto-detection searches for existing configuration files in the standard
                directories of :func:`get_configfile_paths`.

                If ``False``, loading from file is skipped; if ``True``, forces auto-detection
                (regardless of the ``DWAVE_CONFIG_FILE`` environment variable).

            profile (str, default=None):
                Profile name (name of the profile section in the configuration file).

                If undefined, inferred from ``DWAVE_PROFILE`` environment variable if
                defined. If the environment variable is undefined or empty, a profile is
                selected in the following order:

                1. From the default section if it includes a profile key.
                2. The first section (after the default section).
                3. If no other section is defined besides ``[defaults]``, the defaults
                   section is promoted and selected.

            client (str, default=None):
                Client type used for accessing the API. Supported values are ``qpu``
                for :class:`dwave.cloud.qpu.Client` and ``sw`` for
                :class:`dwave.cloud.sw.Client`.

            endpoint (str, default=None):
                API endpoint URL.

            token (str, default=None):
                API authorization token.

            solver (dict/str, default=None):
                Default :term:`solver` features to use in :meth:`~dwave.cloud.client.Client.get_solver`.

                Defined via dictionary of solver feature constraints
                (see :meth:`~dwave.cloud.client.Client.get_solvers`).
                For backward compatibility, a solver name, as a string,
                is also accepted and converted to ``{"name": <solver name>}``.

                If undefined, :meth:`~dwave.cloud.client.Client.get_solver` uses a
                solver definition from environment variables, a configuration file, or
                falls back to the first available online solver.

            proxy (str, default=None):
                URL for proxy to use in connections to D-Wave API. Can include
                username/password, port, scheme, etc. If undefined, client
                uses the system-level proxy, if defined, or connects directly to the API.

            legacy_config_fallback (bool, default=False):
                If True and loading from a standard D-Wave Cloud Client configuration
                file (``dwave.conf``) fails, tries loading a legacy configuration file (``~/.dwrc``).

        Other Parameters:
            Unrecognized keys (str):
                All unrecognized keys are passed through to the appropriate client class constructor
                as string keyword arguments.

                An explicit key value overrides an identical user-defined key value loaded from a
                configuration file.

        Returns:
            :class:`~dwave.cloud.client.Client` (:class:`dwave.cloud.qpu.Client` or :class:`dwave.cloud.sw.Client`, default=:class:`dwave.cloud.qpu.Client`):
                Appropriate instance of a QPU or software client.

        Raises:
            :exc:`~dwave.cloud.exceptions.ConfigFileReadError`:
                Config file specified or detected could not be opened or read.

            :exc:`~dwave.cloud.exceptions.ConfigFileParseError`:
                Config file parse failed.

        Examples:

            A variety of examples are given in :mod:`dwave.cloud.config`.

            This example initializes :class:`~dwave.cloud.client.Client` from an
            explicitly specified configuration file, "~/jane/my_path_to_config/my_cloud_conf.conf"::

            >>> from dwave.cloud import Client
            >>> client = Client.from_config(config_file='~/jane/my_path_to_config/my_cloud_conf.conf')  # doctest: +SKIP
            >>> # code that uses client
            >>> client.close()

        """

        # try loading configuration from a preferred new config subsystem
        # (`./dwave.conf`, `~/.config/dwave/dwave.conf`, etc)
        config = load_config(
            config_file=config_file, profile=profile, client=client,
            endpoint=endpoint, token=token, solver=solver, proxy=proxy)
        logger.debug("Config loaded: %r", config)

        # fallback to legacy `.dwrc` if key variables missing
        if legacy_config_fallback:
            warnings.warn("'legacy_config_fallback' is deprecated, please convert "
                          "your legacy .dwrc file to the new config format.", DeprecationWarning)

            if not config.get('token'):
                config = legacy_load_config(
                    profile=profile, client=client,
                    endpoint=endpoint, token=token, solver=solver, proxy=proxy)
                logger.debug("Legacy config loaded: %r", config)

        # manual override of other (client-custom) arguments
        config.update(kwargs)

        from dwave.cloud import qpu, sw
        _clients = {'qpu': qpu.Client, 'sw': sw.Client, 'base': cls}
        _client = config.pop('client', None) or 'base'

        logger.debug("Final config used for %s.Client(): %r", _client, config)
        return _clients[_client](**config)

    def __init__(self, endpoint=None, token=None, solver=None, proxy=None,
                 permissive_ssl=False, request_timeout=60, polling_timeout=None,
                 connection_close=False, **kwargs):
        """To setup the connection a pipeline of queues/workers is constructed.

        There are five interactions with the server the connection manages:
        1. Downloading solver information.
        2. Submitting problem data.
        3. Polling problem status.
        4. Downloading problem results.
        5. Canceling problems

        Loading solver information is done synchronously. The other four tasks
        are performed by asynchronously workers. For 2, 3, and 5 the workers
        gather tasks in batches.
        """
        if not endpoint:
            endpoint = self.DEFAULT_API_ENDPOINT

        if not token:
            raise ValueError("API token not defined")

        logger.debug(
            "Creating a client for (endpoint=%r, token=%r, solver=%r, proxy=%r, "
            "permissive_ssl=%r, request_timeout=%r, polling_timeout=%r, **kwargs=%r)",
            endpoint, token, solver, proxy, permissive_ssl, request_timeout, polling_timeout, kwargs
        )

        if not solver:
            solver_def = {}

        elif isinstance(solver, collections.Mapping):
            solver_def = solver

        elif isinstance(solver, six.string_types):
            # support features dict encoded as JSON in our config INI file
            # TODO: push this decoding to the config module, once we switch to a
            #       richer config format (JSON or YAML)
            try:
                solver_def = json.loads(solver)
            except Exception:
                # unparseable json, assume string name for solver
                # we'll deprecate this eventually, but for now just convert it to
                # features dict (equality constraint on full solver name)
                logger.debug("Invalid solver JSON, assuming string name: %r", solver)
                solver_def = dict(name__eq=solver)

        else:
            raise ValueError("Expecting a features dictionary or a string name for 'solver'")

        self.endpoint = endpoint
        self.token = token
        self.default_solver = solver_def
        self.request_timeout = parse_float(request_timeout)
        self.polling_timeout = parse_float(polling_timeout)

        # Create a :mod:`requests` session. `requests` will manage our url parsing, https, etc.
        self.session = requests.Session()
        self.session.mount('http://', TimeoutingHTTPAdapter(timeout=self.request_timeout))
        self.session.mount('https://', TimeoutingHTTPAdapter(timeout=self.request_timeout))
        self.session.headers.update({'X-Auth-Token': self.token,
                                     'User-Agent': user_agent(__packagename__, __version__)})
        self.session.proxies = {'http': proxy, 'https': proxy}
        if permissive_ssl:
            self.session.verify = False
        if connection_close:
            self.session.headers.update({'Connection': 'close'})

        # Debug-log headers
        logger.debug("session.headers=%r", self.session.headers)

        # Build the problem submission queue, start its workers
        self._submission_queue = queue.Queue()
        self._submission_workers = []
        for _ in range(self._SUBMISSION_THREAD_COUNT):
            worker = threading.Thread(target=self._do_submit_problems)
            worker.daemon = True
            worker.start()
            self._submission_workers.append(worker)

        # Build the cancel problem queue, start its workers
        self._cancel_queue = queue.Queue()
        self._cancel_workers = []
        for _ in range(self._CANCEL_THREAD_COUNT):
            worker = threading.Thread(target=self._do_cancel_problems)
            worker.daemon = True
            worker.start()
            self._cancel_workers.append(worker)

        # Build the problem status polling queue, start its workers
        self._poll_queue = queue.PriorityQueue()
        self._poll_workers = []
        for _ in range(self._POLL_THREAD_COUNT):
            worker = threading.Thread(target=self._do_poll_problems)
            worker.daemon = True
            worker.start()
            self._poll_workers.append(worker)

        # Build the result loading queue, start its workers
        self._load_queue = queue.Queue()
        self._load_workers = []
        for _ in range(self._LOAD_THREAD_COUNT):
            worker = threading.Thread(target=self._do_load_results)
            worker.daemon = True
            worker.start()
            self._load_workers.append(worker)

    def close(self):
        """Perform a clean shutdown.

        Waits for all the currently scheduled work to finish, kills the workers,
        and closes the connection pool.

        .. note:: Ensure your code does not submit new work while the connection is closing.

        Where possible, it is recommended you use a context manager (a :code:`with Client.from_config(...) as`
        construct) to ensure your code properly closes all resources.

        Examples:
            This example creates a client (based on an auto-detected configuration file), executes
            some code (represented by a placeholder comment), and then closes the client.

            >>> from dwave.cloud import Client
            >>> client = Client.from_config()
            >>> # code that uses client
            >>> client.close()

        """
        # Finish all the work that requires the connection
        logger.debug("Joining submission queue")
        self._submission_queue.join()
        logger.debug("Joining cancel queue")
        self._cancel_queue.join()
        logger.debug("Joining poll queue")
        self._poll_queue.join()
        logger.debug("Joining load queue")
        self._load_queue.join()

        # Send kill-task to all worker threads
        # Note: threads can't be 'killed' in Python, they have to die by
        # natural causes
        for _ in self._submission_workers:
            self._submission_queue.put(None)
        for _ in self._cancel_workers:
            self._cancel_queue.put(None)
        for _ in self._poll_workers:
            self._poll_queue.put((-1, None))
        for _ in self._load_workers:
            self._load_queue.put(None)

        # Wait for threads to die
        for worker in chain(self._submission_workers, self._cancel_workers,
                            self._poll_workers, self._load_workers):
            worker.join()

        # Close the requests session
        self.session.close()

    def __enter__(self):
        """Let connections be used in with blocks."""
        return self

    def __exit__(self, *args):
        """At the end of a with block perform a clean shutdown of the connection."""
        self.close()
        return False

    @staticmethod
    def is_solver_handled(solver):
        """Determine if the specified solver should be handled by this client.

        Default implementation accepts all solvers (always returns True). Override this
        predicate function with a subclass if you want to specialize your client for a
        particular type of solvers.

        Examples:
            This function accepts only solvers named "My_Solver_*".

            .. code:: python

                @staticmethod
                def is_solver_handled(solver):
                    return solver and solver.id.startswith('My_Solver_')

        """
        return True

    @cached(maxage=_SOLVERS_CACHE_MAXAGE)
    def _fetch_solvers(self, name=None):
        if name is not None:
            logger.debug("Fetching definition of a solver with name=%r", name)
            url = posixpath.join(self.endpoint, 'solvers/remote/{}/'.format(name))
        else:
            logger.debug("Fetching definitions of all available solvers")
            url = posixpath.join(self.endpoint, 'solvers/remote/')

        try:
            response = self.session.get(url)
        except requests.exceptions.Timeout:
            raise RequestTimeout

        if response.status_code == 401:
            raise SolverAuthenticationError

        if name is not None and response.status_code == 404:
            raise SolverNotFoundError("No solver with name={!r} available".format(name))

        response.raise_for_status()
        data = response.json()

        if name is not None:
            data = [data]

        logger.debug("Received solver data for %d solver(s).", len(data))
        logger.trace("Solver data received for solver name=%r: %r", name, data)

        solvers = []
        for solver_desc in data:
            for solver_class in available_solvers:
                try:
                    solver = solver_class(self, solver_desc)
                    if self.is_solver_handled(solver):
                        solvers.append(solver)
                        logger.debug("Adding solver %r", solver)
                        break
                    else:
                        logger.debug("Skipping solver %r (not handled by this client)", solver)

                except UnsupportedSolverError as e:
                    logger.debug("Skipping solver due to %r", e)

            # propagate all other/decoding errors, like InvalidAPIResponseError, etc.

        return solvers

    def get_solvers(self, refresh=False, order_by='avg_load', **filters):
        """Return a filtered list of solvers handled by this client.

        Args:
            refresh (bool, default=False):
                Force refresh of cached list of solvers/properties.

            order_by (callable/str/None, default='avg_load'):
                Solver sorting key function (or :class:`Solver` attribute/item
                dot-separated path). By default, solvers are sorted by average
                load. To explicitly not sort the solvers (and use the API-returned
                order), set ``order_by=None``.

                Signature of the `key` `callable` is::

                    key :: (Solver s, Ord k) => s -> k

                Basic structure of the `key` string path is::

                    "-"? (attr|item) ( "." (attr|item) )*

                For example, to use solver property named ``max_anneal_schedule_points``,
                available in ``Solver.properties`` dict, you can either specify a
                callable `key`::

                    key=lambda solver: solver.properties['max_anneal_schedule_points']

                or, you can use a short string path based key::

                    key='properties.max_anneal_schedule_points'

                Solver derived properties, available as :class:`Solver` properties
                can also be used (e.g. ``num_active_qubits``, ``online``,
                ``avg_load``, etc).

                Ascending sort order is implied, unless the key string path does
                not start with ``-``, in which case descending sort is used.

                Note: the sort used for ordering solvers by `key` is **stable**,
                meaning that if multiple solvers have the same value for the
                key, their relative order is preserved, and effectively they are
                in the same order as returned by the API.

                Note: solvers with ``None`` for key appear last in the list of
                solvers. When providing a key callable, ensure all values returned
                are of the same type (particularly in Python 3). For solvers with
                undefined key value, return ``None``.

            **filters:
                See `Filtering forms` and `Operators` below.

        Solver filters are defined, similarly to Django QuerySet filters, with
        keyword arguments of form `<key1>__...__<keyN>[__<operator>]=<value>`.
        Each `<operator>` is a predicate (boolean) function that acts on two
        arguments: value of feature `<name>` (described with keys path
        `<key1.key2...keyN>`) and the required `<value>`.

        Feature `<name>` can be:

        1) a derived solver property, available as an identically named
           :class:`Solver`'s property (`name`, `qpu`, `software`, `online`,
           `num_active_qubits`, `avg_load`)
        2) a solver parameter, available in :obj:`Solver.parameters`
        3) a solver property, available in :obj:`Solver.properties`
        4) a path describing a property in nested dictionaries

        Filtering forms are:

        * <derived_property>__<operator> (object <value>)
        * <derived_property> (bool)

          This form ensures the value of solver's property bound to `derived_property`,
          after applying `operator` equals the `value`. The default operator is `eq`.

          For example::

            >>> client.get_solvers(avg_load__gt=0.5)

          but also::

            >>> client.get_solvers(online=True)
            >>> # identical to:
            >>> client.get_solvers(online__eq=True)

        * <parameter>__<operator> (object <value>)
        * <parameter> (bool)

          This form ensures that the solver supports `parameter`. General operator form can
          be used but usually does not make sense for parameters, since values are human-readable
          descriptions. The default operator is `available`.

          Example::

            >>> client.get_solvers(flux_biases=True)
            >>> # identical to:
            >>> client.get_solvers(flux_biases__available=True)

        * <property>__<operator> (object <value>)
        * <property> (bool)

          This form ensures the value of the solver's `property`, after applying `operator`
          equals the righthand side `value`. The default operator is `eq`.

        Note: if a non-existing parameter/property name/key given, the default operator is `eq`.

        Operators are:

        * `available` (<name>: str, <value>: bool):
            Test availability of <name> feature.
        * `eq`, `lt`, `lte`, `gt`, `gte` (<name>: str, <value>: any):
            Standard relational operators that compare feature <name> value with <value>.
        * `regex` (<name>: str, <value>: str):
            Test regular expression matching feature value.
        * `covers` (<name>: str, <value>: single value or range expressed as 2-tuple/list):
            Test feature <name> value (which should be a *range*) covers a given value or a subrange.
        * `within` (<name>: str, <value>: range expressed as 2-tuple/list):
            Test feature <name> value (which can be a *single value* or a *range*) is within a given range.
        * `in` (<name>: str, <value>: container type):
            Test feature <name> value is *in* <value> container.
        * `contains` (<name>: str, <value>: any):
            Test feature <name> value (container type) *contains* <value>.
        * `issubset` (<name>: str, <value>: container type):
            Test feature <name> value (container type) is a subset of <value>.
        * `issuperset` (<name>: str, <value>: container type):
            Test feature <name> value (container type) is a superset of <value>.

        Derived properies are:

        * `name` (str): Solver name/id.
        * `qpu` (bool): Is solver QPU based?
        * `software` (bool): Is solver software based?
        * `online` (bool, default=True): Is solver online?
        * `num_active_qubits` (int): Number of active qubits. Less then or equal to `num_qubits`.
        * `avg_load` (float): Solver's average load (similar to Unix load average).

        Common solver parameters are:

        * `flux_biases`: Should solver accept flux biases?
        * `anneal_schedule`: Should solver accept anneal schedule?

        Common solver properties are:

        * `num_qubits` (int): Number of qubits available.
        * `vfyc` (bool): Should solver work on "virtual full-yield chip"?
        * `max_anneal_schedule_points` (int): Piecewise linear annealing schedule points.
        * `h_range` ([int,int]), j_range ([int,int]): Biases/couplings values range.
        * `num_reads_range` ([int,int]): Range of allowed values for `num_reads` parameter.

        Returns:
            list[Solver]: List of all solvers that satisfy the conditions.

        Note:
            Client subclasses (e.g. :class:`dwave.cloud.qpu.Client` or
            :class:`dwave.cloud.sw.Client`) already filter solvers by resource
            type, so for `qpu` and `software` filters to have effect, call :meth:`.get_solvers`
            on base class :class:`~dwave.cloud.client.Client`.

        Examples::

            client.get_solvers(
                num_qubits__gt=2000,                # we need more than 2000 qubits
                num_qubits__lt=4000,                # ... but fewer than 4000 qubits
                num_qubits__within=(2000, 4000),    # an alternative to the previous two lines
                num_active_qubits=1089,             # we want a particular number of active qubits
                vfyc=True,                          # we require a fully yielded Chimera
                vfyc__in=[False, None],             # inverse of the previous filter
                vfyc__available=False,              # we want solvers that do not advertize the vfyc property
                anneal_schedule=True,               # we need support for custom anneal schedule
                max_anneal_schedule_points__gte=4,  # we need at least 4 points for our anneal schedule
                num_reads_range__covers=1000,       # our solver must support returning 1000 reads
                extended_j_range__covers=[-2, 2],   # we need extended J range to contain subrange [-2,2]
                couplers__contains=[0, 128],        # coupler (edge between) qubits (0,128) must exist
                couplers__issuperset=[[0,128], [0,4]],
                                                    # two couplers required: (0,128) and (0,4)
                qubits__issuperset={0, 4, 215},     # qubits 0, 4 and 215 must exist
                supported_problem_types__issubset={'ising', 'qubo'},
                                                    # require Ising, QUBO or both to be supported
                name='DW_2000Q_5',                  # full solver name/ID match
                name__regex='.*2000.*',             # partial/regex-based solver name match
                chip_id__regex='DW_.*',             # chip ID prefix must be DW_
                topology__type__eq="chimera"        # topology.type must be chimera
            )
        """

        def covers_op(prop, val):
            """Does LHS `prop` (range) fully cover RHS `val` (range or item)?"""

            # `prop` must be a 2-element list/tuple range.
            if not isinstance(prop, (list, tuple)) or not len(prop) == 2:
                raise ValueError("2-element list/tuple range required for LHS value")
            llo, lhi = min(prop), max(prop)

            # `val` can be a single value, or a range (2-list/2-tuple).
            if isinstance(val, (list, tuple)) and len(val) == 2:
                # val range within prop range?
                rlo, rhi = min(val), max(val)
                return llo <= rlo and lhi >= rhi
            else:
                # val item within prop range?
                return llo <= val <= lhi

        def within_op(prop, val):
            """Is LHS `prop` (range or item) fully covered by RHS `val` (range)?"""
            try:
                return covers_op(val, prop)
            except ValueError:
                raise ValueError("2-element list/tuple range required for RHS value")

        def _set(iterable):
            """Like set(iterable), but works for lists as items in iterable.
            Before constructing a set, lists are converted to tuples.
            """
            first = next(iter(iterable))
            if isinstance(first, list):
                return set(tuple(x) for x in iterable)
            return set(iterable)

        def with_valid_lhs(op):
            @wraps(op)
            def _wrapper(prop, val):
                if prop is None:
                    return False
                return op(prop, val)
            return _wrapper

        # available filtering operators
        ops = {
            'lt': with_valid_lhs(operator.lt),
            'lte': with_valid_lhs(operator.le),
            'gt': with_valid_lhs(operator.gt),
            'gte': with_valid_lhs(operator.ge),
            'eq': operator.eq,
            'available': lambda prop, val: prop is not None if val else prop is None,
            'regex': with_valid_lhs(lambda prop, val: re.match("^{}$".format(val), prop)),
            # range operations
            'covers': with_valid_lhs(covers_op),
            'within': with_valid_lhs(within_op),
            # membership tests
            'in': lambda prop, val: prop in val,
            'contains': with_valid_lhs(lambda prop, val: val in prop),
            # set tests
            'issubset': with_valid_lhs(lambda prop, val: _set(prop).issubset(_set(val))),
            'issuperset': with_valid_lhs(lambda prop, val: _set(prop).issuperset(_set(val))),
        }

        def predicate(solver, query, val):
            # needs to handle kwargs like these:
            #  key=val
            #  key__op=val
            #  key__key=val
            #  key__key__op=val
            # LHS is split on __ in `query`
            assert len(query) >= 1

            potential_path, potential_op_name = query[:-1], query[-1]

            if potential_op_name in ops:
                # op is explicit, and potential path is correct
                op_name = potential_op_name
            else:
                # op is implied and depends on property type, path is the whole query
                op_name = None
                potential_path = query

            path = '.'.join(potential_path)

            if path in solver.derived_properties:
                op = ops[op_name or 'eq']
                return op(getattr(solver, path), val)
            elif pluck(solver.parameters, path, None) is not None:
                op = ops[op_name or 'available']
                return op(pluck(solver.parameters, path), val)
            elif pluck(solver.properties, path, None) is not None:
                op = ops[op_name or 'eq']
                return op(pluck(solver.properties, path), val)
            else:
                op = ops[op_name or 'eq']
                return op(None, val)

        # param validation
        sort_reverse = False
        if not order_by:
            sort_key = None
        elif isinstance(order_by, six.string_types):
            if order_by[0] == '-':
                sort_reverse = True
                order_by = order_by[1:]
            if not order_by:
                sort_key = None
            else:
                sort_key = lambda solver: pluck(solver, order_by, None)
        elif callable(order_by):
            sort_key = order_by
        else:
            raise TypeError("expected string or callable for 'order_by'")

        # default filters:
        filters.setdefault('online', True)

        predicates = []
        for lhs, val in filters.items():
            query = lhs.split('__')
            predicates.append(partial(predicate, query=query, val=val))

        logger.debug("Filtering solvers with predicates=%r", predicates)

        # optimization for case when exact solver name/id is known:
        # we can fetch only that solver
        # NOTE: in future, complete feature-based filtering will be on server-side
        query = dict(refresh_=refresh)
        if 'name' in filters:
            query['name'] = filters['name']
        if 'name__eq' in filters:
            query['name'] = filters['name__eq']

        # filter
        solvers = self._fetch_solvers(**query)
        solvers = [s for s in solvers if all(p(s) for p in predicates)]

        # sort: undefined (None) key values go last
        if sort_key is not None:
            solvers_with_keys = [(sort_key(solver), solver) for solver in solvers]
            solvers_with_invalid_keys = [(key, solver) for key, solver in solvers_with_keys if key is None]
            solvers_with_valid_keys = [(key, solver) for key, solver in solvers_with_keys if key is not None]
            solvers_with_valid_keys.sort(key=operator.itemgetter(0))
            solvers = [solver for key, solver in chain(solvers_with_valid_keys, solvers_with_invalid_keys)]

        # reverse if necessary (as a separate step from sorting, so it works for invalid keys
        # and plain list reverse without sorting)
        if sort_reverse:
            solvers.reverse()

        return solvers

    def solvers(self, refresh=False, **filters):
        """Deprecated in favor of :meth:`.get_solvers`."""
        warnings.warn("'solvers' is deprecated in favor of 'get_solvers'.", DeprecationWarning)
        return self.get_solvers(refresh=refresh, **filters)

    def get_solver(self, name=None, refresh=False, **filters):
        """Load the configuration for a single solver.

        Makes a blocking web call to `{endpoint}/solvers/remote/{solver_name}/`, where `{endpoint}`
        is a URL configured for the client, and returns a :class:`.Solver` instance
        that can be used to submit sampling problems to the D-Wave API and retrieve results.

        Args:
            name (str):
                ID of the requested solver. ``None`` returns the default solver.
                If default solver is not configured, ``None`` returns the first available
                solver in ``Client``'s class (QPU/software/base).

            **filters (keyword arguments, optional):
                Dictionary of filters over features this solver has to have. For a list of
                feature names and values, see: :meth:`~dwave.cloud.client.Client.get_solvers`.

            order_by (callable/str, default='id'):
                Solver sorting key function (or :class:`Solver` attribute name).
                By default, solvers are sorted by ID/name.

            refresh (bool):
                Return solver from cache (if cached with ``get_solvers()``),
                unless set to ``True``.

        Returns:
            :class:`.Solver`

        Examples:
            This example creates two solvers for a client instantiated from
            a local system's auto-detected default configuration file, which configures
            a connection to a D-Wave resource that provides two solvers. The first
            uses the default solver, the second explicitly selects another solver.

            >>> from dwave.cloud import Client
            >>> client = Client.from_config()
            >>> client.get_solvers()   # doctest: +SKIP
            [Solver(id='2000Q_ONLINE_SOLVER1'), Solver(id='2000Q_ONLINE_SOLVER2')]
            >>> solver1 = client.get_solver()    # doctest: +SKIP
            >>> solver2 = client.get_solver(name='2000Q_ONLINE_SOLVER2')    # doctest: +SKIP
            >>> solver1.id  # doctest: +SKIP
            '2000Q_ONLINE_SOLVER1'
            >>> solver2.id   # doctest: +SKIP
            '2000Q_ONLINE_SOLVER2'
            >>> # code that uses client
            >>> client.close() # doctest: +SKIP

        """
        logger.debug("Requested a solver that best matches feature filters=%r", filters)

        # backward compatibility: name as the first feature
        if name is not None:
            filters.setdefault('name', name)

        # in absence of other filters, config/env solver filters/name are used
        if not filters and self.default_solver:
            filters = self.default_solver

        # get the first solver that satisfies all filters
        try:
            logger.debug("Fetching solvers according to filters=%r", filters)
            return self.get_solvers(refresh=refresh, **filters)[0]
        except IndexError:
            raise SolverNotFoundError("Solver with the requested features not available")

    def _submit(self, body, future):
        """Enqueue a problem for submission to the server.

        This method is thread safe.
        """
        self._submission_queue.put(self._submit.Message(body, future))
    _submit.Message = collections.namedtuple('Message', ['body', 'future'])

    def _do_submit_problems(self):
        """Pull problems from the submission queue and submit them.

        Note:
            This method is always run inside of a daemon thread.
        """
        try:
            while True:
                # Pull as many problems as we can, block on the first one,
                # but once we have one problem, switch to non-blocking then
                # submit without blocking again.

                # `None` task is used to signal thread termination
                item = self._submission_queue.get()

                if item is None:
                    break

                ready_problems = [item]
                while len(ready_problems) < self._SUBMIT_BATCH_SIZE:
                    try:
                        ready_problems.append(self._submission_queue.get_nowait())
                    except queue.Empty:
                        break

                # Submit the problems
                logger.debug("Submitting %d problems", len(ready_problems))
                body = '[' + ','.join(mess.body for mess in ready_problems) + ']'
                try:
                    try:
                        response = self.session.post(posixpath.join(self.endpoint, 'problems/'), body)
                        localtime_of_response = epochnow()
                    except requests.exceptions.Timeout:
                        raise RequestTimeout

                    if response.status_code == 401:
                        raise SolverAuthenticationError()
                    response.raise_for_status()

                    message = response.json()
                    logger.debug("Finished submitting %d problems", len(ready_problems))
                except BaseException as exception:
                    logger.debug("Submit failed for %d problems", len(ready_problems))
                    if not isinstance(exception, SolverAuthenticationError):
                        exception = IOError(exception)

                    for mess in ready_problems:
                        mess.future._set_error(exception, sys.exc_info())
                        self._submission_queue.task_done()
                    continue

                # Pass on the information
                for submission, res in zip(ready_problems, message):
                    submission.future._set_clock_diff(response, localtime_of_response)
                    self._handle_problem_status(res, submission.future)
                    self._submission_queue.task_done()

                # this is equivalent to a yield to scheduler in other threading libraries
                time.sleep(0)

        except BaseException as err:
            logger.exception(err)

    def _handle_problem_status(self, message, future):
        """Handle the results of a problem submission or results request.

        This method checks the status of the problem and puts it in the correct
        queue.

        Args:
            message (dict):
                Update message from the SAPI server wrt. this problem.
            future (:class:`dwave.cloud.computation.Future`:
                future corresponding to the problem

        Note:
            This method is always run inside of a daemon thread.
        """
        try:
            logger.trace("Handling response: %r", message)
            logger.debug("Handling response for %s with status %s",
                         message.get('id'), message.get('status'))

            # Handle errors in batch mode
            if 'error_code' in message and 'error_msg' in message:
                raise SolverFailureError(message['error_msg'])

            if 'status' not in message:
                raise InvalidAPIResponseError("'status' missing in problem description response")
            if 'id' not in message:
                raise InvalidAPIResponseError("'id' missing in problem description response")

            future.id = message['id']
            future.remote_status = status = message['status']

            # The future may not have the ID set yet
            with future._single_cancel_lock:
                # This handles the case where cancel has been called on a future
                # before that future received the problem id
                if future._cancel_requested:
                    if not future._cancel_sent and status == self.STATUS_PENDING:
                        # The problem has been canceled but the status says its still in queue
                        # try to cancel it
                        self._cancel(message['id'], future)
                    # If a cancel request could meaningfully be sent it has been now
                    future._cancel_sent = True

            if not future.time_received and message.get('submitted_on'):
                future.time_received = parse_datetime(message['submitted_on'])

            if not future.time_solved and message.get('solved_on'):
                future.time_solved = parse_datetime(message['solved_on'])

            if not future.eta_min and message.get('earliest_estimated_completion'):
                future.eta_min = parse_datetime(message['earliest_estimated_completion'])

            if not future.eta_max and message.get('latest_estimated_completion'):
                future.eta_max = parse_datetime(message['latest_estimated_completion'])

            if status == self.STATUS_COMPLETE:
                # TODO: find a better way to differentiate between
                # `completed-on-submit` and `completed-on-poll`.
                # Loading should happen only once, not every time when response
                # doesn't contain 'answer'.

                # If the message is complete, forward it to the future object
                if 'answer' in message:
                    future._set_message(message)
                # If the problem is complete, but we don't have the result data
                # put the problem in the queue for loading results.
                else:
                    self._load(future)
            elif status in self.ANY_STATUS_ONGOING:
                # If the response is pending add it to the queue.
                self._poll(future)
            elif status == self.STATUS_CANCELLED:
                # If canceled return error
                raise CanceledFutureError()
            else:
                # Return an error to the future object
                errmsg = message.get('error_message', 'An unknown error has occurred.')
                if 'solver is offline' in errmsg.lower():
                    raise SolverOfflineError(errmsg)
                else:
                    raise SolverFailureError(errmsg)

        except Exception as error:
            # If there were any unhandled errors we need to release the
            # lock in the future, otherwise deadlock occurs.
            future._set_error(error, sys.exc_info())

    def _cancel(self, id_, future):
        """Enqueue a problem to be canceled.

        This method is thread safe.
        """
        self._cancel_queue.put((id_, future))

    def _do_cancel_problems(self):
        """Pull ids from the cancel queue and submit them.

        Note:
            This method is always run inside of a daemon thread.
        """
        try:
            while True:
                # Pull as many problems as we can, block when none are available.

                # `None` task is used to signal thread termination
                item = self._cancel_queue.get()
                if item is None:
                    break

                item_list = [item]
                while True:
                    try:
                        item_list.append(self._cancel_queue.get_nowait())
                    except queue.Empty:
                        break

                # Submit the problems, attach the ids as a json list in the
                # body of the delete query.
                try:
                    body = [item[0] for item in item_list]

                    try:
                        self.session.delete(posixpath.join(self.endpoint, 'problems/'), json=body)
                    except requests.exceptions.Timeout:
                        raise RequestTimeout

                except Exception as err:
                    for _, future in item_list:
                        if future is not None:
                            future._set_error(err, sys.exc_info())

                # Mark all the ids as processed regardless of success or failure.
                [self._cancel_queue.task_done() for _ in item_list]

                # this is equivalent to a yield to scheduler in other threading libraries
                time.sleep(0)

        except Exception as err:
            logger.exception(err)

    def _is_clock_diff_acceptable(self, future):
        if not future or future.clock_diff is None:
            return False

        logger.debug("Detected (server,client) clock offset: approx. %.2f sec. "
                     "Acceptable offset is: %.2f sec",
                     future.clock_diff, self._CLOCK_DIFF_MAX)

        return future.clock_diff <= self._CLOCK_DIFF_MAX

    def _poll(self, future):
        """Enqueue a problem to poll the server for status."""

        if future._poll_backoff is None:
            # on first poll, start with minimal back-off
            future._poll_backoff = self._POLL_BACKOFF_MIN
        else:
            # on subsequent polls, do exponential back-off, clipped to a range
            future._poll_backoff = \
                max(self._POLL_BACKOFF_MIN,
                    min(future._poll_backoff * 2, self._POLL_BACKOFF_MAX))

        # for poll priority we use timestamp of next scheduled poll
        at = time.time() + future._poll_backoff

        now = utcnow()
        future_age = (now - future.time_created).total_seconds()
        logger.debug("Polling scheduled at %.2f with %.2f sec new back-off for: %s (future's age: %.2f sec)",
                     at, future._poll_backoff, future.id, future_age)

        # don't enqueue for next poll if polling_timeout is exceeded by then
        future_age_on_next_poll = future_age + (at - datetime_to_timestamp(now))
        if self.polling_timeout is not None and future_age_on_next_poll > self.polling_timeout:
            logger.debug("Polling timeout exceeded before next poll: %.2f sec > %.2f sec, aborting polling!",
                         future_age_on_next_poll, self.polling_timeout)
            raise PollingTimeout

        self._poll_queue.put((at, future))

    def _do_poll_problems(self):
        """Poll the server for the status of a set of problems.

        Note:
            This method is always run inside of a daemon thread.
        """
        try:
            # grouped futures (all scheduled within _POLL_GROUP_TIMEFRAME)
            frame_futures = {}

            def task_done():
                self._poll_queue.task_done()

            def add(future):
                # add future to query frame_futures
                # returns: worker lives on?

                # `None` task signifies thread termination
                if future is None:
                    task_done()
                    return False

                if future.id not in frame_futures and not future.done():
                    frame_futures[future.id] = future
                else:
                    task_done()

                return True

            while True:
                frame_futures.clear()

                # blocking add first scheduled
                frame_earliest, future = self._poll_queue.get()
                if not add(future):
                    return

                # try grouping if scheduled within grouping timeframe
                while len(frame_futures) < self._STATUS_QUERY_SIZE:
                    try:
                        task = self._poll_queue.get_nowait()
                    except queue.Empty:
                        break

                    at, future = task
                    if at - frame_earliest <= self._POLL_GROUP_TIMEFRAME:
                        if not add(future):
                            return
                    else:
                        task_done()
                        self._poll_queue.put(task)
                        break

                # build a query string with ids of all futures in this frame
                ids = [future.id for future in frame_futures.values()]
                logger.debug("Polling for status of futures: %s", ids)
                query_string = 'problems/?id=' + ','.join(ids)

                # if futures were cancelled while `add`ing, skip empty frame
                if not ids:
                    continue

                # wait until `frame_earliest` before polling
                delay = frame_earliest - time.time()
                if delay > 0:
                    logger.debug("Pausing polling %.2f sec for futures: %s", delay, ids)
                    time.sleep(delay)
                else:
                    logger.trace("Skipping non-positive delay of %.2f sec", delay)

                # execute and handle the polling request
                try:
                    logger.trace("Executing poll API request")

                    try:
                        response = self.session.get(posixpath.join(self.endpoint, query_string))
                    except requests.exceptions.Timeout:
                        raise RequestTimeout

                    if response.status_code == 401:
                        raise SolverAuthenticationError()

                    # assume 5xx errors are transient, and don't abort polling
                    if 500 <= response.status_code < 600:
                        logger.warning(
                            "Received an internal server error response on "
                            "problem status polling request (%s). Assuming "
                            "error is transient, and resuming polling.",
                            response.status_code)
                        # add all futures in this frame back to the polling queue
                        # XXX: logic split between `_handle_problem_status` and here
                        for future in frame_futures.values():
                            self._poll(future)

                    else:
                        # otherwise, fail
                        response.raise_for_status()

                        # or handle a successful request
                        statuses = response.json()
                        for status in statuses:
                            self._handle_problem_status(status, frame_futures[status['id']])

                except BaseException as exception:
                    if not isinstance(exception, SolverAuthenticationError):
                        exception = IOError(exception)

                    for id_ in frame_futures.keys():
                        frame_futures[id_]._set_error(IOError(exception), sys.exc_info())

                for id_ in frame_futures.keys():
                    task_done()

                time.sleep(0)

        except Exception as err:
            logger.exception(err)

    def _load(self, future):
        """Enqueue a problem to download results from the server.

        Args:
            future: Future` object corresponding to the query

        This method is threadsafe.
        """
        self._load_queue.put(future)

    def _do_load_results(self):
        """Submit a query asking for the results for a particular problem.

        To request the results of a problem: ``GET /problems/{problem_id}/``

        Note:
            This method is always run inside of a daemon thread.
        """
        try:
            while True:
                # Select a problem
                future = self._load_queue.get()
                # `None` task signifies thread termination
                if future is None:
                    break
                logger.debug("Loading results of: %s", future.id)

                # Submit the query
                query_string = 'problems/{}/'.format(future.id)
                try:
                    try:
                        response = self.session.get(posixpath.join(self.endpoint, query_string))
                    except requests.exceptions.Timeout:
                        raise RequestTimeout

                    if response.status_code == 401:
                        raise SolverAuthenticationError()
                    response.raise_for_status()

                    message = response.json()
                except BaseException as exception:
                    if not isinstance(exception, SolverAuthenticationError):
                        exception = IOError(exception)

                    future._set_error(IOError(exception), sys.exc_info())
                    continue

                # Dispatch the results, mark the task complete
                self._handle_problem_status(message, future)
                self._load_queue.task_done()

                # this is equivalent to a yield to scheduler in other threading libraries
                time.sleep(0)

        except Exception as err:
            logger.error('Load result error: ' + str(err))
