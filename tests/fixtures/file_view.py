"""Native file contracts compose file operations with independent task execution."""
import os
from redpanda.filesystem.file_view import Client
from redpanda.execution.process import Running


class CommandClient(Client):
    def __init__(self, executable, store, base, *, command_environment=None, service_environment=None):
        self.command_environment = dict(os.environ if command_environment is None else command_environment)
        super().__init__(executable, store, base, service_environment=service_environment)

    def run(self, command_id, argv, *, timeout=60, environment=None, cancel_event=None):
        begin = self.begin(command_id)
        if begin['status']!='active': return begin
        running = Running(argv,begin['mount'],self.command_environment if environment is None else environment,self.store/'commands'/command_id)
        result = running.wait(timeout,cancel_event)
        from redpanda.filesystem.file_view.publication import atomic
        atomic(self.store/'commands'/command_id/'execution.json',result)
        # A nonzero exit remains evidence; accepting partial effects is an explicit choice.
        return {'execution':result,'files':self.request('finish')}
