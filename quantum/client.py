# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 Citrix Systems
# All Rights Reserved.
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
#    @author: Tyler Smith, Cisco Systems

import httplib
import socket
import urllib
from quantum.common.wsgi import Serializer

class api_call(object):
    """A Decorator to add support for format and tenant overriding"""
    def __init__(self, f):
        self.f = f

    def __get__(self, instance, owner):
        def with_params(*args, **kwargs):
            # Backup the format and tenant, then temporarily change them if needed
            (format, tenant) = (instance.format, instance.tenant)

            if 'format' in kwargs:
                instance.format = kwargs['format']
            if 'tenant' in kwargs:
                instance.tenant = kwargs['tenant']

            ret = self.f(instance, *args)
            (instance.format, instance.tenant) = (format, tenant)
            return ret
        return with_params

class Client(object):

    """A base client class - derived from Glance.BaseClient"""

    action_prefix = '/v0.1/tenants/{tenant_id}'
    
    """Action query strings"""
    networks_path = "/networks"
    network_path = "/networks/%s"
    ports_path = "/networks/%s/ports"
    port_path = "/networks/%s/ports/%s"
    attachment_path = "/networks/%s/ports/%s/attachment"

    def __init__(self, host = "127.0.0.1", port = 9696, use_ssl = False,
        tenant=None, format="xml", testingStub=None, key_file=None, cert_file=None):
        """
        Creates a new client to some service.

        :param host: The host where service resides
        :param port: The port where service resides
        :param use_ssl: True to use SSL, False to use HTTP
        :param tenant: The tenant ID to make requests with
        :param format: The format to query the server with
        :param testingStub: A class that stubs basic server attributes for tests
        :param key_file: The SSL key file to use if use_ssl is true
        :param cert_file: The SSL cert file to use if use_ssl is true
        """
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.tenant = tenant
        self.format = format
        self.connection = None
        self.testingStub = testingStub
        self.key_file = key_file
        self.cert_file = cert_file

    def get_connection_type(self):
        """
        Returns the proper connection type
        """
        if self.testingStub:
            return self.testingStub
        if self.use_ssl:
            return httplib.HTTPSConnection
        else:
            return httplib.HTTPConnection

    def do_request(self, method, action, body=None,
                   headers=None, params=None):
        """
        Connects to the server and issues a request.  
        Returns the result data, or raises an appropriate exception if
        HTTP status code is not 2xx

        :param method: HTTP method ("GET", "POST", "PUT", etc...)
        :param body: string of data to send, or None (default)
        :param headers: mapping of key/value pairs to add as headers
        :param params: dictionary of key/value pairs to add to append
                             to action

        """
        
        # Ensure we have a tenant id
        if not self.tenant:
            raise Exception("Tenant ID not set")

        # Add format and tenant_id
        action += ".%s" % self.format
        action = Client.action_prefix + action
        action = action.replace('{tenant_id}',self.tenant)

        if type(params) is dict:
            action += '?' + urllib.urlencode(params)

        try:
            connection_type = self.get_connection_type()
            headers = headers or {}
            
            # Open connection and send request, handling SSL certs
            certs = {'key_file':self.key_file, 'cert_file':self.cert_file}
            certs = {x:certs[x] for x in certs if x != None}

            if self.use_ssl and len(certs):
                c = connection_type(self.host, self.port, **certs)
            else:
                c = connection_type(self.host, self.port)

            c.request(method, action, body, headers)
            res = c.getresponse()
            status_code = self.get_status_code(res)
            if status_code in (httplib.OK,
                               httplib.CREATED,
                               httplib.ACCEPTED,
                               httplib.NO_CONTENT):
                return self.deserialize(res)
            else:
                raise Exception("Server returned error: %s" % res.read())

        except (socket.error, IOError), e:
            raise Exception("Unable to connect to "
                            "server. Got error: %s" % e)

    def get_status_code(self, response):
        """
        Returns the integer status code from the response, which
        can be either a Webob.Response (used in testing) or httplib.Response
        """
        if hasattr(response, 'status_int'):
            return response.status_int
        else:
            return response.status

    def serialize(self, data):
        if type(data) is dict:
            return Serializer().serialize(data, self.content_type())

    def deserialize(self, data):
        if self.get_status_code(data) == 202:
            return data.read()
        return Serializer().deserialize(data.read(), self.content_type())

    def content_type(self, format=None):
        if not format:
            format = self.format
        return "application/%s" % (format)

    @api_call
    def list_networks(self):
        """
        Queries the server for a list of networks
        """
        return self.do_request("GET", self.networks_path)

    @api_call
    def list_network_details(self, network):
        """
        Queries the server for the details of a certain network
        """
        return self.do_request("GET", (self.network_path%network))

    @api_call
    def create_network(self, body=None):
        """
        Creates a new network on the server
        """
        body = self.serialize(body)
        return self.do_request("POST", self.networks_path, body=body)

    @api_call
    def update_network(self, network, body=None):
        """
        Updates a network on the server
        """
        body = self.serialize(body)
        return self.do_request("PUT", self.network_path % (network),body=body)

    @api_call
    def delete_network(self, network):
        """
        Deletes a network on the server
        """
        return self.do_request("DELETE", self.network_path % (network))

    @api_call
    def list_ports(self, network):
        """
        Queries the server for a list of ports on a given network
        """
        return self.do_request("GET", self.ports_path % (network))

    @api_call
    def list_port_details(self, network, port):
        """
        Queries the server for a list of ports on a given network
        """
        return self.do_request("GET", self.port_path % (network,port))

    @api_call
    def create_port(self, network):
        """
        Creates a new port on a network on the server
        """
        return self.do_request("POST", self.ports_path % (network))

    @api_call
    def delete_port(self, network, port):
        """
        Deletes a port from a network on the server
        """
        return self.do_request("DELETE", self.port_path % (network,port))

    @api_call
    def set_port_state(self, network, port, body=None):
        """
        Sets the state of a port on the server
        """
        body = self.serialize(body)
        return self.do_request("PUT",
            self.port_path % (network,port), body=body)

    @api_call
    def list_port_attachments(self, network, port):
        """
        Deletes a port from a network on the server
        """
        return self.do_request("GET", self.attachment_path % (network,port))
    
    @api_call
    def attach_resource(self, network, port, body=None):
        """
        Deletes a port from a network on the server
        """
        body = self.serialize(body)
        return self.do_request("PUT",
            self.attachment_path % (network,port), body=body)

    @api_call
    def detach_resource(self, network, port):
        """
        Deletes a port from a network on the server
        """
        return self.do_request("DELETE", self.attachment_path % (network,port))
