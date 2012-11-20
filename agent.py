#!/usr/bin/python
'''
    Datadog
    www.datadoghq.com
    ----
    Make sense of your IT Data

    Licensed under Simplified BSD License (see LICENSE)
    (C) Boxed Ice 2010 all rights reserved
    (C) Datadog, Inc. 2010 all rights reserved
'''

# Core modules
import logging
import modules
import os
import os.path
import re
import signal
import sys
import time
import urllib

# Check we're not using an old version of Python. We need 2.4 above because some modules (like subprocess)
# were only introduced in 2.4.
if int(sys.version_info[1]) <= 3:
    sys.stderr.write("Datadog agent requires python 2.4 or later.\n")
    sys.exit(2)

# Custom modules
from checks.collector import Collector
from checks.check_status import CollectorStatus
from checks.ec2 import EC2
from config import get_config, get_system_stats, get_parsed_args, load_check_directory
from daemon import Daemon
from emitter import http_emitter
from util import Watchdog, PidFile


# Constants
PID_NAME = "dd-agent"
WATCHDOG_MULTIPLIER = 10

# Globals
agent_logger = logging.getLogger('agent')


class Agent(Daemon):
    """
    The agent class is a daemon that runs the collector in a background process.
    """

    def __init__(self, pidfile):
        Daemon.__init__(self, pidfile)
        self.run_forever = True

    def _handle_sigterm(self, signum, frame):
        agent_logger.info("Caught sigterm. Exiting")
        self.run_forever = False

    def run(self, agentConfig=None, run_forever=True):
        """Main loop of the collector"""

        signal.signal(signal.SIGTERM, self._handle_sigterm)

        # Save a start-up status message and delete whatever status message we
        # have on exit.
        CollectorStatus(start_up=True).persist()

        systemStats = get_system_stats()

        if agentConfig is None:
            agentConfig = get_config()

        agentConfig = self._set_agent_config_hostname(agentConfig)

        # Load the checks.d checks
        checksd = load_check_directory(agentConfig)

    
        emitters = [http_emitter]
        for emitter_spec in [s.strip() for s in agentConfig.get('custom_emitters', '').split(',')]:
            if len(emitter_spec) == 0: continue
            emitters.append(modules.load(emitter_spec, 'emitter'))

        check_freq = int(agentConfig['check_freq'])

        # Checks instance
        collector = Collector(agentConfig, emitters, systemStats)

        # Watchdog
        watchdog = None
        if agentConfig.get("watchdog", True):
            watchdog = Watchdog(check_freq * WATCHDOG_MULTIPLIER)
            watchdog.reset()

        while self.run_forever:
            collector.run(checksd=checksd)
            if watchdog is not None:
                watchdog.reset()
            
            # Only sleep if we'll continue.
            if self.run_forever:
                time.sleep(check_freq)

        CollectorStatus.remove_latest_status()
        sys.exit(0)


    def _set_agent_config_hostname(self, agentConfig):
        # Try to fetch instance Id from EC2 if not hostname has been set
        # in the config file.
        # DEPRECATED
        if agentConfig.get('hostname') is None and agentConfig.get('use_ec2_instance_id'):
            instanceId = EC2.get_instance_id()
            if instanceId is not None:
                agent_logger.info("Running on EC2, instanceId: %s" % instanceId)
                agentConfig['hostname'] = instanceId
            else:
                agent_logger.info('Not running on EC2, using hostname to identify this server')
        return agentConfig


def setup_logging(agentConfig):
    """Configure logging to use syslog whenever possible.
    Also controls debug_mode."""
    if agentConfig['debug_mode']:
        logFile = "/tmp/dd-agent.log"
        logging.basicConfig(filename=logFile, filemode='w', level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        logging.info("Logging to %s" % logFile)
    else:
        try:
            from logging.handlers import SysLogHandler
            rootLog = logging.getLogger()
            rootLog.setLevel(logging.INFO)

            sys_log_addr = "/dev/log"

            # Special-case macs
            if sys.platform == 'darwin':
                sys_log_addr = "/var/run/syslog"

            handler = SysLogHandler(address=sys_log_addr, facility=SysLogHandler.LOG_DAEMON)
            formatter = logging.Formatter("dd-agent - %(name)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            rootLog.addHandler(handler)
            logging.info('Logging to syslog is set up')
        except Exception,e:
            sys.stderr.write("Error while setting up syslog logging (%s). No logging available" % str(e))
            logging.disable(logging.ERROR)


def main():
    options, args = get_parsed_args()
    agentConfig = get_config()

    # Logging
    setup_logging(agentConfig)

    if len(args) > 0:
        command = args[0]

        pid_file = PidFile('dd-agent')

        if options.clean:
            pid_file.clean()

        daemon = Agent(pid_file.get_path())

        if 'start' == command:
            logging.info('Start daemon')
            daemon.start()

        elif 'stop' == command:
            logging.info('Stop daemon')
            daemon.stop()

        elif 'restart' == command:
            logging.info('Restart daemon')
            daemon.restart()

        elif 'foreground' == command:
            logging.info('Running in foreground')
            daemon.run()

        elif 'status' == command:
            pid = pid_file.get_pid()
            if pid is not None:
                sys.stdout.write('dd-agent is running as pid %s.\n' % pid)
                logging.info("dd-agent is running as pid %s." % pid)
            else:
                sys.stdout.write('dd-agent is not running.\n')
                logging.info("dd-agent is not running.")

        elif 'check_status' == command:
            CollectorStatus.print_latest_status()

        else:
            sys.stderr.write('Unknown command: %s.\n' % sys.argv[1])
            sys.exit(2)

        sys.exit(0)

    else:
        sys.stderr.write('Usage: %s start|stop|restart|foreground|status' % sys.argv[0])
        sys.exit(2)


if __name__ == '__main__':
    try:
        main()
    except SystemExit, KeyboardInterrupt:
        pass
    except:
        # Try our best to log the error.
        try:
            agent_logger.exception("Uncaught error running the agent")
        except:
            pass
