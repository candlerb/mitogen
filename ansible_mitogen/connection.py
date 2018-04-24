# Copyright 2017, David Wilson
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import absolute_import
import logging
import os
import shlex
import sys
import time

import jinja2.runtime
import ansible.constants as C
import ansible.errors
import ansible.plugins.connection

import mitogen.unix
import mitogen.utils

import ansible_mitogen.target
import ansible_mitogen.process
import ansible_mitogen.services


LOG = logging.getLogger(__name__)


def _connect_local(spec):
    return {
        'method': 'local',
        'kwargs': {
            'python_path': spec['python_path'],
        }
    }


def _connect_ssh(spec):
    return {
        'method': 'ssh',
        'kwargs': {
            'check_host_keys': False,  # TODO
            'hostname': spec['remote_addr'],
            'username': spec['remote_user'],
            'password': spec['password'],
            'port': spec['port'],
            'python_path': spec['python_path'],
            'identity_file': spec['private_key_file'],
            'ssh_path': spec['ssh_executable'],
            'connect_timeout': spec['ansible_ssh_timeout'],
            'ssh_args': spec['ssh_args'],
        }
    }


def _connect_docker(spec):
    return {
        'method': 'docker',
        'kwargs': {
            'username': spec['remote_user'],
            'container': spec['remote_addr'],
            'python_path': spec['python_path'],
            'connect_timeout': spec['ansible_ssh_timeout'] or spec['timeout'],
        }
    }


def _connect_sudo(spec):
    return {
        'method': 'sudo',
        'kwargs': {
            'username': spec['become_user'],
            'password': spec['become_pass'],
            'python_path': spec['python_path'],
            'sudo_path': spec['sudo_exe'],
            'connect_timeout': spec['timeout'],
            'sudo_args': spec['sudo_args'],
        }
    }


CONNECTION_METHOD = {
    'sudo': _connect_sudo,
    'ssh': _connect_ssh,
    'local': _connect_local,
    'docker': _connect_docker,
}


def config_from_play_context(transport, inventory_name, connection):
    """
    Return a dict representing all important connection configuration, allowing
    the same functions to work regardless of whether configuration came from
    play_context (direct connection) or host vars (mitogen_via=).
    """
    return {
        'transport': transport,
        'inventory_name': inventory_name,
        'remote_addr': connection._play_context.remote_addr,
        'remote_user': connection._play_context.remote_user,
        'become': connection._play_context.become,
        'become_method': connection._play_context.become_method,
        'become_user': connection._play_context.become_user,
        'become_pass': connection._play_context.become_pass,
        'password': connection._play_context.password,
        'port': connection._play_context.port,
        'python_path': connection.python_path,
        'private_key_file': connection._play_context.private_key_file,
        'ssh_executable': connection._play_context.ssh_executable,
        'timeout': connection._play_context.timeout,
        'ansible_ssh_timeout': connection.ansible_ssh_timeout,
        'ssh_args': [
            term
            for s in (
                getattr(connection._play_context, 'ssh_args', ''),
                getattr(connection._play_context, 'ssh_common_args', ''),
                getattr(connection._play_context, 'ssh_extra_args', '')
            )
            for term in shlex.split(s or '')
        ],
        'sudo_exe': connection._play_context.sudo_exe,
        'sudo_args': [
            term
            for s in (
                connection._play_context.sudo_flags,
                connection._play_context.become_flags
            )
            for term in shlex.split(s or '')
        ],
        'mitogen_via': connection.mitogen_via,
    }


def config_from_hostvars(transport, inventory_name, connection,
                         hostvars, become_user):
    """
    Override config_from_play_context() to take equivalent information from
    host vars.
    """
    config = config_from_play_context(transport, inventory_name, connection)
    hostvars = dict(hostvars)
    return dict(config, **{
        'remote_addr': hostvars.get('ansible_hostname', inventory_name),
        'become': bool(become_user),
        'become_user': become_user,
        'become_pass': None,
        'remote_user': hostvars.get('ansible_user'),  # TODO
        'password': (hostvars.get('ansible_ssh_pass') or
                     hostvars.get('ansible_password')),
        'port': hostvars.get('ansible_port'),
        'python_path': hostvars.get('ansible_python_interpreter'),
        'private_key_file': (hostvars.get('ansible_ssh_private_key_file') or
                             hostvars.get('ansible_private_key_file')),
        'mitogen_via': hostvars.get('mitogen_via'),
    })


class Connection(ansible.plugins.connection.ConnectionBase):
    #: mitogen.master.Broker for this worker.
    broker = None

    #: mitogen.master.Router for this worker.
    router = None

    #: mitogen.master.Context representing the parent Context, which is
    #: presently always the master process.
    parent = None

    #: mitogen.master.Context connected to the target user account on the
    #: target machine (i.e. via sudo).
    context = None

    #: Only sudo is supported for now.
    become_methods = ['sudo']

    #: Set to 'ansible_python_interpreter' by on_action_run().
    python_path = None

    #: Set to 'ansible_sudo_exe' by on_action_run().
    sudo_exe = None

    #: Set to 'ansible_ssh_timeout' by on_action_run().
    ansible_ssh_timeout = None

    #: Set to 'mitogen_via' by on_action_run().
    mitogen_via = None

    #: Set to 'inventory_hostname' by on_action_run().
    inventory_hostname = None

    #: Set to 'hostvars' by on_action_run()
    host_vars = None

    #: Set after connection to the target context's home directory.
    _homedir = None

    def __init__(self, play_context, new_stdin, **kwargs):
        assert ansible_mitogen.process.MuxProcess.unix_listener_path, (
            'Mitogen connection types may only be instantiated '
             'while the "mitogen" strategy is active.'
        )
        super(Connection, self).__init__(play_context, new_stdin)

    def __del__(self):
        """
        Ansible cannot be trusted to always call close() e.g. the synchronize
        action constructs a local connection like this. So provide a destructor
        in the hopes of catching these cases.
        """
        # https://github.com/dw/mitogen/issues/140
        self.close()

    def on_action_run(self, task_vars):
        """
        Invoked by ActionModuleMixin to indicate a new task is about to start
        executing. We use the opportunity to grab relevant bits from the
        task-specific data.
        """
        self.ansible_ssh_timeout = task_vars.get('ansible_ssh_timeout')
        self.python_path = task_vars.get('ansible_python_interpreter',
                                         '/usr/bin/python')
        self.sudo_exe = task_vars.get('ansible_sudo_exe', C.DEFAULT_SUDO_EXE)
        self.mitogen_via = task_vars.get('mitogen_via')
        self.inventory_hostname = task_vars['inventory_hostname']
        self.host_vars = task_vars['hostvars']
        self.close(new_task=True)

    @property
    def homedir(self):
        self._connect()
        return self._homedir

    @property
    def connected(self):
        return self.context is not None

    def _config_from_via(self, via_spec):
        become_user, _, inventory_name = via_spec.rpartition('@')
        via_vars = self.host_vars[inventory_name]
        if isinstance(via_vars, jinja2.runtime.Undefined):
            raise ansible.errors.AnsibleConnectionFailure(
                self.unknown_via_msg % (
                    self.mitogen_via,
                    config['inventory_name'],
                )
            )

        return config_from_hostvars(
            transport=via_vars.get('ansible_connection', 'ssh'),
            inventory_name=inventory_name,
            connection=self,
            hostvars=via_vars,
            become_user=become_user or None,
        )

    unknown_via_msg = 'mitogen_via=%s of %s specifies an unknown hostname'
    via_cycle_msg = 'mitogen_via=%s of %s creates a cycle (%s)'

    def _stack_from_config(self, config, stack=(), seen_names=()):
        if config['inventory_name'] in seen_names:
            raise ansible.errors.AnsibleConnectionFailure(
                self.via_cycle_msg % (
                    config['mitogen_via'],
                    config['inventory_name'],
                    ' -> '.join(reversed(
                        seen_names + (config['inventory_name'],)
                    )),
                )
            )

        if config['mitogen_via']:
            stack, seen_names = self._stack_from_config(
                self._config_from_via(config['mitogen_via']),
                stack=stack,
                seen_names=seen_names + (config['inventory_name'],)
            )

        stack += (CONNECTION_METHOD[config['transport']](config),)
        if config['become']:
            stack += (CONNECTION_METHOD[config['become_method']](config),)

        return stack, seen_names

    def _connect(self):
        """
        Establish a connection to the master process's UNIX listener socket,
        constructing a mitogen.master.Router to communicate with the master,
        and a mitogen.master.Context to represent it.

        Depending on the original transport we should emulate, trigger one of
        the _connect_*() service calls defined above to cause the master
        process to establish the real connection on our behalf, or return a
        reference to the existing one.
        """
        if self.connected:
            return

        if not self.broker:
            self.broker = mitogen.master.Broker()
            self.router, self.parent = mitogen.unix.connect(
                path=ansible_mitogen.process.MuxProcess.unix_listener_path,
                broker=self.broker,
            )

        stack, _ = self._stack_from_config(
            config_from_play_context(
                transport=self.transport,
                inventory_name=self.inventory_hostname,
                connection=self
            )
        )

        dct = mitogen.service.call(
            context=self.parent,
            handle=ansible_mitogen.services.ContextService.handle,
            method='get',
            kwargs=mitogen.utils.cast({
                'stack': stack,
            })
        )

        if dct['msg']:
            if dct['method_name'] in self.become_methods:
                raise ansible.errors.AnsibleModuleError(dct['msg'])
            raise ansible.errors.AnsibleConnectionFailure(dct['msg'])

        self.context = dct['context']
        self._homedir = dct['home_dir']

    def get_context_name(self):
        """
        Return the name of the target context we issue commands against, i.e. a
        unique string useful as a key for related data, such as a list of
        modules uploaded to the target.
        """
        return self.context.name

    def close(self, new_task=False):
        """
        Arrange for the mitogen.master.Router running in the worker to
        gracefully shut down, and wait for shutdown to complete. Safe to call
        multiple times.
        """
        if self.context:
            mitogen.service.call(
                context=self.parent,
                handle=ansible_mitogen.services.ContextService.handle,
                method='put',
                kwargs={
                    'context': self.context
                }
            )

        self.context = None
        if self.broker and not new_task:
            self.broker.shutdown()
            self.broker.join()
            self.broker = None
            self.router = None

    def call_async(self, func, *args, **kwargs):
        """
        Start a function call to the target.

        :returns:
            mitogen.core.Receiver that receives the function call result.
        """
        self._connect()
        return self.context.call_async(func, *args, **kwargs)

    def call(self, func, *args, **kwargs):
        """
        Start and wait for completion of a function call in the target.

        :raises mitogen.core.CallError:
            The function call failed.
        :returns:
            Function return value.
        """
        t0 = time.time()
        try:
            return self.call_async(func, *args, **kwargs).get().unpickle()
        finally:
            LOG.debug('Call %s%r took %d ms', func.func_name, args,
                      1000 * (time.time() - t0))

    def exec_command(self, cmd, in_data='', sudoable=True, mitogen_chdir=None):
        """
        Implement exec_command() by calling the corresponding
        ansible_mitogen.target function in the target.

        :param str cmd:
            Shell command to execute.
        :param bytes in_data:
            Data to supply on ``stdin`` of the process.
        :returns:
            (return code, stdout bytes, stderr bytes)
        """
        emulate_tty = (not in_data and sudoable)
        rc, stdout, stderr = self.call(
            ansible_mitogen.target.exec_command,
            cmd=mitogen.utils.cast(cmd),
            in_data=mitogen.utils.cast(in_data),
            chdir=mitogen_chdir,
            emulate_tty=emulate_tty,
        )

        stderr += 'Shared connection to %s closed.%s' % (
            self._play_context.remote_addr,
            ('\r\n' if emulate_tty else '\n'),
        )
        return rc, stdout, stderr

    def fetch_file(self, in_path, out_path):
        """
        Implement fetch_file() by calling the corresponding
        ansible_mitogen.target function in the target.

        :param str in_path:
            Remote filesystem path to read.
        :param str out_path:
            Local filesystem path to write.
        """
        output = self.call(ansible_mitogen.target.read_path,
                           mitogen.utils.cast(in_path))
        ansible_mitogen.target.write_path(out_path, output)

    def put_data(self, out_path, data):
        """
        Implement put_file() by caling the corresponding
        ansible_mitogen.target function in the target.

        :param str out_path:
            Remote filesystem path to write.
        :param byte data:
            File contents to put.
        """
        self.call(ansible_mitogen.target.write_path,
                  mitogen.utils.cast(out_path),
                  mitogen.utils.cast(data))

    def put_file(self, in_path, out_path):
        """
        Implement put_file() by streamily transferring the file via
        FileService.

        :param str in_path:
            Local filesystem path to read.
        :param str out_path:
            Remote filesystem path to write.
        """
        mitogen.service.call(
            context=self.parent,
            handle=ansible_mitogen.services.FileService.handle,
            method='register',
            kwargs={
                'path': mitogen.utils.cast(in_path)
            }
        )
        self.call(
            ansible_mitogen.target.transfer_file,
            context=self.parent,
            in_path=in_path,
            out_path=out_path
        )


class SshConnection(Connection):
    transport = 'ssh'


class LocalConnection(Connection):
    transport = 'local'


class DockerConnection(Connection):
    transport = 'docker'
