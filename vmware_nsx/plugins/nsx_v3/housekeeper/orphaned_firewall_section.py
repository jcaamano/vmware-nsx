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

from oslo_log import log

from vmware_nsx.extensions import projectpluginmap
from vmware_nsx.plugins.common.housekeeper import base_job
from vmware_nsx.plugins.nsx_v3 import utils as v3_utils

LOG = log.getLogger(__name__)


class OrphanedFirewallSectionJob(base_job.BaseJob):

    def __init__(self, global_readonly, readonly_jobs):
        super(OrphanedFirewallSectionJob, self).__init__(
            global_readonly, readonly_jobs)

    def get_project_plugin(self, plugin):
        return plugin.get_plugin_by_type(projectpluginmap.NsxPlugins.NSX_T)

    def get_name(self):
        return 'orphaned_firewall_section'

    def get_description(self):
        return 'Detect orphaned firewall sections'

    def run(self, context, readonly=False):
        super(OrphanedFirewallSectionJob, self).run(context)

        # get all orphaned firewall sections
        orphaned_sections = v3_utils.get_orphaned_firewall_sections(
            context, self.plugin.nsxlib)

        info = ""
        if not orphaned_sections:
            msg = 'No orphaned firewall sections detected.'
            info = base_job.housekeeper_info(info, msg)
            return {'error_count': 0, 'fixed_count': 0, 'error_info': info}

        msg = ("Found %(len)s orphaned firewall section%(plural)s:" %
               {'len': len(orphaned_sections),
                'plural': 's' if len(orphaned_sections) > 1 else ''})
        info = base_job.housekeeper_warning(info, msg)
        fixed_count = 0
        for section in orphaned_sections:
            msg = ("Firewall section %(name)s [id: %(id)s] "
                   "neutron security group: %(sg)s" %
                   {'name': section['display_name'],
                    'id': section['id'],
                    'sg': section['neutron_sg_id'] if section['neutron_sg_id']
                    else 'Unknown'})
            if not readonly:
                try:
                    self.plugin.nsxlib.firewall_section.delete(section['id'])
                except Exception as e:
                    msg = "%s failed to be removed: %s." % (msg, e)
                else:
                    fixed_count = fixed_count + 1
                    msg = "%s was removed." % msg
            info = base_job.housekeeper_warning(info, msg)

        return {'error_count': len(orphaned_sections),
                'error_info': info,
                'fixed_count': fixed_count}
