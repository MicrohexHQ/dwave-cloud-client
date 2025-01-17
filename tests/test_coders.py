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

from __future__ import absolute_import, print_function

import copy
import base64
import struct
import unittest
import itertools

import dimod
import numpy as np
from plucky import pluck

from dwave.cloud.coders import (
    encode_problem_as_qp, decode_qp, decode_qp_numpy,
    encode_problem_as_bq, decode_bq)
from dwave.cloud.solver import StructuredSolver, UnstructuredSolver


def get_structured_solver():
    data = {
        "properties": {
            "supported_problem_types": ["qubo", "ising"],
            "qubits": [0, 1, 2, 3],
            "couplers": [(0, 1), (1, 2), (2, 3), (3, 0)],
            "num_qubits": 4,
            "parameters": {"num_reads": "Number of samples to return."}
        },
        "id": "test-structured-solver",
        "description": "A test structured solver"
    }
    return StructuredSolver(client=None, data=data)

def get_unstructured_solver():
    data = {
        "properties": {
            "supported_problem_types": ["bqm"],
            "parameters": {"num_reads": "Number of samples to return."}
        },
        "id": "test-unstructured-solver",
        "description": "A test unstructured solver"
    }
    return UnstructuredSolver(client=None, data=data)


class TestQPCoders(unittest.TestCase):
    nan = float('nan')

    # response to a 5-qubit problem from a 5 qubit machine
    res_msg = {
        "id": "test",
        "solver": "test",
        "status": "COMPLETED",
        "type": "ising",
        "answer": {
            "format": "qp",
            "num_variables": 5,
            "energies": "AAAAAAAALsA=",
            "num_occurrences": "ZAAAAA==",
            "active_variables": "AAAAAAEAAAACAAAAAwAAAAQAAAA=",
            "solutions": "AAAAAA==",
            "timing": {}
        }
    }
    res_num_variables = 5
    res_active_variables = (0, 1, 2, 3, 4)
    res_solutions = [[-1, -1, -1, -1, -1]]
    res_energies = (-15.0,)
    res_num_occurrences = (100,)

    def encode_doubles(self, values):
        return base64.b64encode(struct.pack('<' + ('d' * len(values)), *values)).decode('utf-8')

    def test_qp_request_encoding_all_qubits(self):
        """Test biases and coupling strengths are properly encoded (base64 little-endian doubles)."""

        solver = get_structured_solver()
        linear = {index: 1 for index in solver.nodes}
        quadratic = {key: -1 for key in solver.undirected_edges}
        request = encode_problem_as_qp(solver, linear, quadratic)
        self.assertEqual(request['format'], 'qp')
        self.assertEqual(request['lin'],  self.encode_doubles([1, 1, 1, 1]))
        self.assertEqual(request['quad'], self.encode_doubles([-1, -1, -1, -1]))

    def test_qp_request_encoding_sub_qubits(self):
        """Inactive qubits should be encoded as NaNs. Inactive couplers should be omitted."""

        solver = get_structured_solver()
        linear = {index: 1 for index in sorted(list(solver.nodes))[:2]}
        quadratic = {key: -1 for key in sorted(list(solver.undirected_edges))[:1]}
        request = encode_problem_as_qp(solver, linear, quadratic)
        self.assertEqual(request['format'], 'qp')
        # [1, 1, NaN, NaN]
        self.assertEqual(request['lin'],  self.encode_doubles([1, 1, self.nan, self.nan]))
        # [-1]
        self.assertEqual(request['quad'], self.encode_doubles([-1]))

    def test_qp_request_encoding_missing_qubits(self):
        """Qubits don't have to be specified with biases only, but also with couplings."""

        solver = get_structured_solver()
        linear = {}
        quadratic = {(0, 1): -1}
        request = encode_problem_as_qp(solver, linear, quadratic)
        self.assertEqual(request['format'], 'qp')
        # [0, 0, NaN, NaN]
        self.assertEqual(request['lin'],  self.encode_doubles([0, 0, self.nan, self.nan]))
        # [-1]
        self.assertEqual(request['quad'], self.encode_doubles([-1]))

    def test_qp_request_encoding_sub_qubits_implicit_biases(self):
        """Biases don't have to be specified for qubits to be active."""

        solver = get_structured_solver()
        linear = {}
        quadratic = {(0,3): -1}
        request = encode_problem_as_qp(solver, linear, quadratic)
        self.assertEqual(request['format'], 'qp')
        # [0, NaN, NaN, 0]
        self.assertEqual(request['lin'],  self.encode_doubles([0, self.nan, self.nan, 0]))
        # [-1]
        self.assertEqual(request['quad'], self.encode_doubles([-1]))

    def test_qp_request_encoding_sub_qubits_implicit_couplings(self):
        """Couplings should be zero for active qubits, if not specified."""

        solver = get_structured_solver()
        linear = {0: 0, 3: 0}
        quadratic = {}
        request = encode_problem_as_qp(solver, linear, quadratic)
        self.assertEqual(request['format'], 'qp')
        # [0, NaN, NaN, 0]
        self.assertEqual(request['lin'],  self.encode_doubles([0, self.nan, self.nan, 0]))
        # [0]
        self.assertEqual(request['quad'], self.encode_doubles([0]))

    def test_qp_response_decoding(self):
        res = decode_qp(copy.deepcopy(self.res_msg))

        self.assertEqual(res.get('format'), 'qp')
        self.assertEqual(res.get('num_variables'), self.res_num_variables)
        self.assertEqual(res.get('active_variables'), self.res_active_variables)
        self.assertEqual(res.get('solutions'), self.res_solutions)
        self.assertEqual(res.get('energies'), self.res_energies)
        self.assertEqual(res.get('num_occurrences'), self.res_num_occurrences)

    def test_qp_response_numpy_decoding(self):
        res = decode_qp_numpy(copy.deepcopy(self.res_msg), return_matrix=False)

        self.assertEqual(res.get('format'), 'qp')
        self.assertEqual(res.get('num_variables'), self.res_num_variables)
        self.assertEqual(res.get('active_variables'), list(self.res_active_variables))
        self.assertEqual(res.get('solutions'), list(self.res_solutions))
        self.assertEqual(res.get('energies'), list(self.res_energies))
        self.assertEqual(res.get('num_occurrences'), list(self.res_num_occurrences))

    def test_qp_response_numpy_decoding_numpy_array(self):
        res = decode_qp_numpy(copy.deepcopy(self.res_msg), return_matrix=True)

        self.assertEqual(res.get('format'), 'qp')
        self.assertEqual(res.get('num_variables'), self.res_num_variables)
        np.testing.assert_array_equal(res.get('active_variables'), np.array(self.res_active_variables))
        np.testing.assert_array_equal(res.get('solutions'), np.array(self.res_solutions))
        np.testing.assert_array_equal(res.get('energies'), np.array(self.res_energies))
        np.testing.assert_array_equal(res.get('num_occurrences'), np.array(self.res_num_occurrences))


class TestBQCoders(unittest.TestCase):

    def test_bq_request_encoding_empty_bqm(self):
        """Empty BQM has to be trivially encoded."""

        bqm = dimod.BQM.from_qubo({})
        req = encode_problem_as_bq(bqm)

        self.assertEqual(req.get('format'), 'bq')
        self.assertEqual(pluck(req, 'data.version.bqm_schema'), '3.0.0')
        self.assertEqual(pluck(req, 'data.num_variables'), 0)
        self.assertEqual(pluck(req, 'data.num_interactions'), 0)

    def test_bq_request_encoding_ising_bqm(self):
        """Simple Ising BQM properly encoded."""

        bqm = dimod.BQM.from_ising({0: 1}, {(0, 1): 1})
        req = encode_problem_as_bq(bqm)

        self.assertEqual(req.get('format'), 'bq')
        self.assertEqual(pluck(req, 'data.version.bqm_schema'), '3.0.0')
        self.assertEqual(pluck(req, 'data.variable_type'), 'SPIN')
        self.assertEqual(pluck(req, 'data.num_variables'), 2)
        self.assertEqual(pluck(req, 'data.num_interactions'), 1)

    def test_bq_request_encoding_qubo_bqm(self):
        """Simple Qubo BQM properly encoded."""

        bqm = dimod.BQM.from_qubo({(0, 1): 1})
        req = encode_problem_as_bq(bqm)

        self.assertEqual(req.get('format'), 'bq')
        self.assertEqual(pluck(req, 'data.version.bqm_schema'), '3.0.0')
        self.assertEqual(pluck(req, 'data.variable_type'), 'BINARY')
        self.assertEqual(pluck(req, 'data.num_variables'), 2)
        self.assertEqual(pluck(req, 'data.num_interactions'), 1)

    def test_bq_request_encoding_bqm_named_vars(self):
        """BQM with named variable properly encoded."""

        bqm = dimod.BQM.from_ising({}, {'ab': 1, 'bc': 1, 'ca': 1})
        req = encode_problem_as_bq(bqm)

        self.assertEqual(req.get('format'), 'bq')
        self.assertEqual(pluck(req, 'data.version.bqm_schema'), '3.0.0')
        self.assertEqual(pluck(req, 'data.variable_type'), 'SPIN')
        self.assertEqual(pluck(req, 'data.num_variables'), 3)
        self.assertEqual(pluck(req, 'data.num_interactions'), 3)
        self.assertEqual(pluck(req, 'data.variable_labels'), list('abc'))

    def test_bq_response_decoding(self):
        """Answer to simple problem properly decoded."""

        ss = dimod.SampleSet.from_samples(
            ([[0, 1], [1, 0]], 'ab'), vartype='BINARY', energy=0)

        msg = dict(answer=dict(format='bq', data=ss.to_serializable()))

        res = decode_bq(msg)

        self.assertEqual(res.get('problem_type'), 'bqm')
        self.assertEqual(res.get('sampleset'), ss)
