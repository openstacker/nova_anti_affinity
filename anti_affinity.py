#!/usr/bin/env python
#
# Copyright 2014 Catalyst IT Ltd
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import argparse
import bz2
import datetime
import os
import pdb
import re
import subprocess
import sys
import time
import traceback
import urllib
import uuid
import prettytable
from collections import namedtuple

from oslo_utils import importutils
from oslo_log import log as logging
from oslo_config import cfg

from keystoneclient.v2_0 import client as keystone_client
from novaclient import client as nova_client
from neutronclient.v2_0 import client as neutron_client

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
DOMAIN = "support"

TIMEOUT = 300

REGIONS = ['nz-hlz-1', 'nz-por-1', 'nz_wlg_2']

SERVER_GROUP_LIST = []


def prepare_log():
    logging.register_options(CONF)
    extra_log_level_defaults = [
        'dogpile=INFO',
        'routes=INFO'
        ]

    logging.set_defaults(
        default_log_levels=logging.get_default_log_levels() +
        extra_log_level_defaults)

    logging.setup(CONF, DOMAIN)


def arg(*args, **kwargs):
    def _decorator(func):
        func.__dict__.setdefault('arguments', []).insert(0, (args, kwargs))
        return func
    return _decorator


class CatalystCloudShell(object):

    NZ_POR_1_NETWORK_ID = '715662b0-dc96-4bbd-9c7f-1a3332a86b27'
    NZ_WLG_2_NETWORK_ID = '77b5a8f2-59d3-4291-9ad0-a0a9d17bcb66'
    NZ_HLZ_1_NETWORK_ID = '2bef9c59-934f-45f1-904d-6ba9afb65a27'

    NZ_POR_1_FLAVOR_ID = '28153197-6690-4485-9dbc-fc24489b0683'
    NZ_WLG_2_FLAVOR_ID = '6371ec4a-47d1-4159-a42f-83b84b80eea7'
    NZ_HLZ_1_FLAVOR_ID = '99fb31cc-fdad-4636-b12b-b1e23e84fb25'

    NZ_POR_1_IMAGE_ID = '5017b18e-e7f6-47b0-b1a2-c60ddf9d0033'
    NZ_WLG_2_IMAGE_ID = 'd105d837-67b7-4db6-8aeb-41d92ecb31e1'
    NZ_HLZ_1_IMAGE_ID = '4bc88816-d240-47d7-ae7d-f7325bca396e'

    def get_base_parser(self):
            parser = argparse.ArgumentParser(
                prog='anti_affinity',
                description='Script for Catalyst Cloud to create many servers'
                            ' with anti-affinity cross all regions.',
                add_help=False,
            )

            # Global arguments
            parser.add_argument('-h', '--help',
                                action='store_true',
                                help=argparse.SUPPRESS,
                                )

            parser.add_argument('-a', '--os-auth-url', metavar='OS_AUTH_URL',
                                type=str, required=False, dest='OS_AUTH_URL',
                                default=os.environ.get('OS_AUTH_URL', None),
                                help='Keystone Authentication URL')

            parser.add_argument('-u', '--os-username', metavar='OS_USERNAME',
                                type=str, required=False, dest='OS_USERNAME',
                                default=os.environ.get('OS_USERNAME', None),
                                help='Username for authentication')

            parser.add_argument('-p', '--os-password', metavar='OS_PASSWORD',
                                type=str, required=False, dest='OS_PASSWORD',
                                default=os.environ.get('OS_PASSWORD', None),
                                help='Password for authentication')

            parser.add_argument('-t', '--os-tenant-name',
                                metavar='OS_TENANT_NAME',
                                type=str, required=False,
                                dest='OS_TENANT_NAME',
                                default=os.environ.get('OS_TENANT_NAME', None),
                                help='Tenant name for authentication')

            parser.add_argument('-r', '--os-region-name',
                                metavar='OS_REGION_NAME',
                                type=str, required=False,
                                dest='OS_REGION_NAME',
                                default=os.environ.get('OS_REGION_NAME', None),
                                help='Region for authentication')

            parser.add_argument('-c', '--os-cacert', metavar='OS_CACERT',
                                dest='OS_CACERT',
                                default=os.environ.get('OS_CACERT'),
                                help='Path of CA TLS certificate(s) used to '
                                'verify the remote server\'s certificate. '
                                'Without this option glance looks for the '
                                'default system CA certificates.')

            parser.add_argument('-k', '--insecure',
                                default=False,
                                action='store_true', dest='OS_INSECURE',
                                help='Explicitly allow script to perform '
                                '\"insecure SSL\" (https) requests. '
                                'The server\'s certificate will not be '
                                'verified against any certificate authorities.'
                                ' This option should be used with caution.')

            return parser

    def get_subcommand_parser(self):
        parser = self.get_base_parser()
        self.subcommands = {}
        subparsers = parser.add_subparsers(metavar='<subcommand>')
        submodule = importutils.import_module('anti_affinity')
        self._find_actions(subparsers, submodule)
        self._find_actions(subparsers, self)
        return parser

    def _find_actions(self, subparsers, actions_module):
        for attr in (a for a in dir(actions_module) if a.startswith('do_')):
            command = attr[3:].replace('_', '-')
            callback = getattr(actions_module, attr)
            desc = callback.__doc__ or ''
            help = desc.strip().split('\n')[0]
            arguments = getattr(callback, 'arguments', [])

            subparser = subparsers.add_parser(command,
                                              help=help,
                                              description=desc,
                                              add_help=False,
                                              formatter_class=HelpFormatter
                                              )
            subparser.add_argument('-h', '--help',
                                   action='help',
                                   help=argparse.SUPPRESS,
                                   )
            self.subcommands[command] = subparser
            for (args, kwargs) in arguments:
                subparser.add_argument(*args, **kwargs)
            subparser.set_defaults(func=callback)

    @arg('command', metavar='<subcommand>', nargs='?',
         help='Display help for <subcommand>.')
    def do_help(self, args):
        """Display help about this program or one of its subcommands."""
        if getattr(args, 'command', None):
            if args.command in self.subcommands:
                self.subcommands[args.command].print_help()
            else:
                raise Exception("'%s' is not a valid subcommand" %
                                args.command)
        else:
            self.parser.print_help()

    def init_client(self, args):
        try:
            from keystoneauth1.identity import generic
            from keystoneauth1 import session

            auth = generic.Password(auth_url=args.OS_AUTH_URL,
                                    username=args.OS_USERNAME,
                                    password=args.OS_PASSWORD,
                                    project_name=args.OS_TENANT_NAME,
                                    user_domain_name="default",
                                    project_domain_name="default",
                                    )
            sess = session.Session(auth=auth)

            keystone = keystone_client.Client(session=sess)
            self.keystone = keystone
        except Exception as e:
            raise e

        try:
            nova = nova_client.Client('2', session=sess,
                                      region_name=args.OS_REGION_NAME)
            self.nova = nova
        except Exception as e:
            raise e

    def main(self, argv):
        parser = self.get_base_parser()
        (options, args) = parser.parse_known_args(argv)

        subcommand_parser = self.get_subcommand_parser()
        self.parser = subcommand_parser

        if options.help or not argv:
            self.do_help(options)
            return 0

        args = subcommand_parser.parse_args(argv)
        if args.func == self.do_help:
            self.do_help(args)
            return 0

        try:
            args.func(self, args)
        except Exception:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_exception(exc_type, exc_value, exc_traceback,
                                      limit=2, file=sys.stdout)


class HelpFormatter(argparse.HelpFormatter):
    def start_section(self, heading):
        # Title-case the headings
        heading = '%s%s' % (heading[0].upper(), heading[1:])
        super(HelpFormatter, self).start_section(heading)


@arg('--servers-number', type=int, metavar='SERVERS_NUMBER',
     dest='SERVERS_NUMBER', default=5,
     help='How many servers will be created')
@arg('--assign-public-ip', type=bool, metavar='ASSIGN_PUBLIC_IP',
     dest='ASSIGN_PUBLIC_IP', default=False,
     help='If assign public ip for servers')
@arg('--path-cloud-init-script', type=str, metavar='PATH_CLOUD_INIT_SCRIPT',
     dest='PATH_CLOUD_INIT_SCRIPT',
     help='Path to cloud init script')
@arg('--name-prefix', type=str, metavar='NAME_PREFIX',
     dest='NAME_PREFIX', default="server-",
          help='The name prefix for servers')
@arg('--keypair-name', type=str, metavar='KEYPAIR_NAME',
     dest='KEYPAIR_NAME',required=True,
     help='The name of keypair to be injected into server')
def do_create(shell, args):
    """ Boot servers with anti-affinity policy
    """
    import pdb
    pdb.set_trace()
    LOG.info("Start to create %d servers across all regions..." % args.SERVERS_NUMBER);
    servers = []
    for i in range(args.SERVERS_NUMBER):
        for region in REGIONS:
            group = _find_server_group(shell, region, args)
            import pdb
            pdb.set_trace()
            if group["is_full"]:
                continue  

            args.OS_REGION_NAME = region
            shell.init_client(args)

            capital_region = args.OS_REGION_NAME.replace('-', '_').upper()
            shell.flavor_id = getattr(shell,  capital_region + '_FLAVOR_ID')
            shell.network_id = getattr(shell, capital_region + '_NETWORK_ID')
            shell.image_id = getattr(shell, capital_region + '_IMAGE_ID')
    
            try:
                server = _create_server(shell,
                                        args.NAME_PREFIX + str(uuid.uuid4()),
                                        args.KEYPAIR_NAME,
                                        group["group"].id,
                                        path_cloud_init_script=args.PATH_CLOUD_INIT_SCRIPT,
                                        assign_public_ip=args.ASSIGN_PUBLIC_IP)

                resp = _check_server_status(shell, server)
                import pdb
                pdb.set_trace()
                if resp["active"]:
                    # If the server is created successfully, then try to
                    # create another one
                    LOG.info("Create server %s successfully on regions %s" % (server.name, region))
                    servers.append(server)
                    break
                elif "No valid host" in resp["fault"]["message"]:
                    # If the server is failed then try to create it in
                    # another region
                    import pdb
                    pdb.set_trace()
                    SERVER_GROUP_LIST[-1][region]["is_full"] = True
                    shell.nova.servers.delete(server.id)
                    continue
                else:
                    import pdb
                    pdb.set_trace()
                    LOG.info("Unknown error of server %s" % server.id)
            except Exception as e:
                LOG.error(e)




def _find_server_group(shell, region_name, args):
    # If there is no server group or all are full
    if (len(SERVER_GROUP_LIST) == 0 or (len(SERVER_GROUP_LIST)> 0 and all([region["is_full"] for region in SERVER_GROUP_LIST[-1].values()]))):
        # Would like to have same server group name for all regions
        group_name = "AF-" + str(uuid.uuid4())
        region_groups = {}
        for region in REGIONS:
            args.OS_REGION_NAME = region
            shell.init_client(args)
            
            # Clean old unused server groups
            old_groups = shell.nova.server_groups.list()
            for g in old_groups:
                try:
                    if g.name.startswith("AF-"):
                        shell.nova.server_groups.delete(g.id)
                except:
                    pass
            
            group = shell.nova.server_groups.create(group_name, 'anti-affinity')
            region_groups[region] = {"group": group, "is_full": False}
        
        LOG.info("Created new server groups %s" % str(region_groups))
        SERVER_GROUP_LIST.append(region_groups)

    return SERVER_GROUP_LIST[-1][region_name]


def _check_server_status(shell, server):
    def check():
        inst = shell.nova.servers.get(server.id)
        return inst.status == "ACTIVE"

    status = call_until_true(check, 60, 3)

    if status:
        return {"active": True, "fault": ""}
    else:
        return {"active": False, "fault": getattr(server, "fault", "")}


def _create_server(shell, name, keypair_name, group_id,
                   path_cloud_init_script=None,
                   assign_public_ip=False):
    dev_mapping_2 = {
         'device_name': None,
         'source_type': 'image',
         'destination_type': 'volume',
         'delete_on_termination': 'true',
         'uuid': shell.image_id,
         'volume_size': '20',
    }
    import pdb
    pdb.set_trace()

    create_kwargs = {}

    if path_cloud_init_script:
        boot_kwargs["user_data"] = path_cloud_init_script
  
    try:
        server = shell.nova.servers.create(name,
                                           shell.image_id,
                                           shell.flavor_id,
                                           block_device_mapping_v2=[dev_mapping_2,],
                                           nics=[{'net-id': shell.network_id}],
                                           key_name=keypair_name,
                                           scheduler_hints={"group": group_id},
                                           **create_kwargs)
    except Exception as e:
        raise e

    if assign_public_ip:
        floating_ip = shell.nova.floating_ips.create()
        sleep(10)
        server.add_floating_ip(floating_ip)

    import pdb
    pdb.set_trace()
    return server


def call_until_true(func, duration, sleep_for):
    now = time.time()
    timeout = now + duration
    while now < timeout:
        if func():
            return True
        time.sleep(sleep_for)
        now = time.time()
    return False


def print_list(objs, fields, formatters={}):
    pt = prettytable.PrettyTable([f for f in fields], caching=False)
    pt.align = 'l'

    for o in objs:
        row = []
        for field in fields:
            if field in formatters:
                row.append(formatters[field](o))
            else:
                field_name = field.lower().replace(' ', '_')
                if type(o) == dict and field in o:
                    data = o[field_name]
                else:
                    data = getattr(o, field_name, None) or ''
                row.append(data)
        pt.add_row(row)

    print(encodeutils.safe_encode(pt.get_string()))



if __name__ == '__main__':
    prepare_log()

    try:
        CatalystCloudShell().main(sys.argv[1:])
    except KeyboardInterrupt:
        print("Terminating...")
        sys.exit(1)
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback,
                                  limit=2, file=sys.stdout)