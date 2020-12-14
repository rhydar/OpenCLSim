"""Base classes for the openclsim activities."""

from abc import ABC
from functools import partial

import simpy

import openclsim.core as core


class AbstractPluginClass(ABC):
    """
    Abstract class used as the basis for all Classes implementing a plugin for a specific Activity.

    Instance checks will be performed on this class level.
    """

    def __init__(self):
        pass

    def pre_process(self, env, activity_log, activity, *args, **kwargs):
        return {}

    def post_process(
        self,
        env,
        activity_log,
        activity,
        start_preprocessing,
        start_activity,
        *args,
        **kwargs,
    ):
        return {}

    def validate(self):
        pass


class StartSubProcesses:
    """Mixin for the activities that want to execute their sub_processes in sequence."""

    def start_sequential_subprocesses(self):
        self.start_sequence = self.env.event()

        for (i, sub_process) in enumerate(self.sub_processes):
            start_event = sub_process.start_event
            if isinstance(start_event, dict) or isinstance(start_event, simpy.Event):
                start_event = [start_event]
            if start_event is None:
                start_event = []
            if isinstance(start_event, list):
                pass
            else:
                raise ValueError(f"{type(start_event)} is not a valid type.")

            if i == 0:
                start_event.append(self.start_sequence)
                sub_process.start_event = [{"and": start_event}]
            else:
                start_event.append(
                    {
                        "type": "activity",
                        "state": "done",
                        "name": self.sub_processes[i - 1].name,
                    }
                )
                sub_process.start_event = [{"and": start_event}]

    def start_parallel_subprocesses(self):
        self.start_parallel = self.env.event()

        for (i, sub_process) in enumerate(self.sub_processes):
            start_event = sub_process.start_event
            if isinstance(start_event, dict) or isinstance(start_event, simpy.Event):
                start_event = [start_event]
            if start_event is None:
                start_event = []
            if isinstance(start_event, list):
                pass
            else:
                raise ValueError(f"{type(start_event)} is not a valid type.")

            start_event.append(self.start_parallel)
            sub_process.start_event = [{"and": start_event}]


class PluginActivity(core.Identifiable, core.Log):
    """
    Base class for all activities which will provide a plugin mechanism.

    The plugin mechanism foresees that the plugin function pre_process is called before the activity is executed, while
    the function post_process is called after the activity has been executed.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plugins = list()

    def register_plugin(self, plugin, priority=0):
        self.plugins.append({"priority": priority, "plugin": plugin})
        self.plugins = sorted(self.plugins, key=lambda x: x["priority"])

    def pre_process(self, args_data):
        # iterating over all registered plugins for this activity calling pre_process
        for item in self.plugins:
            yield from item["plugin"].pre_process(**args_data)

    def post_process(self, *args, **kwargs):
        # iterating over all registered plugins for this activity calling post_process
        for item in self.plugins:
            yield from item["plugin"].post_process(*args, **kwargs)

    def delay_processing(self, env, activity_label, activity_log, waiting):
        activity_log.log_entry(
            t=env.now,
            activity_id=activity_log.id,
            activity_state=core.LogState.WAIT_START,
            activity_label=activity_label,
        )
        yield env.timeout(waiting)
        activity_log.log_entry(
            t=env.now,
            activity_id=activity_log.id,
            activity_state=core.LogState.WAIT_STOP,
            activity_label=activity_label,
        )


class GenericActivity(PluginActivity):
    """The GenericActivity Class forms a generic class which sets up all activites."""

    def __init__(
        self,
        registry,
        postpone_start=False,
        start_event=None,
        requested_resources=dict(),
        keep_resources=list(),
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        """Initialization"""
        self.registry = registry
        self.postpone_start = postpone_start
        self.start_event = start_event
        self.requested_resources = requested_resources
        self.keep_resources = keep_resources
        self.done_event = self.env.event()

    def register_process(self, log_wait=True):
        # replace the done event
        self.done_event = self.env.event()

        start_event = (
            None
            if self.start_event is None
            else self.parse_expression(self.start_event)
        )

        main_proc = self.main_process_function
        if start_event is not None:
            main_proc = partial(
                self.delayed_process,
                start_event=start_event,
                sub_processes=[main_proc],
                additional_logs=getattr(self, "additional_logs", []),
                requested_resources=self.requested_resources,
                keep_resources=self.keep_resources,
                log_wait=log_wait,
            )

        self.main_process = self.env.process(main_proc(activity_log=self, env=self.env))

        # add activity to the registry
        self.registry.setdefault("name", {}).setdefault(self.name, []).append(self)
        self.registry.setdefault("id", {}).setdefault(self.id, []).append(self)

    def parse_expression(self, expr):
        if isinstance(expr, simpy.Event):
            return expr
        if isinstance(expr, list):
            return self.env.all_of([self.parse_expression(item) for item in expr])
        if isinstance(expr, dict):
            if "and" in expr:
                return self.env.all_of(
                    [self.parse_expression(item) for item in expr["and"]]
                )
            if "or" in expr:
                return self.env.any_of(
                    [self.parse_expression(item) for item in expr["or"]]
                )
            if expr.get("type") == "container":
                id_ = expr.get("id_", "default")
                obj = expr["concept"]
                if expr["state"] == "full":
                    return obj.container.get_full_event(id_=id_)
                elif expr["state"] == "empty":
                    return obj.container.get_empty_event(id_=id_)
                raise ValueError

            if expr.get("type") == "activity":
                if expr.get("state") != "done":
                    raise ValueError(
                        f"Unknown state {expr.get('state')} in ActivityExpression."
                    )
                key = expr.get("ID", expr.get("name"))
                activity_ = self.registry.get("id", {}).get(
                    key, self.registry.get("name", {}).get(key)
                )

                if activity_ is None:
                    raise Exception(
                        f"No activity found in ActivityExpression for id/name {key}"
                    )
                return self.env.all_of(
                    [activity_item.get_done_event() for activity_item in activity_]
                )

            raise ValueError

        raise ValueError(
            f"{type(expr)} is not a valid input type. Valid input types are: simpy.Event, dict, and list"
        )

    def get_done_event(self):
        if self.postpone_start:
            return self.done_event
        return getattr(self, "main_process", self.done_event)

    def call_main_proc(self, activity_log, env):
        res = self.main_proc(activity_log=activity_log, env=env)
        return res

    def end(self):
        self.done_event.succeed()

    def delayed_process(
        self,
        activity_log,
        env,
        start_event,
        sub_processes,
        requested_resources,
        keep_resources,
        additional_logs=[],
        log_wait=True,
    ):
        """
        Return a generator which can be added as a process to a simpy environment.

        In the process the given
        sub_processes will be executed after the given start_event occurs.

        activity_log: the core.Log object in which log_entries about the activities progress will be added.
        env: the simpy.Environment in which the process will be run
        start_event: a simpy.Event object, when this event occurs the delayed process will start executing its sub_processes
        sub_processes: an Iterable of methods which will be called with the activity_log and env parameters and should
                    return a generator which could be added as a process to a simpy.Environment
                    the sub_processes will be executed sequentially, in the order in which they are given after the
                    start_event occurs
        """
        if hasattr(start_event, "__call__"):
            start_event = start_event()

        if log_wait:
            activity_log.log_entry(
                t=env.now,
                activity_id=activity_log.id,
                activity_state=core.LogState.WAIT_START,
            )
            if isinstance(additional_logs, list) and len(additional_logs) > 0:
                for log in additional_logs:
                    for sub_process in sub_processes:
                        log.log_entry(
                            t=env.now,
                            activity_id=activity_log.id,
                            activity_state=core.LogState.WAIT_START,
                        )

        yield start_event
        if log_wait:
            activity_log.log_entry(
                t=env.now,
                activity_id=activity_log.id,
                activity_state=core.LogState.WAIT_STOP,
            )
            if isinstance(additional_logs, list) and len(additional_logs) > 0:
                for log in additional_logs:
                    for sub_process in sub_processes:
                        log.log_entry(
                            t=env.now,
                            activity_id=activity_log.id,
                            activity_state=core.LogState.WAIT_STOP,
                        )

        for sub_process in sub_processes:
            yield from sub_process(activity_log=activity_log, env=env)

    def _request_resource(self, requested_resources, resource):
        """Request the given resource and yields it."""
        if resource not in requested_resources:
            requested_resources[resource] = resource.request()
            yield requested_resources[resource]

    def _release_resource(self, requested_resources, resource, kept_resource=None):
        """
        Release the given resource, provided it does not equal the kept_resource parameter.

        Deletes the released resource from the requested_resources dictionary.
        """
        if kept_resource is not None:
            if isinstance(kept_resource, list):
                if resource in [item.resource for item in kept_resource]:
                    return
            elif resource == kept_resource.resource or resource == kept_resource:
                return

        if resource in requested_resources.keys():
            resource.release(requested_resources[resource])
            del requested_resources[resource]
