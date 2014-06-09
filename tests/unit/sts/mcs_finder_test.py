#!/usr/bin/env python

import unittest
import sys
import os
import itertools
from copy import copy
import types
import signal
import tempfile

from config.experiment_config_lib import ControllerConfig
from sts.control_flow import Replayer, MCSFinder, EfficientMCSFinder
from sts.topology import FatTree, PatchPanel, MeshTopology
from sts.simulation_state import Simulation, SimulationConfig
from sts.replay_event import Event, InternalEvent, InputEvent
from sts.event_dag import EventDag
from sts.entities import Host, Controller
import logging

sys.path.append(os.path.dirname(__file__) + "/../../..")

<<<<<<< HEAD
class MockMCSFinderBase(object):
  ''' Overrides self.invariant_check and run_simulation_forward() '''
  def __init__(self, event_dag, mcs):
    self.dag = event_dag
=======
class MockSimulationConfig(object):
  def __init__(self, ignore_interposition=False):
    self.ignore_interposition = ignore_interposition

class MockMCSFinderBase(MCSFinder):
  ''' Overrides self.invariant_check and run_simulation_forward() '''
  def __init__(self, event_dag, mcs):
    super(MockMCSFinderBase, self).__init__(MockSimulationConfig(), event_dag,
                                            invariant_check_name="InvariantChecker.check_liveness")
    # Hack! Give a fake name in config.invariant_checks.name_to_invariant_checks, but
    # but remove it from our dict directly after. This is to prevent
    # sanity check exceptions from being thrown.
    self.invariant_check = self._invariant_check
>>>>>>> 7dc03f6... Add option to ignore all internal events during replay
    self.new_dag = None
    self.mcs = mcs
    self.mcs_trace_path = None
    self.transform_dag = None
    self.simulation_cfg = None
    self.ignore_powersets = False
    self.no_violation_verification_runs = True
    self._extra_log = None
    self._runtime_stats = {}

  def log(self, message):
    self._log.info(message)

  def invariant_check(self, _):
    for e in self.mcs:
      if e not in self.new_dag._events_set:
        return []
    return ["violation"]

  def replay(self, new_dag, hook=None):
    self.new_dag = new_dag
    return self.invariant_check(new_dag)

# Horrible horrible hack. This way lies insanity
class MockMCSFinder(MockMCSFinderBase, MCSFinder):
  def __init__(self, event_dag, mcs):
    MockMCSFinderBase.__init__(self, event_dag, mcs)
    self._log = logging.getLogger("mock_mcs_finder")

class MockEfficientMCSFinder(MockMCSFinderBase, EfficientMCSFinder):
  def __init__(self, event_dag, mcs):
    MockMCSFinderBase.__init__(self, event_dag, mcs)
    self._log = logging.getLogger("mock_efficient_mcs_finder")

class MockInputEvent(InputEvent):
  def __init__(self, fingerprint=None, **kws):
    super(MockInputEvent, self).__init__(**kws)
    self.fingerprint = fingerprint

  def proceed(self, simulation):
    return True

class MCSFinderTest(unittest.TestCase):
  def test_basic(self):
    self.basic(MockMCSFinder)

  def test_basic_efficient(self):
    self.basic(MockEfficientMCSFinder)

  def basic(self, mcs_finder_type):
    trace = [ MockInputEvent(fingerprint=("class",f)) for f in range(1,7) ]
    dag = EventDag(trace)
    mcs = [trace[0]]
    mcs_finder = mcs_finder_type(dag, mcs)
    result = mcs_finder.simulate()
    self.assertEqual(mcs, result)

  def test_straddle(self):
    self.straddle(MockMCSFinder)

  def test_straddle_efficient(self):
    self.straddle(MockEfficientMCSFinder)

  def straddle(self, mcs_finder_type):
    trace = [ MockInputEvent(fingerprint=("class",f)) for f in range(1,7) ]
    dag = EventDag(trace)
    mcs = [trace[0],trace[5]]
    mcs_finder = mcs_finder_type(dag, mcs)
    result = mcs_finder.simulate()
    self.assertEqual(mcs, result)

  def test_all(self):
    self.all(MockMCSFinder)

  def test_all_efficient(self):
    self.all(MockEfficientMCSFinder)

  def all(self, mcs_finder_type):
    trace = [ MockInputEvent(fingerprint=("class",f)) for f in range(1,7) ]
    dag = EventDag(trace)
    mcs = trace
    mcs_finder = mcs_finder_type(dag, mcs)
    result = mcs_finder.simulate()
    self.assertEqual(mcs, result)

if __name__ == '__main__':
  unittest.main()
