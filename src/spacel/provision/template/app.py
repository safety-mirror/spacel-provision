import json
import logging

import six

from spacel.aws import INSTANCE_VOLUMES
from spacel.model.orbit import PRIVATE_NAT_GATEWAY
from spacel.provision import base64_encode
from spacel.provision.template.base import BaseTemplateCache

SSL_SCHEMES = ('HTTPS', 'SSL')

logger = logging.getLogger('spacel.provision.template.app')


class AppTemplate(BaseTemplateCache):
    def __init__(self, ami_finder, alarm_factory, cache_factory, rds_factory,
                 spot_decorator, acm, kms_key):
        super(AppTemplate, self).__init__(ami_finder=ami_finder)
        self._alarm_factory = alarm_factory
        self._cache_factory = cache_factory
        self._rds_factory = rds_factory
        self._spot_decorator = spot_decorator
        self._acm = acm
        self._kms_key = kms_key

    def app(self, app, region):
        app_template = self.get('elb-service')

        orbit = app.orbit
        params = app_template['Parameters']
        resources = app_template['Resources']
        outputs = app_template['Outputs']

        # Link to VPC:
        params['VpcId']['Default'] = orbit.vpc_id(region)
        params['Orbit']['Default'] = orbit.name
        params['Service']['Default'] = app.name

        bastion_sg = orbit.bastion_sg(region)
        if bastion_sg:
            params['BastionSecurityGroup']['Default'] = bastion_sg
        else:
            del params['BastionSecurityGroup']
            del resources['Sg']['Properties']['SecurityGroupIngress'][0]
        cache_subnet_group = orbit.private_cache_subnet_group(region)
        if cache_subnet_group:
            params['PrivateCacheSubnetGroup']['Default'] = cache_subnet_group
        public_rds_group = orbit.public_rds_subnet_group(region)
        if public_rds_group:
            params['PublicRdsSubnetGroup']['Default'] = public_rds_group
        private_rds_group = orbit.private_rds_subnet_group(region)
        if private_rds_group:
            params['PrivateRdsSubnetGroup']['Default'] = private_rds_group

        # Inject parameters:
        params['HealthCheckTarget']['Default'] = app.health_check
        params['InstanceType']['Default'] = app.instance_type
        params['InstanceMin']['Default'] = app.instance_min
        min_in_service = app.instance_min
        if app.instance_min and app.instance_min == app.instance_max:
            min_in_service = app.instance_max - 1
        params['InstanceMinInService']['Default'] = min_in_service
        params['InstanceMax']['Default'] = app.instance_max
        params['UserData']['Default'] = self._user_data(params, app)
        params['Ami']['Default'] = self._ami.spacel_ami(region)

        if app.hostnames:
            # TODO: support multiple hostnames
            app_hostname = app.hostnames[0]
            domain = app_hostname.split('.')
            domain = '.'.join(domain[-2:]) + '.'
            params['VirtualHostDomain']['Default'] = domain
            params['VirtualHost']['Default'] = app_hostname
        elif orbit.domain:
            params['VirtualHostDomain']['Default'] = orbit.domain + '.'
            app_hostname = '%s-%s.%s' % (app.name, orbit.name, orbit.domain)
            params['VirtualHost']['Default'] = app_hostname

        if app.loadbalancer:
            params['ElbScheme']['Default'] = app.elb_availability
            # Expand ELB to all AZs:
            public_elb_subnets = orbit.public_elb_subnets(region)
            private_elb_subnets = orbit.private_elb_subnets(region)
            self._subnet_params(params, 'PublicElb', public_elb_subnets)
            self._subnet_params(params, 'PrivateElb', private_elb_subnets)

            self._elb_subnets(resources, 'PublicElb', public_elb_subnets)
            self._elb_subnets(resources, 'PrivateElb', private_elb_subnets)

        if app.elastic_ips and app.instance_max > 0:
            eip_pos = (resources['Lc']
                       ['Properties']
                       ['UserData']
                       ['Fn::Base64']
                       ['Fn::Join'][1])
            eip_pos.insert(1, '"eips":[')
            for instance_index in range(1, app.instance_max + 1):
                eip_name = 'ElasticIp%02d' % instance_index
                # Add resource, output:
                resources[eip_name] = {
                    'Type': 'AWS::EC2::EIP',
                    'Properties': {
                        'Domain': 'vpc'
                    }
                }
                outputs[eip_name] = {'Value': {'Ref': eip_name}}

                # Append to `eips` list in UserData (reverse!)
                if instance_index == 1:
                    eip_pos.insert(2, '],')
                else:
                    eip_pos.insert(2, ',')
                eip_pos.insert(2, '"')
                eip_pos.insert(2, {'Fn::GetAtt': [eip_name, 'AllocationId']})
                eip_pos.insert(2, '"')

            resources['ElasticIpPolicy'] = {
                'DependsOn': 'Role',
                'Type': 'AWS::IAM::Policy',
                'Properties': {
                    'PolicyName': 'AssociateElasticIpAddress',
                    'Roles': [{'Ref': 'Role'}],
                    'PolicyDocument': {
                        'Statement': [
                            {
                                'Effect': 'Allow',
                                'Action': [
                                    'ec2:AssociateAddress',
                                    'ec2:DescribeAddresses',
                                    'ec2:DescribeInstances'
                                ],
                                'Resource': '*'
                            }
                        ]
                    }
                }
            }

        # Expand ASG to all AZs:
        public_instance_subnets = orbit.public_instance_subnets(region)
        self._subnet_params(params, 'PublicInstance',
                            public_instance_subnets)
        private_instance_subnets = orbit.private_instance_subnets(region)
        self._subnet_params(params, 'PrivateInstance',
                            private_instance_subnets)
        if app.instance_availability == 'internet-facing' or \
                app.instance_availability == 'multi-region':
            self._asg_subnets(resources, 'PublicInstance',
                              public_instance_subnets)
            resources['Asg']['Properties']['VPCZoneIdentifier'][0] = {
                'Ref': 'PublicInstanceSubnet01'
            }
            # There is no other means of getting internet (out) otherwise!
            resources['Lc']['Properties']['AssociatePublicIpAddress'] = True
        else:
            private_nat_gateway = orbit.get_param(
                region, PRIVATE_NAT_GATEWAY) == 'enabled'
            if not private_nat_gateway:
                logger.error('"private_nat_gateway" has been disabled in' +
                             ' orbit.json, availability "private" is not' +
                             ' possible while the nat gateway has been' +
                             ' disabled!')
                return False, False

            self._asg_subnets(resources, 'PrivateInstance',
                              private_instance_subnets)

        # Public ports:
        elb_ingress = resources['ElbSg']['Properties']['SecurityGroupIngress']
        instance_ingress = resources['Sg']['Properties']['SecurityGroupIngress']
        public_elb = resources['PublicElb']['Properties']['Listeners']
        private_elb = resources['PrivateElb']['Properties']['Listeners']
        elb_ingress_ports = set()
        for port_number, port_config in sorted(app.public_ports.items()):
            if not app.loadbalancer:
                for ip_source in port_config.sources:
                    instance_ingress.append({
                        'IpProtocol': 'tcp',
                        'FromPort': port_number,
                        'ToPort': port_number,
                        'CidrIp': ip_source
                    })
            else:
                # Allow all sources into ELB:
                for ip_source in port_config.sources:
                    elb_ingress.append({
                        'IpProtocol': 'tcp',
                        'FromPort': port_number,
                        'ToPort': port_number,
                        'CidrIp': ip_source
                    })

                # Allow ELB->Instance
                internal_port = port_config.internal_port
                if internal_port not in elb_ingress_ports:
                    instance_ingress.append({
                        'IpProtocol': 'tcp',
                        'FromPort': internal_port,
                        'ToPort': internal_port,
                        'SourceSecurityGroupId': {'Ref': 'ElbSg'}
                    })
                    elb_ingress_ports.add(internal_port)

                elb_listener = {
                    'InstancePort': str(internal_port),
                    'LoadBalancerPort': port_number,
                    'Protocol': port_config.scheme,
                    'InstanceProtocol': port_config.internal_scheme
                }

                if port_config.scheme in SSL_SCHEMES:
                    cert = port_config.certificate
                    if not cert:
                        cert = self._acm.get_certificate(region, app_hostname)
                    if not cert:
                        logger.warning(
                            'Unable to find certificate for %s. ' +
                            'Specify a "certificate" or request in ACM.',
                            app_hostname)
                        raise Exception(
                            'Public_port %s is missing certificate.' %
                            port_number)
                    elb_listener['SSLCertificateId'] = cert

                public_elb.append(elb_listener)
                private_elb.append(elb_listener)

        # Private ports:
        for private_port, protocols in sorted(app.private_ports.items()):
            port_is_string = isinstance(private_port, six.string_types)
            if port_is_string and '-' in private_port:
                from_port, to_port = private_port.split('-', 1)
                port_label = private_port.replace('-', 'to')
            else:
                from_port = private_port
                to_port = private_port
                port_label = private_port

            for protocol in protocols:
                port_resource = 'PrivatePort%s%s' % (port_label, protocol)
                resources[port_resource] = {
                    'Type': 'AWS::EC2::SecurityGroupIngress',
                    'Properties': {
                        'GroupId': {'Ref': 'Sg'},
                        'IpProtocol': protocol,
                        'FromPort': str(from_port),
                        'ToPort': str(to_port),
                        'SourceSecurityGroupId': {'Ref': 'Sg'}
                    }
                }

        # Map instance storage if available:
        instance_volumes = INSTANCE_VOLUMES.get(app.instance_type, 0)
        if instance_volumes > 0:
            block_devices = resources['Lc']['Properties']['BlockDeviceMappings']
            for volume_index in range(instance_volumes):
                device = '/dev/xvd%s' % chr((ord('b') + volume_index))
                block_devices.append({
                    'DeviceName': device,
                    'VirtualName': 'ephemeral%d' % volume_index
                })

        # Clean up loadbalancer if it's unwanted
        if not app.loadbalancer:
            params['PublicElb'] = {
                'Type': 'String',
                'Default': False
            }
            params['PrivateElb'] = {
                'Type': 'String',
                'Default': False
            }
            del params['PrivateElbSubnet01'], params['PublicElbSubnet01']
            del (resources['PublicElb'], resources['PrivateElb'],
                 resources['ElbSg'],
                 resources['ElbHealthPolicy'],
                 resources['Asg']['Properties']['LoadBalancerNames'])
            resources['Asg']['Properties']['HealthCheckType'] = 'EC2'
            params['ElbScheme']['Default'] = 'disabled'

            # Resolve the DNS record
            dns_record_set = (resources['DnsRecord']['Properties']
                                       ['RecordSets'][0])
            if app.elastic_ips and app.instance_max > 0:
                del dns_record_set['AliasTarget']
                dns_record_set['TTL'] = 60
                eips = []
                for eip_index, _ in enumerate(range(app.instance_max)):
                    eip_index += 1
                    eips.append({'Ref': 'ElasticIp%02d' % eip_index})
                if eips:
                    dns_record_set['ResourceRecords'] = eips
            else:
                # No ELB and no static IPs, give up
                del resources['DnsRecord']

        self._add_kms_iam_policy(app, region, resources)
        self._alarm_factory.add_alarms(app_template, app.alarms)
        self._cache_factory.add_caches(app, region, app_template, app.caches)
        secret_params = self._rds_factory.add_rds(app, region, app_template)

        # Order matters: SpotFleet AFTER Asg/Lc have been fully configured:
        self._spot_decorator.spotify(app, region, app_template)

        return app_template, secret_params

    def _add_kms_iam_policy(self, app, region, resources):
        kms_key = self._kms_key.get_key(app, region, create=False)
        if not kms_key:
            return
        resources['KmsKeyPolicy'] = {
            'DependsOn': 'Role',
            'Type': 'AWS::IAM::Policy',
            'Properties': {
                'PolicyName': 'KmsDecrypt',
                'Roles': [{'Ref': 'Role'}],
                'PolicyDocument': {
                    'Statement': [{
                        'Effect': 'Allow',
                        'Action': 'kms:Decrypt',
                        'Resource': kms_key
                    }]
                }
            }
        }

    @staticmethod
    def _user_data(params, app):
        systemd = {}
        files = {}
        if app.services:
            for service_name, service in app.services.items():
                unit_file = service.unit_file.encode('utf-8')
                systemd[service.name] = {
                    'body': base64_encode(unit_file)
                }
                if service.environment:
                    environment_file = '\n'.join('%s=%s' % (key, value)
                                                 for key, value in
                                                 service.environment.items())
                    files['%s.env' % service_name] = {
                        'body': base64_encode(environment_file.encode('utf-8'))
                    }

        files.update(app.files)

        user_data = ''
        if systemd:
            user_data += ',"systemd":' + json.dumps(systemd, sort_keys=True)
        if files:
            user_data += ',"files":' + json.dumps(files, sort_keys=True)

        if app.volumes:
            params['VolumeSupport']['Default'] = 'true'
            user_data += ',"volumes":' + json.dumps(app.volumes, sort_keys=True)
        return user_data
