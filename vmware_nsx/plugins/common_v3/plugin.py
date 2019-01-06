# Copyright 2018 VMware, Inc.
# All Rights Reserved
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


import netaddr
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
from oslo_utils import excutils
from sqlalchemy import exc as sql_exc
import webob.exc

from six import moves

from neutron.db import agentschedulers_db
from neutron.db import allowedaddresspairs_db as addr_pair_db
from neutron.db.availability_zone import router as router_az_db
from neutron.db import dns_db
from neutron.db import external_net_db
from neutron.db import extradhcpopt_db
from neutron.db import extraroute_db
from neutron.db import l3_attrs_db
from neutron.db import l3_db
from neutron.db import l3_gwmode_db
from neutron.db import portbindings_db
from neutron.db import portsecurity_db
from neutron.db import securitygroups_db
from neutron.db import vlantransparent_db
from neutron.extensions import securitygroup as ext_sg
from neutron_lib.api.definitions import allowedaddresspairs as addr_apidef
from neutron_lib.api.definitions import availability_zone as az_def
from neutron_lib.api.definitions import external_net as extnet_apidef
from neutron_lib.api.definitions import port_security as psec
from neutron_lib.api.definitions import portbindings as pbin
from neutron_lib.api.definitions import provider_net as pnet
from neutron_lib.api import faults
from neutron_lib.api import validators
from neutron_lib.api.validators import availability_zone as az_validator
from neutron_lib import constants
from neutron_lib.db import api as db_api
from neutron_lib.db import utils as db_utils
from neutron_lib import exceptions as n_exc
from neutron_lib.exceptions import allowedaddresspairs as addr_exc
from neutron_lib.exceptions import port_security as psec_exc
from neutron_lib.plugins import utils as plugin_utils
from neutron_lib.services.qos import constants as qos_consts
from neutron_lib.utils import helpers
from neutron_lib.utils import net as nl_net_utils

from vmware_nsx.common import availability_zones as nsx_com_az
from vmware_nsx.common import exceptions as nsx_exc
from vmware_nsx.common import locking
from vmware_nsx.common import nsx_constants
from vmware_nsx.common import utils
from vmware_nsx.db import db as nsx_db
from vmware_nsx.db import extended_security_group as extended_sec
from vmware_nsx.db import extended_security_group_rule as extend_sg_rule
from vmware_nsx.db import maclearning as mac_db
from vmware_nsx.db import nsx_portbindings_db as pbin_db
from vmware_nsx.extensions import maclearning as mac_ext
from vmware_nsx.extensions import providersecuritygroup as provider_sg
from vmware_nsx.extensions import secgroup_rule_local_ip_prefix as sg_prefix
from vmware_nsx.plugins.common import plugin
from vmware_nsx.services.qos.common import utils as qos_com_utils
from vmware_nsx.services.vpnaas.nsxv3 import ipsec_utils

from vmware_nsxlib.v3 import exceptions as nsx_lib_exc
from vmware_nsxlib.v3 import nsx_constants as nsxlib_consts

LOG = logging.getLogger(__name__)


# NOTE(asarfaty): the order of inheritance here is important. in order for the
# QoS notification to work, the AgentScheduler init must be called first
# NOTE(arosen): same is true with the ExtendedSecurityGroupPropertiesMixin
# this needs to be above securitygroups_db.SecurityGroupDbMixin.
# FIXME(arosen): we can solve this inheritance order issue by just mixining in
# the classes into a new class to handle the order correctly.
class NsxPluginV3Base(agentschedulers_db.AZDhcpAgentSchedulerDbMixin,
                      addr_pair_db.AllowedAddressPairsMixin,
                      plugin.NsxPluginBase,
                      extended_sec.ExtendedSecurityGroupPropertiesMixin,
                      pbin_db.NsxPortBindingMixin,
                      extend_sg_rule.ExtendedSecurityGroupRuleMixin,
                      securitygroups_db.SecurityGroupDbMixin,
                      external_net_db.External_net_db_mixin,
                      extraroute_db.ExtraRoute_db_mixin,
                      router_az_db.RouterAvailabilityZoneMixin,
                      l3_gwmode_db.L3_NAT_db_mixin,
                      portbindings_db.PortBindingMixin,
                      portsecurity_db.PortSecurityDbMixin,
                      extradhcpopt_db.ExtraDhcpOptMixin,
                      dns_db.DNSDbMixin,
                      vlantransparent_db.Vlantransparent_db_mixin,
                      mac_db.MacLearningDbMixin,
                      l3_attrs_db.ExtraAttributesMixin,
                      nsx_com_az.NSXAvailabilityZonesPluginCommon):
    """Common methods for NSX-V3 plugins (NSX-V3 & Policy)"""

    def __init__(self):

        super(NsxPluginV3Base, self).__init__()
        self._network_vlans = plugin_utils.parse_network_vlan_ranges(
            self._get_conf_attr('network_vlan_ranges'))

    def _init_native_dhcp(self):
        if not self.nsxlib:
            return

        try:
            for az in self.get_azs_list():
                self.nsxlib.native_dhcp_profile.get(
                    az._native_dhcp_profile_uuid)
        except nsx_lib_exc.ManagerError:
            with excutils.save_and_reraise_exception():
                LOG.error("Unable to retrieve DHCP Profile %s, "
                          "native DHCP service is not supported",
                          az._native_dhcp_profile_uuid)

    def _extend_fault_map(self):
        """Extends the Neutron Fault Map.

        Exceptions specific to the NSX Plugin are mapped to standard
        HTTP Exceptions.
        """
        faults.FAULT_MAP.update({nsx_lib_exc.ManagerError:
                                 webob.exc.HTTPBadRequest,
                                 nsx_lib_exc.ServiceClusterUnavailable:
                                 webob.exc.HTTPServiceUnavailable,
                                 nsx_lib_exc.ClientCertificateNotTrusted:
                                 webob.exc.HTTPBadRequest,
                                 nsx_exc.SecurityGroupMaximumCapacityReached:
                                 webob.exc.HTTPBadRequest,
                                 nsx_lib_exc.NsxLibInvalidInput:
                                 webob.exc.HTTPBadRequest,
                                 nsx_exc.NsxENSPortSecurity:
                                 webob.exc.HTTPBadRequest,
                                 })

    def _get_conf_attr(self, attr):
        plugin_cfg = getattr(cfg.CONF, self.cfg_group)
        return getattr(plugin_cfg, attr)

    def _get_interface_network(self, context, interface_info):
        is_port, is_sub = self._validate_interface_info(interface_info)
        if is_port:
            net_id = self.get_port(context,
                                   interface_info['port_id'])['network_id']
        elif is_sub:
            net_id = self.get_subnet(context,
                                     interface_info['subnet_id'])['network_id']
        return net_id

    def _fix_sg_rule_dict_ips(self, sg_rule):
        # 0.0.0.0/# is not a valid entry for local and remote so we need
        # to change this to None
        if (sg_rule.get('remote_ip_prefix') and
            sg_rule['remote_ip_prefix'].startswith('0.0.0.0/')):
            sg_rule['remote_ip_prefix'] = None
        if (sg_rule.get(sg_prefix.LOCAL_IP_PREFIX) and
            validators.is_attr_set(sg_rule[sg_prefix.LOCAL_IP_PREFIX]) and
            sg_rule[sg_prefix.LOCAL_IP_PREFIX].startswith('0.0.0.0/')):
            sg_rule[sg_prefix.LOCAL_IP_PREFIX] = None

    def _validate_interface_address_scope(self, context,
                                          router_db, interface_info):
        gw_network_id = (router_db.gw_port.network_id if router_db.gw_port
                         else None)

        subnet = self.get_subnet(context, interface_info['subnet_ids'][0])
        if not router_db.enable_snat and gw_network_id:
            self._validate_address_scope_for_router_interface(
                context.elevated(), router_db.id, gw_network_id, subnet['id'])

    def _validate_ipv4_address_pairs(self, address_pairs):
        for pair in address_pairs:
            ip = pair.get('ip_address')
            if not utils.is_ipv4_ip_address(ip):
                raise nsx_exc.InvalidIPAddress(ip_address=ip)

    def _create_port_address_pairs(self, context, port_data):
        (port_security, has_ip) = self._determine_port_security_and_has_ip(
            context, port_data)

        address_pairs = port_data.get(addr_apidef.ADDRESS_PAIRS)
        if validators.is_attr_set(address_pairs):
            if not port_security:
                raise addr_exc.AddressPairAndPortSecurityRequired()
            else:
                self._validate_ipv4_address_pairs(address_pairs)
                self._process_create_allowed_address_pairs(context, port_data,
                                                           address_pairs)
        else:
            port_data[addr_apidef.ADDRESS_PAIRS] = []

    def _provider_sgs_specified(self, port_data):
        # checks if security groups were updated adding/modifying
        # security groups, port security is set and port has ip
        provider_sgs_specified = (validators.is_attr_set(
            port_data.get(provider_sg.PROVIDER_SECURITYGROUPS)) and
            port_data.get(provider_sg.PROVIDER_SECURITYGROUPS) != [])
        return provider_sgs_specified

    def _create_port_preprocess_security(
            self, context, port, port_data, neutron_db, is_ens_tz_port):
        (port_security, has_ip) = self._determine_port_security_and_has_ip(
            context, port_data)
        port_data[psec.PORTSECURITY] = port_security
        # No port security is allowed if the port belongs to an ENS TZ
        if (port_security and is_ens_tz_port and
            not self._ens_psec_supported()):
            raise nsx_exc.NsxENSPortSecurity()
        self._process_port_port_security_create(
                context, port_data, neutron_db)

        # allowed address pair checks
        self._create_port_address_pairs(context, port_data)

        if port_security and has_ip:
            self._ensure_default_security_group_on_port(context, port)
            (sgids, psgids) = self._get_port_security_groups_lists(
                context, port)
        elif (self._check_update_has_security_groups({'port': port_data}) or
              self._provider_sgs_specified(port_data) or
              self._get_provider_security_groups_on_port(context, port)):
            LOG.error("Port has conflicting port security status and "
                      "security groups")
            raise psec_exc.PortSecurityAndIPRequiredForSecurityGroups()
        else:
            sgids = psgids = []
        port_data[ext_sg.SECURITYGROUPS] = (
            self._get_security_groups_on_port(context, port))
        return port_security, has_ip, sgids, psgids

    def _should_validate_port_sec_on_update_port(self, port_data):
        # Need to determine if we skip validations for port security.
        # This is the edge case when the subnet is deleted.
        # This should be called prior to deleting the fixed ip from the
        # port data
        for fixed_ip in port_data.get('fixed_ips', []):
            if 'delete_subnet' in fixed_ip:
                return False
        return True

    def _update_port_preprocess_security(
            self, context, port, id, updated_port, is_ens_tz_port,
            validate_port_sec=True, direct_vnic_type=False):
        delete_addr_pairs = self._check_update_deletes_allowed_address_pairs(
            port)
        has_addr_pairs = self._check_update_has_allowed_address_pairs(port)
        has_security_groups = self._check_update_has_security_groups(port)
        delete_security_groups = self._check_update_deletes_security_groups(
            port)

        # populate port_security setting
        port_data = port['port']
        if psec.PORTSECURITY not in port_data:
            updated_port[psec.PORTSECURITY] = \
                self._get_port_security_binding(context, id)
        has_ip = self._ip_on_port(updated_port)
        # validate port security and allowed address pairs
        if not updated_port[psec.PORTSECURITY]:
            #  has address pairs in request
            if has_addr_pairs:
                raise addr_exc.AddressPairAndPortSecurityRequired()
            elif not delete_addr_pairs:
                # check if address pairs are in db
                updated_port[addr_apidef.ADDRESS_PAIRS] = (
                    self.get_allowed_address_pairs(context, id))
                if updated_port[addr_apidef.ADDRESS_PAIRS]:
                    raise addr_exc.AddressPairAndPortSecurityRequired()

        if delete_addr_pairs or has_addr_pairs:
            self._validate_ipv4_address_pairs(
                updated_port[addr_apidef.ADDRESS_PAIRS])
            # delete address pairs and read them in
            self._delete_allowed_address_pairs(context, id)
            self._process_create_allowed_address_pairs(
                context, updated_port,
                updated_port[addr_apidef.ADDRESS_PAIRS])

        if updated_port[psec.PORTSECURITY] and psec.PORTSECURITY in port_data:
            # No port security is allowed if the port belongs to an ENS TZ
            if is_ens_tz_port and not self._ens_psec_supported():
                raise nsx_exc.NsxENSPortSecurity()

            # No port security is allowed if the port has a direct vnic type
            if direct_vnic_type:
                err_msg = _("Security features are not supported for "
                            "ports with direct/direct-physical VNIC type")
                raise n_exc.InvalidInput(error_message=err_msg)

        # checks if security groups were updated adding/modifying
        # security groups, port security is set and port has ip
        provider_sgs_specified = self._provider_sgs_specified(updated_port)
        if (validate_port_sec and
            not (has_ip and updated_port[psec.PORTSECURITY])):
            if has_security_groups or provider_sgs_specified:
                LOG.error("Port has conflicting port security status and "
                          "security groups")
                raise psec_exc.PortSecurityAndIPRequiredForSecurityGroups()
            # Update did not have security groups passed in. Check
            # that port does not have any security groups already on it.
            filters = {'port_id': [id]}
            security_groups = (
                super(NsxPluginV3Base, self)._get_port_security_group_bindings(
                    context, filters)
            )
            if security_groups and not delete_security_groups:
                raise psec_exc.PortSecurityPortHasSecurityGroup()

        if delete_security_groups or has_security_groups:
            # delete the port binding and read it with the new rules.
            self._delete_port_security_group_bindings(context, id)
            sgids = self._get_security_groups_on_port(context, port)
            self._process_port_create_security_group(context, updated_port,
                                                     sgids)

        if psec.PORTSECURITY in port['port']:
            self._process_port_port_security_update(
                context, port['port'], updated_port)

        return updated_port

    def _validate_create_network(self, context, net_data):
        """Validate the parameters of the new network to be created

        This method includes general validations that does not depend on
        provider attributes, or plugin specific configurations
        """
        external = net_data.get(extnet_apidef.EXTERNAL)
        is_external_net = validators.is_attr_set(external) and external
        with_qos = validators.is_attr_set(
            net_data.get(qos_consts.QOS_POLICY_ID))

        if with_qos:
            self._validate_qos_policy_id(
                context, net_data.get(qos_consts.QOS_POLICY_ID))
            if is_external_net:
                raise nsx_exc.QoSOnExternalNet()

    def _validate_update_network(self, context, id, original_net, net_data):
        """Validate the updated parameters of a network

        This method includes general validations that does not depend on
        provider attributes, or plugin specific configurations
        """
        extern_net = self._network_is_external(context, id)
        with_qos = validators.is_attr_set(
            net_data.get(qos_consts.QOS_POLICY_ID))

        # Do not allow QoS on external networks
        if with_qos and extern_net:
            raise nsx_exc.QoSOnExternalNet()

        # Do not support changing external/non-external networks
        if (extnet_apidef.EXTERNAL in net_data and
            net_data[extnet_apidef.EXTERNAL] != extern_net):
            err_msg = _("Cannot change the router:external flag of a network")
            raise n_exc.InvalidInput(error_message=err_msg)

    def _assert_on_illegal_port_with_qos(self, device_owner):
        # Prevent creating/update port with QoS policy
        # on router-interface/network-dhcp ports.
        if ((device_owner == l3_db.DEVICE_OWNER_ROUTER_INTF or
             device_owner == constants.DEVICE_OWNER_DHCP)):
            err_msg = _("Unable to create or update %s port with a QoS "
                        "policy") % device_owner
            LOG.warning(err_msg)
            raise n_exc.InvalidInput(error_message=err_msg)

    def _assert_on_external_net_with_compute(self, port_data):
        # Prevent creating port with device owner prefix 'compute'
        # on external networks.
        device_owner = port_data.get('device_owner')
        if (device_owner is not None and
            device_owner.startswith(constants.DEVICE_OWNER_COMPUTE_PREFIX)):
            err_msg = _("Unable to update/create a port with an external "
                        "network")
            LOG.warning(err_msg)
            raise n_exc.InvalidInput(error_message=err_msg)

    def _validate_create_port(self, context, port_data):
        self._validate_max_ips_per_port(port_data.get('fixed_ips', []),
                                        port_data.get('device_owner'))

        is_external_net = self._network_is_external(
            context, port_data['network_id'])
        qos_selected = validators.is_attr_set(port_data.get(
            qos_consts.QOS_POLICY_ID))
        device_owner = port_data.get('device_owner')

        # QoS validations
        if qos_selected:
            self._validate_qos_policy_id(
                context, port_data.get(qos_consts.QOS_POLICY_ID))
            self._assert_on_illegal_port_with_qos(device_owner)
            if is_external_net:
                raise nsx_exc.QoSOnExternalNet()

        # External network validations:
        if is_external_net:
            self._assert_on_external_net_with_compute(port_data)

        self._assert_on_port_admin_state(port_data, device_owner)

    def _assert_on_vpn_port_change(self, port_data):
        if port_data['device_owner'] == ipsec_utils.VPN_PORT_OWNER:
            msg = _('Can not update/delete VPNaaS port %s') % port_data['id']
            raise n_exc.InvalidInput(error_message=msg)

    def _assert_on_lb_port_fixed_ip_change(self, port_data, orig_dev_own):
        if orig_dev_own == constants.DEVICE_OWNER_LOADBALANCERV2:
            if "fixed_ips" in port_data and port_data["fixed_ips"]:
                msg = _('Can not update Loadbalancer port with fixed IP')
                raise n_exc.InvalidInput(error_message=msg)

    def _assert_on_device_owner_change(self, port_data, orig_dev_own):
        """Prevent illegal device owner modifications
        """
        if orig_dev_own == constants.DEVICE_OWNER_LOADBALANCERV2:
            if ("allowed_address_pairs" in port_data and
                    port_data["allowed_address_pairs"]):
                msg = _('Loadbalancer port can not be updated '
                        'with address pairs')
                raise n_exc.InvalidInput(error_message=msg)

        if 'device_owner' not in port_data:
            return
        new_dev_own = port_data['device_owner']
        if new_dev_own == orig_dev_own:
            return

        err_msg = (_("Changing port device owner '%(orig)s' to '%(new)s' is "
                     "not allowed") % {'orig': orig_dev_own,
                                       'new': new_dev_own})

        # Do not allow changing nova <-> neutron device owners
        if ((orig_dev_own.startswith(constants.DEVICE_OWNER_COMPUTE_PREFIX) and
             new_dev_own.startswith(constants.DEVICE_OWNER_NETWORK_PREFIX)) or
            (orig_dev_own.startswith(constants.DEVICE_OWNER_NETWORK_PREFIX) and
             new_dev_own.startswith(constants.DEVICE_OWNER_COMPUTE_PREFIX))):
            raise n_exc.InvalidInput(error_message=err_msg)

        # Do not allow removing the device owner in some cases
        if orig_dev_own == constants.DEVICE_OWNER_DHCP:
            raise n_exc.InvalidInput(error_message=err_msg)

    def _assert_on_port_sec_change(self, port_data, device_owner):
        """Do not allow enabling port security/mac learning of some ports

        Trusted ports are created with port security and mac learning disabled
        in neutron, and it should not change.
        """
        if nl_net_utils.is_port_trusted({'device_owner': device_owner}):
            if port_data.get(psec.PORTSECURITY) is True:
                err_msg = _("port_security_enabled=True is not supported for "
                            "trusted ports")
                LOG.warning(err_msg)
                raise n_exc.InvalidInput(error_message=err_msg)

            mac_learning = port_data.get(mac_ext.MAC_LEARNING)
            if (validators.is_attr_set(mac_learning) and mac_learning is True):
                err_msg = _("mac_learning_enabled=True is not supported for "
                            "trusted ports")
                LOG.warning(err_msg)
                raise n_exc.InvalidInput(error_message=err_msg)

    def _assert_on_port_admin_state(self, port_data, device_owner):
        """Do not allow changing the admin state of some ports"""
        if (device_owner == l3_db.DEVICE_OWNER_ROUTER_INTF or
            device_owner == l3_db.DEVICE_OWNER_ROUTER_GW):
            if port_data.get("admin_state_up") is False:
                err_msg = _("admin_state_up=False router ports are not "
                            "supported")
                LOG.warning(err_msg)
                raise n_exc.InvalidInput(error_message=err_msg)

    def _validate_update_port(self, context, id, original_port, port_data):
        qos_selected = validators.is_attr_set(port_data.get
                                              (qos_consts.QOS_POLICY_ID))
        is_external_net = self._network_is_external(
            context, original_port['network_id'])
        device_owner = (port_data['device_owner']
                        if 'device_owner' in port_data
                        else original_port.get('device_owner'))

        # QoS validations
        if qos_selected:
            self._validate_qos_policy_id(
                context, port_data.get(qos_consts.QOS_POLICY_ID))
            if is_external_net:
                raise nsx_exc.QoSOnExternalNet()
            self._assert_on_illegal_port_with_qos(device_owner)

        # External networks validations:
        if is_external_net:
            self._assert_on_external_net_with_compute(port_data)

        # Device owner validations:
        orig_dev_owner = original_port.get('device_owner')
        self._assert_on_device_owner_change(port_data, orig_dev_owner)
        self._assert_on_port_admin_state(port_data, device_owner)
        self._assert_on_port_sec_change(port_data, device_owner)
        self._validate_max_ips_per_port(
            port_data.get('fixed_ips', []), device_owner)
        self._assert_on_vpn_port_change(original_port)
        self._assert_on_lb_port_fixed_ip_change(port_data, orig_dev_owner)

    def _get_dhcp_port_name(self, net_name, net_id):
        return utils.get_name_and_uuid('%s-%s' % ('dhcp',
                                                  net_name or 'network'),
                                       net_id)

    def _build_port_name(self, context, port_data):
        device_owner = port_data.get('device_owner')
        device_id = port_data.get('device_id')
        if device_owner == l3_db.DEVICE_OWNER_ROUTER_INTF and device_id:
            router = self._get_router(context, device_id)
            name = utils.get_name_and_uuid(
                router['name'] or 'router', port_data['id'], tag='port')
        elif device_owner == constants.DEVICE_OWNER_DHCP:
            network = self.get_network(context, port_data['network_id'])
            name = self._get_dhcp_port_name(network['name'],
                                            network['id'])
        elif device_owner.startswith(constants.DEVICE_OWNER_COMPUTE_PREFIX):
            name = utils.get_name_and_uuid(
                port_data['name'] or 'instance-port', port_data['id'])
        else:
            name = port_data['name']
        return name

    def _validate_external_net_create(self, net_data, default_tier0_router,
                                      tier0_validator=None):
        """Validate external network configuration

        Returns a tuple of:
        - Boolean is provider network (always True)
        - Network type (always L3_EXT)
        - tier 0 router id
        - vlan id
        """
        if not validators.is_attr_set(net_data.get(pnet.PHYSICAL_NETWORK)):
            tier0_uuid = default_tier0_router
        else:
            tier0_uuid = net_data[pnet.PHYSICAL_NETWORK]
        if ((validators.is_attr_set(net_data.get(pnet.NETWORK_TYPE)) and
             net_data.get(pnet.NETWORK_TYPE) != utils.NetworkTypes.L3_EXT and
             net_data.get(pnet.NETWORK_TYPE) != utils.NetworkTypes.LOCAL) or
            validators.is_attr_set(net_data.get(pnet.SEGMENTATION_ID))):
            msg = (_("External network cannot be created with %s provider "
                     "network or segmentation id") %
                   net_data.get(pnet.NETWORK_TYPE))
            raise n_exc.InvalidInput(error_message=msg)
        if tier0_validator:
            tier0_validator(tier0_uuid)
        return (True, utils.NetworkTypes.L3_EXT, tier0_uuid, 0)

    def _extend_network_dict_provider(self, context, network, bindings=None):
        """Add network provider fields to the network dict from the DB"""
        if 'id' not in network:
            return
        if not bindings:
            bindings = nsx_db.get_network_bindings(context.session,
                                                   network['id'])

        # With NSX plugin, "normal" overlay networks will have no binding
        if bindings:
            # Network came in through provider networks API
            network[pnet.NETWORK_TYPE] = bindings[0].binding_type
            network[pnet.PHYSICAL_NETWORK] = bindings[0].phy_uuid
            network[pnet.SEGMENTATION_ID] = bindings[0].vlan_id

    def _extend_get_network_dict_provider(self, context, network):
        self._extend_network_dict_provider(context, network)
        network[qos_consts.QOS_POLICY_ID] = (qos_com_utils.
            get_network_policy_id(context, network['id']))

    def get_network(self, context, id, fields=None):
        with db_api.CONTEXT_READER.using(context):
            # Get network from Neutron database
            network = self._get_network(context, id)
            # Don't do field selection here otherwise we won't be able to add
            # provider networks fields
            net = self._make_network_dict(network, context=context)
            self._extend_get_network_dict_provider(context, net)
        return db_utils.resource_fields(net, fields)

    def get_networks(self, context, filters=None, fields=None,
                     sorts=None, limit=None, marker=None,
                     page_reverse=False):
        # Get networks from Neutron database
        filters = filters or {}
        with db_api.CONTEXT_READER.using(context):
            networks = super(NsxPluginV3Base, self).get_networks(
                context, filters, fields, sorts,
                limit, marker, page_reverse)
            # Add provider network fields
            for net in networks:
                self._extend_get_network_dict_provider(context, net)
        return (networks if not fields else
                [db_utils.resource_fields(network,
                                          fields) for network in networks])

    def _assert_on_ens_with_qos(self, net_data):
        qos_id = net_data.get(qos_consts.QOS_POLICY_ID)
        if validators.is_attr_set(qos_id):
            err_msg = _("Cannot configure QOS on ENS networks")
            raise n_exc.InvalidInput(error_message=err_msg)

    def _ens_psec_supported(self):
        """Should be implemented by each plugin"""
        pass

    def _has_native_dhcp_metadata(self):
        """Should be implemented by each plugin"""
        pass

    def _get_nsx_net_tz_id(self, nsx_net):
        """Should be implemented by each plugin"""
        pass

    def _get_network_nsx_id(self, context, neutron_id):
        """Should be implemented by each plugin"""
        pass

    def _get_tier0_uplink_ips(self, tier0_id):
        """Should be implemented by each plugin"""
        pass

    def _validate_ens_net_portsecurity(self, net_data):
        """Validate/Update the port security of the new network for ENS TZ
        Should be implemented by the plugin if necessary
        """
        pass

    def _is_ens_tz_net(self, context, net_id):
        """Should be implemented by each plugin"""
        pass

    def _is_ens_tz_port(self, context, port_data):
        """Should be implemented by each plugin"""
        pass

    def _is_overlay_network(self, network_id):
        """Should be implemented by each plugin"""
        pass

    def _generate_segment_id(self, context, physical_network, net_data):
        bindings = nsx_db.get_network_bindings_by_phy_uuid(
            context.session, physical_network)
        vlan_ranges = self._network_vlans.get(physical_network, [])
        if vlan_ranges:
            vlan_ids = set()
            for vlan_min, vlan_max in vlan_ranges:
                vlan_ids |= set(moves.range(vlan_min, vlan_max + 1))
        else:
            vlan_min = constants.MIN_VLAN_TAG
            vlan_max = constants.MAX_VLAN_TAG
            vlan_ids = set(moves.range(vlan_min, vlan_max + 1))
        used_ids_in_range = set([binding.vlan_id for binding in bindings
                                 if binding.vlan_id in vlan_ids])
        free_ids = list(vlan_ids ^ used_ids_in_range)
        if len(free_ids) == 0:
            raise n_exc.NoNetworkAvailable()
        net_data[pnet.SEGMENTATION_ID] = free_ids[0]
        return net_data[pnet.SEGMENTATION_ID]

    def _validate_provider_create(self, context, network_data,
                                  default_vlan_tz_uuid,
                                  default_overlay_tz_uuid,
                                  nsxlib_tz, nsxlib_network,
                                  transparent_vlan=False):
        """Validate the parameters of a new provider network

        raises an error if illegal
        returns a dictionary with the relevant processed data:
        - is_provider_net: boolean
        - net_type: provider network type or None
        - physical_net: the uuid of the relevant transport zone or None
        - vlan_id: vlan tag, 0 or None
        - switch_mode: standard ot ENS
        """
        is_provider_net = any(
            validators.is_attr_set(network_data.get(f))
            for f in (pnet.NETWORK_TYPE,
                      pnet.PHYSICAL_NETWORK,
                      pnet.SEGMENTATION_ID))

        physical_net = network_data.get(pnet.PHYSICAL_NETWORK)
        if not validators.is_attr_set(physical_net):
            physical_net = None

        vlan_id = network_data.get(pnet.SEGMENTATION_ID)
        if not validators.is_attr_set(vlan_id):
            vlan_id = None

        if vlan_id and transparent_vlan:
            err_msg = (_("Segmentation ID cannot be set with transparent "
                         "vlan!"))
            raise n_exc.InvalidInput(error_message=err_msg)

        err_msg = None
        net_type = network_data.get(pnet.NETWORK_TYPE)
        tz_type = nsxlib_consts.TRANSPORT_TYPE_VLAN
        switch_mode = nsxlib_consts.HOST_SWITCH_MODE_STANDARD
        if validators.is_attr_set(net_type):
            if net_type == utils.NsxV3NetworkTypes.FLAT:
                if vlan_id is not None:
                    err_msg = (_("Segmentation ID cannot be specified with "
                                 "%s network type") %
                               utils.NsxV3NetworkTypes.FLAT)
                else:
                    if not transparent_vlan:
                        # Set VLAN id to 0 for flat networks
                        vlan_id = '0'
                    if physical_net is None:
                        physical_net = default_vlan_tz_uuid
            elif net_type == utils.NsxV3NetworkTypes.VLAN:
                # Use default VLAN transport zone if physical network not given
                if physical_net is None:
                    physical_net = default_vlan_tz_uuid

                if not transparent_vlan:
                    # Validate VLAN id
                    if not vlan_id:
                        vlan_id = self._generate_segment_id(context,
                                                            physical_net,
                                                            network_data)
                    elif not plugin_utils.is_valid_vlan_tag(vlan_id):
                        err_msg = (_('Segmentation ID %(seg_id)s out of '
                                     'range (%(min_id)s through %(max_id)s)') %
                                   {'seg_id': vlan_id,
                                    'min_id': constants.MIN_VLAN_TAG,
                                    'max_id': constants.MAX_VLAN_TAG})
                    else:
                        # Verify VLAN id is not already allocated
                        bindings = nsx_db.\
                            get_network_bindings_by_vlanid_and_physical_net(
                                context.session, vlan_id, physical_net)
                        if bindings:
                            raise n_exc.VlanIdInUse(
                                vlan_id=vlan_id, physical_network=physical_net)
            elif net_type == utils.NsxV3NetworkTypes.GENEVE:
                if vlan_id:
                    err_msg = (_("Segmentation ID cannot be specified with "
                                 "%s network type") %
                               utils.NsxV3NetworkTypes.GENEVE)
                tz_type = nsxlib_consts.TRANSPORT_TYPE_OVERLAY
            elif net_type == utils.NsxV3NetworkTypes.NSX_NETWORK:
                # Linking neutron networks to an existing NSX logical switch
                if not physical_net:
                    err_msg = (_("Physical network must be specified with "
                                 "%s network type") % net_type)
                # Validate the logical switch existence
                else:
                    try:
                        nsx_net = nsxlib_network.get(physical_net)
                        tz_id = self._get_nsx_net_tz_id(nsx_net)
                        switch_mode = nsxlib_tz.get_host_switch_mode(tz_id)
                    except nsx_lib_exc.ResourceNotFound:
                        err_msg = (_('Logical switch %s does not exist') %
                                   physical_net)
                    # make sure no other neutron network is using it
                    bindings = (
                        nsx_db.get_network_bindings_by_vlanid_and_physical_net(
                            context.elevated().session, 0, physical_net))
                    if bindings:
                        err_msg = (_('Logical switch %s is already used by '
                                     'another network') % physical_net)
            else:
                err_msg = (_('%(net_type_param)s %(net_type_value)s not '
                             'supported') %
                           {'net_type_param': pnet.NETWORK_TYPE,
                            'net_type_value': net_type})
        elif is_provider_net:
            # FIXME: Ideally provider-network attributes should be checked
            # at the NSX backend. For now, the network_type is required,
            # so the plugin can do a quick check locally.
            err_msg = (_('%s is required for creating a provider network') %
                       pnet.NETWORK_TYPE)
        else:
            net_type = None

        if physical_net is None:
            # Default to transport type overlay
            physical_net = default_overlay_tz_uuid

        # validate the transport zone existence and type
        if (not err_msg and physical_net and
            net_type != utils.NsxV3NetworkTypes.NSX_NETWORK):
            if is_provider_net:
                try:
                    backend_type = nsxlib_tz.get_transport_type(
                        physical_net)
                except nsx_lib_exc.ResourceNotFound:
                    err_msg = (_('Transport zone %s does not exist') %
                               physical_net)
                else:
                    if backend_type != tz_type:
                        err_msg = (_('%(tz)s transport zone is required for '
                                     'creating a %(net)s provider network') %
                                   {'tz': tz_type, 'net': net_type})
            if not err_msg:
                switch_mode = nsxlib_tz.get_host_switch_mode(physical_net)

        if err_msg:
            raise n_exc.InvalidInput(error_message=err_msg)

        if (switch_mode == nsxlib_consts.HOST_SWITCH_MODE_ENS):
            if not self._allow_ens_networks():
                raise NotImplementedError(_("ENS support is disabled"))
            self._assert_on_ens_with_qos(network_data)
            self._validate_ens_net_portsecurity(network_data)

        return {'is_provider_net': is_provider_net,
                'net_type': net_type,
                'physical_net': physical_net,
                'vlan_id': vlan_id,
                'switch_mode': switch_mode}

    def _network_is_nsx_net(self, context, network_id):
        bindings = nsx_db.get_network_bindings(context.session, network_id)
        if not bindings:
            return False
        return (bindings[0].binding_type ==
                utils.NsxV3NetworkTypes.NSX_NETWORK)

    def _vif_type_by_vnic_type(self, direct_vnic_type):
        return (nsx_constants.VIF_TYPE_DVS if direct_vnic_type
            else pbin.VIF_TYPE_OVS)

    def _get_network_segmentation_id(self, context, neutron_id):
        bindings = nsx_db.get_network_bindings(context.session, neutron_id)
        if bindings:
            return bindings[0].vlan_id

    def _extend_nsx_port_dict_binding(self, context, port_data):
        # Not using the register api for this because we need the context
        # Some attributes were already initialized by _extend_port_portbinding
        if pbin.VIF_TYPE not in port_data:
            port_data[pbin.VIF_TYPE] = pbin.VIF_TYPE_OVS
        if pbin.VNIC_TYPE not in port_data:
            port_data[pbin.VNIC_TYPE] = pbin.VNIC_NORMAL
        if 'network_id' in port_data:
            net_id = port_data['network_id']
            if pbin.VIF_DETAILS not in port_data:
                port_data[pbin.VIF_DETAILS] = {}
            port_data[pbin.VIF_DETAILS][pbin.OVS_HYBRID_PLUG] = False
            if (port_data.get('device_owner') ==
                constants.DEVICE_OWNER_FLOATINGIP):
                # floatingip belongs to an external net without nsx-id
                port_data[pbin.VIF_DETAILS]['nsx-logical-switch-id'] = None
            else:
                port_data[pbin.VIF_DETAILS]['nsx-logical-switch-id'] = (
                    self._get_network_nsx_id(context, net_id))
            if port_data[pbin.VNIC_TYPE] != pbin.VNIC_NORMAL:
                port_data[pbin.VIF_DETAILS]['segmentation-id'] = (
                    self._get_network_segmentation_id(context, net_id))

    def fix_direct_vnic_port_sec(self, direct_vnic_type, port_data):
        if direct_vnic_type:
            if validators.is_attr_set(port_data.get(psec.PORTSECURITY)):
                # 'direct' and 'direct-physical' vnic types ports requires
                # port-security to be disabled.
                if port_data[psec.PORTSECURITY]:
                    err_msg = _("Security features are not supported for "
                                "ports with direct/direct-physical VNIC "
                                "type")
                    raise n_exc.InvalidInput(error_message=err_msg)
            else:
                # Implicitly disable port-security for direct vnic types.
                port_data[psec.PORTSECURITY] = False

    def _validate_network_type(self, context, network_id, net_types):
        net = self.get_network(context, network_id)
        if net.get(pnet.NETWORK_TYPE) in net_types:
            return True
        return False

    def _revert_neutron_port_update(self, context, port_id,
                                    original_port, updated_port,
                                    port_security, sec_grp_updated):
        # revert the neutron port update
        super(NsxPluginV3Base, self).update_port(context, port_id,
                                                 {'port': original_port})
        # revert allowed address pairs
        if port_security:
            orig_pair = original_port.get(addr_apidef.ADDRESS_PAIRS)
            updated_pair = updated_port.get(addr_apidef.ADDRESS_PAIRS)
            if orig_pair != updated_pair:
                self._delete_allowed_address_pairs(context, port_id)
            if orig_pair:
                self._process_create_allowed_address_pairs(
                    context, original_port, orig_pair)
        # revert the security groups modifications
        if sec_grp_updated:
            self.update_security_group_on_port(
                context, port_id, {'port': original_port},
                updated_port, original_port)

    def _get_external_attachment_info(self, context, router):
        gw_port = router.gw_port
        ipaddress = None
        netmask = None
        nexthop = None

        if gw_port:
            # gw_port may have multiple IPs, only configure the first one
            if gw_port.get('fixed_ips'):
                ipaddress = gw_port['fixed_ips'][0]['ip_address']

            network_id = gw_port.get('network_id')
            if network_id:
                ext_net = self._get_network(context, network_id)
                if not ext_net.external:
                    msg = (_("Network '%s' is not a valid external "
                             "network") % network_id)
                    raise n_exc.BadRequest(resource='router', msg=msg)
                if ext_net.subnets:
                    ext_subnet = ext_net.subnets[0]
                    netmask = str(netaddr.IPNetwork(ext_subnet.cidr).netmask)
                    nexthop = ext_subnet.gateway_ip

        return (ipaddress, netmask, nexthop)

    def _validate_router_gw(self, context, router_id, info, org_enable_snat):
        # Ensure that a router cannot have SNAT disabled if there are
        # floating IP's assigned
        if (info and 'enable_snat' in info and
            org_enable_snat != info.get('enable_snat') and
            info.get('enable_snat') is False and
            self.router_gw_port_has_floating_ips(context, router_id)):
            msg = _("Unable to set SNAT disabled. Floating IPs assigned")
            raise n_exc.InvalidInput(error_message=msg)

    def _get_update_router_gw_actions(
        self,
        org_tier0_uuid, orgaddr, org_enable_snat,
        new_tier0_uuid, newaddr, new_enable_snat, lb_exist, fw_exist):
        """Return a dictionary of flags indicating which actions should be
           performed on this router GW update.
        """
        actions = {}
        # Remove router link port between tier1 and tier0 if tier0 router link
        # is removed or changed
        actions['remove_router_link_port'] = (
            org_tier0_uuid and
            (not new_tier0_uuid or org_tier0_uuid != new_tier0_uuid))

        # Remove SNAT rules for gw ip if gw ip is deleted/changed or
        # enable_snat is updated from True to False
        actions['remove_snat_rules'] = (
            org_enable_snat and orgaddr and
            (newaddr != orgaddr or not new_enable_snat))

        # Remove No-DNAT rules if GW was removed or snat was disabled
        actions['remove_no_dnat_rules'] = (
            orgaddr and org_enable_snat and
            (not newaddr or not new_enable_snat))

        # Revocate bgp announce for nonat subnets if tier0 router link is
        # changed or enable_snat is updated from False to True
        actions['revocate_bgp_announce'] = (
            not org_enable_snat and org_tier0_uuid and
            (new_tier0_uuid != org_tier0_uuid or new_enable_snat))

        # Add router link port between tier1 and tier0 if tier0 router link is
        # added or changed to a new one
        actions['add_router_link_port'] = (
            new_tier0_uuid and
            (not org_tier0_uuid or org_tier0_uuid != new_tier0_uuid))

        # Add SNAT rules for gw ip if gw ip is add/changed or
        # enable_snat is updated from False to True
        actions['add_snat_rules'] = (
            new_enable_snat and newaddr and
            (newaddr != orgaddr or not org_enable_snat))

        # Add No-DNAT rules if GW was added, and the router has SNAT enabled,
        # or if SNAT was enabled
        actions['add_no_dnat_rules'] = (
            new_enable_snat and newaddr and
            (not orgaddr or not org_enable_snat))

        # Bgp announce for nonat subnets if tier0 router link is changed or
        # enable_snat is updated from True to False
        actions['bgp_announce'] = (
            not new_enable_snat and new_tier0_uuid and
            (new_tier0_uuid != org_tier0_uuid or not org_enable_snat))

        # Advertise NAT routes if enable SNAT to support FIP. In the NoNAT
        # use case, only NSX connected routes need to be advertised.
        actions['advertise_route_nat_flag'] = (
            True if new_enable_snat else False)
        actions['advertise_route_connected_flag'] = (
            True if not new_enable_snat else False)

        # the purpose of the two vars is to be able to differ between
        # adding a gateway w/o snat and adding snat (when adding/removing gw
        # the snat option is on by default.

        real_new_enable_snat = new_enable_snat and newaddr
        real_org_enable_snat = org_enable_snat and orgaddr

        actions['add_service_router'] = ((real_new_enable_snat and
                                          not real_org_enable_snat) or
                                         (real_new_enable_snat and not
                                         orgaddr and newaddr)
                                         ) and not (fw_exist or lb_exist)
        actions['remove_service_router'] = ((not real_new_enable_snat and
                                             real_org_enable_snat) or (
                orgaddr and not newaddr)) and not (fw_exist or lb_exist)

        return actions

    def _validate_update_router_gw(self, context, router_id, gw_info):
        router_ports = self._get_router_interfaces(context, router_id)
        for port in router_ports:
            # if setting this router as no-snat, make sure gw address scope
            # match those of the subnets
            if not gw_info.get('enable_snat',
                               cfg.CONF.enable_snat_by_default):
                for fip in port['fixed_ips']:
                    self._validate_address_scope_for_router_interface(
                        context.elevated(), router_id,
                        gw_info['network_id'], fip['subnet_id'])
            # If the network attached to a router is a VLAN backed network
            # then it must be attached to an edge cluster
            if (not gw_info and
                not self._is_overlay_network(context, port['network_id'])):
                msg = _("A router attached to a VLAN backed network "
                        "must have an external network assigned")
                raise n_exc.InvalidInput(error_message=msg)

    def _validate_ext_routes(self, context, router_id, gw_info, new_routes):
        ext_net_id = (gw_info['network_id']
                      if validators.is_attr_set(gw_info) and gw_info else None)
        if not ext_net_id:
            port_filters = {'device_id': [router_id],
                            'device_owner': [l3_db.DEVICE_OWNER_ROUTER_GW]}
            gw_ports = self.get_ports(context, filters=port_filters)
            if gw_ports:
                ext_net_id = gw_ports[0]['network_id']
        if ext_net_id:
            subnets = self._get_subnets_by_network(context, ext_net_id)
            ext_cidrs = [subnet['cidr'] for subnet in subnets]
            for route in new_routes:
                if netaddr.all_matching_cidrs(
                    route['nexthop'], ext_cidrs):
                    error_message = (_("route with destination %(dest)s have "
                                       "an external nexthop %(nexthop)s which "
                                       "can't be supported") %
                                     {'dest': route['destination'],
                                      'nexthop': route['nexthop']})
                    LOG.error(error_message)
                    raise n_exc.InvalidInput(error_message=error_message)

    def _get_static_routes_diff(self, context, router_id, gw_info,
                                router_data):
        new_routes = router_data['routes']
        self._validate_ext_routes(context, router_id, gw_info,
                                  new_routes)
        self._validate_routes(context, router_id, new_routes)
        old_routes = self._get_extra_routes_by_router_id(
            context, router_id)
        routes_added, routes_removed = helpers.diff_list_of_dict(
            old_routes, new_routes)
        return routes_added, routes_removed

    def _assert_on_router_admin_state(self, router_data):
        if router_data.get("admin_state_up") is False:
            err_msg = _("admin_state_up=False routers are not supported")
            LOG.warning(err_msg)
            raise n_exc.InvalidInput(error_message=err_msg)

    def _build_dhcp_server_config(self, context, network, subnet, port, az):

        name = self.nsxlib.native_dhcp.build_server_name(
            network['name'], network['id'])

        net_tags = self.nsxlib.build_v3_tags_payload(
            network, resource_type='os-neutron-net-id',
            project_name=context.tenant_name)

        dns_domain = network.get('dns_domain')
        if not dns_domain or not validators.is_attr_set(dns_domain):
            dns_domain = az.dns_domain

        dns_nameservers = subnet['dns_nameservers']
        if not dns_nameservers or not validators.is_attr_set(dns_nameservers):
            dns_nameservers = az.nameservers

        return self.nsxlib.native_dhcp.build_server(
            name,
            ip_address=port['fixed_ips'][0]['ip_address'],
            cidr=subnet['cidr'],
            gateway_ip=subnet['gateway_ip'],
            host_routes=subnet['host_routes'],
            dns_domain=dns_domain,
            dns_nameservers=dns_nameservers,
            dhcp_profile_id=az._native_dhcp_profile_uuid,
            tags=net_tags)

    def _enable_native_dhcp(self, context, network, subnet):
        # Enable native DHCP service on the backend for this network.
        # First create a Neutron DHCP port and use its assigned IP
        # address as the DHCP server address in an API call to create a
        # LogicalDhcpServer on the backend. Then create the corresponding
        # logical port for the Neutron port with DHCP attachment as the
        # LogicalDhcpServer UUID.

        # TODO(annak):
        # This function temporarily serves both nsx_v3 and nsx_p plugins.
        # In future, when platform supports native dhcp in policy for infra
        # segments, this function should move back to nsx_v3 plugin

        # Delete obsolete settings if exist. This could happen when a
        # previous failed transaction was rolled back. But the backend
        # entries are still there.
        self._disable_native_dhcp(context, network['id'])

        # Get existing ports on subnet.
        existing_ports = super(NsxPluginV3Base, self).get_ports(
            context, filters={'network_id': [network['id']],
                              'fixed_ips': {'subnet_id': [subnet['id']]}})
        nsx_net_id = self._get_network_nsx_id(context, network['id'])
        if not nsx_net_id:
            msg = ("Unable to obtain backend network id for logical DHCP "
                   "server for network %s" % network['id'])
            LOG.error(msg)
            raise nsx_exc.NsxPluginException(err_msg=msg)

        az = self.get_network_az_by_net_id(context, network['id'])
        port_data = {
            "name": "",
            "admin_state_up": True,
            "device_id": az._native_dhcp_profile_uuid,
            "device_owner": constants.DEVICE_OWNER_DHCP,
            "network_id": network['id'],
            "tenant_id": network["tenant_id"],
            "mac_address": constants.ATTR_NOT_SPECIFIED,
            "fixed_ips": [{"subnet_id": subnet['id']}],
            psec.PORTSECURITY: False
        }
        # Create the DHCP port (on neutron only) and update its port security
        port = {'port': port_data}
        neutron_port = super(NsxPluginV3Base, self).create_port(context, port)
        is_ens_tz_port = self._is_ens_tz_port(context, port_data)
        self._create_port_preprocess_security(context, port, port_data,
                                              neutron_port, is_ens_tz_port)

        server_data = self._build_dhcp_server_config(
            context, network, subnet, neutron_port, az)
        port_tags = self.nsxlib.build_v3_tags_payload(
            neutron_port, resource_type='os-neutron-dport-id',
            project_name=context.tenant_name)
        dhcp_server = None
        dhcp_port_profiles = []
        if (not self._is_ens_tz_net(context, network['id']) and
            not self._has_native_dhcp_metadata()):
            dhcp_port_profiles.append(self._dhcp_profile)
        try:
            dhcp_server = self.nsxlib.dhcp_server.create(**server_data)
            LOG.debug("Created logical DHCP server %(server)s for network "
                      "%(network)s",
                      {'server': dhcp_server['id'], 'network': network['id']})
            name = self._build_port_name(context, port_data)
            nsx_port = self.nsxlib.logical_port.create(
                nsx_net_id, dhcp_server['id'], tags=port_tags, name=name,
                attachment_type=nsxlib_consts.ATTACHMENT_DHCP,
                switch_profile_ids=dhcp_port_profiles)
            LOG.debug("Created DHCP logical port %(port)s for "
                      "network %(network)s",
                      {'port': nsx_port['id'], 'network': network['id']})
        except nsx_lib_exc.ManagerError:
            with excutils.save_and_reraise_exception():
                LOG.error("Unable to create logical DHCP server for "
                          "network %s", network['id'])
                if dhcp_server:
                    self.nsxlib.dhcp_server.delete(dhcp_server['id'])
                super(NsxPluginV3Base, self).delete_port(
                    context, neutron_port['id'])

        try:
            # Add neutron_port_id -> nsx_port_id mapping to the DB.
            nsx_db.add_neutron_nsx_port_mapping(
                context.session, neutron_port['id'], nsx_net_id,
                nsx_port['id'])
            # Add neutron_net_id -> dhcp_service_id mapping to the DB.
            nsx_db.add_neutron_nsx_service_binding(
                context.session, network['id'], neutron_port['id'],
                nsxlib_consts.SERVICE_DHCP, dhcp_server['id'])
        except (db_exc.DBError, sql_exc.TimeoutError):
            with excutils.save_and_reraise_exception():
                LOG.error("Failed to create mapping for DHCP port %s,"
                          "deleting port and logical DHCP server",
                          neutron_port['id'])
                self.nsxlib.dhcp_server.delete(dhcp_server['id'])
                self._cleanup_port(context, neutron_port['id'], nsx_port['id'])

        # Configure existing ports to work with the new DHCP server
        try:
            for port_data in existing_ports:
                self._add_dhcp_binding(context, port_data)
        except Exception:
            LOG.error('Unable to create DHCP bindings for existing ports '
                      'on subnet %s', subnet['id'])

    def _disable_native_dhcp(self, context, network_id):
        # Disable native DHCP service on the backend for this network.
        # First delete the DHCP port in this network. Then delete the
        # corresponding LogicalDhcpServer for this network.
        dhcp_service = nsx_db.get_nsx_service_binding(
            context.session, network_id, nsxlib_consts.SERVICE_DHCP)
        if not dhcp_service:
            return

        if dhcp_service['port_id']:
            try:
                _net_id, nsx_port_id = nsx_db.get_nsx_switch_and_port_id(
                    context.session, dhcp_service['port_id'])
                self._cleanup_port(context, dhcp_service['port_id'],
                                   nsx_port_id)
            except nsx_lib_exc.ResourceNotFound:
                # This could happen when the port has been manually deleted.
                LOG.error("Failed to delete DHCP port %(port)s for "
                          "network %(network)s",
                          {'port': dhcp_service['port_id'],
                           'network': network_id})
        else:
            LOG.error("DHCP port is not configured for network %s",
                      network_id)

        try:
            self.nsxlib.dhcp_server.delete(dhcp_service['nsx_service_id'])
            LOG.debug("Deleted logical DHCP server %(server)s for network "
                      "%(network)s",
                      {'server': dhcp_service['nsx_service_id'],
                       'network': network_id})
        except nsx_lib_exc.ManagerError:
            with excutils.save_and_reraise_exception():
                LOG.error("Unable to delete logical DHCP server %(server)s "
                          "for network %(network)s",
                          {'server': dhcp_service['nsx_service_id'],
                           'network': network_id})
        try:
            # Delete neutron_id -> dhcp_service_id mapping from the DB.
            nsx_db.delete_neutron_nsx_service_binding(
                context.session, network_id, nsxlib_consts.SERVICE_DHCP)
            # Delete all DHCP bindings under this DHCP server from the DB.
            nsx_db.delete_neutron_nsx_dhcp_bindings_by_service_id(
                context.session, dhcp_service['nsx_service_id'])
        except db_exc.DBError:
            with excutils.save_and_reraise_exception():
                LOG.error("Unable to delete DHCP server mapping for "
                          "network %s", network_id)

    def _cleanup_port(self, context, port_id, nsx_port_id=None):
        # Clean up neutron port and nsx manager port if provided
        # Does not handle cleanup of policy port
        super(NsxPluginV3Base, self).delete_port(context, port_id)
        if nsx_port_id and self.nsxlib:
            self.nsxlib.logical_port.delete(nsx_port_id)

    def _is_excluded_port(self, device_owner, port_security):
        if device_owner == l3_db.DEVICE_OWNER_ROUTER_INTF:
            return False
        if device_owner == constants.DEVICE_OWNER_DHCP:
            if not self._has_native_dhcp_metadata():
                return True
        elif not port_security:
            return True
        return False

    def _validate_obj_az_on_creation(self, context, obj_data, obj_type):
        # validate the availability zone, and get the AZ object
        if az_def.AZ_HINTS in obj_data:
            self._validate_availability_zones_forced(
                context, obj_type, obj_data[az_def.AZ_HINTS])
        return self.get_obj_az_by_hints(obj_data)

    def _add_az_to_net(self, context, net_id, net_data):
        if az_def.AZ_HINTS in net_data:
            # Update the AZ hints in the neutron object
            az_hints = az_validator.convert_az_list_to_string(
                net_data[az_def.AZ_HINTS])
            super(NsxPluginV3Base, self).update_network(
                context, net_id,
                {'network': {az_def.AZ_HINTS: az_hints}})

    def _add_az_to_router(self, context, router_id, router_data):
        if az_def.AZ_HINTS in router_data:
            # Update the AZ hints in the neutron object
            az_hints = az_validator.convert_az_list_to_string(
                router_data[az_def.AZ_HINTS])
            super(NsxPluginV3Base, self).update_router(
                context, router_id,
                {'router': {az_def.AZ_HINTS: az_hints}})

    def get_network_availability_zones(self, net_db):
        if self._has_native_dhcp_metadata():
            hints = az_validator.convert_az_string_to_list(
                net_db[az_def.AZ_HINTS])
            # When using the configured AZs, the az will always be the same
            # as the hint (or default if none)
            if hints:
                az_name = hints[0]
            else:
                az_name = self.get_default_az().name
            return [az_name]
        else:
            return []

    def _get_router_az_obj(self, router):
        l3_attrs_db.ExtraAttributesMixin._extend_extra_router_dict(
            router, router)
        return self.get_router_az(router)

    def get_router_availability_zones(self, router):
        """Return availability zones which a router belongs to."""
        return [self._get_router_az_obj(router).name]

    def _validate_availability_zones_forced(self, context, resource_type,
                                            availability_zones):
        return self.validate_availability_zones(context, resource_type,
                                                availability_zones,
                                                force=True)

    def _list_availability_zones(self, context, filters=None):
        # If no native_dhcp_metadata - use neutron AZs
        if not self._has_native_dhcp_metadata():
            return super(NsxPluginV3Base, self)._list_availability_zones(
                context, filters=filters)

        result = {}
        for az in self._availability_zones_data.list_availability_zones():
            # Add this availability zone as a network & router resource
            if filters:
                if 'name' in filters and az not in filters['name']:
                    continue
            for res in ['network', 'router']:
                if 'resource' not in filters or res in filters['resource']:
                    result[(az, res)] = True
        return result

    def validate_availability_zones(self, context, resource_type,
                                    availability_zones, force=False):
        # This method is called directly from this plugin but also from
        # registered callbacks
        if self._is_sub_plugin and not force:
            # validation should be done together for both plugins
            return
        # If no native_dhcp_metadata - use neutron AZs
        if not self._has_native_dhcp_metadata():
            return super(NsxPluginV3Base, self).validate_availability_zones(
                context, resource_type, availability_zones)
        # Validate against the configured AZs
        return self.validate_obj_azs(availability_zones)

    def _create_subnet(self, context, subnet):
        self._validate_host_routes_input(subnet)

        # TODO(berlin): public external subnet announcement
        native_metadata = self._has_native_dhcp_metadata()
        if (native_metadata and subnet['subnet'].get('enable_dhcp', False)):
            self._validate_external_subnet(context,
                                           subnet['subnet']['network_id'])
            lock = 'nsxv3_network_' + subnet['subnet']['network_id']
            ddi_support, ddi_type = self._is_ddi_supported_on_net_with_type(
                context, subnet['subnet']['network_id'])
            with locking.LockManager.get_lock(lock):
                # Check if it is on an overlay network and is the first
                # DHCP-enabled subnet to create.
                if ddi_support:
                    network = self._get_network(
                        context, subnet['subnet']['network_id'])
                    if self._has_no_dhcp_enabled_subnet(context, network):
                        created_subnet = super(
                            NsxPluginV3Base, self).create_subnet(context,
                                                                 subnet)
                        try:
                            # This can be called only after the super create
                            # since we need the subnet pool to be translated
                            # to allocation pools
                            self._validate_address_space(
                                context, created_subnet)
                        except n_exc.InvalidInput:
                            # revert the subnet creation
                            with excutils.save_and_reraise_exception():
                                super(NsxPluginV3Base, self).delete_subnet(
                                    context, created_subnet['id'])
                        self._extension_manager.process_create_subnet(context,
                            subnet['subnet'], created_subnet)
                        dhcp_relay = self.get_network_az_by_net_id(
                            context,
                            subnet['subnet']['network_id']).dhcp_relay_service
                        if not dhcp_relay:
                            if self.nsxlib:
                                self._enable_native_dhcp(context, network,
                                                         created_subnet)
                            else:
                                msg = (_("Native DHCP is not supported since "
                                         "passthough API is disabled"))
                        msg = None
                    else:
                        msg = (_("Can not create more than one DHCP-enabled "
                                "subnet in network %s") %
                               subnet['subnet']['network_id'])
                else:
                    msg = _("Native DHCP is not supported for %(type)s "
                            "network %(id)s") % {
                          'id': subnet['subnet']['network_id'],
                          'type': ddi_type}
                if msg:
                    LOG.error(msg)
                    raise n_exc.InvalidInput(error_message=msg)
        else:
            created_subnet = super(NsxPluginV3Base, self).create_subnet(
                context, subnet)
            try:
                # This can be called only after the super create
                # since we need the subnet pool to be translated
                # to allocation pools
                self._validate_address_space(context, created_subnet)
            except n_exc.InvalidInput:
                # revert the subnet creation
                with excutils.save_and_reraise_exception():
                    super(NsxPluginV3Base, self).delete_subnet(
                        context, created_subnet['id'])
        return created_subnet

    def delete_subnet(self, context, subnet_id):
        # TODO(berlin): cancel public external subnet announcement
        if self._has_native_dhcp_metadata():
            # Ensure that subnet is not deleted if attached to router.
            self._subnet_check_ip_allocations_internal_router_ports(
                context, subnet_id)
            subnet = self.get_subnet(context, subnet_id)
            if subnet['enable_dhcp']:
                lock = 'nsxv3_network_' + subnet['network_id']
                with locking.LockManager.get_lock(lock):
                    # Check if it is the last DHCP-enabled subnet to delete.
                    network = self._get_network(context, subnet['network_id'])
                    if self._has_single_dhcp_enabled_subnet(context, network):
                        try:
                            self._disable_native_dhcp(context, network['id'])
                        except Exception as e:
                            LOG.error("Failed to disable native DHCP for "
                                      "network %(id)s. Exception: %(e)s",
                                      {'id': network['id'], 'e': e})
                        super(NsxPluginV3Base, self).delete_subnet(
                            context, subnet_id)
                        return
        super(NsxPluginV3Base, self).delete_subnet(context, subnet_id)

    def _is_vlan_router_interface_supported(self):
        """Should be implemented by each plugin"""

    def _is_ddi_supported_on_net_with_type(self, context, network_id):
        net = self.get_network(context, network_id)
        # NSX current does not support transparent VLAN ports for
        # DHCP and metadata
        if cfg.CONF.vlan_transparent:
            if net.get('vlan_transparent') is True:
                return False, "VLAN transparent"
        # NSX current does not support flat network ports for
        # DHCP and metadata
        if net.get(pnet.NETWORK_TYPE) == utils.NsxV3NetworkTypes.FLAT:
            return False, "flat"
        # supported for overlay networks, and for vlan networks depending on
        # NSX version
        is_overlay = self._is_overlay_network(context, network_id)
        net_type = "overlay" if is_overlay else "non-overlay"
        return (is_overlay or
                self._is_vlan_router_interface_supported()), net_type

    def _has_no_dhcp_enabled_subnet(self, context, network):
        # Check if there is no DHCP-enabled subnet in the network.
        for subnet in network.subnets:
            if subnet.enable_dhcp:
                return False
        return True

    def _has_single_dhcp_enabled_subnet(self, context, network):
        # Check if there is only one DHCP-enabled subnet in the network.
        count = 0
        for subnet in network.subnets:
            if subnet.enable_dhcp:
                count += 1
                if count > 1:
                    return False
        return True if count == 1 else False

    def _validate_address_space(self, context, subnet):
        # Only working for IPv4 at the moment
        if (subnet['ip_version'] != 4):
            return

        # get the subnet IPs
        if ('allocation_pools' in subnet and
            validators.is_attr_set(subnet['allocation_pools'])):
            # use the pools instead of the cidr
            subnet_networks = [
                netaddr.IPRange(pool.get('start'), pool.get('end'))
                for pool in subnet.get('allocation_pools')]
        else:
            cidr = subnet.get('cidr')
            if not validators.is_attr_set(cidr):
                return
            subnet_networks = [netaddr.IPNetwork(subnet['cidr'])]

        # Check if subnet overlaps with shared address space.
        # This is checked on the backend when attaching subnet to a router.
        shared_ips_cidrs = self._get_conf_attr('transit_networks')
        for subnet_net in subnet_networks:
            for shared_ips in shared_ips_cidrs:
                if netaddr.IPSet(subnet_net) & netaddr.IPSet([shared_ips]):
                    msg = _("Subnet overlaps with shared address space "
                            "%s") % shared_ips
                    LOG.error(msg)
                    raise n_exc.InvalidInput(error_message=msg)

        # Ensure that the NSX uplink does not lie on the same subnet as
        # the external subnet
        filters = {'id': [subnet['network_id']],
                   'router:external': [True]}
        external_nets = self.get_networks(context, filters=filters)
        tier0_routers = [ext_net[pnet.PHYSICAL_NETWORK]
                         for ext_net in external_nets
                         if ext_net.get(pnet.PHYSICAL_NETWORK)]

        for tier0_rtr in set(tier0_routers):
            tier0_ips = self._get_tier0_uplink_ips(tier0_rtr)
            for ip_address in tier0_ips:
                for subnet_network in subnet_networks:
                    if (netaddr.IPAddress(ip_address) in subnet_network):
                        msg = _("External subnet cannot overlap with T0 "
                                "router address %s") % ip_address
                        LOG.error(msg)
                        raise n_exc.InvalidInput(error_message=msg)

    def _need_router_snat_rules(self, context, router_id, subnet,
                                gw_address_scope):
        # if the subnets address scope is the same as the gateways:
        # no need for SNAT
        if gw_address_scope:
            subnet_address_scope = self._get_subnetpool_address_scope(
                context, subnet['subnetpool_id'])
            if (gw_address_scope == subnet_address_scope):
                LOG.info("No need for SNAT rule for router %(router)s "
                         "and subnet %(subnet)s because they use the "
                         "same address scope %(addr_scope)s.",
                         {'router': router_id,
                          'subnet': subnet['id'],
                          'addr_scope': gw_address_scope})
                return False
        return True