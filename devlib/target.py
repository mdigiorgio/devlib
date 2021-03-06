import os
import re
import time
import logging
import posixpath
import subprocess
import tarfile
import tempfile
import threading
from collections import namedtuple

from devlib.host import LocalConnection, PACKAGE_BIN_DIRECTORY
from devlib.module import get_module
from devlib.platform import Platform
from devlib.exception import TargetError, TargetNotRespondingError, TimeoutError
from devlib.utils.ssh import SshConnection
from devlib.utils.android import AdbConnection, AndroidProperties, LogcatMonitor, adb_command, adb_disconnect
from devlib.utils.misc import memoized, isiterable, convert_new_lines
from devlib.utils.misc import commonprefix, escape_double_quotes, merge_lists
from devlib.utils.misc import ABI_MAP, get_cpu_name, ranges_to_list
from devlib.utils.types import integer, boolean, bitmask, identifier, caseless_string


FSTAB_ENTRY_REGEX = re.compile(r'(\S+) on (.+) type (\S+) \((\S+)\)')
ANDROID_SCREEN_STATE_REGEX = re.compile('(?:mPowerState|mScreenOn|Display Power: state)=([0-9]+|true|false|ON|OFF)',
                                        re.IGNORECASE)
ANDROID_SCREEN_RESOLUTION_REGEX = re.compile(r'mUnrestrictedScreen=\(\d+,\d+\)'
                                             r'\s+(?P<width>\d+)x(?P<height>\d+)')
DEFAULT_SHELL_PROMPT = re.compile(r'^.*(shell|root)@.*:/\S* [#$] ',
                                  re.MULTILINE)
KVERSION_REGEX =re.compile(
    r'(?P<version>\d+)(\.(?P<major>\d+)(\.(?P<minor>\d+)(-rc(?P<rc>\d+))?)?)?(.*-g(?P<sha1>[0-9a-fA-F]{7,}))?'
)

GOOGLE_DNS_SERVER_ADDRESS = '8.8.8.8'


class Target(object):

    path = None
    os = None

    default_modules = [
        'hotplug',
        'cpufreq',
        'cpuidle',
        'cgroups',
        'hwmon',
    ]

    @property
    def core_names(self):
        return self.platform.core_names

    @property
    def core_clusters(self):
        return self.platform.core_clusters

    @property
    def big_core(self):
        return self.platform.big_core

    @property
    def little_core(self):
        return self.platform.little_core

    @property
    def is_connected(self):
        return self.conn is not None

    @property
    def connected_as_root(self):
        if self._connected_as_root is None:
            result = self.execute('id')
            self._connected_as_root = 'uid=0(' in result
        return self._connected_as_root

    @property
    @memoized
    def is_rooted(self):
        if self.connected_as_root:
            return True
        try:
            self.execute('ls /', timeout=2, as_root=True)
            return True
        except (TargetError, TimeoutError):
            return False

    @property
    @memoized
    def needs_su(self):
        return not self.connected_as_root and self.is_rooted

    @property
    @memoized
    def kernel_version(self):
        return KernelVersion(self.execute('{} uname -r -v'.format(self.busybox)).strip())

    @property
    def os_version(self):  # pylint: disable=no-self-use
        return {}

    @property
    def abi(self):  # pylint: disable=no-self-use
        return None

    @property
    def supported_abi(self):
        return [self.abi]

    @property
    @memoized
    def cpuinfo(self):
        return Cpuinfo(self.execute('cat /proc/cpuinfo'))

    @property
    @memoized
    def number_of_cpus(self):
        num_cpus = 0
        corere = re.compile(r'^\s*cpu\d+\s*$')
        output = self.execute('ls /sys/devices/system/cpu')
        for entry in output.split():
            if corere.match(entry):
                num_cpus += 1
        return num_cpus

    @property
    @memoized
    def config(self):
        try:
            return KernelConfig(self.execute('zcat /proc/config.gz'))
        except TargetError:
            for path in ['/boot/config', '/boot/config-$(uname -r)']:
                try:
                    return KernelConfig(self.execute('cat {}'.format(path)))
                except TargetError:
                    pass
        return KernelConfig('')

    @property
    @memoized
    def user(self):
        return self.getenv('USER')

    @property
    def conn(self):
        if self._connections:
            tid = id(threading.current_thread())
            if tid not in self._connections:
                self._connections[tid] = self.get_connection()
            return self._connections[tid]
        else:
            return None

    @property
    def shutils(self):
        if self._shutils is None:
            self._setup_shutils()
        return self._shutils

    def __init__(self,
                 connection_settings=None,
                 platform=None,
                 working_directory=None,
                 executables_directory=None,
                 connect=True,
                 modules=None,
                 load_default_modules=True,
                 shell_prompt=DEFAULT_SHELL_PROMPT,
                 conn_cls=None,
                 ):
        self._connected_as_root = None
        self.connection_settings = connection_settings or {}
        # Set self.platform: either it's given directly (by platform argument)
        # or it's given in the connection_settings argument
        # If neither, create default Platform()
        if platform is None:
            self.platform = self.connection_settings.get('platform', Platform())
        else:
            self.platform = platform
        # Check if the user hasn't given two different platforms
        if 'platform' in self.connection_settings:
            if connection_settings['platform'] is not platform:
                raise TargetError('Platform specified in connection_settings '
                                   '({}) differs from that directly passed '
                                   '({})!)'
                                   .format(connection_settings['platform'],
                                    self.platform))
        self.connection_settings['platform'] = self.platform
        self.working_directory = working_directory
        self.executables_directory = executables_directory
        self.modules = modules or []
        self.load_default_modules = load_default_modules
        self.shell_prompt = shell_prompt
        self.conn_cls = conn_cls
        self.logger = logging.getLogger(self.__class__.__name__)
        self._installed_binaries = {}
        self._installed_modules = {}
        self._cache = {}
        self._connections = {}
        self._shutils = None
        self.busybox = None

        if load_default_modules:
            module_lists = [self.default_modules]
        else:
            module_lists = []
        module_lists += [self.modules, self.platform.modules]
        self.modules = merge_lists(*module_lists, duplicates='first')
        self._update_modules('early')
        if connect:
            self.connect()

    # connection and initialization

    def connect(self, timeout=None):
        self.platform.init_target_connection(self)
        tid = id(threading.current_thread())
        self._connections[tid] = self.get_connection(timeout=timeout)
        self._resolve_paths()
        self.execute('mkdir -p {}'.format(self.working_directory))
        self.execute('mkdir -p {}'.format(self.executables_directory))
        self.busybox = self.install(os.path.join(PACKAGE_BIN_DIRECTORY, self.abi, 'busybox'))
        self.platform.update_from_target(self)
        self._update_modules('connected')
        if self.platform.big_core and self.load_default_modules:
            self._install_module(get_module('bl'))

    def disconnect(self):
        for conn in self._connections.itervalues():
            conn.close()
        self._connections = {}

    def get_connection(self, timeout=None):
        if self.conn_cls == None:
            raise ValueError('Connection class not specified on Target creation.')
        return self.conn_cls(timeout=timeout, **self.connection_settings)  # pylint: disable=not-callable

    def setup(self, executables=None):
        self._setup_shutils()

        for host_exe in (executables or []):  # pylint: disable=superfluous-parens
            self.install(host_exe)

        # Check for platform dependent setup procedures
        self.platform.setup(self)

        # Initialize modules which requires Buxybox (e.g. shutil dependent tasks)
        self._update_modules('setup')

    def reboot(self, hard=False, connect=True, timeout=180):
        if hard:
            if not self.has('hard_reset'):
                raise TargetError('Hard reset not supported for this target.')
            self.hard_reset()  # pylint: disable=no-member
        else:
            if not self.is_connected:
                message = 'Cannot reboot target becuase it is disconnected. ' +\
                          'Either connect() first, or specify hard=True ' +\
                          '(in which case, a hard_reset module must be installed)'
                raise TargetError(message)
            self.reset()
            # Wait a fixed delay before starting polling to give the target time to
            # shut down, otherwise, might create the connection while it's still shutting
            # down resulting in subsequenct connection failing.
            self.logger.debug('Waiting for target to power down...')
            reset_delay = 20
            time.sleep(reset_delay)
            timeout = max(timeout - reset_delay, 10)
        if self.has('boot'):
            self.boot()  # pylint: disable=no-member
        self._connected_as_root = None
        if connect:
            self.connect(timeout=timeout)

    # file transfer

    def push(self, source, dest, timeout=None):
        return self.conn.push(source, dest, timeout=timeout)

    def pull(self, source, dest, timeout=None):
        return self.conn.pull(source, dest, timeout=timeout)

    def get_directory(self, source_dir, dest):
        """ Pull a directory from the device, after compressing dir """
        # Create all file names
        tar_file_name = source_dir.lstrip(self.path.sep).replace(self.path.sep, '.')
        # Host location of dir
        outdir = os.path.join(dest, tar_file_name)
        # Host location of archive
        tar_file_name  = '{}.tar'.format(tar_file_name)
        tempfile = os.path.join(dest, tar_file_name)

        # Does the folder exist?
        self.execute('ls -la {}'.format(source_dir))
        # Try compressing the folder
        try:
            self.execute('{} tar -cvf {} {}'.format(self.busybox, tar_file_name,
                                                     source_dir))
        except TargetError:
            self.logger.debug('Failed to run tar command on target! ' \
                              'Not pulling directory {}'.format(source_dir))
        # Pull the file
        os.mkdir(outdir)
        self.pull(tar_file_name, tempfile )
        # Decompress
        f = tarfile.open(tempfile, 'r')
        f.extractall(outdir)
        os.remove(tempfile)

    # execution

    def execute(self, command, timeout=None, check_exit_code=True, as_root=False):
        return self.conn.execute(command, timeout, check_exit_code, as_root)

    def background(self, command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, as_root=False):
        return self.conn.background(command, stdout, stderr, as_root)

    def invoke(self, binary, args=None, in_directory=None, on_cpus=None,
               as_root=False, timeout=30):
        """
        Executes the specified binary under the specified conditions.

        :binary: binary to execute. Must be present and executable on the device.
        :args: arguments to be passed to the binary. The can be either a list or
               a string.
        :in_directory:  execute the binary in the  specified directory. This must
                        be an absolute path.
        :on_cpus:  taskset the binary to these CPUs. This may be a single ``int`` (in which
                   case, it will be interpreted as the mask), a list of ``ints``, in which
                   case this will be interpreted as the list of cpus, or string, which
                   will be interpreted as a comma-separated list of cpu ranges, e.g.
                   ``"0,4-7"``.
        :as_root: Specify whether the command should be run as root
        :timeout: If the invocation does not terminate within this number of seconds,
                  a ``TimeoutError`` exception will be raised. Set to ``None`` if the
                  invocation should not timeout.

        :returns: output of command.
        """
        command = binary
        if args:
            if isiterable(args):
                args = ' '.join(args)
            command = '{} {}'.format(command, args)
        if on_cpus:
            on_cpus = bitmask(on_cpus)
            command = '{} taskset 0x{:x} {}'.format(self.busybox, on_cpus, command)
        if in_directory:
            command = 'cd {} && {}'.format(in_directory, command)
        return self.execute(command, as_root=as_root, timeout=timeout)

    def background_invoke(self, binary, args=None, in_directory=None,
                          on_cpus=None, as_root=False):
        """
        Executes the specified binary as a background task under the
        specified conditions.

        :binary: binary to execute. Must be present and executable on the device.
        :args: arguments to be passed to the binary. The can be either a list or
               a string.
        :in_directory:  execute the binary in the  specified directory. This must
                        be an absolute path.
        :on_cpus:  taskset the binary to these CPUs. This may be a single ``int`` (in which
                   case, it will be interpreted as the mask), a list of ``ints``, in which
                   case this will be interpreted as the list of cpus, or string, which
                   will be interpreted as a comma-separated list of cpu ranges, e.g.
                   ``"0,4-7"``.
        :as_root: Specify whether the command should be run as root

        :returns: the subprocess instance handling that command
        """
        command = binary
        if args:
            if isiterable(args):
                args = ' '.join(args)
            command = '{} {}'.format(command, args)
        if on_cpus:
            on_cpus = bitmask(on_cpus)
            command = '{} taskset 0x{:x} {}'.format(self.busybox, on_cpus, command)
        if in_directory:
            command = 'cd {} && {}'.format(in_directory, command)
        return self.background(command, as_root=as_root)

    def kick_off(self, command, as_root=False):
        raise NotImplementedError()

    # sysfs interaction

    def read_value(self, path, kind=None):
        output = self.execute('cat \'{}\''.format(path), as_root=self.needs_su).strip()  # pylint: disable=E1103
        if kind:
            return kind(output)
        else:
            return output

    def read_int(self, path):
        return self.read_value(path, kind=integer)

    def read_bool(self, path):
        return self.read_value(path, kind=boolean)

    def write_value(self, path, value, verify=True):
        value = str(value)
        self.execute('echo {} > \'{}\''.format(value, path), check_exit_code=False, as_root=True)
        if verify:
            output = self.read_value(path)
            if not output == value:
                message = 'Could not set the value of {} to "{}" (read "{}")'.format(path, value, output)
                raise TargetError(message)

    def reset(self):
        try:
            self.execute('reboot', as_root=self.needs_su, timeout=2)
        except (TargetError, TimeoutError, subprocess.CalledProcessError):
            # on some targets "reboot" doesn't return gracefully
            pass
        self._connected_as_root = None

    def check_responsive(self):
        try:
            self.conn.execute('ls /', timeout=5)
        except (TimeoutError, subprocess.CalledProcessError):
            raise TargetNotRespondingError(self.conn.name)

    # process management

    def kill(self, pid, signal=None, as_root=False):
        signal_string = '-s {}'.format(signal) if signal else ''
        self.execute('kill {} {}'.format(signal_string, pid), as_root=as_root)

    def killall(self, process_name, signal=None, as_root=False):
        for pid in self.get_pids_of(process_name):
            try:
                self.kill(pid, signal=signal, as_root=as_root)
            except TargetError:
                pass

    def get_pids_of(self, process_name):
        raise NotImplementedError()

    def ps(self, **kwargs):
        raise NotImplementedError()

    # files

    def file_exists(self, filepath):
        command = 'if [ -e \'{}\' ]; then echo 1; else echo 0; fi'
        output = self.execute(command.format(filepath), as_root=self.is_rooted)
        return boolean(output.strip())

    def directory_exists(self, filepath):
        output = self.execute('if [ -d \'{}\' ]; then echo 1; else echo 0; fi'.format(filepath))
        # output from ssh my contain part of the expression in the buffer,
        # split out everything except the last word.
        return boolean(output.split()[-1])  # pylint: disable=maybe-no-member

    def list_file_systems(self):
        output = self.execute('mount')
        fstab = []
        for line in output.split('\n'):
            line = line.strip()
            if not line:
                continue
            match = FSTAB_ENTRY_REGEX.search(line)
            if match:
                fstab.append(FstabEntry(match.group(1), match.group(2),
                                        match.group(3), match.group(4),
                                        None, None))
            else:  # assume pre-M Android
                fstab.append(FstabEntry(*line.split()))
        return fstab

    def list_directory(self, path, as_root=False):
        raise NotImplementedError()

    def get_workpath(self, name):
        return self.path.join(self.working_directory, name)

    def tempfile(self, prefix='', suffix=''):
        names = tempfile._get_candidate_names()  # pylint: disable=W0212
        for _ in xrange(tempfile.TMP_MAX):
            name = names.next()
            path = self.get_workpath(prefix + name + suffix)
            if not self.file_exists(path):
                return path
        raise IOError('No usable temporary filename found')

    def remove(self, path, as_root=False):
        self.execute('rm -rf "{}"'.format(escape_double_quotes(path)), as_root=as_root)

    # misc
    def core_cpus(self, core):
        return [i for i, c in enumerate(self.core_names) if c == core]

    def list_online_cpus(self, core=None):
        path = self.path.join('/sys/devices/system/cpu/online')
        output = self.read_value(path)
        all_online = ranges_to_list(output)
        if core:
            cpus = self.core_cpus(core)
            if not cpus:
                raise ValueError(core)
            return [o for o in all_online if o in cpus]
        else:
            return all_online

    def list_offline_cpus(self):
        online = self.list_online_cpus()
        return [c for c in xrange(self.number_of_cpus)
                if c not in online]

    def getenv(self, variable):
        return self.execute('echo ${}'.format(variable)).rstrip('\r\n')

    def capture_screen(self, filepath):
        raise NotImplementedError()

    def install(self, filepath, timeout=None, with_name=None):
        raise NotImplementedError()

    def uninstall(self, name):
        raise NotImplementedError()

    def get_installed(self, name, search_system_binaries=True):
        # Check user installed binaries first
        if self.file_exists(self.executables_directory):
            if name in self.list_directory(self.executables_directory):
                return self.path.join(self.executables_directory, name)
        # Fall back to binaries in PATH
        if search_system_binaries:
            for path in self.getenv('PATH').split(self.path.pathsep):
                try:
                    if name in self.list_directory(path):
                        return self.path.join(path, name)
                except TargetError:
                    pass  # directory does not exist or no executable premssions

    which = get_installed

    def install_if_needed(self, host_path, search_system_binaries=True):

        binary_path = self.get_installed(os.path.split(host_path)[1],
                                         search_system_binaries=search_system_binaries)
        if not binary_path:
            binary_path = self.install(host_path)
        return binary_path

    def is_installed(self, name):
        return bool(self.get_installed(name))

    def bin(self, name):
        return self._installed_binaries.get(name, name)

    def has(self, modname):
        return hasattr(self, identifier(modname))

    def lsmod(self):
        lines = self.execute('lsmod').splitlines()
        entries = []
        for line in lines[1:]:  # first line is the header
            if not line.strip():
                continue
            parts = line.split()
            name = parts[0]
            size = int(parts[1])
            use_count = int(parts[2])
            if len(parts) > 3:
                used_by = ''.join(parts[3:]).split(',')
            else:
                used_by = []
            entries.append(LsmodEntry(name, size, use_count, used_by))
        return entries

    def insmod(self, path):
        target_path = self.get_workpath(os.path.basename(path))
        self.push(path, target_path)
        self.execute('insmod {}'.format(target_path), as_root=True)


    def extract(self, path, dest=None):
        """
        Extact the specified on-target file. The extraction method to be used
        (unzip, gunzip, bunzip2, or tar) will be based on the file's extension.
        If ``dest`` is specified, it must be an existing directory on target;
        the extracted contents will be placed there.

        Note that, depending on the archive file format (and therfore the
        extraction method used), the original archive file may or may not exist
        after the extraction.

        The return value is the path to the extracted contents.  In case of
        gunzip and bunzip2, this will be path to the extracted file; for tar
        and uzip, this will be the directory with the extracted file(s)
        (``dest`` if it was specified otherwise, the directory that cotained
        the archive).

        """
        for ending in ['.tar.gz', '.tar.bz', '.tar.bz2',
                       '.tgz', '.tbz', '.tbz2']:
            if path.endswith(ending):
                return self._extract_archive(path, 'tar xf {} -C {}', dest)

        ext = self.path.splitext(path)[1]
        if ext in ['.bz', '.bz2']:
            return self._extract_file(path, 'bunzip2 -f {}', dest)
        elif ext == '.gz':
            return self._extract_file(path, 'gunzip -f {}', dest)
        elif ext == '.zip':
            return self._extract_archive(path, 'unzip {} -d {}', dest)
        else:
            raise ValueError('Unknown compression format: {}'.format(ext))

    def sleep(self, duration):
        timeout = duration + 10
        self.execute('sleep {}'.format(duration), timeout=timeout)

    def read_tree_values_flat(self, path, depth=1, check_exit_code=True):
        command = 'read_tree_values {} {}'.format(path, depth)
        output = self._execute_util(command, as_root=self.is_rooted,
                                    check_exit_code=check_exit_code)
        result = {}
        for entry in output.strip().split('\n'):
            if ':' not in entry:
                continue
            path, value = entry.strip().split(':', 1)
            result[path] = value
        return result

    def read_tree_values(self, path, depth=1, dictcls=dict, check_exit_code=True):
	value_map = self.read_tree_values_flat(path, depth, check_exit_code)
	return _build_path_tree(value_map, path, self.path.sep, dictcls)

    # internal methods

    def _setup_shutils(self):
        shutils_ifile = os.path.join(PACKAGE_BIN_DIRECTORY, 'scripts', 'shutils.in')
        shutils_ofile = os.path.join(PACKAGE_BIN_DIRECTORY, 'scripts', 'shutils')
        shell_path = '/bin/sh'
        if self.os == 'android':
            shell_path = '/system/bin/sh'
        with open(shutils_ifile) as fh:
            lines = fh.readlines()
        with open(shutils_ofile, 'w') as ofile:
            for line in lines:
                line = line.replace("__DEVLIB_SHELL__", shell_path)
                line = line.replace("__DEVLIB_BUSYBOX__", self.busybox)
                ofile.write(line)
        self._shutils = self.install(os.path.join(PACKAGE_BIN_DIRECTORY, 'scripts', 'shutils'))

    def _execute_util(self, command, timeout=None, check_exit_code=True, as_root=False):
        command = '{} {}'.format(self.shutils, command)
        return self.conn.execute(command, timeout, check_exit_code, as_root)

    def _extract_archive(self, path, cmd, dest=None):
        cmd = '{} ' + cmd  # busybox
        if dest:
            extracted = dest
        else:
            extracted = self.path.dirname(path)
        cmdtext = cmd.format(self.busybox, path, extracted)
        self.execute(cmdtext)
        return extracted

    def _extract_file(self, path, cmd, dest=None):
        cmd = '{} ' + cmd  # busybox
        cmdtext = cmd.format(self.busybox, path)
        self.execute(cmdtext)
        extracted = self.path.splitext(path)[0]
        if dest:
            self.execute('mv -f {} {}'.format(extracted, dest))
            if dest.endswith('/'):
                extracted = self.path.join(dest, self.path.basename(extracted))
            else:
                extracted = dest
        return extracted

    def _update_modules(self, stage):
        for mod in self.modules:
            if isinstance(mod, dict):
                mod, params = mod.items()[0]
            else:
                params = {}
            mod = get_module(mod)
            if not mod.stage == stage:
                continue
            if mod.probe(self):
                self._install_module(mod, **params)
            else:
                msg = 'Module {} is not supported by the target'.format(mod.name)
                if self.load_default_modules:
                    self.logger.debug(msg)
                else:
                    self.logger.warning(msg)

    def _install_module(self, mod, **params):
        if mod.name not in self._installed_modules:
            self.logger.debug('Installing module {}'.format(mod.name))
            mod.install(self, **params)
            self._installed_modules[mod.name] = mod
        else:
            self.logger.debug('Module {} is already installed.'.format(mod.name))

    def _resolve_paths(self):
        raise NotImplementedError()

    def is_network_connected(self):
        self.logger.debug('Checking for internet connectivity...')

        timeout_s = 5
        # It would be nice to use busybox for this, but that means we'd need
        # root (ping is usually setuid so it can open raw sockets to send ICMP)
        command = 'ping -q -c 1 -w {} {} 2>&1'.format(timeout_s,
                                                      GOOGLE_DNS_SERVER_ADDRESS)

        # We'll use our own retrying mechanism (rather than just using ping's -c
        # to send multiple packets) so that we don't slow things down in the
        # 'good' case where the first packet gets echoed really quickly.
        attempts = 5
        for _ in range(attempts):
            try:
                self.execute(command)
                return True
            except TargetError as e:
                err = str(e).lower()
                if '100% packet loss' in err:
                    # We sent a packet but got no response.
                    # Try again - we don't want this to fail just because of a
                    # transient drop in connection quality.
                    self.logger.debug('No ping response from {} after {}s'
                                      .format(GOOGLE_DNS_SERVER_ADDRESS, timeout_s))
                    continue
                elif 'network is unreachable' in err:
                    # No internet connection at all, we can fail straight away
                    self.logger.debug('Network unreachable')
                    return False
                else:
                    # Something else went wrong, we don't know what, raise an
                    # error.
                    raise

        self.logger.debug('Failed to ping {} after {} attempts'.format(
            GOOGLE_DNS_SERVER_ADDRESS, attempts))
        return False

class LinuxTarget(Target):

    path = posixpath
    os = 'linux'

    @property
    @memoized
    def abi(self):
        value = self.execute('uname -m').strip()
        for abi, architectures in ABI_MAP.iteritems():
            if value in architectures:
                result = abi
                break
        else:
            result = value
        return result

    @property
    @memoized
    def os_version(self):
        os_version = {}
        try:
            command = 'ls /etc/*-release /etc*-version /etc/*_release /etc/*_version 2>/dev/null'
            version_files = self.execute(command, check_exit_code=False).strip().split()
            for vf in version_files:
                name = self.path.basename(vf)
                output = self.read_value(vf)
                os_version[name] = output.strip().replace('\n', ' ')
        except TargetError:
            raise
        return os_version

    @property
    @memoized
    # There is currently no better way to do this cross platform.
    # ARM does not have dmidecode
    def model(self):
        if self.file_exists("/proc/device-tree/model"):
            raw_model = self.execute("cat /proc/device-tree/model")
            return '_'.join(raw_model.split()[:2])
        return None

    def __init__(self,
                 connection_settings=None,
                 platform=None,
                 working_directory=None,
                 executables_directory=None,
                 connect=True,
                 modules=None,
                 load_default_modules=True,
                 shell_prompt=DEFAULT_SHELL_PROMPT,
                 conn_cls=SshConnection,
                 ):
        super(LinuxTarget, self).__init__(connection_settings=connection_settings,
                                          platform=platform,
                                          working_directory=working_directory,
                                          executables_directory=executables_directory,
                                          connect=connect,
                                          modules=modules,
                                          load_default_modules=load_default_modules,
                                          shell_prompt=shell_prompt,
                                          conn_cls=conn_cls)

    def connect(self, timeout=None):
        super(LinuxTarget, self).connect(timeout=timeout)

    def kick_off(self, command, as_root=False):
        command = 'sh -c "{}" 1>/dev/null 2>/dev/null &'.format(escape_double_quotes(command))
        return self.conn.execute(command, as_root=as_root)

    def get_pids_of(self, process_name):
        """Returns a list of PIDs of all processes with the specified name."""
        # result should be a column of PIDs with the first row as "PID" header
        result = self.execute('ps -C {} -o pid'.format(process_name),  # NOQA
                              check_exit_code=False).strip().split()
        if len(result) >= 2:  # at least one row besides the header
            return map(int, result[1:])
        else:
            return []

    def ps(self, **kwargs):
        command = 'ps -eo user,pid,ppid,vsize,rss,wchan,pcpu,state,fname'
        lines = iter(convert_new_lines(self.execute(command)).split('\n'))
        lines.next()  # header

        result = []
        for line in lines:
            parts = re.split(r'\s+', line, maxsplit=8)
            if parts and parts != ['']:
                result.append(PsEntry(*(parts[0:1] + map(int, parts[1:5]) + parts[5:])))

        if not kwargs:
            return result
        else:
            filtered_result = []
            for entry in result:
                if all(getattr(entry, k) == v for k, v in kwargs.iteritems()):
                    filtered_result.append(entry)
            return filtered_result

    def list_directory(self, path, as_root=False):
        contents = self.execute('ls -1 {}'.format(path), as_root=as_root)
        return [x.strip() for x in contents.split('\n') if x.strip()]

    def install(self, filepath, timeout=None, with_name=None):  # pylint: disable=W0221
        destpath = self.path.join(self.executables_directory,
                                  with_name and with_name or self.path.basename(filepath))
        self.push(filepath, destpath)
        self.execute('chmod a+x {}'.format(destpath), timeout=timeout)
        self._installed_binaries[self.path.basename(destpath)] = destpath
        return destpath

    def uninstall(self, name):
        path = self.path.join(self.executables_directory, name)
        self.remove(path)

    def capture_screen(self, filepath):
        if not self.is_installed('scrot'):
            self.logger.debug('Could not take screenshot as scrot is not installed.')
            return
        try:

            tmpfile = self.tempfile()
            self.execute('DISPLAY=:0.0 scrot {}'.format(tmpfile))
            self.pull(tmpfile, filepath)
            self.remove(tmpfile)
        except TargetError as e:
            if "Can't open X dispay." not in e.message:
                raise e
            message = e.message.split('OUTPUT:', 1)[1].strip()  # pylint: disable=no-member
            self.logger.debug('Could not take screenshot: {}'.format(message))

    def _resolve_paths(self):
        if self.working_directory is None:
            if self.connected_as_root:
                self.working_directory = '/root/devlib-target'
            else:
                self.working_directory = '/home/{}/devlib-target'.format(self.user)
        if self.executables_directory is None:
            self.executables_directory = self.path.join(self.working_directory, 'bin')


class AndroidTarget(Target):

    path = posixpath
    os = 'android'
    ls_command = ''

    @property
    @memoized
    def abi(self):
        return self.getprop()['ro.product.cpu.abi'].split('-')[0]

    @property
    @memoized
    def supported_abi(self):
        props = self.getprop()
        result = [props['ro.product.cpu.abi']]
        if 'ro.product.cpu.abi2' in props:
            result.append(props['ro.product.cpu.abi2'])
        if 'ro.product.cpu.abilist' in props:
            for abi in props['ro.product.cpu.abilist'].split(','):
                if abi not in result:
                    result.append(abi)

        mapped_result = []
        for supported_abi in result:
            for abi, architectures in ABI_MAP.iteritems():
                found = False
                if supported_abi in architectures and abi not in mapped_result:
                    mapped_result.append(abi)
                    found = True
                    break
            if not found and supported_abi not in mapped_result:
                mapped_result.append(supported_abi)
        return mapped_result

    @property
    @memoized
    def os_version(self):
        os_version = {}
        for k, v in self.getprop().iteritems():
            if k.startswith('ro.build.version'):
                part = k.split('.')[-1]
                os_version[part] = v
        return os_version

    @property
    def adb_name(self):
        return self.conn.device

    @property
    @memoized
    def android_id(self):
        """
        Get the device's ANDROID_ID. Which is

            "A 64-bit number (as a hex string) that is randomly generated when the user
            first sets up the device and should remain constant for the lifetime of the
            user's device."

        .. note:: This will get reset on userdata erasure.

        """
        output = self.execute('content query --uri content://settings/secure --projection value --where "name=\'android_id\'"').strip()
        return output.split('value=')[-1]

    @property
    @memoized
    def model(self):
        try:
            return self.getprop(prop='ro.product.device')
        except KeyError:
            return None

    @property
    @memoized
    def external_storage(self):
        return self.execute('echo $EXTERNAL_STORAGE').strip()

    @property
    @memoized
    def screen_resolution(self):
        output = self.execute('dumpsys window')
        match = ANDROID_SCREEN_RESOLUTION_REGEX.search(output)
        if match:
            return (int(match.group('width')),
                    int(match.group('height')))
        else:
            return (0, 0)

    def __init__(self,
                 connection_settings=None,
                 platform=None,
                 working_directory=None,
                 executables_directory=None,
                 connect=True,
                 modules=None,
                 load_default_modules=True,
                 shell_prompt=DEFAULT_SHELL_PROMPT,
                 conn_cls=AdbConnection,
                 package_data_directory="/data/data",
                 ):
        super(AndroidTarget, self).__init__(connection_settings=connection_settings,
                                            platform=platform,
                                            working_directory=working_directory,
                                            executables_directory=executables_directory,
                                            connect=connect,
                                            modules=modules,
                                            load_default_modules=load_default_modules,
                                            shell_prompt=shell_prompt,
                                            conn_cls=conn_cls)
        self.package_data_directory = package_data_directory
        self.clear_logcat_lock = threading.Lock()

    def reset(self, fastboot=False):  # pylint: disable=arguments-differ
        try:
            self.execute('reboot {}'.format(fastboot and 'fastboot' or ''),
                         as_root=self.needs_su, timeout=2)
        except (TargetError, TimeoutError, subprocess.CalledProcessError):
            # on some targets "reboot" doesn't return gracefully
            pass
        self._connected_as_root = None

    def wait_boot_complete(self, timeout=10):
        start = time.time()
        boot_completed = boolean(self.getprop('sys.boot_completed'))
        while not boot_completed and timeout >= time.time() - start:
            time.sleep(5)
            boot_completed = boolean(self.getprop('sys.boot_completed'))
        if not boot_completed:
            raise TargetError('Connected but Android did not fully boot.')

    def connect(self, timeout=10, check_boot_completed=True):  # pylint: disable=arguments-differ
        device = self.connection_settings.get('device')
        if device and ':' in device:
            # ADB does not automatically remove a network device from it's
            # devices list when the connection is broken by the remote, so the
            # adb connection may have gone "stale", resulting in adb blocking
            # indefinitely when making calls to the device. To avoid this,
            # always disconnect first.
            adb_disconnect(device)
        super(AndroidTarget, self).connect(timeout=timeout)

        if check_boot_completed:
            self.wait_boot_complete(timeout)

    def setup(self, executables=None):
        super(AndroidTarget, self).setup(executables)
        self.execute('mkdir -p {}'.format(self._file_transfer_cache))

    def kick_off(self, command, as_root=None):
        """
        Like execute but closes adb session and returns immediately, leaving the command running on the
        device (this is different from execute(background=True) which keeps adb connection open and returns
        a subprocess object).
        """
        if as_root is None:
            as_root = self.needs_su
        try:
            command = 'cd {} && {} nohup {} &'.format(self.working_directory, self.busybox, command)
            output = self.execute(command, timeout=1, as_root=as_root)
        except TimeoutError:
            pass

    def __setup_list_directory(self):
        # In at least Linaro Android 16.09 (which was their first Android 7 release) and maybe
        # AOSP 7.0 as well, the ls command was changed.
        # Previous versions default to a single column listing, which is nice and easy to parse.
        # Newer versions default to a multi-column listing, which is not, but it does support
        # a '-1' option to get into single column mode. Older versions do not support this option
        # so we try the new version, and if it fails we use the old version.
        self.ls_command = 'ls -1'
        try:
            self.execute('ls -1 {}'.format(self.working_directory), as_root=False)
        except TargetError:
            self.ls_command = 'ls'

    def list_directory(self, path, as_root=False):
        if self.ls_command == '':
            self.__setup_list_directory()
        contents = self.execute('{} {}'.format(self.ls_command, path), as_root=as_root)
        return [x.strip() for x in contents.split('\n') if x.strip()]

    def install(self, filepath, timeout=None, with_name=None):  # pylint: disable=W0221
        ext = os.path.splitext(filepath)[1].lower()
        if ext == '.apk':
            return self.install_apk(filepath, timeout)
        else:
            return self.install_executable(filepath, with_name)

    def uninstall(self, name):
        if self.package_is_installed(name):
            self.uninstall_package(name)
        else:
            self.uninstall_executable(name)

    def get_pids_of(self, process_name):
        result = []
        search_term = process_name[-15:]
        for entry in self.ps():
            if search_term in entry.name:
                result.append(entry.pid)
        return result

    def ps(self, **kwargs):
        lines = iter(convert_new_lines(self.execute('ps')).split('\n'))
        lines.next()  # header
        result = []
        for line in lines:
            parts = line.split(None, 8)
            if not parts:
                continue
            if len(parts) == 8:
                # wchan was blank; insert an empty field where it should be.
                parts.insert(5, '')
            result.append(PsEntry(*(parts[0:1] + map(int, parts[1:5]) + parts[5:])))
        if not kwargs:
            return result
        else:
            filtered_result = []
            for entry in result:
                if all(getattr(entry, k) == v for k, v in kwargs.iteritems()):
                    filtered_result.append(entry)
            return filtered_result

    def capture_screen(self, filepath):
        on_device_file = self.path.join(self.working_directory, 'screen_capture.png')
        self.execute('screencap -p  {}'.format(on_device_file))
        self.pull(on_device_file, filepath)
        self.remove(on_device_file)

    def push(self, source, dest, as_root=False, timeout=None):  # pylint: disable=arguments-differ
        if not as_root:
            self.conn.push(source, dest, timeout=timeout)
        else:
            device_tempfile = self.path.join(self._file_transfer_cache, source.lstrip(self.path.sep))
            self.execute("mkdir -p '{}'".format(self.path.dirname(device_tempfile)))
            self.conn.push(source, device_tempfile, timeout=timeout)
            self.execute("cp '{}' '{}'".format(device_tempfile, dest), as_root=True)

    def pull(self, source, dest, as_root=False, timeout=None):  # pylint: disable=arguments-differ
        if not as_root:
            self.conn.pull(source, dest, timeout=timeout)
        else:
            device_tempfile = self.path.join(self._file_transfer_cache, source.lstrip(self.path.sep))
            self.execute("mkdir -p '{}'".format(self.path.dirname(device_tempfile)))
            self.execute("cp '{}' '{}'".format(source, device_tempfile), as_root=True)
            self.execute("chmod 0644 '{}'".format(device_tempfile), as_root=True)
            self.conn.pull(device_tempfile, dest, timeout=timeout)

    # Android-specific

    def swipe_to_unlock(self, direction="diagonal"):
        width, height = self.screen_resolution
        command = 'input swipe {} {} {} {}'
        if direction == "diagonal":
            start = 100
            stop = width - start
            swipe_height = height * 2 // 3
            self.execute(command.format(start, swipe_height, stop, 0))
        elif direction == "horizontal":
            swipe_height = height * 2 // 3
            start = 100
            stop = width - start
            self.execute(command.format(start, swipe_height, stop, swipe_height))
        elif direction == "vertical":
            swipe_middle = width / 2
            swipe_height = height * 2 // 3
            self.execute(command.format(swipe_middle, swipe_height, swipe_middle, 0))
        else:
            raise TargetError("Invalid swipe direction: {}".format(direction))

    def getprop(self, prop=None):
        props = AndroidProperties(self.execute('getprop'))
        if prop:
            return props[prop]
        return props

    def is_installed(self, name):
        return super(AndroidTarget, self).is_installed(name) or self.package_is_installed(name)

    def package_is_installed(self, package_name):
        return package_name in self.list_packages()

    def list_packages(self):
        output = self.execute('pm list packages')
        output = output.replace('package:', '')
        return output.split()

    def get_package_version(self, package):
        output = self.execute('dumpsys package {}'.format(package))
        for line in convert_new_lines(output).split('\n'):
            if 'versionName' in line:
                return line.split('=', 1)[1]
        return None

    def get_sdk_version(self):
        try:
            return int(self.getprop('ro.build.version.sdk'))
        except (ValueError, TypeError):
            return None

    def install_apk(self, filepath, timeout=None, replace=False, allow_downgrade=False):  # pylint: disable=W0221
        ext = os.path.splitext(filepath)[1].lower()
        if ext == '.apk':
            flags = []
            if replace:
                flags.append('-r')  # Replace existing APK
            if allow_downgrade:
                flags.append('-d')  # Install the APK even if a newer version is already installed
            if self.get_sdk_version() >= 23:
                flags.append('-g')  # Grant all runtime permissions
            self.logger.debug("Replace APK = {}, ADB flags = '{}'".format(replace, ' '.join(flags)))
            return adb_command(self.adb_name, "install {} '{}'".format(' '.join(flags), filepath), timeout=timeout)
        else:
            raise TargetError('Can\'t install {}: unsupported format.'.format(filepath))

    def grant_package_permission(self, package, permission):
        try:
            return self.execute('pm grant {} {}'.format(package, permission))
        except TargetError as e:
            if 'is not a changeable permission type' in e.message:
                pass # Ignore if unchangeable
            elif 'Unknown permission' in e.message:
                pass # Ignore if unknown
            elif 'has not requested permission' in e.message:
                pass # Ignore if not requested
            else:
                raise

    def refresh_files(self, file_list):
        """
        Depending on the android version and root status, determine the
        appropriate method of forcing a re-index of the mediaserver cache for a given
        list of files.
        """
        if self.is_rooted or self.get_sdk_version() < 24:  # MM and below
            common_path = commonprefix(file_list, sep=self.path.sep)
            self.broadcast_media_mounted(common_path, self.is_rooted)
        else:
            for f in file_list:
                self.broadcast_media_scan_file(f)

    def broadcast_media_scan_file(self, filepath):
        """
        Force a re-index of the mediaserver cache for the specified file.
        """
        command = 'am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://'
        self.execute(command + filepath)

    def broadcast_media_mounted(self, dirpath, as_root=False):
        """
        Force a re-index of the mediaserver cache for the specified directory.
        """
        command = 'am broadcast -a  android.intent.action.MEDIA_MOUNTED -d file://'
        self.execute(command + dirpath, as_root=as_root)

    def install_executable(self, filepath, with_name=None):
        self._ensure_executables_directory_is_writable()
        executable_name = with_name or os.path.basename(filepath)
        on_device_file = self.path.join(self.working_directory, executable_name)
        on_device_executable = self.path.join(self.executables_directory, executable_name)
        self.push(filepath, on_device_file)
        if on_device_file != on_device_executable:
            self.execute('cp {} {}'.format(on_device_file, on_device_executable), as_root=self.needs_su)
            self.remove(on_device_file, as_root=self.needs_su)
        self.execute("chmod 0777 '{}'".format(on_device_executable), as_root=self.needs_su)
        self._installed_binaries[executable_name] = on_device_executable
        return on_device_executable

    def uninstall_package(self, package):
        adb_command(self.adb_name, "uninstall {}".format(package), timeout=30)

    def uninstall_executable(self, executable_name):
        on_device_executable = self.path.join(self.executables_directory, executable_name)
        self._ensure_executables_directory_is_writable()
        self.remove(on_device_executable, as_root=self.needs_su)

    def dump_logcat(self, filepath, filter=None, append=False, timeout=30):  # pylint: disable=redefined-builtin
        op = '>>' if append else '>'
        filtstr = ' -s {}'.format(filter) if filter else ''
        command = 'logcat -d{} {} {}'.format(filtstr, op, filepath)
        adb_command(self.adb_name, command, timeout=timeout)

    def clear_logcat(self):
        with self.clear_logcat_lock:
            adb_command(self.adb_name, 'logcat -c', timeout=30)

    def get_logcat_monitor(self, regexps=None):
        return LogcatMonitor(self, regexps)

    def adb_kill_server(self, timeout=30):
        adb_command(self.adb_name, 'kill-server', timeout)

    def adb_wait_for_device(self, timeout=30):
        adb_command(self.adb_name, 'wait-for-device', timeout)

    def adb_reboot_bootloader(self, timeout=30):
        adb_command(self.adb_name, 'reboot-bootloader', timeout)

    def adb_root(self, enable=True, force=False):
        if enable:
            if self._connected_as_root and not force:
                return
            adb_command(self.adb_name, 'root', timeout=30)
            self._connected_as_root = True
            return
        adb_command(self.adb_name, 'unroot', timeout=30)
        self._connected_as_root = False

    def is_screen_on(self):
        output = self.execute('dumpsys power')
        match = ANDROID_SCREEN_STATE_REGEX.search(output)
        if match:
            return boolean(match.group(1))
        else:
            raise TargetError('Could not establish screen state.')

    def ensure_screen_is_on(self):
        if not self.is_screen_on():
            self.execute('input keyevent 26')

    def ensure_screen_is_off(self):
        if self.is_screen_on():
            self.execute('input keyevent 26')

    def set_auto_brightness(self, auto_brightness):
        cmd = 'settings put system screen_brightness_mode {}'
        self.execute(cmd.format(int(boolean(auto_brightness))))

    def get_auto_brightness(self):
        cmd = 'settings get system screen_brightness_mode'
        return boolean(self.execute(cmd).strip())

    def set_brightness(self, value):
        if not 0 <= value <= 255:
            msg = 'Invalid brightness "{}"; Must be between 0 and 255'
            raise ValueError(msg.format(value))
        self.set_auto_brightness(False)
        cmd = 'settings put system screen_brightness {}'
        self.execute(cmd.format(int(value)))

    def get_brightness(self):
        cmd = 'settings get system screen_brightness'
        return integer(self.execute(cmd).strip())

    def get_airplane_mode(self):
        cmd = 'settings get global airplane_mode_on'
        return boolean(self.execute(cmd).strip())

    def set_airplane_mode(self, mode):
        root_required = self.get_sdk_version() > 23
        if root_required and not self.is_rooted:
            raise TargetError('Root is required to toggle airplane mode on Android 7+')
        mode = int(boolean(mode))
        cmd = 'settings put global airplane_mode_on {}'
        self.execute(cmd.format(mode))
        self.execute('am broadcast -a android.intent.action.AIRPLANE_MODE '
                     '--ez state {}'.format(mode), as_root=root_required)

    def get_auto_rotation(self):
        cmd = 'settings get system accelerometer_rotation'
        return boolean(self.execute(cmd).strip())

    def set_auto_rotation(self, autorotate):
        cmd = 'settings put system accelerometer_rotation {}'
        self.execute(cmd.format(int(boolean(autorotate))))

    def set_natural_rotation(self):
        self.set_rotation(0)

    def set_left_rotation(self):
        self.set_rotation(1)

    def set_inverted_rotation(self):
        self.set_rotation(2)

    def set_right_rotation(self):
        self.set_rotation(3)

    def get_rotation(self):
        cmd = 'settings get system user_rotation'
        return int(self.execute(cmd).strip())

    def set_rotation(self, rotation):
        if not 0 <= rotation <= 3:
            raise ValueError('Rotation value must be between 0 and 3')
        self.set_auto_rotation(False)
        cmd = 'settings put system user_rotation {}'
        self.execute(cmd.format(rotation))

    def homescreen(self):
        self.execute('am start -a android.intent.action.MAIN -c android.intent.category.HOME')

    def _resolve_paths(self):
        if self.working_directory is None:
            self.working_directory = '/data/local/tmp/devlib-target'
        self._file_transfer_cache = self.path.join(self.working_directory, '.file-cache')
        if self.executables_directory is None:
            self.executables_directory = '/data/local/tmp/bin'

    def _ensure_executables_directory_is_writable(self):
        matched = []
        for entry in self.list_file_systems():
            if self.executables_directory.rstrip('/').startswith(entry.mount_point):
                matched.append(entry)
        if matched:
            entry = sorted(matched, key=lambda x: len(x.mount_point))[-1]
            if 'rw' not in entry.options:
                self.execute('mount -o rw,remount {} {}'.format(entry.device,
                                                                entry.mount_point),
                             as_root=True)
        else:
            message = 'Could not find mount point for executables directory {}'
            raise TargetError(message.format(self.executables_directory))

    _charging_enabled_path = '/sys/class/power_supply/battery/charging_enabled'

    @property
    def charging_enabled(self):
        """
        Whether drawing power to charge the battery is enabled

        Not all devices have the ability to enable/disable battery charging
        (e.g. because they don't have a battery). In that case,
        ``charging_enabled`` is None.
        """
        if not self.file_exists(self._charging_enabled_path):
            return None
        return self.read_bool(self._charging_enabled_path)

    @charging_enabled.setter
    def charging_enabled(self, enabled):
        """
        Enable/disable drawing power to charge the battery

        Not all devices have this facility. In that case, do nothing.
        """
        if not self.file_exists(self._charging_enabled_path):
            return
        self.write_value(self._charging_enabled_path, int(bool(enabled)))

FstabEntry = namedtuple('FstabEntry', ['device', 'mount_point', 'fs_type', 'options', 'dump_freq', 'pass_num'])
PsEntry = namedtuple('PsEntry', 'user pid ppid vsize rss wchan pc state name')
LsmodEntry = namedtuple('LsmodEntry', ['name', 'size', 'use_count', 'used_by'])


class Cpuinfo(object):

    @property
    @memoized
    def architecture(self):
        for section in self.sections:
            if 'CPU architecture' in section:
                return section['CPU architecture']
            if 'architecture' in section:
                return section['architecture']

    @property
    @memoized
    def cpu_names(self):
        cpu_names = []
        global_name = None
        for section in self.sections:
            if 'processor' in section:
                if 'CPU part' in section:
                    cpu_names.append(_get_part_name(section))
                elif 'model name' in section:
                    cpu_names.append(_get_model_name(section))
                else:
                    cpu_names.append(None)
            elif 'CPU part' in section:
                global_name = _get_part_name(section)
        return [caseless_string(c or global_name) for c in cpu_names]

    def __init__(self, text):
        self.sections = None
        self.text = None
        self.parse(text)

    @memoized
    def get_cpu_features(self, cpuid=0):
        global_features = []
        for section in self.sections:
            if 'processor' in section:
                if int(section.get('processor')) != cpuid:
                    continue
                if 'Features' in section:
                    return section.get('Features').split()
                elif 'flags' in section:
                    return section.get('flags').split()
            elif 'Features' in section:
                global_features = section.get('Features').split()
            elif 'flags' in section:
                global_features = section.get('flags').split()
        return global_features

    def parse(self, text):
        self.sections = []
        current_section = {}
        self.text = text.strip()
        for line in self.text.split('\n'):
            line = line.strip()
            if line:
                key, value = line.split(':', 1)
                current_section[key.strip()] = value.strip()
            else:  # not line
                self.sections.append(current_section)
                current_section = {}
        self.sections.append(current_section)

    def __str__(self):
        return 'CpuInfo({})'.format(self.cpu_names)

    __repr__ = __str__


class KernelVersion(object):
    """
    Class representing the version of a target kernel

    Not expected to work for very old (pre-3.0) kernel version numbers.

    :ivar release: Version number/revision string. Typical output of
                   ``uname -r``
    :type release: str
    :ivar version: Extra version info (aside from ``release``) reported by
                   ``uname``
    :type version: str
    :ivar version_number: Main version number (e.g. 3 for Linux 3.18)
    :type version_number: int
    :ivar major: Major version number (e.g. 18 for Linux 3.18)
    :type major: int
    :ivar minor: Minor version number for stable kernels (e.g. 9 for 4.9.9). May
                 be None
    :type minor: int
    :ivar rc: Release candidate number (e.g. 3 for Linux 4.9-rc3). May be None.
    :type rc: int
    :ivar sha1: Kernel git revision hash, if available (otherwise None)
    :type sha1: str

    :ivar parts: Tuple of version number components. Can be used for
                 lexicographically comparing kernel versions.
    :type parts: tuple(int)
    """
    def __init__(self, version_string):
        if ' #' in version_string:
            release, version = version_string.split(' #')
            self.release = release
            self.version = version
        elif version_string.startswith('#'):
            self.release = ''
            self.version = version_string
        else:
            self.release = version_string
            self.version = ''

        self.version_number = None
        self.major = None
        self.minor = None
        self.sha1 = None
        self.rc = None
        match = KVERSION_REGEX.match(version_string)
        if match:
            groups = match.groupdict()
            self.version_number = int(groups['version'])
            self.major = int(groups['major'])
            if groups['minor'] is not None:
                self.minor = int(groups['minor'])
            if groups['rc'] is not None:
                self.rc = int(groups['rc'])
            if groups['sha1'] is not None:
                self.sha1 = match.group('sha1')

        self.parts = (self.version_number, self.major, self.minor)

    def __str__(self):
        return '{} {}'.format(self.release, self.version)

    __repr__ = __str__


class KernelConfig(object):

    not_set_regex = re.compile(r'# (\S+) is not set')

    @staticmethod
    def get_config_name(name):
        name = name.upper()
        if not name.startswith('CONFIG_'):
            name = 'CONFIG_' + name
        return name

    def iteritems(self):
        return self._config.iteritems()

    def __init__(self, text):
        self.text = text
        self._config = {}
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('#'):
                match = self.not_set_regex.search(line)
                if match:
                    self._config[match.group(1)] = 'n'
            elif '=' in line:
                name, value = line.split('=', 1)
                self._config[name.strip()] = value.strip()

    def get(self, name):
        return self._config.get(self.get_config_name(name))

    def like(self, name):
        regex = re.compile(name, re.I)
        result = {}
        for k, v in self._config.iteritems():
            if regex.search(k):
                result[k] = v
        return result

    def is_enabled(self, name):
        return self.get(name) == 'y'

    def is_module(self, name):
        return self.get(name) == 'm'

    def is_not_set(self, name):
        return self.get(name) == 'n'

    def has(self, name):
        return self.get(name) in ['m', 'y']


class LocalLinuxTarget(LinuxTarget):

    def __init__(self,
                 connection_settings=None,
                 platform=None,
                 working_directory=None,
                 executables_directory=None,
                 connect=True,
                 modules=None,
                 load_default_modules=True,
                 shell_prompt=DEFAULT_SHELL_PROMPT,
                 conn_cls=LocalConnection,
                 ):
        super(LocalLinuxTarget, self).__init__(connection_settings=connection_settings,
                                               platform=platform,
                                               working_directory=working_directory,
                                               executables_directory=executables_directory,
                                               connect=connect,
                                               modules=modules,
                                               load_default_modules=load_default_modules,
                                               shell_prompt=shell_prompt,
                                               conn_cls=conn_cls)

    def _resolve_paths(self):
        if self.working_directory is None:
            self.working_directory = '/tmp'
        if self.executables_directory is None:
            self.executables_directory = '/tmp'


def _get_model_name(section):
    name_string = section['model name']
    parts = name_string.split('@')[0].strip().split()
    return ' '.join([p for p in parts
                     if '(' not in p and p != 'CPU'])


def _get_part_name(section):
    implementer = section.get('CPU implementer', '0x0')
    part = section['CPU part']
    variant = section.get('CPU variant', '0x0')
    name = get_cpu_name(*map(integer, [implementer, part, variant]))
    if name is None:
        name = '{}/{}/{}'.format(implementer, part, variant)
    return name


def _build_path_tree(path_map, basepath, sep=os.path.sep, dictcls=dict):
    """
    Convert a flat mapping of paths to values into a nested structure of
    dict-line object (``dict``'s by default), mirroring the directory hierarchy
    represented by the paths relative to ``basepath``.

    """
    def process_node(node, path, value):
        parts = path.split(sep, 1)
        if len(parts) == 1:   # leaf
            node[parts[0]] = value
        else:  # branch
            if parts[0] not in node:
                node[parts[0]] = dictcls()
            process_node(node[parts[0]], parts[1], value)

    relpath_map = {os.path.relpath(p, basepath): v
                   for p, v in path_map.iteritems()}

    if len(relpath_map) == 1 and relpath_map.keys()[0] == '.':
        result = relpath_map.values()[0]
    else:
        result = dictcls()
        for path, value in relpath_map.iteritems():
            process_node(result, path, value)

    return result
