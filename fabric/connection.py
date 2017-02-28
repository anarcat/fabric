from contextlib import contextmanager
from threading import Event
import socket

from invoke.vendor.decorator import decorator
from invoke.vendor import six

from invoke import Context
from invoke.exceptions import ThreadException
from paramiko.agent import AgentRequestHandler
from paramiko.client import SSHClient, AutoAddPolicy
from paramiko.config import SSHConfig
from paramiko.proxy import ProxyCommand

from .config import Config
from .runners import Remote
from .transfer import Transfer
from .tunnels import TunnelManager, Tunnel


@decorator
def opens(method, self, *args, **kwargs):
    self.open()
    return method(self, *args, **kwargs)


class Connection(Context):
    """
    A connection to an SSH daemon, with methods for commands and file transfer.

    **Basics**

    This class inherits from Invoke's `~invoke.context.Context`, as it is a
    context within which commands, tasks etc can operate. It also encapsulates
    a Paramiko `~paramiko.client.SSHClient` instance, performing useful high
    level operations with that `~paramiko.client.SSHClient` and
    `~paramiko.channel.Channel` instances generated from it.

    **Lifecycle**

    `.Connection` has a basic "`create <__init__>`, `connect/open <open>`, `do
    work <run>`, `disconnect/close <close>`" lifecycle:

    * `Instantiation <__init__>` imprints the object with its connection
      parameters (but does **not** actually initiate the network connection).
    * Methods like `run`, `get` etc automatically trigger a call to
      `open` if the connection is not active; users may of course call `open`
      manually if desired.
    * Connections do not always need to be explicitly closed; much of the
      time, Paramiko's garbage collection hooks or Python's own shutdown
      sequence will take care of things. **However**, should you encounter edge
      cases (for example, sessions hanging on exit) it's helpful to explicitly
      close connections when you're done with them.

      This can be accomplished by manually calling `close`, or by using the
      object as a contextmanager::

        with Connection('host') as cxn:
            cxn.run('command')
            cxn.put('file')

    .. note::
        This class rebinds `invoke.context.Context.run` to `.local` so both
        remote and local command execution can coexist.

    **Configuration**

    Most `.Connection` parameters honor :doc:`Invoke-style configuration
    </concepts/configuration>` as well as any applicable :ref:`SSH config file
    directives <connection-ssh-config>`. For example, to end up with a
    connection to ``admin@myhost``, one could:

    - Use any built-in config mechanism, such as ``/etc/fabric.yml``,
      ``~/.fabric.json``, collection-driven configuration, env vars, etc,
      stating ``user: admin`` (or ``{"user": "admin"}``, depending on config
      format.) Then ``Connection('myhost')`` would implicitly have a ``user``
      of ``admin``.
    - Use an SSH config file containing ``User admin`` within any applicable
      ``Host`` header (``Host myhost``, ``Host *``, etc.) Again,
      ``Connection('myhost')`` will default to an ``admin`` user.
    - Leverage host-parameter shorthand (described in `.Config.__init__`), i.e.
      ``Connection('admin@myhost')``.
    - Give the parameter directly: ``Connection('myhost', user='admin')``.

    The same applies to agent forwarding, gateways, and so forth.
    """
    # NOTE: these are initialized here to hint to invoke.Config.__setattr__
    # that they should be treated as real attributes instead of config proxies.
    # (Additionally, we're doing this instead of using invoke.Config._set() so
    # we can take advantage of Sphinx's attribute-doc-comment static analysis.)
    # Once an instance is created, these values will usually be non-None
    # because they default to the default config values.
    host = None
    original_host = None
    user = None
    port = None
    ssh_config = None
    gateway = None
    forward_agent = None
    connect_kwargs = None
    client = None
    transport = None
    _sftp = None
    _agent_handler = None

    # TODO: should "reopening" an existing Connection object that has been
    # closed, be allowed? (See e.g. how v1 detects closed/semi-closed
    # connections & nukes them before creating a new client to the same host.)
    # TODO: push some of this into paramiko.client.Client? e.g. expand what
    # Client.exec_command does, it already allows configuring a subset of what
    # we do / will eventually do / did in 1.x. It's silly to have to do
    # .get_transport().open_session().
    def __init__(
        self,
        host,
        user=None,
        port=None,
        config=None,
        gateway=None,
        forward_agent=None,
        connect_kwargs=None,
    ):
        """
        Set up a new object representing a server connection.

        :param str host:
            the hostname (or IP address) of this connection.

            May include shorthand for the ``user`` and/or ``port`` parameters,
            of the form ``user@host``, ``host:port``, or ``user@host:port``.

            .. note::
                Due to ambiguity, IPv6 host addresses are incompatible with the
                ``host:port`` shorthand (though ``user@host`` will still work
                OK). In other words, the presence of >1 ``:`` character will
                prevent any attempt to derive a shorthand port number; use the
                explicit ``port`` parameter instead.

            .. note::
                If ``host`` matches a ``Host`` clause in loaded SSH config
                data, and that ``Host`` clause contains a ``Hostname``
                directive, the resulting `.Connection` object will behave as if
                ``host`` is equal to that ``Hostname`` value.

                In all cases, the original value of ``host`` is preserved as
                the ``original_host`` attribute.

                Thus, given SSH config like so::

                    Host myalias
                        Hostname realhostname

                a call like ``Connection(host='myalias')`` will result in an
                object whose ``host`` attribute is ``realhostname``, and whose
                ``original_host`` attribute is ``myalias``.

        :param str user:
            the login user for the remote connection. Defaults to
            ``config.user``.

        :param int port:
            the remote port. Defaults to ``config.port``.

        :param config:
            configuration settings to use when executing methods on this
            `.Connection` (e.g. default SSH port and so forth).

            Should be a `.Config` or an `invoke.config.Config`
            (which will be turned into a `.Config`).

            Default is an anonymous `.Config` object.

        :param gateway:
            An object to use as a proxy or gateway for this connection.

            This parameter accepts one of the following:

            - another `.Connection` (for a ``ProxyJump`` style gateway);
            - a shell command string as a `str` or `unicode` (for a
              ``ProxyCommand`` style style gateway).

            Default: ``None``, meaning no gatewaying will occur (unless
            otherwise configured; if one wants to override a configured gateway
            at runtime, specify ``gateway=False``.)

            .. seealso:: :ref:`ssh-gateways`

        :param bool forward_agent:
            Whether to enable SSH agent forwarding.

            Default: ``False`` (same as OpenSSH).

        :param dict connect_kwargs:
            Keyword arguments handed verbatim to
            `SSHClient.connect <paramiko.client.SSHClient.connect>` (when
            `.open` is called).

            `.Connection` tries not to grow additional settings/kwargs of its
            own unless it is adding value of some kind; thus,
            ``connect_kwargs`` is currently the right place to hand in
            parameters such as ``pkey`` or ``key_filename``.

            Default: ``config.connect_kwargs``.

        :raises exceptions.ValueError:
            if user or port values are given via both ``host`` shorthand *and*
            their own arguments. (We `refuse the temptation to guess`_).

        .. _refuse the temptation to guess:
            http://zen-of-python.info/
            in-the-face-of-ambiguity-refuse-the-temptation-to-guess.html#12
        """
        # NOTE: for now, we don't call our parent __init__, since all it does
        # is set a default config (to Invoke's Config, not ours). If
        # invoke.Context grows more behavior later we may need to change this,
        # e.g. by having parent define a '_set_default_config' or whatnot.

        #: The .Config object referenced when handling default values (for e.g.
        #: user or port, when not explicitly given) or deciding how to behave.
        if config is None:
            config = Config()
        # Handle 'vanilla' Invoke config objects, which need cloning 'into' one
        # of our own Configs (which grants the new defaults, etc, while not
        # squashing them if the Invoke-level config already accounted for them)
        elif not isinstance(config, Config):
            config = config.clone(into=Config)
        self._set(_config=config)
        # TODO: when/how to run load_files, merge, load_shell_env, etc?
        # TODO: i.e. what is the lib use case here (and honestly in invoke too)

        shorthand = self.derive_shorthand(host)
        host = shorthand['host']
        err = "You supplied the {0} via both shorthand and kwarg! Please pick one." # noqa
        if shorthand['user'] is not None:
            if user is not None:
                raise ValueError(err.format('user'))
            user = shorthand['user']
        if shorthand['port'] is not None:
            if port is not None:
                raise ValueError(err.format('port'))
            port = shorthand['port']

        # NOTE: we load SSH config data as early as possible as it has
        # potential to affect nearly every other attribute.
        #: The per-host SSH config data, if any. (See :ref:`ssh-config`.)
        self.ssh_config = self.config.base_ssh_config.lookup(host)

        self.original_host = host
        #: The hostname of the target server.
        self.host = host
        if 'hostname' in self.ssh_config:
            # TODO: log that this occurred?
            self.host = self.ssh_config['hostname']

        #: The username this connection will use to connect to the remote end.
        self.user = user or self.ssh_config.get('user', self.config.user)

        #: The network port to connect on.
        self.port = port or int(self.ssh_config.get('port', self.config.port))

        # Non-None values - string, Connection, even eg False - get set
        # directly; None triggers seek in config/ssh_config
        if gateway is None:
            # SSH config wins over Invoke-style config
            if 'proxyjump' in self.ssh_config:
                # Happily, ProxyJump uses identical format to our host
                # shorthand...
                gateway = Connection(self.ssh_config['proxyjump'])
            elif 'proxycommand' in self.ssh_config:
                # Just a string, which we interpret as a proxy command..
                gateway = self.ssh_config['proxycommand']
            else:
                # Neither of those? Our config value please.
                gateway = self.config.gateway
        #: The gateway `.Connection` or ``ProxyCommand`` string to be used,
        #: if any.
        self.gateway = gateway
        # NOTE: we use string above, vs ProxyCommand obj, to avoid spinning up
        # the ProxyCommand subprocess at init time, vs open() time.
        # TODO: make paramiko.proxy.ProxyCommand lazy instead?

        if forward_agent is None:
            # Default to config...
            forward_agent = self.config.forward_agent
            # But if ssh_config is present, it wins
            if 'forwardagent' in self.ssh_config:
                # TODO: SSHConfig really, seriously needs some love here, god
                map_ = {'yes': True, 'no': False}
                forward_agent = map_[self.ssh_config['forwardagent']]
        #: Whether agent forwarding is enabled.
        self.forward_agent = forward_agent

        if connect_kwargs is None:
            # TODO: should they merge or is that too unclean?
            # TODO: how would a user then override (or derive from ssh_config)
            # just one setting? Feels like we want to rip out anything we
            # explicitly support via SSHConfig, from connect_kwargs, leaving it
            # solely for overrides/extensions. Still has the problem of wanting
            # to override just one key some of the time, but less likely?
            connect_kwargs = self.config.connect_kwargs
        #: Keyword arguments given to `paramiko.client.SSHClient.connect` when
        #: `open` is called.
        self.connect_kwargs = connect_kwargs

        #: The `paramiko.client.SSHClient` instance this connection wraps.
        client = SSHClient()
        client.set_missing_host_key_policy(AutoAddPolicy())
        self.client = client

        #: A convenience handle onto the return value of
        #: ``self.client.get_transport()``.
        self.transport = None

    def __str__(self):
        # Host comes first as it's the most common differentiator by far
        bits = [('host', self.host)]
        # TODO: maybe always show user regardless? Explicit is good...
        if self.user != self.config.user:
            bits.append(('user', self.user))
        # TODO: harder to make case for 'always show port'; maybe if it's
        # non-22 (even if config has overridden the local default)?
        if self.port != self.config.port:
            bits.append(('port', self.port))
        # NOTE: sometimes self.gateway may be eg False if someone wants to
        # explicitly override a configured non-None value (as otherwise it's
        # impossible for __init__ to tell if a None means "nothing given" or
        # "seriously please no gatewaying". So, this must always be a vanilla
        # truth test and not eg "is not None".
        if self.gateway:
            # Displaying type because gw params would probs be too verbose
            val = 'proxyjump'
            if isinstance(self.gateway, six.string_types):
                val = 'proxycommand'
            bits.append(('gw', val))
        return "<Connection {0}>".format(
            " ".join("{0}={1}".format(*x) for x in bits)
        )

    def _identity(self):
        return (self.host, self.user, self.port)

    def __eq__(self, other):
        if not isinstance(other, Connection):
            return False
        # TODO: consider including gateway and maybe even other init kwargs?
        # Whether two cxns w/ same user/host/port but different
        # gateway/keys/etc, should be considered "the same", is unclear.
        return self._identity() == other._identity()

    def __hash__(self):
        # NOTE: this departs from Context/DataProxy, which is not usefully
        # hashable.
        return hash(self._identity())

    def derive_shorthand(self, host_string):
        user_hostport = host_string.rsplit('@', 1)
        hostport = user_hostport.pop()
        user = user_hostport[0] if user_hostport and user_hostport[0] else None

        # IPv6: can't reliably tell where addr ends and port begins, so don't
        # try (and don't bother adding special syntax either, user should avoid
        # this situation by using port=).
        if hostport.count(':') > 1:
            host = hostport
            port = None
        # IPv4: can split on ':' reliably.
        else:
            host_port = hostport.rsplit(':', 1)
            host = host_port.pop(0) or None
            port = host_port[0] if host_port and host_port[0] else None

        if port is not None:
            port = int(port)

        return {'user': user, 'host': host, 'port': port}

    @property
    def is_connected(self):
        """
        Whether or not this connection is actually open.
        """
        return self.transport.active if self.transport else False

    def open(self):
        """
        Initiate an SSH connection to the host/port this object is bound to.

        This may include activating the configured gateway connection, if one
        is set.

        Also saves a handle to the now-set Transport object for easier access.

        Various connect-time settings (and/or their corresponding :ref:`SSH
        config options <ssh-config>`) are utilized here in the call to
        `SSHClient.connect <paramiko.client.SSHClient.connect>`. (For details,
        see :doc:`the configuration docs </concepts/configuration>`.)
        """
        if not self.is_connected:
            kwargs = dict(
                username=self.user,
                hostname=self.host,
                port=self.port,
            )
            if self.gateway:
                kwargs['sock'] = self.open_gateway()
            kwargs.update(self.connect_kwargs)
            self.client.connect(**kwargs)
            self.transport = self.client.get_transport()

    def open_gateway(self):
        """
        Obtain a socket-like object from `gateway`.

        :returns:
            A ``direct-tcpip`` `paramiko.channel.Channel`, if `gateway` was a
            `.Connection`; or a `~paramiko.proxy.ProxyCommand`, if `gateway`
            was a `str` or `unicode`.
        """
        # ProxyCommand is faster to set up, so do it first.
        if isinstance(self.gateway, six.string_types):
            # Leverage a dummy SSHConfig to ensure %h/%p/etc are parsed.
            # TODO: use real SSH config once loading one properly is
            # implemented.
            ssh_conf = SSHConfig()
            dummy = "Host {0}\n    ProxyCommand {1}"
            ssh_conf.parse(six.StringIO(dummy.format(self.host, self.gateway)))
            return ProxyCommand(ssh_conf.lookup(self.host)['proxycommand'])
        # Handle inner-Connection gateway type here.
        # TODO: logging
        self.gateway.open()
        # TODO: expose the opened channel itself as an attribute? (another
        # possible argument for separating the two gateway types...) e.g. if
        # someone wanted to piggyback on it for other same-interpreter socket
        # needs...
        # TODO: and the inverse? allow users to supply their own socket/like
        # object they got via $WHEREEVER?
        # TODO: how best to expose timeout param? reuse general connection
        # timeout from config?
        return self.gateway.transport.open_channel(
            kind='direct-tcpip',
            dest_addr=(self.host, int(self.port)),
            # NOTE: src_addr needs to be 'empty but not None' values to
            # correctly encode into a network message. Theoretically Paramiko
            # could auto-interpret None sometime & save us the trouble.
            src_addr=('', 0),
        )

    def close(self):
        """
        Terminate the network connection to the remote end, if open.

        If no connection is open, this method does nothing.
        """
        if self.is_connected:
            self.client.close()
            if self.forward_agent and self._agent_handler is not None:
                self._agent_handler.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @opens
    def create_session(self):
        channel = self.transport.open_session()
        if self.forward_agent:
            self._agent_handler = AgentRequestHandler(channel)
        return channel

    @opens
    def run(self, command, **kwargs):
        """
        Execute a shell command on the remote end of this connection.

        This method wraps an SSH-capable implementation of
        `invoke.runners.Runner.run`; see its documentation for details.

        .. warning::
            There are a few spots where Fabric departs from Invoke's default
            settings/behaviors; they are documented under
            `.Config.global_defaults`.
        """
        return Remote(context=self).run(command, **kwargs)

    def sudo(self, command, **kwargs):
        """
        Execute a shell command, via ``sudo``, on the remote end.

        This method is identical to `invoke.context.Context.sudo` in every way,
        except in that -- like `run` -- it honors per-host/per-connection
        configuration overrides in addition to the generic/global ones. Thus,
        for example, per-host sudo passwords may be configured.
        """
        # TODO: if we never end up needing to modify this, is there a non shite
        # way to tell Sphinx to just use the docstring for the parent class'
        # sudo()? :inherited-members: adds waaaay too much garbage :(
        # NOTE: no need to open(), can rely on run()'s.
        return super(Connection, self).sudo(command, **kwargs)

    def local(self, *args, **kwargs):
        """
        Execute a shell command on the local system.

        This method is a straight wrapper of `invoke.run`; see its docs for
        details and call signature.
        """
        return super(Connection, self).run(*args, **kwargs)

    @opens
    def sftp(self):
        """
        Return a `~paramiko.sftp_client.SFTPClient` object.

        If called more than one time, memoizes the first result; thus, any
        given `.Connection` instance will only ever have a single SFTP client,
        and state (such as that managed by
        `~paramiko.sftp_client.SFTPClient.chdir`) will be preserved.
        """
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def get(self, *args, **kwargs):
        """
        Get a remote file to the local filesystem or file-like object.

        Simply a wrapper for `.Transfer.get`. Please see its documentation for
        all details.
        """
        return Transfer(self).get(*args, **kwargs)

    def put(self, *args, **kwargs):
        """
        Put a remote file (or file-like object) to the remote filesystem.

        Simply a wrapper for `.Transfer.put`. Please see its documentation for
        all details.
        """
        return Transfer(self).put(*args, **kwargs)

    # TODO: yield the socket for advanced users? Other advanced use cases
    # (perhaps factor out socket creation itself)?
    # TODO: probably push some of this down into Paramiko
    @contextmanager
    @opens
    def forward_local(
        self,
        local_port,
        remote_port=None,
        remote_host='localhost',
        local_host='localhost',
    ):
        """
        Open a tunnel connecting ``local_port`` to the server's environment.

        For example, say you want to connect to a remote PostgreSQL database
        which is locked down and only accessible via the system it's running
        on. You have SSH access to this server, so you can temporarily make
        port 5432 on your local system act like port 5432 on the server::

            import psycopg2
            from fabric import Connection

            with Connection('my-db-server').forward_local(5432):
                db = psycopg2.connect(
                    host='localhost', port=5432, database='mydb'
                )
                # Do things with 'db' here

        This method is analogous to using the ``-L`` option of OpenSSH's
        ``ssh`` program.

        :param int local_port: The local port number on which to listen.

        :param int remote_port:
            The remote port number. Defaults to the same value as
            ``local_port``.

        :param str local_host:
            The local hostname/interface on which to listen. Default:
            ``localhost``.

        :param str remote_host:
            The remote hostname serving the forwarded remote port. Default:
            ``localhost`` (i.e., the host this `.Connection` is connected to.)

        :returns:
            Nothing; this method is only useful as a context manager affecting
            local operating system state.
        """
        if not remote_port:
            remote_port = local_port

        # TunnelManager does all of the work, sitting in the background (so we
        # can yield) and spawning threads every time somebody connects to our
        # local port.
        finished = Event()
        manager = TunnelManager(
            local_port=local_port, local_host=local_host,
            remote_port=remote_port, remote_host=remote_host,
            # TODO: not a huge fan of handing in our transport, but...?
            transport=self.transport, finished=finished,
        )
        manager.start()

        # Return control to caller now that things ought to be operational
        try:
            yield
        # Teardown once user exits block
        finally:
            # Signal to manager that it should close all open tunnels
            finished.set()
            # Then wait for it to do so
            manager.join()
            # Raise threading errors from within the manager, which would be
            # one of:
            # - an inner ThreadException, which was created by the manager on
            # behalf of its Tunnels; this gets directly raised.
            # - some other exception, which would thus have occurred in the
            # manager itself; we wrap this in a new ThreadException.
            # NOTE: in these cases, some of the metadata tracking in
            # ExceptionHandlingThread/ExceptionWrapper/ThreadException (which
            # is useful when dealing with multiple nearly-identical sibling IO
            # threads) is superfluous, but it doesn't feel worth breaking
            # things up further; we just ignore it for now.
            wrapper = manager.exception()
            if wrapper is not None:
                if wrapper.type is ThreadException:
                    raise wrapper.value
                else:
                    raise ThreadException([wrapper])

            # TODO: cancel port forward on transport? Does that even make sense
            # here (where we used direct-tcpip) vs the opposite method (which
            # is what uses forward-tcpip)?

    # TODO: probably push some of this down into Paramiko
    @contextmanager
    @opens
    def forward_remote(
        self,
        remote_port,
        local_port=None,
        remote_host='127.0.0.1',
        local_host='localhost',
    ):
        """
        Open a tunnel connecting ``remote_port`` to the local environment.

        For example, say you're running a daemon in development mode on your
        workstation at port 8080, and want to funnel traffic to it from a
        production or staging environment.

        In most situations this isn't possible as your office/home network
        probably blocks inbound traffic. But you have SSH access to this
        server, so you can temporarily make port 8080 on that server act like
        port 8080 on your workstation::

            from fabric import Connection

            cxn = Connection('my-remote-server')
            with cxn.forward_remote(8080):
                cxn.run("remote-data-writer --port 8080")
                # Assuming remote-data-writer runs until interrupted, this will
                # stay open until you Ctrl-C...

        This method is analogous to using the ``-R`` option of OpenSSH's
        ``ssh`` program.

        :param int remote_port: The remote port number on which to listen.

        :param int local_port:
            The local port number. Defaults to the same value as
            ``remote_port``.

        :param str local_host:
            The local hostname/interface the forwarded connection talks to.
            Default: ``localhost``.

        :param str remote_host:
            The remote interface address to listen on when forwarding
            connections. Default: ``127.0.0.1`` (i.e. only listen on the remote
            localhost).

        :returns:
            Nothing; this method is only useful as a context manager affecting
            local operating system state.
        """
        if not local_port:
            local_port = remote_port
        # Callback executes on each connection to the remote port and is given
        # a Channel hooked up to said port. (We don't actually care about the
        # source/dest host/port pairs at all; only whether the channel has data
        # to read and suchlike.)
        # We then pair that channel with a new 'outbound' socket connection to
        # the local host/port being forwarded, in a new Tunnel.
        # That Tunnel is then added to a shared data structure so we can track
        # & close them during shutdown.
        #
        # TODO: this approach is less than ideal because we have to share state
        # between ourselves & the callback handed into the transport's own
        # thread handling (which is roughly analogous to our self-controlled
        # TunnelManager for local forwarding). See if we can use more of
        # Paramiko's API (or improve it and then do so) so that isn't
        # necessary.
        tunnels = []
        def callback(channel, src_addr_tup, dst_addr_tup):
            sock = socket.socket()
            # TODO: handle connection failure such that channel, etc get closed
            sock.connect((local_host, local_port))
            # TODO: we don't actually need to generate the Events at our level,
            # do we? Just let Tunnel.__init__ do it; all we do is "press its
            # button" on shutdown...
            tunnel = Tunnel(channel=channel, sock=sock, finished=Event())
            tunnel.start()
            # Communication between ourselves & the Paramiko handling subthread
            tunnels.append(tunnel)
        # Ask Paramiko (really, the remote sshd) to call our callback whenever
        # connections are established on the remote iface/port.
        # transport.request_port_forward(remote_host, remote_port, callback)
        try:
            self.transport.request_port_forward(
                address=remote_host,
                port=remote_port,
                handler=callback,
            )
            yield
        finally:
            # TODO: see above re: lack of a TunnelManager
            # TODO: and/or also refactor with TunnelManager re: shutdown logic.
            # E.g. maybe have a non-thread TunnelManager-alike with a method
            # that acts as the callback? At least then there's a tiny bit more
            # encapsulation...meh.
            for tunnel in tunnels:
                tunnel.finished.set()
                tunnel.join()
            self.transport.cancel_port_forward(
                address=remote_host,
                port=remote_port,
            )


class Group(list):
    """
    A collection of `.Connection` objects whose API operates on its contents.
    """
    def __init__(self, hosts=None):
        """
        Create a group of connections from an iterable of shorthand strings.

        See `.Connection` for details on the format of these strings - they
        will be used as the first positional argument of `.Connection`
        constructors.
        """
        # TODO: allow splat-args form in addition to iterable arg?
        # TODO: #563, #388 (could be here or higher up in Program area)
        if hosts:
            self.extend(map(Connection, hosts))

    @classmethod
    def from_connections(cls, connections):
        """
        Alternate constructor accepting `.Connection` objects.
        """
        group = cls()
        group.extend(connections)
        return group

    def run(self, *args, **kwargs):
        # TODO: how to change method of execution across contents? subclass,
        # kwargs, additional methods, inject an executor?
        # TODO: retval needs to be host objects or something non-string. See
        # how tutorial mentions 'ResultSet' - useful to construct or no?
        # TODO: also need way to deal with duplicate connections (see THOUGHTS)
        result = {}
        for cxn in self:
            result[cxn] = cxn.run(*args, **kwargs)
        return result

    # TODO: mirror Connection's close()?

    # TODO: execute() as mentioned in tutorial
