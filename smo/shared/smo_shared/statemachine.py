"""Minimal finite-state-machine base every module's lifecycle implementation
extends. Deliberately small: this SMO's state machines (WriteConfigJob,
SoftwareManagementJob, O1AdaptorEndpoint health, ApplicationPackage,
RAppInstance, MLModel, InferenceJob) are all simple enough that a full FSM
framework would be more machinery than the problem needs — each one is just
a set of (from_state, event) -> to_state transitions plus optional guards.

PR-MSG-3.5: every transition taken is handed to the functions in `TRANSITION_LISTENERS` (by default `smo_shared.events.on_transition`, which publishes it on
the event bus when the context names the entity, e.g. `instance=`). One hook here, so no module publishes its state changes by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

from .metrics import record_illegal_transition, record_transition

S = TypeVar("S")  # state enum/str type
E = TypeVar("E")  # event enum/str type


def _publish_transition(machine: str, from_state: Any, event: Any, to_state: Any, context: dict) -> None:
    """The default listener: the event bus's (imported here, so a machine used without a database never loads it)."""
    from .events import on_transition
    on_transition(machine, from_state, event, to_state, context)


# What is told of every transition taken: (machine name, from state, event, to state, the context `fire` was given). A test may replace the list's content.
TRANSITION_LISTENERS: list[Callable[[str, Any, Any, Any, dict], None]] = [_publish_transition]


class IllegalTransition(Exception):
    """Raised by `StateMachine.fire` when no transition fits; carries the `state` and the `event`. A route turns it into 409
    LIFECYCLE_ILLEGAL_TRANSITION with `errors.illegal_transition_error`.
    """
    def __init__(self, state: S, event: E):
        super().__init__(f"no transition for event {event!r} in state {state!r}")
        self.state = state
        self.event = event


@dataclass
class Transition(Generic[S, E]):
    """One row of a transition table: in `from_state`, `event` leads to `to_state`, if `guard(**context)` is true (or there is no guard), running
    `action(**context)` when taken.
    """
    from_state: S
    event: E
    to_state: S
    guard: Callable[..., bool] | None = None  # optional precondition, e.g. schema-checked
    action: Callable[..., None] | None = None  # optional side effect, e.g. publish DME event


@dataclass
class StateMachine(Generic[S, E]):
    """Instantiate once per model class (e.g. WriteConfigJobFSM), reuse across
    every instance of that model — this holds the transition TABLE, not any
    one object's current state.
    """

    transitions: list[Transition[S, E]] = field(default_factory=list)

    def add(self, from_state: S, event: E, to_state: S, guard=None, action=None) -> "StateMachine":
        """Appends a transition to the table and returns the machine, so definitions can be chained. Transitions for the same (state, event) are tried
        in the order added.
        """
        self.transitions.append(Transition(from_state, event, to_state, guard, action))
        return self

    def fire(self, current_state: S, event: E, **context) -> S:
        """Evaluate every matching transition's guard in order; the first
        whose guard passes (or has none) wins. Raises IllegalTransition if
        no transition matches at all, or every guard rejects.
        """
        candidates = [t for t in self.transitions if t.from_state == current_state and t.event == event]
        machine = type(current_state).__name__                       # PR-OBS-2.5: the state enum names the machine
        if not candidates:
            record_illegal_transition(machine, current_state, event)
            raise IllegalTransition(current_state, event)
        for t in candidates:
            if t.guard is None or t.guard(**context):
                if t.action is not None:
                    t.action(**context)
                record_transition(machine, current_state, event, t.to_state)
                if context:                                          # MSG-3.5: a transition with nothing to say what it is about publishes nothing
                    for listener in TRANSITION_LISTENERS:
                        listener(machine, current_state, event, t.to_state, context)
                return t.to_state
        record_illegal_transition(machine, current_state, event)
        raise IllegalTransition(current_state, event)

    def legal_events(self, current_state: S) -> list[E]:
        return [t.event for t in self.transitions if t.from_state == current_state]
