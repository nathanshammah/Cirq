"""Samplers for the AQT ion trap device"""
import json
import time
import uuid
from typing import Iterable, List, Union, Tuple, cast

import numpy as np
from requests import put
from cirq import circuits, Sampler, resolve_parameters, LineQubit
from cirq.study.sweeps import Sweep
from cirq.aqt.aqt_device import AQTSimulator, get_op_string
from cirq import study, ops, schedules, IonDevice

Sweepable = Union[study.ParamResolver, Iterable[study.ParamResolver], Sweep,
                  Iterable[Sweep]]


class AQTSampler(Sampler):
    """Sampler for the AQT ion trap device
    This sampler connects to the AQT machine and
    runs a single circuit or an entire sweep remotely
    """

    def __init__(self, remote_host: str, access_token: str):
        """
        Args:
            remote_host: Address of the remote device.
            access_token: Access token for the remote api.
        """
        self.remote_host = remote_host
        self.access_token = access_token

    def _generate_json(
            self,
            circuit: circuits.Circuit,
            param_resolver: study.ParamResolverOrSimilarType,
    ) -> str:
        """
        Args:
            circuit: Circuit to be run
            param_resolver: Param resolver for the

        Returns:
            json formatted string of the sequence

        The json format is defined as follows:

        [[op_string,gate_exponent,qubits]]

        which is a list of sequential quantum operations,
        each operation defined by:

        op_string: str that specifies the operation type, either "X","Y","MS"
        gate_exponent: float that specifies the gate_exponent of the operation
        qubits: list of qubits where the operation acts on.
        """

        seq_list: List[Tuple[str, float, List[int]]] = []
        circuit = resolve_parameters(circuit, param_resolver)
        # TODO: Check if circuit is empty
        for op in circuit.all_operations():
            line_qubit = cast(Tuple[LineQubit], op.qubits)
            op = cast(ops.GateOperation, op)
            qubit_idx = [obj.x for obj in line_qubit]
            op_str = get_op_string(op)
            gate = cast(ops.EigenGate, op.gate)
            seq_list.append((op_str, gate.exponent, qubit_idx))
        json_str = json.dumps(seq_list)
        return json_str

    def _send_json(self,
                   *,
                   json_str: str,
                   id_str: Union[str, uuid.UUID],
                   repetitions: int = 1,
                   num_qubits: int = 1):
        """Sends the json string to the remote AQT device
        Args:
            json_str: Json representation of the circuit.
            id_str: Unique id of the datapoint.
            repetitions: Number of repetitions.
            num_qubits: Number of qubits present in the device.

        Returns:
            Measurement results as an array of boolean.

        The interface is given by PUT requests to a single endpoint URL.

        The first PUT will insert the circuit into the remote queue,
        given a valid access key.

        Every subsequent PUT will retturn a dictionary, where the key "status"
        is either 'queued', if the circuit has not been processed yet or
        'finished' if the circuit has been processed.

        The experimental data is returned via the key 'data'
        """
        data = put(self.remote_host,
                   data={
                       'data': json_str,
                       'access_token': self.access_token,
                       'repetitions': repetitions,
                       'no_qubits': num_qubits
                   }).json()
        if data['status'] == 'error':
            raise RuntimeError('AQT server reported error: \n' + str(data))

        if 'id' not in data.keys():
            raise RuntimeError(
                'Got unexpected return data from AQT server: \n' + str(data))
        id_str = data['id']

        while True:
            data = put(self.remote_host,
                       data={
                           'id': id_str,
                           'access_token': self.access_token
                       }).json()
            print(data)
            if 'status' not in data.keys():
                raise RuntimeError(
                    'Got unexpected return data from AQT server: \n' +
                    str(data))
            if data['status'] == 'finished':
                break
            elif data['status'] == 'error':
                raise RuntimeError(
                    'Got unexpected return data from AQT server: \n' +
                    str(data))
            time.sleep(1.0)
        measurements_int = data['samples']
        measurements = np.zeros((len(measurements_int), num_qubits))
        for i, result_int in enumerate(measurements_int):
            for j in range(num_qubits):
                measurements[i, j] = np.floor(result_int / 2**j)
        return measurements

    def run_sweep(self,
                  program: Union[circuits.Circuit, schedules.Schedule],
                  params: study.Sweepable,
                  repetitions: int = 1) -> List[study.TrialResult]:
        """Samples from the given Circuit or Schedule.

        In contrast to run, this allows for sweeping over different parameter
        values.

        Args:
            program: The circuit or schedule to simulate.
            Should be generated using AQTSampler.generate_circuit_from_list
            params: Parameters to run with the program.
            repetitions: The number of repetitions to simulate.

        The parameters remote_host and access_token are not used.

        Returns:
            TrialResult list for this run; one for each possible parameter
            resolver.
        """
        meas_name = 'm'  # TODO: Get measurement name from circuit
        circuit = (program if isinstance(program, circuits.Circuit) else
                   program.to_circuit())
        assert isinstance(circuit.device, IonDevice)
        trial_results = []  # type: List[study.TrialResult]
        for param_resolver in study.to_resolvers(params):
            id_str = uuid.uuid1()
            num_qubits = len(circuit.device.qubits)
            json_str = self._generate_json(circuit=circuit,
                                           param_resolver=param_resolver)
            results = self._send_json(json_str=json_str,
                                      id_str=id_str,
                                      repetitions=repetitions,
                                      num_qubits=num_qubits)
            results = results.astype(bool)
            res_dict = {meas_name: results}
            trial_results.append(
                study.TrialResult(params=param_resolver, measurements=res_dict))
        return trial_results


class AQTRemoteSimulator(AQTSampler):
    """Sampler using the AQT simulator
    When the attribute simulate_ideal is set to True,0
    an ideal circuit is sampled

    If not, the error model defined in aqt_simulator_test.py is used
    Example for running the ideal sampler:

    sampler = AQTSamplerSim()
    sampler.simulate_ideal=True
    """

    def __init__(self,
                 remote_host: str = '',
                 access_token: str = '',
                 simulate_ideal: bool = False):
        """

        Args:
            remote_host: Remote host is not used by the local simulator.
            access_token: Access token is not used by the local simulator.
            simulate_ideal: Boolean that determines whether a noisy or
                            an ideal simulation is performed.
        """
        self.remote_host = remote_host
        self.access_token = access_token
        self.simulate_ideal = simulate_ideal

    def _send_json(self,
                   *,
                   json_str: str,
                   id_str: Union[str, uuid.UUID],
                   repetitions: int = 1,
                   num_qubits: int = 1) -> np.ndarray:
        """Replaces the remote host with a local simulator
        Args:
            json_str: Json representation of the circuit.
            id_str: Unique id of the datapoint.
            repetitions: Number of repetitions.
            num_qubits: Number of qubits present in the device.

        Returns:
            Measurement results as an ndarray of booleans.
        """
        sim = AQTSimulator(num_qubits=num_qubits,
                           simulate_ideal=self.simulate_ideal)
        sim.generate_circuit_from_list(json_str)
        data = sim.simulate_samples(repetitions)
        return data.measurements['m']
