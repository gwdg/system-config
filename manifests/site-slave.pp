#
node 'slave-skvm-01' {
  include openstack_project
  class { 'openstack_project::single_use_slave':
    thin => false,
    sudo => true,
    install_users => false,
    ssh_key => $openstack_project::jenkins_ssh_key,
    project_config_repo => 'https://gitlab.xlab.si/mikelangelo/ci-config.git'
  }
}
