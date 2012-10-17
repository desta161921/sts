from sts.entities import Link
from sts.god_scheduler import PendingReceive, MessageReceipt
from sts.input_traces.fingerprints import *
from sts.replay_event import *
import sts.control_flow
from invariant_checker import InvariantChecker
import itertools
import abc
import logging
import time
import json
import math
import pytrie
from collections import namedtuple
from sts.syncproto.base import SyncTime
log = logging.getLogger("event_dag")

class EventDag(object):
  # We peek ahead this many seconds after the timestamp of the subseqeunt
  # event
  # TODO(cs): be smarter about this -- peek() too far, and peek()'ing not far
  # enough can both have negative consequences
  _peek_seconds = 3.0
  # If we prune a failure, make sure that the subsequent
  # recovery doesn't occur
  _failure_types = set([SwitchFailure, LinkFailure, ControllerFailure, ControlChannelBlock])
  # NOTE: we treat failure/recovery as an atomic pair, since it doesn't make
  # much sense to prune a recovery event
  _recovery_types = set([SwitchRecovery, LinkRecovery, ControllerRecovery, ControlChannelUnblock])
  # For now, we're ignoring these input types, since their dependencies with
  # other inputs are too complicated to model
  # TODO(cs): model these!
  _ignored_input_types = set([DataplaneDrop, DataplanePermit, HostMigration])

  '''A collection of Event objects. EventDags are primarily used to present a
  view of the underlying events with some subset of the input events pruned
  '''
  def __init__(self, events, is_view=False, prefix_trie=None,
               label2event=None, ignore_unsupported_input_types=False):
    '''events is a list of EventWatcher objects. Refer to log_parser.parse to
    see how this is assembled.'''
    if ignore_unsupported_input_types:
      self._events_list = [ e for e in events
                            if events not in self._ignored_input_types ]
    else:
      self._events_list = events
    self._populate_indices(label2event)

    # Fill in domain knowledge about valid input
    # sequences (e.g. don't prune failure without pruning recovery.)
    # Only do so if this isn't a view of a previously computed DAG
    # TODO(cs): there is probably a cleaner way to implement views
    if not is_view:
      self._mark_invalid_input_sequences()
      prefix_trie = pytrie.Trie()
    # The prefix trie stores lists of input events as keys,
    # and lists of both input and internal events as values
    # Note that we pass the trie around between DAG views
    self._prefix_trie = prefix_trie

  def _populate_indices(self, label2event):
    self._event_to_index = {
      e : i
      for i, e in enumerate(self._events_list)
    }
    # Optimization: only compute label2event once (at the first unpruned
    # initialization)
    # TODO(cs): need to ensure that newly added events get labeled
    # uniquely. (Easy, but inelegant way: set the label generator's
    # initial value to max(labels) + 1)
    if label2event is None:
      self._label2event = {
        event.label : event
        for event in self._events_list
      }
    else:
      self._label2event = label2event

  @property
  def events(self):
    '''Return the events in the DAG'''
    return self._events_list

  @property
  def event_watchers(self):
    '''Return a generator of the EventWatchers in the DAG'''
    return map(EventWatcher, self._events_list)

  def _remove_event(self, event):
    ''' Recursively remove the event and its dependents '''
    if event in self._event_to_index:
      list_idx = self._event_to_index[event]
      del self._event_to_index[event]
      self._event_list.pop(list_idx)

    # Note that dependent_labels only contains dependencies between input
    # events. We run peek() to infer dependencies with internal events
    for label in event.dependent_labels:
      if label in self._label2event:
        dependent_event = self._label2event[label]
        if dependent_event in self._event_to_index:
          self._remove_event(dependent_event)

  def remove_events(self, ignored_portion, simulation):
    ''' Mutate the DAG: remove all input events in ignored_inputs,
    as well all of their dependent input events'''
    # Note that we treat failure/recovery as an atomic pair, so we don't prune
    # recovery events on their own
    for event in [ e for e in ignored_portion
                   if (isinstance(e, InputEvent) and
                       type(e) not in self._recovery_types) ]:
      self._remove_event(event)
    # Now run peek() to hide the internal events that will no longer occur
    # Note that causal dependencies change depending on what the prefix is!
    # So we have to run peek() once per prefix
    self.peek(simulation)

  def ignore_portion(self, ignored_portion, simulation):
    ''' Return a view of the dag with ignored_portion and its dependents
    removed'''
    dag = EventDag(list(self._events_list), is_view=True,
                   prefix_trie=self._prefix_trie,
                   label2event=self._label2event)
    # TODO(cs): potentially some redundant computation here
    dag.remove_events(ignored_portion, simulation)
    return dag

  def split_inputs(self, split_ways):
    ''' Split our events into split_ways separate lists '''
    events = self._events_list
    if len(events) == 0:
      return [[]]
    if split_ways == 1:
      return [events]
    if split_ways < 1 or split_ways > len(events):
      raise ValueError("Invalid split ways %d" % split_ways)

    splits = []
    split_interval = int(math.ceil(len(events) * 1.0 / split_ways))
    start_idx = 0
    split_idx = start_idx + split_interval
    while start_idx < len(events):
      splits.append(events[start_idx:split_idx])
      start_idx = split_idx
      # Account for odd numbered splits -- if we're about to eat up
      # all elements even though we will only have added split_ways-1
      # splits, back up the split interval by 1
      if (split_idx + split_interval >= len(events) and
          len(splits) == split_ways - 2):
        split_interval -= 1
      split_idx += split_interval
    return splits

  def peek(self, simulation):
    ''' Infer which internal events are/aren't going to occur, '''
    # TODO(cs): optimization: write the prefix trie to a file, in case we want to run
    # FindMCS again?
    input_events = [ e for e in self._events_list if isinstance(e, InputEvent) ]
    if len(input_events) == 0:
      # Postcondition: input_events[-1] is not None
      #                and self._events_list[-1] is not None
      return

    # Note that we recompute wait times for every view, since the set of
    # inputs and intervening expected internal events changes
    def get_wait_times(input_events):
      event2wait_time = {}
      for i in xrange(0, len(input_events)-1):
        current_input = input_events[i]
        next_input = input_events[i+1]
        wait_time = next_input.time.as_float() + self._peek_seconds
        event2wait_time[current_input] = wait_time
      # For the last event, we wait until the last internal event
      last_wait_time = self._events_list[-1].time.as_float() + self._peek_seconds
      event2wait_time[input_events[-1]] = last_wait_time
      return event2wait_time

    event2wait_time = get_wait_times(input_events)

    # Also compute the internal events that we expect for each interval between
    # input events
    def get_expected_internal_events(input_events):
      input_to_exected_events = {}
      for i in xrange(0, len(input_events)-1):
        # Infer the internal events that we expect
        current_input = input_events[i]
        current_input_idx = self._event_to_index[current_input]
        next_input = input_events[i+1]
        next_input_idx = self._event_to_index[next_input]
        expected_internal_events = \
                self._events_list[current_input_idx+1:next_input_idx]
        input_to_expected_events[current_input] = expected_internal_events
      # The last input's expected internal events are anything that follow it
      # in the log.
      last_input = input_events[-1]
      last_input_idx = self._event_to_index[last_input]
      input_to_expected_events[last_input] = self._events_list[last_input_idx:]
      return input_to_exected_events

    input_to_expected_events = get_expected_internal_events(input_events)

    # Now, play the execution forward iteratively for each input event, and
    # record what internal events happen between the injection and the
    # wait_time

    # Initilize current_input_prefix to the longest_match prefix we've
    # inferred previously (or [] if this is an entirely new prefix)
    current_input_prefix = self._prefix_trie\
                               .longest_prefix(input_events, default=[])

    # The value is both internal events and input events (values of the trie)
    # leading up to, but not including the next input following the tail of the prefix
    inferred_events = self._prefix_trie\
                          .longest_prefix_value(input_events, default=[])

    # current_input is the next input after the tail of the prefix
    if current_input_prefix == []:
      current_input_idx = 0
    else:
      current_input_idx = input_events.index(current_input_prefix[-1]) + 1

    while current_input_idx < len(input_events):
      current_input = input_events[current_input_idx]
      expected_internal_events = input_to_expected_events[current_input]
      # Optimization: if no internal events occured between this input and the
      # next, no need to peek()
      if expected_internal_events == []:
        newly_inferred_events = []
      else:
        # Now actually do the peek()'ing! First replay the prefix
        # plus the next input
        prefix_dag = EventDag(inferred_events + [current_input])
        replayer = control_flow.Replayer(prefix_dag, ignore_unsupported_input_types=True)
        replayer.simulate(simulation)

        # Directly after the last input has been injected, flush the internal
        # event buffers in case there were unaccounted internal events
        # Note that there isn't a race condition between flush()'ing and
        # incoming internal events, since sts is single-threaded
        simulation.god_scheduler.flush()
        simulation.controller_sync_callback.flush()

        # Now set all internal event buffers (GodScheduler for
        # ControlMessageReceives and ReplaySyncCallback for state changes)
        # to "pass through + record" by defining event handlers
        newly_inferred_events = []
        def receipt_pass_through(receipt_event):
          pending_receipt = receipt_event.pending_receipt
          # Pass through
          simulation.god_scheduler.schedule(pending_receipt)
          # Record
          replay_event = ControlMessageReceive(pending_receipt.dpid,
                                               pending_receipt.controller_id,
                                               pending_receipt.fingerprint.to_dict())
          newly_inferred_events.append(replay_event)

        simulation.god_scheduler.addListener(MessageReceipt,
                                             receipt_pass_through)

        def state_change_pass_through(state_change_event):
          state_change = state_change_event.pending_state_change
          # Pass through
          simulation.controller_sync_callback.gc_pending_state_change(state_change)
          # Record
          replay_event = ControllerStateChange(state_change.controller_id,
                                               state_change.time,
                                               state_change.fingerprint,
                                               state_change.name,
                                               state_change.value)
          newly_inferred_events.append(replay_event)

        simulation.controller_sync_callback.addListener(control_flow.StateChange,
                                                        state_change_pass_through)

        # Now sit tight for wait_seconds
        wait_seconds = event2wait_time[current_input]
        # Note that this is the monkey patched version of time.sleep
        time.sleep(wait_seconds)

        # Turn off those listeners
        simulation.god_scheduler.removeListener(receipt_pass_through)
        simulation.controller_sync_callback.removeListener(state_change_pass_through)

        # TODO(cs): slurp up the internal events and
        # do the fingerprint matching
        # Make sure to ignore any events after the point where the next input
        # is supposed to be injected

      # Update the trie for this prefix
      current_input_prefix.append(current_input)
      inferred_events.append(current_input)
      inferred_events += newly_inferred_events
      self._prefix_trie[current_input_prefix] = inferred_events

      current_input_idx += 1

    # Now that the new execution has been inferred,
    # present a view of ourselves that only includes the updated
    # events
    self._events_list = inferred_events
    self._populate_indices(self._label2event)

  def _mark_invalid_input_sequences(self):
    # Note: we treat each failure/recovery pair atomically, since it doesn't
    # make much sense to prune recovery events. Also note that that we will
    # never see two failures (for a particular node) in a row without an
    # interleaving recovery event
    fingerprint2previousfailure = {}

    # NOTE: mutates self._events
    for event in self._events_list:
      if type(event) in self._failure_types:
        # Insert it into the previous failure hash
        fingerprint2previousfailure[event.fingerprint] = event
      elif type(event) in self._recovery_types:
        # Check if there were any failure predecessors
        if event.fingerprint in fingerprint2previousfailure:
          failure = fingerprint2previousfailure[event.fingerprint]
          failure.dependent_labels.append(event.label)
      elif type(event) in self._ignored_input_types:
        raise RuntimeError("No support for %s dependencies" %
                            type(event).__name__)

class EventWatcher(object):
  '''EventWatchers watch events. This class can be used to wrap either
  InternalEvents or ExternalEvents to perform pre and post functionality.'''

  def __init__(self, event):
    self.event = event

  def run(self, simulation):
    self._pre()

    while not self.event.proceed(simulation):
      time.sleep(0.05)
      log.debug(".")

    self._post()

  def _pre(self):
    log.debug("Executing %s" % str(self.event))

  def _post(self):
    log.debug("Finished Executing %s" % str(self.event))
