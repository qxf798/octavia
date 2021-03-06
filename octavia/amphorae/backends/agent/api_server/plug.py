# Copyright 2015 Hewlett-Packard Development Company, L.P.
# Copyright 2016 Rackspace
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import ipaddress
import os
import socket
import stat
import subprocess

from oslo_config import cfg
from oslo_log import log as logging
import pyroute2
import webob
from werkzeug import exceptions

from octavia.common import constants as consts


CONF = cfg.CONF

LOG = logging.getLogger(__name__)


class Plug(object):
    def __init__(self, osutils):
        self._osutils = osutils

    def plug_lo(self):
        self._osutils.write_interface_file(
            interface="lo",
            ip_address="127.0.0.1",
            prefixlen=8)

    def plug_vip(self, vip, subnet_cidr, gateway,
                 mac_address, mtu=None, vrrp_ip=None, host_routes=None):
        # Validate vip and subnet_cidr, calculate broadcast address and netmask
        try:
            ip = ipaddress.ip_address(vip)
            network = ipaddress.ip_network(subnet_cidr)
            vip = ip.exploded
            prefixlen = network.prefixlen
        except ValueError:
            return webob.Response(json=dict(message="Invalid VIP"),
                                  status=400)

        # Check if the interface is already in the network namespace
        # Do not attempt to re-plug the VIP if it is already in the
        # network namespace
        if self._netns_interface_exists(mac_address):
            return webob.Response(
                json=dict(message="Interface already exists"), status=409)

        # Check that the interface has been fully plugged
        self._interface_by_mac(mac_address)

        # Always put the VIP interface as eth1
        primary_interface = consts.NETNS_PRIMARY_INTERFACE

        self._osutils.write_vip_interface_file(
            interface=primary_interface,
            vip=vip,
            ip_version=ip.version,
            prefixlen=prefixlen,
            gateway=gateway,
            mtu=mtu,
            vrrp_ip=vrrp_ip,
            host_routes=host_routes)

        # Update the list of interfaces to add to the namespace
        # This is used in the amphora reboot case to re-establish the namespace
        self._update_plugged_interfaces_file(primary_interface, mac_address)

        # Create the namespace
        netns = pyroute2.NetNS(consts.AMPHORA_NAMESPACE, flags=os.O_CREAT)
        netns.close()

        # Load sysctl in new namespace
        sysctl = pyroute2.NSPopen(consts.AMPHORA_NAMESPACE,
                                  [consts.SYSCTL_CMD, '--system'],
                                  stdout=subprocess.PIPE)

        sysctl.communicate()
        sysctl.wait()
        sysctl.release()

        cmd_list = [['modprobe', 'ip_vs'],
                    [consts.SYSCTL_CMD, '-w', 'net.ipv4.vs.conntrack=1']]
        if ip.version == 4:
            # For lvs function, enable ip_vs kernel module, enable ip_forward
            # conntrack in amphora network namespace.
            cmd_list.append([consts.SYSCTL_CMD, '-w', 'net.ipv4.ip_forward=1'])
        elif ip.version == 6:
            cmd_list.append([consts.SYSCTL_CMD, '-w',
                             'net.ipv6.conf.all.forwarding=1'])
        for cmd in cmd_list:
            ns_exec = pyroute2.NSPopen(consts.AMPHORA_NAMESPACE, cmd,
                                       stdout=subprocess.PIPE)
            ns_exec.wait()
            ns_exec.release()

        with pyroute2.IPRoute() as ipr:
            # Move the interfaces into the namespace
            idx = ipr.link_lookup(address=mac_address)[0]
            ipr.link('set', index=idx, net_ns_fd=consts.AMPHORA_NAMESPACE,
                     IFLA_IFNAME=primary_interface)

        # bring interfaces up
        self._osutils.bring_interfaces_up(ip, primary_interface)

        return webob.Response(json=dict(
            message="OK",
            details="VIP {vip} plugged on interface {interface}".format(
                vip=vip, interface=primary_interface)), status=202)

    def _check_ip_addresses(self, fixed_ips):
        if fixed_ips:
            for ip in fixed_ips:
                try:
                    socket.inet_pton(socket.AF_INET, ip.get('ip_address'))
                except socket.error:
                    socket.inet_pton(socket.AF_INET6, ip.get('ip_address'))

    def plug_network(self, mac_address, fixed_ips, mtu=None):
        # Check if the interface is already in the network namespace
        # Do not attempt to re-plug the network if it is already in the
        # network namespace
        if self._netns_interface_exists(mac_address):
            return webob.Response(json=dict(
                message="Interface already exists"), status=409)

        # This is the interface as it was initially plugged into the
        # default network namespace, this will likely always be eth1

        try:
            self._check_ip_addresses(fixed_ips=fixed_ips)
        except socket.error:
            return webob.Response(json=dict(
                message="Invalid network port"), status=400)

        default_netns_interface = self._interface_by_mac(mac_address)

        # We need to determine the interface name when inside the namespace
        # to avoid name conflicts
        with pyroute2.NetNS(consts.AMPHORA_NAMESPACE,
                            flags=os.O_CREAT) as netns:

            # 1 means just loopback, but we should already have a VIP. This
            # works for the add/delete/add case as we don't delete interfaces
            # Note, eth0 is skipped because that is the VIP interface
            netns_interface = 'eth{0}'.format(len(netns.get_links()))

        LOG.info('Plugged interface %s will become %s in the namespace %s',
                 default_netns_interface, netns_interface,
                 consts.AMPHORA_NAMESPACE)
        self._osutils.write_port_interface_file(
            interface=netns_interface,
            fixed_ips=fixed_ips,
            mtu=mtu)

        # Update the list of interfaces to add to the namespace
        self._update_plugged_interfaces_file(netns_interface, mac_address)

        with pyroute2.IPRoute() as ipr:
            # Move the interfaces into the namespace
            idx = ipr.link_lookup(address=mac_address)[0]
            ipr.link('set', index=idx,
                     net_ns_fd=consts.AMPHORA_NAMESPACE,
                     IFLA_IFNAME=netns_interface)

        self._osutils._bring_if_down(netns_interface)
        self._osutils._bring_if_up(netns_interface, 'network')

        return webob.Response(json=dict(
            message="OK",
            details="Plugged on interface {interface}".format(
                interface=netns_interface)), status=202)

    def _interface_by_mac(self, mac):
        try:
            with pyroute2.IPRoute() as ipr:
                idx = ipr.link_lookup(address=mac)[0]
                addr = ipr.get_links(idx)[0]
                for attr in addr['attrs']:
                    if attr[0] == 'IFLA_IFNAME':
                        return attr[1]
        except Exception as e:
            LOG.info('Unable to find interface with MAC: %s, rescanning '
                     'and returning 404. Reported error: %s', mac, str(e))

        # Poke the kernel to re-enumerate the PCI bus.
        # We have had cases where nova hot plugs the interface but
        # the kernel doesn't get the memo.
        filename = '/sys/bus/pci/rescan'
        flags = os.O_WRONLY
        if os.path.isfile(filename):
            with os.fdopen(os.open(filename, flags), 'w') as rescan_file:
                rescan_file.write('1')
        raise exceptions.HTTPException(
            response=webob.Response(json=dict(
                details="No suitable network interface found"), status=404))

    def _update_plugged_interfaces_file(self, interface, mac_address):
        # write interfaces to plugged_interfaces file and prevent duplicates
        plug_inf_file = consts.PLUGGED_INTERFACES
        flags = os.O_RDWR | os.O_CREAT
        # mode 0644
        mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
        with os.fdopen(os.open(plug_inf_file, flags, mode), 'r+') as text_file:
            inf_list = [inf.split()[0].rstrip() for inf in text_file]
            if mac_address not in inf_list:
                text_file.write("{mac_address} {interface}\n".format(
                    mac_address=mac_address, interface=interface))

    def _netns_interface_exists(self, mac_address):
        with pyroute2.NetNS(consts.AMPHORA_NAMESPACE,
                            flags=os.O_CREAT) as netns:
            for link in netns.get_links():
                for attr in link['attrs']:
                    if attr[0] == 'IFLA_ADDRESS' and attr[1] == mac_address:
                        return True
        return False
