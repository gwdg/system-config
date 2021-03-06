#!/usr/bin/env python

# Launch a new OpenStack project infrastructure node.

# Copyright (C) 2011-2012 OpenStack LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
#
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import dns
import utils

import os_client_config
import paramiko
import shade

SCRIPT_DIR = os.path.dirname(sys.argv[0])

try:
    # This unactionable warning does not need to be printed over and over.
    import requests.packages.urllib3
    requests.packages.urllib3.disable_warnings()
except:
    pass


class JobDir(object):
    def __init__(self, keep=False):
        self.keep = keep
        self.root = tempfile.mkdtemp()
        self.inventory_root = os.path.join(self.root, 'inventory')
        os.makedirs(self.inventory_root)
        self.hosts = os.path.join(self.inventory_root, 'hosts')
        self.groups = os.path.join(self.inventory_root, 'groups')
        self.key = os.path.join(self.root, 'id_rsa')

    def __enter__(self):
        return self

    def __exit__(self, etype, value, tb):
        if not self.keep:
            shutil.rmtree(self.root)


def bootstrap_server(server, key, name, volume_device, keep,
                     mount_path, fs_label):

    ip = server.public_v4
    ssh_kwargs = dict(pkey=key)

    print 'Public IP', ip
    for username in ['root', 'ubuntu', 'centos', 'admin']:
        ssh_client = utils.ssh_connect(ip, username, ssh_kwargs, timeout=600)
        if ssh_client:
            break

    if not ssh_client:
        raise Exception("Unable to log in via SSH")

    # cloud-init puts the "please log in as user foo" message and
    # subsequent exit() in root's authorized_keys -- overwrite it with
    # a normal version to get root login working again.
    if username != 'root':
        ssh_client.ssh("sudo cp ~/.ssh/authorized_keys"
                       " ~root/.ssh/authorized_keys")
        ssh_client.ssh("sudo chmod 644 ~root/.ssh/authorized_keys")
        ssh_client.ssh("sudo chown root.root ~root/.ssh/authorized_keys")

    ssh_client = utils.ssh_connect(ip, 'root', ssh_kwargs, timeout=600)

    if server.public_v6:
        ssh_client.ssh('ping6 -c5 -Q 0x10 review.openstack.org '
                       '|| ping6 -c5 -Q 0x10 wiki.openstack.org')

    ssh_client.scp(os.path.join(SCRIPT_DIR, '..', 'make_swap.sh'),
                   'make_swap.sh')
    ssh_client.ssh('bash -x make_swap.sh')

    if volume_device:
        ssh_client.scp(os.path.join(SCRIPT_DIR, '..', 'mount_volume.sh'),
                       'mount_volume.sh')
        ssh_client.ssh('bash -x mount_volume.sh %s %s %s' %
                       (volume_device, mount_path, fs_label))

    # This next chunk should really exist as a playbook, but whatev
    ssh_client.scp(os.path.join(SCRIPT_DIR, '..', 'install_puppet.sh'),
                   'install_puppet.sh')
    ssh_client.ssh('bash -x install_puppet.sh')

    # Zero the ansible inventory cache so that next run finds the new server
    inventory_cache = '/var/cache/ansible-inventory/ansible-inventory.cache'
    if os.path.exists(inventory_cache):
        with open(inventory_cache, 'w'):
            pass

    # Update the generated-groups file globally and incorporate it
    # into our inventory
    # Remove cloud and region from the environment to work around a bug in occ
    expand_env = os.environ.copy()
    for env_key in expand_env.keys():
        if env_key.startswith('OS_'):
            expand_env.pop(env_key, None)

    # Regenerate inventory cache, throwing an error if there is an issue
    # so that we don't generate a bogus groups file
    try:
        subprocess.check_output(
            ['/etc/ansible/hosts/openstack', '--list'],
            env=expand_env)
    except subprocess.CalledProcessError as e:
        print "Inventory regeneration failed"
        print e.output
        raise

    print subprocess.check_output(
        '/usr/local/bin/expand-groups.sh',
        env=expand_env,
        stderr=subprocess.STDOUT)

    with JobDir(keep) as jobdir:
        # Write out the private SSH key we generated
        with open(jobdir.key, 'w') as key_file:
            key.write_private_key(key_file)
        os.chmod(jobdir.key, 0o600)

        # Write out inventory
        with open(jobdir.hosts, 'w') as inventory_file:
            inventory_file.write(
                "{host} ansible_host={ip} ansible_user=root".format(
                    host=name, ip=server.interface_ip))

        os.symlink('/etc/ansible/hosts/generated-groups',
                   jobdir.groups)

        ansible_cmd = [
            'ansible-playbook',
            '-i', jobdir.inventory_root, '-l', name,
            '--private-key={key}'.format(key=jobdir.key),
            "--ssh-common-args='-o StrictHostKeyChecking=no'",
            '-e', 'target={name}'.format(name=name),
        ]

        # Run the remote puppet apply playbook limited to just this server
        # we just created
        try:
            for playbook in [
                    'set_hostnames.yml',
                    'remote_puppet_adhoc.yaml']:
                print subprocess.check_output(
                    ansible_cmd + [
                        os.path.join(
                            SCRIPT_DIR, '..', 'playbooks', playbook)],
                    stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            print "Subprocess failed"
            print e.output
            raise

    try:
        ssh_client.ssh("reboot")
    except Exception as e:
        # Some init system kill the connection too fast after reboot.
        # Deal with it by ignoring ssh errors when rebooting.
        if e.rc == -1:
            pass
        else:
            raise


def build_server(cloud, name, image, flavor,
                 volume, keep, network, boot_from_volume, config_drive,
                 mount_path, fs_label, availability_zone):
    key = None
    server = None

    create_kwargs = dict(image=image, flavor=flavor, name=name,
                         reuse_ips=False, wait=True,
                         boot_from_volume=boot_from_volume,
                         network=network,
                         config_drive=config_drive)

    if availability_zone:
        create_kwargs['availability_zone'] = availability_zone

    if volume:
        create_kwargs['volumes'] = [volume]

    key_name = 'launch-%i' % (time.time())
    key = paramiko.RSAKey.generate(2048)
    public_key = key.get_name() + ' ' + key.get_base64()
    cloud.create_keypair(key_name, public_key)
    create_kwargs['key_name'] = key_name

    try:
        server = cloud.create_server(**create_kwargs)
    except Exception:
        try:
            cloud.delete_keypair(key_name)
        except Exception:
            print "Exception encountered deleting keypair:"
            traceback.print_exc()
        raise

    try:
        cloud.delete_keypair(key_name)

        server = cloud.get_openstack_vars(server)
        if volume:
            volume = cloud.get_volume(volume)
            volume_device = cloud.get_volume_attach_device(volume,
                                                           server['id'])
        else:
            volume_device = None
        bootstrap_server(server, key, name, volume_device, keep,
                         mount_path, fs_label)
        print('UUID=%s\nIPV4=%s\nIPV6=%s\n' % (
            server.id, server.public_v4, server.public_v6))
    except Exception:
        try:
            if keep:
                print "Server failed to build, keeping as requested."
            else:
                cloud.delete_server(server.id, delete_ips=True)
        except Exception:
            print "Exception encountered deleting server:"
            traceback.print_exc()
        # Raise the important exception that started this
        raise

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="server name")
    parser.add_argument("--cloud", dest="cloud", required=True,
                        help="cloud name")
    parser.add_argument("--region", dest="region",
                        help="cloud region")
    parser.add_argument("--flavor", dest="flavor", default='1GB',
                        help="name (or substring) of flavor")
    parser.add_argument("--image", dest="image",
                        default="Ubuntu 14.04 LTS (Trusty Tahr) (PVHVM)",
                        help="image name")
    parser.add_argument("--volume", dest="volume",
                        help="UUID of volume to attach to the new server.",
                        default=None)
    parser.add_argument("--mount-path", dest="mount_path",
                        help="Path to mount cinder volume at.",
                        default=None)
    parser.add_argument("--fs-label", dest="fs_label",
                        help="FS label to use when mounting cinder volume.",
                        default=None)
    parser.add_argument("--boot-from-volume", dest="boot_from_volume",
                        help="Create a boot volume for the server and use it.",
                        action='store_true',
                        default=False)
    parser.add_argument("--keep", dest="keep",
                        help="Don't clean up or delete the server on error.",
                        action='store_true',
                        default=False)
    parser.add_argument("--verbose", dest="verbose", default=False,
                        action='store_true',
                        help="Be verbose about logging cloud actions")
    parser.add_argument("--network", dest="network", default=None,
                        help="network label to attach instance to")
    parser.add_argument("--config-drive", dest="config_drive",
                        help="Boot with config_drive attached.",
                        action='store_true',
                        default=False)
    parser.add_argument("--az", dest="availability_zone", default=None,
                        help="AZ to boot in.")
    options = parser.parse_args()

    shade.simple_logging(debug=options.verbose)

    cloud_kwargs = {}
    if options.region:
        cloud_kwargs['region_name'] = options.region
    cloud_config = os_client_config.OpenStackConfig().get_one_cloud(
        options.cloud, **cloud_kwargs)

    cloud = shade.OpenStackCloud(cloud_config)

    flavor = cloud.get_flavor(options.flavor)
    if flavor:
        print "Found flavor", flavor.name
    else:
        print "Unable to find matching flavor; flavor list:"
        for i in cloud.list_flavors():
            print i.name
        sys.exit(1)

    image = cloud.get_image_exclude(options.image, 'deprecated')
    if image:
        print "Found image", image.name
    else:
        print "Unable to find matching image; image list:"
        for i in cloud.list_images():
            print i.name
        sys.exit(1)

    server = build_server(cloud, options.name, image, flavor,
                          options.volume, options.keep,
                          options.network, options.boot_from_volume,
                          options.config_drive,
                          options.mount_path, options.fs_label,
                          options.availability_zone)
    dns.print_dns(cloud, server)

if __name__ == '__main__':
    main()
